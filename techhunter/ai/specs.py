"""Regex spec/defect extraction from listing text (title + description +
params). Local, no LLM. Defect codes are structured so Stage 3 can map them
to repair costs and scam signals.

Defect codes:
  screen_cracked      front glass / display physically broken
  back_glass_cracked  rear glass / housing broken
  screen_replaced     non-original display installed
  battery_replaced    non-original / swollen / weak battery, or low health
  faceid_broken       Face ID / Touch ID not working
  truetone_missing    True Tone gone (typical after a screen swap)
  icloud_locked       iCloud / Activation Lock / Google account locked
  not_original_parts  refurbished / restored / non-original components
  replica             counterfeit / 1:1 copy
  no_power            does not turn on / no image / dead
  cosmetic_wear       scratches / dents / chips, declared "with nuances"
"""
import re
from dataclasses import dataclass, field

# ─── battery health ─────────────────────────────────────────────────────────
_BAT_WORD = r"(?:акб|аккумулятор[а-я]*|батаре[ияй]|battery(?:\s*health)?|ё?мкост[ьи](?:\s*акб)?)"
_BAT_AFTER = re.compile(_BAT_WORD + r"[^0-9%]{0,12}(\d{1,3})\s*%?", re.I)
_BAT_BEFORE = re.compile(r"(\d{1,3})\s*%[^0-9]{0,12}" + _BAT_WORD, re.I)

# ─── storage / ram ──────────────────────────────────────────────────────────
_RAM_ROM = re.compile(r"\b(\d{1,2})\s*/\s*(\d{2,4})\s*(?:гб|gb)\b", re.I)
_STORAGE = re.compile(r"\b(\d{2,4})\s*(гб|gb|тб|tb)\b", re.I)
_RAM_ONLY = re.compile(r"\b(?:озу|ram)\s*[:\-]?\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:гб|gb)\s*озу\b", re.I)

_VALID_STORAGE = {16, 32, 64, 128, 256, 512, 1024, 2048}

# ─── defect keyword -> code ─────────────────────────────────────────────────
_DEFECT_PATTERNS: list[tuple[str, str]] = [
    (r"icloud|айклауд|i-cloud|activation lock|залочен|привязан\w* к|"
     r"гугл[ао]?\s*аккаунт|frp", "icloud_locked"),
    (r"на\s*зап\.?части|на\s*донор|по\s*запчаст", "no_power"),
    (r"не\s*включ|не\s*работает\s*(?:вообще|телефон|совсем)|нет\s*изображени|"
     r"не\s*запуска|dead\b|до[нр]ор", "no_power"),
    (r"разби\w*\s*экран|разби\w*\s*дисплей|треснут\w*\s*экран|трещин\w*\s*на\s*экран|"
     r"би[тл]\w*\s*экран|разбит\s*перед|треснут\w*\s*стекл\w*\s*спереди|broken screen|"
     r"cracked screen", "screen_cracked"),
    (r"разби\w*\s*(?:зад|крышк|корпус)|треснут\w*\s*(?:зад|крышк)|"
     r"стекл\w*\s*сзади\s*(?:разб|трес)|би[тл]\w*\s*крышк", "back_glass_cracked"),
    (r"замен\w*\s*(?:экран|дисплей|модул)|неоригинал\w*\s*(?:экран|дисплей)|"
     r"дисплей\s*не\s*родн|копийн\w*\s*экран", "screen_replaced"),
    (r"замен\w*\s*(?:акб|аккумулятор|батаре)|акб\s*(?:под\s*замен|сла[бч]|"
     r"быстро\s*сад|вздут)|батаре\w*\s*вздут|нужн\w*\s*замен\w*\s*акб", "battery_replaced"),
    (r"face\s*id\s*(?:не\s*раб|сломан|нет|отсутств)|"
     r"не\s*работает\s*face|тач\s*айди\s*не|touch\s*id\s*не", "faceid_broken"),
    (r"тру\s*тон\w*\s*(?:нет|отсутств|не\s*раб)|нет\s*tru[e]?\s*tone|"
     r"tru[e]?\s*tone\s*(?:отсутств|нет|off)|без\s*truetone", "truetone_missing"),
    (r"реф\b|рефаб|refurb|восстановл\w*|восстановк|неоригинал|не\s*родн\w*\s*запчаст",
     "not_original_parts"),
    (r"копи[яйи]\b|реплик|1\s*[:в]\s*1|люкс\s*копи|fake\b|подделк", "replica"),
    # Wear words are guarded against negation ("без царапин", "нет сколов")
    # so a clean lot is not falsely downgraded. Calibrated in Stage 5.
    (r"(?<!без )(?<!нет )(?:царапин|потертост|потёртост|скол\w*|вмятин)|"
     r"сост\w*\s*на\s*фото|есть\s*нюанс|по\s*состоян\w*\s*нюанс|"
     r"трещин(?!\w*\s*экран)", "cosmetic_wear"),
]
_DEFECT_RE = [(re.compile(p, re.I), code) for p, code in _DEFECT_PATTERNS]

_ROSTEST_RE = re.compile(r"ростест|rst\b|для\s*рф\b|росси[йяи]\w*\s*верс", re.I)
_SEALED_RE = re.compile(r"запечатан|новый\s*в\s*плёнк|новый\s*в\s*пленк|sealed|"
                        r"не\s*вскрыт|новый,?\s*не\s*актив", re.I)

_COLOR_KEYWORDS = {
    "черный": "black", "чёрный": "black", "белый": "white", "синий": "blue",
    "голубой": "blue", "красный": "red", "зеленый": "green", "зелёный": "green",
    "желтый": "yellow", "жёлтый": "yellow", "фиолетовый": "purple",
    "розовый": "pink", "серый": "gray", "графит": "graphite",
    "титан": "titanium", "золотой": "gold", "midnight": "midnight",
}


@dataclass
class DeviceSpecs:
    storage_gb: int | None = None
    ram_gb: int | None = None
    battery_health: int | None = None
    color: str | None = None
    defects: set[str] = field(default_factory=set)
    is_rostest: bool = False
    is_sealed: bool = False


def _extract_battery(text: str) -> int | None:
    for rx in (_BAT_AFTER, _BAT_BEFORE):
        for m in rx.finditer(text):
            try:
                v = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= v <= 100:
                return v
    return None


_GB_RE = re.compile(r"(\d{1,4})\s*(гб|gb|тб|tb)", re.I)


def _gb(value: str) -> int | None:
    m = _GB_RE.search(value or "")
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2).lower() in ("тб", "tb"):
        n *= 1024
    return n


def _storage_ram_from_params(
    params: dict[str, str],
) -> tuple[int | None, int | None]:
    """Prefer the structured "Характеристики" block - far more reliable
    than parsing "12/256" out of the title."""
    storage = ram = None
    for k, v in params.items():
        key = k.lower()
        if "памят" not in key:
            continue
        if "оператив" in key or "озу" in key:
            g = _gb(v)
            if g and 1 <= g <= 64:
                ram = g
        elif "встроен" in key or "устройств" in key or key.strip() == "память":
            g = _gb(v)
            if g in _VALID_STORAGE:
                storage = g
    return storage, ram


def _extract_storage_ram(text: str) -> tuple[int | None, int | None]:
    storage = ram = None
    if (m := _RAM_ROM.search(text)):
        ram = int(m.group(1))
        s = int(m.group(2))
        if s in _VALID_STORAGE:
            storage = s
    if storage is None:
        for m in _STORAGE.finditer(text):
            val = int(m.group(1))
            unit = m.group(2).lower()
            if unit in ("тб", "tb"):
                val *= 1024
            if val in _VALID_STORAGE:
                storage = val
                break
    if ram is None and (m := _RAM_ONLY.search(text)):
        g = m.group(1) or m.group(2)
        if g:
            r = int(g)
            if 1 <= r <= 24:
                ram = r
    return storage, ram


def extract_specs(
    title: str, description: str = "", params: dict[str, str] | None = None
) -> DeviceSpecs:
    params = params or {}
    text = " ".join(
        [title or "", description or "", " ".join(f"{k} {v}" for k, v in params.items())]
    ).lower()

    specs = DeviceSpecs()
    # Structured params first (e.g. "Встроенная память: 256 ГБ",
    # "Оперативная память: 12 ГБ"), then fall back to title/text regex.
    p_storage, p_ram = _storage_ram_from_params(params)
    t_storage, t_ram = _extract_storage_ram(text)
    specs.storage_gb = p_storage if p_storage is not None else t_storage
    specs.ram_gb = p_ram if p_ram is not None else t_ram
    specs.battery_health = _extract_battery(text)
    specs.is_rostest = bool(_ROSTEST_RE.search(text))
    specs.is_sealed = bool(_SEALED_RE.search(text))

    for rx, code in _DEFECT_RE:
        if rx.search(text):
            specs.defects.add(code)

    from .. import config

    if (
        specs.battery_health is not None
        and specs.battery_health <= config.BATTERY_DEFECT_THRESHOLD
    ):
        specs.defects.add("battery_replaced")

    for word, norm in _COLOR_KEYWORDS.items():
        if word in text:
            specs.color = norm
            break

    return specs
