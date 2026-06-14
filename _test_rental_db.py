"""Быстрый smoke-тест слоя аренды (временная БД). Запуск: python _test_rental_db.py"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bot"))
from database import db


async def main():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    await db.init_db(path)

    # Каталог: сервис + тарифы + 1 аккаунт
    sid = await db.upsert_rental_service("ChatGPT Plus", "Выдача аккаунта", "gpt")
    t6 = await db.upsert_rental_tariff(sid, "6 часов", 6, 490)
    await db.upsert_rental_tariff(sid, "1 месяц", 720, 3500)
    await db.add_rental_account(sid, "user@mail.com", "pass123", "test")

    cat = await db.get_rental_services_catalog()
    assert len(cat) == 1 and cat[0]["available"] == 1 and len(cat[0]["tariffs"]) == 2, cat
    assert "password" not in str(cat), "креды утекли в каталог!"

    # Пользователь с балансом
    await db.get_or_create_user(111, "buyer", "Buyer")
    await db.add_tenge(111, 1000, reason="topup", idempotency_key="t1")

    # Нехватка средств (тариф 3500 > 1000)
    big = [t for t in cat[0]["tariffs"] if t["price"] == 3500][0]
    try:
        await db.create_rental_order(111, "buyer", sid, big["id"], "rental:111:r0")
        assert False, "ожидали InsufficientFunds"
    except db.InsufficientFunds:
        pass

    # Покупка
    res = await db.create_rental_order(111, "buyer", sid, t6, "rental:111:r1")
    assert res["ok"] and not res["duplicate"] and res["balance"] == 510, res
    assert res["login"] == "user@mail.com" and res["password"] == "pass123", res
    order_id = res["order_id"]

    # Идемпотентность: тот же ключ → дубль, без списания
    res2 = await db.create_rental_order(111, "buyer", sid, t6, "rental:111:r1")
    assert res2["duplicate"] and res2["order_id"] == order_id and res2["balance"] == 510, res2

    # Аккаунтов больше нет
    try:
        await db.create_rental_order(111, "buyer", sid, t6, "rental:111:r2")
        assert False, "ожидали NoFreeAccount"
    except db.NoFreeAccount:
        pass
    bal = await db.get_tenge_balance(111)
    assert bal == 510, f"NoFreeAccount списал деньги! {bal}"

    # Мои аренды: активная с кредами
    my = await db.get_user_rentals(111)
    assert my[0]["status"] == "active" and my[0]["password"] == "pass123", my

    # Waitlist
    assert await db.add_to_waitlist(sid, 222) is True
    assert await db.add_to_waitlist(sid, 222) is False  # идемпотентно
    assert (await db.get_rental_services_catalog())[0]["waiting"] == 1

    # Отмена с возвратом (дважды — возврат один)
    r = await db.cancel_rental_with_refund(order_id)
    assert r["ok"] and r["refunded"] == 490, r
    assert await db.get_tenge_balance(111) == 1000
    r2 = await db.cancel_rental_with_refund(order_id)
    assert not r2["ok"] and r2["error"] == "already_final", r2
    assert await db.get_tenge_balance(111) == 1000, "двойной возврат!"
    acc = (await db.get_rental_accounts(sid))[0]
    assert acc["status"] == "maintenance" and acc["current_order_id"] is None, acc

    # Ротация: maintenance → free, pop_waitlist
    await db.update_rental_account(acc["id"], status="free", password="newpass")
    assert await db.pop_waitlist(sid) == [222]
    assert await db.pop_waitlist(sid) == []

    # Вторая аренда → истечение
    res3 = await db.create_rental_order(111, "buyer", sid, t6, "rental:111:r3")
    assert res3["password"] == "newpass", res3
    from datetime import datetime, timedelta
    future = (datetime.utcnow() + timedelta(hours=7)).isoformat()
    due = await db.get_due_rentals(future)
    assert len(due) == 1, due
    expired = await db.expire_rental(due[0]["id"])
    assert expired and (await db.get_rental_accounts(sid))[0]["status"] == "maintenance"
    assert await db.expire_rental(due[0]["id"]) is None  # повторно — no-op

    # Напоминания: окно [now, now+30m]
    res4_due = await db.get_reminder_due_rentals(future, datetime.utcnow().isoformat())
    assert res4_due == [], res4_due  # активных нет

    # Сверка ledger
    mismatches = await db.reconcile_balances()
    assert mismatches == [], mismatches

    print("ALL RENTAL DB TESTS PASSED")


asyncio.run(main())
