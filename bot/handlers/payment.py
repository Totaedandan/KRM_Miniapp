"""
Payment handler.

Потоки:
  1. TengeTopupFSM  — пополнение тенге-баланса через Kaspi PDF-чек (из Mini App)
  2. TokenPayFSM    — покупка токенов через Kaspi (из бота напрямую, legacy)
"""

import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, Document, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import settings
from database import db
from keyboards.payment_kb import token_packages_kb
from keyboards.main_kb import main_menu_kb, cancel_kb, open_app_inline
from services.kaspi_service import parse_kaspi_pdf, validate_receipt

logger = logging.getLogger(__name__)
router = Router()


# ═══════════════════════════════════════════════════════════
#  FSM: пополнение тенге-баланса (из Mini App)
# ═══════════════════════════════════════════════════════════

class TengeTopupFSM(StatesGroup):
    waiting_kaspi_pdf = State()


@router.message(TengeTopupFSM.waiting_kaspi_pdf, F.document)
async def receive_topup_kaspi_pdf(message: Message, state: FSMContext):
    doc: Document = message.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await message.answer("Прикрепи оригинальный PDF-чек из приложения Kaspi.")
        return

    data = await state.get_data()
    payment_id = data.get("topup_payment_id")
    amount     = float(data.get("topup_amount", 0))
    promo_code = data.get("topup_promo_code")

    file = await message.bot.get_file(doc.file_id)
    downloaded = await message.bot.download_file(file.file_path)
    pdf_bytes = downloaded.read()

    receipt = parse_kaspi_pdf(pdf_bytes)
    if not receipt:
        await message.answer("Не удалось прочитать чек. Отправь оригинальный PDF из Kaspi.")
        return

    if await db.receipt_exists(receipt.transaction_id or ""):
        await message.answer("Этот чек уже использовался.")
        return

    ok, error = validate_receipt(receipt, expected_tenge=amount, expire_minutes=settings.KASPI_EXPIRE_MINUTES)
    if not ok:
        await message.answer(error)
        return

    await db.add_receipt(receipt.transaction_id, message.from_user.id)
    if payment_id:
        await db.confirm_payment(payment_id, receipt_id=receipt.transaction_id)

    new_balance = await db.add_tenge(
        message.from_user.id, amount, reason="topup",
        idempotency_key=f"kaspi:{receipt.transaction_id}",
    )

    bonus_line = ""
    if promo_code:
        promo_result = await db.redeem_promo_percent(message.from_user.id, promo_code, base_amount=amount)
        if promo_result.get("applied"):
            bonus_line = (f"\n🎁 Бонус по промокоду <b>{promo_code}</b>: "
                          f"+{promo_result['bonus']:.0f} ₸ (бонусный баланс)")

    await state.clear()

    kb = open_app_inline()
    await message.answer(
        f"✅ <b>Баланс пополнен!</b>\n\n"
        f"💰 Зачислено: <b>{amount:.0f} ₸</b>{bonus_line}\n"
        f"💰 Новый баланс: <b>{new_balance:.0f} ₸</b>",
        reply_markup=kb or main_menu_kb(),
    )


@router.message(TengeTopupFSM.waiting_kaspi_pdf, F.text == "❌ Отмена")
async def cancel_topup(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# ═══════════════════════════════════════════════════════════
#  FSM: покупка токенов через Kaspi напрямую (legacy / бот)
# ═══════════════════════════════════════════════════════════

class TokenPayFSM(StatesGroup):
    waiting_kaspi_pdf = State()


@router.message(F.text == "💳 Купить токены")
@router.callback_query(F.data == "buy_menu")
async def buy_menu(event, state: FSMContext):
    packages  = await db.get_token_packages()
    tg_id     = event.from_user.id
    tenge_bal = await db.get_tenge_balance(tg_id)
    token_bal = await db.get_token_balance(tg_id)
    text = (
        f"🪙 <b>Покупка токенов Хуманайзера</b>\n\n"
        f"💰 Баланс ₸: <b>{tenge_bal:.0f} ₸</b>\n"
        f"🪙 Токены: <b>{token_bal:.1f}</b>\n\n"
        f"Стоимость: 0.5 🪙/слово (бизнес-мод: 5 🪙/слово)\n\n"
        f"Выберите пакет:"
    )
    kb = token_packages_kb(packages)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()


@router.callback_query(F.data.startswith("buy_tokens:"))
async def choose_package(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1]) - 1
    packages = await db.get_token_packages()
    if idx < 0 or idx >= len(packages):
        await callback.answer("Неверный пакет.")
        return
    pkg = packages[idx]

    is_wl     = await db.is_whitelisted(callback.from_user.id)
    tenge_bal = await db.get_tenge_balance(callback.from_user.id)

    # Достаточно тенге на балансе — покупаем мгновенно
    if is_wl or tenge_bal >= pkg["tenge"]:
        if not is_wl:
            await db.deduct_tenge(callback.from_user.id, pkg["tenge"], reason="token_purchase")
        new_tok   = await db.add_tokens(callback.from_user.id, pkg["tokens"], reason="token_purchase")
        new_tenge = await db.get_tenge_balance(callback.from_user.id)
        if not is_wl:
            await db.create_payment(
                user_id=callback.from_user.id,
                payment_type="balance",
                purpose="tokens",
                amount_tenge=pkg["tenge"],
                tokens_amount=pkg["tokens"],
            )
        await callback.message.edit_text(
            f"✅ <b>Токены куплены!</b>\n\n"
            f"🪙 +{pkg['tokens']} токенов\n"
            f"💰 Баланс ₸: <b>{new_tenge:.0f} ₸</b>\n"
            f"🪙 Токены: <b>{new_tok:.1f}</b>",
        )
        await callback.answer("✅ Готово!")
        return

    # Недостаточно тенге → Kaspi чек
    payment_id = await db.create_payment(
        user_id=callback.from_user.id,
        payment_type="kaspi",
        purpose="tokens",
        amount_tenge=pkg["tenge"],
        tokens_amount=pkg["tokens"],
    )
    await state.update_data(token_payment_id=payment_id, pkg=pkg)
    await state.set_state(TokenPayFSM.waiting_kaspi_pdf)

    phone = await db.get_setting("kaspi_phone") or settings.KASPI_PHONE
    name  = await db.get_setting("kaspi_recipient_name") or settings.KASPI_RECIPIENT_NAME
    kaspi_qr = Path(__file__).parent.parent / "assets" / "kaspi_qr.jpg"
    caption = (
        f"💳 <b>Оплата через Kaspi</b>\n\n"
        f"Пакет: <b>{pkg['tokens']} токенов</b>\n"
        f"Сумма: <b>{pkg['tenge']} ₸</b>\n"
        f"Получатель: <b>{name}</b>\n"
        f"Телефон: <b>{phone}</b>\n\n"
        f"После оплаты отправьте PDF-чек из Kaspi."
    )
    if kaspi_qr.exists():
        await callback.message.answer_photo(
            FSInputFile(str(kaspi_qr)), caption=caption, parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    else:
        await callback.message.answer(caption, reply_markup=cancel_kb())
    await callback.answer()


@router.message(TokenPayFSM.waiting_kaspi_pdf, F.document)
async def receive_token_kaspi_pdf(message: Message, state: FSMContext):
    doc: Document = message.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await message.answer("Прикрепи PDF-чек из приложения Kaspi.")
        return

    data       = await state.get_data()
    payment_id = data["token_payment_id"]
    pkg        = data["pkg"]

    file       = await message.bot.get_file(doc.file_id)
    downloaded = await message.bot.download_file(file.file_path)
    pdf_bytes  = downloaded.read()

    receipt = parse_kaspi_pdf(pdf_bytes)
    if not receipt:
        await message.answer("Не удалось прочитать чек. Отправь оригинальный PDF из Kaspi.")
        return

    if await db.receipt_exists(receipt.transaction_id or ""):
        await message.answer("Этот чек уже использовался.")
        return

    ok, error = validate_receipt(receipt, expected_tenge=pkg["tenge"], expire_minutes=settings.KASPI_EXPIRE_MINUTES)
    if not ok:
        await message.answer(error)
        return

    await db.add_receipt(receipt.transaction_id, message.from_user.id)
    await db.confirm_payment(payment_id, receipt_id=receipt.transaction_id)
    new_balance = await db.add_tokens(
        message.from_user.id, pkg["tokens"], reason="token_purchase",
        idempotency_key=f"kaspi-tok:{receipt.transaction_id}",
    )

    await state.clear()
    await message.answer(
        f"✅ <b>Токены зачислены!</b>\n\n"
        f"🪙 +{pkg['tokens']} токенов\n"
        f"🪙 Баланс токенов: <b>{new_balance:.1f}</b>",
        reply_markup=main_menu_kb(),
    )


@router.message(TokenPayFSM.waiting_kaspi_pdf, F.text == "❌ Отмена")
async def cancel_token_kaspi(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# ═══════════════════════════════════════════════════════════
#  Fallback: Kaspi PDF без FSM state (пополнение из Mini App)
# ═══════════════════════════════════════════════════════════

@router.message(F.document)
async def receive_topup_miniapp(message: Message, state: FSMContext):
    """Срабатывает когда пользователь прислал PDF без FSM-стейта (из Mini App)."""
    current = await state.get_state()
    if current is not None:
        return  # уже обрабатывается FSM-хэндлером выше

    pending = await db.get_pending_action(message.from_user.id)
    if pending.get("action") != "waiting_topup_pdf":
        return

    data       = pending.get("data", {})
    payment_id = data.get("payment_id")
    amount     = float(data.get("amount", 0))
    promo_code = data.get("promo_code")

    if not payment_id or not amount:
        await message.answer("❌ Платёж не найден. Создайте новый через приложение.")
        return

    await db.clear_pending_action(message.from_user.id)
    await state.update_data(topup_payment_id=payment_id, topup_amount=amount, topup_promo_code=promo_code)
    await state.set_state(TengeTopupFSM.waiting_kaspi_pdf)
    await receive_topup_kaspi_pdf(message, state)
