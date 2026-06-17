"""One-shot diagnostic: can the bot post to DEMO_CHANNEL_ID?

Run from the project folder with the venv active:
    python test_channel.py

It prints the channel info, the bot's membership/rights in the channel, and
the result of an actual test post. Use the output to fix channel setup.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
RAW = (os.getenv("DEMO_CHANNEL_ID") or "").strip()


def parse_chat_id(raw: str):
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return raw


async def main():
    from aiogram import Bot

    if not TOKEN:
        print("BOT_TOKEN is empty in .env"); return
    chat_id = parse_chat_id(RAW)
    if chat_id is None:
        print("DEMO_CHANNEL_ID is empty in .env"); return
    print(f"DEMO_CHANNEL_ID = {chat_id!r}")

    bot = Bot(TOKEN)
    try:
        me = await bot.get_me()
        print(f"Bot: @{me.username} (id={me.id})")

        try:
            chat = await bot.get_chat(chat_id)
            print(f"get_chat OK: id={chat.id} type={chat.type} title={chat.title!r}")
        except Exception as e:
            print(f"get_chat FAILED: {type(e).__name__}: {e}")
            print("-> Channel id is wrong, or the bot is not a member of the channel.")
            return

        try:
            member = await bot.get_chat_member(chat_id, me.id)
            print(f"Bot status in channel: {member.status} | "
                  f"can_post_messages={getattr(member, 'can_post_messages', None)}")
        except Exception as e:
            print(f"get_chat_member FAILED: {type(e).__name__}: {e}")

        try:
            msg = await bot.send_message(
                chat_id,
                "TechHunter: тестовое сообщение в канал. "
                "Видишь его -> права на постинг есть, всё ок.",
            )
            print(f"SEND OK: message_id={msg.message_id}  <-- check the channel")
        except Exception as e:
            print(f"SEND FAILED: {type(e).__name__}: {e}")
            print("-> Add the bot as an ADMIN of the channel with the "
                  "'Post Messages' right, then run this again.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
