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
    InlineKeyboardMarkup, Message,
)


# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = Path(__file__).parent / "rates.db"

CURRENCIES = {
    "btc": ("BTC", "🪙"),
    "usd": ("USD", "💵"),
    "eur": ("EUR", "💶"),
    "kzt": ("KZT", "🇰🇿"),
}
VALID_CODES = {*CURRENCIES, "rub", "usd_d"}


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
    """Удалить записи курсов старше keep_days, чтобы не разрастаться бесконечно."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        deleted = conn.execute(
            "DELETE FROM rates WHERE date < ?", (cutoff,)
        ).rowcount
    if deleted:
        logging.info("Cleanup: removed %d rate records older than %s", deleted, cutoff)


def db_register_user(user_id: int, username: str | None,
                     first_name: str | None, last_name: str | None) -> None:
    """Upsert пользователя + обновление last_seen при каждом сообщении."""
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
    """(total, active)"""
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
    return total, active


# ── External rate APIs ────────────────────────────────────────────────────────

_btc_price: float | None = None


async def fetch_cbr_rates() -> dict[str, float]:
    """USD и EUR с ЦБ РФ (RUB за 1 единицу)."""
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
    """Текущий KZT/RUB (RUB за 1 тенге)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.cbr-xml-daily.ru/daily_json.js", timeout=15
        ) as resp:
            data = await resp.json(content_type=None)
    valute = data["Valute"]["KZT"]
    return float(valute["Value"]) / float(valute["Nominal"])


async def fetch_kzt_weekly() -> list[tuple[datetime, float]]:
    """7 дней KZT/RUB с XML dynamic API ЦБ (R01335 = KZT)."""
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


# ── Rate helpers ──────────────────────────────────────────────────────────────

async def get_btc_usd_rate() -> float:
    return _btc_price if _btc_price is not None else await fetch_btc_price()


async def get_rate_to_rub(currency: str) -> float:
    if currency == "kzt":
        return await fetch_kzt_rate()
    rates = await fetch_cbr_rates()
    return rates[currency]


async def get_weekly_rates(currency: str) -> list[tuple[datetime, float]]:
    if currency == "btc":
        return await fetch_btc_weekly()
    if currency == "kzt":
        points = await fetch_kzt_weekly()
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


# ── Background tasks ──────────────────────────────────────────────────────────

async def save_today_cbr_rates() -> dict[str, float]:
    today = datetime.now().strftime("%Y-%m-%d")
    rates = await fetch_cbr_rates()
    for currency, rate in rates.items():
        db_save_rate(today, currency, rate)
    db_cleanup_old_rates()
    logging.info("CBR rates saved for %s: %s", today, rates)
    return rates


async def rate_updater() -> None:
    """Раз в час проверяем, сохранён ли сегодняшний курс."""
    while True:
        try:
            if not db_has_today("usd") or not db_has_today("eur"):
                await save_today_cbr_rates()
        except Exception:
            logging.exception("rate_updater: failed to fetch/save CBR rates")
        await asyncio.sleep(3600)


async def btc_price_updater() -> None:
    """Раз в 10 минут обновляем кэш BTC/USD."""
    global _btc_price
    while True:
        try:
            _btc_price = await fetch_btc_price()
            logging.info("BTC price updated: $%s", _btc_price)
        except Exception:
            logging.exception("btc_price_updater: failed")
        await asyncio.sleep(600)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_amount(value: float) -> str:
    """До 4 знаков после запятой, без научной нотации и хвостовых нулей."""
    return f"{round(value, 4):.4f}".rstrip("0").rstrip(".")


def labels(src: str, dst: str) -> tuple[str, str]:
    def label(code: str) -> str:
        if code == "rub":
            return "₽"
        if code == "usd_d":
            return "💵 USD"
        ticker, emoji = CURRENCIES[code]
        return f"{emoji} {ticker}"
    return label(src), label(dst)


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
    if is_btc:
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


def prompt_keyboard(src: str, dst: str) -> InlineKeyboardMarkup:
    """Клавиатура под приглашением ввести сумму: график + возврат в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 График за неделю", callback_data=f"chart:{src}:{dst}")],
            [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Главное меню", callback_data="back:menu")]]
    )


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
    # Удаляем сообщение с кнопкой
    try:
        await callback.message.delete()
    except Exception:
        pass
    # Удаляем график, если он был
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


@dp.callback_query(F.data.startswith("conv:"))
async def on_direction(callback: CallbackQuery, state: FSMContext) -> None:
    _, src, dst = callback.data.split(":")
    if src not in VALID_CODES or dst not in VALID_CODES:
        await callback.answer("Неизвестная валюта")
        return

    await state.set_state(ConvertStates.waiting_amount)
    await state.update_data(src=src, dst=dst)
    from_label, to_label = labels(src, dst)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        f"Введи количество *{from_label}* для конвертации в *{to_label}*:",
        parse_mode="Markdown",
        reply_markup=prompt_keyboard(src, dst),
    )
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
    from_label, to_label = labels(src, dst)
    chat_id = callback.message.chat.id

    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass

    chart_msg = await callback.bot.send_photo(
        chat_id=chat_id,
        photo=BufferedInputFile(chart_bytes, filename="chart.png"),
        caption=f"📊 *{emoji} {ticker} / ₽* — курс ЦБ РФ",
        parse_mode="Markdown",
    )
    await callback.bot.send_message(
        chat_id=chat_id,
        text=f"Введи количество *{from_label}* для конвертации в *{to_label}*:",
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

    text = message.text.replace(",", ".").strip()
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

    # BTC ↔ USD
    if "btc" in (src, dst):
        try:
            rate = await get_btc_usd_rate()
        except Exception as e:
            logging.exception("btc rate fetch failed")
            await state.clear()
            await message.answer(f"Не удалось получить курс: {e}. Попробуй позже.")
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

    # USD/EUR/KZT ↔ RUB
    currency = dst if src == "rub" else src
    if currency not in CURRENCIES:
        await state.clear()
        await message.answer("Сессия сброшена. Нажми /start", reply_markup=main_keyboard())
        return

    try:
        rate = await get_rate_to_rub(currency)
    except Exception as e:
        logging.exception("rate fetch failed")
        await state.clear()
        await message.answer(f"Не удалось получить курс: {e}. Попробуй позже.")
        return

    ticker, emoji = CURRENCIES[currency]
    if src == "rub":
        from_str = f"{amount:,.2f} ₽".replace(",", " ")
        to_str = f"{fmt_amount(amount / rate)} {emoji} {ticker}"
    else:
        from_str = f"{fmt_amount(amount)} {emoji} {ticker}"
        to_str = f"{amount * rate:,.2f} ₽".replace(",", " ")

    rate_str = f"1 {ticker} = {fmt_amount(rate)} ₽"
    await message.answer(f"💱 *{from_str} = {to_str}*\n_{rate_str}_", parse_mode="Markdown")
    await state.clear()
    await message.answer("🔄 Ещё конвертация?", reply_markup=main_keyboard())


@dp.message(StateFilter(None))
async def on_unknown_message(message: Message) -> None:
    """Любое сообщение вне FSM — показываем главное меню."""
    register_user(message)
    await message.answer("Выбери направление конвертации:", reply_markup=main_keyboard())


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
        await save_today_cbr_rates()
    except Exception:
        logging.exception("Failed to save initial CBR rates")

    bot = Bot(BOT_TOKEN)
    await setup_bot_commands(bot)

    asyncio.create_task(rate_updater())
    asyncio.create_task(btc_price_updater())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
