"""Pure formatting + command parsing (no aiogram, unit-testable).

Card layout adapted for electronics: device + market + profit (incl.
"после ремонта" for broken lots) + AI condition report + scam verdict.
"""
import html as _html
import re

from ..scraper.urls import resolve_city_slug

CONDITION_RU = {
    "ideal": ("✨", "Идеал"),
    "good": ("👍", "Хорошее"),
    "defect": ("⚠️", "С дефектом"),
    "broken": ("🔧", "Битый (под ремонт)"),
    "for_parts": ("🪦", "На запчасти"),
    "unknown": ("❔", "Не определено"),
}

DEFECT_RU = {
    "screen_cracked": "разбит экран",
    "screen_display_defect": "дефект дисплея",
    "back_glass_cracked": "разбита задняя крышка",
    "screen_replaced": "неоригинальный экран",
    "battery_replaced": "АКБ под замену",
    "faceid_broken": "Face ID не работает",
    "truetone_missing": "нет True Tone",
    "icloud_locked": "привязка iCloud",
    "carrier_locked": "блокировка оператора / R-SIM / MDM",
    "not_original_parts": "неоригинальные запчасти",
    "replica": "реплика/копия",
    "no_power": "не включается",
    "cosmetic_wear": "косметические дефекты",
}

REPAIR_NAME_RU = {
    "screen": "экран",
    "back_glass": "задняя крышка",
    "battery": "АКБ",
    "faceid": "Face ID",
    "no_power": "плата/питание",
}

MISSING_RU = {
    "no_device": "модель не распознана",
    "no_baseline": "база цен ещё копится",
    "repair_cost_unknown": "не задана цена ремонта",
    "condition_discount_unknown": "дефект без оценки скидки за состояние",
    "condition_unknown": "состояние не определено",
    "for_parts": "лот на запчасти",
    "flip_blocked": "дефект мешает перепродаже (напр. Face ID)",
    "avito_market_badge_conflict": "Авито считает цену рыночной для заявленного состояния",
}

VISUAL_RU = {
    "is_stock_photo": "фото похоже на стоковое",
    "is_box_only": "на фото только коробка",
    "is_accessory": "на фото аксессуар, не телефон",
    "is_settings_screenshot": "на фото скриншот настроек",
    "screen_cracked_visual": "на фото видно разбитый экран",
}


def esc(s: str | None) -> str:
    return _html.escape(str(s or ""), quote=False)


def fmt_price(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ") + " ₽"


def _deal_badges(item, report, valuation) -> list[str]:
    badges: list[str] = []
    avito_badge = getattr(item, "avito_price_badge", None)
    if avito_badge == "below":
        badges.append("ниже рынка")
    elif avito_badge == "above":
        badges.append("выше рынка")
    elif getattr(item, "avito_market_badge", False):
        badges.append("рыночная цена")

    if getattr(valuation, "shoplike", False):
        badges.append("магазин/перекуп")
    else:
        seller_text = " ".join(
            str(x or "").lower()
            for x in (
                getattr(item, "seller_type", None),
                getattr(item, "seller_label", None),
                *getattr(valuation, "pros", []),
            )
        )
        if "част" in seller_text or "private" in seller_text:
            badges.append("частник")

    if getattr(valuation, "scam_score", 100) < 70 or getattr(valuation, "cons", None):
        badges.append("риск")
    if getattr(report, "condition", "") in ("broken", "defect", "for_parts"):
        badges.append("битый/ремонт")
    if getattr(report, "defects", None):
        badges.append("дефекты")
    return badges[:5]


def format_deal_card(item, report, valuation, sub_query: str = "") -> str:
    """Redesigned card: a profit headline up top, then market, condition,
    seller, trust, and specs. Scannable in ~2 seconds."""
    c_emoji, c_label = CONDITION_RU.get(
        report.condition, CONDITION_RU["unknown"]
    )
    sv_emoji = {
        "original": "🟢", "unknown": "⚪️",
        "suspicious": "🟠", "fake": "🔴",
    }.get(valuation.scam_verdict, "⚪️")

    # Title with regional badge if detected.
    title = item.title
    if report.is_rostest and "rst" not in title.lower() and "ростест" not in title.lower():
        title += " [RST]"
    
    lines: list[str] = [f"{sv_emoji} <b>{esc(title)}</b>"]
    badges = _deal_badges(item, report, valuation)
    if badges:
        lines.append("🏷 " + " · ".join(esc(b) for b in badges))
    lines.append("")

    # 1. Headline: price + profit (the reason this lot was sent).
    lines.append(f"💰 <b>{fmt_price(item.price)}</b>   {c_emoji} {c_label}")
    if valuation.net_profit is not None:
        pct = (
            f" · +{valuation.profit_pct*100:.0f}%"
            if valuation.profit_pct is not None else ""
        )
        if valuation.opportunity_type == "broken_flip":
            lines.append(
                f"🟢 <b>Профит после ремонта: ~"
                f"{fmt_price(valuation.net_profit)}</b>{pct}"
            )
            if valuation.repair_cost is not None:
                rb = ", ".join(
                    f"{REPAIR_NAME_RU.get(k, k)} {fmt_price(v)}"
                    for k, v in valuation.repair_breakdown.items()
                )
                lines.append(
                    f"🔧 Ремонт: {fmt_price(valuation.repair_cost)}"
                    + (" (оценочно)" if getattr(valuation, "repair_estimated", False) else "")
                    + (f" ({esc(rb)})" if rb else "")
                )
        else:
            lines.append(
                f"🟢 <b>Профит: ~{fmt_price(valuation.net_profit)}</b>{pct}"
            )

    # 2. Specs & Details.
    spec_bits = []
    if report.battery_health is not None:
        bat = f"🔋 {report.battery_health}%"
        if report.battery_cycles:
            bat += f" ({report.battery_cycles} циклов)"
        spec_bits.append(bat)
    
    if report.is_rostest:
        spec_bits.append("🌍 Ростест")
    elif report.is_sealed:
        spec_bits.append("📦 Новый/Зап")
    
    if spec_bits:
        lines.append("\n" + " · ".join(spec_bits))

    # 3. Market context.
    if valuation.device_key:
        lines.append(f"📱 {esc(valuation.device_key)}")
    if valuation.baseline_price:
        bl = f"📊 Рынок: {fmt_price(valuation.baseline_price)}"
        if item.price and valuation.baseline_price:
            bl += f" · цена {item.price / valuation.baseline_price*100:.0f}%"
        lines.append(bl)
        conf = {
            "high": "🎯 Уверенность: высокая",
            "medium": "🎯 Уверенность: средняя",
            "low": "🎯 Уверенность: низкая",
        }.get(valuation.baseline_confidence)
        if conf:
            lines.append(f"{conf} (выборка {valuation.baseline_sample})")
    avito_badge = getattr(item, "avito_price_badge", None)
    if avito_badge == "below":
        lines.append("🏷 Авито: цена ниже рыночной")
    elif avito_badge == "above":
        lines.append("🏷 Авито: цена выше рыночной")
    elif getattr(item, "avito_market_badge", False):
        lines.append("🏷 Авито: рыночная цена по заявленному состоянию")

    # 4. Seller.
    if item.seller_name:
        sb = [esc(item.seller_name)]
        if item.seller_label:
            sb.append(esc(item.seller_label))
        if item.seller_listings is not None:
            sb.append(f"{item.seller_listings} объявл")
        if item.seller_reviews is not None:
            sb.append(f"{item.seller_reviews} отз")
        lines.append("\n👤 " + " · ".join(sb))

    # 5. Condition & Defects.
    if report.defects or report.visual:
        lines.append("\n🧠 <b>Состояние</b>")
        if report.defects:
            defs = [DEFECT_RU.get(d, d) for d in report.defects]
            lines.append("⚠️ " + ", ".join(esc(d) for d in defs))
        vis = [
            VISUAL_RU[k]
            for k, val in (report.visual or {}).items()
            if val and k in VISUAL_RU
        ]
        if vis:
            lines.append("👁 " + ", ".join(esc(v) for v in vis))

    # 6. Trust & Verdict.
    lines.append(
        f"\n🛡 Надёжность {valuation.scam_score}/100 · "
        f"{valuation.scam_verdict}"
    )
    for p in valuation.pros[:2]:
        lines.append(f"✅ {esc(p)}")
    for c in valuation.cons[:2]:
        lines.append(f"⛔ {esc(c)}")

    # 7. Footnotes.
    if valuation.missing:
        miss = [MISSING_RU.get(m, m) for m in valuation.missing]
        lines.append("\nℹ️ " + "; ".join(esc(m) for m in miss))
    
    footer = []
    if item.date_text:
        footer.append(f"⌛ {esc(item.date_text)}")
    if sub_query:
        footer.append(f"🔎 <i>{esc(sub_query)}</i>")
    
    if footer:
        lines.append("\n" + " · ".join(footer))

        
    return "\n".join(lines)



_TIER_LABELS = (
    ("ideal", "идеал"), ("good", "хор"), ("defect", "с деф"),
    ("broken", "битый"), ("for_parts", "на з/ч"),
)


def onboarding_started_text(query: str, eta_sec: int) -> str:
    mins = max(1, round(eta_sec / 60))
    return (
        f"🔎 <b>Анализирую рынок: {esc(query)}</b>\n"
        f"Собираю объявления и считаю средние цены по состояниям.\n"
        f"Это разовый стартовый анализ, примерно до {mins} мин "
        f"(часто быстрее). Пока он идёт, мониторинг уже работает; "
        f"цены будут уточняться постоянно."
    )


def onboarding_done_text(query: str, summary: list) -> str:
    if not summary:
        return (
            f"📊 <b>Анализ цен завершён: {esc(query)}</b>\n"
            f"Пока мало данных для надёжной средней. Бот продолжает "
            f"копить статистику и уточнит цены автоматически."
        )
    lines = [f"📊 <b>Анализ цен завершён: {esc(query)}</b>", ""]
    for label, tiers in summary[:12]:
        parts = [
            f"{lbl} ~{fmt_price(tiers[k])}"
            for k, lbl in _TIER_LABELS
            if k in tiers
        ]
        work = tiers.get("working")
        head = f"<b>{esc(label)}</b>"
        if work:
            head += f" · рынок {fmt_price(work)}"
        lines.append(head)
        if parts:
            lines.append("  └ " + " · ".join(parts))
    lines.append("")
    lines.append("Цены продолжают уточняться по мере новых объявлений.")
    return "\n".join(lines)


def build_haggle_text(item, valuation) -> str:
    """Copy-paste first message to the seller."""
    city = (item.location or "").split(",")[0].strip()
    suffix = f" ({city})" if city else ""
    if (
        valuation.baseline_price
        and item.price
        and item.price < 0.5 * valuation.baseline_price
    ):
        return (
            "<code>Здравствуйте! Лот ещё актуален? Можно фото/видео "
            "состояния (экран, корпус, АКБ, проверка на активацию)? "
            "Готов забрать сразу.</code>"
        )
    low = int(item.price * 0.92 / 100) * 100 if item.price else 0
    return (
        f"<code>Здравствуйте! Готов забрать сегодня{suffix}. "
        f"Есть {low}₽ на руках, торг возможен? "
        f"Можно видеоосмотр и проверку IMEI?</code>"
    )


# ─── Command parsing ────────────────────────────────────────────────────────

_RE_PRICE = re.compile(r"(?:до|от)[:\s]?\s*\d", re.I)
_RE_BAT = re.compile(r"(?:акб|батар|bat|аккум)\w*[:\s]*\d", re.I)
_RE_NUM = re.compile(r"^\d[\d \.,]*$")


def _classify(value: str) -> str:
    v = value.strip().lower()
    if not v:
        return ""
    if _RE_BAT.search(v):
        return "battery"
    if _RE_PRICE.search(v):
        return "price"
    if _RE_NUM.match(v):
        try:
            num = int(v.replace(" ", "").replace(",", ""))
        except ValueError:
            return "city"
        return "battery" if num <= 100 else "price"
    return "city"


def _parse_price(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    m_min = re.search(r"от[:\s]*(\d[\d ]*)", value, re.I)
    m_max = re.search(r"до[:\s]*(\d[\d ]*)", value, re.I)
    mn = int(m_min.group(1).replace(" ", "")) if m_min else None
    mx = int(m_max.group(1).replace(" ", "")) if m_max else None
    if mn is None and mx is None:
        m = re.search(r"(\d[\d ]*)", value)
        if m:
            mx = int(m.group(1).replace(" ", ""))
    return mn, mx


def _parse_battery(value: str) -> int | None:
    m = re.search(r"(\d{1,3})", value)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 100 else None


def parse_command(body: str) -> dict | None:
    """Parse '/add iphone 13 | до:50000 | москва | акб:85'.
    Fields after the query are classified by content; order is free."""
    if not body or not body.strip():
        return None
    parts = [p.strip() for p in body.split("|")]
    query = parts[0].strip()
    if not query:
        return None
    fields: dict[str, str | None] = {
        "price": None, "city": None, "battery": None
    }
    for raw in parts[1:]:
        kind = _classify(raw)
        if kind and fields.get(kind) is None:
            fields[kind] = raw
    mn, mx = _parse_price(fields["price"] or "")
    return {
        "query": query,
        "min_price": mn,
        "max_price": mx,
        "city_slug": resolve_city_slug(fields["city"]) if fields["city"]
        else "rossiya",
        "min_battery": _parse_battery(fields["battery"] or "")
        if fields["battery"] else None,
    }
