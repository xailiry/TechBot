import asyncio
import contextlib
import logging
import random
import time
from collections import deque

from .. import config, runtime
from ..db.repository import RepositoryContainer
from ..ai.evaluate import evaluate_listing
from ..valuation.engine import (
    learn_from_card_device,
    learn_from_detail,
    should_fetch_detail_for_learning,
)
from ..valuation.devices import relearn

log = logging.getLogger(__name__)

class TrainingLoop:
    def __init__(self, repos: RepositoryContainer, browser):
        self.repos = repos
        self.browser = browser

    async def run(self) -> None:
        current_page = 1
        seen_order: deque[str] = deque()
        seen_ids: set[str] = set()

        while True:
            if not runtime.is_training_mode():
                current_page = 1
                seen_order.clear()
                seen_ids.clear()
                await asyncio.sleep(5)
                continue
            
            if self.browser.session.is_paused:
                log.debug("Training poll skipped because browser is paused.")
                runtime.update_cycle(
                    mode="training", status="captcha",
                    subs=0, scraped=0, processed=0, cached=0, errors=0,
                    deals=0, filtered=0, drop_reasons={}
                )
                await asyncio.sleep(5)
                continue
                
            try:
                max_pages = config.TRAINING_MAX_PAGES
                runtime.update_training(active=True, current_page=current_page, max_pages=max_pages, last_error=None)
                log.info("Training Mode: scanning page %d of %d...", current_page, max_pages)
                
                items = await self.browser.search(
                    query="", city_slug="rossiya", min_price=config.TRAINING_MIN_PRICE,
                    pages=1, start_page=current_page, mode="deep"
                )
                
                if not items:
                    log.info("Training Mode: page %d returned no items or got blocked.", current_page)
                    runtime.update_cycle(
                        mode="training", status="empty",
                        subs=0, scraped=0, processed=0, cached=0, errors=0,
                        deals=0, filtered=0, drop_reasons={}
                    )
                    runtime.update_training(current_page=current_page, max_pages=max_pages, card_learned=0, detail_learned=0, skipped=0, devices=0)
                    current_page = 1
                    await asyncio.sleep(15)
                    continue
                
                log.info("Training Mode: found %d items on page %d.", len(items), current_page)
                card_learned = 0
                detail_learned = 0
                skipped = 0
                detail_used = 0
                device_ids: set[int] = set()
                
                for item in items:
                    await asyncio.sleep(0.02)
                    if not runtime.is_training_mode():
                        break

                    if item.id in seen_ids:
                        skipped += 1
                        continue
                    
                    seen_ids.add(item.id)
                    seen_order.append(item.id)
                    while len(seen_order) > config.TRAINING_SEEN_CACHE:
                        old = seen_order.popleft()
                        seen_ids.discard(old)

                    did = await learn_from_card_device(item, source="training")
                    if did is not None:
                        card_learned += 1
                        device_ids.add(did)
                        continue

                    if detail_used >= config.TRAINING_DETAIL_PER_PAGE or not should_fetch_detail_for_learning(item):
                        skipped += 1
                        continue

                    detail_used += 1
                    try:
                        detail_item = await self.browser.fetch_details(item)
                        rep = await evaluate_listing(detail_item, run_clip=False, do_dedup=False)
                        did = await learn_from_detail(detail_item, rep, source="training_detail")
                    except Exception as e:
                        log.debug("Training detail learn failed for %s: %s", item.id, e)
                        did = None
                        
                    if did is not None:
                        detail_learned += 1
                        device_ids.add(did)
                    else:
                        skipped += 1

                for did in device_ids:
                    with contextlib.suppress(Exception):
                        await relearn(did)
                
                processed = card_learned + detail_learned
                log.info("Training Mode: page %d finished. card=%d detail=%d skipped=%d devices=%d.", current_page, card_learned, detail_learned, skipped, len(device_ids))
                
                runtime.update_cycle(
                    mode="training", status="success", subs=0, scraped=len(items),
                    processed=processed, cached=0, errors=0, deals=0, filtered=skipped,
                    drop_reasons={"training_detail": detail_learned, "training_skipped": skipped}
                )
                runtime.update_training(
                    current_page=current_page, max_pages=max_pages, card_learned=card_learned,
                    detail_learned=detail_learned, skipped=skipped, devices=len(device_ids),
                    last_success_at=time.time(), last_error=None
                )
                
                current_page += 1
                if current_page > max_pages:
                    log.info("Training Mode: reached max page %d. Wrapping to 1.", max_pages)
                    current_page = 1
                
                await asyncio.sleep(random.uniform(config.TRAINING_PAGE_DELAY_MIN_SEC, config.TRAINING_PAGE_DELAY_MAX_SEC))
                
            except Exception as e:
                log.error("Training loop error: %s", e)
                runtime.update_cycle(
                    mode="training", status="error", subs=0, scraped=0, processed=0,
                    cached=0, errors=1, deals=0, filtered=0, drop_reasons={}
                )
                runtime.update_training(last_error=str(e))
                await asyncio.sleep(10)
