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
    image: str | None = None
    snippet: str = ""

    # Enriched from the detail page (Stage 2 consumes these).
    images: list[str] = Field(default_factory=list)
    description: str = ""
    params: dict[str, str] = Field(default_factory=dict)
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
