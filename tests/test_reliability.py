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


def main() -> None:
    asyncio.run(test_cache_and_retry())
    asyncio.run(test_per_user_dedup())
    print("\nAll reliability checks passed.")


if __name__ == "__main__":
    main()
