"""M2 verification: listing cache (no lost lots on failure) + per-user
delivery dedup. No network."""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys
import uuid

from techhunter.ai.evaluate import EvaluationReport
from techhunter.scraper.models import ParsedListing
from techhunter.storage import (
    alert_already_sent,
    cache_listing,
    cache_listing_skipped,
    get_cached_listing,
    mark_alert_sent,
    record_feedback,
)
from techhunter.valuation.engine import Valuation


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def _item(lid, price=40000):
    return ParsedListing(id=lid, title="iPhone 13 Pro 128 ГБ", price=price,
                          url="/x/1", description="")


def _rep(lid):
    return EvaluationReport(listing_id=lid, brand="apple",
                            model="iPhone 13 Pro", storage_gb=128,
                            condition="good", defects=[])


def _val(lid):
    return Valuation(listing_id=lid, condition="good", baseline_price=60000,
                     net_profit=15000, profit_pct=0.25, opportunity=True,
                     opportunity_type="working", scam_score=70,
                     scam_verdict="unknown")


async def test_cache_and_retry() -> None:
    lid = f"rel-{uuid.uuid4().hex}"

    # Never processed -> None (so the monitor will (re)process it; a
    # transient failure that never cached is therefore NOT lost).
    check("unseen -> None (retry)", await get_cached_listing(lid) is None)

    await cache_listing(_item(lid, 40000), _rep(lid), _val(lid))
    crow = await get_cached_listing(lid)
    check("cached after success", crow is not None)
    check("cached price", crow["price"] == 40000)
    check("report reconstructable",
          EvaluationReport(**crow["report"]).model == "iPhone 13 Pro")
    check("valuation reconstructable",
          Valuation(**crow["valuation"]).opportunity is True)

    # Price change must invalidate reuse (monitor compares prices).
    check("price-change detectable", crow["price"] != 41000)

    # Non-target listing: processed, but empty payload (won't be
    # re-pipelined every poll, won't be delivered).
    lid2 = f"rel-{uuid.uuid4().hex}"
    await cache_listing_skipped(_item(lid2))
    c2 = await get_cached_listing(lid2)
    check("skipped marked processed", c2 is not None)
    check("skipped has no payload",
          c2["report"] is None and c2["valuation"] is None)


async def test_content_hash_card_stable() -> None:
    lid = f"rel-{uuid.uuid4().hex}"
    card = _item(lid, 40000)
    detailed = card.model_copy()
    detailed.description = "Полное описание продавца"
    detailed.price = 39000
    check("content hash ignores details/price",
          card.get_content_hash() == detailed.get_content_hash())


async def test_per_user_dedup() -> None:
    lid = f"rel-{uuid.uuid4().hex}"
    a, b = 700001, 700002
    check("A not sent", not await alert_already_sent(a, lid))
    check("B not sent", not await alert_already_sent(b, lid))
    await mark_alert_sent(a, lid, price=40000, verdict="unknown")
    check("A sent now", await alert_already_sent(a, lid))
    # Another user must still receive it independently (global Listing
    # cache must NOT consume the lot for other users).
    check("B still independent", not await alert_already_sent(b, lid))
    await mark_alert_sent(a, lid)  # idempotent, no error


async def test_delivery_failure_not_marked_sent() -> None:
    from techhunter import monitor

    lid = f"rel-{uuid.uuid4().hex}"
    tg = 710000 + (uuid.uuid4().int % 1000)
    item = _item(lid)
    rep = _rep(lid)
    val = _val(lid)

    class Target:
        tg_id = tg
        query = "iphone"

    class FailingNotifier:
        async def deal(self, *args, **kwargs):
            raise RuntimeError("telegram down")

    async def fake_evaluate(*args, **kwargs):
        return rep, val

    orig = monitor._evaluate
    monitor._evaluate = fake_evaluate  # type: ignore
    try:
        counters = {"errors": 0, "processed": 0, "cached": 0,
                    "deals": 0, "filtered": 0}
        await monitor._handle_items(
            object(), [item], Target(), FailingNotifier(),
            asyncio.Semaphore(1), {}, counters, {}, mode="fast",
        )
    finally:
        monitor._evaluate = orig  # type: ignore

    check("delivery failure counted", counters["errors"] == 1)
    check("delivery failure not marked sent",
          not await alert_already_sent(tg, lid))


async def test_feedback_threshold_filters_noisy_user() -> None:
    from techhunter import monitor

    lid = f"rel-{uuid.uuid4().hex}"
    tg = 711000 + (uuid.uuid4().int % 1000)
    item = _item(lid)
    rep = _rep(lid)
    val = Valuation(
        listing_id=lid, condition="good", baseline_price=60000,
        net_profit=3500, profit_pct=0.20, opportunity=True,
        opportunity_type="working", scam_score=70, scam_verdict="unknown",
    )
    for i in range(5):
        await record_feedback(tg, f"old-miss-{i}-{uuid.uuid4().hex}", "down")

    class Target:
        tg_id = tg
        query = "iphone"

    class Notifier:
        calls = 0

        async def deal(self, *args, **kwargs):
            self.calls += 1

    async def fake_evaluate(*args, **kwargs):
        return rep, val

    orig = monitor._evaluate
    monitor._evaluate = fake_evaluate  # type: ignore
    notifier = Notifier()
    try:
        counters = {"errors": 0, "processed": 0, "cached": 0,
                    "deals": 0, "filtered": 0}
        drops = {}
        await monitor._handle_items(
            object(), [item], Target(), notifier,
            asyncio.Semaphore(1), {}, counters, drops, mode="fast",
        )
    finally:
        monitor._evaluate = orig  # type: ignore

    check("feedback threshold filtered", notifier.calls == 0
          and counters["filtered"] == 1)
    check("feedback drop reason", drops.get("feedback_threshold") == 1)


async def test_sqlite_pragmas() -> None:
    from sqlalchemy import text

    from techhunter.db import get_session

    async with get_session() as s:
        jm = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        bt = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
    check("WAL enabled", str(jm).lower() == "wal")
    check("busy_timeout set", int(bt) >= 5000)


async def test_image_hash_dedup_and_reuse() -> None:
    from techhunter.storage import count_reused_images, record_image_hash

    a = f"ih-{uuid.uuid4().hex}"
    h = f"{uuid.uuid4().int & ((1 << 64) - 1):016x}"
    await record_image_hash(a, h)
    await record_image_hash(a, "ffffffffffffffff")  # same listing -> skipped

    from sqlalchemy import func, select

    from techhunter.db import get_session
    from techhunter.db.models import ImageHash

    async with get_session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(ImageHash)
                .where(ImageHash.listing_id == a)
            )
        ).scalar()
    check("one hash row per listing", n == 1)

    b = f"ih-{uuid.uuid4().hex}"
    check("exact reuse detected (other listing)",
          await count_reused_images(b, h) == 1)
    check("self not counted", await count_reused_images(a, h) == 0)
    check("empty hash -> 0", await count_reused_images(b, "") == 0)


async def test_cleanup_old_rows() -> None:
    from datetime import datetime, timedelta, timezone

    from techhunter.db import get_session
    from techhunter.db.models import CardState, ImageHash, SentAlert
    from techhunter.storage import cleanup_old_rows

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=400
    )
    tag = uuid.uuid4().hex[:8]
    async with get_session() as s:
        s.add(ImageHash(listing_id=f"old-{tag}", img_hash="0" * 16,
                        created_at=old))
        s.add(ImageHash(listing_id=f"new-{tag}", img_hash="1" * 16))
        s.add(CardState(tg_id=10**9 + 1, message_id=int(tag, 16) % 100000,
                        item_json="{}", score_json="{}", created_at=old))
        s.add(SentAlert(tg_id=10**9 + 1, listing_id=f"sa-old-{tag}",
                        sent_at=old))

    deleted = await cleanup_old_rows()
    check("cleanup returns per-table counts",
          "image_hashes" in deleted and "sent_alerts" in deleted)

    from sqlalchemy import func, select

    async with get_session() as s:
        old_ih = (
            await s.execute(
                select(func.count()).select_from(ImageHash)
                .where(ImageHash.listing_id == f"old-{tag}")
            )
        ).scalar()
        new_ih = (
            await s.execute(
                select(func.count()).select_from(ImageHash)
                .where(ImageHash.listing_id == f"new-{tag}")
            )
        ).scalar()
    check("old image hash pruned", old_ih == 0)
    check("fresh image hash kept", new_ih == 1)


def main() -> None:
    asyncio.run(test_cache_and_retry())
    asyncio.run(test_content_hash_card_stable())
    asyncio.run(test_per_user_dedup())
    asyncio.run(test_delivery_failure_not_marked_sent())
    asyncio.run(test_feedback_threshold_filters_noisy_user())
    asyncio.run(test_sqlite_pragmas())
    asyncio.run(test_image_hash_dedup_and_reuse())
    asyncio.run(test_cleanup_old_rows())
    print("\nAll reliability checks passed.")


if __name__ == "__main__":
    main()
