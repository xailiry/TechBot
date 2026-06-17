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

from techhunter import config
from techhunter.ai.normalize import normalize_device
from techhunter.ai.specs import extract_specs
from techhunter.db import get_session
from techhunter.db.repository import RepositoryContainer
from techhunter.monitoring import poller as poller_module
from techhunter.monitoring.poller import AvitoPoller
from techhunter.notifier import ConsoleNotifier
from techhunter.scraper.models import ParsedListing
from techhunter.valuation.devices import (
    get_model_working_meta,
    get_or_create_device,
    set_manual_baseline,
)
from techhunter.valuation.engine import (
    fast_value_listing,
    learn_from_card,
    learn_from_detail,
    should_fetch_detail_for_learning,
)


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def _item(item_id: str, title: str, price: int) -> ParsedListing:
    return ParsedListing(
        id=item_id, title=title, price=price,
        url=f"/items/{item_id}",
    )


def _make_poller(browser=None) -> AvitoPoller:
    """A poller wired with real repos, the way MonitorManager builds it."""
    if browser is None:
        browser = SimpleNamespace(session=SimpleNamespace(is_paused=False))
    return AvitoPoller(
        RepositoryContainer(get_session), browser, ConsoleNotifier(beep=False)
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


async def test_android_missing_ram_uses_storage_fallback() -> None:
    dev_id = await get_or_create_device(
        "samsung", "Galaxy S25 Ultra", 256, 12
    )
    await set_manual_baseline(dev_id, 60000)

    verdict = await fast_value_listing(
        _item("d2-ram-fallback", "Samsung Galaxy S25 Ultra 256GB", 45000),
        is_discovery=True,
    )
    check("android no-ram title uses storage fallback", verdict == "deal")


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


async def test_iphone_16e_does_not_use_iphone_16_baseline() -> None:
    regular = await _resolve_device("iPhone 16 128GB")
    budget = await _resolve_device("iPhone 16e 128GB")
    await set_manual_baseline(regular, 43000)
    await set_manual_baseline(budget, 33000)

    verdict = await fast_value_listing(
        _item("d16e", "iPhone 16e, 128 ГБ, SIM + eSIM", 31000),
        is_discovery=True,
    )
    check("16e uses own baseline", verdict == "skip")


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
    orig = poller_module.process_new_listing
    poller_module.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d7", title, 22000)
        counters = {"errors": 0, "processed": 0, "cached": 0}
        await _make_poller()._evaluate(
            item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget={"n": 5},
        )
    finally:
        poller_module.process_new_listing = orig  # type: ignore

    check("detail NOT opened on learn", calls["n"] == 0)
    check("learning counted", counters.get("learning") == 1)
    prices = await _prices(dev_id, ("ideal", "good"))
    check("card observation logged", 22000 in prices)


async def test_extreme_avito_badges_do_not_teach_baseline() -> None:
    from techhunter.valuation.devices import _prices  # noqa: PLC2701

    title = "Honor 90 256GB"
    dev_id = await _resolve_device(title)
    item = _item("d7-below", title, 18000)
    item.avito_price_badge = "below"
    item.avito_market_badge = True

    learned = await learn_from_card(item)
    prices = await _prices(dev_id, ("ideal", "good"))
    check("below-market badge not learned", learned is False and 18000 not in prices)


async def test_training_skips_emoji_shop_cards() -> None:
    from techhunter.valuation.devices import _prices  # noqa: PLC2701

    title = "Honor 90 256GB"
    dev_id = await _resolve_device(title)
    item = _item("d7-emoji", title, 21000)
    item.snippet = "🔥🔥🔥✅✅ гарантия, рассрочка, большой выбор"

    learned = await learn_from_card(item, source="training")
    prices = await _prices(dev_id, ("ideal", "good"))
    check("emoji/shop card not learned", learned is False and 21000 not in prices)


async def test_training_detail_learning_for_fresh_iphone() -> None:
    from techhunter.ai.evaluate import evaluate_listing
    from techhunter.valuation.devices import _prices  # noqa: PLC2701

    item = _item("d17-detail", "iPhone 17 Pro 256GB", 87000)
    check("fresh iPhone selected for detail learning",
          should_fetch_detail_for_learning(item) is True)

    item.description = "Used phone, battery health 98%, everything works."
    item.params = {"встроенная память": "256 ГБ"}
    rep = await evaluate_listing(item, run_clip=False, do_dedup=False)
    did = await learn_from_detail(item, rep, source="training_detail")
    prices = await _prices(did, ("ideal", "good")) if did else []
    check("fresh iPhone detail observation logged",
          did is not None and 87000 in prices)


async def test_fresh_iphone_learning_opens_detail() -> None:
    """Fresh Apple models are too easy to pollute with new/reseller stock from
    search cards, so Discovery must spend deep budget and inspect detail."""
    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    orig = poller_module.process_new_listing
    poller_module.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d17", "iPhone 17 Pro 256GB", 85000)
        budget = {"n": 1}
        counters = {"errors": 0, "processed": 0, "cached": 0}
        await _make_poller()._evaluate(
            item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget=budget,
        )
    finally:
        poller_module.process_new_listing = orig  # type: ignore

    check("fresh iPhone opens detail for learning", calls["n"] == 1)
    check("fresh iPhone budget decremented", budget["n"] == 0)


async def test_storage_missing_opens_detail_for_learning() -> None:
    """If the search card has no storage, Discovery must not learn a
    storage-less market. It spends deep budget and opens detail so structured
    Avito params can provide memory/RAM."""
    calls = {"n": 0}

    async def _fake_process(browser, it, mode="fast"):
        calls["n"] += 1
        return None, None

    orig = poller_module.process_new_listing
    poller_module.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d7b", "Samsung Galaxy S25 Ultra", 40000)
        budget = {"n": 1}
        counters = {"errors": 0, "processed": 0, "cached": 0}
        await _make_poller()._evaluate(
            item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget=budget,
        )
    finally:
        poller_module.process_new_listing = orig  # type: ignore

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

    orig = poller_module.process_new_listing
    poller_module.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d8", title, 10000)  # well under the gate -> "deal"
        counters = {"errors": 0, "processed": 0, "cached": 0}
        rep, val = await _make_poller()._evaluate(
            item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget={"n": 0},
        )
    finally:
        poller_module.process_new_listing = orig  # type: ignore

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

    orig = poller_module.process_new_listing
    poller_module.process_new_listing = _fake_process  # type: ignore
    try:
        item = _item("d9", title, 15000)
        counters = {"errors": 0, "processed": 0, "cached": 0}
        budget = {"n": 1}
        await _make_poller()._evaluate(
            item, asyncio.Semaphore(1), {}, counters,
            mode="fast", is_discovery=True, deep_budget=budget,
        )
    finally:
        poller_module.process_new_listing = orig  # type: ignore

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
    check("strict preset", "disc:preset:strict" in cbs)
    check("balance preset", "disc:preset:balance" in cbs)
    check("aggressive preset", "disc:preset:aggressive" in cbs)


async def test_discovery_scan_mode_controls() -> None:
    from techhunter.bot.screens import screen_discovery
    from techhunter.storage import set_discovery_scan_mode, upsert_user

    tg = 555003
    await upsert_user(tg, "t3")
    await set_discovery_scan_mode(tg, "careful")
    text, kb = await screen_discovery(tg)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    check("careful mode reflected in screen", "3-5" in text)
    check("fast mode button exists", "disc:mode:fast" in cbs)
    check("careful mode button exists", "disc:mode:careful" in cbs)


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
    check("iPhone in learned list", any("iPhone 11 Pro" in label for label in labels))
    check("Honor in learned list", any("Honor 90" in label for label in labels))
    check("no-memory hidden from learned list",
          not any("NULLMEM" in label for label in labels))

    text, _ = await screen_learned(0)
    check("learned screen header", "База цен" in text)
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
            self.session = SimpleNamespace(is_paused=False)

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

    orig_fast_pages = config.FAST_SCAN_PAGES
    try:
        config.FAST_SCAN_PAGES = 3
        poller = _make_poller(FakeBrowser())

        poller.repos.subscriptions.active_subscriptions = one_default_sub
        poller.repos.users.discovery_users = no_discovery
        await poller.poll_fast()
        check("subscription default scans hot pages",
              calls[-1]["kwargs"]["pages"] == 3)

        calls.clear()
        poller.repos.subscriptions.active_subscriptions = one_custom_sub
        await poller.poll_fast()
        check("subscription custom page count preserved",
              calls[-1]["kwargs"]["pages"] == 2)

        calls.clear()
        poller.repos.subscriptions.active_subscriptions = no_subs
        poller.repos.users.discovery_users = one_discovery_user
        await poller.poll_fast()
        check("discovery scans hot pages", calls[-1]["kwargs"]["pages"] == 3)
    finally:
        config.FAST_SCAN_PAGES = orig_fast_pages
        for task in list(poller._onboard_tasks):
            task.cancel()


async def test_fast_poll_groups_and_parallelizes_searches() -> None:
    calls: list[str] = []
    active = 0
    max_active = 0

    class FakeBrowser:
        def __init__(self) -> None:
            self.session = SimpleNamespace(is_paused=False)

        async def search(self, query, *args, **kwargs):
            nonlocal active, max_active
            calls.append(query)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.03)
            active -= 1
            return []

    now = datetime.now(timezone.utc)

    def _sub(sid: int, tg_id: int, query: str):
        return SimpleNamespace(
            id=sid,
            tg_id=tg_id,
            query=query,
            city_slug="rossiya",
            min_price=None,
            max_price=None,
            min_battery=None,
            search_pages=1,
            onboarded_at=now,
        )

    subs = [
        _sub(910, 10, "iphone"),
        _sub(911, 11, "iphone"),
        _sub(912, 12, "samsung"),
    ]

    async def active_subs():
        return subs

    async def no_discovery():
        return []

    handled: list[int] = []

    async def fake_handle(items, target, *args, **kwargs):
        handled.append(target.tg_id)

    poller = _make_poller(FakeBrowser())
    poller.repos.subscriptions.active_subscriptions = active_subs
    poller.repos.users.discovery_users = no_discovery
    poller._handle_items = fake_handle  # type: ignore[method-assign]
    await poller.poll_fast()

    check("identical subscriptions share one search",
          sorted(calls) == ["iphone", "samsung"])
    check("different searches run in parallel", max_active == 2)
    check("shared results still reach every subscriber",
          sorted(handled) == [10, 11, 12])


async def test_careful_discovery_is_throttled_and_has_no_deep_scan() -> None:
    calls: list[dict] = []
    handled: list[dict] = []

    class FakeBrowser:
        def __init__(self) -> None:
            self.session = SimpleNamespace(is_paused=False)

        async def search(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return []

    careful_user = SimpleNamespace(
        tg_id=20,
        discovery_scan_mode="careful",
        discovery_min_profit_rub=None,
        discovery_min_profit_ratio=None,
    )

    async def no_subs():
        return []

    async def one_careful_user():
        return [careful_user]

    async def fake_handle(items, target, *args, **kwargs):
        handled.append(kwargs)

    old_values = (
        config.DISCOVERY_CAREFUL_INTERVAL_MIN_SEC,
        config.DISCOVERY_CAREFUL_INTERVAL_MAX_SEC,
        config.DISCOVERY_CAREFUL_PAGES,
        config.DISCOVERY_CAREFUL_DEEP_PER_CYCLE,
        config.DISCOVERY_CAREFUL_DETAIL_DELAY_SEC,
        config.DISCOVERY_CAREFUL_SETTLE_DELAY_SEC,
    )
    try:
        config.DISCOVERY_CAREFUL_INTERVAL_MIN_SEC = 1000
        config.DISCOVERY_CAREFUL_INTERVAL_MAX_SEC = 1000
        config.DISCOVERY_CAREFUL_PAGES = 1
        config.DISCOVERY_CAREFUL_DEEP_PER_CYCLE = 3
        config.DISCOVERY_CAREFUL_DETAIL_DELAY_SEC = (0.0, 0.0)
        config.DISCOVERY_CAREFUL_SETTLE_DELAY_SEC = (0.0, 0.0)

        poller = _make_poller(FakeBrowser())
        poller.repos.subscriptions.active_subscriptions = no_subs
        poller.repos.users.discovery_users = one_careful_user
        poller._handle_items = fake_handle  # type: ignore[method-assign]

        await poller.poll_fast()
        await poller.poll_fast()
        await poller.poll_deep()
    finally:
        (
            config.DISCOVERY_CAREFUL_INTERVAL_MIN_SEC,
            config.DISCOVERY_CAREFUL_INTERVAL_MAX_SEC,
            config.DISCOVERY_CAREFUL_PAGES,
            config.DISCOVERY_CAREFUL_DEEP_PER_CYCLE,
            config.DISCOVERY_CAREFUL_DETAIL_DELAY_SEC,
            config.DISCOVERY_CAREFUL_SETTLE_DELAY_SEC,
        ) = old_values

    check("careful discovery scans only once before cooldown", len(calls) == 1)
    check("careful discovery scans one page",
          calls[0]["kwargs"]["pages"] == 1)
    check("careful discovery uses slower page settle",
          calls[0]["kwargs"]["settle_delay"] == (0.0, 0.0))
    check("careful discovery limits detail budget",
          handled and handled[0]["deep_budget"]["n"] == 3)
    check("careful discovery passes detail delay",
          handled[0]["detail_delay"] == (0.0, 0.0))


async def test_deep_poll_starts_after_fast_window() -> None:
    calls: list[dict] = []

    class FakeBrowser:
        def __init__(self) -> None:
            self.session = SimpleNamespace(is_paused=False)

        async def search(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return []

    discovery_user = SimpleNamespace(
        tg_id=3,
        discovery_min_profit_rub=None,
        discovery_min_profit_ratio=None,
    )

    async def one_discovery_user():
        return [discovery_user]

    orig_fast_pages = config.FAST_SCAN_PAGES
    orig_deep_pages = config.DEEP_SCAN_PAGES
    try:
        config.FAST_SCAN_PAGES = 3
        config.DEEP_SCAN_PAGES = 12
        poller = _make_poller(FakeBrowser())
        poller.repos.users.discovery_users = one_discovery_user
        await poller.poll_deep()
    finally:
        config.FAST_SCAN_PAGES = orig_fast_pages
        config.DEEP_SCAN_PAGES = orig_deep_pages

    check("deep starts at page 4", calls[-1]["kwargs"]["start_page"] == 4)
    check("deep scans remaining pages", calls[-1]["kwargs"]["pages"] == 9)
    check("deep uses deep browser pool", calls[-1]["kwargs"]["mode"] == "deep")


async def test_training_mode_skips_fast_and_deep() -> None:
    calls: list[dict] = []

    class FakeBrowser:
        def __init__(self) -> None:
            self.session = SimpleNamespace(is_paused=False)

        async def search(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return []

    now = datetime.now(timezone.utc)
    sub = SimpleNamespace(
        id=903,
        tg_id=1,
        query="iphone",
        city_slug="rossiya",
        min_price=None,
        max_price=None,
        search_pages=None,
        onboarded_at=now,
    )
    discovery_user = SimpleNamespace(
        tg_id=4,
        discovery_min_profit_rub=None,
        discovery_min_profit_ratio=None,
    )

    async def one_sub():
        return [sub]

    async def one_discovery_user():
        return [discovery_user]

    from techhunter import runtime

    poller = _make_poller(FakeBrowser())
    poller.repos.subscriptions.active_subscriptions = one_sub
    poller.repos.users.discovery_users = one_discovery_user
    try:
        runtime.set_training_mode(True)
        await poller.poll_fast()
        await poller.poll_deep()
    finally:
        runtime.set_training_mode(False)

    check("training skips fast/deep searches", calls == [])


def main() -> None:
    asyncio.run(test_verdict_skip_unrecognized())
    asyncio.run(test_verdict_learn_without_baseline())
    asyncio.run(test_verdict_uses_model_fallback_without_storage())
    asyncio.run(test_android_missing_ram_uses_storage_fallback())
    asyncio.run(test_verdict_deal_vs_skip_with_baseline())
    asyncio.run(test_iphone_16e_does_not_use_iphone_16_baseline())
    asyncio.run(test_learn_uses_card_not_detail())
    asyncio.run(test_extreme_avito_badges_do_not_teach_baseline())
    asyncio.run(test_training_skips_emoji_shop_cards())
    asyncio.run(test_training_detail_learning_for_fresh_iphone())
    asyncio.run(test_fresh_iphone_learning_opens_detail())
    asyncio.run(test_storage_missing_opens_detail_for_learning())
    asyncio.run(test_deal_budget_defers())
    asyncio.run(test_deal_budget_consumed())
    asyncio.run(test_screen_state_matches_button())
    asyncio.run(test_discovery_threshold_controls())
    asyncio.run(test_discovery_scan_mode_controls())
    asyncio.run(test_learned_devices_screen())
    asyncio.run(test_blocked_phone_not_a_deal())
    asyncio.run(test_fast_poll_uses_hot_scan_window())
    asyncio.run(test_fast_poll_groups_and_parallelizes_searches())
    asyncio.run(test_careful_discovery_is_throttled_and_has_no_deep_scan())
    asyncio.run(test_deep_poll_starts_after_fast_window())
    asyncio.run(test_training_mode_skips_fast_and_deep())
    print("\nAll discovery checks passed.")


if __name__ == "__main__":
    main()
