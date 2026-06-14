"""
Воркер аренды ИИ-аккаунтов.

Каждый тик (60 c):
  1. Напоминания за ~30 мин до истечения аренды (reminder_sent=0).
  2. Истёкшие аренды: order → expired, аккаунт → maintenance (пароль
     скомпрометирован — в пул вернёт админ после ротации).

Waitlist уведомляется НЕ здесь, а при переходе аккаунта в `free`
(админ сменил пароль или добавил новый аккаунт) — см. notify_waitlist(),
вызывается из admin-эндпоинтов api.py.

Восстановление после рестарта автоматическое: состояние целиком в БД,
первый тик подметает всё просроченное за время простоя.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from database import db
from bot_sender import send_message

logger = logging.getLogger(__name__)

TICK_SEC = 60
REMINDER_MIN = 30


def _fmt_expires(expires_at: str) -> str:
    """'2026-06-10T14:30:00' → '14:30 10.06 (UTC)'."""
    try:
        dt = datetime.fromisoformat(expires_at)
        return dt.strftime("%H:%M %d.%m (UTC)")
    except ValueError:
        return expires_at


class RentalManager:
    def __init__(self):
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="rental_worker")
            logger.info("Rental worker started (tick %ss)", TICK_SEC)

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error("rental tick error: %s", e)
            await asyncio.sleep(TICK_SEC)

    async def _tick(self):
        now = datetime.utcnow()
        now_iso = now.isoformat()

        # 1. Напоминания
        threshold = (now + timedelta(minutes=REMINDER_MIN)).isoformat()
        for r in await db.get_reminder_due_rentals(threshold, now_iso):
            # Сначала помечаем, потом шлём: лучше потерять одно напоминание,
            # чем спамить каждый тик при недоступном Telegram.
            await db.mark_rental_reminder_sent(r["id"])
            await send_message(
                r["user_id"],
                f"⏳ Аренда <b>{r['service_name']}</b> истекает через ~{REMINDER_MIN} минут "
                f"(до {_fmt_expires(r['expires_at'])}).\n"
                f"Успейте сохранить нужное — после окончания доступ закроется.",
            )

        # 2. Истёкшие
        for r in await db.get_due_rentals(now_iso):
            expired = await db.expire_rental(r["id"])
            if not expired:
                continue  # параллельно отменили/уже истёк
            logger.info("rental #%s expired (service %s, user %s)",
                        r["id"], r["service_id"], r["user_id"])
            await send_message(
                r["user_id"],
                f"⌛ Аренда <b>{r['service_name']}</b> завершена. "
                f"Спасибо, что пользуетесь сервисом!\n"
                f"Арендовать снова можно в приложении.",
            )

    async def notify_waitlist(self, service_id: int):
        """Аккаунт сервиса стал свободен — уведомить всех ожидающих (кто успел)."""
        users = await db.pop_waitlist(service_id)
        if not users:
            return
        svc = await db.get_rental_service(service_id)
        name = svc["name"] if svc else "Сервис"
        logger.info("waitlist notify: service %s → %s users", service_id, len(users))
        for uid in users:
            await send_message(
                uid,
                f"🔔 <b>{name}</b> снова в наличии!\n"
                f"Аккаунтов мало — забронируйте в приложении, пока не разобрали.",
            )


rental_manager = RentalManager()
