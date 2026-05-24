"""Scam / fake guard for electronics. Ported in spirit from Shmotki
scam_score.py (homoglyph normalization + signal scoring).

Critical difference vs clothing: a cheap BROKEN/DEFECT/FOR_PARTS lot is the
*opportunity*, not a scam. The price-vs-baseline penalty therefore applies
only to working-grade listings, so we never kill a "cracked screen, sold for
peanuts" deal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Re-exported for back-compat; logic lives in the shared textnorm module.
from ..textnorm import homoglyph_ratio, normalize_homoglyphs  # noqa: F401


_ORIG = [
    re.compile(r"\bоригинал[а-я]*\b", re.I),
    re.compile(r"\bофициальн[а-я]*\b", re.I),
    re.compile(r"\bростест\b", re.I),
    re.compile(r"\bгаранти[ия]\b", re.I),
    re.compile(r"\bчек[\s,\.]", re.I),
    re.compile(r"\bполн\w*\s*комплект\b", re.I),
    re.compile(r"\bкоробк[аи]\b", re.I),
]
_FAKE = [
    re.compile(r"\bкопи[яийюе]\b", re.I),
    re.compile(r"\bреплик[аиую]\b", re.I),
    re.compile(r"\bподделк", re.I),
    re.compile(r"\b1\s*[:вкx]\s*1\b", re.I),
    re.compile(r"\b(?:люкс|lux|premium)\s*копи", re.I),
    re.compile(r"\bне\s*оригинал\b", re.I),
    re.compile(r"\bfake\b", re.I),
]
_REFURB = re.compile(
    r"\bреф\b|рефаб|refurb|восстановл\w*|восстановк", re.I
)
_NOT_REFURB = re.compile(
    r"\b(?:не|not)\s*(?:реф|рефаб|refurb\w*|восстановл\w*|восстановк\w*)\b",
    re.I,
)
_SHOP = [
    re.compile(r"\bмагазин\b", re.I),
    re.compile(r"\bопт[ом]?\b", re.I),
    re.compile(r"\bпод\s*заказ\b", re.I),
    re.compile(r"\bрассрочк[аи]\b", re.I),
    re.compile(r"\btrade[\s\-]?in\b|трейд[\s\-]?ин", re.I),
    re.compile(r"\bдоставк[аи]\s*по\s*(?:росси|рф)\b", re.I),
    re.compile(r"\bв\s*наличи[ия]\b.{0,30}\b(?:цвет|памят|модел)", re.I),
    re.compile(r"\bпиш[иу](?:те)?\s*в\s*л[счс]\b", re.I),
    re.compile(r"\bбольш[оией]+\s*выбор\b", re.I),
    re.compile(r"\bнаш[аи]?\s*магазин\b", re.I),
]
_ICLOUD = re.compile(
    r"icloud|айклауд|activation lock|залочен|привязан\w*\s*к|frp", re.I
)
_IDEAL_CLAIM = re.compile(r"\bидеал\w*|как\s*новый|без\s*царапин", re.I)

_WORKING_GRADES = {"ideal", "good"}


@dataclass
class ScamScore:
    score: int = 50
    verdict: str = "unknown"  # original | suspicious | fake | unknown
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)


def _any(text: str, pats: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in pats)


# Extra retail/refurb markers that inflate or distort the USED baseline.
_RETAIL = [
    re.compile(r"\bгаранти[яи]\b", re.I),
    re.compile(r"\bвыкуп\b", re.I),
    re.compile(r"\bкредит\b", re.I),
    re.compile(r"\bофициальн\w*", re.I),
    re.compile(r"\boutlet\b|аутлет", re.I),
    re.compile(r"\bне\s*ref\b|не\s*реф\b", re.I),
    re.compile(r"\bоптов\w*", re.I),
    re.compile(r"\bассортимент\b", re.I),
    re.compile(r"\bновый\b.{0,20}\bв\s*наличи", re.I),
]


def looks_shoplike(
    title: str,
    description: str = "",
    seller_type: str | None = None,
    seller_listings: int | None = None,
    seller_reviews: int | None = None,
) -> bool:
    """True if the listing is a shop / reseller / refurbisher / wholesale.
    Such listings are excluded from baseline LEARNING so the reference
    reflects the real private used market, not inflated retail prices.
    (They are still valued and can still be delivered as deals.)"""
    if seller_type == "shop":
        return True
    if seller_listings is not None and seller_listings >= 30:
        return True
    if (
        seller_type == "private"
        and seller_listings is not None
        and seller_reviews is not None
        and seller_listings >= 5
        and seller_reviews >= 20
    ):
        return True
    text = normalize_homoglyphs(f"{title} {description}")
    shop_text = _NOT_REFURB.sub(" ", text)
    return (
        _any(shop_text, _SHOP)
        or _any(shop_text, _RETAIL)
        or bool(_REFURB.search(shop_text))
    )


def score_listing(
    title: str,
    description: str = "",
    price: int = 0,
    *,
    condition: str = "good",
    baseline_price: int | None = None,
    seller_type: str | None = None,
    seller_listings: int | None = None,
    seller_reviews: int | None = None,
    reused_image_count: int = 0,
    visual: dict | None = None,
    defects: list[str] | None = None,
    avito_market_badge: bool = False,
    avito_price_badge: str | None = None,
) -> ScamScore:
    full = normalize_homoglyphs(f"{title} {description}")
    pros: list[str] = []
    cons: list[str] = []
    score = 50
    visual = visual or {}
    defects = defects or []
    avito_price_badge = avito_price_badge or ("market" if avito_market_badge else None)

    fake = _any(full, _FAKE)
    if fake:
        score -= 40
        cons.append("маркер реплики/копии/подделки")
    if _ICLOUD.search(full) or "icloud_locked" in defects:
        score -= 25
        cons.append("привязка iCloud / Activation Lock")
    if "carrier_locked" in defects:
        score -= 25
        cons.append("заблокирован под оператора / R-SIM / MDM / демо")
    if _REFURB.search(full):
        score -= 8
        cons.append("реф/восстановленный")

    if _any(full, _ORIG):
        score += 16
        pros.append("признаки оригинала (чек/гарантия/ростест)")

    if _any(full, _SHOP):
        score -= 12
        cons.append("признаки магазина/перекупа в тексте")
    if seller_type == "shop":
        score -= 12
        cons.append("продавец — магазин")
    elif seller_type == "private":
        if seller_listings and seller_reviews and seller_listings >= 5 and seller_reviews >= 20:
            score -= 18
            no_original = True
            cons.append(
                f"«частник» с {seller_listings} объявл. и {seller_reviews} отзывами — похож на перекупа"
            )
        elif seller_listings and seller_listings >= 50:
            score -= 18
            cons.append(f"«частник» с {seller_listings} объявл. — магазин в маске")
        elif seller_listings and seller_listings >= 20:
            score -= 6
        else:
            score += 6
            pros.append("частник")

    # Price-vs-baseline scam check ONLY for working-grade lots. For broken /
    # defect / for_parts a low price is the opportunity, not a red flag.
    if (
        baseline_price
        and price > 0
        and condition in _WORKING_GRADES
    ):
        ratio = price / baseline_price
        if ratio < 0.20:
            score -= 35
            cons.append(f"цена {ratio*100:.0f}% от рынка для рабочего — подозрительно")
        elif ratio < 0.40:
            score -= 10
            cons.append(f"цена {ratio*100:.0f}% от рынка")
        elif 0.40 <= ratio <= 0.85:
            score += 14
            pros.append(f"цена {ratio*100:.0f}% от рынка — хороший арбитраж")
        if avito_price_badge == "below":
            score += 8
            pros.append("Авито: цена ниже рыночной")
        elif avito_price_badge == "above":
            score -= 12
            cons.append("Авито: цена выше рыночной")
        elif avito_price_badge == "market" and ratio <= 0.70:
            score -= 14
            cons.append(
                "Авито считает цену рыночной — проверь заявленное состояние"
            )

    if reused_image_count > 0:
        pen = min(40, reused_image_count * 15)
        score -= pen
        cons.append(f"фото в {reused_image_count} др. объявлениях (скам?)")

    if visual.get("is_stock_photo"):
        score -= 18
        cons.append("студийное/стоковое фото вместо реального")
    if visual.get("is_box_only"):
        score -= 20
        cons.append("на фото коробка, не устройство")
    if visual.get("screen_cracked_visual") and _IDEAL_CLAIM.search(full):
        score -= 22
        cons.append("текст «идеал», но на фото разбитый экран")

    no_original = False

    # Obfuscation: Latin lookalikes embedded in Cyrillic words. Honest
    # private sellers don't do this; resellers/scammers do.
    ratio = homoglyph_ratio(f"{title} {description}")
    if ratio >= 0.04:
        score -= 25
        no_original = True
        cons.append("массовая подмена букв (обфускация спама)")
    elif ratio >= 0.015:
        score -= 10
        cons.append("подмена букв в тексте")

    # "Выкуп вашей техники Apple/Samsung..." = reseller, not a one-off
    # private sale, even if the badge says "Частное лицо".
    if re.search(r"\bвыкуп\w*\s+(?:ваш|вашей|техник|устройств|айфон|"
                 r"телефон)", full, re.I):
        score -= 14
        no_original = True
        cons.append("скупка/выкуп техники — перекуп")

    # Claims "оригинал/идеал" while the screen/parts are NOT original.
    if (
        ({"screen_replaced", "not_original_parts", "truetone_missing"} & set(defects))
        and (_any(full, _ORIG) or _IDEAL_CLAIM.search(full))
    ):
        score -= 22
        no_original = True
        cons.append("заявлен оригинал/идеал, но экран/детали не родные")

    if fake:
        verdict = "fake"
    elif score >= 68 and not no_original:
        verdict = "original"
    elif score <= 32:
        verdict = "suspicious"
    else:
        verdict = "unknown"

    return ScamScore(
        score=max(0, min(100, score)), verdict=verdict, pros=pros, cons=cons
    )
