# 💱 Currency Converter Bot

Telegram-бот для конвертации валют с актуальными курсами и графиками.

## Возможности

- Конвертация **BTC ↔ USD**, **USD ↔ ₽**, **EUR ↔ ₽**
- Курс USD/EUR берётся с **ЦБ РФ**, BTC — с **CoinGecko**
- График курса за последние 7 дней прямо в чате
- История курсов ЦБ РФ хранится локально в SQLite и пополняется каждый день
- Цена BTC обновляется каждые 10 минут

## Стек

- Python 3.14
- [aiogram 3](https://docs.aiogram.dev/) — Telegram Bot API
- [matplotlib](https://matplotlib.org/) — графики
- SQLite — хранение истории курсов ЦБ РФ
- aiohttp — HTTP-запросы

## Запуск

```bash
pip install aiogram aiohttp matplotlib
python bot.py
```

## Источники данных

| Валюта | Источник |
|--------|----------|
| USD, EUR | [cbr-xml-daily.ru](https://www.cbr-xml-daily.ru) (ЦБ РФ) |
| BTC | [CoinGecko API](https://www.coingecko.com/en/api) |
| История USD/EUR | SQLite БД (накапливается с первого запуска) |
