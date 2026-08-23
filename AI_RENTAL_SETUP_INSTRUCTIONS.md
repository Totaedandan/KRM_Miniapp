# Аренда ИИ-аккаунтов v2 (email+OTP, авто-разлогин, прокси-группы) — перенос на второй бот

## Что это за фича

Почасовая аренда премиум-аккаунтов (ChatGPT/Claude): юзеру выдаётся
`login@ВАШ_ДОМЕН`, код входа приходит на email и перехватывается Cloudflare
Email Worker'ом, по истечении аренды бэкенд сам разлогинивает аккаунт через
Playwright. Аккаунты группируются 2-3 на 1 ISP-прокси с очередью разлогина.

Полностью реализовано и протестировано (включая реальный e2e-тест с
ChatGPT) в репозитории **https://github.com/Totaedandan/KRM_Miniapp.git**.
Дай эту ссылку и файл своему второму Claude Code — пусть читает РЕАЛЬНЫЙ
рабочий код как образец, а не пересобирает с нуля по пересказу. Точный
prompt для него — в самом низу файла.

## ⚠️ Главное правило: НОВЫЙ домен, не тот же самый

У каждого бота — свой домен для email-адресов аренды. **Нельзя** второму
боту использовать `academiceducation.org` (или что там у первого бота) —
Cloudflare Worker шлёт коды на ОДИН захардкоженный `WEBHOOK_URL`, и если
домен общий — все коды обоих ботов будут улетать в один и тот же бот, а не
туда, откуда пришёл запрос. Плюс у ботов разные базы `ai_accounts` — без
общего домена не будет коллизий одинаковых `login@`.

Купите/возьмите отдельный домен под второй бот, заведите на Cloudflare
NS — дальше всё как у первого, просто параллельно, не пересекаясь.

## Прокси и внешние сервисы — тоже отдельно

- **Webshare (или другой ISP-прокси-провайдер)**: можно тот же аккаунт
  провайдера, но прокси (IP) — берите новые, отдельные для второго бота.
  Не переиспользуйте те же IP, что у первого — группировка 2-3 аккаунта на
  прокси у каждого бота своя, боты друг про друга не знают.
- **2Captcha** (если используется — `TWOCAPTCHA_API_KEY`): можно тот же
  ключ/аккаунт, это просто API для решения капч, не завязано на домен.

## Что нужно реализовать в коде (файлы-образцы в репозитории)

Дай второму Claude Code вот эту карту — где что искать в рабочем коде:

- **Схема БД** (новые таблицы `ai_proxies`, `ai_accounts`, `ai_rentals`,
  `otp_incoming_codes`) — `bot/database/db.py`, секция в `init_db()` с
  комментарием "Аренда ИИ-аккаунтов v2", плюс весь блок функций в конце
  файла под заголовком "АРЕНДА ИИ-АККАУНТОВ v2".
- **Playwright авто-разлогин** — новый файл `bot/services/ai_rental_service.py`
  (разлогин ChatGPT/Claude через сохранённые cookies, привязка к прокси
  аккаунта, отдельный browser/context на каждую задачу — не общий с Turnitin).
- **Воркер истечения + очередь на прокси** — новый файл
  `bot/services/ai_rental_manager.py` (тик 60с, напоминания, истечение,
  cooldown→available, `dict[proxy_id, asyncio.Lock]` — без Redis/Celery).
- **API-эндпоинты** — секции в `bot/api.py`: `/api/rental/*`,
  `/api/email-hook`, `/api/admin/ai/proxies*`, `/api/admin/ai/accounts*`.
- **Новые настройки** — `bot/config.py`: `EMAIL_DOMAIN`,
  `EMAIL_WEBHOOK_SECRET`, `TWOCAPTCHA_API_KEY`.
- **Mini App UI** — `mini_app/index.html`: `RentalScreen`/`OtpButton`
  (пользовательский экран получения кода) + админ-экраны
  `rental_accounts`/`rental_proxies`/`rental_otp_logs` в `AdminScreen`.
- **Полное техническое описание архитектуры** — раздел «Аренда ИИ-аккаунтов
  v2» в `CLAUDE.md` в корне репозитория — там расписаны все атомарные
  операции, guard-паттерны списания денег, LRU-выбор аккаунта и т.д.
- **Cloudflare Worker** — `cloudflare/email-worker.js` (готовый, много раз
  доработанный под реальные письма OpenAI — разбирает MIME, обходит битые
  заголовки, отличает настоящий код от CSS-цветов/mso-id в письме).

## Настройка Cloudflare (повторить для нового домена)

1. Домен на Cloudflare NS → Email → Email Routing → Enable →
   **Settings → DNS records → Add missing records** (добавит MX+TXT сам).
2. Destination Addresses → добавить и подтвердить свой email (форвард
   security-писем).
3. Workers & Pages → Create Application → Email Worker → вставить код из
   `cloudflare/email-worker.js` → Deploy.
4. В этом Worker'е → Settings → Variables and Secrets (тип Secret):
   - `WEBHOOK_URL` = `https://ДОМЕН_ВТОРОГО_БОТА/api/email-hook`
   - `WEBHOOK_SECRET` = сгенерировать новый случайный (НЕ тот же, что у
     первого бота — это отдельный секрет в `.env` второго бота,
     `EMAIL_WEBHOOK_SECRET`)
   - `ADMIN_EMAIL` = подтверждённый в шаге 2 адрес
5. Email Routing → Routing rules → Catch-all → Send to a Worker → выбрать
   этот Worker.
6. `.env` второго бота: `EMAIL_DOMAIN=домен_второго_бота`,
   `EMAIL_WEBHOOK_SECRET=<тот же секрет, что в шаге 4>`.

## Тест

Через новую админку («Аренда: прокси» → «Аренда: склад») добавить 1 прокси
+ 1 аккаунт с email на новом домене → арендовать самому → зарегистрироваться
на этот email в ChatGPT/Claude → «Получить код» в Mini App.

## Готовый prompt для второго Claude Code

```
Изучи https://github.com/Totaedandan/KRM_Miniapp.git — там реализована
и протестирована (включая реальный e2e-тест с ChatGPT) фича почасовой
аренды ИИ-аккаунтов v2: email+OTP вход, авто-разлогин через Playwright,
группировка 2-3 аккаунта на 1 ISP-прокси. Полное техническое описание —
раздел "Аренда ИИ-аккаунтов v2" в CLAUDE.md этого репозитория.

Перенеси эту фичу в наш проект, адаптировав под нашу структуру кода:
- схема БД (ai_proxies, ai_accounts, ai_rentals, otp_incoming_codes) —
  см. bot/database/db.py в исходном репо
- bot/services/ai_rental_service.py (Playwright авто-разлогин)
- bot/services/ai_rental_manager.py (воркер истечения + очередь на прокси)
- API-эндпоинты /api/rental/*, /api/email-hook, /api/admin/ai/*
- Mini App UI для аренды и админки
- cloudflare/email-worker.js — этот файл можно взять почти как есть,
  он уже отлажен на реальных письмах ChatGPT (не переписывай логику
  парсинга MIME и выбора кода среди нескольких кандидатов — она прошла
  через несколько раундов исправлений на живых данных)

ВАЖНО: у нас будет ДРУГОЙ домен для email-адресов аренды (не тот, что в
исходном репо) — отдельный EMAIL_WEBHOOK_SECRET, отдельный Cloudflare
Worker с собственным WEBHOOK_URL, указывающим на НАШ /api/email-hook.
```
