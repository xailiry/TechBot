# TechHunter_Bot Technical Documentation

This document is compiled specifically for AI code editors, LLMs, and developers to instantly understand the business logic, directory structure, architecture, database schemas, AI modules, scraping engine, valuation logic, and known gotchas of TechHunter_Bot.

---

## 1. Overview and Business Rules

TechHunter_Bot is a specialized Telegram bot designed for used-phone resellers (Russian: "перекупы") to monitor Avito for underpriced listings (iPhones and flagship Android devices). The bot calculates the resale profit margin, identifies repair-and-flip opportunities, detects scam attempts, and alerts subscribed users.

### Core Business Premises
1. **Anti-Captcha Strategy**: The scraping engine runs Playwright with a visible browser window (`headless=False` in production). Real browser windows almost never trigger Avito/Datadome captchas. If a captcha is encountered, the scraping loop suspends, notifies the operator to solve it manually, and automatically resumes once cleared.
2. **Deterministic & Learned Prices**: Prices are never fabricated. Valuation is based on actual market prices learned dynamically from listing observations.
3. **Target Segment**: Apple iPhones and flagship Android devices (Samsung Galaxy S/Z/Note, Google Pixel).
4. **Local-Only AI Processing**: Heavy classification uses local CLIP models and fast regular expression text extractors. No external commercial LLM APIs are used.
5. **Avito-Centric Design**: Currently designed for Avito, but abstracts page collection and parsing to allow future sources.

---

## 2. Directory Structure

```
TechHunter_Bot/
├── main.py                     # Entrypoint (bot polling + Avito monitor supervisors)
├── requirements.txt            # Python dependencies (aiogram, sqlalchemy, playwright, etc.)
├── alembic.ini                 # Schema migration configuration
├── alembic/                    # DB schema migrations
├── data/                       # Production databases (techhunter.db) and browser profiles
├── logs/                       # Application run logs
├── tests/                      # Testing suite (isolated from production data)
│   ├── _dbsetup.py             # Pre-test database isolation hook
│   └── run_all.py              # Test orchestrator (11 test suites)
└── techhunter/                 # Core packages
    ├── __init__.py
    ├── config.py               # Complete project configuration loaded from env/defaults
    ├── logging_config.py       # Global logging setup
    ├── monitor.py              # Main entrypoint wrapper for the monitoring system
    ├── monitoring/             # Monitoring lifecycle managers
    │   ├── poller.py           # Core scrape loop, orchestration, and worker pools
    │   ├── training.py         # Baseline onboarding and continuous learning
    │   └── maintenance.py      # Background tasks (retention cleanup, etc.)
    ├── pipeline.py             # Single listing valuation processing pipeline
    ├── delivery.py             # Per-user filter checking
    ├── storage.py              # Legacy async database helpers (being phased out)
    ├── textnorm.py             # Shared homoglyph normalization & similarity check
    ├── runtime.py              # In-memory status snapshot of the scraper cycle
    ├── notifier.py             # Notifier interfaces (Console + Telegram)
    ├── db/                     # DB session, base class, and models
    │   ├── base.py
    │   ├── session.py          # SQLite WAL tuning & session factory
    │   ├── models.py           # Database tables
    │   └── repository.py       # Modern data access layer (Repository Pattern)
    ├── scraper/                # Web scraping and Avito parser
    │   ├── session.py          # Global state management for Captcha / scraping locks
    │   ├── browser.py          # Playwright resilience, tab pool, context recycling
    │   ├── parser.py           # BeautifulSoup4 data extraction
    │   ├── stealth.py          # Playwright-stealth signatures
    │   ├── urls.py             # Avito search URL builder
    │   └── models.py           # Pydantic schemas (ParsedListing)
    ├── ai/                     # Local AI, CLIP, and text analyzers
    │   ├── specs.py            # Regex spec/defect extractor
    │   ├── condition.py        # Grade scoring (ideal, good, defect, broken, for_parts)
    │   ├── images.py           # Downloader and image dhash generator
    │   ├── clip_engine.py      # Lazy-loaded thread-safe CLIP classification
    │   └── evaluate.py         # Evaluation consolidator
    └── valuation/              # Market price calculations & scammers detection
        ├── clustering.py       # Robust median with IQR outlier trimming
        ├── repair.py           # Physical repair cost databases and estimations
        ├── scam.py             # Scam guard, trust scoring, and obfuscation checks
        ├── devices.py          # Dynamic per-condition reference learning
        └── engine.py           # Fast search-card valuation & deep valuation logic
```

---

## 3. Technical Stack and Environment

### Technologies
- **Python**: 3.13 (Venv path: `..\venv\`)
- **Telegram Bot**: `aiogram==3.13.1` (FSM using default MemoryStorage)
- **Database**: `SQLAlchemy==2.0.49` + `alembic==1.18.4` (Async SQLite driver: `aiosqlite==0.20.0`)
- **Scraping**: `playwright==1.48.0` + `playwright-stealth==2.0.3` + `beautifulsoup4==4.12.3` + `lxml==5.3.0`
- **AI & Images**: `torch==2.11.0` + `transformers==5.8.0` + `pillow==10.4.0` + `numpy==2.4.4`
- **Network**: `httpx==0.27.2` (Avito CDN image downloading)

### Running Commands
Run these commands from inside the `TechHunter_Bot` directory:
- **Start the Bot + Monitor**: `..\venv\Scripts\python.exe main.py`
- **Run all Tests**: `..\venv\Scripts\python.exe -m tests.run_all`
- **Apply Database Migrations**: `..\venv\Scripts\python.exe -m alembic upgrade head`

---

## 4. Database Schema and Lifecycles

Database models reside in `techhunter/db/models.py`. Schema migrations are governed by Alembic. 

### SQLite WAL and Session Configuration (`techhunter/db/session.py`)
To prevent `database is locked` issues during concurrent scraping, onboarding, and user bot interactions, SQLite is tuned at connection startup:
```python
cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA synchronous=NORMAL")
cur.execute("PRAGMA busy_timeout=5000")
```

### SQLAlchemy ORM Models
- **User**: Represents a Telegram user.
  - Fields: `tg_id` (BigInteger, PK), `username`, `paused` (Integer), `min_score` (Integer, default 0), `exclude_conditions` (CSV string of condition exclusions), `exclude_shop` (Integer, default 0), `discovery_enabled` (Integer), `discovery_min_profit_rub`, `discovery_min_profit_ratio`, `discovery_city_slug`, `created_at`.
- **Subscription**: Search terms subscribed by a user.
  - Fields: `id` (Integer, PK), `tg_id` (FK to User), `query`, `city_slug`, `min_price`, `max_price`, `min_battery`, `search_pages`, `onboarded_at`, `created_at`.
- **DeviceCatalog**: The canonical brand + model + memory configurations.
  - Fields: `id` (Integer, PK), `brand`, `model`, `storage_gb`, `ram_gb`, `aliases`. Unique constraint on `(brand, model, storage_gb)`.
- **Listing**: Cached evaluations to avoid re-processing existing listings.
  - Fields: `id` (String, PK), `source`, `url`, `title`, `content_hash` (Index), `device_id` (FK to DeviceCatalog), `last_price`, `condition`, `processed_at` (Null until pipeline succeeds to prevent transient failures from caching), `report_json`, `valuation_json`, `first_seen`, `last_seen`.
- **PriceObservation**: Raw observations extracted during crawls.
  - Fields: `id` (Integer, PK), `device_id` (FK), `listing_id`, `raw_title`, `storage_gb`, `condition`, `price`, `source`, `observed_at` (Index).
- **MarketBaseline**: Dynamically calculated reference prices.
  - Fields: `id` (Integer, PK), `device_id` (FK), `condition`, `median_price`, `sample_size`, `updated_at`. Unique constraint on `(device_id, condition)`.
- **RepairCost**: Operator-specified real repair costs.
  - Fields: `id` (Integer, PK), `brand`, `model_pattern`, `defect_type` (screen, battery, back_glass, faceid, no_power), `cost_rub`, `updated_at`.
- **SentAlert**: Log of delivered deals to prevent duplicate alerts.
  - Fields: `tg_id` (PK), `listing_id` (PK), `price`, `profit`, `verdict`, `condition`, `sub_query`, `sent_at` (Index).
- **ImageHash**: Holds the dhash of processed photos to detect reused/cloned listings.
  - Fields: `id` (Integer, PK), `listing_id` (Index), `img_hash` (Index), `created_at`.
- **CardState**: Persisted Telegram deal cards for stateful button callbacks.
  - Fields: `tg_id` (PK), `message_id` (PK), `item_json`, `score_json`, `sub_query`, `created_at` (Index).
- **DealFeedback**: Stores 👍 and 👎 user reactions to deals.
  - Fields: `tg_id` (PK), `listing_id` (PK), `reaction` (up / down), `created_at`.

### Retention Maintenance (`techhunter/monitoring/maintenance.py` & `storage.py`)
To prevent unbounded database growth, a background loop inside `maintenance.py` calls `cleanup_old_rows()` every 6 hours (configurable via `CLEANUP_INTERVAL_SEC`):
- `ImageHash`: Deleted after `IMAGE_HASH_RETENTION_DAYS` (default 21)
- `PriceObservation`: Deleted after `PRICE_OBS_RETENTION_DAYS` (default 150)
- `Listing`: Deleted after `LISTINGS_RETENTION_DAYS` (default 14)
- `CardState`: Deleted after `CARD_STATE_RETENTION_DAYS` (default 7)
- `SentAlert`: Deleted after `SENT_ALERT_RETENTION_DAYS` (default 30)

---

## 5. Web Scraping & Playwright Engine

The scraping module resides in `techhunter/scraper/browser.py`.

### Browser Resilience and Threading Pools
Playwright drives a persistent Chrome user profile (`BROWSER_PROFILE_DIR`). The browser is reused across cycles, preserving session cookies and anticaptcha settings.
- **Tab Pool Gating**: The browser initializes a static page pool of `PAGE_POOL_SIZE` tabs.
- **Transient Recovery**: If a page is closed, it is replaced on-the-fly. If the page queue is blocked, a temporary transient tab is spawned to prevent deadlocks (timeout `PAGE_ACQUIRE_TIMEOUT_SEC`).
- **Recycle Interval**: To avoid Chrome memory bloat, the browser context auto-restarts every 10 minutes (`BROWSER_RESTART_INTERVAL_SEC`), serializing context reconstruction behind a lock.

### Multi-Worker Partitioning
Workers are separated into Fast and Deep pools:
- **Fast Workers**: `FAST_WORKERS` (default 2) scan page 1 of search listings every `POLL_INTERVAL_SEC` (default 20s) to discover new postings instantly.
- **Deep Workers**: `DEEP_WORKERS` scan pages 2 to `DEEP_SCAN_PAGES` (default 12) on a slower schedule (`DEEP_SCAN_INTERVAL_SEC`, default 5 mins).

### Block & Captcha Gate
Avito detects blocks via `page_blocked(html)` and `looks_blocked(html)` in `parser.py`.
1. **Datadome Capture Lock**: Captcha block is IP-wide. When a block is hit by a worker, it locks the `_captcha_lock` and suspends all other page fetches.
2. **Out-of-band Checking**: The supervisor alerts the operator via Telegram. The supervisor opens a stable tab (`_probe_clear`) to check if the ban is gone by hitting `AVITO_PROBE_PATH` every 12s.
3. **Auto-reload**: Once the captcha is solved by the human, the monitor reload event fires, refreshing all blocked pages.

### Watchdog System
A background thread (`_watchdog_loop`) monitors cycle activity:
- **Stall Detector**: Fires an alert if no crawl cycle completes in `WATCHDOG_STALL_SEC` (default 15 mins).
- **Dry Detector**: Fires an alert if 0 listings are parsed across consecutive cycles for `WATCHDOG_DRY_SEC` (default 30 mins) while active subscriptions exist, warning of possible page layout changes or silent IP blocks.

---

## 6. AI Spec Extraction & Local Visual Filter

The AI modules reside in `techhunter/ai/`.

### Regex Specs and Negation Parser (`techhunter/ai/specs.py`)
Free-text parser `extract_specs` translates Russian inflections into standard defect codes without using external APIs.
- **Defect Codes Mapping**:
  - `icloud_locked`: Activation lock, blacklist ("в розыске", "черный список"), or completely dead network modules.
  - `carrier_locked`: R-SIM, MDM lock, demo units (`ldu`), bypass software, locked carrier bands.
  - `screen_cracked`: Physical crack on glass or display assembly.
  - `screen_replaced`: Replaced display, "not original", or copies.
  - `battery_replaced`: Replaced battery, bloated battery, battery health under `BATTERY_DEFECT_THRESHOLD` (default 80%), exact 100% health on older models, or physical mismatch between cycle count and reported health (dynamic wear curve).
  - `faceid_broken`: Non-functional Face ID or Touch ID.
  - `truetone_missing`: True Tone feature absent (typical indicator of screen swap).
  - `screen_display_defect`: Matrix damage, stripes, dead pixels, or bright spots.
  - `cosmetic_wear`: Surface scratches, cracks (excluding front display), minor dents (Note: treated as GOOD condition, not DEFECT, per user preference).
- **Homoglyph Normalization**: Spammers often bypass regex filters by blending Russian characters with identical-looking Latin letters (e.g. Russian "а" with Latin "a"). Every description is pre-normalized via `normalize_homoglyphs` inside `techhunter/textnorm.py` before parsing.
- **Negation Guard (`_NEG_CLEAN`)**: Before running defect regexes, typical Russian negative claims like "не битый", "без замены", "Neverlock", or "нет никаких полос" are wiped out. Similarly, `scam.py` guards against "не копия", "не паль" to prevent false positive flags on clean listings.

### Lazy Thread-Safe CLIP (`techhunter/ai/clip_engine.py`)
CLIP is used to filter out noise photos (e.g. settings screenshots, empty boxes, or accessories).
- **Graceful Degradation**: CLIP loads lazy-singletons. If weights or CUDA are missing, the system shuts CLIP down and processes purely via text normalization.
- **Loop Off-threading**: CLIP runs inside `asyncio.to_thread` via `classify_async`. This prevents heavy tensor math from blocking the single-threaded asyncio event loop.
- **Image Deduping**: Listed image dhashes are calculated asynchronously. Exact duplicates are matched via database index. Near-duplicates are searched in thread via Hamming distance against the last `DHASH_FUZZY_LIMIT` (default 1500) database entries.

---

## 7. Valuation & Market Price Learning

The valuation module resides in `techhunter/valuation/`.

### Continuous Baseline Self-Learning
A subscription onboarding crawler searches up to 15 pages (`ONBOARDING_PAGES`) and extracts up to 120 listings (`ONBOARDING_MAX_DETAILS`) to warm-start medians. The bot also does silent refreshes every 90 minutes.

### Robust Median Estimation (`techhunter/valuation/clustering.py`)
To isolate genuine used market prices from inflated retail/wholesale prices, `robust_median` runs an Interquartile Range (IQR) trimming process:
1. Slices raw price data in ascending order.
2. Removes outliers outside `[Q1 - 1.5 * IQR, Q3 + 1.5 * IQR]`.
3. If `drop_low_cluster` is enabled (for working tiers), it filters out lower bargain listings to prevent broken devices from depressing the working resale baseline.

### Price Tier Validation (`techhunter/valuation/devices.py`)
Dynamic tiers (`ideal`, `good`, `defect`, `broken`, `for_parts`) are anchored to the working baseline. To prevent illogical data anomalies (e.g. `ideal` pricing falling below `good` pricing), each tier's median must land within a calibrated band relative to the working median:
- `ideal`: `[0.85 * working, 1.60 * working]`
- `good`: `[0.70 * working, 1.30 * working]`
- `defect`: `[0.40 * working, 1.05 * working]`
- `broken`: `[0.20 * working, 0.90 * working]`
- `for_parts`: `[0.10 * working, 0.70 * working]`

Tiers falling outside these bands are discarded as noise.

### Valuation and Broken Flip Math (`techhunter/valuation/engine.py`)
When a listing is valued:
- **Resale Baseline**: Aggregated worked baseline is loaded.
- **Scam Guard Check**: Runs `score_listing` in `scam.py`. If the score is high or it flags a replica, the listing is dropped.
- **Working-Grade Calculation**:
  `net_profit = (baseline * (1 - PROFIT_HAGGLE_PERCENT)) - listed_price - PROFIT_OVERHEAD_RUB`
- **Broken-Flip Calculation**: 
  Calculates physical repair costs for `screen`, `back_glass`, `battery`, `faceid`, or `no_power` from database entries (or falls back to estimated model arrays in `repair.py`).
  `net_profit = (working_baseline * (1 - PROFIT_HAGGLE_PERCENT)) - listed_price - total_repair_costs - PROFIT_OVERHEAD_RUB`
- **Deal Gate**: Deals are surfaced if they exceed `MIN_PROFIT_RUB` (default 3000 RUB) and `MIN_PROFIT_RATIO` (default 12%), and are not labeled as `fake`.

---

## 8. Telegram Bot UI and FSM

The bot code resides in `techhunter/bot/`.

- **Hub Menu navigation (`bot/screens.py`)**: Designed as an edit-in-place menu. Screen switches update the original message (`edit_text`), reducing UI clutter. 
- **Back buttons**: Every screen features a navigation array containing a "Back" button routing to the parent layout.
- **FSM Subscription Guide (`bot/app.py`)**: Uses guided пошаговый dialogs for adding queries, prompting with examples, and setting price boundaries.
- **Onboarding and Status reporting**: Onboarding alerts are delivered in single, aggregated messages. The Status screen lists cycle performance, total samples, and baseline coverage.
- **Card Actions**: Deal alerts include feedback triggers 👍 and 👎. Reaction triggers are logged to `DealFeedback` to dynamically adjust filtering gates.

---

## 9. Guidelines for AI Code Editors

When modifying or extending this codebase, adhere strictly to these engineering practices:

### 1. Test Isolation Hook
Every single test file inside `tests/` MUST import `tests._dbsetup` as its **FIRST** import line:
```python
import tests._dbsetup  # MUST be first
```
This module intercepts session factories and redirects them to the isolated test database `data/test_suite.db`. If you omit this import, running tests will pollute the live production database (`data/techhunter.db`), destroying baseline learning.

### 2. Timezone Skew Prevention
SQLite stores datetimes as naive objects. When computing the age of pricing baselines or observations, never run raw `.timestamp()` calls. Timezone differences between the host machine and UTC will produce negative time spans. Always use the built-in age helper `_age_seconds` in `techhunter/valuation/devices.py`:
```python
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
```

### 3. Schema Changes and Alembic
Never call `Base.metadata.create_all()` directly for production databases. Database schemas must be modified via Alembic migrations.
- When generating migrations that add columns with `NOT NULL` constraints, you **must** supply a `server_default` inside the migration file to allow existing rows to populate safely on SQLite.

### 4. Zero Emojis and Zero Em-Dashes in System Code/Docs
Per project rules, no emojis or em-dashes (long dashes `—`) are allowed inside codebase comments, docstrings, developer messages, or documentation files (such as `AI_DOCUMENTATION.md`).
- Standard hyphens (`-`) must be used.
- Emojis are only allowed inside Russian user-facing Telegram copy within the bot code.

### 5. Multi-Worker Browser Safety
Do not modify the Playwright launch arguments to enforce headless mode in production. The visible browser window is critical to bypass Datadome/Avito scraping defenses.
