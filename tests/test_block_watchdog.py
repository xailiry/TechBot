"""Stage P0-c: robust block detection + watchdog. Pure, no network."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import sys

from techhunter import config
from techhunter.monitor import watchdog_decide
from techhunter.scraper.parser import (
    has_detail,
    has_listings,
    is_block_page,
    page_blocked,
)


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def test_block_detection() -> None:
    results = '<div data-marker="item" data-item-id="1">iPhone</div>'
    detail = '<div data-marker="item-view/title-info">iPhone 15</div>'
    block = "<html><title>Доступ ограничен</title>проблема с ip</html>"
    empty = "<html><body>ничего не найдено</body></html>"

    check("has_listings", has_listings(results) and not has_listings(empty))
    check("has_detail", has_detail(detail) and not has_detail(results))

    # Key anti-false-positive: legit page with results that ALSO contains
    # the word datadome/captcha in a bundle must NOT be 'blocked'.
    legit_with_bundle = results + "<script>window.datadome captcha</script>"
    check("results+bundle not blocked",
          not page_blocked(legit_with_bundle))
    check("is_block_page ignores bare datadome",
          not is_block_page("<script>datadome captcha sdk</script>"))

    check("real block page", is_block_page(block)
          and page_blocked(block))
    check("empty search is NOT a block", not page_blocked(empty))
    check("detail block", page_blocked(block, detail=True))
    check("detail ok not block", not page_blocked(detail, detail=True))
    check("none html safe", not page_blocked(None))


def test_watchdog() -> None:
    st: dict = {}
    now = 100000.0

    # Healthy: recent cycle, items scraped -> nothing.
    snap = {"last_cycle_at": now - 10, "subs": 2, "scraped": 50,
            "captcha": False}
    check("healthy -> no alerts",
          watchdog_decide(snap, st, now) == [])

    # Stall: no completed cycle for > WATCHDOG_STALL_SEC, not captcha.
    old = now - config.WATCHDOG_STALL_SEC - 60
    a = watchdog_decide(
        {"last_cycle_at": old, "subs": 2, "scraped": 0, "captcha": False},
        st, now,
    )
    check("stall alert", any("завис" in x for x in a))
    # Throttled: same key within repeat window -> no duplicate.
    a2 = watchdog_decide(
        {"last_cycle_at": old, "subs": 2, "scraped": 0, "captcha": False},
        st, now + 10,
    )
    check("stall throttled", not any("завис" in x for x in a2))

    # Captcha suppresses stall.
    st2: dict = {}
    a3 = watchdog_decide(
        {"last_cycle_at": old, "subs": 2, "scraped": 0, "captcha": True},
        st2, now,
    )
    check("captcha suppresses stall", a3 == [])

    # Dry: subs>0, scraped==0, not captcha, longer than WATCHDOG_DRY_SEC.
    st3: dict = {}
    base = {"last_cycle_at": now, "subs": 2, "scraped": 0, "captcha": False}
    watchdog_decide(base, st3, now)  # arms dry_since
    dry = watchdog_decide(
        base, st3, now + config.WATCHDOG_DRY_SEC + 1
    )
    check("dry alert", any("ничего не собирается" in x for x in dry))
    # scraped > 0 resets dry timer.
    watchdog_decide(
        {**base, "scraped": 10}, st3, now + config.WATCHDOG_DRY_SEC + 2
    )
    check("dry reset on scrape", st3.get("dry_since") is None)


def main() -> None:
    test_block_detection()
    test_watchdog()
    print("\nAll block/watchdog checks passed.")


if __name__ == "__main__":
    main()
