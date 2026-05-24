"""Stage 5.1 calibration: realistic Russian Avito-style mock listings fed
end-to-end through evaluate + value. Asserts spec extraction, model
normalization, condition grading, margin (working + broken flip) and the
scam guard on representative cases.

No network (run_clip=False, do_dedup=False). Deterministic via manually
seeded real baselines / repair costs.

Run: ../venv/Scripts/python.exe -m tests.test_calibration
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys

from techhunter.ai.evaluate import evaluate_listing
from techhunter.scraper.models import ParsedListing
from techhunter.valuation.devices import get_or_create_device, set_manual_baseline
from techhunter.valuation.engine import value_listing
from techhunter.valuation.repair import set_repair_cost


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def _item(title, desc, price, params=None, **kw):
    return ParsedListing(
        id=f"cal-{title[:8]}-{price}", title=title, price=price,
        url="/x/1", description=desc, params=params or {}, **kw
    )


async def _seed_baseline(brand, model, storage, price):
    dev = await get_or_create_device(brand, model, storage)
    await set_manual_baseline(dev, price)
    return dev


async def case_working_deal() -> None:
    await _seed_baseline("apple", "iPhone 13 Pro [RST]", 256, 80000)
    it = _item(
        "iPhone 13 Pro 256GB",
        "Идеал, акб 91%, ростест, чек и коробка, Face ID работает, "
        "тру тон родной, без царапин",
        62000, seller_name="Иван", seller_listings=3,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("WD battery 91", rep.battery_health == 91)
    check("WD no defects", rep.defects == [])
    check("WD condition good", rep.condition == "good")
    check("WD model", rep.model == "iPhone 13 Pro [RST]" and rep.storage_gb == 256)
    check("WD net 18000", val.net_profit == 18000)
    check("WD opportunity working",
      val.opportunity is True and val.opportunity_type == "working")
    check("WD not fake", val.scam_verdict != "fake")


async def case_broken_flip() -> None:
    await _seed_baseline("apple", "iPhone 12", 128, 45000)
    await set_repair_cost("apple", "iPhone 12", "screen", 7000)
    it = _item(
        "iPhone 12 128 ГБ разбит экран",
        "Разбит экран, в остальном идеал, всё работает, акб 89%, "
        "продаю дёшево срочно",
        22000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("BF screen_cracked", "screen_cracked" in rep.defects)
    check("BF condition broken", rep.condition == "broken")
    if val.net_profit != 16000:
        print(f"DEBUG: net_profit={val.net_profit}, repair={val.repair_cost}, market={val.baseline_price}")
    check("BF net = 45000-22000-7000", val.net_profit == 16000)
    check("BF opportunity broken_flip",
          val.opportunity is True and val.opportunity_type == "broken_flip")
    check("BF repair surfaced", val.repair_cost == 7000)
    check("BF cheap broken NOT scam-flagged",
          not any("от рынка" in c for c in val.cons))


async def case_display_stripe_not_working_deal() -> None:
    """Real Avito bug: display stripes/dead pixels + "рыночная цена" badge
    must not be delivered as a clean ideal/working arbitrage."""
    await _seed_baseline("apple", "iPhone 12 Pro", 128, 18000)
    it = _item(
        "iPhone 12 Pro, 128 ГБ",
        "Аккумулятор заменили на новый, корпус и экран в идеале, "
        "но есть полоса, от этого и такая цена.",
        12000,
        params={
            "состояние": "Удовлетворительное",
            "экран": "Полосы и битые пиксели",
            "корпус": "Без дефектов",
            "состояние аккумулятора": "100 %",
            "встроенная память": "128 ГБ",
        },
        avito_market_badge=True,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("Stripe display defect", "screen_display_defect" in rep.defects)
    check("Stripe condition broken", rep.condition == "broken")
    check("Stripe repair is screen", val.repair_breakdown.get("screen") is not None)
    check("Stripe not opportunity", val.opportunity is False)


async def case_avito_market_badge_conflict() -> None:
    """If our text parser sees a clean working lot, but Avito says the very
    low price is still market, suppress the working-deal false positive."""
    await _seed_baseline("apple", "iPhone 12 Pro", 128, 18000)
    it = _item(
        "iPhone 12 Pro, 128 ГБ",
        "Идеал, акб 100%, всё работает.",
        12000,
        params={"встроенная память": "128 ГБ"},
        avito_market_badge=True,
    )
    it.description = "Ideal, battery health 92%, everything works."
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("Badge conflict starts working", rep.condition in {"ideal", "good"})
    check("Badge conflict suppresses opportunity", val.opportunity is False)
    check("Badge conflict missing reason",
          "avito_market_badge_conflict" in val.missing)


async def case_replica_fake() -> None:
    it = _item(
        "iPhone 14 Pro Max 256",
        "Копия 1:1, люкс качество, не отличить от оригинала, новый",
        20000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("RF replica defect", "replica" in rep.defects)
    check("RF scam fake", val.scam_verdict == "fake")
    check("RF no opportunity", val.opportunity is False)


async def case_icloud_for_parts() -> None:
    await _seed_baseline("apple", "iPhone 11", None, 30000)
    it = _item(
        "iPhone 11 заблокирован",
        "Привязан к iCloud, не активирован, не включается без активации",
        8000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("IC icloud_locked", "icloud_locked" in rep.defects)
    check("IC condition for_parts", rep.condition == "for_parts")
    check("IC for_parts flagged",
          "for_parts" in val.missing and val.opportunity is False)


async def case_low_battery_defect() -> None:
    it = _item(
        "Samsung Galaxy S22",
        "Состояние хорошее, акб 71%, полный комплект",
        28000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    check("LB battery 71", rep.battery_health == 71)
    check("LB battery -> defect", "battery_replaced" in rep.defects)
    check("LB samsung S22",
          rep.brand == "samsung" and rep.model == "Galaxy S22")
    check("LB condition defect", rep.condition == "defect")


async def case_spec_extraction() -> None:
    it = _item(
        "Самсунг Галакси S23 Ультра 12/256 ГБ",
        "АКБ 100%, на гарантии, ростест, идеальное состояние не учитываем",
        70000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    check("SE ram 12", rep.ram_gb == 12)
    check("SE storage 256", rep.storage_gb == 256)
    check("SE battery 100", rep.battery_health == 100)
    check("SE rostest", rep.is_rostest is True)
    check("SE model S23 Ultra", rep.model == "Galaxy S23 Ultra [RST]")


async def case_repair_unknown() -> None:
    await _seed_baseline("apple", "iPhone SE", 128, 25000)
    it = _item(
        "iPhone SE 128 ГБ",
        "Разбита задняя крышка, экран идеальный, всё работает",
        15000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("RU back_glass_cracked", "back_glass_cracked" in rep.defects)
    check("RU condition broken", rep.condition == "broken")
    check("RU net None (no fabricated profit)", val.net_profit is None)
    check("RU repair_cost_unknown flagged",
          "repair_cost_unknown" in val.missing and val.opportunity is False)


async def case_cosmetic_only_no_overvalue() -> None:
    # Regression for the over-valuation bug: a DEFECT with no priced
    # physical repair path must NOT be valued at the full working baseline.
    await _seed_baseline("apple", "iPhone 13 Pro", 256, 80000)
    it = _item(
        "iPhone 13 Pro 256 ГБ",
        "Небольшие потертости на корпусе, экран идеальный, всё работает",
        60000,
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("CO cosmetic_wear", "cosmetic_wear" in rep.defects)
    check("CO condition defect", rep.condition == "defect")
    check("CO no fabricated net", val.net_profit is None)
    check("CO no opportunity", val.opportunity is False)
    check("CO discount flagged",
          "condition_discount_unknown" in val.missing)


async def case_homoglyph_screen_replaced() -> None:
    # Real long-test bug: spam listing hides "замена дисплея" with Latin
    # lookalikes. Must be detected -> defect, no deal, not "original".
    await _seed_baseline("apple", "iPhone 15 Pro", 256, 50000)
    it = _item(
        "iPhone 15 Pro, 256 ГБ, 2 SIM",
        "Прoдам iPhone 15 Pro 256gb. Прoизводилаcь зaмена диcплeя нa "
        "xopоший аналoг бeз oшибки. Cocтoяние aккумулятopa 90% оpигинaл. "
        "Otвязан oт аккаунтов. Ecть выкуп вашeй тexники "
        "Aррlе Sаmsung Gооglе.",
        38990, seller_name="Пользователь",
    )
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    val = await value_listing(it, rep, log_obs=False)
    check("HG model", rep.brand == "apple"
          and rep.model == "iPhone 15 Pro" and rep.storage_gb == 256)
    check("HG screen_replaced detected",
          "screen_replaced" in rep.defects)
    check("HG condition defect", rep.condition == "defect")
    check("HG no opportunity",
          val.opportunity is False
          and ("condition_discount_unknown" in val.missing or "repair_cost_unknown" in val.missing))
    check("HG not original verdict", val.scam_verdict != "original")
    check("HG scam flagged it",
          any(("обфускац" in c or "перекуп" in c or "не родны" in c)
              for c in val.cons))


async def case_carrier_lock() -> None:
    # Seed baseline
    await _seed_baseline("apple", "iPhone 13 Pro", 256, 80000)

    # 1. Check positive unlocked claims (neverlock, без rsim) don't trigger carrier_locked
    it_clean = _item(
        "iPhone 13 Pro 256GB Neverlock",
        "Отличное состояние, без r-sim, чистый айфон, без мдм",
        62000,
    )
    rep_clean = await evaluate_listing(it_clean, run_clip=False, do_dedup=False)
    check("CL neverlock clean", "carrier_locked" not in rep_clean.defects)

    # 2. Check locked claims do trigger carrier_locked
    it_locked = _item(
        "iPhone 13 Pro 256GB R-SIM",
        "Телефон сша, работает через чип рсим, симлок, mdm блокировка",
        35000,
    )
    rep_locked = await evaluate_listing(it_locked, run_clip=False, do_dedup=False)
    val_locked = await value_listing(it_locked, rep_locked, log_obs=False)
    check("CL rsim detected", "carrier_locked" in rep_locked.defects)
    check("CL rsim condition for_parts", rep_locked.condition == "for_parts")
    check("CL rsim opportunity blocked", val_locked.opportunity is False)
    check("CL rsim scam penalized", val_locked.scam_score < 40 and any("оператор" in c for c in val_locked.cons))


async def case_regional_variants() -> None:
    it_rst = _item("iPhone 13 Pro ростест", "Идеальное состояние, рст версия", 60000)
    rep_rst = await evaluate_listing(it_rst, run_clip=False, do_dedup=False)
    check("RST suffix model", rep_rst.model == "iPhone 13 Pro [RST]")

    it_esim = _item("iPhone 14 Pro eSIM", "американец, esim only, без физ сим", 70000)
    rep_esim = await evaluate_listing(it_esim, run_clip=False, do_dedup=False)
    check("eSIM suffix model", rep_esim.model == "iPhone 14 Pro [eSIM]")

    it_ref = _item("iPhone 13 Pro реф", "восстановленный телефон cpo", 50000)
    rep_ref = await evaluate_listing(it_ref, run_clip=False, do_dedup=False)
    check("Ref suffix model", rep_ref.model == "iPhone 13 Pro [Ref]")


async def case_battery_cycles_fraud() -> None:
    it = _item("iPhone 13 128GB", "АКБ 96%, 350 циклов перезарядки", 40000)
    rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
    check("Cycles parsed", rep.battery_cycles == 350)
    check("Cycles fraud detected defect", "battery_replaced" in rep.defects)


async def case_dynamic_battery_threshold() -> None:
    await _seed_baseline("apple", "iPhone 15 Pro", 256, 90000)
    it_ip15 = _item("iPhone 15 Pro 256GB", "Идеал, акб 84%, без нюансов", 75000)
    rep_ip15 = await evaluate_listing(it_ip15, run_clip=False, do_dedup=False)
    check("iPhone 15 Pro battery defect", "battery_replaced" in rep_ip15.defects)
    check("iPhone 15 Pro battery condition defect", rep_ip15.condition == "defect")

    await _seed_baseline("apple", "iPhone 11", 128, 25000)
    it_ip11_78 = _item("iPhone 11 128GB", "Хорошее сост, акб 78%", 15000)
    rep_ip11_78 = await evaluate_listing(it_ip11_78, run_clip=False, do_dedup=False)
    check("iPhone 11 battery defect <= 79", "battery_replaced" in rep_ip11_78.defects)

    it_ip11_80 = _item("iPhone 11 128GB", "Хорошее сост, акб 80%", 16000)
    rep_ip11_80 = await evaluate_listing(it_ip11_80, run_clip=False, do_dedup=False)
    check("iPhone 11 battery ok >= 80", "battery_replaced" not in rep_ip11_80.defects)


async def case_old_iphone_100_battery_red_flag() -> None:
    it_old = _item(
        "iPhone 14 Pro 128GB",
        "Used phone, battery health 100%, ideal condition.",
        52000,
    )
    rep_old = await evaluate_listing(it_old, run_clip=False, do_dedup=False)
    check("old iPhone 100 battery flagged",
          "battery_replaced" in rep_old.defects)
    check("old iPhone 100 battery condition defect",
          rep_old.condition == "defect")

    it_new = _item(
        "iPhone 14 Pro 128GB new",
        "New sealed phone, battery health 100%.",
        80000,
    )
    rep_new = await evaluate_listing(it_new, run_clip=False, do_dedup=False)
    check("new old-generation iPhone 100 allowed",
          "battery_replaced" not in rep_new.defects)

    it_recent = _item(
        "iPhone 16e 128GB",
        "Used phone, battery health 100%, ideal condition.",
        36000,
    )
    rep_recent = await evaluate_listing(it_recent, run_clip=False, do_dedup=False)
    check("recent iPhone 100 allowed",
          "battery_replaced" not in rep_recent.defects)


async def case_repairable_gems() -> None:
    await _seed_baseline("apple", "iPhone 13", 128, 55000)
    await set_repair_cost("apple", "iPhone 13", "faceid", 5000)
    it_faceid = _item("iPhone 13 128GB", "Не работает Face ID, остальное отлично", 35000)
    rep_faceid = await evaluate_listing(it_faceid, run_clip=False, do_dedup=False)
    val_faceid = await value_listing(it_faceid, rep_faceid, log_obs=False)
    check("FaceID defect code", "faceid_broken" in rep_faceid.defects)
    check("FaceID condition defect", rep_faceid.condition == "defect")
    check("FaceID repair flips", val_faceid.opportunity is True and val_faceid.opportunity_type == "broken_flip")
    check("FaceID net matches repair cost", val_faceid.net_profit == 55000 - 35000 - 5000)

    dev_id = await get_or_create_device("apple", "iPhone 13", 128)
    from techhunter.db import get_session
    from techhunter.db.models import PriceObservation
    from datetime import datetime, timezone
    from techhunter.valuation.devices import _upsert_baseline
    async with get_session() as s:
        from sqlalchemy import delete
        await s.execute(delete(PriceObservation).where(PriceObservation.device_id == dev_id))
        for p in (41000, 42000, 43000):
            s.add(PriceObservation(device_id=dev_id, condition="defect", price=p, listing_id=f"obs-{p}", observed_at=datetime.now(timezone.utc)))
        await s.commit()
    await _upsert_baseline(dev_id, "defect", 42000, 3)

    it_as_is = _item("iPhone 13 128GB", "Небольшие потертости на корпусе, экран идеальный, всё работает", 30000)
    rep_as_is = await evaluate_listing(it_as_is, run_clip=False, do_dedup=False)
    val_as_is = await value_listing(it_as_is, rep_as_is, log_obs=False)
    check("As-is condition baseline exists", val_as_is.condition_baselines.get("defect") == 42000)
    check("As-is net matches defect median", val_as_is.net_profit == 42000 - 30000)
    check("As-is opportunity working", val_as_is.opportunity is True and val_as_is.opportunity_type == "working")

    await set_repair_cost("apple", "iPhone 13", "no_power", 8000)
    it_nopower = _item("iPhone 13 128GB", "Не включается телефон, донор", 20000)
    rep_nopower = await evaluate_listing(it_nopower, run_clip=False, do_dedup=False)
    val_nopower = await value_listing(it_nopower, rep_nopower, log_obs=False)
    check("no_power in defects", "no_power" in rep_nopower.defects)
    check("no_power condition broken", rep_nopower.condition == "broken")
    check("no_power flips to working", val_nopower.opportunity is True and val_nopower.opportunity_type == "broken_flip")
    check("no_power net profit matches", val_nopower.net_profit == 55000 - 20000 - 8000)


async def _main() -> None:
    await case_working_deal()
    await case_cosmetic_only_no_overvalue()
    await case_homoglyph_screen_replaced()
    await case_broken_flip()
    await case_display_stripe_not_working_deal()
    await case_avito_market_badge_conflict()
    await case_replica_fake()
    await case_icloud_for_parts()
    await case_low_battery_defect()
    await case_spec_extraction()
    await case_repair_unknown()
    await case_carrier_lock()
    await case_regional_variants()
    await case_battery_cycles_fraud()
    await case_dynamic_battery_threshold()
    await case_old_iphone_100_battery_red_flag()
    await case_repairable_gems()
    print("\nAll calibration checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
