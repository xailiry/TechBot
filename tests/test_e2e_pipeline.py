"""Stage 5: offline end-to-end proxy for the manual visible-monitor run.

Avito search HTML -> parse_listings -> evaluate -> value -> deal card.
Exercises the whole chain without a browser or network, so a green run
means the only unknowns left for the manual step are live DOM selectors
and anti-bot behaviour.

Run: ../venv/Scripts/python.exe -m tests.test_e2e_pipeline
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys

from techhunter.ai.evaluate import evaluate_listing
from techhunter.bot.format import format_deal_card
from techhunter.scraper.parser import parse_listings
from techhunter.valuation.devices import get_or_create_device, set_manual_baseline
from techhunter.valuation.engine import value_listing

SEARCH_HTML = """
<html><body>
<div data-marker="item" data-item-id="900001">
  <a data-marker="item-title" href="/moskva/telefony/iphone_13_pro_900001">
     iPhone 13 Pro 256 ГБ, акб 92%, идеал, ростест</a>
  <span data-marker="item-price-value">63 000 ₽</span>
  <span data-marker="item-address">Москва</span>
  <img data-marker="item-photo" src="//img.avito.st/image/1/a.jpg">
</div>
<div data-marker="item" data-item-id="900002">
  <a data-marker="item-title" href="/spb/telefony/iphone_12_900002">
     iPhone 12 128, разбит экран, всё работает</a>
  <span data-marker="item-price-value">21 000 ₽</span>
  <span data-marker="item-address">Санкт-Петербург</span>
</div>
</body></html>
"""


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


async def _main() -> None:
    dev = await get_or_create_device("apple", "iPhone 13 Pro [RST]", 256)
    await set_manual_baseline(dev, 82000)

    items = parse_listings(SEARCH_HTML)
    check("parsed 2 cards", len(items) == 2)

    cards = []
    for it in items:
        rep = await evaluate_listing(it, run_clip=False, do_dedup=False)
        val = await value_listing(it, rep, log_obs=False)
        text = format_deal_card(it, rep, val, sub_query="iphone")
        cards.append((it, rep, val, text))

    it0, rep0, val0, txt0 = cards[0]
    check("e2e price parsed", it0.price == 63000)
    check("e2e model normalized", rep0.model == "iPhone 13 Pro [RST]")
    check("e2e battery from title", rep0.battery_health == 92)
    check("e2e working profit", val0.net_profit == 19000
          and val0.opportunity is True)
    check("e2e card has title", "iPhone 13 Pro" in txt0)
    check("e2e card has profit line", "Профит" in txt0)

    it1, rep1, val1, txt1 = cards[1]
    check("e2e broken detected",
          rep1.condition == "broken" and "screen_cracked" in rep1.defects)
    check("e2e broken card renders", "21 000 ₽" in txt1)

    print("\nAll e2e pipeline checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
