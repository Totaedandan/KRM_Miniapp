"""Быстрый smoke-тест аренды ИИ-аккаунтов v2 (временная БД). Запуск:
python _test_ai_rental_db.py"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bot"))
from database import db


async def main():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    await db.init_db(path)

    # ── Каталог: сервис + тариф (переиспользуем rental_services/tariffs) ────
    sid = await db.upsert_rental_service("ChatGPT Plus", "d", "openai")
    tid = await db.upsert_rental_tariff(sid, "1 час", 1, 500)

    # ── Прокси: max_accounts guard ───────────────────────────────────────
    pid = await db.create_ai_proxy("http://user:pass@1.2.3.4:8080", max_accounts=2)
    proxies = await db.list_ai_proxies()
    assert len(proxies) == 1 and proxies[0]["accounts_count"] == 0, proxies

    a1 = await db.add_ai_account(sid, "one@dom.kz", pid)
    a2 = await db.add_ai_account(sid, "two@dom.kz", pid)
    try:
        await db.add_ai_account(sid, "three@dom.kz", pid)
        assert False, "ожидали ProxyFull"
    except db.ProxyFull:
        pass

    # Дубликат email — ValueError
    try:
        await db.add_ai_account(sid, "ONE@dom.kz".lower(), None)
        assert False, "ожидали ValueError на дубликат email"
    except ValueError:
        pass

    # Несуществующий прокси
    try:
        await db.add_ai_account(sid, "four@dom.kz", proxy_id=9999)
        assert False, "ожидали ValueError (прокси не найден)"
    except ValueError:
        pass

    # Нельзя удалить прокси с привязанными аккаунтами
    assert await db.delete_ai_proxy(pid) is False

    # ── LRU-выбор: первым отдаётся никогда не использовавшийся / дольше отдыхавший ──
    await db.get_or_create_user(111, "buyer", "Buyer")
    await db.add_tenge(111, 2000, reason="topup", idempotency_key="t1")
    await db.add_bonus(111, 200, reason="promo_fixed", idempotency_key="b1")

    res = await db.create_ai_rental(111, "buyer", sid, tid, idempotency_key="r1", use_bonus=True)
    assert res["ok"] and not res["duplicate"], res
    assert res["email"] in ("one@dom.kz", "two@dom.kz")
    rented_email = res["email"]
    order_id = res["order_id"]
    assert await db.get_bonus_balance(111) == 0, "бонус (200) должен был списаться первым"
    assert await db.get_tenge_balance(111) == 1700, "тенге: 2000-300=1700"

    split = await db._get_charge_split(order_id, "ai_rental_charge")
    assert split.get("bonus") == 200 and split.get("tenge") == 300, split

    # Аккаунт захвачен — статус rented, у прокси занято 1 свободное место меньше
    acc = await db.get_ai_account(a1 if rented_email == "one@dom.kz" else a2)
    assert acc["status"] == "rented" and acc["current_order_id"] == order_id, acc

    # Идемпотентность — повтор с тем же ключом не списывает второй раз
    res_dup = await db.create_ai_rental(111, "buyer", sid, tid, idempotency_key="r1", use_bonus=True)
    assert res_dup["duplicate"] and res_dup["email"] == rented_email, res_dup
    assert await db.get_tenge_balance(111) == 1700

    # ── Второй юзер берёт последний свободный аккаунт ────────────────────
    await db.get_or_create_user(222, "second", "Second")
    await db.add_tenge(222, 1000, reason="topup", idempotency_key="t2")
    res2 = await db.create_ai_rental(222, "second", sid, tid, idempotency_key="r2", use_bonus=False)
    assert res2["ok"] and res2["email"] != rented_email

    # ── Третий юзер — аккаунтов больше нет ───────────────────────────────
    await db.get_or_create_user(333, "third", "Third")
    await db.add_tenge(333, 1000, reason="topup", idempotency_key="t3")
    try:
        await db.create_ai_rental(333, "third", sid, tid, idempotency_key="r3", use_bonus=False)
        assert False, "ожидали NoFreeAccount"
    except db.NoFreeAccount:
        pass
    assert await db.get_tenge_balance(333) == 1000, "деньги списались при провале захвата аккаунта!"

    # ── Владение email проверяется ────────────────────────────────────────
    owned = await db.get_active_ai_rental_by_email(111, rented_email)
    assert owned is not None
    not_owned = await db.get_active_ai_rental_by_email(333, rented_email)
    assert not_owned is None

    # ── OTP: свежий код виден, за окном — нет ────────────────────────────
    await db.insert_otp_code(rented_email, "123456")
    code = await db.get_recent_otp(rented_email, window_sec=120)
    assert code == "123456", code
    old_code = await db.get_recent_otp(rented_email, window_sec=0)
    assert old_code is None, "код за пределами окна не должен возвращаться"

    logs = await db.get_otp_logs()
    assert any(l["recipient_email"] == rented_email and l["otp_code"] == "123456" for l in logs)

    # ── Возврат при отмене — каждая валюта туда, откуда списалась ────────
    r = await db.cancel_ai_rental_with_refund(order_id)
    assert r["ok"] and r["refunded"] == 500, r
    assert await db.get_bonus_balance(111) == 200, "бонус не вернулся!"
    assert await db.get_tenge_balance(111) == 2000, "тенге не вернулся полностью!"

    acc_after = await db.get_ai_account(a1 if rented_email == "one@dom.kz" else a2)
    assert acc_after["status"] == "cooldown", acc_after

    # Повторная отмена — уже финализирован
    r_dup = await db.cancel_ai_rental_with_refund(order_id)
    assert r_dup["ok"] is False and r_dup["error"] == "already_final", r_dup

    # ── Истечение: get_due_ai_rentals / expire_ai_rental ──────────────────
    due = await db.get_due_ai_rentals("2999-01-01T00:00:00")
    ids = {r["id"] for r in due}
    assert res2["order_id"] in ids

    expired = await db.expire_ai_rental(res2["order_id"])
    assert expired is not None and expired["id"] == res2["order_id"]
    # Повторный expire — идемпотентно-безопасно (гонка)
    assert await db.expire_ai_rental(res2["order_id"]) is None

    # ── Cooldown → available (окно воркера) ───────────────────────────────
    due_cooldown = await db.get_cooldown_accounts_due("2999-01-01T00:00:00")
    assert any(a["id"] == acc_after["id"] for a in due_cooldown)
    await db.update_ai_account_status(acc_after["id"], "available")
    refreshed = await db.get_ai_account(acc_after["id"])
    assert refreshed["status"] == "available"

    # ── Нельзя удалить аккаунт, пока он арендован ─────────────────────────
    rented_now = a1 if a2 == (a1 if rented_email == "one@dom.kz" else a2) else a2
    # (второй аккаунт всё ещё в статусе expired-но-rented до реального разлогина воркером)
    still_rented_acc = await db.get_ai_account(a2 if a1 != acc_after["id"] else a1)
    if still_rented_acc["status"] == "rented":
        assert await db.delete_ai_account(still_rented_acc["id"]) is False

    # ── Каталог наличия ────────────────────────────────────────────────────
    catalog = await db.get_ai_services_catalog()
    svc = next(s for s in catalog if s["id"] == sid)
    assert svc["tariffs"], "тариф должен присутствовать в каталоге"

    # ── Ledger должен сходиться после всех операций ────────────────────────
    mismatches = await db.reconcile_balances()
    assert mismatches == [], mismatches

    print("ALL AI RENTAL DB TESTS PASSED")


asyncio.run(main())
