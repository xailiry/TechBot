"""Tiny in-process runtime snapshot the bot's Status screen reads.

The monitor writes here each poll; the Telegram side reads it. No DB, no
locks needed (single event loop, last-write-wins is fine for a status view).
"""
import time

_STATE: dict = {
    "started_at": time.time(),
    "last_cycle_at": 0.0,
    "subs": 0,
    "scraped": 0,
    "processed": 0,   # ran the pipeline this cycle
    "cached": 0,       # reused a cached evaluation
    "errors": 0,       # transient pipeline failures (will retry)
    "deals": 0,
    "filtered": 0,
    "drop_reasons": {},  # reason -> count (why a deal was not delivered)
    "captcha": False,
    "training_mode": False,
    "training_pid": None,
}


def update_cycle(**kw) -> None:
    _STATE.update(last_cycle_at=time.time(), **kw)


def set_captcha(active: bool) -> None:
    _STATE["captcha"] = active


def set_training_mode(active: bool) -> None:
    _STATE["training_mode"] = active


def is_training_mode() -> bool:
    return _STATE.get("training_mode", False)


def snapshot() -> dict:
    return dict(_STATE)
