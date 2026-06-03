"""Visible Avito browser.

Runs Chromium with a real on-disk profile and a visible window. Empirically
Avito almost never challenges a real, shown browser. Captcha logic is extracted
to session.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time

from playwright.async_api import BrowserContext, Page, async_playwright

from .. import config
from ..notifier import Notifier
from .models import ParsedListing
from .session import SessionManager

from .parser import has_detail, has_listings, page_blocked, parse_detail, parse_listings
from .stealth import DESKTOP_UA, STEALTH_INIT
from .urls import build_search_url

log = logging.getLogger(__name__)


class AvitoBrowser:
    def __init__(
        self,
        session_manager: SessionManager | None = None,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self.session = session_manager or SessionManager(notifier)
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._fast_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._deep_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._pool: asyncio.Queue[Page] = self._fast_pool
        self._profile_lock_file = None
        self._lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._last_restart = 0.0
        self._last_captcha_notify = 0.0
        self._captcha_solved_page: Page | None = None

    @staticmethod
    def _is_closed_error(err: object) -> bool:
        m = str(err).lower()
        return any(
            s in m for s in (
                "target page, context or browser has been closed",
                "target closed", "targetclosederror",
                "browser has been closed", "connection closed",
                "page has been closed", "context has been closed",
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
            self._acquire_profile_lock()
            
            for lock_name in ("SingletonLock", "lock", "parent.lock"):
                lock_file = os.path.join(config.BROWSER_PROFILE_DIR, lock_name)
                if config.BROWSER_FORCE_CLEAR_PROFILE_LOCKS and os.path.exists(lock_file):
                    try:
                        os.remove(lock_file)
                    except Exception as le:
                        log.debug("Browser: could not remove stale lock file %s: %s", lock_name, le)

            try:
                self._pw = await async_playwright().start()
            except Exception:
                self._release_profile_lock()
                raise

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
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    channel=config.BROWSER_CHANNEL, **common
                )
            except Exception as e:
                log.warning("channel=%s failed (%s); using bundled Chromium", config.BROWSER_CHANNEL, e)
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        user_agent=DESKTOP_UA,
                        **{**common, "args": base_args + ["--no-sandbox", "--disable-dev-shm-usage"]},
                    )
                except Exception:
                    await self.stop()
                    raise

            await self._ctx.add_init_script(STEALTH_INIT)
            from playwright_stealth import Stealth

            self._fast_pool = asyncio.Queue()
            self._deep_pool = asyncio.Queue()
            self._pool = self._fast_pool

            existing = list(self._ctx.pages)
            total_pages = max(1, config.PAGE_POOL_SIZE)
            fast_pages = min(max(1, config.FAST_WORKERS), total_pages)
            
            for i in range(total_pages):
                if i < len(existing):
                    page = existing[i]
                    with contextlib.suppress(Exception):
                        await Stealth().apply_stealth_async(page)
                else:
                    page = await self._new_stealth_page()

                if i < fast_pages:
                    self._fast_pool.put_nowait(page)
                else:
                    self._deep_pool.put_nowait(page)

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
        self._release_profile_lock()

    def _acquire_profile_lock(self) -> None:
        if self._profile_lock_file is not None:
            return
        lock_path = os.path.join(config.BROWSER_PROFILE_DIR, "techhunter_profile.lock")
        lock_file = open(lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    raise RuntimeError("Browser profile is already in use.") from e
            else:
                import fcntl
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    raise RuntimeError("Browser profile is already in use.") from e
        except Exception:
            with contextlib.suppress(Exception):
                lock_file.close()
            raise
        self._profile_lock_file = lock_file

    def _release_profile_lock(self) -> None:
        lock_file = self._profile_lock_file
        self._profile_lock_file = None
        if lock_file is None:
            return
        with contextlib.suppress(Exception):
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            lock_file.close()

    def _select_pool(self, mode: str) -> asyncio.Queue[Page]:
        if hasattr(self, "_available"):
            return self._available
        if mode == "deep":
            return self._deep_pool if self._deep_pool is not None else self._pool
        return self._fast_pool if self._fast_pool is not None else self._pool

    async def _close_page(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.close()

    async def _reload(self, page: Page) -> None:
        await page.reload(wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)

    async def _run_captcha_wait(self, url: str) -> bool:
        async def _no_page_probe() -> bool:
            return False

        return await self.session.handle_captcha(url, _no_page_probe)

    async def _notify_captcha_detected(self, url: str, force: bool = False) -> bool:
        # Compatibility wrapper for tests and older call sites; the real
        # throttle state lives in SessionManager.
        self.session._last_notify = self._last_captcha_notify
        ok = await self.session._notify(url, force=force)
        self._last_captcha_notify = self.session._last_notify
        return ok

    async def _ensure_unblocked(self, page: Page, url: str) -> bool:
        ok = await self._run_captcha_wait(url)
        if ok and page is not self._captcha_solved_page:
            await self._reload(page)
        return ok

    @contextlib.asynccontextmanager
    async def acquire_page(self, mode: str = "fast"):
        await self.start()
        
        while self.session.is_paused:
            await self.session.wait_for_unblock()
            
        pool = self._select_pool(mode)
        transient = False
        try:
            page = await asyncio.wait_for(
                pool.get(),
                timeout=max(0.0, config.PAGE_ACQUIRE_TIMEOUT_SEC),
            )
        except asyncio.TimeoutError:
            page = await self._new_stealth_page()
            transient = True
        try:
            if self._is_page_closed(page):
                page = await self._new_stealth_page()
                transient = True
            yield page
        except Exception as e:
            await self._maybe_restart(e)
            raise
        finally:
            current_pool = self._select_pool(mode)
            if transient or pool is not current_pool:
                await self._close_page(page)
            elif self._is_page_closed(page):
                try:
                    current_pool.put_nowait(await self._new_stealth_page())
                except Exception as e:
                    log.error("acquire_page: failed to replace dead page: %s", e)
            else:
                current_pool.put_nowait(page)

    # ─── search ─────────────────────────────────────────────────────────────

    async def _handle_captcha(self, page: Page, url: str, is_detail: bool) -> bool:
        async def check_cleared() -> bool:
            try:
                if self._is_page_closed(page):
                    return False
                await page.reload(wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                html = await page.content()
                return has_detail(html) if is_detail else has_listings(html)
            except Exception:
                return False

        if await self.session.handle_captcha(url, check_cleared):
            if not self._is_page_closed(page):
                with contextlib.suppress(Exception):
                    await page.reload(wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            return True
        return False

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
                url = build_search_url(query, city_slug, min_price=min_price, max_price=max_price, page=page_num)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                    with contextlib.suppress(Exception):
                        await page.wait_for_selector('[data-marker="item"], [data-marker="catalog-serp"], [data-marker="captcha"]', timeout=6000)
                    await asyncio.sleep(random.uniform(0.2, 0.6))
                    html = await page.content()
                except Exception as e:
                    log.warning("search %r p%d navigation failed: %s", query, page_num, e)
                    continue

                if page_blocked(html):
                    if await self._handle_captcha(page, url, False):
                        with contextlib.suppress(Exception):
                            html = await page.content()
                            if has_listings(html):
                                items = parse_listings(html)
                                out.extend([it for it in items if it.id not in seen and not seen.add(it.id)])
                    continue

                if has_listings(html):
                    items = parse_listings(html)
                    new = 0
                    for it in items:
                        if it.id not in seen:
                            seen.add(it.id)
                            out.append(it)
                            new += 1
                    if new == 0:
                        break
                else:
                    break

                if page_num < start_page + pages - 1:
                    await asyncio.sleep(random.uniform(*config.PAGE_TURN_DELAY_SEC))
            return out

    async def fetch_details(self, item: ParsedListing, mode: str = "fast") -> ParsedListing:
        async with self.acquire_page(mode=mode) as page:
            try:
                await page.goto(item.full_url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                with contextlib.suppress(Exception):
                    await page.wait_for_selector('[data-marker="item-view/item-description"], [data-marker="item-view/title-info"], h1', timeout=6000)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                html = await page.content()
                if page_blocked(html, detail=True):
                    if await self._handle_captcha(page, item.full_url, True):
                        html = await page.content()
                    else:
                        return item
                return parse_detail(html, item)
            except Exception as e:
                log.debug("fetch_details failed for %s: %s", item.id, e)
                return item

_browser: AvitoBrowser | None = None
_browser_lock = asyncio.Lock()

async def get_browser(session_manager: SessionManager | None = None) -> AvitoBrowser:
    global _browser
    if _browser is None:
        async with _browser_lock:
            if _browser is None:
                assert session_manager is not None
                _browser = AvitoBrowser(session_manager)
                await _browser.start()
    elif _browser._ctx is None:
        async with _browser_lock:
            if _browser is not None and _browser._ctx is None:
                if session_manager is not None:
                    _browser.session = session_manager
                await _browser.start()
    return _browser

async def shutdown_browser() -> None:
    global _browser
    if _browser is not None:
        await _browser.stop()
        _browser = None
