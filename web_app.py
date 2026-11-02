import asyncio
import logging
import os
from threading import Thread

from flask import Flask
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

# Импортируем роутер и функцию init_db из твоего основного файла bot.py
from bot import router, init_db

# Настройки для Flask
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health_check():
    """Эндпоинт для проверки, что сервис жив."""
    return "Bot is running", 200

def run_flask():
    """Запускает Flask-сервер в отдельном потоке."""
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

async def start_bot():
    """Инициализирует и запускает бота в основном потоке."""
    init_db()
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    logging.info("Bot started (polling)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Запускаем Flask в фоновом потоке
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота в основном потоке
    asyncio.run(start_bot())
