"""
Админ-панель: /admin
  - Статистика
  - Смена цен (из БД)
  - Управление админами (добавить/удалить)
  - Смена логина/пароля Turnitin
  - Управление чеками (список, добавить, удалить)
"""

import logging

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from config import config
from database import Database
from utils.keyboards import (
    admin_main, admin_prices_keyboard, admin_receipts_keyboard,
)
from utils.states import AdminFlow

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ── Entry ────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔️ Нет доступа")
        return
    await state.clear()
    await msg.answer("🛠 <b>Панель администратора</b>", reply_markup=admin_main())


@router.callback_query(F.data == "adm:back")
async def adm_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=admin_main())
    await cb.answer()


# ── Stats ────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)

    s = await db.get_stats()
    by_status  = s["by_status"]
    by_type    = s["by_type"]
    total_done = s["total_done"]
    total_rcpt = s["total_receipts"]

    lines = [
        "📊 <b>Статистика</b>\n",
        f"✔️ Завершено: <b>{total_done}</b>",
        f"🗃 Чеков в БД: <b>{total_rcpt}</b>",
        "",
        "<b>По статусам:</b>",
    ]
    for st, cnt in by_status.items():
        lines.append(f"  • {st}: {cnt}")
    lines.append("\n<b>Завершённые по типу:</b>")
    for t, cnt in by_type.items():
        lines.append(f"  • {t}: {cnt}")

    await cb.message.edit_text("\n".join(lines), reply_markup=admin_main())
    await cb.answer()


# ── Prices ───────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:prices")
async def adm_prices(cb: CallbackQuery, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    prices = await db.get_prices()
    cur = prices.get("price_currency", "тг")
    await cb.message.edit_text(
        f"💰 <b>Текущие цены</b>\n\n"
        f"AI-детекция: <b>{prices.get('price_ai')} {cur}</b>\n"
        f"Плагиат: <b>{prices.get('price_similarity')} {cur}</b>\n"
        f"AI+Плагиат: <b>{prices.get('price_both')} {cur}</b>\n\n"
        "Выберите что изменить:",
        reply_markup=admin_prices_keyboard()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:price:"))
async def adm_price_choose(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    key = cb.data.split(":")[2]
    await state.set_state(AdminFlow.waiting_price_value)
    await state.update_data(price_key=key)
    label = {"ai": "AI-детекция", "similarity": "Плагиат",
             "both": "AI+Плагиат", "currency": "Валюта (например тг, ₸, KZT)"}
    await cb.message.edit_text(
        f"✏️ Введите новое значение для <b>{label.get(key, key)}</b>:"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_price_value)
async def adm_price_save(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return
    data = await state.get_data()
    key  = data.get("price_key")
    val  = msg.text.strip()
    if key != "currency" and not val.isdigit():
        await msg.answer("❌ Введите число")
        return
    await db.set_setting(f"price_{key}", val)
    await state.clear()
    await msg.answer(f"✅ Цена обновлена: <b>{key}</b> → <b>{val}</b>", reply_markup=admin_main())


# ── Admins management ─────────────────────────────────────────────

@router.callback_query(F.data == "adm:admins")
async def adm_admins(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)

    current = ", ".join(str(i) for i in config.ADMIN_IDS) or "—"
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить админа", callback_data="adm:admins:add")
    kb.button(text="➖ Удалить админа", callback_data="adm:admins:del")
    kb.button(text="◀️ Назад", callback_data="adm:back")
    kb.adjust(1)

    await cb.message.edit_text(
        f"👥 <b>Управление администраторами</b>\n\n"
        f"Текущие админы (ID):\n<code>{current}</code>",
        reply_markup=kb.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data == "adm:admins:add")
async def adm_admins_add_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_admin_add)
    await cb.message.edit_text(
        "➕ Введите Telegram ID нового администратора:\n\n"
        "<i>Узнать ID можно через @userinfobot</i>"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_admin_add)
async def adm_admins_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    val = msg.text.strip()
    if not val.isdigit():
        await msg.answer("❌ Введите числовой Telegram ID")
        return
    new_id = int(val)
    if new_id in config.ADMIN_IDS:
        await msg.answer(f"⚠️ ID <code>{new_id}</code> уже является админом", reply_markup=admin_main())
    else:
        config.ADMIN_IDS.append(new_id)
        logger.info("Admin added: %s", new_id)
        await msg.answer(f"✅ Пользователь <code>{new_id}</code> добавлен как администратор", reply_markup=admin_main())
    await state.clear()


@router.callback_query(F.data == "adm:admins:del")
async def adm_admins_del_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    current = ", ".join(str(i) for i in config.ADMIN_IDS) or "—"
    await state.set_state(AdminFlow.waiting_admin_del)
    await cb.message.edit_text(
        f"➖ Введите Telegram ID для удаления из администраторов:\n\n"
        f"Текущие: <code>{current}</code>"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_admin_del)
async def adm_admins_del(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    val = msg.text.strip()
    if not val.isdigit():
        await msg.answer("❌ Введите числовой Telegram ID")
        return
    del_id = int(val)
    if del_id not in config.ADMIN_IDS:
        await msg.answer(f"❌ ID <code>{del_id}</code> не найден в списке админов", reply_markup=admin_main())
    elif len(config.ADMIN_IDS) <= 1:
        await msg.answer("⚠️ Нельзя удалить последнего администратора", reply_markup=admin_main())
    elif del_id == msg.from_user.id:
        await msg.answer("⚠️ Нельзя удалить самого себя", reply_markup=admin_main())
    else:
        config.ADMIN_IDS.remove(del_id)
        logger.info("Admin removed: %s", del_id)
        await msg.answer(f"✅ Пользователь <code>{del_id}</code> удалён из администраторов", reply_markup=admin_main())
    await state.clear()


# ── Turnitin credentials ─────────────────────────────────────────

@router.callback_query(F.data == "adm:creds")
async def adm_creds(cb: CallbackQuery, state: FSMContext, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    email = await db.get_setting("turnitin_email")
    cid   = await db.get_setting("turnitin_class_id")
    aid   = await db.get_setting("turnitin_assign_id")
    await state.set_state(AdminFlow.waiting_turnitin_cred)
    await cb.message.edit_text(
        f"🔑 <b>Turnitin credentials</b>\n\n"
        f"Email: <code>{email}</code>\n"
        f"Class ID: <code>{cid}</code>\n"
        f"Assignment ID: <code>{aid}</code>\n\n"
        "Отправьте новые данные в формате:\n"
        "<code>email пароль class_id assignment_id</code>\n\n"
        "Пример:\n"
        "<code>test@mail.com MyPass123 1234567 9876543</code>"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_turnitin_cred)
async def adm_creds_save(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        await msg.answer("❌ Нужно минимум: email пароль [class_id] [assign_id]")
        return
    await db.set_setting("turnitin_email", parts[0])
    await db.set_setting("turnitin_password", parts[1])
    if len(parts) >= 3:
        await db.set_setting("turnitin_class_id", parts[2])
    if len(parts) >= 4:
        await db.set_setting("turnitin_assign_id", parts[3])
    await state.clear()
    await msg.answer("✅ Данные Turnitin обновлены!", reply_markup=admin_main())


# ── Receipts management ──────────────────────────────────────────

@router.callback_query(F.data == "adm:receipts")
async def adm_receipts(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.clear()
    await cb.message.edit_text("🗃 <b>Управление чеками</b>", reply_markup=admin_receipts_keyboard())
    await cb.answer()


@router.callback_query(F.data == "adm:receipts:list")
async def adm_receipts_list(cb: CallbackQuery, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    rows = await db.list_receipts(50)
    if not rows:
        await cb.answer("База чеков пуста", show_alert=True)
        return
    lines = ["🗃 <b>Последние чеки (50):</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['receipt_id']}</code> — user {r['user_id']} — order #{r['order_id']}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await cb.message.edit_text(text, reply_markup=admin_receipts_keyboard())
    await cb.answer()


@router.callback_query(F.data == "adm:receipts:del")
async def adm_receipts_del_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_receipt_del)
    await cb.message.edit_text("🗑 Введите ID чека для удаления:")
    await cb.answer()


@router.message(AdminFlow.waiting_receipt_del)
async def adm_receipts_del(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return
    rid = msg.text.strip()
    deleted = await db.delete_receipt(rid)
    await state.clear()
    if deleted:
        await msg.answer(f"✅ Чек <code>{rid}</code> удалён", reply_markup=admin_main())
    else:
        await msg.answer(f"❌ Чек <code>{rid}</code> не найден", reply_markup=admin_main())


@router.callback_query(F.data == "adm:receipts:add")
async def adm_receipts_add_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_receipt_add)
    await cb.message.edit_text("➕ Введите ID чека для добавления вручную:")
    await cb.answer()


@router.message(AdminFlow.waiting_receipt_add)
async def adm_receipts_add(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return
    rid = msg.text.strip()
    if await db.receipt_exists(rid):
        await msg.answer(f"⚠️ Чек <code>{rid}</code> уже существует", reply_markup=admin_main())
    else:
        await db.add_receipt(rid, user_id=0, order_id=0)
        await msg.answer(f"✅ Чек <code>{rid}</code> добавлен", reply_markup=admin_main())
    await state.clear()


# ── Cleanup ──────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:cleanup")
async def adm_cleanup(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)

    import subprocess
    import gc

    await cb.answer("⏳ Очищаю...")
    await cb.message.edit_text("🧹 <b>Очистка памяти...</b>")

    killed = 0
    errors = []

    # 1. Kill zombie chromium processes (не текущий)
    try:
        result = subprocess.run(
            ["pkill", "-f", "chromium"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            killed += 1
    except Exception as e:
        errors.append(f"pkill: {e}")

    # 2. Kill zombie Xvfb-related orphans
    try:
        subprocess.run(["pkill", "-f", "chrome-linux"], capture_output=True)
    except Exception:
        pass

    # 3. GC
    gc.collect()

    # 4. Показываем статистику памяти
    try:
        mem = subprocess.run(
            ["cat", "/proc/meminfo"],
            capture_output=True, text=True
        ).stdout
        mem_lines = {line.split(":")[0]: line.split(":")[1].strip()
                     for line in mem.strip().split("\n") if ":" in line}
        mem_total = mem_lines.get("MemTotal", "?")
        mem_free = mem_lines.get("MemAvailable", "?")
        mem_info = f"💾 Памяти доступно: <b>{mem_free}</b> из <b>{mem_total}</b>"
    except Exception:
        mem_info = ""

    status = "✅ Готово" if not errors else f"⚠️ Частично: {'; '.join(errors)}"
    text = (
        f"🧹 <b>Очистка памяти выполнена</b>\n\n"
        f"{status}\n"
        f"{mem_info}\n\n"
        f"<i>Если браузер сейчас обрабатывает заказ — он не будет затронут</i>"
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="adm:back")

    await cb.message.edit_text(text, reply_markup=kb.as_markup())


# ── Kaspi настройки ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:kaspi")
async def adm_kaspi(cb: CallbackQuery, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)

    link    = await db.get_setting("kaspi_link")           or "не задана"
    expire  = await db.get_setting("kaspi_expire_minutes") or "60"

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Изменить ссылку Kaspi",     callback_data="adm:kaspi:link")
    kb.button(text="⏱ Изменить таймаут чека",      callback_data="adm:kaspi:expire")
    kb.button(text="◀️ Назад",                      callback_data="adm:back")
    kb.adjust(1)

    await cb.message.edit_text(
        f"💳 <b>Настройки Kaspi</b>\n\n"
        f"🔗 Ссылка для оплаты:\n<code>{link}</code>\n\n"
        f"⏱ Таймаут чека: <b>{expire} мин</b>\n\n"
        f"<i>QR-код берётся из файла assets/kaspi_qr.jpg</i>",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data == "adm:kaspi:link")
async def adm_kaspi_link(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_kaspi_setting)
    await state.update_data(kaspi_key="kaspi_link")
    await cb.message.edit_text(
        "🔗 Введите новую ссылку Kaspi QR:\n\n"
        "<i>Пример: https://qr.kaspi.kz/11111248499363881...</i>"
    )
    await cb.answer()


@router.callback_query(F.data == "adm:kaspi:expire")
async def adm_kaspi_expire(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_kaspi_setting)
    await state.update_data(kaspi_key="kaspi_expire_minutes")
    await cb.message.edit_text(
        "⏱ Введите таймаут чека в минутах:\n\n"
        "<i>Например: 60 (чек принимается не старше 60 минут)</i>"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_kaspi_setting)
async def adm_kaspi_save(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return
    data      = await state.get_data()
    kaspi_key = data.get("kaspi_key", "kaspi_link")
    val       = msg.text.strip()

    if kaspi_key == "kaspi_expire_minutes":
        if not val.isdigit() or int(val) < 1:
            await msg.answer("⚠️ Введите положительное число (минуты).")
            return

    await db.set_setting(kaspi_key, val)
    await state.clear()

    label = "ссылка" if kaspi_key == "kaspi_link" else "таймаут"
    await msg.answer(
        f"✅ Kaspi {label} обновлён: <code>{val}</code>",
        reply_markup=admin_main()
    )


# ── Бесплатный доступ (free users) ───────────────────────────────────────────

async def _get_free_users(db: Database) -> list[str]:
    raw = await db.get_setting("free_users") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


@router.callback_query(F.data == "adm:free_users")
async def adm_free_users(cb: CallbackQuery, db: Database):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)

    users = await _get_free_users(db)
    lines = "\n".join(f"• <code>{u}</code>" for u in users) if users else "<i>список пуст</i>"

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить",  callback_data="adm:free_users:add")
    kb.button(text="➖ Удалить",   callback_data="adm:free_users:del")
    kb.button(text="◀️ Назад",    callback_data="adm:back")
    kb.adjust(2, 1)

    await cb.message.edit_text(
        f"🆓 <b>Бесплатный доступ</b>\n\n"
        f"Эти пользователи проходят проверку без оплаты:\n{lines}\n\n"
        f"<i>Админы из .env всегда в списке автоматически.</i>",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data == "adm:free_users:add")
async def adm_free_users_add_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_free_users)
    await state.update_data(free_users_action="add")
    await cb.message.edit_text(
        "➕ Введите Telegram ID пользователя которому дать бесплатный доступ:\n\n"
        "<i>ID можно узнать через @userinfobot</i>"
    )
    await cb.answer()


@router.callback_query(F.data == "adm:free_users:del")
async def adm_free_users_del_prompt(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔️", show_alert=True)
    await state.set_state(AdminFlow.waiting_free_users)
    await state.update_data(free_users_action="del")
    await cb.message.edit_text(
        "➖ Введите Telegram ID пользователя которого убрать из бесплатного доступа:"
    )
    await cb.answer()


@router.message(AdminFlow.waiting_free_users)
async def adm_free_users_save(msg: Message, state: FSMContext, db: Database):
    if not is_admin(msg.from_user.id):
        return

    uid = msg.text.strip()
    if not uid.lstrip("-").isdigit():
        await msg.answer("⚠️ Введите числовой Telegram ID.")
        return

    data   = await state.get_data()
    action = data.get("free_users_action", "add")
    users  = await _get_free_users(db)

    if action == "add":
        if uid not in users:
            users.append(uid)
        result_text = f"✅ Пользователь <code>{uid}</code> добавлен в бесплатный доступ."
    else:
        if uid in users:
            users.remove(uid)
            result_text = f"✅ Пользователь <code>{uid}</code> удалён из бесплатного доступа."
        else:
            result_text = f"⚠️ Пользователь <code>{uid}</code> не найден в списке."

    await db.set_setting("free_users", ",".join(users))
    await state.clear()
    await msg.answer(result_text, reply_markup=admin_main())