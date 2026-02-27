import os
import json
import asyncio
import aiohttp
import websockets
import sqlite3
import logging
import io
import statistics
import matplotlib.pyplot as plt

from datetime import datetime, timedelta
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile
)

from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

# =====================================================
# ================== CONFIG ===========================
# =====================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_ID", "0"))

BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_PRICE = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

COOLDOWN_MINUTES = 15
DEFAULT_THRESHOLD = 5
DEFAULT_MODE = "both"

logging.basicConfig(level=logging.INFO)

# =====================================================
# ================== DATABASE =========================
# =====================================================

conn = sqlite3.connect("groups.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    alarm_active INTEGER DEFAULT 1,
    threshold REAL DEFAULT 5,
    mode TEXT DEFAULT 'both'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_alarms (
    user_id INTEGER,
    symbol TEXT,
    threshold REAL
)
""")

conn.commit()

if GROUP_CHAT_ID != 0:
    cursor.execute(
        "INSERT OR IGNORE INTO groups (chat_id, threshold, mode) VALUES (?, ?, ?)",
        (GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE),
    )
    conn.commit()

# =====================================================
# ================== MEMORY ===========================
# =====================================================

price_memory = defaultdict(list)
cooldowns = {}
user_cooldowns = {}

# =====================================================
# ================== HELPERS ==========================
# =====================================================

async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

async def calculate_rsi(symbol, period=14, interval="1m", limit=100):
    try:
        data = await fetch_json(
            f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
        )

        closes = [float(x[4]) for x in data]

        gains = []
        losses = []

        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    except:
        return 0

async def get_change(symbol, interval):
    try:
        data = await fetch_json(
            f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit=2"
        )
        open_price = float(data[0][1])
        close_price = float(data[-1][4])
        return round(((close_price - open_price) / open_price) * 100, 2)
    except:
        return 0

async def generate_chart(symbol):
    try:
        data = await fetch_json(
            f"{BINANCE_KLINES}?symbol={symbol}&interval=1m&limit=240"
        )

        closes = [float(x[4]) for x in data]

        plt.figure(figsize=(10, 4))
        plt.plot(closes)
        plt.title(f"{symbol} - Son 4 Saat")
        plt.grid()

        buf = io.BytesIO()
        plt.savefig(buf, format="png")
        buf.seek(0)
        plt.close()
        return buf
    except:
        return None

# =====================================================
# ================== COMMANDS =========================
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚀 KRİPTO ALARM BOTU\n\n"
        "/status\n"
        "/top24\n"
        "/top5\n"
        "/market\n\n"
        "ADMIN:\n"
        "/alarmon\n"
        "/alarmoff\n"
        "/set 7\n"
        "/mode pump|dump|both\n\n"
        "USER:\n"
        "/myalarm BTCUSDT 3\n\n"
        "Sembol yazarak detay alabilirsiniz."
    )
    await update.message.reply_text(text)

# =====================================================
# ================== STATUS ===========================
# =====================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Group ayarı bulunamadı.")
        return

    active, threshold, mode = row

    text = (
        f"📊 BOT DURUMU\n\n"
        f"Alarm Aktif: {'✅' if active else '❌'}\n"
        f"Eşik: %{threshold}\n"
        f"Mod: {mode}\n"
        f"Cooldown: {COOLDOWN_MINUTES} dk"
    )

    await update.message.reply_text(text)

# =====================================================
# ================== TOP 24 ===========================
# =====================================================

async def top24(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_json(BINANCE_24H)
    sorted_data = sorted(data, key=lambda x: float(x["priceChangePercent"]), reverse=True)

    text = "🔥 TOP 10 (24s)\n\n"
    for coin in sorted_data[:10]:
        text += f"{coin['symbol']} - %{float(coin['priceChangePercent']):.2f}\n"

    await update.message.reply_text(text)

# =====================================================
# ================== TOP 5DK ==========================
# =====================================================

async def top5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = []

    data = await fetch_json(BINANCE_24H)

    for coin in data[:100]:
        symbol = coin["symbol"]
        if not symbol.endswith("USDT"):
            continue
        change = await get_change(symbol, "5m")
        results.append((symbol, change))

    results.sort(key=lambda x: x[1], reverse=True)

    text = "⚡ TOP 10 (5dk)\n\n"
    for sym, ch in results[:10]:
        text += f"{sym} - %{ch}\n"

    await update.message.reply_text(text)

# =====================================================
# ================== MARKET ===========================
# =====================================================

async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_json(BINANCE_24H)

    btc = next(x for x in data if x["symbol"] == "BTCUSDT")
    eth = next(x for x in data if x["symbol"] == "ETHUSDT")

    text = (
        "📈 MARKET ÖZETİ\n\n"
        f"BTC: %{float(btc['priceChangePercent']):.2f}\n"
        f"ETH: %{float(eth['priceChangePercent']):.2f}"
    )

    await update.message.reply_text(text)

# =====================================================
# ================== SYMBOL DETAY =====================
# =====================================================

async def reply_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    symbol = update.message.text.upper().strip()

    if not symbol.endswith("USDT"):
        return

    try:
        data = await fetch_json(f"{BINANCE_24H}?symbol={symbol}")

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])
        ch4h = await get_change(symbol, "4h")
        ch1h = await get_change(symbol, "1h")
        ch15m = await get_change(symbol, "15m")

        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)

        chart = await generate_chart(symbol)

        caption = (
            f"💎 {symbol}\n\n"
            f"💰 {price}\n\n"
            f"24s: %{ch24:.2f}\n"
            f"4s: %{ch4h}\n"
            f"1s: %{ch1h}\n"
            f"15dk: %{ch15m}\n\n"
            f"RSI7: {rsi7}\n"
            f"RSI14: {rsi14}"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📊 Binance Grafik",
                url=f"https://www.binance.com/en/trade/{symbol}"
            )]
        ])

        if chart:
            await update.message.reply_photo(
                photo=InputFile(chart),
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(caption)

    except Exception as e:
        logging.error(e)

# =====================================================
# ================== WEBSOCKET ========================
# =====================================================

async def price_stream(app):
    while True:
        try:
            async with websockets.connect(BINANCE_PRICE) as ws:
                async for message in ws:
                    data = json.loads(message)

                    for coin in data:
                        symbol = coin["s"]
                        price = float(coin["c"])

                        price_memory[symbol].append(price)

                        if len(price_memory[symbol]) > 5:
                            price_memory[symbol].pop(0)

        except:
            await asyncio.sleep(5)

# =====================================================
# ================== ALARM JOB ========================
# =====================================================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    if GROUP_CHAT_ID == 0:
        return

    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()

    if not row:
        return

    active, threshold, mode = row

    if not active:
        return

    now = datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue

        change = ((prices[-1] - prices[0]) / prices[0]) * 100

        if abs(change) >= threshold:

            if symbol in cooldowns and now < cooldowns[symbol]:
                continue

            if mode == "pump" and change < 0:
                continue
            if mode == "dump" and change > 0:
                continue

            text = f"🚨 ALARM {symbol}\nDeğişim: %{round(change,2)}"

            await context.bot.send_message(GROUP_CHAT_ID, text)

            cooldowns[symbol] = now + timedelta(minutes=COOLDOWN_MINUTES)

# =====================================================
# ================== MAIN =============================
# =====================================================

async def post_init(application):
    application.create_task(price_stream(application))

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.run_polling()
