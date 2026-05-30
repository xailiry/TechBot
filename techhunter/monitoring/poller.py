import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

from .. import config, runtime
from ..notifier import Notifier
from ..db.repository import RepositoryContainer
from ..ai.evaluate import EvaluationReport, evaluate_listing
from ..ai.normalize import normalize_device
from ..pipeline import process_new_listing
from ..valuation.engine import Valuation, fast_value_listing, learn_from_card
from ..valuation.devices import device_summary, get_or_create_device, log_observation, relearn
from ..valuation.scam import looks_shoplike
from ..delivery import passes_filters

log = logging.getLogger(__name__)

def _age_sec(dt) -> float:
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


class AvitoPoller:
    def __init__(self, repos: RepositoryContainer, browser, notifier: Notifier):
        self.repos = repos
        self.browser = browser
        self.notifier = notifier
        self._onboard_inprogress: set[int] = set()
        self._onboard_tasks: set[asyncio.Task] = set()

    async def _onboard(self, sub, announce: bool) -> None:
        if self.browser.session.is_paused:
            log.warning("Onboarding skipped because browser is paused.")
            return
        if announce:
            with contextlib.suppress(Exception):
                await self.notifier.onboarding_started(sub.tg_id, sub.query, config.ONBOARDING_MAX_SEC)
        
        deadline = time.time() + config.ONBOARDING_MAX_SEC
        try:
            items = await self.browser.search(
                sub.query, sub.city_slug, min_price=sub.min_price,
                max_price=sub.max_price, pages=config.ONBOARDING_PAGES
            )
        except Exception as e:
            log.warning("Onboarding search failed sub %s: %s", sub.id, e)
            return

        cand = [it for it in items if normalize_device(it.title).model is not None][:config.ONBOARDING_MAX_DETAILS]
        sem = asyncio.Semaphore(config.MONITOR_CONCURRENCY)
        device_ids: set[int] = set()

        async def _collect(it):
            if time.time() > deadline: return
            async with sem:
                try:
                    it = await self.browser.fetch_details(it)
                    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
                except Exception: return
                did = await get_or_create_device(rep.brand, rep.model or "", rep.storage_gb, rep.ram_gb)
                if did is None or rep.is_sealed or looks_shoplike(it.title, it.description, it.seller_type, it.seller_listings, it.seller_reviews):
                    return
                if await log_observation(did, rep.condition, it.price, listing_id=it.id, raw_title=it.title, storage_gb=rep.storage_gb):
                    device_ids.add(did)

        await asyncio.gather(*(_collect(it) for it in cand))
        for did in device_ids:
            with contextlib.suppress(Exception):
                await relearn(did)
        
        if not device_ids:
            log.info("Onboarding deferred for sub %s: no useful observations.", sub.id)
            return

        await self.repos.subscriptions.mark_subscription_onboarded(sub.id)
        log.info("Onboarding finished for sub %s", sub.id)
        if announce:
            summary = []
            for did in device_ids:
                with contextlib.suppress(Exception):
                    label, tiers = await device_summary(did)
                    if tiers: summary.append((label, tiers))
            summary.sort(key=lambda x: x[0])
            with contextlib.suppress(Exception):
                await self.notifier.onboarding_finished(sub.tg_id, sub.query, summary)

    async def _evaluate(self, item, sem, cycle_eval, counters, mode: str = "fast", is_discovery: bool = False, deep_budget: dict | None = None):
        if item.id in cycle_eval:
            return cycle_eval[item.id]

        async with sem:
            if item.id in cycle_eval:
                return cycle_eval[item.id]

            def _apply_cache(c_row):
                if c_row and _age_sec(c_row["processed_at"]) <= config.LISTING_CACHE_TTL_SEC:
                    r = EvaluationReport(**c_row["report"]) if c_row["report"] else None
                    v = Valuation(**c_row["valuation"]) if c_row["valuation"] else None
                    counters["cached"] += 1
                    cycle_eval[item.id] = (r, v)
                    return r, v
                return None

            orig_id = await self.repos.listings.check_content_duplicate(item)
            if orig_id:
                crow = await self.repos.listings.get_cached_listing(orig_id)
                res = _apply_cache(crow)
                if res: return res

            crow = await self.repos.listings.get_cached_listing(item.id)
            if crow and crow["price"] == (item.price or None):
                res = _apply_cache(crow)
                if res: return res

            try:
                verdict = await fast_value_listing(item, is_discovery=is_discovery)
                if verdict == "skip":
                    await self.repos.listings.cache_listing_skipped(item)
                    cycle_eval[item.id] = (None, None)
                    return None, None
                if verdict == "learn" and is_discovery:
                    if await learn_from_card(item):
                        await self.repos.listings.cache_listing_skipped(item)
                        cycle_eval[item.id] = (None, None)
                        counters["learning"] = counters.get("learning", 0) + 1
                        return None, None
                
                if is_discovery and deep_budget is not None:
                    if deep_budget.get("n", 0) <= 0:
                        cycle_eval[item.id] = (None, None)
                        return None, None
                    deep_budget["n"] -= 1

                report, valuation = await process_new_listing(self.browser, item, mode=mode)
            except Exception as e:
                counters["errors"] += 1
                log.warning("Pipeline failed for %s: %s", item.id, e)
                return None, None

            counters["processed"] += 1
            if valuation is None:
                await self.repos.listings.cache_listing_skipped(item)
            else:
                await self.repos.listings.cache_listing(item, report, valuation)
            cycle_eval[item.id] = (report, valuation)
            return report, valuation

    async def _handle_items(self, items, target, sem, cycle_eval, counters, runtime_drop, mode: str = "fast", deep_budget: dict | None = None):
        is_sub = hasattr(target, "query")
        is_discovery = not is_sub
        tg_id = target.tg_id
        sub_query = target.query if is_sub else "🔎 Discovery"
        prefs = await self.repos.users.get_delivery_prefs(tg_id)
        
        # In a real setup, feedback methods would be in a service layer. For now, we call storage or repo.
        from ..storage import feedback_threshold_multiplier, feedback_personal_penalty
        feedback_mult = await feedback_threshold_multiplier(tg_id)

        async def _handle_one(it):
            try:
                report, valuation = await self._evaluate(it, sem, cycle_eval, counters, mode=mode, is_discovery=is_discovery, deep_budget=deep_budget)
                if valuation is None or not valuation.opportunity:
                    return

                if is_sub and getattr(target, "min_battery", None) and report.battery_health is not None:
                    if report.battery_health < target.min_battery:
                        counters["filtered"] += 1
                        runtime_drop["low_battery"] = runtime_drop.get("low_battery", 0) + 1
                        return

                if not is_sub:
                    min_rub = target.discovery_min_profit_rub or config.DISCOVERY_MIN_PROFIT_RUB
                    min_ratio = target.discovery_min_profit_ratio or config.DISCOVERY_MIN_PROFIT_RATIO
                    if valuation.net_profit < min_rub or (valuation.profit_pct or 0.0) < min_ratio:
                        counters["filtered"] += 1
                        runtime_drop["discovery_threshold"] = runtime_drop.get("discovery_threshold", 0) + 1
                        return

                if feedback_mult != 1.0:
                    min_rub = int(config.MIN_PROFIT_RUB * feedback_mult)
                    min_ratio = config.MIN_PROFIT_RATIO * feedback_mult
                    if valuation.net_profit is None or valuation.net_profit < min_rub or (valuation.profit_pct or 0.0) < min_ratio:
                        counters["filtered"] += 1
                        runtime_drop["feedback_threshold"] = runtime_drop.get("feedback_threshold", 0) + 1
                        return

                personal = await feedback_personal_penalty(tg_id, it, report, valuation)
                if personal.get("drop"):
                    counters["filtered"] += 1
                    reason = personal.get("reason") or "feedback_personal"
                    runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
                    return

                if await self.repos.alerts.alert_already_sent(tg_id, it.id):
                    return

                ok, reason = passes_filters(prefs, it, report, valuation)
                if not ok:
                    counters["filtered"] += 1
                    runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
                    return

                try:
                    await self.notifier.deal(it, report, valuation, tg_id=tg_id, sub_query=sub_query)
                except Exception as e:
                    counters["errors"] += 1
                    with contextlib.suppress(Exception):
                        await self.repos.alerts.queue_pending_alert(tg_id, it, report, valuation, sub_query=sub_query, error=str(e))
                    return

                counters["deals"] += 1
                with contextlib.suppress(Exception):
                    await self.repos.alerts.mark_alert_sent(
                        tg_id, it.id, price=it.price, profit=valuation.net_profit,
                        verdict=valuation.scam_verdict, condition=valuation.condition,
                        sub_query=sub_query,
                    )
            except Exception as e:
                counters["errors"] += 1
                log.warning("Item handling failed for %s: %s", getattr(it, "id", "?"), e)

        await asyncio.gather(*(_handle_one(it) for it in items), return_exceptions=True)

    async def poll_fast(self) -> None:
        if self.browser.session.is_paused:
            log.debug("Fast poll skipped because browser is paused.")
            runtime.update_cycle(mode="fast", status="captcha", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return
        if runtime.is_training_mode():
            runtime.update_cycle(mode="fast", status="training", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return
        
        subs = await self.repos.subscriptions.active_subscriptions()
        d_users = await self.repos.users.discovery_users()
        if not subs and not d_users:
            runtime.update_cycle(mode="fast", status="idle", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return

        counters = {"scraped": 0, "processed": 0, "cached": 0, "errors": 0, "deals": 0, "filtered": 0}
        cycle_eval = {}
        runtime_drop = {}
        eval_sem = asyncio.Semaphore(config.FAST_WORKERS)

        if d_users:
            if self._onboard_tasks:
                for t in list(self._onboard_tasks):
                    with contextlib.suppress(Exception): t.cancel()
                self._onboard_tasks.clear()
                self._onboard_inprogress.clear()
        else:
            for sub in subs:
                if sub.id not in self._onboard_inprogress:
                    if sub.onboarded_at is None or (config.ONBOARDING_REFRESH_SEC > 0 and _age_sec(sub.onboarded_at) >= config.ONBOARDING_REFRESH_SEC):
                        self._onboard_inprogress.add(sub.id)
                        t = asyncio.create_task(self._onboard(sub, announce=(sub.onboarded_at is None)))
                        self._onboard_tasks.add(t)
                        t.add_done_callback(self._onboard_tasks.discard)
                        t.add_done_callback(lambda _t, sid=sub.id: self._onboard_inprogress.discard(sid))
                
                try:
                    pages = max(1, sub.search_pages or config.FAST_SCAN_PAGES)
                    items = await self.browser.search(sub.query, sub.city_slug, min_price=sub.min_price, max_price=sub.max_price, pages=pages, mode="fast")
                    counters["scraped"] += len(items)
                    await self._handle_items(items, sub, eval_sem, cycle_eval, counters, runtime_drop, mode="fast")
                except Exception as e:
                    log.warning("Fast search failed sub %s: %s", sub.id, e)

        if d_users:
            deep_budget = {"n": config.DISCOVERY_DEEP_PER_CYCLE}
            try:
                items = await self.browser.search(query="", city_slug=config.DISCOVERY_CITY_SLUG, min_price=config.DISCOVERY_MIN_PRICE, pages=config.FAST_SCAN_PAGES, mode="fast")
                counters["scraped"] += len(items)
                for u in d_users:
                    await self._handle_items(items, u, eval_sem, cycle_eval, counters, runtime_drop, mode="fast", deep_budget=deep_budget)
            except Exception as e:
                log.warning("Fast discovery failed: %s", e)

        runtime.update_cycle(
            mode="fast", status="success",
            subs=len(subs), scraped=counters["scraped"], processed=counters["processed"],
            cached=counters["cached"], errors=counters["errors"], deals=counters["deals"],
            filtered=counters["filtered"], drop_reasons=runtime_drop,
        )

    async def poll_deep(self) -> None:
        if self.browser.session.is_paused:
            log.debug("Deep poll skipped because browser is paused.")
            runtime.update_cycle(mode="deep", status="captcha", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return
        if runtime.is_training_mode():
            runtime.update_cycle(mode="deep", status="training", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return

        d_users = await self.repos.users.discovery_users()
        if not d_users:
            runtime.update_cycle(mode="deep", status="idle", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return

        start_page = config.FAST_SCAN_PAGES + 1
        deep_pages = max(0, config.DEEP_SCAN_PAGES - config.FAST_SCAN_PAGES)
        if deep_pages <= 0:
            runtime.update_cycle(mode="deep", status="disabled", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return

        counters = {"scraped": 0, "processed": 0, "cached": 0, "errors": 0, "deals": 0, "filtered": 0}
        cycle_eval = {}
        runtime_drop = {}
        eval_sem = asyncio.Semaphore(config.DEEP_WORKERS)
        deep_budget = {"n": config.DISCOVERY_DEEP_PER_CYCLE}

        try:
            items = await self.browser.search(
                query="", city_slug=config.DISCOVERY_CITY_SLUG,
                min_price=config.DISCOVERY_MIN_PRICE, pages=deep_pages,
                start_page=start_page, mode="deep",
            )
            counters["scraped"] += len(items)
            for u in d_users:
                await self._handle_items(items, u, eval_sem, cycle_eval, counters, runtime_drop, mode="deep", deep_budget=deep_budget)
        except Exception as e:
            counters["errors"] += 1
            log.warning("Deep discovery failed: %s", e)
            
        runtime.update_cycle(
            mode="deep", status="success" if counters["errors"] == 0 else "error",
            subs=0, scraped=counters["scraped"], processed=counters["processed"],
            cached=counters["cached"], errors=counters["errors"], deals=counters["deals"],
            filtered=counters["filtered"], drop_reasons=runtime_drop,
        )
