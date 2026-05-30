"""Repository layer for database operations.

Replaces the legacy storage.py module to enforce the Dependency Inversion
Principle (DIP). Business logic receives repository instances rather than
importing specific functions or db models directly.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Callable, AsyncContextManager, Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..ai.images import hamming
from .models import (
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
from ..scraper.models import ParsedListing


class UserRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def upsert_user(self, tg_id: int, username: str | None = None) -> None:
        async with self._sf() as s:
            stmt = (
                sqlite_insert(User)
                .values(tg_id=tg_id, username=username)
                .on_conflict_do_update(
                    index_elements=[User.tg_id], set_={"username": username}
                )
            )
            await s.execute(stmt)

    async def discovery_users(self) -> list[User]:
        async with self._sf() as s:
            rows = await s.execute(
                select(User).where(User.discovery_enabled == 1, User.paused == 0)
            )
            return list(rows.scalars().all())

    async def all_user_ids(self) -> list[int]:
        async with self._sf() as s:
            rows = await s.execute(select(User.tg_id))
            return [r[0] for r in rows.all()]

    async def set_paused(self, tg_id: int, paused: bool) -> None:
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.paused = 1 if paused else 0

    async def is_paused(self, tg_id: int) -> bool:
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            return bool(user and user.paused)

    async def get_delivery_prefs(self, tg_id: int):
        from ..delivery import DeliveryPrefs, parse_exclude_conditions
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is None:
                return DeliveryPrefs()
            return DeliveryPrefs(
                paused=bool(user.paused),
                min_score=int(user.min_score or 0),
                exclude_conditions=parse_exclude_conditions(user.exclude_conditions),
                exclude_shop=bool(user.exclude_shop),
            )

    async def set_min_score(self, tg_id: int, value: int) -> None:
        value = max(0, min(100, value))
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.min_score = value

    async def set_exclude_shop(self, tg_id: int, value: bool) -> None:
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.exclude_shop = 1 if value else 0

    async def toggle_exclude_condition(self, tg_id: int, condition: str) -> bool:
        from ..delivery import SELECTABLE_CONDITIONS, parse_exclude_conditions
        if condition not in SELECTABLE_CONDITIONS:
            return False
        async with self._sf() as s:
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

    async def set_discovery_enabled(self, tg_id: int, enabled: bool) -> None:
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                user.discovery_enabled = 1 if enabled else 0

    async def set_discovery_profit(self, tg_id: int, rub: int | None = None, ratio: float | None = None) -> None:
        async with self._sf() as s:
            user = await s.get(User, tg_id)
            if user is not None:
                if rub is not None:
                    user.discovery_min_profit_rub = rub
                if ratio is not None:
                    user.discovery_min_profit_ratio = ratio


class SubscriptionRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def add_subscription(
        self, tg_id: int, query: str, *,
        city_slug: str = "rossiya", min_price: int | None = None,
        max_price: int | None = None, min_battery: int | None = None,
        search_pages: int | None = None,
    ) -> int:
        async with self._sf() as s:
            sub = Subscription(
                tg_id=tg_id, query=query, city_slug=city_slug,
                min_price=min_price, max_price=max_price,
                min_battery=min_battery, search_pages=search_pages,
            )
            s.add(sub)
            await s.flush()
            return sub.id

    async def active_subscriptions(self) -> list[Subscription]:
        async with self._sf() as s:
            rows = await s.execute(
                select(Subscription)
                .join(User, User.tg_id == Subscription.tg_id)
                .where(User.paused == 0)
                .order_by(Subscription.id)
            )
            return list(rows.scalars().all())

    async def list_subscriptions(self, tg_id: int) -> list[Subscription]:
        async with self._sf() as s:
            rows = await s.execute(
                select(Subscription).where(Subscription.tg_id == tg_id).order_by(Subscription.id)
            )
            return list(rows.scalars().all())

    async def get_subscription(self, tg_id: int, sub_id: int) -> Subscription | None:
        async with self._sf() as s:
            sub = await s.get(Subscription, sub_id)
            return sub if sub is not None and sub.tg_id == tg_id else None

    async def remove_subscription(self, tg_id: int, sub_id: int) -> bool:
        async with self._sf() as s:
            sub = await s.get(Subscription, sub_id)
            if sub is None or sub.tg_id != tg_id:
                return False
            await s.delete(sub)
            return True

    async def mark_subscription_onboarded(self, sub_id: int) -> None:
        async with self._sf() as s:
            sub = await s.get(Subscription, sub_id)
            if sub is not None:
                sub.onboarded_at = datetime.now(timezone.utc)


class ListingRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def register_listing(self, item: ParsedListing) -> bool:
        async with self._sf() as s:
            existing = await s.get(Listing, item.id)
            if existing is not None:
                existing.last_price = item.price or existing.last_price
                return False
            s.add(
                Listing(
                    id=item.id, source="avito", url=item.full_url,
                    title=item.title, last_price=item.price or None,
                )
            )
            return True

    async def get_cached_listing(self, listing_id: str) -> dict | None:
        async with self._sf() as s:
            row = await s.get(Listing, listing_id)
            if row is None or row.processed_at is None:
                return None
            return {
                "id": row.id,
                "price": row.last_price,
                "processed_at": row.processed_at,
                "report": json.loads(row.report_json) if row.report_json else None,
                "valuation": json.loads(row.valuation_json) if row.valuation_json else None,
            }

    async def check_content_duplicate(self, item: ParsedListing) -> str | None:
        h = item.get_content_hash()
        async with self._sf() as s:
            existing = await s.execute(
                select(Listing.id).where(Listing.content_hash == h, Listing.id != item.id).limit(1)
            )
            row = existing.first()
            return row[0] if row else None

    async def cache_listing(self, item: ParsedListing, report, valuation) -> None:
        async with self._sf() as s:
            stmt = (
                sqlite_insert(Listing)
                .values(
                    id=item.id, source="avito", url=item.full_url, title=item.title,
                    content_hash=item.get_content_hash(), last_price=item.price or None,
                    condition=report.condition, processed_at=datetime.now(timezone.utc),
                    report_json=json.dumps(report.model_dump(), ensure_ascii=False),
                    valuation_json=json.dumps(valuation.model_dump(), ensure_ascii=False),
                )
                .on_conflict_do_update(
                    index_elements=[Listing.id],
                    set_={
                        "last_price": item.price or None,
                        "content_hash": item.get_content_hash(),
                        "condition": report.condition,
                        "processed_at": datetime.now(timezone.utc),
                        "report_json": json.dumps(report.model_dump(), ensure_ascii=False),
                        "valuation_json": json.dumps(valuation.model_dump(), ensure_ascii=False),
                    },
                )
            )
            await s.execute(stmt)

    async def cache_listing_skipped(self, item: ParsedListing) -> None:
        async with self._sf() as s:
            stmt = (
                sqlite_insert(Listing)
                .values(
                    id=item.id, source="avito", url=item.full_url, title=item.title,
                    last_price=item.price or None, content_hash=item.get_content_hash(),
                    processed_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=[Listing.id],
                    set_={
                        "last_price": item.price or None,
                        "content_hash": item.get_content_hash(),
                        "processed_at": datetime.now(timezone.utc),
                    },
                )
            )
            await s.execute(stmt)


class AlertRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def save_card_state(
        self, tg_id: int, message_id: int, item: dict, valuation: dict,
        report: dict | None = None, sub_query: str | None = None,
    ) -> None:
        if isinstance(report, str) and sub_query is None:
            sub_query = report
            report = None
        report_json = json.dumps(report, ensure_ascii=False) if report is not None else None
        async with self._sf() as s:
            stmt = (
                sqlite_insert(CardState)
                .values(
                    tg_id=tg_id, message_id=message_id,
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

    async def get_card_state(self, tg_id: int, message_id: int) -> dict | None:
        async with self._sf() as s:
            row = await s.get(CardState, (tg_id, message_id))
            if row is None:
                return None
            return {
                "item": json.loads(row.item_json),
                "report": json.loads(row.report_json) if row.report_json else None,
                "valuation": json.loads(row.score_json),
                "sub_query": row.sub_query,
            }

    async def alert_already_sent(self, tg_id: int, listing_id: str) -> bool:
        async with self._sf() as s:
            row = await s.get(SentAlert, (tg_id, listing_id))
            return row is not None

    async def mark_alert_sent(
        self, tg_id: int, listing_id: str, *,
        price: int | None = None, profit: int | None = None,
        verdict: str | None = None, condition: str | None = None,
        sub_query: str | None = None,
    ) -> None:
        async with self._sf() as s:
            stmt = (
                sqlite_insert(SentAlert)
                .values(
                    tg_id=tg_id, listing_id=listing_id, price=price, profit=profit,
                    verdict=verdict, condition=condition, sub_query=sub_query,
                )
                .on_conflict_do_nothing(index_elements=[SentAlert.tg_id, SentAlert.listing_id])
            )
            await s.execute(stmt)

    async def queue_pending_alert(
        self, tg_id: int, item: ParsedListing, report, valuation, *,
        sub_query: str | None = None, error: str | None = None,
    ) -> None:
        async with self._sf() as s:
            stmt = (
                sqlite_insert(PendingAlert)
                .values(
                    tg_id=tg_id, listing_id=item.id,
                    item_json=json.dumps(item.model_dump(), ensure_ascii=False),
                    report_json=json.dumps(report.model_dump(), ensure_ascii=False),
                    valuation_json=json.dumps(valuation.model_dump(), ensure_ascii=False),
                    sub_query=sub_query, last_error=error,
                    next_attempt_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_update(
                    index_elements=[PendingAlert.tg_id, PendingAlert.listing_id],
                    set_={
                        "item_json": json.dumps(item.model_dump(), ensure_ascii=False),
                        "report_json": json.dumps(report.model_dump(), ensure_ascii=False),
                        "valuation_json": json.dumps(valuation.model_dump(), ensure_ascii=False),
                        "sub_query": sub_query, "last_error": error, "dead_reason": None,
                        "next_attempt_at": datetime.now(timezone.utc),
                    },
                )
            )
            await s.execute(stmt)

    async def list_pending_alerts(self, limit: int = 10) -> list[dict]:
        now = datetime.now(timezone.utc)
        async with self._sf() as s:
            rows = await s.execute(
                select(PendingAlert)
                .where(
                    PendingAlert.dead_reason.is_(None),
                    or_(PendingAlert.next_attempt_at.is_(None), PendingAlert.next_attempt_at <= now),
                )
                .order_by(PendingAlert.created_at)
                .limit(limit)
            )
            out = []
            for row in rows.scalars().all():
                out.append(
                    {
                        "tg_id": row.tg_id, "listing_id": row.listing_id,
                        "item": json.loads(row.item_json),
                        "report": json.loads(row.report_json),
                        "valuation": json.loads(row.valuation_json),
                        "sub_query": row.sub_query, "attempts": row.attempts,
                        "last_error": row.last_error, "created_at": row.created_at,
                    }
                )
            return out

    async def mark_pending_failed(self, tg_id: int, listing_id: str, error: str) -> None:
        async with self._sf() as s:
            row = await s.get(PendingAlert, (tg_id, listing_id))
            if row is not None:
                row.attempts += 1
                row.last_error = error
                delay = min(3600, 15 * (2 ** (row.attempts - 1)))
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                if row.attempts >= config.PENDING_ALERT_MAX_RETRIES:
                    row.dead_reason = "max_retries"

    async def mark_pending_sent(self, tg_id: int, listing_id: str) -> None:
        async with self._sf() as s:
            row = await s.get(PendingAlert, (tg_id, listing_id))
            if row is not None:
                await s.delete(row)


class ImageRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def count_reused_images(self, listing_id: str, img_hash: str) -> int:
        if not img_hash:
            return 0
        others: set[str] = set()
        async with self._sf() as s:
            exact = await s.execute(
                select(ImageHash.listing_id)
                .where(ImageHash.img_hash == img_hash, ImageHash.listing_id != listing_id)
            )
            others.update(r[0] for r in exact.all())

            if config.DHASH_MAX_DISTANCE > 0:
                recent = await s.execute(
                    select(ImageHash.listing_id, ImageHash.img_hash)
                    .order_by(ImageHash.id.desc())
                    .limit(config.DHASH_FUZZY_LIMIT)
                )
                recent_rows = [(r[0], r[1]) for r in recent.all()]
                
                def _find_fuzzy_matches(lid, ihash, oth, rrows, mdist):
                    res = set(oth)
                    for oid, ohash in rrows:
                        if oid == lid or oid in res:
                            continue
                        if hamming(ihash, ohash) <= mdist:
                            res.add(oid)
                    return res

                others = await asyncio.to_thread(
                    _find_fuzzy_matches, listing_id, img_hash, others,
                    recent_rows, config.DHASH_MAX_DISTANCE
                )
        return len(others)

    async def record_image_hash(self, listing_id: str, img_hash: str) -> None:
        if not img_hash:
            return
        async with self._sf() as s:
            exists = await s.execute(
                select(ImageHash.id).where(ImageHash.listing_id == listing_id).limit(1)
            )
            if exists.first() is not None:
                return
            s.add(ImageHash(listing_id=listing_id, img_hash=img_hash))


class MaintenanceRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def cleanup_old_rows(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        def _cut(days: int) -> datetime:
            return now - timedelta(days=days)

        plan = [
            (ImageHash, ImageHash.created_at, config.IMAGE_HASH_RETENTION_DAYS),
            (PriceObservation, PriceObservation.observed_at, config.PRICE_OBS_RETENTION_DAYS),
            (Listing, Listing.last_seen, config.LISTINGS_RETENTION_DAYS),
            (CardState, CardState.created_at, config.CARD_STATE_RETENTION_DAYS),
            (SentAlert, SentAlert.sent_at, config.SENT_ALERT_RETENTION_DAYS),
            (PendingAlert, PendingAlert.created_at, config.PENDING_ALERT_RETENTION_DAYS),
        ]
        out: dict[str, int] = {}
        async with self._sf() as s:
            for model, col, days in plan:
                res = await s.execute(delete(model).where(col < _cut(days)))
                out[model.__tablename__] = res.rowcount or 0
        return out


class FeedbackRepository:
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self._sf = session_factory

    async def record_feedback(self, tg_id: int, listing_id: str, reaction: str, *, reason: str | None = None, features: dict | None = None) -> None:
        if reaction not in ("up", "down"):
            return
        if reaction == "up":
            reason = None
        async with self._sf() as s:
            stmt = (
                sqlite_insert(DealFeedback)
                .values(
                    tg_id=tg_id, listing_id=listing_id, reaction=reaction, reason=reason,
                    feature_json=json.dumps(features, ensure_ascii=False) if features is not None else None,
                )
                .on_conflict_do_update(
                    index_elements=[DealFeedback.tg_id, DealFeedback.listing_id],
                    set_={
                        "reaction": reaction, "reason": reason,
                        "feature_json": json.dumps(features, ensure_ascii=False) if features is not None else None,
                    },
                )
            )
            await s.execute(stmt)

    async def feedback_stats(self, tg_id: int | None = None) -> dict:
        async with self._sf() as s:
            q = select(DealFeedback.reaction, func.count()).group_by(DealFeedback.reaction)
            if tg_id is not None:
                q = q.where(DealFeedback.tg_id == tg_id)
            rows = (await s.execute(q)).all()
            stats = {"up": 0, "down": 0}
            for reaction, count in rows:
                if reaction in stats:
                    stats[reaction] = count
            return {"up": stats["up"], "down": stats["down"], "total": stats["up"] + stats["down"]}

    async def feedback_reason_stats(self, tg_id: int) -> dict[str, int]:
        async with self._sf() as s:
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


class RepositoryContainer:
    """Convenience container to hold all repositories."""
    def __init__(self, session_factory: Callable[[], AsyncContextManager[AsyncSession]]):
        self.users = UserRepository(session_factory)
        self.subscriptions = SubscriptionRepository(session_factory)
        self.listings = ListingRepository(session_factory)
        self.alerts = AlertRepository(session_factory)
        self.images = ImageRepository(session_factory)
        self.maintenance = MaintenanceRepository(session_factory)
        self.feedback = FeedbackRepository(session_factory)
