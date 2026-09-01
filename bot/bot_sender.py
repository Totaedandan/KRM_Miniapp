"""
Отправка сообщений через Telegram Bot HTTP API.
Используется из FastAPI (без импорта aiogram Bot).
"""
import logging
from typing import Optional

import httpx
from config import settings

logger = logging.getLogger(__name__)

_BOT_API = f"https://api.telegram.org/bot{settings.BOT_TOKEN}"


async def send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
    """Отправить текстовое сообщение пользователю. parse_mode="" или None — без
    разметки (например, для необработанного текста, который может содержать
    <, &, и т.п. и сломать HTML-парсинг)."""
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_BOT_API}/sendMessage", json=payload)
            if r.status_code != 200:
                logger.warning(f"bot_sender: status {r.status_code} for chat {chat_id}")
            return r.status_code == 200
    except Exception as e:
        logger.error(f"bot_sender.send_message error: {e}")
        return False


async def send_document(chat_id: int, filename: str, content: bytes,
                         caption: str = "", parse_mode: str = "HTML") -> bool:
    """Отправить файл пользователю (аналог message.answer_document из aiogram)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_BOT_API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                files={"document": (filename, content, "text/plain")},
            )
            if r.status_code != 200:
                logger.warning(f"bot_sender: sendDocument status {r.status_code} for chat {chat_id}")
            return r.status_code == 200
    except Exception as e:
        logger.error(f"bot_sender.send_document error: {e}")
        return False


async def get_chat_member(chat_id: str, user_id: int) -> Optional[dict]:
    """Участник чата ({status, ...}) или None при сбое (бот не админ канала,
    канал не существует, сетевая ошибка и т.п.) — вызывающий код должен сам
    решить, как трактовать None (см. api.py::_check_subscription — fail-open)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BOT_API}/getChatMember",
                params={"chat_id": chat_id, "user_id": user_id},
            )
            data = r.json()
            if not data.get("ok"):
                logger.warning(f"bot_sender.get_chat_member: {data}")
                return None
            return data["result"]
    except Exception as e:
        logger.error(f"bot_sender.get_chat_member error: {e}")
        return None
