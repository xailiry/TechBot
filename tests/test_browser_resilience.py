"""Stage P0-a: page-pool never deadlocks + crash restart logic.
Pure asyncio, no real Playwright."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys

from techhunter import config
from techhunter.scraper.browser import AvitoBrowser
from techhunter.scraper.session import SessionManager


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self) -> None:
        self.detected = 0
        self.cleared = 0

    async def captcha_detected(self, url: str) -> None:
        self.detected += 1

    async def captcha_cleared(self) -> None:
        self.cleared += 1


def test_is_closed_error() -> None:
    yes = [
        "Target page, context or browser has been closed",
        "TargetClosedError: ...",
        "websocket connection closed",
        "Page has been closed",
    ]
    for m in yes:
        check(f"closed: {m[:24]}", AvitoBrowser._is_closed_error(m))
    for m in ["net::ERR_TIMED_OUT", "Timeout 30000ms exceeded", "boom"]:
        check(f"not closed: {m[:18]}",
              not AvitoBrowser._is_closed_error(m))


async def test_acquire_pooled() -> None:
    b = AvitoBrowser()

    async def _noop():
        return None

    b.start = _noop  # type: ignore
    b._available = asyncio.Queue()
    p1 = FakePage()
    b._available.put_nowait(p1)
    async with b.acquire_page() as pg:
        check("pooled page yielded", pg is p1)
    check("pooled page returned", b._available.qsize() == 1)
    check("pooled page not closed", p1.closed is False)


async def test_acquire_transient_on_exhaust() -> None:
    b = AvitoBrowser()

    async def _noop():
        return None

    transient = FakePage()

    async def _new_page():
        return transient

    b.start = _noop  # type: ignore
    b._new_stealth_page = _new_page  # type: ignore
    b._available = asyncio.Queue()  # empty -> exhausted
    old = config.PAGE_ACQUIRE_TIMEOUT_SEC
    config.PAGE_ACQUIRE_TIMEOUT_SEC = 0.05
    try:
        async with b.acquire_page() as pg:
            check("transient page yielded", pg is transient)
    finally:
        config.PAGE_ACQUIRE_TIMEOUT_SEC = old
    check("transient closed on release", transient.closed is True)
    check("pool not poisoned", b._available.qsize() == 0)


async def test_transient_tabs_are_capped() -> None:
    b = AvitoBrowser()

    async def _noop():
        return None

    created = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _new_page():
        nonlocal created
        created += 1
        return FakePage()

    async def _first_user():
        async with b.acquire_page():
            first_entered.set()
            await release_first.wait()

    b.start = _noop  # type: ignore
    b._new_stealth_page = _new_page  # type: ignore
    b._available = asyncio.Queue()
    old_timeout = config.PAGE_ACQUIRE_TIMEOUT_SEC
    old_cap = config.MAX_TRANSIENT_PAGES
    config.PAGE_ACQUIRE_TIMEOUT_SEC = 0.01
    config.MAX_TRANSIENT_PAGES = 1
    b._transient_slots = asyncio.Semaphore(1)
    try:
        first = asyncio.create_task(_first_user())
        await first_entered.wait()
        second = asyncio.create_task(_first_user())
        await asyncio.sleep(0.05)
        check("only one transient tab created", created == 1)
        release_first.set()
        await asyncio.gather(first, second)
    finally:
        config.PAGE_ACQUIRE_TIMEOUT_SEC = old_timeout
        config.MAX_TRANSIENT_PAGES = old_cap


async def test_release_after_pool_swap() -> None:
    b = AvitoBrowser()

    async def _noop():
        return None

    b.start = _noop  # type: ignore
    b._available = asyncio.Queue()
    p1 = FakePage()
    b._available.put_nowait(p1)
    async with b.acquire_page() as pg:
        check("got p1", pg is p1)
        b._available = asyncio.Queue()  # simulate restart swapping the pool
    check("stale page not put into new pool", b._available.qsize() == 0)
    check("stale page closed/dropped", p1.closed is True)


async def test_restart_rate_limited() -> None:
    b = AvitoBrowser()
    calls = {"stop": 0, "start": 0}

    async def _stop():
        calls["stop"] += 1

    async def _start():
        calls["start"] += 1

    b.stop = _stop  # type: ignore
    b.start = _start  # type: ignore
    b._last_restart = 0.0
    await b.restart()
    await b.restart()  # within cooldown -> skipped
    check("restart rate-limited", calls["start"] == 1)
    # Non-closed error must not trigger restart.
    calls["start"] = 0
    b._last_restart = 0.0
    await b._maybe_restart("Timeout 30000ms exceeded")
    check("no restart on timeout", calls["start"] == 0)


async def test_pools_independent() -> None:
    b = AvitoBrowser()

    async def _noop():
        return None

    b.start = _noop  # type: ignore
    b._fast_pool = asyncio.Queue()
    b._deep_pool = asyncio.Queue()
    
    p_fast = FakePage()
    p_deep = FakePage()
    b._fast_pool.put_nowait(p_fast)
    b._deep_pool.put_nowait(p_deep)
    
    async with b.acquire_page(mode="fast") as pg:
        check("got fast page", pg is p_fast)
    check("fast pool size returned", b._fast_pool.qsize() == 1)
    
    async with b.acquire_page(mode="deep") as pg:
        check("got deep page", pg is p_deep)
    check("deep pool size returned", b._deep_pool.qsize() == 1)


async def test_captcha_notify_force_bypasses_throttle() -> None:
    notifier = FakeNotifier()
    b = AvitoBrowser(notifier=notifier)  # type: ignore[arg-type]
    b._last_captcha_notify = 10**12

    ok = await b._notify_captcha_detected("https://www.avito.ru/captcha", force=True)
    blocked = await b._notify_captcha_detected(
        "https://www.avito.ru/captcha", force=False
    )

    check("captcha notify forced", ok and notifier.detected == 1)
    check("captcha notify reminder throttled", not blocked and notifier.detected == 1)


def test_captcha_overlay_never_counts_as_cleared() -> None:
    listings = '<div data-marker="catalog-serp"><div data-marker="item"></div></div>'
    overlay = listings + '<div data-marker="captcha">solve</div>'
    check(
        "stale listings under captcha stay blocked",
        not AvitoBrowser._captcha_is_cleared(overlay, is_detail=False),
    )
    check(
        "plain listings count as cleared",
        AvitoBrowser._captcha_is_cleared(listings, is_detail=False),
    )


async def test_parallel_captcha_callers_share_one_result() -> None:
    notifier = FakeNotifier()
    session = SessionManager(notifier)  # type: ignore[arg-type]
    checks = 0

    async def _check() -> bool:
        nonlocal checks
        checks += 1
        return True

    old_recheck = config.CAPTCHA_RECHECK_SEC
    config.CAPTCHA_RECHECK_SEC = 1
    try:
        first = asyncio.create_task(
            session.handle_captcha("https://www.avito.ru/captcha", _check)
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            session.handle_captcha("https://www.avito.ru/captcha", _check)
        )
        results = await asyncio.gather(first, second)
    finally:
        config.CAPTCHA_RECHECK_SEC = old_recheck

    check("parallel captcha callers share success", results == [True, True])
    check("captcha checked by one owner", checks == 1)
    check("captcha detected once", notifier.detected == 1)
    check("captcha cleared once", notifier.cleared == 1)


async def test_parallel_captcha_failure_is_shared() -> None:
    notifier = FakeNotifier()
    session = SessionManager(notifier)  # type: ignore[arg-type]

    async def _still_blocked() -> bool:
        return False

    old_recheck = config.CAPTCHA_RECHECK_SEC
    old_wait = config.CAPTCHA_MAX_WAIT_SEC
    config.CAPTCHA_RECHECK_SEC = 1
    config.CAPTCHA_MAX_WAIT_SEC = 1
    try:
        first = asyncio.create_task(
            session.handle_captcha(
                "https://www.avito.ru/captcha", _still_blocked
            )
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            session.handle_captcha(
                "https://www.avito.ru/captcha", _still_blocked
            )
        )
        results = await asyncio.gather(first, second)
    finally:
        config.CAPTCHA_RECHECK_SEC = old_recheck
        config.CAPTCHA_MAX_WAIT_SEC = old_wait

    check("parallel captcha callers share failure", results == [False, False])
    check("failed captcha detected once", notifier.detected == 1)
    check("failed captcha never reported cleared", notifier.cleared == 0)


async def test_solved_captcha_page_not_double_reloaded() -> None:
    b = AvitoBrowser()
    page = FakePage()
    reloads = {"count": 0}

    async def _wait(url: str) -> bool:
        b._captcha_solved_page = page  # type: ignore[assignment]
        return True

    async def _reload(pg) -> None:
        reloads["count"] += 1

    b._run_captcha_wait = _wait  # type: ignore[method-assign]
    b._reload = _reload  # type: ignore[method-assign]

    ok = await b._ensure_unblocked(page, "https://www.avito.ru/captcha")  # type: ignore[arg-type]
    check("captcha solved page resumes", ok)
    check("captcha solved page not reloaded twice", reloads["count"] == 0)


async def test_other_blocked_page_reloaded_after_solve() -> None:
    b = AvitoBrowser()
    page = FakePage()
    solved_elsewhere = FakePage()
    reloads = {"count": 0}

    async def _wait(url: str) -> bool:
        b._captcha_solved_page = solved_elsewhere  # type: ignore[assignment]
        return True

    async def _reload(pg) -> None:
        reloads["count"] += 1

    b._run_captcha_wait = _wait  # type: ignore[method-assign]
    b._reload = _reload  # type: ignore[method-assign]

    ok = await b._ensure_unblocked(page, "https://www.avito.ru/captcha")  # type: ignore[arg-type]
    check("other blocked page resumes", ok)
    check("other blocked page reloaded", reloads["count"] == 1)


def main() -> None:
    test_is_closed_error()
    asyncio.run(test_acquire_pooled())
    asyncio.run(test_acquire_transient_on_exhaust())
    asyncio.run(test_transient_tabs_are_capped())
    asyncio.run(test_release_after_pool_swap())
    asyncio.run(test_restart_rate_limited())
    asyncio.run(test_pools_independent())
    asyncio.run(test_captcha_notify_force_bypasses_throttle())
    test_captcha_overlay_never_counts_as_cleared()
    asyncio.run(test_parallel_captcha_callers_share_one_result())
    asyncio.run(test_parallel_captcha_failure_is_shared())
    asyncio.run(test_solved_captcha_page_not_double_reloaded())
    asyncio.run(test_other_blocked_page_reloaded_after_solve())
    print("\nAll browser-resilience checks passed.")


if __name__ == "__main__":
    main()
