from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── Telegram ─────────────────────────────────────────────────
    BOT_TOKEN: str
    SUPERADMIN_ID: int
    # Доп. админы (через запятую). Эти пользователи проходят флоу
    # бесплатно — бот не просит оплату (как whitelist).
    ADMIN_IDS: str = ""

    # ── Turnitin ─────────────────────────────────────────────────
    TURNITIN_EMAIL: str = ""
    TURNITIN_PASSWORD: str = ""
    TURNITIN_CLASS_ID: str = ""
    TURNITIN_ASSIGNMENT_ID: str = ""
    TURNITIN_POLL_INTERVAL: int = 45   # секунд между проверками готовности
    TURNITIN_TIMEOUT: int = 1800       # макс ожидание отчёта (30 мин)

    # ── Humanizer (StealthGPT) ───────────────────────────────────
    STEALTHGPT_API_KEY: Optional[str] = None

    # ── Kaspi ────────────────────────────────────────────────────
    KASPI_PHONE: str = ""
    KASPI_RECIPIENT_NAME: str = ""
    KASPI_EXPIRE_MINUTES: int = 60     # срок действия чека

    # ── Цены Turnitin (тенге) — переопределяются из БД settings ─
    DEFAULT_PRICE_SIM: int = 700       # только плагиат
    DEFAULT_PRICE_AI: int = 700        # только AI-детекция
    DEFAULT_PRICE_BOTH: int = 1200     # оба отчёта

    # ── Пакеты токенов для Хуманайзера ───────────────────────────
    PACKAGE_1_TOKENS: int = 100
    PACKAGE_1_STARS: int = 100
    PACKAGE_1_TENGE: int = 1000

    PACKAGE_2_TOKENS: int = 250
    PACKAGE_2_STARS: int = 230
    PACKAGE_2_TENGE: int = 2300

    PACKAGE_3_TOKENS: int = 600
    PACKAGE_3_STARS: int = 520
    PACKAGE_3_TENGE: int = 5200

    PACKAGE_4_TOKENS: int = 1200
    PACKAGE_4_STARS: int = 950
    PACKAGE_4_TENGE: int = 9500

    # Стоимость хуманайзера: 0.5 токена/слово (бизнес × 10)
    HUMANIZER_COST_PER_WORD: float = 0.5

    # ── Mini App ─────────────────────────────────────────────────
    MINI_APP_URL: str = ""      # https://yourdomain.com (нужен HTTPS для Telegram)
    MINI_APP_PORT: int = 8000   # порт FastAPI сервера

    # ── БД ───────────────────────────────────────────────────────
    DATABASE_PATH: str = "data/bot.db"

    # ── Пути ────────────────────────────────────────────────────
    REPORTS_DIR: str = "data/reports"
    UPLOADS_DIR: str = "data/uploads"

    @property
    def admin_id_list(self) -> list[int]:
        """Список ID из ADMIN_IDS (через запятую) + суперадмин."""
        ids = []
        for part in self.ADMIN_IDS.replace(" ", "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.append(int(part))
        if self.SUPERADMIN_ID not in ids:
            ids.append(self.SUPERADMIN_ID)
        return ids

    def is_admin(self, tg_id: int) -> bool:
        """True для админов из ADMIN_IDS и суперадмина — проходят флоу бесплатно."""
        return tg_id in self.admin_id_list

    def get_humanizer_packages(self) -> list[dict]:
        return [
            {"tokens": self.PACKAGE_1_TOKENS, "stars": self.PACKAGE_1_STARS, "tenge": self.PACKAGE_1_TENGE},
            {"tokens": self.PACKAGE_2_TOKENS, "stars": self.PACKAGE_2_STARS, "tenge": self.PACKAGE_2_TENGE},
            {"tokens": self.PACKAGE_3_TOKENS, "stars": self.PACKAGE_3_STARS, "tenge": self.PACKAGE_3_TENGE},
            {"tokens": self.PACKAGE_4_TOKENS, "stars": self.PACKAGE_4_STARS, "tenge": self.PACKAGE_4_TENGE},
        ]

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
