import asyncio
import contextlib
import logging
import time

from .. import config, runtime
from ..notifier import Notifier

log = logging.getLogger(__name__)

class CaptchaException(Exception):
    def __init__(self, url: str, html: str | None = None):
        super().__init__(f"Captcha detected at {url}")
        self.url = url
        self.html = html

class SessionManager:
    """Manages global bot pause state, primarily for Captcha blocks."""
    def __init__(self, notifier: Notifier | None = None):
        self.notifier = notifier
        self._paused = asyncio.Event()
        self._captcha_lock = asyncio.Lock()
        self._unblocked = asyncio.Event()
        self._unblocked.set()
        self._last_notify = 0.0

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    async def wait_for_unblock(self) -> None:
        await self._unblocked.wait()

    async def _notify(self, url: str, force: bool = False) -> bool:
        if not self.notifier:
            return False
        now = time.time()
        if not force and now - self._last_notify < config.CAPTCHA_NOTIFY_INTERVAL_SEC:
            return False
        try:
            await self.notifier.captcha_detected(url)
            self._last_notify = now
            return True
        except Exception as e:
            log.warning("SessionManager: notify failed: %s", e)
            return False

    async def handle_captcha(self, url: str, check_cleared_coro) -> bool:
        """Called by the first tab that spots a captcha. Others wait."""
        am_first = False
        async with self._captcha_lock:
            if self._unblocked.is_set():
                self._unblocked.clear()
                am_first = True
        
        if am_first:
            try:
                self._paused.set()
                runtime.set_captcha(True)
                notified = await self._notify(url, force=True)
                
                deadline = time.time() + config.CAPTCHA_MAX_WAIT_SEC if config.CAPTCHA_MAX_WAIT_SEC > 0 else None
                recheck_sec = max(1, config.CAPTCHA_RECHECK_SEC)
                
                while deadline is None or time.time() < deadline:
                    await asyncio.sleep(recheck_sec)
                    notified = await self._notify(url, force=False) or notified
                    
                    # Call the provided coroutine to check if the human solved it
                    if await check_cleared_coro():
                        if notified and self.notifier:
                            with contextlib.suppress(Exception):
                                await self.notifier.captcha_cleared()
                        return True
                        
                log.warning("Captcha not solved within %ss.", config.CAPTCHA_MAX_WAIT_SEC)
                return False
            finally:
                self._paused.clear()
                runtime.set_captcha(False)
                self._unblocked.set()
        else:
            await self._unblocked.wait()
            return True

