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
    await _seed_baseline("apple", "iPhone 13 Pro", 256, 80000)
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
    check("WD condition ideal", rep.condition == "ideal")
    check("WD model", rep.model == "iPhone 13 Pro" and rep.storage_gb == 256)
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
    check("BF net = 45000-22000-7000", val.net_profit == 16000)
    check("BF opportunity broken_flip",
          val.opportunity is True and val.opportunity_type == "broken_flip")
    check("BF repair surfaced", val.repair_cost == 7000)
    check("BF cheap broken NOT scam-flagged",
          not any("от рынка" in c for c in val.cons))


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
        "iPhone 11 на запчасти",
        "Привязан к iCloud, не активирован, donor, не включается без активации",
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
    check("SE model S23 Ultra", rep.model == "Galaxy S23 Ultra")


async def case_repair_unknown() -> None:
    await _seed_baseline("apple", "iPhone 13", 128, 55000)
    it = _item(
        "iPhone 13 128 ГБ",
        "Разбита задняя крышка, экран идеальный, всё работает",
        30000,
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
        "xopоший аналoг бeз oшибки. Cocтoяние aккумулятopa 100% оpигинaл, "
        "334 циклa. Otвязан oт аккаунтов. Ecть выкуп вашeй тexники "
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
          and "condition_discount_unknown" in val.missing)
    check("HG not original verdict", val.scam_verdict != "original")
    check("HG scam flagged it",
          any(("обфускац" in c or "перекуп" in c or "не родны" in c)
              for c in val.cons))


async def _main() -> None:
    await case_working_deal()
    await case_cosmetic_only_no_overvalue()
    await case_homoglyph_screen_replaced()
    await case_broken_flip()
    await case_replica_fake()
    await case_icloud_for_parts()
    await case_low_battery_defect()
    await case_spec_extraction()
    await case_repair_unknown()
    print("\nAll calibration checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
