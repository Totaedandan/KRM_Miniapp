from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def token_packages_kb(packages: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(packages, 1):
        rows.append([InlineKeyboardButton(
            text=f"🪙 {p['tokens']} токенов — {p['tenge']} ₸",
            callback_data=f"buy_tokens:{i}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
