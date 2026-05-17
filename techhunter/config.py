import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_IDS = {
    int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

# Database. App uses the async driver; Alembic uses the sync one (same file).
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "techhunter.db"))
DB_URL = os.getenv("DB_URL", f"sqlite+aiosqlite:///{DB_PATH}")
DB_URL_SYNC = os.getenv("DB_URL_SYNC", f"sqlite:///{DB_PATH}")

# Default search scope.
DEFAULT_CITY_SLUG = os.getenv("DEFAULT_CITY_SLUG", "rossiya")

# ─── Scraper / browser ──────────────────────────────────────────────────────
# Visible browser by default: Avito rarely shows captcha when the window is
# real and shown; when it does, a human solves it fast and the bot resumes.
AVITO_HEADLESS = os.getenv("AVITO_HEADLESS", "False").lower() in ("true", "1", "yes")
# Persistent on-disk profile keeps the Datadome/Avito session across restarts.
BROWSER_PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", str(DATA_DIR / "browser_profile"))
# Prefer real installed Chrome; code falls back to bundled Chromium if absent.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")
PAGE_POOL_SIZE = int(os.getenv("PAGE_POOL_SIZE", "5"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))

# Avito search URL params.
AVITO_BASE_URL = "https://www.avito.ru"
AVITO_CATEGORY = os.getenv("AVITO_CATEGORY", "telefony")  # phones; "" = global
AVITO_SORT_BY_DATE = int(os.getenv("AVITO_SORT_BY_DATE", "104"))

# ─── Polling loop ───────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "20"))
SUBSCRIPTION_STAGGER_SEC = float(os.getenv("SUBSCRIPTION_STAGGER_SEC", "0.3"))
# Newest-sorted: new lots land on page 1, so 1 page keeps cycles fast.
DEFAULT_SEARCH_PAGES = int(os.getenv("DEFAULT_SEARCH_PAGES", "1"))
PAGE_TURN_DELAY_SEC = (
    float(os.getenv("PAGE_TURN_DELAY_MIN", "0.8")),
    float(os.getenv("PAGE_TURN_DELAY_MAX", "1.8")),
)
# How many new listings to enrich+value concurrently (bounded by the page
# pool). Higher = faster cycles, slightly higher anti-bot pressure.
MONITOR_CONCURRENCY = int(os.getenv("MONITOR_CONCURRENCY", "4"))
# Reuse a listing's cached evaluation if processed within this window and
# its price is unchanged (skip the heavy pipeline). Failures are NOT
# cached, so they retry next cycle and a lot is never lost.
LISTING_CACHE_TTL_SEC = int(os.getenv("LISTING_CACHE_TTL_SEC", "1800"))

# ─── Captcha suspension ─────────────────────────────────────────────────────
# How often to re-check whether a human solved the captcha.
CAPTCHA_RECHECK_SEC = int(os.getenv("CAPTCHA_RECHECK_SEC", "8"))
# 0 = wait indefinitely for manual solve (recommended for the visible workflow).
CAPTCHA_MAX_WAIT_SEC = int(os.getenv("CAPTCHA_MAX_WAIT_SEC", "0"))
# Audible beep on captcha so the operator notices (Windows only).
CAPTCHA_BEEP = os.getenv("CAPTCHA_BEEP", "True").lower() in ("true", "1", "yes")

# ─── AI evaluation (Stage 2, local only) ────────────────────────────────────
# Battery health at/under this % counts as a condition defect.
BATTERY_DEFECT_THRESHOLD = int(os.getenv("BATTERY_DEFECT_THRESHOLD", "80"))
# CLIP visual prefilter. Degrades gracefully if weights/network unavailable.
CLIP_ENABLED = os.getenv("CLIP_ENABLED", "True").lower() in ("true", "1", "yes")
CLIP_MODEL_ID = os.getenv("CLIP_MODEL_ID", "openai/clip-vit-base-patch32")
MAX_IMAGES_FOR_CLIP = int(os.getenv("MAX_IMAGES_FOR_CLIP", "3"))
IMAGE_DOWNLOAD_TIMEOUT = float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "10.0"))
# dhash Hamming distance at/under which two photos are "the same" (reused).
DHASH_MAX_DISTANCE = int(os.getenv("DHASH_MAX_DISTANCE", "2"))
# How many recent stored hashes to scan when detecting photo reuse.
DHASH_SCAN_LIMIT = int(os.getenv("DHASH_SCAN_LIMIT", "5000"))

# ─── Valuation (Stage 3) ────────────────────────────────────────────────────
# Reseller overhead per flip (cleaning, shipping, fees) in RUB. Real number;
# defaults to 0 so we never inflate profit with a made-up cost.
PROFIT_OVERHEAD_RUB = int(os.getenv("PROFIT_OVERHEAD_RUB", "0"))
# A lot is surfaced as a deal only above both thresholds.
MIN_PROFIT_RUB = int(os.getenv("MIN_PROFIT_RUB", "3000"))
MIN_PROFIT_RATIO = float(os.getenv("MIN_PROFIT_RATIO", "0.12"))
# Baselines are learned from real observations only and keep adapting
# forever (never locked). Per-condition tiers learn separately.
BASELINE_MIN_SAMPLE = int(os.getenv("BASELINE_MIN_SAMPLE", "8"))
BASELINE_MIN_SAMPLE_COND = int(os.getenv("BASELINE_MIN_SAMPLE_COND", "8"))
# Confidence labels for the card (sample size + freshness of the baseline).
CONF_HIGH_SAMPLE = int(os.getenv("CONF_HIGH_SAMPLE", "25"))
CONF_MED_SAMPLE = int(os.getenv("CONF_MED_SAMPLE", "12"))
CONF_FRESH_DAYS = int(os.getenv("CONF_FRESH_DAYS", "3"))
BASELINE_LOOKBACK_DAYS = int(os.getenv("BASELINE_LOOKBACK_DAYS", "120"))
BASELINE_MAX_SAMPLE = int(os.getenv("BASELINE_MAX_SAMPLE", "600"))
# Re-learn a stored baseline if older than this (continuous learning).
BASELINE_REFRESH_SEC = int(os.getenv("BASELINE_REFRESH_SEC", str(30 * 60)))
# Absolute sanity for learning: drop placeholder/typo prices (1 RUB, 1 mln).
# A genuine cheap broken flagship is still detected per-listing as an
# opportunity (it lands in the broken/parts tier, not the working pool).
PRICE_ABS_FLOOR = int(os.getenv("PRICE_ABS_FLOOR", "500"))
PRICE_ABS_CEIL = int(os.getenv("PRICE_ABS_CEIL", "3000000"))
JUNK_PRICE_MIN = PRICE_ABS_FLOOR  # back-compat alias

# ─── Onboarding deep crawl (new subscription) ───────────────────────────────
# On first sight of a subscription the bot crawls many pages to quickly
# build per-condition price stats, then keeps learning incrementally.
ONBOARDING_PAGES = int(os.getenv("ONBOARDING_PAGES", "15"))
ONBOARDING_MAX_DETAILS = int(os.getenv("ONBOARDING_MAX_DETAILS", "120"))
ONBOARDING_MAX_SEC = int(os.getenv("ONBOARDING_MAX_SEC", "480"))
# Periodically redo the deep crawl (silently) so medians stay fresh from a
# broad sample even when page 1 has no churn. 0 disables. Default 6h.
ONBOARDING_REFRESH_SEC = int(os.getenv("ONBOARDING_REFRESH_SEC", str(90 * 60)))


def require_bot_token() -> str:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill it."
        )
    return BOT_TOKEN
