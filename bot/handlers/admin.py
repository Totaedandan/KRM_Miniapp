"""
Административная панель.
Команда: /admin
"""

import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import settings
from database import db
from keyboards.admin_kb import admin_main_kb, admin_whitelist_kb, admin_user_kb
from keyboards.main_kb import main_menu_kb

logger = logging.getLogger(__name__)
router = Router()


class AdminFSM(StatesGroup):
    wl_add       = State()
    wl_remove    = State()
    find_user    = State()
    give_tokens  = State()
    set_price    = State()
    set_turnitin = State()


def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id)   # ADMIN_IDS + суперадмин


def _back_kb(cb: str = "adm_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]
    ])


# ── Вход в панель ─────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer("🔑 <b>Администрирование</b>", reply_markup=admin_main_kb())


@router.callback_query(F.data == "adm_main")
async def cb_adm_main(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("🔑 <b>Администрирование</b>", reply_markup=admin_main_kb())
    await callback.answer()


# ── Статистика ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    stats = await db.get_stats()

    orders = stats["order_counts"]
    by_type = stats["by_type"]
    rev = stats["turnitin_revenue"]
    tok = stats["token_sales"]

    lines = [
        "📊 <b>Статистика</b>\n",
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>",
        f"⭐ Whitelist: <b>{stats['whitelist_count']}</b>",
        "",
        "📄 <b>Заказы Turnitin:</b>",
    ]
    for status, cnt in orders.items():
        lines.append(f"  • {status}: {cnt}")

    lines += ["", "📊 <b>По типу (всего заказов):</b>"]
    for t, cnt in by_type.items():
        lines.append(f"  • {t}: {cnt}")

    lines += ["", "💰 <b>Выручка Turnitin:</b>"]
    for ptype, info in rev.items():
        lines.append(f"  • {ptype}: {info['count']} заказов, {info['tenge'] or 0:.0f} ₸")

    lines += ["", "🪙 <b>Продажа токенов:</b>"]
    for ptype, info in tok.items():
        lines.append(f"  • {ptype}: {info['count']} шт., {info['tokens'] or 0:.0f} токенов")

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=_back_kb(), parse_mode="HTML"
    )
    await callback.answer()


# ── Активные заказы ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_orders")
async def adm_orders(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    orders = await db.get_pending_orders(limit=30)
    if not orders:
        text = "✅ Нет активных заказов."
    else:
        prem = [o for o in orders if o.get("is_premium")]
        reg  = [o for o in orders if not o.get("is_premium")]
        lines = [f"📋 <b>Очереди ({len(orders)}):</b>"]

        def fmt_order(o):
            amt = o.get("amount_tenge") or 0
            return (f"#{o['id']} | @{o['username'] or o['user_id']} | "
                    f"{o['report_type']} | {o['status']} | {amt:.0f} ₸")

        lines.append(f"\n⚡ <b>Премиум ({len(prem)}):</b>")
        lines += [fmt_order(o) for o in prem] or ["  —"]
        lines.append(f"\n🐢 <b>Обычная ({len(reg)}):</b>")
        lines += [fmt_order(o) for o in reg] or ["  —"]
        lines.append("\n↩️ Отмена с возвратом: <code>/cancelorder ID</code>")
        text = "\n".join(lines)

    await callback.message.edit_text(text, reply_markup=_back_kb(), parse_mode="HTML")
    await callback.answer()


@router.message(Command("cancelorder"))
async def cmd_cancelorder(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /cancelorder <id заказа>")
        return
    from services.queue_manager import turnitin_queue
    order_id = int(parts[1])
    result = await turnitin_queue.cancel_order(order_id, reason="cancelled_by_admin")
    if result.get("ok"):
        await message.answer(
            f"✅ Заказ #{order_id} отменён.\n"
            f"💰 Возвращено пользователю: <b>{result.get('refunded', 0):.0f} ₸</b>"
        )
    else:
        await message.answer(f"❌ Не удалось отменить: {result.get('error', 'ошибка')}")


# ── Вайтлист ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_whitelist")
async def adm_whitelist(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "👥 <b>Управление Whitelist</b>\n\nWhitelist — бесплатный доступ к боту.",
        reply_markup=admin_whitelist_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "adm_wl_list")
async def adm_wl_list(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    users = await db.get_whitelist_users()
    if not users:
        text = "📋 Whitelist пуст."
    else:
        lines = ["📋 <b>Whitelist:</b>\n"]
        for u in users:
            name = f"@{u['username']}" if u.get("username") else u.get("full_name") or "—"
            lines.append(f"• <code>{u['tg_id']}</code> {name}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=_back_kb("adm_whitelist"))
    await callback.answer()


@router.callback_query(F.data == "adm_wl_add")
async def adm_wl_add_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFSM.wl_add)
    await callback.message.answer("Введи Telegram ID для добавления в whitelist:")
    await callback.answer()


@router.message(AdminFSM.wl_add)
async def adm_wl_add_exec(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        tg_id = int(message.text.strip())
        ok = await db.set_whitelist(tg_id, True)
        if ok:
            await message.answer(f"✅ <code>{tg_id}</code> добавлен в whitelist.")
        else:
            await message.answer(f"❌ Пользователь <code>{tg_id}</code> не найден в БД.")
    except ValueError:
        await message.answer("❌ Введи числовой Telegram ID.")
    await state.clear()


@router.callback_query(F.data == "adm_wl_remove")
async def adm_wl_remove_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFSM.wl_remove)
    await callback.message.answer("Введи Telegram ID для удаления из whitelist:")
    await callback.answer()


@router.message(AdminFSM.wl_remove)
async def adm_wl_remove_exec(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        tg_id = int(message.text.strip())
        ok = await db.set_whitelist(tg_id, False)
        if ok:
            await message.answer(f"✅ <code>{tg_id}</code> убран из whitelist.")
        else:
            await message.answer(f"❌ Пользователь не найден.")
    except ValueError:
        await message.answer("❌ Введи числовой Telegram ID.")
    await state.clear()


# Inline whitelist из карточки пользователя
@router.callback_query(F.data.startswith("adm_wl_add_id:"))
async def inline_wl_add(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    tg_id = int(callback.data.split(":")[1])
    await db.set_whitelist(tg_id, True)
    await callback.answer(f"✅ {tg_id} добавлен в whitelist", show_alert=True)


@router.callback_query(F.data.startswith("adm_wl_remove_id:"))
async def inline_wl_remove(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    tg_id = int(callback.data.split(":")[1])
    await db.set_whitelist(tg_id, False)
    await callback.answer(f"✅ {tg_id} убран из whitelist", show_alert=True)


# ── Найти пользователя ────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_find_user")
async def adm_find_user_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFSM.find_user)
    await callback.message.answer("Введи Telegram ID или @username:")
    await callback.answer()


@router.message(AdminFSM.find_user)
async def adm_find_user_exec(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    user = await db.find_user(message.text.strip())
    await state.clear()
    if not user:
        await message.answer("❌ Пользователь не найден.")
        return
    name    = f"@{user['username']}" if user.get("username") else user.get("full_name") or "—"
    balance = user.get("token_balance", 0.0)
    is_wl   = bool(user.get("is_whitelisted"))
    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"🆔 ID: <code>{user['tg_id']}</code>\n"
        f"👤 Имя: {name}\n"
        f"🪙 Токены: <b>{balance:.1f}</b>\n"
        f"⭐ Whitelist: {'✅' if is_wl else '❌'}\n"
        f"📅 Регистрация: {user.get('created_at', '—')[:10]}"
    )
    await message.answer(text, reply_markup=admin_user_kb(user["tg_id"], is_wl))


# ── Начислить токены ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_give_tokens:"))
async def adm_give_tokens_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    tg_id = int(callback.data.split(":")[1])
    await state.update_data(give_target=tg_id)
    await state.set_state(AdminFSM.give_tokens)
    await callback.message.answer(f"Сколько токенов начислить <code>{tg_id}</code>?")
    await callback.answer()


@router.message(AdminFSM.give_tokens)
async def adm_give_tokens_exec(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = data.get("give_target")
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
        new_balance = await db.add_tokens(tg_id, amount, reason="admin_adjust")
        await message.answer(
            f"✅ Начислено <b>{amount} 🪙</b> пользователю <code>{tg_id}</code>.\n"
            f"Новый баланс: <b>{new_balance:.1f} 🪙</b>"
        )
    except ValueError:
        await message.answer("❌ Введи числовое значение больше 0.")
    await state.clear()


# ── Забанить ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_ban:"))
async def adm_ban(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    tg_id = int(callback.data.split(":")[1])
    await db.set_banned(tg_id, True)
    await callback.answer(f"🚫 {tg_id} забанен", show_alert=True)


# ── Цены Turnitin ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_prices")
async def adm_prices(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    prices = await db.get_prices()
    await callback.message.edit_text(
        f"💰 <b>Цены Turnitin</b>\n\n"
        f"📊 Плагиат: <b>{prices.get('price_sim', 700)} ₸</b>\n"
        f"🤖 AI-детекция: <b>{prices.get('price_ai', 700)} ₸</b>\n"
        f"✨ Оба отчёта: <b>{prices.get('price_both', 1200)} ₸</b>\n\n"
        f"Отправь в формате:\n"
        f"<code>/setprice sim 700</code>\n"
        f"<code>/setprice ai 700</code>\n"
        f"<code>/setprice both 1200</code>",
        reply_markup=_back_kb(),
    )
    await callback.answer()


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /setprice <sim|ai|both> <цена>")
        return
    _, type_, price_str = parts
    if type_ not in ("sim", "ai", "both"):
        await message.answer("Тип: sim | ai | both")
        return
    try:
        price = int(price_str)
        await db.set_setting(f"price_{type_}", str(price))
        await message.answer(f"✅ Цена '{type_}' обновлена: {price} ₸")
    except ValueError:
        await message.answer("Цена должна быть числом.")


# ── Настройки Turnitin ────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_turnitin_cfg")
async def adm_turnitin_cfg(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    email    = await db.get_setting("turnitin_email") or "не задан"
    class_id = await db.get_setting("turnitin_class_id") or "не задан"
    assign   = await db.get_setting("turnitin_assign_id") or "не задан"
    class_p  = await db.get_setting("turnitin_class_id_premium") or "не задан"
    assign_p = await db.get_setting("turnitin_assign_id_premium") or "не задан"
    mult     = await db.get_setting("premium_multiplier") or "1.5"
    await callback.message.edit_text(
        f"⚙️ <b>Настройки Turnitin</b>\n\n"
        f"Email: <code>{email}</code>\n"
        f"🐢 Class ID: <code>{class_id}</code>\n"
        f"🐢 Assignment ID: <code>{assign}</code>\n"
        f"⚡ Premium Class ID: <code>{class_p}</code>\n"
        f"⚡ Premium Assignment ID: <code>{assign_p}</code>\n"
        f"Множитель премиум: <b>×{mult}</b>\n\n"
        f"Изменить через команды:\n"
        f"<code>/setturnitin email your@email.com</code>\n"
        f"<code>/setturnitin password your_pass</code>\n"
        f"<code>/setturnitin class_id 12345</code>\n"
        f"<code>/setturnitin assign_id 67890</code>\n"
        f"<code>/setturnitin class_id_premium 12345</code>\n"
        f"<code>/setturnitin assign_id_premium 67890</code>\n"
        f"<code>/setturnitin premium_mult 1.5</code>",
        reply_markup=_back_kb(),
    )
    await callback.answer()


@router.message(Command("setturnitin"))
async def cmd_setturnitin(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: /setturnitin <ключ> <значение>")
        return
    _, key, value = parts
    valid_keys = {"email": "turnitin_email", "password": "turnitin_password",
                  "class_id": "turnitin_class_id", "assign_id": "turnitin_assign_id",
                  "class_id_premium": "turnitin_class_id_premium",
                  "assign_id_premium": "turnitin_assign_id_premium",
                  "premium_mult": "premium_multiplier"}
    if key not in valid_keys:
        await message.answer(f"Ключи: {', '.join(valid_keys.keys())}")
        return
    await db.set_setting(valid_keys[key], value)
    await message.answer(f"✅ Настройка '{key}' обновлена.")


@router.message(Command("sethelp"))
async def cmd_sethelp(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: /sethelp <username|phone> <значение>")
        return
    _, key, value = parts
    valid = {"username": "help_username", "phone": "help_phone",
             "kaspi_phone": "kaspi_phone", "kaspi_name": "kaspi_recipient_name"}
    if key not in valid:
        await message.answer(f"Ключи: {', '.join(valid.keys())}")
        return
    await db.set_setting(valid[key], value)
    await message.answer(f"✅ '{key}' обновлено: {value}")
