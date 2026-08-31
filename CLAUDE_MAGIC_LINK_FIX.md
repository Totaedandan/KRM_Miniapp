# Claude присылает magic-link, а не код — фикс для второго бота

`email-worker.js` вы уже перенесли сами. Тут — что нужно поменять на
Python-стороне (бэкенд бота), чтобы связка реально заработала. Всё в одном
коммите нашего репозитория: **`64e3727` — "Resolve Claude magic-link OTP via
real Playwright browser"** (3 файла: `bot/api.py`, `bot/database/db.py`,
`bot/services/ai_rental_service.py`).

## В чём была проблема

ChatGPT шлёт код прямо в письме — просто регэксп по тексту. **Claude — нет.**
Письмо от Claude содержит только кнопку "Sign in" (magic-link вида
`https://claude.ai/magic-link#токен:base64(email)`). Код появляется только
на странице, куда ведёт ссылка, и то не всегда — по тексту самой формы
логина Claude: *"If the link shows a verification code instead of signing
you in, enter it here"* — то есть Claude показывает код ВМЕСТО автовхода,
только если ссылку открывают не из той сессии/браузера, что запрашивала
вход. У нас именно так — письмо читает Cloudflare Worker, а не браузер
арендатора.

Первая попытка — заставить сам Worker сходить по ссылке через `fetch()` —
не сработала в принципе, по двум причинам:
1. **`#токен` — это URL fragment.** Он физически не отправляется на сервер
   ни в одном HTTP-запросе (ни fetch, ни в настоящем браузере) — это чисто
   клиентский механизм.
2. Даже если бы токен как-то доехал — сам код **не в статичном HTML**, его
   дорисовывает JS самой страницы Claude уже после загрузки. Простой
   `fetch()` получает только пустой каркас страницы.

(На реальном тесте `fetch()` из Worker'а вдобавок словил 403 — Cloudflare
на стороне Anthropic блокирует такие запросы отдельно, но даже без этого
подход был бы бесполезен по причинам выше.)

## Решение

Worker больше не пытается сам добывать код по ссылке — просто пересылает
её как есть в `/api/email-hook`, а бэкенд открывает её в **настоящем
Playwright-браузере** (у нас уже есть вся инфраструктура для этого —
`ai_rental_service.py`).

### 1. `bot/api.py` — `/api/email-hook` принимает `magic_link`

```python
class EmailHookBody(BaseModel):
    recipient_email: str
    otp_code:        Optional[str] = None
    magic_link:      Optional[str] = None


@app.post("/api/email-hook")
async def email_hook(body: EmailHookBody, x_webhook_secret: str = Header(None)):
    if not settings.EMAIL_WEBHOOK_SECRET or not x_webhook_secret or \
       not hmac.compare_digest(x_webhook_secret, settings.EMAIL_WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid webhook secret")
    email = body.recipient_email.strip().lower()

    if body.otp_code:
        code = re.sub(r"\D", "", body.otp_code)[:8]
        if not code:
            raise HTTPException(400, "otp_code пустой")
        await database.insert_otp_code(email, code)
        return {"ok": True}

    if body.magic_link:
        from services.ai_rental_service import resolve_magic_link_otp
        asyncio.create_task(resolve_magic_link_otp(email, body.magic_link))
        return {"ok": True, "queued": True}  # не блокируем ответ вебхуку

    raise HTTPException(400, "otp_code или magic_link обязателен")
```

Важно: задача запускается через `asyncio.create_task` и НЕ блокирует ответ
вебхуку — сам Playwright-браузер открывается в фоне, результат появляется в
`otp_incoming_codes` через 5-20+ секунд.

### 2. `bot/database/db.py` — новая функция поиска аккаунта по email

```python
async def get_ai_account_by_email(email: str) -> Optional[dict]:
    """Для резолва magic-link писем — там письмо приходит на email аккаунта,
    но нет order_id/account_id, только сам адрес."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ai_accounts WHERE email=?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None
```

### 3. `bot/services/ai_rental_service.py` — сама функция резолва

Новая публичная функция `resolve_magic_link_otp(email, magic_link)`:
- находит аккаунт по email через `get_ai_account_by_email` и смотрит его
  `proxy_id` — **идёт через прокси именно этого аккаунта** (та же логика,
  что у `auto_logout`), чтобы не плодить разные IP на один email;
- открывает `magic_link` в headful-браузере (`_ensure_xvfb()` + те же
  launch-args, что везде в этом файле);
- ждёт до 20 секунд, пока на странице не появится 6-значное число (Claude
  дорисовывает его JS-ом не мгновенно);
- кладёт найденный код в `otp_incoming_codes` через `db.insert_otp_code` —
  дальше всё как с обычным OTP, `/api/rental/otp` и Mini App ничего не
  знают о разнице, просто заберут код тем же способом;
- если код не появился — сохраняет скриншот в `/tmp/debug_rental_claude_magiclink_no_code_*.png`
  для отладки (тот же приём, что и везде в проекте) и просто логирует
  предупреждение, ничего не ломает.

Полный код функции — смотрите диф коммита `64e3727` в репозитории, он
самодостаточный и вставляется как есть (использует уже существующие в этом
файле `_ensure_xvfb`, `_parse_proxy`, `_solve_captcha_if_present`, `_debug_shot`).

## ⚠️ Важно проверить сразу — без прокси не работает

На живом тесте у нас всплыла отдельная проблема: если у аккаунта в
`ai_accounts` **не назначен прокси** (`proxy_id = NULL`), запрос идёт прямо
с IP сервера — Cloudflare-защита на стороне claude.ai моментально показывает
экран "Performing security verification" вместо страницы с кодом, и код
так и не появляется. Никакой автоматической подсказки об этом бэкенд не
даёт (просто уходит в `debug_..._no_code_*.png` с картинкой капчи) — сразу
проверьте, что у тестового аккаунта прокси назначен через админку
(«Аренда: склад»), иначе первый тест гарантированно не пройдёт по этой
причине, а не из-за самого кода.

## Как тестировать

1. Убедитесь, что у тестового Claude-аккаунта назначен прокси.
2. Арендуйте его, начните вход на claude.ai на выданный email.
3. Нажмите «Получить код» в Mini App.
4. Если не пришло — смотрите логи бэкенда (`grep -i magic` в логах контейнера)
   и, если есть, скриншот `/tmp/debug_rental_claude_magiclink_no_code_*.png`
   внутри контейнера — по нему сразу видно, на чём застряло (капча,
   не та страница и т.п.).
