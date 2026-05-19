"""Valuation orchestrator: ParsedListing + EvaluationReport -> Valuation.

Implements Stage 3.1-3.4:
  - logs every recognized lot into PriceObservation,
  - reads/auto-learns the working baseline (real data only),
  - computes normal margin and the broken-lot post-repair net,
  - runs the scam guard (which spares cheap broken lots).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .. import config
from .devices import (
    get_condition_baselines,
    get_or_create_device,
    get_working_meta,
    log_observation,
)
from .repair import estimate_repairs
from .scam import looks_shoplike, score_listing

if TYPE_CHECKING:
    from ..ai.evaluate import EvaluationReport
    from ..scraper.models import ParsedListing

log = logging.getLogger(__name__)

_WORKING = {"ideal", "good", "unknown"}


class Valuation(BaseModel):
    listing_id: str
    device_key: str | None = None
    condition: str = "unknown"

    baseline_price: int | None = None
    baseline_sample: int = 0
    baseline_confidence: str = "none"  # none | low | medium | high
    # Learned median per condition tier, for display ("по состояниям").
    condition_baselines: dict[str, int] = Field(default_factory=dict)
    repair_cost: int | None = None
    repair_breakdown: dict[str, int] = Field(default_factory=dict)

    gross_profit: int | None = None
    net_profit: int | None = None
    profit_pct: float | None = None

    opportunity: bool = False
    opportunity_type: str = "none"  # working | broken_flip | none

    scam_score: int = 50
    scam_verdict: str = "unknown"
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    # Why we could not (fully) value it: no_device | no_baseline |
    # repair_cost_unknown | for_parts | flip_blocked
    missing: list[str] = Field(default_factory=list)


async def value_listing(
    item: "ParsedListing", report: "EvaluationReport", *, log_obs: bool = True
) -> Valuation:
    v = Valuation(listing_id=item.id, condition=report.condition)
    missing: list[str] = []

    device_id = await get_or_create_device(
        report.brand, report.model or "", report.storage_gb, report.ram_gb
    )
    if device_id is None:
        missing.append("no_device")
    else:
        v.device_key = (
            f"{report.brand} {report.model}"
            + (f" {report.storage_gb}GB" if report.storage_gb else "")
        )
        # Feed EVERY condition tier from used private listings; exclude
        # sealed/new and shop/retail so medians reflect the real used
        # market, not inflated store prices. Absolute junk is dropped in
        # log_observation.
        if (
            log_obs
            and not report.is_sealed
            and not looks_shoplike(
                item.title, item.description,
                item.seller_type, item.seller_listings,
            )
        ):
            await log_observation(
                device_id,
                report.condition,
                item.price,
                listing_id=item.id,
                raw_title=item.title,
                storage_gb=report.storage_gb,
            )

    baseline = None
    if device_id is not None:
        baseline, sample, age = await get_working_meta(device_id)
        v.baseline_price = baseline
        v.baseline_sample = sample
        if baseline is None:
            v.baseline_confidence = "none"
        elif (
            sample >= config.CONF_HIGH_SAMPLE
            and age <= config.CONF_FRESH_DAYS * 86400
        ):
            v.baseline_confidence = "high"
        elif sample >= config.CONF_MED_SAMPLE:
            v.baseline_confidence = "medium"
        else:
            v.baseline_confidence = "low"
        v.condition_baselines = await get_condition_baselines(device_id)

    scam = score_listing(
        item.title,
        item.description,
        item.price,
        condition=report.condition,
        baseline_price=baseline,
        seller_type=item.seller_type,
        seller_listings=item.seller_listings,
        reused_image_count=report.reused_image_count,
        visual=report.visual,
        defects=report.defects,
    )
    v.scam_score = scam.score
    v.scam_verdict = scam.verdict
    v.pros = scam.pros
    v.cons = scam.cons

    if baseline is None:
        if "no_device" not in missing:
            missing.append("no_baseline")
        v.missing = missing
        return v

    if item.price > 0:
        v.gross_profit = baseline - item.price

    overhead = config.PROFIT_OVERHEAD_RUB
    cond = report.condition

    if cond in _WORKING:
        if v.gross_profit is not None:
            v.net_profit = v.gross_profit - overhead
            v.opportunity_type = "working"
    elif cond == "for_parts":
        missing.append("for_parts")
    else:  # broken / defect -> repair-and-flip math
        total, breakdown, miss, blocked = await estimate_repairs(
            report.brand, report.model or "", report.defects
        )
        v.repair_breakdown = breakdown
        if blocked:
            missing.append("flip_blocked")
        elif miss:
            missing.append("repair_cost_unknown")
            v.opportunity_type = "broken_flip"  # informational only
        elif not breakdown:
            defect_baseline = v.condition_baselines.get("defect")
            if cond == "defect" and defect_baseline is not None:
                if item.price > 0:
                    v.net_profit = defect_baseline - item.price - overhead
                    v.opportunity_type = "working"
            else:
                missing.append("condition_discount_unknown")
        else:
            v.repair_cost = total
            if item.price > 0:
                v.net_profit = baseline - item.price - total - overhead
                v.opportunity_type = "broken_flip"

    if v.net_profit is not None and baseline:
        v.profit_pct = round(v.net_profit / baseline, 4)
        v.opportunity = (
            v.net_profit >= config.MIN_PROFIT_RUB
            and v.profit_pct >= config.MIN_PROFIT_RATIO
            and scam.verdict != "fake"
        )

    v.missing = missing
    return v
