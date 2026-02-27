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

# ================= YENİ ÖZELLİK FONKSİYONLARI =================

async def get_price_change(symbol, interval, limit=2):
    """Belirlenen zaman dilimine göre fiyat değişim yüzdesini hesaplar."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}") as resp:
                data = await resp.json()
                if not data or len(data) < 2: return 0.0
                first_close = float(data[0][4])
                last_close = float(data[-1][4])
                change = ((last_close - first_close) / first_close) * 100
                return round(change, 2)
    except:
        return 0.0

async def calculate_rsi(symbol, period=14, interval="1h", limit=100):
    """RSI değerini hesaplar."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}") as resp:
                data = await resp.json()
        closes = [float(x[4]) for x in data]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0: return 100
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return 0

def get_binance_markup(symbol):
    """Binance butonu oluşturur."""
    url = f"https://www.binance.com/tr/trade/{symbol.replace('USDT', '_USDT')}"
    keyboard = [[InlineKeyboardButton("📊 Binance Grafiği", url=url)]]
    return InlineKeyboardMarkup(keyboard)

async def format_and_send(bot, chat_id, symbol, extra_title=""):
    """Detaylı coin bilgisini hazırlar ve gönderir."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
            data = await resp.json()
    
    price = float(data["lastPrice"])
    ch24 = float(data["priceChangePercent"])
    ch4h = await get_price_change(symbol, "4h")
    ch1h = await get_price_change(symbol, "1h")
    ch5m = await get_price_change(symbol, "5m")
    
    rsi7 = await calculate_rsi(symbol, 7)
    rsi14 = await calculate_rsi(symbol, 14)
    
    text = (
        f"{extra_title}\n\n"
        f"💎 **{symbol}**\n"
        f"💰 Fiyat: `{price}`\n\n"
        f"⌛ **Fiyat Değişimleri:**\n"
        f"• 24 Saat: %{ch24}\n"
        f"• 4 Saat:  %{ch4h}\n"
        f"• 1 Saat:  %{ch1h}\n"
        f"• 5 Dak:   %{ch5m}\n\n"
        f"📈 **RSI Verileri:**\n"
        f"• RSI(7):  {rsi7}\n"
        f"• RSI(14): {rsi14}\n\n"
        f"🖼 **4 Saatlik Mum Grafiği:**\n"
        f"[Grafiği Gör](https://s3.tradingview.com/snapshots/c/{symbol.lower()}.png)"
    )
    
    await bot.send_message(
        chat_id, 
        text, 
        reply_markup=get_binance_markup(symbol),
        parse_mode="Markdown",
        disable_web_page_preview=False
    )

# ================= ESKİ İŞLEVLER (KORUNDU) =================

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    await update.effective_message.reply_text(f"📊 Market Ortalama: %{avg:.2f}")

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "📊 24 Saat Top 10\n\n"
    for c in top: text += f"{c['symbol']} → %{float(c['priceChangePercent']):.2f}\n"
    await update.effective_message.reply_text(text)

async def top5(update: Update, context):
    changes = []
    for symbol, prices in price_memory.items():
        if len(prices) >= 2:
            old, new = prices[0][1], prices[-1][1]
            ch = ((new - old) / old) * 100
            changes.append((symbol, ch))
    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]
    if not top:
        await update.effective_message.reply_text("Henüz 5dk veri birikmedi.")
        return
    text = "⚡ 5 Dakika Top 10\n\n"
    for sym, ch in top: text += f"{sym} → %{ch:.2f}\n"
    await update.effective_message.reply_text(text)

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    if not row: return
    await update.effective_message.reply_text(f"Alarm: {'Açık' if row[0] else 'Kapalı'}\nEşik: %{row[1]}\nMod: {row[2]}")

async def alarm_on(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.effective_message.reply_text("✅ Alarm Açıldı")

async def alarm_off(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.effective_message.reply_text("❌ Alarm Kapandı")

async def set_threshold(update: Update, context):
    try:
        value = float(context.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?", (value, GROUP_CHAT_ID))
        conn.commit()
        await update.effective_message.reply_text(f"Eşik %{value} yapıldı")
    except: await update.effective_message.reply_text("Kullanım: /set 7")

async def set_mode(update: Update, context):
    try:
        mode = context.args[0].lower()
        cursor.execute("UPDATE groups SET mode=? WHERE chat_id=?", (mode, GROUP_CHAT_ID))
        conn.commit()
        await update.effective_message.reply_text(f"Mod: {mode}")
    except: await update.effective_message.reply_text("Kullanım: /mode pump|dump|both")

async def myalarm(update: Update, context):
    try:
        symbol, threshold = context.args[0].upper(), float(context.args[1])
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)", (update.effective_user.id, symbol, threshold))
        conn.commit()
        await update.effective_message.reply_text(f"🎯 {symbol} %{threshold} alarm eklendi.")
    except: await update.effective_message.reply_text("Kullanım: /myalarm BTCUSDT 3")

async def reply_symbol(update: Update, context):
    if not update.message: return
    symbol = update.message.text.upper().strip()
    if not symbol.endswith("USDT"): return
    try:
        await format_and_send(context.bot, update.effective_chat.id, symbol, "🔍 SEMBOL SORGUSU")
    except: pass

async def button(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "top24": await top24(update, context)
    elif query.data == "top5": await top5(update, context)
    elif query.data == "market": await market(update, context)
    elif query.data == "status": await status(update, context)

# ================= ALARM JOB & ENGINE =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    if not row or row[0] == 0: return
    threshold, mode = row[1], row[2]
    now = datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2: continue
        old, new = prices[0][1], prices[-1][1]
        change5 = ((new - old) / old) * 100
        if (mode == "pump" and change5 < 0) or (mode == "dump" and change5 > 0): continue
        if abs(change5) >= threshold:
            if symbol in cooldowns and now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES): continue
            cooldowns[symbol] = now
            trend = "🚀 YÜKSELİŞ" if change5 > 0 else "🔻 DÜŞÜŞ"
            await format_and_send(context.bot, GROUP_CHAT_ID, symbol, f"{trend} ALARMI")

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
                        if not symbol.endswith("USDT"): continue
                        price = float(coin["c"])
                        price_memory[symbol].append((now, price))
                        price_memory[symbol] = [(t, p) for (t, p) in price_memory[symbol] if now - t <= timedelta(minutes=5)]
        except: await asyncio.sleep(5)

# ================= MAIN =================

async def post_init(app):
    asyncio.create_task(binance_engine())

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.add_handler(CommandHandler("start", status))
    app.add_handler(CommandHandler("help", status))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("mode", set_mode))
    app.add_handler(CommandHandler("myalarm", myalarm))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT TAM AKTİF VE TÜM ÖZELLİKLER KORUNDU")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
