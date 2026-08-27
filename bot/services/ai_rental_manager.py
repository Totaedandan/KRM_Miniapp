"""
Воркер аренды ИИ-аккаунтов v2 (email+OTP, авто-разлогин, прокси-группы).

Каждый тик (60 c):
  1. Напоминания за ~30 мин до истечения (ai_rentals.reminder_sent=0) — как у
     старого rental_manager.
  2. Истёкшие аренды: ai_rentals.status → expired сразу (доступ юзера в Mini
     App закрывается логически), затем задача авто-разлогина ставится в
     очередь по прокси аккаунта (не блокирует тик — asyncio.create_task).
  3. Аккаунты, отсидевшие COOLDOWN_MIN в cooldown после разлогина, → available
     → notify_waitlist (переиспользуем как есть).

service_type для автоматизации берём из rental_services.icon (там уже лежит
бренд-ключ вроде 'openai'/'claude' — см. CLAUDE.md, раздел про RentIcon).
Сервисы вне {chatgpt, claude} авто-разлогин не поддерживают в v1 — аккаунт
уходит в maintenance, админ закрывает сессию вручную (см. ai_rental_service.py).

Очередь на прокси: без Redis/Celery (в проекте их нет, см. queue_manager.py —
тот же голый asyncio) — dict[proxy_id, asyncio.Lock] + dict[proxy_id, float]
времени последнего запуска, пауза PROXY_GAP_SEC между задачами на одном IP,
чтобы не долбить один прокси параллельными разлогинами разных аккаунтов.

Восстановление после рестарта — как у старого rental_manager: всё состояние в
БД, первый тик подметает всё, что скопилось за время простоя (в т.ч. аккаунты,
которые не успели доразлогиниться до перезапуска, — level статус 'rented' у
истёкшей аренды уже не важен, expire_ai_rental сам разберётся по ai_rentals).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from database import db
from bot_sender import send_message
from services import ai_rental_service
from services.ai_rental_service import LOGOUT_GRACE_SEC

logger = logging.getLogger(__name__)

TICK_SEC = 60
REMINDER_MIN = 30
COOLDOWN_MIN = 5
PROXY_GAP_SEC = 20

ICON_TO_SERVICE_TYPE = {"openai": "chatgpt", "claude": "claude"}


def _fmt_expires(expires_at: str) -> str:
    """'2026-06-10T14:30:00' → '14:30 10.06 (UTC)'."""
    try:
        dt = datetime.fromisoformat(expires_at)
        return dt.strftime("%H:%M %d.%m (UTC)")
    except ValueError:
        return expires_at


class AiRentalManager:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._proxy_locks: dict[int, asyncio.Lock] = {}
        self._proxy_last_run: dict[int, float] = {}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="ai_rental_worker")
            logger.info("AI rental worker started (tick %ss)", TICK_SEC)

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error("ai_rental tick error: %s", e)
            await asyncio.sleep(TICK_SEC)

    async def _tick(self):
        now = datetime.utcnow()
        now_iso = now.isoformat()

        # 1. Напоминания
        threshold = (now + timedelta(minutes=REMINDER_MIN)).isoformat()
        for r in await db.get_ai_reminder_due_rentals(threshold, now_iso):
            await db.mark_ai_rental_reminder_sent(r["id"])
            await send_message(
                r["user_id"],
                f"⏳ Аренда <b>{r['service_name']}</b> истекает через ~{REMINDER_MIN} минут "
                f"(до {_fmt_expires(r['expires_at'])}).\n"
                f"Успейте сохранить нужное — после окончания доступ закроется.",
            )

        # 2. Истёкшие — статус закрываем сразу, разлогин ставим в очередь по прокси
        for r in await db.get_due_ai_rentals(now_iso):
            expired = await db.expire_ai_rental(r["id"])
            if not expired:
                continue  # гонка — параллельно уже обработали (отмена админом и т.п.)
            logger.info("ai_rental #%s expired (service %s, user %s, account %s)",
                        r["id"], r["service_id"], r["user_id"], r["account_id"])
            await send_message(
                r["user_id"],
                f"⌛ Аренда <b>{r['service_name']}</b> завершена. "
                f"Спасибо, что пользуетесь сервисом!\n"
                f"Арендовать снова можно в приложении.",
            )
            asyncio.create_task(self._delayed_logout(r["account_id"]))

        # 3. Cooldown → available
        cooldown_threshold = (now - timedelta(minutes=COOLDOWN_MIN)).isoformat()
        for acc in await db.get_cooldown_accounts_due(cooldown_threshold):
            await db.update_ai_account_status(acc["id"], "available")
            logger.info("ai_account #%s cooldown → available", acc["id"])
            await self.notify_waitlist(acc["service_id"])

    async def _delayed_logout(self, account_id: int):
        """Пауза перед логаутом истёкшей аренды — снижает шанс столкнуться с
        арендатором, который в этот же момент ещё запрашивает код. Ручной
        force_logout/отмена админом идут напрямую в logout_account(), без паузы."""
        await asyncio.sleep(LOGOUT_GRACE_SEC)
        await self.logout_account(account_id)

    async def logout_account(self, account_id: int):
        """Запускает авто-разлогин с учётом очереди на прокси аккаунта, затем
        переводит аккаунт в cooldown (успех) или maintenance (неудача/не
        поддерживается — ждёт ручного разбора админом). Публичный метод — его
        также дёргают напрямую из api.py (отмена админом, force_logout)."""
        account = await db.get_ai_account(account_id)
        if not account:
            return
        svc = await db.get_rental_service(account["service_id"])
        service_type = ICON_TO_SERVICE_TYPE.get((svc or {}).get("icon", ""))

        proxy_id = account.get("proxy_id")
        proxy_url = None
        if proxy_id:
            proxy = await db.get_ai_proxy(proxy_id)
            proxy_url = proxy["proxy_url"] if proxy else None

            lock = self._proxy_locks.setdefault(proxy_id, asyncio.Lock())
            async with lock:
                gap = time.monotonic() - self._proxy_last_run.get(proxy_id, 0)
                if gap < PROXY_GAP_SEC:
                    await asyncio.sleep(PROXY_GAP_SEC - gap)
                ok = await self._run_logout(account, service_type, proxy_url)
                self._proxy_last_run[proxy_id] = time.monotonic()
        else:
            ok = await self._run_logout(account, service_type, proxy_url)

        if ok:
            await db.update_ai_account_status(account_id, "cooldown")
        else:
            await db.update_ai_account_status(account_id, "maintenance")
            await self._alert_admin(account, service_type)

    async def _run_logout(self, account: dict, service_type: str | None, proxy_url: str | None) -> bool:
        if not service_type:
            logger.warning(
                "ai_account #%s: авто-разлогин не поддерживается для этого сервиса "
                "(icon=%r) — нужен ручной разбор", account["id"], account.get("service_id"))
            return False
        try:
            return await ai_rental_service.auto_logout(account, service_type, proxy_url)
        except Exception as e:
            logger.error("auto_logout crashed for account #%s: %s", account["id"], e, exc_info=True)
            return False

    async def _alert_admin(self, account: dict, service_type: str | None):
        from config import settings
        reason = "сервис не поддерживает авто-разлогин" if not service_type else "попытка разлогина не удалась"
        text = (
            f"⚠️ Аккаунт <code>{account.get('email')}</code> (id {account['id']}) переведён в "
            f"maintenance: {reason}.\nНужна ручная проверка/разлогин."
        )
        for admin_id in settings.admin_id_list:
            await send_message(admin_id, text)

    async def notify_waitlist(self, service_id: int):
        """Аккаунт сервиса стал свободен — уведомить всех ожидающих (кто успел)."""
        users = await db.pop_waitlist(service_id)
        if not users:
            return
        svc = await db.get_rental_service(service_id)
        name = svc["name"] if svc else "Сервис"
        logger.info("ai waitlist notify: service %s → %s users", service_id, len(users))
        for uid in users:
            await send_message(
                uid,
                f"🔔 <b>{name}</b> снова в наличии!\n"
                f"Аккаунтов мало — забронируйте в приложении, пока не разобрали.",
            )


ai_rental_manager = AiRentalManager()
