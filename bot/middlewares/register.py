from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from database import db
import logging

logger = logging.getLogger(__name__)


class RegisterMiddleware(BaseMiddleware):
    """Авторегистрация пользователей + блокировка забаненных."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user

        if user:
            try:
                u = await db.get_or_create_user(
                    tg_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                )
                if u.get("is_banned"):
                    if isinstance(event, Message):
                        await event.answer("🚫 Ваш аккаунт заблокирован.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("🚫 Заблокирован.", show_alert=True)
                    return
            except Exception as e:
                logger.error(f"RegisterMiddleware error: {e}")

        return await handler(event, data)
