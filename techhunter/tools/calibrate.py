"""Operator CLI for Stage 5 manual verification and calibration.

Lets you (the operator) seed a subscription for the visible-monitor run and
enter REAL market baselines / repair costs. All numbers come from you or
from learned real data - nothing is fabricated.

Examples (from project root, venv python):
  python -m techhunter.tools.calibrate sub --tg 123 "iphone 13 pro" \
      --max-price 60000 --city москва --min-battery 85
  python -m techhunter.tools.calibrate baseline apple "iPhone 13 Pro" \
      --storage 256 80000
  python -m techhunter.tools.calibrate repair apple "iPhone 13" screen 8000
  python -m techhunter.tools.calibrate show --tg 123
"""
import argparse
import asyncio

from ..scraper.urls import resolve_city_slug
from ..storage import (
    add_subscription,
    list_subscriptions,
    upsert_user,
)
from ..valuation.devices import get_or_create_device, set_manual_baseline
from ..valuation.repair import set_repair_cost


async def _sub(a) -> None:
    await upsert_user(a.tg, "operator")
    sid = await add_subscription(
        a.tg,
        a.query,
        city_slug=resolve_city_slug(a.city) if a.city else "rossiya",
        min_price=a.min_price,
        max_price=a.max_price,
        min_battery=a.min_battery,
    )
    print(f"OK subscription #{sid} for tg={a.tg}: {a.query!r}")


async def _baseline(a) -> None:
    dev = await get_or_create_device(a.brand, a.model, a.storage)
    if dev is None:
        print("FAIL: unknown brand/model")
        return
    await set_manual_baseline(dev, a.price)
    print(
        f"OK warm-start baseline: {a.brand} {a.model} "
        f"{a.storage or '-'}GB = {a.price} RUB "
        f"(NOT locked - learning refines it from real data)"
    )


async def _repair(a) -> None:
    await set_repair_cost(a.brand, a.model_pattern, a.defect_type, a.cost)
    print(
        f"OK repair cost: {a.brand} '{a.model_pattern}' "
        f"{a.defect_type} = {a.cost} RUB"
    )


async def _show(a) -> None:
    subs = await list_subscriptions(a.tg)
    if not subs:
        print(f"tg={a.tg}: no subscriptions")
        return
    for s in subs:
        print(
            f"#{s.id} {s.query!r} | max={s.max_price} min={s.min_price} "
            f"batt>={s.min_battery} city={s.city_slug}"
        )


def main() -> None:
    p = argparse.ArgumentParser(prog="techhunter.tools.calibrate")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sub", help="add a user subscription")
    s.add_argument("query")
    s.add_argument("--tg", type=int, required=True)
    s.add_argument("--max-price", type=int, dest="max_price")
    s.add_argument("--min-price", type=int, dest="min_price")
    s.add_argument("--min-battery", type=int, dest="min_battery")
    s.add_argument("--city", default="")
    s.set_defaults(fn=_sub)

    b = sub.add_parser(
        "baseline", help="optional warm-start price (learning refines it)"
    )
    b.add_argument("brand")
    b.add_argument("model")
    b.add_argument("price", type=int)
    b.add_argument("--storage", type=int, default=None)
    b.set_defaults(fn=_baseline)

    r = sub.add_parser("repair", help="set a REAL repair cost")
    r.add_argument("brand")
    r.add_argument("model_pattern")
    r.add_argument("defect_type", choices=["screen", "back_glass", "battery"])
    r.add_argument("cost", type=int)
    r.set_defaults(fn=_repair)

    sh = sub.add_parser("show", help="list subscriptions for a user")
    sh.add_argument("--tg", type=int, required=True)
    sh.set_defaults(fn=_show)

    args = p.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
