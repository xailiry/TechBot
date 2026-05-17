"""Stage 2 verification (no network, no CLIP weights download).

Run: ../venv/Scripts/python.exe -m tests.test_stage2  (from project root)
"""
import tests._dbsetup  # noqa: F401  (must precede techhunter imports)

import asyncio
import io
import sys

from PIL import Image

from techhunter.ai.clip_engine import get_clip_engine, interpret_visual
from techhunter.ai.condition import Condition, grade_condition
from techhunter.ai.evaluate import evaluate_listing
from techhunter.ai.images import dhash_bytes, hamming
from techhunter.ai.normalize import normalize_device
from techhunter.ai.specs import extract_specs
from techhunter.scraper.models import ParsedListing


def check(name: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        sys.exit(1)


def test_specs() -> None:
    s = extract_specs(
        "iPhone 13 Pro 256GB", "акб 89%, замена экрана, Face ID не работает"
    )
    check("storage 256", s.storage_gb == 256)
    check("battery 89", s.battery_health == 89)
    check("screen_replaced", "screen_replaced" in s.defects)
    check("faceid_broken", "faceid_broken" in s.defects)
    check("battery 89 not defect", "battery_replaced" not in s.defects)

    s2 = extract_specs("Samsung S23", "8/256 ГБ, акб 70%, разбит экран")
    check("ram 8", s2.ram_gb == 8)
    check("storage 256 (ram/rom)", s2.storage_gb == 256)
    check("low battery -> defect", "battery_replaced" in s2.defects)
    check("screen_cracked", "screen_cracked" in s2.defects)

    s3 = extract_specs("iPhone 12", "залочен на icloud, копия 1:1")
    check("icloud_locked", "icloud_locked" in s3.defects)
    check("replica", "replica" in s3.defects)

    s4 = extract_specs("iPhone 15 новый запечатан ростест", "")
    check("sealed", s4.is_sealed is True)
    check("rostest", s4.is_rostest is True)

    # Structured "Характеристики" must win over title heuristics.
    s5 = extract_specs(
        "Samsung Galaxy S25 Ultra 12/256",
        "",
        {
            "производитель": "Samsung",
            "модель": "Galaxy S25 Ultra",
            "встроенная память": "256 ГБ",
            "оперативная память": "12 ГБ",
            "цвет": "Серебристый",
        },
    )
    check("params storage 256", s5.storage_gb == 256)
    check("params ram 12", s5.ram_gb == 12)

    s6 = extract_specs(
        "iPhone 15 Pro", "", {"встроенная память": "1 ТБ"}
    )
    check("params 1 TB -> 1024", s6.storage_gb == 1024)

    # No params -> title "12/256" fallback still works.
    s7 = extract_specs("Samsung S24 12/512 ГБ", "")
    check("fallback ram 12", s7.ram_gb == 12)
    check("fallback storage 512", s7.storage_gb == 512)


def test_normalize() -> None:
    d = normalize_device("Айфон 13 про макс 256гб", storage_gb=256)
    check("apple brand", d.brand == "apple")
    check("iphone 13 pro max", d.model == "iPhone 13 Pro Max")
    check("storage passthrough", d.storage_gb == 256)

    check("iphone se", normalize_device("iPhone SE 2022").model == "iPhone SE")
    check("iphone xr", normalize_device("айфон xr").model == "iPhone XR")
    sg = normalize_device("Samsung Galaxy S24 Ultra")
    check("samsung s24 ultra", sg.brand == "samsung" and sg.model == "Galaxy S24 Ultra")
    zf = normalize_device("Galaxy Z Fold5 512")
    check("z fold5", zf.model == "Galaxy Z Fold5")
    px = normalize_device("Google Pixel 8 Pro")
    check("pixel 8 pro", px.brand == "google" and px.model == "Pixel 8 Pro")
    unk = normalize_device("MacBook Air M2")
    check("unknown brand", unk.brand == "unknown" and unk.model is None)


def test_condition() -> None:
    def cond(title, desc=""):
        return grade_condition(extract_specs(title, desc), f"{title} {desc}")

    check("cracked -> BROKEN", cond("iPhone 13 разбит экран") == Condition.BROKEN)
    check("icloud -> FOR_PARTS",
          cond("iPhone 12 на запчасти icloud") == Condition.FOR_PARTS)
    check("faceid -> DEFECT",
          cond("iPhone 11 Face ID не работает") == Condition.DEFECT)
    check("ideal -> IDEAL", cond("iPhone 14 идеал") == Condition.IDEAL)
    check("plain -> GOOD", cond("iPhone 12 128 б/у") == Condition.GOOD)


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return buf.getvalue()


def test_dhash() -> None:
    a = _png((10, 20, 30))
    a2 = _png((10, 20, 30))
    ha, ha2 = dhash_bytes(a), dhash_bytes(a2)
    check("hash computed", ha is not None and len(ha) == 16)
    check("identical -> hamming 0", hamming(ha, ha2) == 0)

    grad = Image.new("L", (64, 64))
    # Three vertical bands (dark / bright / dark). Low-frequency so it
    # survives the LANCZOS downscale and produces high>low transitions,
    # giving a non-zero dhash clearly distinct from a flat color (hash 0).
    grad.putdata([
        (0 if col < 22 else 255 if col < 43 else 0)
        for _row in range(64) for col in range(64)
    ])
    gb = io.BytesIO()
    grad.save(gb, format="PNG")
    check("different image -> hamming > 2",
          hamming(ha, dhash_bytes(gb.getvalue())) > 2)
    check("bad hash -> 100", hamming(None, ha) == 100)


def test_clip_embed_coercion() -> None:
    # Regression: transformers 5.x returns BaseModelOutputWithPooling from
    # get_text/image_features; _to_embed must coerce it to a tensor.
    import torch
    from types import SimpleNamespace

    from techhunter.ai.clip_engine import _to_embed

    t = torch.ones(2, 4)
    check("tensor passthrough", _to_embed(torch, t, None) is t)

    emb = torch.zeros(2, 4)
    check("text_embeds used",
          _to_embed(torch, SimpleNamespace(text_embeds=emb), None) is emb)

    pooled = torch.ones(1, 3)
    proj = torch.nn.Linear(3, 5, bias=False)
    out = _to_embed(torch, SimpleNamespace(pooler_output=pooled), proj)
    check("pooled projected to joint dim", tuple(out.shape) == (1, 5))

    lhs = torch.ones(1, 6, 3)
    out2 = _to_embed(torch, SimpleNamespace(last_hidden_state=lhs), None)
    check("last_hidden_state mean-pooled", tuple(out2.shape) == (1, 3))


def test_clip_degrade() -> None:
    eng = get_clip_engine()
    eng._unavailable = True  # force the no-weights / offline path
    eng._loaded = False
    check("classify degrades to {}", eng.classify(_png((1, 2, 3))) == {})
    flags = interpret_visual({})
    check("interpret empty -> all None",
          all(v is None for v in flags.values()) and "is_box_only" in flags)


async def test_evaluate_textonly() -> None:
    item = ParsedListing(
        id="ev-1",
        title="iPhone 13 Pro 256 ГБ",
        price=55000,
        url="/x/1",
        description="акб 91%, состояние идеал, тру тон есть, чек коробка",
    )
    r = await evaluate_listing(item, run_clip=False, do_dedup=False)
    check("eval brand", r.brand == "apple")
    check("eval model", r.model == "iPhone 13 Pro")
    check("eval storage", r.storage_gb == 256)
    check("eval battery", r.battery_health == 91)
    check("eval condition IDEAL", r.condition == "ideal")
    check("eval visual empty", r.visual == {})
    check("eval reused 0", r.reused_image_count == 0)


async def test_dedup_db() -> None:
    import uuid

    from techhunter.storage import count_reused_images, record_image_hash

    tag = uuid.uuid4().hex[:8]
    h = f"{int(tag, 16):016x}"  # unique 16-hex hash, far from prior rows
    a, b, c = f"A-{tag}", f"B-{tag}", f"C-{tag}"
    await record_image_hash(a, h)
    check("B sees A's photo", await count_reused_images(b, h) == 1)
    check("A excludes itself", await count_reused_images(a, h) == 0)
    await record_image_hash(b, h)
    check("C sees A and B", await count_reused_images(c, h) == 2)


def main() -> None:
    test_specs()
    test_normalize()
    test_condition()
    test_dhash()
    test_clip_embed_coercion()
    test_clip_degrade()
    asyncio.run(test_evaluate_textonly())
    asyncio.run(test_dedup_db())
    print("\nAll Stage 2 checks passed.")


if __name__ == "__main__":
    main()
