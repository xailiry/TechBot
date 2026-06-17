"""Screen builders for the Telegram button UI.

The normal UI is intentionally operator-friendly: it explains the hunt in
plain language. Raw counters live in /dev so the main screens stay calm.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import config, runtime, settings_store
from ..ai.normalize import normalize_device
from ..delivery import SELECTABLE_CONDITIONS
from ..storage import (
    dev_db_stats,
    discovery_users,
    feedback_stats,
    get_delivery_prefs,
    get_subscription,
    list_subscriptions,
    pending_alert_stats,
)
from ..valuation.devices import (
    count_working_baselines,
    learned_devices,
    learning_overview,
    prices_for_model,
)
from .format import CONDITION_RU, esc, fmt_price

SUBS_PER_PAGE = 6
LEARNED_PER_PAGE = 12

DISCOVERY_PRESETS: dict[str, tuple[str, int, float]] = {
    "strict": ("Строго", 12000, 0.30),
    "balance": (
        "Баланс",
        config.DISCOVERY_MIN_PROFIT_RUB,
        config.DISCOVERY_MIN_PROFIT_RATIO,
    ),
    "aggressive": ("Агрессивно", 4000, 0.12),
}

_BACK = InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:hub")

_REASON_RU = {
    "paused": "пауза",
    "shop": "магазин",
    "low_score": "низкая надёжность",
    "excluded_condition": "состояние",
    "discovery_threshold": "невыгодно",
    "discovery_roi": "низкий ROI",
    "discovery_quality": "качество сделки",
    "duplicate_repost": "повтор объявления",
    "feedback_threshold": "личный порог",
    "low_battery": "низкая АКБ",
    "no_baseline": "нет базы",
    "training_skipped": "обучение: пропуск",
    "training_detail": "обучение: карточка",
    "send_error": "ошибка отправки",
    "feedback_reseller_rebuild": "пересобранный перекупом",
    "feedback_reseller": "перекуп по твоему фидбеку",
    "feedback_battery": "АКБ по твоему фидбеку",
    "feedback_condition": "состояние по твоему фидбеку",
    "feedback_wrong_model": "модель/память по твоему фидбеку",
    "feedback_too_expensive": "дорого по твоему фидбеку",
    "feedback_scam": "мутно по твоему фидбеку",
}

_DEV_ACTIONS = {
    "retry_outbox": "Retry outbox",
    "train_start": "Train start",
    "train_stop": "Train stop",
    "browser_restart": "Force browser restart",
    "clear_dead_outbox": "Clear dead outbox",
    "relearn_stale": "Relearn stale baselines",
    "cleanup_old_rows": "Cleanup old rows",
}


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ago(ts: float | datetime | None) -> str:
    if not ts:
        return "ещё не было"
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = int((datetime.now(timezone.utc) - ts).total_seconds())
    else:
        delta = int(time.time() - ts)
    if delta < 0:
        delta = 0
    if delta < 90:
        return f"{delta} сек назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    return f"{delta // 3600} ч назад"


def _drop_line(drops: dict | None, *, human: bool = True) -> str:
    if not drops:
        return "нет"
    if human:
        return ", ".join(
            f"{_REASON_RU.get(str(k), str(k))} {v}" for k, v in drops.items()
        )
    return ", ".join(f"{k}={v}" for k, v in drops.items())


def _cycle_human(label: str, c: dict | None) -> str:
    c = c or {}
    seen = int(c.get("scraped") or 0)
    opened = int(c.get("processed") or 0)
    known = int(c.get("cached") or 0)
    deals = int(c.get("deals") or 0)
    errors = int(c.get("errors") or 0)
    status = c.get("status") or "idle"
    return (
        f"<b>{label}</b>: {status}, {_ago(c.get('last_at'))}\n"
        f"Нашёл {seen}, открыл {opened}, уже видел {known}, "
        f"отправил {deals}, ошибок {errors}."
    )


def _cycle_raw(label: str, c: dict | None) -> str:
    c = c or {}
    return (
        f"{label}: status={c.get('status', 'idle')} last={_ago(c.get('last_at'))}; "
        f"scraped={c.get('scraped', 0)} processed={c.get('processed', 0)} "
        f"cache={c.get('cached', 0)} deals={c.get('deals', 0)} "
        f"filtered={c.get('filtered', 0)} drop_reasons={{{_drop_line(c.get('drop_reasons'), human=False)}}} "
        f"errors={c.get('errors', 0)}"
    )


def _mode_title(st: dict, discovery_on: bool) -> str:
    if st.get("training_mode"):
        return "🧪 Обучение идёт, охота остановлена"
    if st.get("captcha"):
        return "🧩 Капча: реши её в окне браузера"
    if discovery_on:
        return "🟢 Discovery охотится"
    return "⏸ Охота выключена"


def _bool_ru(v: bool) -> str:
    return "да" if v else "нет"


def hub_kb(
    discovery_on: bool = False, is_admin: bool = False
) -> InlineKeyboardMarkup:
    d_text = "🔎 Радар Discovery" + (" ✅" if discovery_on else "")
    rows = [
        [InlineKeyboardButton(text=d_text, callback_data="nav:discovery")],
        [
            InlineKeyboardButton(text="🎯 Сделки и фильтры", callback_data="nav:settings"),
            InlineKeyboardButton(text="📊 Статус", callback_data="nav:status"),
        ],
        [
            InlineKeyboardButton(text="📚 База цен", callback_data="nav:learned:0"),
            InlineKeyboardButton(text="📋 Подписки", callback_data="nav:subs:0"),
        ],
        [
            InlineKeyboardButton(text="📈 Качество", callback_data="nav:quality"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="nav:help"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Dev", callback_data="nav:dev")])
    return _kb(rows)


async def screen_hub(tg_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from ..db import get_session
    from ..db.models import User

    discovery_on = False
    is_admin = bool(tg_id and tg_id in config.ADMIN_USER_IDS)
    if tg_id:
        async with get_session() as s:
            u = await s.get(User, tg_id)
            discovery_on = bool(u and u.discovery_enabled)

    st = runtime.snapshot()
    cycles = st.get("cycles") or {}
    fast = cycles.get("fast") or {}
    deep = cycles.get("deep") or {}
    outbox = await pending_alert_stats()
    ov = await learning_overview(limit=3)

    lines = [
        "🎯 <b>TechHunter</b>",
        _mode_title(st, discovery_on),
        "",
        _cycle_human("Последний быстрый проход", fast),
        _cycle_human("Deep", deep),
        "",
        f"База цен: <b>{ov['with_baseline']}</b> моделей с рабочей ценой.",
        f"Outbox: <b>{outbox['pending']}</b> ждут отправки"
        + (f", <b>{outbox['dead']}</b> умерли" if outbox["dead"] else "."),
    ]
    if discovery_on:
        lines.append("")
        lines.append("Пока Discovery включён, подписки отдыхают.")
    return "\n".join(lines), hub_kb(discovery_on, is_admin)


async def screen_discovery(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from ..db import get_session
    from ..db.models import User

    on = False
    min_rub = config.DISCOVERY_MIN_PROFIT_RUB
    min_ratio = config.DISCOVERY_MIN_PROFIT_RATIO
    scan_mode = "fast"
    async with get_session() as s:
        u = await s.get(User, tg_id)
        if u:
            on = bool(u.discovery_enabled)
            min_rub = int(u.discovery_min_profit_rub)
            min_ratio = float(u.discovery_min_profit_ratio)
            scan_mode = u.discovery_scan_mode or "fast"
    learned = await count_working_baselines()
    mode_label = "Быстрый" if scan_mode == "fast" else "Аккуратный"

    lines = [
        f"🔎 <b>Радар Discovery: {'ВКЛ' if on else 'ВЫКЛ'}</b>",
        "",
        "Это главный режим охоты: бот быстро смотрит свежие страницы, "
        "открывает перспективные карточки и присылает только сделки.",
        "",
        f"Порог профита: <b>{fmt_price(min_rub)}</b>",
        f"Порог маржи: <b>{int(min_ratio * 100)}%</b>",
        f"Минимальный ROI: <b>{config.DISCOVERY_MIN_ROI_RATIO * 100:.0f}%</b>",
        f"Качество сделки: <b>{config.DISCOVERY_MIN_DEAL_SCORE}/100</b>",
        f"База цен: <b>{learned}</b> моделей",
        f"Темп сканирования: <b>{mode_label}</b>",
        "",
        "<b>Быстрый</b> - как сейчас: частые проходы и глубокий поиск.",
        "<b>Аккуратный</b> - 1 страница раз в 3-5 минут, до 3 карточек, "
        "без глубокого обхода. Подходит, чтобы оставить бота без присмотра.",
        "",
        "Пресеты:",
        "Строго — меньше шума. Баланс — рабочий режим. "
        "Агрессивно — больше кандидатов, но больше мусора.",
    ]
    if on:
        lines.append("")
        lines.append("⏸ Пока Discovery включён, подписки приостановлены.")

    rows = [
        [InlineKeyboardButton(
            text="❌ Выключить Discovery" if on else "✅ Включить Discovery",
            callback_data="disc:off" if on else "disc:on",
        )],
        [
            InlineKeyboardButton(
                text=("Быстрый ✓" if scan_mode == "fast" else "Быстрый"),
                callback_data="disc:mode:fast",
            ),
            InlineKeyboardButton(
                text=("Аккуратный ✓" if scan_mode == "careful" else "Аккуратный"),
                callback_data="disc:mode:careful",
            ),
        ],
        [
            InlineKeyboardButton(text="Строго", callback_data="disc:preset:strict"),
            InlineKeyboardButton(text="Баланс", callback_data="disc:preset:balance"),
            InlineKeyboardButton(text="Агрессивно", callback_data="disc:preset:aggressive"),
        ],
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
        "❓ <b>Как пользоваться TechHunter</b>\n\n"
        "Главный режим — <b>Discovery</b>. Он смотрит свежие объявления по "
        "категории телефонов и присылает те, где есть запас для перепродажи.\n\n"
        "<b>Подписки</b> нужны точечно: например, искать конкретную модель, "
        "доноров или редкую конфигурацию.\n\n"
        "<b>/train_start</b> — отдельное обучение цен. Пока оно идёт, охота "
        "за сделками полностью остановлена. <b>/train_stop</b> возвращает "
        "обычную работу.\n\n"
        "Формат подписки:\n"
        "<code>модель | до:цена | город | акб:мин%</code>\n\n"
        "Примеры:\n"
        "<code>iphone 13 pro | до:55000 | москва</code>\n"
        "<code>samsung s25 ultra | до:120000 | акб:85</code>\n\n"
        "Техническая панель владельца: <b>/dev</b>."
    )
    return text, _kb([[_BACK]])


def screen_add() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "➕ <b>Новая подписка</b>\n\n"
        "Подписки — вторичный, точечный инструмент. Напиши запрос одной "
        "строкой, остальное можно не указывать.\n\n"
        "<code>iphone 13 pro | до:55000 | москва | акб:85</code>"
    )
    return text, _kb([
        [InlineKeyboardButton(text="✍️ Ввести запрос", callback_data="add:start")],
        [_BACK],
    ])


async def screen_subs(tg_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    subs = await list_subscriptions(tg_id)
    if not subs:
        return (
            "📭 <b>Подписок пока нет</b>\n\n"
            "Основной режим — Discovery. Подписку добавляй, когда нужен "
            "точный поиск конкретной модели.",
            _kb([
                [InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add")],
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
        bits.append("РФ" if s.city_slug == "rossiya" else s.city_slug.capitalize())
        paused = bool(getattr(s, "paused", 0))
        status = " ⏸ на паузе" if paused else ""
        lines.append(f"<b>#{s.id}</b> {esc(s.query)}{status}\n  {' · '.join(bits)}")
        rows.append([
            InlineKeyboardButton(text=f"цены #{s.id}", callback_data=f"nav:prices:{s.id}"),
            InlineKeyboardButton(
                text=(f"▶️ вкл #{s.id}" if paused else f"⏸ пауза #{s.id}"),
                callback_data=f"sub:pause:{s.id}:{page}",
            ),
            InlineKeyboardButton(text=f"удалить #{s.id}", callback_data=f"sub:del:{s.id}:{page}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:subs:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:subs:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="nav:add"),
        _BACK,
    ])
    return "\n".join(lines), _kb(rows)


async def screen_settings(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    p = await get_delivery_prefs(tg_id)
    status = "на паузе" if p.paused else "активна"
    score = int(p.min_score or 0)
    shop = "скрываю" if p.exclude_shop else "показываю"

    text = [
        "⚙️ <b>Сделки и фильтры</b>",
        "",
        "<b>Доставка сделок</b>",
        f"Состояние: <b>{status}</b>",
        "",
        "<b>Фильтр продавцов</b>",
        f"Магазины и перекупы: <b>{shop}</b>",
        f"Мин. надёжность: <b>{score}</b> (0 = выключено)",
        "",
        "<b>Состояния устройств</b>",
        "✅ присылаю · 🚫 не присылаю",
    ]

    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text="▶️ Включить доставку" if p.paused else "⏸ Пауза доставки",
            callback_data="set:pause",
        )
    ], [
        InlineKeyboardButton(text="score −10", callback_data="set:score:-10"),
        InlineKeyboardButton(text=str(score), callback_data="noop"),
        InlineKeyboardButton(text="score +10", callback_data="set:score:10"),
    ], [
        InlineKeyboardButton(
            text="🏬 Магазины: скрывать" if not p.exclude_shop else "🏬 Магазины: показывать",
            callback_data="set:shop",
        )
    ]]

    cond_btns = []
    for c in SELECTABLE_CONDITIONS:
        _emoji, label = CONDITION_RU.get(c, ("", c))
        on = c not in p.exclude_conditions
        cond_btns.append(InlineKeyboardButton(
            text=f"{'✅' if on else '🚫'} {label}",
            callback_data=f"set:cond:{c}",
        ))
    for i in range(0, len(cond_btns), 2):
        rows.append(cond_btns[i:i + 2])
    rows.append([_BACK])
    return "\n".join(text), _kb(rows)


async def screen_status() -> tuple[str, InlineKeyboardMarkup]:
    st = runtime.snapshot()
    d_users = await discovery_users()
    ov = await learning_overview()
    outbox = await pending_alert_stats()
    cycles = st.get("cycles") or {}
    fast = cycles.get("fast") or {}
    deep = cycles.get("deep") or {}
    train = st.get("training") or {}

    lines = [
        "📊 <b>Статус охоты</b>",
        "",
        _mode_title(st, bool(d_users)),
        "Подписки: приостановлены Discovery" if d_users else "Подписки: активны",
        "",
        _cycle_human("Быстрый проход", fast),
    ]
    if fast.get("drop_reasons"):
        lines.append(f"Почему не прислал: {_drop_line(fast.get('drop_reasons'))}.")
    lines += ["", _cycle_human("Deep", deep)]
    if deep.get("drop_reasons"):
        lines.append(f"Почему не прислал: {_drop_line(deep.get('drop_reasons'))}.")
    lines += [
        "",
        f"Обучение: <b>{'идёт, мониторинг сделок остановлен' if st.get('training_mode') else 'выключено'}</b>",
        f"Страница: {train.get('current_page', 0)}/{train.get('max_pages', 0)}; "
        f"собрано card/detail {train.get('card_learned', 0)}/{train.get('detail_learned', 0)}; "
        f"пропущено {train.get('skipped', 0)}.",
        f"Последний успешный шаг: {_ago(train.get('last_success_at'))}.",
        "",
        f"База цен: <b>{ov['with_baseline']}</b> моделей с рабочей ценой.",
        f"Очередь отправки: <b>{outbox['pending']}</b> ждут"
        + (f", <b>{outbox['dead']}</b> требуют разбора." if outbox["dead"] else "."),
    ]
    if not ov["top"]:
        lines.append("База ещё копится: дай боту поработать или запусти обучение.")

    rows = [
        [InlineKeyboardButton(text="📚 База цен", callback_data="nav:learned:0")],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:status"),
            InlineKeyboardButton(text="🛠 Dev", callback_data="nav:dev"),
        ],
        [_BACK],
    ]
    return "\n".join(lines), _kb(rows)


async def screen_dev(raw: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    st = runtime.snapshot()
    d_users = await discovery_users()
    outbox = await pending_alert_stats()
    db = await dev_db_stats()
    cycles = st.get("cycles") or {}
    train = st.get("training") or {}
    runtime_sec = int(time.time() - float(st.get("started_at") or time.time()))

    lines = [
        "🛠 <b>Dev</b>",
        "",
        "<b>Monitor</b>",
        _cycle_raw("Fast Discovery", cycles.get("fast")),
        _cycle_raw("Deep Discovery", cycles.get("deep")),
        _cycle_raw("Train loop", cycles.get("training")),
        f"subscriptions_paused_by_discovery={_bool_ru(bool(d_users))}",
        f"train_active={_bool_ru(bool(st.get('training_mode')))} captcha={_bool_ru(bool(st.get('captcha')))}",
        "",
        "<b>Runtime</b>",
        f"runtime_sec={runtime_sec} last_cycle={_ago(st.get('last_cycle_at'))}",
        f"training page={train.get('current_page', 0)}/{train.get('max_pages', 0)} "
        f"card={train.get('card_learned', 0)} detail={train.get('detail_learned', 0)} "
        f"skipped={train.get('skipped', 0)} devices={train.get('devices', 0)} "
        f"last_error={esc(train.get('last_error') or 'None')}",
        "",
        "<b>Outbox</b>",
        f"pending={outbox['pending']} dead={outbox['dead']} oldest={_ago(outbox.get('oldest'))}",
        f"runtime_outbox={esc(st.get('outbox') or {})}",
        "",
        "<b>Browser</b>",
        f"captcha={_bool_ru(bool(st.get('captcha')))} "
        f"restart_interval={config.BROWSER_RESTART_INTERVAL_SEC}s "
        f"profile_lock=enabled",
        "",
        "<b>Channel</b>",
        f"id={esc(config.DEMO_CHANNEL_ID) if config.DEMO_CHANNEL_ID is not None else 'не настроен'} "
        f"mirror={'ВКЛ' if settings_store.channel_enabled() else 'ВЫКЛ'}",
        "",
        "<b>DB</b>",
        " · ".join(f"{k}={v}" for k, v in db.items()),
    ]
    if raw:
        lines += ["", "<b>Raw snapshot</b>", f"<code>{esc(st)}</code>"]

    rows = [
        [
            InlineKeyboardButton(text="Retry outbox", callback_data="dev:confirm:retry_outbox"),
            InlineKeyboardButton(text="Raw counters", callback_data="dev:raw"),
        ],
        [
            InlineKeyboardButton(text="Train start", callback_data="dev:confirm:train_start"),
            InlineKeyboardButton(text="Train stop", callback_data="dev:confirm:train_stop"),
        ],
        [
            InlineKeyboardButton(text="Browser restart", callback_data="dev:confirm:browser_restart"),
            InlineKeyboardButton(text="Clear dead", callback_data="dev:confirm:clear_dead_outbox"),
        ],
        [
            InlineKeyboardButton(text="Relearn stale", callback_data="dev:confirm:relearn_stale"),
            InlineKeyboardButton(text="Cleanup rows", callback_data="dev:confirm:cleanup_old_rows"),
        ],
    ]
    if config.DEMO_CHANNEL_ID is not None:
        ch_on = settings_store.channel_enabled()
        rows.append([
            InlineKeyboardButton(
                text=("📡 Канал: выключить" if ch_on else "📡 Канал: включить"),
                callback_data="dev:channel",
            )
        ])
    rows.append(
        [InlineKeyboardButton(text="Back to normal UI", callback_data="nav:hub")]
    )
    return "\n".join(lines), _kb(rows)


def screen_dev_confirm(action: str) -> tuple[str, InlineKeyboardMarkup]:
    label = _DEV_ACTIONS.get(action, action)
    text = (
        f"⚠️ <b>{esc(label)}</b>\n\n"
        "Это сервисное действие. Подтверди запуск."
    )
    return text, _kb([
        [InlineKeyboardButton(text="Запустить", callback_data=f"dev:run:{action}")],
        [InlineKeyboardButton(text="Отмена", callback_data="nav:dev")],
    ])


def screen_dev_result(title: str, details: str) -> tuple[str, InlineKeyboardMarkup]:
    return (
        f"🛠 <b>{esc(title)}</b>\n\n{details}",
        _kb([
            [InlineKeyboardButton(text="🔄 Dev", callback_data="nav:dev")],
            [_BACK],
        ]),
    )


async def screen_learned(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    total = await count_working_baselines()
    if total == 0:
        return (
            "📚 <b>База цен пустая</b>\n\n"
            "Бот ещё не накопил достаточно наблюдений. Запусти Discovery "
            "или отдельное обучение /train_start.",
            _kb([[_BACK]]),
        )
    pages = (total + LEARNED_PER_PAGE - 1) // LEARNED_PER_PAGE
    page = max(0, min(page, pages - 1))
    devs = await learned_devices(
        limit=LEARNED_PER_PAGE, offset=page * LEARNED_PER_PAGE
    )

    lines = [f"📚 <b>База цен</b> ({total} моделей)", ""]
    for d in devs:
        lines.append(
            f"• {esc(d['label'])}: <b>{fmt_price(d['price'])}</b> "
            f"(выборка {d['sample']})"
        )
    lines.append("")
    lines.append("<i>Это рабочая рыночная цена по реальным объявлениям.</i>")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"nav:learned:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"nav:learned:{page+1}"))
    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:learned:{page}"),
        _BACK,
    ])
    return "\n".join(lines), _kb(rows)


_TIER_ORDER = (
    ("ideal", "идеал"),
    ("good", "хорошее"),
    ("defect", "с дефектом"),
    ("broken", "битый"),
    ("for_parts", "на запчасти"),
)


async def screen_prices(tg_id: int, sub_id: int) -> tuple[str, InlineKeyboardMarkup]:
    back = _kb([
        [InlineKeyboardButton(text="⬅️ К подпискам", callback_data="nav:subs:0")],
        [_BACK],
    ])
    sub = await get_subscription(tg_id, sub_id)
    if sub is None:
        return "Подписка не найдена.", back
    dev = normalize_device(sub.query)
    if not dev.model:
        return (
            f"💹 <b>Цены: {esc(sub.query)}</b>\n\n"
            "Модель в запросе не распознана. Уточни запрос, например "
            "<code>iphone 15 pro</code>.",
            back,
        )
    variants = await prices_for_model(dev.brand, dev.model)
    head = f"💹 <b>Цены по состояниям: {esc(dev.model)}</b>"
    if not variants:
        return (
            head + "\n\nБаза ещё копится. Бот уточняет цены по реальным объявлениям.",
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
            lines.append(f"  рынок: {fmt_price(p)} (выборка {n})")
        for key, label in _TIER_ORDER:
            if key in tiers:
                p, n = tiers[key]
                lines.append(f"  {label}: {fmt_price(p)} (выборка {n})")
        lines.append("")
    return "\n".join(lines), back


async def screen_quality(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mine = await feedback_stats(tg_id)
    glob = await feedback_stats()
    acc = f"{glob['up'] / glob['total'] * 100:.0f}%" if glob["total"] else "—"
    text = (
        "📈 <b>Качество</b>\n\n"
        "Реакции на сделки помогают понять, насколько шумный сейчас режим.\n\n"
        f"Твои реакции: 👍 {mine['up']} · 👎 {mine['down']}\n"
        f"Всего: 👍 {glob['up']} · 👎 {glob['down']} · точность {acc}\n\n"
        "<b>Глобальные пороги</b>\n"
        f"Мин. профит: {fmt_price(config.MIN_PROFIT_RUB)}\n"
        f"Мин. маржа: {config.MIN_PROFIT_RATIO*100:.0f}%\n"
        f"Мин. ROI: {config.MIN_ROI_RATIO*100:.0f}%\n"
        f"Мин. качество: {config.MIN_DEAL_SCORE}/100\n"
        f"Оверхед на флип: {fmt_price(config.PROFIT_OVERHEAD_RUB)}"
    )
    rows = [
        [InlineKeyboardButton(text="⚙️ Сделки и фильтры", callback_data="nav:settings")],
        [_BACK],
    ]
    return text, _kb(rows)
