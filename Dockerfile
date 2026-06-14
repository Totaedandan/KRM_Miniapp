FROM python:3.11-slim-bullseye

WORKDIR /app

# Системные библиотеки для Chromium (Playwright) + Xvfb (headful-режим в Docker)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxshmfence1 \
    libxss1 \
    libxtst6 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости (отдельным слоем для кэширования)
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

# Код приложения
COPY bot/ ./bot/
COPY mini_app/ ./mini_app/
COPY .env* ./

# entrypoint поднимает Xvfb (X-сервер) для headed-Chromium и выставляет DISPLAY
COPY entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

WORKDIR /app/bot

ENV PYTHONPATH=/app/bot
ENV PYTHONUNBUFFERED=1

# Порт FastAPI Mini App
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
