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
_SAMSUNG_Z_RE = re.compile(
    r"(?:galaxy|галакси)?\s*z\s*(fold|flip|фолд|флип)\s*(\d)", re.I
)
_SAMSUNG_NOTE_RE = re.compile(
    r"(?:galaxy\s*)?note\s*(\d{1,2})\s*(ultra|ультра)?", re.I
)
_PIXEL_RE = re.compile(
    r"(?:pixel|пиксель)\s*(\d{1,2})\s*(pro\s*xl|pro|xl|a)?", re.I
)

_BRAND_WORDS = {
    "apple": ("iphone", "айфон", "айф", "apple", "эпл", "эппл"),
    "samsung": ("samsung", "самсунг", "galaxy", "галакси"),
    "google": ("pixel", "пиксель", "google pixel"),
}

_VARIANT_NORM = {
    "про макс": "Pro Max", "промакс": "Pro Max", "pro max": "Pro Max",
    "promax": "Pro Max", "про": "Pro", "pro": "Pro", "плюс": "Plus",
    "plus": "Plus", "мини": "mini", "mini": "mini", "макс": "Max",
    "max": "Max", "ультра": "Ultra", "ultra": "Ultra", "fe": "FE",
    "+": "Plus",
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


def normalize_device(
    title: str,
    *,
    storage_gb: int | None = None,
    ram_gb: int | None = None,
    params: dict[str, str] | None = None,
) -> CanonicalDevice:
    params = params or {}
    text = normalize_homoglyphs(title or "").lower()
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
    else:
        # Brand word absent but model pattern may still be present.
        model = _iphone_model(text) or _samsung_model(text) or _pixel_model(text)
        if model:
            brand = (
                "apple" if model.startswith("iPhone")
                else "google" if model.startswith("Pixel")
                else "samsung"
            )

    return CanonicalDevice(
        brand=brand,
        model=model,
        storage_gb=storage_gb,
        ram_gb=ram_gb,
        raw_title=title or "",
    )
