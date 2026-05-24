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
    get_model_working_meta,
    get_or_create_device,
    get_working_meta,
    log_observation,
)
from .repair import estimate_repairs_meta
from .scam import looks_shoplike, score_listing

if TYPE_CHECKING:
    from ..ai.evaluate import EvaluationReport
    from ..scraper.models import ParsedListing

log = logging.getLogger(__name__)

_WORKING = {"ideal", "good"}


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
    repair_estimated: bool = False
    shoplike: bool = False

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


async def fast_value_listing(
    item: "ParsedListing", *, is_discovery: bool = False
) -> str:
    """Stage 1 triage from the search-card data only (no detail fetch).

    Returns one of:
      "skip"  - not a recognized target, or recognized + has a baseline but
                the price is not interesting -> do nothing.
      "learn" - recognized device without a confident baseline yet. In
                Discovery this is learned cheaply from the card itself
                (see learn_from_card); a subscription deep-fetches it.
      "deal"  - recognized, baseline exists, price beats the gate -> open
                the detail page for full evaluation.

    Storage is parsed from the card title so the gate resolves the SAME
    device identity that learning writes to (otherwise a storage-less gate
    would never see the storage-specific baseline).
    """
    from ..ai.normalize import normalize_device
    from ..ai.specs import extract_specs

    specs = extract_specs(item.title, item.description, item.params)
    norm = normalize_device(
        item.title, storage_gb=specs.storage_gb, ram_gb=specs.ram_gb
    )
    if not norm.model:
        return "skip"  # could not even guess what it is
    if norm.storage_gb is None:
        baseline, sample, _variants = await get_model_working_meta(
            norm.brand, norm.model
        )
        if baseline is None or sample < config.BASELINE_MIN_SAMPLE:
            return "learn"
        market = baseline * (1 - config.PROFIT_HAGGLE_PERCENT)
        limit = market * config.FAST_VALUATION_THRESHOLD_PCT
        return "deal" if (item.price > 0 and item.price < limit) else "skip"

    device_id = await get_or_create_device(
        norm.brand, norm.model, norm.storage_gb, norm.ram_gb
    )
    if device_id is None:
        return "skip"  # unresolved brand -> cannot value or learn it

    baseline, sample, _ = await get_working_meta(device_id)
    if baseline is None or sample < config.BASELINE_MIN_SAMPLE:
        return "learn"  # insufficient data -> grow the baseline

    # If it's cheaper than (market * (1 - haggle) * threshold), it's a candidate.
    # We use a loose threshold because we don't know the condition yet.
    market = baseline * (1 - config.PROFIT_HAGGLE_PERCENT)
    limit = market * config.FAST_VALUATION_THRESHOLD_PCT
    return "deal" if (item.price > 0 and item.price < limit) else "skip"


async def learn_from_card(item: "ParsedListing", source: str = "avito") -> bool:
    """Card-only price learning: grade condition from the title and log a real
    observation WITHOUT opening the listing detail page. Cheap and calm (no
    browser navigation), so Discovery can learn the whole category fast.
    Returns True if an observation was logged."""
    from ..ai.normalize import normalize_device
    from ..ai.specs import extract_specs
    from ..ai.condition import grade_condition
    from .scam import looks_shoplike

    specs = extract_specs(item.title, item.description, item.params)
    norm = normalize_device(
        item.title, storage_gb=specs.storage_gb, ram_gb=specs.ram_gb
    )
    if not norm.model:
        return False
    if norm.storage_gb is None:
        return False
    if specs.is_new:
        return False  # never learn the used market from sealed/new lots
    # Only title-level shop signals are visible before the detail page; enough
    # to drop the obvious resellers from the learned median.
    if looks_shoplike(
        item.title, item.description, item.seller_type, item.seller_listings
    ):
        return False
    device_id = await get_or_create_device(
        norm.brand, norm.model, norm.storage_gb, norm.ram_gb
    )
    if device_id is None:
        return False
    cond = grade_condition(specs, item.title).value
    return await log_observation(
        device_id, cond, item.price,
        listing_id=item.id, raw_title=item.title, storage_gb=norm.storage_gb,
        source=source
    )


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
        v.shoplike = looks_shoplike(
            item.title, item.description,
            item.seller_type, item.seller_listings,
        )
        if (
            log_obs
            and not report.is_new
            and not v.shoplike
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
        avito_market_badge=getattr(item, "avito_market_badge", False),
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

    # Honest market price: median minus expected haggle.
    market = baseline * (1 - config.PROFIT_HAGGLE_PERCENT)

    if item.price > 0:
        v.gross_profit = int(market - item.price)

    overhead = config.PROFIT_OVERHEAD_RUB
    cond = report.condition
    market_badge_conflict = (
        bool(getattr(item, "avito_market_badge", False))
        and cond in _WORKING
        and item.price > 0
        and baseline > 0
        and (item.price / baseline) <= config.AVITO_MARKET_BADGE_CONFLICT_RATIO
    )

    if cond in _WORKING:
        if v.gross_profit is not None:
            v.net_profit = v.gross_profit - overhead
            v.opportunity_type = "working"
    elif cond == "for_parts":
        missing.append("for_parts")
    elif cond == "unknown":
        missing.append("condition_unknown")
    else:  # broken / defect -> repair-and-flip math
        total, breakdown, miss, blocked, estimated = await estimate_repairs_meta(
            report.brand, report.model or "", report.defects
        )
        v.repair_breakdown = breakdown
        v.repair_estimated = estimated
        if blocked:
            missing.append("flip_blocked")
        elif miss:
            missing.append("repair_cost_unknown")
            v.opportunity_type = "broken_flip"  # informational only
        elif not breakdown:
            defect_baseline = v.condition_baselines.get("defect")
            if cond == "defect" and defect_baseline is not None:
                if item.price > 0:
                    v.net_profit = int(defect_baseline * (1 - config.PROFIT_HAGGLE_PERCENT)) - item.price - overhead
                    v.opportunity_type = "working"
            else:
                missing.append("condition_discount_unknown")
        else:
            v.repair_cost = total
            if item.price > 0:
                v.net_profit = int(market - item.price - total - overhead)
                v.opportunity_type = "broken_flip"

    if v.net_profit is not None and market:
        v.profit_pct = round(v.net_profit / market, 4)
        v.opportunity = (
            v.net_profit >= config.MIN_PROFIT_RUB
            and v.profit_pct >= config.MIN_PROFIT_RATIO
            and scam.verdict != "fake"
        )
        if v.opportunity and market_badge_conflict:
            v.opportunity = False
            missing.append("avito_market_badge_conflict")


    v.missing = missing
    return v
