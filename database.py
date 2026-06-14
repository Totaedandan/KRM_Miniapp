"""
База данных:
  orders       — заказы
  receipts     — ID чеков Kaspi (для защиты от дублей)
  settings     — динамические настройки (цены, тексты, логин Turnitin)
"""

import aiosqlite
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id          INTEGER NOT NULL,
                    username         TEXT,
                    report_type      TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'pending',
                    payment_method   TEXT DEFAULT 'stars',
                    stars_payment_id TEXT,
                    receipt_id       TEXT,
                    file_name        TEXT,
                    file_path        TEXT,
                    submission_id    TEXT,
                    report_path_sim  TEXT,
                    report_path_ai   TEXT,
                    error_text       TEXT,
                    created_at       TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS receipts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT UNIQUE NOT NULL,
                    user_id    INTEGER,
                    order_id   INTEGER,
                    added_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Значения по умолчанию
            defaults = {
                "price_ai":         "700",
                "price_similarity":  "700",
                "price_both":       "1200",
                "price_currency":   "тг",
                "turnitin_email":   "kurmankhan_a0605@fmalm.nis.edu.kz",
                "turnitin_password": "Slspp224rfd!",
                "turnitin_class_id": "",
                "turnitin_assign_id": "",
                "help_username":    "@awata_krm",
                "help_phone":       "87771113003",
                "qr_caption":       "Отправьте оплату по QR-коду выше и пришлите скриншот чека.",
            }
            for k, v in defaults.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
                )
            await db.commit()
        logger.info("DB ready: %s", self.path)

    # ─── Settings ────────────────────────────────────────────────

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_prices(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('price_ai','price_similarity','price_both','price_currency')"
            )
            rows = await cur.fetchall()
            return {r[0]: r[1] for r in rows}

    # ─── Receipts ─────────────────────────────────────────────────

    async def receipt_exists(self, receipt_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM receipts WHERE receipt_id=?", (receipt_id,)
            )
            return await cur.fetchone() is not None

    async def add_receipt(self, receipt_id: str, user_id: int, order_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO receipts (receipt_id, user_id, order_id, added_at) "
                "VALUES (?,?,?,?)",
                (receipt_id, user_id, order_id, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def delete_receipt(self, receipt_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM receipts WHERE receipt_id=?", (receipt_id,)
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_receipts(self, limit: int = 50) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM receipts ORDER BY added_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cur.fetchall()]

    # ─── Orders ──────────────────────────────────────────────────

    async def create_order(
        self,
        user_id: int,
        username: Optional[str],
        report_type: str,
        payment_method: str = "stars",
    ) -> int:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO orders
                   (user_id, username, report_type, status, payment_method, created_at, updated_at)
                   VALUES (?,?,?,'pending',?,?,?)""",
                (user_id, username, report_type, payment_method, now, now),
            )
            await db.commit()
            return cur.lastrowid

    async def get_order(self, order_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_order(self, order_id: int, **kwargs):
        kwargs["updated_at"] = datetime.utcnow().isoformat()
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [order_id]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE orders SET {cols} WHERE id=?", vals)
            await db.commit()

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT status, COUNT(*) FROM orders GROUP BY status"
            )
            rows = await cur.fetchall()
            counts = {r[0]: r[1] for r in rows}

            cur2 = await db.execute(
                "SELECT report_type, COUNT(*) FROM orders WHERE status='done' GROUP BY report_type"
            )
            by_type = {r[0]: r[1] for r in await cur2.fetchall()}

            cur3 = await db.execute("SELECT COUNT(*) FROM receipts")
            receipt_count = (await cur3.fetchone())[0]

        return {
            "by_status": counts,
            "by_type": by_type,
            "total_receipts": receipt_count,
            "total_done": counts.get("done", 0),
        }
