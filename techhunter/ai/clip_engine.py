"""Local CLIP visual prefilter. Ported from Shmotki ml_engine.py and adapted
to electronics. Lazy singleton; degrades gracefully (returns no signal) if
torch/weights/network are unavailable so the text pipeline always works.

Label groups (one cached forward pass of text per group):
  subject  is it actually the phone, a box, an accessory, or a screenshot
  screen   cracked vs intact display
  origin   real handheld photo vs official studio/stock image (scam tell)
"""
import asyncio
import io
import logging
import threading

from .. import config

log = logging.getLogger(__name__)

LABEL_GROUPS: dict[str, list[str]] = {
    "subject": [
        "a photo of a smartphone",
        "a photo of an empty phone box and packaging",
        "a photo of a phone case or charger accessory",
        "a screenshot of a phone settings or battery screen",
    ],
    "screen": [
        "a smartphone with a cracked or broken screen",
        "a smartphone with an intact undamaged screen",
    ],
    "origin": [
        "a real casual photo of a phone taken by hand at home",
        "an official studio product stock photo of a phone",
    ],
}


def _to_embed(torch, out, projection):
    """Coerce a CLIP feature call result into a joint-space embedding
    tensor. transformers versions differ: some return a ready tensor,
    newer ones return BaseModelOutputWithPooling (then we project the
    pooled hidden state into the shared CLIP space ourselves)."""
    if isinstance(out, torch.Tensor):
        return out
    for attr in ("text_embeds", "image_embeds"):
        v = getattr(out, attr, None)
        if v is not None:
            return v
    pooled = getattr(out, "pooler_output", None)
    if pooled is None:
        lhs = getattr(out, "last_hidden_state", None)
        if lhs is None:
            raise TypeError("CLIP output has no usable embedding")
        pooled = lhs.mean(dim=1)
    return projection(pooled) if projection is not None else pooled


class ClipEngine:
    _instance: "ClipEngine | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
            cls._instance._unavailable = False
            # Guards model load across worker threads (no double load).
            cls._instance._load_lock = threading.Lock()
        return cls._instance

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if self._unavailable:
            return False
        with self._load_lock:
            if self._loaded:
                return True
            if self._unavailable:
                return False
            return self._load()

    def _load(self) -> bool:
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor

            self._torch = torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = CLIPModel.from_pretrained(config.CLIP_MODEL_ID).to(
                self.device
            )
            self.processor = CLIPProcessor.from_pretrained(config.CLIP_MODEL_ID)

            self._txt_proj = getattr(self.model, "text_projection", None)
            self._img_proj = getattr(self.model, "visual_projection", None)
            self._text_cache = {}
            for group, labels in LABEL_GROUPS.items():
                inputs = self.processor(
                    text=labels, return_tensors="pt", padding=True
                ).to(self.device)
                with torch.no_grad():
                    feats = _to_embed(
                        torch,
                        self.model.get_text_features(**inputs),
                        self._txt_proj,
                    )
                feats = feats / feats.norm(dim=-1, keepdim=True)
                self._text_cache[group] = feats
            self._loaded = True
            log.info("ClipEngine loaded on %s.", self.device)
            return True
        except Exception as e:
            self._unavailable = True
            log.warning(
                "ClipEngine unavailable (%s). Visual prefilter disabled; "
                "text pipeline continues.", e,
            )
            return False

    @property
    def available(self) -> bool:
        return self._ensure_loaded()

    def classify(self, image_bytes: bytes) -> dict[str, dict[str, float]]:
        if not self._ensure_loaded():
            return {}
        try:
            from PIL import Image

            torch = self._torch
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            inputs = self.processor(images=image, return_tensors="pt").to(
                self.device
            )
            with torch.no_grad():
                img_feat = _to_embed(
                    torch,
                    self.model.get_image_features(**inputs),
                    self._img_proj,
                )
                img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
                scale = self.model.logit_scale.exp()
                out: dict[str, dict[str, float]] = {}
                for group, labels in LABEL_GROUPS.items():
                    logits = scale * img_feat @ self._text_cache[group].t()
                    probs = logits.softmax(dim=-1)[0]
                    out[group] = {
                        lbl: float(p) for lbl, p in zip(labels, probs)
                    }
            return out
        except Exception as e:
            log.debug("clip classify error: %s", e)
            return {}

    async def classify_async(
        self, image_bytes: bytes
    ) -> dict[str, dict[str, float]]:
        """Off-loop: model load + torch/PIL run in a worker thread so the
        event loop (aiogram, captcha checks, timers) never freezes."""
        return await asyncio.to_thread(self.classify, image_bytes)


def get_clip_engine() -> ClipEngine:
    return ClipEngine()


def interpret_visual(groups: dict[str, dict[str, float]]) -> dict:
    """Reduce raw CLIP probabilities to actionable boolean flags.
    Missing/empty input -> all None (unknown)."""
    flags: dict[str, bool | None] = {
        "is_box_only": None,
        "is_accessory": None,
        "is_settings_screenshot": None,
        "screen_cracked_visual": None,
        "is_stock_photo": None,
    }
    sub = groups.get("subject")
    if sub:
        top = max(sub, key=sub.get)
        flags["is_box_only"] = top == LABEL_GROUPS["subject"][1] and sub[top] > 0.5
        flags["is_accessory"] = top == LABEL_GROUPS["subject"][2] and sub[top] > 0.5
        flags["is_settings_screenshot"] = (
            top == LABEL_GROUPS["subject"][3] and sub[top] > 0.5
        )
    scr = groups.get("screen")
    if scr:
        cracked = scr[LABEL_GROUPS["screen"][0]]
        flags["screen_cracked_visual"] = cracked > 0.60
    org = groups.get("origin")
    if org:
        stock = org[LABEL_GROUPS["origin"][1]]
        flags["is_stock_photo"] = stock > 0.65
    return flags
