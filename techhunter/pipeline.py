"""Per-listing pipeline: cheap gate -> enrich -> evaluate -> value.

Speed strategy:
  1. Reject non-target titles BEFORE opening the detail page (no network,
     no CLIP for accessories / cases / glass / chargers).
  2. Text-only evaluate + value first (fast, no image download / CLIP).
  3. Run the heavy CLIP + photo-reuse pass ONLY for lots that already look
     like an opportunity, to refine the scam verdict.
"""
import logging

from . import config
from .ai.evaluate import EvaluationReport, evaluate_listing
from .ai.normalize import normalize_device
from .scraper.models import ParsedListing
from .valuation.engine import Valuation, value_listing

log = logging.getLogger(__name__)


async def process_new_listing(
    browser, item: ParsedListing, mode: str = "fast"
) -> tuple[EvaluationReport | None, Valuation | None]:
    # Cheap gate: skip anything whose title is not a recognised device
    # (accessories, glass, cases, chargers, unrelated phones).
    if normalize_device(item.title).model is None:
        return None, None

    try:
        item = await browser.fetch_details(item, mode=mode)
    except Exception as e:
        log.debug("fetch_details failed for %s: %s", item.id, e)

    report = await evaluate_listing(item, run_clip=False, do_dedup=False)
    valuation = await value_listing(item, report, log_obs=False)

    # Heavy visual + photo-reuse check only for promising lots.
    if valuation.opportunity and config.CLIP_ENABLED:
        try:
            report = await evaluate_listing(item, run_clip=True, do_dedup=True)
            valuation = await value_listing(item, report, log_obs=True)
        except Exception as e:
            log.debug("CLIP refine failed for %s: %s", item.id, e)
            valuation = await value_listing(item, report, log_obs=True)
    else:
        valuation = await value_listing(item, report, log_obs=True)

    return report, valuation
