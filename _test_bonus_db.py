"""Быстрый smoke-тест бонусного баланса (временная БД). Запуск: python _test_bonus_db.py"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bot"))
from database import db


async def main():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    await db.init_db(path)

    # ── Turnitin-заказ: бонус тратится первым, остаток — с тенге ────────
    await db.get_or_create_user(111, "buyer", "Buyer")
    await db.add_tenge(111, 1000, reason="topup", idempotency_key="t1")
    await db.add_bonus(111, 300, reason="promo_fixed", idempotency_key="b1")

    res = await db.create_paid_order(111, "buyer", "sim", price=500, idempotency_key="o1", use_bonus=True)
    assert res["ok"] and not res["duplicate"], res
    order_id = res["order_id"]
    assert await db.get_bonus_balance(111) == 0, "бонус (300) должен был списаться полностью"
    assert await db.get_tenge_balance(111) == 800, "тенге: 1000-200=800"

    split = await db._get_charge_split(order_id, "order_charge")
    assert split.get("bonus") == 300 and split.get("tenge") == 200, split

    # Идемпотентность — повтор с тем же ключом не списывает второй раз
    res_dup = await db.create_paid_order(111, "buyer", "sim", price=500, idempotency_key="o1", use_bonus=True)
    assert res_dup["duplicate"], res_dup
    assert await db.get_tenge_balance(111) == 800, "повторное списание!"

    # Возврат — каждая валюта туда, откуда списалась
    r = await db.cancel_order_with_refund(order_id)
    assert r["ok"] and r["refunded"] == 500, r
    assert await db.get_bonus_balance(111) == 300, "бонус не вернулся в bonus_balance!"
    assert await db.get_tenge_balance(111) == 1000, "тенге не вернулся полностью!"

    # ── use_bonus=False: бонус не трогается, даже если он есть ──────────
    await db.get_or_create_user(222, "second", "Second")
    await db.add_tenge(222, 1000, reason="topup", idempotency_key="t2")
    await db.add_bonus(222, 500, reason="promo_fixed", idempotency_key="b2")
    await db.create_paid_order(222, "second", "sim", price=300, idempotency_key="o2", use_bonus=False)
    assert await db.get_bonus_balance(222) == 500, "бонус потрачен при use_bonus=False!"
    assert await db.get_tenge_balance(222) == 700

    # ── Не хватает даже с бонусом — деньги не должны списаться вообще ────
    await db.get_or_create_user(333, "third", "Third")
    await db.add_tenge(333, 100, reason="topup", idempotency_key="t3")
    await db.add_bonus(333, 50, reason="promo_fixed", idempotency_key="b3")
    try:
        await db.create_paid_order(333, "third", "sim", price=1000, idempotency_key="o3", use_bonus=True)
        assert False, "ожидали InsufficientFunds"
    except db.InsufficientFunds:
        pass
    assert await db.get_tenge_balance(333) == 100 and await db.get_bonus_balance(333) == 50, \
        "деньги списались при провале транзакции!"

    # ── Аренда: та же механика списания/возврата ─────────────────────────
    sid = await db.upsert_rental_service("Test Svc", "d", "icon")
    tid = await db.upsert_rental_tariff(sid, "1h", 1, 400)
    await db.add_rental_account(sid, "u@x.com", "pass", "")

    res_r = await db.create_rental_order(111, "buyer", sid, tid, idempotency_key="r1", use_bonus=True)
    assert res_r["ok"], res_r
    assert await db.get_bonus_balance(111) == 0, "бонус (300) должен был уйти на аренду"
    assert await db.get_tenge_balance(111) == 900, "тенге: 1000-100=900"

    r2 = await db.cancel_rental_with_refund(res_r["order_id"])
    assert r2["ok"] and r2["refunded"] == 400, r2
    assert await db.get_bonus_balance(111) == 300
    assert await db.get_tenge_balance(111) == 1000
    # Полный цикл списание+возврат вернул юзера ровно в исходное состояние
    # (300 бонус / 1000 тенге) — хороший sanity-check корректности разбивки.

    mismatches = await db.reconcile_balances()
    assert mismatches == [], mismatches

    print("ALL BONUS DB TESTS PASSED")


asyncio.run(main())
