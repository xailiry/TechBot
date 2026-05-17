"""Homoglyph normalization, shared by spec extraction and the scam guard.

Spammers swap Cyrillic letters for visually identical Latin ones
("зaмена диcплeя", "оpигинaл") to slip past text filters. We map them
back when a Latin lookalike sits next to Cyrillic. Heavy obfuscation is
itself a reseller/scam tell, so we also expose a density metric.

No project imports here (keep it dependency-free to avoid cycles).
"""

_LAT_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}


def _is_cyr(ch: str) -> bool:
    return bool(ch) and (0x0410 <= ord(ch) <= 0x044F or ch in ("ё", "Ё"))


def normalize_homoglyphs(text: str) -> str:
    if not text:
        return text
    out = list(text)
    n = len(out)
    for i, ch in enumerate(out):
        if ch not in _LAT_TO_CYR:
            continue
        left = out[i - 1] if i > 0 else ""
        right = out[i + 1] if i < n - 1 else ""
        if _is_cyr(left) or _is_cyr(right):
            out[i] = _LAT_TO_CYR[ch]
    return "".join(out)


def homoglyph_ratio(text: str) -> float:
    """Share of letters that are Latin lookalikes embedded in Cyrillic
    words. ~0 for normal text; high for deliberately obfuscated spam."""
    if not text:
        return 0.0
    out = list(text)
    n = len(out)
    letters = 0
    swapped = 0
    for i, ch in enumerate(out):
        if ch.isalpha():
            letters += 1
        if ch not in _LAT_TO_CYR:
            continue
        left = out[i - 1] if i > 0 else ""
        right = out[i + 1] if i < n - 1 else ""
        if _is_cyr(left) or _is_cyr(right):
            swapped += 1
    return swapped / letters if letters else 0.0
