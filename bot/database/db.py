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

            CREATE TABLE IF NOT EXISTS banned_usernames (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
                reason      TEXT,
                added_at    TEXT NOT NULL
            );

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
    currency: str = "tenge",    # 'tenge' | 'token'
    order_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    allow_overdraft: bool = True,
) -> float:
    """Изменить баланс И записать строку в ledger в одной БД-транзакции.

    Возвращает новый баланс. При idempotency_key, который уже встречался,
    операция НЕ повторяется — возвращается ранее сохранённый balance_after.
    При allow_overdraft=False и нехватке средств бросает InsufficientFunds.
    """
    col = "tenge_balance" if currency == "tenge" else "token_balance"
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
) -> dict:
    """Атомарно: списать тенге + создать заказ (queued) + записать в ledger.

    Всё в одной БД-транзакции — деньги не могут списаться без заказа и наоборот.
    Возвращает {ok, duplicate, order_id, balance}.
      • duplicate=True  — этот idempotency_key уже проводился, повторно НЕ списываем,
        возвращаем ранее созданный order_id (защита от двойного тапа, п.8.1 ТЗ).
      • InsufficientFunds — если баланса не хватило.
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
                return {"ok": True, "duplicate": True,
                        "order_id": row[0], "balance": float(row[1])}

        # 2. Атомарное списание (защита от гонок и ухода в минус)
        cur = await db.execute(
            "UPDATE users SET tenge_balance=tenge_balance-? "
            "WHERE tg_id=? AND tenge_balance>=?",
            (price, user_id, price),
        )
        if cur.rowcount == 0:
            await db.rollback()
            raise InsufficientFunds()

        cur = await db.execute(
            "SELECT tenge_balance FROM users WHERE tg_id=?", (user_id,)
        )
        balance = float((await cur.fetchone())[0])

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

        # 4. Журналируем списание (привязка к заказу)
        try:
            await db.execute(
                _tx_insert_sql(),
                (user_id, order_id, "debit", "order_charge", "tenge",
                 price, balance, idempotency_key, now),
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
                return {"ok": True, "duplicate": True,
                        "order_id": row[0], "balance": float(row[1])}
            raise

        return {"ok": True, "duplicate": False,
                "order_id": order_id, "balance": balance}


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


async def cancel_order_with_refund(order_id: int, reason: str = "") -> dict:
    """Отменить заказ и вернуть деньги на тенге-баланс пользователя.

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
        await add_tenge(
            order["user_id"], amount,
            reason="order_refund", order_id=order_id,
            idempotency_key=f"refund:{order_id}",
        )
        refunded = amount

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
) -> int:
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO payments
               (user_id, payment_type, purpose, amount_tenge, amount_stars,
                tokens_amount, order_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,'pending',?)""",
            (user_id, payment_type, purpose, amount_tenge, amount_stars,
             tokens_amount, order_id, _now()),
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
        cur = await db.execute("SELECT tg_id, tenge_balance, token_balance FROM users")
        users = [dict(r) for r in await cur.fetchall()]

        for u in users:
            for currency, col in (("tenge", "tenge_balance"), ("token", "token_balance")):
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

        cur = await db.execute(
            "SELECT payment_type, COUNT(*), SUM(amount_tenge) FROM payments "
            "WHERE status='confirmed' AND purpose='turnitin' GROUP BY payment_type"
        )
        turnitin_revenue = {r[0]: {"count": r[1], "tenge": r[2] or 0} for r in await cur.fetchall()}

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
) -> dict:
    """Атомарно: списать тенге + захватить свободный аккаунт + создать аренду + ledger.

    Всё в одной БД-транзакции. Ключ идемпотентности: rental:<tg_id>:<request_id>.
    Бросает InsufficientFunds / NoFreeAccount.
    Возвращает {ok, duplicate, order_id, service_name, tariff_name,
                expires_at, login, password, balance}.
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
                return {"ok": True, "duplicate": True,
                        "balance": float(row[1]), **payload}

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
        cur = await db.execute(
            "UPDATE users SET tenge_balance=tenge_balance-? "
            "WHERE tg_id=? AND tenge_balance>=?",
            (price, user_id, price),
        )
        if cur.rowcount == 0:
            await db.rollback()
            raise InsufficientFunds()

        cur = await db.execute(
            "SELECT tenge_balance FROM users WHERE tg_id=?", (user_id,)
        )
        balance = float((await cur.fetchone())[0])

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

        # 6. Журналируем списание
        try:
            await db.execute(
                _tx_insert_sql(),
                (user_id, order_id, "debit", "rental_charge", "tenge",
                 price, balance, idempotency_key, now),
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
                return {"ok": True, "duplicate": True,
                        "balance": float(row[1]), **payload}
            raise

        payload = await _rental_order_payload(db, order_id) or {}
        return {"ok": True, "duplicate": False, "balance": balance, **payload}


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
        await add_tenge(
            order["user_id"], amount,
            reason="rental_refund", order_id=order_id,
            idempotency_key=f"rental_refund:{order_id}",
        )
        refunded = amount

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
    """Все сервисы (включая выключенные) с тарифами и счётчиками склада."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT s.*, "
            " (SELECT COUNT(*) FROM rental_accounts a WHERE a.service_id=s.id AND a.status='free') AS cnt_free, "
            " (SELECT COUNT(*) FROM rental_accounts a WHERE a.service_id=s.id AND a.status='rented') AS cnt_rented, "
            " (SELECT COUNT(*) FROM rental_accounts a WHERE a.service_id=s.id AND a.status='maintenance') AS cnt_maintenance, "
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
