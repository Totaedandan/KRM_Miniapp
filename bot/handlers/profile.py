from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from database import db
from keyboards.main_kb import main_menu_kb, back_inline

router = Router()


@router.message(F.text == "👤 Профиль")
async def profile_menu(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Профиль не найден.", reply_markup=main_menu_kb())
        return

    orders = await db.get_user_orders(message.from_user.id, limit=5)
    status_emoji = {"pending": "⏳", "queued": "📋", "awaiting_file": "📎",
                    "ready": "📥", "paid": "✅", "processing": "⚙️",
                    "done": "✔️", "error": "❌", "cancelled": "🚫", "whitelist": "🎁"}
    type_label   = {"sim": "Плагиат", "ai": "AI", "both": "AI+Плагиат"}

    wl = "✅ Да (бесплатный доступ)" if user.get("is_whitelisted") else "❌ Нет"
    name = f"@{user['username']}" if user.get("username") else user.get("full_name") or "—"
    balance = user.get("token_balance", 0.0)

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user['tg_id']}</code>\n"
        f"👤 Имя: {name}\n"
        f"🪙 Баланс токенов: <b>{balance:.1f}</b>\n"
        f"⭐ Whitelist: {wl}\n"
    )

    if orders:
        text += "\n📂 <b>Последние заказы Turnitin:</b>\n"
        for o in orders:
            e = status_emoji.get(o["status"], "❓")
            t = type_label.get(o["report_type"], o["report_type"])
            d = o["created_at"][:10] if o.get("created_at") else "—"
            text += f"  {e} #{o['id']} — {t} — {d}\n"

    await message.answer(text, reply_markup=main_menu_kb())
