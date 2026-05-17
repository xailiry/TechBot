"""Stage 2 orchestrator: ParsedListing -> EvaluationReport.

Combines regex specs, model normalization, condition grading, optional CLIP
visual prefilter and cross-seller photo-reuse detection. Stage 3 consumes
this report for valuation and the broken-lot opportunity logic.
"""
import logging

from pydantic import BaseModel, Field

from .. import config
from ..scraper.models import ParsedListing
from .clip_engine import get_clip_engine, interpret_visual
from .condition import Condition, grade_condition
from .images import dhash_bytes, download_image_bytes
from .normalize import normalize_device
from .specs import extract_specs

log = logging.getLogger(__name__)


class EvaluationReport(BaseModel):
    listing_id: str
    brand: str
    model: str | None
    storage_gb: int | None = None
    ram_gb: int | None = None
    color: str | None = None

    condition: str  # Condition value
    battery_health: int | None = None
    defects: list[str] = Field(default_factory=list)
    is_rostest: bool = False
    is_sealed: bool = False

    # CLIP visual flags (None == unknown / engine unavailable).
    visual: dict = Field(default_factory=dict)
    # Number of other listings reusing this listing's primary photo.
    reused_image_count: int = 0
    primary_image_hash: str | None = None


async def evaluate_listing(
    item: ParsedListing,
    *,
    run_clip: bool = True,
    do_dedup: bool = True,
) -> EvaluationReport:
    text = f"{item.title} {item.description}"
    specs = extract_specs(item.title, item.description, item.params)
    device = normalize_device(
        item.title,
        storage_gb=specs.storage_gb,
        ram_gb=specs.ram_gb,
        params=item.params,
    )
    condition = grade_condition(specs, text)

    report = EvaluationReport(
        listing_id=item.id,
        brand=device.brand,
        model=device.model,
        storage_gb=device.storage_gb,
        ram_gb=device.ram_gb,
        color=specs.color,
        condition=condition.value,
        battery_health=specs.battery_health,
        defects=sorted(specs.defects),
        is_rostest=specs.is_rostest,
        is_sealed=specs.is_sealed,
    )

    images = item.images or ([item.image] if item.image else [])
    primary_bytes: bytes | None = None
    if images:
        primary_bytes = await download_image_bytes(images[0])

    if run_clip and config.CLIP_ENABLED and images:
        engine = get_clip_engine()
        if engine.available:
            agg: dict[str, dict[str, float]] = {}
            n = 0
            for idx, url in enumerate(images[: config.MAX_IMAGES_FOR_CLIP]):
                data = primary_bytes if idx == 0 else await download_image_bytes(url)
                if not data:
                    continue
                groups = engine.classify(data)
                if not groups:
                    continue
                n += 1
                for g, probs in groups.items():
                    bucket = agg.setdefault(g, {})
                    for lbl, p in probs.items():
                        # mean for subject/origin; max for "screen cracked".
                        if g == "screen":
                            bucket[lbl] = max(bucket.get(lbl, 0.0), p)
                        else:
                            bucket[lbl] = bucket.get(lbl, 0.0) + p
            if n:
                for g, bucket in agg.items():
                    if g != "screen":
                        for lbl in bucket:
                            bucket[lbl] /= n
                report.visual = interpret_visual(agg)

    if do_dedup and primary_bytes:
        h = dhash_bytes(primary_bytes)
        if h:
            from ..storage import count_reused_images, record_image_hash

            report.primary_image_hash = h
            report.reused_image_count = await count_reused_images(item.id, h)
            await record_image_hash(item.id, h)

    return report
