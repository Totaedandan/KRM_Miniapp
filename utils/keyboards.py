from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Новый заказ", callback_data="new_order")],
        [InlineKeyboardButton(text="📂 Мои заказы",  callback_data="my_orders")],
        [InlineKeyboardButton(text="❓ Помощь",       callback_data="help")],
    ])


def service_keyboard(prices: dict) -> InlineKeyboardMarkup:
    cur = prices.get("price_currency", "тг")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🤖 AI-детекция — {prices.get('price_ai','700')} {cur}",
            callback_data="service:ai"
        )],
        [InlineKeyboardButton(
            text=f"📊 Плагиат — {prices.get('price_similarity','700')} {cur}",
            callback_data="service:similarity"
        )],
        [InlineKeyboardButton(
            text=f"✨ AI + Плагиат — {prices.get('price_both','1200')} {cur}",
            callback_data="service:both"
        )],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],
    ])


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


# ── Admin keyboards ──────────────────────────────────────────────

def admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",            callback_data="adm:stats")],
        [InlineKeyboardButton(text="💰 Изменить цены",         callback_data="adm:prices")],
        [InlineKeyboardButton(text="👥 Управление админами",   callback_data="adm:admins")],
        [InlineKeyboardButton(text="🔑 Логин/пароль Turnitin", callback_data="adm:creds")],
        [InlineKeyboardButton(text="🗃 Управление чеками",     callback_data="adm:receipts")],
        [InlineKeyboardButton(text="💳 Настройки Kaspi",       callback_data="adm:kaspi")],
        [InlineKeyboardButton(text="🆓 Бесплатный доступ",     callback_data="adm:free_users")],
        [InlineKeyboardButton(text="🧹 Очистить память",       callback_data="adm:cleanup")],
    ])


def payment_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить оплату", callback_data="payment:cancel")],
    ])


def admin_prices_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI-детекция",  callback_data="adm:price:ai")],
        [InlineKeyboardButton(text="📊 Плагиат",      callback_data="adm:price:similarity")],
        [InlineKeyboardButton(text="✨ AI + Плагиат", callback_data="adm:price:both")],
        [InlineKeyboardButton(text="🔤 Валюта",       callback_data="adm:price:currency")],
        [InlineKeyboardButton(text="◀️ Назад",        callback_data="adm:back")],
    ])


def admin_receipts_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список чеков", callback_data="adm:receipts:list")],
        [InlineKeyboardButton(text="➕ Добавить чек", callback_data="adm:receipts:add")],
        [InlineKeyboardButton(text="🗑 Удалить чек",  callback_data="adm:receipts:del")],
        [InlineKeyboardButton(text="◀️ Назад",        callback_data="adm:back")],
    ])