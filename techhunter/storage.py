"""Async DB helpers: subscriptions, dedup, card state, alert log."""
import asyncio
import json
import logging

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import config
from .ai.images import hamming
from .db import get_session
from .db.models import (
    CardState,
    DealFeedback,
    DeviceCatalog,
    ImageHash,
    Listing,
    MarketBaseline,
    PendingAlert,
    PriceObservation,
    SentAlert,
    Subscription,
    User,
)
from .scraper.models import ParsedListing

log = logging.getLogger(__name__)


async def upsert_user(tg_id: int, username: str | None = None) -> None:
    async with get_session() as s:
        stmt = (
            sqlite_insert(User)
            .values(tg_id=tg_id, username=username)
            .on_conflict_do_update(
                index_elements=[User.tg_id], set_={"username": username}
            )
        )
        await s.execute(stmt)


async def set_approved(tg_id: int, approved: bool) -> None:
    async with get_session() as s:
        u = await s.get(User, tg_id)
        if u:
            u.is_approved = approved


async def is_user_approved(tg_id: int) -> bool:
    async with get_session() as s:
        u = await s.get(User, tg_id)
        return bool(u and u.is_approved)


async def add_subscription(
    tg_id: int,
    query: str,
    *,
    city_slug: str = "rossiya",
    min_price: int | None = None,
    max_price: int | None = None,
    min_battery: int | None = None,
    search_pages: int | None = None,
) -> int:
    async with get_session() as s:
        sub = Subscription(
            tg_id=tg_id,
            query=query,
            city_slug=city_slug,
            min_price=min_price,
            max_price=max_price,
            min_battery=min_battery,
            search_pages=search_pages,
        )
        s.add(sub)
        await s.flush()
        return sub.id


async def active_subscriptions() -> list[Subscription]:
    """All subscriptions of non-paused users."""
    async with get_session() as s:
        rows = await s.execute(
            select(Subscription)
            .join(User, User.tg_id == Subscription.tg_id)
            .where(User.paused == 0)
            .order_by(Subscription.id)
        )
        return list(rows.scalars().all())


async def discovery_users() -> list[User]:
    """All users with Discovery Mode enabled."""
    async with get_session() as s:
        rows = await s.execute(
            select(User).where(User.discovery_enabled == 1, User.paused == 0)
        )
        return list(rows.scalars().all())



async def register_listing(item: ParsedListing) -> bool:
    """Dedup layer 1. Returns True if this listing id is new (first seen),
    False if already processed (also refreshes last_seen / last_price)."""
    async with get_session() as s:
        existing = await s.get(Listing, item.id)
        if existing is not None:
            existing.last_price = item.price or existing.last_price
            return False
        s.add(
            Listing(
                id=item.id,
                source="avito",
                url=item.full_url,
                title=item.title,
                last_price=item.price or None,
            )
        )
        return True


async def get_cached_listing(listing_id: str) -> dict | None:
    """Return the cached evaluation if this listing was already processed
    successfully. None means: never seen, or seen but processing failed
    (so it must be retried -> a transient error never loses a lot)."""
    async with get_session() as s:
        row = await s.get(Listing, listing_id)
        if row is None or row.processed_at is None:
            return None
        return {
            "id": row.id,
            "price": row.last_price,
            "processed_at": row.processed_at,
            "report": json.loads(row.report_json) if row.report_json else None,
            "valuation": (
                json.loads(row.valuation_json) if row.valuation_json else None
            ),
        }


async def check_content_duplicate(item: ParsedListing) -> str | None:
    """Returns the original listing_id if a listing with the same content hash
    already exists. Helps detect re-posts with different IDs."""
    h = item.get_content_hash()
    async with get_session() as s:
        existing = await s.execute(
            select(Listing.id).where(
                Listing.content_hash == h,
                Listing.id != item.id
            ).limit(1)
        )
        row = existing.first()
        return row[0] if row else None



async def cache_listing(item: ParsedListing, report, valuation) -> None:
    """Persist a SUCCESSFUL evaluation. Called only after the pipeline
    finished, so a failed listing stays unprocessed and is retried."""
    from datetime import datetime, timezone

    async with get_session() as s:
        stmt = (
            sqlite_insert(Listing)
            .values(
                id=item.id,
                source="avito",
                url=item.full_url,
                title=item.title,
                content_hash=item.get_content_hash(),
                last_price=item.price or None,
                condition=report.condition,
                processed_at=datetime.now(timezone.utc),
                report_json=json.dumps(report.model_dump(), ensure_ascii=False),
                valuation_json=json.dumps(
                    valuation.model_dump(), ensure_ascii=False
                ),
            )
            .on_conflict_do_update(
                index_elements=[Listing.id],
                set_={
                    "last_price": item.price or None,
                    "content_hash": item.get_content_hash(),
                    "condition": report.condition,
                    "processed_at": datetime.now(timezone.utc),
                    "report_json": json.dumps(
                        report.model_dump(), ensure_ascii=False
                    ),
                    "valuation_json": json.dumps(
                        valuation.model_dump(), ensure_ascii=False
                    ),
                },
            )
        )
        await s.execute(stmt)



async def cache_listing_skipped(item: ParsedListing) -> None:
    """Mark a non-target / un-valuable listing as processed (no payload) so
    we do not re-run the pipeline on it every poll."""
    from datetime import datetime, timezone

    async with get_session() as s:
        stmt = (
            sqlite_insert(Listing)
            .values(
                id=item.id, source="avito", url=item.full_url,
                title=item.title, last_price=item.price or None,
                content_hash=item.get_content_hash(),
                processed_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=[Listing.id],
                set_={
                    "last_price": item.price or None,
                    "content_hash": item.get_content_hash(),
                    "processed_at": datetime.now(timezone.utc),
                    "report_json": None,
                    "valuation_json": None,
                },
            )
        )
        await s.execute(stmt)



async def count_reused_images(listing_id: str, img_hash: str) -> int:
    """How many OTHER listings carry the same photo. Exact-hash match
    first (uses the index, O(matches), catches the common scam of the
    identical file), then a bounded fuzzy near-dup pass on the most
    recent rows only - so cost stays bounded as the table grows."""
    if not img_hash:
        return 0
    others: set[str] = set()
    async with get_session() as s:
        exact = await s.execute(
            select(ImageHash.listing_id)
            .where(
                ImageHash.img_hash == img_hash,
                ImageHash.listing_id != listing_id,
            )
        )
        others.update(r[0] for r in exact.all())

        if config.DHASH_MAX_DISTANCE > 0:
            recent = await s.execute(
                select(ImageHash.listing_id, ImageHash.img_hash)
                .order_by(ImageHash.id.desc())
                .limit(config.DHASH_FUZZY_LIMIT)
            )
            recent_rows = [(r[0], r[1]) for r in recent.all()]
            # Offload the heavy Hamming distance comparisons to a background thread
            others = await asyncio.to_thread(
                _find_fuzzy_matches,
                listing_id,
                img_hash,
                others,
                recent_rows,
                config.DHASH_MAX_DISTANCE,
            )
    return len(others)


def _find_fuzzy_matches(
    listing_id: str,
    img_hash: str,
    others: set[str],
    recent_rows: list[tuple[str, str]],
    max_distance: int,
) -> set[str]:
    res = set(others)
    for other_id, other_hash in recent_rows:
        if other_id == listing_id or other_id in res:
            continue
        if hamming(img_hash, other_hash) <= max_distance:
            res.add(other_id)
    return res


async def record_image_hash(listing_id: str, img_hash: str) -> None:
    """One hash row per listing is enough; skip if it already has one
    (re-processing on cache expiry must not grow the table)."""
    if not img_hash:
        return
    async with get_session() as s:
        exists = await s.execute(
            select(ImageHash.id)
            .where(ImageHash.listing_id == listing_id)
            .limit(1)
        )
        if exists.first() is not None:
            return
        s.add(ImageHash(listing_id=listing_id, img_hash=img_hash))


async def cleanup_old_rows() -> dict[str, int]:
    """Prune rows beyond their retention windows so SQLite stays small
    over long unattended runs. Returns deleted counts per table."""
    now = datetime.now(timezone.utc)

    def _cut(days: int) -> datetime:
        return now - timedelta(days=days)

    plan = [
        (ImageHash, ImageHash.created_at,
         config.IMAGE_HASH_RETENTION_DAYS),
        (PriceObservation, PriceObservation.observed_at,
         config.PRICE_OBS_RETENTION_DAYS),
        (Listing, Listing.last_seen, config.LISTINGS_RETENTION_DAYS),
        (CardState, CardState.created_at,
         config.CARD_STATE_RETENTION_DAYS),
        (SentAlert, SentAlert.sent_at, config.SENT_ALERT_RETENTION_DAYS),
        (PendingAlert, PendingAlert.created_at,
         config.PENDING_ALERT_RETENTION_DAYS),
    ]
    out: dict[str, int] = {}
    async with get_session() as s:
        for model, col, days in plan:
            res = await s.execute(delete(model).where(col < _cut(days)))
            out[model.__tablename__] = res.rowcount or 0
    return out


# ─── Telegram bot helpers (Stage 4) ─────────────────────────────────────────

async def list_subscriptions(tg_id: int) -> list[Subscription]:
    async with get_session() as s:
        rows = await s.execute(
            select(Subscription)
            .where(Subscription.tg_id == tg_id)
            .order_by(Subscription.id)
        )
        return list(rows.scalars().all())


async def get_subscription(tg_id: int, sub_id: int) -> Subscription | None:
    async with get_session() as s:
        sub = await s.get(Subscription, sub_id)
        return sub if sub is not None and sub.tg_id == tg_id else None


async def record_feedback(
    tg_id: int,
    listing_id: str,
    reaction: str,
    *,
    reason: str | None = None,
    features: dict | None = None,
) -> None:
    """Persist a user's reaction to a deal (feedback loop)."""
    if reaction not in ("up", "down"):
        return
    if reaction == "up":
        reason = None
    async with get_session() as s:
        stmt = (
            sqlite_insert(DealFeedback)
            .values(
                tg_id=tg_id,
                listing_id=listing_id,
                reaction=reaction,
                reason=reason,
                feature_json=(
                    json.dumps(features, ensure_ascii=False)
                    if features is not None else None
                ),
            )
            .on_conflict_do_update(
                index_elements=[
                    DealFeedback.tg_id, DealFeedback.listing_id
                ],
                set_={
                    "reaction": reaction,
                    "reason": reason,
                    "feature_json": (
                        json.dumps(features, ensure_ascii=False)
                        if features is not None else None
                    ),
                },
            )
        )
        await s.execute(stmt)


async def feedback_stats(tg_id: int | None = None) -> dict:
    """Reaction tallies + accuracy and avg profit% split by reaction
    (joined with SentAlert) to inform threshold calibration."""
    async with get_session() as s:
        q = select(DealFeedback.reaction, DealFeedback.tg_id,
                   DealFeedback.listing_id)
        if tg_id is not None:
            q = q.where(DealFeedback.tg_id == tg_id)
        fb = (await s.execute(q)).all()
        up = sum(1 for r, *_ in fb if r == "up")
        down = sum(1 for r, *_ in fb if r == "down")
        return {"up": up, "down": down, "total": up + down}


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


async def feedback_reason_stats(tg_id: int) -> dict[str, int]:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(DealFeedback.reason, func.count())
                .where(
                    DealFeedback.tg_id == tg_id,
                    DealFeedback.reaction == "down",
                    DealFeedback.reason.is_not(None),
                )
                .group_by(DealFeedback.reason)
            )
        ).all()
    return {str(reason): int(count) for reason, count in rows if reason}


async def feedback_personal_penalty(
    tg_id: int,
    item,
    report,
    valuation,
    *,
    target_query: str | None = None,
) -> dict:
    """Reason-aware personal calibration.

    The old feedback loop only changed the global profit threshold. This one
    adds explainable reason-specific pressure after repeated dislikes.
    """
    stats = await feedback_reason_stats(tg_id)
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


async def feedback_threshold_multiplier(tg_id: int) -> float:
    """Tiny feedback loop for delivery gates.

    A few reactions are too noisy, so we only tune after 5+ labels. Mostly
    thumbs-down means be stricter; mostly thumbs-up lets the user see slightly
    thinner deals. The valuation itself stays deterministic and auditable.
    """
    st = await feedback_stats(tg_id)
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


async def remove_subscription(tg_id: int, sub_id: int) -> bool:
    async with get_session() as s:
        sub = await s.get(Subscription, sub_id)
        if sub is None or sub.tg_id != tg_id:
            return False
        await s.delete(sub)
        return True


async def mark_subscription_onboarded(sub_id: int) -> None:
    """Persist that the deep market analysis finished (or was refreshed),
    so a restart does not re-run it from scratch."""
    from datetime import datetime, timezone

    async with get_session() as s:
        sub = await s.get(Subscription, sub_id)
        if sub is not None:
            sub.onboarded_at = datetime.now(timezone.utc)


async def set_paused(tg_id: int, paused: bool) -> None:
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is not None:
            user.paused = 1 if paused else 0


async def is_paused(tg_id: int) -> bool:
    async with get_session() as s:
        user = await s.get(User, tg_id)
        return bool(user and user.paused)


async def get_delivery_prefs(tg_id: int):
    """Per-user delivery filters. Defaults if the user row is absent."""
    from .delivery import DeliveryPrefs, parse_exclude_conditions

    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is None:
            return DeliveryPrefs()
        return DeliveryPrefs(
            paused=bool(user.paused),
            min_score=int(user.min_score or 0),
            exclude_conditions=parse_exclude_conditions(
                user.exclude_conditions
            ),
            exclude_shop=bool(user.exclude_shop),
        )


async def set_min_score(tg_id: int, value: int) -> None:
    value = max(0, min(100, value))
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is not None:
            user.min_score = value


async def set_exclude_shop(tg_id: int, value: bool) -> None:
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is not None:
            user.exclude_shop = 1 if value else 0


async def toggle_exclude_condition(tg_id: int, condition: str) -> bool:
    """Flip exclusion of one condition tier. Returns the new state
    (True = now excluded)."""
    from .delivery import SELECTABLE_CONDITIONS, parse_exclude_conditions

    if condition not in SELECTABLE_CONDITIONS:
        return False
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is None:
            return False
        cur = parse_exclude_conditions(user.exclude_conditions)
        now_excluded = condition not in cur
        if now_excluded:
            cur.add(condition)
        else:
            cur.discard(condition)
        user.exclude_conditions = ",".join(sorted(cur))
        return now_excluded


async def set_discovery_enabled(tg_id: int, enabled: bool) -> None:
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is not None:
            user.discovery_enabled = 1 if enabled else 0


async def set_discovery_profit(tg_id: int, rub: int | None = None, ratio: float | None = None) -> None:
    async with get_session() as s:
        user = await s.get(User, tg_id)
        if user is not None:
            if rub is not None:
                user.discovery_min_profit_rub = rub
            if ratio is not None:
                user.discovery_min_profit_ratio = ratio



async def all_user_ids() -> list[int]:
    async with get_session() as s:
        rows = await s.execute(select(User.tg_id))
        return [r[0] for r in rows.all()]


async def save_card_state(
    tg_id: int,
    message_id: int,
    item: dict,
    valuation: dict,
    report: dict | None = None,
    sub_query: str | None = None,
) -> None:
    if isinstance(report, str) and sub_query is None:
        sub_query = report
        report = None
    report_json = json.dumps(report, ensure_ascii=False) if report is not None else None
    async with get_session() as s:
        stmt = (
            sqlite_insert(CardState)
            .values(
                tg_id=tg_id,
                message_id=message_id,
                item_json=json.dumps(item, ensure_ascii=False),
                report_json=report_json,
                score_json=json.dumps(valuation, ensure_ascii=False),
                sub_query=sub_query,
            )
            .on_conflict_do_update(
                index_elements=[CardState.tg_id, CardState.message_id],
                set_={
                    "item_json": json.dumps(item, ensure_ascii=False),
                    "report_json": report_json,
                    "score_json": json.dumps(valuation, ensure_ascii=False),
                    "sub_query": sub_query,
                },
            )
        )
        await s.execute(stmt)


async def get_card_state(tg_id: int, message_id: int) -> dict | None:
    async with get_session() as s:
        row = await s.get(CardState, (tg_id, message_id))
        if row is None:
            return None
        return {
            "item": json.loads(row.item_json),
            "report": json.loads(row.report_json) if row.report_json else None,
            "valuation": json.loads(row.score_json),
            "sub_query": row.sub_query,
        }


async def alert_already_sent(tg_id: int, listing_id: str) -> bool:
    async with get_session() as s:
        row = await s.get(SentAlert, (tg_id, listing_id))
        return row is not None


async def mark_alert_sent(
    tg_id: int,
    listing_id: str,
    *,
    price: int | None = None,
    profit: int | None = None,
    verdict: str | None = None,
    condition: str | None = None,
    sub_query: str | None = None,
) -> None:
    async with get_session() as s:
        stmt = (
            sqlite_insert(SentAlert)
            .values(
                tg_id=tg_id,
                listing_id=listing_id,
                price=price,
                profit=profit,
                verdict=verdict,
                condition=condition,
                sub_query=sub_query,
            )
            .on_conflict_do_nothing(
                index_elements=[SentAlert.tg_id, SentAlert.listing_id]
            )
        )
        await s.execute(stmt)


async def queue_pending_alert(
    tg_id: int,
    item: ParsedListing,
    report,
    valuation,
    *,
    sub_query: str | None = None,
    error: str | None = None,
) -> None:
    """Persist a deal card for later delivery when Telegram is reachable."""
    async with get_session() as s:
        stmt = (
            sqlite_insert(PendingAlert)
            .values(
                tg_id=tg_id,
                listing_id=item.id,
                item_json=json.dumps(item.model_dump(), ensure_ascii=False),
                report_json=json.dumps(report.model_dump(), ensure_ascii=False),
                valuation_json=json.dumps(
                    valuation.model_dump(), ensure_ascii=False
                ),
                sub_query=sub_query,
                last_error=error,
                next_attempt_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=[PendingAlert.tg_id, PendingAlert.listing_id],
                set_={
                    "item_json": json.dumps(
                        item.model_dump(), ensure_ascii=False
                    ),
                    "report_json": json.dumps(
                        report.model_dump(), ensure_ascii=False
                    ),
                    "valuation_json": json.dumps(
                        valuation.model_dump(), ensure_ascii=False
                    ),
                    "sub_query": sub_query,
                    "last_error": error,
                    "dead_reason": None,
                    "next_attempt_at": datetime.now(timezone.utc),
                },
            )
        )
        await s.execute(stmt)


async def list_pending_alerts(limit: int = 10) -> list[dict]:
    now = datetime.now(timezone.utc)
    async with get_session() as s:
        rows = await s.execute(
            select(PendingAlert)
            .where(
                PendingAlert.dead_reason.is_(None),
                or_(
                    PendingAlert.next_attempt_at.is_(None),
                    PendingAlert.next_attempt_at <= now,
                ),
            )
            .order_by(PendingAlert.created_at)
            .limit(limit)
        )
        out = []
        for row in rows.scalars().all():
            out.append(
                {
                    "tg_id": row.tg_id,
                    "listing_id": row.listing_id,
                    "item": json.loads(row.item_json),
                    "report": json.loads(row.report_json),
                    "valuation": json.loads(row.valuation_json),
                    "sub_query": row.sub_query,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "created_at": row.created_at,
                    "next_attempt_at": row.next_attempt_at,
                    "dead_reason": row.dead_reason,
                }
            )
        return out


async def mark_pending_alert_attempt(
    tg_id: int,
    listing_id: str,
    error: str | None = None,
    *,
    dead_reason: str | None = None,
) -> None:
    async with get_session() as s:
        row = await s.get(PendingAlert, (tg_id, listing_id))
        if row is None:
            return
        row.attempts = (row.attempts or 0) + 1
        row.last_attempt_at = datetime.now(timezone.utc)
        row.last_error = error
        if dead_reason:
            row.dead_reason = dead_reason
            row.next_attempt_at = None
        else:
            delay = min(3600, 60 * (2 ** min(row.attempts - 1, 5)))
            row.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                seconds=delay
            )


async def delete_pending_alert(tg_id: int, listing_id: str) -> None:
    async with get_session() as s:
        await s.execute(
            delete(PendingAlert).where(
                PendingAlert.tg_id == tg_id,
                PendingAlert.listing_id == listing_id,
            )
        )


async def clear_dead_pending_alerts() -> int:
    """Drop outbox rows that will never be retried again."""
    async with get_session() as s:
        res = await s.execute(
            delete(PendingAlert).where(PendingAlert.dead_reason.is_not(None))
        )
        return int(res.rowcount or 0)


async def pending_alert_stats() -> dict:
    async with get_session() as s:
        pending = (
            await s.execute(
                select(func.count()).select_from(PendingAlert)
                .where(PendingAlert.dead_reason.is_(None))
            )
        ).scalar() or 0
        dead = (
            await s.execute(
                select(func.count()).select_from(PendingAlert)
                .where(PendingAlert.dead_reason.is_not(None))
            )
        ).scalar() or 0
        oldest = (
            await s.execute(
                select(PendingAlert.created_at)
                .where(PendingAlert.dead_reason.is_(None))
                .order_by(PendingAlert.created_at)
                .limit(1)
            )
        ).scalar()
    return {"pending": int(pending), "dead": int(dead), "oldest": oldest}


async def dev_db_stats() -> dict[str, int]:
    """Raw table counters for the owner-only /dev panel."""
    models = {
        "devices": DeviceCatalog,
        "baselines": MarketBaseline,
        "observations": PriceObservation,
        "listings": Listing,
        "sent_alerts": SentAlert,
        "pending_alerts": PendingAlert,
        "card_state": CardState,
        "feedback": DealFeedback,
    }
    out: dict[str, int] = {}
    async with get_session() as s:
        for key, model in models.items():
            out[key] = int(
                (
                    await s.execute(select(func.count()).select_from(model))
                ).scalar()
                or 0
            )
    return out
