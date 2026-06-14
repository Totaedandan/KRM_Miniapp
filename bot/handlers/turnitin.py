"""
Хэндлер Turnitin: выбор услуги → выбор очереди → оплата → бронь места → файл по очереди.

Новый флоу (по схеме клиента):
  1. "📄 Turnitin" → выбор типа проверки (sim / ai / both)
  2. Выбор очереди: обычная (×1) или премиум (×1.5)
  3. Оплата: whitelist — бесплатно; иначе Kaspi-чек (или баланс из Mini App)
  4. Бронирование места в очереди (статус 'queued')
  5. Контроллер очереди сам запрашивает файл, когда подходит очередь
     (премиум — сразу), даётся 3 минуты, иначе возврат денег
  6. Файл принимается, когда заказ в статусе awaiting_file (pending_action=waiting_file)
"""

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, Document, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import settings
from database import db
from keyboards.turnitin_kb import service_kb, queue_kb
from keyboards.main_kb import main_menu_kb, cancel_kb
from services.kaspi_service import parse_kaspi_pdf, validate_receipt
from services.queue_manager import turnitin_queue

logger = logging.getLogger(__name__)
router = Router()

ALLOWED_EXT = {".pdf", ".docx", ".doc", ".txt", ".rtf"}
MAX_FILE_SIZE = 40 * 1024 * 1024  # 40 MB
MIN_WORDS = 300

TYPE_LABEL = {"sim": "📊 Плагиат", "ai": "🤖 AI-детекция", "both": "✨ Оба отчёта"}


class TurnitinFlow(StatesGroup):
    choosing_service = State()
    choosing_queue   = State()
    waiting_kaspi    = State()


# ── Главное меню Turnitin ─────────────────────────────────────────────────────

@router.message(F.text == "📄 Turnitin")
async def turnitin_menu(message: Message, state: FSMContext):
    await state.clear()
    prices = await db.get_prices()
    await message.answer(
        "📄 <b>Turnitin — проверка работы</b>\n\nВыберите тип проверки:",
        reply_markup=service_kb(prices),
    )
    await state.set_state(TurnitinFlow.choosing_service)


@router.callback_query(F.data == "turnitin_back")
async def turnitin_back(callback: CallbackQuery, state: FSMContext):
    prices = await db.get_prices()
    await callback.message.edit_text(
        "📄 <b>Turnitin — проверка работы</b>\n\nВыберите тип проверки:",
        reply_markup=service_kb(prices),
    )
    await state.set_state(TurnitinFlow.choosing_service)
    await callback.answer()


# ── Выбор типа проверки → выбор очереди ──────────────────────────────────────

@router.callback_query(F.data.startswith("turnitin:"), TurnitinFlow.choosing_service)
async def choose_service(callback: CallbackQuery, state: FSMContext):
    report_type = callback.data.split(":")[1]  # sim | ai | both
    prices = await db.get_prices()
    base = {"sim": prices.get("price_sim", 700),
            "ai":  prices.get("price_ai", 700),
            "both": prices.get("price_both", 1200)}[report_type]
    mult = await db.get_premium_multiplier()
    await state.update_data(report_type=report_type, base_price=base)

    label = TYPE_LABEL.get(report_type, report_type)
    await callback.message.edit_text(
        f"{label}\n\n"
        f"Выберите очередь:\n"
        f"🐢 <b>Обычная</b> — дешевле, но с ожиданием\n"
        f"⚡ <b>Премиум</b> — без ожидания, приоритетная обработка",
        reply_markup=queue_kb(report_type, base, mult),
    )
    await state.set_state(TurnitinFlow.choosing_queue)
    await callback.answer()


# ── Выбор очереди → оплата / бронь ───────────────────────────────────────────

@router.callback_query(F.data.startswith("tqueue:"), TurnitinFlow.choosing_queue)
async def choose_queue(callback: CallbackQuery, state: FSMContext):
    _, report_type, queue = callback.data.split(":")
    is_premium = (queue == "premium")
    prices = await db.get_prices()
    base = {"sim": prices.get("price_sim", 700),
            "ai":  prices.get("price_ai", 700),
            "both": prices.get("price_both", 1200)}[report_type]
    mult = await db.get_premium_multiplier()
    price = int(round(base * mult)) if is_premium else base

    await state.update_data(report_type=report_type, price_tenge=price, is_premium=is_premium)
    uid = callback.from_user.id

    # Whitelist и админы — бесплатно, сразу бронируем место
    if settings.is_admin(uid) or await db.is_whitelisted(uid):
        order_id = await db.create_order(
            user_id=uid, username=callback.from_user.username,
            report_type=report_type, payment_method="whitelist",
            amount_tenge=0, is_premium=is_premium, status="queued",
        )
        await callback.message.delete()
        await state.clear()
        await callback.answer()
        await turnitin_queue.on_reservation(order_id)
        return

    # Обычный пользователь — Kaspi-оплата
    order_id = await db.create_order(
        user_id=uid, username=callback.from_user.username,
        report_type=report_type, payment_method="kaspi",
        amount_tenge=price, is_premium=is_premium, status="pending",
    )
    payment_id = await db.create_payment(
        user_id=uid, payment_type="kaspi", purpose="turnitin",
        amount_tenge=price, order_id=order_id,
    )
    await state.update_data(order_id=order_id, payment_id=payment_id)
    await state.set_state(TurnitinFlow.waiting_kaspi)

    phone = await db.get_setting("kaspi_phone") or settings.KASPI_PHONE
    name  = await db.get_setting("kaspi_recipient_name") or settings.KASPI_RECIPIENT_NAME
    label = TYPE_LABEL.get(report_type, report_type)
    q_label = "⚡ Премиум" if is_premium else "🐢 Обычная"

    kaspi_qr = Path(__file__).parent.parent / "assets" / "kaspi_qr.jpg"
    caption = (
        f"💳 <b>Оплата через Kaspi</b>\n\n"
        f"Услуга: <b>{label}</b>\n"
        f"Очередь: <b>{q_label}</b>\n"
        f"Сумма: <b>{price} ₸</b>\n"
        f"Получатель: <b>{name}</b>\n"
        f"Телефон: <b>{phone}</b>\n\n"
        f"После оплаты отправьте PDF-чек из приложения Kaspi."
    )
    await callback.message.delete()
    if kaspi_qr.exists():
        await callback.message.answer_photo(
            FSInputFile(str(kaspi_qr)), caption=caption, parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    else:
        await callback.message.answer(caption, reply_markup=cancel_kb())
    await callback.answer()


# ── Kaspi: получить PDF-чек → бронь места ─────────────────────────────────────

@router.message(TurnitinFlow.waiting_kaspi, F.text == "❌ Отмена")
async def cancel_kaspi(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("order_id"):
        await db.update_order(data["order_id"], status="cancelled")
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.message(TurnitinFlow.waiting_kaspi, F.document)
async def receive_kaspi_receipt(message: Message, state: FSMContext, bot: Bot):
    doc: Document = message.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await message.answer("⚠️ Прикрепи оригинальный PDF-чек из приложения Kaspi.")
        return

    data = await state.get_data()
    order_id   = data["order_id"]
    payment_id = data["payment_id"]
    price      = data["price_tenge"]

    file = await bot.get_file(doc.file_id)
    downloaded = await bot.download_file(file.file_path)
    pdf_bytes = downloaded.read()

    receipt = parse_kaspi_pdf(pdf_bytes)
    if not receipt:
        await message.answer("❌ Не удалось прочитать чек. Убедись что это оригинальный PDF из Kaspi.")
        return
    if await db.receipt_exists(receipt.transaction_id or ""):
        await message.answer("❌ Этот чек уже использовался.")
        return

    ok, error = validate_receipt(receipt, expected_tenge=price, expire_minutes=settings.KASPI_EXPIRE_MINUTES)
    if not ok:
        await message.answer(error)
        return

    await db.add_receipt(receipt.transaction_id, message.from_user.id)
    await db.confirm_payment(payment_id, receipt_id=receipt.transaction_id)
    # Деньги получены — бронируем место в очереди
    await db.update_order(order_id, status="queued", receipt_id=receipt.transaction_id)
    await state.clear()
    await message.answer("✅ Оплата подтверждена! Бронируем место в очереди…",
                         reply_markup=main_menu_kb())
    await turnitin_queue.on_reservation(order_id)


# ── Приём файла (когда заказ в статусе awaiting_file) ────────────────────────

@router.message(F.document)
async def receive_file(message: Message, state: FSMContext, bot: Bot):
    """Срабатывает, когда контроллер очереди запросил файл (pending_action=waiting_file)."""
    if await state.get_state() is not None:
        return  # активен другой FSM-флоу (Kaspi-чек, хуманайзер и т.п.)

    pending = await db.get_pending_action(message.from_user.id)
    if pending.get("action") != "waiting_file":
        return

    pdata = pending.get("data", {})
    order_id    = pdata.get("order_id")
    report_type = pdata.get("report_type", "both")
    if not order_id:
        return

    order = await db.get_order(order_id)
    if not order or order["status"] != "awaiting_file":
        await message.answer("⌛ Время на отправку файла истекло. Создайте заказ заново.")
        await db.clear_pending_action(message.from_user.id)
        return

    doc: Document = message.document
    fname = (doc.file_name or "file").lower()
    ext   = Path(fname).suffix
    if ext not in ALLOWED_EXT:
        await message.answer(f"⚠️ Поддерживаются форматы: {', '.join(ALLOWED_EXT)}")
        return
    if doc.file_size > MAX_FILE_SIZE:
        await message.answer("⚠️ Файл слишком большой. Максимум 40 МБ.")
        return

    os.makedirs(settings.UPLOADS_DIR, exist_ok=True)
    file = await bot.get_file(doc.file_id)
    downloaded = await bot.download_file(file.file_path)
    file_path = os.path.join(settings.UPLOADS_DIR, f"{order_id}_{fname}")
    with open(file_path, "wb") as f:
        f.write(downloaded.read())

    ok, err_msg = await _validate_file(file_path, ext, report_type)
    if not ok:
        try:
            os.remove(file_path)
        except Exception:
            pass
        await message.answer(err_msg)  # остаётся время дослать корректный файл
        return

    await db.update_order(order_id, status="ready", file_name=fname, file_path=file_path)
    await db.clear_pending_action(message.from_user.id)
    await message.answer("✅ Файл принят! Обрабатываем в порядке очереди ⏳",
                         reply_markup=main_menu_kb())
    await turnitin_queue.on_file_received(order_id)


async def _validate_file(file_path: str, ext: str, report_type: str) -> tuple[bool, str]:
    loop = asyncio.get_event_loop()

    def extract_text() -> str:
        try:
            if ext == ".docx":
                import docx
                d = docx.Document(file_path)
                return " ".join(p.text for p in d.paragraphs)
            elif ext == ".pdf":
                import pdfplumber
                with pdfplumber.open(file_path) as pdf:
                    return " ".join(page.extract_text() or "" for page in pdf.pages)
            else:
                with open(file_path, "r", errors="ignore") as f:
                    return f.read()
        except Exception:
            return ""

    text = await loop.run_in_executor(None, extract_text)
    words = len(text.split())

    if words < MIN_WORDS:
        return False, (
            f"❌ <b>Слишком мало текста</b>\n\n"
            f"Нужно минимум <b>{MIN_WORDS} слов</b>, найдено: <b>{words}</b>.\n"
            f"Отправьте файл с большим объёмом текста (время ещё идёт)."
        )
    if words > 30000:
        return False, (
            f"❌ <b>Слишком много текста</b> ({words} слов).\n"
            f"Максимум — 30 000 слов. Разбейте файл на части."
        )

    if report_type in ("ai", "both"):
        def detect_lang():
            try:
                from lingua import Language, LanguageDetectorBuilder
                detector = LanguageDetectorBuilder.from_all_languages().build()
                sample = " ".join(text.split()[:2000])
                vals = detector.compute_language_confidence_values(sample)
                en_conf = next((v.value for v in vals if v.language == Language.ENGLISH), 0.0)
                top = max(vals, key=lambda v: v.value)
                return top.language.name.capitalize(), en_conf
            except Exception:
                return "Unknown", 1.0

        lang, en_conf = await loop.run_in_executor(None, detect_lang)
        if en_conf < 0.80:
            return False, (
                f"❌ <b>Документ не на английском языке</b>\n\n"
                f"AI-детекция работает только с английскими текстами.\n"
                f"Обнаружен язык: <b>{lang}</b> (время ещё идёт)."
            )

    return True, ""
