from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from keyboards.main_kb import main_menu_kb, open_app_inline
from database import db

router = Router()

WELCOME = (
    "\U0001f44b <b>Добро пожаловать!</b>\n\n"
    "Сервис проверки и обработки текстов:\n\n"
    "\U0001f4c4 <b>Turnitin</b> \xe2\x80\x94 проверка на плагиат и AI-детекция\n"
    "• Плагиат (Similarity)\n"
    "• AI-детекция\n"
    "• Оба отчёта по выгодной цене\n\n"
    "✍️ <b>Хуманайзер</b> — делает ИИ-тексты неотличимыми от человеческих\n\n"
    "Нажми кнопку ниже чтобы открыть приложение \U0001f447"
)

BAN_TEXT = (
    "\U0001f6ab <b>Доступ ограничен</b>\n\n"
    "Ваш аккаунт заблокирован.\n"
    "Если вы считаете это ошибкой — обратитесь в поддержку."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    await db.get_or_create_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )

    banned = await db.is_user_banned(
        message.from_user.id,
        message.from_user.username,
    )
    if banned:
        await message.answer(BAN_TEXT, parse_mode="HTML")
        return

    # Устанавливаем reply-клавиатуру (кнопка "Открыть приложение" внизу)
    await message.answer(WELCOME, reply_markup=main_menu_kb(), parse_mode="HTML")

    # Дополнительно: большая инлайн-кнопка для открытия прямо в сообщении
    inline_kb = open_app_inline()
    if inline_kb:
        await message.answer("\U0001f447", reply_markup=inline_kb)


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(WELCOME, reply_markup=main_menu_kb(), parse_mode="HTML")
    inline_kb = open_app_inline()
    if inline_kb:
        await callback.message.answer("\U0001f447", reply_markup=inline_kb)
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    username = await db.get_setting("help_username") or "@support"
    phone    = await db.get_setting("help_phone") or ""
    await message.answer(
        f"❓ <b>Помощь</b>\n\n"
        f"Telegram: {username}\n"
        f"{'Телефон: ' + phone if phone else ''}",
        parse_mode="HTML",
    )
