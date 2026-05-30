"""Polling monitor manager.

Orchestrates the background tasks: polling, training, maintenance.
"""
import asyncio
import logging

from . import config
from .db import dispose_engine, get_session
from .db.repository import RepositoryContainer
from .logging_config import setup_logging
from .notifier import ConsoleNotifier, Notifier
from .scraper.browser import get_browser, shutdown_browser
from .scraper.session import SessionManager
from .monitoring.poller import AvitoPoller
from .monitoring.training import TrainingLoop
from .monitoring.maintenance import MaintenanceTasks

log = logging.getLogger("techhunter.monitor")


class MonitorManager:
    def __init__(self, notifier: Notifier | None = None):
        self.notifier = notifier or ConsoleNotifier(beep=config.CAPTCHA_BEEP)
        self.session_manager = SessionManager(self.notifier)
        self.repos = RepositoryContainer(get_session)
        self.browser = None
        self.poller = None
        self.training = None
        self.maintenance = None

    async def _fast_loop(self):
        while True:
            try:
                await self.poller.poll_fast()
            except Exception as e:
                log.exception("Fast loop crashed: %s", e)
            await asyncio.sleep(config.POLL_INTERVAL_SEC)

    async def _deep_loop(self):
        while True:
            try:
                await self.poller.poll_deep()
            except Exception as e:
                log.exception("Deep loop crashed: %s", e)
            await asyncio.sleep(config.DEEP_SCAN_INTERVAL_SEC)

    async def run_forever(self) -> None:
        self.browser = await get_browser(self.session_manager)
        self.poller = AvitoPoller(self.repos, self.browser, self.notifier)
        self.training = TrainingLoop(self.repos, self.browser)
        self.maintenance = MaintenanceTasks(self.repos, self.browser, self.notifier)

        log.info("Monitor started. Workers: %d Fast, %d Deep", config.FAST_WORKERS, config.DEEP_WORKERS)
        
        tasks = [
            asyncio.create_task(self.maintenance.watchdog_loop()),
            asyncio.create_task(self.maintenance.cleanup_loop()),
            asyncio.create_task(self.maintenance.browser_refresh_loop()),
            asyncio.create_task(self.maintenance.delivery_retry_loop()),
            asyncio.create_task(self._fast_loop()),
            asyncio.create_task(self._deep_loop()),
            asyncio.create_task(self.training.run()),
        ]
        
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()
            await shutdown_browser()
            await dispose_engine()


async def run_forever(notifier: Notifier | None = None) -> None:
    manager = MonitorManager(notifier)
    await manager.run_forever()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")
