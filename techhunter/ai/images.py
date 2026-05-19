"""Image download + perceptual dhash. Ported from Shmotki image_utils.py.

dhash detects the same photo reused across different sellers, a strong scam
signal consumed in Stage 3.
"""
import asyncio
import io
import logging

import httpx
from PIL import Image

from .. import config

log = logging.getLogger(__name__)

# Avito CDN serves images only with browser-like headers (else 403 / HTML).
_HEADERS_AVITO = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.avito.ru/",
    "sec-fetch-dest": "image",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-site",
}
_HEADERS_GENERIC = {
    "User-Agent": _HEADERS_AVITO["User-Agent"],
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _headers_for(url: str) -> dict:
    return _HEADERS_AVITO if ("avito" in url or ".avito.st" in url) else _HEADERS_GENERIC


async def download_image_bytes(
    url: str, timeout: float | None = None
) -> bytes | None:
    if not url:
        return None
    timeout = timeout or config.IMAGE_DOWNLOAD_TIMEOUT
    try:
        async with httpx.AsyncClient(
            timeout=timeout, headers=_headers_for(url), follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            ct = resp.headers.get("content-type", "")
            magic = resp.content[:12]
            looks_image = (
                "image" in ct
                or magic[:3] == b"\xff\xd8\xff"
                or magic[:4] == b"\x89PNG"
                or magic[:6] in (b"GIF87a", b"GIF89a")
                or (magic[:4] == b"RIFF" and magic[8:12] == b"WEBP")
            )
            return resp.content if looks_image else None
    except Exception as e:
        log.debug("download_image %s -> %s", url[:80], e)
        return None


def dhash_bytes(content: bytes) -> str | None:
    """64-bit difference hash as a 16-char hex string."""
    try:
        img = Image.open(io.BytesIO(content)).convert("L").resize(
            (9, 8), Image.Resampling.LANCZOS
        )
        pixels = list(img.getdata())
        value = 0
        bit = 0
        for row in range(8):
            for col in range(8):
                idx = row * 9 + col
                if pixels[idx] > pixels[idx + 1]:
                    value += 1 << bit
                bit += 1
        return hex(value)[2:].zfill(16)
    except Exception as e:
        log.debug("dhash failed: %s", e)
        return None


async def get_image_hash(
    url: str, precomputed: bytes | None = None
) -> str | None:
    content = precomputed or await download_image_bytes(url)
    if not content:
        return None
    # PIL decode/resize is CPU-bound -> off the event loop.
    return await asyncio.to_thread(dhash_bytes, content)


def hamming(h1: str | None, h2: str | None) -> int:
    """Bit distance between two hex hashes. 0-2 == near-identical."""
    if not h1 or not h2:
        return 100
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except ValueError:
        return 100
