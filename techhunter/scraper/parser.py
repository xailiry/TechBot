"""HTML parsing for Avito search and detail pages.

Pure functions, decoupled from the browser. Selectors are empirical (Avito's
DOM) and centralized here so Stage 5 calibration is a one-file change.
"""
import logging
import re

from bs4 import BeautifulSoup, Tag

from .models import ParsedListing

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\d[\d\s ]*")
_REVIEWS_RE = re.compile(r"(\d+)\s*отзыв", re.I)
_LISTINGS_RE = re.compile(r"(\d+)\s*объявлен", re.I)
_YEAR_RE = re.compile(r"с\s*(\d{4})\s*года", re.I)
_RATING_RE = re.compile(r"\b([1-5][.,]\d)\b")
_AVITO_MARKET_BADGE_RE = re.compile(
    r"рыночная\s+цена|независимая\s+оценка\s+авито",
    re.I,
)
_AVITO_PRICE_BELOW_RE = re.compile(r"цена\s+ниже\s+рыночн", re.I)
_AVITO_PRICE_ABOVE_RE = re.compile(r"цена\s+выше\s+рыночн", re.I)


def _parse_avito_price_badge(text: str) -> str | None:
    if _AVITO_PRICE_BELOW_RE.search(text):
        return "below"
    if _AVITO_PRICE_ABOVE_RE.search(text):
        return "above"
    if _AVITO_MARKET_BADGE_RE.search(text):
        return "market"
    return None


def _parse_price(text: str) -> int:
    if not text:
        return 0
    m = _PRICE_RE.search(text)
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else 0


def _abs_img(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


# ─── Search page ────────────────────────────────────────────────────────────

def _parse_card(card: Tag) -> ParsedListing | None:
    try:
        item_id = card.get("data-item-id") or (card.get("id", "") or "").lstrip("i")
        if not item_id:
            return None

        title_el = card.find(attrs={"data-marker": "item-title"})
        title = title_el.get_text(strip=True) if title_el else ""
        url = title_el.get("href") if title_el else ""

        price_el = card.find(attrs={"data-marker": "item-price-value"}) or card.find(
            attrs={"data-marker": "item-price"}
        )
        price = _parse_price(price_el.get_text(strip=True)) if price_el else 0

        loc_el = card.find(attrs={"data-marker": "item-address"}) or card.find(
            attrs={"data-marker": "item-location"}
        )
        location = loc_el.get_text(strip=True) if loc_el else ""
        if not location and url:
            parts = url.lstrip("/").split("/")
            if parts:
                location = parts[0].capitalize()

        img = card.find("img", attrs={"data-marker": "item-photo"})
        if not img:
            wrap = card.find(attrs={"data-marker": "item-image"})
            img = wrap.find("img") if wrap else None
        image = None
        if img:
            image = _abs_img(
                img.get("src")
                or img.get("data-src")
                or (img.get("srcset") or "").split(" ")[0]
            )

        date_el = card.find(attrs={"data-marker": "item-date"})
        date_text = date_el.get_text(strip=True) if date_el else ""

        return ParsedListing(
            id=str(item_id),
            title=title,
            price=price,
            currency="RUB",
            url=url or "",
            location=location,
            date_text=date_text,
            image=image,
            snippet=card.get_text(" ", strip=True),
        )
    except Exception as e:  # one bad card must not kill the page
        log.debug("parse_card failed: %s", e)
        return None


def parse_listings(html: str) -> list[ParsedListing]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.find_all(attrs={"data-marker": "item"})
    return [it for it in (_parse_card(c) for c in cards) if it]


# ─── Detail page ────────────────────────────────────────────────────────────

def parse_gallery(soup: BeautifulSoup) -> list[str]:
    """Best-effort full photo gallery. Tries known gallery markers, then any
    Avito-CDN <img>, then og:image. Order preserved, deduped."""
    urls: list[str] = []

    def _add(u: str | None):
        u = _abs_img(u)
        if u and u not in urls:
            urls.append(u)

    gallery = (
        soup.find(attrs={"data-marker": "item-view/gallery"})
        or soup.find(attrs={"data-marker": "image-frame/image-wrapper"})
        or soup
    )
    for img in gallery.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and ("avito.st" in src or "avito.ru" in src):
            _add(src)
        srcset = img.get("srcset")
        if srcset:
            _add(srcset.split(",")[-1].strip().split(" ")[0])

    if not urls:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            _add(og["content"])
    return urls


def parse_detail(html: str, item: ParsedListing) -> ParsedListing:
    """Enrich a ParsedListing with data from its detail page."""
    soup = BeautifulSoup(html, "lxml")

    desc_el = soup.find(attrs={"data-marker": "item-view/item-description"})
    if desc_el:
        item.description = desc_el.get_text(" ", strip=True)

    item.images = parse_gallery(soup)
    page_text = soup.get_text(" ", strip=True)
    item.avito_price_badge = _parse_avito_price_badge(page_text)
    item.avito_market_badge = item.avito_price_badge is not None

    params_el = soup.find(attrs={"data-marker": "item-view/item-params"})
    if params_el:
        params: dict[str, str] = {}
        for li in params_el.find_all("li"):
            txt = li.get_text(" ", strip=True)
            if ":" in txt:
                k, _, v = txt.partition(":")
                k, v = k.strip().lower(), v.strip()
                if k and v:
                    params[k] = v
        if not params:
            for line in params_el.get_text("\n", strip=True).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    k, v = k.strip().lower(), v.strip()
                    if k and v:
                        params[k] = v
        item.params = params

    seller_root = soup.find(
        attrs={"data-marker": "item-view/seller-info"}
    ) or soup.find(attrs={"data-marker": "sellerInfo"})
    if seller_root:
        seller_text = seller_root.get_text(" ", strip=True)

        name_el = seller_root.find(attrs={"data-marker": "seller-info/name"})
        if name_el:
            item.seller_name = name_el.get_text(strip=True)

        label_el = seller_root.find(attrs={"data-marker": "seller-info/label"})
        if label_el:
            label = label_el.get_text(strip=True)
            item.seller_label = label
            low = label.lower()
            if "частн" in low:
                item.seller_type = "private"
            elif any(x in low for x in ("компани", "магазин", "интернет", "shop")):
                item.seller_type = "shop"

        if (m := _REVIEWS_RE.search(seller_text)):
            item.seller_reviews = int(m.group(1))
        if (m := _LISTINGS_RE.search(seller_text)):
            item.seller_listings = int(m.group(1))
        if (m := _YEAR_RE.search(seller_text)):
            item.seller_year = int(m.group(1))
        if (m := _RATING_RE.search(seller_text)):
            try:
                item.seller_rating = float(m.group(1).replace(",", "."))
            except ValueError:
                pass

    item.detail_fetched = True
    return item


# ─── Page state checks ──────────────────────────────────────────────────────

def has_listings(html: str | None) -> bool:
    """Positive signal that the search results page actually rendered."""
    if not html:
        return False
    return (
        'data-marker="item"' in html
        or 'data-marker="catalog-serp"' in html
    )


def has_detail(html: str | None) -> bool:
    """Positive signal that an item page actually rendered."""
    if not html:
        return False
    return (
        'data-marker="item-view/' in html
        or 'data-marker="item-view/title-info"' in html
    )


def is_block_page(html: str | None) -> bool:
    """Tight block/antibot detection. Unlike looks_blocked it does NOT
    trip on bare 'captcha'/'datadome' substrings (those live in legit
    Avito bundles); it requires a real interstitial signal."""
    if not html:
        return False
    s = html[:16000].lower()
    return any(
        m in s
        for m in (
            "captcha-delivery", "geo.captcha-delivery", "px-captcha",
            'data-marker="captcha"', "доступ ограничен",
            "проблема с ip", "подозрительн", "вы не робот",
            "слишком много запрос", "ip заблокир", "are you a robot",
            "access denied",
        )
    )


def page_blocked(html: str | None, *, detail: bool = False) -> bool:
    """Robust decision for the captcha gate: blocked only if the positive
    result marker is ABSENT and a real block/loading signal is present.
    An empty search (no results, no block) returns False -> not a captcha."""
    positive = has_detail(html) if detail else has_listings(html)
    if positive:
        return False
    return is_block_page(html) or looks_loading(html)


def looks_blocked(html: str | None) -> bool:
    if not html:
        return False
    snippet = html[:8000].lower()
    return any(
        m in snippet
        for m in ("доступ ограничен", "проблема с ip", "captcha", "datadome",
                  "verified-user")
    )


def looks_loading(html: str | None) -> bool:
    if not html:
        return False
    snippet = html[:8000].lower()
    return ("подождите" in snippet and "загрузк" in snippet) or (
        'meta http-equiv="refresh"' in snippet[:3000]
    )
