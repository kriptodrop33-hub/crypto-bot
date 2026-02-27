import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
import io
import matplotlib.pyplot as plt

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

# ================= DATABASE =================A

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

# ================= YARDIMCI =================

def get_number_emoji(n):
    emojis = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}
    return emojis.get(n,str(n))

def format_price(price):
    if price >= 1:
        return f"{price:,.2f}"
    else:
        return f"{price:.8g}"

# ================= GRAFİK =================

async def generate_4h_chart(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=50"
        ) as resp:
            data = await resp.json()

    closes = [float(x[4]) for x in data]
    times = [datetime.fromtimestamp(x[0]/1000) for x in data]

    plt.figure(figsize=(10,5))
    plt.plot(times, closes)
    plt.title(f"{symbol} - 4 Saatlik Mum Kapanış")
    plt.xticks(rotation=45)
    plt.tight_layout()

    buffer = io.BytesIO()
    plt.savefig(buffer, format="png")
    plt.close()
    buffer.seek(0)

    return buffer

# ================= ANALİZ =================

async def get_price_change(symbol, interval, limit=2):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}",
                timeout=5
            ) as resp:
                data = await resp.json()
                if not data or len(data) < 2:
                    return 0.0
                first_close = float(data[0][4])
                last_close = float(data[-1][4])
                return round(((last_close-first_close)/first_close)*100,2)
    except:
        return 0.0

async def calculate_rsi(symbol, period=14, interval="1h", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
            ) as resp:
                data = await resp.json()

        closes=[float(x[4]) for x in data]
        gains,losses=[],[]

        for i in range(1,len(closes)):
            diff=closes[i]-closes[i-1]
            gains.append(max(diff,0))
            losses.append(abs(min(diff,0)))

        avg_gain=sum(gains[-period:])/period
        avg_loss=sum(losses[-period:])/period

        if avg_loss==0:
            return 100

        rs=avg_gain/avg_loss
        return round(100-(100/(1+rs)),2)
    except:
        return 0

# ================= ANALİZ GÖNDER =================

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                data=await resp.json()

        if "lastPrice" not in data:
            return

        price=float(data["lastPrice"])
        ch24=float(data["priceChangePercent"])
        ch4h=await get_price_change(symbol,"4h")
        ch1h=await get_price_change(symbol,"1h")
        ch5m=await get_price_change(symbol,"5m")
        rsi7=await calculate_rsi(symbol,7)
        rsi14=await calculate_rsi(symbol,14)

        text=(
            f"💎 {symbol}\n\n"
            f"💰 Fiyat: {format_price(price)} USDT\n\n"
            f"⏱ 1s: %{ch1h:+.2f}\n"
            f"⏱ 4s: %{ch4h:+.2f}\n"
            f"📊 24s: %{ch24:+.2f}\n\n"
            f"📈 RSI(7): {rsi7}\n"
            f"📉 RSI(14): {rsi14}"
        )

        chart=await generate_4h_chart(symbol)

        await bot.send_photo(
            chat_id=chat_id,
            photo=chart,
            caption=text
        )

    except Exception as e:
        logging.error(e)

# ================= SEMBOL =================

async def reply_symbol(update:Update,context):
    if not update.message:
        return

    symbol=update.message.text.upper().strip().replace("#","").replace("/","")

    if symbol.endswith("USDT"):
        await send_full_analysis(context.bot,update.effective_chat.id,symbol,"PİYASA ANALİZ")

# ================= KOMUTLAR =================

async def start(update:Update,context):
    await update.message.reply_text("Bot Aktif")

async def top24(update:Update,context):
    await update.message.reply_text("Top24 çalışıyor")

async def top5(update:Update,context):
    await update.message.reply_text("Top5 çalışıyor")

async def market(update:Update,context):
    await update.message.reply_text("Market çalışıyor")

async def status(update:Update,context):
    await update.message.reply_text("Status çalışıyor")

async def button_handler(update:Update,context):
    await update.callback_query.answer()

# ================= ALARM =================

async def alarm_job(context:ContextTypes.DEFAULT_TYPE):
    now=datetime.utcnow()

    for symbol,prices in price_memory.items():
        if len(prices)<2:
            continue

        ch5=((prices[-1][1]-prices[0][1])/prices[0][1])*100

        if abs(ch5)>=DEFAULT_THRESHOLD:
            if symbol in cooldowns and now-cooldowns[symbol]<timedelta(minutes=COOLDOWN_MINUTES):
                continue

            cooldowns[symbol]=now

            await send_full_analysis(
                context.bot,
                GROUP_CHAT_ID,
                symbol,
                "🚀 ANLIK SİNYAL",
                DEFAULT_THRESHOLD
            )

# ================= WEBSOCKET =================

async def binance_engine():
    uri="wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                async for msg in ws:
                    data=json.loads(msg)
                    now=datetime.utcnow()
                    for c in data:
                        s=c["s"]
                        if s.endswith("USDT"):
                            price_memory[s].append((now,float(c["c"])))
                            price_memory[s]=[
                                (t,p) for (t,p) in price_memory[s]
                                if now-t<=timedelta(minutes=5)
                            ]
        except:
            await asyncio.sleep(5)

async def post_init(app):
    asyncio.create_task(binance_engine())

# ================= MAIN =================

def main():
    app=ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job,interval=60,first=30)

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("top24",top24))
    app.add_handler(CommandHandler("top5",top5))
    app.add_handler(CommandHandler("market",market))
    app.add_handler(CommandHandler("status",status))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,reply_symbol))

    print("🚀 BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
