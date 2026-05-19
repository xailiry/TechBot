"""Visible Avito browser.

Runs Chromium with a real on-disk profile and a visible window. Empirically
Avito almost never challenges a real, shown browser; when it does, the loop
suspends, alerts the operator, waits for a manual solve, and auto-resumes.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext, Page, async_playwright

from .. import config
from .models import ParsedListing

if TYPE_CHECKING:
    from ..notifier import Notifier
from .parser import (
    has_listings,
    page_blocked,
    parse_detail,
    parse_listings,
)
from .stealth import DESKTOP_UA, STEALTH_INIT
from .urls import build_search_url

log = logging.getLogger(__name__)


class AvitoBrowser:
    def __init__(self, notifier: Notifier | None = None) -> None:
        self.notifier = notifier
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._available: asyncio.Queue[Page] | None = None
        self._lock = asyncio.Lock()
        # Set while a captcha is being solved; lets the monitor observe state.
        self.paused = asyncio.Event()
        # Session-wide captcha gate. Avito/Datadome blocks by IP, so a block
        # hits every tab at once. Only ONE coroutine notifies + waits; the
        # rest just wait on this event and then reload their own tab.
        self._captcha_lock = asyncio.Lock()
        self._unblocked = asyncio.Event()
        self._unblocked.set()
        # Crash recovery: serialize restarts, rate-limit them.
        self._restart_lock = asyncio.Lock()
        self._last_restart = 0.0
        self._last_captcha_notify = 0.0

    @staticmethod
    def _is_closed_error(err: object) -> bool:
        m = str(err).lower()
        return any(
            s in m
            for s in (
                "target page, context or browser has been closed",
                "target closed", "targetclosederror",
                "browser has been closed", "connection closed",
                "page has been closed", "context has been closed",
                "browser closed", "websocket",
            )
        )

    async def _new_stealth_page(self) -> Page:
        assert self._ctx is not None
        page = await self._ctx.new_page()
        with contextlib.suppress(Exception):
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(page)
        return page

    async def restart(self) -> None:
        """Recreate the browser after a crash. Rate-limited; concurrent
        callers collapse into one restart."""
        async with self._restart_lock:
            now = time.time()
            if now - self._last_restart < config.BROWSER_RESTART_COOLDOWN_SEC:
                return
            self._last_restart = now
            log.warning("Browser: restarting (dead context/crash).")
            await self.stop()
            await self.start()

    async def _maybe_restart(self, err: object) -> None:
        if self._is_closed_error(err):
            with contextlib.suppress(Exception):
                await self.restart()

    # ─── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._ctx:
            return
        async with self._lock:
            if self._ctx:
                return
            os.makedirs(config.BROWSER_PROFILE_DIR, exist_ok=True)
            self._pw = await async_playwright().start()

            base_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            common = dict(
                user_data_dir=config.BROWSER_PROFILE_DIR,
                headless=config.AVITO_HEADLESS,
                no_viewport=True,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                args=base_args,
            )
            try:
                # Prefer the real installed Chrome (least detectable).
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    channel=config.BROWSER_CHANNEL, **common
                )
                log.info("Browser: launched %s profile (headless=%s)",
                         config.BROWSER_CHANNEL, config.AVITO_HEADLESS)
            except Exception as e:
                log.warning("channel=%s failed (%s); using bundled Chromium",
                            config.BROWSER_CHANNEL, e)
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    user_agent=DESKTOP_UA,
                    **{**common, "args": base_args + ["--no-sandbox",
                                                     "--disable-dev-shm-usage"]},
                )

            await self._ctx.add_init_script(STEALTH_INIT)

            from playwright_stealth import Stealth

            self._available = asyncio.Queue()
            existing = list(self._ctx.pages)
            for i in range(config.PAGE_POOL_SIZE):
                if i < len(existing):
                    page = existing[i]
                    with contextlib.suppress(Exception):
                        await Stealth().apply_stealth_async(page)
                else:
                    page = await self._new_stealth_page()
                self._available.put_nowait(page)

    async def stop(self) -> None:
        if self._ctx:
            with contextlib.suppress(Exception):
                await self._ctx.close()
            self._ctx = None
        if self._pw:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None
        self._available = None

    @contextlib.asynccontextmanager
    async def acquire_page(self):
        await self.start()
        assert self._available is not None
        q = self._available  # capture: a restart swaps this out
        transient = False
        try:
            page = q.get_nowait()
        except asyncio.QueueEmpty:
            try:
                page = await asyncio.wait_for(
                    q.get(), timeout=config.PAGE_ACQUIRE_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                # Pool oversubscribed (onboarding + multi-sub + probe).
                # Use a throwaway tab so we NEVER deadlock the monitor.
                page = await self._new_stealth_page()
                transient = True
                log.debug("page pool exhausted; using transient tab")
        try:
            yield page
        finally:
            if transient or self._available is not q:
                # Restart replaced the pool, or this was a throwaway tab:
                # close it instead of poisoning the (new) pool.
                with contextlib.suppress(Exception):
                    await page.close()
            else:
                q.put_nowait(page)

    # ─── captcha suspension (session-wide, single-flight) ───────────────────

    async def _reload(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.reload(
                wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
            )
            await asyncio.sleep(random.uniform(0.4, 1.0))

    async def _probe_clear(self) -> bool:
        """Check if the IP block is gone WITHOUT touching the pooled tabs
        the operator may be solving. Throwaway tab on a stable search URL;
        'cleared' = the results marker actually rendered (positive signal),
        not the absence of a substring."""
        if self._ctx is None:
            return False
        probe = None
        try:
            probe = await self._ctx.new_page()
            await probe.goto(
                config.AVITO_BASE_URL + config.AVITO_PROBE_PATH,
                wait_until="domcontentloaded",
                timeout=config.NAV_TIMEOUT_MS,
            )
            await asyncio.sleep(0.5)
            html = await probe.content()
            return has_listings(html)
        except Exception:
            return False
        finally:
            if probe is not None:
                with contextlib.suppress(Exception):
                    await probe.close()

    async def _run_captcha_wait(self, url: str) -> bool:
        """First detector only: notify once, poll an out-of-band probe until
        the human solves the captcha on any one tab."""
        self.paused.set()
        # Throttle: a long block re-enters the gate every cycle; do not
        # spam the operator each time.
        notified = False
        if self.notifier and (
            time.time() - self._last_captcha_notify
            > config.CAPTCHA_NOTIFY_INTERVAL_SEC
        ):
            self._last_captcha_notify = time.time()
            notified = True
            with contextlib.suppress(Exception):
                await self.notifier.captcha_detected(url)
        deadline = (
            None
            if config.CAPTCHA_MAX_WAIT_SEC <= 0
            else time.time() + config.CAPTCHA_MAX_WAIT_SEC
        )
        try:
            while deadline is None or time.time() < deadline:
                await asyncio.sleep(config.CAPTCHA_RECHECK_SEC)
                if await self._probe_clear():
                    if notified and self.notifier:
                        with contextlib.suppress(Exception):
                            await self.notifier.captcha_cleared()
                    return True
            log.warning("Captcha not solved within %ss, will retry later.",
                        config.CAPTCHA_MAX_WAIT_SEC)
            return False
        finally:
            self.paused.clear()

    async def _ensure_unblocked(self, page: Page, url: str) -> bool:
        """Called by any tab that hit a block. Exactly one coroutine becomes
        the handler (notifies + waits); the others just wait, then every tab
        reloads itself so work resumes without manual page refresh."""
        am_first = False
        async with self._captcha_lock:
            if self._unblocked.is_set():
                self._unblocked.clear()
                am_first = True
        if am_first:
            try:
                ok = await self._run_captcha_wait(url)
            finally:
                self._unblocked.set()  # release all waiters
            if not ok:
                return False
        else:
            await self._unblocked.wait()
            # Stagger so the pool does not reload all tabs at once.
            await asyncio.sleep(random.uniform(0.5, 2.5))
        await self._reload(page)
        return True

    # ─── search ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        city_slug: str = "rossiya",
        *,
        min_price: int | None = None,
        max_price: int | None = None,
        pages: int = 1,
    ) -> list[ParsedListing]:
        pages = max(1, pages)
        async with self.acquire_page() as page:
            out: list[ParsedListing] = []
            seen: set[str] = set()
            for page_num in range(1, pages + 1):
                items = await self._fetch_page(
                    page, query, city_slug, min_price, max_price, page_num
                )
                if not items:
                    break
                new = 0
                for it in items:
                    if it.id in seen:
                        continue
                    seen.add(it.id)
                    out.append(it)
                    new += 1
                if new == 0:
                    break
                if page_num < pages:
                    await asyncio.sleep(random.uniform(*config.PAGE_TURN_DELAY_SEC))
            return out

    async def _fetch_page(
        self,
        page: Page,
        query: str,
        city_slug: str,
        min_price: int | None,
        max_price: int | None,
        page_num: int,
    ) -> list[ParsedListing]:
        url = build_search_url(
            query, city_slug, min_price=min_price, max_price=max_price,
            page=page_num,
        )
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=config.NAV_TIMEOUT_MS)
            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    '[data-marker="item"], [data-marker="catalog-serp"], '
                    '[data-marker="captcha"]',
                    timeout=6000,
                )
            await asyncio.sleep(random.uniform(0.2, 0.6))
            html = await page.content()
        except Exception as e:
            log.warning("search %r p%d navigation failed: %s", query, page_num, e)
            await self._maybe_restart(e)
            return []

        if has_listings(html):
            return parse_listings(html)
        if not page_blocked(html):
            return []  # genuinely empty search, not a block -> no captcha

        # Blocked: session-wide gate (one notify), then this tab is reloaded.
        if await self._ensure_unblocked(page, url):
            with contextlib.suppress(Exception):
                new_html = await page.content()
                return parse_listings(new_html) if has_listings(new_html) else []
        return []

    async def fetch_details(self, item: ParsedListing) -> ParsedListing:
        async with self.acquire_page() as page:
            try:
                await page.goto(item.full_url, wait_until="domcontentloaded",
                                timeout=config.NAV_TIMEOUT_MS)
                with contextlib.suppress(Exception):
                    await page.wait_for_selector(
                        '[data-marker="item-view/item-description"], '
                        '[data-marker="item-view/title-info"], h1',
                        timeout=6000,
                    )
                await asyncio.sleep(random.uniform(0.2, 0.5))
                html = await page.content()
                if page_blocked(html, detail=True):
                    if await self._ensure_unblocked(page, item.full_url):
                        html = await page.content()
                    else:
                        return item
                return parse_detail(html, item)
            except Exception as e:
                log.debug("fetch_details failed for %s: %s", item.id, e)
                await self._maybe_restart(e)
                return item


# ─── module singleton ───────────────────────────────────────────────────────

_browser: AvitoBrowser | None = None
_browser_lock = asyncio.Lock()


async def get_browser(notifier: Notifier | None = None) -> AvitoBrowser:
    global _browser
    if _browser is None:
        async with _browser_lock:
            if _browser is None:
                _browser = AvitoBrowser(notifier=notifier)
                await _browser.start()
    elif notifier is not None and _browser.notifier is None:
        _browser.notifier = notifier
    return _browser


async def shutdown_browser() -> None:
    global _browser
    if _browser is not None:
        await _browser.stop()
        _browser = None
