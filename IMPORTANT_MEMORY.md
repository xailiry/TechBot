# IMPORTANT_MEMORY - read this first if you lost context

You are Claude working with the operator on TechHunter_Bot. If your context
was reset, read THIS file, then ROADMAP.md, then MAJOR_FIXES.md, then the
auto-memory files. After that you are caught up. Do not re-ask the operator
things already decided here.

## 0. Hard rules (operator's CLAUDE.md - obey exactly)

- Read files before writing; do not re-read unchanged files.
- Thorough reasoning, concise output. No sycophantic openers/closers.
- No emojis and no em-dashes in your output, code, comments, docs.
  EXCEPTION: emojis are allowed ONLY inside the bot's user-facing Russian
  UI strings (product copy), because the product uses them and the
  operator wants that. Never in code/docs/your chat replies.
- Do not guess APIs/versions/flags/SHAs/package names. Verify by reading.
- Operator writes in Russian; reply in Russian.

## 1. What the project is

Telegram bot that monitors Avito for underpriced used phones (iPhone +
flagship Android) for resellers ("перекупы"). It learns real market prices
per condition, extracts specs/defects, computes resale margin (including
"broken under repair" flips), runs a scam guard, and pushes deal cards.
Implementation fully delegated to Claude; no fixed stack was imposed.

Core business premises (operator-given, do not relitigate):
- Visible Playwright browser (headless=False) almost never gets captcha;
  when it does, human solves it once and bot resumes. This is THE
  anti-bot strategy. Do not switch to headless.
- Prices must be REAL (learned from real listings or operator-entered),
  never fabricated. (See memory feedback_pricing.md.)
- Segment for MVP: iPhone + flagship Android (Samsung S/Z/Note, Pixel).
- AI is LOCAL ONLY (CLIP + regex), no external LLM/API.
- Avito only for MVP; source abstraction is future backlog.

## 2. Workflow protocol with the operator

- Deliver a staged plan; execute ONE stage/milestone at a time.
- Inside a milestone work autonomously (operator said so for M1+).
- Ask for explicit go ONLY at major transitions (M2, M3, big batches).
- Never auto-commit. Operator commits/pushes on explicit request only.
- After each stage: short summary + "что тебе сделать вручную" if any.
- Keep ROADMAP.md "Журнал решений" updated when a milestone/hotfix lands.

## 3. Environment / how to run

- OS Windows. Python 3.13 venv at:
  C:\Users\bogat\Downloads\NoCodeProjects\venv
- This Claude session cwd is the PARENT C:\Users\bogat\Downloads\
  NoCodeProjects (where the operator's CLAUDE.md and project memory live).
  The git repo is the SUBDIR TechHunter_Bot. /ultrareview needs cwd to be
  the repo, so it must be run from a session started in TechHunter_Bot.
- Run bot:   ..\venv\Scripts\python.exe main.py   (from TechHunter_Bot)
- Tests:     ..\venv\Scripts\python.exe -m tests.run_all   (8 suites)
- Migrations: ..\venv\Scripts\python.exe -m alembic upgrade head
- BOT_TOKEN is in .env (gitignored). Token value lives only in .env.
- Prod DB: data/techhunter.db (gitignored). Tests are ISOLATED to
  data/test_suite.db via tests/_dbsetup.py (imported FIRST in every test
  module). NEVER let tests touch the prod DB again - that caused the
  82000 baseline pollution incident; isolation is the fix, keep it.

## 4. Architecture (packages under techhunter/)

- scraper/: browser.py (visible persistent Chromium, page pool,
  single-flight captcha gate, _probe_clear on a throwaway tab),
  parser.py (data-marker selectors, looks_blocked/looks_loading),
  urls.py, models.py (ParsedListing), stealth.py.
- ai/: specs.py (regex specs/defects), condition.py (grading),
  normalize.py (free text -> canonical device), clip_engine.py (lazy
  CLIP, _to_embed coerces transformers output), images.py (download +
  dhash), evaluate.py (orchestrator -> EvaluationReport).
- valuation/: clustering.py (robust_median - centers on USED bulk, not
  upper cluster), devices.py (catalog, log_observation, relearn,
  get_baseline/get_working_meta, per-condition tiers, learning_overview,
  prices_for_model), repair.py (RepairCost), scam.py (score_listing,
  looks_shoplike), engine.py (Valuation, value_listing).
- bot/: app.py (aiogram dp, command shortcuts, nav router, FSM add,
  callbacks), screens.py (hub/subs/settings/status/prices/quality
  builders), cards.py (deal card send + state), format.py (card text,
  parse_command, onboarding texts), notifier.py (TelegramNotifier).
- monitor.py (poll loop, onboarding, eval cache, delivery), pipeline.py,
  storage.py (all DB helpers), db/ (models, session, base), runtime.py
  (status snapshot), delivery.py (per-user filters), notifier.py
  (Notifier protocol + ConsoleNotifier), textnorm.py (shared homoglyph
  normalization + ratio), tools/calibrate.py (operator CLI).

## 5. What we built (timeline / decisions)

- Stages 0-5 done: storage (SQLAlchemy2 async + Alembic), visible
  scraper + captcha suspension, AI specs/condition/CLIP/dhash,
  valuation (baseline + broken-flip + scam), Telegram bot, tests.
- Pushed to GitHub: github.com/xailiry/TechBot, branch main.
- Pricing redesigned to CONTINUOUS self-learning: per-condition medians
  (working/ideal/good/defect/broken/for_parts), no manual lock,
  onboarding deep crawl on new subscription + periodic silent refresh
  (ONBOARDING_REFRESH_SEC, ~90 min), persisted Subscription.onboarded_at
  (no re-onboard on restart), in-memory _refreshed_run forces one
  relearn per process start. set_manual_baseline is a soft seed only.
- Key hotfixes already in code: robust_median centered on used bulk
  (was biased up to shop/new -> 60k vs ~44k); looks_shoplike excludes
  shops/refurb/wholesale from LEARNING only (still delivered as deals);
  tiers anchored to working with sane bands, illogical tiers deleted;
  CLIP _to_embed fix (transformers 5.x returns BaseModelOutputWithPooling);
  homoglyph normalization shared in textnorm and applied in specs +
  normalize; defect regex stems broadened (диспл\w*/экран\w*/модул\w*)
  and negation guard _NEG_CLEAN ("не менялся/без замены/не разбит").
- M1 UI: hub menu, single-message nav with Back, FSM guided add,
  Settings screen with per-user delivery filters (min_score,
  exclude_conditions, exclude_shop), Status screen, redesigned deal
  card (profit headline), noise reduction.
- M2 reliability: Listing became eval cache (processed_at +
  report_json/valuation_json) -> dedup AFTER success (transient failure
  retried, not lost); per-user delivery dedup via SentAlert; eval cache
  TTL LISTING_CACHE_TTL_SEC + price invalidation + in-cycle cache;
  cycle metrics + drop reasons on Status.
- M3 transparency: baseline confidence on card (sample+freshness, CONF_*
  in config), feedback buttons up/down -> DealFeedback table,
  per-subscription "Цены по состояниям" screen, "Качество" screen.
  Calibration = surface data + levers; NO auto-mutation of thresholds.
- DB was wiped several times to clear test/old pollution; current prod
  DB is clean and learning from live runs.

## 6. Git state (critical - do not destroy)

- Remote: git@github.com:xailiry/TechBot.git, branch main.
- Last COMMITTED: 0ddc970 "Hotfix: detect replaced-display spam,
  homoglyph-proof extraction".
- STAGED but NOT committed (await operator's commit word): SQLite
  WAL+busy_timeout (db/session.py), UTC-safe age (valuation/devices.py),
  negated-defect cleaning (ai/specs.py), .gitignore (+.claude/),
  CLAUDE.md added, plus regression tests. Run `git status` to confirm.
  Do NOT discard these; commit only when operator says.
- Untracked docs: ROADMAP.md is committed; MAJOR_FIXES.md and this
  IMPORTANT_MEMORY.md are new (stage/commit on operator word).

## 7. Open work (NOT yet done - deferred by operator to "later")

A 3-reviewer audit (business, senior dev, Apple reseller) produced a
prioritized fix list in MAJOR_FIXES.md. NOTHING from it is implemented
yet. Plan: P0 (unattended reliability: page-pool deadlock, no browser
auto-restart, sync CLIP/PIL in event loop, infinite captcha wait +
brittle block detect, no watchdog alert, unbounded DB growth) -> P1
(money bugs: carrier-lock/Neverlock, device version not in
identity/pricing, swapped-screen/True Tone, battery cycles, hard-blocked
gems) -> P2 (broken-flip default repair prices, speed, listing age +
content dedup, card content, profit honesty, model coverage, use
feedback) -> P3 (techdebt). Gate between P0/P1/P2 with operator OK.

PENDING OPERATOR DECISIONS before planning fixes:
1. CLIP: drop by default vs move off-thread (asyncio.to_thread).
2. Strategy: Avito ToS / monetization direction.

## 8. Gotchas learned (do not repeat mistakes)

- Tests must stay isolated (tests/_dbsetup.py). If a new test forgets
  `import tests._dbsetup` first, it pollutes prod DB.
- SQLite naive datetimes are UTC; use the UTC-safe age helpers, never
  raw .timestamp() (tz skew can make age negative under +TZ).
- Do not take the upper price cluster as baseline (inflates to retail).
- Defect regex must handle Russian inflection and negation.
- aiogram 3.13.1, FSM uses default MemoryStorage. Dispatcher created in
  bot/app.py module scope; handlers registered by import order.
- Alembic is schema canon for prod; tests use create_all (metadata).
  Adding NOT NULL columns to SQLite needs server_default in the
  migration (learned the hard way).
- run_all must be 8/8 green before declaring anything done.

## 9. Pointers

- ROADMAP.md - milestones + decision log (keep updated).
- MAJOR_FIXES.md - the pending audit action list (P0-P3).
- Auto-memory dir: C:\Users\bogat\.claude\projects\
  C--Users-bogat-Downloads-NoCodeProjects\memory\ (MEMORY.md index,
  project_techhunter.md, feedback_pricing.md, audit_majorfixes.md).
