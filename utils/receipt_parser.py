"""
Парсинг чека Kaspi:
  - Из изображения (screenshot) — через pytesseract (OCR)
  - Из PDF — через pdfplumber
Возвращает словарь с полями: receipt_id, amount, sender, receiver, datetime
"""

import logging
import re
import io
from typing import Optional

logger = logging.getLogger(__name__)

# Паттерны для Kaspi-чека
_PATTERNS = {
    "receipt_id": [
        r'(?:ID|Номер|номер|транзакц[а-яА-Я]*)[:\s#№]+([A-Z0-9\-]{6,30})',
        r'\b([A-Z]{2,4}\d{8,20})\b',
        r'(?:transaction|txn)[:\s]+([A-Z0-9\-]{6,30})',
    ],
    "amount": [
        r'([\d\s]+(?:\.\d{2})?)\s*(?:тенге|тг|₸|KZT)',
        r'Сумма[:\s]+([\d\s]+(?:\.\d{2})?)',
    ],
    "sender": [
        r'(?:Отправитель|От|Плательщик)[:\s]+([^\n]+)',
    ],
    "receiver": [
        r'(?:Получатель|Кому)[:\s]+([^\n]+)',
    ],
}


def _search_field(text: str, patterns: list) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def parse_receipt_text(text: str) -> dict:
    """Парсит текст чека и возвращает извлечённые поля."""
    result = {}
    for field, patterns in _PATTERNS.items():
        val = _search_field(text, patterns)
        if val:
            result[field] = re.sub(r'\s+', ' ', val).strip()
    return result


async def extract_from_image(image_bytes: bytes) -> dict:
    """OCR через pytesseract (нужен tesseract + langpack rus)."""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="rus+eng")
        logger.debug("OCR text: %s", text[:500])
        return parse_receipt_text(text)
    except ImportError:
        logger.warning("pytesseract not installed — OCR unavailable")
        return {}
    except Exception as e:
        logger.error("OCR error: %s", e)
        return {}


async def extract_from_pdf(pdf_bytes: bytes) -> dict:
    """Парсим PDF через pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts)
        logger.debug("PDF text: %s", text[:500])
        return parse_receipt_text(text)
    except ImportError:
        logger.warning("pdfplumber not installed — PDF parsing unavailable")
        return {}
    except Exception as e:
        logger.error("PDF parse error: %s", e)
        return {}


def validate_receipt(data: dict) -> tuple[bool, str]:
    """
    Проверяет достаточность данных чека.
    Возвращает (is_valid, reason).
    Минимальное требование — наличие receipt_id.
    """
    rid = data.get("receipt_id", "").strip()
    if not rid:
        return False, "Не удалось извлечь ID транзакции из чека."
    if len(rid) < 5:
        return False, f"ID транзакции слишком короткий: «{rid}»"
    return True, rid
