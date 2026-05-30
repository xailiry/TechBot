# Code Quality Audit: TechHunter Bot

Дата аудита: 2026-05-30.

Цель: найти не вкусовщину, а места, где код реально начинает пахнуть legacy: нарушение SOLID/DRY/KISS, спагетти-связи, дублирование, хрупкие тесты и зоны, которые будет больно развивать.

## Короткий вывод

Проект не выглядит как полностью джуновский хаос: есть миграции Alembic, модели, тестовые сценарии, попытка выделить `db/repository.py`, отдельные модули scraper/valuation/bot/monitoring. Но сейчас код находится в промежуточном состоянии после рефакторинга: старый `storage.py` живет параллельно с новым repository layer, тесты частично смотрят на старую архитектуру, а несколько "толстых" модулей смешивают UI, бизнес-правила, инфраструктуру и доступ к БД.

Самые проблемные зоны:

- `techhunter/storage.py` и `techhunter/db/repository.py` дублируют один и тот же DAL.
- `techhunter/bot/app.py`, `techhunter/bot/screens.py`, `techhunter/monitoring/poller.py` стали god-modules/god-classes.
- В dev-командах Telegram нет явной проверки админских прав.
- Тестовая инфраструктура сейчас не является надежным quality gate.
- Много широких `except Exception` / `contextlib.suppress(Exception)`, которые скрывают реальные дефекты.

## P0/P1: Нужно чинить в первую очередь

### 1. Два слоя доступа к БД живут одновременно

Файлы:

- `techhunter/storage.py:1-897`
- `techhunter/db/repository.py:1-553`
- `techhunter/monitoring/poller.py:160-162`
- `techhunter/ai/evaluate.py:176-183`
- `techhunter/bot/app.py:19-30`
- `techhunter/bot/screens.py:16-24`

`db/repository.py` прямо заявляет, что заменяет legacy `storage.py` и должен enforcing DIP (`repository.py:1-5`). Но по факту большая часть проекта все еще импортирует `storage.py`: bot handlers, screen builders, notifier, AI evaluation, CLI tools и tests.

Это нарушение DRY и DIP:

- Одна и та же логика есть в двух местах.
- Бизнес-код зависит то от репозиториев, то от глобальных функций.
- Поведение уже начало расходиться.

Пример расхождения: `RepositoryContainer` содержит `AlertRepository.mark_pending_failed()` (`techhunter/db/repository.py:397-407`), где используется `config.PENDING_ALERT_MAX_RETRIES`, но в `techhunter/config.py` такой настройки нет. В старом `storage.py` похожая логика называется иначе (`mark_pending_alert_attempt`, `techhunter/storage.py:808-829`) и использует другой backoff.

Риск: часть runtime-кода работает через новый DAL, часть через старый. Любая правка БД может починить один путь и сломать второй.

Рекомендация: выбрать один источник правды. Лучше довести `RepositoryContainer` до полного покрытия и оставить `storage.py` только как временный compatibility facade, который делегирует в репозитории. Затем убрать прямые импорты `storage.py` из bot/AI/valuation.

### 2. Тесты не соответствуют текущей архитектуре

Проверки:

- `python -m pytest -q` падает на async-тестах: нет `pytest-asyncio`/аналога.
- `python tests/run_all.py` падает в `tests.test_reliability`, `tests.test_browser_resilience`, `tests.test_calibration`, `tests.test_discovery`.

Конкретные несовпадения:

- `tests/test_browser_resilience.py:52-90` ожидает `AvitoBrowser()` без аргументов и поле `_available`, но текущий `AvitoBrowser.__init__` требует `SessionManager`, а пул называется `_pool` (`techhunter/scraper/browser.py:32-39`).
- `tests/test_reliability.py:124-134` патчит `techhunter.monitor._evaluate`, но после рефакторинга `_evaluate` стал методом `AvitoPoller` (`techhunter/monitoring/poller.py:92`).
- `tests/test_discovery.py:147-155` патчит `techhunter.monitor.process_new_listing`, но сейчас импорт и вызов находятся в `techhunter/monitoring/poller.py:12,139`.
- `tests/test_calibration.py` падает на проверке `CO cosmetic_wear`.

Это не просто "тесты красные". Это значит, что тесты проверяют старые швы системы. Рефакторинг уже произошел, а safety net остался в прошлом.

Рекомендация: сначала восстановить единый тестовый вход. Либо:

- добавить `pytest-asyncio` и маркировать async-тесты,
- либо официально оставить `tests/run_all.py`, но обновить тесты под `MonitorManager`/`AvitoPoller`/новый `AvitoBrowser`.

До этого крупный рефакторинг будет идти почти вслепую.

### 3. Dev-команды Telegram доступны без явного admin gate

Файлы:

- `techhunter/config.py:16-18`
- `techhunter/bot/app.py:138-157`
- `techhunter/bot/app.py:265-289`
- `techhunter/bot/app.py:464-517`
- `techhunter/bot/screens.py:486-503`

Есть `ADMIN_USER_IDS`, но он используется для operator broadcast в `TelegramNotifier`, а не для ограничения команд. Хендлеры `/dev`, `/retry_pending`, `/train_start`, `/train_stop` и dev-кнопки не проверяют, что пользователь админ.

Особенно опасные действия:

- `browser_restart`
- `train_start` / `train_stop`
- `clear_dead_outbox`
- `cleanup_old_rows`
- `relearn_stale`
- `retry_outbox`

Это уже не clean code, а security/design smell: UI-команды инфраструктурного уровня лежат рядом с обычными пользовательскими командами.

Рекомендация: ввести декоратор/guard `require_admin(cb_or_msg)` и закрыть все dev routes. В screen layer лучше не показывать `/dev` не-админам.

## P2: Архитектурные запахи

### 4. God modules и длинные функции

Самые тяжелые файлы по размеру:

- `techhunter/storage.py` - 897 строк.
- `techhunter/bot/app.py` - 686 строк.
- `techhunter/bot/screens.py` - 640 строк.
- `techhunter/valuation/devices.py` - 574 строки.
- `techhunter/db/repository.py` - 553 строки.
- `techhunter/valuation/engine.py` - 441 строка.

Самые сложные функции по грубой цикломатике:

- `techhunter/valuation/scam.py:138 score_listing` - примерно 48 ветвлений.
- `techhunter/valuation/engine.py:280 value_listing` - примерно 41.
- `techhunter/bot/format.py:107 format_deal_card` - примерно 39.
- `techhunter/ai/evaluate.py:59 evaluate_listing` - примерно 36.
- `techhunter/monitoring/poller.py:153 _handle_items` - примерно 27.
- `techhunter/monitoring/poller.py:164 _handle_one` - примерно 26.

Проблема не в длине сама по себе. Проблема в том, что эти функции смешивают разные причины для изменения:

- `value_listing` одновременно учит рынок, выбирает baseline, считает profit, вызывает scam scoring, решает opportunity.
- `score_listing` одновременно нормализует текст, считает fraud score, формирует человекочитаемые причины.
- `bot/app.py` одновременно роутит Telegram, парсит команды, пишет в БД, запускает maintenance-действия.
- `screens.py` одновременно строит UI, ходит в БД и читает runtime snapshot.

Рекомендация: резать по use-case сервисам:

- `DealEvaluationService`
- `MarketLearningService`
- `FeedbackCalibrationService`
- `AdminCommandService`
- `BotScreenService` без прямого SQL/storage внутри screen builders

### 5. Широкое подавление исключений скрывает баги

Примеры:

- `techhunter/monitoring/poller.py:58-64` - `_collect` глотает любую ошибку и просто `return`.
- `techhunter/monitoring/poller.py:208-214` - ошибка отправки карточки сохраняется в outbox, но ошибка очереди тоже подавляется.
- `techhunter/pipeline.py:29-33` - ошибка `fetch_details` превращается в тихий fallback к карточке.
- `techhunter/pipeline.py:39-44` - ошибка CLIP/dedup refine логируется на debug и pipeline идет дальше.
- `techhunter/scraper/browser.py:240-248` - captcha check возвращает `False` на любую ошибку.
- `techhunter/bot/app.py:556-563` - ошибка редактирования сообщения полностью подавляется.

Часть таких suppress оправдана для long-running бота, но сейчас нет четкой политики: где ошибка ожидаемая, где деградация, а где надо поднимать alarm. Это ухудшает observability и делает баги "тихими".

Рекомендация: завести доменные исключения и уровни деградации:

- expected Telegram edit errors - suppress/log debug;
- network/browser transient - warning + метрика;
- DB/outbox failure - error + watchdog;
- invariant violation - exception, не suppress.

### 6. SOLID/DIP нарушается прямыми импортами инфраструктуры из домена

Примеры:

- `techhunter/ai/evaluate.py:179-183` внутри AI evaluation импортирует `storage` и пишет image hashes.
- `techhunter/valuation/devices.py` и `techhunter/valuation/repair.py` напрямую импортируют `get_session`.
- `techhunter/bot/app.py:296-307` делает raw SQL прямо в handler `/train_stat`.
- `techhunter/bot/app.py:407-437` callback Discovery импортирует `User`, `get_session` и напрямую читает БД.
- `techhunter/bot/screens.py:172-212` screen builders тоже ходят в БД.

Из-за этого доменная логика плохо тестируется отдельно от SQLite и сложно меняется. Сейчас dependency inversion начат через `RepositoryContainer`, но не доведен до конца.

Рекомендация: AI/valuation должны получать нужные gateway/repository зависимости снаружи или через service layer. UI должен общаться с application services, а не с SQLAlchemy.

## P3: Локальные clean-code проблемы

### 7. Неиспользуемые и дрейфующие настройки

Примеры:

- `config.PAGE_ACQUIRE_TIMEOUT_SEC` объявлен (`techhunter/config.py:43-45`), тесты ожидают transient tab behavior, но текущий `AvitoBrowser.acquire_page()` просто ждет `_pool.get()` без timeout (`techhunter/scraper/browser.py:215-236`).
- `config.SUBSCRIPTION_STAGGER_SEC` объявлен (`techhunter/config.py:65-66`), но не используется.
- `config.PENDING_ALERT_MAX_RETRIES` используется в `db/repository.py:405`, но не объявлен.
- `DEFAULT_SEARCH_PAGES`, `DHASH_SCAN_LIMIT`, `JUNK_PRICE_MIN` оставлены как aliases; это нормально временно, но стоит пометить срок удаления.

Рекомендация: добавить тест/линтер на "config declared but unused" хотя бы вручную в audit check, а undefined config ловить type checker/ruff через более строгую конфигурацию.

### 8. Статический анализ не настроен как gate

В проекте нет `pyproject.toml`/`ruff.toml`. Команда `python -m ruff check techhunter tests main.py` нашла 25 проблем:

- unused imports: `Condition`, `Any`, `DeviceCatalog`, `MarketBaseline`;
- `E701` multiple statements on one line;
- import not at top of file в `techhunter/valuation/engine.py:34`;
- мелкие проблемы в тестах.

Это не катастрофа, но отсутствие общего lint gate позволяет стилю расползаться. Особенно видно по однострочным `if x: return`, которые ухудшают читаемость в уже сложных функциях.

Рекомендация: добавить `pyproject.toml` с ruff, сначала с мягкими правилами, затем постепенно включить complexity/import rules.

### 9. Глобальные singleton/state усложняют тестирование

Примеры:

- `techhunter/scraper/browser.py:328-351` - глобальный `_browser` и `_browser_lock`.
- `techhunter/runtime.py` - глобальный runtime snapshot, который пишется из разных loops.
- `techhunter/bot/app.py:52` - глобальный `Dispatcher`.

Для одного процесса это прагматично, но тесты уже страдают: они патчат модули и приватные поля, а после рефакторинга ломаются.

Рекомендация: оставить singleton только на composition root (`main.py` / `MonitorManager`), а остальным слоям передавать зависимости явно.

## Что хорошо

- Есть Alembic migrations и SQLAlchemy models.
- Есть понятные доменные зоны: scraper, ai, valuation, bot, monitoring.
- Есть попытка уйти от legacy DAL в сторону repositories.
- Много бизнес-сценариев покрыто тестовыми скриптами.
- Long-running поведение продумано: outbox, watchdog, captcha pause, browser restart.

То есть проблема не в том, что "все плохо". Проблема в том, что проект уже перерос начальную архитектуру, а миграция на более чистые слои не завершена.

## Рекомендуемый порядок разгребания

1. Починить тестовый gate: выбрать `pytest` + `pytest-asyncio` или обновленный `tests/run_all.py`, убрать stale-patching старого `monitor`.
2. Закрыть dev/admin handlers через `ADMIN_USER_IDS`.
3. Свести DAL к одному источнику правды: `RepositoryContainer`; `storage.py` временно сделать facade.
4. Вынести feedback/personal penalty из `storage.py` в отдельный service.
5. Разрезать `AvitoPoller._handle_items/_evaluate` на use-case методы: collect, evaluate, filter, deliver.
6. Разделить `value_listing` на baseline selection, profit calculation, repair calculation, opportunity decision.
7. Добавить ruff config и постепенно включить complexity checks.
8. Убрать/документировать dead config и undefined config references.

## Команды, которыми проверялось

```powershell
python -m ruff check techhunter tests main.py
python -m pytest -q
python tests\run_all.py
python -m tests.test_calibration
python -m tests.test_browser_resilience
python -m tests.test_discovery
python -m tests.test_reliability
```

