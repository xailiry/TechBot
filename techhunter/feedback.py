"""Feedback-driven personal calibration.

Pure business logic on top of the FeedbackRepository: feature snapshots for
stored reactions, the reason-aware personal penalty and the global threshold
multiplier. DB access lives in db/repository.py; this module only reads
aggregated stats and decides.
"""
from . import config
from .db import get_session
from .db.repository import FeedbackRepository

_feedback_repo = FeedbackRepository(get_session)


def feedback_features(item, report, valuation) -> dict:
    """Small explainable feature snapshot stored with each reaction."""
    seller_listings = getattr(item, "seller_listings", None)
    seller_reviews = getattr(item, "seller_reviews", None)
    battery = getattr(report, "battery_health", None)
    baseline = getattr(valuation, "baseline_price", None)
    price_ratio = None
    if baseline and getattr(item, "price", None):
        price_ratio = round(item.price / baseline, 3)
    return {
        "title": getattr(item, "title", ""),
        "price": getattr(item, "price", None),
        "device_key": getattr(valuation, "device_key", None),
        "condition": getattr(report, "condition", None),
        "sealed": bool(getattr(report, "is_sealed", False)),
        "battery": battery,
        "defects": list(getattr(report, "defects", []) or []),
        "seller_type": getattr(item, "seller_type", None),
        "seller_label": getattr(item, "seller_label", None),
        "seller_listings": seller_listings,
        "seller_reviews": seller_reviews,
        "shoplike": bool(getattr(valuation, "shoplike", False)),
        "scam_score": getattr(valuation, "scam_score", None),
        "baseline_price": baseline,
        "profit": getattr(valuation, "net_profit", None),
        "profit_pct": getattr(valuation, "profit_pct", None),
        "price_ratio": price_ratio,
        "avito_price_badge": getattr(item, "avito_price_badge", None),
        "avito_market_badge": bool(getattr(item, "avito_market_badge", False)),
        "reseller_rebuild_signal": bool(
            (
                seller_listings is not None and seller_listings >= 8
            )
            or bool(getattr(valuation, "shoplike", False))
            or (
                battery == 100
                and not bool(getattr(report, "is_sealed", False))
            )
        ),
    }


def _model_base(model: str | None) -> str | None:
    if not model:
        return None
    return model.split("[", 1)[0].strip().lower()


def _wrong_model_signal(target_query: str | None, report) -> bool:
    if not target_query:
        return False

    from .ai.normalize import normalize_device
    from .ai.specs import extract_specs

    specs = extract_specs(target_query)
    expected = normalize_device(
        target_query,
        storage_gb=specs.storage_gb,
        ram_gb=specs.ram_gb,
    )
    if not expected.model:
        return False

    actual_model = _model_base(getattr(report, "model", None))
    if actual_model is None:
        return True
    if actual_model != _model_base(expected.model):
        return True

    actual_storage = getattr(report, "storage_gb", None)
    return expected.storage_gb is not None and actual_storage != expected.storage_gb


def evaluate_personal_penalty(
    stats: dict[str, int],
    item,
    report,
    valuation,
    *,
    target_query: str | None = None,
) -> dict:
    """Reason-aware personal calibration, pure part.

    `stats` is the per-user thumbs-down reason tally; the monitor fetches it
    once per cycle target instead of querying the DB per item.
    """
    if not stats:
        return {"drop": False, "reason": None, "score_penalty": 0, "extra_profit": 0}

    f = feedback_features(item, report, valuation)
    reason = None
    score_penalty = 0
    extra_profit = 0

    if stats.get("reseller_rebuild", 0) >= 2 and f["reseller_rebuild_signal"]:
        reason = "feedback_reseller_rebuild"
        score_penalty += min(35, 15 + stats["reseller_rebuild"] * 5)
        extra_profit += min(20000, 6000 + stats["reseller_rebuild"] * 2000)
    elif stats.get("reseller", 0) >= 3 and (
        f["shoplike"]
        or f["seller_type"] == "shop"
        or (f["seller_listings"] is not None and f["seller_listings"] >= 8)
    ):
        reason = "feedback_reseller"
        score_penalty += min(25, 10 + stats["reseller"] * 3)
        extra_profit += min(15000, 4000 + stats["reseller"] * 1500)
    elif stats.get("battery", 0) >= 2 and (
        (f["battery"] is not None and f["battery"] < 88)
        or "battery_replaced" in f["defects"]
        or (f["battery"] == 100 and f["reseller_rebuild_signal"])
    ):
        reason = "feedback_battery"
        score_penalty += 15
        extra_profit += 5000
    elif stats.get("condition", 0) >= 2 and f["condition"] in {
        "defect", "broken", "for_parts"
    }:
        reason = "feedback_condition"
        score_penalty += 15
        extra_profit += 5000
    elif stats.get("wrong_model", 0) >= 2 and _wrong_model_signal(
        target_query, report
    ):
        reason = "feedback_wrong_model"
        score_penalty += 20
        extra_profit += 10000
    elif stats.get("too_expensive", 0) >= 3:
        reason = "feedback_too_expensive"
        extra_profit += min(20000, 5000 + stats["too_expensive"] * 1500)
    elif stats.get("scam", 0) >= 2 and (f["scam_score"] or 100) < 80:
        reason = "feedback_scam"
        score_penalty += 20

    if not reason:
        return {"drop": False, "reason": None, "score_penalty": 0, "extra_profit": 0}

    adjusted_score = (f["scam_score"] or 0) - score_penalty
    required_profit = (getattr(valuation, "net_profit", None) or 0) - extra_profit
    drop = adjusted_score < 60 or required_profit < config.MIN_PROFIT_RUB
    return {
        "drop": drop,
        "reason": reason,
        "score_penalty": score_penalty,
        "extra_profit": extra_profit,
    }


async def feedback_personal_penalty(
    tg_id: int,
    item,
    report,
    valuation,
    *,
    target_query: str | None = None,
) -> dict:
    """Convenience wrapper that fetches the user's reason stats itself."""
    stats = await _feedback_repo.feedback_reason_stats(tg_id)
    return evaluate_personal_penalty(
        stats, item, report, valuation, target_query=target_query
    )


async def feedback_threshold_multiplier(tg_id: int) -> float:
    """Tiny feedback loop for delivery gates.

    A few reactions are too noisy, so we only tune after 5+ labels. Mostly
    thumbs-down means be stricter; mostly thumbs-up lets the user see slightly
    thinner deals. The valuation itself stays deterministic and auditable.
    """
    st = await _feedback_repo.feedback_stats(tg_id)
    total = st["total"]
    if total < 5:
        return 1.0
    down_rate = st["down"] / total
    up_rate = st["up"] / total
    if down_rate >= 0.65:
        return 1.35
    if down_rate >= 0.50:
        return 1.15
    if up_rate >= 0.80:
        return 0.85
    return 1.0
