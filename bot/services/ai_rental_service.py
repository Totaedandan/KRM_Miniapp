"""
Авто-разлогин арендованных ИИ-аккаунтов (ChatGPT/Claude) через Playwright.

В отличие от turnitin_service.py (один персистентный браузер на весь процесс,
без прокси) — здесь новый browser context на КАЖДУЮ задачу, обязательно
привязанный к прокси конкретного аккаунта (разные аккаунты сидят на разных IP,
общий браузер тут не годится).

Логин у этих аккаунтов — email + код с почты (тот же вебхук/таблица
otp_incoming_codes, что уже работает для арендатора через /api/rental/otp),
пароля нет. Поэтому при истечении аренды бот сам проходит ровно тот же путь,
что арендатор: открывает страницу логина, вводит email, запрашивает код,
ждёт его в БД, вводит, и только потом идёт в Settings жать логаут.

ВАЖНО: селекторы ChatGPT/Claude — первая версия по общим представлениям об
интерфейсе, ни разу не проверена вживую (в отличие от Turnitin, где всё
вылизано через реальную отладку). На каждом шаге при сбое сохраняется
скриншот в /tmp/debug_rental_*.png — по нему и донастраиваем, как с Turnitin.
"""
import asyncio
import logging
import os
import sys
import time
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import settings

logger = logging.getLogger(__name__)

LOGIN_URL = {
    "chatgpt": "https://chatgpt.com/",
    "claude":  "https://claude.ai/login",
}
SETTINGS_URL = {
    "chatgpt": "https://chatgpt.com/#settings",
    "claude": "https://claude.ai/settings/account",
}

OTP_WAIT_SEC     = 90   # сколько ждём код после запроса, прежде чем сдаться
OTP_POLL_SEC     = 4
LOGOUT_GRACE_SEC = 30   # пауза после истечения аренды перед попыткой логаута —
                        # снижает шанс столкнуться с ещё активным запросом кода арендатора


async def _debug_shot(page: Page, tag: str):
    """Скриншот в момент сбоя — без него донастройка селекторов вслепую."""
    try:
        path = f"/tmp/debug_rental_{tag}_{int(time.time())}.png"
        await page.screenshot(path=path, full_page=True)
        logger.warning("debug screenshot saved: %s", path)
    except Exception:
        pass


async def _wait_for_otp(email: str, requested_at: float) -> Optional[str]:
    """Поллим otp_incoming_codes, пока не придёт код НЕ старше времени запроса."""
    from database import db as database
    deadline = requested_at + OTP_WAIT_SEC
    while time.time() < deadline:
        window = int(time.time() - requested_at) + 10
        code = await database.get_recent_otp(email, window_sec=window)
        if code:
            return code
        await asyncio.sleep(OTP_POLL_SEC)
    return None


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


async def _login_via_otp_chatgpt(page: Page, email: str) -> bool:
    """Проходит логин ChatGPT email-кодом — тем же способом, что арендатор."""
    await page.goto(LOGIN_URL["chatgpt"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    try:
        login_btn = page.get_by_text("Log in", exact=False)
        if await login_btn.count() > 0:
            await login_btn.first.click(timeout=8000)
            await asyncio.sleep(2)
    except Exception:
        pass  # возможно уже на форме логина, а не на лендинге

    try:
        email_input = page.locator('input[type="email"], input[name="email"], #email-input')
        await email_input.first.fill(email, timeout=10000)
        await page.get_by_text("Continue", exact=False).first.click(timeout=8000)
    except Exception as e:
        await _debug_shot(page, "chatgpt_email_step")
        logger.warning(f"ChatGPT {email}: не удалось ввести email: {e}")
        return False

    await asyncio.sleep(2)
    # Аккаунт без пароля — иногда нужно явно выбрать "войти по коду" вместо пароля
    try:
        use_code = page.get_by_text("code", exact=False)
        if await use_code.count() > 0:
            await use_code.first.click(timeout=5000)
            await asyncio.sleep(1)
    except Exception:
        pass

    requested_at = time.time()
    code = await _wait_for_otp(email, requested_at)
    if not code:
        await _debug_shot(page, "chatgpt_otp_timeout")
        logger.warning(f"ChatGPT {email}: код для входа бота не пришёл за {OTP_WAIT_SEC}с")
        return False

    try:
        code_input = page.locator('input[type="text"], input[inputmode="numeric"], input[name*="code" i]')
        await code_input.first.fill(code, timeout=8000)
        await page.get_by_text("Continue", exact=False).first.click(timeout=8000)
    except Exception as e:
        await _debug_shot(page, "chatgpt_code_step")
        logger.warning(f"ChatGPT {email}: не удалось ввести код: {e}")
        return False

    await asyncio.sleep(3)
    ok = "login" not in page.url.lower() and "auth" not in page.url.lower()
    if not ok:
        await _debug_shot(page, "chatgpt_login_failed")
    return ok


async def _login_via_otp_claude(page: Page, email: str) -> bool:
    """Проходит логин Claude email-кодом — тем же способом, что арендатор."""
    await page.goto(LOGIN_URL["claude"], wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    try:
        email_input = page.locator('input[type="email"], input[name="email"]')
        await email_input.first.fill(email, timeout=10000)
        await page.get_by_text("Continue", exact=False).first.click(timeout=8000)
    except Exception as e:
        await _debug_shot(page, "claude_email_step")
        logger.warning(f"Claude {email}: не удалось ввести email: {e}")
        return False

    requested_at = time.time()
    code = await _wait_for_otp(email, requested_at)
    if not code:
        await _debug_shot(page, "claude_otp_timeout")
        logger.warning(f"Claude {email}: код для входа бота не пришёл за {OTP_WAIT_SEC}с")
        return False

    try:
        code_input = page.locator('input[type="text"], input[inputmode="numeric"]')
        await code_input.first.fill(code, timeout=8000)
        await page.get_by_text("Continue", exact=False).first.click(timeout=8000)
    except Exception as e:
        await _debug_shot(page, "claude_code_step")
        logger.warning(f"Claude {email}: не удалось ввести код: {e}")
        return False

    await asyncio.sleep(3)
    ok = "login" not in page.url.lower()
    if not ok:
        await _debug_shot(page, "claude_login_failed")
    return ok


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
        await _debug_shot(page, "chatgpt_logout_click")
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
        await _debug_shot(page, "claude_logout_click")
        logger.warning(f"Claude: кнопка разлогина не найдена/не кликнулась: {e}")
        return False


async def auto_logout(account: dict, service_type: str, proxy_url: Optional[str]) -> bool:
    """Разлогинить аккаунт со всех устройств. Логинится сам email-кодом (тем же
    путём, что арендатор), затем жмёт «Log out of all devices»/«all sessions».
    Возвращает True при успехе, False — если попытка не удалась (аккаунт уйдёт
    в maintenance, админ получит алерт — см. ai_rental_manager.py)."""
    email = account.get("email")
    if not email:
        logger.error("auto_logout: у аккаунта нет email — id=%s", account.get("id"))
        return False

    await _ensure_xvfb()
    proxy = _parse_proxy(proxy_url) if proxy_url else None
    if proxy_url and not proxy:
        logger.error(f"Не удалось разобрать proxy_url для аккаунта {email}")

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
            page = await context.new_page()
            await _solve_captcha_if_present(page)

            if service_type == "chatgpt":
                logged_in = await _login_via_otp_chatgpt(page, email)
            elif service_type == "claude":
                logged_in = await _login_via_otp_claude(page, email)
            else:
                logger.error(f"Неизвестный service_type: {service_type}")
                logged_in = False

            if not logged_in:
                await context.close()
                return False

            await _solve_captcha_if_present(page)
            if service_type == "chatgpt":
                ok = await _click_logout_chatgpt(page)
            else:
                ok = await _click_logout_claude(page)

            await context.close()
            return ok
        except Exception as e:
            logger.error(f"auto_logout error для {email}: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
