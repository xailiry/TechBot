"""Condition grading for electronics. Ported in spirit from Shmotki
condition.py but driven by structured defect codes (Stage 3 needs to know
*which* defect to price the repair)."""
from enum import Enum

from .specs import DeviceSpecs

_SEALED_KEYWORDS = ("идеал", "как новый", "новый", "запечатан", "не вскрыт")
_DECLARED_DEFECT_KEYWORDS = (
    "удовлетворительное",
    "состояние удовлетвор",
    "с дефект",
    "есть дефект",
    "имеются дефект",
)

# A FOR_PARTS device cannot be resold as working without major work.
_FOR_PARTS = {"icloud_locked", "carrier_locked"}
# A BROKEN device is intact-but-cracked/display-damaged: the key "cheap
# broken lot" signal.
_BROKEN = {
    "screen_cracked", "screen_display_defect", "back_glass_cracked", "no_power",
}
_DEFECT = {
    "screen_replaced", "battery_replaced", "faceid_broken",
    "truetone_missing", "not_original_parts", "replica", "cosmetic_wear",
}


class Condition(Enum):
    IDEAL = "ideal"
    GOOD = "good"
    DEFECT = "defect"
    BROKEN = "broken"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"


def grade_condition(specs: DeviceSpecs, text: str = "") -> Condition:
    d = specs.defects

    if d & _FOR_PARTS:
        return Condition.FOR_PARTS
    if d & _BROKEN:
        return Condition.BROKEN
    if d & _DEFECT:
        return Condition.DEFECT

    low = text.lower()
    if any(k in low for k in _DECLARED_DEFECT_KEYWORDS):
        return Condition.DEFECT
    if specs.is_sealed or any(k in low for k in _SEALED_KEYWORDS):
        # "идеал" with no detected defects -> top condition.
        return Condition.IDEAL

    # Avito default for used electronics without stated issues.
    return Condition.GOOD
