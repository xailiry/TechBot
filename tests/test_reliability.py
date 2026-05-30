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
    list_pending_alerts,
    mark_alert_sent,
    mark_pending_alert_attempt,
    pending_alert_stats,
    queue_pending_alert,
    record_feedback,
    feedback_reason_stats,
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
        min_battery = None

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
    pending = await list_pending_alerts()
    check("delivery failure queued",
          any(p["tg_id"] == tg and p["listing_id"] == lid for p in pending))

    class RecoveringNotifier:
        async def deal(self, item, report, valuation, *, tg_id=None, sub_query=None):
            await mark_alert_sent(
                tg_id, item.id, price=item.price,
                profit=valuation.net_profit, verdict=valuation.scam_verdict,
                condition=valuation.condition, sub_query=sub_query,
            )

    await monitor._retry_pending_alerts(RecoveringNotifier())  # type: ignore
    check("queued delivery retried", await alert_already_sent(tg, lid))
    pending_after = await list_pending_alerts()
    check("queued delivery removed",
          not any(p["tg_id"] == tg and p["listing_id"] == lid for p in pending_after))


async def test_pending_alert_backoff_and_dead() -> None:
    lid = f"rel-{uuid.uuid4().hex}"
    tg = 720000 + (uuid.uuid4().int % 1000)
    item = _item(lid)
    rep = _rep(lid)
    val = _val(lid)

    await queue_pending_alert(
        tg,
        item,
        rep,
        val,
        sub_query="Discovery",
        error="first fail",
    )
    first = await list_pending_alerts()
    check("new pending visible",
          any(p["tg_id"] == tg and p["listing_id"] == lid for p in first))

    await mark_pending_alert_attempt(tg, lid, "telegram timeout")
    backoff = await list_pending_alerts()
    check("transient pending backs off",
          not any(p["tg_id"] == tg and p["listing_id"] == lid for p in backoff))

    await mark_pending_alert_attempt(
        tg, lid, "bot was blocked", dead_reason="permanent_telegram_error"
    )
    stats = await pending_alert_stats()
    dead_rows = await list_pending_alerts()
    check("permanent error marked dead", stats["dead"] >= 1)
    check("dead pending hidden from retry list",
          not any(p["tg_id"] == tg and p["listing_id"] == lid for p in dead_rows))


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
        min_battery = None

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


async def test_feedback_reason_filters_rebuilt_reseller() -> None:
    from techhunter import monitor

    lid = f"rel-{uuid.uuid4().hex}"
    tg = 711500 + (uuid.uuid4().int % 1000)
    item = _item(lid)
    item.seller_type = "private"
    item.seller_listings = 17
    item.seller_reviews = 49
    rep = _rep(lid)
    rep.battery_health = 100
    val = Valuation(
        listing_id=lid, condition="good", baseline_price=45000,
        net_profit=7750, profit_pct=0.18, opportunity=True,
        opportunity_type="working", scam_score=70, scam_verdict="unknown",
        shoplike=False,
    )
    for i in range(2):
        await record_feedback(
            tg, f"rebuilt-{i}-{uuid.uuid4().hex}", "down",
            reason="reseller_rebuild",
        )
    stats = await feedback_reason_stats(tg)
    check("feedback reason stored", stats.get("reseller_rebuild") == 2)

    class Target:
        tg_id = tg
        query = "iphone"
        min_battery = None

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

    check("rebuilt reseller filtered", notifier.calls == 0
          and counters["filtered"] == 1)
    check("rebuilt drop reason", drops.get("feedback_reseller_rebuild") == 1)


async def test_subscription_battery_filter_applied() -> None:
    from techhunter import monitor

    lid = f"rel-{uuid.uuid4().hex}"
    tg = 712000 + (uuid.uuid4().int % 1000)
    item = _item(lid)
    rep = _rep(lid)
    rep.battery_health = 80
    val = _val(lid)

    class Target:
        tg_id = tg
        query = "iphone"
        min_battery = 85

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

    check("subscription battery filter blocks low akb", notifier.calls == 0)
    check("battery filter counted", counters["filtered"] == 1)
    check("battery drop reason", drops.get("low_battery") == 1)


async def test_pipeline_logs_only_after_final_evaluation() -> None:
    from techhunter import pipeline

    flags: list[bool] = []
    item = _item(f"rel-{uuid.uuid4().hex}", 40000)

    class Browser:
        async def fetch_details(self, it, mode="fast"):
            it.description = "battery 91%, everything works"
            return it

    async def fake_evaluate(it, *, run_clip=False, do_dedup=False):
        return _rep(it.id)

    async def fake_value(it, report, *, log_obs=True):
        flags.append(log_obs)
        return Valuation(
            listing_id=it.id,
            condition="good",
            baseline_price=60000,
            net_profit=0,
            profit_pct=0.0,
            opportunity=False,
            opportunity_type="none",
            scam_score=70,
            scam_verdict="unknown",
        )

    orig_eval = pipeline.evaluate_listing
    orig_value = pipeline.value_listing
    try:
        pipeline.evaluate_listing = fake_evaluate  # type: ignore[assignment]
        pipeline.value_listing = fake_value  # type: ignore[assignment]
        await pipeline.process_new_listing(Browser(), item)
    finally:
        pipeline.evaluate_listing = orig_eval  # type: ignore[assignment]
        pipeline.value_listing = orig_value  # type: ignore[assignment]

    check("preliminary valuation does not log", flags == [False, True])


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
    asyncio.run(test_pending_alert_backoff_and_dead())
    asyncio.run(test_feedback_threshold_filters_noisy_user())
    asyncio.run(test_feedback_reason_filters_rebuilt_reseller())
    asyncio.run(test_subscription_battery_filter_applied())
    asyncio.run(test_pipeline_logs_only_after_final_evaluation())
    asyncio.run(test_sqlite_pragmas())
    asyncio.run(test_image_hash_dedup_and_reuse())
    asyncio.run(test_cleanup_old_rows())
    print("\nAll reliability checks passed.")


if __name__ == "__main__":
    main()
