"""aiogram bot: button-first UI with a single-message hub, Back everywhere,
guided add, per-user delivery filters. Commands remain as shortcuts."""
import asyncio
import contextlib
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .. import config
from ..storage import (
    add_subscription,
    feedback_features,
    get_card_state,
    is_user_approved,
    record_feedback,
    remove_subscription,
    set_approved,
    set_exclude_shop,
    set_min_score,
    set_paused,
    toggle_exclude_condition,
    toggle_subscription_paused,
    upsert_user,
)
from .cards import haggle_text
from .format import esc, fmt_price, parse_command
from .screens import (
    DISCOVERY_PRESETS,
    hub_kb,
    screen_add,
    screen_dev,
    screen_dev_confirm,
    screen_dev_result,
    screen_discovery,
    screen_help,
    screen_hub,
    screen_learned,
    screen_prices,
    screen_quality,
    screen_settings,
    screen_status,
    screen_subs,
)

log = logging.getLogger(__name__)
dp = Dispatcher()

FEEDBACK_REASONS: tuple[tuple[str, str], ...] = (
    ("too_expensive", "дорого / мало профита"),
    ("reseller", "магазин / перекуп"),
    ("reseller_rebuild", "пересобранный перекупом"),
    ("condition", "состояние хуже"),
    ("battery", "АКБ / замена батареи"),
    ("wrong_model", "не та модель / память"),
    ("scam", "мошенник / мутно"),
    ("duplicate", "дубль / уже видел"),
    ("other", "другое"),
)


class AdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in config.ADMIN_USER_IDS)


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        
        # 1. Admins bypass everything.
        if user.id in config.ADMIN_USER_IDS:
            return await handler(event, data)
        
        # 2. Approved users pass.
        if await is_user_approved(user.id):
            return await handler(event, data)
        
        # 3. Deny others.
        if isinstance(event, Message):
            await event.answer("⛔ У вас нет доступа к этому боту.")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        return

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


class AddSub(StatesGroup):
    waiting = State()


async def _edit(cb: CallbackQuery, built: tuple) -> None:
    text, kb = built
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        log.debug("edit failed (%s), resending", e)
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)

    except Exception as e:  # message unchanged / too old -> resend
        log.debug("edit failed (%s), resending", e)
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ─── Commands (shortcuts) ───────────────────────────────────────────────────

async def _answer_cb(cb: CallbackQuery, *args, **kwargs) -> None:
    """Answer callback queries without noisy logs for expired Telegram clicks."""
    try:
        await cb.answer(*args, **kwargs)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if (
            "query is too old" in msg
            or "response timeout expired" in msg
            or "query id is invalid" in msg
        ):
            log.debug("callback answer expired: %s", e)
            return
        raise


async def _answer_msg(msg: Message, *args, retries: int = 2, **kwargs) -> None:
    """Retry transient Telegram send failures so commands do not look ignored."""
    for attempt in range(retries + 1):
        try:
            await msg.answer(*args, **kwargs)
            return
        except TelegramNetworkError as e:
            if attempt >= retries:
                log.warning("message answer failed after %s attempts: %s", attempt + 1, e)
                raise
            delay = float(attempt + 1)
            log.warning("message answer failed, retrying in %.1fs: %s", delay, e)
            await asyncio.sleep(delay)


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = await screen_hub(msg.from_user.id)
    await _answer_msg(msg, text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    text, kb = screen_help()
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("list"))
async def cmd_list(msg: Message) -> None:
    text, kb = await screen_subs(msg.from_user.id, 0)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("settings"))
async def cmd_settings(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = await screen_settings(msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    text, kb = await screen_status()
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("dev"), AdminFilter())
async def cmd_dev(msg: Message) -> None:
    text, kb = await screen_dev()
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("quality"))
async def cmd_quality(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = await screen_quality(msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("retry_pending"), AdminFilter())
async def cmd_retry_pending(msg: Message) -> None:
    from ..monitor import _retry_pending_alerts
    from .notifier import TelegramNotifier

    sent = await _retry_pending_alerts(TelegramNotifier(msg.bot))
    await msg.answer(f"🔁 Outbox retry: отправлено {sent}.")


@dp.message(Command("channel"), AdminFilter())
async def cmd_channel(msg: Message) -> None:
    from .. import settings_store

    if config.DEMO_CHANNEL_ID is None:
        await msg.answer("Канал не настроен: задай DEMO_CHANNEL_ID в .env.")
        return
    args = (msg.text or "").split()
    arg = args[1].lower() if len(args) > 1 else ""
    if arg in ("on", "вкл", "1"):
        settings_store.set_channel_enabled(True)
    elif arg in ("off", "выкл", "0"):
        settings_store.set_channel_enabled(False)
    elif arg:
        await msg.answer("Использование: <code>/channel on</code> или <code>/channel off</code>", parse_mode="HTML")
        return
    else:
        settings_store.set_channel_enabled(not settings_store.channel_enabled())
    state = "ВКЛ" if settings_store.channel_enabled() else "ВЫКЛ"
    await msg.answer(f"📡 Постинг сделок в канал: <b>{state}</b>", parse_mode="HTML")


@dp.message(Command("allow"), AdminFilter())
async def cmd_allow(msg: Message) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await msg.answer("Использование: <code>/allow 12345678</code>", parse_mode="HTML")
        return
    tg_id = int(args[1])
    await upsert_user(tg_id, f"approved_{tg_id}")
    await set_approved(tg_id, True)
    await msg.answer(f"✅ Пользователь <code>{tg_id}</code> получил доступ к боту.", parse_mode="HTML")


@dp.message(Command("revoke"), AdminFilter())
async def cmd_revoke(msg: Message) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await msg.answer("Использование: <code>/revoke 12345678</code>", parse_mode="HTML")
        return
    tg_id = int(args[1])
    await set_approved(tg_id, False)
    await msg.answer(f"❌ Доступ пользователя <code>{tg_id}</code> отозван.", parse_mode="HTML")


async def _save_subscription(tg_id: int, body: str) -> str | None:
    """Parse + persist. Returns a confirmation text, or None if unparseable."""
    parsed = parse_command(body)
    if not parsed:
        return None
    # An unknown Cyrillic city would silently break the Avito URL (the slug
    # map only covers major cities); fall back to whole-country search and
    # tell the user instead of returning an empty subscription forever.
    city_warning = ""
    if not parsed["city_slug"].isascii():
        city_warning = (
            f"\n⚠️ Город «{esc(parsed['city_slug'])}» не распознан, "
            "ищу по всей России. Можно указать slug с Avito, "
            "например <code>rostov-na-donu</code>."
        )
        parsed["city_slug"] = "rossiya"
    sub_id = await add_subscription(
        tg_id,
        parsed["query"],
        city_slug=parsed["city_slug"],
        min_price=parsed["min_price"],
        max_price=parsed["max_price"],
        min_battery=parsed["min_battery"],
    )
    log.info("USER %s add #%s %r", tg_id, sub_id, parsed["query"])
    city = ("Вся Россия" if parsed["city_slug"] == "rossiya"
            else parsed["city_slug"].capitalize())
    return (
        f"✅ <b>Подписка #{sub_id} создана</b>\n\n"
        f"Запрос: <b>{esc(parsed['query'])}</b>\n"
        f"Цена: {fmt_price(parsed['min_price']) if parsed['min_price'] else '—'}"
        f" – {fmt_price(parsed['max_price']) if parsed['max_price'] else '—'}\n"
        f"Мин. АКБ: {parsed['min_battery'] or '—'}%\n"
        f"Локация: {city}{city_warning}\n\n"
        "Запускаю анализ рынка по этой модели."
    )


@dp.message(Command("add"))
async def cmd_add(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    body = (msg.text or "").removeprefix("/add").strip()
    if not body:
        text, kb = screen_add()
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    conf = await _save_subscription(msg.from_user.id, body)
    if conf is None:
        await msg.answer(
            "Не понял запрос. Пример:\n"
            "<code>iphone 13 pro | до:55000 | москва</code>",
            parse_mode="HTML", reply_markup=screen_add()[1],
        )
        return
    await msg.answer(conf, parse_mode="HTML", reply_markup=hub_kb())


@dp.message(Command("pause"))
async def cmd_pause(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    await set_paused(msg.from_user.id, True)
    await msg.answer("⏸ Доставка на паузе.", reply_markup=hub_kb())


@dp.message(Command("resume"))
async def cmd_resume(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    await set_paused(msg.from_user.id, False)
    await msg.answer("▶️ Доставка включена.", reply_markup=hub_kb())


@dp.message(Command("del"))
@dp.message(Command("remove"))
async def cmd_del(msg: Message) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await msg.answer("Использование: <code>/del 12</code> (id из /list)",
                         parse_mode="HTML")
        return
    ok = await remove_subscription(msg.from_user.id, int(args[1]))
    await msg.answer("✅ Удалено" if ok else "Такой подписки у тебя нет.",
                     reply_markup=hub_kb())


@dp.message(Command("discovery"))
async def cmd_discovery(msg: Message) -> None:
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = await screen_discovery(msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("discovery_profit"))
async def cmd_discovery_profit(msg: Message) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await msg.answer("Пример: <code>/discovery_profit 10000</code>", parse_mode="HTML")
        return
    from ..storage import set_discovery_profit
    await set_discovery_profit(msg.from_user.id, rub=int(args[1]))
    await msg.answer(f"✅ Минимальный профит Discovery изменен на {args[1]}₽", reply_markup=hub_kb())


@dp.message(Command("discovery_ratio"))
async def cmd_discovery_ratio(msg: Message) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].replace("%", "").isdigit():
        await msg.answer("Пример: <code>/discovery_ratio 25</code>", parse_mode="HTML")
        return
    val = float(args[1].replace("%", "")) / 100
    from ..storage import set_discovery_profit
    await set_discovery_profit(msg.from_user.id, ratio=val)
    await msg.answer(f"✅ Минимальная маржа Discovery изменена на {int(val*100)}%", reply_markup=hub_kb())


# ─── Training Mode ──────────────────────────────────────────────────────────

@dp.message(Command("train_start"), AdminFilter())
async def cmd_train_start(msg: Message) -> None:
    from .. import runtime
    
    if runtime.is_training_mode():
        await msg.answer("⚠️ Обучение уже запущено.")
        return
        
    try:
        runtime.set_training_mode(True)
        await msg.answer("🚀 <b>Режим обучения запущен!</b>\nПодписки приостановлены. Бот сканирует рынок в фоновом процессе.", parse_mode="HTML")
    except Exception as e:
        await msg.answer(f"❌ Ошибка запуска: {e}")


@dp.message(Command("train_stop"), AdminFilter())
async def cmd_train_stop(msg: Message) -> None:
    from .. import runtime
    
    if not runtime.is_training_mode():
        await msg.answer("Обучение не запущено.")
        return
        
    runtime.set_training_mode(False)
    await msg.answer("🛑 <b>Режим обучения остановлен.</b>\nОбычная работа монитора восстановлена.", parse_mode="HTML")


@dp.message(Command("train_stat"), AdminFilter())
async def cmd_train_stat(msg: Message) -> None:
    try:
        from .. import runtime
        from techhunter.db.session import async_session_factory
        from sqlalchemy import text
        
        async with async_session_factory() as session:
            # How many items were learned during training.
            res = await session.execute(text("SELECT count(*) FROM price_observations WHERE source IN ('training', 'training_detail')"))
            obs_count = res.scalar() or 0
            res = await session.execute(text("SELECT count(*) FROM device_catalog"))
            dev_count = res.scalar() or 0
            
            res = await session.execute(text("SELECT count(*) FROM listings"))
            cache_count = res.scalar() or 0
            
            snap = runtime.snapshot()
            tr = snap.get("training") or {}
            status = "🏃 <b>Запущено</b>" if runtime.is_training_mode() else "⏸ <b>Остановлено</b>"
            
            await msg.answer(
                f"📊 <b>Статистика обучения:</b>\n"
                f"Статус: {status}\n\n"
                f"Страница: <b>{tr.get('current_page', 0)}/{tr.get('max_pages', 0)}</b>\n"
                f"Последний шаг: card <b>{tr.get('card_learned', 0)}</b>, "
                f"detail <b>{tr.get('detail_learned', 0)}</b>, "
                f"skipped <b>{tr.get('skipped', 0)}</b>\n"
                f"Последняя ошибка: <b>{esc(tr.get('last_error') or 'нет')}</b>\n\n"
                f"📱 Известно устройств: <b>{dev_count}</b>\n"
                f"💰 Собрано новых цен: <b>{obs_count}</b>\n"
                f"🗃 Просмотрено карточек (общее): <b>{cache_count}</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        await msg.answer(f"Ошибка чтения БД: {e}")




# ─── Navigation router ──────────────────────────────────────────────────────

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await _answer_cb(cb)


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    dest = parts[1]
    if dest == "dev" and cb.from_user.id not in config.ADMIN_USER_IDS:
        await _answer_cb(cb, "Только для владельца", show_alert=True)
        return
    await _answer_cb(cb)
    if dest == "hub":
        await state.clear()
        await _edit(cb, await screen_hub(cb.from_user.id))
    elif dest == "help":
        await _edit(cb, screen_help())
    elif dest == "add":
        await _edit(cb, screen_add())
    elif dest == "subs":
        page = int(parts[2]) if len(parts) > 2 else 0
        await _edit(cb, await screen_subs(cb.from_user.id, page))
    elif dest == "settings":
        await _edit(cb, await screen_settings(cb.from_user.id))
    elif dest == "status":
        await _edit(cb, await screen_status())
    elif dest == "dev":
        await _edit(cb, await screen_dev())
    elif dest == "learned":
        page = int(parts[2]) if len(parts) > 2 else 0
        await _edit(cb, await screen_learned(page))
    elif dest == "discovery":
        await _edit(cb, await screen_discovery(cb.from_user.id))
    elif dest == "quality":
        await _edit(cb, await screen_quality(cb.from_user.id))
    elif dest == "prices":
        sid = int(parts[2]) if len(parts) > 2 else 0
        await _edit(cb, await screen_prices(cb.from_user.id, sid))


@dp.callback_query(F.data.startswith("sub:del:"))
async def cb_sub_del(cb: CallbackQuery) -> None:
    _, _, sid, page = cb.data.split(":")
    ok = await remove_subscription(cb.from_user.id, int(sid))
    await _answer_cb(cb, "Удалено" if ok else "Не найдено")
    await _edit(cb, await screen_subs(cb.from_user.id, int(page)))


@dp.callback_query(F.data.startswith("sub:pause:"))
async def cb_sub_pause(cb: CallbackQuery) -> None:
    _, _, sid, page = cb.data.split(":")
    new_state = await toggle_subscription_paused(cb.from_user.id, int(sid))
    if new_state is None:
        await _answer_cb(cb, "Не найдено")
    else:
        await _answer_cb(cb, "Подписка на паузе" if new_state else "Поиск возобновлён")
    await _edit(cb, await screen_subs(cb.from_user.id, int(page)))


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery) -> None:
    await _answer_cb(cb)
    parts = cb.data.split(":")
    action = parts[1]
    tg = cb.from_user.id
    if action == "pause":
        from ..storage import is_paused
        await set_paused(tg, not await is_paused(tg))
        await _edit(cb, await screen_settings(tg))
    elif action == "shop":
        from ..storage import get_delivery_prefs
        cur = await get_delivery_prefs(tg)
        await set_exclude_shop(tg, not cur.exclude_shop)
        await _edit(cb, await screen_settings(tg))
    elif action == "score":
        from ..storage import get_delivery_prefs
        cur = await get_delivery_prefs(tg)
        await set_min_score(tg, cur.min_score + int(parts[2]))
        await _edit(cb, await screen_settings(tg))
    elif action == "cond":
        await toggle_exclude_condition(tg, parts[2])
        await _edit(cb, await screen_settings(tg))


@dp.callback_query(F.data.startswith("disc:"))
async def cb_discovery(cb: CallbackQuery) -> None:
    from ..db.models import User
    from ..storage import (
        get_session,
        set_discovery_enabled,
        set_discovery_profit,
        set_discovery_scan_mode,
    )

    await upsert_user(cb.from_user.id, cb.from_user.username)
    parts = cb.data.split(":")
    action = parts[1]
    tg = cb.from_user.id

    if action in ("on", "off"):
        enable = action == "on"
        await set_discovery_enabled(tg, enable)
        await _answer_cb(cb, "Discovery включён" if enable else "Discovery выключен")
    elif action == "profit":
        delta = int(parts[2])
        async with get_session() as s:
            u = await s.get(User, tg)
            cur = u.discovery_min_profit_rub if u else config.DISCOVERY_MIN_PROFIT_RUB
        new = max(0, cur + delta)
        await set_discovery_profit(tg, rub=new)
        await _answer_cb(cb, f"Мин. профит: {new}₽")
    elif action == "ratio":
        delta = int(parts[2])  # percentage points
        async with get_session() as s:
            u = await s.get(User, tg)
            cur = u.discovery_min_profit_ratio if u else config.DISCOVERY_MIN_PROFIT_RATIO
        new = min(0.9, max(0.0, round(cur + delta / 100, 2)))
        await set_discovery_profit(tg, ratio=new)
        await _answer_cb(cb, f"Мин. маржа: {int(new * 100)}%")
    elif action == "preset":
        preset = parts[2] if len(parts) > 2 else "balance"
        label, rub, ratio = DISCOVERY_PRESETS.get(
            preset, DISCOVERY_PRESETS["balance"]
        )
        await set_discovery_profit(tg, rub=rub, ratio=ratio)
        await _answer_cb(cb, f"Discovery: {label}")
    elif action == "mode":
        mode = parts[2] if len(parts) > 2 else "fast"
        await set_discovery_scan_mode(tg, mode)
        label = "Быстрый" if mode == "fast" else "Аккуратный"
        await _answer_cb(cb, f"Темп Discovery: {label}")
    else:
        await _answer_cb(cb)
        return
    await _edit(cb, await screen_discovery(cb.from_user.id))


@dp.callback_query(F.data == "dev:raw", AdminFilter())
async def cb_dev_raw(cb: CallbackQuery) -> None:
    await _edit(cb, await screen_dev(raw=True))


@dp.callback_query(F.data == "dev:channel", AdminFilter())
async def cb_dev_channel(cb: CallbackQuery) -> None:
    from .. import settings_store

    if config.DEMO_CHANNEL_ID is None:
        await _answer_cb(cb, "Канал не настроен (DEMO_CHANNEL_ID пуст)", show_alert=True)
        return
    new_state = not settings_store.channel_enabled()
    settings_store.set_channel_enabled(new_state)
    await _answer_cb(cb, "Постинг в канал включён" if new_state else "Постинг в канал выключен")
    await _edit(cb, await screen_dev())


@dp.callback_query(F.data.startswith("dev:confirm:"), AdminFilter())
async def cb_dev_confirm(cb: CallbackQuery) -> None:
    await _answer_cb(cb)
    action = cb.data.split(":", 2)[2]
    await _edit(cb, screen_dev_confirm(action))


@dp.callback_query(F.data.startswith("dev:run:"), AdminFilter())
async def cb_dev_run(cb: CallbackQuery) -> None:
    await _answer_cb(cb, "Запускаю")
    action = cb.data.split(":", 2)[2]
    try:
        details = await _run_dev_action(action, cb.bot)
        await _edit(cb, screen_dev_result("Готово", details))
    except Exception as e:
        log.exception("dev action failed: %s", action)
        await _edit(cb, screen_dev_result("Ошибка", f"<code>{esc(e)}</code>"))


async def _run_dev_action(action: str, bot: Bot) -> str:
    if action == "retry_outbox":
        from ..monitor import _retry_pending_alerts
        from .notifier import TelegramNotifier

        sent = await _retry_pending_alerts(TelegramNotifier(bot))
        return f"Outbox retry: отправлено <b>{sent}</b>."
    if action == "train_start":
        from .. import runtime

        runtime.set_training_mode(True)
        return "Training mode включён. Мониторинг сделок остановлен."
    if action == "train_stop":
        from .. import runtime

        runtime.set_training_mode(False)
        return "Training mode выключен. Обычная работа восстановится следующим циклом."
    if action == "browser_restart":
        from ..scraper.browser import shutdown_browser

        await shutdown_browser()
        return "Браузер остановлен. Монитор создаст новый контекст на следующем обращении."
    if action == "clear_dead_outbox":
        from ..storage import clear_dead_pending_alerts

        deleted = await clear_dead_pending_alerts()
        return f"Удалено dead outbox rows: <b>{deleted}</b>."
    if action == "relearn_stale":
        from ..valuation.devices import relearn_stale_baselines

        res = await relearn_stale_baselines()
        return (
            f"Проверено: <b>{res['checked']}</b>. "
            f"Пересчитано: <b>{res['relearned']}</b>."
        )
    if action == "cleanup_old_rows":
        from ..storage import cleanup_old_rows

        res = await cleanup_old_rows()
        rows = "\n".join(f"{esc(k)}: <b>{v}</b>" for k, v in res.items())
        return rows or "Нечего чистить."
    return f"Неизвестное действие: <code>{esc(action)}</code>"



# ─── Guided add (FSM) ───────────────────────────────────────────────────────

@dp.callback_query(F.data == "add:start")
async def cb_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await _answer_cb(cb)
    await upsert_user(cb.from_user.id, cb.from_user.username)
    await state.set_state(AddSub.waiting)
    await state.update_data(
        mid=cb.message.message_id, chat=cb.message.chat.id
    )
    await cb.message.edit_text(
        "✍️ Пришли запрос одним сообщением.\n\n"
        "<code>запрос | до:цена | город | акб:мин%</code>\n"
        "Пример: <code>iphone 13 pro | до:55000 | москва</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖️ Отмена", callback_data="nav:hub")
        ]]),
    )


@dp.message(StateFilter(AddSub.waiting), F.text & ~F.text.startswith("/"))
async def fsm_add_text(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    conf = await _save_subscription(msg.from_user.id, (msg.text or "").strip())
    if conf is None:
        await msg.answer(
            "Не понял. Формат: <code>запрос | до:цена | город | акб:%</code>\n"
            "Пример: <code>iphone 13 pro | до:55000</code>",
            parse_mode="HTML",
        )
        return  # stay in state for a retry
    await state.clear()
    if data.get("mid") and data.get("chat"):
        try:
            await msg.bot.edit_message_text(
                conf, chat_id=data["chat"], message_id=data["mid"],
                parse_mode="HTML", reply_markup=hub_kb(),
            )
            return
        except Exception:
            pass
    await msg.answer(conf, parse_mode="HTML", reply_markup=hub_kb())


# ─── Deal card callback ─────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("haggle:"))
async def cb_haggle(cb: CallbackQuery) -> None:
    st = await get_card_state(cb.from_user.id, cb.message.message_id)
    if not st:
        await _answer_cb(cb, "Данные карточки устарели.", show_alert=True)
        return
    try:
        text = haggle_text(st["item"], st["valuation"])
    except Exception as e:
        log.debug("haggle build failed: %s", e)
        await _answer_cb(cb, "Не удалось собрать скрипт.", show_alert=True)
        return
    await cb.message.reply(
        f"📋 <b>Скрипт для копирования:</b>\n\n{text}", parse_mode="HTML"
    )
    await _answer_cb(cb)


@dp.callback_query(F.data.startswith("fb:"))
async def cb_feedback(cb: CallbackQuery) -> None:
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await _answer_cb(cb)
        return
    action = parts[1]
    if action == "down":
        await _answer_cb(cb, "Выбери причину")
        await cb.message.reply(
            "👎 <b>Почему мимо?</b>\n\n"
            "Причина попадёт в личную калибровку. Если такие промахи "
            "повторяются, бот начнёт душить похожие лоты.",
            parse_mode="HTML",
            reply_markup=_feedback_reason_kb(cb.message.message_id),
        )
        return
    if action == "reason":
        rest = parts[2].split(":", 1)
        if len(rest) != 2 or not rest[0].isdigit():
            await _answer_cb(cb)
            return
        source_mid = int(rest[0])
        reason = rest[1]
        await _record_card_feedback(cb.from_user.id, source_mid, "down", reason)
        label = dict(FEEDBACK_REASONS).get(reason, reason)
        await _answer_cb(cb, f"Запомнил: {label}")
        with contextlib.suppress(Exception):
            await cb.message.edit_text(
                f"👎 Запомнил причину: <b>{esc(label)}</b>",
                parse_mode="HTML",
            )
        return
    if action == "up":
        await _record_card_feedback(cb.from_user.id, cb.message.message_id, "up")
        await _answer_cb(cb, "Спасибо, учту 👍")
        return
    await _answer_cb(cb)


def _feedback_reason_kb(message_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(FEEDBACK_REASONS), 2):
        row = []
        for reason, label in FEEDBACK_REASONS[i:i + 2]:
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"fb:reason:{message_id}:{reason}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _record_card_feedback(
    tg_id: int,
    message_id: int,
    reaction: str,
    reason: str | None = None,
) -> None:
    st = await get_card_state(tg_id, message_id)
    if not st:
        await record_feedback(tg_id, str(message_id), reaction, reason=reason)
        return

    listing_id = st.get("item", {}).get("id") or str(message_id)
    features = None
    if st.get("report") is not None:
        try:
            from ..ai.evaluate import EvaluationReport
            from ..scraper.models import ParsedListing
            from ..valuation.engine import Valuation

            item = ParsedListing(**st["item"])
            report = EvaluationReport(**st["report"])
            valuation = Valuation(**st["valuation"])
            features = feedback_features(item, report, valuation)
        except Exception as e:
            log.debug("feedback feature snapshot failed: %s", e)
    await record_feedback(
        tg_id, listing_id, reaction, reason=reason, features=features
    )


# ─── Fallback (no spam loop) ────────────────────────────────────────────────

@dp.message(F.text)
async def fallback(msg: Message) -> None:
    text, kb = await screen_hub(msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)



def build_bot() -> tuple[Bot, Dispatcher]:
    token = config.require_bot_token()
    if config.TELEGRAM_PROXY:
        from aiogram.client.session.aiohttp import AiohttpSession
        log.info("Bot: using proxy %s for Telegram API", config.TELEGRAM_PROXY)
        session = AiohttpSession(proxy=config.TELEGRAM_PROXY)
        return Bot(token=token, session=session), dp
    return Bot(token=token), dp
