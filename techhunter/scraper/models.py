from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import AVITO_BASE_URL


class ParsedListing(BaseModel):
    """Runtime DTO for a scraped Avito listing.

    Distinct from the ORM `Listing` (db/models.py) which only tracks dedup.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str
    title: str
    price: int
    currency: str = "RUB"
    url: str
    location: str = ""
    date_text: str = ""
    image: str | None = None

    snippet: str = ""

    # Enriched from the detail page (Stage 2 consumes these).
    images: list[str] = Field(default_factory=list)
    description: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    avito_price_badge: str | None = None  # below | market | above
    avito_market_badge: bool = False
    seller_name: str | None = None
    seller_label: str | None = None
    seller_type: str | None = None
    seller_rating: float | None = None
    seller_reviews: int | None = None
    seller_listings: int | None = None
    seller_year: int | None = None
    detail_fetched: bool = False

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, v):
        if isinstance(v, str):
            digits = "".join(ch for ch in v if ch.isdigit())
            return int(digits) if digits else 0
        return int(v) if v is not None else 0

    @property
    def full_url(self) -> str:
        if self.url.startswith("http"):
            return self.url
        return f"{AVITO_BASE_URL}{self.url}"

    @property
    def chat_url(self) -> str:
        base = self.full_url
        return f"{base}#chat" if "avito.ru" in base else base

    def get_content_hash(self) -> str:
        """Stable card-level hash to detect re-posts with a new listing id."""
        import hashlib

        image_key = self.image or (self.images[0] if self.images else "")
        image_key = image_key.split("?", 1)[0]
        # Without a photo, title+location is too coarse: two legitimate
        # same-model phones would collapse into one. In that case keep the
        # listing id, accepting that a photo-less repost cannot be deduped.
        identity = image_key or self.id
        payload = f"{self.title}|{self.location}|{identity}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
