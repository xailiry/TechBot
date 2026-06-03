"""Polling monitor manager.

Orchestrates the background tasks: polling, training, maintenance.
"""
import asyncio
from types import SimpleNamespace
import logging

from . import config
from . import runtime
from .db import dispose_engine, get_session
from .db.repository import RepositoryContainer
from .logging_config import setup_logging
from .monitoring.maintenance import MaintenanceTasks
from .notifier import ConsoleNotifier, Notifier
from .pipeline import process_new_listing
from .scraper.browser import get_browser, shutdown_browser
from .scraper.session import SessionManager
from .monitoring.poller import AvitoPoller
from .monitoring.training import TrainingLoop

log = logging.getLogger("techhunter.monitor")

# Re-exported for older tests/admin helpers that inspect monitor.runtime.
_RUNTIME_MODULE = runtime

_onboard_inprogress: set[int] = set()
_onboard_tasks: set[asyncio.Task] = set()


async def active_subscriptions():
    from .storage import active_subscriptions as _active_subscriptions

    return await _active_subscriptions()


async def discovery_users():
    from .storage import discovery_users as _discovery_users

    return await _discovery_users()


class _LegacySubscriptions:
    async def active_subscriptions(self):
        return await active_subscriptions()

    async def mark_subscription_onboarded(self, sub_id: int) -> None:
        from .storage import mark_subscription_onboarded

        await mark_subscription_onboarded(sub_id)


class _LegacyUsers:
    async def discovery_users(self):
        return await discovery_users()

    async def get_delivery_prefs(self, tg_id: int):
        from .storage import get_delivery_prefs

        return await get_delivery_prefs(tg_id)


class _LegacyRepos:
    def __init__(self):
        real = RepositoryContainer(get_session)
        self.subscriptions = _LegacySubscriptions()
        self.users = _LegacyUsers()
        self.listings = real.listings
        self.alerts = real.alerts
        self.images = real.images
        self.maintenance = real.maintenance
        self.feedback = real.feedback


def _ensure_session_adapter(browser) -> None:
    if hasattr(browser, "session"):
        return

    browser.session = SimpleNamespace(is_paused=False)


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
            for t in tasks:
                t.cancel()
            await shutdown_browser()
            await dispose_engine()


async def run_forever(notifier: Notifier | None = None) -> None:
    manager = MonitorManager(notifier)
    await manager.run_forever()


async def poll_fast(browser, notifier) -> None:
    """Legacy module-level fast poll; delegates to AvitoPoller."""
    _ensure_session_adapter(browser)
    poller = AvitoPoller(_LegacyRepos(), browser, notifier)
    poller._onboard_inprogress = _onboard_inprogress
    poller._onboard_tasks = _onboard_tasks
    await poller.poll_fast()


async def poll_deep(browser, notifier) -> None:
    """Legacy module-level deep poll; delegates to AvitoPoller."""
    _ensure_session_adapter(browser)
    poller = AvitoPoller(_LegacyRepos(), browser, notifier)
    await poller.poll_deep()


async def _retry_pending_alerts(notifier: Notifier) -> int:
    """Retry queued deal alerts from admin commands or tests.

    The normal long-running monitor owns this through MaintenanceTasks; this
    small composition-root helper keeps Telegram admin handlers away from the
    repository internals.
    """
    repos = RepositoryContainer(get_session)
    maintenance = MaintenanceTasks(repos, browser=None, notifier=notifier)
    return await maintenance._retry_pending_alerts()


async def _evaluate(
    browser,
    item,
    sem,
    cycle_eval,
    counters,
    mode: str = "fast",
    is_discovery: bool = False,
    deep_budget: dict | None = None,
):
    """Legacy test seam; delegates to AvitoPoller._evaluate."""
    import techhunter.monitoring.poller as poller_module

    repos = RepositoryContainer(get_session)
    poller = AvitoPoller(repos, browser, ConsoleNotifier(beep=False))
    old_process = poller_module.process_new_listing
    poller_module.process_new_listing = process_new_listing
    try:
        return await poller._evaluate(
            item,
            sem,
            cycle_eval,
            counters,
            mode=mode,
            is_discovery=is_discovery,
            deep_budget=deep_budget,
        )
    finally:
        poller_module.process_new_listing = old_process


async def _handle_items(
    browser,
    items,
    target,
    notifier,
    sem,
    cycle_eval,
    counters,
    runtime_drop,
    mode: str = "fast",
    deep_budget: dict | None = None,
):
    """Legacy test seam; delegates delivery/filtering to AvitoPoller."""
    repos = RepositoryContainer(get_session)
    poller = AvitoPoller(repos, browser, notifier)

    async def _evaluate_via_legacy(
        item,
        sem,
        cycle_eval,
        counters,
        mode: str = "fast",
        is_discovery: bool = False,
        deep_budget: dict | None = None,
    ):
        return await _evaluate(
            browser,
            item,
            sem,
            cycle_eval,
            counters,
            mode=mode,
            is_discovery=is_discovery,
            deep_budget=deep_budget,
        )

    poller._evaluate = _evaluate_via_legacy
    return await poller._handle_items(
        items,
        target,
        sem,
        cycle_eval,
        counters,
        runtime_drop,
        mode=mode,
        deep_budget=deep_budget,
    )


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")
