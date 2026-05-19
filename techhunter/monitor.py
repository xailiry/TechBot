"""Polling monitor. Iterates active subscriptions, scrapes newest-first Avito
pages, deduplicates, runs the evaluate+value pipeline, and notifies.

The Telegram card UX (Stage 4) plugs in behind the same Notifier.
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
    get_cached_listing,
    get_delivery_prefs,
    mark_alert_sent,
    mark_subscription_onboarded,
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

# Subscriptions whose onboarding task is running right now (avoid duplicate
# concurrent crawls). Completion is persisted in DB (Subscription.onboarded_at).
_onboard_inprogress: set[int] = set()
_onboard_tasks: set[asyncio.Task] = set()
# Once per process: re-learn already-onboarded subs so estimator/code
# changes take effect right after a restart (no waiting for the timer).
_refreshed_run: set[int] = set()


def _age_sec(dt) -> float:
    """Seconds since dt. SQLite returns naive datetimes (UTC) -> assume UTC."""
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _onboard(browser, sub, notifier: Notifier, announce: bool) -> None:
    """Deep crawl: collect many real listings with condition/price to build
    per-condition baselines. First time per subscription it announces; the
    periodic silent refresh keeps medians fresh from a broad sample."""
    if announce:
        with contextlib.suppress(Exception):
            await notifier.onboarding_started(
                sub.tg_id, sub.query, config.ONBOARDING_MAX_SEC
            )
    deadline = time.time() + config.ONBOARDING_MAX_SEC
    try:
        items = await browser.search(
            sub.query, sub.city_slug,
            min_price=sub.min_price, max_price=sub.max_price,
            pages=config.ONBOARDING_PAGES,
        )
    except Exception as e:
        # Do NOT persist onboarded: a failed crawl should be retried.
        log.warning("onboarding search failed sub %s: %s", sub.id, e)
        if announce:
            with contextlib.suppress(Exception):
                await notifier.onboarding_finished(sub.tg_id, sub.query, [])
        return

    cand = []
    for it in items:
        if normalize_device(it.title).model is not None:
            cand.append(it)
        if len(cand) >= config.ONBOARDING_MAX_DETAILS:
            break

    sem = asyncio.Semaphore(config.MONITOR_CONCURRENCY)
    device_ids: set[int] = set()

    async def _collect(it):
        if time.time() > deadline:
            return
        async with sem:
            try:
                it = await browser.fetch_details(it)
                rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
            except Exception:
                return
            did = await get_or_create_device(
                rep.brand, rep.model or "", rep.storage_gb, rep.ram_gb
            )
            if did is None or rep.is_sealed or looks_shoplike(
                it.title, it.description, it.seller_type, it.seller_listings
            ):
                return
            if await log_observation(
                did, rep.condition, it.price,
                listing_id=it.id, raw_title=it.title,
                storage_gb=rep.storage_gb,
            ):
                device_ids.add(did)

    await asyncio.gather(*(_collect(it) for it in cand))
    summary = []
    for did in device_ids:
        with contextlib.suppress(Exception):
            await relearn(did)
            label, tiers = await device_summary(did)
            if tiers:
                summary.append((label, tiers))
    # Persist completion so a restart does not re-run onboarding; the
    # timestamp also schedules the next silent refresh.
    with contextlib.suppress(Exception):
        await mark_subscription_onboarded(sub.id)
    log.info("onboarding %s sub %s %r: %d devices learned",
             "done" if announce else "refreshed",
             sub.id, sub.query, len(device_ids))
    if announce:
        summary.sort(key=lambda x: x[0])
        with contextlib.suppress(Exception):
            await notifier.onboarding_finished(sub.tg_id, sub.query, summary)


async def poll_once(browser, notifier: Notifier) -> None:
    subs = await active_subscriptions()
    runtime.set_captcha(browser.paused.is_set())
    if not subs:
        log.debug("No active subscriptions.")
        runtime.update_cycle(
            subs=0, scraped=0, processed=0, cached=0, errors=0,
            deals=0, filtered=0, drop_reasons={},
        )
        return
    tot_scraped = tot_processed = tot_cached = tot_errors = 0
    tot_deals = tot_filtered = 0
    # Eval cache for THIS poll: a listing shared by several subscriptions
    # is evaluated once. drop_reasons aggregates why deals were withheld.
    cycle_eval: dict[str, tuple] = {}
    runtime_drop: dict[str, int] = {}
    for sub in subs:
        if sub.id not in _onboard_inprogress:
            first = sub.onboarded_at is None
            startup = not first and sub.id not in _refreshed_run
            refresh = startup or (
                not first
                and config.ONBOARDING_REFRESH_SEC > 0
                and _age_sec(sub.onboarded_at) >= config.ONBOARDING_REFRESH_SEC
            )
            if not first:
                _refreshed_run.add(sub.id)
            if first or refresh:
                _onboard_inprogress.add(sub.id)
                t = asyncio.create_task(
                    _onboard(browser, sub, notifier, announce=first)
                )
                _onboard_tasks.add(t)
                t.add_done_callback(_onboard_tasks.discard)
                t.add_done_callback(
                    lambda _t, sid=sub.id: _onboard_inprogress.discard(sid)
                )
                log.info(
                    "onboarding %s for sub %s %r",
                    "started" if first else "refresh", sub.id, sub.query,
                )
        pages = sub.search_pages or config.DEFAULT_SEARCH_PAGES
        try:
            items = await browser.search(
                sub.query,
                sub.city_slug,
                min_price=sub.min_price,
                max_price=sub.max_price,
                pages=pages,
            )
        except Exception as e:
            log.exception("search failed for sub %s (%r): %s",
                          sub.id, sub.query, e)
            continue

        prefs = await get_delivery_prefs(sub.tg_id)
        sem = asyncio.Semaphore(config.MONITOR_CONCURRENCY)
        deals = filtered = processed = cached = errors = 0

        async def _evaluate(item):
            """Get (report, valuation) for a listing: reuse a fresh cache,
            otherwise run the pipeline. On transient failure: NOT cached
            -> retried next cycle (a lot is never lost)."""
            nonlocal processed, cached, errors
            if item.id in cycle_eval:
                return cycle_eval[item.id]
            crow = await get_cached_listing(item.id)
            if (
                crow is not None
                and crow["price"] == (item.price or None)
                and _age_sec(crow["processed_at"])
                <= config.LISTING_CACHE_TTL_SEC
            ):
                rep = (EvaluationReport(**crow["report"])
                       if crow["report"] else None)
                val = (Valuation(**crow["valuation"])
                       if crow["valuation"] else None)
                cached += 1
                cycle_eval[item.id] = (rep, val)
                return rep, val
            async with sem:
                try:
                    report, valuation = await process_new_listing(
                        browser, item
                    )
                except Exception as e:
                    errors += 1
                    log.warning("pipeline failed for %s (retry next): %s",
                                item.id, e)
                    cycle_eval[item.id] = (None, None)
                    return None, None
            processed += 1
            if valuation is None:
                await cache_listing_skipped(item)
            else:
                await cache_listing(item, report, valuation)
            cycle_eval[item.id] = (report, valuation)
            return report, valuation

        async def _handle(item):
            nonlocal deals, filtered
            report, valuation = await _evaluate(item)
            if valuation is None or not valuation.opportunity:
                return
            # Per-user delivery dedup: another sub/user processing this
            # listing must NOT consume it for this user.
            if await alert_already_sent(sub.tg_id, item.id):
                return
            ok, reason = passes_filters(prefs, item, report, valuation)
            if not ok:
                filtered += 1
                runtime_drop[reason] = runtime_drop.get(reason, 0) + 1
                return
            deals += 1
            with contextlib.suppress(Exception):
                await notifier.deal(
                    item, report, valuation,
                    tg_id=sub.tg_id, sub_query=sub.query,
                )
            # Mark delivered regardless of notifier type so the per-user
            # dedup holds across cycles (idempotent insert).
            with contextlib.suppress(Exception):
                await mark_alert_sent(
                    sub.tg_id, item.id,
                    price=item.price, profit=valuation.net_profit,
                    verdict=valuation.scam_verdict,
                    condition=valuation.condition, sub_query=sub.query,
                )

        await asyncio.gather(*(_handle(it) for it in items))
        log.info(
            "sub %s %r: %d scraped, %d processed, %d cached, %d errors, "
            "%d deals, %d filtered",
            sub.id, sub.query, len(items), processed, cached, errors,
            deals, filtered,
        )
        tot_scraped += len(items)
        tot_processed += processed
        tot_cached += cached
        tot_errors += errors
        tot_deals += deals
        tot_filtered += filtered
        await asyncio.sleep(config.SUBSCRIPTION_STAGGER_SEC)

    runtime.set_captcha(browser.paused.is_set())
    runtime.update_cycle(
        subs=len(subs), scraped=tot_scraped, processed=tot_processed,
        cached=tot_cached, errors=tot_errors, deals=tot_deals,
        filtered=tot_filtered, drop_reasons=dict(runtime_drop),
    )


def watchdog_decide(snap: dict, state: dict, now: float) -> list[str]:
    """Pure: given a runtime snapshot + persistent state, return health
    alerts to send (throttled). Mutates state (dry timer, last-alert times)."""
    alerts: list[str] = []
    last = state.setdefault("last_alert", {})

    def _emit(key: str, text: str) -> None:
        if now - last.get(key, 0.0) >= config.WATCHDOG_REPEAT_SEC:
            last[key] = now
            alerts.append(text)

    captcha = bool(snap.get("captcha"))
    lca = snap.get("last_cycle_at") or 0.0
    if not captcha and lca > 0 and now - lca >= config.WATCHDOG_STALL_SEC:
        _emit("stall", (
            f"Монитор не завершал цикл ~{int((now - lca) // 60)} мин. "
            "Похоже, завис."
        ))

    subs = snap.get("subs") or 0
    scraped = snap.get("scraped") or 0
    if captcha or subs == 0 or scraped > 0:
        state["dry_since"] = None
    else:
        ds = state.get("dry_since")
        if ds is None:
            state["dry_since"] = now
        elif now - ds >= config.WATCHDOG_DRY_SEC:
            _emit("dry", (
                f"Уже ~{int((now - ds) // 60)} мин ничего не собирается "
                "при активных подписках. Возможен бан IP или смена "
                "вёрстки Avito - проверь окно браузера."
            ))
    return alerts


async def _watchdog_loop(notifier: Notifier) -> None:
    state: dict = {}
    while True:
        await asyncio.sleep(config.WATCHDOG_CHECK_SEC)
        try:
            for text in watchdog_decide(
                runtime.snapshot(), state, time.time()
            ):
                with contextlib.suppress(Exception):
                    await notifier.watchdog(text)
        except Exception as e:
            log.debug("watchdog tick failed: %s", e)


async def _maintenance_loop() -> None:
    """Periodically prune old rows so SQLite stays small for long runs."""
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL_SEC)
        try:
            deleted = await cleanup_old_rows()
            total = sum(deleted.values())
            if total:
                log.info("cleanup: removed %d rows %s", total, deleted)
        except Exception as e:
            log.warning("cleanup failed: %s", e)


async def run_forever(notifier: Notifier | None = None) -> None:
    notifier = notifier or ConsoleNotifier(beep=config.CAPTCHA_BEEP)
    browser = await get_browser(notifier)
    log.info("Monitor started. Poll interval %ss.", config.POLL_INTERVAL_SEC)
    wd = asyncio.create_task(_watchdog_loop(notifier))
    mt = asyncio.create_task(_maintenance_loop())
    try:
        while True:
            try:
                await poll_once(browser, notifier)
            except Exception as e:
                log.exception("poll_once crashed: %s", e)
            await asyncio.sleep(config.POLL_INTERVAL_SEC)
    finally:
        for t in (wd, mt):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
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
