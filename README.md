# 💱 Currency Converter Bot

Telegram-бот для конвертации валют с актуальными курсами и графиками за неделю.

---

## ✨ Возможности

- 🪙 **BTC ↔ USD** — цена с CoinGecko, кэш обновляется каждые 10 минут
- 💵 **USD ↔ ₽**, 💶 **EUR ↔ ₽** — курсы ЦБ РФ, обновляются раз в сутки
- 🇰🇿 **KZT ↔ ₽** — курс ЦБ РФ (XML dynamic API)
- 📊 График за последние 7 дней прямо в чате
- 👥 Регистрация пользователей в SQLite + рассылка от админа
- 🔒 `/broadcast`, `/stats`, `/cancel` — только для админа

---

## 🚀 Запуск

```bash
pip install aiogram aiohttp matplotlib python-dotenv
```

Создай файл `.env` рядом с `bot.py`:

```env
BOT_TOKEN=123456:ABC-your-telegram-bot-token
ADMIN_ID=123456789
```

- `BOT_TOKEN` — получить у [@BotFather](https://t.me/BotFather)
- `ADMIN_ID` — твой Telegram user_id (узнать у [@userinfobot](https://t.me/userinfobot))

Запуск:

```bash
python bot.py
```

---

## 🤖 Команды

| Команда | Кому | Описание |
|---|---|---|
| `/start` | Всем | Открыть главное меню конвертации |
| `/help` | Всем | Справка по доступным командам |
| `/broadcast` | Админу | Рассылка текста всем активным пользователям |
| `/stats` | Админу | Кол-во пользователей (всего / активных) |
| `/cancel` | Админу | Отменить рассылку |

---

## 📦 Стек

- Python 3.14
- [aiogram 3](https://docs.aiogram.dev/) — Telegram Bot API
- [matplotlib](https://matplotlib.org/) — графики
- SQLite — история курсов + пользователи
- aiohttp — HTTP-запросы
- python-dotenv — загрузка `.env`

---

## 🌐 Источники данных

| Валюта | Источник |
|---|---|
| USD, EUR | [cbr-xml-daily.ru](https://www.cbr-xml-daily.ru) (ЦБ РФ) |
| KZT | [CBR XML dynamic](https://www.cbr.ru/scripts/XML_dynamic.asp) (R01335) |
| BTC | [CoinGecko API](https://www.coingecko.com/en/api) |
| История USD/EUR | локальная SQLite (накапливается с первого запуска) |
| История KZT/BTC | берётся напрямую из API, в БД не сохраняется |

---

## 🗂 Хранение данных

- `rates.db` — SQLite с двумя таблицами: `rates` (курсы ЦБ) и `users` (для рассылки).
- История курсов хранится **30 дней** — старые записи удаляются автоматически.
- `.env`, `*.db` и `__pycache__/` — в `.gitignore`, в репозиторий не попадают.
