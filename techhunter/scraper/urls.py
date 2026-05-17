from urllib.parse import urlencode

from ..config import AVITO_BASE_URL, AVITO_CATEGORY, AVITO_SORT_BY_DATE

# Minimal city-name -> Avito slug map. Unknown -> "rossiya" (whole country).
CITY_SLUGS = {
    "москва": "moskva",
    "санкт-петербург": "sankt-peterburg",
    "спб": "sankt-peterburg",
    "питер": "sankt-peterburg",
    "екатеринбург": "ekaterinburg",
    "новосибирск": "novosibirsk",
    "казань": "kazan",
    "нижний новгород": "nizhniy_novgorod",
    "краснодар": "krasnodar",
    "россия": "rossiya",
}


def resolve_city_slug(city: str) -> str:
    if not city:
        return "rossiya"
    return CITY_SLUGS.get(city.lower().strip(), city.lower().strip() or "rossiya")


def build_search_url(
    query: str,
    city_slug: str = "rossiya",
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    page: int = 1,
    category: str | None = None,
) -> str:
    """Avito search URL sorted by date (newest first) for monitoring.

    category defaults to config AVITO_CATEGORY; pass "" for a global search.
    """
    cat = AVITO_CATEGORY if category is None else category
    path = f"/{city_slug}"
    if cat:
        path = f"{path}/{cat}"

    params: dict = {"q": query, "s": AVITO_SORT_BY_DATE}
    if max_price:
        params["pmax"] = max_price
    if min_price:
        params["pmin"] = min_price
    if page and page > 1:
        params["p"] = page
    return f"{AVITO_BASE_URL}{path}?{urlencode(params)}"
