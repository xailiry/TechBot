from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    paused: Mapped[bool] = mapped_column(Integer, default=0)
    # Per-user delivery filters (M1).
    # Minimum scam/trust score to deliver (0 = off).
    min_score: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # CSV of Condition values NEVER to deliver, e.g. "for_parts,broken".
    exclude_conditions: Mapped[str] = mapped_column(
        String(128), default="", server_default=""
    )
    # 1 = never deliver listings from shop/company sellers.
    exclude_shop: Mapped[bool] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # Discovery Mode: broad scan of the whole category (e.g. all of Russia).
    discovery_enabled: Mapped[bool] = mapped_column(
        Integer, default=0, server_default="0"
    )
    discovery_min_profit_rub: Mapped[int] = mapped_column(
        Integer, default=7000, server_default="7000"
    )
    discovery_min_profit_ratio: Mapped[float] = mapped_column(
        Float, default=0.20, server_default="0.2"
    )
    discovery_city_slug: Mapped[str] = mapped_column(
        String(64), default="rossiya", server_default="'rossiya'"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(
        ForeignKey("users.tg_id", ondelete="CASCADE"), index=True
    )
    query: Mapped[str] = mapped_column(String(256))
    city_slug: Mapped[str] = mapped_column(String(64), default="rossiya")
    min_price: Mapped[int | None] = mapped_column(Integer)
    max_price: Mapped[int | None] = mapped_column(Integer)
    # Electronics-specific filter: minimum acceptable battery health %.
    min_battery: Mapped[int | None] = mapped_column(Integer)
    search_pages: Mapped[int | None] = mapped_column(Integer)
    # When the one-time deep market analysis finished. Persisted so a bot
    # restart does NOT re-run onboarding. Also drives periodic refresh.
    onboarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="subscriptions")


class DeviceCatalog(Base):
    """Canonical device identity (brand + model + storage)."""

    __tablename__ = "device_catalog"
    __table_args__ = (
        UniqueConstraint("brand", "model", "storage_gb", name="uq_device_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    storage_gb: Mapped[int | None] = mapped_column(Integer)
    ram_gb: Mapped[int | None] = mapped_column(Integer)
    # CSV of free-text aliases used to normalize raw listing titles.
    aliases: Mapped[str | None] = mapped_column(Text)


class Listing(Base):
    """Every processed marketplace listing, for deduplication."""

    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="avito")
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_catalog.id", ondelete="SET NULL")
    )
    last_price: Mapped[int | None] = mapped_column(Integer)
    condition: Mapped[str | None] = mapped_column(String(32))
    # Set ONLY after the pipeline succeeded. NULL = seen but not yet
    # processed (transient failure) -> retried next cycle, never lost.
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Cached evaluation so we do not re-run the heavy pipeline every poll.
    report_json: Mapped[str | None] = mapped_column(Text)
    valuation_json: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )


class PriceObservation(Base):
    """Raw observed prices feeding the auto-learned market baseline."""

    __tablename__ = "price_observations"
    __table_args__ = (
        Index("ix_priceobs_device_cond", "device_id", "condition"),
        Index(
            "ux_priceobs_source_listing_device_condition_price",
            "source", "listing_id", "device_id", "condition", "price",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_catalog.id", ondelete="SET NULL"), index=True
    )
    listing_id: Mapped[str | None] = mapped_column(String(64))
    raw_title: Mapped[str | None] = mapped_column(Text)
    storage_gb: Mapped[int | None] = mapped_column(Integer)
    condition: Mapped[str] = mapped_column(String(32), default="working")
    price: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), default="avito", index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class MarketBaseline(Base):
    """Learned reference price per (device, condition). Never fabricated."""

    __tablename__ = "market_baselines"
    __table_args__ = (
        UniqueConstraint("device_id", "condition", name="uq_baseline_device_cond"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("device_catalog.id", ondelete="CASCADE"), index=True
    )
    condition: Mapped[str] = mapped_column(String(32), default="working")
    median_price: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )


class RepairCost(Base):
    """Real repair costs (user-provided) for post-repair margin on broken lots."""

    __tablename__ = "repair_costs"
    __table_args__ = (
        UniqueConstraint(
            "brand", "model_pattern", "defect_type", name="uq_repair_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand: Mapped[str] = mapped_column(String(32))
    # Prefix match against canonical model, e.g. "iPhone 13".
    model_pattern: Mapped[str] = mapped_column(String(64))
    defect_type: Mapped[str] = mapped_column(String(32))  # screen / battery / back_glass
    cost_rub: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )


class SentAlert(Base):
    """Delivery log + dedup: what was sent to whom."""

    __tablename__ = "sent_alerts"
    __table_args__ = (
        Index("ix_sentalert_sent_at", "sent_at"),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    price: Mapped[int | None] = mapped_column(Integer)
    profit: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str | None] = mapped_column(String(32))
    condition: Mapped[str | None] = mapped_column(String(32))
    sub_query: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PendingAlert(Base):
    """Outbox for profitable deals that could not be delivered to Telegram."""

    __tablename__ = "pending_alerts"
    __table_args__ = (
        Index("ix_pendingalert_created_at", "created_at"),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_json: Mapped[str] = mapped_column(Text)
    report_json: Mapped[str] = mapped_column(Text)
    valuation_json: Mapped[str] = mapped_column(Text)
    sub_query: Mapped[str | None] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ImageHash(Base):
    """dhash of listing photos, to detect the same photo reused across
    sellers (a scam signal consumed in Stage 3)."""

    __tablename__ = "image_hashes"
    __table_args__ = (
        Index("ix_imagehash_hash", "img_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(String(64), index=True)
    img_hash: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CardState(Base):
    """Persisted card payload so Telegram callbacks survive bot restarts."""

    __tablename__ = "card_state"
    __table_args__ = (
        Index("ix_cardstate_created", "created_at"),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_json: Mapped[str] = mapped_column(Text)
    score_json: Mapped[str] = mapped_column(Text)
    sub_query: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DealFeedback(Base):
    """User reaction to a delivered deal (feedback loop for calibration)."""

    __tablename__ = "deal_feedback"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reaction: Mapped[str] = mapped_column(String(8))  # up | down
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow
    )
