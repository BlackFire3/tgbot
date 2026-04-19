import asyncio
import io
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent, Message,
)


# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = Path(__file__).parent / "rates.db"

# code -> (ticker, emoji). "rub" нужна как равноправная валюта для кросс-конверсии.
CURRENCIES = {
    "btc": ("BTC", "🪙"),
    "usd": ("USD", "💵"),
    "eur": ("EUR", "💶"),
    "kzt": ("KZT", "🇰🇿"),
    "rub": ("RUB", "₽"),
}
VALID_CODES = set(CURRENCIES)


# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rates (
                date     TEXT NOT NULL,
                currency TEXT NOT NULL,
                rate     REAL NOT NULL,
                PRIMARY KEY (date, currency)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                last_name  TEXT,
                joined_at  TEXT NOT NULL,
                last_seen  TEXT NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 1
            )
        """)


def db_save_rate(date: str, currency: str, rate: float) -> None:
    """Единицы: RUB/unit для usd/eur/kzt, USD/unit для btc."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rates VALUES (?, ?, ?)",
            (date, currency, rate),
        )


def db_get_history(currency: str, days: int = 8) -> list[tuple[datetime, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, rate FROM rates WHERE currency = ? ORDER BY date ASC",
            (currency,),
        ).fetchall()
    return [(datetime.strptime(r[0], "%Y-%m-%d"), r[1]) for r in rows[-days:]]


def db_has_today(currency: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rates WHERE date = ? AND currency = ?",
            (today, currency),
        ).fetchone()[0]
    return count > 0


def db_cleanup_old_rates(keep_days: int = 30) -> None:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        deleted = conn.execute(
            "DELETE FROM rates WHERE date < ?", (cutoff,)
        ).rowcount
    if deleted:
        logging.info("Cleanup: removed %d rate records older than %s", deleted, cutoff)


def db_register_user(user_id: int, username: str | None,
                     first_name: str | None, last_name: str | None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_seen  = excluded.last_seen,
                is_active  = 1
        """, (user_id, username, first_name, last_name, now, now))


def db_mark_inactive(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))


def db_get_active_user_ids() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_active = 1").fetchall()
    return [r[0] for r in rows]


def db_user_stats() -> tuple[int, int]:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
    return total, active


# ── External rate APIs ────────────────────────────────────────────────────────

_btc_price: float | None = None


async def fetch_cbr_rates() -> dict[str, float]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.cbr-xml-daily.ru/daily_json.js", timeout=15
        ) as resp:
            data = await resp.json(content_type=None)
    return {
        "usd": float(data["Valute"]["USD"]["Value"]),
        "eur": float(data["Valute"]["EUR"]["Value"]),
    }


async def fetch_kzt_rate() -> float:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.cbr-xml-daily.ru/daily_json.js", timeout=15
        ) as resp:
            data = await resp.json(content_type=None)
    valute = data["Valute"]["KZT"]
    return float(valute["Value"]) / float(valute["Nominal"])


async def fetch_kzt_weekly() -> list[tuple[datetime, float]]:
    end = datetime.now()
    start = end - timedelta(days=10)
    url = (
        "https://www.cbr.ru/scripts/XML_dynamic.asp"
        f"?date_req1={start.strftime('%d/%m/%Y')}"
        f"&date_req2={end.strftime('%d/%m/%Y')}"
        "&VAL_NM_RQ=R01335"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as resp:
            text = await resp.text(encoding="windows-1251")
    root = ET.fromstring(text)
    points: list[tuple[datetime, float]] = []
    for rec in root.findall("Record"):
        dt = datetime.strptime(rec.get("Date"), "%d.%m.%Y")
        value = float(rec.find("Value").text.replace(",", "."))
        nominal = float(rec.find("Nominal").text)
        points.append((dt, value / nominal))
    return points[-7:]


async def fetch_btc_price() -> float:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            timeout=15,
        ) as resp:
            data = await resp.json()
    return float(data["bitcoin"]["usd"])


async def fetch_btc_weekly() -> list[tuple[datetime, float]]:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
            "?vs_currency=usd&days=7&interval=daily",
            timeout=20,
        ) as resp:
            data = await resp.json()
    points = [(datetime.fromtimestamp(p[0] / 1000), p[1]) for p in data["prices"]]
    current = _btc_price or (await fetch_btc_price())
    if points:
        points[-1] = (points[-1][0], current)
    return points


# ── Rate helpers & conversion ─────────────────────────────────────────────────

async def get_btc_usd_rate() -> float:
    return _btc_price if _btc_price is not None else await fetch_btc_price()


async def get_rub_rate(currency: str) -> float:
    """Рублей за 1 единицу валюты. Универсальная базовая ставка для конверсии."""
    if currency == "rub":
        return 1.0
    if currency == "btc":
        btc_usd = await get_btc_usd_rate()
        usd_rub = (await fetch_cbr_rates())["usd"]
        return btc_usd * usd_rub
    if currency == "kzt":
        return await fetch_kzt_rate()
    rates = await fetch_cbr_rates()  # usd, eur
    return rates[currency]


async def convert(amount: float, src: str, dst: str) -> float:
    """Универсальная конверсия через RUB как промежуточную."""
    if src == dst:
        return amount
    src_rub = await get_rub_rate(src)
    dst_rub = await get_rub_rate(dst)
    return amount * src_rub / dst_rub


def _apply_live_last_point(
    points: list[tuple[datetime, float]], live_rate: float, max_days: int = 7
) -> list[tuple[datetime, float]]:
    """Гарантирует, что последняя точка графика = текущему live-курсу (который видит конверсия).
    Если последняя точка уже за сегодня — перезаписываем значение,
    иначе добавляем новую точку на сегодня и подрезаем до max_days.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if points and points[-1][0].date() == today.date():
        points[-1] = (points[-1][0], live_rate)
    else:
        points.append((today, live_rate))
        points = points[-max_days:]
    return points


async def get_weekly_rates(currency: str) -> list[tuple[datetime, float]]:
    if currency == "btc":
        # fetch_btc_weekly сама подменяет последнюю точку на live-цену
        return await fetch_btc_weekly()

    if currency == "kzt":
        points = await fetch_kzt_weekly()
        if not points:
            raise ValueError("Не удалось получить историю курса KZT. Попробуй позже.")
        try:
            live = await fetch_kzt_rate()
            points = _apply_live_last_point(points, live)
        except Exception:
            logging.exception("failed to refresh live KZT rate for chart")
        return points

    # usd / eur — история в SQLite, текущий курс из CBR JSON
    rows = db_get_history(currency)
    if not rows:
        raise ValueError(
            "История курсов ЦБ ещё не накоплена.\n"
            "Данные начнут собираться с сегодняшнего дня — попробуй завтра."
        )
    try:
        live = (await fetch_cbr_rates()).get(currency)
        if live is not None:
            rows = _apply_live_last_point(rows, live)
    except Exception:
        logging.exception("failed to refresh live CBR rate for chart")
    return rows


def get_rate_delta(currency: str) -> tuple[float, float] | None:
    """Возвращает (абс. изменение, % изменение) между последней и предпоследней записью в SQLite.
    None, если данных недостаточно. Для btc — в USD, для usd/eur/kzt — в RUB.
    """
    rows = db_get_history(currency, days=3)
    if len(rows) < 2:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    today_rate = None
    prev_rate = None
    for dt, r in reversed(rows):
        if dt.strftime("%Y-%m-%d") == today and today_rate is None:
            today_rate = r
        elif r > 0 and dt.strftime("%Y-%m-%d") != today:
            prev_rate = r
            break
    # Если сегодня не сохранено — сравниваем две последние записи
    if today_rate is None:
        today_rate = rows[-1][1]
        prev_rate = rows[-2][1] if len(rows) >= 2 else None
    if today_rate is None or prev_rate is None or prev_rate == 0:
        return None
    delta = today_rate - prev_rate
    return delta, delta / prev_rate * 100


# ── Background tasks ──────────────────────────────────────────────────────────

async def save_today_rates() -> None:
    """Один раз в сутки: кладём в SQLite usd/eur/kzt/btc. BTC — в USD, остальные — в RUB."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        rates = await fetch_cbr_rates()
        for cur, r in rates.items():
            db_save_rate(today, cur, r)
    except Exception:
        logging.exception("Failed to save CBR rates")
    try:
        db_save_rate(today, "kzt", await fetch_kzt_rate())
    except Exception:
        logging.exception("Failed to save KZT rate")
    try:
        db_save_rate(today, "btc", await get_btc_usd_rate())
    except Exception:
        logging.exception("Failed to save BTC rate")
    db_cleanup_old_rates()
    logging.info("Daily rates saved for %s", today)


async def rate_updater() -> None:
    """Раз в час добираем пропущенные сегодняшние записи."""
    while True:
        try:
            if not all(db_has_today(c) for c in ("usd", "eur", "kzt", "btc")):
                await save_today_rates()
        except Exception:
            logging.exception("rate_updater failed")
        await asyncio.sleep(3600)


async def btc_price_updater() -> None:
    """Раз в 10 минут обновляем кэш BTC/USD."""
    global _btc_price
    while True:
        try:
            _btc_price = await fetch_btc_price()
            logging.info("BTC price updated: $%s", _btc_price)
        except Exception:
            logging.exception("btc_price_updater failed")
        await asyncio.sleep(600)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_amount(value: float) -> str:
    """До 4 знаков после запятой, без научной нотации и хвостовых нулей."""
    return f"{round(value, 4):.4f}".rstrip("0").rstrip(".")


def fmt_currency(amount: float, code: str) -> str:
    """Человекочитаемое представление суммы в указанной валюте."""
    if code == "btc":
        return f"{fmt_amount(amount)} 🪙 BTC"
    if 0 < amount < 1:
        s = fmt_amount(amount)
    else:
        s = f"{amount:,.2f}".replace(",", " ")
    if code == "usd":
        return f"${s}"
    if code == "eur":
        return f"€{s}"
    if code == "rub":
        return f"{s} ₽"
    if code == "kzt":
        return f"{s} 🇰🇿 KZT"
    return s


def fmt_label(code: str) -> str:
    ticker, emoji = CURRENCIES[code]
    return f"{emoji} {ticker}"


def fmt_delta_line(currency: str) -> str:
    """Строка с Δ за сутки (курса currency к его базе — RUB, для BTC — к USD)."""
    if currency == "rub":
        return ""
    d = get_rate_delta(currency)
    if not d:
        return ""
    abs_d, pct = d
    arrow = "▲" if abs_d > 0 else ("▼" if abs_d < 0 else "•")
    if currency == "btc":
        val = f"{abs_d:+,.0f}".replace(",", " ") + " $"
    elif currency == "kzt":
        val = f"{abs_d:+.4f} ₽"
    else:
        val = f"{abs_d:+.2f} ₽"
    return f"\n_{arrow} {val} ({pct:+.2f}%) за сутки_"


# ── Chart ─────────────────────────────────────────────────────────────────────

def build_chart(currency: str, src: str, dst: str, data: list[tuple[datetime, float]]) -> bytes:
    """Строит график курса src → dst за ~7 дней.

    Источник данных `data` хранится в каноническом направлении:
      - для btc: USD за 1 BTC
      - для fiat: RUB за 1 currency
    Если конверсия пользователя идёт в обратную сторону (RUB → X, USD → BTC),
    значения инвертируются, чтобы последняя точка на графике численно
    совпадала с «1 src = X dst» из ответа конверсии.
    """
    if not data:
        raise ValueError("Нет данных для построения графика")
    is_btc = currency == "btc"
    ticker, _ = CURRENCIES[currency]
    src_ticker = CURRENCIES[src][0]
    dst_ticker = CURRENCIES[dst][0]

    # Прямая пара — одна из сторон RUB (для fiat) или {btc,usd} (для BTC):
    # тогда график можно развернуть в сторону, которую выбрал пользователь.
    direct_fiat = not is_btc and (src == "rub" or dst == "rub")
    direct_btc = is_btc and {src, dst} <= {"btc", "usd"}
    is_direct = direct_fiat or direct_btc

    if is_direct:
        inverted = (src == "usd") if is_btc else (src == "rub")
    else:
        inverted = False  # для кросс-пар (usd↔eur и т.п.) показываем src→RUB как есть

    dates = [d[0] for d in data]
    prices_raw = [d[1] for d in data]
    prices = [1 / p if p > 0 else 0 for p in prices_raw] if inverted else prices_raw

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    color = "#f7931a" if is_btc else ("#00c896" if currency == "kzt" else "#4cc9f0")
    ax.plot(dates, prices, color=color, linewidth=2.5, zorder=3)
    ax.fill_between(dates, prices, min(prices) * 0.998, alpha=0.25, color=color)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.tick_params(colors="#e0e0e0", labelsize=10)
    for spine in ax.spines.values():
        spine.set_edgecolor("#2d2d5e")
    ax.grid(True, color="#2d2d5e", linestyle="--", alpha=0.6, zorder=0)

    # Формат Y-оси и подписи последней точки — под выбранное направление
    last_val = prices[-1]
    if is_btc and not inverted:         # BTC → USD
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:,.0f}"))
        y_label = "USD за 1 🪙 BTC"
        last_label = f"${last_val:,.0f}"
    elif is_btc and inverted:           # USD → BTC
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.6f}"))
        y_label = "🪙 BTC за $1"
        last_label = f"{last_val:.6f} 🪙 BTC"
    elif currency == "kzt" and not inverted:   # KZT → RUB
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.4f} ₽"))
        y_label = "₽ за 1 🇰🇿 KZT"
        last_label = f"{last_val:.4f} ₽"
    elif currency == "kzt" and inverted:       # RUB → KZT
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.2f}"))
        y_label = "🇰🇿 KZT за 1 ₽"
        last_label = f"{last_val:.2f} 🇰🇿 KZT"
    elif not inverted:                          # USD/EUR → RUB или кросс (src→RUB)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.2f} ₽"))
        y_label = f"₽ за 1 {ticker}"
        last_label = f"{last_val:,.2f} ₽"
    else:                                       # RUB → USD / RUB → EUR
        prefix = "$" if currency == "usd" else "€"
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.4f}"))
        y_label = f"{ticker} за 1 ₽"
        last_label = f"{prefix}{last_val:.4f}"

    ax.scatter([dates[-1]], [last_val], color=color, s=60, zorder=5)
    ax.annotate(
        last_label,
        xy=(dates[-1], last_val),
        xytext=(0, 10),
        textcoords="offset points",
        ha="center",
        fontsize=10,
        fontweight="bold",
        color="#ffffff",
        bbox=dict(boxstyle="round,pad=0.3", fc="#1a1a2e", ec=color, lw=1.2),
        zorder=6,
    )

    days_shown = len(dates)
    period = f"последние {days_shown} дн." if days_shown < 7 else "последние 7 дней"
    if is_direct:
        direction = f"{src_ticker} → {dst_ticker}"
    else:
        # Кросс: честно указываем, что на графике src к RUB
        direction = f"{src_ticker} → RUB (курс ЦБ)"
    ax.set_title(f"{direction}  |  {period}", color="#e0e0e0", fontsize=13, pad=10)
    ax.set_ylabel(y_label, color="#a0a0c0", fontsize=10)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🪙 BTC → $", callback_data="conv:btc:usd"),
            InlineKeyboardButton(text="💵 USD → ₽", callback_data="conv:usd:rub"),
            InlineKeyboardButton(text="💶 EUR → ₽", callback_data="conv:eur:rub"),
        ],
        [
            InlineKeyboardButton(text="$ → 🪙 BTC", callback_data="conv:usd:btc"),
            InlineKeyboardButton(text="₽ → 💵 USD", callback_data="conv:rub:usd"),
            InlineKeyboardButton(text="₽ → 💶 EUR", callback_data="conv:rub:eur"),
        ],
        [
            InlineKeyboardButton(text="🇰🇿 KZT → ₽", callback_data="conv:kzt:rub"),
            InlineKeyboardButton(text="₽ → 🇰🇿 KZT", callback_data="conv:rub:kzt"),
            InlineKeyboardButton(text="🌍 Другая пара", callback_data="pick:from"),
        ],
    ])


def _grid_buttons(codes: list[str], prefix: str, per_row: int = 3) -> list[list[InlineKeyboardButton]]:
    rows, row = [], []
    for code in codes:
        row.append(InlineKeyboardButton(text=fmt_label(code), callback_data=f"{prefix}:{code}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def pick_from_keyboard() -> InlineKeyboardMarkup:
    rows = _grid_buttons(list(CURRENCIES), "pick_from")
    rows.append([InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pick_to_keyboard(src: str) -> InlineKeyboardMarkup:
    targets = [c for c in CURRENCIES if c != src]
    rows = _grid_buttons(targets, f"conv:{src}")
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="pick:from")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prompt_keyboard(src: str, dst: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 График за неделю", callback_data=f"chart:{src}:{dst}")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")]
    ])


# ── FSM ───────────────────────────────────────────────────────────────────────

class ConvertStates(StatesGroup):
    waiting_amount = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()


def register_user(message: Message) -> None:
    u = message.from_user
    if u:
        db_register_user(u.id, u.username, u.first_name, u.last_name)


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


# ── Handlers ──────────────────────────────────────────────────────────────────

dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    register_user(message)
    await state.clear()
    await message.answer("👋 Привет! Выбери направление конвертации:", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def on_help(message: Message) -> None:
    if is_admin(message.from_user.id):
        await message.answer(
            "📋 *Доступные команды*\n\n"
            "👤 *Для всех:*\n"
            "/start — перезапустить бота и открыть меню\n"
            "/help — показать эту справку\n\n"
            "🔑 *Только для тебя:*\n"
            "/broadcast — рассылка текстового сообщения всем пользователям\n"
            "/stats — статистика пользователей (всего / активных)\n"
            "/cancel — отменить рассылку\n\n"
            "💡 *Inline-режим:* в любом чате наберите `@currencycheckertest123_bot <сумма> <валюта> [целевая]` Если целевая не указана — подставляется rub (для BTC — usd).",
            parse_mode="Markdown",
        )
    else:
        await message.answer(
            "📋 *Команды бота:*\n\n"
            "/start — перезапустить бота и открыть меню\n\n"
            "💡 *Inline-режим:* в любом чате наберите `@currencycheckertest123_bot <сумма> <валюта> [целевая]` Если целевая не указана — подставляется rub (для BTC — usd).",
            parse_mode="Markdown",
        )


@dp.message(Command("stats"))
async def on_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    total, active = db_user_stats()
    await message.answer(
        f"📊 *Статистика пользователей*\n"
        f"Всего: {total}\n"
        f"Активных: {active}",
        parse_mode="Markdown",
    )


@dp.message(Command("broadcast"))
async def on_broadcast_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    _, active = db_user_stats()
    await state.set_state(BroadcastStates.waiting_message)
    await message.answer(
        f"✉️ Введи текст для рассылки.\n"
        f"Получатели: *{active}* активных пользователей.\n\n"
        f"Для отмены — /cancel",
        parse_mode="Markdown",
    )


@dp.message(Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current == BroadcastStates.waiting_message:
        await message.answer("❌ Рассылка отменена.")
    else:
        await message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.message(BroadcastStates.waiting_message)
async def on_broadcast_message(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text:
        await message.answer("⚠️ Поддерживается только текст. Введи текстовое сообщение:")
        return
    await state.clear()
    user_ids = db_get_active_user_ids()
    sent = failed = blocked = 0
    status_msg = await message.answer(f"⏳ Начинаю рассылку для {len(user_ids)} чел...")
    for uid in user_ids:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err or "not found" in err:
                db_mark_inactive(uid)
                blocked += 1
            else:
                failed += 1
        await asyncio.sleep(0.05)  # Telegram rate limit
    await status_msg.edit_text(
        f"✅ Рассылка завершена\n"
        f"Доставлено: {sent}\n"
        f"Заблокировали бота: {blocked}\n"
        f"Ошибки: {failed}"
    )


@dp.callback_query(F.data == "back:menu")
async def on_back_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    chart_msg_id = data.get("chart_msg_id")
    if chart_msg_id:
        try:
            await callback.bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=chart_msg_id,
            )
        except Exception:
            pass
    await callback.message.answer("Выбери направление конвертации:", reply_markup=main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "pick:from")
async def on_pick_from(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "Выбери исходную валюту:",
        reply_markup=pick_from_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pick_from:"))
async def on_pick_to(callback: CallbackQuery, state: FSMContext) -> None:
    _, src = callback.data.split(":")
    if src not in VALID_CODES:
        await callback.answer("Неизвестная валюта")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"Выбрано: *{fmt_label(src)}*\nВ какую валюту конвертировать?",
        parse_mode="Markdown",
        reply_markup=pick_to_keyboard(src),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("conv:"))
async def on_direction(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неверный callback")
        return
    _, src, dst = parts
    if src not in VALID_CODES or dst not in VALID_CODES or src == dst:
        await callback.answer("Неверная пара")
        return
    await state.set_state(ConvertStates.waiting_amount)
    await state.update_data(src=src, dst=dst)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"Введи количество *{fmt_label(src)}* для конвертации в *{fmt_label(dst)}*:",
        parse_mode="Markdown",
        reply_markup=prompt_keyboard(src, dst),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("chart:"))
async def on_chart(callback: CallbackQuery, state: FSMContext) -> None:
    _, src, dst = callback.data.split(":")
    # Валюта графика: BTC → BTC/USD, иначе non-rub сторона
    if "btc" in (src, dst):
        currency = "btc"
    elif src == "rub":
        currency = dst
    elif dst == "rub":
        currency = src
    else:
        currency = src  # для кросс-пар (usd↔eur и т.п.) — график src/RUB
    if currency not in CURRENCIES or currency == "rub":
        await callback.answer("График для этой пары недоступен")
        return

    await callback.answer("⏳ Загружаю график...")
    try:
        weekly = await get_weekly_rates(currency)
        chart_bytes = build_chart(currency, src, dst, weekly)
    except Exception as e:
        logging.exception("chart build failed")
        await callback.message.answer(f"Не удалось построить график: {e}")
        return

    chat_id = callback.message.chat.id
    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass
    chart_msg = await callback.bot.send_photo(
        chat_id=chat_id,
        photo=BufferedInputFile(chart_bytes, filename="chart.png"),
        caption=f"📊 *{fmt_label(src)} → {fmt_label(dst)}* — 7 дней",
        parse_mode="Markdown",
    )
    await callback.bot.send_message(
        chat_id=chat_id,
        text=f"Введи количество *{fmt_label(src)}* для конвертации в *{fmt_label(dst)}*:",
        parse_mode="Markdown",
        reply_markup=back_keyboard(),
    )
    await state.update_data(chart_msg_id=chart_msg.message_id)


@dp.message(ConvertStates.waiting_amount)
async def on_amount(message: Message, state: FSMContext) -> None:
    register_user(message)
    if message.text is None:
        await state.clear()
        await message.answer(
            "⚠️ Неверный формат. Пожалуйста, введи числовое значение.\n\n"
            "Выбери направление конвертации:",
            reply_markup=main_keyboard(),
        )
        return

    text = message.text.replace(",", ".").replace(" ", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await state.clear()
        await message.answer(
            "⚠️ Неверный формат. Пожалуйста, введи числовое значение больше нуля.\n\n"
            "Выбери направление конвертации:",
            reply_markup=main_keyboard(),
        )
        return

    data = await state.get_data()
    src, dst = data.get("src"), data.get("dst")
    if src not in VALID_CODES or dst not in VALID_CODES or src == dst:
        await state.clear()
        await message.answer("Сессия сброшена. Нажми /start", reply_markup=main_keyboard())
        return

    try:
        result = await convert(amount, src, dst)
    except Exception as e:
        logging.exception("convert failed")
        await state.clear()
        await message.answer(f"Не удалось получить курс: {e}. Попробуй позже.")
        return

    from_str = fmt_currency(amount, src)
    to_str = fmt_currency(result, dst)
    per_unit = result / amount
    rate_str = f"1 {CURRENCIES[src][0]} = {fmt_currency(per_unit, dst)}"
    # Δ за сутки показываем для «основной» валюты пары (не рублёвой стороны)
    delta = fmt_delta_line(src if src != "rub" else dst)

    await message.answer(
        f"💱 *{from_str} = {to_str}*\n_{rate_str}_{delta}",
        parse_mode="Markdown",
    )
    await state.clear()
    await message.answer("🔄 Ещё конвертация?", reply_markup=main_keyboard())


@dp.message(StateFilter(None))
async def on_unknown_message(message: Message) -> None:
    """Любое сообщение вне FSM — показываем главное меню."""
    register_user(message)
    await message.answer("Выбери направление конвертации:", reply_markup=main_keyboard())


# ── Inline mode ───────────────────────────────────────────────────────────────

INLINE_RE = re.compile(
    r"^\s*([\d][\d\s.,]*)\s*(btc|usd|eur|kzt|rub|₽|\$|€)\s*(?:to|в|->|→|-)?\s*"
    r"(btc|usd|eur|kzt|rub|₽|\$|€)?\s*$",
    re.IGNORECASE,
)
SYMBOL_MAP = {"₽": "rub", "$": "usd", "€": "eur"}


def parse_inline_query(q: str) -> tuple[float, str, str] | None:
    if not q:
        return None
    m = INLINE_RE.match(q)
    if not m:
        return None
    raw_num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        amount = float(raw_num)
        if amount <= 0:
            return None
    except ValueError:
        return None

    def normalize(s: str) -> str:
        s = s.lower()
        return SYMBOL_MAP.get(s, s)

    src = normalize(m.group(2))
    dst = normalize(m.group(3)) if m.group(3) else ("usd" if src == "btc" else "rub")
    if src not in VALID_CODES or dst not in VALID_CODES or src == dst:
        return None
    return amount, src, dst


@dp.inline_query()
async def on_inline(query: InlineQuery) -> None:
    parsed = parse_inline_query(query.query)
    if not parsed:
        hint = InlineQueryResultArticle(
            id="hint",
            title="Введи сумму и валюту",
            description="Примеры: 100 usd • 50 eur rub • 0.1 btc • 1000 kzt usd",
            input_message_content=InputTextMessageContent(
                message_text="Формат: `<сумма> <валюта> [целевая]`\nПример: `100 usd rub`",
                parse_mode="Markdown",
            ),
        )
        await query.answer(results=[hint], cache_time=5, is_personal=True)
        return

    amount, src, dst = parsed
    try:
        result = await convert(amount, src, dst)
    except Exception:
        logging.exception("inline convert failed")
        err = InlineQueryResultArticle(
            id="err",
            title="⚠️ Курс временно недоступен",
            description="Попробуй позже",
            input_message_content=InputTextMessageContent(
                message_text="Курс временно недоступен. Попробуй позже."
            ),
        )
        await query.answer(results=[err], cache_time=5, is_personal=True)
        return

    from_str = fmt_currency(amount, src)
    to_str = fmt_currency(result, dst)
    per_unit = result / amount
    rate_str = f"1 {CURRENCIES[src][0]} = {fmt_currency(per_unit, dst)}"
    delta = fmt_delta_line(src if src != "rub" else dst)

    article = InlineQueryResultArticle(
        id=f"{src}-{dst}-{amount}",
        title=f"{from_str} = {to_str}",
        description=rate_str,
        input_message_content=InputTextMessageContent(
            message_text=f"💱 *{from_str} = {to_str}*\n_{rate_str}_{delta}",
            parse_mode="Markdown",
        ),
    )
    await query.answer(results=[article], cache_time=30)


# ── Entry point ───────────────────────────────────────────────────────────────

async def setup_bot_commands(bot: Bot) -> None:
    user_commands = [
        BotCommand(command="start", description="Открыть меню конвертации"),
        BotCommand(command="help",  description="Список команд"),
    ]
    admin_commands = user_commands + [
        BotCommand(command="broadcast", description="Рассылка сообщения всем пользователям"),
        BotCommand(command="stats",     description="Статистика пользователей"),
        BotCommand(command="cancel",    description="Отменить рассылку"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    if ADMIN_ID:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    try:
        await save_today_rates()
    except Exception:
        logging.exception("Failed to save initial rates")

    bot = Bot(BOT_TOKEN)
    await setup_bot_commands(bot)

    asyncio.create_task(rate_updater())
    asyncio.create_task(btc_price_updater())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
