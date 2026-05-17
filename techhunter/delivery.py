"""Per-user delivery filters (M1).

Pure logic, no DB/aiogram, so it is trivially unit-testable and shared by
the monitor. Decides whether an already-profitable lot should actually be
pushed to a given user, on top of the opportunity gate.
"""
from dataclasses import dataclass, field

# Condition values the user may choose to exclude (must match Condition enum).
SELECTABLE_CONDITIONS = ("ideal", "good", "defect", "broken", "for_parts")


@dataclass
class DeliveryPrefs:
    paused: bool = False
    min_score: int = 0
    exclude_conditions: set[str] = field(default_factory=set)
    exclude_shop: bool = False


def parse_exclude_conditions(csv: str | None) -> set[str]:
    if not csv:
        return set()
    return {
        c.strip()
        for c in csv.split(",")
        if c.strip() in SELECTABLE_CONDITIONS
    }


def passes_filters(prefs: DeliveryPrefs, item, report, valuation):
    """Return (ok, reason). reason is a short code when dropped (for logs
    and future analytics), None when delivered."""
    if prefs.paused:
        return False, "paused"
    if prefs.exclude_shop and getattr(item, "seller_type", None) == "shop":
        return False, "shop"
    if (
        prefs.min_score
        and getattr(valuation, "scam_score", 0) < prefs.min_score
    ):
        return False, "low_score"
    if report.condition in prefs.exclude_conditions:
        return False, "excluded_condition"
    return True, None
