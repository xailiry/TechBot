"""Stage 4 verification (no network, no live Telegram). Idempotent per run.

Run: ../venv/Scripts/python.exe -m tests.test_stage4  (from project root)
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys
import uuid

from techhunter.ai.evaluate import EvaluationReport
from techhunter.bot.cards import build_action_kb, haggle_text
from techhunter.bot.format import (
    build_haggle_text,
    format_deal_card,
    parse_command,
)
from techhunter.scraper.models import ParsedListing
from techhunter.valuation.engine import Valuation


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def test_parser() -> None:
    p = parse_command("iphone 13 pro | до:55000 | москва | акб:85")
    check("query", p["query"] == "iphone 13 pro")
    check("max_price", p["max_price"] == 55000)
    check("city slug", p["city_slug"] == "moskva")
    check("battery", p["min_battery"] == 85)

    p2 = parse_command("samsung s23 | акб:90 | до:30000")
    check("order-free price", p2["max_price"] == 30000)
    check("order-free battery", p2["min_battery"] == 90)

    p3 = parse_command("iphone 12")
    check("query only", p3["query"] == "iphone 12"
          and p3["max_price"] is None and p3["city_slug"] == "rossiya")

    check("empty -> None", parse_command("") is None)
    check("blank -> None", parse_command("   ") is None)

    p4 = parse_command("iphone | 50000 | 85")
    check("bare 50000 -> price", p4["max_price"] == 50000)
    check("bare 85 -> battery", p4["min_battery"] == 85)


def _report(**kw):
    base = dict(
        listing_id="r1", brand="apple", model="iPhone 13 Pro",
        storage_gb=256, condition="broken", battery_health=88,
        defects=["screen_cracked"], is_rostest=True, is_sealed=False,
        visual={"is_stock_photo": True}, reused_image_count=0,
    )
    base.update(kw)
    return EvaluationReport(**base)


def _val(**kw):
    base = dict(
        listing_id="r1", device_key="apple iPhone 13 Pro 256GB",
        condition="broken", baseline_price=80000, repair_cost=9000,
        repair_breakdown={"screen": 9000}, gross_profit=50000,
        net_profit=41000, profit_pct=0.5125, opportunity=True,
        opportunity_type="broken_flip", scam_score=70,
        scam_verdict="unknown", pros=["частник"], cons=[],
        missing=[],
    )
    base.update(kw)
    return Valuation(**base)


def _item(**kw):
    base = dict(
        id=f"s4-{uuid.uuid4().hex}", title="iPhone 13 Pro 256 разбит экран",
        price=30000, url="/x/1", location="Москва, м. Тверская",
        description="разбит экран, всё работает", seller_name="Иван",
        seller_listings=3,
    )
    base.update(kw)
    return ParsedListing(**base)


def test_card_text() -> None:
    txt = format_deal_card(_item(), _report(), _val(), "iphone 13 pro")
    check("has title", "iPhone 13 Pro 256" in txt)
    check("has price", "30 000 ₽" in txt)
    check("broken post-repair", "после ремонта" in txt)
    check("repair line", "Ремонт" in txt and "9 000" in txt)
    est_txt = format_deal_card(
        _item(), _report(), _val(repair_estimated=True), "iphone 13 pro"
    )
    check("estimated repair labelled", "оценочно" in est_txt)
    check("AI battery", "🔋 88%" in txt)
    check("defect RU", "разбит экран" in txt)
    check("visual RU", "стоковое" in txt)
    check("scam line", "70/100" in txt)
    check("sub line", "iphone 13 pro" in txt)
    ct = format_deal_card(
        _item(), _report(),
        _val(baseline_confidence="medium", baseline_sample=14),
        "",
    )
    check("confidence line", "Уверенность: средняя" in ct
          and "выборка 14" in ct)

    # Working deal: plain profit, no repair line.
    wt = format_deal_card(
        _item(), _report(condition="good", defects=[]),
        _val(condition="good", opportunity_type="working",
             repair_cost=None, repair_breakdown={}, net_profit=20000,
             profit_pct=0.25),
        "",
    )
    check("working profit", "Профит: ~20 000" in wt)
    check("working no repair", "после ремонта" not in wt)

    # Missing reasons rendered.
    mt = format_deal_card(
        _item(), _report(),
        _val(missing=["repair_cost_unknown"], net_profit=None,
             opportunity=False),
        "",
    )
    check("missing RU", "не задана цена ремонта" in mt)

    bt = format_deal_card(
        _item(avito_price_badge="below", seller_type="company"),
        _report(condition="defect"),
        _val(shoplike=True, scam_score=55, cons=["много объявлений"]),
        "",
    )
    check("card has badges", "ниже рынка" in bt
          and "магазин/перекуп" in bt and "риск" in bt)


def test_haggle_and_kb() -> None:
    cheap = build_haggle_text(_item(price=20000), _val(baseline_price=80000))
    check("cheap haggle template", "ещё актуален" in cheap)
    normal = build_haggle_text(_item(price=70000), _val(baseline_price=80000))
    check("normal haggle template", "торг возможен" in normal)

    kb = build_action_kb(_item())
    flat = [b for row in kb.inline_keyboard for b in row]
    check("action+reaction buttons", len(flat) == 5)
    check("avito url", any("avito.ru" in (b.url or "") for b in flat))
    check("chat deep link", any("#chat" in (b.url or "") for b in flat))
    check("haggle callback",
          any((b.callback_data or "").startswith("haggle:") for b in flat))
    check("fb up callback",
          any((b.callback_data or "").startswith("fb:up:") for b in flat))
    check("fb down callback",
          any((b.callback_data or "").startswith("fb:down:") for b in flat))

    from techhunter.bot.app import _feedback_reason_kb
    rkb = _feedback_reason_kb(12345)
    rcbs = [b.callback_data for row in rkb.inline_keyboard for b in row]
    rtxt = [b.text for row in rkb.inline_keyboard for b in row]
    check("feedback has rebuild reason",
          "пересобранный перекупом" in rtxt)
    check("feedback reason callback compact",
          "fb:reason:12345:reseller_rebuild" in rcbs)


async def test_card_state_roundtrip() -> None:
    from techhunter.storage import (
        get_card_state,
        save_card_state,
        upsert_user,
    )

    tg = 777000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "tester")
    item = _item()
    val = _val()
    rep = _report()
    await save_card_state(
        tg, 42, item.model_dump(), val.model_dump(), rep.model_dump(), "q"
    )
    st = await get_card_state(tg, 42)
    check("state restored", st is not None and st["sub_query"] == "q")
    check("state has report", st["report"] is not None
          and st["report"]["condition"] == rep.condition)
    txt = haggle_text(st["item"], st["valuation"])
    check("haggle from restored state", "<code>" in txt)
    check("missing state -> None", await get_card_state(tg, 999999) is None)


async def test_subs_and_dedup() -> None:
    from techhunter.storage import (
        add_subscription,
        alert_already_sent,
        is_paused,
        list_subscriptions,
        mark_alert_sent,
        remove_subscription,
        set_paused,
        upsert_user,
    )

    tg = 888000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "subuser")
    sid = await add_subscription(tg, "iphone 13", max_price=50000,
                                 min_battery=85)
    subs = await list_subscriptions(tg)
    check("sub created", any(s.id == sid and s.min_battery == 85 for s in subs))
    await set_paused(tg, True)
    check("paused", await is_paused(tg) is True)
    await set_paused(tg, False)
    check("resumed", await is_paused(tg) is False)
    check("remove ok", await remove_subscription(tg, sid) is True)
    check("remove again false", await remove_subscription(tg, sid) is False)

    lid = f"al-{uuid.uuid4().hex}"
    check("not sent yet", await alert_already_sent(tg, lid) is False)
    await mark_alert_sent(tg, lid, price=1000, verdict="unknown")
    check("sent now", await alert_already_sent(tg, lid) is True)
    await mark_alert_sent(tg, lid, price=1000)  # idempotent, no error


async def test_empty_list_no_markup() -> None:
    # Empty subscription list must still render a valid (non-empty)
    # keyboard (Add + Back), never an empty inline keyboard (Telegram 400).
    from techhunter.bot.screens import screen_subs
    from techhunter.storage import upsert_user

    tg = 870000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "nolist")
    text, kb = await screen_subs(tg, 0)
    check("empty list text", "Подписок пока нет" in text)
    check("empty list kb non-empty",
          kb is not None and len(kb.inline_keyboard) >= 1
          and any(kb.inline_keyboard))


async def test_edit_not_modified_no_resend() -> None:
    from aiogram.exceptions import TelegramBadRequest
    from techhunter.bot.app import _edit

    class Msg:
        answers = 0

        async def edit_text(self, *args, **kwargs):
            raise TelegramBadRequest(
                method=object(),
                message="Bad Request: message is not modified",
            )

        async def answer(self, *args, **kwargs):
            self.answers += 1

    class Cb:
        message = Msg()

    await _edit(Cb(), ("same", None))
    check("not modified no resend", Cb.message.answers == 0)


async def test_expired_callback_answer_suppressed() -> None:
    from aiogram.exceptions import TelegramBadRequest
    from techhunter.bot.app import _answer_cb

    class Cb:
        async def answer(self, *args, **kwargs):
            raise TelegramBadRequest(
                method=object(),
                message=(
                    "Bad Request: query is too old and response timeout "
                    "expired or query ID is invalid"
                ),
            )

    await _answer_cb(Cb(), "ok")
    check("expired callback answer suppressed", True)


def test_imports() -> None:
    import inspect

    import main  # noqa: F401  (top-level entrypoint script)

    import techhunter.bot.app  # noqa: F401
    import techhunter.bot.notifier  # noqa: F401
    check("main does not create_all",
          "create_all" not in inspect.getsource(main._main))
    check("bot/main import", True)


def test_onboarding_text() -> None:
    from techhunter.bot.format import (
        onboarding_done_text,
        onboarding_started_text,
    )
    from techhunter.notifier import ConsoleNotifier, Notifier

    st = onboarding_started_text("iphone 13 pro", 300)
    check("started text", "Анализирую" in st and "5 мин" in st)

    done = onboarding_done_text(
        "iphone 13 pro",
        [("apple iPhone 13 Pro 256GB",
          {"working": 80000, "good": 78000, "broken": 40000})],
    )
    check("done has device", "iPhone 13 Pro 256GB" in done)
    check("done has tier", "битый ~40 000 ₽" in done)
    check("done has market", "рынок 80 000 ₽" in done)
    check("done empty msg", "мало данных" in onboarding_done_text("x", []))

    check("ConsoleNotifier conforms",
          isinstance(ConsoleNotifier(), Notifier))


async def test_hub_screen() -> None:
    from techhunter.bot.screens import screen_help, screen_hub

    text, kb = await screen_hub()
    check("hub text", "TechHunter" in text)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    for need in ("nav:discovery", "nav:subs:0", "nav:learned:0",
                 "nav:settings", "nav:status", "nav:help", "nav:dev"):
        check(f"hub has {need}", need in cbs)
    _, hkb = screen_help()
    check("help has back",
          any(b.callback_data == "nav:hub"
              for row in hkb.inline_keyboard for b in row))


async def test_settings_screen() -> None:
    from techhunter.bot.screens import screen_settings
    from techhunter.storage import (
        set_exclude_shop,
        set_min_score,
        toggle_exclude_condition,
        upsert_user,
    )

    tg = 940000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "scr")
    await set_min_score(tg, 40)
    await set_exclude_shop(tg, True)
    await toggle_exclude_condition(tg, "for_parts")
    text, kb = await screen_settings(tg)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("settings score shown", "40" in text)
    check("settings has score btn", "set:score:10" in cbs)
    check("settings has shop toggle", "set:shop" in cbs)
    check("settings has cond toggle", "set:cond:for_parts" in cbs)
    check("settings shows shop hidden", "скры" in text)
    check("settings has back",
          any(b.callback_data == "nav:hub"
              for row in kb.inline_keyboard for b in row))


async def test_status_screen() -> None:
    from techhunter.bot.screens import screen_dev, screen_dev_confirm, screen_status

    text, kb = await screen_status()
    check("status text", "Статус" in text and "База цен" in text)
    for raw in ("processed", "cache", "drop_reasons", "runtime", "outbox dead"):
        check(f"status hides {raw}", raw not in text)
    check("status refresh btn",
          any(b.callback_data == "nav:status"
              for row in kb.inline_keyboard for b in row))
    check("status has dev btn",
          any(b.callback_data == "nav:dev"
              for row in kb.inline_keyboard for b in row))

    dt, dkb = await screen_dev()
    check("dev text", "Dev" in dt and "processed" in dt
          and "cache" in dt and "drop_reasons" in dt and "Outbox" in dt)
    dcbs = [b.callback_data for row in dkb.inline_keyboard for b in row]
    for need in ("dev:confirm:retry_outbox", "dev:confirm:cleanup_old_rows",
                 "dev:confirm:browser_restart", "dev:raw", "nav:hub"):
        check(f"dev has {need}", need in dcbs)
    ct, ckb = screen_dev_confirm("retry_outbox")
    check("dev confirm text", "Retry outbox" in ct)
    check("dev confirm run",
          any(b.callback_data == "dev:run:retry_outbox"
              for row in ckb.inline_keyboard for b in row))


async def test_quality_and_prices_screens() -> None:
    from techhunter.bot.screens import screen_prices, screen_quality
    from techhunter.storage import (
        add_subscription,
        record_feedback,
        upsert_user,
    )

    tg = 950000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "q")
    await record_feedback(tg, "lid-a", "up")
    await record_feedback(tg, "lid-b", "down")
    await record_feedback(tg, "lid-a", "up")  # idempotent upsert
    qt, qkb = await screen_quality(tg)
    check("quality text", "Качество" in qt and "👍 1" in qt
          and "👎 1" in qt)
    check("quality has settings btn",
          any(b.callback_data == "nav:settings"
              for row in qkb.inline_keyboard for b in row))

    sid = await add_subscription(tg, "macbook air m2")
    pt, pkb = await screen_prices(tg, sid)
    check("prices unknown model", "не распознан" in pt)
    sid2 = await add_subscription(tg, "iphone 15 pro")
    pt2, _ = await screen_prices(tg, sid2)
    check("prices known model header", "iPhone 15 Pro" in pt2)
    check("prices missing sub",
          (await screen_prices(tg, 99999999))[0] == "Подписка не найдена.")


def main() -> None:
    test_parser()
    test_card_text()
    test_haggle_and_kb()
    test_onboarding_text()
    asyncio.run(test_hub_screen())
    asyncio.run(test_settings_screen())
    asyncio.run(test_status_screen())
    asyncio.run(test_quality_and_prices_screens())
    asyncio.run(test_card_state_roundtrip())
    asyncio.run(test_subs_and_dedup())
    asyncio.run(test_empty_list_no_markup())
    asyncio.run(test_edit_not_modified_no_resend())
    asyncio.run(test_expired_callback_answer_suppressed())
    test_imports()
    print("\nAll Stage 4 checks passed.")


if __name__ == "__main__":
    main()
