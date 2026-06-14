"""
Kaspi PDF чек парсер.
Время в чеке всегда по Астане (UTC+5).
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import fitz  # PyMuPDF
from loguru import logger

ASTANA_TZ = timezone(timedelta(hours=5))
DEFAULT_EXPIRE_MINUTES = 60


class KaspiReceiptData:
    def __init__(self):
        self.transaction_id: Optional[str] = None
        self.amount: Optional[float] = None
        self.date: Optional[datetime] = None
        self.sender: Optional[str] = None
        self.raw_text: str = ""

    def __repr__(self):
        return f"KaspiReceipt(id={self.transaction_id}, amount={self.amount}, date={self.date})"


def parse_kaspi_pdf(pdf_bytes: bytes) -> Optional[KaspiReceiptData]:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()

        logger.debug(f"Kaspi PDF raw text:\n{text}")

        receipt = KaspiReceiptData()
        receipt.raw_text = text

        # ID транзакции
        id_patterns = [
            r"№\s*операции\s*\n?\s*(QR\d+)",
            r"№\s*чека\s*\n?\s*(QR\d+)",
            r"№\s*операции\s*\n?\s*([A-Za-z0-9\-]{6,30})",
            r"(QR\d{8,})",
        ]
        for pattern in id_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                receipt.transaction_id = m.group(1).strip()
                break

        # Сумма
        amount_patterns = [
            r"([\d\s\xa0]+)\s*[₸тТ]",
            r"([\d\s\xa0]+)\s*тенге",
            r"([\d\s\xa0]+)\s*KZT",
        ]
        for pattern in amount_patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                amount_str = m.group(1).replace(" ", "").replace("\xa0", "").strip()
                try:
                    val = float(amount_str)
                    if val >= 100:
                        receipt.amount = val
                        break
                except ValueError:
                    continue
            if receipt.amount:
                break

        # Дата
        date_patterns = [
            r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})",
            r"(\d{2}\.\d{2}\.\d{4})",
        ]
        for pattern in date_patterns:
            m = re.search(pattern, text)
            if m:
                try:
                    date_str = m.group(1)
                    time_str = m.group(2) if m.lastindex >= 2 else "00:00"
                    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
                    receipt.date = naive_dt.replace(tzinfo=ASTANA_TZ)
                    break
                except (ValueError, IndexError):
                    continue

        # Отправитель
        sender_patterns = [
            r"ФИО\s+покупателя\s*\n?\s*(.+?)(?:\n|Дата|ИИН)",
            r"Покупатель\s*[:\s]+(.+?)(?:\n)",
        ]
        for pattern in sender_patterns:
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                receipt.sender = m.group(1).strip()[:100]
                break

        logger.info(f"Parsed: {receipt}")
        return receipt

    except Exception as e:
        logger.error(f"Failed to parse Kaspi PDF: {e}")
        return None


def validate_receipt(
    receipt: KaspiReceiptData,
    expected_tenge: float,
    expire_minutes: int = DEFAULT_EXPIRE_MINUTES,
) -> tuple[bool, str]:
    if not receipt.transaction_id:
        return False, (
            "❌ Не удалось извлечь ID транзакции из чека.\n"
            "Убедись что отправляешь оригинальный PDF из приложения Kaspi (не скриншот)."
        )

    if receipt.amount is None:
        return False, "❌ Не удалось определить сумму из чека."

    if abs(receipt.amount - expected_tenge) > 1:
        return False, (
            f"❌ Сумма в чеке <b>{receipt.amount:,.0f} ₸</b> "
            f"не совпадает с ожидаемой <b>{expected_tenge:,.0f} ₸</b>."
        )

    if receipt.date:
        now_astana = datetime.now(tz=ASTANA_TZ)
        expire = timedelta(minutes=expire_minutes)

        if receipt.date < now_astana - expire:
            return False, (
                f"❌ Чек устарел. Время оплаты: <b>{receipt.date.strftime('%H:%M')}</b>. "
                f"Принимаются чеки не старше {expire_minutes} минут."
            )
        if receipt.date > now_astana + timedelta(minutes=10):
            return False, (
                f"❌ Дата чека в будущем — чек недействителен.\n"
                f"Время чека: {receipt.date.strftime('%d.%m.%Y %H:%M')} (Астана)"
            )

    return True, "✅ Чек верифицирован"