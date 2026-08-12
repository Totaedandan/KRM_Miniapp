"""
Интеграция с ApiPay (apipay.kz) — автоматическое пополнение баланса через Kaspi.
Документация: https://apipay.kz/docs.html#webhooks
"""
import hashlib
import hmac
import sys
import os

import httpx
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import settings

_BASE_URL = "https://api.apipay.kz/api/v1"


class ApiPayError(Exception):
    """Сетевая/серверная ошибка ApiPay (не 4xx-валидация)."""


class ApiPayValidationError(Exception):
    """422 — некорректные параметры счёта (сумма/телефон)."""


async def create_invoice(amount: int, phone_number: str, description: str, external_order_id: str) -> dict:
    """Создать счёт на оплату. Возвращает ответ ApiPay (status='processing' сразу после создания,
    реальный статус — 'pending'/'paid'/'error' и т.д. — придёт вебхуком)."""
    headers = {"X-API-Key": settings.APIPAY_API_KEY, "Content-Type": "application/json"}
    payload = {
        "amount": int(amount),
        "phone_number": phone_number,
        "description": description,
        "external_order_id": external_order_id,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{_BASE_URL}/invoices", headers=headers, json=payload)
    except httpx.HTTPError as e:
        logger.error(f"ApiPay create_invoice network error: {e}")
        raise ApiPayError(f"ApiPay недоступен: {e}")

    if resp.status_code == 422:
        data = resp.json()
        errors = data.get("errors", {})
        detail = "; ".join(
            f"{field}: {', '.join(msgs)}" for field, msgs in errors.items()
        ) or data.get("message", "Некорректные данные счёта")
        raise ApiPayValidationError(detail)

    if resp.status_code not in (200, 201):
        logger.error(f"ApiPay create_invoice error {resp.status_code}: {resp.text[:300]}")
        raise ApiPayError(f"ApiPay error {resp.status_code}")

    data = resp.json()
    logger.info(
        f"ApiPay invoice created: id={data.get('id')} status={data.get('status')} "
        f"external_order_id={external_order_id} amount={amount} phone={phone_number} "
        f"raw={data}"
    )
    return data


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Проверка X-Webhook-Signature: 'sha256=' + HMAC-SHA256(raw_body, secret) в hex,
    timing-safe сравнение. Сверяется по сырым байтам тела, без парсинга JSON."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        settings.APIPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
