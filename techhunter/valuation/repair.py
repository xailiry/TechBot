"""Repair-cost lookup for the broken-lot flip math.

Costs are operator-supplied real values (RepairCost table). If a needed
cost is missing we report it as unknown and refuse to claim a profit, rather
than invent a number.
"""
import logging

from sqlalchemy import select

from ..db import get_session
from ..db.models import RepairCost

log = logging.getLogger(__name__)

# Defect code -> physical repair type priced to flip the lot to "working".
REPAIRABLE: dict[str, str] = {
    "screen_cracked": "screen",
    "back_glass_cracked": "back_glass",
    "battery_replaced": "battery",
    "faceid_broken": "faceid",
    "no_power": "no_power",
}
# Defects that block a cheap flip-to-working regardless of repair prices.
FLIP_BLOCKING = {"icloud_locked", "carrier_locked"}


async def get_repair_cost(
    brand: str, model: str, defect_type: str
) -> int | None:
    """Longest model_pattern prefix match (e.g. 'iPhone 13' matches
    'iPhone 13 Pro'). Returns None if the operator has not entered it."""
    if not brand or not model:
        return None
    async with get_session() as s:
        rows = (
            await s.execute(
                select(RepairCost).where(
                    RepairCost.brand == brand,
                    RepairCost.defect_type == defect_type,
                )
            )
        ).scalars().all()
    best: RepairCost | None = None
    for r in rows:
        if model.lower().startswith(r.model_pattern.lower()):
            if best is None or len(r.model_pattern) > len(best.model_pattern):
                best = r
    return best.cost_rub if best else None


async def estimate_repairs(
    brand: str, model: str, defects: list[str]
) -> tuple[int | None, dict[str, int], list[str], bool]:
    """Return (total, breakdown, missing, blocked).

    total is None if any required repair cost is unknown (so the engine will
    not claim a profit). blocked is True if a defect makes a cheap flip
    unrealistic (e.g. Face ID)."""
    blocked = any(d in FLIP_BLOCKING for d in defects)
    breakdown: dict[str, int] = {}
    missing: list[str] = []
    for d in defects:
        rtype = REPAIRABLE.get(d)
        if not rtype or rtype in breakdown or rtype in missing:
            continue
        cost = await get_repair_cost(brand, model, rtype)
        if cost is None:
            missing.append(rtype)
        else:
            breakdown[rtype] = cost
    total = None if missing else sum(breakdown.values())
    return total, breakdown, missing, blocked


async def set_repair_cost(
    brand: str, model_pattern: str, defect_type: str, cost_rub: int
) -> None:
    """Operator enters a real repair price."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(RepairCost).where(
                    RepairCost.brand == brand,
                    RepairCost.model_pattern == model_pattern,
                    RepairCost.defect_type == defect_type,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(
                RepairCost(
                    brand=brand,
                    model_pattern=model_pattern,
                    defect_type=defect_type,
                    cost_rub=cost_rub,
                )
            )
        else:
            row.cost_rub = cost_rub
