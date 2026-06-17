import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime, timezone

from .. import config, runtime, settings_store
from ..notifier import Notifier
from ..db.repository import RepositoryContainer
from ..ai.evaluate import EvaluationReport, evaluate_listing
from ..ai.normalize import normalize_device
from ..pipeline import process_new_listing
from ..valuation.engine import Valuation, fast_value_listing, learn_from_card
from ..valuation.devices import device_summary, get_or_create_device, log_observation, relearn
from ..valuation.scam import looks_shoplike
from ..delivery import passes_filters
from ..feedback import evaluate_personal_penalty, feedback_threshold_multiplier

log = logging.getLogger(__name__)

def _age_sec(dt) -> float:
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _new_counters() -> dict[str, int]:
    return {
        "scraped": 0,
        "processed": 0,
        "cached": 0,
        "learning": 0,
        "deals": 0,
        "queued": 0,
        "filtered": 0,
        "errors": 0,
    }


def _log_cycle(
    mode: str,
    status: str,
    *,
    subs: int,
    counters: dict[str, int],
    drop_reasons: dict,
) -> None:
    drops = ", ".join(f"{k}={v}" for k, v in sorted(drop_reasons.items()))
    log.info(
        "%s cycle %s: subs=%s scraped=%s processed=%s cached=%s "
        "learning=%s deals=%s queued=%s filtered=%s errors=%s%s",
        mode,
        status,
        subs,
        counters.get("scraped", 0),
        counters.get("processed", 0),
        counters.get("cached", 0),
        counters.get("learning", 0),
        counters.get("deals", 0),
        counters.get("queued", 0),
        counters.get("filtered", 0),
        counters.get("errors", 0),
        f" drops: {drops}" if drops else "",
    )


class AvitoPoller:
    def __init__(self, repos: RepositoryContainer, browser, notifier: Notifier):
        self.repos = repos
        self.browser = browser
        self.notifier = notifier
        self._onboard_inprogress: set[int] = set()
        self._onboard_tasks: set[asyncio.Task] = set()
        self._onboard_lock = asyncio.Lock()
        self._careful_discovery_active = False
        self._careful_discovery_next_at = 0.0

    async def _onboard(self, sub, announce: bool) -> None:
        async with self._onboard_lock:
            if self.browser.session.is_paused:
                log.warning("Onboarding skipped because browser is paused.")
                return
            if announce:
                with contextlib.suppress(Exception):
                    await self.notifier.onboarding_started(
                        sub.tg_id, sub.query, config.ONBOARDING_MAX_SEC
                    )

            await self._run_onboarding(sub, announce)

    async def _run_onboarding(self, sub, announce: bool) -> None:
        deadline = time.time() + config.ONBOARDING_MAX_SEC
        try:
            items = await self.browser.search(
                sub.query, sub.city_slug, min_price=sub.min_price,
                max_price=sub.max_price, pages=config.ONBOARDING_PAGES,
                mode="deep",
            )
        except Exception as e:
            log.warning("Onboarding search failed sub %s: %s", sub.id, e)
            return

        cand = [it for it in items if normalize_device(it.title).model is not None][:config.ONBOARDING_MAX_DETAILS]
        sem = asyncio.Semaphore(config.ONBOARDING_WORKERS)
        device_ids: set[int] = set()

        async def _collect(it):
            if time.time() > deadline:
                return
            async with sem:
                try:
                    it = await self.browser.fetch_details(it, mode="deep")
                    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
                except Exception:
                    return
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
                    if tiers:
                        summary.append((label, tiers))
            summary.sort(key=lambda x: x[0])
            with contextlib.suppress(Exception):
                await self.notifier.onboarding_finished(sub.tg_id, sub.query, summary)

    async def _evaluate(
        self,
        item,
        sem,
        cycle_eval,
        counters,
        mode: str = "fast",
        is_discovery: bool = False,
        deep_budget: dict | None = None,
        detail_delay: tuple[float, float] | None = None,
    ):
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
                if crow and crow["price"] == (item.price or None):
                    res = _apply_cache(crow)
                    if res:
                        return res

            crow = await self.repos.listings.get_cached_listing(item.id)
            if crow and crow["price"] == (item.price or None):
                res = _apply_cache(crow)
                if res:
                    return res

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

                if detail_delay is not None:
                    await asyncio.sleep(random.uniform(*detail_delay))
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

    async def _handle_items(
        self,
        items,
        target,
        sem,
        cycle_eval,
        counters,
        runtime_drop,
        mode: str = "fast",
        deep_budget: dict | None = None,
        detail_delay: tuple[float, float] | None = None,
    ):
        is_sub = hasattr(target, "query")
        is_discovery = not is_sub
        tg_id = target.tg_id
        sub_query = target.query if is_sub else "🔎 Discovery"
        prefs = await self.repos.users.get_delivery_prefs(tg_id)
        # Per-target feedback state is fetched once per cycle, not per item.
        feedback_mult = await feedback_threshold_multiplier(tg_id)
        reason_stats = await self.repos.feedback.feedback_reason_stats(tg_id)
        # Channel mirror toggle (shared in-process cache), read once per cycle.
        channel_on = settings_store.channel_enabled()

        async def _handle_one(it):
            try:
                report, valuation = await self._evaluate(
                    it,
                    sem,
                    cycle_eval,
                    counters,
                    mode=mode,
                    is_discovery=is_discovery,
                    deep_budget=deep_budget,
                    detail_delay=detail_delay,
                )
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
                    if (valuation.roi_pct or 0.0) < config.DISCOVERY_MIN_ROI_RATIO:
                        counters["filtered"] += 1
                        runtime_drop["discovery_roi"] = (
                            runtime_drop.get("discovery_roi", 0) + 1
                        )
                        return
                    if valuation.deal_score < config.DISCOVERY_MIN_DEAL_SCORE:
                        counters["filtered"] += 1
                        runtime_drop["discovery_quality"] = (
                            runtime_drop.get("discovery_quality", 0) + 1
                        )
                        return

                if feedback_mult != 1.0:
                    min_rub = int(config.MIN_PROFIT_RUB * feedback_mult)
                    min_ratio = config.MIN_PROFIT_RATIO * feedback_mult
                    if valuation.net_profit is None or valuation.net_profit < min_rub or (valuation.profit_pct or 0.0) < min_ratio:
                        counters["filtered"] += 1
                        runtime_drop["feedback_threshold"] = runtime_drop.get("feedback_threshold", 0) + 1
                        return

                personal = evaluate_personal_penalty(
                    reason_stats,
                    it,
                    report,
                    valuation,
                    target_query=target.query if is_sub else None,
                )
                if personal.get("drop"):
                    counters["filtered"] += 1
                    reason = personal.get("reason") or "feedback_personal"
                    runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
                    return

                # Content-repost guard and delivery filters are shared gates:
                # a lot that must not be delivered is not mirrored either.
                if await self.repos.alerts.content_alert_already_sent(
                    tg_id, it.get_content_hash(), it.price or None
                ):
                    counters["filtered"] += 1
                    runtime_drop["duplicate_repost"] = (
                        runtime_drop.get("duplicate_repost", 0) + 1
                    )
                    return

                ok, reason = passes_filters(prefs, it, report, valuation)
                if not ok:
                    counters["filtered"] += 1
                    runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
                    return

                # Personal delivery: skip only the personal send if it already
                # went out, but never block the channel mirror below (otherwise
                # a network blip on the channel post is lost forever).
                if not await self.repos.alerts.alert_already_sent(tg_id, it.id):
                    try:
                        await self.notifier.deal(it, report, valuation, tg_id=tg_id, sub_query=sub_query)
                    except Exception as e:
                        counters["errors"] += 1
                        with contextlib.suppress(Exception):
                            await self.repos.alerts.queue_pending_alert(tg_id, it, report, valuation, sub_query=sub_query, error=str(e))
                            counters["queued"] += 1
                    else:
                        counters["deals"] += 1
                        with contextlib.suppress(Exception):
                            await self.repos.alerts.mark_alert_sent(
                                tg_id, it.id, content_hash=it.get_content_hash(),
                                price=it.price, profit=valuation.net_profit,
                                verdict=valuation.scam_verdict, condition=valuation.condition,
                                sub_query=sub_query,
                            )

                # Mirror deals (subscriptions and Discovery) to the public demo
                # channel when enabled, independent of the personal delivery
                # state (own dedup key + own outbox).
                if channel_on:
                    await self._mirror_to_channel(it, report, valuation, sub_query)
            except Exception as e:
                counters["errors"] += 1
                log.warning("Item handling failed for %s: %s", getattr(it, "id", "?"), e)

        await asyncio.gather(*(_handle_one(it) for it in items), return_exceptions=True)

    async def _mirror_to_channel(self, it, report, valuation, sub_query) -> None:
        """Mirror a Discovery deal to the public demo channel exactly once.

        Independent of personal delivery: own dedup key and, on failure, its
        own outbox row (sub_query="channel") so a Telegram network blip on the
        channel POST is retried instead of lost forever.
        """
        if config.DEMO_CHANNEL_ID is None:
            return
        ch_key = (
            config.DEMO_CHANNEL_ID
            if isinstance(config.DEMO_CHANNEL_ID, int)
            else config.CHANNEL_DEDUP_FALLBACK_ID
        )
        try:
            if await self.repos.alerts.alert_already_sent(ch_key, it.id):
                return
            await self.notifier.deal_to_channel(
                it, report, valuation, sub_query=sub_query
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                await self.repos.alerts.queue_pending_alert(
                    ch_key, it, report, valuation,
                    sub_query="channel", error=str(e),
                )
            log.warning(
                "Channel post FAILED for %s (channel=%s), queued for retry: %s",
                getattr(it, "id", "?"), config.DEMO_CHANNEL_ID, e,
            )
            return
        with contextlib.suppress(Exception):
            await self.repos.alerts.mark_alert_sent(
                ch_key, it.id,
                content_hash=it.get_content_hash(),
                price=it.price, profit=valuation.net_profit,
                verdict=valuation.scam_verdict,
                condition=valuation.condition,
                sub_query="channel",
            )
        log.info("Channel post OK for %s -> %s", it.id, config.DEMO_CHANNEL_ID)

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
            self._careful_discovery_active = False
            self._careful_discovery_next_at = 0.0
            runtime.update_cycle(mode="fast", status="idle", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            _log_cycle("fast", "idle", subs=0, counters=_new_counters(), drop_reasons={})
            return

        fast_d_users = [
            u for u in d_users
            if getattr(u, "discovery_scan_mode", "fast") != "careful"
        ]
        careful_d_users = [
            u for u in d_users
            if getattr(u, "discovery_scan_mode", "fast") == "careful"
        ]
        careful_active = bool(careful_d_users)
        if careful_active and not self._careful_discovery_active:
            self._careful_discovery_next_at = 0.0
        self._careful_discovery_active = careful_active

        now = time.monotonic()
        careful_due = careful_active and now >= self._careful_discovery_next_at
        if careful_due:
            interval_min = max(1, config.DISCOVERY_CAREFUL_INTERVAL_MIN_SEC)
            interval_max = max(interval_min, config.DISCOVERY_CAREFUL_INTERVAL_MAX_SEC)
            self._careful_discovery_next_at = now + random.uniform(
                interval_min, interval_max
            )

        counters = _new_counters()
        cycle_eval = {}
        runtime_drop = {}
        careful_only = careful_due and not fast_d_users
        eval_sem = asyncio.Semaphore(1 if careful_only else config.FAST_WORKERS)
        cycle_status = "success"

        if d_users:
            if self._onboard_tasks:
                for t in list(self._onboard_tasks):
                    with contextlib.suppress(Exception):
                        t.cancel()
                self._onboard_tasks.clear()
                self._onboard_inprogress.clear()
        else:
            scan_plans: dict[tuple, list] = {}
            for sub in subs:
                if sub.id not in self._onboard_inprogress:
                    if sub.onboarded_at is None or (config.ONBOARDING_REFRESH_SEC > 0 and _age_sec(sub.onboarded_at) >= config.ONBOARDING_REFRESH_SEC):
                        self._onboard_inprogress.add(sub.id)
                        t = asyncio.create_task(self._onboard(sub, announce=(sub.onboarded_at is None)))
                        self._onboard_tasks.add(t)
                        t.add_done_callback(self._onboard_tasks.discard)
                        t.add_done_callback(lambda _t, sid=sub.id: self._onboard_inprogress.discard(sid))

                pages = max(1, sub.search_pages or config.FAST_SCAN_PAGES)
                key = (
                    sub.query,
                    sub.city_slug,
                    sub.min_price,
                    sub.max_price,
                    pages,
                )
                scan_plans.setdefault(key, []).append(sub)

            search_sem = asyncio.Semaphore(max(1, config.FAST_WORKERS))

            async def _search_plan(key, targets):
                query, city_slug, min_price, max_price, pages = key
                try:
                    async with search_sem:
                        items = await self.browser.search(
                            query,
                            city_slug,
                            min_price=min_price,
                            max_price=max_price,
                            pages=pages,
                            mode="fast",
                        )
                    return targets, items, None
                except Exception as e:
                    return targets, [], e

            results = await asyncio.gather(
                *(_search_plan(key, targets) for key, targets in scan_plans.items())
            )
            for targets, items, error in results:
                if error is not None:
                    counters["errors"] += 1
                    ids = ",".join(str(sub.id) for sub in targets)
                    log.warning("Fast search failed subs %s: %s", ids, error)
                    continue
                counters["scraped"] += len(items)
                for sub in targets:
                    await self._handle_items(items, sub, eval_sem, cycle_eval, counters, runtime_drop, mode="fast")

        if d_users:
            recipients = list(fast_d_users)
            if careful_due:
                recipients.extend(careful_d_users)

            if not recipients:
                cycle_status = "careful_wait"
            else:
                careful_profile = not fast_d_users
                pages = (
                    max(1, config.DISCOVERY_CAREFUL_PAGES)
                    if careful_profile
                    else config.FAST_SCAN_PAGES
                )
                deep_budget = {
                    "n": (
                        config.DISCOVERY_CAREFUL_DEEP_PER_CYCLE
                        if careful_profile
                        else config.DISCOVERY_DEEP_PER_CYCLE
                    )
                }
                detail_delay = (
                    config.DISCOVERY_CAREFUL_DETAIL_DELAY_SEC
                    if careful_profile
                    else None
                )
                settle_delay = (
                    config.DISCOVERY_CAREFUL_SETTLE_DELAY_SEC
                    if careful_profile
                    else None
                )
                try:
                    items = await self.browser.search(
                        query="",
                        city_slug=config.DISCOVERY_CITY_SLUG,
                        min_price=config.DISCOVERY_MIN_PRICE,
                        pages=pages,
                        mode="fast",
                        settle_delay=settle_delay,
                    )
                    counters["scraped"] += len(items)
                    for u in recipients:
                        await self._handle_items(
                            items,
                            u,
                            eval_sem,
                            cycle_eval,
                            counters,
                            runtime_drop,
                            mode="fast",
                            deep_budget=deep_budget,
                            detail_delay=detail_delay,
                        )
                except Exception as e:
                    counters["errors"] += 1
                    cycle_status = "error"
                    log.warning("Fast discovery failed: %s", e)

        # "subs" counts active scan targets (subscriptions + discovery
        # users) so the watchdog dry alert also works in pure Discovery mode.
        targets = len(subs) + len(d_users)
        runtime.update_cycle(
            mode="fast", status=cycle_status,
            subs=targets, scraped=counters["scraped"], processed=counters["processed"],
            cached=counters["cached"], errors=counters["errors"], deals=counters["deals"],
            filtered=counters["filtered"], drop_reasons=runtime_drop,
        )
        _log_cycle(
            "fast",
            cycle_status,
            subs=targets,
            counters=counters,
            drop_reasons=runtime_drop,
        )

    async def poll_deep(self) -> None:
        if self.browser.session.is_paused:
            log.debug("Deep poll skipped because browser is paused.")
            runtime.update_cycle(mode="deep", status="captcha", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return
        if runtime.is_training_mode():
            runtime.update_cycle(mode="deep", status="training", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            return

        all_d_users = await self.repos.users.discovery_users()
        d_users = [
            u for u in all_d_users
            if getattr(u, "discovery_scan_mode", "fast") != "careful"
        ]
        if not d_users:
            status = "careful_off" if all_d_users else "idle"
            runtime.update_cycle(mode="deep", status=status, subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            _log_cycle("deep", status, subs=0, counters=_new_counters(), drop_reasons={})
            return

        start_page = config.FAST_SCAN_PAGES + 1
        deep_pages = max(0, config.DEEP_SCAN_PAGES - config.FAST_SCAN_PAGES)
        if deep_pages <= 0:
            runtime.update_cycle(mode="deep", status="disabled", subs=0, scraped=0, processed=0, cached=0, errors=0, deals=0, filtered=0, drop_reasons={})
            _log_cycle("deep", "disabled", subs=0, counters=_new_counters(), drop_reasons={})
            return

        counters = _new_counters()
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
            subs=len(d_users), scraped=counters["scraped"], processed=counters["processed"],
            cached=counters["cached"], errors=counters["errors"], deals=counters["deals"],
            filtered=counters["filtered"], drop_reasons=runtime_drop,
        )
        _log_cycle(
            "deep",
            "success" if counters["errors"] == 0 else "error",
            subs=len(d_users),
            counters=counters,
            drop_reasons=runtime_drop,
        )
