from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def service_kb(prices: dict) -> InlineKeyboardMarkup:
    sim   = prices.get("price_sim", 700)
    ai    = prices.get("price_ai", 700)
    both  = prices.get("price_both", 1200)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📊 Плагиат — {sim} ₸",     callback_data="turnitin:sim")],
        [InlineKeyboardButton(text=f"🤖 AI-детекция — {ai} ₸",  callback_data="turnitin:ai")],
        [InlineKeyboardButton(text=f"✨ Оба отчёта — {both} ₸", callback_data="turnitin:both")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def queue_kb(report_type: str, base_price: int, mult: float) -> InlineKeyboardMarkup:
    """Выбор очереди: обычная (×1) или премиум (×mult)."""
    premium_price = int(round(base_price * mult))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🐢 Обычная очередь — {base_price} ₸",
            callback_data=f"tqueue:{report_type}:regular")],
        [InlineKeyboardButton(
            text=f"⚡ Премиум (без ожидания) — {premium_price} ₸",
            callback_data=f"tqueue:{report_type}:premium")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="turnitin_back")],
    ])
