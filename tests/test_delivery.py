"""M1.1 verification: per-user delivery filters (pure + storage)."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys
import uuid
from types import SimpleNamespace

from techhunter.delivery import DeliveryPrefs, passes_filters


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def _io(seller_type=None):
    return SimpleNamespace(seller_type=seller_type)


def test_pure() -> None:
    item = _io()
    rep = SimpleNamespace(condition="good")
    val = SimpleNamespace(scam_score=70)

    ok, why = passes_filters(DeliveryPrefs(), item, rep, val)
    check("default passes", ok and why is None)

    ok, why = passes_filters(
        DeliveryPrefs(paused=True), item, rep, val)
    check("paused drops", not ok and why == "paused")

    ok, why = passes_filters(
        DeliveryPrefs(exclude_shop=True), _io("shop"), rep, val)
    check("shop dropped", not ok and why == "shop")
    ok, _ = passes_filters(
        DeliveryPrefs(exclude_shop=True), _io("private"), rep, val)
    check("private kept", ok)

    ok, why = passes_filters(
        DeliveryPrefs(min_score=80), item, rep, val)
    check("low score dropped", not ok and why == "low_score")
    ok, _ = passes_filters(DeliveryPrefs(min_score=60), item, rep, val)
    check("score ok kept", ok)

    ok, why = passes_filters(
        DeliveryPrefs(exclude_conditions={"for_parts", "broken"}),
        item, SimpleNamespace(condition="broken"), val)
    check("excluded condition dropped",
          not ok and why == "excluded_condition")
    ok, _ = passes_filters(
        DeliveryPrefs(exclude_conditions={"for_parts"}), item, rep, val)
    check("non-excluded condition kept", ok)


async def test_storage() -> None:
    from techhunter.storage import (
        get_delivery_prefs,
        set_exclude_shop,
        set_min_score,
        toggle_exclude_condition,
        upsert_user,
    )

    tg = 930000 + (uuid.uuid4().int % 1000)
    await upsert_user(tg, "prefs")

    p = await get_delivery_prefs(tg)
    check("defaults", p.min_score == 0 and not p.exclude_shop
          and p.exclude_conditions == set())

    await set_min_score(tg, 75)
    await set_exclude_shop(tg, True)
    now1 = await toggle_exclude_condition(tg, "for_parts")
    now2 = await toggle_exclude_condition(tg, "broken")
    check("toggle returns new state", now1 is True and now2 is True)

    p = await get_delivery_prefs(tg)
    check("min_score saved", p.min_score == 75)
    check("exclude_shop saved", p.exclude_shop is True)
    check("conditions saved",
          p.exclude_conditions == {"for_parts", "broken"})

    off = await toggle_exclude_condition(tg, "for_parts")
    check("toggle off", off is False)
    p = await get_delivery_prefs(tg)
    check("condition removed", p.exclude_conditions == {"broken"})

    await set_min_score(tg, 999)
    p = await get_delivery_prefs(tg)
    check("min_score clamped", p.min_score == 100)

    check("unknown user -> defaults",
          (await get_delivery_prefs(123456789)).min_score == 0)


def main() -> None:
    test_pure()
    asyncio.run(test_storage())
    print("\nAll delivery checks passed.")


if __name__ == "__main__":
    main()
