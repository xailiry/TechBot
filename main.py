"""TechHunter Bot entrypoint: runs the Telegram bot and the Avito monitor
concurrently. Schema is managed by Alembic migrations."""
import asyncio
import logging

from aiogram.exceptions import TelegramNetworkError

from techhunter.bot.app import build_bot
from techhunter.bot.notifier import TelegramNotifier
from techhunter.config import require_bot_token
from techhunter.db import dispose_engine
from techhunter.logging_config import setup_logging
from techhunter.monitor import run_forever
from techhunter.scraper.browser import shutdown_browser

log = logging.getLogger("techhunter.main")

# api.telegram.org can be briefly unreachable (throttling, a flaky link, or
# regional filtering -> WinError 121). Such a blip must NOT kill the process
# and drag the Avito monitor down with it, so each half runs under its own
# supervisor and is restarted after a short pause.
RETRY_SEC = 15


async def _supervise(name: str, factory) -> None:
    while True:
        try:
            await factory()
            return  # clean, intentional stop
        except asyncio.CancelledError:
            raise
        except TelegramNetworkError as e:
            log.warning(
                "%s: Telegram unreachable (%s). Retrying in %ss.",
                name, e, RETRY_SEC,
            )
            await asyncio.sleep(RETRY_SEC)
        except Exception:
            log.exception("%s crashed. Restarting in %ss.", name, RETRY_SEC)
            await asyncio.sleep(RETRY_SEC)


async def _main() -> None:
    setup_logging()
    require_bot_token()

    bot, dp = build_bot()
    notifier = TelegramNotifier(bot)
    log.info("Starting bot polling + Avito monitor.")
    try:
        await asyncio.gather(
            _supervise(
                "Telegram polling",
                lambda: dp.start_polling(bot, handle_signals=False),
            ),
            _supervise("Avito monitor", lambda: run_forever(notifier)),
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
