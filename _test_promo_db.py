"""Быстрый smoke-тест промокодов (временная БД). Запуск: python _test_promo_db.py"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "bot"))
from database import db


async def main():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    await db.init_db(path)

    await db.get_or_create_user(111, "buyer", "Buyer")
    await db.get_or_create_user(222, "second", "Second")

    # ── fixed: код не найден ──────────────────────────────────────────
    try:
        await db.apply_promo_fixed(111, "NOPE")
        assert False, "ожидали PromoNotFound"
    except db.PromoNotFound:
        pass

    # ── fixed: happy path (бонус идёт на bonus_balance, не tenge_balance) ──
    promo_id = await db.create_promo("welcome10", "fixed", 500, per_user_limit=1, total_limit=2)
    res = await db.apply_promo_fixed(111, "welcome10")  # проверка регистронезависимости
    assert res["bonus"] == 500 and res["new_balance"] == 500, res
    assert await db.get_bonus_balance(111) == 500
    assert await db.get_tenge_balance(111) == 0, "промо зашло не в тот баланс!"

    # ── fixed: повторное использование тем же юзером ──────────────────
    try:
        await db.apply_promo_fixed(111, "WELCOME10")
        assert False, "ожидали PromoAlreadyUsed"
    except db.PromoAlreadyUsed:
        pass

    # ── fixed: общий лимит (total_limit=2, использован 1) ─────────────
    res2 = await db.apply_promo_fixed(222, "WELCOME10")
    assert res2["bonus"] == 500 and await db.get_bonus_balance(222) == 500

    await db.get_or_create_user(333, "third", "Third")
    try:
        await db.apply_promo_fixed(333, "WELCOME10")
        assert False, "ожидали PromoExhausted"
    except db.PromoExhausted:
        pass
    assert await db.get_bonus_balance(333) == 0, "деньги начислены при исчерпанном лимите!"

    # ── fixed: истёкший промокод ───────────────────────────────────────
    past = (datetime.utcnow() - timedelta(days=2)).isoformat()
    past_end = (datetime.utcnow() - timedelta(days=1)).isoformat()
    await db.create_promo("OLD2024", "fixed", 100, starts_at=past, ends_at=past_end)
    try:
        await db.apply_promo_fixed(111, "OLD2024")
        assert False, "ожидали PromoNotActive"
    except db.PromoNotActive:
        pass

    # ── fixed: попытка применить percent-код как fixed ─────────────────
    await db.create_promo("PCT10", "percent", 10, per_user_limit=1)
    try:
        await db.apply_promo_fixed(111, "PCT10")
        assert False, "ожидали ValueError (не fixed)"
    except ValueError:
        pass

    # ── percent: превью не консьюмит ────────────────────────────────────
    preview = await db.validate_promo_for_topup(111, "pct10")
    assert preview["type"] == "percent" and preview["value"] == 10, preview
    preview2 = await db.validate_promo_for_topup(111, "pct10")  # повторный превью — всё ещё ок
    assert preview2["value"] == 10

    # ── percent: реальный редемпшен при пополнении (тоже в bonus_balance) ──
    bal_before = await db.get_bonus_balance(111)
    result = await db.redeem_promo_percent(111, "PCT10", base_amount=2000)
    assert result["applied"] and result["bonus"] == 200.0, result  # 10% от 2000
    assert await db.get_bonus_balance(111) == bal_before + 200

    # ── percent: повторный редемпшен тем же юзером — best-effort отказ ───
    result2 = await db.redeem_promo_percent(111, "PCT10", base_amount=1000)
    assert result2["applied"] is False and "использовали" in result2["reason"], result2
    assert await db.get_bonus_balance(111) == bal_before + 200, "бонус начислен повторно!"

    # ── percent: несуществующий код — best-effort, без исключения ────────
    result3 = await db.redeem_promo_percent(222, "GHOST", base_amount=1000)
    assert result3["applied"] is False and result3["reason"] == "Промокод не найден.", result3

    # ── список + статусы ──────────────────────────────────────────────
    promos = await db.list_promos()
    by_code = {p["code"]: p for p in promos}
    assert by_code["WELCOME10"]["status"] == "exhausted", by_code["WELCOME10"]
    assert by_code["OLD2024"]["status"] == "expired", by_code["OLD2024"]
    assert by_code["PCT10"]["status"] == "active", by_code["PCT10"]
    assert by_code["PCT10"]["activations_count"] == 1

    future = (datetime.utcnow() + timedelta(days=1)).isoformat()
    await db.create_promo("SOON", "fixed", 50, starts_at=future)
    promos2 = await db.list_promos()
    assert {p["code"]: p for p in promos2}["SOON"]["status"] == "scheduled"

    # ── удаление (soft-delete) ────────────────────────────────────────
    assert await db.delete_promo(promo_id) is True
    assert await db.delete_promo(promo_id) is False  # уже удалён
    remaining_codes = {p["code"] for p in await db.list_promos()}
    assert "WELCOME10" not in remaining_codes

    # ── дубликат кода при создании ──────────────────────────────────────
    try:
        await db.create_promo("pct10", "fixed", 10)  # то же имя, другой регистр
        assert False, "ожидали ValueError на дубликат"
    except ValueError:
        pass

    # ── get_all_user_ids для рассылки ───────────────────────────────────
    ids = await db.get_all_user_ids(exclude_banned=True)
    assert set(ids) == {111, 222, 333}, ids
    await db.set_banned(222, True)
    ids2 = await db.get_all_user_ids(exclude_banned=True)
    assert set(ids2) == {111, 333}, ids2

    # ── ledger должен сходиться после всех операций ─────────────────────
    mismatches = await db.reconcile_balances()
    assert mismatches == [], mismatches

    print("ALL PROMO DB TESTS PASSED")


asyncio.run(main())
