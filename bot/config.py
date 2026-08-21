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

    # ── ApiPay (автоматическое пополнение баланса через Kaspi) ───
    APIPAY_API_KEY: str = ""
    APIPAY_WEBHOOK_SECRET: str = ""

    # ── Аренда ИИ-аккаунтов v2 (email+OTP, авто-разлогин) ────────
    EMAIL_DOMAIN: str = ""            # напр. mrk.uk — домен для выдачи email@domain
    EMAIL_WEBHOOK_SECRET: str = ""    # секрет для проверки X-Webhook-Secret от Cloudflare Worker
    TWOCAPTCHA_API_KEY: str = ""      # решение капчи при авто-разлогине (опционально)

    # ── Цены Turnitin (тенге) — переопределяются из БД settings ─
    DEFAULT_PRICE_SIM: int = 700       # только плагиат
    DEFAULT_PRICE_AI: int = 700        # только AI-детекция
    DEFAULT_PRICE_BOTH: int = 1200     # оба отчёта

    # Пакеты токенов Хуманайзера теперь хранятся и редактируются через
    # database.get_token_packages()/set_token_package() (таблица settings).

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

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
