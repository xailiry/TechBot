"""aiogram bot: button-first UI with a single-message hub, Back everywhere,
guided add, per-user delivery filters. Commands remain as shortcuts."""
import logging

from aiogram import Bot, Dispatcher, F
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
    screen_help,
    screen_hub,
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
    except Exception as e:  # message unchanged / too old -> resend
        log.debug("edit failed (%s), resending", e)
        await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ─── Commands (shortcuts) ───────────────────────────────────────────────────

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await upsert_user(msg.from_user.id, msg.from_user.username)
    text, kb = screen_hub()
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
        await _edit(cb, screen_hub())
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
    elif action == "shop":
        from ..storage import get_delivery_prefs
        cur = await get_delivery_prefs(tg)
        await set_exclude_shop(tg, not cur.exclude_shop)
    elif action == "score":
        from ..storage import get_delivery_prefs
        cur = await get_delivery_prefs(tg)
        await set_min_score(tg, cur.min_score + int(parts[2]))
    elif action == "cond":
        await toggle_exclude_condition(tg, parts[2])
    await _edit(cb, await screen_settings(tg))
    await cb.answer("Сохранено")


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
    text, kb = screen_hub()
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


def build_bot() -> tuple[Bot, Dispatcher]:
    return Bot(token=config.require_bot_token()), dp
