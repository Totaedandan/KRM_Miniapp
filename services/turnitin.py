import subprocess
"""
Turnitin automation через Playwright.
Логин → View → три точки → Submit file → загрузка → Upload and Preview →
Submit #1 → Submit #2 → polling scores → Download report → Request deletion
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import async_playwright, Page, Browser

logger = logging.getLogger(__name__)

BASE_URL = "https://www.turnitin.com"
CHROMIUM_PATH = "/Users/totae/Library/Caches/ms-playwright/chromium-1187/chrome-mac/Chromium.app/Contents/MacOS/Chromium"

# Найти элемент по точному тексту через Shadow DOM
FIND_BY_TEXT_JS = """(btnText) => {
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
            if (el.textContent.trim() === btnText) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
            }
        }
        return null;
    }
    return deepFind(document);
}"""

# Найти кнопку три точки (actions menu) — только в строке сабмита (y > 300)
FIND_THREE_DOTS_JS = """() => {
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
            if (el.tagName === 'BUTTON') {
                const aria = el.getAttribute('aria-label') || '';
                const data = el.getAttribute('data-px') || '';
                if (aria.includes('Display actions menu') || data === 'more-options') {
                    const r = el.getBoundingClientRect();
                    // y > 300 чтобы не поймать кнопки в шапке страницы
                    if (r.width > 0 && r.y > 300) return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
                }
            }
        }
        return null;
    }
    return deepFind(document);
}"""

# Найти активную filled-кнопку Submit
FIND_FILLED_BTN_JS = """() => {
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
            if (el.tagName === 'BUTTON' && el.className.includes('tdl-button-filled') && !el.disabled) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
            }
        }
        return null;
    }
    return deepFind(document);
}"""

# Найти % scores (готовые отчёты Similarity)
FIND_SCORES_JS = r"""() => {
    const res = [];
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) deepFind(el.shadowRoot);
            if (el.children.length === 0) {
                const t = el.textContent.trim();
                if (/^[0-9]+%$/.test(t)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0) res.push({text: t, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
                }
            }
        }
    }
    deepFind(document);
    return res;
}"""

# Найти URL отчёта в lti frame (href или атрибуты с integrity.turnitin.com)
FIND_REPORT_URL_JS = """() => {
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
            if (el.tagName === 'A' && el.href && el.href.includes('integrity.turnitin.com'))
                return el.href;
            for (const attr of (el.attributes || [])) {
                if (attr.value && attr.value.includes('integrity.turnitin.com') && attr.value.startsWith('http'))
                    return attr.value;
            }
        }
        return null;
    }
    return deepFind(document);
}"""

# Найти кнопку Download на странице отчёта
FIND_DOWNLOAD_BTN_JS = """() => {
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
            const datapx = el.getAttribute('withdatapx') || '';
            if (datapx.includes('Download')) {
                const r = el.getBoundingClientRect();
                if (r.width > 0) return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
            }
        }
        return null;
    }
    return deepFind(document);
}"""

# Найти пункты меню скачивания
FIND_DOWNLOAD_ITEMS_JS = """() => {
    const res = [];
    const names = ['Similarity Report','AI Writing Report','Grading and Feedback Report','Original File'];
    function deepFind(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) deepFind(el.shadowRoot);
            const t = el.textContent.trim();
            if (names.includes(t)) {
                const r = el.getBoundingClientRect();
                if (r.width > 0) res.push({text: t, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
            }
        }
    }
    deepFind(document);
    return res;
}"""


class TurnitinError(Exception):
    pass


class TurnitinService:

    def __init__(self):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def _get_browser(self) -> Browser:
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            import sys
            args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,1200",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if sys.platform == "linux":
                # На Linux в Docker: Xvfb уже запущен через entrypoint.sh
                await self._ensure_xvfb()
                args.extend([
                    "--window-position=0,0",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                ])
                self._browser = await self._pw.chromium.launch(
                    headless=False,
                    args=args,
                )
            else:
                # На Mac: headless=False, окно за экраном
                args.append("--window-position=-32000,-32000")
                self._browser = await self._pw.chromium.launch(
                    headless=False,
                    executable_path=CHROMIUM_PATH,
                    args=args,
                )
                await self._hide_browser_macos()
        return self._browser

    async def _ensure_xvfb(self):
        """Запускаем Xvfb если ещё не запущен и устанавливаем DISPLAY."""
        import os, subprocess
        if os.environ.get("DISPLAY"):
            logger.info("DISPLAY already set: %s", os.environ["DISPLAY"])
            return
        display = ":99"
        try:
            # Проверяем не запущен ли уже
            result = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
            if result.returncode != 0:
                subprocess.Popen(
                    ["Xvfb", display, "-screen", "0", "1280x1200x24", "-ac"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(1.5)
            os.environ["DISPLAY"] = display
            logger.info("Xvfb started, DISPLAY=%s", display)
        except FileNotFoundError:
            logger.error("Xvfb not found! Run: apt-get install xvfb")
            raise

    async def _hide_browser_macos(self):
        """Скрываем окно Chromium через AppleScript на Mac."""
        import subprocess
        try:
            script = 'tell application "System Events" to set visible of every process whose name contains "Chromium" to false'
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
            )
            logger.info("Chromium hidden via AppleScript")
        except Exception as e:
            logger.debug("Could not hide Chromium: %s", e)

    async def _get_iframe_offset(self, page):
        return await page.evaluate("""() => {
            for (const f of document.querySelectorAll('iframe')) {
                const r = f.getBoundingClientRect();
                if (r.width > 100) return {x: Math.round(r.x), y: Math.round(r.y)};
            }
            return {x: 0, y: 0};
        }""")

    async def _get_lti(self, page):
        for frame in page.frames:
            if "lti.int.turnitin.com/launch" in frame.url:
                return frame
        return None

    async def _do_login(self, page: Page, email: str, password: str):
        logger.info("Logging in as %s...", email)
        await page.context.clear_cookies()
        await page.goto(f"{BASE_URL}/login_page.asp", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        await page.click(
            'input[type="email"], input[name="email"], #email, input[placeholder*="email" i]',
            timeout=8000
        )
        await asyncio.sleep(0.3)
        await page.keyboard.press("Meta+a")
        await page.keyboard.type(email, delay=50)
        await asyncio.sleep(0.3)

        await page.click(
            'input[type="password"], input[name="password"], #password',
            timeout=8000
        )
        await asyncio.sleep(0.3)
        await page.keyboard.press("Meta+a")
        await page.keyboard.type(password, delay=50)
        await asyncio.sleep(0.3)

        # ✅ no_wait_after=True — не ждём навигацию внутри click()
        # Потом ждём отдельно с большим таймаутом
        await page.click(
            'button:has-text("Log in"), button[type="submit"], input[type="submit"]',
            timeout=8000,
            no_wait_after=True,  # <-- ключевое изменение
        )

        # ✅ Ждём навигацию отдельно — 30 сек достаточно для медленного Turnitin
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass  # Если уже загрузилась — игнорируем

        await asyncio.sleep(3)

        logger.info("After login URL: %s", page.url)
        if "login_page" in page.url:
            raise TurnitinError("Неверный логин или пароль Turnitin")

        logger.info("Login OK")

    async def _wait_lti(self, page, wait: int = 30):
        for _ in range(wait):
            await asyncio.sleep(1)
            lti = await self._get_lti(page)
            if lti:
                return lti
        raise TurnitinError("LTI iframe не найден")

    async def process(
        self,
        order_id: int,
        file_path: str,
        file_name: str,
        report_type: str,
        email: str,
        password: str,
        class_id: str,
        assign_id: str,
        reports_dir: str,
        poll_interval: int = 30,
        timeout: int = 1800,
        max_retries: int = 3,
    ) -> Tuple[Optional[str], Optional[str]]:

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                result = await self._process_once(
                    order_id=order_id,
                    file_path=file_path,
                    file_name=file_name,
                    report_type=report_type,
                    email=email,
                    password=password,
                    class_id=class_id,
                    assign_id=assign_id,
                    reports_dir=reports_dir,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
                return result
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Retry при временных ошибках (логин/сеть/LTI/три точки)
                retryable = any(x in err_str for x in [
                    "Timeout", "timeout", "LTI iframe", "три точки", "login_page",
                    "Не найдена кнопка три точки", "LTI frame",
                    "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ServerDisconnected",
                    "net::", "Name or service not known",
                ]) or isinstance(e, TurnitinError)
                if retryable and attempt < max_retries:
                    wait = 20 * attempt  # 20s, 40s между попытками
                    logger.warning(
                        "Order %s attempt %d/%d failed (%s) — retrying in %ds",
                        order_id, attempt, max_retries, type(e).__name__, wait
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

        raise last_error

    async def _process_once(
        self,
        order_id: int,
        file_path: str,
        file_name: str,
        report_type: str,
        email: str,
        password: str,
        class_id: str,
        assign_id: str,
        reports_dir: str,
        poll_interval: int = 30,
        timeout: int = 1800,
    ) -> Tuple[Optional[str], Optional[str]]:

        async with self._lock:
            browser = await self._get_browser()
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 1200},
                accept_downloads=True,
                locale="en-US",
            )
            page = await ctx.new_page()
            out_dir = Path(reports_dir) / str(order_id)
            out_dir.mkdir(parents=True, exist_ok=True)

            try:
                await self._do_login(page, email, password)
                await self._upload(page, file_path, file_name, order_id, class_id)

                sim_path = None
                ai_path = None

                if report_type in ("similarity", "both"):
                    sim_path = await self._get_report(
                        page, "similarity", out_dir, poll_interval, timeout, order_id
                    )

                if report_type in ("ai", "both"):
                    ai_path = await self._get_report(
                        page, "ai", out_dir, poll_interval, timeout, order_id
                    )

                # Удаляем только после успешного скачивания
                try:
                    await self._delete(page)
                except Exception as e:
                    logger.warning("Delete failed: %s", e)

                return sim_path, ai_path

            except Exception:
                raise
            finally:
                # ctx.close может упасть если браузер уже закрылся — игнорируем
                try:
                    await ctx.close()
                except Exception:
                    pass
                # Закрываем браузер после каждого заказа — новый заказ = чистая сессия
                try:
                    if self._browser and self._browser.is_connected():
                        await self._browser.close()
                        self._browser = None
                        logger.info("Browser closed after order %s", order_id)
                except Exception:
                    self._browser = None
                    logger.info("Browser closed (forced) after order %s", order_id)
                # Убиваем zombie-процессы chromium которые могли зависнуть
                try:
                    subprocess.run(["pkill", "-f", "chrome-linux"], capture_output=True)
                except Exception:
                    pass

    async def _upload(
        self, page: Page, file_path: str, file_name: str,
        order_id: int, class_id: str
    ):
        logger.info("Order %s: uploading %s", order_id, file_name)

        await page.goto(
            f"{BASE_URL}/class/{class_id}/instructor_home?lang=en_us",
            wait_until="domcontentloaded"
        )
        await asyncio.sleep(3)

        if "login" in page.url.lower():
            raise TurnitinError(f"Редирект на логин: {page.url}")

        await page.click('a:has-text("View")', timeout=8000)
        logger.info("Clicked View, loading LTI iframe...")

        lti = await self._wait_lti(page, wait=60)
        logger.info("LTI frame loaded: %s", lti.url)

        # Ждём три точки
        btn_coords = None
        for i in range(60):
            btn_coords = await lti.evaluate(FIND_THREE_DOTS_JS)
            if btn_coords:
                logger.info("Three dots found after %ds: %s", i, btn_coords)
                break
            if i % 10 == 9:
                logger.info("Still waiting for three dots... %ds", i+1)
            await asyncio.sleep(1)

        if not btn_coords:
            await page.screenshot(path=f"/tmp/debug_threedots_{order_id}.png")
            raise TurnitinError("Не найдена кнопка три точки")

        offset = await self._get_iframe_offset(page)
        ox, oy = offset['x'], offset['y']
        logger.info("iframe offset: %s,%s", ox, oy)

        # Кликаем три точки через JavaScript (надёжнее чем mouse.click в Docker)
        await lti.evaluate("""() => {
            function deepFind(root) {
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                    if (el.tagName === 'BUTTON') {
                        const aria = el.getAttribute('aria-label') || '';
                        const data = el.getAttribute('data-px') || '';
                        if (aria.includes('Display actions menu') || data === 'more-options') {
                            el.click();
                            return true;
                        }
                    }
                }
                return false;
            }
            return deepFind(document);
        }""")
        logger.info("JS-clicked three dots")
        await asyncio.sleep(2)

        # Ищем пункты меню в lti Shadow DOM
        FIND_MENU_ITEM_JS = """(text) => {
            function deepFind(root) {
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                    if (el.textContent.trim() === text) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
                    }
                }
                return null;
            }
            return deepFind(document);
        }"""

        resubmit = await lti.evaluate(FIND_MENU_ITEM_JS, "Resubmit file")
        submit_file = await lti.evaluate(FIND_MENU_ITEM_JS, "Submit file")
        logger.info("resubmit=%s submit_file=%s", resubmit, submit_file)

        if resubmit:
            # Resubmit координаты из lti Shadow DOM — добавляем iframe offset
            await page.mouse.click(resubmit['x'] + ox, resubmit['y'] + oy)
            logger.info("Clicked Resubmit file at abs(%s,%s)", resubmit['x']+ox, resubmit['y']+oy)
            await asyncio.sleep(2)
            await page.screenshot(path=f"/tmp/debug_after_resubmit_click_{order_id}.png")
            await asyncio.sleep(2)

            # Ищем Confirm во всех контекстах: lti shadow DOM и page
            confirm = None
            confirm_in_lti = False
            for i in range(15):
                # 1. Ищем в lti через JS с полным обходом Shadow DOM
                c = await lti.evaluate("""() => {
                    function deepFind(root) {
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                            const t = (el.textContent || '').trim();
                            if (t === 'Confirm' && el.getBoundingClientRect().width > 0)
                                return {x: Math.round(el.getBoundingClientRect().x + el.getBoundingClientRect().width/2),
                                        y: Math.round(el.getBoundingClientRect().y + el.getBoundingClientRect().height/2)};
                        }
                        return null;
                    }
                    return deepFind(document);
                }""")
                if c:
                    confirm = c
                    confirm_in_lti = True
                    logger.info("Confirm found in lti shadow DOM after %ds: %s", i, c)
                    break
                # 2. Ищем в page
                c2 = await page.evaluate("""() => {
                    function deepFind(root) {
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                            const t = (el.textContent || '').trim();
                            if (t === 'Confirm' && el.getBoundingClientRect().width > 0)
                                return {x: Math.round(el.getBoundingClientRect().x + el.getBoundingClientRect().width/2),
                                        y: Math.round(el.getBoundingClientRect().y + el.getBoundingClientRect().height/2)};
                        }
                        return null;
                    }
                    return deepFind(document);
                }""")
                if c2:
                    confirm = c2
                    confirm_in_lti = False
                    logger.info("Confirm found in page after %ds: %s", i, c2)
                    break
                await asyncio.sleep(1)

            if confirm:
                if confirm_in_lti:
                    await page.mouse.click(confirm['x'] + ox, confirm['y'] + oy)
                    logger.info("Clicked Confirm (lti) at abs(%s,%s)", confirm['x']+ox, confirm['y']+oy)
                else:
                    await page.mouse.click(confirm['x'], confirm['y'])
                    logger.info("Clicked Confirm (page) at abs(%s,%s)", confirm['x'], confirm['y'])
                await asyncio.sleep(5)
            else:
                # Последний резерв — кликаем по известным координатам диалога Confirm
                # Из скриншота: кнопка Confirm ~(757, 655) в абсолютных координатах страницы
                logger.warning("Confirm not found by DOM, clicking by known coords")
                await page.mouse.click(757, 655)
                await asyncio.sleep(5)

        elif submit_file:
            await page.mouse.click(submit_file['x'] + ox, submit_file['y'] + oy)
            logger.info("Clicked Submit file at abs(%s,%s)", submit_file['x']+ox, submit_file['y']+oy)
            await asyncio.sleep(4)

        else:
            await page.screenshot(path=f"/tmp/debug_no_menu_{order_id}.png")
            raise TurnitinError("Меню не открылось — не найдены Submit file / Resubmit file")

        # Делаем file input видимым — ищем в lti и в page (после Resubmit форма может быть в page)
        FIX_INPUTS_JS = """() => {
            function fix(root) {
                root.querySelectorAll('input[type=file]').forEach(el => {
                    el.style.cssText += ';display:block!important;opacity:1!important;visibility:visible!important;width:1px!important;height:1px!important;';
                    el.removeAttribute('hidden');
                });
                root.querySelectorAll('*').forEach(el => { if(el.shadowRoot) fix(el.shadowRoot); });
            }
            fix(document);
        }"""

        file_input = None
        for i in range(30):
            # Ищем в lti
            await lti.evaluate(FIX_INPUTS_JS)
            file_input = await lti.query_selector('input[type="file"]')
            if file_input:
                logger.info("File input found in lti after %ds", i)
                break
            # Ищем в page
            await page.evaluate(FIX_INPUTS_JS)
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                logger.info("File input found in page after %ds", i)
                break
            if i % 5 == 4:
                await page.screenshot(path=f"/tmp/debug_waiting_input_{order_id}_{i}.png")
                logger.info("Still waiting for file input... %ds", i+1)
            await asyncio.sleep(1)

        if not file_input:
            await page.screenshot(path=f"/tmp/debug_no_input_{order_id}.png")
            raise TurnitinError("Не найдено поле загрузки файла")

        await file_input.set_input_files(file_path)
        logger.info("File attached")
        await asyncio.sleep(2)

        # Заголовок
        try:
            title_input = await lti.query_selector('input[type="text"]')
            if title_input:
                await title_input.fill(f"Order_{order_id}")
        except Exception as e:
            logger.warning("Title fill failed: %s", e)
        await asyncio.sleep(1)

        # Upload and Preview
        up = await lti.evaluate(FIND_BY_TEXT_JS, "Upload and Preview")
        if not up:
            raise TurnitinError("Кнопка Upload and Preview не найдена")
        await page.mouse.click(up['x'] + ox, up['y'] + oy)
        logger.info("Clicked Upload and Preview, waiting for preview...")

        # Submit #1
        sub1 = None
        for i in range(120):
            sub1 = await lti.evaluate(FIND_FILLED_BTN_JS)
            if sub1:
                logger.info("Submit #1 ready after %ds", i)
                break
            await asyncio.sleep(1)

        if not sub1:
            raise TurnitinError("Кнопка Submit (превью) не появилась за 2 мин")

        await page.mouse.click(sub1['x'] + ox, sub1['y'] + oy)
        logger.info("Clicked Submit #1 (preview)")

        # Submit #2
        await asyncio.sleep(3)
        sub2 = None
        for i in range(30):
            sub2 = await lti.evaluate(FIND_FILLED_BTN_JS)
            if sub2:
                logger.info("Submit #2 ready after %ds", i)
                break
            await asyncio.sleep(1)

        if sub2:
            await page.mouse.click(sub2['x'] + ox, sub2['y'] + oy)
            logger.info("Clicked Submit #2 (final)")
        else:
            logger.info("No Submit #2 — single-step submission")

        # Ждём закрытия модала
        for _ in range(30):
            if not await lti.evaluate(FIND_FILLED_BTN_JS):
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)
        logger.info("Upload complete")

    async def _get_report(
        self, page: Page, rtype: str, out_dir: Path,
        poll_interval: int, timeout: int, order_id: int = 0
    ) -> str:
        label = "Similarity" if rtype == "similarity" else "AI Writing"
        logger.info("Waiting for %s report...", label)
        out_path = out_dir / f"{rtype}_report.pdf"
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                lti = await self._get_lti(page)
                if lti:
                    ready = await self._is_report_ready(lti, rtype)
                    if ready:
                        logger.info("%s report ready, downloading...", label)
                        result = await self._download_report(page, lti, rtype, out_path)
                        if result:
                            return result
            except Exception as e:
                err_str = str(e)
                if "Target page" in err_str or "TargetClosed" in err_str or "browser has been closed" in err_str:
                    # Браузер упал — бросаем выше чтобы retry сработал
                    raise
                logger.warning("Poll iteration error (order %s): %s", order_id, e)

            logger.info("%s not ready for order %s, waiting %ds...", label, order_id, poll_interval)
            await asyncio.sleep(poll_interval)

        raise TurnitinError(f"{label} отчёт не готов за {timeout // 60} мин")

    async def _is_report_ready(self, lti, rtype: str) -> bool:
        """
        Similarity: ждём число% (например 12%).
        AI Writing: ждём *% или число% — оба означают что отчёт сгенерирован.
        *% = отчёт готов, AI не обнаружен (0%). Это тоже готовый результат!
        """
        if rtype == "similarity":
            scores = await lti.evaluate(FIND_SCORES_JS)
            return len(scores) > 0
        else:
            # AI Writing: *% означает готов (0% AI), число% тоже готов
            result = await lti.evaluate(r"""() => {
                function deepFind(root) {
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                        const t = el.textContent.trim();
                        // *% = готов (0% AI обнаружено), число% = готов с результатом
                        if (t === '*%' || /^[0-9]+%$/.test(t)) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0) return t;  // возвращаем сам текст
                        }
                    }
                    return null;
                }
                return deepFind(document);
            }""")
            if result:
                logger.info("AI Writing score found: '%s'", result)
            return result is not None

    async def _download_report(self, page, lti, rtype: str, out_path: Path) -> Optional[str]:
        """Кликаем на % → ловим новую страницу → Download → PDF."""
        report_page = None
        try:
            offset = await self._get_iframe_offset(page)
            ox, oy = offset['x'], offset['y']

            scores = await lti.evaluate(FIND_SCORES_JS)
            ai_btn = None

            if not scores:
                # Для AI Writing (*%) — ищем кнопку *%
                ai_btn = await lti.evaluate(r"""() => {
                    function deepFind(root) {
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                            const t = el.textContent.trim();
                            if (t === '*%') {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0) return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), text: t};
                            }
                        }
                        return null;
                    }
                    return deepFind(document);
                }""")
                if ai_btn:
                    scores = [ai_btn]
                else:
                    logger.warning("No score button found (neither %%d nor *%%)")
                    return None

            pct = scores[0]
            is_star = pct.get('text') == '*%'
            logger.info("Clicking '%s' at lti(%s,%s) [is_star=%s]", pct['text'], pct['x'], pct['y'], is_star)

            # Ловим новую страницу — регистрируем listener ДО клика
            new_page_future = asyncio.get_event_loop().create_future()
            def on_page(p):
                if not new_page_future.done():
                    new_page_future.set_result(p)
            page.context.on("page", on_page)

            # ВАЖНО: используем mouse.click (не JS click!) — только настоящий клик мыши
            # триггерит window.open() для открытия отчёта в новой вкладке
            abs_x = pct['x'] + ox
            abs_y = pct['y'] + oy
            logger.info("mouse.click at abs(%s,%s)", abs_x, abs_y)
            await page.mouse.click(abs_x, abs_y)

            try:
                report_page = await asyncio.wait_for(new_page_future, timeout=30)
                await report_page.wait_for_load_state("domcontentloaded", timeout=20000)
                logger.info("New page opened: %s", report_page.url)
            except asyncio.TimeoutError:
                logger.warning("New page did not open after clicking %s, trying fallbacks...", pct['text'])
                try:
                    page.context.remove_listener("page", on_page)
                except Exception:
                    pass

                # Fallback 1: прямой URL из lti DOM
                report_url = await lti.evaluate(FIND_REPORT_URL_JS)
                if report_url:
                    logger.info("Found report URL in DOM: %s", report_url)
                    report_page = await page.context.new_page()
                    await report_page.goto(report_url, wait_until="domcontentloaded", timeout=30000)
                elif is_star:
                    # *% = 0% AI detected. Иногда Turnitin не открывает viewer.
                    # Пробуем ещё раз через JS клик
                    logger.info("*%% — trying JS click as fallback")
                    new_page_future2 = asyncio.get_event_loop().create_future()
                    def on_page2(p):
                        if not new_page_future2.done():
                            new_page_future2.set_result(p)
                    page.context.on("page", on_page2)
                    await lti.evaluate(r"""() => {
                        function deepFind(root) {
                            for (const el of root.querySelectorAll('*')) {
                                if (el.shadowRoot) { const f = deepFind(el.shadowRoot); if (f) return f; }
                                if (el.textContent.trim() === '*%' && el.children.length === 0) {
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0) { el.click(); return true; }
                                }
                            }
                            return false;
                        }
                        return deepFind(document);
                    }""")
                    try:
                        report_page = await asyncio.wait_for(new_page_future2, timeout=15)
                        await report_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        logger.info("New page opened via JS fallback: %s", report_page.url)
                    except asyncio.TimeoutError:
                        try:
                            page.context.remove_listener("page", on_page2)
                        except Exception:
                            pass
                        logger.warning("*%% — no report page opened, AI Writing report unavailable (0%% AI = no PDF)")
                        return None
                else:
                    logger.warning("No report URL found and no fallback, skipping download")
                    return None
            else:
                try:
                    page.context.remove_listener("page", on_page)
                except Exception:
                    pass

            await asyncio.sleep(5)
            logger.info("Report viewer URL: %s", report_page.url)

            # Проверяем что это не страница ошибки
            if "#/error" in report_page.url or "error" in report_page.url.lower():
                logger.warning("Report page is an error page: %s", report_page.url)
                await report_page.close()
                return None

            # Ждём кнопку Download до 20 сек
            dl_btn = None
            for i in range(20):
                dl_btn = await report_page.evaluate(FIND_DOWNLOAD_BTN_JS)
                if dl_btn:
                    logger.info("Download button found after %ds", i)
                    break
                await asyncio.sleep(1)

            if not dl_btn:
                logger.warning("Download button not found")
                await report_page.screenshot(path=f"/tmp/debug_report_{rtype}.png")
                await report_page.close()
                return None

            await report_page.mouse.click(dl_btn['x'], dl_btn['y'])
            await asyncio.sleep(2)

            # Меню отчётов
            report_name = "Similarity Report" if rtype == "similarity" else "AI Writing Report"
            items = await report_page.evaluate(FIND_DOWNLOAD_ITEMS_JS)
            logger.info("Download menu items: %s", [i['text'] for i in items])

            target = next((i for i in items if i['text'] == report_name), None)
            if not target:
                logger.warning("'%s' not found in menu", report_name)
                await report_page.close()
                return None

            async with report_page.expect_download(timeout=60000) as dl_info:
                await report_page.mouse.click(target['x'], target['y'])

            download = await dl_info.value
            await download.save_as(str(out_path))
            logger.info("Downloaded %s -> %s", rtype, out_path)
            await report_page.close()
            return str(out_path)

        except Exception as e:
            logger.warning("Download failed: %s", e)
            # Закрываем report_page если она была открыта (утечка вкладок = mouse.click перестаёт работать)
            if report_page is not None:
                try:
                    await report_page.close()
                except Exception:
                    pass
            return None

    async def _delete(self, page: Page):
        """Три точки → Request deletion → чекбокс → Request Permanent Deletion."""
        logger.info("Deleting submission...")
        try:
            lti = await self._get_lti(page)
            if not lti:
                logger.warning("LTI frame not found for deletion")
                return

            offset = await self._get_iframe_offset(page)
            ox, oy = offset['x'], offset['y']

            # Три точки
            btn = await lti.evaluate(FIND_THREE_DOTS_JS)
            if not btn:
                logger.warning("Three dots not found for deletion")
                return

            await page.mouse.click(ox + btn['x'], oy + btn['y'])
            await asyncio.sleep(2)

            # Request deletion
            rd = await lti.evaluate(FIND_BY_TEXT_JS, "Request deletion")
            if not rd:
                logger.warning("'Request deletion' not found in menu")
                return

            await page.mouse.click(rd['x'] + ox, rd['y'] + oy)
            logger.info("Clicked Request deletion")
            await asyncio.sleep(2)

            # Чекбокс — ищем в lti frame
            cb = await lti.evaluate("""() => {
                const inp = document.querySelector('input[type=checkbox]');
                if (inp) {
                    const r = inp.getBoundingClientRect();
                    return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), checked: inp.checked};
                }
                return null;
            }""")

            if cb and not cb['checked']:
                await page.mouse.click(cb['x'] + ox, cb['y'] + oy)
                logger.info("Checked confirmation checkbox")
                await asyncio.sleep(1)
            else:
                logger.info("Checkbox already checked or not found")

            # Request Permanent Deletion
            rpd = await lti.evaluate(FIND_BY_TEXT_JS, "Request Permanent Deletion")
            if rpd:
                await page.mouse.click(rpd['x'] + ox, rpd['y'] + oy)
                logger.info("Clicked Request Permanent Deletion")
                await asyncio.sleep(3)
                logger.info("Submission deleted!")
            else:
                logger.warning("'Request Permanent Deletion' button not found")

        except Exception as e:
            logger.warning("Delete error: %s", e)

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


turnitin_service = TurnitinService()