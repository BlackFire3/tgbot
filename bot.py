import asyncio
import io
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH = Path(__file__).parent / "rates.db"

# ── Admin settings ─────────────────────────────────────────────────────────────
# Укажи свой Telegram user_id в файле .env (ADMIN_ID=123456789)
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

CURRENCIES = {
    "btc": ("BTC", "🪙"),
    "usd": ("USD", "💵"),
    "eur": ("EUR", "💶"),
    "kzt": ("KZT", "🇰🇿"),
}


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


def db_save(date: str, currency: str, rate: float) -> None:
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


# ── Users ─────────────────────────────────────────────────────────────────────

def db_register_user(user_id: int, username: str | None,
                     first_name: str | None, last_name: str | None) -> None:
    """Upsert user record. Updates username/name and last_seen on every call."""
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
    """Called when bot is blocked — mark user as inactive so they skip broadcasts."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )


def db_get_active_user_ids() -> list[int]:
    """Return all user_ids eligible for broadcast."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE is_active = 1"
        ).fetchall()
    return [r[0] for r in rows]


def db_user_stats() -> tuple[int, int]:
    """Return (total_users, active_users)."""
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]
    return total, active


# ── CBR fetch & save ──────────────────────────────────────────────────────────

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


def db_cleanup_old_rates(keep_days: int = 30) -> None:
    """Delete rate records older than keep_days, keeping at most 30 days of history."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        deleted = conn.execute(
            "DELETE FROM rates WHERE date < ?", (cutoff,)
        ).rowcount
    if deleted:
        logging.info("Cleanup: removed %d rate records older than %s", deleted, cutoff)


async def save_today_cbr_rates() -> dict[str, float]:
    today = datetime.now().strftime("%Y-%m-%d")
    rates = await fetch_cbr_rates()
    for currency, rate in rates.items():
        db_save(today, currency, rate)
    db_cleanup_old_rates()
    logging.info("CBR rates saved for %s: %s", today, rates)
    return rates


async def rate_updater() -> None:
    """Background task: every hour check if today's CBR rates are saved."""
    while True:
        try:
            if not db_has_today("usd") or not db_has_today("eur"):
                await save_today_cbr_rates()
        except Exception:
            logging.exception("rate_updater: failed to fetch/save CBR rates")
        await asyncio.sleep(3600)


# ── BTC price cache (updated every 10 min) ────────────────────────────────────

_btc_price: float | None = None


async def _fetch_btc_price() -> float:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            timeout=15,
        ) as resp:
            data = await resp.json()
    return float(data["bitcoin"]["usd"])


async def btc_price_updater() -> None:
    """Background task: refresh BTC/USD price every 10 minutes."""
    global _btc_price
    while True:
        try:
            _btc_price = await _fetch_btc_price()
            logging.info("BTC price updated: $%s", _btc_price)
        except Exception:
            logging.exception("btc_price_updater: failed")
        await asyncio.sleep(600)


# ── Rates for conversion ──────────────────────────────────────────────────────

async def get_btc_usd_rate() -> float:
    if _btc_price is not None:
        return _btc_price
    return await _fetch_btc_price()


async def get_rate_to_rub(currency: str) -> float:
    if currency == "kzt":
        return await _fetch_kzt_rate()
    rates = await fetch_cbr_rates()
    return rates[currency]


# ── KZT — current rate & 7-day history from CBR XML API ──────────────────────

async def _fetch_kzt_rate() -> float:
    """Current KZT/RUB rate from CBR JSON (per 1 KZT)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.cbr-xml-daily.ru/daily_json.js", timeout=15
        ) as resp:
            data = await resp.json(content_type=None)
    valute = data["Valute"]["KZT"]
    return float(valute["Value"]) / float(valute["Nominal"])


async def _fetch_kzt_weekly() -> list[tuple[datetime, float]]:
    """7-day KZT/RUB history from CBR dynamic XML API (R01335 = KZT, nominal 100)."""
    end = datetime.now()
    start = end - timedelta(days=10)   # запас на выходные
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


# ── Historical rates for chart ────────────────────────────────────────────────

async def get_weekly_rates(currency: str) -> list[tuple[datetime, float]]:
    if currency == "btc":
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                "?vs_currency=usd&days=7&interval=daily",
                timeout=20,
            ) as resp:
                data = await resp.json()
        points = [(datetime.fromtimestamp(p[0] / 1000), p[1]) for p in data["prices"]]
        current = _btc_price or (await _fetch_btc_price())
        if points:
            points[-1] = (points[-1][0], current)
        return points

    if currency == "kzt":
        points = await _fetch_kzt_weekly()
        if not points:
            raise ValueError("Не удалось получить историю курса KZT. Попробуй позже.")
        return points

    rows = db_get_history(currency)
    if not rows:
        raise ValueError(
            "История курсов ЦБ ещё не накоплена.\n"
            "Данные начнут собираться с сегодняшнего дня — попробуй завтра."
        )
    return rows


# ── FSM States ────────────────────────────────────────────────────────────────

class ConvertStates(StatesGroup):
    waiting_amount = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()


# ── Chart ─────────────────────────────────────────────────────────────────────

def build_chart(currency: str, src: str, data: list[tuple[datetime, float]]) -> bytes:
    if not data:
        raise ValueError("Нет данных для построения графика")
    ticker, _ = CURRENCIES[currency]
    dates = [d[0] for d in data]
    prices = [d[1] for d in data]

    is_btc = currency == "btc"

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

    if is_btc:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:,.0f}"))
        y_label = "Цена (USD)"
        last_label = f"${prices[-1]:,.0f}"
    elif currency == "kzt":
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.4f} ₽"))
        y_label = "Курс ЦБ РФ (₽)"
        last_label = f"{prices[-1]:.4f} ₽"
    else:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.2f} ₽"))
        y_label = "Курс ЦБ РФ (₽)"
        last_label = f"{prices[-1]:,.2f} ₽"

    ax.scatter([dates[-1]], [prices[-1]], color=color, s=60, zorder=5)
    ax.annotate(
        last_label,
        xy=(dates[-1], prices[-1]),
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
    if currency == "btc":
        direction = "USD -> BTC" if src in ("usd_d", "usd") else "BTC -> USD"
    else:
        direction = f"RUB -> {ticker}" if src == "rub" else f"{ticker} -> RUB"
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🪙 BTC → $", callback_data="conv:btc:usd_d"),
                InlineKeyboardButton(text="💵 USD → ₽", callback_data="conv:usd:rub"),
                InlineKeyboardButton(text="💶 EUR → ₽", callback_data="conv:eur:rub"),
            ],
            [
                InlineKeyboardButton(text="$ → 🪙 BTC", callback_data="conv:usd_d:btc"),
                InlineKeyboardButton(text="₽ → 💵 USD", callback_data="conv:rub:usd"),
                InlineKeyboardButton(text="₽ → 💶 EUR", callback_data="conv:rub:eur"),
            ],
            [
                InlineKeyboardButton(text="🇰🇿 KZT → ₽", callback_data="conv:kzt:rub"),
                InlineKeyboardButton(text="₽ → 🇰🇿 KZT", callback_data="conv:rub:kzt"),
            ],
        ]
    )


def chart_keyboard(src: str, dst: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 График за неделю", callback_data=f"chart:{src}:{dst}")],
            [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")],
        ]
    )


def _labels(src: str, dst: str) -> tuple[str, str]:
    def label(code: str) -> str:
        if code == "rub":
            return "₽"
        if code == "usd_d":
            return "💵 USD"
        ticker, emoji = CURRENCIES[code]
        return f"{emoji} {ticker}"
    return label(src), label(dst)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_amount(value: float) -> str:
    """Round to max 4 decimal places, no scientific notation, no trailing zeros."""
    rounded = round(value, 4)
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_CODES = {*CURRENCIES, "rub", "usd_d"}


def _register(message: Message) -> None:
    """Save/update user from any incoming message."""
    u = message.from_user
    if u:
        db_register_user(u.id, u.username, u.first_name, u.last_name)


# ── Bot handlers ──────────────────────────────────────────────────────────────

dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    _register(message)
    await state.clear()
    await message.answer("👋 Привет! Выбери направление конвертации:", reply_markup=main_keyboard())


@dp.message(Command("help"))
async def on_help(message: Message) -> None:
    if ADMIN_ID and message.from_user.id == ADMIN_ID:
        await message.answer(
            "📋 *Доступные команды*\n\n"
            "👤 *Для всех:*\n"
            "/start — перезапустить бота и открыть меню\n"
            "/help — показать эту справку\n\n"
            "🔑 *Только для тебя:*\n"
            "/broadcast — рассылка текстового сообщения всем пользователям\n"
            "/stats — статистика пользователей (всего / активных)\n"
            "/cancel — отменить рассылку",
            parse_mode="Markdown",
        )
    else:
        await message.answer(
            "📋 *Команды бота:*\n\n"
            "/start — перезапустить бота и открыть меню",
            parse_mode="Markdown",
        )


@dp.message(Command("stats"))
async def on_stats(message: Message) -> None:
    """Admin: show user count."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
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
    """Admin: start broadcast — next message will be sent to all active users."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
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
    if current == BroadcastStates.waiting_message:
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
    else:
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.message(BroadcastStates.waiting_message)
async def on_broadcast_message(message: Message, state: FSMContext, bot: Bot) -> None:
    """Send text message to every active user."""
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
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Выбери направление конвертации:", reply_markup=main_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("conv:"))
async def on_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, src, dst = callback.data.split(":")
    if src not in VALID_CODES or dst not in VALID_CODES:
        await callback.answer("Неизвестная валюта")
        return

    await state.set_state(ConvertStates.waiting_amount)
    from_label, to_label = _labels(src, dst)

    try:
        await callback.message.delete()
    except Exception:
        pass

    sent = await callback.message.answer(
        f"Введи количество *{from_label}* для конвертации в *{to_label}*:",
        parse_mode="Markdown",
        reply_markup=chart_keyboard(src, dst),
    )
    await state.update_data(src=src, dst=dst, prompt_msg_id=sent.message_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("chart:"))
async def on_chart(callback: CallbackQuery, state: FSMContext) -> None:
    _, src, dst = callback.data.split(":")
    currency = "btc" if "btc" in (src, dst) else (dst if src == "rub" else src)
    if currency not in CURRENCIES:
        await callback.answer("Неизвестная валюта")
        return

    await callback.answer("⏳ Загружаю график...")

    try:
        weekly = await get_weekly_rates(currency)
        chart_bytes = build_chart(currency, src, weekly)
    except Exception as e:
        logging.exception("chart build failed")
        await callback.message.answer(f"Не удалось построить график: {e}")
        return

    ticker, emoji = CURRENCIES[currency]
    from_label, to_label = _labels(src, dst)

    try:
        await callback.bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
        )
    except Exception:
        pass

    await callback.bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=BufferedInputFile(chart_bytes, filename="chart.png"),
        caption=f"📊 *{emoji} {ticker} / ₽* — курс ЦБ РФ",
        parse_mode="Markdown",
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")],
    ])
    sent = await callback.bot.send_message(
        chat_id=callback.message.chat.id,
        text=f"Введи количество *{from_label}* для конвертации в *{to_label}*:",
        parse_mode="Markdown",
        reply_markup=back_kb,
    )
    await state.update_data(prompt_msg_id=sent.message_id)


@dp.message(ConvertStates.waiting_amount)
async def on_amount(message: Message, state: FSMContext) -> None:
    _register(message)

    # Принимаем только текстовые сообщения с числом
    raw = message.text
    if raw is None:
        # Пользователь прислал медиа, стикер и т.п.
        await state.clear()
        await message.answer(
            "⚠️ Неверный формат. Пожалуйста, введи числовое значение.\n\n"
            "Выбери направление конвертации:",
            reply_markup=main_keyboard(),
        )
        return

    text = raw.replace(",", ".").strip()
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

    # BTC ↔ USD pair
    if "btc" in (src, dst):
        try:
            rate = await get_btc_usd_rate()
        except Exception as e:
            logging.exception("btc rate fetch failed")
            await message.answer(f"Не удалось получить курс: {e}. Попробуй позже.")
            await state.clear()
            return
        if src == "btc":
            from_str = f"{fmt_amount(amount)} 🪙 BTC"
            to_str = f"${amount * rate:,.2f}".replace(",", " ")
        else:
            from_str = f"${amount:,.2f}".replace(",", " ")
            to_str = f"{fmt_amount(amount / rate)} 🪙 BTC"
        rate_str = f"1 BTC = ${rate:,.2f}".replace(",", " ")
        await message.answer(f"💱 *{from_str} = {to_str}*\n_{rate_str}_", parse_mode="Markdown")
        await state.clear()
        await message.answer("🔄 Ещё конвертация?", reply_markup=main_keyboard())
        return

    currency = dst if src == "rub" else src
    if currency not in CURRENCIES:
        await state.clear()
        await message.answer("Сессия сброшена. Нажми /start", reply_markup=main_keyboard())
        return

    try:
        rate = await get_rate_to_rub(currency)
    except Exception as e:
        logging.exception("rate fetch failed")
        await message.answer(f"Не удалось получить курс: {e}. Попробуй позже.")
        await state.clear()
        return

    ticker, emoji = CURRENCIES[currency]
    if src == "rub":
        result_amount = amount / rate
        from_str = f"{amount:,.2f} ₽".replace(",", " ")
        to_str = f"{fmt_amount(result_amount)} {emoji} {ticker}"
    else:
        result_amount = amount * rate
        from_str = f"{fmt_amount(amount)} {emoji} {ticker}"
        to_str = f"{result_amount:,.2f} ₽".replace(",", " ")

    rate_str = f"1 {ticker} = {fmt_amount(rate)} ₽"
    await message.answer(f"💱 *{from_str} = {to_str}*\n_{rate_str}_", parse_mode="Markdown")
    await state.clear()
    await message.answer("🔄 Ещё конвертация?", reply_markup=main_keyboard())


@dp.message(StateFilter(None))
async def on_unknown_message(message: Message) -> None:
    """Любое сообщение вне FSM — показываем главное меню."""
    _register(message)
    await message.answer("Выбери направление конвертации:", reply_markup=main_keyboard())


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    try:
        await save_today_cbr_rates()
    except Exception:
        logging.exception("Failed to save initial CBR rates")
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(rate_updater())
    asyncio.create_task(btc_price_updater())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
