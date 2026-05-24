"""TechHunter Bot entrypoint: runs the Telegram bot and the Avito monitor
concurrently. Schema is managed by Alembic migrations."""
import asyncio
import contextlib
import logging
import sys

from aiogram.exceptions import TelegramNetworkError

from techhunter.bot.app import build_bot
from techhunter.bot.notifier import TelegramNotifier
from techhunter.config import DATA_DIR, require_bot_token
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


@contextlib.contextmanager
def _single_instance_lock():
    """Keep one bot process per workspace.

    The visible browser uses one persistent Chrome profile; two bot processes
    fighting for it make Playwright fail with a noisy TargetClosedError.
    """
    lock_path = DATA_DIR / "techhunter.lock"
    lock_file = lock_path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise RuntimeError("TechHunter Bot is already running.") from e
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise RuntimeError("TechHunter Bot is already running.") from e
        yield
    finally:
        with contextlib.suppress(Exception):
            lock_file.close()


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
        with _single_instance_lock():
            asyncio.run(_main())
    except RuntimeError as e:
        print(e)
    except KeyboardInterrupt:
        pass
