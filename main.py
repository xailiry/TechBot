"""TechHunter Bot entrypoint: runs the Telegram bot and the Avito monitor
concurrently. Schema is managed by Alembic; create_all here is a safety net
for first run."""
import asyncio
import logging

from techhunter.bot.app import build_bot
from techhunter.bot.notifier import TelegramNotifier
from techhunter.config import require_bot_token
from techhunter.db import create_all, dispose_engine
from techhunter.logging_config import setup_logging
from techhunter.monitor import run_forever
from techhunter.scraper.browser import shutdown_browser


async def _main() -> None:
    setup_logging()
    log = logging.getLogger("techhunter.main")
    require_bot_token()
    await create_all()

    bot, dp = build_bot()
    notifier = TelegramNotifier(bot)
    log.info("Starting bot polling + Avito monitor.")
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            run_forever(notifier),
        )
    finally:
        await shutdown_browser()
        await dispose_engine()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
