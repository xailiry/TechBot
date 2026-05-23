"""Normalize a free-text listing into a canonical device identity.

Scope: iPhone + flagship Android (Samsung Galaxy S/Z/Note, Google Pixel),
per the MVP segment. Unknown models return brand-only / None so the pipeline
still works and Stage 3 can skip valuation gracefully.
"""
import re
from dataclasses import dataclass

from ..textnorm import normalize_homoglyphs

_IPHONE_RE = re.compile(
    r"(?:iphone|айфон|айф)\s*"
    r"(1[0-6]|[5-9]|se|xr|xs|x)\s*"
    r"(pro\s*max|pro|plus|mini|max|про\s*макс|про|плюс|мини|макс)?",
    re.I,
)
_SAMSUNG_S_RE = re.compile(
    r"(?:galaxy|галакси|samsung|самсунг)?\s*\bs\s*?(\d{2})\s*"
    r"(ultra|plus|fe|ультра|\+)?",
    re.I,
)
_SAMSUNG_A_RE = re.compile(
    r"(?:galaxy|галакси|samsung|самсунг)?\s*\ba\s*?(\d{2})\s*"
    r"(s|core|ultra|plus|fe|\+)?",
    re.I,
)
_SAMSUNG_Z_RE = re.compile(
    r"(?:galaxy|галакси)?\s*z\s*(fold|flip|фолд|флип)\s*(\d)", re.I
)
_SAMSUNG_NOTE_RE = re.compile(
    r"(?:galaxy\s*)?note\s*(\d{1,2})\s*(ultra|ультра)?", re.I
)
_PIXEL_RE = re.compile(
    r"(?:pixel|пиксель)\s*(\d{1,2})\s*(pro\s*xl|pro|xl|a)?", re.I
)
_XIAOMI_RE = re.compile(
    r"(?:xiaomi|сяоми|сиаоми|ми)\s*(\d{1,2})\s*(t|pro|ultra|lite|i|x)?", re.I
)
_REDMI_RE = re.compile(
    r"(?:redmi|редми)\s*(note\s*)?(\d{1,2})\s*(pro\s*\+|pro|plus|s|t|a|c)?",
    re.I,
)
_POCO_RE = re.compile(
    r"(?:poco|поко)\s*([xfm][\d])\s*(pro|gt)?", re.I
)
_ONEPLUS_RE = re.compile(
    r"(?:oneplus|ванплас|1plus)\s*(\d{1,2})\s*(pro|t|r|rt)?", re.I
)
_HONOR_RE = re.compile(
    r"(?:honor|хонор)\s*(\d{2,3})\s*(pro|lite|x|i)?", re.I
)
_HUAWEI_RE = re.compile(
    r"(?:huawei|хуавей)\s*(p|mate)\s*(\d{2,3})\s*(pro\s*\+|pro|lite)?", re.I
)

_PARTS_TITLE_RE = re.compile(
    r"(?:^|\b)(?:на\s+запчасти|разбор|донор|пустая\s*коробка)\b|"
    r"^(?:оригинальный\s*|оригинальная\s*|новый\s*|новая\s*)?(?:материнская\s*плата|плата|дисплей|экран|шлейф|корпус|коробка|чехол|защитное\s*стекло|стекло)\b",
    re.I,
)

_BRAND_WORDS = {
    "apple": ("iphone", "айфон", "айф", "apple", "эпл", "эппл"),
    "samsung": ("samsung", "самсунг", "galaxy", "галакси"),
    "google": ("pixel", "пиксель", "google pixel"),
    "xiaomi": ("xiaomi", "сяоми", "сиаоми", "mi", "ми ", "redmi", "редми", "poco", "поко"),
    "oneplus": ("oneplus", "ванплас", "1plus"),
    "honor": ("honor", "хонор"),
    "huawei": ("huawei", "хуавей"),
}

_VARIANT_NORM = {
    "про макс": "Pro Max", "промакс": "Pro Max", "pro max": "Pro Max",
    "promax": "Pro Max", "про": "Pro", "pro": "Pro", "плюс": "Plus",
    "plus": "Plus", "мини": "mini", "mini": "mini", "макс": "Max",
    "max": "Max", "ультра": "Ultra", "ultra": "Ultra", "fe": "FE",
    "+": "Plus", "лайт": "Lite", "lite": "Lite", "т": "T", "t": "T",
}



@dataclass
class CanonicalDevice:
    brand: str  # apple / samsung / google / unknown
    model: str | None  # e.g. "iPhone 13 Pro Max", None if not recognized
    storage_gb: int | None = None
    ram_gb: int | None = None
    raw_title: str = ""

    @property
    def key(self) -> str:
        parts = [self.brand, self.model or "?"]
        if self.storage_gb:
            parts.append(f"{self.storage_gb}GB")
        return " ".join(parts)


def _detect_brand(text: str) -> str:
    for brand, words in _BRAND_WORDS.items():
        if any(w in text for w in words):
            return brand
    return "unknown"


def _norm_variant(v: str | None) -> str:
    if not v:
        return ""
    return _VARIANT_NORM.get(v.lower().strip().replace("  ", " "), "")


def _iphone_model(text: str) -> str | None:
    m = _IPHONE_RE.search(text)
    if not m:
        return None
    num = m.group(1).lower()
    variant = _norm_variant(m.group(2))
    if num == "se":
        # Year disambiguation for SE
        if "2022" in text or " 3" in text or "gen 3" in text:
            base = "iPhone SE 2022"
        elif "2020" in text or " 2" in text or "gen 2" in text:
            base = "iPhone SE 2020"
        else:
            base = "iPhone SE"
    elif num in ("x", "xr", "xs"):
        base = f"iPhone {num.upper()}"
    else:
        base = f"iPhone {num}"
    return f"{base} {variant}".strip()


def _samsung_model(text: str) -> str | None:
    if (m := _SAMSUNG_Z_RE.search(text)):
        kind = "Fold" if m.group(1).lower() in ("fold", "фолд") else "Flip"
        return f"Galaxy Z {kind}{m.group(2)}"
    if (m := _SAMSUNG_NOTE_RE.search(text)):
        v = " Ultra" if m.group(2) else ""
        return f"Galaxy Note{m.group(1)}{v}"
    if (m := _SAMSUNG_S_RE.search(text)):
        v = _norm_variant(m.group(2))
        return f"Galaxy S{m.group(1)} {v}".strip()
    if (m := _SAMSUNG_A_RE.search(text)):
        v = (m.group(2) or "").upper()
        return f"Galaxy A{m.group(1)}{v}".strip()
    return None


def _pixel_model(text: str) -> str | None:
    m = _PIXEL_RE.search(text)
    if not m:
        return None
    suffix = (m.group(2) or "").lower().replace(" ", "")
    tail = {"proxl": " Pro XL", "pro": " Pro", "xl": " XL", "a": "a"}.get(suffix, "")
    if tail == "a":
        return f"Pixel {m.group(1)}a"
    return f"Pixel {m.group(1)}{tail}"


def _xiaomi_model(text: str) -> str | None:
    if (m := _REDMI_RE.search(text)):
        is_note = "Note " if m.group(1) else ""
        num = m.group(2)
        v = _norm_variant(m.group(3)) or (m.group(3) or "").upper()
        return f"Redmi {is_note}{num} {v}".strip().replace("  ", " ")
    if (m := _POCO_RE.search(text)):
        v = (m.group(2) or "").upper()
        return f"Poco {m.group(1).upper()} {v}".strip()
    if (m := _XIAOMI_RE.search(text)):
        v = _norm_variant(m.group(2)) or (m.group(2) or "").upper()
        return f"Xiaomi {m.group(1)} {v}".strip()
    return None


def _oneplus_model(text: str) -> str | None:
    m = _ONEPLUS_RE.search(text)
    if not m:
        return None
    v = (m.group(2) or "").upper()
    return f"OnePlus {m.group(1)} {v}".strip()


def _honor_model(text: str) -> str | None:
    m = _HONOR_RE.search(text)
    if not m:
        return None
    v = _norm_variant(m.group(2)) or (m.group(2) or "").upper()
    return f"Honor {m.group(1)} {v}".strip()


def _huawei_model(text: str) -> str | None:
    m = _HUAWEI_RE.search(text)
    if not m:
        return None
    kind = m.group(1).capitalize()
    v = _norm_variant(m.group(3)) or (m.group(3) or "").upper()
    return f"Huawei {kind} {m.group(2)} {v}".strip()



_REF_RE = re.compile(
    r"\b(?:реф|рефаб|refurb\w*|восстановл\w*|восстановк\w*|cpo)\b",
    re.I,
)
_NOT_REF_RE = re.compile(
    r"\b(?:не|not)\s*(?:реф|рефаб|refurb\w*|восстановл\w*|восстановк\w*)\b",
    re.I,
)
_ROSTEST_RE = re.compile(
    r"\b(?:ростест|rst|рст)\b|для\s*рф\b|росси[йяи]\w*\s*верс",
    re.I,
)
_ESIM_RE = re.compile(
    r"\besim\s*(?:only|онли)\b|\b(?:only|онли)\s*esim\b|без\s*(?:физ|сим\s*лот|лотка\s*сим|слота)|без\s*сим\s*карт\w*|\bамериканец\b|\bамерик\w*\s*(?:верс|модел)",
    re.I,
)
_CN_RE = re.compile(
    r"\b(?:cn/a|ch/a)\b|китай\w*\s*(?:верс|модел)|\bдве\s*физ\s*сим\b|\b2\s*физ\s*сим\b|\bдвухсимочн\w*\b",
    re.I,
)
_CIS_RE = re.compile(
    r"\b(?:снг|кз|kz|индия|индус|оаэ|uae|япония|японец|еас|eac)\b",
    re.I,
)
_GLOBAL_RE = re.compile(
    r"\b(?:global|глобал|европеец|euro|eu)\b",
    re.I,
)


def _detect_version(text: str) -> str | None:
    version_text = _NOT_REF_RE.sub(" ", text)
    if _REF_RE.search(version_text):
        return "Ref"
    if _ROSTEST_RE.search(text):
        return "RST"
    if _GLOBAL_RE.search(text):
        return "Global"
    if _ESIM_RE.search(text):
        return "eSIM"
    if _CN_RE.search(text):
        return "CN"
    if _CIS_RE.search(text):
        return "CIS"
    return None



def normalize_device(
    title: str,
    *,
    description: str = "",
    storage_gb: int | None = None,
    ram_gb: int | None = None,
    params: dict[str, str] | None = None,
) -> CanonicalDevice:
    if _PARTS_TITLE_RE.search(title or ""):
        return CanonicalDevice(
            brand="unknown",
            model=None,
            storage_gb=storage_gb,
            ram_gb=ram_gb,
            raw_title=title or "",
        )

    params = params or {}
    text = normalize_homoglyphs(
        " ".join([title or "", description or ""])
    ).lower()
    if params:
        text += " " + normalize_homoglyphs(
            " ".join(str(v) for v in params.values())
        ).lower()

    brand = _detect_brand(text)
    model = None
    if brand == "apple":
        model = _iphone_model(text)
    elif brand == "samsung":
        model = _samsung_model(text)
    elif brand == "google":
        model = _pixel_model(text)
    elif brand == "xiaomi":
        model = _xiaomi_model(text)
    elif brand == "oneplus":
        model = _oneplus_model(text)
    elif brand == "honor":
        model = _honor_model(text)
    elif brand == "huawei":
        model = _huawei_model(text)
    else:
        # Brand word absent but model pattern may still be present.
        model = (
            _iphone_model(text) or _samsung_model(text) or _pixel_model(text) or
            _xiaomi_model(text) or _oneplus_model(text) or _honor_model(text) or
            _huawei_model(text)
        )
        if model:
            if model.startswith("iPhone"): brand = "apple"
            elif model.startswith("Pixel"): brand = "google"
            elif model.startswith("Galaxy"): brand = "samsung"
            elif model.startswith("Redmi") or model.startswith("Poco") or model.startswith("Xiaomi"): brand = "xiaomi"
            elif model.startswith("OnePlus"): brand = "oneplus"
            elif model.startswith("Honor"): brand = "honor"
            elif model.startswith("Huawei"): brand = "huawei"


    version = _detect_version(text)
    if model and version:
        model = f"{model} [{version}]"

    return CanonicalDevice(
        brand=brand,
        model=model,
        storage_gb=storage_gb,
        ram_gb=ram_gb,
        raw_title=title or "",
    )
