import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
import io
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from datetime import datetime, timedelta
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
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

# ================= YARDIMCI =================

def get_number_emoji(n):
    emojis = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",
              7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}
    return emojis.get(n,str(n))

def format_price(price):
    return f"{price:,.2f}" if price >= 1 else f"{price:.8g}"

# ================= 4H GRAFİK =================

async def generate_4h_chart(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=60"
        ) as resp:
            data = await resp.json()

    fig, ax = plt.subplots(figsize=(10,5))

    for candle in data:
        t = datetime.fromtimestamp(candle[0]/1000)
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])

        color = "green" if c >= o else "red"
        ax.plot([t,t],[l,h],color=color)
        ax.plot([t,t],[o,c],linewidth=6,color=color)

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%m'))
    ax.set_title(f"{symbol} 4H Mum Grafiği")
    ax.grid(True)

    buf = io.BytesIO()
    plt.savefig(buf,format="png")
    plt.close(fig)
    buf.seek(0)
    return buf

# ================= ANALİZ =================

async def get_price_change(symbol, interval):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit=2"
            ) as resp:
                data = await resp.json()
        if len(data)<2:
            return 0
        old=float(data[0][4])
        new=float(data[-1][4])
        return round(((new-old)/old)*100,2)
    except:
        return 0

async def calculate_rsi(symbol,period=14):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval=1h&limit=100"
            ) as resp:
                data=await resp.json()

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

async def send_full_analysis(bot,chat_id,symbol,title,threshold=None):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
            data=await resp.json()

    if "lastPrice" not in data:
        return

    price=float(data["lastPrice"])
    ch24=float(data["priceChangePercent"])
    ch5=await get_price_change(symbol,"5m")
    ch1=await get_price_change(symbol,"1h")
    ch4=await get_price_change(symbol,"4h")
    rsi7=await calculate_rsi(symbol,7)
    rsi14=await calculate_rsi(symbol,14)

    text=(
        f"📊 *{title}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Parite:* `#{symbol}`\n"
        f"💵 *Fiyat:* `{format_price(price)} USDT`\n\n"
        f"5dk: `%{ch5:+.2f}`\n"
        f"1sa: `%{ch1:+.2f}`\n"
        f"4sa: `%{ch4:+.2f}`\n"
        f"24s: `%{ch24:+.2f}`\n\n"
        f"RSI7: `{rsi7}` | RSI14: `{rsi14}`"
    )

    if threshold:
        text+=f"\n\n🎯 Alarm Eşiği: `%{threshold}`"

    chart=await generate_4h_chart(symbol)

    await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(chart),
        caption=text,
        parse_mode="Markdown"
    )

# ================= SEMBOL =================

async def reply_symbol(update:Update,context):
    if not update.message:
        return
    raw=update.message.text.upper().strip()
    symbol=raw.replace("#","").replace("/","")
    if symbol.endswith("USDT"):
        await send_full_analysis(
            context.bot,
            update.effective_chat.id,
            symbol,
            "PİYASA ANALİZ RAPORU"
        )

# ================= /SET KOMUTU =================

async def set_threshold(update:Update,context):
    if not context.args:
        await update.message.reply_text("Kullanım: /set 7")
        return
    try:
        value=float(context.args[0])
        cursor.execute(
            "UPDATE groups SET threshold=? WHERE chat_id=?",
            (value,GROUP_CHAT_ID)
        )
        conn.commit()
        await update.message.reply_text(f"🎯 Yeni alarm eşiği: %{value}")
    except:
        await update.message.reply_text("Geçersiz değer.")

# ================= ALARM =================

async def alarm_job(context:ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active,threshold FROM groups WHERE chat_id=?",(GROUP_CHAT_ID,))
    row=cursor.fetchone()
    if not row or row[0]==0:
        return
    threshold=row[1]
    now=datetime.utcnow()

    for s,p in price_memory.items():
        if len(p)<2:
            continue
        ch=((p[-1][1]-p[0][1])/p[0][1])*100
        if abs(ch)>=threshold:
            if s in cooldowns and now-cooldowns[s]<timedelta(minutes=COOLDOWN_MINUTES):
                continue
            cooldowns[s]=now
            await send_full_analysis(
                context.bot,
                GROUP_CHAT_ID,
                s,
                "🚀 ANLIK SİNYAL",
                threshold
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

    app.add_handler(CommandHandler("set",set_threshold))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,reply_symbol))

    print("🚀 BOT FULL AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
