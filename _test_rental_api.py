"""E2E-тест API аренды через FastAPI TestClient (без Telegram-polling).
Запуск: winvenv\\Scripts\\python.exe _test_rental_api.py
Использует временную БД; сообщения бота замоканы (реальный Telegram не трогаем).
"""
import asyncio, hashlib, hmac, json, os, sys, tempfile, time, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)                            # .env лежит в корне проекта
sys.path.insert(0, os.path.join(ROOT, "bot"))

from config import settings
from database import db

# Мокаем отправку сообщений ДО использования
SENT = []
async def fake_send(chat_id, text, parse_mode="HTML"):
    SENT.append((chat_id, text))
    return True

import api
import services.rental_manager as rm
api._bot_send = fake_send
rm.send_message = fake_send

from fastapi.testclient import TestClient


def make_init_data(user: dict) -> str:
    payload = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in {**payload, "hash": h}.items())


ADMIN = make_init_data({"id": settings.SUPERADMIN_ID, "first_name": "Admin", "username": "admin"})
U1 = make_init_data({"id": 555001, "first_name": "Один", "username": "user1"})
U2 = make_init_data({"id": 555002, "first_name": "Два", "username": "user2"})


def main():
    path = os.path.join(tempfile.mkdtemp(), "api_test.db")
    asyncio.run(db.init_db(path))
    c = TestClient(api.app)
    H = lambda d: {"X-Telegram-Init-Data": d}

    # Админ создаёт сервис + тариф + 1 аккаунт
    r = c.post("/api/admin/rental/service", headers=H(ADMIN),
               json={"name": "ChatGPT Plus", "icon": "🤖", "description": "тест"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = c.post("/api/admin/rental/tariff", headers=H(ADMIN),
               json={"service_id": sid, "name": "6 часов", "duration_hours": 6, "price": 490})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    r = c.post("/api/admin/rental/account", headers=H(ADMIN),
               json={"service_id": sid, "login": "acc1@m.com", "password": "p1"})
    assert r.status_code == 200, r.text

    # Не-админу админка закрыта
    assert c.get("/api/admin/rental/services", headers=H(U1)).status_code == 403

    # Каталог виден, кредов в нём нет
    r = c.get("/api/rental/services", headers=H(U1))
    assert r.status_code == 200 and "password" not in r.text and r.json()["services"][0]["available"] == 1

    # 0 ₸ → 402
    r = c.post("/api/rental/order", headers=H(U1),
               json={"service_id": sid, "tariff_id": tid, "request_id": "r1"})
    assert r.status_code == 402, r.text

    # Пополняем и покупаем
    asyncio.run(db.add_tenge(555001, 1000, reason="topup", idempotency_key="t:u1"))
    r = c.post("/api/rental/order", headers=H(U1),
               json={"service_id": sid, "tariff_id": tid, "request_id": "r1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["login"] == "acc1@m.com" and d["password"] == "p1" and d["balance"] == 510, d
    order_id = d["order_id"]

    # Идемпотентность: тот же request_id → duplicate, без второго списания
    r = c.post("/api/rental/order", headers=H(U1),
               json={"service_id": sid, "tariff_id": tid, "request_id": "r1"})
    assert r.status_code == 200 and r.json()["duplicate"] and r.json()["balance"] == 510, r.text

    # Свободных нет → 409; второй юзер встаёт в waitlist
    asyncio.run(db.get_or_create_user(555002, "user2", "Два"))
    asyncio.run(db.add_tenge(555002, 1000, reason="topup", idempotency_key="t:u2"))
    r = c.post("/api/rental/order", headers=H(U2),
               json={"service_id": sid, "tariff_id": tid, "request_id": "r2"})
    assert r.status_code == 409, r.text
    assert asyncio.run(db.get_tenge_balance(555002)) == 1000, "409 списал деньги!"
    assert c.post("/api/rental/notify", headers=H(U2), json={"service_id": sid}).status_code == 200

    # «Мои аренды»: активная с кредами
    r = c.get("/api/rental/my", headers=H(U1))
    assert r.json()["rentals"][0]["password"] == "p1", r.text

    # Админ: активные аренды + отмена с возвратом (и уведомлением)
    r = c.get("/api/admin/rental/orders", headers=H(ADMIN))
    assert len(r.json()["rentals"]) == 1
    SENT.clear()
    r = c.post("/api/admin/rental/cancel", headers=H(ADMIN), json={"order_id": order_id})
    assert r.status_code == 200 and r.json()["refunded"] == 490, r.text
    assert asyncio.run(db.get_tenge_balance(555001)) == 1000
    assert any(cid == 555001 for cid, _ in SENT), "юзеру не пришло уведомление об отмене"
    # Повторная отмена — 400, без двойного возврата
    assert c.post("/api/admin/rental/cancel", headers=H(ADMIN), json={"order_id": order_id}).status_code == 400
    assert asyncio.run(db.get_tenge_balance(555001)) == 1000

    # Аккаунт после отмены в maintenance; админ ротирует пароль → free → waitlist уведомлён
    accs = c.get("/api/admin/rental/accounts", headers=H(ADMIN)).json()["accounts"]
    assert accs[0]["status"] == "maintenance", accs
    SENT.clear()
    r = c.post("/api/admin/rental/account/update", headers=H(ADMIN),
               json={"id": accs[0]["id"], "password": "p2-rotated", "status": "free"})
    assert r.status_code == 200, r.text
    assert any(cid == 555002 for cid, _ in SENT), "waitlist не уведомлён"

    # Гонка за последний аккаунт: два параллельных запроса → ровно один 200
    async def race():
        import httpx
        tr = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as ac:
            return await asyncio.gather(
                ac.post("/api/rental/order", headers=H(U1),
                        json={"service_id": sid, "tariff_id": tid, "request_id": "race1"}),
                ac.post("/api/rental/order", headers=H(U2),
                        json={"service_id": sid, "tariff_id": tid, "request_id": "race2"}),
            )
    r1, r2 = asyncio.run(race())
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"гонка: {codes} {r1.text} {r2.text}"

    # Сверка ledger чистая
    r = c.get("/api/admin/ledger_check", headers=H(ADMIN))
    assert r.json()["ok"] is True, r.text

    print("ALL RENTAL API E2E TESTS PASSED")


main()
