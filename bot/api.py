"""
FastAPI — бэкенд для Telegram Mini App.

Пользовательские эндпоинты:
  GET  /                     — отдаёт mini_app/index.html
  GET  /api/me               — данные пользователя (авторизация через initData)
  GET  /api/prices           — текущие цены Turnitin
  GET  /api/orders           — история заказов пользователя
  GET  /api/packages         — пакеты токенов
  POST /api/humanize         — хуманизировать текст (списывает токены)
  POST /api/promo/apply      — активировать промокод (fixed — сразу, percent — превью)
  POST /api/topup            — создать счёт ApiPay на пополнение баланса (Kaspi)
  GET  /api/topup/status     — статус пополнения (для поллинга из Mini App)
  POST /api/kaspi/webhook    — вебхук ApiPay (без initData, авторизация по подписи)
  GET  /api/rental/services  — каталог аренды ИИ-аккаунтов (email+OTP)
  GET  /api/rental/my        — аренды пользователя (active/history)
  POST /api/rental/order     — арендовать (LRU-аккаунт, оплата тенге+бонус)
  POST /api/rental/notify    — подписка на waitlist
  GET  /api/rental/otp       — получить код входа для email своей аренды
  POST /api/email-hook       — вебхук Cloudflare Worker (OTP), авторизация по X-Webhook-Secret

Админские эндпоинты (только superadmin):
  GET  /api/admin/stats
  GET  /api/admin/orders
  GET  /api/admin/user?q=
  POST /api/admin/adjust_balance
  GET  /api/admin/whitelist
  POST /api/admin/whitelist/add
  POST /api/admin/whitelist/remove
  GET  /api/admin/banlist
  POST /api/admin/ban
  POST /api/admin/unban
  POST /api/admin/prices
  POST /api/admin/packages
  GET  /api/admin/settings
  POST /api/admin/settings
  POST /api/admin/cleanup
  GET  /api/admin/promo
  POST /api/admin/promo
  POST /api/admin/promo/delete
  POST /api/admin/broadcast
  POST /api/admin/rental/service/delete
  POST /api/admin/rental/tariff/delete
  GET  /api/admin/rental/orders
  POST /api/admin/rental/cancel
  GET/POST /api/admin/ai/proxies, POST /api/admin/ai/proxies/delete
  GET/POST /api/admin/ai/accounts, POST /api/admin/ai/accounts/update|delete|force_logout
  GET  /api/admin/ai/otp_logs
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from database import db as database
from bot_sender import send_message as _bot_send, send_document as _bot_send_document

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
    packages = await database.get_token_packages()
    return {
        "tg_id":           user["tg_id"],
        "username":        user.get("username"),
        "full_name":       user.get("full_name"),
        "tenge_balance":   round(user.get("tenge_balance", 0.0), 2),
        "token_balance":   round(user.get("token_balance", 0.0), 2),
        "bonus_balance":   round(user.get("bonus_balance", 0.0), 2),
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
    return {"packages": await database.get_token_packages()}


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
    asyncio.create_task(_send_humanize_result_to_chat(
        user["tg_id"], result, word_count, cost, is_wl, new_balance,
    ))
    return {
        "result":       result,
        "words_input":  word_count,
        "words_output": len(result.split()),
        "tokens_spent": cost if not is_wl else 0,
        "balance":      round(new_balance, 2),
    }


async def _send_humanize_result_to_chat(tg_id: int, result: str, word_count: int,
                                         cost: float, is_wl: bool, new_balance: float):
    """Дублирует результат хуманайзера в чат — как в bot-flow (handlers/humanizer.py),
    иначе он виден только внутри Mini App."""
    header = (
        f"✅ <b>Готово!</b> (из приложения)\n"
        f"📊 Слов: {word_count} → {len(result.split())}\n"
        f"{'💰 Списано: <b>' + str(cost) + ' 🪙</b>' if not is_wl else '✨ Бесплатно (whitelist)'}\n"
        f"{'🪙 Баланс: <b>' + str(round(new_balance, 1)) + '</b>' if not is_wl else ''}\n"
        f"─────────────────────\n"
    )
    if len(header) + len(result) <= 4000:
        await _bot_send(tg_id, header)
        await _bot_send(tg_id, result, parse_mode="")
    else:
        await _bot_send(tg_id, header)
        await _bot_send_document(
            tg_id, "humanized.txt", result.encode("utf-8"),
            caption="📎 Результат (файл — текст слишком длинный для сообщения)",
        )


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


class AdjustBalanceBody(BaseModel):
    tg_id:     int
    currency:  str    # 'tenge' | 'token' | 'bonus'
    direction: str    # 'add' | 'deduct'
    amount:    float


@app.post("/api/admin/adjust_balance")
async def admin_adjust_balance(body: AdjustBalanceBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.amount <= 0:
        raise HTTPException(400, "Сумма должна быть > 0")
    if body.currency not in ("tenge", "token", "bonus"):
        raise HTTPException(400, "currency: tenge | token | bonus")
    if body.direction not in ("add", "deduct"):
        raise HTTPException(400, "direction: add | deduct")

    fn = {
        ("tenge", "add"):    database.add_tenge,
        ("tenge", "deduct"): database.deduct_tenge,
        ("token", "add"):    database.add_tokens,
        ("token", "deduct"): database.deduct_tokens,
        ("bonus", "add"):    database.add_bonus,
        ("bonus", "deduct"): database.deduct_bonus,
    }[(body.currency, body.direction)]
    new_balance = await fn(body.tg_id, body.amount, reason="admin_adjust")
    return {"currency": body.currency, "new_balance": round(new_balance, 2)}


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


class PackagePriceBody(BaseModel):
    index:  int    # 1-4
    tokens: int
    tenge:  int


@app.post("/api/admin/packages")
async def admin_set_package(body: PackagePriceBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if body.tokens <= 0 or body.tenge <= 0:
        raise HTTPException(400, "Токены и цена должны быть > 0")
    try:
        await database.set_token_package(body.index, body.tokens, body.tenge)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"packages": await database.get_token_packages()}


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
    use_bonus:   bool = False           # списывать ли сначала с бонусного баланса


@app.post("/api/order")
async def create_order(body: OrderBody, x_telegram_init_data: str = Header(None)):
    """Создать заказ Turnitin — списать тенге (+бонус, если включено), забронировать
    место в очереди."""
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
        new_bonus = user.get("bonus_balance", 0.0)
    else:
        # Платно: атомарно списать (бонус сначала, если use_bonus) + создать заказ + ledger.
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
                use_bonus=body.use_bonus,
            )
        except database.InsufficientFunds:
            balance = await database.get_tenge_balance(tg_id)
            bonus = await database.get_bonus_balance(tg_id) if body.use_bonus else 0
            have = f"{balance:.0f} ₸" + (f" + {bonus:.0f} ₸ бонусов" if bonus else "")
            raise HTTPException(402, f"Недостаточно средств. Нужно {price} ₸, есть {have}")

        order_id = res["order_id"]
        new_balance = res["balance"]
        new_bonus = res.get("bonus_balance", 0.0)
        if res.get("duplicate"):
            # Повторный запрос — заказ уже создан, второй раз не списываем и не дёргаем очередь
            return {
                "ok": True,
                "order_id": order_id,
                "is_premium": body.is_premium,
                "price": price,
                "balance": round(new_balance, 2),
                "bonus_balance": round(new_bonus, 2),
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
        "bonus_balance": round(new_bonus, 2),
    }


class TopupBody(BaseModel):
    amount: float
    phone_number: str
    promo_code: Optional[str] = None


_PHONE_RE = re.compile(r"^87\d{9}$")


@app.post("/api/topup")
async def init_topup(body: TopupBody, x_telegram_init_data: str = Header(None)):
    """Инициализировать автоматическое пополнение тенге через ApiPay (Kaspi)."""
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]

    if body.amount < 100:
        raise HTTPException(400, "Минимальная сумма пополнения — 100 ₸")
    phone = body.phone_number.strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "Номер телефона в формате 87001234567")

    # Процентный промокод — лёгкая пре-проверка (fail-fast). Если код невалиден
    # к этому моменту — просто не прикрепляем его, пополнение не блокируем.
    promo_code = None
    if body.promo_code:
        try:
            await database.validate_promo_for_topup(tg_id, body.promo_code)
            promo_code = database._normalize_promo_code(body.promo_code)
        except Exception as e:
            logger.info(f"topup promo pre-check failed for {tg_id}: {e}")

    payment_id = await database.create_payment(
        user_id=tg_id,
        payment_type="apipay",
        purpose="topup_tenge",
        amount_tenge=body.amount,
        promo_code=promo_code,
    )

    from services import apipay_service
    try:
        await apipay_service.create_invoice(
            amount=int(body.amount),
            phone_number=phone,
            description=f"Пополнение баланса #{payment_id}",
            external_order_id=str(payment_id),
        )
    except apipay_service.ApiPayValidationError as e:
        raise HTTPException(400, str(e))
    except apipay_service.ApiPayError:
        raise HTTPException(502, "Платёжный сервис недоступен, попробуйте позже")

    return {
        "ok": True,
        "payment_id": payment_id,
        "amount": body.amount,
        "phone_number": phone,
    }


@app.get("/api/topup/status")
async def topup_status(payment_id: int, x_telegram_init_data: str = Header(None)):
    user = await _get_user(x_telegram_init_data)
    payment = await database.get_payment(payment_id)
    if not payment or payment["user_id"] != user["tg_id"]:
        raise HTTPException(404, "Платёж не найден")
    result = {"status": payment["status"]}
    if payment["status"] == "confirmed":
        result["tenge_balance"] = round(await database.get_tenge_balance(user["tg_id"]), 2)
    return result


@app.post("/api/kaspi/webhook")
async def kaspi_webhook(request: Request, x_webhook_signature: str = Header(None)):
    """Вебхук ApiPay о смене статуса счёта. Аутентификация — по подписи, не по initData."""
    from services import apipay_service

    raw = await request.body()
    logger.info(f"kaspi_webhook received: has_sig={bool(x_webhook_signature)} len={len(raw)} body={raw[:500]}")
    if not apipay_service.verify_webhook_signature(raw, x_webhook_signature):
        logger.warning(f"kaspi_webhook: signature mismatch, got={x_webhook_signature}")
        raise HTTPException(401, "Invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": True}

    if payload.get("event") != "invoice.status_changed":
        return {"ok": True}

    invoice = payload.get("invoice") or {}
    status = invoice.get("status")
    invoice_id = invoice.get("id")
    external_order_id = invoice.get("external_order_id")

    try:
        payment_id = int(external_order_id)
    except (TypeError, ValueError):
        logger.warning(f"kaspi_webhook: bad external_order_id={external_order_id!r}")
        return {"ok": True}

    payment = await database.get_payment(payment_id)
    if not payment or payment["status"] == "confirmed":
        return {"ok": True}

    if status == "paid":
        amount = float(payment["amount_tenge"] or 0)
        new_balance = await database.add_tenge(
            payment["user_id"], amount, reason="topup",
            idempotency_key=f"apipay:{invoice_id}",
        )
        bonus_line = ""
        if payment.get("promo_code"):
            promo_result = await database.redeem_promo_percent(
                payment["user_id"], payment["promo_code"], base_amount=amount,
            )
            if promo_result.get("applied"):
                bonus_line = (f"\n🎁 Бонус по промокоду <b>{payment['promo_code']}</b>: "
                              f"+{promo_result['bonus']:.0f} ₸ (бонусный баланс)")
        await database.confirm_payment(payment_id, charge_id=str(invoice_id))
        await _bot_send(
            payment["user_id"],
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💰 Зачислено: <b>{amount:.0f} ₸</b>{bonus_line}\n"
            f"💰 Новый баланс: <b>{new_balance:.0f} ₸</b>",
        )
    elif status in ("cancelled", "expired", "error"):
        await database.fail_payment(payment_id, status)
        reason = {"cancelled": "отменена", "expired": "истёк срок ожидания", "error": "техническая ошибка"}.get(status, status)
        await _bot_send(payment["user_id"], f"❌ Пополнение баланса не удалось: {reason}.")
    # status == "pending" — счёт создан, ждём подтверждения в Kaspi, ничего не делаем

    return {"ok": True}


class BuyTokensApiBody(BaseModel):
    tokens: float
    tenge: float
    use_bonus: bool = False   # списывать ли сначала с бонусного баланса


@app.post("/api/buy_tokens")
async def buy_tokens_api(body: BuyTokensApiBody, x_telegram_init_data: str = Header(None)):
    """Купить токены из тенге-баланса (+бонус, если включено) мгновенно."""
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]

    if body.tokens <= 0 or body.tenge <= 0:
        raise HTTPException(400, "Неверные данные пакета")

    is_wl = bool(user.get("is_whitelisted"))
    new_bonus = user.get("bonus_balance", 0.0)
    if not is_wl:
        try:
            debit = await database.debit_with_bonus(
                tg_id, body.tenge, body.use_bonus, reason="token_purchase",
            )
        except database.InsufficientFunds:
            balance = await database.get_tenge_balance(tg_id)
            bonus = await database.get_bonus_balance(tg_id) if body.use_bonus else 0
            have = f"{balance:.0f} ₸" + (f" + {bonus:.0f} ₸ бонусов" if bonus else "")
            raise HTTPException(402, f"Недостаточно средств. Нужно {body.tenge:.0f} ₸, есть {have}")
        new_bonus = debit["bonus_balance"]

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
        "new_bonus_balance": round(new_bonus, 2),
    }


# ── Промокоды ──────────────────────────────────────────────────────────────
# fixed — редимится мгновенно здесь. percent — только проверяется здесь
# (превью), реально консьюмится позже в payment.py при подтверждении Kaspi-чека.

class PromoApplyBody(BaseModel):
    code: str


_PROMO_EXC_STATUS = {
    database.PromoNotFound:    404,
    database.PromoNotActive:   400,
    database.PromoExhausted:   400,
    database.PromoAlreadyUsed: 400,
}


@app.post("/api/promo/apply")
async def promo_apply(body: PromoApplyBody, x_telegram_init_data: str = Header(None)):
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "Введите промокод")

    try:
        preview = await database.validate_promo_for_topup(tg_id, code)
        return {"type": "percent", "percent": preview["value"], "code": preview["code"]}
    except ValueError:
        pass  # не percent-код — пробуем как fixed ниже
    except tuple(_PROMO_EXC_STATUS) as e:
        raise HTTPException(_PROMO_EXC_STATUS[type(e)], database.PROMO_ERROR_MESSAGES[type(e)])

    try:
        result = await database.apply_promo_fixed(tg_id, code)
        return {"type": "fixed", "bonus": result["bonus"], "new_bonus_balance": round(result["new_balance"], 2)}
    except tuple(_PROMO_EXC_STATUS) as e:
        raise HTTPException(_PROMO_EXC_STATUS[type(e)], database.PROMO_ERROR_MESSAGES[type(e)])
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Аренда ИИ-аккаунтов v2 (email+OTP, авто-разлогин, прокси-группы) ─────────
# Каталог (rental_services/rental_tariffs) переиспользуется как есть.

@app.get("/api/rental/services")
async def rental_services(x_telegram_init_data: str = Header(None)):
    """Каталог сервисов аренды: тарифы, наличие, размер waitlist. Без кредов."""
    await _get_user(x_telegram_init_data)
    return {"services": await database.get_ai_services_catalog()}


@app.get("/api/rental/my")
async def rental_my(x_telegram_init_data: str = Header(None)):
    """{'active': [...], 'history': [...]} — аренды пользователя (email вместо кредов)."""
    user = await _get_user(x_telegram_init_data)
    return await database.get_user_ai_rentals(user["tg_id"])


class RentalOrderBody(BaseModel):
    service_id: int
    tariff_id:  int
    request_id: Optional[str] = None   # ключ идемпотентности (от Mini App)
    use_bonus:  bool = False           # списывать ли сначала с бонусного баланса


@app.post("/api/rental/order")
async def rental_order(body: RentalOrderBody, x_telegram_init_data: str = Header(None)):
    """Арендовать аккаунт: атомарно списать тенге (+бонус, если включено) + выдать
    свободный (LRU) аккаунт по email.

    Whitelist-бесплатно здесь НЕТ: инвентарь конечен, платят все.
    """
    user = await _get_user(x_telegram_init_data)
    tg_id = user["tg_id"]
    idem = f"rental:{tg_id}:{body.request_id}" if body.request_id else None

    try:
        res = await database.create_ai_rental(
            user_id=tg_id,
            username=user.get("username"),
            service_id=body.service_id,
            tariff_id=body.tariff_id,
            idempotency_key=idem,
            use_bonus=body.use_bonus,
        )
    except database.InsufficientFunds:
        balance = await database.get_tenge_balance(tg_id)
        raise HTTPException(402, f"Недостаточно средств. Пополните баланс (есть {balance:.0f} ₸)")
    except database.NoFreeAccount:
        raise HTTPException(409, "Аккаунты закончились — нажмите «Уведомить», и мы напишем, когда освободится")

    res["balance"] = round(res.get("balance", 0), 2)
    res["bonus_balance"] = round(res.get("bonus_balance", 0), 2)
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


@app.get("/api/rental/otp")
async def rental_otp(email: str, x_telegram_init_data: str = Header(None)):
    """Юзер жмёт «Получить код»: отдаём последний OTP, пришедший на email его
    активной аренды (владение проверяется — иначе можно подсмотреть чужой код)."""
    user = await _get_user(x_telegram_init_data)
    email = email.strip().lower()
    rental = await database.get_active_ai_rental_by_email(user["tg_id"], email)
    if not rental:
        raise HTTPException(403, "Этот email не привязан к вашей активной аренде")
    code = await database.get_recent_otp(email)
    if not code:
        return {"ok": False, "code": None, "message": "Код ещё не пришёл, попробуйте через несколько секунд"}
    return {"ok": True, "code": code}


class EmailHookBody(BaseModel):
    recipient_email: str
    otp_code:        Optional[str] = None
    magic_link:      Optional[str] = None


@app.post("/api/email-hook")
async def email_hook(body: EmailHookBody, x_webhook_secret: str = Header(None)):
    """Приём кода/ссылки от Cloudflare Worker (Email Routing → пересылка на
    этот эндпоинт). Публичный (без Telegram initData) эндпоинт — защищён
    отдельным заголовком-секретом, сверяемым constant-time.

    Обычно приходит otp_code (код прямо в письме). Некоторые сервисы (Claude)
    шлют только magic-link — код появляется лишь на странице, куда она ведёт,
    и рендерится их собственным JS, так что нужен реальный браузер: запускаем
    resolve_magic_link_otp в фоне (не блокируем ответ вебхуку) и возвращаемся
    сразу — результат сам появится в otp_incoming_codes через 5-20 секунд."""
    if not settings.EMAIL_WEBHOOK_SECRET or not x_webhook_secret or \
       not hmac.compare_digest(x_webhook_secret, settings.EMAIL_WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid webhook secret")
    email = body.recipient_email.strip().lower()

    if body.otp_code:
        code = re.sub(r"\D", "", body.otp_code)[:8]
        if not code:
            raise HTTPException(400, "otp_code пустой")
        await database.insert_otp_code(email, code)
        logger.info(f"email-hook: OTP received for {email}")
        return {"ok": True}

    if body.magic_link:
        from services.ai_rental_service import resolve_magic_link_otp
        logger.info(f"email-hook: magic link received for {email}, resolving via browser")
        asyncio.create_task(resolve_magic_link_otp(email, body.magic_link))
        return {"ok": True, "queued": True}

    raise HTTPException(400, "otp_code или magic_link обязателен")


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


class RentalServiceDeleteBody(BaseModel):
    id: int


@app.post("/api/admin/rental/service/delete")
async def admin_rental_service_delete(body: RentalServiceDeleteBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_rental_service(body.id)
    if not ok:
        raise HTTPException(400, "Нельзя удалить: есть аккаунты на складе или заказы аренды по этому сервису")
    return {"ok": True}


class RentalTariffDeleteBody(BaseModel):
    id: int


@app.post("/api/admin/rental/tariff/delete")
async def admin_rental_tariff_delete(body: RentalTariffDeleteBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_rental_tariff(body.id)
    if not ok:
        raise HTTPException(400, "Нельзя удалить: есть заказы аренды по этому тарифу")
    return {"ok": True}


# -- Аренда: админ — прокси -----------------------------------------------------

@app.get("/api/admin/ai/proxies")
async def admin_ai_proxies(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"proxies": await database.list_ai_proxies()}


class AiProxyBody(BaseModel):
    proxy_url:    str          # http://user:pass@ip:port
    max_accounts: int = 3


@app.post("/api/admin/ai/proxies")
async def admin_ai_proxy_add(body: AiProxyBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    if not body.proxy_url.strip():
        raise HTTPException(400, "proxy_url обязателен")
    if body.max_accounts <= 0:
        raise HTTPException(400, "max_accounts должен быть > 0")
    pid = await database.create_ai_proxy(body.proxy_url.strip(), body.max_accounts)
    return {"ok": True, "id": pid}


class AiProxyDeleteBody(BaseModel):
    id: int


@app.post("/api/admin/ai/proxies/delete")
async def admin_ai_proxy_delete(body: AiProxyDeleteBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_ai_proxy(body.id)
    if not ok:
        raise HTTPException(400, "Нельзя удалить: к прокси привязаны аккаунты")
    return {"ok": True}


# -- Аренда: админ — аккаунты (email вместо логин/пароль) -----------------------

@app.get("/api/admin/ai/accounts")
async def admin_ai_accounts(service_id: Optional[int] = None,
                             x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"accounts": await database.list_ai_accounts(service_id)}


class AiAccountBody(BaseModel):
    service_id: int
    email:      str
    proxy_id:   Optional[int] = None


@app.post("/api/admin/ai/accounts")
async def admin_ai_account_add(body: AiAccountBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(400, "Email обязателен")
    catalog_before = await database.get_ai_services_catalog()
    had_free = next((s["available"] for s in catalog_before if s["id"] == body.service_id), 0)
    try:
        acc_id = await database.add_ai_account(body.service_id, email, body.proxy_id)
    except database.ProxyFull:
        raise HTTPException(409, "У этого прокси уже максимум аккаунтов")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Сервис был пуст → ожидающим пора сообщить о наличии
    if had_free == 0:
        from services.ai_rental_manager import ai_rental_manager
        await ai_rental_manager.notify_waitlist(body.service_id)
    return {"ok": True, "id": acc_id}


class AiAccountUpdateBody(BaseModel):
    id:     int
    status: str   # available | maintenance | disabled | banned


@app.post("/api/admin/ai/accounts/update")
async def admin_ai_account_update(body: AiAccountUpdateBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    acc = await database.get_ai_account(body.id)
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    if body.status not in ("available", "cooldown", "maintenance", "disabled", "banned"):
        raise HTTPException(400, "Недопустимый статус")
    if acc["status"] == "rented":
        raise HTTPException(400, "Аккаунт сейчас арендован — сначала отмените аренду")
    was_free = acc["status"] == "available"
    await database.update_ai_account_status(body.id, body.status)
    # Вернулся в пул → уведомить waitlist («кто успел, тот арендовал»)
    if body.status == "available" and not was_free:
        from services.ai_rental_manager import ai_rental_manager
        await ai_rental_manager.notify_waitlist(acc["service_id"])
    return {"ok": True}


class AiAccountIdBody(BaseModel):
    id: int


@app.post("/api/admin/ai/accounts/delete")
async def admin_ai_account_delete(body: AiAccountIdBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_ai_account(body.id)
    if not ok:
        raise HTTPException(400, "Не найден или сейчас арендован")
    return {"ok": True}


@app.post("/api/admin/ai/accounts/force_logout")
async def admin_ai_account_force_logout(body: AiAccountIdBody, x_telegram_init_data: str = Header(None)):
    """Вручную запустить авто-разлогин (повторная попытка для аккаунта в
    maintenance, либо сервис без поддержки автоматизации — тогда задача сразу
    вернёт False и статус останется maintenance для ручной проверки)."""
    await _get_admin(x_telegram_init_data)
    acc = await database.get_ai_account(body.id)
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    from services.ai_rental_manager import ai_rental_manager
    asyncio.create_task(ai_rental_manager.logout_account(body.id))
    return {"ok": True, "started": True}


@app.get("/api/admin/ai/otp_logs")
async def admin_ai_otp_logs(limit: int = 100, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"logs": await database.get_otp_logs(limit)}


@app.get("/api/admin/rental/orders")
async def admin_rental_orders(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"rentals": await database.get_active_ai_rentals_admin()}


class RentalCancelBody(BaseModel):
    order_id: int


@app.post("/api/admin/rental/cancel")
async def admin_rental_cancel(body: RentalCancelBody, x_telegram_init_data: str = Header(None)):
    """Отменить активную аренду с автовозвратом денег + постановкой реального
    разлогина в очередь (cancel в БД сразу переводит аккаунт в cooldown как
    защиту в глубину — здесь запускаем настоящий Playwright-разлогин)."""
    await _get_admin(x_telegram_init_data)
    result = await database.cancel_ai_rental_with_refund(body.order_id, reason="cancelled_by_admin")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Не удалось отменить"))
    from services.ai_rental_manager import ai_rental_manager
    asyncio.create_task(ai_rental_manager.logout_account(result["account_id"]))
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


# -- Промокоды (админ) ---------------------------------------------------------

class PromoCreateBody(BaseModel):
    code: str
    type: str
    value: float
    per_user_limit: int = 1
    total_limit: Optional[int] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


@app.get("/api/admin/promo")
async def admin_promo_list(x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    return {"promos": await database.list_promos()}


@app.post("/api/admin/promo")
async def admin_promo_create(body: PromoCreateBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    try:
        promo_id = await database.create_promo(
            code=body.code, type_=body.type, value=body.value,
            per_user_limit=body.per_user_limit, total_limit=body.total_limit,
            starts_at=body.starts_at, ends_at=body.ends_at,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": promo_id}


class PromoDeleteBody(BaseModel):
    id: int


@app.post("/api/admin/promo/delete")
async def admin_promo_delete(body: PromoDeleteBody, x_telegram_init_data: str = Header(None)):
    await _get_admin(x_telegram_init_data)
    ok = await database.delete_promo(body.id)
    if not ok:
        raise HTTPException(404, "Промокод не найден")
    return {"ok": True}


# -- Рассылка (админ) -----------------------------------------------------------

class BroadcastBody(BaseModel):
    text: str


async def _run_broadcast(admin_tg_id: int, ids: list[int], text: str):
    sent = failed = 0
    for uid in ids:
        ok = await _bot_send(uid, text)
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 msg/s — с запасом от лимитов Bot API
    await _bot_send(
        admin_tg_id,
        f"📣 Рассылка завершена: {sent} доставлено, {failed} не доставлено из {len(ids)}.",
    )


@app.post("/api/admin/broadcast")
async def admin_broadcast(body: BroadcastBody, x_telegram_init_data: str = Header(None)):
    admin = await _get_admin(x_telegram_init_data)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Текст рассылки не может быть пустым")

    ids = await database.get_all_user_ids(exclude_banned=True)
    asyncio.create_task(_run_broadcast(admin["tg_id"], ids, text))
    return {"ok": True, "total": len(ids)}


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
