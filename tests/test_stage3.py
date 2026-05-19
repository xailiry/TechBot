"""Stage 3 verification (no network, DB-backed, idempotent per run).

Run: ../venv/Scripts/python.exe -m tests.test_stage3  (from project root)
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys
import uuid

from techhunter.ai.evaluate import EvaluationReport
from techhunter.scraper.models import ParsedListing
from techhunter.valuation.clustering import cluster_high_median, robust_median
from techhunter.valuation.devices import (
    get_baseline,
    get_or_create_device,
    log_observation,
    set_manual_baseline,
)
from techhunter.valuation.engine import value_listing
from techhunter.valuation.repair import (
    estimate_repairs,
    get_repair_cost,
    set_repair_cost,
)
from techhunter.valuation.scam import (
    looks_shoplike,
    normalize_homoglyphs,
    score_listing,
)


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def test_age_seconds() -> None:
    from datetime import datetime, timedelta, timezone

    from techhunter.valuation.devices import _age_seconds

    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    a = _age_seconds(naive_now)
    check("naive utc age ~0 not negative", 0.0 <= a < 30)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    check("naive 2h ago ~7200", 7000 < _age_seconds(old) < 7400)
    aware = datetime.now(timezone.utc) - timedelta(hours=1)
    check("aware 1h ago ~3600", 3500 < _age_seconds(aware) < 3700)


def test_clustering() -> None:
    bimodal = [5000, 5500, 6000, 48000, 50000, 52000, 51000, 49500]
    hi = cluster_high_median(bimodal)
    check("high cluster wins scam lows", hi is not None and hi >= 40000)
    check("tiny sample -> median", cluster_high_median([10, 20, 30]) == 20)
    check("empty -> None", cluster_high_median([]) is None)

    # robust_median must center on the USED bulk, NOT bias up to the
    # new/shop high end (the bug behind "60k when market is ~44k").
    used = [38000, 40000, 41000, 42000, 43000, 45000, 46000,
            55000, 57000, 59000]
    rm = robust_median(used)
    check("robust_median centered on bulk", rm is not None and rm <= 48000)
    check("robust_median drops scam lows",
          robust_median([1000, 1100, 41000, 42000, 43000, 44000, 45000])
          >= 40000)
    check("robust_median empty -> None", robust_median([]) is None)


def test_shoplike() -> None:
    check("shop text", looks_shoplike("iPhone 15 Pro", "магазин, гарантия 1 год"))
    check("refurb", looks_shoplike("iPhone 15 Pro", "восстановленный, не ref"))
    check("rassrochka/trade-in",
          looks_shoplike("iPhone 15 Pro", "рассрочка, trade-in, выкуп"))
    check("seller_type shop",
          looks_shoplike("iPhone 15 Pro", "", seller_type="shop"))
    check("many listings",
          looks_shoplike("iPhone 15 Pro", "", seller_listings=50))
    check("plain private NOT shoplike",
          not looks_shoplike("iPhone 15 Pro 128", "продаю свой, акб 92%",
                             seller_type="private", seller_listings=2))


def test_scam() -> None:
    fake = score_listing("iPhone 13", "копия 1:1 люкс", 20000)
    check("replica -> fake", fake.verdict == "fake" and fake.score < 35)

    cheap_clean = score_listing(
        "iPhone 13 128", "идеал, всё работает", 6000,
        condition="good", baseline_price=60000,
    )
    check("clean working too cheap -> con",
          any("от рынка для рабочего" in c for c in cheap_clean.cons))

    cheap_broken = score_listing(
        "iPhone 13 разбит экран", "трещина, продаю дёшево", 6000,
        condition="broken", baseline_price=60000, defects=["screen_cracked"],
    )
    check("broken cheap NOT price-flagged",
          not any("от рынка" in c for c in cheap_broken.cons))
    check("broken cheap not auto-suspicious",
          cheap_broken.verdict != "fake")

    check("homoglyph normalize",
          "айфон" in normalize_homoglyphs("aйфoн".replace("o", "о")) or
          normalize_homoglyphs("Bаре") == "Варе")

    # Obfuscated reseller spam must not pass as "original".
    spam = score_listing(
        "iPhone 15 Pro 256", "оpигинaл, идeaл. Bыкуп вашeй тexники Aррlе",
        38000, condition="good", baseline_price=50000,
    )
    check("obfuscated spam not original", spam.verdict != "original")
    check("spam flagged",
          any(("обфускац" in c or "подмена" in c or "перекуп" in c)
              for c in spam.cons))

    # Claims original but screen was replaced -> contradiction.
    contra = score_listing(
        "iPhone 15 Pro", "оригинал, идеал, всё родное", 40000,
        condition="defect", baseline_price=50000,
        defects=["screen_replaced"],
    )
    check("orig-claim vs replaced screen",
          any("не родны" in c for c in contra.cons)
          and contra.verdict != "original")


async def test_devices_baseline() -> None:
    tag = uuid.uuid4().hex[:8]
    model = f"iPhone TEST {tag}"
    d1 = await get_or_create_device("apple", model, 128)
    d1b = await get_or_create_device("apple", model, 128)
    d2 = await get_or_create_device("apple", model, 256)
    check("device id stable", d1 == d1b)
    check("storage differs -> new device", d1 != d2)
    check("unknown brand -> None",
          await get_or_create_device("unknown", "", None) is None)

    # Too few observations -> no fabricated baseline.
    few = await get_or_create_device("apple", f"iPhone FEW {tag}", 128)
    for p in (60000, 61000, 59000):
        await log_observation(few, "good", p)
    check("under min sample -> None", await get_baseline(few) is None)

    # Enough real obs incl. scam lows -> learns the genuine cluster.
    for p in (60000, 61000, 59000, 62000, 60500, 59500, 61500, 5000, 5500):
        await log_observation(d1, "good", p)
    base = await get_baseline(d1)
    check("baseline learned from real data",
          base is not None and 55000 <= base <= 65000)

    # Manual real value is locked from auto-learning.
    man = await get_or_create_device("apple", f"iPhone MAN {tag}", 128)
    await set_manual_baseline(man, 70000)
    for p in (1000, 1500, 2000, 2500, 3000, 3500):
        await log_observation(man, "good", p)
    check("manual baseline not overwritten",
          await get_baseline(man) == 70000)


async def test_repair() -> None:
    tag = uuid.uuid4().hex[:6]
    brand = f"apple{tag}"
    await set_repair_cost(brand, "iPhone 13", "screen", 8000)
    check("prefix match",
          await get_repair_cost(brand, "iPhone 13 Pro", "screen") == 8000)
    check("missing cost -> None",
          await get_repair_cost(brand, "iPhone 13", "battery") is None)

    total, bd, miss, blocked = await estimate_repairs(
        brand, "iPhone 13 Pro", ["screen_cracked"]
    )
    check("repair total", total == 8000 and bd == {"screen": 8000})
    check("not blocked", blocked is False)

    total2, _, miss2, _ = await estimate_repairs(
        brand, "iPhone 13", ["back_glass_cracked"]
    )
    check("unknown repair -> total None", total2 is None and "back_glass" in miss2)

    _, _, _, blk = await estimate_repairs(
        brand, "iPhone 13", ["screen_cracked", "icloud_locked"]
    )
    check("icloud blocks flip", blk is True)


def _item(price, title="iPhone 13 Pro 128", desc="", **kw):
    return ParsedListing(id=f"v-{uuid.uuid4().hex}", title=title,
                         price=price, url="/x/1", description=desc, **kw)


def _report(model, condition, defects=None, brand="apple", storage=128):
    return EvaluationReport(
        listing_id="r", brand=brand, model=model, storage_gb=storage,
        condition=condition, defects=defects or [],
    )


async def test_engine() -> None:
    tag = uuid.uuid4().hex[:8]

    # Working deal vs a manual real baseline.
    dev = await get_or_create_device("apple", f"iPhone WD {tag}", 128)
    await set_manual_baseline(dev, 60000)
    rep = _report(f"iPhone WD {tag}", "good")
    val = await value_listing(_item(45000), rep)
    check("working net", val.net_profit == 15000)
    check("working opportunity", val.opportunity is True)
    check("working type", val.opportunity_type == "working")

    # Broken flip with a known repair cost.
    brand = f"apple{tag}"
    bmodel = f"iPhone BF {tag}"
    devb = await get_or_create_device(brand, bmodel, 128)
    await set_manual_baseline(devb, 60000)
    await set_repair_cost(brand, bmodel, "screen", 8000)
    rb = _report(bmodel, "broken", ["screen_cracked"], brand=brand)
    vb = await value_listing(
        _item(30000, title=f"{bmodel} разбит экран"), rb
    )
    check("broken net = base-price-repair", vb.net_profit == 22000)
    check("broken opportunity", vb.opportunity is True)
    check("broken type", vb.opportunity_type == "broken_flip")
    check("repair cost surfaced", vb.repair_cost == 8000)

    # Broken but repair cost unknown -> no fabricated profit.
    devb2 = await get_or_create_device(brand, f"iPhone NB {tag}", 128)
    await set_manual_baseline(devb2, 60000)
    rb2 = _report(f"iPhone NB {tag}", "broken", ["back_glass_cracked"],
                  brand=brand)
    vb2 = await value_listing(_item(30000), rb2)
    check("repair unknown -> no net", vb2.net_profit is None)
    check("repair unknown flagged",
          "repair_cost_unknown" in vb2.missing and vb2.opportunity is False)

    # for_parts -> not an opportunity.
    devp = await get_or_create_device("apple", f"iPhone FP {tag}", 128)
    await set_manual_baseline(devp, 60000)
    vp = await value_listing(
        _item(8000), _report(f"iPhone FP {tag}", "for_parts", ["no_power"])
    )
    check("for_parts no opportunity",
          vp.opportunity is False and "for_parts" in vp.missing)

    # No baseline at all -> flagged, not guessed.
    devn = await get_or_create_device("apple", f"iPhone NOBASE {tag}", 128)
    vn = await value_listing(_item(40000), _report(f"iPhone NOBASE {tag}", "good"))
    check("no baseline flagged",
          vn.baseline_price is None and "no_baseline" in vn.missing
          and vn.opportunity is False)

    # Fake suppresses opportunity even at a great price.
    devf = await get_or_create_device("apple", f"iPhone FAKE {tag}", 128)
    await set_manual_baseline(devf, 60000)
    vf = await value_listing(
        _item(20000, title=f"iPhone FAKE {tag}", desc="копия 1:1 люкс"),
        _report(f"iPhone FAKE {tag}", "good"),
    )
    check("fake suppresses opportunity",
          vf.scam_verdict == "fake" and vf.opportunity is False)


def main() -> None:
    test_age_seconds()
    test_clustering()
    test_shoplike()
    test_scam()
    asyncio.run(test_devices_baseline())
    asyncio.run(test_repair())
    asyncio.run(test_engine())
    print("\nAll Stage 3 checks passed.")


if __name__ == "__main__":
    main()
