from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardRemove,
)
from config import settings


def main_menu_kb() -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Если задан MINI_APP_URL — одна кнопка открытия приложения, иначе убираем клавиатуру."""
    if settings.MINI_APP_URL:
        return ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="🌐 Открыть приложение",
                    web_app=WebAppInfo(url=settings.MINI_APP_URL),
                )
            ]],
            resize_keyboard=True,
        )
    return ReplyKeyboardRemove()


def open_app_inline() -> InlineKeyboardMarkup | None:
    """Инлайн-кнопка для открытия приложения в сообщении."""
    if not settings.MINI_APP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚀 Открыть приложение",
            web_app=WebAppInfo(url=settings.MINI_APP_URL),
        )
    ]])


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def back_inline(callback_data: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
    ])
