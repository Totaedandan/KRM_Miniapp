"""
Обработчик сообщений от Telegram Mini App (web_app_data).

Поддерживаемые действия:
  turnitin_check   — выбран тип проверки; спишет тенге с баланса
  buy_tokens       — купить токены из тенге-баланса (мгновенно, без чека)
  topup_tenge      — пополнить тенге-баланс через Kaspi (открывает FSM)
"""

import json
import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from aiogram.fsm.context import FSMContext

from config import settings
from database import db
from keyboards.main_kb import main_menu_kb, open_app_inline, cancel_kb

logger = logging.getLogger(__name__)
router = Router()

TYPE_LABEL = {"sim": "📊 Плагиат", "ai": "🤖 AI-детекция", "both": "✨ Оба отчёта"}


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message, state: FSMContext):
    await state.clear()

    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        logger.warning(f"Invalid web_app_data: {message.web_app_data.data!r}")
        return

    action = data.get("action")
    logger.info(f"web_app_data: {action} from {message.from_user.id}")

    if action == "turnitin_check":
        await _start_turnitin(message, state, data.get("report_type", "both"),
                              bool(data.get("is_premium")))

    elif action == "buy_tokens":
        await _instant_buy_tokens(message, data.get("pkg", {}))

    elif action == "topup_tenge":
        await _start_tenge_topup(message, state, float(data.get("amount", 0)))

    else:
        logger.warning(f"Unknown web_app_data action: {action}")


# ── Turnitin: спишем тенге и запросим файл ───────────────────────────────────

async def _start_turnitin(message: Message, state: FSMContext, report_type: str,
                          is_premium: bool = False):
    from services.queue_manager import turnitin_queue
    prices = await db.get_prices()
    base = {"sim": int(prices.get("price_sim", 700)),
            "ai":  int(prices.get("price_ai", 700)),
            "both": int(prices.get("price_both", 1200))}.get(report_type, 700)
    mult = await db.get_premium_multiplier()
    price = int(round(base * mult)) if is_premium else base
    label = TYPE_LABEL.get(report_type, report_type)

    is_wl = settings.is_admin(message.from_user.id) or await db.is_whitelisted(message.from_user.id)

    if is_wl:
        order_id = await db.create_order(
            user_id=message.from_user.id,
            username=message.from_user.username,
            report_type=report_type,
            payment_method="whitelist",
            amount_tenge=0,
            is_premium=is_premium,
            status="queued",
        )
    else:
        balance = await db.get_tenge_balance(message.from_user.id)
        if balance < price:
            await message.answer(
                f"❌ <b>Недостаточно средств</b>\n\n"
                f"Нужно: <b>{price} ₸</b>\n"
                f"Ваш баланс: <b>{balance:.0f} ₸</b>\n\n"
                f"Пополните баланс через приложение.",
            )
            return
        # Атомарно: списание + заказ + запись в ledger в одной транзакции
        try:
            res = await db.create_paid_order(
                user_id=message.from_user.id,
                username=message.from_user.username,
                report_type=report_type,
                price=price,
                is_premium=is_premium,
            )
        except db.InsufficientFunds:
            await message.answer(
                "❌ <b>Недостаточно средств</b>\n\nПополните баланс через приложение.",
            )
            return
        order_id = res["order_id"]
    await state.clear()

    new_balance = await db.get_tenge_balance(message.from_user.id)
    balance_note = f"\n💰 Остаток баланса: <b>{new_balance:.0f} ₸</b>" if not is_wl else ""
    q_label = "⚡ Премиум" if is_premium else "🐢 Обычная"
    await message.answer(
        f"✅ <b>Оплачено {price if not is_wl else 0} ₸</b> — {label}\n"
        f"Очередь: <b>{q_label}</b>{balance_note}\n\n"
        f"Место в очереди забронировано. Когда подойдёт очередь — попросим файл 🔔",
        reply_markup=main_menu_kb(),
    )
    await turnitin_queue.on_reservation(order_id)


# ── Мгновенная покупка токенов из тенге-баланса ──────────────────────────────

async def _instant_buy_tokens(message: Message, pkg: dict):
    tokens = float(pkg.get("tokens", 0))
    tenge  = float(pkg.get("tenge", 0))

    if not tokens or not tenge:
        await message.answer("❌ Неверный пакет.")
        return

    is_wl = settings.is_admin(message.from_user.id) or await db.is_whitelisted(message.from_user.id)
    if not is_wl:
        balance = await db.get_tenge_balance(message.from_user.id)
        if balance < tenge:
            await message.answer(
                f"❌ <b>Недостаточно средств</b>\n\n"
                f"Нужно: <b>{tenge:.0f} ₸</b>\n"
                f"Ваш баланс: <b>{balance:.0f} ₸</b>\n\n"
                f"Пополните баланс через приложение.",
            )
            return
        await db.deduct_tenge(message.from_user.id, tenge, reason="token_purchase")

    new_token_balance = await db.add_tokens(message.from_user.id, tokens, reason="token_purchase")
    new_tenge_balance = await db.get_tenge_balance(message.from_user.id)

    # Логируем транзакцию
    await db.create_payment(
        user_id=message.from_user.id,
        payment_type="balance",
        purpose="tokens",
        amount_tenge=tenge,
        tokens_amount=tokens,
    )

    await message.answer(
        f"✅ <b>Токены зачислены!</b>\n\n"
        f"🪙 +{tokens:.0f} токенов\n"
        f"💰 Баланс ₸: <b>{new_tenge_balance:.0f} ₸</b>\n"
        f"🪙 Баланс токенов: <b>{new_token_balance:.1f}</b>",
        reply_markup=open_app_inline(),
    )


# ── Пополнение тенге-баланса через Kaspi ─────────────────────────────────────

async def _start_tenge_topup(message: Message, state: FSMContext, amount: float):
    if amount <= 0:
        await message.answer("❌ Укажите сумму пополнения.")
        return

    payment_id = await db.create_payment(
        user_id=message.from_user.id,
        payment_type="kaspi",
        purpose="topup_tenge",
        amount_tenge=amount,
    )
    await state.update_data(topup_payment_id=payment_id, topup_amount=amount)

    from handlers.payment import TengeTopupFSM
    await state.set_state(TengeTopupFSM.waiting_kaspi_pdf)

    phone = await db.get_setting("kaspi_phone") or settings.KASPI_PHONE
    name  = await db.get_setting("kaspi_recipient_name") or settings.KASPI_RECIPIENT_NAME
    kaspi_qr = Path(__file__).parent.parent / "assets" / "kaspi_qr.jpg"

    caption = (
        f"💳 <b>Пополнение баланса через Kaspi</b>\n\n"
        f"Сумма: <b>{amount:.0f} ₸</b>\n"
        f"Получатель: <b>{name}</b>\n"
        f"Телефон: <b>{phone}</b>\n\n"
        f"Переведите точную сумму и отправьте PDF-чек из приложения Kaspi."
    )

    if kaspi_qr.exists():
        await message.answer_photo(
            FSInputFile(str(kaspi_qr)), caption=caption, parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    else:
        await message.answer(caption, reply_markup=cancel_kb())
