import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ================= CONFIG =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_ID"))

BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COOLDOWN_MINUTES = 15
DEFAULT_THRESHOLD = 5
DEFAULT_MODE = "both"

logging.basicConfig(level=logging.INFO)

# ================= DATABASE =================

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

cursor.execute(
    "INSERT OR IGNORE INTO groups (chat_id, threshold, mode) VALUES (?, ?, ?)",
    (GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE),
)
conn.commit()

# ================= MEMORY =================

price_memory = defaultdict(list)
cooldowns = {}

# ================= MINI GRAPH =================

def generate_mini_chart(prices):
    if len(prices) < 2:
        return ""

    blocks = "▁▂▃▄▅▆▇█"
    min_p = min(prices)
    max_p = max(prices)

    if max_p - min_p == 0:
        return "▁" * len(prices)

    chart = ""
    for p in prices:
        level = int((p - min_p) / (max_p - min_p) * (len(blocks) - 1))
        chart += blocks[level]

    return chart


async def get_mini_chart(symbol):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval=1m&limit=20"
            ) as resp:
                data = await resp.json()

        closes = [float(x[4]) for x in data]
        return generate_mini_chart(closes)

    except:
        return ""

# ================= RSI =================

async def calculate_rsi(symbol, period=14, interval="1m", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
            ) as resp:
                data = await resp.json()

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

# ================= HELP =================

async def help_command(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Top 24", callback_data="top24")],
        [InlineKeyboardButton("⚡ Top 5dk", callback_data="top5")],
        [InlineKeyboardButton("📈 Market", callback_data="market")],
        [InlineKeyboardButton("ℹ️ Status", callback_data="status")]
    ])

    text = (
        "🚀 KRİPTO ALARM BOTU\n\n"
        "/start\n/help\n/top24\n/top5\n/market\n/status\n\n"
        "ADMIN:\n/alarmon\n/alarmoff\n/set 7\n/mode pump|dump|both\n\n"
        "KULLANICI:\n/myalarm BTCUSDT 3"
    )

    await update.effective_message.reply_text(text, reply_markup=keyboard)

async def start(update: Update, context):
    await help_command(update, context)

# ================= SYMBOL =================

async def reply_symbol(update: Update, context):
    if not update.message:
        return

    symbol = update.message.text.upper().strip()

    if not symbol.endswith("USDT"):
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                data = await resp.json()

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])

        chart = await get_mini_chart(symbol)

        await update.message.reply_text(
            f"💎 {symbol}\n"
            f"Fiyat: {price}\n"
            f"24s: %{ch24:.2f}\n\n"
            f"📊 {chart}"
        )

    except:
        pass

# ================= ALARM JOB =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?",
        (GROUP_CHAT_ID,)
    )
    row = cursor.fetchone()

    if not row or row[0] == 0:
        return

    threshold = row[1]
    mode = row[2]
    now = datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue

        old = prices[0][1]
        new = prices[-1][1]
        change5 = ((new - old) / old) * 100

        if mode == "pump" and change5 < 0:
            continue
        if mode == "dump" and change5 > 0:
            continue

        if abs(change5) >= threshold:

            if symbol in cooldowns:
                if now - cooldowns[symbol] < timedelta(minutes=15):
                    continue

            cooldowns[symbol] = now

            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                    data = await resp.json()

            price = float(data["lastPrice"])
            change24 = float(data["priceChangePercent"])

            rsi7 = await calculate_rsi(symbol, 7)
            rsi14 = await calculate_rsi(symbol, 14)
            chart = await get_mini_chart(symbol)

            trend = "🚀 YÜKSELİŞ" if change5 > 0 else "🔻 DÜŞÜŞ"

            text = (
                f"{trend} ALARMI\n\n"
                f"💎 {symbol}\n"
                f"💰 Fiyat: {price}\n\n"
                f"⚡ 5dk: %{change5:.2f}\n"
                f"📊 24s: %{change24:.2f}\n\n"
                f"📈 RSI(7): {rsi7}\n"
                f"📉 RSI(14): {rsi14}\n\n"
                f"📊 {chart}\n\n"
                f"🎯 Eşik: %{threshold}"
            )

            await context.bot.send_message(GROUP_CHAT_ID, text)

# ================= WEBSOCKET =================

async def binance_engine():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

    while True:
        try:
            async with websockets.connect(uri) as ws:
                async for message in ws:
                    data = json.loads(message)
                    now = datetime.utcnow()

                    for coin in data:
                        symbol = coin["s"]

                        if not symbol.endswith("USDT"):
                            continue

                        price = float(coin["c"])

                        price_memory[symbol].append((now, price))

                        price_memory[symbol] = [
                            (t, p)
                            for (t, p) in price_memory[symbol]
                            if now - t <= timedelta(minutes=5)
                        ]

        except:
            await asyncio.sleep(5)

# ================= MAIN =================

async def post_init(app):
    asyncio.create_task(binance_engine())

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT TAM AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
