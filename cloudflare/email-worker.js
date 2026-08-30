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
 * v2 (проверено на живых письмах от ChatGPT): письма от ChatGPT/Claude почти
 * всегда multipart-MIME с base64/quoted-printable кодировкой, и заголовки
 * Content-Type/Content-Transfer-Encoding на реальных письмах OpenAI не всегда
 * совпадают с обычным регэкспом (нестандартное форматирование) — поэтому
 * декодирование и определение HTML/plain текста в первую очередь опираются
 * на СОДЕРЖИМОЕ части (похоже ли на quoted-printable / похоже ли на HTML), а
 * заголовок используется только как подсказка. HTML-тело дополнительно
 * чистится от <style> и MSO-условных комментариев Outlook перед поиском
 * кода — в них попадаются похожие на код 6-значные числа (CSS-цвета вида
 * #123456, технические id). Если чисел-кандидатов несколько — выбирается то,
 * что стоит рядом со словом "код"/"code"/"verification" и т.п.
 *
 * v3: некоторые сервисы (Claude) не кладут код прямо в письмо — только
 * ссылку "Sign in" (magic-link). Сам код рисуется JS-ом только на странице,
 * куда она ведёт, и только если её открывают не из той сессии, что
 * запрашивала вход — ровно наш случай. Worker НЕ пытается сам сходить по
 * такой ссылке (fetch() не видит #фрагмент с токеном — он не уходит на
 * сервер ни в одном HTTP-запросе, и код всё равно рисуется JS-ом уже после
 * загрузки, так что голый fetch() получил бы пустой каркас страницы и 403
 * на реальном тесте) — вместо этого просто пересылает саму ссылку на
 * /api/email-hook как {recipient_email, magic_link}, а бэкенд открывает её в
 * настоящем Playwright-браузере (см. services/ai_rental_service.py).
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

// Ключевые слова, по которым письмо считается «чувствительным» (не OTP) —
// такие форвардим админу вместо автоматической обработки.
const SENSITIVE_KEYWORDS = [
  "password", "reset your password", "change your password", "recovery",
  "security alert", "suspicious", "пароль", "восстановлен", "безопасност",
];

// Слова-подсказки рядом с настоящим кодом — используются, когда в письме
// найдено НЕСКОЛЬКО 6-значных чисел. На живом письме от OpenAI, например,
// кроме настоящего кода в HTML оказались CSS-цвет (#353740, случайно похож
// на код) и технический id внутри MSO-условного комментария Outlook
// (<!--[if mso]--> 554762 <!--[endif]-->, который структурно НЕЛЬЗЯ вырезать
// как обычный HTML-комментарий — это два отдельных комментария с реальным
// контентом между ними, так и задумано для рендеринга в Outlook). Поэтому
// вместо попытки вычистить все возможные технические числа — при нескольких
// кандидатах выбираем тот, что стоит рядом со словом-подсказкой.
const CODE_HINT_RE = /(код|code|verification|confirm|passcode|otp)/i;
const SIX_DIGITS_RE = /\b(\d{6})\b/g;

function extractOtpCode(haystack) {
  const matches = [...haystack.matchAll(SIX_DIGITS_RE)];
  if (matches.length === 0) return null;
  if (matches.length === 1) return matches[0][1];
  for (const m of matches) {
    const before = haystack.slice(Math.max(0, m.index - 60), m.index);
    if (CODE_HINT_RE.test(before)) return m[1];
  }
  return matches[0][1]; // ни одна подсказка не нашлась — как раньше, первое совпадение
}

export default {
  async email(message, env, ctx) {
    const to = (message.to || "").toLowerCase().trim();
    const subject = (message.headers.get("subject") || "");
    const rawText = await streamToText(message.raw);
    const decodedBody = getDecodedText(rawText);
    const haystack = `${subject}\n${decodedBody}`;
    const haystackLower = haystack.toLowerCase();

    const isSensitive = SENSITIVE_KEYWORDS.some((kw) => haystackLower.includes(kw));
    const otpCode = isSensitive ? null : extractOtpCode(haystack);

    // Claude шлёт не голый код, а magic-link — письмо содержит только кнопку
    // "Sign in". Ссылка с #фрагментом (токен после # — не долетает до сервера
    // ни в каком fetch(), это чисто клиентский механизм) и сам код рисуется
    // JS-ом страницы уже ПОСЛЕ загрузки — обычный fetch() из Worker'а получает
    // пустой каркас страницы без токена и без кода (пробовали — 403 и пусто).
    // Поэтому здесь ссылку не трогаем, а просто пересылаем как есть в бэкенд —
    // там уже настоящий Playwright-браузer её откроет и вытащит код из DOM.
    const magicLink = (!otpCode && !isSensitive) ? findMagicLink(getDecodedHtml(rawText)) : null;

    if (otpCode) {
      const ok = await postToWebhooks(env, { recipient_email: to, otp_code: otpCode });
      if (ok) return; // успешно передали в бэкенд — форвардить не нужно
      // Вебхук не ответил ok — не теряем письмо молча, форвардим админу
    } else if (magicLink) {
      const ok = await postToWebhooks(env, { recipient_email: to, magic_link: magicLink });
      if (ok) return;
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

// Ищем прямую ссылку на magic-link (не через click-tracking редирект вида
// url*.mail.anthropic.com/ls/click?...) — она обычно тоже есть в письме как
// запасной "если кнопка не работает, скопируйте эту ссылку" вариант, и вести
// себя должна идентично, но без лишнего перехода через редирект-сервис.
function findMagicLink(html) {
  const links = [...html.matchAll(/href="([^"]+)"/gi)].map((m) => m[1]);
  const direct = links.find((l) => /\/magic-link/i.test(l));
  if (direct) return direct;
  return links.find((l) => /claude\.ai|anthropic\.com/i.test(l)) || null;
}

// Один домен может обслуживать НЕСКОЛЬКО ботов (у каждого своя база
// аккаунтов/аренд) — тогда в Variables задаётся WEBHOOK_URLS (через запятую)
// вместо одиночного WEBHOOK_URL, и payload рассылается ВСЕМ сразу. Каждый бот
// сам решает по своей БД, принадлежит ли ему этот email (есть ли активная
// аренда) — лишняя запись у "чужого" бота просто не используется, вреда нет.
// payload — {recipient_email, otp_code} ИЛИ {recipient_email, magic_link},
// бэкенд сам разбирается, что из этого пришло (см. /api/email-hook).
async function postToWebhooks(env, payload) {
  const urls = (env.WEBHOOK_URLS || env.WEBHOOK_URL || "")
    .split(",")
    .map((u) => u.trim())
    .filter(Boolean);
  if (!urls.length || !env.WEBHOOK_SECRET) {
    console.error("WEBHOOK_URLS/WEBHOOK_URL или WEBHOOK_SECRET не заданы в Variables — см. инструкцию в шапке файла");
    return false;
  }
  const results = await Promise.all(
    urls.map(async (url) => {
      try {
        const resp = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Webhook-Secret": env.WEBHOOK_SECRET,
          },
          body: JSON.stringify(payload),
        });
        return resp.ok;
      } catch (e) {
        console.error("postToWebhooks fetch failed for", url, e);
        return false;
      }
    })
  );
  return results.some(Boolean); // хотя бы один бот принял — считаем успехом
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
  const cleaned = str.replace(/=\r\n/g, "").replace(/=\n/g, "");
  // Собираем настоящие байты (не char-per-char), чтобы многобайтовые UTF-8
  // последовательности (кириллица и т.п.) декодировались правильно, а не
  // превращались в "ÐÐ°Ñ" — для поиска цифр это не критично (ASCII цифры не
  // экранируются), но делает контекст читаемым при разборе логов.
  const bytes = [];
  for (let i = 0; i < cleaned.length; i++) {
    if (cleaned[i] === "=" && /^[0-9A-Fa-f]{2}$/.test(cleaned.slice(i + 1, i + 3))) {
      bytes.push(parseInt(cleaned.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(cleaned.charCodeAt(i) & 0xff);
    }
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(new Uint8Array(bytes));
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

// Эвристика: похоже ли содержимое на ещё не раскодированный quoted-printable
// (много "=XX" последовательностей или мягкие переносы "=\r\n"/"=\n"). Нужна
// как страховка на случай, если Content-Transfer-Encoding не нашёлся в
// заголовках части из-за нестандартной структуры письма реального сервиса —
// так и оказалось на живом письме от OpenAI, регэксп по заголовкам ничего не
// нашёл, и письмо осталось нераскодированным.
function looksQuotedPrintable(body) {
  const softBreaks = (body.match(/=\r?\n/g) || []).length;
  const hexEscapes = (body.match(/=[0-9A-Fa-f]{2}/g) || []).length;
  return softBreaks > 0 || hexEscapes > 5;
}

function looksLikeHtml(body) {
  return /<html[\s>]/i.test(body) || /<\/?(p|div|span|br|table|tr|td|title|head|body)[\s>]/i.test(body);
}

function decodePart(part) {
  const cte = (part.headers.match(/Content-Transfer-Encoding:\s*([^\r\n;]+)/i) || [])[1] || "";
  const ctypeHeader = (part.headers.match(/Content-Type:\s*([^\r\n;]+)/i) || [])[1] || "";
  let body = part.body;
  if (/base64/i.test(cte)) {
    body = decodeBase64Safe(body);
  } else if (/quoted-printable/i.test(cte) || looksQuotedPrintable(body)) {
    // Content-based fallback работает ВСЕГДА, а не только когда cte пустой —
    // на реальном письме от OpenAI заголовок Content-Transfer-Encoding
    // находился, но, видимо, с чем-то, что не совпало с "quoted-printable"
    // (нестандартное форматирование заголовков), и старая версия (!cte && ...)
    // из-за этого пропускала декодирование целиком.
    body = decodeQuotedPrintable(body);
  }
  // Определяем HTML в первую очередь ПО СОДЕРЖИМОМУ, а не по заголовку —
  // на реальных письмах OpenAI регэксп по Content-Type периодически не
  // совпадает (та же нестандартная структура заголовков, что ломала и
  // Content-Transfer-Encoding выше), и тогда html-часть молча уходила в
  // ветку "просто склеить как есть" без вырезания тегов — оттуда и лезли
  // CSS-цвета/mso-id в поиск кода. Заголовок используется только как
  // подсказка, когда по содержимому не понятно.
  const ctype = looksLikeHtml(body) ? "text/html"
              : (ctypeHeader ? ctypeHeader.trim().toLowerCase() : "text/plain");
  return { ctype, body };
}

// Вырезает то, что заведомо не может содержать настоящий код, но легко
// содержит случайно похожие на код 6-значные числа: CSS в <style>, и
// MSO-условные комментарии Outlook (<!--[if mso]>...<![endif]-->), в которых
// часто сидят технические id. Именно оттуда пришли оба ложных совпадения на
// живом письме от OpenAI (#353740 — цвет, 554762 — id внутри mso-комментария).
function stripNonContent(html) {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--\[if[\s\S]*?<!\[endif\]-->/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ");
}

// То же самое, что getDecodedText, но БЕЗ вырезания тегов — нужно, когда
// в письме нет прямого кода, а есть только ссылка (magic link), как у
// Claude: письмо содержит кнопку "Sign in", а код показывается только на
// странице, куда она ведёт (при переходе с "чужого" устройства/сессии —
// ровно наш случай, письмо открывает Worker, а не сам пользователь).
function getDecodedHtml(raw) {
  const parts = extractMimeParts(raw).map(decodePart);
  const html = parts.find((p) => p.ctype.startsWith("text/html"));
  if (html) return stripNonContent(html.body);
  const plain = parts.find((p) => p.ctype.startsWith("text/plain"));
  return plain ? plain.body : parts.map((p) => p.body).join("\n");
}

// Достаёт читаемый текст письма: приоритет text/plain, иначе text/html
// с вырезанными тегами, иначе — конкатенация всего раскодированного.
function getDecodedText(raw) {
  const parts = extractMimeParts(raw).map(decodePart);

  const plain = parts.find((p) => p.ctype.startsWith("text/plain"));
  if (plain) return plain.body;

  const html = parts.find((p) => p.ctype.startsWith("text/html"));
  if (html) return stripNonContent(html.body).replace(/<[^>]+>/g, " ");

  return parts.map((p) => p.body).join("\n");
}
