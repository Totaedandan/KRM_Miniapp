"""
База данных бота (aiosqlite).

Таблицы:
  users             — пользователи, баланс токенов, whitelist
  turnitin_orders   — заказы на проверку Turnitin
  payments          — платёжные транзакции (Stars / Kaspi)
  kaspi_receipts    — дедупликация чеков Kaspi
  settings          — динамические настройки (цены, реквизиты и т.д.)
  rental_services   — каталог сервисов аренды ИИ (ChatGPT Plus, Claude Pro…)
  rental_tariffs    — тарифы сервиса (часы/неделя/месяц/год, цена в тенге)
  rental_accounts   — склад аккаунтов (логин/пароль, free|rented|maintenance|disabled)
  rental_orders     — аренды (active|expired|cancelled, expires_at)
  rental_waitlist   — лист ожидания «уведомить, когда освободится»

Деньги аренды идут через тот же ledger (transactions): reasons rental_charge /
rental_refund, ключи rental:<tg_id>:<request_id> и rental_refund:<order_id>.
transactions.order_id для этих reasons ссылается на rental_orders.id (для
остальных — turnitin_orders.id); различать по reason.
"""

import aiosqlite
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH: str = "data/bot.db"


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path


async def init_db(path: str):
    set_db_path(path)
    import os
    dir_part = os.path.dirname(os.path.abspath(path))
    if dir_part:
        os.makedirs(dir_part, exist_ok=True)

    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id           INTEGER UNIQUE NOT NULL,
                username        TEXT,
                full_name       TEXT,
                tenge_balance   REAL    NOT NULL DEFAULT 0.0,
                token_balance   REAL    NOT NULL DEFAULT 0.0,
                is_whitelisted  INTEGER NOT NULL DEFAULT 0,
                is_banned       INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turnitin_orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                username        TEXT,
                report_type     TEXT    NOT NULL,   -- sim | ai | both
                status          TEXT    NOT NULL DEFAULT 'pending',
                payment_method  TEXT    DEFAULT 'stars',
                amount_tenge    REAL,
                amount_stars    INTEGER,
                receipt_id      TEXT,
                file_name       TEXT,
                file_path       TEXT,
                submission_id   TEXT,
                report_path_sim TEXT,
                report_path_ai  TEXT,
                error_text      TEXT,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                payment_type    TEXT    NOT NULL,   -- stars | kaspi
                purpose         TEXT    NOT NULL,   -- turnitin | tokens
                amount_tenge    REAL,
                amount_stars    INTEGER,
                tokens_amount   REAL,
                order_id        INTEGER,
                receipt_id      TEXT,
                charge_id       TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL,
                confirmed_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS kaspi_receipts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id  TEXT UNIQUE NOT NULL,
                user_id     INTEGER,
                used_at     TEXT NOT NULL
            );

            -- Журнал движений баланса (ledger). Источник правды для аудита/сверки.
            -- Колонки tenge_balance/token_balance в users — кэш, который меняется
            -- ТОЛЬКО вместе с записью сюда (см. _record_movement / create_paid_order).
            CREATE TABLE IF NOT EXISTS transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,            -- tg_id
                order_id        INTEGER,                     -- NULL для пополнений/покупок
                type            TEXT    NOT NULL,            -- debit | credit
                reason          TEXT    NOT NULL,            -- order_charge|order_refund|topup|token_purchase|humanizer|admin_adjust|opening_balance
                currency        TEXT    NOT NULL DEFAULT 'tenge',  -- tenge | token
                amount          REAL    NOT NULL,            -- всегда > 0, знак задаёт type
                balance_after   REAL    NOT NULL,            -- снимок баланса после операции
                status          TEXT    NOT NULL DEFAULT 'completed',  -- pending|completed|failed
                idempotency_key TEXT    UNIQUE,              -- защита от дублей (п.8.1 ТЗ)
                created_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );

            -- ── Аренда ИИ-аккаунтов ──────────────────────────────
            CREATE TABLE IF NOT EXISTS rental_services (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                description TEXT,
                icon        TEXT,                          -- emoji или ключ иконки
                is_active   INTEGER NOT NULL DEFAULT 1,
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rental_tariffs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id     INTEGER NOT NULL,           -- → rental_services.id
                name           TEXT    NOT NULL,           -- «6 часов» / «1 месяц»
                duration_hours INTEGER NOT NULL,           -- 6 / 168 / 720 / 8760
                price          REAL    NOT NULL,           -- тенге
                is_active      INTEGER NOT NULL DEFAULT 1,
                sort_order     INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL
            );

            -- Пароли хранятся плейнтекстом: не логировать, не отдавать из
            -- каталожных эндпоинтов, в админке маскировать.
            CREATE TABLE IF NOT EXISTS rental_accounts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id       INTEGER NOT NULL,
                login            TEXT    NOT NULL,
                password         TEXT    NOT NULL,
                note             TEXT,
                status           TEXT    NOT NULL DEFAULT 'free',  -- free|rented|maintenance|disabled
                current_order_id INTEGER,                  -- → rental_orders.id (когда rented)
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rental_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,            -- tg_id
                username      TEXT,
                service_id    INTEGER NOT NULL,
                tariff_id     INTEGER NOT NULL,
                account_id    INTEGER NOT NULL,
                amount_tenge  REAL    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'active',  -- active|expired|cancelled
                starts_at     TEXT    NOT NULL,            -- naive-UTC isoformat (_now())
                expires_at    TEXT    NOT NULL,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rental_waitlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id  INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,              -- tg_id
                created_at  TEXT    NOT NULL,
                UNIQUE(service_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_rental_accounts_svc ON rental_accounts(service_id, status);
            CREATE INDEX IF NOT EXISTS idx_rental_orders_user  ON rental_orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_rental_orders_exp   ON rental_orders(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_rental_wait_svc     ON rental_waitlist(service_id);

            -- ── Аренда ИИ-аккаунтов v2: email+OTP, авто-разлогин, прокси-группы ──
            -- Заменяет rental_accounts/rental_orders (те таблицы не удаляются —
            -- просто больше не используются, без риска потери исторических данных).
            -- Каталог услуг/тарифов (rental_services/rental_tariffs) переиспользуется как есть.
            CREATE TABLE IF NOT EXISTS ai_proxies (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_url     TEXT    NOT NULL,             -- http://user:pass@ip:port
                max_accounts  INTEGER NOT NULL DEFAULT 3,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_accounts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id        INTEGER NOT NULL,          -- → rental_services.id
                email             TEXT    UNIQUE NOT NULL,   -- login@домен
                proxy_id          INTEGER,                   -- → ai_proxies.id
                cookies_data      TEXT,                      -- JSON сессии после логина
                status            TEXT    NOT NULL DEFAULT 'available',
                                  -- available|rented|cooldown|maintenance|banned
                current_order_id  INTEGER,                   -- → ai_rentals.id (когда rented)
                last_used_at      TEXT,
                created_at        TEXT    NOT NULL,
                updated_at        TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_rentals (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,             -- tg_id
                username       TEXT,
                service_id     INTEGER NOT NULL,
                tariff_id      INTEGER NOT NULL,
                account_id     INTEGER NOT NULL,
                amount_tenge   REAL    NOT NULL,
                paid_bonus     REAL    NOT NULL DEFAULT 0,   -- для отображения в админке
                paid_main      REAL    NOT NULL DEFAULT 0,   -- источник правды — ledger
                status         TEXT    NOT NULL DEFAULT 'active',  -- active|expired|cancelled
                starts_at      TEXT    NOT NULL,
                expires_at     TEXT    NOT NULL,
                reminder_sent  INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS otp_incoming_codes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email  TEXT    NOT NULL,
                otp_code         TEXT    NOT NULL,
                created_at       TEXT    NOT NULL
            );

            -- Magic-link сервисы (Claude) шлют не код, а ссылку — код рисуется их
            -- собственным JS на странице, куда она ведёт, и наш серверный браузер
            -- регулярно словит на этой странице Cloudflare Turnstile (см. историю
            -- resolve_magic_link_otp в git). Вместо попытки решить капчу за юзера —
            -- отдаём ему саму ссылку, он открывает её В СВОЁМ браузере (реальный IP,
            -- никакого риск-скоринга) и либо сразу логинится, либо видит код сам.
            CREATE TABLE IF NOT EXISTS otp_incoming_links (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email  TEXT    NOT NULL,
                magic_link       TEXT    NOT NULL,
                created_at       TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_accounts_svc   ON ai_accounts(service_id, status);
            CREATE INDEX IF NOT EXISTS idx_ai_accounts_proxy ON ai_accounts(proxy_id);
            CREATE INDEX IF NOT EXISTS idx_ai_rentals_user   ON ai_rentals(user_id);
            CREATE INDEX IF NOT EXISTS idx_ai_rentals_exp    ON ai_rentals(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_otp_email         ON otp_incoming_codes(recipient_email, created_at);
            CREATE INDEX IF NOT EXISTS idx_otp_links_email   ON otp_incoming_links(recipient_email, created_at);

            CREATE TABLE IF NOT EXISTS banned_usernames (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
                reason      TEXT,
                added_at    TEXT NOT NULL
            );

            -- ── Промокоды ──────────────────────────────────────
            -- type: fixed (фикс. ₸) | percent (% от суммы пополнения, консьюмится
            -- в момент подтверждения Kaspi-чека — см. redeem_promo_percent).
            CREATE TABLE IF NOT EXISTS promo_codes (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                code               TEXT    UNIQUE NOT NULL,
                type               TEXT    NOT NULL,
                value              REAL    NOT NULL,
                per_user_limit     INTEGER NOT NULL DEFAULT 1,
                total_limit        INTEGER,
                activations_count  INTEGER NOT NULL DEFAULT 0,
                starts_at          TEXT,
                ends_at            TEXT,
                is_deleted         INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id         INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                amount_credited  REAL    NOT NULL,
                created_at       TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_promo_redemptions_promo ON promo_redemptions(promo_id, user_id);

            CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON turnitin_orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_tx_order ON transactions(order_id);
        """)

        # Миграции для существующих БД
        for col_sql in [
            "ALTER TABLE users ADD COLUMN tenge_balance REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN pending_action TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN pending_data TEXT DEFAULT NULL",
            "ALTER TABLE turnitin_orders ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE turnitin_orders ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE payments ADD COLUMN promo_code TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN bonus_balance REAL NOT NULL DEFAULT 0.0",
        ]:
            try:
                await db.execute(col_sql)
                await db.commit()
            except Exception:
                pass  # Колонка уже существует

        defaults = {
            "price_sim":             "700",
            "price_ai":              "700",
            "price_both":            "1200",
            "turnitin_email":        "",
            "turnitin_password":     "",
            "turnitin_class_id":     "",
            "turnitin_assign_id":    "",
            "turnitin_class_id_premium":  "",   # премиум-класс Turnitin (тот же аккаунт)
            "turnitin_assign_id_premium": "",
            "premium_multiplier":    "1.5",     # множитель цены премиум-очереди
            "help_username":         "@support",
            "help_phone":            "",
            "free_users":            "",   # comma-separated tg_ids (whitelist)
            # Пакеты токенов Хуманайзера (редактируются в админке)
            "pkg1_tokens": "100",  "pkg1_tenge": "1000",
            "pkg2_tokens": "250",  "pkg2_tenge": "2300",
            "pkg3_tokens": "600",  "pkg3_tenge": "5200",
            "pkg4_tokens": "1200", "pkg4_tenge": "9500",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        await db.commit()

        # Бэкфилл: занести текущие балансы существующих юзеров в ledger одной
        # стартовой записью, чтобы SUM(transactions) сходился с колонкой баланса.
        # Идемпотентный ключ opening:<currency>:<tg_id> защищает от повторного
        # внесения при следующих запусках.
        cur = await db.execute(
            "SELECT tg_id, tenge_balance, token_balance FROM users "
            "WHERE tenge_balance > 0 OR token_balance > 0"
        )
        for tg_id, tenge_bal, token_bal in await cur.fetchall():
            for currency, amount in (("tenge", tenge_bal), ("token", token_bal)):
                if not amount or amount <= 0:
                    continue
                key = f"opening:{currency}:{tg_id}"
                exists = await db.execute(
                    "SELECT 1 FROM transactions WHERE idempotency_key=?", (key,)
                )
                if await exists.fetchone():
                    continue
                await db.execute(
                    "INSERT INTO transactions(user_id,order_id,type,reason,currency,"
                    "amount,balance_after,status,idempotency_key,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'completed',?,?)",
                    (tg_id, None, "credit", "opening_balance", currency,
                     amount, amount, key, _now()),
                )
        await db.commit()

    logger.info("DB ready: %s", path)


def _now() -> str:
    return datetime.utcnow().isoformat()


# ═══════════════════════════════════════════════════════════════
#  LEDGER (журнал транзакций — источник правды для денег)
# ═══════════════════════════════════════════════════════════════

class InsufficientFunds(Exception):
    """Недостаточно средств для списания (или пользователь не найден)."""


def _tx_insert_sql() -> str:
    return (
        "INSERT INTO transactions(user_id,order_id,type,reason,currency,"
        "amount,balance_after,status,idempotency_key,created_at) "
        "VALUES(?,?,?,?,?,?,?,'completed',?,?)"
    )


async def _record_movement(
    user_id: int,
    type_: str,                 # 'debit' | 'credit'
    reason: str,
    amount: float,
    currency: str = "tenge",    # 'tenge' | 'token' | 'bonus'
    order_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    allow_overdraft: bool = True,
) -> float:
    """Изменить баланс И записать строку в ledger в одной БД-транзакции.

    Возвращает новый баланс. При idempotency_key, который уже встречался,
    операция НЕ повторяется — возвращается ранее сохранённый balance_after.
    При allow_overdraft=False и нехватке средств бросает InsufficientFunds.
    """
    col = {"tenge": "tenge_balance", "token": "token_balance", "bonus": "bonus_balance"}[currency]
    async with aiosqlite.connect(_DB_PATH) as db:
        # Идемпотентность: уже проводили — ничего не делаем
        if idempotency_key:
            cur = await db.execute(
                "SELECT balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                return float(row[0])

        if type_ == "debit":
            if allow_overdraft:
                await db.execute(
                    f"UPDATE users SET {col}=MAX(0, {col}-?) WHERE tg_id=?",
                    (amount, user_id),
                )
            else:
                cur = await db.execute(
                    f"UPDATE users SET {col}={col}-? WHERE tg_id=? AND {col}>=?",
                    (amount, user_id, amount),
                )
                if cur.rowcount == 0:
                    await db.rollback()
                    raise InsufficientFunds()
        else:
            await db.execute(
                f"UPDATE users SET {col}={col}+? WHERE tg_id=?", (amount, user_id)
            )

        cur = await db.execute(f"SELECT {col} FROM users WHERE tg_id=?", (user_id,))
        r = await cur.fetchone()
        bal = float(r[0]) if r else 0.0

        try:
            await db.execute(
                _tx_insert_sql(),
                (user_id, order_id, type_, reason, currency, amount, bal,
                 idempotency_key, _now()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            # Гонка: параллельный запрос уже записал ту же idempotency_key.
            # Откатываем своё изменение баланса и отдаём ранее сохранённый итог.
            await db.rollback()
            cur = await db.execute(
                "SELECT balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            return float(row[0]) if row else bal
        return bal


# ═══════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_prices() -> dict:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT key,value FROM settings WHERE key IN ('price_sim','price_ai','price_both')"
        )
        return {r[0]: int(r[1]) for r in await cur.fetchall()}


async def get_premium_multiplier() -> float:
    """Множитель цены для премиум-очереди (по умолчанию 1.5)."""
    val = await get_setting("premium_multiplier")
    try:
        return float(val) if val else 1.5
    except (TypeError, ValueError):
        return 1.5


_PKG_DEFAULTS = [
    (100, 1000), (250, 2300), (600, 5200), (1200, 9500),
]


async def get_token_packages() -> list[dict]:
    """4 пакета токенов Хуманайзера — цены редактируются в админке (settings)."""
    keys = [f"pkg{n}_{f}" for n in range(1, 5) for f in ("tokens", "tenge")]
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            f"SELECT key,value FROM settings WHERE key IN ({','.join('?'*len(keys))})", keys
        )
        rows = {r[0]: r[1] for r in await cur.fetchall()}
    packages = []
    for i, (dtok, dtenge) in enumerate(_PKG_DEFAULTS, start=1):
        packages.append({
            "tokens": int(rows.get(f"pkg{i}_tokens", dtok)),
            "tenge":  int(rows.get(f"pkg{i}_tenge", dtenge)),
        })
    return packages


async def set_token_package(index: int, tokens: int, tenge: int):
    if index not in (1, 2, 3, 4):
        raise ValueError("index должен быть 1-4")
    await set_setting(f"pkg{index}_tokens", str(int(tokens)))
    await set_setting(f"pkg{index}_tenge", str(int(tenge)))


async def get_active_file_paths() -> set[str]:
    """Пути файлов заказов, которые уже загружены и ждут/идут в обработке.
    Используется при очистке кэша, чтобы их не удалить."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT file_path FROM turnitin_orders "
            "WHERE status IN ('ready','processing') AND file_path IS NOT NULL"
        )
        return {r[0] for r in await cur.fetchall() if r[0]}


# ═══════════════════════════════════════════════════════════════
#  USERS
# ═══════════════════════════════════════════════════════════════

async def get_or_create_user(tg_id: int, username: Optional[str], full_name: Optional[str]) -> dict:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        await db.execute(
            "INSERT INTO users(tg_id,username,full_name,created_at) VALUES(?,?,?,?)",
            (tg_id, username, full_name, _now()),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return dict(await cur.fetchone())


async def get_user(tg_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_id(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_token_balance(tg_id: int) -> float:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT token_balance FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return row[0] if row else 0.0


async def add_tokens(tg_id: int, amount: float, reason: str = "token_topup",
                     order_id: Optional[int] = None,
                     idempotency_key: Optional[str] = None) -> float:
    return await _record_movement(
        tg_id, "credit", reason, amount, currency="token",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def deduct_tokens(tg_id: int, amount: float, reason: str = "humanizer",
                        order_id: Optional[int] = None,
                        idempotency_key: Optional[str] = None) -> float:
    return await _record_movement(
        tg_id, "debit", reason, amount, currency="token",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def get_tenge_balance(tg_id: int) -> float:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT tenge_balance FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0


async def get_bonus_balance(tg_id: int) -> float:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0


async def add_tenge(tg_id: int, amount: float, reason: str = "topup",
                    order_id: Optional[int] = None,
                    idempotency_key: Optional[str] = None) -> float:
    return await _record_movement(
        tg_id, "credit", reason, amount, currency="tenge",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def deduct_tenge(tg_id: int, amount: float, reason: str = "debit",
                       order_id: Optional[int] = None,
                       idempotency_key: Optional[str] = None) -> float:
    """Списывает тенге с баланса (с записью в ledger). НЕ уходит ниже 0."""
    return await _record_movement(
        tg_id, "debit", reason, amount, currency="tenge",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def add_bonus(tg_id: int, amount: float, reason: str = "promo_fixed",
                    order_id: Optional[int] = None,
                    idempotency_key: Optional[str] = None) -> float:
    return await _record_movement(
        tg_id, "credit", reason, amount, currency="bonus",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def deduct_bonus(tg_id: int, amount: float, reason: str = "debit",
                       order_id: Optional[int] = None,
                       idempotency_key: Optional[str] = None) -> float:
    """Списывает бонусный баланс (с записью в ledger). НЕ уходит ниже 0."""
    return await _record_movement(
        tg_id, "debit", reason, amount, currency="bonus",
        order_id=order_id, idempotency_key=idempotency_key,
    )


async def _apply_bonus_debit(db, user_id: int, price: float, use_bonus: bool) -> tuple[float, float]:
    """Списать `price` с уже открытой транзакции: сначала bonus_balance (если
    use_bonus), остаток — с tenge_balance. Возвращает (bonus_used, tenge_used).

    Guarded UPDATE считает сумму списания по ТЕКУЩЕМУ значению bonus_balance —
    предварительный SELECT нужен только чтобы посчитать bonus_used/tenge_used для
    ledger, а не для проверки достаточности (её делает сам guard, атомарно).
    """
    cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
    row = await cur.fetchone()
    bonus_bal = float(row[0]) if row else 0.0

    bonus_used = round(min(bonus_bal, price), 2) if use_bonus else 0.0
    tenge_used = round(price - bonus_used, 2)

    cur = await db.execute(
        "UPDATE users SET bonus_balance=bonus_balance-?, tenge_balance=tenge_balance-? "
        "WHERE tg_id=? AND bonus_balance>=? AND tenge_balance>=?",
        (bonus_used, tenge_used, user_id, bonus_used, tenge_used),
    )
    if cur.rowcount == 0:
        await db.rollback()
        raise InsufficientFunds()

    return bonus_used, tenge_used


async def debit_with_bonus(
    user_id: int, price: float, use_bonus: bool, reason: str,
    order_id: Optional[int] = None, idempotency_key: Optional[str] = None,
) -> dict:
    """Самостоятельное атомарное списание price (bonus сначала, потом tenge) вне
    контекста создания заказа/аренды — используется покупкой токенов.

    Возвращает {bonus_used, tenge_used, bonus_balance, tenge_balance}.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        if idempotency_key:
            cur = await db.execute(
                "SELECT balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            if await cur.fetchone():
                cur = await db.execute(
                    "SELECT bonus_balance, tenge_balance FROM users WHERE tg_id=?", (user_id,)
                )
                row = await cur.fetchone()
                return {"bonus_used": 0.0, "tenge_used": 0.0,
                        "bonus_balance": float(row[0]) if row else 0.0,
                        "tenge_balance": float(row[1]) if row else 0.0,
                        "duplicate": True}

        bonus_used, tenge_used = await _apply_bonus_debit(db, user_id, price, use_bonus)

        now = _now()
        if bonus_used > 0:
            cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
            bonus_after = float((await cur.fetchone())[0])
            await db.execute(
                _tx_insert_sql(),
                (user_id, order_id, "debit", reason, "bonus", bonus_used, bonus_after,
                 f"{idempotency_key}:bonus" if idempotency_key else None, now),
            )
        if tenge_used > 0:
            cur = await db.execute("SELECT tenge_balance FROM users WHERE tg_id=?", (user_id,))
            tenge_after = float((await cur.fetchone())[0])
            await db.execute(
                _tx_insert_sql(),
                (user_id, order_id, "debit", reason, "tenge", tenge_used, tenge_after,
                 idempotency_key, now),
            )
        await db.commit()

        cur = await db.execute("SELECT bonus_balance, tenge_balance FROM users WHERE tg_id=?", (user_id,))
        row = await cur.fetchone()
        return {"bonus_used": bonus_used, "tenge_used": tenge_used,
                "bonus_balance": float(row[0]), "tenge_balance": float(row[1])}


async def set_whitelist(tg_id: int, value: bool) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET is_whitelisted=? WHERE tg_id=?", (int(value), tg_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def set_banned(tg_id: int, value: bool):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned=? WHERE tg_id=?", (int(value), tg_id)
        )
        await db.commit()


async def is_whitelisted(tg_id: int) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT is_whitelisted FROM users WHERE tg_id=?", (tg_id,)
        )
        row = await cur.fetchone()
        return bool(row[0]) if row else False


async def get_whitelist_users() -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE is_whitelisted=1 ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_whitelist_orders(limit: int = 100) -> list[dict]:
    """История заказов Turnitin, прошедших бесплатно через whitelist
    (payment_method='whitelist' — ставится и для юзеров из БД-вайтлиста, и для
    админов из ADMIN_IDS/SUPERADMIN_ID, см. settings.is_admin). Для админки —
    видно, кто и сколько бесплатных проверок сделал."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM turnitin_orders WHERE payment_method='whitelist' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def find_user(query: str) -> Optional[dict]:
    """Найти пользователя по tg_id или username."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            tg_id = int(query.lstrip("@"))
            cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        except ValueError:
            uname = query.lstrip("@")
            cur = await db.execute("SELECT * FROM users WHERE username=?", (uname,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_payment(payment_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_users_count() -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]


# ═══════════════════════════════════════════════════════════════
#  TURNITIN ORDERS
# ═══════════════════════════════════════════════════════════════

async def create_order(
    user_id: int,
    username: Optional[str],
    report_type: str,
    payment_method: str,
    amount_tenge: Optional[float] = None,
    amount_stars: Optional[int] = None,
    is_premium: bool = False,
    status: str = "pending",
) -> int:
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO turnitin_orders
               (user_id, username, report_type, status, payment_method,
                amount_tenge, amount_stars, is_premium, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, username, report_type, status, payment_method,
             amount_tenge, amount_stars, int(is_premium), now, now),
        )
        await db.commit()
        return cur.lastrowid


async def create_paid_order(
    user_id: int,
    username: Optional[str],
    report_type: str,
    price: float,
    is_premium: bool = False,
    idempotency_key: Optional[str] = None,
    use_bonus: bool = False,
) -> dict:
    """Атомарно: списать (бонус сначала, если use_bonus, потом тенге) + создать
    заказ (queued) + записать в ledger.

    Всё в одной БД-транзакции — деньги не могут списаться без заказа и наоборот.
    Возвращает {ok, duplicate, order_id, balance, bonus_balance}.
      • duplicate=True  — этот idempotency_key уже проводился, повторно НЕ списываем,
        возвращаем ранее созданный order_id (защита от двойного тапа, п.8.1 ТЗ).
      • InsufficientFunds — если баланса (тенге+бонус) не хватило.
    """
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        # 1. Идемпотентность — заказ по этому ключу уже создан
        if idempotency_key:
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "order_id": row[0],
                        "balance": float(row[1]), "bonus_balance": float(brow[0]) if brow else 0.0}

        # 2. Атомарное списание (бонус сначала, потом тенге; защита от гонок и минуса)
        bonus_used, tenge_used = await _apply_bonus_debit(db, user_id, price, use_bonus)

        cur = await db.execute(
            "SELECT tenge_balance, bonus_balance FROM users WHERE tg_id=?", (user_id,)
        )
        balance, bonus_balance = (float(x) for x in await cur.fetchone())

        # 3. Создаём заказ
        cur = await db.execute(
            """INSERT INTO turnitin_orders
               (user_id, username, report_type, status, payment_method,
                amount_tenge, is_premium, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, username, report_type, "queued", "balance",
             price, int(is_premium), now, now),
        )
        order_id = cur.lastrowid

        # 4. Журналируем списание (привязка к заказу) — 1-2 строки в зависимости
        #    от того, участвовал ли бонус
        try:
            if bonus_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "order_charge", "bonus",
                     bonus_used, bonus_balance,
                     f"{idempotency_key}:bonus" if idempotency_key else None, now),
                )
            if tenge_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "order_charge", "tenge",
                     tenge_used, balance, idempotency_key, now),
                )
            await db.commit()
        except aiosqlite.IntegrityError:
            # Параллельный дубль успел записать ту же idempotency_key — откат,
            # возвращаем уже созданный заказ.
            await db.rollback()
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "order_id": row[0],
                        "balance": float(row[1]), "bonus_balance": float(brow[0]) if brow else 0.0}
            raise

        return {"ok": True, "duplicate": False, "order_id": order_id,
                "balance": balance, "bonus_balance": bonus_balance}


async def get_order(order_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM turnitin_orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_order(order_id: int, **kwargs):
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [order_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(f"UPDATE turnitin_orders SET {cols} WHERE id=?", vals)
        await db.commit()


async def get_user_orders(tg_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM turnitin_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (tg_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


# Статусы заказа, который занимает место в очереди (деньги уже списаны)
ACTIVE_QUEUE_STATUSES = ("queued", "awaiting_file", "ready", "processing")


async def get_pending_orders(limit: int = 50) -> list[dict]:
    """Активные заказы в очереди (для админки), премиум — выше, затем по времени."""
    placeholders = ",".join("?" * len(ACTIVE_QUEUE_STATUSES))
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM turnitin_orders WHERE status IN ({placeholders}) "
            f"ORDER BY is_premium DESC, created_at ASC LIMIT ?",
            (*ACTIVE_QUEUE_STATUSES, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_active_orders() -> list[dict]:
    """Все заказы, занимающие место в очереди, в порядке обработки
    (премиум раньше обычных, внутри — по времени создания)."""
    placeholders = ",".join("?" * len(ACTIVE_QUEUE_STATUSES))
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM turnitin_orders WHERE status IN ({placeholders}) "
            f"ORDER BY is_premium DESC, created_at ASC",
            ACTIVE_QUEUE_STATUSES,
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_orders_by_status(statuses: tuple[str, ...]) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT * FROM turnitin_orders WHERE status IN ({placeholders}) "
            f"ORDER BY is_premium DESC, created_at ASC",
            statuses,
        )
        return [dict(r) for r in await cur.fetchall()]


async def _get_charge_split(order_id: int, reason: str) -> dict:
    """{'bonus': X, 'tenge': Y} — сколько реально списано с каждой валюты на этот
    заказ (по ledger). Для заказов до появления bonus_balance там будет только
    'tenge' — обратная совместимость сохраняется сама собой."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT currency, SUM(amount) FROM transactions "
            "WHERE order_id=? AND reason=? AND type='debit' GROUP BY currency",
            (order_id, reason),
        )
        return {r[0]: float(r[1]) for r in await cur.fetchall()}


async def cancel_order_with_refund(order_id: int, reason: str = "") -> dict:
    """Отменить заказ и вернуть деньги пользователю — каждой валюте туда, откуда
    списывалась (бонус → bonus_balance, тенге → tenge_balance), а не всё в тенге.

    Возвращает dict: {ok, refunded, user_id, report_type, is_premium, status_before, error?}.
    Возврат только если деньги реально списывались (amount_tenge>0 и не whitelist).
    """
    order = await get_order(order_id)
    if not order:
        return {"ok": False, "error": "not_found"}
    status_before = order["status"]
    if status_before in ("done", "cancelled"):
        return {"ok": False, "error": "already_final", "status_before": status_before}

    amount = float(order.get("amount_tenge") or 0)
    refunded = 0.0
    if order.get("payment_method") != "whitelist" and amount > 0:
        # idempotency_key привязан к заказу → деньги не вернутся дважды,
        # даже если cancel вызван и по таймауту, и админом одновременно.
        split = await _get_charge_split(order_id, "order_charge")
        bonus_part = split.get("bonus", 0.0)
        tenge_part = split.get("tenge", amount - bonus_part)
        if bonus_part > 0:
            await add_bonus(
                order["user_id"], bonus_part,
                reason="order_refund", order_id=order_id,
                idempotency_key=f"refund:{order_id}:bonus",
            )
        if tenge_part > 0:
            await add_tenge(
                order["user_id"], tenge_part,
                reason="order_refund", order_id=order_id,
                idempotency_key=f"refund:{order_id}",
            )
        refunded = bonus_part + tenge_part

    await update_order(
        order_id,
        status="cancelled",
        error_text=(reason or "cancelled")[:500],
    )
    return {
        "ok": True,
        "refunded": refunded,
        "user_id": order["user_id"],
        "report_type": order["report_type"],
        "is_premium": bool(order.get("is_premium")),
        "status_before": status_before,
    }


# ═══════════════════════════════════════════════════════════════
#  PAYMENTS
# ═══════════════════════════════════════════════════════════════

async def create_payment(
    user_id: int,
    payment_type: str,
    purpose: str,
    amount_tenge: Optional[float] = None,
    amount_stars: Optional[int] = None,
    tokens_amount: Optional[float] = None,
    order_id: Optional[int] = None,
    promo_code: Optional[str] = None,
) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO payments
               (user_id, payment_type, purpose, amount_tenge, amount_stars,
                tokens_amount, order_id, promo_code, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
            (user_id, payment_type, purpose, amount_tenge, amount_stars,
             tokens_amount, order_id, promo_code, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def confirm_payment(payment_id: int, receipt_id: Optional[str] = None, charge_id: Optional[str] = None):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status='confirmed', confirmed_at=?, receipt_id=?, charge_id=? WHERE id=?",
            (_now(), receipt_id, charge_id, payment_id),
        )
        await db.commit()


async def fail_payment(payment_id: int, status: str) -> bool:
    """Пометить платёж как неуспешный (cancelled/expired/error от ApiPay). Не трогает уже
    подтверждённый платёж — защита от гонки с опоздавшим/дублирующимся вебхуком."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "UPDATE payments SET status=? WHERE id=? AND status!='confirmed'",
            (status, payment_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════
#  KASPI RECEIPTS (дедупликация)
# ═══════════════════════════════════════════════════════════════

async def receipt_exists(receipt_id: str) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM kaspi_receipts WHERE receipt_id=?", (receipt_id,)
        )
        return await cur.fetchone() is not None


async def add_receipt(receipt_id: str, user_id: int):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO kaspi_receipts(receipt_id,user_id,used_at) VALUES(?,?,?)",
            (receipt_id, user_id, _now()),
        )
        await db.commit()


async def find_user_by_receipt(receipt_id: str) -> Optional[dict]:
    """Найти пользователя и связанный платёж по номеру чека Kaspi."""
    clean = receipt_id.strip()
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Ищем в kaspi_receipts
        cur = await db.execute(
            "SELECT kr.receipt_id, kr.user_id, kr.used_at, "
            "       u.username, u.full_name, u.tg_id, u.tenge_balance, u.token_balance "
            "FROM kaspi_receipts kr "
            "LEFT JOIN users u ON u.tg_id = kr.user_id "
            "WHERE kr.receipt_id = ? COLLATE NOCASE",
            (clean,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        result = dict(row)
        # Найдём связанный платёж
        cur2 = await db.execute(
            "SELECT p.id, p.purpose, p.amount_tenge, p.tokens_amount, p.status, p.created_at "
            "FROM payments p "
            "WHERE p.receipt_id = ? COLLATE NOCASE OR p.user_id = ? "
            "ORDER BY p.created_at DESC LIMIT 1",
            (clean, result.get("user_id")),
        )
        row2 = await cur2.fetchone()
        result["payment"] = dict(row2) if row2 else None
        return result


# ═══════════════════════════════════════════════════════════════
#  LEDGER: история и сверка
# ═══════════════════════════════════════════════════════════════

async def get_user_transactions(tg_id: int, limit: int = 50) -> list[dict]:
    """История движений баланса пользователя (для поддержки/админки)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM transactions WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (tg_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def reconcile_balances() -> list[dict]:
    """Сверка кэша баланса с журналом. Пустой список = всё сходится.

    Возвращает список расхождений: {tg_id, currency, cached, ledger}.
    Непустой результат = баланс где-то меняли в обход ledger — сигнал к разбору.
    """
    mismatches: list[dict] = []
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT tg_id, tenge_balance, token_balance, bonus_balance FROM users")
        users = [dict(r) for r in await cur.fetchall()]

        for u in users:
            for currency, col in (("tenge", "tenge_balance"), ("token", "token_balance"), ("bonus", "bonus_balance")):
                cur = await db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount "
                    "ELSE -amount END), 0) FROM transactions "
                    "WHERE user_id=? AND currency=? AND status='completed'",
                    (u["tg_id"], currency),
                )
                ledger_sum = float((await cur.fetchone())[0])
                cached = float(u[col] or 0)
                if abs(cached - ledger_sum) > 0.01:
                    mismatches.append({
                        "tg_id": u["tg_id"],
                        "currency": currency,
                        "cached": round(cached, 2),
                        "ledger": round(ledger_sum, 2),
                    })
    return mismatches


# ═══════════════════════════════════════════════════════════════
#  STATS (для админки)
# ═══════════════════════════════════════════════════════════════

async def get_stats() -> dict:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_whitelisted=1")
        whitelist_count = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")
        banned_count = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM banned_usernames")
        banned_username_count = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT status, COUNT(*) FROM turnitin_orders GROUP BY status"
        )
        order_counts = {r[0]: r[1] for r in await cur.fetchall()}

        # Все заказы по типу отчёта (sim | ai | both) — сколько было сделано
        cur = await db.execute(
            "SELECT report_type, COUNT(*) FROM turnitin_orders GROUP BY report_type"
        )
        by_type = {r[0]: r[1] for r in await cur.fetchall()}

        # Завершённые заказы по типу — для справки
        cur = await db.execute(
            "SELECT report_type, COUNT(*) FROM turnitin_orders "
            "WHERE status='done' GROUP BY report_type"
        )
        by_type_done = {r[0]: r[1] for r in await cur.fetchall()}

        # Легаси-путь: прямая оплата Kaspi-чеком в самом боте (payments.purpose='turnitin').
        cur = await db.execute(
            "SELECT payment_type, COUNT(*), SUM(amount_tenge) FROM payments "
            "WHERE status='confirmed' AND purpose='turnitin' GROUP BY payment_type"
        )
        turnitin_revenue = {
            f"{r[0]} (чек в боте)": {"count": r[1], "tenge": r[2] or 0}
            for r in await cur.fetchall()
        }

        # Основной путь сейчас — списание с баланса через ledger (create_paid_order /
        # webapp._start_turnitin), эти заказы в payments вообще не попадают. Берём
        # чистую сумму (списание минус возврат по отменённым заказам) из transactions,
        # отдельно по валюте списания (тенге / бонус).
        cur = await db.execute(
            "SELECT currency, "
            "SUM(CASE WHEN type='debit'  AND reason='order_charge' THEN amount ELSE 0 END) "
            "- SUM(CASE WHEN type='credit' AND reason='order_refund' THEN amount ELSE 0 END) AS net, "
            "COUNT(DISTINCT CASE WHEN type='debit' AND reason='order_charge' THEN order_id END) AS cnt "
            "FROM transactions WHERE reason IN ('order_charge','order_refund') GROUP BY currency"
        )
        currency_label = {"tenge": "с баланса (₸)", "bonus": "с баланса (бонус)"}
        for currency, net, cnt in await cur.fetchall():
            turnitin_revenue[currency_label.get(currency, currency)] = {
                "count": cnt or 0, "tenge": net or 0,
            }

        cur = await db.execute(
            "SELECT payment_type, COUNT(*), SUM(tokens_amount) FROM payments "
            "WHERE status='confirmed' AND purpose='tokens' GROUP BY payment_type"
        )
        token_sales = {r[0]: {"count": r[1], "tokens": r[2] or 0} for r in await cur.fetchall()}

    return {
        "total_users":         total_users,
        "whitelist_count":     whitelist_count,
        "banned_count":        banned_count + banned_username_count,
        "order_counts":        order_counts,
        "by_type":             by_type,
        "by_type_done":        by_type_done,
        "turnitin_revenue":    turnitin_revenue,
        "token_sales":         token_sales,
    }


# ═══════════════════════════════════════════════════════════════
#  BAN  (by tg_id  or  username)
# ═══════════════════════════════════════════════════════════════

async def get_banned_users() -> dict:
    """Все забаненные: по tg_id (в users) и по username (в banned_usernames)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT tg_id, username, full_name, created_at FROM users "
            "WHERE is_banned=1 ORDER BY created_at DESC"
        )
        by_id = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT username, reason, added_at FROM banned_usernames ORDER BY added_at DESC"
        )
        by_username = [dict(r) for r in await cur.fetchall()]

    return {"by_id": by_id, "by_username": by_username}


async def ban_username(username: str, reason: str = "") -> bool:
    """Добавить username в ban-list. True = вставлен, False = уже был."""
    uname = username.lstrip("@").strip()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO banned_usernames(username, reason, added_at) VALUES(?,?,?)",
            (uname, reason, _now()),
        )
        await db.commit()
        inserted = cur.rowcount > 0

    # Дополнительно баним в users, если такой пользователь уже есть
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned=1 WHERE username=? COLLATE NOCASE", (uname,)
        )
        await db.commit()

    return inserted


async def unban_username(username: str) -> bool:
    """Убрать username из ban-list."""
    uname = username.lstrip("@").strip()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM banned_usernames WHERE username=? COLLATE NOCASE", (uname,)
        )
        await db.commit()
        deleted = cur.rowcount > 0

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned=0 WHERE username=? COLLATE NOCASE AND is_banned=1",
            (uname,),
        )
        await db.commit()

    return deleted


async def unban_user(tg_id: int) -> bool:
    """Разбанить пользователя по tg_id."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (tg_id,))
        await db.commit()
        return cur.rowcount > 0


async def is_user_banned(tg_id: int, username: Optional[str] = None) -> bool:
    """Проверить бан по tg_id и/или username."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT is_banned FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row and row[0]:
            return True
        if username:
            uname = username.lstrip("@").strip()
            cur = await db.execute(
                "SELECT 1 FROM banned_usernames WHERE username=? COLLATE NOCASE", (uname,)
            )
            if await cur.fetchone():
                return True
    return False


# ═══════════════════════════════════════════════════════════════
#  PENDING ACTION (Mini App → Bot state without FSM)
# ═══════════════════════════════════════════════════════════════

import json as _json


async def set_pending_action(tg_id: int, action: str, data: Optional[dict] = None):
    """Сохранить ожидаемое действие пользователя (из Mini App)."""
    data_str = _json.dumps(data) if data else None
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE users SET pending_action=?, pending_data=? WHERE tg_id=?",
            (action, data_str, tg_id),
        )
        await db.commit()


async def get_pending_action(tg_id: int) -> dict:
    """Вернуть текущее ожидаемое действие пользователя."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT pending_action, pending_data FROM users WHERE tg_id=?", (tg_id,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return {}
        return {
            "action": row[0],
            "data": _json.loads(row[1]) if row[1] else {},
        }


async def clear_pending_action(tg_id: int):
    """Сбросить ожидаемое действие пользователя."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE users SET pending_action=NULL, pending_data=NULL WHERE tg_id=?",
            (tg_id,),
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════
#  АРЕНДА ИИ-АККАУНТОВ
# ═══════════════════════════════════════════════════════════════

class NoFreeAccount(Exception):
    """Нет свободного аккаунта для аренды (или сервис/тариф неактивен)."""


async def _rental_order_payload(db, order_id: int) -> Optional[dict]:
    """Полные данные аренды (с кредами) — для ответа на покупку."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT ro.id AS order_id, ro.expires_at, ro.status, "
        "       ra.login, ra.password, rs.name AS service_name, "
        "       rt.name AS tariff_name "
        "FROM rental_orders ro "
        "JOIN rental_accounts ra ON ra.id = ro.account_id "
        "JOIN rental_services rs ON rs.id = ro.service_id "
        "JOIN rental_tariffs rt ON rt.id = ro.tariff_id "
        "WHERE ro.id=?",
        (order_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_rental_order(
    user_id: int,
    username: Optional[str],
    service_id: int,
    tariff_id: int,
    idempotency_key: Optional[str] = None,
    use_bonus: bool = False,
) -> dict:
    """Атомарно: списать (бонус сначала, если use_bonus, потом тенге) +
    захватить свободный аккаунт + создать аренду + ledger.

    Всё в одной БД-транзакции. Ключ идемпотентности: rental:<tg_id>:<request_id>.
    Бросает InsufficientFunds / NoFreeAccount.
    Возвращает {ok, duplicate, order_id, service_name, tariff_name,
                expires_at, login, password, balance, bonus_balance}.
    """
    from datetime import timedelta
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        # 1. Идемпотентность: уже проводили — отдаём ту же аренду с теми же кредами
        if idempotency_key:
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                payload = await _rental_order_payload(db, row[0]) or {}
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "balance": float(row[1]),
                        "bonus_balance": float(brow[0]) if brow else 0.0, **payload}

        # 2. Тариф и сервис должны существовать и быть активными
        cur = await db.execute(
            "SELECT t.price, t.duration_hours FROM rental_tariffs t "
            "JOIN rental_services s ON s.id = t.service_id "
            "WHERE t.id=? AND t.service_id=? AND t.is_active=1 AND s.is_active=1",
            (tariff_id, service_id),
        )
        tariff = await cur.fetchone()
        if not tariff:
            await db.rollback()
            raise NoFreeAccount()
        price, duration_hours = float(tariff[0]), int(tariff[1])

        # 3. Атомарное списание (как в create_paid_order)
        bonus_used, tenge_used = await _apply_bonus_debit(db, user_id, price, use_bonus)

        cur = await db.execute(
            "SELECT tenge_balance, bonus_balance FROM users WHERE tg_id=?", (user_id,)
        )
        balance, bonus_balance = (float(x) for x in await cur.fetchone())

        # 4. Захват свободного аккаунта. WHERE status='free' — защита от выдачи
        #    одного аккаунта двоим (single-writer SQLite сериализует транзакции).
        account_id = None
        for _ in range(3):
            cur = await db.execute(
                "SELECT id FROM rental_accounts "
                "WHERE service_id=? AND status='free' ORDER BY id LIMIT 1",
                (service_id,),
            )
            row = await cur.fetchone()
            if not row:
                break
            cur = await db.execute(
                "UPDATE rental_accounts SET status='rented', updated_at=? "
                "WHERE id=? AND status='free'",
                (now, row[0]),
            )
            if cur.rowcount:
                account_id = row[0]
                break
        if account_id is None:
            await db.rollback()
            raise NoFreeAccount()

        # 5. Создаём аренду
        expires_at = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
        cur = await db.execute(
            "INSERT INTO rental_orders"
            "(user_id, username, service_id, tariff_id, account_id, amount_tenge,"
            " status, starts_at, expires_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
            (user_id, username, service_id, tariff_id, account_id, price,
             now, expires_at, now, now),
        )
        order_id = cur.lastrowid
        await db.execute(
            "UPDATE rental_accounts SET current_order_id=? WHERE id=?",
            (order_id, account_id),
        )

        # 6. Журналируем списание — 1-2 строки в зависимости от участия бонуса
        try:
            if bonus_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "rental_charge", "bonus",
                     bonus_used, bonus_balance,
                     f"{idempotency_key}:bonus" if idempotency_key else None, now),
                )
            if tenge_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "rental_charge", "tenge",
                     tenge_used, balance, idempotency_key, now),
                )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                payload = await _rental_order_payload(db, row[0]) or {}
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "balance": float(row[1]),
                        "bonus_balance": float(brow[0]) if brow else 0.0, **payload}
            raise

        payload = await _rental_order_payload(db, order_id) or {}
        return {"ok": True, "duplicate": False, "balance": balance,
                "bonus_balance": bonus_balance, **payload}


async def cancel_rental_with_refund(order_id: int, reason: str = "") -> dict:
    """Отменить аренду с возвратом денег. Аккаунт → maintenance
    (пользователь видел пароль — нужна ротация перед возвратом в пул)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM rental_orders WHERE id=?", (order_id,))
        order = await cur.fetchone()
    if not order:
        return {"ok": False, "error": "not_found"}
    order = dict(order)
    if order["status"] != "active":
        return {"ok": False, "error": "already_final", "status_before": order["status"]}

    amount = float(order["amount_tenge"] or 0)
    refunded = 0.0
    if amount > 0:
        # rental_refund:<id> — отдельный namespace, не пересекается с refund:<id> (turnitin)
        split = await _get_charge_split(order_id, "rental_charge")
        bonus_part = split.get("bonus", 0.0)
        tenge_part = split.get("tenge", amount - bonus_part)
        if bonus_part > 0:
            await add_bonus(
                order["user_id"], bonus_part,
                reason="rental_refund", order_id=order_id,
                idempotency_key=f"rental_refund:{order_id}:bonus",
            )
        if tenge_part > 0:
            await add_tenge(
                order["user_id"], tenge_part,
                reason="rental_refund", order_id=order_id,
                idempotency_key=f"rental_refund:{order_id}",
            )
        refunded = bonus_part + tenge_part

    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE rental_orders SET status='cancelled', updated_at=? WHERE id=?",
            (now, order_id),
        )
        await db.execute(
            "UPDATE rental_accounts SET status='maintenance', current_order_id=NULL, "
            "updated_at=? WHERE id=? AND current_order_id=?",
            (now, order["account_id"], order_id),
        )
        await db.commit()
    return {"ok": True, "refunded": refunded,
            "user_id": order["user_id"], "service_id": order["service_id"]}


async def expire_rental(order_id: int) -> Optional[dict]:
    """Аренда истекла: заказ → expired, аккаунт → maintenance (до ротации пароля)."""
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM rental_orders WHERE id=? AND status='active'", (order_id,)
        )
        order = await cur.fetchone()
        if not order:
            return None
        order = dict(order)
        await db.execute(
            "UPDATE rental_orders SET status='expired', updated_at=? WHERE id=?",
            (now, order_id),
        )
        await db.execute(
            "UPDATE rental_accounts SET status='maintenance', current_order_id=NULL, "
            "updated_at=? WHERE id=? AND current_order_id=?",
            (now, order["account_id"], order_id),
        )
        await db.commit()
    return order


async def get_due_rentals(now_iso: str) -> list[dict]:
    """Активные аренды, у которых срок уже вышел."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ro.*, rs.name AS service_name FROM rental_orders ro "
            "JOIN rental_services rs ON rs.id = ro.service_id "
            "WHERE ro.status='active' AND ro.expires_at<=?",
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_reminder_due_rentals(threshold_iso: str, now_iso: str) -> list[dict]:
    """Активные аренды, истекающие в ближайшие ~30 мин, без отправленного напоминания."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ro.*, rs.name AS service_name FROM rental_orders ro "
            "JOIN rental_services rs ON rs.id = ro.service_id "
            "WHERE ro.status='active' AND ro.reminder_sent=0 "
            "AND ro.expires_at<=? AND ro.expires_at>?",
            (threshold_iso, now_iso),
        )
        return [dict(r) for r in await cur.fetchall()]


async def mark_rental_reminder_sent(order_id: int):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE rental_orders SET reminder_sent=1, updated_at=? WHERE id=?",
            (_now(), order_id),
        )
        await db.commit()


# ── Каталог и «мои аренды» ──────────────────────────────────────

async def get_rental_services_catalog() -> list[dict]:
    """Активные сервисы с тарифами и наличием. Кредов здесь НЕТ и быть не должно."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT s.id, s.name, s.description, s.icon, s.sort_order, "
            " (SELECT COUNT(*) FROM rental_accounts a "
            "   WHERE a.service_id=s.id AND a.status='free') AS available, "
            " (SELECT COUNT(*) FROM rental_waitlist w "
            "   WHERE w.service_id=s.id) AS waiting "
            "FROM rental_services s WHERE s.is_active=1 "
            "ORDER BY s.sort_order, s.id"
        )
        services = [dict(r) for r in await cur.fetchall()]
        if not services:
            return []
        cur = await db.execute(
            "SELECT id, service_id, name, duration_hours, price "
            "FROM rental_tariffs WHERE is_active=1 ORDER BY sort_order, duration_hours"
        )
        tariffs = [dict(r) for r in await cur.fetchall()]
    by_svc: dict[int, list] = {}
    for t in tariffs:
        by_svc.setdefault(t["service_id"], []).append(t)
    for s in services:
        s["tariffs"] = by_svc.get(s["id"], [])
    return [s for s in services if s["tariffs"]]


async def get_rental_service(service_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM rental_services WHERE id=?", (service_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_rentals(tg_id: int, history_limit: int = 10) -> list[dict]:
    """Аренды пользователя: активные с кредами + последние завершённые без."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ro.id, ro.status, ro.starts_at, ro.expires_at, ro.amount_tenge, "
            "       rs.name AS service_name, rs.icon, rt.name AS tariff_name, "
            "       ra.login, ra.password "
            "FROM rental_orders ro "
            "JOIN rental_services rs ON rs.id = ro.service_id "
            "JOIN rental_tariffs rt ON rt.id = ro.tariff_id "
            "JOIN rental_accounts ra ON ra.id = ro.account_id "
            "WHERE ro.user_id=? ORDER BY ro.id DESC LIMIT ?",
            (tg_id, history_limit + 20),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    result = []
    finished = 0
    for r in rows:
        if r["status"] != "active":
            # Креды отдаём только по активной аренде
            r.pop("login", None)
            r.pop("password", None)
            finished += 1
            if finished > history_limit:
                continue
        result.append(r)
    return result


# ── Waitlist ────────────────────────────────────────────────────

async def add_to_waitlist(service_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO rental_waitlist(service_id,user_id,created_at) "
            "VALUES(?,?,?)",
            (service_id, user_id, _now()),
        )
        await db.commit()
        return cur.rowcount > 0


async def pop_waitlist(service_id: int) -> list[int]:
    """Забрать и очистить весь waitlist сервиса (одноразовое уведомление всем)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM rental_waitlist WHERE service_id=? ORDER BY id",
            (service_id,),
        )
        users = [r[0] for r in await cur.fetchall()]
        if users:
            await db.execute(
                "DELETE FROM rental_waitlist WHERE service_id=?", (service_id,)
            )
            await db.commit()
        return users


# ── Админ: сервисы / тарифы / склад / аренды ────────────────────

async def get_rental_services_admin() -> list[dict]:
    """Все сервисы (включая выключенные) с тарифами и счётчиками склада.

    Счётчики берутся из ai_accounts (v2, email+OTP) — старая rental_accounts
    больше не пополняется, только читается историческими заказами."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM ai_accounts a WHERE a.service_id=s.id AND a.status='available') AS cnt_free, "
            " (SELECT COUNT(*) FROM ai_accounts a WHERE a.service_id=s.id AND a.status='rented') AS cnt_rented, "
            " (SELECT COUNT(*) FROM ai_accounts a WHERE a.service_id=s.id AND a.status IN ('maintenance','cooldown','disabled','banned')) AS cnt_maintenance, "
            " (SELECT COUNT(*) FROM rental_waitlist w WHERE w.service_id=s.id) AS waiting "
            "FROM rental_services s ORDER BY s.sort_order, s.id"
        )
        services = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT * FROM rental_tariffs ORDER BY sort_order, duration_hours"
        )
        tariffs = [dict(r) for r in await cur.fetchall()]
    by_svc: dict[int, list] = {}
    for t in tariffs:
        by_svc.setdefault(t["service_id"], []).append(t)
    for s in services:
        s["tariffs"] = by_svc.get(s["id"], [])
    return services


async def upsert_rental_service(
    name: str, description: str = "", icon: str = "",
    is_active: bool = True, sort_order: int = 0,
    service_id: Optional[int] = None,
) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        if service_id:
            await db.execute(
                "UPDATE rental_services SET name=?, description=?, icon=?, "
                "is_active=?, sort_order=? WHERE id=?",
                (name, description, icon, int(is_active), sort_order, service_id),
            )
            await db.commit()
            return service_id
        cur = await db.execute(
            "INSERT INTO rental_services(name,description,icon,is_active,sort_order,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (name, description, icon, int(is_active), sort_order, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def upsert_rental_tariff(
    service_id: int, name: str, duration_hours: int, price: float,
    is_active: bool = True, sort_order: int = 0,
    tariff_id: Optional[int] = None,
) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        if tariff_id:
            await db.execute(
                "UPDATE rental_tariffs SET service_id=?, name=?, duration_hours=?, "
                "price=?, is_active=?, sort_order=? WHERE id=?",
                (service_id, name, duration_hours, price, int(is_active),
                 sort_order, tariff_id),
            )
            await db.commit()
            return tariff_id
        cur = await db.execute(
            "INSERT INTO rental_tariffs(service_id,name,duration_hours,price,"
            "is_active,sort_order,created_at) VALUES(?,?,?,?,?,?,?)",
            (service_id, name, duration_hours, price, int(is_active),
             sort_order, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def delete_rental_service(service_id: int) -> bool:
    """Удалить сервис из каталога. Нельзя, если есть аккаунты на складе или заказы аренды —
    сначала нужно убрать их (защита от осиротевших ссылок в истории/складе)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM rental_services WHERE id=? "
            "AND NOT EXISTS (SELECT 1 FROM rental_accounts WHERE service_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM rental_orders WHERE service_id=?)",
            (service_id, service_id, service_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_rental_tariff(tariff_id: int) -> bool:
    """Удалить тариф. Нельзя, если по нему уже были заказы аренды (сохраняем историю)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM rental_tariffs WHERE id=? "
            "AND NOT EXISTS (SELECT 1 FROM rental_orders WHERE tariff_id=?)",
            (tariff_id, tariff_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_rental_accounts(service_id: Optional[int] = None) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if service_id:
            cur = await db.execute(
                "SELECT * FROM rental_accounts WHERE service_id=? ORDER BY status, id",
                (service_id,),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM rental_accounts ORDER BY service_id, status, id"
            )
        return [dict(r) for r in await cur.fetchall()]


async def count_free_accounts(service_id: int) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM rental_accounts WHERE service_id=? AND status='free'",
            (service_id,),
        )
        return (await cur.fetchone())[0]


async def add_rental_account(service_id: int, login: str, password: str,
                             note: str = "") -> int:
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO rental_accounts(service_id,login,password,note,status,"
            "created_at,updated_at) VALUES(?,?,?,?,'free',?,?)",
            (service_id, login, password, note, now, now),
        )
        await db.commit()
        return cur.lastrowid


async def get_rental_account(account_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM rental_accounts WHERE id=?", (account_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_rental_account(account_id: int, **fields) -> bool:
    """Обновить аккаунт (login/password/note/status). updated_at — автоматически."""
    allowed = {k: v for k, v in fields.items()
               if k in ("login", "password", "note", "status") and v is not None}
    if not allowed:
        return False
    allowed["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in allowed)
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE rental_accounts SET {cols} WHERE id=?",
            (*allowed.values(), account_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_rental_account(account_id: int) -> bool:
    """Удалить аккаунт со склада. Арендованный удалять нельзя."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM rental_accounts WHERE id=? AND status!='rented'",
            (account_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_active_rentals_admin() -> list[dict]:
    """Активные аренды для админки (без паролей — только логин аккаунта)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ro.id, ro.user_id, ro.username, ro.amount_tenge, "
            "       ro.starts_at, ro.expires_at, "
            "       rs.name AS service_name, rt.name AS tariff_name, "
            "       ra.login AS account_login "
            "FROM rental_orders ro "
            "JOIN rental_services rs ON rs.id = ro.service_id "
            "JOIN rental_tariffs rt ON rt.id = ro.tariff_id "
            "JOIN rental_accounts ra ON ra.id = ro.account_id "
            "WHERE ro.status='active' ORDER BY ro.expires_at"
        )
        return [dict(r) for r in await cur.fetchall()]


# ═══════════════════════════════════════════════════════════════
#  ПРОМОКОДЫ
#
#  type: fixed (фикс. ₸, редимится мгновенно) | percent (% от суммы
#  пополнения — консьюмится в момент подтверждения Kaspi-чека, см.
#  validate_promo_for_topup / redeem_promo_percent).
#  Бонус всегда идёт в tenge_balance с отдельным ledger-reason
#  (promo_fixed/promo_percent) — это и есть «бонусный баланс» по ТЗ,
#  полностью прослеживаемый через transactions, без отдельной колонки.
# ═══════════════════════════════════════════════════════════════

class PromoNotFound(Exception):
    """Промокод с таким кодом не найден (или удалён)."""


class PromoNotActive(Exception):
    """Срок действия промокода ещё не начался или уже истёк."""


class PromoExhausted(Exception):
    """Исчерпан общий лимит активаций промокода."""


class PromoAlreadyUsed(Exception):
    """Пользователь уже исчерпал свой лимит использований этого промокода."""


PROMO_ERROR_MESSAGES = {
    PromoNotFound:    "Промокод не найден.",
    PromoNotActive:   "Срок действия этого промокода истёк.",
    PromoExhausted:   "К сожалению, этот промокод больше недоступен.",
    PromoAlreadyUsed: "Вы уже использовали этот промокод.",
}


def _normalize_promo_code(code: str) -> str:
    return (code or "").strip().upper()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None


async def create_promo(
    code: str,
    type_: str,
    value: float,
    per_user_limit: int = 1,
    total_limit: Optional[int] = None,
    starts_at: Optional[str] = None,
    ends_at: Optional[str] = None,
) -> int:
    """Создать промокод. Бросает ValueError на дубликат кода / некорректные поля."""
    code = _normalize_promo_code(code)
    if not code:
        raise ValueError("Код не может быть пустым")
    if type_ not in ("fixed", "percent"):
        raise ValueError("Тип должен быть 'fixed' или 'percent'")
    if value <= 0:
        raise ValueError("Размер бонуса должен быть больше 0")
    if per_user_limit < 1:
        raise ValueError("Лимит на пользователя должен быть не меньше 1")
    if total_limit is not None and total_limit < 1:
        raise ValueError("Общий лимит должен быть не меньше 1")

    async with aiosqlite.connect(_DB_PATH) as db:
        try:
            cur = await db.execute(
                "INSERT INTO promo_codes(code,type,value,per_user_limit,total_limit,"
                "activations_count,starts_at,ends_at,is_deleted,created_at) "
                "VALUES(?,?,?,?,?,0,?,?,0,?)",
                (code, type_, value, per_user_limit, total_limit, starts_at, ends_at, _now()),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            await db.rollback()
            raise ValueError(f"Промокод '{code}' уже существует")


def _promo_status(row: dict, now: datetime) -> str:
    if row.get("total_limit") is not None and row["activations_count"] >= row["total_limit"]:
        return "exhausted"
    ends = _parse_dt(row.get("ends_at"))
    if ends and now > ends:
        return "expired"
    starts = _parse_dt(row.get("starts_at"))
    if starts and now < starts:
        return "scheduled"
    return "active"


async def list_promos() -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM promo_codes WHERE is_deleted=0 ORDER BY id DESC"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    now = datetime.utcnow()
    for r in rows:
        r["status"] = _promo_status(r, now)
    return rows


async def delete_promo(promo_id: int) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "UPDATE promo_codes SET is_deleted=1 WHERE id=? AND is_deleted=0", (promo_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def _find_active_promo(db, code: str) -> dict:
    """SELECT промокода по коду + проверка окна действия (без per-user/total)."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT * FROM promo_codes WHERE code=? AND is_deleted=0", (code,)
    )
    row = await cur.fetchone()
    if not row:
        await db.rollback()
        raise PromoNotFound()
    promo = dict(row)

    now = datetime.utcnow()
    starts = _parse_dt(promo.get("starts_at"))
    ends = _parse_dt(promo.get("ends_at"))
    if (starts and now < starts) or (ends and now > ends):
        await db.rollback()
        raise PromoNotActive()

    return promo


async def _check_promo_user_limit(db, promo: dict, user_id: int):
    cur = await db.execute(
        "SELECT COUNT(*) FROM promo_redemptions WHERE promo_id=? AND user_id=?",
        (promo["id"], user_id),
    )
    used = (await cur.fetchone())[0]
    if used >= promo["per_user_limit"]:
        await db.rollback()
        raise PromoAlreadyUsed()


async def validate_promo_for_topup(user_id: int, code: str) -> dict:
    """Лёгкая пре-проверка percent-кода (окно действия + лимиты), без консьюминга.

    Кидает PromoNotFound/PromoNotActive/PromoExhausted/PromoAlreadyUsed/ValueError.
    """
    code = _normalize_promo_code(code)
    async with aiosqlite.connect(_DB_PATH) as db:
        promo = await _find_active_promo(db, code)
        if promo["type"] != "percent":
            raise ValueError("Этот промокод активируется мгновенно, без пополнения")
        if promo.get("total_limit") is not None and promo["activations_count"] >= promo["total_limit"]:
            raise PromoExhausted()
        await _check_promo_user_limit(db, promo, user_id)
        return {"code": code, "type": promo["type"], "value": promo["value"]}


async def apply_promo_fixed(user_id: int, code: str) -> dict:
    """Мгновенное применение fixed-промокода. Атомарно: лимиты + баланс + ledger."""
    code = _normalize_promo_code(code)
    async with aiosqlite.connect(_DB_PATH) as db:
        promo = await _find_active_promo(db, code)
        if promo["type"] != "fixed":
            raise ValueError("Этот промокод активируется при пополнении баланса")
        await _check_promo_user_limit(db, promo, user_id)

        cur = await db.execute(
            "UPDATE promo_codes SET activations_count=activations_count+1 "
            "WHERE id=? AND (total_limit IS NULL OR activations_count<total_limit)",
            (promo["id"],),
        )
        if cur.rowcount == 0:
            await db.rollback()
            raise PromoExhausted()

        now = _now()
        await db.execute(
            "UPDATE users SET bonus_balance=bonus_balance+? WHERE tg_id=?",
            (promo["value"], user_id),
        )
        cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
        row = await cur.fetchone()
        new_balance = float(row[0]) if row else promo["value"]

        await db.execute(
            "INSERT INTO promo_redemptions(promo_id,user_id,amount_credited,created_at) "
            "VALUES(?,?,?,?)",
            (promo["id"], user_id, promo["value"], now),
        )
        await db.execute(
            _tx_insert_sql(),
            (user_id, None, "credit", "promo_fixed", "bonus",
             promo["value"], new_balance, None, now),
        )
        await db.commit()
        return {"bonus": promo["value"], "new_balance": new_balance}


async def redeem_promo_percent(user_id: int, code: str, base_amount: float) -> dict:
    """Финальное применение percent-промо в момент подтверждения пополнения.

    Best-effort: любая ошибка валидации возвращается как {"applied": False, "reason": ...}
    вместо исключения — базовое пополнение уже прошло и откатывать его нельзя.
    """
    code = _normalize_promo_code(code)
    try:
        async with aiosqlite.connect(_DB_PATH) as db:
            promo = await _find_active_promo(db, code)
            if promo["type"] != "percent":
                raise ValueError("not a percent promo")
            await _check_promo_user_limit(db, promo, user_id)

            cur = await db.execute(
                "UPDATE promo_codes SET activations_count=activations_count+1 "
                "WHERE id=? AND (total_limit IS NULL OR activations_count<total_limit)",
                (promo["id"],),
            )
            if cur.rowcount == 0:
                await db.rollback()
                raise PromoExhausted()

            credited = round(base_amount * promo["value"] / 100, 2)
            now = _now()
            await db.execute(
                "UPDATE users SET bonus_balance=bonus_balance+? WHERE tg_id=?",
                (credited, user_id),
            )
            cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
            row = await cur.fetchone()
            new_balance = float(row[0]) if row else credited

            await db.execute(
                "INSERT INTO promo_redemptions(promo_id,user_id,amount_credited,created_at) "
                "VALUES(?,?,?,?)",
                (promo["id"], user_id, credited, now),
            )
            await db.execute(
                _tx_insert_sql(),
                (user_id, None, "credit", "promo_percent", "bonus",
                 credited, new_balance, None, now),
            )
            await db.commit()
            return {"applied": True, "bonus": credited, "new_balance": new_balance}
    except (PromoNotFound, PromoNotActive, PromoExhausted, PromoAlreadyUsed) as e:
        return {"applied": False, "reason": PROMO_ERROR_MESSAGES[type(e)]}
    except ValueError:
        return {"applied": False, "reason": "Промокод не подходит для пополнения"}


async def get_all_user_ids(exclude_banned: bool = True) -> list[int]:
    """Все tg_id пользователей — для рассылки."""
    async with aiosqlite.connect(_DB_PATH) as db:
        q = "SELECT tg_id FROM users"
        if exclude_banned:
            q += " WHERE is_banned=0"
        cur = await db.execute(q)
        return [r[0] for r in await cur.fetchall()]


# ═══════════════════════════════════════════════════════════════
#  АРЕНДА ИИ-АККАУНТОВ v2 (email+OTP, авто-разлогин, прокси-группы)
#
#  Каталог (rental_services/rental_tariffs) и waitlist (rental_waitlist)
#  переиспользуются как есть — концепция «услуга + тариф на N часов» не
#  изменилась. Новое — учёт прокси, аккаунтов по email (без пароля,
#  логин через OTP) и сами аренды (ai_rentals вместо rental_orders).
# ═══════════════════════════════════════════════════════════════

class ProxyFull(Exception):
    """У выбранного прокси уже max_accounts привязанных аккаунтов."""


# ── Прокси ────────────────────────────────────────────────────────

async def create_ai_proxy(proxy_url: str, max_accounts: int = 3) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO ai_proxies(proxy_url,max_accounts,is_active,created_at) VALUES(?,?,1,?)",
            (proxy_url, max_accounts, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def list_ai_proxies() -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM ai_accounts a WHERE a.proxy_id=p.id) AS accounts_count "
            "FROM ai_proxies p ORDER BY p.id"
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_ai_proxy(proxy_id: int) -> bool:
    """Удалить прокси. Нельзя, если к нему ещё привязаны аккаунты."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM ai_proxies WHERE id=? "
            "AND NOT EXISTS (SELECT 1 FROM ai_accounts WHERE proxy_id=?)",
            (proxy_id, proxy_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ── Аккаунты ──────────────────────────────────────────────────────

async def add_ai_account(service_id: int, email: str, proxy_id: Optional[int] = None) -> int:
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        if proxy_id is not None:
            cur = await db.execute(
                "SELECT max_accounts, (SELECT COUNT(*) FROM ai_accounts WHERE proxy_id=?) "
                "FROM ai_proxies WHERE id=?",
                (proxy_id, proxy_id),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                raise ValueError("Прокси не найден")
            max_accounts, current = row
            if current >= max_accounts:
                await db.rollback()
                raise ProxyFull()
        try:
            cur = await db.execute(
                "INSERT INTO ai_accounts(service_id,email,proxy_id,status,created_at,updated_at) "
                "VALUES(?,?,?,'available',?,?)",
                (service_id, email, proxy_id, now, now),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            await db.rollback()
            raise ValueError(f"Email '{email}' уже используется")


async def list_ai_accounts(service_id: Optional[int] = None) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if service_id is not None:
            cur = await db.execute(
                "SELECT * FROM ai_accounts WHERE service_id=? ORDER BY id DESC", (service_id,)
            )
        else:
            cur = await db.execute("SELECT * FROM ai_accounts ORDER BY id DESC")
        return [dict(r) for r in await cur.fetchall()]


async def get_ai_account(account_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ai_accounts WHERE id=?", (account_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_ai_account_by_email(email: str) -> Optional[dict]:
    """Для резолва magic-link писем (Claude и т.п.) — там письмо приходит на
    email аккаунта, но нет order_id/account_id, только сам адрес."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ai_accounts WHERE email=?", (email,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_ai_account_status(account_id: int, status: str) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "UPDATE ai_accounts SET status=?, updated_at=? WHERE id=?",
            (status, _now(), account_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def save_ai_account_cookies(account_id: int, cookies_json: str):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE ai_accounts SET cookies_data=?, updated_at=? WHERE id=?",
            (cookies_json, _now(), account_id),
        )
        await db.commit()


async def delete_ai_account(account_id: int) -> bool:
    """Нельзя удалить аккаунт прямо сейчас в аренде."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM ai_accounts WHERE id=? AND status!='rented'", (account_id,)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Каталог с учётом наличия (переиспользует rental_services/rental_tariffs) ──

async def get_ai_services_catalog() -> list[dict]:
    """Активные сервисы с тарифами и наличием (email-аккаунты, без кредов)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT s.id, s.name, s.description, s.icon, s.sort_order, "
            " (SELECT COUNT(*) FROM ai_accounts a "
            "   WHERE a.service_id=s.id AND a.status='available') AS available, "
            " (SELECT COUNT(*) FROM rental_waitlist w "
            "   WHERE w.service_id=s.id) AS waiting "
            "FROM rental_services s WHERE s.is_active=1 "
            "ORDER BY s.sort_order, s.id"
        )
        services = [dict(r) for r in await cur.fetchall()]
        if not services:
            return []
        cur = await db.execute(
            "SELECT id, service_id, name, duration_hours, price "
            "FROM rental_tariffs WHERE is_active=1 ORDER BY sort_order, duration_hours"
        )
        tariffs = [dict(r) for r in await cur.fetchall()]
    by_svc: dict[int, list] = {}
    for t in tariffs:
        by_svc.setdefault(t["service_id"], []).append(t)
    for s in services:
        s["tariffs"] = by_svc.get(s["id"], [])
    return [s for s in services if s["tariffs"]]


# ── LRU-выбор и создание аренды ──────────────────────────────────────

async def _get_lru_account_id(db, service_id: int) -> Optional[int]:
    """Тот свободный аккаунт, который отдыхал дольше всех (никогда не
    использовавшиеся — первыми)."""
    cur = await db.execute(
        "SELECT id FROM ai_accounts WHERE service_id=? AND status='available' "
        "ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC LIMIT 1",
        (service_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _ai_rental_payload(db, order_id: int) -> Optional[dict]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT r.id AS order_id, r.expires_at, r.status, "
        "       a.email, rs.name AS service_name, rt.name AS tariff_name "
        "FROM ai_rentals r "
        "JOIN ai_accounts a ON a.id = r.account_id "
        "JOIN rental_services rs ON rs.id = r.service_id "
        "JOIN rental_tariffs rt ON rt.id = r.tariff_id "
        "WHERE r.id=?",
        (order_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_ai_rental(
    user_id: int,
    username: Optional[str],
    service_id: int,
    tariff_id: int,
    use_bonus: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict:
    """Атомарно: списать (бонус сначала, если use_bonus, потом тенге) + захватить
    LRU-свободный аккаунт + создать аренду + ledger.

    Возвращает {ok, duplicate, order_id, service_name, tariff_name, email,
                expires_at, balance, bonus_balance}.
    Бросает InsufficientFunds / NoFreeAccount.
    """
    from datetime import timedelta
    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        # 1. Идемпотентность
        if idempotency_key:
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                payload = await _ai_rental_payload(db, row[0]) or {}
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "balance": float(row[1]),
                        "bonus_balance": float(brow[0]) if brow else 0.0, **payload}

        # 2. Тариф должен существовать и быть активным
        cur = await db.execute(
            "SELECT price, duration_hours FROM rental_tariffs "
            "WHERE id=? AND service_id=? AND is_active=1",
            (tariff_id, service_id),
        )
        tariff = await cur.fetchone()
        if not tariff:
            await db.rollback()
            raise NoFreeAccount()
        price, duration_hours = float(tariff[0]), int(tariff[1])

        # 3. Атомарное списание (бонус сначала, потом тенге)
        bonus_used, tenge_used = await _apply_bonus_debit(db, user_id, price, use_bonus)

        cur = await db.execute(
            "SELECT tenge_balance, bonus_balance FROM users WHERE tg_id=?", (user_id,)
        )
        balance, bonus_balance = (float(x) for x in await cur.fetchone())

        # 4. Захват LRU-свободного аккаунта
        account_id = await _get_lru_account_id(db, service_id)
        if account_id is None:
            await db.rollback()
            raise NoFreeAccount()
        cur = await db.execute(
            "UPDATE ai_accounts SET status='rented', updated_at=? WHERE id=? AND status='available'",
            (now, account_id),
        )
        if cur.rowcount == 0:
            await db.rollback()
            raise NoFreeAccount()

        # 5. Создаём аренду
        expires_at = (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat()
        cur = await db.execute(
            "INSERT INTO ai_rentals(user_id,username,service_id,tariff_id,account_id,"
            "amount_tenge,paid_bonus,paid_main,status,starts_at,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?)",
            (user_id, username, service_id, tariff_id, account_id, price,
             bonus_used, tenge_used, now, expires_at, now, now),
        )
        order_id = cur.lastrowid
        await db.execute(
            "UPDATE ai_accounts SET current_order_id=?, last_used_at=? WHERE id=?",
            (order_id, now, account_id),
        )

        # 6. Журналируем списание
        try:
            if bonus_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "ai_rental_charge", "bonus",
                     bonus_used, bonus_balance,
                     f"{idempotency_key}:bonus" if idempotency_key else None, now),
                )
            if tenge_used > 0:
                await db.execute(
                    _tx_insert_sql(),
                    (user_id, order_id, "debit", "ai_rental_charge", "tenge",
                     tenge_used, balance, idempotency_key, now),
                )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            cur = await db.execute(
                "SELECT order_id, balance_after FROM transactions WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            if row:
                payload = await _ai_rental_payload(db, row[0]) or {}
                cur = await db.execute("SELECT bonus_balance FROM users WHERE tg_id=?", (user_id,))
                brow = await cur.fetchone()
                return {"ok": True, "duplicate": True, "balance": float(row[1]),
                        "bonus_balance": float(brow[0]) if brow else 0.0, **payload}
            raise

        payload = await _ai_rental_payload(db, order_id) or {}
        return {"ok": True, "duplicate": False, "balance": balance,
                "bonus_balance": bonus_balance, **payload}


async def get_user_ai_rentals(tg_id: int, history_limit: int = 10) -> dict:
    """{'active': [...], 'history': [...]} — активные и недавние завершённые аренды юзера."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.id, r.status, r.starts_at, r.expires_at, r.amount_tenge, "
            "       a.email, rs.name AS service_name, rs.icon, rt.name AS tariff_name "
            "FROM ai_rentals r "
            "JOIN ai_accounts a ON a.id = r.account_id "
            "JOIN rental_services rs ON rs.id = r.service_id "
            "JOIN rental_tariffs rt ON rt.id = r.tariff_id "
            "WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 30",
            (tg_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    active = [r for r in rows if r["status"] == "active"]
    history = [r for r in rows if r["status"] != "active"][:history_limit]
    return {"active": active, "history": history}


async def get_active_ai_rental_by_email(user_id: int, email: str) -> Optional[dict]:
    """Проверка владения: активна ли у этого юзера аренда именно этого email."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.* FROM ai_rentals r JOIN ai_accounts a ON a.id=r.account_id "
            "WHERE r.user_id=? AND a.email=? AND r.status='active'",
            (user_id, email),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Отмена/возврат и истечение ───────────────────────────────────────

async def cancel_ai_rental_with_refund(order_id: int, reason: str = "") -> dict:
    """Отменить аренду (админ) с возвратом денег — каждой валюте туда, откуда
    списывалась. Аккаунт переводится в 'cooldown' (та же логика, что при
    естественном истечении) — вернётся в пул после стандартного окна cooldown
    даже если немедленный форс-разлогин (вызывается отдельно из api.py через
    ai_rental_manager) почему-то не сработает — защита в глубину.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ai_rentals WHERE id=?", (order_id,))
        order = await cur.fetchone()
    if not order:
        return {"ok": False, "error": "not_found"}
    order = dict(order)
    if order["status"] != "active":
        return {"ok": False, "error": "already_final", "status_before": order["status"]}

    amount = float(order["amount_tenge"] or 0)
    refunded = 0.0
    if amount > 0:
        split = await _get_charge_split(order_id, "ai_rental_charge")
        bonus_part = split.get("bonus", 0.0)
        tenge_part = split.get("tenge", amount - bonus_part)
        if bonus_part > 0:
            await add_bonus(
                order["user_id"], bonus_part,
                reason="ai_rental_refund", order_id=order_id,
                idempotency_key=f"ai_rental_refund:{order_id}:bonus",
            )
        if tenge_part > 0:
            await add_tenge(
                order["user_id"], tenge_part,
                reason="ai_rental_refund", order_id=order_id,
                idempotency_key=f"ai_rental_refund:{order_id}",
            )
        refunded = bonus_part + tenge_part

    now = _now()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE ai_rentals SET status='cancelled', updated_at=? WHERE id=?",
            (now, order_id),
        )
        await db.execute(
            "UPDATE ai_accounts SET status='cooldown', current_order_id=NULL, "
            "updated_at=? WHERE id=? AND current_order_id=?",
            (now, order["account_id"], order_id),
        )
        await db.commit()
    return {"ok": True, "refunded": refunded, "user_id": order["user_id"],
            "service_id": order["service_id"], "account_id": order["account_id"]}


async def get_due_ai_rentals(now_iso: str) -> list[dict]:
    """Активные аренды, у которых уже прошёл expires_at."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.*, rs.name AS service_name FROM ai_rentals r "
            "JOIN rental_services rs ON rs.id = r.service_id "
            "WHERE r.status='active' AND r.expires_at<=?",
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_ai_reminder_due_rentals(threshold_iso: str, now_iso: str) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.*, rs.name AS service_name FROM ai_rentals r "
            "JOIN rental_services rs ON rs.id = r.service_id "
            "WHERE r.status='active' AND r.reminder_sent=0 "
            "AND r.expires_at<=? AND r.expires_at>?",
            (threshold_iso, now_iso),
        )
        return [dict(r) for r in await cur.fetchall()]


async def mark_ai_rental_reminder_sent(order_id: int):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "UPDATE ai_rentals SET reminder_sent=1, updated_at=? WHERE id=?",
            (_now(), order_id),
        )
        await db.commit()


async def expire_ai_rental(order_id: int) -> Optional[dict]:
    """Аренда истекла: статус → expired. Аккаунт НЕ трогаем здесь — это делает
    ai_rental_manager после реального разлогина (Playwright), выставляя
    cooldown/available сам. Возвращает данные для постановки в очередь разлогина,
    либо None если уже обработано параллельно (idempotent-safe)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ai_rentals WHERE id=? AND status='active'", (order_id,)
        )
        order = await cur.fetchone()
        if not order:
            return None
        order = dict(order)
        now = _now()
        cur = await db.execute(
            "UPDATE ai_rentals SET status='expired', updated_at=? WHERE id=? AND status='active'",
            (now, order_id),
        )
        if cur.rowcount == 0:
            return None  # гонка — кто-то параллельно уже обработал
        await db.commit()
    return order


async def get_active_ai_rentals_admin() -> list[dict]:
    """Активные аренды для админки."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.id, r.user_id, r.username, r.amount_tenge, r.paid_bonus, r.paid_main, "
            "       r.starts_at, r.expires_at, "
            "       rs.name AS service_name, rt.name AS tariff_name, a.email "
            "FROM ai_rentals r "
            "JOIN rental_services rs ON rs.id = r.service_id "
            "JOIN rental_tariffs rt ON rt.id = r.tariff_id "
            "JOIN ai_accounts a ON a.id = r.account_id "
            "WHERE r.status='active' ORDER BY r.expires_at"
        )
        return [dict(r) for r in await cur.fetchall()]


# ── OTP-коды (принимаются с Cloudflare Worker) ───────────────────────

async def insert_otp_code(email: str, code: str):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO otp_incoming_codes(recipient_email,otp_code,created_at) VALUES(?,?,?)",
            (email, code, _now()),
        )
        await db.commit()


async def get_recent_otp(email: str, window_sec: int = 120) -> Optional[str]:
    from datetime import timedelta
    threshold = (datetime.utcnow() - timedelta(seconds=window_sec)).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT otp_code FROM otp_incoming_codes "
            "WHERE recipient_email=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (email, threshold),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def get_otp_logs(limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM otp_incoming_codes ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]


# ── Magic-link (Claude и т.п.) — отдаём ссылку юзеру вместо решения капчи ─

MAGIC_LINK_TTL_SEC = 900  # оценка (Claude не документирует точный TTL) — 15 минут


async def insert_magic_link(email: str, link: str):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO otp_incoming_links(recipient_email,magic_link,created_at) VALUES(?,?,?)",
            (email, link, _now()),
        )
        await db.commit()


async def get_recent_magic_link(email: str, window_sec: int = MAGIC_LINK_TTL_SEC) -> Optional[dict]:
    from datetime import timedelta
    threshold = (datetime.utcnow() - timedelta(seconds=window_sec)).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT magic_link, created_at FROM otp_incoming_links "
            "WHERE recipient_email=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (email, threshold),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Прокси/cooldown — используются воркером ai_rental_manager ────────

async def get_ai_proxy(proxy_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM ai_proxies WHERE id=?", (proxy_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_cooldown_accounts_due(threshold_iso: str) -> list[dict]:
    """Аккаунты, которые дольше COOLDOWN_MIN сидят в cooldown — готовы вернуться
    в available (updated_at выставляется при каждом переходе статуса, так что
    отсчёт идёт именно с момента входа в cooldown)."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ai_accounts WHERE status='cooldown' AND updated_at<=?",
            (threshold_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]
