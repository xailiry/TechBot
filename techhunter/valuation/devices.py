"""Device catalog + continuous, self-learned per-condition baselines.

Prices come ONLY from real observed listings. Nothing is fabricated and
nothing is locked: the longer a subscription lives, the more observations
accumulate and the more accurate the medians become. Per condition tier
(ideal / good / defect / broken / for_parts) a separate median is learned.
"""
import logging
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..db import get_session
from ..db.models import DeviceCatalog, MarketBaseline, PriceObservation
from .clustering import robust_median

log = logging.getLogger(__name__)
_relearn_locks: dict[int, asyncio.Lock] = {}


def _relearn_lock(device_id: int) -> asyncio.Lock:
    lock = _relearn_locks.get(device_id)
    if lock is None:
        lock = asyncio.Lock()
        _relearn_locks[device_id] = lock
    return lock


def _age_seconds(dt: datetime) -> float:
    """Seconds since dt. SQLite returns naive datetimes that are actually
    UTC; .timestamp() would treat them as local time and skew the age
    (even negative under +TZ), so we pin tzinfo to UTC first."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


WORKING_GRADES = ("ideal", "good")
WORKING_CONDITION = "working"  # synthetic tier = ideal+good resale value
ALL_CONDITIONS = ("ideal", "good", "defect", "broken", "for_parts")


async def get_or_create_device(
    brand: str,
    model: str,
    storage_gb: int | None = None,
    ram_gb: int | None = None,
) -> int | None:
    if not model or brand == "unknown":
        return None
    async with get_session() as s:
        stmt = select(DeviceCatalog).where(
            DeviceCatalog.brand == brand, DeviceCatalog.model == model
        )
        stmt = stmt.where(
            DeviceCatalog.storage_gb.is_(None)
            if storage_gb is None
            else DeviceCatalog.storage_gb == storage_gb
        )
        existing = (await s.execute(stmt)).scalar_one_or_none()
        if existing:
            return existing.id
        dev = DeviceCatalog(
            brand=brand, model=model, storage_gb=storage_gb, ram_gb=ram_gb
        )
        s.add(dev)
        await s.flush()
        return dev.id


async def log_observation(
    device_id: int,
    condition: str,
    price: int,
    listing_id: str | None = None,
    raw_title: str | None = None,
    storage_gb: int | None = None,
    source: str = "avito",
) -> bool:
    """Record a real price point. Only absolute placeholder/typo prices are
    rejected here; genuine cheap broken lots are kept (in their own tier)."""
    if not (config.PRICE_ABS_FLOOR <= price <= config.PRICE_ABS_CEIL):
        return False
    async with get_session() as s:
        if listing_id:
            exists = await s.execute(
                select(PriceObservation.id)
                .where(
                    PriceObservation.source == source,
                    PriceObservation.listing_id == listing_id,
                    PriceObservation.device_id == device_id,
                    PriceObservation.condition == condition,
                )
                .limit(1)
            )
            if exists.first() is not None:
                return False
        s.add(
            PriceObservation(
                device_id=device_id,
                listing_id=listing_id,
                raw_title=raw_title,
                storage_gb=storage_gb,
                condition=condition,
                price=price,
                source=source,
            )
        )
    return True


async def _prices(device_id: int, conditions, session: AsyncSession | None = None) -> list[int]:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=config.BASELINE_LOOKBACK_DAYS
    )
    stmt = (
        select(PriceObservation.price)
        .where(
            PriceObservation.device_id == device_id,
            PriceObservation.condition.in_(tuple(conditions)),
            PriceObservation.price >= config.PRICE_ABS_FLOOR,
            PriceObservation.observed_at >= cutoff,
        )
        .order_by(PriceObservation.observed_at.desc())
        .limit(config.BASELINE_MAX_SAMPLE)
    )
    if session is not None:
        rows = await session.execute(stmt)
        return [r[0] for r in rows.all()]
    async with get_session() as s:
        rows = await s.execute(stmt)
        return [r[0] for r in rows.all()]


async def _upsert_baseline(
    device_id: int, condition: str, median: int, sample: int, session: AsyncSession | None = None
) -> None:
    stmt = (
        sqlite_insert(MarketBaseline)
        .values(
            device_id=device_id,
            condition=condition,
            median_price=median,
            sample_size=sample,
        )
        .on_conflict_do_update(
            index_elements=[
                MarketBaseline.device_id, MarketBaseline.condition
            ],
            set_={"median_price": median, "sample_size": sample},
        )
    )
    if session is not None:
        await session.execute(stmt)
    else:
        async with get_session() as s:
            await s.execute(stmt)


# Sane band for each tier relative to the (reliable) working baseline.
# A tier whose learned median falls outside its band is treated as noise
# (weak text grading on a small sample) and is dropped, so the card never
# shows nonsense like "ideal < good".
_TIER_BAND = {
    "ideal": (0.85, 1.60),
    "good": (0.70, 1.30),
    "defect": (0.40, 1.05),
    "broken": (0.20, 0.90),
    "for_parts": (0.10, 0.70),
}


async def _delete_baseline(device_id: int, condition: str, session: AsyncSession | None = None) -> None:
    stmt = select(MarketBaseline).where(
        MarketBaseline.device_id == device_id,
        MarketBaseline.condition == condition,
    )
    if session is not None:
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
    else:
        async with get_session() as s:
            row = (await s.execute(stmt)).scalar_one_or_none()
            if row is not None:
                await s.delete(row)


async def relearn(device_id: int, session: AsyncSession | None = None) -> int | None:
    """Recompute the working baseline plus every per-condition tier.

    The working baseline is the trustworthy number (most data, centered on
    the used bulk). Per-condition tiers are only kept when they have enough
    data AND sit in a sane band around working; otherwise the stale/illogical
    tier is deleted. We never guess."""
    async with _relearn_lock(device_id):
        if session is not None:
            return await _relearn_with_session(device_id, session)
        async with get_session() as s:
            res = await _relearn_with_session(device_id, s)
            await s.commit()
            return res


async def _relearn_with_session(device_id: int, session: AsyncSession) -> int | None:
    working_prices = await _prices(device_id, WORKING_GRADES, session=session)
    working_median: int | None = None
    if len(working_prices) >= config.BASELINE_MIN_SAMPLE:
        working_median = robust_median(working_prices, drop_low_cluster=True)
    if working_median:
        await _upsert_baseline(
            device_id, WORKING_CONDITION, working_median,
            len(working_prices),
            session=session,
        )
    else:
        # No reliable anchor -> drop everything; better silent than wrong.
        for cond in (WORKING_CONDITION, *ALL_CONDITIONS):
            await _delete_baseline(device_id, cond, session=session)
        return None

    for cond in ALL_CONDITIONS:
        pts = await _prices(device_id, (cond,), session=session)
        med = (
            robust_median(pts, drop_low_cluster=cond in WORKING_GRADES)
            if len(pts) >= config.BASELINE_MIN_SAMPLE_COND
            else None
        )
        lo, hi = _TIER_BAND[cond]
        if med and lo * working_median <= med <= hi * working_median:
            await _upsert_baseline(device_id, cond, med, len(pts), session=session)
        else:
            await _delete_baseline(device_id, cond, session=session)

    return working_median


async def _row(device_id: int, condition: str) -> MarketBaseline | None:
    async with get_session() as s:
        return (
            await s.execute(
                select(MarketBaseline).where(
                    MarketBaseline.device_id == device_id,
                    MarketBaseline.condition == condition,
                )
            )
        ).scalar_one_or_none()


async def get_baseline(device_id: int) -> int | None:
    """Working resale baseline. Continuously re-learned (never frozen);
    None until enough real data exists."""
    row = await _row(device_id, WORKING_CONDITION)
    if row is None:
        return await relearn(device_id)
    age = _age_seconds(row.updated_at)
    if age >= config.BASELINE_REFRESH_SEC:
        relearned = await relearn(device_id)
        return relearned if relearned is not None else row.median_price
    return row.median_price


async def get_working_meta(device_id: int) -> tuple[int | None, int, float]:
    """(price, sample_size, age_seconds) for the working baseline, after
    ensuring it is fresh. Powers the confidence label on the card."""
    price = await get_baseline(device_id)
    row = await _row(device_id, WORKING_CONDITION)
    if row is None:
        return price, 0, 0.0
    return price, int(row.sample_size or 0), _age_seconds(row.updated_at)


async def get_model_working_meta(
    brand: str, model: str
) -> tuple[int | None, int, int]:
    """Aggregate working baseline for card triage when storage is absent.

    This is deliberately computed, not stored as a fake storage-less device:
    exact storage baselines remain the source of truth for final valuation.
    Returns (median_of_variant_medians, total_sample, variant_count).
    """
    async with get_session() as s:
        rows = (
            await s.execute(
                select(
                    MarketBaseline.median_price,
                    MarketBaseline.sample_size,
                )
                .join(DeviceCatalog, DeviceCatalog.id == MarketBaseline.device_id)
                .where(
                    DeviceCatalog.brand == brand,
                    DeviceCatalog.model == model,
                    DeviceCatalog.storage_gb.is_not(None),
                    MarketBaseline.condition == WORKING_CONDITION,
                    MarketBaseline.sample_size >= config.BASELINE_MIN_SAMPLE,
                )
            )
        ).all()
    if not rows:
        return None, 0, 0
    prices = [int(price) for price, _sample in rows]
    sample = sum(int(sample or 0) for _price, sample in rows)
    return robust_median(prices, drop_low_cluster=False), sample, len(rows)


async def get_condition_baselines(device_id: int) -> dict[str, int]:
    """All learned tier medians for display: {'good': X, 'broken': Y, ...}."""
    async with get_session() as s:
        rows = await s.execute(
            select(MarketBaseline.condition, MarketBaseline.median_price)
            .where(MarketBaseline.device_id == device_id)
        )
        return {c: m for c, m in rows.all()}


async def prices_for_model(
    brand: str, model: str
) -> list[dict]:
    """All catalog variants of brand+model with their learned tiers, for
    the per-subscription "Цены по состояниям" screen."""
    async with get_session() as s:
        devs = (
            await s.execute(
                select(DeviceCatalog)
                .where(
                    DeviceCatalog.brand == brand,
                    DeviceCatalog.model == model,
                )
                .order_by(DeviceCatalog.storage_gb)
            )
        ).scalars().all()
        out = []
        for d in devs:
            rows = (
                await s.execute(
                    select(
                        MarketBaseline.condition,
                        MarketBaseline.median_price,
                        MarketBaseline.sample_size,
                    ).where(MarketBaseline.device_id == d.id)
                )
            ).all()
            tiers = {c: (p, n) for c, p, n in rows}
            if tiers:
                out.append({
                    "storage_gb": d.storage_gb,
                    "ram_gb": d.ram_gb,
                    "tiers": tiers,
                })
        return out


async def count_working_baselines() -> int:
    """How many devices currently have a usable working baseline. Powers the
    Discovery warm-up progress line so the operator can see it is learning."""
    async with get_session() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(MarketBaseline)
                .where(
                    MarketBaseline.condition == WORKING_CONDITION,
                    MarketBaseline.sample_size >= config.BASELINE_MIN_SAMPLE,
                    DeviceCatalog.storage_gb.is_not(None),
                )
                .join(DeviceCatalog, DeviceCatalog.id == MarketBaseline.device_id)
            )
        ).scalar() or 0


async def learned_devices(limit: int = 12, offset: int = 0) -> list[dict]:
    """Paginated list of every device with a usable working baseline,
    most-confident (largest sample) first. Powers the price-knowledge screen."""
    async with get_session() as s:
        rows = (
            await s.execute(
                select(
                    DeviceCatalog.brand,
                    DeviceCatalog.model,
                    DeviceCatalog.storage_gb,
                    MarketBaseline.median_price,
                    MarketBaseline.sample_size,
                )
                .join(MarketBaseline, MarketBaseline.device_id == DeviceCatalog.id)
                .where(
                    MarketBaseline.condition == WORKING_CONDITION,
                    MarketBaseline.sample_size >= config.BASELINE_MIN_SAMPLE,
                    DeviceCatalog.storage_gb.is_not(None),
                )
                .order_by(MarketBaseline.sample_size.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    out = []
    for b, m, st, price, sample in rows:
        label = m or b
        if st:
            label += f" {st}GB"
        out.append({"label": label, "price": int(price), "sample": int(sample)})
    return out


async def learning_overview(limit: int = 8) -> dict:
    """Coverage stats for the Status screen: how many devices have a learned
    working baseline, with the top ones by sample size."""
    async with get_session() as s:
        total = (
            await s.execute(select(func.count()).select_from(DeviceCatalog))
        ).scalar() or 0
        rows = (
            await s.execute(
                select(
                    DeviceCatalog.brand,
                    DeviceCatalog.model,
                    DeviceCatalog.storage_gb,
                    MarketBaseline.median_price,
                    MarketBaseline.sample_size,
                )
                .join(MarketBaseline,
                      MarketBaseline.device_id == DeviceCatalog.id)
                .where(MarketBaseline.condition == WORKING_CONDITION)
                .where(DeviceCatalog.storage_gb.is_not(None))
                .order_by(MarketBaseline.sample_size.desc())
                .limit(limit)
            )
        ).all()
    top = [
        {
            "label": f"{b} {m}" + (f" {st}GB" if st else ""),
            "price": int(price),
            "sample": int(sample),
        }
        for b, m, st, price, sample in rows
    ]
    return {"devices": int(total), "with_baseline": len(top), "top": top}


async def device_summary(device_id: int) -> tuple[str, dict[str, int]]:
    """(human label, {condition: median}) for onboarding reports."""
    async with get_session() as s:
        dev = await s.get(DeviceCatalog, device_id)
    label = "устройство"
    if dev is not None:
        label = f"{dev.brand} {dev.model}"
        if dev.storage_gb:
            label += f" {dev.storage_gb}GB"
    return label, await get_condition_baselines(device_id)


async def set_manual_baseline(device_id: int, median_price: int) -> None:
    """Optional warm-start seed (e.g. from the calibrate CLI). NOT locked:
    once enough real observations exist, learning refines/overwrites it."""
    await _upsert_baseline(
        device_id, WORKING_CONDITION, median_price,
        config.BASELINE_MIN_SAMPLE,
    )
