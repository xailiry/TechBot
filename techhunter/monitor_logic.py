"""Pure logic for monitor health checks."""
from . import config

def watchdog_decide(snap: dict, state: dict, now: float) -> list[str]:
    """Given a runtime snapshot + persistent state, return health alerts."""
    alerts: list[str] = []
    last = state.setdefault("last_alert", {})

    def _emit(key: str, text: str) -> None:
        if now - last.get(key, 0.0) >= config.WATCHDOG_REPEAT_SEC:
            last[key] = now
            alerts.append(text)

    captcha = bool(snap.get("captcha"))
    lca = snap.get("last_cycle_at") or 0.0
    if not captcha and lca > 0 and now - lca >= config.WATCHDOG_STALL_SEC:
        _emit("stall", (
            f"Монитор не завершал цикл ~{int((now - lca) // 60)} мин. "
            "Похоже, завис."
        ))

    subs = snap.get("subs") or 0
    scraped = snap.get("scraped") or 0
    if captcha or subs == 0 or scraped > 0:
        state["dry_since"] = None
    else:
        ds = state.get("dry_since")
        if ds is None:
            state["dry_since"] = now
        elif now - ds >= config.WATCHDOG_DRY_SEC:
            _emit("dry", (
                f"Уже ~{int((now - ds) // 60)} мин ничего не собирается "
                "при активных подписках. Возможен бан IP или смена "
                "вёрстки Avito - проверь окно браузера."
            ))
    return alerts
