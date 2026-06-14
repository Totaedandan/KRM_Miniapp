"""
FastAPI — бэкенд для Telegram Mini App.

Пользовательские эндпоинты:
  GET  /                     — отдаёт mini_app/index.html
  GET  /api/me               — данные пользователя (авторизация через initData)
  GET  /api/prices           — текущие цены Turnitin
  GET  /api/orders           — история заказов пользователя
  GET  /api/packages         — пакеты токенов
  POST /api/humanize         — хуманизировать текст (списывает токены)

Админские эндпоинты (только superadmin):
  GET  /api/admin/stats
  GET  /api/admin/orders
  GET  /api/admin/user?q=
  POST /api/admin/give_tokens
  GET  /api/admin/whitelist
  POST /api/admin/whitelist/add
  POST /api/admin/whitelist/remove
  GET  /api/admin/banlist
  POST /api/admin/ban
  POST /api/admin/unban
  POST /api/admin/prices
  GET  /api/admin/settings
  POST /api/admin/settings
  POST /api/admin/cleanup
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from database import db as database
from bot_sender import send_message as _bot_send

logger = logging.getLogger(__name__)

app = FastAPI(title="Turnitin Bot Mini App API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MINI_APP_DIR = Path(__file__).parent.parent / "mini_app"


# ── Авторизация ───────────────────────────────────────────────────────────────

def _validate_init_data(init_data: str) -> dict:
    parsed = {}
    for part in init_data.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            parsed[k] = unquote(v)

    hash_value = parsed.pop("hash", "")
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed   = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, hash_value):
        raise ValueError("Invalid initData hash")

    return json.loads(parsed.get("user", "{}"))


async def _get_user(x_telegram_init_data: str = Header(None)) -> dict:
    if not x_telegram_init_data:
        raise HTTPException(401, "Missing X-Telegram-Init-Data header")
    try:
        tg_user = _validate_init_data(x_telegram_init_data)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(401, f"Invalid initData: {e}")

    user = await database.get_or_create_user(
        tg_id=tg_user["id"],
        username=tg_user.get("username"),
        full_name=f"{tg_user.get('first_name','')} {tg_user.get('last_name','')}".strip(),
    )
    if user.get("is_banned"):
        raise HTTPException(403, "User is banned")
    # Админы проходят флоу бесплатно — считаем их как whitelist
    if settings.is_admin(user["tg_id"]):
        user["is_whitelisted"] = 1
    return user


async def _get_admin(x_telegram_init_data: str = Header(None)) -> dict:
    """Админы из ADMIN_IDS + суперадмин."""
    user = await _get_user(x_telegram_init_data)
    if not settings.is_admin(user["tg_id"]):
        raise HTTPException(403, "Admin only")
    return user


# ── Статика ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_mini_app():
    html = MINI_APP_DIR / "index.html"
    if html.exists():
        return FileResponse(html)
    return JSONResponse({"error": "mini_app/index.html not found"}, status_code=404)


# ── Пользовательское API ──────────────────────────────────────────────────────

@app.get("/api/me")
async def get_me(x_telegram_init_data: str = Header(None)):
    user     = await _get_user(x_telegram_init_data)
    prices   = await database.get_prices()
    packages = settings.get_humanizer_packages()
    return {
        "tg_id":           user["tg_id"],
        "username":        user.get("username"),
        "full_name":       user.get("full_name"),
        "tenge_balance":   round(user.get("tenge_balance", 0.0), 2),
        "token_balance":   round(user.get("token_balance", 0.0), 2),
        "is_whitelisted":  bool(user.get("is_whitelisted")),
        "is_admin":        settings.is_admin(user["tg_id"]),
        "prices":          prices,
        "premium_multiplier": await database.get_premium_multiplier(),
        "packages":        packages,
    }


@app.get("/api/orders")
async def get_orders(x_telegram_init_data: str = Header(None)):
    user   = await _get_user(x_telegram_init_data)
    orders = await database.get_user_orders(user["tg_id"], limit=20)
    return {"orders": orders}


@app.get("/api/prices")
async def get_prices():
    return await database.get_prices()


@app.get("/api/packages")
async def get_packages():
    return {"packages": settings.get_humanizer_packages()}


# ── Извлечение текста из docx (для Хуманайзера в Mini App) ───────────────────

from fastapi import UploadFile, File

@app.post("/api/extract_text")
async def extract_text(file: UploadFile = File(...), x_telegram_init_data: str = Header(None)):
    await _get_user(x_telegram_init_data)
    fname = (file.filename or "").lower()
    data  = await file.read()
    text  = ""
    try:
        if fname.endswith(".txt"):
            text = data.decode("utf-8", errors="ignore")
        elif fname.endswith(".docx"):
            import docx, io
            doc  = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        elif fname.endswith(".doc"):
            # Простой fallback — попытаться прочитать как текст
            text = data.decode("utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать файл: {e}")
    return {"text": text, "words": len(text.split())}


# ── Хуманайзер ───────────────────────────────────────────────────────────────

class HumanizeRequest(BaseModel):
    text:     str
    tone:     str  = "College"
    mode:     str  = "Medium"
    business: bool = False


@app.post("/api/humanize")
async def humanize(body: HumanizeRequest, x_telegram_init_data: str = Header(None)):
    user = await _get_user(x_telegram_init_data)

    word_count = len(body.text.split())
    if word_count < 10:
        raise HTTPException(400, "Текст слишком короткий (минимум 10 слов)")
    if word_count > 20000:
        raise HTTPException(400, "Текст слишком длинный (максимум 20 000 слов)")

    from services.humanizer_service import humanize_text, calculate_humanizer_cost

    cost  = calculate_humanizer_cost(body.text, body.business)
    is_wl = bool(user.get("is_whitelisted"))

    if not is_wl:
        balance = await database.get_token_balance(user["tg_id"])
        if balance < cost:
            raise HTTPException(402, f"Недостаточно токенов. Нужно {cost:.1f}, есть {balance:.1f}")

    try:
        result, _ = await humanize_text(body.text, body.tone, body.mode, body.business)
    except Exception as e:
        logger.error(f"Humanizer API error: {e}")
        raise HTTPException(500, str(e)[:300])

    if not is_wl:
        await database.deduct_tokens(user["tg_id"], cost, reason="humanizer")

    new_balance = await database.get_token_balance(user["tg_id"])
    return {
        "result":       result,
        "words_input":  word_count,
        "words_output": len(result.split()),
        "tokens_spent": cost if not is_wl else 0,
        "balance":      round(new_balance, 2),
    }


# ── ADMIN API ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/stats")
async def admin_stats(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return await database.get_stats()


@app.get("/api/admin/orders")
async def admin_orders(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    orders = await database.get_pending_orders(limit=50)
    return {"orders": orders}


@app.get("/api/admin/queue")
async def admin_queue(x_telegram_init_data: str = Header(None)):
    """Срез двух очередей (премиум + обычная) с позицией, ETA и суммой."""
    await _get_admin(x_telegram_init_data)
    from services.queue_manager import turnitin_queue
    return await turnitin_queue.queue_snapshot()


class CancelOrderBody(BaseModel):
    order_id: int


@app.post("/api/admin/cancel_order")
async def admin_cancel_order(body: CancelOrderBody, x_telegram_init_data: str = Header(None)):
    """Отменить заказ по id с возвратом денег на баланс пользователя."""
    await _get_admin(x_telegram_init_data)
    from services.queue_manager import turnitin_queue
    result = await turnitin_queue.cancel_order(body.order_id, reason="cancelled_by_admin")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Не удалось отменить заказ"))
    return result


@app.get("/api/admin/user")
async def admin_find_user(q: str, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    user = await database.find_user(q.strip())
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return user


class GiveTokensBody(BaseModel):
    tg_id:  int
    amount: float


@app.post("/api/admin/give_tokens")
async def admin_give_tokens(body: GiveTokensBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    new_balance = await database.add_tokens(body.tg_id, body.amount, reason="admin_adjust")
    return {"new_balance": round(new_balance, 2)}


# -- Whitelist ----------------------------------------------------------------

@app.get("/api/admin/whitelist")
async def admin_whitelist(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    users = await database.get_whitelist_users()
    return {"users": users}


class WlBody(BaseModel):
    tg_id: int


@app.post("/api/admin/whitelist/add")
async def admin_wl_add(body: WlBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.set_whitelist(body.tg_id, True)
    if not ok:
        raise HTTPException(404, "Пользователь не найден в БД")
    return {"ok": True}


@app.post("/api/admin/whitelist/remove")
async def admin_wl_remove(body: WlBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.set_whitelist(body.tg_id, False)
    if not ok:
        raise HTTPException(404, "Пользователь не найден в БД")
    return {"ok": True}


# -- Banlist ------------------------------------------------------------------

@app.get("/api/admin/banlist")
async def admin_banlist(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return await database.get_banned_users()


class BanByIdBody(BaseModel):
    tg_id: int


class BanByUsernameBody(BaseModel):
    username: str
    reason:   Optional[str] = ""


@app.post("/api/admin/ban")
async def admin_ban(body: BanByIdBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    await database.set_banned(body.tg_id, True)
    return {"ok": True}


@app.post("/api/admin/unban")
async def admin_unban(body: BanByIdBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    await database.unban_user(body.tg_id)
    return {"ok": True}


@app.post("/api/admin/ban_username")
async def admin_ban_username(body: BanByUsernameBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    inserted = await database.ban_username(body.username, body.reason or "")
    return {"ok": True, "inserted": inserted}


@app.post("/api/admin/unban_username")
async def admin_unban_username(body: BanByUsernameBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    deleted = await database.unban_username(body.username)
    return {"ok": True, "deleted": deleted}


# -- Prices -------------------------------------------------------------------

class PricesBody(BaseModel):
    price_sim:  Optional[int] = None
    price_ai:   Optional[int] = None
    price_both: Optional[int] = None


@app.post("/api/admin/prices")
async def admin_set_prices(body: PricesBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.price_sim  is not None: await database.set_setting("price_sim",  str(body.price_sim))
    if body.price_ai   is not None: await database.set_setting("price_ai",   str(body.price_ai))
    if body.price_both is not None: await database.set_setting("price_both", str(body.price_both))
    return await database.get_prices()


# -- Settings -----------------------------------------------------------------

EDITABLE_SETTINGS = {
    "turnitin_email", "turnitin_password", "turnitin_class_id", "turnitin_assign_id",
    "turnitin_class_id_premium", "turnitin_assign_id_premium", "premium_multiplier",
    "kaspi_phone", "kaspi_recipient_name",
    "help_username", "help_phone",
}


@app.get("/api/admin/settings")
async def admin_get_settings(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    result = {}
    for key in EDITABLE_SETTINGS:
        result[key] = await database.get_setting(key) or ""
    return result


class SettingBody(BaseModel):
    key:   str
    value: str


@app.post("/api/admin/settings")
async def admin_set_setting(body: SettingBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.key not in EDITABLE_SETTINGS:
        raise HTTPException(400, f"Недопустимый ключ: {body.key}")
    await database.set_setting(body.key, body.value)
    return {"ok": True, "key": body.key, "value": body.value}


# ── Mini App Actions (замена tg.sendData) ─────────────────────────────────────

_TYPE_LABEL = {"sim": "📊 Плагиат", "ai": "🤖 AI-детекция", "both": "✨ Оба отчёта"}


class OrderBody(BaseModel):
    report_type: str = "both"
    is_premium:  bool = False
    request_id:  Optional[str] = None   # ключ идемпотентности (от Mini App)


@app.post("/api/order")
async def create_order(body: OrderBody, x_telegram_init_data: str = Header(None)):
    """Создать заказ Turnitin — списать тенге, забронировать место в очереди."""
    from services.queue_manager import turnitin_queue
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]

    prices = await database.get_prices()
    base_map = {
        "sim":  int(prices.get("price_sim",  700)),
        "ai":   int(prices.get("price_ai",   700)),
        "both": int(prices.get("price_both", 1200)),
    }
    base = base_map.get(body.report_type, 700)
    mult = await database.get_premium_multiplier()
    price = int(round(base * mult)) if body.is_premium else base
    is_wl = bool(user.get("is_whitelisted"))

    if is_wl:
        # Whitelist — без денег и без ledger
        order_id = await database.create_order(
            user_id=tg_id,
            username=user.get("username"),
            report_type=body.report_type,
            payment_method="whitelist",
            amount_tenge=0,
            is_premium=body.is_premium,
            status="queued",
        )
        new_balance = await database.get_tenge_balance(tg_id)
    else:
        # Платно: атомарно списать + создать заказ + записать в ledger.
        # request_id из Mini App защищает от двойного списания (двойной тап/ретрай).
        idem = f"order:{tg_id}:{body.request_id}" if body.request_id else None
        try:
            res = await database.create_paid_order(
                user_id=tg_id,
                username=user.get("username"),
                report_type=body.report_type,
                price=price,
                is_premium=body.is_premium,
                idempotency_key=idem,
            )
        except database.InsufficientFunds:
            balance = await database.get_tenge_balance(tg_id)
            raise HTTPException(402, f"Недостаточно средств. Нужно {price} ₸, есть {balance:.0f} ₸")

        order_id = res["order_id"]
        new_balance = res["balance"]
        if res.get("duplicate"):
            # Повторный запрос — заказ уже создан, второй раз не списываем и не дёргаем очередь
            return {
                "ok": True,
                "order_id": order_id,
                "is_premium": body.is_premium,
                "price": price,
                "balance": round(new_balance, 2),
                "duplicate": True,
            }

    # Контроллер очереди сам уведомит и запросит файл, когда подойдёт очередь
    await turnitin_queue.on_reservation(order_id)

    return {
        "ok": True,
        "order_id": order_id,
        "is_premium": body.is_premium,
        "price": price,
        "balance": round(new_balance, 2),
    }


class TopupBody(BaseModel):
    amount: float


@app.post("/api/topup")
async def init_topup(body: TopupBody, x_telegram_init_data: str = Header(None)):
    """Инициализировать пополнение тенге через Kaspi."""
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]

    if body.amount < 100:
        raise HTTPException(400, "Минимальная сумма пополнения — 100 ₸")

    payment_id = await database.create_payment(
        user_id=tg_id,
        payment_type="kaspi",
        purpose="topup_tenge",
        amount_tenge=body.amount,
    )
    await database.set_pending_action(
        tg_id, "waiting_topup_pdf",
        {"payment_id": payment_id, "amount": body.amount},
    )

    phone = await database.get_setting("kaspi_phone") or settings.KASPI_PHONE
    name  = await database.get_setting("kaspi_recipient_name") or settings.KASPI_RECIPIENT_NAME

    await _bot_send(tg_id,
        f"💳 <b>Пополнение баланса — {body.amount:.0f} ₸</b>\n\n"
        f"Получатель: <b>{name}</b>\n"
        f"Телефон: <b>{phone}</b>\n"
        f"Сумма: <b>{body.amount:.0f} ₸</b>\n\n"
        f"Переведите <b>точную сумму</b> и отправьте PDF-чек из Kaspi в <b>этот чат</b>."
    )

    return {
        "ok": True,
        "payment_id": payment_id,
        "kaspi_phone": phone,
        "kaspi_name": name,
        "amount": body.amount,
    }


class BuyTokensApiBody(BaseModel):
    tokens: float
    tenge: float


@app.post("/api/buy_tokens")
async def buy_tokens_api(body: BuyTokensApiBody, x_telegram_init_data: str = Header(None)):
    """Купить токены из тенге-баланса мгновенно."""
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]

    if body.tokens <= 0 or body.tenge <= 0:
        raise HTTPException(400, "Неверные данные пакета")

    is_wl = bool(user.get("is_whitelisted"))
    if not is_wl:
        balance = await database.get_tenge_balance(tg_id)
        if balance < body.tenge:
            raise HTTPException(402, f"Недостаточно средств. Нужно {body.tenge:.0f} ₸, есть {balance:.0f} ₸")
        await database.deduct_tenge(tg_id, body.tenge, reason="token_purchase")

    new_tokens = await database.add_tokens(tg_id, body.tokens, reason="token_purchase")
    new_tenge  = await database.get_tenge_balance(tg_id)

    await database.create_payment(
        user_id=tg_id,
        payment_type="balance",
        purpose="tokens",
        amount_tenge=body.tenge,
        tokens_amount=body.tokens,
    )

    return {
        "ok": True,
        "new_token_balance": round(new_tokens, 2),
        "new_tenge_balance": round(new_tenge, 2),
    }


# ── Аренда ИИ-аккаунтов ──────────────────────────────────────────────────────

@app.get("/api/rental/services")
async def rental_services(x_telegram_init_data: str = Header(None)):
    """Каталог сервисов аренды: тарифы, наличие, размер waitlist. Без кредов."""
    await _get_user(x_telegram_init_data)
    return {"services": await database.get_rental_services_catalog()}


@app.get("/api/rental/my")
async def rental_my(x_telegram_init_data: str = Header(None)):
    """Аренды пользователя: активные с кредами + недавняя история без."""
    user = await _get_user(x_telegram_init_data)
    return {"rentals": await database.get_user_rentals(user["tg_id"])}


class RentalOrderBody(BaseModel):
    service_id: int
    tariff_id:  int
    request_id: Optional[str] = None   # ключ идемпотентности (от Mini App)


@app.post("/api/rental/order")
async def rental_order(body: RentalOrderBody, x_telegram_init_data: str = Header(None)):
    """Арендовать аккаунт: атомарно списать тенге + выдать свободный аккаунт.

    Whitelist-бесплатно здесь НЕТ: инвентарь конечен, платят все.
    """
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]
    idem = f"rental:{tg_id}:{body.request_id}" if body.request_id else None

    try:
        res = await database.create_rental_order(
            user_id=tg_id,
            username=user.get("username"),
            service_id=body.service_id,
            tariff_id=body.tariff_id,
            idempotency_key=idem,
        )
    except database.InsufficientFunds:
        balance = await database.get_tenge_balance(tg_id)
        raise HTTPException(402, f"Недостаточно средств. Пополните баланс (есть {balance:.0f} ₸)")
    except database.NoFreeAccount:
        raise HTTPException(409, "Аккаунты закончились — нажмите «Уведомить», и мы напишем, когда освободится")

    res["balance"] = round(res.get("balance", 0), 2)
    return res


class RentalNotifyBody(BaseModel):
    service_id: int


@app.post("/api/rental/notify")
async def rental_notify(body: RentalNotifyBody, x_telegram_init_data: str = Header(None)):
    """Подписаться на уведомление «когда освободится» (идемпотентно)."""
    user = await _get_user(x_telegram_init_data)
    svc = await database.get_rental_service(body.service_id)
    if not svc or not svc.get("is_active"):
        raise HTTPException(404, "Сервис не найден")
    await database.add_to_waitlist(body.service_id, user["tg_id"])
    return {"ok": True, "subscribed": True}


# -- Аренда: админ --------------------------------------------------------------

@app.get("/api/admin/rental/services")
async def admin_rental_services(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"services": await database.get_rental_services_admin()}


class RentalServiceBody(BaseModel):
    id:          Optional[int] = None
    name:        str
    description: Optional[str] = ""
    icon:        Optional[str] = ""
    is_active:   bool = True
    sort_order:  int = 0


@app.post("/api/admin/rental/service")
async def admin_rental_service(body: RentalServiceBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if not body.name.strip():
        raise HTTPException(400, "Название обязательно")
    sid = await database.upsert_rental_service(
        name=body.name.strip(), description=body.description or "",
        icon=body.icon or "", is_active=body.is_active,
        sort_order=body.sort_order, service_id=body.id,
    )
    return {"ok": True, "id": sid}


class RentalTariffBody(BaseModel):
    id:             Optional[int] = None
    service_id:     int
    name:           str
    duration_hours: int
    price:          float
    is_active:      bool = True
    sort_order:     int = 0


@app.post("/api/admin/rental/tariff")
async def admin_rental_tariff(body: RentalTariffBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.duration_hours <= 0 or body.price <= 0:
        raise HTTPException(400, "Длительность и цена должны быть > 0")
    tid = await database.upsert_rental_tariff(
        service_id=body.service_id, name=body.name.strip(),
        duration_hours=body.duration_hours, price=body.price,
        is_active=body.is_active, sort_order=body.sort_order,
        tariff_id=body.id,
    )
    return {"ok": True, "id": tid}


@app.get("/api/admin/rental/accounts")
async def admin_rental_accounts(service_id: Optional[int] = None,
                                x_telegram_init_data: str = Header(None)):
    """Склад аккаунтов (с кредами — только для админа)."""
    await _get_admin(x_telegram_init_data)
    return {"accounts": await database.get_rental_accounts(service_id)}


class RentalAccountBody(BaseModel):
    service_id: int
    login:      str
    password:   str
    note:       Optional[str] = ""


@app.post("/api/admin/rental/account")
async def admin_rental_account_add(body: RentalAccountBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if not body.login.strip() or not body.password:
        raise HTTPException(400, "Логин и пароль обязательны")
    had_free = await database.count_free_accounts(body.service_id)
    acc_id = await database.add_rental_account(
        body.service_id, body.login.strip(), body.password, body.note or "",
    )
    # Сервис был пуст → ожидающим пора сообщить о наличии
    if had_free == 0:
        from services.rental_manager import rental_manager
        await rental_manager.notify_waitlist(body.service_id)
    return {"ok": True, "id": acc_id}


class RentalAccountUpdateBody(BaseModel):
    id:       int
    status:   Optional[str] = None   # free | maintenance | disabled
    login:    Optional[str] = None
    password: Optional[str] = None
    note:     Optional[str] = None


@app.post("/api/admin/rental/account/update")
async def admin_rental_account_update(body: RentalAccountUpdateBody,
                                      x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    acc = await database.get_rental_account(body.id)
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    if body.status is not None:
        if body.status not in ("free", "maintenance", "disabled"):
            raise HTTPException(400, "Недопустимый статус")
        if acc["status"] == "rented":
            raise HTTPException(400, "Аккаунт сейчас арендован — сначала отмените аренду")
    await database.update_rental_account(
        body.id, status=body.status, login=body.login,
        password=body.password, note=body.note,
    )
    # Вернулся в пул → уведомить waitlist («кто успел, тот арендовал»)
    if body.status == "free" and acc["status"] != "free":
        from services.rental_manager import rental_manager
        await rental_manager.notify_waitlist(acc["service_id"])
    return {"ok": True}


class RentalAccountDeleteBody(BaseModel):
    id: int


@app.post("/api/admin/rental/account/delete")
async def admin_rental_account_delete(body: RentalAccountDeleteBody,
                                      x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_rental_account(body.id)
    if not ok:
        raise HTTPException(400, "Не найден или сейчас арендован")
    return {"ok": True}


@app.get("/api/admin/rental/orders")
async def admin_rental_orders(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"rentals": await database.get_active_rentals_admin()}


class RentalCancelBody(BaseModel):
    order_id: int


@app.post("/api/admin/rental/cancel")
async def admin_rental_cancel(body: RentalCancelBody, x_telegram_init_data: str = Header(None)):
    """Отменить активную аренду с автовозвратом денег."""
    await _get_admin(x_telegram_init_data)
    result = await database.cancel_rental_with_refund(body.order_id, reason="cancelled_by_admin")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Не удалось отменить"))
    await _bot_send(
        result["user_id"],
        f"↩️ Аренда #{body.order_id} отменена администратором. "
        f"Возврат {result['refunded']:.0f} ₸ зачислен на баланс.",
    )
    return result


# -- Receipt search -----------------------------------------------------------

@app.get("/api/admin/receipt")
async def admin_find_by_receipt(q: str, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    result = await database.find_user_by_receipt(q.strip())
    if not result:
        raise HTTPException(404, "Чек не найден в базе данных")
    return result


# -- Ledger -------------------------------------------------------------------

@app.get("/api/admin/transactions")
async def admin_transactions(q: str, x_telegram_init_data: str = Header(None)):
    """История движений баланса пользователя (по tg_id или @username)."""
    await _get_admin(x_telegram_init_data)
    target = await database.find_user(q.strip())
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    txs = await database.get_user_transactions(target["tg_id"], limit=100)
    return {
        "user": {"tg_id": target["tg_id"], "username": target.get("username"),
                 "tenge_balance": target.get("tenge_balance"),
                 "token_balance": target.get("token_balance")},
        "transactions": txs,
    }


@app.get("/api/admin/ledger_check")
async def admin_ledger_check(x_telegram_init_data: str = Header(None)):
    """Сверка кэша балансов с журналом. mismatches=[] значит всё сходится."""
    await _get_admin(x_telegram_init_data)
    mismatches = await database.reconcile_balances()
    return {"ok": len(mismatches) == 0, "mismatches": mismatches}


# -- Cleanup ------------------------------------------------------------------

@app.post("/api/admin/cleanup")
async def admin_cleanup(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    from services.turnitin_service import turnitin_service
    # Файлы активных заказов (ready/processing) защищаем от удаления. БД
    # (очередь, бан-лист, статистика, история заказов) очистка не затрагивает.
    result = await turnitin_service.cleanup(
        reports_dir=settings.REPORTS_DIR,
        uploads_dir=settings.UPLOADS_DIR,
        protected_paths=await database.get_active_file_paths(),
    )
    return result


# ── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    import uvicorn

    async def startup():
        await database.init_db(settings.DATABASE_PATH)

    asyncio.run(startup())
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
