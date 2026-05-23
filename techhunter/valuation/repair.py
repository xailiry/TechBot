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

# Default estimated repair costs (parts + labor) for common models.
# Used if the operator has not entered specific prices in the DB.
DEFAULT_REPAIR_COSTS: dict[str, dict[str, int]] = {
    "iPhone 11": {"screen": 5000, "back_glass": 3000, "battery": 3000, "faceid": 5000, "no_power": 4000},
    "iPhone 12": {"screen": 7500, "back_glass": 4500, "battery": 3500, "faceid": 6000, "no_power": 5000},
    "iPhone 13": {"screen": 11000, "back_glass": 6000, "battery": 4500, "faceid": 8000, "no_power": 6000},
    "iPhone 14": {"screen": 16000, "back_glass": 8000, "battery": 5500, "faceid": 10000, "no_power": 8000},
    "iPhone 15": {"screen": 28000, "back_glass": 14000, "battery": 7000, "faceid": 15000, "no_power": 12000},
}


async def get_repair_cost_meta(
    brand: str, model: str, defect_type: str
) -> tuple[int | None, bool]:
    """Return (cost, estimated).

    DB prices are operator-entered real prices. Built-in defaults are only
    estimates, and the card must surface that weaker source.
    """
    if not brand or not model:
        return None, False
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

    if best:
        return best.cost_rub, False

    # Fallback to defaults
    if brand.lower() == "apple":
        for pattern, costs in DEFAULT_REPAIR_COSTS.items():
            if model.lower().startswith(pattern.lower()):
                cost = costs.get(defect_type)
                return (cost, True) if cost is not None else (None, False)

    return None, False


async def get_repair_cost(
    brand: str, model: str, defect_type: str
) -> int | None:
    """Longest model_pattern prefix match, falling back to defaults."""
    cost, _estimated = await get_repair_cost_meta(brand, model, defect_type)
    return cost



async def estimate_repairs_meta(
    brand: str, model: str, defects: list[str]
) -> tuple[int | None, dict[str, int], list[str], bool, bool]:
    """Return (total, breakdown, missing, blocked).

    total is None if any required repair cost is unknown (so the engine will
    not claim a profit). blocked is True if a defect makes a cheap flip
    unrealistic (e.g. Face ID)."""
    blocked = any(d in FLIP_BLOCKING for d in defects)
    breakdown: dict[str, int] = {}
    missing: list[str] = []
    estimated = False
    for d in defects:
        rtype = REPAIRABLE.get(d)
        if not rtype or rtype in breakdown or rtype in missing:
            continue
        cost, is_estimated = await get_repair_cost_meta(brand, model, rtype)
        if cost is None:
            missing.append(rtype)
        else:
            breakdown[rtype] = cost
            estimated = estimated or is_estimated
    total = None if missing else sum(breakdown.values())
    return total, breakdown, missing, blocked, estimated


async def estimate_repairs(
    brand: str, model: str, defects: list[str]
) -> tuple[int | None, dict[str, int], list[str], bool]:
    """Backward-compatible repair estimate without source metadata."""
    total, breakdown, missing, blocked, _estimated = await estimate_repairs_meta(
        brand, model, defects
    )
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
