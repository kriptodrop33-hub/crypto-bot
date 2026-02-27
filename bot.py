import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
import matplotlib.pyplot as plt

from io import BytesIO
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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

conn.commit()

cursor.execute(
    "INSERT OR IGNORE INTO groups (chat_id, threshold, mode) VALUES (?, ?, ?)",
    (GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE),
)
conn.commit()

# ================= MEMORY =================

price_memory = defaultdict(list)
cooldowns = {}

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

# ================= CANDLE CHART =================

async def generate_candle_chart(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=50"
        ) as resp:
            data = await resp.json()

    opens = [float(x[1]) for x in data]
    closes = [float(x[4]) for x in data]

    fig, ax = plt.subplots(figsize=(10, 5))

    for i in range(len(opens)):
        color = "green" if closes[i] >= opens[i] else "red"
        ax.plot([i, i], [opens[i], closes[i]], color=color, linewidth=6)

    ax.set_title(f"{symbol} 4H Candle")
    ax.grid(True)

    buffer = BytesIO()
    plt.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close()

    return buffer

# ================= CHANGE DATA =================

async def get_multi_timeframe_change(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
            data = await resp.json()

    change24 = float(data["priceChangePercent"])

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval=1h&limit=5"
        ) as resp:
            klines = await resp.json()

    change1h = ((float(klines[-1][4]) - float(klines[-2][4])) / float(klines[-2][4])) * 100
    change4h = ((float(klines[-1][4]) - float(klines[-5][4])) / float(klines[-5][4])) * 100

    return round(change24,2), round(change4h,2), round(change1h,2)

# ================= SYMBOL DETAIL =================

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

        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)

        ch24, ch4, ch1 = await get_multi_timeframe_change(symbol)

        chart = await generate_candle_chart(symbol)

        text = (
            f"💎 {symbol}\n\n"
            f"💰 Fiyat: {price}\n\n"
            f"📊 24s: %{ch24}\n"
            f"📈 4s: %{ch4}\n"
            f"⏱ 1s: %{ch1}\n\n"
            f"📉 RSI(7): {rsi7}\n"
            f"📉 RSI(14): {rsi14}"
        )

        await update.message.reply_photo(photo=chart, caption=text)

    except:
        pass

# ================= TOP24 =================

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]

    text = "🔥 24 Saat Top 10\n\n"

    for c in top:
        text += f"{c['symbol']} → %{float(c['priceChangePercent']):.2f}\n"

    await update.message.reply_text(text)

# ================= TOP5 =================

async def top5(update: Update, context):
    changes = []

    for symbol, prices in price_memory.items():
        if len(prices) >= 2:
            old = prices[0][1]
            new = prices[-1][1]
            ch = ((new - old) / old) * 100
            changes.append((symbol, ch))

    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]

    if not top:
        await update.message.reply_text("Henüz 5dk veri yok.")
        return

    text = "⚡ 5 Dakika Top 10\n\n"
    for sym, ch in top:
        text += f"{sym} → %{ch:.2f}\n"

    await update.message.reply_text(text)

# ================= ALARM =================

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
                if now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                    continue

            cooldowns[symbol] = now

            rsi7 = await calculate_rsi(symbol, 7)
            rsi14 = await calculate_rsi(symbol, 14)
            ch24, ch4, ch1 = await get_multi_timeframe_change(symbol)
            chart = await generate_candle_chart(symbol)

            trend = "🚀 PUMP" if change5 > 0 else "🔻 DUMP"

            text = (
                f"{trend} ALARMI\n\n"
                f"💎 {symbol}\n"
                f"⚡ 5dk: %{change5:.2f}\n"
                f"📊 24s: %{ch24}\n"
                f"📈 4s: %{ch4}\n"
                f"⏱ 1s: %{ch1}\n\n"
                f"📉 RSI7: {rsi7}\n"
                f"📉 RSI14: {rsi14}\n\n"
                f"🎯 Eşik: %{threshold}"
            )

            await context.bot.send_photo(
                chat_id=GROUP_CHAT_ID,
                photo=chart,
                caption=text
            )

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

    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT TAM AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
