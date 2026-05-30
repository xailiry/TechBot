"""TelegramNotifier: delivers captcha alerts to the operator and deal cards
to subscribers. Implements the same Notifier protocol as ConsoleNotifier."""
import asyncio
import contextlib
import logging

from aiogram import Bot

from .. import config
from ..notifier import _beep
from ..storage import all_user_ids
from .cards import send_deal_card
from .format import onboarding_done_text, onboarding_started_text

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot: Bot, beep: bool = True) -> None:
        self.bot = bot
        self._beep = beep

    async def _operator_ids(self) -> list[int]:
        if config.ADMIN_USER_IDS:
            return list(config.ADMIN_USER_IDS)
        return await all_user_ids()

    async def _broadcast(self, text: str) -> tuple[int, int]:
        delivered = 0
        failed = 0
        for uid in await self._operator_ids():
            try:
                await self.bot.send_message(uid, text, parse_mode="HTML")
                delivered += 1
            except Exception as e:
                failed += 1
                log.warning("Telegram broadcast failed for %s: %s", uid, e)
        return delivered, failed

    async def captcha_detected(self, url: str) -> None:
        log.error("CAPTCHA detected. Manual solve needed. %s", url)
        if self._beep:
            await asyncio.to_thread(_beep)
        delivered, _failed = await self._broadcast(
            "🧩 <b>Капча на Avito</b>\nРеши её вручную в открытом окне "
            "браузера — монитор продолжит сам."
        )
        if delivered == 0:
            raise RuntimeError("captcha notification was not delivered")

    async def captcha_cleared(self) -> None:
        log.info("Captcha cleared, monitor resumed.")
        await self._broadcast("✅ Капча решена, монитор продолжает работу.")

    async def new_listing(self, item) -> None:
        # Deliberately silent: only opportunities are pushed to users.
        return

    async def deal(
        self, item, report, valuation, *, tg_id=None, sub_query=None
    ) -> None:
        if tg_id is None:
            return
        await send_deal_card(
            self.bot, tg_id, item, report, valuation, sub_query or ""
        )

    async def onboarding_started(
        self, tg_id: int, query: str, eta_sec: int
    ) -> None:
        with contextlib.suppress(Exception):
            await self.bot.send_message(
                tg_id, onboarding_started_text(query, eta_sec),
                parse_mode="HTML",
            )

    async def onboarding_finished(
        self, tg_id: int, query: str, summary: list
    ) -> None:
        with contextlib.suppress(Exception):
            await self.bot.send_message(
                tg_id, onboarding_done_text(query, summary),
                parse_mode="HTML",
            )

    async def watchdog(self, text: str) -> None:
        await self._broadcast(f"🛠 <b>Внимание</b>\n{text}")
