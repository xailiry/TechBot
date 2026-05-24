import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
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
# If the page pool is exhausted, wait this long, then use a transient
# throwaway tab instead of blocking forever (no deadlock).
PAGE_ACQUIRE_TIMEOUT_SEC = float(os.getenv("PAGE_ACQUIRE_TIMEOUT_SEC", "2.0"))
# Min seconds between automatic browser restarts (crash recovery).
BROWSER_RESTART_COOLDOWN_SEC = int(
    os.getenv("BROWSER_RESTART_COOLDOWN_SEC", "30")
)
# Proactively recycle Chrome on this interval to flush accumulated memory.
# A visible persistent-profile browser rendering heavy Avito pages balloons
# to several GB over a long run and slows the whole machine down. The on-disk
# profile keeps the anti-bot session across the restart. 0 disables.
BROWSER_RESTART_INTERVAL_SEC = int(
    os.getenv("BROWSER_RESTART_INTERVAL_SEC", "600")
)
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))

# Avito search URL params.
AVITO_BASE_URL = "https://www.avito.ru"
AVITO_CATEGORY = os.getenv("AVITO_CATEGORY", "telefony")  # phones; "" = global
AVITO_SORT_BY_DATE = int(os.getenv("AVITO_SORT_BY_DATE", "104"))

# ─── Polling loop ───────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "20"))
SUBSCRIPTION_STAGGER_SEC = float(os.getenv("SUBSCRIPTION_STAGGER_SEC", "0.3"))

# Multi-worker split: reserve some tabs for fast page-1 scans, 
# and use others for slower deep scans (pages 2+).
FAST_WORKERS = int(os.getenv("FAST_WORKERS", "2"))
DEEP_WORKERS = int(os.getenv("DEEP_WORKERS", str(max(1, PAGE_POOL_SIZE - FAST_WORKERS))))
DEEP_SCAN_INTERVAL_SEC = int(os.getenv("DEEP_SCAN_INTERVAL_SEC", "300"))  # 5 min
DEEP_SCAN_PAGES = int(os.getenv("DEEP_SCAN_PAGES", "12"))

# Hot scan window. In high-volume categories a listing posted minutes ago can
# already sit on page 2-3, so the deal hunter checks a small newest-first
# window every fast cycle. DEFAULT_SEARCH_PAGES is kept as a back-compat env.
FAST_SCAN_PAGES = max(
    1,
    int(os.getenv("FAST_SCAN_PAGES", os.getenv("DEFAULT_SEARCH_PAGES", "3"))),
)
DEFAULT_SEARCH_PAGES = FAST_SCAN_PAGES

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
# Max wait for a manual solve before giving up THIS cycle (it retries next
# cycle). 0 = wait indefinitely. Finite default so an unattended night
# does not stall the bot forever.
CAPTCHA_MAX_WAIT_SEC = int(os.getenv("CAPTCHA_MAX_WAIT_SEC", "3600"))
# Do not re-notify the operator about captcha more often than this.
CAPTCHA_NOTIFY_INTERVAL_SEC = int(
    os.getenv("CAPTCHA_NOTIFY_INTERVAL_SEC", "1800")
)
# Stable URL used to probe whether the IP block is gone (has_listings).
AVITO_PROBE_PATH = os.getenv("AVITO_PROBE_PATH", "/rossiya/telefony?s=104")
# Audible beep on captcha so the operator notices (Windows only).
CAPTCHA_BEEP = os.getenv("CAPTCHA_BEEP", "True").lower() in ("true", "1", "yes")

# ─── Watchdog (unattended health alerts) ────────────────────────────────────
WATCHDOG_CHECK_SEC = int(os.getenv("WATCHDOG_CHECK_SEC", "300"))
# No completed poll cycle for this long -> "monitor stuck" alert.
WATCHDOG_STALL_SEC = int(os.getenv("WATCHDOG_STALL_SEC", "900"))
# Consecutive cycles scraping 0 items (subs exist, not captcha) for this
# long -> "nothing collected, possible IP ban / layout change" alert.
WATCHDOG_DRY_SEC = int(os.getenv("WATCHDOG_DRY_SEC", "1800"))
# Min seconds between repeats of the same watchdog alert.
WATCHDOG_REPEAT_SEC = int(os.getenv("WATCHDOG_REPEAT_SEC", "3600"))

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
# Exact-hash match uses the index (cheap, common scam case). The fuzzy
# near-dup pass is bounded to this many most-recent rows.
DHASH_FUZZY_LIMIT = int(os.getenv("DHASH_FUZZY_LIMIT", "1500"))
DHASH_SCAN_LIMIT = DHASH_FUZZY_LIMIT  # back-compat alias

# ─── DB retention (keep SQLite small for long unattended runs) ───────────────
CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_SEC", str(6 * 3600)))
IMAGE_HASH_RETENTION_DAYS = int(os.getenv("IMAGE_HASH_RETENTION_DAYS", "21"))
LISTINGS_RETENTION_DAYS = int(os.getenv("LISTINGS_RETENTION_DAYS", "14"))
CARD_STATE_RETENTION_DAYS = int(os.getenv("CARD_STATE_RETENTION_DAYS", "7"))
SENT_ALERT_RETENTION_DAYS = int(os.getenv("SENT_ALERT_RETENTION_DAYS", "30"))

# ─── Valuation (Stage 3) ────────────────────────────────────────────────────
# Reseller overhead per flip (cleaning, shipping, fees) in RUB. Real number;
# defaults to 0 so we never inflate profit with a made-up cost.
PROFIT_OVERHEAD_RUB = int(os.getenv("PROFIT_OVERHEAD_RUB", "0"))
# Average discount expected during haggling (e.g. 0.05 = 5% off market).
PROFIT_HAGGLE_PERCENT = float(os.getenv("PROFIT_HAGGLE_PERCENT", "0.05"))
# A lot is surfaced as a deal only above both thresholds.
MIN_PROFIT_RUB = int(os.getenv("MIN_PROFIT_RUB", "3000"))
MIN_PROFIT_RATIO = float(os.getenv("MIN_PROFIT_RATIO", "0.12"))

# ─── Fast Valuation (Stage 1 / Speed) ───────────────────────────────────────
# If item.price < (market * (1 - HAGGLE) * threshold), trigger Stage 2.
# 0.90 = we deep-evaluate if it looks like at least a 10% "pre-profit".
FAST_VALUATION_THRESHOLD_PCT = float(os.getenv("FAST_VALUATION_THRESHOLD_PCT", "0.90"))
# Fresh Apple models are often mixed with sealed/import/reseller stock. Search
# cards do not expose enough state, so Discovery opens detail pages before
# using them as market-learning observations. 0 disables this extra caution.
DETAIL_LEARN_APPLE_MIN_GENERATION = int(
    os.getenv("DETAIL_LEARN_APPLE_MIN_GENERATION", "16")
)
# If Avito itself shows "market price" while our model thinks a working lot is
# far below market, treat it as a condition-mismatch warning. Usually the
# seller declared a defect that our text parser may have missed.
AVITO_MARKET_BADGE_CONFLICT_RATIO = float(
    os.getenv("AVITO_MARKET_BADGE_CONFLICT_RATIO", "0.85")
)

# ─── Discovery Mode (Broad Category Scan) ───────────────────────────────────
# Polling the whole category for any deal that matches these thresholds.
DISCOVERY_MIN_PROFIT_RUB = int(os.getenv("DISCOVERY_MIN_PROFIT_RUB", "7000"))
DISCOVERY_MIN_PROFIT_RATIO = float(os.getenv("DISCOVERY_MIN_PROFIT_RATIO", "0.20"))
DISCOVERY_CITY_SLUG = os.getenv("DISCOVERY_CITY_SLUG", "rossiya")
# Junk floor: the category scan ignores everything below this price, which
# cheaply drops "старое говно" (ancient/worthless phones). No-name Chinese
# brands are already excluded by the model normalizer. Raise to be stricter,
# lower to also chase cheap broken-flip lots.
DISCOVERY_MIN_PRICE = int(os.getenv("DISCOVERY_MIN_PRICE", "5000"))
# Discovery learns baselines cheaply from search cards (title + price, no
# detail fetch). It only OPENS a listing when it already looks like a deal.
# This caps such deep evaluations per scan cycle so a mispriced baseline
# cannot trigger a flood of detail fetches (captcha/IP-ban protection).
DISCOVERY_DEEP_PER_CYCLE = int(os.getenv("DISCOVERY_DEEP_PER_CYCLE", "15"))

# Broad market-learning crawl. Card learning is cheap, but some cards are too
# weak/noisy (missing storage or fresh Apple models), so training may open a
# small bounded number of detail pages per search page.
TRAINING_MAX_PAGES = int(os.getenv("TRAINING_MAX_PAGES", "120"))
TRAINING_MIN_PRICE = int(os.getenv("TRAINING_MIN_PRICE", str(DISCOVERY_MIN_PRICE)))
TRAINING_DETAIL_PER_PAGE = int(os.getenv("TRAINING_DETAIL_PER_PAGE", "8"))
TRAINING_SEEN_CACHE = int(os.getenv("TRAINING_SEEN_CACHE", "5000"))
TRAINING_PAGE_DELAY_MIN_SEC = float(os.getenv("TRAINING_PAGE_DELAY_MIN_SEC", "10.0"))
TRAINING_PAGE_DELAY_MAX_SEC = float(os.getenv("TRAINING_PAGE_DELAY_MAX_SEC", "20.0"))

# Baselines are learned from real observations only and keep adapting
# forever (never locked). Per-condition tiers learn separately.
BASELINE_MIN_SAMPLE = int(os.getenv("BASELINE_MIN_SAMPLE", "12"))
BASELINE_MIN_SAMPLE_COND = int(os.getenv("BASELINE_MIN_SAMPLE_COND", "12"))
# Confidence labels for the card (sample size + freshness of the baseline).
CONF_HIGH_SAMPLE = int(os.getenv("CONF_HIGH_SAMPLE", "25"))
CONF_MED_SAMPLE = int(os.getenv("CONF_MED_SAMPLE", "12"))
CONF_FRESH_DAYS = int(os.getenv("CONF_FRESH_DAYS", "3"))
BASELINE_LOOKBACK_DAYS = int(os.getenv("BASELINE_LOOKBACK_DAYS", "120"))
BASELINE_MAX_SAMPLE = int(os.getenv("BASELINE_MAX_SAMPLE", "600"))
# Prune observations older than this. MUST stay >= lookback so learning
# is unaffected; only truly dead data is removed.
PRICE_OBS_RETENTION_DAYS = int(
    os.getenv("PRICE_OBS_RETENTION_DAYS", str(BASELINE_LOOKBACK_DAYS + 30))
)
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
