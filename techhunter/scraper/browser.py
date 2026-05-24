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

from .. import config, runtime
from .models import ParsedListing

if TYPE_CHECKING:
    from ..notifier import Notifier
from .parser import (
    has_detail,
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
        self._pool: asyncio.Queue[Page] | None = None
        self._available: asyncio.Queue[Page] | None = None
        self._fast_pool = None
        self._deep_pool = None
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
        # Pages currently actively waiting in _ensure_unblocked
        self._blocked_pages: set[Page] = set()
        self._captcha_solved_page: Page | None = None

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

    @staticmethod
    def _is_page_closed(page: Page) -> bool:
        if hasattr(page, "is_closed"):
            return page.is_closed()
        return getattr(page, "closed", False)

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
            # Remove Chrome profile lock files if they exist on Windows/Linux to prevent startup freezes
            for lock_name in ("SingletonLock", "lock", "parent.lock"):
                lock_file = os.path.join(config.BROWSER_PROFILE_DIR, lock_name)
                if os.path.exists(lock_file):
                    try:
                        os.remove(lock_file)
                        log.info("Browser: removed stale lock file %s", lock_name)
                    except Exception as le:
                        log.debug("Browser: could not remove stale lock file %s: %s", lock_name, le)
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

            self._pool = asyncio.Queue()
            self._fast_pool = self._pool
            self._deep_pool = self._pool
            self._available = self._pool
            existing = list(self._ctx.pages)
            for i in range(config.PAGE_POOL_SIZE):
                if i < len(existing):
                    page = existing[i]
                    with contextlib.suppress(Exception):
                        await Stealth().apply_stealth_async(page)
                else:
                    page = await self._new_stealth_page()
                
                self._pool.put_nowait(page)

            # Close any extra pages restored by Chrome from previous sessions
            if len(existing) > config.PAGE_POOL_SIZE:
                for page in existing[config.PAGE_POOL_SIZE:]:
                    with contextlib.suppress(Exception):
                        await page.close()

    async def stop(self) -> None:
        if self._ctx:
            with contextlib.suppress(Exception):
                await self._ctx.close()
            self._ctx = None
        if self._pw:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None
        self._pool = None
        self._available = None
        self._fast_pool = None
        self._deep_pool = None

    @contextlib.asynccontextmanager
    async def acquire_page(self, mode: str = "fast"):
        await self.start()
        
        # Fallback to _fast_pool, _deep_pool, or _available if unified _pool is not initialized (e.g. in tests)
        fast_pool = self._fast_pool if self._fast_pool is not None else self._available
        deep_pool = self._deep_pool if self._deep_pool is not None else self._available
        q = self._pool if self._pool is not None else (deep_pool if mode == "deep" else fast_pool)
        
        assert q is not None
        
        page = None
        transient = False
        
        # If captcha is active, wait until it is resolved first
        while self.paused.is_set():
            log.debug("acquire_page: captcha active, waiting for resolution")
            await self._unblocked.wait()
            
        try:
            try:
                page = q.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    page = await asyncio.wait_for(
                        q.get(), timeout=config.PAGE_ACQUIRE_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    # If captcha was activated during the wait, do not create a transient tab.
                    # Wait for unblock and retry.
                    if self.paused.is_set():
                        log.debug("acquire_page: captcha activated during wait, retrying")
                        await self._unblocked.wait()
                        # Recursively call/retry to acquire page safely
                        async with self.acquire_page(mode=mode) as p:
                            yield p
                        return
                    
                    # Pool truly oversubscribed. Use a throwaway tab.
                    page = await self._new_stealth_page()
                    transient = True
                    log.debug("page pool exhausted; using transient tab")
            
            # If the page we got from the pool is dead/closed, replace it on-the-fly!
            if not transient and (page is None or self._is_page_closed(page)):
                log.warning("acquire_page: page from pool is closed/dead, replacing on-the-fly")
                if page is not None:
                    with contextlib.suppress(Exception):
                        await page.close()
                page = await self._new_stealth_page()
            
            yield page
            
        finally:
            if page is not None:
                current_pool = self._pool if self._pool is not None else (self._deep_pool if mode == "deep" else (self._fast_pool if self._fast_pool is not None else self._available))
                if transient or q is not current_pool:
                    # Transient or pool was replaced during a restart
                    with contextlib.suppress(Exception):
                        await page.close()
                else:
                    if self._is_page_closed(page):
                        log.warning("acquire_page: page is closed, creating replacement page for the pool")
                        try:
                            replacement_page = await self._new_stealth_page()
                            q.put_nowait(replacement_page)
                        except Exception as pe:
                            log.error("acquire_page: failed to create replacement page: %s", pe)
                    else:
                        q.put_nowait(page)

    # ─── captcha suspension (session-wide, single-flight) ───────────────────

    async def _reload(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.reload(
                wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
            )
            await asyncio.sleep(random.uniform(0.4, 1.0))

    async def _reload_safe(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.reload(
                wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
            )

    async def _reload_safe_staggered(self, page: Page) -> None:
        await asyncio.sleep(random.uniform(0.5, 3.0))
        with contextlib.suppress(Exception):
            if not self._is_page_closed(page) and page.url != "about:blank":
                await page.reload(
                    wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
                )

    async def _probe_clear(self) -> bool:
        """Check if the IP block is gone WITHOUT touching the pooled tabs
        the operator may be solving. Throwaway tab on a stable search URL;
        'cleared' = the results marker actually rendered (positive signal),
        not the absence of a substring."""
        if self._ctx is None:
            return False
        probe = None
        try:
            probe = await self._new_stealth_page()
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

    async def _notify_captcha_detected(self, url: str, *, force: bool = False) -> bool:
        if not self.notifier:
            return False
        now = time.time()
        if (
            not force
            and now - self._last_captcha_notify
            < config.CAPTCHA_NOTIFY_INTERVAL_SEC
        ):
            return False
        try:
            await self.notifier.captcha_detected(url)
        except Exception as e:
            log.warning("Browser: captcha notification failed: %s", e)
            return False
        self._last_captcha_notify = now
        return True

    async def _notify_captcha_cleared(self) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.captcha_cleared()
        except Exception as e:
            log.debug("Browser: captcha-cleared notification failed: %s", e)

    async def _refresh_stale_captcha_pages(self, exclude: set[Page]) -> None:
        """Refresh tabs that are visibly stuck on captcha but are not part of
        the active waiters. Active waiters reload themselves after the gate
        opens; the tab where the human solved captcha keeps its current DOM."""
        if self._ctx is None:
            return

        tasks = []
        for p in list(self._ctx.pages):
            try:
                if p in exclude or self._is_page_closed(p) or p.url == "about:blank":
                    continue
                p_html = await p.content()
                url_low = p.url.lower()
                if page_blocked(p_html) or "datadome" in url_low or "captcha" in url_low:
                    log.info("Browser: auto-refreshing stale captcha page: %s", p.url)
                    tasks.append(self._reload_safe_staggered(p))
            except Exception as e:
                log.debug("Browser: stale captcha page check failed: %s", e)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_captcha_wait(self, url: str) -> bool:
        """First detector only: notify, poll open pages and out-of-band probe until
        the human solves the captcha on any one tab."""
        self.paused.set()
        runtime.set_captcha(True)
        self._captcha_solved_page = None
        notified = await self._notify_captcha_detected(url, force=True)
        deadline = (
            None
            if config.CAPTCHA_MAX_WAIT_SEC <= 0
            else time.time() + config.CAPTCHA_MAX_WAIT_SEC
        )
        try:
            # Out of band check counter to throttle probe_clear hits
            oob_counter = 0
            recheck_sec = max(1, config.CAPTCHA_RECHECK_SEC)
            while deadline is None or time.time() < deadline:
                await asyncio.sleep(recheck_sec)
                notified = (
                    await self._notify_captcha_detected(url, force=False)
                    or notified
                )
                
                # 1. Quick check: did the human solve it? Only inspect the
                # pages that are ACTUALLY blocked right now (they show the
                # captcha and turn into listings only after a real solve).
                # Scanning every tab gave false positives: other pooled tabs
                # still display stale results from the previous cycle, so the
                # gate "cleared" itself instantly the moment a captcha popped.
                solved_page: Page | None = None
                for p in list(self._blocked_pages):
                    try:
                        if self._is_page_closed(p) or p.url == "about:blank":
                            continue
                        p_html = await p.content()
                        if has_listings(p_html) or has_detail(p_html):
                            log.info("Browser: detected captcha solve on blocked page: %s", p.url)
                            solved_page = p
                            self._captcha_solved_page = p
                            break
                    except Exception:
                        continue

                # 2. Slow fallback check: run the out of band probe once every 12 seconds
                oob_solved = False
                oob_counter += recheck_sec
                if solved_page is None and oob_counter >= 12:
                    oob_counter = 0
                    oob_solved = await self._probe_clear()

                if solved_page or oob_solved:
                    if notified:
                        await self._notify_captcha_cleared()

                    exclude = set(self._blocked_pages)
                    if solved_page is not None:
                        exclude.add(solved_page)
                    await self._refresh_stale_captcha_pages(exclude)
                    return True
            log.warning("Captcha not solved within %ss, will retry later.",
                        config.CAPTCHA_MAX_WAIT_SEC)
            return False
        finally:
            self.paused.clear()
            runtime.set_captcha(False)

    async def _ensure_unblocked(self, page: Page, url: str) -> bool:
        """Called by any tab that hit a block. Exactly one coroutine becomes
        the handler (notifies + waits); the others just wait, then blocked
        tabs reload themselves unless this is the tab where the captcha was
        solved and the real page DOM is already visible."""
        self._blocked_pages.add(page)
        try:
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
            if page is not self._captcha_solved_page:
                await self._reload(page)
            return True
        finally:
            self._blocked_pages.discard(page)

    # ─── search ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        city_slug: str = "rossiya",
        *,
        min_price: int | None = None,
        max_price: int | None = None,
        pages: int = 1,
        start_page: int = 1,
        mode: str = "fast",
    ) -> list[ParsedListing]:
        pages = max(1, pages)
        async with self.acquire_page(mode=mode) as page:
            out: list[ParsedListing] = []
            seen: set[str] = set()
            for page_num in range(start_page, start_page + pages):
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
                if page_num < start_page + pages - 1:
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

        if page_blocked(html):
            # Blocked: session-wide gate (one notify), then this tab resumes.
            if await self._ensure_unblocked(page, url):
                with contextlib.suppress(Exception):
                    new_html = await page.content()
                    return parse_listings(new_html) if has_listings(new_html) else []
            return []
        if has_listings(html):
            return parse_listings(html)
        return []  # genuinely empty search, not a block -> no captcha

    async def fetch_details(self, item: ParsedListing, mode: str = "fast") -> ParsedListing:
        async with self.acquire_page(mode=mode) as page:
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
