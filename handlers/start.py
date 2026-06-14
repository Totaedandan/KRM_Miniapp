from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from config import config
from database import Database
from utils.keyboards import main_menu, service_keyboard, back_to_menu
from utils.states import OrderFlow

router = Router()

WELCOME = (
    "👋 <b>Добро пожаловать в Turnitin Bot!</b>\n\n"
    "Проверьте вашу работу:\n"
    "🤖 <b>AI-детекция</b> — определяет % сгенерированного текста\n"
    "📊 <b>Плагиат</b> — показывает % заимствований\n"
    "✨ <b>AI + Плагиат</b> — оба отчёта по выгодной цене\n\n"
    "📋 <b>Требования к файлу:</b>\n"
    "• Форматы: .pdf · .docx · .doc · .txt · .rtf\n"
    "• Объём: <b>от 300 до 30 000 слов</b>\n"
    "• Для проверки на ИИ: текст должен быть <b>на английском языке</b>"
)


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(WELCOME, reply_markup=main_menu())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(WELCOME, reply_markup=main_menu())
    await cb.answer()


@router.callback_query(F.data == "new_order")
async def cb_new_order(cb: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    prices = await db.get_prices()
    await cb.message.edit_text(
        "📋 <b>Выберите услугу:</b>",
        reply_markup=service_keyboard(prices)
    )
    await state.set_state(OrderFlow.choosing_service)
    await cb.answer()


@router.callback_query(F.data == "my_orders")
async def cb_my_orders(cb: CallbackQuery, db: Database):
    orders = await db.get_user_orders(cb.from_user.id, limit=8)
    if not orders:
        await cb.answer("У вас ещё нет заказов", show_alert=True)
        return

    emoji = {"pending": "⏳", "paid": "✅", "processing": "⚙️", "done": "✔️", "error": "❌", "cancelled": "🚫"}
    label = {"ai": "AI", "similarity": "Плагиат", "both": "AI+Плагиат"}

    lines = ["<b>📂 Ваши заказы:</b>\n"]
    for o in orders:
        e = emoji.get(o["status"], "❓")
        lines.append(f"{e} #{o['id']} — {label.get(o['report_type'], o['report_type'])} — {o['status']}")

    await cb.message.edit_text("\n".join(lines), reply_markup=back_to_menu())
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery, db: Database):
    username = await db.get_setting("help_username")
    phone    = await db.get_setting("help_phone")
    await cb.message.edit_text(
        f"❓ <b>Помощь</b>\n\n"
        f"Если возникли проблемы — свяжитесь с нами:\n"
        f"Telegram: {username}\n"
        f"Телефон: {phone}",
        reply_markup=back_to_menu()
    )
    await cb.answer()


@router.message(Command("help"))
async def cmd_help(msg: Message, db: Database):
    username = await db.get_setting("help_username")
    phone    = await db.get_setting("help_phone")
    await msg.answer(
        f"❓ <b>Помощь</b>\n\n"
        f"Telegram: {username}\n"
        f"Телефон: {phone}",
        reply_markup=back_to_menu()
    )