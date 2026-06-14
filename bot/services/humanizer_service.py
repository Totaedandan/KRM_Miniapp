"""
Humanizer service via StealthGPT API.
Большие тексты разбиваются на чанки по 500 слов.
Uses curl_cffi to impersonate Chrome TLS fingerprint and bypass Vercel WAF.
"""
from curl_cffi.requests import AsyncSession
import asyncio
from loguru import logger
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import settings

STEALTHGPT_URL = "https://www.stealthgpt.ai/api/stealthify"
CHUNK_SIZE = 300
MAX_WORDS = 20000

TONE_OPTIONS = {
    "Standard":   "Стандартный",
    "HighSchool": "Школьный",
    "College":    "Студенческий",
    "PhD":        "Академический",
}

MODE_OPTIONS = {
    "Low":    "Краткий",
    "Medium": "Средний",
    "High":   "Подробный",
}


def split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    paragraphs = text.split("\n")
    chunks = []
    current_words = []
    current_count = 0

    for para in paragraphs:
        words = para.split()
        if current_count + len(words) > chunk_size and current_words:
            chunks.append(" ".join(current_words))
            current_words = words
            current_count = len(words)
        else:
            current_words.extend(words)
            current_count += len(words)

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks


async def _humanize_chunk(text: str, tone: str, mode: str, business: bool) -> str:
    payload = {
        "prompt": text,
        "rephrase": True,
        "tone": tone,
        "mode": mode,
        "qualityMode": "quality",
        "business": business,
        "isMultilingual": True,
        "outputFormat": "text",
    }
    headers = {
        "api-token": settings.STEALTHGPT_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.stealthgpt.ai",
        "Referer": "https://www.stealthgpt.ai/",
    }

    for attempt in range(1, 4):
        try:
            # impersonate="chrome124" spoofs Chrome's TLS/JA3 fingerprint,
            # which is what Vercel's WAF checks — plain httpx always fails this.
            async with AsyncSession(impersonate="chrome124") as session:
                resp = await session.post(
                    STEALTHGPT_URL,
                    headers=headers,
                    json=payload,
                    timeout=60,
                )

            if resp.status_code == 401:
                raise RuntimeError("StealthGPT: недействительный API-ключ или закончились кредиты.")
            if resp.status_code == 429:
                body = resp.text[:300]
                retry_after = int(resp.headers.get("Retry-After", 0))
                wait = retry_after if retry_after > 0 else 15 * attempt  # 15s, 30s, 45s
                logger.warning(
                    f"StealthGPT 429 (attempt {attempt}/3), body={body!r}, "
                    f"Retry-After={retry_after}s, sleeping {wait}s..."
                )
                if attempt == 3:
                    raise RuntimeError(f"StealthGPT: лимит запросов исчерпан. Ответ API: {body}")
                await asyncio.sleep(wait)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"StealthGPT error {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            result = data.get("result", "")
            if not result:
                raise RuntimeError(f"Пустой ответ: {data}")

            logger.info(f"Chunk OK: words={data.get('wordsSpent', 0)}, remaining={data.get('remainingCredits', '?')}")
            return result

        except RuntimeError:
            raise  # don't retry on known API errors

        except Exception as e:
            logger.warning(f"Chunk error attempt {attempt}/3: {e}")
            if attempt == 3:
                raise RuntimeError(f"StealthGPT недоступен после 3 попыток: {e}")
            await asyncio.sleep(5 * attempt)


async def humanize_text(
    text: str,
    tone: str = "College",
    mode: str = "Medium",
    business: bool = False,
) -> tuple[str, int]:
    if not settings.STEALTHGPT_API_KEY:
        raise RuntimeError("STEALTHGPT_API_KEY не задан в .env")

    chunks = split_into_chunks(text, CHUNK_SIZE)
    logger.info(f"Humanizing {len(text.split())} words in {len(chunks)} chunk(s), business={business}")

    results = []
    total_words = 0

    for i, chunk in enumerate(chunks, 1):
        logger.info(f"Processing chunk {i}/{len(chunks)} ({len(chunk.split())} words)")
        result = await _humanize_chunk(chunk, tone, mode, business)
        results.append(result)
        total_words += len(chunk.split())
        if i < len(chunks):
            await asyncio.sleep(1)

    return "\n\n".join(results), total_words


def calculate_humanizer_cost(text: str, business: bool = False) -> float:
    """0.5 монеты за слово в обычном режиме, x10 в бизнес режиме."""
    word_count = len(text.split())
    base_cost = word_count * 0.5
    return round(base_cost * 10 if business else base_cost, 2)