import asyncio
import contextlib
import logging
import time

from .. import config, runtime
from ..notifier import Notifier
from ..db.repository import RepositoryContainer
from ..scraper.models import ParsedListing
from ..ai.evaluate import EvaluationReport
from ..valuation.engine import Valuation

log = logging.getLogger(__name__)

def _is_permanent_delivery_error(err: object) -> str | None:
    msg = str(err).lower()
    markers = (
        "bot was blocked", "chat not found", "user is deactivated",
        "bot can't initiate", "bot cannot initiate", "forbidden",
        "blocked by the user",
    )
    return "permanent_telegram_error" if any(m in msg for m in markers) else None


class MaintenanceTasks:
    def __init__(self, repos: RepositoryContainer, browser, notifier: Notifier):
        self.repos = repos
        self.browser = browser
        self.notifier = notifier

    async def _retry_pending_alerts(self) -> int:
        sent = 0
        last_error = None
        rows = await self.repos.alerts.list_pending_alerts(config.DELIVERY_RETRY_BATCH_SIZE)
        for row in rows:
            tg_id = row["tg_id"]
            listing_id = row["listing_id"]
            if await self.repos.alerts.alert_already_sent(tg_id, listing_id):
                await self.repos.alerts.mark_pending_sent(tg_id, listing_id)
                continue
            try:
                item = ParsedListing(**row["item"])
                report = EvaluationReport(**row["report"])
                valuation = Valuation(**row["valuation"])
                await self.notifier.deal(
                    item, report, valuation, tg_id=tg_id, sub_query=row.get("sub_query")
                )
            except Exception as e:
                last_error = str(e)
                await self.repos.alerts.mark_pending_failed(
                    tg_id,
                    listing_id,
                    str(e),
                    dead_reason=_is_permanent_delivery_error(e),
                )
                log.warning("Pending deal delivery failed for %s to %s: %s", listing_id, tg_id, e)
                continue
            
            await self.repos.alerts.mark_alert_sent(
                tg_id, listing_id, price=item.price, profit=valuation.net_profit,
                verdict=valuation.scam_verdict, condition=valuation.condition,
                sub_query=row.get("sub_query"),
            )
            await self.repos.alerts.mark_pending_sent(tg_id, listing_id)
            sent += 1
        
        if sent:
            log.info("Pending deal delivery retried successfully: %d", sent)
        with contextlib.suppress(Exception):
            stats = await self.repos.alerts.pending_alert_stats()
            runtime.update_outbox(
                pending=stats["pending"], dead=stats["dead"],
                last_retry_at=time.time(),
                last_sent=sent, last_error=last_error,
            )
        return sent

    async def delivery_retry_loop(self) -> None:
        interval = max(10, config.DELIVERY_RETRY_INTERVAL_SEC)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._retry_pending_alerts()
            except Exception as e:
                log.debug("Pending deal retry tick failed: %s", e)

    async def watchdog_loop(self) -> None:
        from ..monitor_logic import watchdog_decide
        state: dict = {}
        while True:
            await asyncio.sleep(config.WATCHDOG_CHECK_SEC)
            try:
                for text in watchdog_decide(runtime.snapshot(), state, time.time()):
                    with contextlib.suppress(Exception):
                        await self.notifier.watchdog(text)
            except Exception as e:
                log.debug("Watchdog tick failed: %s", e)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SEC)
            try:
                deleted = await self.repos.maintenance.cleanup_old_rows()
                if sum(deleted.values()):
                    log.info("Cleanup done: %s", deleted)
            except Exception as e:
                log.warning("Cleanup failed: %s", e)

    async def browser_refresh_loop(self) -> None:
        interval = config.BROWSER_RESTART_INTERVAL_SEC
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            if self.browser.session.is_paused:
                continue
            try:
                log.info("Scheduled browser refresh (flushing memory).")
                await self.browser.restart()
            except Exception as e:
                log.warning("Scheduled browser refresh failed: %s", e)
