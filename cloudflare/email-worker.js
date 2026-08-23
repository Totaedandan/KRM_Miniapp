/**
 * Cloudflare Email Worker — перехват OTP-кодов для арендованных email-адресов
 * (ChatGPT/Claude и т.п.) и пересылка в бэкенд бота.
 *
 * ЧТО ДЕЛАЕТ:
 *   1. Письмо приходит на email@ВАШ_ДОМЕН (арендованный аккаунт).
 *   2. Если в письме похоже на код входа (6-значное число в теме/теле) —
 *      POST на /api/email-hook с {recipient_email, otp_code}, заголовок
 *      X-Webhook-Secret для авторизации.
 *   3. Если письмо похоже на смену пароля / security alert / recovery —
 *      НЕ трогаем как OTP, форвардим на мастер-почту админа (ADMIN_EMAIL) —
 *      это чувствительная переписка, которую нельзя отдавать в общий пайплайн.
 *   4. Любое другое неопознанное письмо — тоже форвардим админу (безопасный
 *      дефолт: лучше лишний форвард, чем молча потерянное письмо).
 *
 * v2: письма от ChatGPT/Claude почти всегда multipart-MIME с base64 или
 * quoted-printable кодировкой HTML/текстовой части. Первая версия искала
 * 6 цифр прямо в сыром, ещё не раскодированном источнике письма — это могло
 * зацепить случайную последовательность цифр из служебных частей (Message-ID,
 * base64-блок картинки и т.п.) вместо настоящего кода. Теперь письмо сначала
 * разбирается на MIME-части, каждая раскодируется по своему
 * Content-Transfer-Encoding, и код ищется уже в реальном раскодированном
 * тексте (text/plain в приоритете, иначе text/html с вырезанными тегами).
 *
 * ─────────────────────────────────────────────────────────────────────────
 * УСТАНОВКА (Cloudflare Dashboard):
 *
 * 1. Email → Email Routing → включите для вашего домена (если ещё не включено).
 * 2. Destination addresses → добавьте и подтвердите ADMIN_EMAIL (почту, куда
 *    форвардить security-письма) — Cloudflare не даст форвардить на
 *    неподтверждённый адрес.
 * 3. Email → Email Routing → Email Workers → Create worker → вставьте этот
 *    файл целиком → Deploy.
 * 4. Откройте настройки этого Worker'а → Settings → Variables and Secrets →
 *    добавьте (Secret, не Plaintext, чтобы не светились в коде):
 *      WEBHOOK_URL    = https://ВАШ_ДОМЕН/api/email-hook
 *      WEBHOOK_SECRET = то же значение, что EMAIL_WEBHOOK_SECRET в .env бота
 *      ADMIN_EMAIL    = подтверждённый в шаге 2 адрес
 * 5. Email Routing → Routing rules → Catch-all (или правило на конкретный
 *    поддомен, если аренда сидит на отдельном поддомене) → Action: Send to
 *    a Worker → выберите этот Worker.
 * 6. Тест: арендуйте любой аккаунт в Mini App, попросите код входа на его
 *    email — письмо должно долететь до /api/email-hook (смотрите логи
 *    Worker'а в Cloudflare Dashboard → Logs, и логи бота на сервере).
 *
 * После обновления кода в существующем Worker'е обязательно нажмите Deploy —
 * иначе продолжит работать старая (уже задеплоенная) версия.
 * ─────────────────────────────────────────────────────────────────────────
 */

// 6-значный код — формат кодов ChatGPT/Claude/большинства сервисов.
const OTP_REGEX = /\b(\d{6})\b/;

// Ключевые слова, по которым письмо считается «чувствительным» (не OTP) —
// такие форвардим админу вместо автоматической обработки.
const SENSITIVE_KEYWORDS = [
  "password", "reset your password", "change your password", "recovery",
  "security alert", "suspicious", "пароль", "восстановлен", "безопасност",
];

export default {
  async email(message, env, ctx) {
    const to = (message.to || "").toLowerCase().trim();
    const subject = (message.headers.get("subject") || "");
    const rawText = await streamToText(message.raw);
    const decodedBody = getDecodedText(rawText);
    const haystack = `${subject}\n${decodedBody}`;
    const haystackLower = haystack.toLowerCase();

    const isSensitive = SENSITIVE_KEYWORDS.some((kw) => haystackLower.includes(kw));
    const otpMatch = isSensitive ? null : haystack.match(OTP_REGEX);

    if (otpMatch) {
      const ok = await postOtp(env, to, otpMatch[1]);
      if (ok) return; // успешно передали в бэкенд — форвардить не нужно
      // Вебхук не ответил ok — не теряем письмо молча, форвардим админу
    }

    if (env.ADMIN_EMAIL) {
      try {
        await message.forward(env.ADMIN_EMAIL);
      } catch (e) {
        console.error("forward to admin failed:", e);
      }
    }
  },
};

async function postOtp(env, recipientEmail, otpCode) {
  if (!env.WEBHOOK_URL || !env.WEBHOOK_SECRET) {
    console.error("WEBHOOK_URL/WEBHOOK_SECRET не заданы в Variables — см. инструкцию в шапке файла");
    return false;
  }
  try {
    const resp = await fetch(env.WEBHOOK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Webhook-Secret": env.WEBHOOK_SECRET,
      },
      body: JSON.stringify({ recipient_email: recipientEmail, otp_code: otpCode }),
    });
    return resp.ok;
  } catch (e) {
    console.error("postOtp fetch failed:", e);
    return false;
  }
}

async function streamToText(stream) {
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return new TextDecoder("utf-8").decode(merged);
}

// ── Минимальный MIME-парсер (без внешних библиотек) ───────────────────────

function decodeQuotedPrintable(str) {
  return str
    .replace(/=\r\n/g, "")
    .replace(/=\n/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function decodeBase64Safe(str) {
  try {
    return atob(str.replace(/[\r\n\s]/g, ""));
  } catch (e) {
    return str; // не смогли раскодировать — отдаём как есть, regex просто не найдёт совпадение
  }
}

function splitHeadersAndBody(chunk) {
  let idx = chunk.indexOf("\r\n\r\n");
  let sepLen = 4;
  if (idx === -1) {
    idx = chunk.indexOf("\n\n");
    sepLen = 2;
  }
  if (idx === -1) return { headers: "", body: chunk };
  return { headers: chunk.slice(0, idx), body: chunk.slice(idx + sepLen) };
}

// Разбирает письмо (возможно вложенное multipart/*) на плоский список частей.
function extractMimeParts(raw) {
  const top = splitHeadersAndBody(raw);
  const boundaryMatch = top.headers.match(/boundary="?([^"\r\n;]+)"?/i);
  if (!boundaryMatch) return [top];

  const boundary = boundaryMatch[1];
  const rawParts = top.body.split(`--${boundary}`).slice(1, -1);
  const parts = [];
  for (const chunk of rawParts) {
    const { headers, body } = splitHeadersAndBody(chunk);
    if (/multipart\//i.test(headers)) {
      // Вложенный multipart (например multipart/alternative внутри multipart/mixed)
      parts.push(...extractMimeParts(headers + "\r\n\r\n" + body));
    } else {
      parts.push({ headers, body });
    }
  }
  return parts.length ? parts : [top];
}

function decodePart(part) {
  const cte = (part.headers.match(/Content-Transfer-Encoding:\s*([^\r\n;]+)/i) || [])[1] || "";
  const ctype = (part.headers.match(/Content-Type:\s*([^\r\n;]+)/i) || [])[1] || "text/plain";
  let body = part.body;
  if (/base64/i.test(cte)) {
    body = decodeBase64Safe(body);
  } else if (/quoted-printable/i.test(cte)) {
    body = decodeQuotedPrintable(body);
  }
  return { ctype: ctype.trim().toLowerCase(), body };
}

// Достаёт читаемый текст письма: приоритет text/plain, иначе text/html
// с вырезанными тегами, иначе — конкатенация всего раскодированного.
function getDecodedText(raw) {
  const parts = extractMimeParts(raw).map(decodePart);

  const plain = parts.find((p) => p.ctype.startsWith("text/plain"));
  if (plain) return plain.body;

  const html = parts.find((p) => p.ctype.startsWith("text/html"));
  if (html) return html.body.replace(/<[^>]+>/g, " ");

  return parts.map((p) => p.body).join("\n");
}
