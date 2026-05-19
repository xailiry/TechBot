"""Notification sink. Stage 1 ships a console/beep notifier; Stage 4 plugs in
the Telegram implementation behind the same Protocol."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .ai.evaluate import EvaluationReport
    from .scraper.models import ParsedListing
    from .valuation.engine import Valuation

log = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    async def captcha_detected(self, url: str) -> None: ...
    async def captcha_cleared(self) -> None: ...
    async def new_listing(self, item: ParsedListing) -> None: ...
    async def deal(
        self,
        item: ParsedListing,
        report: EvaluationReport,
        valuation: Valuation,
        *,
        tg_id: int | None = None,
        sub_query: str | None = None,
    ) -> None: ...
    async def onboarding_started(
        self, tg_id: int, query: str, eta_sec: int
    ) -> None: ...
    async def onboarding_finished(
        self, tg_id: int, query: str, summary: list
    ) -> None: ...
    async def watchdog(self, text: str) -> None: ...


def _beep() -> None:
    # Windows-only audible alert so the operator notices the captcha.
    if sys.platform != "win32":
        return
    try:
        import winsound

        for _ in range(3):
            winsound.Beep(880, 250)
    except Exception:
        pass


class ConsoleNotifier:
    """Default Stage 1 notifier: loud logs + a beep on captcha."""

    def __init__(self, beep: bool = True) -> None:
        self._beep = beep

    async def captcha_detected(self, url: str) -> None:
        log.error(
            "CAPTCHA / BLOCK detected. Solve it manually in the browser window. "
            "URL: %s",
            url,
        )
        if self._beep:
            await asyncio.to_thread(_beep)

    async def captcha_cleared(self) -> None:
        log.info("Captcha cleared, resuming monitor.")

    async def new_listing(self, item: ParsedListing) -> None:
        log.info(
            "NEW %s | %s RUB | %s | %s",
            item.id, item.price, item.title[:70], item.full_url,
        )

    async def deal(
        self, item, report, valuation, *, tg_id=None, sub_query=None
    ) -> None:
        net = valuation.net_profit
        pct = (
            f" ({valuation.profit_pct*100:.0f}%)"
            if valuation.profit_pct is not None
            else ""
        )
        repair = (
            f" repair={valuation.repair_cost}"
            if valuation.repair_cost is not None
            else ""
        )
        log.warning(
            "DEAL [%s] %s | price=%s baseline=%s net=%s%s%s | scam=%s | %s",
            valuation.opportunity_type,
            valuation.device_key or item.title[:50],
            item.price,
            valuation.baseline_price,
            net,
            pct,
            repair,
            valuation.scam_verdict,
            item.full_url,
        )

    async def onboarding_started(
        self, tg_id: int, query: str, eta_sec: int
    ) -> None:
        log.info("ONBOARDING start tg=%s %r eta~%ss", tg_id, query, eta_sec)

    async def onboarding_finished(
        self, tg_id: int, query: str, summary: list
    ) -> None:
        log.info("ONBOARDING done tg=%s %r: %d devices priced",
                 tg_id, query, len(summary))

    async def watchdog(self, text: str) -> None:
        log.warning("WATCHDOG: %s", text)
