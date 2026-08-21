import asyncio
import logging
import os
import sys

# Добавляем папку bot/ в путь
sys.path.insert(0, os.path.dirname(__file__))

# Стандартное logging на уровень INFO — чтобы в логах были видны шаги
# turnitin_service (открытие Chrome, логин, загрузка файла, клики и т.д.)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Глушим слишком болтливые библиотеки
for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio", "hpack", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, MenuButtonDefault, WebAppInfo
from loguru import logger

from config import settings
from database import db
from middlewares.register import RegisterMiddleware
from handlers import start, turnitin, humanizer, payment, profile, admin, webapp
from services.queue_manager import turnitin_queue
from services.ai_rental_manager import ai_rental_manager


async def run_api():
    """Запускает FastAPI сервер параллельно с ботом."""
    import uvicorn
    from api import app
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.MINI_APP_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    logger.info("Starting bot...")

    # Инициализация БД
    await db.init_db(settings.DATABASE_PATH)

    # Создаём директории
    os.makedirs(settings.REPORTS_DIR, exist_ok=True)
    os.makedirs(settings.UPLOADS_DIR, exist_ok=True)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Middlewares
    dp.message.middleware(RegisterMiddleware())
    dp.callback_query.middleware(RegisterMiddleware())

    # Routers (порядок важен!)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(webapp.router)   # Mini App web_app_data
    dp.include_router(turnitin.router)
    dp.include_router(humanizer.router)
    dp.include_router(payment.router)
    dp.include_router(profile.router)

    # Устанавливаем кнопку-меню (синяя кнопка слева от поля ввода)
    if settings.MINI_APP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Приложение",
                web_app=WebAppInfo(url=settings.MINI_APP_URL),
            )
        )
        logger.info(f"Menu button set → {settings.MINI_APP_URL}")
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    # Запускаем воркер очереди Turnitin
    turnitin_queue.start(bot, settings)
    logger.info("Turnitin queue worker started")

    # Воркер аренды ИИ-аккаунтов (истечение, напоминания, авто-разлогин по прокси-очереди)
    ai_rental_manager.start()

    logger.info(f"Bot started. Mini App API на порту {settings.MINI_APP_PORT}. Polling...")

    # Запускаем бот и API сервер одновременно
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        run_api(),
    )


if __name__ == "__main__":
    asyncio.run(main())
