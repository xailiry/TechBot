"""aiogram bot: button-first UI with a single-message hub, Back everywhere,
guided add, per-user delivery filters. Commands remain as shortcuts."""
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import config
from ..storage import (
    add_subscription,
    get_card_state,
    record_feedback,
    remove_subscription,
    set_exclude_shop,
    set_min_score,
    set_paused,
    toggle_exclude_condition,
    upsert_user,
)
from .cards import haggle_text
from .format import esc, fmt_price, parse_command
from .screens import (
    hub_kb,
    screen_add,
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

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = await screen_hub(msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


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


async def _save_subscription(tg_id: int, body: str) -> str | None:
    """Parse + persist. Returns a confirmation text, or None if unparseable."""
    parsed = parse_command(body)
    if not parsed:
        return None
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
        f"Локация: {city}\n\n"
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

@dp.message(Command("train_start"))
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


@dp.message(Command("train_stop"))
async def cmd_train_stop(msg: Message) -> None:
    from .. import runtime
    
    if not runtime.is_training_mode():
        await msg.answer("Обучение не запущено.")
        return
        
    runtime.set_training_mode(False)
    await msg.answer("🛑 <b>Режим обучения остановлен.</b>\nОбычная работа монитора восстановлена.", parse_mode="HTML")


@dp.message(Command("train_stat"))
async def cmd_train_stat(msg: Message) -> None:
    try:
        from techhunter.db.session import async_session_factory
        from sqlalchemy import text
        
        async with async_session_factory() as session:
            # How many items were learned during training (source='training')
            res = await session.execute(text("SELECT count(*) FROM price_observations WHERE source='training'"))
            obs_count = res.scalar() or 0
            
            res = await session.execute(text("SELECT count(*) FROM device_catalog"))
            dev_count = res.scalar() or 0
            
            res = await session.execute(text("SELECT count(*) FROM listings"))
            cache_count = res.scalar() or 0
            
            status = "🏃 <b>Запущено</b>" if (from_runtime := __import__("techhunter.runtime", fromlist=["is_training_mode"])).is_training_mode() else "⏸ <b>Остановлено</b>"
            
            await msg.answer(
                f"📊 <b>Статистика обучения:</b>\n"
                f"Статус: {status}\n\n"
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
    await cb.answer()


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    dest = parts[1]
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
    await cb.answer()


@dp.callback_query(F.data.startswith("sub:del:"))
async def cb_sub_del(cb: CallbackQuery) -> None:
    _, _, sid, page = cb.data.split(":")
    ok = await remove_subscription(cb.from_user.id, int(sid))
    await cb.answer("Удалено" if ok else "Не найдено")
    await _edit(cb, await screen_subs(cb.from_user.id, int(page)))


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery) -> None:
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
    await cb.answer("Сохранено")


@dp.callback_query(F.data.startswith("disc:"))
async def cb_discovery(cb: CallbackQuery) -> None:
    from ..db.models import User
    from ..storage import (
        get_session,
        set_discovery_enabled,
        set_discovery_profit,
    )

    await upsert_user(cb.from_user.id, cb.from_user.username)
    parts = cb.data.split(":")
    action = parts[1]
    tg = cb.from_user.id

    if action in ("on", "off"):
        enable = action == "on"
        await set_discovery_enabled(tg, enable)
        await cb.answer("Discovery включён" if enable else "Discovery выключен")
    elif action == "profit":
        delta = int(parts[2])
        async with get_session() as s:
            u = await s.get(User, tg)
            cur = u.discovery_min_profit_rub if u else config.DISCOVERY_MIN_PROFIT_RUB
        new = max(0, cur + delta)
        await set_discovery_profit(tg, rub=new)
        await cb.answer(f"Мин. профит: {new}₽")
    elif action == "ratio":
        delta = int(parts[2])  # percentage points
        async with get_session() as s:
            u = await s.get(User, tg)
            cur = u.discovery_min_profit_ratio if u else config.DISCOVERY_MIN_PROFIT_RATIO
        new = min(0.9, max(0.0, round(cur + delta / 100, 2)))
        await set_discovery_profit(tg, ratio=new)
        await cb.answer(f"Мин. маржа: {int(new * 100)}%")
    else:
        await cb.answer()
        return
    await _edit(cb, await screen_discovery(cb.from_user.id))



# ─── Guided add (FSM) ───────────────────────────────────────────────────────

@dp.callback_query(F.data == "add:start")
async def cb_add_start(cb: CallbackQuery, state: FSMContext) -> None:
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
    await cb.answer()


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
        await cb.answer("Данные карточки устарели.", show_alert=True)
        return
    try:
        text = haggle_text(st["item"], st["valuation"])
    except Exception as e:
        log.debug("haggle build failed: %s", e)
        await cb.answer("Не удалось собрать скрипт.", show_alert=True)
        return
    await cb.message.reply(
        f"📋 <b>Скрипт для копирования:</b>\n\n{text}", parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("fb:"))
async def cb_feedback(cb: CallbackQuery) -> None:
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.answer()
        return
    direction = "up" if parts[1] == "up" else "down"
    await record_feedback(cb.from_user.id, parts[2], direction)
    await cb.answer(
        "Спасибо, учту 👍" if direction == "up" else "Понял, мимо 👎"
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
