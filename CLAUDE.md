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
- `database/db.py` — весь доступ к SQLite. Таблицы: `users`, `turnitin_orders`, `payments`, `kaspi_receipts`, `settings`, `banned_usernames`, `transactions` (ledger), `promo_codes`, `promo_redemptions`. Динамические настройки (цены, реквизиты, Turnitin-креды) — в таблице `settings`.
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

## Аренда ИИ-аккаунтов v2 (РЕАЛИЗОВАНО — email+OTP, авто-разлогин, прокси-группы)

Полностью заменяет старую систему логин/пароль (см. ниже «старая версия»).
Юзеру выдаётся **email на своём домене** (`login@EMAIL_DOMAIN`), код входа
приходит на почту и перехватывается Cloudflare Worker'ом, по истечении аренды
бэкенд сам разлогинивает аккаунт через Playwright. Аккаунты группируются по
2-3 на один ISP-прокси с очередью разлогина, чтобы не долбить один IP параллельно.

- **Каталог переиспользуется как есть**: `rental_services`/`rental_tariffs` (те же
  таблицы, что и в старой версии) — концепция «услуга + тариф на N часов» не менялась.
- **Новые таблицы** (`database/db.py`): `ai_proxies` (`proxy_url`, `max_accounts`),
  `ai_accounts` (`email` UNIQUE, `proxy_id`, `cookies_data` JSON, статусы
  `available|rented|cooldown|maintenance|disabled|banned`), `ai_rentals` (аналог
  rental_orders: `paid_bonus`/`paid_main` для отображения, источник правды — ledger),
  `otp_incoming_codes` (`recipient_email`, `otp_code`, короткое окно жизни —
  сюда же кладётся и magic-link Claude, см. «OTP / magic-link» ниже). Старые
  `rental_accounts`/`rental_orders` НЕ удалены (не используются, история цела).
- **Покупка**: `create_ai_rental(...)` — атомарно: `_apply_bonus_debit` (бонус
  сначала, потом тенге) + захват **LRU-свободного** аккаунта (`ORDER BY
  (last_used_at IS NULL) DESC, last_used_at ASC` — свежедобавленные и дольше
  отдыхавшие уходят первыми) + INSERT `ai_rentals` + ledger (`reason=ai_rental_charge`).
  Исключения: `InsufficientFunds` (402), `NoFreeAccount` (409). Идемпотентность:
  `rental:<tg_id>:<request_id>`.
- **Возврат**: `cancel_ai_rental_with_refund` — `_get_charge_split` возвращает
  каждую валюту туда, откуда списалась (бонус/тенге), не всегда в тенге (иначе
  можно отмыть бонус в реальные деньги через покупку+отмену). Аккаунт → `cooldown`.
- **OTP / magic-link**: `POST /api/email-hook` принимает от Cloudflare Worker'а
  либо `{recipient_email, otp_code}` (код прямо в письме — большинство сервисов),
  либо `{recipient_email, magic_link}` (Claude — шлёт только ссылку "Sign in",
  код рисуется их собственным JS уже на странице). Авторизация — заголовок
  `X-Webhook-Secret` (constant-time сравнение с `EMAIL_WEBHOOK_SECRET` из `.env`,
  **не Telegram initData** — это внешний публичный вебхук). Оба случая кладутся
  в `otp_incoming_codes` через один и тот же `insert_otp_code` — для magic-link
  значение колонки `otp_code` просто оказывается полной ссылкой вместо цифр.
  `GET /api/rental/otp?email=` отдаёт `{ok, code}` как раньше (окно поиска
  расширено с 120 до 600с — ссылка живёт дольше пары минут); Mini App
  (`OtpButton`) сам отличает ссылку от кода по префиксу `http(s)` и рисует
  Копировать/Открыть вместо голого кода — владение email проверяется через
  `get_active_ai_rental_by_email` (нельзя подсмотреть чужие данные). Скрипт
  воркера — `cloudflare/email-worker.js` (не задеплоен автоматически —
  вставляется вручную в Cloudflare Dashboard, инструкция в шапке файла).
  Раньше magic-link пытался открыть наш Playwright-браузер
  (`resolve_magic_link_otp`, удалена) — на практике заметная доля попыток
  упиралась в Cloudflare Turnstile на самом claude.ai даже с прокси на
  аккаунте (риск-скоринг серверных IP). Теперь ссылку просто отдаём юзеру,
  он открывает её в своём браузере (реальный IP, риска нет) и либо сразу
  логинится, либо видит код на странице сам — как это делают конкуренты.
  Намеренно нет обратного отсчёта "истекает через X" — точный TTL magic-link
  у Claude не задокументирован, выдумывать цифру рискованно.
- **Авто-разлогин**: `services/ai_rental_service.py` — Playwright, **новый
  browser/context на каждую задачу** (не персистентный браузер, как у Turnitin),
  `proxy={...}` привязан к прокси конкретного аккаунта. `auto_logout(account,
  service_type, proxy_url)` логинится по сохранённым `cookies_data` →
  `chatgpt.com/#settings` / `claude.ai/settings/account` → «Log out of all
  devices/sessions». `service_type` берётся из `rental_services.icon`
  (`openai`→chatgpt, `claude`→claude) — другие сервисы (grok/notion/figma/…)
  авто-разлогин пока не поддерживают, уходят в `maintenance` для ручного разбора.
  **Селекторы — первая версия**, потребуют донастройки на реальных страницах
  (как было с Turnitin).
- **Воркер** `services/ai_rental_manager.py` (заменил `rental_manager.py`, старт
  из `main.py`): тик 60с — напоминания (~30 мин), истечение (`ai_rentals` →
  `expired` сразу + `logout_account()` в фоне), cooldown→available через
  `COOLDOWN_MIN=5` мин → `notify_waitlist`. Очередь на прокси — БЕЗ Redis/Celery
  (в проекте их нет): `dict[proxy_id, asyncio.Lock]` + пауза `PROXY_GAP_SEC=20`
  между задачами на одном IP. `logout_account()` публичный — дёргается также из
  `api.py` при отмене админом и `force_logout`.
- **API**: `GET /api/rental/services` (каталог, без кредов), `POST /api/rental/order`,
  `GET /api/rental/my` → `{active, history}`, `POST /api/rental/notify`,
  `GET /api/rental/otp?email=`. Админ: `/api/admin/rental/service|tariff[/delete]`
  (общий каталог), `/api/admin/ai/proxies[/delete]`,
  `/api/admin/ai/accounts[/update|/delete|/force_logout]`, `/api/admin/ai/otp_logs`,
  `/api/admin/rental/orders|cancel`. Whitelist-бесплатно НЕТ — платят все.
- **Пароли не хранятся** — вход только через email+OTP, поэтому `cookies_data`
  (сессия) — единственный секрет на аккаунте, не логировать/не отдавать из каталога.
- Smoke-тест слоя БД: `python _test_ai_rental_db.py` (прокси-guard, LRU-выбор,
  бонус+тенге списание/возврат, идемпотентность, cooldown-окно).

### Старая версия (логин/пароль, НЕ используется, код/таблицы не удалены)

`rental_accounts` (склад кредов, `free|rented|maintenance|disabled`),
`create_rental_order`/`cancel_rental_with_refund`/`services/rental_manager.py` —
не вызываются нигде в `api.py`/`main.py`. Оставлены нетронутыми ради истории
заказов; когда v2 будет полностью проверена в проде, эти функции и таблицы
можно удалить (см. TODO в памяти сессии).
- **Мок-каталог старой версии**: `python _seed_rental_catalog.py` (использует
  старые таблицы — актуальность под вопросом после перехода на v2).

## Гейт подписки на канал (РЕАЛИЗОВАНО)

Как у конкурентов (напр. "Rent Mao Bot"/"Zenly Store" — полноэкранный блок
поверх Mini App с «Открыть канал» / «Я подписался»): без подписки на канал
бот не даёт доступ к функционалу.

- Настройка через `settings.required_channel_username` (username канала без
  `@`, через Mini App: Админ → Настройки). **Пусто = гейт выключен** — так
  по умолчанию сразу после деплоя, ничего не ломает, пока админ не заполнит.
- `GET /api/me` (единая точка входа Mini App при загрузке) добавляет
  `required_channel`/`is_subscribed` — если гейт включён и юзер не подписан,
  `App` рендерит `SubscriptionGate` вместо обычных вкладок.
- Проверка — `api._check_subscription()`: зовёт `bot_sender.get_chat_member()`
  (обёртка над Bot API `getChatMember`, бот должен быть админом канала —
  добавлен вручную через Telegram, не через код). Админы (`ADMIN_IDS`/
  `SUPERADMIN_ID`) проходят без проверки — тот же паттерн бесплатного доступа,
  что и у whitelist везде в проекте. При сбое запроса к Telegram (бот не
  админ, сеть и т.п.) — **fail-open**, чтобы не заблокировать всех из-за
  нашей же ошибки конфигурации.
- Проверка идёт **один раз при открытии Mini App** (плюс по кнопке «Я
  подписался»), не на каждый запрос. **Известный и осознанно отложенный
  пробел**: юзер может подписаться → получить доступ → отписаться — доступ
  при этом не отзовётся сам, нужна периодическая перепроверка (сделаем
  отдельно).
- Гейтится только Mini App — бот-чат сам по себе почти не несёт
  функционала (`/start` прямо говорит, что нужен только для загрузки файла
  и получения PDF, вся реальная логика — внутри Mini App), отдельно не
  трогали.

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

## Промокоды (РЕАЛИЗОВАНО)

- **Таблицы**: `promo_codes` (`code` UNIQUE uppercase, `type` fixed|percent, `value`,
  `per_user_limit`, `total_limit` NULL=безлимит, `activations_count`, `starts_at/ends_at`
  naive-UTC, `is_deleted` soft-delete), `promo_redemptions` (история активаций, по ней же
  считается лимит на пользователя).
- Бонус всегда идёт в `tenge_balance` через ledger с отдельным `reason`
  (`promo_fixed`/`promo_percent`) — отдельной колонки под «бонусный баланс» нет,
  трассируемость — через `transactions.reason`.
- **fixed** — редимится мгновенно кнопкой в Mini App (`POST /api/promo/apply`) →
  `db.apply_promo_fixed`. **percent** — привязывается к пополнению баланса: превью через
  тот же `/api/promo/apply` (`db.validate_promo_for_topup`, без консьюминга), код едет в
  `pending_action`/FSM вместе с суммой топ-апа (`api.py::init_topup` →
  `payment.py::receive_topup_kaspi_pdf`) и реально консьюмится там же —
  `db.redeem_promo_percent(user_id, code, base_amount)` — сразу после зачисления базовой
  суммы. Percent — best-effort: любая невалидность на этом шаге НЕ откатывает уже
  прошедшее пополнение, просто бонус не начисляется.
- Лимиты — тем же guard-UPDATE паттерном, что и списание баланса/захват аккаунта аренды
  (`UPDATE ... WHERE activations_count<total_limit`, `rowcount==0` → `PromoExhausted`).
- 4 текста ошибок (`db.PROMO_ERROR_MESSAGES`) 1:1 на исключения `PromoNotFound/
  PromoNotActive/PromoExhausted/PromoAlreadyUsed`.
- Редимится **только в Mini App** (вкладка «Кошелёк»), без bot-команды.
  Админка промокодов (создание/список со статусом/удаление) — тоже только Mini App
  (`GET|POST /api/admin/promo`, `POST /api/admin/promo/delete`), без bot-команд —
  по прецеденту раздела «Аренда».
- Smoke-тест: `python _test_promo_db.py`.

## Рассылка (РЕАЛИЗОВАНО)

- `POST /api/admin/broadcast {text}` (Mini App, только админ) — берёт всех
  `database.get_all_user_ids(exclude_banned=True)`, кладёт `_run_broadcast` в фоновый
  `asyncio.create_task` (не блокирует HTTP-запрос) и сразу отвечает `{total}`.
  `_run_broadcast` шлёт через `bot_sender.send_message` с `sleep(0.05)` между сообщениями
  (~20 msg/s), по завершении шлёт админу личным сообщением сводку sent/failed.

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
