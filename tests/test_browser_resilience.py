"""Stage P0-a: page-pool never deadlocks + crash restart logic.
Pure asyncio, no real Playwright."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys

from techhunter import config
from techhunter.scraper.browser import AvitoBrowser


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


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


def main() -> None:
    test_is_closed_error()
    asyncio.run(test_acquire_pooled())
    asyncio.run(test_acquire_transient_on_exhaust())
    asyncio.run(test_release_after_pool_swap())
    asyncio.run(test_restart_rate_limited())
    asyncio.run(test_pools_independent())
    print("\nAll browser-resilience checks passed.")


if __name__ == "__main__":
    main()
