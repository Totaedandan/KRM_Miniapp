# Turnitin Bot — финальная версия

Telegram-бот для проверки работ в Turnitin. Оплата через **Telegram Stars** (через BotFather).

---

## Структура проекта

```
turnitin_bot/
├── main.py                     # Запуск бота
├── config.py                   # Настройки
├── database.py                 # SQLite (заказы, чеки, настройки)
├── requirements.txt
├── .env.example                # Шаблон переменных
├── assets/
│   └── kaspi_qr.jpg            # QR для оплаты (загрузить вручную или через /admin)
├── handlers/
│   ├── start.py                # /start, меню
│   ├── payment.py              # Оплата Stars + Kaspi QR
│   ├── upload.py               # Приём файла, запуск Turnitin
│   └── admin.py                # /admin панель
├── services/
│   └── turnitin.py             # Playwright-автоматизация
└── utils/
    ├── keyboards.py            # Все клавиатуры
    ├── states.py               # FSM-состояния
    └── receipt_parser.py       # OCR чеков Kaspi
```

---

## Установка

```bash
# 1. Python окружение
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Зависимости
pip install -r requirements.txt
playwright install chromium

# 3. OCR для чеков (нужен Tesseract)
# Ubuntu/Debian:
sudo apt-get install tesseract-ocr tesseract-ocr-rus
# Windows: https://github.com/UB-Mannheim/tesseract/wiki

# 4. Конфигурация
cp .env.example .env
nano .env                       # заполнить ADMIN_IDS и остальное

# 5. Запуск
python main.py
```

---

## Обязательно заполнить в .env

| Переменная | Что вставить |
|---|---|
| `BOT_TOKEN` | Уже вставлен |
| `ADMIN_IDS` | Ваш Telegram ID (узнать у @userinfobot) |
| `HELP_USERNAME` | Ваш Telegram username |
| `HELP_PHONE` | Ваш номер телефона |
| `TURNITIN_CLASS_ID` | ID класса из URL Turnitin |
| `TURNITIN_ASSIGNMENT_ID` | ID задания из URL Turnitin |

---

## Как найти CLASS_ID и ASSIGNMENT_ID

Войдите в Turnitin как преподаватель → откройте нужное задание.  
URL будет: `turnitin.com/...?class_id=`**XXXXXXX**`&assign_id=`**YYYYYYY**

---

## Настройка оплаты — Robokassa (карты, KZT)

1. Напишите **@RobokassaBot** в Telegram → зарегистрируйтесь
2. Создайте магазин, подключите вашего бота через BotFather  
   (`BotFather → Bot Settings → Payments → Robokassa`)
3. Получите **provider_token** и вставьте в `.env`:
   ```
   PAYMENT_PROVIDER_TOKEN=401643678:LIVE:xxxxxxxxxxxxxxxx
   ```
4. Для теста используйте тестовый токен — деньги не списываются:
   ```
   PAYMENT_PROVIDER_TOKEN=401643678:TEST:xxxxxxxxxxxxxxxx
   ```

> Цены в тенге (KZT) меняются через `/admin → Изменить цены` без перезапуска бота.

---

## Флоу пользователя

```
/start
  → Выбор услуги (AI / Плагиат / Оба)
    → Оплата через Telegram Stars
      → Подтверждение оплаты (автоматически)
        → Отправьте файл (.pdf/.docx/.doc/.txt/.rtf)
          → Playwright: логин → загрузка → polling 45 сек → скачать PDF → удалить
            → Отправка отчёта пользователю
              → "Спасибо! Для новой проверки — /start"
```

---

## Команды бота

| Команда | Кто |
|---|---|
| `/start` | Все — главное меню |
| `/help` | Все — контакты поддержки |
| `/admin` | Только ADMIN_IDS |

---

## Админ-панель (/admin)

- 📊 **Статистика** — количество проверок по статусам и типам
- 💰 **Изменить цены** — меняются в БД, сразу отображаются пользователям
- 🖼 **Заменить QR** — загрузить новое фото QR-кода
- 🔑 **Логин/пароль Turnitin** — обновить credentials без перезапуска
- 🗃 **Управление чеками** — список, удалить, добавить вручную

---

## Prodaction (systemd)

```ini
# /etc/systemd/system/turnitin-bot.service
[Unit]
Description=Turnitin Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/turnitin_bot
ExecStart=/opt/turnitin_bot/venv/bin/python main.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/turnitin_bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable turnitin-bot
systemctl start turnitin-bot
journalctl -u turnitin-bot -f   # логи
```

---

## Важные ограничения Turnitin

- Нужен **Instructor-аккаунт** (не Student)
- Создайте отдельный класс и assignment специально для бота
- Не делайте более 20-30 загрузок в сутки с одного аккаунта
- Selectors в `services/turnitin.py` могут потребовать подстройки после первого запуска — смотрите логи

---

## Если бот завис на "Загружаю в Turnitin"

Проверьте логи:
```bash
journalctl -u turnitin-bot -f
# или при ручном запуске:
python main.py 2>&1 | tee bot.log
```

Чаще всего причина — неверный CLASS_ID или ASSIGNMENT_ID, или изменился интерфейс Turnitin.
