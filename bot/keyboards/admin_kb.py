from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",        callback_data="adm_stats")],
        [InlineKeyboardButton(text="📋 Заказы (в процессе)", callback_data="adm_orders")],
        [InlineKeyboardButton(text="👥 Вайтлист",           callback_data="adm_whitelist")],
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="adm_find_user")],
        [InlineKeyboardButton(text="💰 Цены Turnitin",      callback_data="adm_prices")],
        [InlineKeyboardButton(text="⚙️ Настройки Turnitin", callback_data="adm_turnitin_cfg")],
    ])


def admin_whitelist_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить",     callback_data="adm_wl_add")],
        [InlineKeyboardButton(text="➖ Убрать",       callback_data="adm_wl_remove")],
        [InlineKeyboardButton(text="📋 Список",       callback_data="adm_wl_list")],
        [InlineKeyboardButton(text="◀️ Назад",        callback_data="adm_main")],
    ])


def admin_user_kb(tg_id: int, is_wl: bool) -> InlineKeyboardMarkup:
    wl_text = "➖ Убрать из вайтлиста" if is_wl else "➕ В вайтлист"
    wl_cb   = f"adm_wl_remove_id:{tg_id}" if is_wl else f"adm_wl_add_id:{tg_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=wl_text,              callback_data=wl_cb)],
        [InlineKeyboardButton(text="🪙 Начислить токены", callback_data=f"adm_give_tokens:{tg_id}")],
        [InlineKeyboardButton(text="🚫 Забанить",         callback_data=f"adm_ban:{tg_id}")],
        [InlineKeyboardButton(text="◀️ Назад",            callback_data="adm_main")],
    ])
