"""Discovery Mode: triage verdicts, card-only learning, deep-eval budget.

Regression guard for the Discovery rework:
  - un-baselined devices return "learn" (were silently skipped -> 0 deals);
  - Discovery learns baselines cheaply from the search CARD (no detail fetch);
  - the listing detail is opened only for "deal" candidates, bounded per cycle;
  - the Discovery screen never contradicts its own toggle state."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from techhunter import config, monitor
from techhunter.ai.normalize import normalize_device
from techhunter.ai.specs import extract_specs
from techhunter.scraper.models import ParsedListing
from techhunter.valuation.devices import (
    get_model_working_meta,
    get_or_create_device,
    set_manual_baseline,
)
from techhunter.valuation.engine import fast_value_listing


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def _item(item_id: str, title: str, price: int) -> ParsedListing:
    return ParsedListing(
        id=item_id, title=title, price=price,
        url=f"/items/{item_id}",
    )


async def _resolve_device(title: str) -> int:
    """Resolve the device id exactly the way the gate / learner do (storage
    parsed from the title), so a seeded baseline lands on the same row."""
    specs = extract_specs(title, "", {})
    norm = normalize_device(
        title, storage_gb=specs.storage_gb, ram_gb=specs.ram_gb
    )
    return await get_or_create_device(
        norm.brand, norm.model, norm.storage_gb, norm.ram_gb
    )


async def test_verdict_skip_unrecognized() -> None:
    v = await fast_value_listing(_item("d1", "Стол письменный дубовый", 5000),
                                 is_discovery=True)
    check("unrecognized -> skip", v == "skip")


async def test_verdict_learn_without_baseline() -> None:
    v = await fast_value_listing(_item("d2", "iPhone 11 64GB", 25000),
                                 is_discovery=True)
    check("no baseline + discovery -> learn", v == "learn")
    v2 = await fast_value_listing(_item("d2b", "iPhone 11 64GB", 25000),
                                  is_discovery=False)
    check("no baseline + subscription -> learn", v2 == "learn")


async def test_verdict_uses_model_fallback_without_storage() -> None:
    title = "iPhone 14 Pro"
    dev_id = await _resolve_device("iPhone 14 Pro 256GB")
    await set_manual_baseline(dev_id, 70000)
    base, sample, variants = await get_model_working_meta("apple", title)
    check("model fallback exists",
          base == 70000 and sample >= config.BASELINE_MIN_SAMPLE
          and variants == 1)
    cheap = await fast_value_listing(_item("d2c", title, 45000),
                                     is_discovery=True)
    check("no storage cheap -> deal/detail", cheap == "deal")
    pricey = await fast_value_listing(_item("d2d", title, 65000),
                                      is_discovery=True)
    check("no storage pricey -> skip", pricey == "skip")
    unknown = await fast_value_listing(_item("d2e", "Pixel 9 Pro", 50000),
                                       is_discovery=True)
    check("no storage no fallback -> learn/detail", unknown == "learn")


async def test_verdict_deal_vs_skip_with_baseline() -> None:
    title = "iPhone 13 Pro 128GB"
    dev_id = await _resolve_device(title)
    check("device created", dev_id is not None)
    await set_manual_baseline(dev_id, 50000)

    market = 50000 * (1 - config.PROFIT_HAGGLE_PERCENT)
    limit = market * config.FAST_VALUATION_THRESHOLD_PCT

    cheap = await fast_value_listing(_item("d3", title, int(limit * 0.5)),
                                     is_discovery=True)
    check("baseline + cheap -> deal", cheap == "deal")
    pricey = await fast_value_listing(_item("d4", title, int(market) + 5000),
                                      is_discovery=True)
    check("baseline + pricey -> skip", pricey == "skip")


async def test_learn_uses_card_not_detail() -> None:
    """Discovery learning logs an observation from the card and must NOT open
    the listing detail page (no process_new_listing call)."""
    from techhunter.valuation.devices import _prices  # noqa: PLC2701

    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    title = "Honor 90 256GB"
    dev_id = await _resolve_device(title)
    orig = monitor.process_new_listing
    monitor.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d7", title, 22000)
        counters = {"errors": 0, "processed": 0, "cached": 0}
        await monitor._evaluate(
            object(), item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget={"n": 5},
        )
    finally:
        monitor.process_new_listing = orig  # type: ignore

    check("detail NOT opened on learn", calls["n"] == 0)
    check("learning counted", counters.get("learning") == 1)
    prices = await _prices(dev_id, ("ideal", "good"))
    check("card observation logged", 22000 in prices)


async def test_storage_missing_opens_detail_for_learning() -> None:
    """If the search card has no storage, Discovery must not learn a
    storage-less market. It spends deep budget and opens detail so structured
    Avito params can provide memory/RAM."""
    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    orig = monitor.process_new_listing
    monitor.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d7b", "Samsung Galaxy S25 Ultra", 65000)
        budget = {"n": 1}
        counters = {"errors": 0, "processed": 0, "cached": 0}
        await monitor._evaluate(
            object(), item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget=budget,
        )
    finally:
        monitor.process_new_listing = orig  # type: ignore

    check("storage-missing detail opened", calls["n"] == 1)
    check("storage-missing budget decremented", budget["n"] == 0)
    check("storage-missing processed after detail",
          counters["processed"] == 1)


async def test_deal_budget_defers() -> None:
    """A deal candidate over the deep-eval budget is deferred (not opened,
    not cached) so it is retried next cycle."""
    from techhunter.storage import get_cached_listing

    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    title = "iPhone 12 128GB"
    dev_id = await _resolve_device(title)
    await set_manual_baseline(dev_id, 40000)

    orig = monitor.process_new_listing
    monitor.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d8", title, 10000)  # well under the gate -> "deal"
        counters = {"errors": 0, "processed": 0, "cached": 0}
        rep, val = await monitor._evaluate(
            object(), item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget={"n": 0},
        )
    finally:
        monitor.process_new_listing = orig  # type: ignore

    check("deferred returns nothing", rep is None and val is None)
    check("deferred did NOT open detail", calls["n"] == 0)
    check("deferred NOT cached (retried later)",
          await get_cached_listing(item.id) is None)


async def test_deal_budget_consumed() -> None:
    """Within budget a deal candidate is deep-evaluated and the budget drops."""
    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    title = "iPhone 12 Pro 256GB"
    dev_id = await _resolve_device(title)
    await set_manual_baseline(dev_id, 60000)

    orig = monitor.process_new_listing
    monitor.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d9", title, 15000)
        counters = {"errors": 0, "processed": 0, "cached": 0}
        budget = {"n": 1}
        await monitor._evaluate(
            object(), item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget=budget,
        )
    finally:
        monitor.process_new_listing = orig  # type: ignore

    check("deal opened detail", calls["n"] == 1)
    check("budget decremented", budget["n"] == 0)


async def test_screen_state_matches_button() -> None:
    """The Discovery screen must never contradict itself: the shown state and
    the action button always agree (regression for the toggle-confusion bug)."""
    from techhunter.bot.screens import screen_discovery
    from techhunter.storage import set_discovery_enabled, upsert_user

    tg = 555001
    await upsert_user(tg, "tester")

    await set_discovery_enabled(tg, True)
    text, kb = await screen_discovery(tg)
    btn = kb.inline_keyboard[0][0]
    check("ON -> shows ВКЛ", "ВКЛ" in text)
    check("ON -> button turns it OFF", btn.callback_data == "disc:off")
    check("ON -> button labelled Выключить", "Выключить" in btn.text)

    await set_discovery_enabled(tg, False)
    text, kb = await screen_discovery(tg)
    btn = kb.inline_keyboard[0][0]
    check("OFF -> shows ВЫКЛ", "ВЫКЛ" in text)
    check("OFF -> button turns it ON", btn.callback_data == "disc:on")
    check("OFF -> button labelled Включить", "Включить" in btn.text)


async def test_discovery_threshold_controls() -> None:
    """The /discovery screen exposes profit/margin +/- controls and reflects
    the stored per-user thresholds."""
    from techhunter.bot.screens import screen_discovery
    from techhunter.storage import set_discovery_profit, upsert_user

    tg = 555002
    await upsert_user(tg, "t2")
    await set_discovery_profit(tg, rub=4000, ratio=0.15)
    text, kb = await screen_discovery(tg)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    check("ratio reflected in text", "15%" in text)
    check("profit decrease button", "disc:profit:-1000" in cbs)
    check("profit increase button", "disc:profit:1000" in cbs)
    check("ratio decrease button", "disc:ratio:-5" in cbs)
    check("ratio increase button", "disc:ratio:5" in cbs)


async def test_learned_devices_screen() -> None:
    """The price-knowledge screen lists devices the bot has learned."""
    from techhunter.bot.screens import screen_learned
    from techhunter.valuation.devices import learned_devices

    d1 = await _resolve_device("iPhone 11 Pro 256GB")
    d2 = await _resolve_device("Honor 90 256GB")
    d3 = await get_or_create_device("apple", "iPhone NULLMEM TEST", None)
    await set_manual_baseline(d1, 41000)
    await set_manual_baseline(d2, 19000)
    await set_manual_baseline(d3, 50000)

    labels = [d["label"] for d in await learned_devices(limit=200)]
    check("iPhone in learned list", any("iPhone 11 Pro" in l for l in labels))
    check("Honor in learned list", any("Honor 90" in l for l in labels))
    check("no-memory hidden from learned list",
          not any("NULLMEM" in l for l in labels))

    text, _ = await screen_learned(0)
    check("learned screen header", "Что бот знает" in text)
    check("learned screen shows samples", "выборка" in text)


async def test_blocked_phone_not_a_deal() -> None:
    """A scammer-blocked / no-network phone must grade for_parts, never a
    working deal (regression: the bot delivered a blocked iPhone 13)."""
    from techhunter.ai.condition import grade_condition
    from techhunter.ai.specs import extract_specs

    title = "iPhone 13, 128 ГБ, SIM + eSIM"
    desc = "Заблокировали мошенники. Проблема с сеть. Ёмкость акб 85%. Коробка и чек."
    specs = extract_specs(title, desc)
    cond = grade_condition(specs, f"{title} {desc}".lower()).value
    check("blocked -> icloud_locked", "icloud_locked" in specs.defects)
    check("blocked -> for_parts", cond == "for_parts")

    clean = extract_specs("iPhone 13", "Идеал, нет проблем с сетью, не заблокирован")
    check("clean phone not flagged", "icloud_locked" not in clean.defects)


async def test_fast_poll_uses_hot_scan_window() -> None:
    """Fast polling must scan the configured newest-listings window, not only
    page 1. This is the actual deal-hunting path."""

    calls: list[dict] = []

    class FakeBrowser:
        def __init__(self) -> None:
            self.paused = asyncio.Event()

        async def search(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return []

    now = datetime.now(timezone.utc)
    default_sub = SimpleNamespace(
        id=901,
        tg_id=1,
        query="iphone",
        city_slug="rossiya",
        min_price=None,
        max_price=None,
        search_pages=None,
        onboarded_at=now,
    )
    custom_sub = SimpleNamespace(
        id=902,
        tg_id=1,
        query="samsung",
        city_slug="rossiya",
        min_price=None,
        max_price=None,
        search_pages=2,
        onboarded_at=now,
    )
    discovery_user = SimpleNamespace(
        tg_id=2,
        discovery_min_profit_rub=None,
        discovery_min_profit_ratio=None,
    )

    async def no_subs():
        return []

    async def no_discovery():
        return []

    async def one_default_sub():
        return [default_sub]

    async def one_custom_sub():
        return [custom_sub]

    async def one_discovery_user():
        return [discovery_user]

    orig_active = monitor.active_subscriptions
    orig_discovery = monitor.discovery_users
    orig_fast_pages = config.FAST_SCAN_PAGES
    orig_inprogress = set(monitor._onboard_inprogress)  # noqa: SLF001
    orig_tasks = set(monitor._onboard_tasks)  # noqa: SLF001
    try:
        config.FAST_SCAN_PAGES = 3
        browser = FakeBrowser()

        monitor.active_subscriptions = one_default_sub  # type: ignore[assignment]
        monitor.discovery_users = no_discovery  # type: ignore[assignment]
        await monitor.poll_fast(browser, object())
        check("subscription default scans hot pages",
              calls[-1]["kwargs"]["pages"] == 3)

        calls.clear()
        monitor.active_subscriptions = one_custom_sub  # type: ignore[assignment]
        await monitor.poll_fast(browser, object())
        check("subscription custom page count preserved",
              calls[-1]["kwargs"]["pages"] == 2)

        calls.clear()
        monitor.active_subscriptions = no_subs  # type: ignore[assignment]
        monitor.discovery_users = one_discovery_user  # type: ignore[assignment]
        await monitor.poll_fast(browser, object())
        check("discovery scans hot pages", calls[-1]["kwargs"]["pages"] == 3)
    finally:
        monitor.active_subscriptions = orig_active  # type: ignore[assignment]
        monitor.discovery_users = orig_discovery  # type: ignore[assignment]
        config.FAST_SCAN_PAGES = orig_fast_pages
        new_tasks = monitor._onboard_tasks - orig_tasks  # noqa: SLF001
        for task in list(new_tasks):
            task.cancel()
        monitor._onboard_tasks.clear()  # noqa: SLF001
        monitor._onboard_tasks.update(orig_tasks)  # noqa: SLF001
        monitor._onboard_inprogress.clear()  # noqa: SLF001
        monitor._onboard_inprogress.update(orig_inprogress)  # noqa: SLF001


def main() -> None:
    asyncio.run(test_verdict_skip_unrecognized())
    asyncio.run(test_verdict_learn_without_baseline())
    asyncio.run(test_verdict_uses_model_fallback_without_storage())
    asyncio.run(test_verdict_deal_vs_skip_with_baseline())
    asyncio.run(test_learn_uses_card_not_detail())
    asyncio.run(test_storage_missing_opens_detail_for_learning())
    asyncio.run(test_deal_budget_defers())
    asyncio.run(test_deal_budget_consumed())
    asyncio.run(test_screen_state_matches_button())
    asyncio.run(test_discovery_threshold_controls())
    asyncio.run(test_learned_devices_screen())
    asyncio.run(test_blocked_phone_not_a_deal())
    asyncio.run(test_fast_poll_uses_hot_scan_window())
    print("\nAll discovery checks passed.")


if __name__ == "__main__":
    main()
