"""Tiny in-process runtime snapshot the bot's Status screen reads.

The monitor writes here each poll; the Telegram side reads it. No DB, no
locks needed (single event loop, last-write-wins is fine for a status view).
"""
import copy
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
    "cycles": {
        "fast": {},
        "deep": {},
        "training": {},
    },
    "captcha": False,
    "training_mode": False,
    "training_pid": None,
    "training": {
        "active": False,
        "started_at": None,
        "stopped_at": None,
        "current_page": 0,
        "max_pages": 0,
        "card_learned": 0,
        "detail_learned": 0,
        "skipped": 0,
        "devices": 0,
        "last_success_at": 0.0,
        "last_error": None,
    },
    "outbox": {
        "pending": 0,
        "dead": 0,
        "last_retry_at": 0.0,
        "last_sent": 0,
        "last_error": None,
    },
}


def update_cycle(mode: str = "fast", status: str = "success", **kw) -> None:
    now = time.time()
    payload = {"last_at": now, "status": status, **kw}
    cycles = _STATE.setdefault("cycles", {})
    cycles[mode] = payload
    _STATE.update(last_cycle_at=now, **kw)


def set_captcha(active: bool) -> None:
    _STATE["captcha"] = active


def set_training_mode(active: bool) -> None:
    _STATE["training_mode"] = active
    tr = _STATE.setdefault("training", {})
    tr["active"] = active
    if active:
        tr["started_at"] = time.time()
        tr["stopped_at"] = None
        tr["last_error"] = None
    else:
        tr["stopped_at"] = time.time()


def is_training_mode() -> bool:
    return _STATE.get("training_mode", False)


def update_training(**kw) -> None:
    tr = _STATE.setdefault("training", {})
    tr.update(kw)


def update_outbox(**kw) -> None:
    out = _STATE.setdefault("outbox", {})
    out.update(kw)


def snapshot() -> dict:
    return copy.deepcopy(_STATE)
