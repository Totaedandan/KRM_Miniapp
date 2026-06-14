# CLAUDE.md

Контекст проекта для Claude Code. Обновляй этот файл при значимых изменениях архитектуры.

## Что это за проект

Telegram-бот для проверки работ через **Turnitin** (плагиат + AI-детекция) с **Telegram Mini App**.
Дополнительно: «Хуманайзер» текста (StealthGPT), оплата через Kaspi (PDF-чеки) и баланс в тенге/токенах.

- **Язык/стек:** Python 3.11, aiogram 3.x (long-polling), FastAPI + uvicorn (Mini App API), Playwright (автоматизация Turnitin в headful + Xvfb), aiosqlite (SQLite).
- **Активный код — в каталоге `bot/`.** В корне есть устаревшая копия (main.py, config.py, handlers/ и т.д.) — НЕ используется, не трогать.
- Запуск: `bot/main.py` поднимает одновременно бота (polling) и FastAPI на порту `MINI_APP_PORT` (8000).

## Структура (bot/)

- `main.py` — точка входа: инициализация БД, роутеры, запуск воркера очереди, бот + API через `asyncio.gather`.
- `config.py` — `settings` (pydantic-settings, читает `.env`). Обязательны: `BOT_TOKEN`, `SUPERADMIN_ID`. `ADMIN_IDS` (через запятую) + суперадмин проходят флоу **бесплатно** (как whitelist) — `settings.is_admin(tg_id)`.
- `api.py` — FastAPI: эндпоинты Mini App (`/api/...`) + админские (`/api/admin/...`). Авторизация — через Telegram `initData` (заголовок `X-Telegram-Init-Data`).
- `bot_sender.py` — отправка сообщений из FastAPI через HTTP Bot API (без aiogram).
- `database/db.py` — весь доступ к SQLite. Таблицы: `users`, `turnitin_orders`, `payments`, `kaspi_receipts`, `settings`, `banned_usernames`, `transactions` (ledger). Динамические настройки (цены, реквизиты, Turnitin-креды) — в таблице `settings`.
- `services/`
  - `turnitin_service.py` — Playwright-автоматизация Turnitin. На Linux сам поднимает Xvfb (headful Chromium). `process(...)` загружает файл, ждёт отчёт, возвращает пути sim/ai. `cleanup(...)` закрывает браузер + чистит старые файлы.
  - `queue_manager.py` — очередь заказов Turnitin (см. ниже).
  - `humanizer_service.py`, `kaspi_service.py`.
- `handlers/` — `start, turnitin, humanizer, payment, profile, admin, webapp`.
- `keyboards/`, `middlewares/register.py`.
- `../mini_app/index.html` — Mini App (React через CDN, один файл). FastAPI отдаёт его по `/` (путь `/app/mini_app/index.html` в Docker).

## Жизненный цикл заказа Turnitin

Статусы `turnitin_orders.status`: `pending` → `paid`/`queued` → `awaiting_file` → `ready` → `processing` → `done`/`error`/`cancelled`.
`report_type`: `sim` (плагиат) | `ai` (AI-детекция) | `both`.

## Премиум-очередь (РЕАЛИЗОВАНО — см. схему от клиента)

Реализация: `services/queue_manager.py` (DB-backed контроллер с таймерами и воркером),
статусы заказа `queued → awaiting_file → ready → processing`. Заказ создаётся сразу в
статусе `queued` (без файла); контроллер сам запрашивает файл и обрабатывает таймауты.
API: `POST /api/order` (поле `is_premium`), `GET /api/admin/queue`, `POST /api/admin/cancel_order`.
Бот: `/cancelorder <id>`, `/setturnitin class_id_premium|assign_id_premium|premium_mult`.

Две очереди внутри Turnitin (два **класса Turnitin**, тот же аккаунт, разные `class_id`/`assignment_id`):
- **Обычная:** списывает по прайсу (×1). Файл запрашивается «точно в срок» — когда подходит очередь («остался 1 человек перед тобой»), даётся 3 минуты. Не успел → возврат денег + потеря места.
- **Премиум:** списывает **×1.5** от прайса. Место бронируется сразу, файл запрашивается немедленно (3 мин), обработка с приоритетом.
- Везде показывается число людей в очереди + ETA (**1 файл = 7 минут**, ожидание = файлы × 7 мин).
- Деньги берутся при бронировании; при отмене/просрочке — возврат на тенге-баланс.

Админ (Mini App): просмотр обеих очередей со списком (id, услуга, статус, **сумма**) и **отмена по id с автовозвратом** денег на баланс пользователя.

Константы: `PREMIUM_MULTIPLIER=1.5`, `FILE_TIMEOUT_SEC=180`, `MINUTES_PER_FILE=7`.

## Аренда ИИ-аккаунтов (РЕАЛИЗОВАНО)

Каталог аренды премиум-аккаунтов (ChatGPT Plus, Claude Pro и т.д.):
выдача **полностью автоматическая** из заранее загруженного пула логинов/паролей.

- **Таблицы** (`database/db.py`): `rental_services` (каталог), `rental_tariffs`
  (часы/неделя/месяц/год, `duration_hours`, цена ₸), `rental_accounts` (склад кредов,
  статусы `free|rented|maintenance|disabled`), `rental_orders` (`active|expired|cancelled`,
  `expires_at` naive-UTC), `rental_waitlist` (UNIQUE(service_id, user_id)).
- **Покупка**: `create_rental_order(...)` — атомарно в одной транзакции: списание тенге
  (guard `tenge_balance>=?`) + захват аккаунта (guard `status='free'`, гонка за последний
  аккаунт исключена) + INSERT аренды + ledger. Исключения: `InsufficientFunds` (402),
  `NoFreeAccount` (409). Идемпотентность: `rental:<tg_id>:<request_id>`.
- **Возврат**: `cancel_rental_with_refund` — ключ `rental_refund:<order_id>`
  (НЕ `refund:<id>` — этот namespace занят turnitin). Аккаунт → `maintenance`.
- **Воркер** `services/rental_manager.py` (старт из `main.py`): тик 60с — напоминание за
  ~30 мин (`reminder_sent`) и истечение (заказ → `expired`, аккаунт → `maintenance`,
  юзеру уведомление). Состояние в БД — рестарт ничего не теряет.
- **Waitlist**: «Уведомить» → `POST /api/rental/notify`. Когда админ возвращает аккаунт
  в `free` (после ротации пароля) или добавляет аккаунт в пустой сервис —
  `rental_manager.notify_waitlist()` шлёт сообщение ВСЕМ ожидающим («кто успел»),
  записи waitlist удаляются.
- **API**: `GET /api/rental/services` (каталог, без кредов), `POST /api/rental/order`,
  `GET /api/rental/my` (креды только у активных), `POST /api/rental/notify`.
  Админ: `/api/admin/rental/services|service|tariff|accounts|account[/update|/delete]|orders|cancel`.
  Whitelist-бесплатно у аренды НЕТ — инвентарь конечен, платят все.
- **Пароли хранятся плейнтекстом** в SQLite: не логировать, не отдавать из каталога,
  в админке замаскированы (tap-reveal).
- `transactions.order_id` для reasons `rental_charge|rental_refund` ссылается на
  `rental_orders.id` (иначе — `turnitin_orders.id`); различать по `reason`.
- **Иконки сервисов**: `rental_services.icon` хранит либо ключ из `BRANDS` в
  `mini_app/index.html` (официальные SVG-логотипы, заинлайнены: openai, claude, grok,
  perplexity, gemini, figma, netflix, notion, microsoft, canva, midjourney), либо эмодзи —
  компонент `RentIcon` разруливает сам. Монохромные логотипы (`currentColor`) адаптируются к теме.
- **Мок-каталог**: `python _seed_rental_catalog.py` — идемпотентно заливает 12 сервисов
  с тарифами и демо-аккаунтами (фейковые креды, перед продом заменить/удалить в админке).
- Smoke-тест слоя БД: `python _test_rental_db.py` (временная БД: гонки, идемпотентность, возвраты).
  E2E-тест API: `python _test_rental_api.py` (TestClient, реальная HMAC-авторизация initData).

## Mini App: дизайн-система

- **Две темы** (light кремовая / dark) через CSS-токены на `[data-theme]`; boot-скрипт
  до React: `localStorage.theme || tg.colorScheme`. Переключатель — на Главной.
- **Навбар**: активная вкладка — анимированная «пилюля» (flex-grow + label, 250ms ease-out).
  Вкладки: Главная | Аренда | Кошелёк | Профиль (+Админ). Turnitin/Хуманайзер — с карточек
  Главной (с кнопкой «Назад»).
- Принципы анимаций — скилл `.claude/skills/emil-design-eng/SKILL.md` (Emil Kowalski):
  ease-out, ≤300ms, только transform/opacity, без анимаций частых действий.
- Градиент `--grad` — только на карточке баланса; остальное — solid `--accent`.
- **Даты с сервера naive-UTC** → на фронте парсить через `parseUTC()` (добавляет 'Z'),
  иначе таймеры уедут на локальный сдвиг (+5ч в KZ).

## Баланс через ledger (РЕАЛИЗОВАНО)

Деньги движутся ТОЛЬКО через журнал `transactions` (debit/credit, currency=tenge|token,
reason, balance_after, idempotency_key UNIQUE). Колонки `users.tenge_balance/token_balance`
— кэш, меняется исключительно вместе с записью в журнал.

- `_record_movement(...)` — атомарно: изменить баланс + записать строку ledger в одной БД-транзакции.
  `add_tenge/deduct_tenge/add_tokens/deduct_tokens` — тонкие обёртки над ним (принимают `reason`,
  `order_id`, `idempotency_key`).
- `create_paid_order(...)` — атомарно списать тенге + создать заказ (`queued`) + ledger в одной
  транзакции. Используется в `POST /api/order` и `webapp._start_turnitin`. Идемпотентность по
  `idempotency_key=order:<tg_id>:<request_id>` (Mini App шлёт `request_id`, ref в `TurnitinScreen`).
  При нехватке средств — `db.InsufficientFunds`. Whitelist идёт мимо (через `create_order`, 0 ₸).
- Возврат: `cancel_order_with_refund` пишет credit с `idempotency_key=refund:<order_id>` → деньги
  не вернутся дважды (важно: cancel зовётся и по таймауту, и админом).
- Пополнения/покупки токенов journaled с `reason` (topup/token_purchase/admin_adjust/humanizer),
  Kaspi — с ключом по `receipt.transaction_id`.
- Бэкфилл в `init_db`: старые балансы заносятся как `opening_balance` (ключ `opening:<cur>:<tg_id>`).
- Админка: `GET /api/admin/transactions?q=` (история), `GET /api/admin/ledger_check` (сверка
  кэша с журналом, `reconcile_balances()` → пустой список = всё сходится).

## Docker

- `Dockerfile` (корень) собирает образ: ставит системные либы для Chromium + Xvfb, pip-зависимости из `bot/requirements.txt`, `playwright install chromium`, копирует `bot/` → `/app/bot/` и `mini_app/` → `/app/mini_app/`. WORKDIR `/app/bot`, `ENTRYPOINT /app/entrypoint.sh`.
- **`entrypoint.sh` ОБЯЗАТЕЛЕН:** Chromium запускается в headed-режиме (`headless=False` для обхода антибота Turnitin), поэтому нужен X-сервер. entrypoint поднимает `Xvfb :99`, экспортирует `DISPLAY=:99`, затем `exec python main.py`. Без него Playwright падает: «Missing X server or $DISPLAY».
- **Возобновление очереди после краха/перезапуска** (`queue_manager._startup_recover`): `processing → ready`, `awaiting_file → queued`, а также `error` с сохранённым файлом → `ready` (до `MAX_PROCESS_RETRIES=3` повторов). Бот сам продолжает обрабатывать заказы по списку.
- `docker-compose.yml`: сервис `bot`, порт `8000:8000`, volume `./data:/app/bot/data` (БД/отчёты/загрузки) и `./bot/assets:/app/bot/assets` (QR Kaspi), `shm_size: 256mb`.
- `.dockerignore` исключает venv, __pycache__, локальные данные.
- БД: `DATABASE_PATH=data/bot.db` в `.env` (внутри volume — переживает перезапуск).
- `MINI_APP_URL` в `.env` — ngrok-адрес (нужен HTTPS для Telegram Mini App), проксирует на `localhost:8000`.

Команды:
```
docker compose up -d --build   # собрать и запустить
docker compose logs -f         # логи
docker compose down            # остановить
```

## Важные замечания

- **Очистка кэша** (`/api/admin/cleanup`) НЕ трогает БД (бан-лист, статистика, история заказов сохраняются) и НЕ удаляет файлы заказов, ещё активных в очереди.
- В `.env` лежат реальные секреты (BOT_TOKEN, Turnitin-креды) — не публиковать.
- Хардкод Mac-пути к Chromium в `turnitin_service.py` используется только в ветке non-Linux; в Docker (Linux) не задействован.
