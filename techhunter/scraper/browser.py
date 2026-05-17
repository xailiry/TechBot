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
from .parser import looks_blocked, looks_loading, parse_detail, parse_listings
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
                page = existing[i] if i < len(existing) else await self._ctx.new_page()
                with contextlib.suppress(Exception):
                    await Stealth().apply_stealth_async(page)
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
        page = await self._available.get()
        try:
            yield page
        finally:
            self._available.put_nowait(page)

    # ─── captcha suspension (session-wide, single-flight) ───────────────────

    async def _reload(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.reload(
                wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
            )
            await asyncio.sleep(random.uniform(0.4, 1.0))

    async def _probe_clear(self) -> bool:
        """Check if the IP block is gone WITHOUT touching the pooled tabs the
        operator may be solving. Uses a throwaway tab on the Avito home."""
        if self._ctx is None:
            return False
        probe = None
        try:
            probe = await self._ctx.new_page()
            await probe.goto(
                config.AVITO_BASE_URL,
                wait_until="domcontentloaded",
                timeout=config.NAV_TIMEOUT_MS,
            )
            await asyncio.sleep(0.5)
            html = await probe.content()
            return not looks_blocked(html) and not looks_loading(html)
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
        if self.notifier:
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
                    if self.notifier:
                        with contextlib.suppress(Exception):
                            await self.notifier.captcha_cleared()
                    return True
            log.warning("Captcha not solved within %ss, skipping this cycle.",
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
            return []

        if not looks_blocked(html) and not looks_loading(html):
            return parse_listings(html)

        # Blocked: session-wide gate (one notify), then this tab is reloaded.
        if await self._ensure_unblocked(page, url):
            with contextlib.suppress(Exception):
                return parse_listings(await page.content())
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
                if looks_blocked(html) or looks_loading(html):
                    if await self._ensure_unblocked(page, item.full_url):
                        html = await page.content()
                    else:
                        return item
                return parse_detail(html, item)
            except Exception as e:
                log.debug("fetch_details failed for %s: %s", item.id, e)
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
