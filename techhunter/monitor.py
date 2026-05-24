"""Polling monitor. Iterates active subscriptions and performs discovery scans.

Separated into Fast (newest listings) and Deep (background category scan) loops.
Uses AI for evaluation and Telegram for notification.
"""
import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

from . import config, runtime
from .ai.evaluate import EvaluationReport, evaluate_listing
from .ai.normalize import normalize_device
from .db import dispose_engine
from .delivery import passes_filters
from .logging_config import setup_logging
from .notifier import ConsoleNotifier, Notifier
from .pipeline import process_new_listing
from .scraper.browser import get_browser, shutdown_browser
from .storage import (
    active_subscriptions,
    alert_already_sent,
    cache_listing,
    cache_listing_skipped,
    cleanup_old_rows,
    discovery_users,
    get_cached_listing,
    get_delivery_prefs,
    feedback_threshold_multiplier,
    delete_pending_alert,
    list_pending_alerts,
    mark_alert_sent,
    mark_pending_alert_attempt,
    mark_subscription_onboarded,
    queue_pending_alert,
    check_content_duplicate,
)
from .valuation.engine import Valuation
from .valuation.devices import (
    device_summary,
    get_or_create_device,
    log_observation,
    relearn,
)
from .valuation.scam import looks_shoplike

log = logging.getLogger("techhunter.monitor")

# Subscriptions whose onboarding task is running right now
_onboard_inprogress: set[int] = set()
_onboard_tasks: set[asyncio.Task] = set()
# Once per process: re-learn already-onboarded subs on startup
_refreshed_run: set[int] = set()


def _age_sec(dt) -> float:
    """Seconds since dt (UTC)."""
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _onboard(browser, sub, notifier: Notifier, announce: bool) -> None:
    """Deep crawl to build price baselines."""
    if browser.paused.is_set():
        log.warning("Onboarding skipped/deferred because captcha is active.")
        return
    if announce:
        with contextlib.suppress(Exception):
            await notifier.onboarding_started(sub.tg_id, sub.query, config.ONBOARDING_MAX_SEC)
    
    deadline = time.time() + config.ONBOARDING_MAX_SEC
    try:
        items = await browser.search(
            sub.query, sub.city_slug,
            min_price=sub.min_price, max_price=sub.max_price,
            pages=config.ONBOARDING_PAGES,
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
                it = await browser.fetch_details(it)
                rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
            except Exception: return
            did = await get_or_create_device(rep.brand, rep.model or "", rep.storage_gb, rep.ram_gb)
            if did is None or rep.is_sealed or looks_shoplike(
                it.title,
                it.description,
                it.seller_type,
                it.seller_listings,
                it.seller_reviews,
            ):
                return
            if await log_observation(did, rep.condition, it.price, listing_id=it.id, raw_title=it.title, storage_gb=rep.storage_gb):
                device_ids.add(did)

    await asyncio.gather(*(_collect(it) for it in cand))
    for did in device_ids:
        with contextlib.suppress(Exception):
            await relearn(did)
    
    await mark_subscription_onboarded(sub.id)
    log.info("Onboarding finished for sub %s %r", sub.id, sub.query)
    if announce:
        summary = []
        for did in device_ids:
            with contextlib.suppress(Exception):
                label, tiers = await device_summary(did)
                if tiers: summary.append((label, tiers))
        summary.sort(key=lambda x: x[0])
        with contextlib.suppress(Exception):
            await notifier.onboarding_finished(sub.tg_id, sub.query, summary)


async def _evaluate(browser, item, sem, cycle_eval, counters, mode: str = "fast", is_discovery: bool = False, deep_budget: dict | None = None):
    """Pipeline: check cache -> fast valuation -> full enrichment."""
    if item.id in cycle_eval:
        return cycle_eval[item.id]

    # Dedup re-posts
    orig_id = await check_content_duplicate(item)
    if orig_id:
        crow = await get_cached_listing(orig_id)
        if crow and _age_sec(crow["processed_at"]) <= config.LISTING_CACHE_TTL_SEC:
            rep = (EvaluationReport(**crow["report"]) if crow["report"] else None)
            val = (Valuation(**crow["valuation"]) if crow["valuation"] else None)
            counters["cached"] += 1
            cycle_eval[item.id] = (rep, val)
            return rep, val

    crow = await get_cached_listing(item.id)
    if crow and crow["price"] == (item.price or None) and _age_sec(crow["processed_at"]) <= config.LISTING_CACHE_TTL_SEC:
        rep = (EvaluationReport(**crow["report"]) if crow["report"] else None)
        val = (Valuation(**crow["valuation"]) if crow["valuation"] else None)
        counters["cached"] += 1
        cycle_eval[item.id] = (rep, val)
        return rep, val

    async with sem:
        try:
            from .valuation.engine import fast_value_listing, learn_from_card
            verdict = await fast_value_listing(item, is_discovery=is_discovery)
            if verdict == "skip":
                await cache_listing_skipped(item)
                cycle_eval[item.id] = (None, None)
                return None, None
            if verdict == "learn" and is_discovery:
                # Card-only learning: log the price from the search card
                # without opening the listing. Calm (no navigation) and fast
                # (every card contributes), so baselines grow quickly.
                if await learn_from_card(item):
                    await cache_listing_skipped(item)
                    cycle_eval[item.id] = (None, None)
                    counters["learning"] = counters.get("learning", 0) + 1
                    return None, None
            # "deal" (or a subscription "learn"): bounded deep evaluation.
            if is_discovery and deep_budget is not None:
                if deep_budget.get("n", 0) <= 0:
                    cycle_eval[item.id] = (None, None)
                    return None, None
                deep_budget["n"] -= 1
            report, valuation = await process_new_listing(browser, item, mode=mode)
        except Exception as e:
            counters["errors"] += 1
            log.warning("Pipeline failed for %s: %s", item.id, e)
            return None, None

    counters["processed"] += 1
    if valuation is None:
        await cache_listing_skipped(item)
    else:
        await cache_listing(item, report, valuation)
    cycle_eval[item.id] = (report, valuation)
    return report, valuation


async def _handle_items(browser, items, target, notifier, sem, cycle_eval, counters, runtime_drop, mode: str = "fast", deep_budget: dict | None = None):
    """Process a batch of items for a specific sub or discovery user."""
    is_sub = hasattr(target, "query")
    is_discovery = not is_sub
    tg_id = target.tg_id
    sub_query = target.query if is_sub else "🔎 Discovery"
    prefs = await get_delivery_prefs(tg_id)
    feedback_mult = await feedback_threshold_multiplier(tg_id)

    async def _handle_one(it):
        report, valuation = await _evaluate(browser, it, sem, cycle_eval, counters, mode=mode, is_discovery=is_discovery, deep_budget=deep_budget)
        if valuation is None or not valuation.opportunity:
            return
        
        if not is_sub:
            min_rub = target.discovery_min_profit_rub or config.DISCOVERY_MIN_PROFIT_RUB
            min_ratio = target.discovery_min_profit_ratio or config.DISCOVERY_MIN_PROFIT_RATIO
            if valuation.net_profit < min_rub or (valuation.net_profit / it.price) < min_ratio:
                counters["filtered"] += 1
                runtime_drop["discovery_threshold"] = (
                    runtime_drop.get("discovery_threshold", 0) + 1
                )
                return

        if feedback_mult != 1.0:
            min_rub = int(config.MIN_PROFIT_RUB * feedback_mult)
            min_ratio = config.MIN_PROFIT_RATIO * feedback_mult
            price = it.price or 1
            if (
                valuation.net_profit is None
                or valuation.net_profit < min_rub
                or (valuation.net_profit / price) < min_ratio
            ):
                counters["filtered"] += 1
                runtime_drop["feedback_threshold"] = (
                    runtime_drop.get("feedback_threshold", 0) + 1
                )
                return

        if await alert_already_sent(tg_id, it.id):
            return

        ok, reason = passes_filters(prefs, it, report, valuation)
        if not ok:
            counters["filtered"] += 1
            runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
            return

        try:
            await notifier.deal(
                it, report, valuation, tg_id=tg_id, sub_query=sub_query
            )
        except Exception as e:
            counters["errors"] += 1
            with contextlib.suppress(Exception):
                await queue_pending_alert(
                    tg_id,
                    it,
                    report,
                    valuation,
                    sub_query=sub_query,
                    error=str(e),
                )
            log.warning("Deal delivery failed for %s to %s: %s", it.id, tg_id, e)
            return

        counters["deals"] += 1
        with contextlib.suppress(Exception):
            await mark_alert_sent(
                tg_id, it.id, price=it.price, profit=valuation.net_profit,
                verdict=valuation.scam_verdict, condition=valuation.condition,
                sub_query=sub_query,
            )

    await asyncio.gather(*(_handle_one(it) for it in items))


async def poll_fast(browser, notifier: Notifier) -> None:
    """Fast loop: check the newest hot window for subs + Discovery."""
    is_paused = browser.paused.is_set()
    runtime.set_captcha(is_paused)
    if is_paused:
        log.debug("Fast poll skipped because browser is paused (solving captcha).")
        return
    if runtime.is_training_mode():
        log.info("Fast poll skipped because Training Mode is active.")
        return
    
    subs = await active_subscriptions()
    d_users = await discovery_users()
    if not subs and not d_users: return

    counters = {"scraped": 0, "processed": 0, "cached": 0, "errors": 0, "deals": 0, "filtered": 0}
    cycle_eval = {}
    runtime_drop = {}
    eval_sem = asyncio.Semaphore(config.FAST_WORKERS)

    # Subscriptions (suspended if Discovery Mode is active)
    if d_users:
        log.info("Discovery Mode active: suspending active subscriptions scans and onboarding.")
        if _onboard_tasks:
            log.info("Discovery Mode active: cancelling %d in-progress onboarding tasks.", len(_onboard_tasks))
            for t in list(_onboard_tasks):
                with contextlib.suppress(Exception):
                    t.cancel()
            _onboard_tasks.clear()
            _onboard_inprogress.clear()
    else:
        for sub in subs:
            if sub.id not in _onboard_inprogress:
                if sub.onboarded_at is None or (config.ONBOARDING_REFRESH_SEC > 0 and _age_sec(sub.onboarded_at) >= config.ONBOARDING_REFRESH_SEC):
                    _onboard_inprogress.add(sub.id)
                    t = asyncio.create_task(_onboard(browser, sub, notifier, announce=(sub.onboarded_at is None)))
                    _onboard_tasks.add(t)
                    t.add_done_callback(_onboard_tasks.discard)
                    t.add_done_callback(lambda _t, sid=sub.id: _onboard_inprogress.discard(sid))
            
            try:
                pages = max(1, sub.search_pages or config.FAST_SCAN_PAGES)
                items = await browser.search(sub.query, sub.city_slug, min_price=sub.min_price, max_price=sub.max_price, pages=pages, mode="fast")
                counters["scraped"] += len(items)
                await _handle_items(browser, items, sub, notifier, eval_sem, cycle_eval, counters, runtime_drop, mode="fast")
            except Exception as e:
                log.warning("Fast search failed sub %s: %s", sub.id, e)

    # Discovery
    if d_users:
        deep_budget = {"n": config.DISCOVERY_DEEP_PER_CYCLE}
        try:
            items = await browser.search(query="", city_slug=config.DISCOVERY_CITY_SLUG, min_price=config.DISCOVERY_MIN_PRICE, pages=config.FAST_SCAN_PAGES, mode="fast")
            counters["scraped"] += len(items)
            for u in d_users:
                await _handle_items(browser, items, u, notifier, eval_sem, cycle_eval, counters, runtime_drop, mode="fast", deep_budget=deep_budget)
        except Exception as e:
            log.warning("Fast discovery failed: %s", e)

    runtime.update_cycle(
        subs=len(subs), scraped=counters["scraped"], processed=counters["processed"],
        cached=counters["cached"], errors=counters["errors"], deals=counters["deals"],
        filtered=counters["filtered"], drop_reasons=runtime_drop,
    )


async def poll_deep(browser, notifier: Notifier) -> None:
    """Deep loop: background category scan (pages 2+)."""
    if browser.paused.is_set():
        log.debug("Deep poll skipped because browser is paused (solving captcha).")
        return
    if runtime.is_training_mode():
        log.info("Deep poll skipped because Training Mode is active.")
        return

    d_users = await discovery_users()
    if not d_users: return

    log.info("Deep Scanner: starting pages 2-%d", config.DEEP_SCAN_PAGES)
    counters = {"scraped": 0, "processed": 0, "cached": 0, "errors": 0, "deals": 0, "filtered": 0}
    cycle_eval = {}
    runtime_drop = {}
    eval_sem = asyncio.Semaphore(config.DEEP_WORKERS)
    deep_budget = {"n": config.DISCOVERY_DEEP_PER_CYCLE}

    try:
        items = await browser.search(query="", city_slug=config.DISCOVERY_CITY_SLUG, min_price=config.DISCOVERY_MIN_PRICE, pages=config.DEEP_SCAN_PAGES, mode="deep")
        counters["scraped"] += len(items)
        for u in d_users:
            await _handle_items(browser, items, u, notifier, eval_sem, cycle_eval, counters, runtime_drop, mode="deep", deep_budget=deep_budget)
    except Exception as e:
        log.warning("Deep discovery failed: %s", e)


async def _retry_pending_alerts(notifier: Notifier) -> int:
    from .scraper.models import ParsedListing

    sent = 0
    rows = await list_pending_alerts(config.DELIVERY_RETRY_BATCH_SIZE)
    for row in rows:
        tg_id = row["tg_id"]
        listing_id = row["listing_id"]
        if await alert_already_sent(tg_id, listing_id):
            await delete_pending_alert(tg_id, listing_id)
            continue
        try:
            item = ParsedListing(**row["item"])
            report = EvaluationReport(**row["report"])
            valuation = Valuation(**row["valuation"])
            await notifier.deal(
                item,
                report,
                valuation,
                tg_id=tg_id,
                sub_query=row.get("sub_query"),
            )
        except Exception as e:
            await mark_pending_alert_attempt(tg_id, listing_id, str(e))
            log.warning(
                "Pending deal delivery failed for %s to %s: %s",
                listing_id, tg_id, e,
            )
            continue
        await delete_pending_alert(tg_id, listing_id)
        sent += 1
    if sent:
        log.info("Pending deal delivery retried successfully: %d", sent)
    return sent


async def _delivery_retry_loop(notifier: Notifier) -> None:
    interval = max(10, config.DELIVERY_RETRY_INTERVAL_SEC)
    while True:
        await asyncio.sleep(interval)
        try:
            await _retry_pending_alerts(notifier)
        except Exception as e:
            log.debug("Pending deal retry tick failed: %s", e)


async def _watchdog_loop(notifier: Notifier) -> None:
    from .monitor_logic import watchdog_decide
    state: dict = {}
    while True:
        await asyncio.sleep(config.WATCHDOG_CHECK_SEC)
        try:
            for text in watchdog_decide(runtime.snapshot(), state, time.time()):
                with contextlib.suppress(Exception):
                    await notifier.watchdog(text)
        except Exception as e:
            log.debug("Watchdog tick failed: %s", e)


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL_SEC)
        try:
            deleted = await cleanup_old_rows()
            if sum(deleted.values()):
                log.info("Cleanup done: %s", deleted)
        except Exception as e:
            log.warning("Cleanup failed: %s", e)


async def _browser_refresh_loop(browser) -> None:
    """Proactively recycle Chrome so its memory does not balloon over a long
    run and drag the whole machine into swapping. The on-disk profile keeps
    the anti-bot session, so no extra captchas after the restart."""
    interval = config.BROWSER_RESTART_INTERVAL_SEC
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        if browser.paused.is_set():
            continue  # never interrupt a manual captcha solve
        try:
            log.info("Scheduled browser refresh (flushing memory).")
            await browser.restart()
        except Exception as e:
            log.warning("Scheduled browser refresh failed: %s", e)


async def run_forever(notifier: Notifier | None = None) -> None:
    notifier = notifier or ConsoleNotifier(beep=config.CAPTCHA_BEEP)
    browser = await get_browser(notifier)
    log.info("Monitor started. Workers: %d Fast, %d Deep", config.FAST_WORKERS, config.DEEP_WORKERS)
    
    async def fast_loop():
        while True:
            try:
                await poll_fast(browser, notifier)
            except Exception as e:
                log.exception("Fast loop crashed: %s", e)
            await asyncio.sleep(config.POLL_INTERVAL_SEC)

    async def deep_loop():
        # Execute once immediately on startup
        try:
            await poll_deep(browser, notifier)
        except Exception as e:
            log.exception("Deep loop crashed: %s", e)
        # Then continue with normal sleep interval
        while True:
            await asyncio.sleep(config.DEEP_SCAN_INTERVAL_SEC)
            try:
                await poll_deep(browser, notifier)
            except Exception as e:
                log.exception("Deep loop crashed: %s", e)

    async def training_loop():
        import random
        from collections import deque

        current_page = 1
        seen_order: deque[str] = deque()
        seen_ids: set[str] = set()
        
        while True:
            if not runtime.is_training_mode():
                current_page = 1
                seen_order.clear()
                seen_ids.clear()
                await asyncio.sleep(5)
                continue
            
            if browser.paused.is_set():
                log.debug("Training poll skipped because browser is paused (solving captcha).")
                await asyncio.sleep(5)
                continue
                
            try:
                max_pages = config.TRAINING_MAX_PAGES
                log.info("Training Mode: scanning page %d of %d...", current_page, max_pages)
                items = await browser.search(
                    query="",
                    city_slug="rossiya",
                    min_price=config.TRAINING_MIN_PRICE,
                    pages=1,
                    start_page=current_page,
                    mode="deep"
                )
                
                if not items:
                    log.info("Training Mode: page %d returned no items or got blocked. Wrapping or waiting...", current_page)
                    current_page = 1
                    await asyncio.sleep(15)
                    continue
                
                log.info("Training Mode: found %d items on page %d.", len(items), current_page)
                from .valuation.engine import (
                    learn_from_card_device,
                    learn_from_detail,
                    should_fetch_detail_for_learning,
                )
                card_learned = 0
                detail_learned = 0
                skipped = 0
                detail_used = 0
                device_ids: set[int] = set()
                for item in items:
                    # Let the event loop breathe between database writes
                    await asyncio.sleep(0.02)
                    if not runtime.is_training_mode():
                        break

                    if item.id in seen_ids:
                        skipped += 1
                        continue
                    seen_ids.add(item.id)
                    seen_order.append(item.id)
                    while len(seen_order) > config.TRAINING_SEEN_CACHE:
                        old = seen_order.popleft()
                        seen_ids.discard(old)

                    did = await learn_from_card_device(item, source="training")
                    if did is not None:
                        card_learned += 1
                        device_ids.add(did)
                        continue

                    if (
                        detail_used >= config.TRAINING_DETAIL_PER_PAGE
                        or not should_fetch_detail_for_learning(item)
                    ):
                        skipped += 1
                        continue

                    detail_used += 1
                    try:
                        detail_item = await browser.fetch_details(item)
                        rep = await evaluate_listing(
                            detail_item, run_clip=False, do_dedup=False
                        )
                        did = await learn_from_detail(
                            detail_item, rep, source="training_detail"
                        )
                    except Exception as e:
                        log.debug("Training detail learn failed for %s: %s", item.id, e)
                        did = None
                    if did is not None:
                        detail_learned += 1
                        device_ids.add(did)
                    else:
                        skipped += 1

                for did in device_ids:
                    with contextlib.suppress(Exception):
                        await relearn(did)
                
                processed = card_learned + detail_learned
                log.info(
                    "Training Mode: page %d finished. card=%d detail=%d skipped=%d devices=%d.",
                    current_page, card_learned, detail_learned, skipped, len(device_ids),
                )
                runtime.update_cycle(
                    subs=0,
                    scraped=len(items),
                    processed=processed,
                    cached=0,
                    errors=0,
                    deals=0,
                    filtered=skipped,
                    drop_reasons={
                        "training_detail": detail_learned,
                        "training_skipped": skipped,
                    },
                )
                
                # Increment page number
                current_page += 1
                if current_page > max_pages:
                    log.info("Training Mode: reached max page %d. Wrapping back to page 1.", max_pages)
                    current_page = 1
                
                # Delay between pages to stagger requests and let telegram bot poll smoothly
                await asyncio.sleep(random.uniform(
                    config.TRAINING_PAGE_DELAY_MIN_SEC,
                    config.TRAINING_PAGE_DELAY_MAX_SEC,
                ))
                
            except Exception as e:
                log.error("Training loop error: %s", e)
                await asyncio.sleep(10)

    tasks = [
        asyncio.create_task(_watchdog_loop(notifier)),
        asyncio.create_task(_maintenance_loop()),
        asyncio.create_task(_browser_refresh_loop(browser)),
        asyncio.create_task(_delivery_retry_loop(notifier)),
        asyncio.create_task(fast_loop()),
        asyncio.create_task(deep_loop()),
        asyncio.create_task(training_loop()),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks: t.cancel()
        await shutdown_browser()
        await dispose_engine()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")


if __name__ == "__main__":
    main()
