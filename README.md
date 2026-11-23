# Telegram Booking & AI Consultant Bot

Telegram-бот для автоматизации приёма заявок и записи клиентов на услуги на базе **aiogram 3**, **SQLite** и **Google Gemini API**. Поддерживает пошаговое оформление заявки (`FSM`), ответы ИИ по прайс-листу и FAQ, уведомления менеджеру с кнопками подтверждения/отмены, выгрузку базы в CSV и деплой на **Railway**.

## Возможности

- **Пошаговая запись (`FSM`):** выбор услуги из каталога (`knowledge.example.json`), имя, контакт, удобное время, комментарий и подтверждение.
- **AI-консультант (Gemini API):** отвечает на свободные вопросы клиентов строго на основе каталога услуг, сроков, цен и FAQ; хранит историю диалога в SQLite.
- **Админ-уведомления и модерация:** при новой заявке отправляет карточку администратору (`ADMIN_CHAT_ID`) с inline-кнопками **Подтвердить** / **Отменить** (статус автоматически синхронизируется и отправляется клиенту).
- **Экспорт и отчётность:** команда `/bookings` показывает последние заявки, `/export` формирует и отправляет `.csv` файл в Telegram.
- **Готовность к 24/7 деплою:** автоматический выбор между **Webhook** (`aiohttp` + healthcheck `/`) при наличии `WEBHOOK_URL` / `RAILWAY_PUBLIC_DOMAIN` и **Polling** для локального запуска.

## Структура проекта

```text
tg-booking-ai-bot/
├── bot.py                   # Telegram-бот (aiogram 3, FSM, админ-кнопки, webhook/polling)
├── storage.py               # SQLite хранилище заявок и контекста диалогов + экспорт CSV
├── ai_consultant.py         # Загрузка базы знаний и асинхронный клиент Gemini API
├── knowledge.example.json   # Пример каталога услуг, цен и FAQ
├── requirements.txt
├── Procfile                 # Конфигурация запуска для Railway
├── .env.example
└── tests/
    └── test_booking_bot.py
```

## Быстрый старт

1. Установите зависимости:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Скопируйте `.env.example` в `.env` и заполните переменные:
   - `BOT_TOKEN` — токен от `@BotFather`
   - `GEMINI_API_KEY` — ключ от Google AI Studio *(опционально; без него работает сама запись)*
   - `ADMIN_CHAT_ID` — ваш Telegram ID для получения уведомлений о заявках

3. Запустите локально:
   ```bash
   python bot.py
   ```

## Тесты

```bash
pytest
```
