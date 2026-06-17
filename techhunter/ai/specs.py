"""Regex spec/defect extraction from listing text (title + description +
params). Local, no LLM. Defect codes are structured so Stage 3 can map them
to repair costs and scam signals.

Defect codes:
  screen_cracked      front glass / display physically broken
  back_glass_cracked  rear glass / housing broken
  screen_replaced     non-original display installed
  battery_replaced    seller says the battery was already replaced
  battery_worn        weak/swollen battery or low reported health
  battery_suspicious  health/cycle values look reprogrammed or implausible
  faceid_broken       Face ID / Touch ID not working
  truetone_missing    True Tone gone (typical after a screen swap)
  icloud_locked       iCloud/Activation Lock/Google lock, or otherwise
                      unusable: blocked by scammers, stolen / blacklisted
                      ("в розыске", "чёрный список"), no network registration
  not_original_parts  refurbished / restored / non-original components
  replica             counterfeit / 1:1 copy
  no_power            does not turn on / no image / dead
  screen_display_defect display matrix issue: stripes / dead pixels / flicker
  cosmetic_wear       scratches / dents / chips, declared "with nuances"
"""
import re
from dataclasses import dataclass, field

from ..textnorm import normalize_homoglyphs

# --- battery health ---------------------------------------------------------
_BAT_WORD = r"(?:акб|аккумулятор[а-я]*|батаре[ияй]|battery(?:\s*health)?|ё?мкост[ьи](?:\s*акб)?)"
_BAT_AFTER = re.compile(_BAT_WORD + r"[^0-9%]{0,12}(\d{1,3})\s*%?", re.I)
_BAT_BEFORE = re.compile(r"(\d{1,3})\s*%[^0-9]{0,5}" + _BAT_WORD, re.I)

_CYCLES_RE = re.compile(
    r"(?:цикл\w*|cycle\w*|зарядок\b)[^\d]{0,10}(\d{1,4})|(\d{1,4})\s*(?:цикл\w*|cycle\w*|зарядок\b)",
    re.I,
)


def _extract_cycles(text: str) -> int | None:
    for m in _CYCLES_RE.finditer(text):
        g1, g2 = m.group(1), m.group(2)
        v_str = g1 or g2
        if v_str:
            try:
                v = int(v_str)
                if 1 <= v <= 9999:
                    return v
            except ValueError:
                continue
    return None


# --- storage / ram ----------------------------------------------------------
_RAM_ROM = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,4})\s*(гб|gb|тб|tb)\b", re.I)
_STORAGE = re.compile(r"\b(\d{1,4})\s*(гб|gb|тб|tb)\b", re.I)
_RAM_ONLY = re.compile(r"\b(?:озу|ram)\s*[:\-]?\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:гб|gb)\s*озу\b", re.I)

_VALID_STORAGE = {16, 32, 64, 128, 256, 512, 1024, 2048}

# ─── defect keyword -> code ─────────────────────────────────────────────────
_DEFECT_PATTERNS: list[tuple[str, str]] = [
    (r"icloud|айклауд|i-cloud|activation lock|залочен|привязан\w* к|"
     r"гугл[ао]?\s*аккаунт|frp", "icloud_locked"),
    (r"\br[\s\-]?sim\b|\bр[\s\-]?сим\b|\bсим[\s\-]?лок\b|\bsim[\s\-]?lock\b|\bsimlock\b|"
     r"\bmdm\b|\bмдм\b|\bgevey\b|\bгевей\b|\bгивей\b|турбосим|турбо[\s\-]?сим|\bldu\b|"
     r"\bдемо\b|обход\s*активац|\bbypass\b|заблокирован\s+(?:на|под)\s+оператора",
     "carrier_locked"),
    # Blocked / stolen / blacklisted / no-network = unusable phone, NOT a
    # working bargain. Bait scam: cheap price, "идеал", but the device is
    # bricked. Routed to for_parts via icloud_locked + scam penalty.
    (r"заблокир\w*\s+мошенник|мошенник\w*\s+(?:за)?блокир\w*|"
     r"\bв\s*розыске\b|укра[дл]\w*|ворован\w*|"
     r"бл[эе]к[\s\-]?лист|black[\s\-]?list|ч[её]рн\w*\s+списк\w*|"
     r"imei\s+(?:заблокир|в\s*розыске|в\s*ч[её]рн)|"
     r"проблем\w*\s+с\s+сет\w*|не\s+(?:ловит|вид[ия]т|работает)\s+сет\w*|"
     r"нет\s+сет[ьи]\b|не\s+регистрир\w*\s+(?:в\s+)?сет|не\s+работает\s+сим",
     "icloud_locked"),
    (r"на\s*зап\.?части|на\s*донор|по\s*запчаст", "no_power"),
    (r"не\s*включ|не\s*работает\s*(?:вообще|телефон|совсем)|нет\s*изображени|"
     r"не\s*запуска|dead\b|до[нр]ор|утопленн\w*|после\s*воды|залит\w*|"
     r"попал[ао]?\s*вод[аы]", "no_power"),
    (r"разби\w*\s*экран|разби\w*\s*диспл\w*|треснут\w*\s*экран|трещин\w*\s*на\s*экран|"
     r"би[тл]\w*\s*экран|разбит\s*перед|треснут\w*\s*стекл\w*\s*спереди|broken screen|"
     r"cracked screen", "screen_cracked"),
    (r"(?:экран|диспл\w*|матриц\w*)[^.\n]{0,60}(?:полос\w*|бит\w*\s*пиксел\w*|пиксел\w*\s*бит\w*|мерца\w*|морга\w*|"
     r"зелен[а-я]*\s+экран|бел[а-я]*\s+экран|черн[а-я]*\s+экран|засвет\w*|не\s*работа\w*)|"
     r"(?:полос\w*|бит\w*\s*пиксел\w*|пиксел\w*\s*бит\w*|мерца\w*|морга\w*)[^.\n]{0,60}(?:экран|диспл\w*|матриц\w*)|"
     r"(?:есть|имеетс[яь]|появил\w*|появля\w*)[^.\n]{0,25}полос[аы]\b|"
     r"\b(?:битые|битый|мертвые|мёртвые|dead)\s+пиксел\w*\b|"
     r"\bне\s+работа\w*[^.\n]{0,25}(?:экран|диспл\w*|матриц\w*|тач|сенсор)\b|"
     r"\b(?:тач|сенсор|touch)\s+не\s+работа\w*\b|"
     r"\b(?:экран|диспл\w*)\s+полосит\b",
     "screen_display_defect"),
    (r"разби\w*\s*(?:зад|крышк|корпус)|треснут\w*\s*(?:зад|крышк)|"
     r"(?:задн\w*\s*)?стекл\w*[^.\n]{0,40}(?:разб|трес|трещ)|"
     r"(?:крышк|задник)[^.\n]{0,40}трещин|"
     r"стекл\w*\s*сзади\s*(?:разб|трес)|би[тл]\w*\s*крышк", "back_glass_cracked"),
    (r"пот[её]рт\w*|царапин\w*|скол\w*|коцк\w*|замятин\w*|"
     r"след\w*\s+использован|есть\s+нюанс\w*", "cosmetic_wear"),
    (r"замен\w*\s*(?:экран\w*|диспл\w*|модул\w*)|неоригинал\w*\s*(?:экран\w*|диспл\w*)|"
     r"(?:менял\w*|поменя\w*)\s*(?:экран\w*|диспл\w*|модул\w*)|"
     r"диспл\w*\s*не\s*родн|копийн\w*\s*экран|"
     r"(?:экран\w*|диспл\w*)\s*(?:замен|менял|поменя|неоригинал|не\s*родн|аналог)", "screen_replaced"),
    (r"(?:заменил\w*|поменял\w*|установил\w*)\s*"
     r"(?:акб|аккумулятор|батаре)|"
     r"(?:акб|аккумулятор|батаре\w*)\s*"
     r"(?:заменен|заменён|заменили|поменян|поменяли|новый)", "battery_replaced"),
    (r"акб\s*(?:под\s*замен|сла[бч]|быстро\s*сад|вздут)|"
     r"батаре\w*\s*(?:вздут|сла[бч]|быстро\s*сад)|"
     r"нужн\w*\s*замен\w*\s*(?:акб|аккумулятор|батаре)", "battery_worn"),
    (r"face\s*id\s*(?:не\s*раб|сломан|нет|отсутств|ошибк)|"
     r"не\s*работает\s*face|тач\s*айди\s*не|touch\s*id\s*не|отпечаток\s*(?:не\s*раб|не\s*скан)", "faceid_broken"),
    (r"тру\s*тон\w*\s*(?:нет|отсутств|не\s*раб)|нет\s*tru[e]?\s*tone|"
     r"tru[e]?\s*tone\s*(?:отсутств|нет|off)|без\s*truetone", "truetone_missing"),
    (r"выгор\w*\s*экран|экран\s*выгор|пятн[оа]\s*на\s*экран|выгоревш\w*\s*пиксел", "screen_replaced"),
    (r"реф\b|рефаб|refurb|восстановл\w*|восстановк|неоригинал|не\s*родн\w*\s*запчаст",
     "not_original_parts"),
    (r"копи[яйи]\b|реплик|1\s*[:в]\s*1|люкс\s*копи|fake\b|подделк|паль\b|закос\b|муляж\b", "replica"),
]

_DEFECT_RE = [(re.compile(p, re.I), code) for p, code in _DEFECT_PATTERNS]

# Negated "it's fine" claims removed before the damage scan. Does NOT
# touch "не родной"/"неоригинал"/"замена ..." (those are real defects).
_NEG_CLEAN = re.compile(
    r"\bне\s+мен[яеёи]\w*"
    r"|\bбез\s+замен\w+"
    r"|\bне\s+(?:разб\w+|треснут\w+|бит\w+|колот\w+)"
    r"|\bбез\s+(?:r[\s\-]?sim|р[\s\-]?сим|mdm|мдм|сим[\s\-]?лок|sim[\s\-]?lock|чип\w*|обход\w*)\b"
    r"|\bне\s+(?:залочен\w*|заблокирован\w*)"
    r"|\bне\s+(?:реф\b|рефаб|восстановл\w*)"
    r"|\bне\s+(?:копи\w*|реплик\w*|подделк\w*)"
    r"|\bбез\s+(?:царапин\w*|сколов|дефектов|нюансов)\b"
    r"|\b(?:царапин|сколов|дефектов|нюансов)\s+нет\b"
    r"|\b(?:нет|без|никаких)\s+проблем\w*"
    r"|\b(?:нет|без|никаких)\s+(?:полос\w*|бит\w*\s*пиксел\w*)"
    r"(?:\s*(?:и|,)\s*(?:полос\w*|бит\w*\s*пиксел\w*))*"
    r"|\b(?:полос\w*|бит\w*\s*пиксел\w*)\s+нет\b"
    r"|\bneverlock\b|\bневерлок\b",
    re.I,
)

_ROSTEST_RE = re.compile(r"ростест|rst\b|для\s*рф\b|росси[йяи]\w*\s*верс", re.I)
_SEALED_RE = re.compile(r"запечатан|новый\s*в\s*плёнк|новый\s*в\s*пленк|sealed|"
                        r"не\s*вскрыт|новый,?\s*не\s*актив", re.I)
_NEW_RE = re.compile(r"\bнов(?:ый|ая|ое|ые)\b|\bnew\b", re.I)
_LIKE_NEW_RE = re.compile(r"\bкак\s+нов(?:ый|ая|ое|ые)\b", re.I)

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
    battery_cycles: int | None = None
    color: str | None = None
    defects: set[str] = field(default_factory=set)
    is_rostest: bool = False
    is_sealed: bool = False
    is_new: bool = False


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
        if m.group(3).lower() in ("тб", "tb"):
            s *= 1024
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
    # Normalize Latin-lookalike homoglyphs first: spam listings disguise
    # "замена дисплея" as "зaмена диcплeя" to dodge defect detection.
    text = normalize_homoglyphs(
        " ".join(
            [title or "", description or "",
             " ".join(f"{k} {v}" for k, v in params.items())]
        )
    ).lower()

    specs = DeviceSpecs()
    # Structured params first (e.g. "Встроенная память: 256 ГБ",
    # "Оперативная память: 12 ГБ"), then fall back to title/text regex.
    p_storage, p_ram = _storage_ram_from_params(params)
    t_storage, t_ram = _extract_storage_ram(text)
    specs.storage_gb = p_storage if p_storage is not None else t_storage
    specs.ram_gb = p_ram if p_ram is not None else t_ram
    specs.battery_health = _extract_battery(text)
    specs.battery_cycles = _extract_cycles(text)
    specs.is_rostest = bool(_ROSTEST_RE.search(text))
    specs.is_sealed = bool(_SEALED_RE.search(text))
    new_text = _LIKE_NEW_RE.sub(" ", text)
    specs.is_new = specs.is_sealed or bool(_NEW_RE.search(new_text))

    # Neutralize explicit "it is fine" claims so they do not trigger
    # physical-damage defects ("экран не менялся", "без замены дисплея",
    # "не разбит/не битый"). True defects ("не родной", "неоригинал",
    # "замена дисплея") are untouched.
    defect_text = _NEG_CLEAN.sub(" ", text)
    for rx, code in _DEFECT_RE:
        if rx.search(defect_text):
            specs.defects.add(code)



    earliest_idx = -1
    best_color = None
    for word, norm in _COLOR_KEYWORDS.items():
        idx = text.find(word)
        if idx != -1:
            if earliest_idx == -1 or idx < earliest_idx:
                earliest_idx = idx
                best_color = norm
    specs.color = best_color

    return specs
