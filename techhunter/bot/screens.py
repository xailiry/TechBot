"""Screen builders for the button-first UI. Each returns (text, markup).

Navigation is single-message: callbacks edit the same message. The tree is
shallow (hub -> screen -> back to hub), so "Назад" is always unambiguous.
"""
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import config, runtime
from ..ai.normalize import normalize_device
from ..delivery import SELECTABLE_CONDITIONS
from ..storage import (
    feedback_stats,
    get_delivery_prefs,
    get_subscription,
    list_subscriptions,
)
from ..valuation.devices import (
    count_working_baselines,
    learned_devices,
    learning_overview,
    prices_for_model,
)
from .format import CONDITION_RU, esc, fmt_price

SUBS_PER_PAGE = 6
_BACK = InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:hub")


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hub_kb(discovery_on: bool = False) -> InlineKeyboardMarkup:
    d_text = "🔎 Discovery: ВЫКЛ" if not discovery_on else "🔎 Discovery: ВКЛ ✅"
    return _kb([
        [
            InlineKeyboardButton(text="📋 Подписки", callback_data="nav:subs:0"),
            InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
        ],
        [
            InlineKeyboardButton(text=d_text, callback_data="nav:discovery"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки",
                                 callback_data="nav:settings"),
            InlineKeyboardButton(text="📊 Статус",
                                 callback_data="nav:status"),
        ],
        [
            InlineKeyboardButton(text="📈 Качество",
                                 callback_data="nav:quality"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help"),
        ],
    ])


async def screen_hub(tg_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from ..db import get_session
    from ..db.models import User
    discovery_on = False
    if tg_id:
        async with get_session() as s:
            u = await s.get(User, tg_id)
            if u:
                discovery_on = bool(u.discovery_enabled)

    text = (
        "🎯 <b>TechHunter</b>\n"
        "Монитор Avito для перекупа техники.\n\n"
        "Слежу за новыми объявлениями, считаю рыночную цену по состояниям "
        "и присылаю только выгодные лоты (включая битые под ремонт).\n\n"
        "Выбери раздел:"
    )
    return text, hub_kb(discovery_on)


async def screen_discovery(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Discovery status + explicit on/off. State shown always matches reality
    (no blind toggle), so the label can never contradict the actual state."""
    from ..db import get_session
    from ..db.models import User
    from ..valuation.devices import count_working_baselines

    on = False
    min_rub = config.DISCOVERY_MIN_PROFIT_RUB
    min_ratio = config.DISCOVERY_MIN_PROFIT_RATIO
    async with get_session() as s:
        u = await s.get(User, tg_id)
        if u:
            on = bool(u.discovery_enabled)
            min_rub = u.discovery_min_profit_rub
            min_ratio = u.discovery_min_profit_ratio
    learned = await count_working_baselines()

    lines = [
        f"🔎 <b>Глобальный поиск (Discovery): "
        f"{'ВКЛ ✅' if on else 'ВЫКЛ ❌'}</b>",
        "",
        "Сканирую всю категорию телефонов по РФ и присылаю лучшие лоты "
        "под твои пороги.",
        "",
        f"Мин. профит: <b>{fmt_price(min_rub)}</b>",
        f"Мин. маржа: <b>{int(min_ratio * 100)}%</b>",
        f"Выучено моделей: <b>{learned}</b>",
        "",
        "Меняй пороги кнопками ниже. Ниже порог — больше сделок (в т.ч. "
        "андроиды и бюджет), но больше шума.",
    ]
    if on:
        lines += [
            "",
            "⏸ Пока Discovery включён, обычные подписки приостановлены.",
            "Идёт прогрев: бот изучает рынок (заходит в объявления и копит "
            "цены), затем начинает присылать сделки. На это нужно время.",
        ]
    rows = [
        [InlineKeyboardButton(
            text="❌ Выключить Discovery" if on else "✅ Включить Discovery",
            callback_data="disc:off" if on else "disc:on",
        )],
        [
            InlineKeyboardButton(text="− 1000", callback_data="disc:profit:-1000"),
            InlineKeyboardButton(text=f"💰 {fmt_price(min_rub)}", callback_data="noop"),
            InlineKeyboardButton(text="+ 1000", callback_data="disc:profit:1000"),
        ],
        [
            InlineKeyboardButton(text="− 5%", callback_data="disc:ratio:-5"),
            InlineKeyboardButton(text=f"📈 {int(min_ratio * 100)}%", callback_data="noop"),
            InlineKeyboardButton(text="+ 5%", callback_data="disc:ratio:5"),
        ],
        [_BACK],
    ]
    return "\n".join(lines), _kb(rows)



def screen_help() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "❓ <b>Помощь</b>\n\n"
        "Управление — кнопками. Команды работают как ярлыки:\n"
        "/menu — главное меню\n"
        "/add — добавить подписку\n"
        "/list — мои подписки\n"
        "/pause /resume — пауза доставки\n\n"
        "<b>Формат добавления</b>\n"
        "<code>запрос | до:цена | город | акб:мин%</code>\n"
        "Порядок свободный, всё кроме запроса опционально.\n\n"
        "<b>Примеры</b>\n"
        "<code>iphone 13 pro | до:55000 | москва</code>\n"
        "<code>samsung s25 ultra | до:120000 | акб:85</code>\n"
        "<code>iphone 14 разбит экран | до:40000</code>\n\n"
        "Без города ищу по всей России."
    )
    return text, _kb([[_BACK]])


def screen_add() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "➕ <b>Новая подписка</b>\n\n"
        "Нажми кнопку и пришли запрос одним сообщением.\n\n"
        "Формат: <code>запрос | до:цена | город | акб:мин%</code>\n"
        "Всё кроме запроса опционально, порядок свободный.\n\n"
        "Примеры:\n"
        "<code>iphone 13 pro | до:55000 | москва</code>\n"
        "<code>samsung s25 ultra | до:120000 | акб:85</code>"
    )
    return text, _kb([
        [InlineKeyboardButton(text="✍️ Ввести запрос",
                              callback_data="add:start")],
        [_BACK],
    ])


async def screen_subs(
    tg_id: int, page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    subs = await list_subscriptions(tg_id)
    if not subs:
        return (
            "📭 <b>Подписок пока нет</b>\n\n"
            "Добавь первую — и я начну искать выгодные лоты.",
            _kb([
                [InlineKeyboardButton(text="➕ Добавить",
                                      callback_data="nav:add")],
                [_BACK],
            ]),
        )
    pages = (len(subs) + SUBS_PER_PAGE - 1) // SUBS_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = subs[page * SUBS_PER_PAGE:(page + 1) * SUBS_PER_PAGE]

    lines = [f"📋 <b>Подписки</b> ({len(subs)})", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for s in chunk:
        bits = []
        if s.min_price or s.max_price:
            lo = fmt_price(s.min_price) if s.min_price else "—"
            hi = fmt_price(s.max_price) if s.max_price else "—"
            bits.append(f"{lo}–{hi}")
        if s.min_battery:
            bits.append(f"АКБ≥{s.min_battery}%")
        bits.append("РФ" if s.city_slug == "rossiya"
                    else s.city_slug.capitalize())
        lines.append(
            f"<b>#{s.id}</b> {esc(s.query)}\n  └ {' · '.join(bits)}"
        )
        rows.append([
            InlineKeyboardButton(
                text=f"💹 Цены #{s.id}",
                callback_data=f"nav:prices:{s.id}",
            ),
            InlineKeyboardButton(
                text=f"🗑 #{s.id}",
                callback_data=f"sub:del:{s.id}:{page}",
            ),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"nav:subs:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(
            text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"nav:subs:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
        _BACK,
    ])
    return "\n".join(lines), _kb(rows)


async def screen_settings(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    p = await get_delivery_prefs(tg_id)
    status = "⏸ на паузе" if p.paused else "▶️ активна"
    score = p.min_score or 0
    shop = "скрыты" if p.exclude_shop else "доставляются"

    text = [
        "⚙️ <b>Настройки доставки</b>",
        "",
        f"Доставка: <b>{status}</b>",
        f"Мин. score (надёжность): <b>{score}</b> (0 = выкл)",
        f"Магазины/перекупы: <b>{shop}</b>",
        "",
        "<b>Какие состояния присылать:</b>",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(
        text="▶️ Включить доставку" if p.paused else "⏸ Пауза",
        callback_data="set:pause",
    )])
    rows.append([
        InlineKeyboardButton(text="score −10", callback_data="set:score:-10"),
        InlineKeyboardButton(text=str(score), callback_data="noop"),
        InlineKeyboardButton(text="score +10", callback_data="set:score:10"),
    ])
    rows.append([InlineKeyboardButton(
        text=("🏬 Магазины: скрыть" if not p.exclude_shop
              else "🏬 Магазины: показывать"),
        callback_data="set:shop",
    )])
    cond_btns = []
    for c in SELECTABLE_CONDITIONS:
        emoji, label = CONDITION_RU.get(c, ("", c))
        on = c not in p.exclude_conditions
        cond_btns.append(InlineKeyboardButton(
            text=f"{'✅' if on else '🚫'} {label}",
            callback_data=f"set:cond:{c}",
        ))
    for i in range(0, len(cond_btns), 2):
        rows.append(cond_btns[i:i + 2])
    rows.append([_BACK])
    text.append("(✅ присылаю · 🚫 не присылаю)")
    return "\n".join(text), _kb(rows)


async def screen_status() -> tuple[str, InlineKeyboardMarkup]:
    st = runtime.snapshot()
    ov = await learning_overview()

    if st["last_cycle_at"]:
        ago = int(time.time() - st["last_cycle_at"])
        last = f"{ago} сек назад" if ago < 120 else f"{ago // 60} мин назад"
    else:
        last = "ещё не было"
    mon = "🧩 капча (реши в окне браузера)" if st["captcha"] else "🟢 работает"

    reason_ru = {
        "paused": "на паузе", "shop": "магазин",
        "low_score": "низкий score", "excluded_condition": "состояние",
    }
    drops = st.get("drop_reasons") or {}
    drop_line = (
        " · ".join(f"{reason_ru.get(k, k)} {v}" for k, v in drops.items())
        if drops else "нет"
    )

    lines = [
        "📊 <b>Статус</b>",
        "",
        f"Мониторинг: <b>{mon}</b>",
        f"Последний цикл: {last}",
        f"  собрано {st.get('scraped', 0)} · обработано "
        f"{st.get('processed', 0)} · из кэша {st.get('cached', 0)} · "
        f"ошибок {st.get('errors', 0)}",
        f"  сделок {st.get('deals', 0)} · отфильтровано "
        f"{st.get('filtered', 0)} ({drop_line})",
        f"Активных подписок: {st.get('subs', 0)}",
        "",
        f"<b>Обучение цен</b>: моделей с базой {ov['with_baseline']}"
        f" из {ov['devices']}",
    ]
    for d in ov["top"]:
        lines.append(
            f"  • {esc(d['label'])}: ~{fmt_price(d['price'])}"
            f" (выборка {d['sample']})"
        )
    if not ov["top"]:
        lines.append("  база ещё копится, дай боту поработать")
    rows = [
        [InlineKeyboardButton(text="📚 База цен (все модели)",
                              callback_data="nav:learned:0")],
        [InlineKeyboardButton(text="🔄 Обновить",
                              callback_data="nav:status")],
        [_BACK],
    ]
    return "\n".join(lines), _kb(rows)


LEARNED_PER_PAGE = 12


async def screen_learned(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """All devices whose market price the bot has learned, most-confident
    first, paginated. Lets the operator see exactly what the bot can value."""
    total = await count_working_baselines()
    if total == 0:
        return (
            "📚 <b>База цен пуста</b>\n\n"
            "Бот ещё не накопил достаточно наблюдений. Цены учатся из "
            "реальных объявлений — дай ему поработать.",
            _kb([[_BACK]]),
        )
    pages = (total + LEARNED_PER_PAGE - 1) // LEARNED_PER_PAGE
    page = max(0, min(page, pages - 1))
    devs = await learned_devices(
        limit=LEARNED_PER_PAGE, offset=page * LEARNED_PER_PAGE
    )

    lines = [f"📚 <b>Что бот знает по ценам</b> ({total} моделей)", ""]
    for d in devs:
        lines.append(
            f"• {esc(d['label'])}: <b>{fmt_price(d['price'])}</b>"
            f" (выборка {d['sample']})"
        )
    lines.append("")
    lines.append("<i>Цена — рыночная (рабочее состояние). "
                 "Сортировка: сначала самые изученные.</i>")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"nav:learned:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(
            text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"nav:learned:{page+1}"))
    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить",
                             callback_data=f"nav:learned:{page}"),
        _BACK,
    ])
    return "\n".join(lines), _kb(rows)


_TIER_ORDER = (
    ("ideal", "идеал"), ("good", "хорошее"), ("defect", "с дефектом"),
    ("broken", "битый"), ("for_parts", "на запчасти"),
)


async def screen_prices(
    tg_id: int, sub_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    back = _kb([
        [InlineKeyboardButton(text="⬅️ К подпискам",
                              callback_data="nav:subs:0")],
        [_BACK],
    ])
    sub = await get_subscription(tg_id, sub_id)
    if sub is None:
        return "Подписка не найдена.", back
    dev = normalize_device(sub.query)
    if not dev.model:
        return (
            f"💹 <b>Цены: {esc(sub.query)}</b>\n\n"
            "Модель в запросе не распознана, поэтому раздельных цен по "
            "состояниям нет. Уточни запрос (например «iphone 15 pro»).",
            back,
        )
    variants = await prices_for_model(dev.brand, dev.model)
    head = f"💹 <b>Цены по состояниям: {esc(dev.model)}</b>"
    if not variants:
        return (
            head + "\n\nБаза ещё копится — бот считает цены из реальных "
            "частных объявлений. Загляни позже.",
            back,
        )
    lines = [head, ""]
    for v in variants:
        cap = f"{v['storage_gb']} ГБ" if v["storage_gb"] else "память ?"
        if v.get("ram_gb"):
            cap += f" / {v['ram_gb']} ОЗУ"
        lines.append(f"<b>{cap}</b>")
        tiers = v["tiers"]
        if "working" in tiers:
            p, n = tiers["working"]
            lines.append(f"  рынок (рабочий): {fmt_price(p)} (выборка {n})")
        for key, label in _TIER_ORDER:
            if key in tiers:
                p, n = tiers[key]
                lines.append(f"  {label}: {fmt_price(p)} (выборка {n})")
        lines.append("")
    lines.append("<i>Цены учатся из реальных частных объявлений и "
                 "уточняются постоянно.</i>")
    return "\n".join(lines), back


async def screen_quality(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mine = await feedback_stats(tg_id)
    glob = await feedback_stats()
    acc = (
        f"{glob['up'] / glob['total'] * 100:.0f}%"
        if glob["total"] else "—"
    )
    text = (
        "📈 <b>Качество и калибровка</b>\n\n"
        "Реакции на сделки помогают подстраивать пороги.\n\n"
        f"Твои: 👍 {mine['up']} · 👎 {mine['down']}\n"
        f"Всего по боту: 👍 {glob['up']} · 👎 {glob['down']} · "
        f"точность {acc}\n\n"
        "<b>Текущие пороги сделки</b>\n"
        f"  мин. профит: {fmt_price(config.MIN_PROFIT_RUB)}\n"
        f"  мин. маржа: {config.MIN_PROFIT_RATIO*100:.0f}%\n"
        f"  оверхед на флип: {fmt_price(config.PROFIT_OVERHEAD_RUB)}\n\n"
        "Личные фильтры (score/состояния/магазины) — в Настройках. "
        "Глобальные пороги меняются в .env с перезапуском."
    )
    rows = [
        [InlineKeyboardButton(text="⚙️ Настройки",
                              callback_data="nav:settings")],
        [_BACK],
    ]
    return text, _kb(rows)
