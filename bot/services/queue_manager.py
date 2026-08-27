"""
Очередь заказов Turnitin — две очереди (обычная и премиум) с приоритетом.

Модель (источник правды — БД, статусы turnitin_orders.status):
  queued        — место забронировано, деньги списаны, файл ещё не запрошен
  awaiting_file — бот попросил файл, идёт таймер 3 минуты
  ready         — файл получен, ждёт обработки
  processing    — обрабатывается Playwright-ом
  done/error/cancelled — финальные

Логика (по схеме клиента):
  • Премиум (×1.5): место бронируется сразу, файл запрашивается немедленно,
    обработка с приоритетом (раньше обычных).
  • Обычная (×1): пользователь ждёт; файл запрашивается, когда подходит очередь
    («остался 1 человек перед тобой») — на отправку даётся 3 минуты.
  • Не успел отправить файл за 3 минуты → возврат денег + потеря места.
  • Везде показывается число людей в очереди и ETA (1 файл = 7 минут).

Воркер держит два "слота" обработки: основной (общий персистентный браузер
turnitin_service, им пользуются обе очереди, когда он свободен) и overflow
(временный отдельный TurnitinService() — поднимается ТОЛЬКО если премиум
пришёл, пока основной уже занят обычным заказом, чтобы премиум не ждал за
обычным; закрывается сразу после того заказа). Так основную часть времени
работает один браузер (экономия RAM), а второй появляется лишь при реальном
пересечении очередей. Одновременно overflow-слот только один.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

FILE_TIMEOUT_SEC = 180     # 3 минуты на отправку файла
MINUTES_PER_FILE = 7       # оценка времени обработки одного файла
MAX_PROCESS_RETRIES = 3    # макс. авто-повторов обработки (после сбоев/перезапусков)
# Жёсткий потолок на весь process() (включая внутренние ретраи и 30-минутное
# ожидание отчёта) — без него зависший без исключения await (например,
# frame.evaluate() на отсоединившемся LTI-фрейме — нет своего таймаута) вешает
# воркер НАВСЕГДА: _worker() обрабатывает заказы строго по одному, так что один
# зависший заказ блокирует и обычную, и премиум очередь целиком, независимо от
# того, что у них разные class_id/assignment_id в Turnitin.
PROCESS_TIMEOUT_SEC = 3600  # 60 минут — с запасом даже на полный цикл ретраев


@dataclass
class TurnitinJob:
    """Оставлено для обратной совместимости (новый флоу хранит всё в БД)."""
    order_id: int
    tg_id: int
    file_path: str
    report_type: str
    is_premium: bool = False


class TurnitinQueueManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._timers: dict[int, asyncio.Task] = {}   # order_id -> timeout task
        self._wakeup = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None
        self.bot = None
        self.settings = None
        # id заказа, занимающего основной/overflow слот прямо сейчас (или None)
        self._main_busy_order: Optional[int] = None
        self._overflow_busy_order: Optional[int] = None

    # ── Запуск ────────────────────────────────────────────────────────────────

    def start(self, bot, settings):
        """Запустить воркер. Вызывать один раз при старте бота."""
        self.bot = bot
        self.settings = settings
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="turnitin_worker")
            logger.info("TurnitinQueue worker started")
        asyncio.create_task(self._startup_recover())

    async def _startup_recover(self):
        """После рестарта/краха: возобновить обработку очереди по списку.

        - processing → ready: обработка прервалась крахом, повторяем;
        - awaiting_file → queued: таймеры потеряны, попросим файл заново;
        - error (файл на месте, повторов < лимита) → ready: заказ упал из-за
          сбоя инфраструктуры — возобновляем, пока не исчерпан лимит повторов.
        """
        from database import db as database
        import os
        try:
            for o in await database.get_orders_by_status(("processing",)):
                await database.update_order(o["id"], status="ready")
                logger.info("Recover: order %s processing→ready", o["id"])

            for o in await database.get_orders_by_status(("awaiting_file",)):
                await database.update_order(o["id"], status="queued")

            for o in await database.get_orders_by_status(("error",)):
                fp = o.get("file_path")
                rc = o.get("retry_count") or 0
                if fp and os.path.exists(fp) and rc < MAX_PROCESS_RETRIES:
                    await database.update_order(o["id"], status="ready",
                                                retry_count=rc + 1, error_text=None)
                    logger.info("Recover: resuming errored order %s (retry %d/%d)",
                                o["id"], rc + 1, MAX_PROCESS_RETRIES)

            await self._reevaluate()
            self._wakeup.set()
        except Exception as e:
            logger.error("startup_recover error: %s", e)

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def on_reservation(self, order_id: int):
        """Вызывать после того, как заказ переведён в статус 'queued' (оплачен)."""
        await self._reevaluate()
        # Если заказ так и остался ждать — сообщим позицию в очереди
        from database import db as database
        order = await database.get_order(order_id)
        if order and order["status"] == "queued":
            ahead, eta, total = await self._position(order_id)
            await self._send(order["user_id"],
                f"📋 <b>Заказ #{order_id} — вы в очереди</b>\n\n"
                f"Перед вами: <b>{ahead}</b> · ориентировочно <b>~{eta} мин</b>\n"
                f"Когда подойдёт очередь — пришлём запрос на файл 🔔"
            )

    async def on_file_received(self, order_id: int):
        """Вызывать после того, как файл сохранён и заказ переведён в 'ready'."""
        self._cancel_timer(order_id)
        self._wakeup.set()
        await self._reevaluate()

    async def cancel_order(self, order_id: int, reason: str = "", notify: bool = True) -> dict:
        """Отменить заказ с возвратом денег. Используется админом и по таймауту."""
        from database import db as database
        self._cancel_timer(order_id)
        result = await database.cancel_order_with_refund(order_id, reason=reason)
        if result.get("ok"):
            await database.clear_pending_action(result["user_id"])
            if notify:
                refunded = result.get("refunded", 0)
                refund_note = (f"\n💰 Возвращено на баланс: <b>{refunded:.0f} ₸</b>"
                               if refunded else "")
                await self._send(result["user_id"],
                    f"🚫 <b>Заказ #{order_id} отменён.</b>{refund_note}"
                )
        await self._reevaluate()
        return result

    async def queue_snapshot(self) -> dict:
        """Срез очередей для админки: premium[], regular[] с позицией и ETA."""
        from database import db as database
        active = await database.get_active_orders()
        premium, regular = [], []
        for idx, o in enumerate(active):
            row = {
                "id": o["id"],
                "user_id": o["user_id"],
                "username": o.get("username"),
                "report_type": o["report_type"],
                "status": o["status"],
                "amount_tenge": o.get("amount_tenge") or 0,
                "is_premium": bool(o.get("is_premium")),
                "position": idx + 1,
                "eta_min": idx * MINUTES_PER_FILE,
                "created_at": o.get("created_at"),
            }
            (premium if row["is_premium"] else regular).append(row)
        return {
            "premium": premium,
            "regular": regular,
            "total": len(active),
            "eta_total_min": len(active) * MINUTES_PER_FILE,
        }

    # ── Внутреннее: переоценка очереди ──────────────────────────────────────────

    async def _reevaluate(self):
        """Решить, у кого запросить файл. Под локом, без долгих операций."""
        from database import db as database
        async with self._lock:
            active = await database.get_active_orders()

            # 1. Все премиум 'queued' — просим файл немедленно
            for o in active:
                if o.get("is_premium") and o["status"] == "queued":
                    await self._request_file(o, premium=True)

            # 2. Обычная очередь — держим ровно один заказ «на подаче»
            regular = [o for o in active if not o.get("is_premium")]
            has_on_deck = any(o["status"] in ("awaiting_file", "ready") for o in regular)
            if not has_on_deck:
                front = next((o for o in regular if o["status"] == "queued"), None)
                if front:
                    await self._request_file(front, premium=False)

        # Если появились готовые заказы — разбудить воркер
        if any(o["status"] == "ready" for o in await database.get_active_orders()):
            self._wakeup.set()

    async def _request_file(self, order: dict, premium: bool):
        """Перевести заказ в awaiting_file, запустить таймер и попросить файл."""
        from database import db as database
        order_id = order["id"]
        await database.update_order(order_id, status="awaiting_file")
        await database.set_pending_action(
            order["user_id"], "waiting_file",
            {"order_id": order_id, "report_type": order["report_type"],
             "is_premium": premium},
        )
        self._start_timer(order_id)

        label = TYPE_LABEL.get(order["report_type"], order["report_type"])
        lang_note = ("\n⚠️ Для AI-детекции текст должен быть на <b>английском</b>."
                     if order["report_type"] in ("ai", "both") else "")
        prefix = "⚡ <b>Премиум-заказ" if premium else "🚀 <b>Ваша очередь подошла! Заказ"
        await self._send(order["user_id"],
            f"{prefix} #{order_id}</b>\n\n"
            f"Тип: <b>{label}</b>{lang_note}\n\n"
            f"📎 Отправьте файл в <b>этот чат</b> в течение <b>3 минут</b>,\n"
            f"иначе место в очереди освободится и деньги вернутся на баланс.\n\n"
            f"Форматы: .pdf · .docx · .doc · .txt · .rtf"
        )

    # ── Таймеры ────────────────────────────────────────────────────────────────

    def _start_timer(self, order_id: int):
        self._cancel_timer(order_id)
        self._timers[order_id] = asyncio.create_task(self._expire_after(order_id))

    def _cancel_timer(self, order_id: int):
        task = self._timers.pop(order_id, None)
        if task and not task.done():
            task.cancel()

    async def _expire_after(self, order_id: int):
        from database import db as database
        try:
            await asyncio.sleep(FILE_TIMEOUT_SEC)
        except asyncio.CancelledError:
            return
        self._timers.pop(order_id, None)
        order = await database.get_order(order_id)
        if not order or order["status"] != "awaiting_file":
            return  # файл уже пришёл или заказ отменён
        logger.info("Order %s: file timeout, refunding", order_id)
        result = await database.cancel_order_with_refund(order_id, reason="file_timeout")
        await database.clear_pending_action(order["user_id"])
        refunded = result.get("refunded", 0) if result.get("ok") else 0
        refund_note = (f"\n💰 Возвращено на баланс: <b>{refunded:.0f} ₸</b>"
                       if refunded else "")
        await self._send(order["user_id"],
            f"⌛ <b>Время вышло — вы не отправили файл.</b>\n"
            f"Заказ #{order_id} отменён, место в очереди освобождено.{refund_note}\n\n"
            f"Создайте заказ заново, когда будете готовы."
        )
        await self._reevaluate()

    # ── Воркер обработки ────────────────────────────────────────────────────────
    #
    # Два "слота": основной (общий персистентный браузер turnitin_service —
    # использует и премиум, и обычная очередь, когда браузер свободен) и
    # overflow (временный отдельный TurnitinService(), поднимается только
    # когда премиум-заказ пришёл, а основной уже занят обычным — премиум не
    # должен ждать за обычным). Не блокирующий: сами заказы выполняются как
    # фоновые задачи (create_task), а диспетчер в цикле проверяет БД и раздаёт
    # слоты — так он не "зависает" внутри await на время обработки одного
    # заказа и успевает заметить премиум, пришедший, пока обычный ещё крутится.
    #
    # Одновременно overflow-слот только один — если премиум-заказов пришло
    # больше, чем свободных слотов, лишние просто ждут своей очереди как раньше.

    async def _worker(self):
        from database import db as database
        logger.info("TurnitinQueue: worker loop started")
        while True:
            dispatched = False
            async with self._lock:
                active = await database.get_active_orders()
                for job in (o for o in active if o["status"] == "ready"):
                    is_premium = bool(job.get("is_premium"))
                    if self._main_busy_order is None:
                        self._main_busy_order = job["id"]
                        await database.update_order(job["id"], status="processing")
                        asyncio.create_task(self._run_turnitin(job, overflow=False))
                        dispatched = True
                        break
                    if is_premium and self._overflow_busy_order is None:
                        self._overflow_busy_order = job["id"]
                        await database.update_order(job["id"], status="processing")
                        logger.info("Order %s: премиум мимо очереди — отдельный overflow-браузер "
                                    "(основной занят заказом #%s)", job["id"], self._main_busy_order)
                        asyncio.create_task(self._run_turnitin(job, overflow=True))
                        dispatched = True
                        break

            if not dispatched:
                self._wakeup.clear()
                await self._wakeup.wait()

    async def _run_turnitin(self, order: dict, overflow: bool = False):
        from services.turnitin_service import turnitin_service, TurnitinService
        from database import db as database
        from aiogram.types import FSInputFile

        overflow_service = TurnitinService() if overflow else None
        service = overflow_service or turnitin_service

        order_id    = order["id"]
        tg_id       = order["user_id"]
        file_path   = order["file_path"]
        report_type = order["report_type"]
        is_premium  = bool(order.get("is_premium"))
        s = self.settings

        try:
            email    = await database.get_setting("turnitin_email")    or s.TURNITIN_EMAIL
            password = await database.get_setting("turnitin_password") or s.TURNITIN_PASSWORD
            if is_premium:
                class_id  = (await database.get_setting("turnitin_class_id_premium")
                             or await database.get_setting("turnitin_class_id") or s.TURNITIN_CLASS_ID)
                assign_id = (await database.get_setting("turnitin_assign_id_premium")
                             or await database.get_setting("turnitin_assign_id") or s.TURNITIN_ASSIGNMENT_ID)
            else:
                class_id  = await database.get_setting("turnitin_class_id")  or s.TURNITIN_CLASS_ID
                assign_id = await database.get_setting("turnitin_assign_id") or s.TURNITIN_ASSIGNMENT_ID

            os.makedirs(s.REPORTS_DIR, exist_ok=True)

            await self._send(tg_id,
                f"🚀 <b>Начинаем проверку!</b> Заказ #{order_id}\n"
                "Отчёт придёт через 5–30 минут ⏱"
            )

            sim_path, ai_path = await asyncio.wait_for(
                service.process(
                    order_id=order_id,
                    file_path=file_path,
                    file_name=os.path.basename(file_path),
                    report_type=report_type,
                    email=email,
                    password=password,
                    class_id=class_id,
                    assign_id=assign_id,
                    reports_dir=s.REPORTS_DIR,
                ),
                timeout=PROCESS_TIMEOUT_SEC,
            )

            await database.update_order(
                order_id, status="done",
                report_path_sim=sim_path, report_path_ai=ai_path,
            )
            await self.bot.send_message(tg_id, f"✅ <b>Отчёт готов!</b> Заказ #{order_id}")

            if sim_path and os.path.exists(sim_path):
                await self.bot.send_document(
                    tg_id, FSInputFile(sim_path, filename=f"similarity_{order_id}.pdf"),
                    caption="📊 Отчёт на плагиат",
                )
            if ai_path and os.path.exists(ai_path):
                await self.bot.send_document(
                    tg_id, FSInputFile(ai_path, filename=f"ai_detection_{order_id}.pdf"),
                    caption="🤖 Отчёт AI-детекции",
                )
            try:
                os.remove(file_path)
            except Exception:
                pass

        except asyncio.TimeoutError:
            # asyncio.wait_for отменил задачу — сама TimeoutError() приходит без
            # текста, поэтому формируем сообщение явно, иначе error_text пустой.
            logger.error("Turnitin processing timed out for order %s after %ds",
                         order_id, PROCESS_TIMEOUT_SEC)
            await database.update_order(
                order_id, status="error",
                error_text=f"Обработка зависла (>{PROCESS_TIMEOUT_SEC // 60} мин) и была прервана",
            )
            support = await database.get_setting("help_username") or "@support"
            await self._send(tg_id,
                f"😔 <b>Не удалось получить отчёт.</b>\n"
                f"Свяжитесь с нами: {support}\nНомер заказа: <b>#{order_id}</b>",
            )
        except Exception as e:
            logger.error("Turnitin error order %s: %s", order_id, e, exc_info=True)
            await database.update_order(order_id, status="error", error_text=str(e)[:500])
            support = await database.get_setting("help_username") or "@support"
            await self._send(tg_id,
                f"😔 <b>Не удалось получить отчёт.</b>\n"
                f"Свяжитесь с нами: {support}\nНомер заказа: <b>#{order_id}</b>",
            )
        finally:
            if overflow_service is not None:
                # Временный overflow-браузер — закрываем сразу, он больше не нужен
                # (основной turnitin_service остаётся жить между заказами как обычно).
                try:
                    await overflow_service.cleanup()
                except Exception as e:
                    logger.warning("overflow browser cleanup error (order %s): %s", order_id, e)
                self._overflow_busy_order = None
            else:
                self._main_busy_order = None
            self._wakeup.set()
            await self._reevaluate()

    # ── Вспомогательное ─────────────────────────────────────────────────────────

    async def _position(self, order_id: int) -> tuple[int, int, int]:
        """(сколько впереди, ETA минут, всего активных) для заказа."""
        from database import db as database
        active = await database.get_active_orders()
        for idx, o in enumerate(active):
            if o["id"] == order_id:
                return idx, idx * MINUTES_PER_FILE, len(active)
        return 0, 0, len(active)

    async def _send(self, tg_id: int, text: str):
        try:
            await self.bot.send_message(tg_id, text)
        except Exception as e:
            logger.warning("_send failed tg_id=%s: %s", tg_id, e)


TYPE_LABEL = {"sim": "📊 Плагиат", "ai": "🤖 AI-детекция", "both": "✨ Оба отчёта"}

# Синглтон — импортируется в main.py, api.py и handlers/turnitin.py
turnitin_queue = TurnitinQueueManager()
