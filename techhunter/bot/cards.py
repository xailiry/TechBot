"""Deal card: inline keyboard, photo+caption send, persistent state so the
"Скрипт торга" callback survives a bot restart (Stage 4.3)."""
import logging

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ..ai.images import download_image_bytes
from ..storage import save_card_state
from .format import build_haggle_text, format_deal_card

log = logging.getLogger(__name__)

_TG_CAPTION_LIMIT = 1024


def build_action_kb(item) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔎 Открыть на Avito →", url=item.full_url)],
        [
            InlineKeyboardButton(
                text="✉️ Написать продавцу", url=item.chat_url
            ),
            InlineKeyboardButton(
                text="📋 Скрипт торга", callback_data=f"haggle:{item.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="👍 норм", callback_data=f"fb:up:{item.id}"
            ),
            InlineKeyboardButton(
                text="👎 мимо", callback_data=f"fb:down:{item.id}"
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_deal_card(
    bot: Bot, tg_id: int, item, report, valuation, sub_query: str = ""
) -> None:
    text = format_deal_card(item, report, valuation, sub_query)
    kb = build_action_kb(item)

    photo = None
    img_url = item.image or (item.images[0] if item.images else None)
    if img_url:
        data = await download_image_bytes(img_url)
        if data:
            photo = BufferedInputFile(data, filename="avito.jpg")

    msg = None
    try:
        if photo and len(text) <= _TG_CAPTION_LIMIT:
            msg = await bot.send_photo(
                tg_id, photo=photo, caption=text,
                parse_mode="HTML", reply_markup=kb,
            )
        elif photo:
            await bot.send_photo(tg_id, photo=photo)
            msg = await bot.send_message(
                tg_id, text, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=kb,
            )
        else:
            msg = await bot.send_message(
                tg_id, text, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=kb,
            )
    except Exception as e:
        log.warning("send_deal_card failed for %s: %s", tg_id, e)
        return

    if msg is not None:
        try:
            await save_card_state(
                tg_id,
                msg.message_id,
                item.model_dump(),
                valuation.model_dump(),
                sub_query,
            )
        except Exception as e:
            log.debug("save_card_state failed: %s", e)


def haggle_text(item_dict: dict, valuation_dict: dict) -> str:
    """Rebuild the haggle script from persisted card state."""
    from ..scraper.models import ParsedListing
    from ..valuation.engine import Valuation

    item = ParsedListing(**item_dict)
    val = Valuation(**valuation_dict)
    return build_haggle_text(item, val)
