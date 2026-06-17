"""Tiny persistent key-value store for runtime-toggleable settings.

The bot and the monitor run in one process / one event loop, so a module-level
cache is shared between them: a toggle from a Telegram handler is seen by the
poller on its next cycle without re-reading the file. The JSON file only keeps
the value across restarts. Deliberately not in the DB: one boolean flag does
not warrant a table + migration.
"""
import json
import logging

from . import config

log = logging.getLogger(__name__)

_PATH = config.DATA_DIR / "app_settings.json"
_cache: dict | None = None

# Channel mirror: deals are posted to DEMO_CHANNEL_ID when this is on. Default
# follows whether a channel is configured at all.
_CHANNEL_KEY = "channel_enabled"


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    try:
        _PATH.write_text(
            json.dumps(_load(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning("settings_store: failed to persist %s: %s", _PATH, e)


def channel_enabled() -> bool:
    """Whether deals are mirrored to the demo channel. Off automatically when
    no channel is configured, otherwise the stored toggle (default on)."""
    if config.DEMO_CHANNEL_ID is None:
        return False
    return bool(_load().get(_CHANNEL_KEY, True))


def set_channel_enabled(value: bool) -> None:
    _load()[_CHANNEL_KEY] = bool(value)
    _save()
