"""
Авто-разлогин арендованных ИИ-аккаунтов (ChatGPT/Claude) через Playwright.

В отличие от turnitin_service.py (один персистентный браузер на весь процесс,
без прокси) — здесь новый browser context на КАЖДУЮ задачу, обязательно
привязанный к прокси конкретного аккаунта (разные аккаунты сидят на разных IP,
общий браузер тут не годится).

ВАЖНО: селекторы ChatGPT/Claude settings-страниц ниже — первая версия по
документации/общим представлениям об интерфейсе. Разметка обоих сервисов
меняется без предупреждения и заранее не протестирована вживую (в отличие от
Turnitin, где селекторы вылизаны через реальную отладку) — почти наверняка
потребуется донастройка после первого реального прогона на проде.
"""
import asyncio
import json
import logging
import os
import sys
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import settings

logger = logging.getLogger(__name__)

SETTINGS_URL = {
    "chatgpt": "https://chatgpt.com/#settings",
    "claude": "https://claude.ai/settings/account",
}


def _parse_proxy(proxy_url: str) -> Optional[dict]:
    """'http://user:pass@ip:port' → {'server','username','password'} для Playwright."""
    if not proxy_url:
        return None
    u = urlparse(proxy_url)
    if not u.hostname:
        return None
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy["username"] = u.username
    if u.password:
        proxy["password"] = u.password
    return proxy


async def _ensure_xvfb():
    """Тот же приём, что в turnitin_service.py — в Docker Xvfb уже поднят через
    entrypoint.sh, здесь только страховка на случай локального запуска."""
    if sys.platform != "linux" or os.environ.get("DISPLAY"):
        return
    import subprocess
    display = ":99"
    try:
        result = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
        if result.returncode != 0:
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x1200x24", "-ac"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await asyncio.sleep(1.5)
        os.environ["DISPLAY"] = display
    except FileNotFoundError:
        logger.error("Xvfb не найден — установите пакет xvfb")


async def _solve_captcha_if_present(page: Page) -> None:
    """Best-effort: детект Cloudflare Turnstile. Полноценного авто-решения через
    2Captcha в первой версии нет — при обнаружении просто логируем предупреждение,
    чтобы задача не тихо зависла, а вызывающий код увидел причину неудачи."""
    if not settings.TWOCAPTCHA_API_KEY:
        return
    try:
        frame = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
        if await frame.locator("body").count() > 0:
            logger.warning(
                "Обнаружена капча (Cloudflare Turnstile) — автоматическое решение "
                "через 2Captcha пока не реализовано, нужна ручная проверка аккаунта"
            )
    except Exception:
        pass


async def _click_logout_chatgpt(page: Page) -> bool:
    await page.goto(SETTINGS_URL["chatgpt"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    try:
        await page.get_by_text("Log out of all devices", exact=False).click(timeout=8000)
        await asyncio.sleep(1)
        confirm = page.get_by_role("button", name="Log out", exact=False)
        if await confirm.count() > 0:
            await confirm.first.click(timeout=5000)
        return True
    except Exception as e:
        logger.warning(f"ChatGPT: кнопка разлогина не найдена/не кликнулась: {e}")
        return False


async def _click_logout_claude(page: Page) -> bool:
    await page.goto(SETTINGS_URL["claude"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    try:
        await page.get_by_text("Log out all sessions", exact=False).click(timeout=8000)
        await asyncio.sleep(1)
        confirm = page.get_by_role("button", name="Log out", exact=False)
        if await confirm.count() > 0:
            await confirm.first.click(timeout=5000)
        return True
    except Exception as e:
        logger.warning(f"Claude: кнопка разлогина не найдена/не кликнулась: {e}")
        return False


async def auto_logout(account: dict, service_type: str, proxy_url: Optional[str]) -> bool:
    """Разлогинить аккаунт со всех устройств. `account` — строка из ai_accounts
    (нужны email, cookies_data). Возвращает True при успехе/отсутствии активной
    сессии (нечего разлогинивать), False — если попытка не удалась."""
    if not account.get("cookies_data"):
        logger.info(f"{account.get('email')}: сохранённой сессии нет — нечего разлогинивать")
        return True

    await _ensure_xvfb()
    proxy = _parse_proxy(proxy_url) if proxy_url else None
    if proxy_url and not proxy:
        logger.error(f"Не удалось разобрать proxy_url для аккаунта {account.get('email')}")

    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1280,1200",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if sys.platform == "linux":
        args += ["--window-position=0,0", "--disable-gpu", "--disable-software-rasterizer"]
    else:
        args.append("--window-position=-32000,-32000")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=args)
        try:
            context = await browser.new_context(proxy=proxy) if proxy else await browser.new_context()
            try:
                cookies = json.loads(account["cookies_data"])
                await context.add_cookies(cookies)
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"{account.get('email')}: не удалось разобрать cookies_data: {e}")
                await context.close()
                return False

            page = await context.new_page()
            await _solve_captcha_if_present(page)

            if service_type == "chatgpt":
                ok = await _click_logout_chatgpt(page)
            elif service_type == "claude":
                ok = await _click_logout_claude(page)
            else:
                logger.error(f"Неизвестный service_type: {service_type}")
                ok = False

            await context.close()
            return ok
        except Exception as e:
            logger.error(f"auto_logout error для {account.get('email')}: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
