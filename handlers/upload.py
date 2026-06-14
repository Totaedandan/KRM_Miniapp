import asyncio
import logging
import os
import io
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile, Document
from aiogram.fsm.context import FSMContext

from config import config
from database import Database
from services.turnitin import turnitin_service, TurnitinError
from services.kaspi_service import parse_kaspi_pdf, validate_receipt
from utils.keyboards import back_to_menu, service_keyboard, payment_cancel_keyboard
from utils.states import OrderFlow

router = Router()
logger = logging.getLogger(__name__)

ALLOWED  = {".pdf", ".docx", ".doc", ".txt", ".rtf"}
MAX_SIZE = 40 * 1024 * 1024

MIN_WORDS = 300
LANG_EN_THRESHOLD = 0.80  # минимальная доля английского для AI Detection


def _extract_text(file_path: str, ext: str) -> str:
    """Извлечь текст из docx, pdf, txt, doc, rtf."""
    try:
        if ext == ".docx":
            import docx
            doc = docx.Document(file_path)
            return " ".join(p.text for p in doc.paragraphs)
        elif ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                return " ".join(
                    page.extract_text() or "" for page in pdf.pages
                )
        elif ext in (".txt", ".rtf", ".doc"):
            # Для txt читаем напрямую, для doc/rtf — лучшее что можем без внешних утилит
            with open(file_path, "r", errors="ignore") as f:
                return f.read()
        else:
            return ""
    except Exception as e:
        logger.warning("Text extraction failed for %s: %s", file_path, e)
        return ""


def _count_words(text: str) -> int:
    return len(text.split())


def _detect_language(text: str) -> tuple[str, float]:
    """
    Возвращает (язык, доля_английского).
    Использует lingua-language-detector.
    """
    try:
        from lingua import Language, LanguageDetectorBuilder
        detector = LanguageDetectorBuilder.from_all_languages().build()
        # Берём первые 2000 слов для скорости
        sample = " ".join(text.split()[:2000])
        confidence_values = detector.compute_language_confidence_values(sample)
        # Ищем долю английского
        en_confidence = 0.0
        top_lang = "Unknown"
        top_conf = 0.0
        for cv in confidence_values:
            if cv.language == Language.ENGLISH:
                en_confidence = cv.value
            if cv.value > top_conf:
                top_conf = cv.value
                top_lang = cv.language.name.capitalize()
        return top_lang, en_confidence
    except Exception as e:
        logger.warning("Language detection failed: %s", e)
        # Если определить не удалось — пропускаем проверку
        return "Unknown", 1.0


LANG_NAMES = {
    "Russian": "русский",
    "Kazakh": "казахский",
    "German": "немецкий",
    "French": "французский",
    "Spanish": "испанский",
    "Chinese": "китайский",
    "Unknown": "неизвестный",
}


async def _validate_file(
    file_path: str,
    ext: str,
    report_type: str,
    order_id: int,
) -> tuple[bool, str]:
    """
    Возвращает (ok, error_message).
    ok=True — файл прошёл валидацию.
    """
    import asyncio
    loop = asyncio.get_event_loop()

    # Шаг 1: извлечь текст
    text = await loop.run_in_executor(None, _extract_text, file_path, ext)
    word_count = _count_words(text)
    logger.info("Order %s: word count=%d", order_id, word_count)

    # Шаг 2: проверка слов
    if word_count < MIN_WORDS:
        return False, (
            f"❌ <b>Ошибка: слишком мало текста</b>\n\n"
            f"Для проверки необходимо минимум <b>{MIN_WORDS} слов</b>.\n"
            f"В вашем документе обнаружено: <b>{word_count} слов</b>.\n\n"
            f"Загрузите файл с достаточным объёмом текста —\n"
            f"<b>повторная оплата не требуется.</b>"
        )

    # Шаг 3: языковая проверка (только для ai / both)
    if report_type in ("ai", "both"):
        top_lang, en_conf = await loop.run_in_executor(
            None, _detect_language, text
        )
        logger.info(
            "Order %s: detected lang=%s, en_confidence=%.2f",
            order_id, top_lang, en_conf,
        )
        if en_conf < LANG_EN_THRESHOLD:
            lang_ru = LANG_NAMES.get(top_lang, top_lang.lower())
            return False, (
                f"❌ <b>Ошибка: документ не на английском языке</b>\n\n"
                f"AI Detection работает только с текстами на английском.\n"
                f"Ваш документ определён как: <b>{lang_ru}</b>.\n\n"
                f"Загрузите файл на английском языке —\n"
                f"<b>повторная оплата не требуется.</b>"
            )

    return True, ""

FILE_REMINDER = (
    "📋 <b>Напоминаем требования к файлу:</b>\n"
    "• Объём: от <b>300 до 30 000 слов</b>\n"
    "• Для AI-детекции: текст должен быть <b>на английском языке</b>\n\n"
    "<i>Если файл не соответствует — Turnitin может не выдать отчёт.</i>"
)

KASPI_QR_PATH = os.path.join(os.path.dirname(__file__), "assets", "kaspi_qr.jpg")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def is_free_user(user_id: int, db: Database) -> bool:
    """Admins and users in free_users list skip payment."""
    if user_id in config.ADMIN_IDS:
        return True
    raw = await db.get_setting("free_users") or ""
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return str(user_id) in ids


async def _error_text(db: Database, order_id: int) -> str:
    username = await db.get_setting("help_username") or "@support"
    phone    = await db.get_setting("help_phone") or ""
    return (
        "😔 <b>К сожалению, не удалось получить отчёт.</b>\n\n"
        "Это может происходить из-за временных технических проблем на стороне Turnitin.\n\n"
        "Пожалуйста, свяжитесь с нами — мы разберёмся и вернём оплату:\n"
        f"📱 Telegram: {username}\n"
        f"📞 Телефон: {phone}\n\n"
        f"При обращении укажите номер заказа: <b>#{order_id}</b>"
    )


# ── Выбор услуги → показываем инструкцию по оплате ────────────────────────────

@router.callback_query()
async def cb_any(cb: CallbackQuery, state: FSMContext, db: Database):
    logger.info("GOT CALLBACK: data=%s from=%s state=%s", cb.data, cb.from_user.id, await state.get_state())
    if cb.data == "payment:cancel":
        await _cancel_payment(cb, state, db)
        return
    if cb.data not in {"service:ai", "service:similarity", "service:both"}:
        return
    await cb_choose_service_impl(cb, state, db)
    await cb.answer()


async def cb_choose_service_impl(cb: CallbackQuery, state: FSMContext, db: Database):
    report_type = cb.data.split(":")[1]  # ai | similarity | both

    prices = await db.get_prices()
    cur    = prices.get("price_currency", "тг")
    price_map = {
        "ai":         prices.get("price_ai", "700"),
        "similarity": prices.get("price_similarity", "700"),
        "both":       prices.get("price_both", "1200"),
    }
    label = {"ai": "AI-детекция", "similarity": "Плагиат", "both": "AI + Плагиат"}
    price_val = price_map[report_type]

    # Создаём заказ со статусом pending
    order_id = await db.create_order(
        user_id  =cb.from_user.id,
        username =cb.from_user.username or cb.from_user.full_name,
        report_type=report_type,
    )

    await state.update_data(
        order_id    =order_id,
        report_type =report_type,
        price_tenge =price_val,
    )

    # ── Бесплатный доступ (админы + whitelist) ────────────────────────────────
    if await is_free_user(cb.from_user.id, db):
        await db.update_order(order_id, status="paid")
        await state.set_state(OrderFlow.waiting_file)
        await cb.message.edit_text(
            f"✅ <b>{label[report_type]}</b>\n\n"
            "📎 Отправьте файл для проверки.\n"
            "Форматы: .pdf · .docx · .doc · .txt · .rtf"
        )
        await cb.answer()
        return

    # ── Обычный пользователь → оплата ────────────────────────────────────────
    await state.set_state(OrderFlow.waiting_payment)

    kaspi_link  = await db.get_setting("kaspi_link") or "https://kaspi.kz/pay"
    expire_mins = await db.get_setting("kaspi_expire_minutes") or "60"

    caption = (
        f"💳 <b>Оплата — {label[report_type]}</b>\n\n"
        f"1️⃣ Переведи ровно <b>{price_val} {cur}</b> по Kaspi QR\n"
        f"   или по ссылке: {kaspi_link}\n\n"
        f"2️⃣ В приложении Kaspi нажми <b>«Поделиться»</b> → <b>«PDF»</b>\n"
        f"   и отправь мне этот PDF-файл.\n\n"
        f"⚠️ Чек принимается не старше <b>{expire_mins} мин</b> после оплаты.\n\n"
        f"<i>Заказ #{order_id}</i>"
    )

    try:
        qr_path = os.path.normpath(KASPI_QR_PATH)
        if os.path.exists(qr_path):
            photo = FSInputFile(qr_path)
            await cb.message.answer_photo(
                photo=photo,
                caption=caption,
                reply_markup=payment_cancel_keyboard(),
            )
            try:
                await cb.message.delete()
            except Exception:
                pass
        else:
            await cb.message.edit_text(caption, reply_markup=payment_cancel_keyboard())
    except Exception as e:
        logger.warning("Failed to send QR: %s", e)
        await cb.message.edit_text(caption, reply_markup=payment_cancel_keyboard())

    await cb.answer()


# ── Отмена оплаты ─────────────────────────────────────────────────────────────

async def _cancel_payment(cb: CallbackQuery, state: FSMContext, db: Database):
    data     = await state.get_data()
    order_id = data.get("order_id")
    if order_id:
        await db.update_order(order_id, status="cancelled")
    await state.clear()
    prices = await db.get_prices()

    try:
        # Если сообщение текстовое — edit_text
        await cb.message.edit_text(
            "❌ Оплата отменена.\n\nВыберите услугу:",
            reply_markup=service_keyboard(prices),
        )
    except Exception:
        # Если сообщение с фото (QR-код) — удаляем и отправляем новое
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(
            "❌ Оплата отменена.\n\nВыберите услугу:",
            reply_markup=service_keyboard(prices),
        )

    await state.set_state(OrderFlow.choosing_service)
    await cb.answer()


# ── Получение PDF чека ────────────────────────────────────────────────────────

@router.message(OrderFlow.waiting_payment, F.document)
async def receive_payment_pdf(msg: Message, state: FSMContext, db: Database):
    doc: Document = msg.document
    if not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await msg.answer(
            "⚠️ Это не PDF-файл.\n"
            "Отправь именно <b>PDF чек</b> из приложения Kaspi\n"
            "(Поделиться → PDF в чеке)."
        )
        return

    data        = await state.get_data()
    order_id    = data.get("order_id")
    report_type = data.get("report_type")
    price_tenge = data.get("price_tenge", "700")

    processing_msg = await msg.answer("🔍 Проверяю чек...")

    try:
        tg_file   = await msg.bot.get_file(doc.file_id)
        file_bytes = await msg.bot.download_file(tg_file.file_path)
        pdf_bytes = file_bytes.read()
        logger.info(
            "File downloaded OK: order=%s file=%s (attempt 1/3, size=%d bytes)",
            order_id, doc.file_name, len(pdf_bytes)
        )

        receipt = parse_kaspi_pdf(pdf_bytes)
        if not receipt:
            await processing_msg.edit_text(
                "❌ Не удалось прочитать PDF.\n"
                "Убедись, что это оригинальный чек из приложения Kaspi (не скриншот)."
            )
            return

        expire_mins = int(await db.get_setting("kaspi_expire_minutes") or "60")
        valid, reason = validate_receipt(
            receipt,
            expected_tenge=float(price_tenge),
            expire_minutes=expire_mins,
        )

        if not valid:
            await processing_msg.edit_text(
                f"{reason}\n\nОтправь корректный чек или нажми «Отменить».",
                reply_markup=payment_cancel_keyboard(),
            )
            return

        # Проверяем что чек не использовался
        if await db.receipt_exists(receipt.transaction_id):
            await processing_msg.edit_text(
                "❌ Этот чек уже был использован.\n"
                "Каждый чек можно использовать только один раз."
            )
            return

        # Сохраняем чек и подтверждаем заказ
        await db.add_receipt(
            receipt_id=receipt.transaction_id,
            user_id=msg.from_user.id,
            order_id=order_id,
        )
        await db.update_order(order_id, status="paid")

        await processing_msg.delete()
        await state.update_data(order_id=order_id, report_type=report_type)
        await state.set_state(OrderFlow.waiting_file)

        label = {"ai": "AI-детекция", "similarity": "Плагиат", "both": "AI + Плагиат"}
        await msg.answer(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"🧾 Чек: <code>{receipt.transaction_id}</code>\n"
            f"💵 Сумма: <b>{receipt.amount:,.0f} ₸</b>\n\n"
            f"📎 Теперь отправь файл для проверки.\n"
            f"Форматы: .pdf · .docx · .doc · .txt · .rtf"
        )
        logger.info(
            "Payment confirmed: user=%s order=%s receipt=%s amount=%.0f",
            msg.from_user.id, order_id, receipt.transaction_id, receipt.amount
        )

    except Exception as e:
        logger.error("Payment PDF processing error order %s: %s", order_id, e)
        await processing_msg.edit_text(
            f"❌ Ошибка при обработке чека: <code>{str(e)[:200]}</code>\n\n"
            "Попробуй снова или нажми «Отменить».",
            reply_markup=payment_cancel_keyboard(),
        )


@router.message(OrderFlow.waiting_payment)
async def wrong_payment_input(msg: Message):
    await msg.answer(
        "📎 Отправь <b>PDF-файл чека</b> из приложения Kaspi.\n\n"
        "Как получить: открой историю операций в Kaspi → найди платёж → "
        "нажми <b>«Поделиться»</b> → выбери <b>PDF</b>."
    )


# ── Получение файла для проверки ──────────────────────────────────────────────

@router.message(OrderFlow.waiting_file, F.document)
async def receive_file(msg: Message, state: FSMContext, db: Database, bot: Bot):
    doc  = msg.document
    name = doc.file_name or "document"
    ext  = Path(name).suffix.lower()

    if ext not in ALLOWED:
        await msg.answer(
            f"❌ Формат <b>{ext}</b> не поддерживается.\n"
            f"Разрешены: {', '.join(ALLOWED)}"
        )
        return

    if doc.file_size and doc.file_size > MAX_SIZE:
        await msg.answer("❌ Файл больше 40 МБ — уменьшите его и попробуйте снова.")
        return

    data        = await state.get_data()
    order_id    = data["order_id"]
    report_type = data["report_type"]

    # Скачиваем файл для валидации
    upload_dir = Path(config.UPLOADS_DIR) / str(order_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path  = str(upload_dir / name)

    tg_file = await bot.get_file(doc.file_id)
    await bot.download_file(tg_file.file_path, file_path)

    # Валидируем файл (кол-во слов + язык)
    valid_msg = await msg.answer("🔍 Проверяю файл...")
    ok, error_text = await _validate_file(file_path, ext, report_type, order_id)

    try:
        await valid_msg.delete()
    except Exception:
        pass

    if not ok:
        # Переводим в состояние ожидания повторной загрузки
        await state.set_state(OrderFlow.awaiting_reupload)
        await msg.answer(error_text)
        return

    # Валидация пройдена — запускаем обработку
    await state.set_state(OrderFlow.processing)
    await db.update_order(order_id, status="processing", file_name=name)
    await msg.answer(FILE_REMINDER)

    status_msg = await msg.answer(
        "⚙️ <b>Файл получен, загружаю в Turnitin...</b>\n\n"
        "⏳ Проверка занимает <b>5–30 минут</b>.\n"
        "<i>Я пришлю отчёт как только он будет готов — можете закрыть чат.</i>"
    )

    await db.update_order(order_id, file_path=file_path)

    asyncio.create_task(_background(
        bot=bot,
        chat_id=msg.chat.id,
        status_msg_id=status_msg.message_id,
        order_id=order_id,
        file_path=file_path,
        file_name=name,
        report_type=report_type,
        db=db,
        state=state,
    ))


@router.message(OrderFlow.waiting_file)
async def wrong_type(msg: Message):
    await msg.answer(
        "📎 Отправьте именно <b>файл</b> (скрепка 📎, не фото).\n"
        "Форматы: .pdf · .docx · .doc · .txt · .rtf"
    )


@router.message(OrderFlow.awaiting_reupload, F.document)
async def reupload_file(msg: Message, state: FSMContext, db: Database, bot: Bot):
    """Повторная загрузка после ошибки валидации — оплата не требуется."""
    # Переводим обратно в waiting_file и переиспользуем receive_file
    await state.set_state(OrderFlow.waiting_file)
    await receive_file(msg, state, db, bot)


@router.message(OrderFlow.awaiting_reupload)
async def reupload_wrong_type(msg: Message):
    await msg.answer(
        "📎 Загрузите файл документа (docx или pdf)"
    )


# ── Фоновая обработка ─────────────────────────────────────────────────────────

async def _background(
    bot: Bot,
    chat_id: int,
    status_msg_id: int,
    order_id: int,
    file_path: str,
    file_name: str,
    report_type: str,
    db: Database,
    state: FSMContext,
):
    email     = await db.get_setting("turnitin_email")    or config.TURNITIN_EMAIL
    password  = await db.get_setting("turnitin_password") or config.TURNITIN_PASSWORD
    class_id  = await db.get_setting("turnitin_class_id") or config.TURNITIN_CLASS_ID
    assign_id = await db.get_setting("turnitin_assign_id") or config.TURNITIN_ASSIGNMENT_ID

    try:
        sim_path, ai_path = await turnitin_service.process(
            order_id=order_id,
            file_path=file_path,
            file_name=file_name,
            report_type=report_type,
            email=email,
            password=password,
            class_id=class_id,
            assign_id=assign_id,
            reports_dir=config.REPORTS_DIR,
            poll_interval=config.TURNITIN_POLL_INTERVAL,
            timeout=config.TURNITIN_TIMEOUT,
        )

        await db.update_order(
            order_id, status="done",
            report_path_sim=sim_path,
            report_path_ai=ai_path,
        )

        try:
            await bot.edit_message_text(
                "✅ <b>Отчёты готовы! Отправляю...</b>",
                chat_id=chat_id, message_id=status_msg_id
            )
        except Exception:
            pass

        sent = False

        if sim_path and os.path.exists(sim_path):
            await bot.send_document(
                chat_id,
                FSInputFile(sim_path, filename=f"similarity_report_{order_id}.pdf"),
                caption="📊 <b>Similarity Report (Плагиат)</b>"
            )
            sent = True

        if ai_path and os.path.exists(ai_path):
            await bot.send_document(
                chat_id,
                FSInputFile(ai_path, filename=f"ai_report_{order_id}.pdf"),
                caption="🤖 <b>AI Detection Report</b>"
            )
            sent = True

        if not sent and report_type == "ai":
            await db.update_order(order_id, status="done")
            await bot.send_message(
                chat_id,
                "🤖 <b>AI Detection: результат получен</b>\n\n"
                "Показатель: <b>*%</b> (0% AI-контента не обнаружено)\n\n"
                "Turnitin не генерирует PDF-отчёт при нулевом результате.\n"
                "Для новой проверки нажмите /start",
                reply_markup=back_to_menu()
            )
            sent = True

        if sent and (sim_path or ai_path):
            await bot.send_message(
                chat_id,
                "✔️ <b>Готово!</b>\n\nДля новой проверки нажмите /start",
                reply_markup=back_to_menu()
            )
        elif not sent:
            await db.update_order(order_id, status="error", error_text="Reports not downloaded")
            err_msg = await _error_text(db, order_id)
            try:
                await bot.edit_message_text(
                    err_msg, chat_id=chat_id, message_id=status_msg_id, reply_markup=back_to_menu()
                )
            except Exception:
                await bot.send_message(chat_id, err_msg, reply_markup=back_to_menu())

    except TurnitinError as e:
        logger.error("TurnitinError order %s: %s", order_id, e)
        await db.update_order(order_id, status="error", error_text=str(e))
        err_msg = await _error_text(db, order_id)
        try:
            await bot.edit_message_text(err_msg, chat_id=chat_id, message_id=status_msg_id, reply_markup=back_to_menu())
        except Exception:
            await bot.send_message(chat_id, err_msg, reply_markup=back_to_menu())

    except Exception as e:
        logger.exception("Unexpected error order %s: %s", order_id, e)
        await db.update_order(order_id, status="error", error_text=str(e))
        err_msg = await _error_text(db, order_id)
        try:
            await bot.edit_message_text(err_msg, chat_id=chat_id, message_id=status_msg_id, reply_markup=back_to_menu())
        except Exception:
            await bot.send_message(chat_id, err_msg, reply_markup=back_to_menu())

    finally:
        await state.clear()
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info("Cleaned up upload file: %s", file_path)
        except Exception:
            pass