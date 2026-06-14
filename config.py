import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Telegram ──────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8165250704:AAEA_32hSdkXZoVYRtcIcPFq95eIdkOywTc")
    ADMIN_IDS: list[int] = [
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    ]
    HELP_USERNAME: str = os.getenv("HELP_USERNAME", "@awata_krm")
    HELP_PHONE: str = os.getenv("HELP_PHONE", "87771113003")

    # ── Turnitin ──────────────────────────────────────────
    TURNITIN_EMAIL: str = os.getenv("TURNITIN_EMAIL", "kurmankhan_a0605@fmalm.nis.edu.kz")
    TURNITIN_PASSWORD: str = os.getenv("TURNITIN_PASSWORD", "Slspp224rfd!")
    TURNITIN_CLASS_ID: str = os.getenv("TURNITIN_CLASS_ID", "")
    TURNITIN_ASSIGNMENT_ID: str = os.getenv("TURNITIN_ASSIGNMENT_ID", "")
    TURNITIN_POLL_INTERVAL: int = 45  # секунд между проверками готовности отчёта
    TURNITIN_TIMEOUT: int = 1800      # макс ожидание отчёта (30 мин)

    # ── Payments (Robokassa через Telegram Payments) ───────
    # Ключ провайдера платежей — получить у @RobokassaBot в Telegram
    PAYMENT_PROVIDER_TOKEN: str = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
    # Валюта — KZT (тенге)
    PAYMENT_CURRENCY: str = "KZT"

    # ── Пути ─────────────────────────────────────────────
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "bot.db")
    REPORTS_DIR: str = "reports"
    UPLOADS_DIR: str = "uploads"
    QR_IMAGE_PATH: str = "assets/kaspi_qr.jpg"  # картинка QR (замена через /admin)

    def __init__(self):
        os.makedirs(self.REPORTS_DIR, exist_ok=True)
        os.makedirs(self.UPLOADS_DIR, exist_ok=True)
        os.makedirs("assets", exist_ok=True)


config = Config()
