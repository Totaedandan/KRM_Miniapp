"""
Отправка сообщений через Telegram Bot HTTP API.
Используется из FastAPI (без импорта aiogram Bot).
"""
import logging
import httpx
from config import settings

logger = logging.getLogger(__name__)

_BOT_API = f"https://api.telegram.org/bot{settings.BOT_TOKEN}"


async def send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
    """Отправить текстовое сообщение пользователю."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{_BOT_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            )
            if r.status_code != 200:
                logger.warning(f"bot_sender: status {r.status_code} for chat {chat_id}")
            return r.status_code == 200
    except Exception as e:
        logger.error(f"bot_sender.send_message error: {e}")
        return False
