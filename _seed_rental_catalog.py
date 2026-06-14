"""Мок-каталог аренды ИИ: сервисы + тарифы + ДЕМО-аккаунты (фейковые креды!).

Запуск: winvenv\\Scripts\\python.exe _seed_rental_catalog.py
БД берётся из .env (DATABASE_PATH). Идемпотентно: сервис с таким именем
уже есть → пропускается. Перед продом демо-аккаунты замените реальными
(или удалите в админке «Аренда: склад»).
"""
import asyncio, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))

from config import settings
from database import db

# icon: ключ из BRANDS в mini_app/index.html (официальный логотип) или эмодзи
SERVICES = [
    ("ChatGPT Plus", "openai", "Выдача готового аккаунта. Абсолютно без ограничений.",
     [("6 часов", 6, 490), ("Неделя", 168, 1990), ("Месяц", 720, 3500)], 3),
    ("Claude Pro", "claude", "Claude Pro аккаунт, выдача только 1 пользователю. Лимит не делится ни с кем.",
     [("5 часов", 5, 790), ("Месяц", 720, 6990)], 1),
    ("Grok Super", "grok", "Выдача 1 аккаунт = 1 пользователь. Лимит не делится ни с кем.",
     [("3 часа", 3, 490), ("Месяц", 720, 6990)], 2),
    ("Quillbot", "🦜", "Paraphrase, AI humanizer, Grammar checker, Summarizer, AI detector, Plagiarism checker.",
     [("3 часа", 3, 590), ("Неделя", 168, 1490)], 2),
    ("Perplexity Pro", "perplexity", "AI-поиск с источниками, Pro Search, неограниченные запросы.",
     [("6 часов", 6, 490), ("Месяц", 720, 1990)], 2),
    ("Gemini Pro", "gemini", "Подключение Gemini Pro на ваш персональный аккаунт. Никаких ограничений.",
     [("Месяц", 720, 1990)], 1),
    ("Figma Edu", "figma", "Весь функционал Figma Professional. Доступны все услуги Figma.",
     [("Год", 8760, 19990)], 0),   # «Нет в наличии» — для демонстрации waitlist
    ("CapCut Pro", "✂️", "Выдача готового личного аккаунта. Никаких ограничений.",
     [("Неделя", 168, 990), ("Месяц", 720, 2990)], 2),
    ("Netflix 4K Premium", "netflix", "Собственный профиль с кодом, никаких ограничений.",
     [("Месяц", 720, 1600)], 1),
    ("Microsoft 365 Premium", "microsoft", "Все сервисы Microsoft в одной подписке. Подключение на личную почту.",
     [("Год", 8760, 9990)], 1),
    ("Canva Pro", "canva", "Все Pro-шаблоны, фоны и инструменты бренда.",
     [("Месяц", 720, 1490)], 2),
    ("Midjourney", "midjourney", "Генерация изображений без лимитов, fast-режим.",
     [("Месяц", 720, 4990)], 1),
]


async def main():
    await db.init_db(settings.DATABASE_PATH)
    existing = {s["name"] for s in await db.get_rental_services_admin()}
    added = skipped = 0
    for order, (name, icon, desc, tariffs, n_accounts) in enumerate(SERVICES):
        if name in existing:
            skipped += 1
            continue
        sid = await db.upsert_rental_service(
            name=name, description=desc, icon=icon, sort_order=order)
        for t_order, (t_name, hours, price) in enumerate(tariffs):
            await db.upsert_rental_tariff(
                service_id=sid, name=t_name, duration_hours=hours,
                price=price, sort_order=t_order)
        slug = name.lower().replace(" ", "")[:10]
        for i in range(1, n_accounts + 1):
            await db.add_rental_account(
                sid, f"demo{i}@{slug}.mock", f"mock-pass-{sid}{i:02d}",
                note="ДЕМО — заменить перед продом")
        added += 1
        print(f"+ {name}: тарифов {len(tariffs)}, демо-аккаунтов {n_accounts}")
    print(f"\nГотово: добавлено {added}, пропущено (уже были) {skipped}. БД: {settings.DATABASE_PATH}")
    print("ВНИМАНИЕ: креды демо-аккаунтов фейковые — перед продом заменить/удалить.")


asyncio.run(main())
