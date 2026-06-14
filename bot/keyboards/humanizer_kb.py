from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.humanizer_service import TONE_OPTIONS, MODE_OPTIONS


def humanizer_menu_kb(tone: str = "College", mode: str = "Medium", business: bool = False) -> InlineKeyboardMarkup:
    tone_label = TONE_OPTIONS.get(tone, tone)
    mode_label = MODE_OPTIONS.get(mode, mode)
    biz_label  = "✅ Вкл" if business else "❌ Выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Хуманизировать текст", callback_data="humanize_start")],
        [InlineKeyboardButton(text=f"🎓 Тон: {tone_label}",      callback_data="set_tone")],
        [InlineKeyboardButton(text=f"📊 Режим: {mode_label}",    callback_data="set_mode")],
        [InlineKeyboardButton(text=f"💼 Бизнес-мод: {biz_label}", callback_data="toggle_business")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


def tone_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v}", callback_data=f"tone:{k}")]
        for k, v in TONE_OPTIONS.items()
    ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="humanizer_menu")]])


def mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v}", callback_data=f"mode:{k}")]
        for k, v in MODE_OPTIONS.items()
    ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="humanizer_menu")]])
