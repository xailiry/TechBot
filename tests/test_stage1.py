"""Stage 1 verification (no live network, no browser launch).

Run: ../venv/Scripts/python.exe -m tests.test_stage1   (from project root)
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import sys

from techhunter.scraper.parser import (
    looks_blocked,
    looks_loading,
    parse_detail,
    parse_listings,
)
from techhunter.scraper.urls import build_search_url, resolve_city_slug

SEARCH_HTML = """
<html><body>
<div data-marker="item" data-item-id="111">
  <a data-marker="item-title" href="/moskva/telefony/iphone_13_128_2222">iPhone 13 128GB</a>
  <span data-marker="item-price-value">45 000 ₽</span>
  <span data-marker="item-address">Москва, м. Тверская</span>
  <img data-marker="item-photo" src="//img.avito.st/image/1/abc.jpg">
</div>
<div data-marker="item" data-item-id="222">
  <a data-marker="item-title" href="/spb/telefony/iphone_14_3333">iPhone 14 256</a>
  <span data-marker="item-price-value">от 60 000 руб.</span>
</div>
</body></html>
"""

DETAIL_HTML = """
<html><head><meta property="og:image" content="https://img.avito.st/og.jpg"></head>
<body>
<div data-marker="item-view/item-description">Идеал, акб 89%, Face ID работает.</div>
<div data-marker="item-view/item-params"><ul>
  <li>Объём встроенной памяти: 128 ГБ</li>
  <li>Состояние: Б/у</li>
</ul></div>
<div data-marker="item-view/seller-info">
  <span data-marker="seller-info/name">Иван</span>
  <span data-marker="seller-info/label">Частное лицо</span>
  4,8 на Авито, 27 отзывов, 12 объявлений, с 2019 года
</div>
<div data-marker="item-view/gallery">
  <img src="https://img.avito.st/image/1/p1.jpg">
  <img src="https://img.avito.st/image/1/p2.jpg">
</div>
</body></html>
"""


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def test_urls() -> None:
    u = build_search_url("iphone 13", "moskva", max_price=50000, page=2)
    check("url has query", "q=iphone+13" in u)
    check("url sorted by date", "s=104" in u)
    check("url pmax", "pmax=50000" in u)
    check("url page", "p=2" in u)
    check("url category", "/moskva/telefony" in u)
    check("city resolve спб", resolve_city_slug("спб") == "sankt-peterburg")
    check("city resolve unknown", resolve_city_slug("Тверь") == "тверь")


def test_search_parse() -> None:
    items = parse_listings(SEARCH_HTML)
    check("2 cards parsed", len(items) == 2)
    a = items[0]
    check("id", a.id == "111")
    check("title", a.title == "iPhone 13 128GB")
    check("price", a.price == 45000)
    check("location", "Москва" in a.location)
    check("image abs", a.image == "https://img.avito.st/image/1/abc.jpg")
    check("full_url", a.full_url.startswith("https://www.avito.ru/moskva/"))
    check("price 'от 60 000'", items[1].price == 60000)


def test_detail_parse() -> None:
    items = parse_listings(SEARCH_HTML)
    it = parse_detail(DETAIL_HTML, items[0])
    check("desc", "Face ID" in it.description)
    check("param memory", it.params.get("объём встроенной памяти") == "128 ГБ")
    check("seller name", it.seller_name == "Иван")
    check("seller type private", it.seller_type == "private")
    check("seller reviews", it.seller_reviews == 27)
    check("seller listings", it.seller_listings == 12)
    check("seller year", it.seller_year == 2019)
    check("seller rating", it.seller_rating == 4.8)
    check("gallery 2 imgs", it.images == [
        "https://img.avito.st/image/1/p1.jpg",
        "https://img.avito.st/image/1/p2.jpg",
    ])
    check("detail_fetched", it.detail_fetched is True)


def test_block_detect() -> None:
    check("blocked detect", looks_blocked("<html>Доступ ограничен: проблема с IP"))
    check("captcha detect", looks_blocked("please solve the CAPTCHA"))
    check("not blocked", not looks_blocked("<div data-marker='item'>"))
    check("loading detect", looks_loading("Подождите, идёт загрузка"))


async def test_dedup() -> None:
    import uuid

    from techhunter.scraper.models import ParsedListing
    from techhunter.storage import register_listing

    pl = ParsedListing(id=f"stage1-{uuid.uuid4().hex}", title="t", price=1000,
                        url="/x/999")
    first = await register_listing(pl)
    second = await register_listing(pl)
    check("first seen -> new", first is True)
    check("second seen -> dup", second is False)


def main() -> None:
    test_urls()
    test_search_parse()
    test_detail_parse()
    test_block_detect()
    asyncio.run(test_dedup())
    print("\nAll Stage 1 checks passed.")


if __name__ == "__main__":
    main()
