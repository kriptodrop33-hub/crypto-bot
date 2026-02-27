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

# ================= YARDIMCI ANALİZ FONKSİYONLARI =================

async def get_price_change(symbol, interval, limit=2):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}", timeout=5) as resp:
                data = await resp.json()
                if not data or len(data) < 2: return 0.0
                first_close = float(data[0][4])
                last_close = float(data[-1][4])
                return round(((last_close - first_close) / first_close) * 100, 2)
    except:
        return 0.0

async def calculate_rsi(symbol, period=14, interval="1h", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}", timeout=5) as resp:
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

# ================= ANA GÖNDERİM MERKEZİ =================

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=5) as resp:
                data = await resp.json()
        
        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])
        ch4h = await get_price_change(symbol, "4h")
        ch1h = await get_price_change(symbol, "1h")
        ch5m = await get_price_change(symbol, "5m")
        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)

        chart_url = f"https://s3.tradingview.com/snapshots/c/{symbol.lower()}.png"

        text = (
            f"🚨 *{extra_title}*\n\n"
            f"🔥 **Sembol:** `#{symbol}`\n"
            f"💰 **Fiyat:** `{price}`\n\n"
            f"📊 **Değişimler:**\n"
            f"• 5 Dak:   `% {ch5m}`\n"
            f"• 1 Saat:  `% {ch1h}`\n"
            f"• 4 Saat:  `% {ch4h}`\n"
            f"• 24 Saat: `% {ch24}`\n\n"
            f"📉 **RSI (7/14):** `{rsi7}` / `{rsi14}`\n"
        )
        
        if threshold_info:
            text += f"🎯 **Alarm Eşiği:** `% {threshold_info}`\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Binance Grafiği", url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT', '_USDT')}")]
        ])

        # Önce fotoğrafı gönderiyoruz, caption olarak metni ekliyoruz
        await bot.send_photo(
            chat_id=chat_id,
            photo=chart_url,
            caption=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Gönderim hatası ({symbol}): {e}")

# ================= KOMUTLAR =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market", callback_data="market"), InlineKeyboardButton("📈 Top 24s", callback_data="top24")],
        [InlineKeyboardButton("⚡ Top 5dk", callback_data="top5"), InlineKeyboardButton("ℹ️ Durum", callback_data="status")],
        [InlineKeyboardButton("🛠 Yardım / Admin", callback_data="admin_help")]
    ])
    welcome_text = "👋 **Kripto Analiz Botu**\n\n#BTCUSDT gibi sembol yazın veya menüyü kullanın."
    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

async def admin_help(update: Update, context):
    text = "⚙️ **Admin Komutları**\n\n`/alarmon`, `/alarmoff`, `/set 5`, `/mode pump|dump|both`"
    if update.callback_query: await update.callback_query.message.edit_text(text, parse_mode="Markdown")
    else: await update.message.reply_text(text, parse_mode="Markdown")

# --- Eski Fonksiyonlar ---
async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    await (update.callback_query.message if update.callback_query else update.effective_message).reply_text(f"📊 Market Ortalama: %{avg:.2f}")

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    usdt = sorted([x for x in data if x["symbol"].endswith("USDT")], key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "📊 **24 Saat Top 10**\n\n" + "\n".join([f"`{c['symbol']}` → %{float(c['priceChangePercent']):.2f}" for c in usdt])
    await (update.callback_query.message if update.callback_query else update.effective_message).reply_text(text, parse_mode="Markdown")

async def top5(update: Update, context):
    changes = []
    for s, p in price_memory.items():
        if len(p) >= 2: changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))
    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]
    if not top: text = "Veri bekleniyor..."
    else: text = "⚡ **5 Dakika Top 10**\n\n" + "\n".join([f"`{s}` → %{c:.2f}" for s, c in top])
    await (update.callback_query.message if update.callback_query else update.effective_message).reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    r = cursor.fetchone()
    text = f"📢 **Sistem Durumu**\n\nAlarm: `{'AÇIK' if r[0] else 'KAPALI'}`\nEşik: `%{r[1]}`\nMod: `{r[2]}`"
    await (update.callback_query.message if update.callback_query else update.effective_message).reply_text(text, parse_mode="Markdown")

async def alarm_on(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?"); conn.commit()
    await update.message.reply_text("✅ Alarm Açıldı")

async def alarm_off(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?"); conn.commit()
    await update.message.reply_text("❌ Alarm Kapandı")

async def set_threshold(update: Update, context):
    try:
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?", (float(context.args[0]), GROUP_CHAT_ID)); conn.commit()
        await update.message.reply_text(f"🎯 Eşik: %{context.args[0]}")
    except: await update.message.reply_text("Hata! Örn: /set 5")

async def myalarm(update: Update, context):
    try:
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)", (update.effective_user.id, context.args[0].upper(), float(context.args[1]))); conn.commit()
        await update.message.reply_text(f"🎯 {context.args[0]} alarmı kuruldu.")
    except: await update.message.reply_text("Örn: /myalarm BTCUSDT 3")

# ================= REACTION FIX (TEPKİ SORUNU ÇÖZÜMÜ) =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text: return
    
    # Mesajı temizle ve büyük harfe çevir
    raw_text = update.message.text.upper().strip()
    
    # Sadece parite gibi görünen metinlere odaklan (Örn: BTCUSDT veya /BTCUSDT)
    symbol = raw_text.replace("/", "") 
    
    if symbol.endswith("USDT"):
        logging.info(f"Sembol sorgusu alındı: {symbol}")
        await send_full_analysis(context.bot, update.effective_chat.id, symbol, "🔍 ANALİZ SONUCU")

async def button_handler(update: Update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "market": await market(update, context)
    elif q.data == "top24": await top24(update, context)
    elif q.data == "top5": await top5(update, context)
    elif q.data == "status": await status(update, context)
    elif q.data == "admin_help": await admin_help(update, context)

# ================= ALARM JOB & ENGINE =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    if not row or row[0] == 0: return
    threshold, mode, now = row[1], row[2], datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2: continue
        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
        if (mode == "pump" and ch5 < 0) or (mode == "dump" and ch5 > 0): continue
        if abs(ch5) >= threshold:
            if symbol in cooldowns and now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES): continue
            cooldowns[symbol] = now
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, "🚀 HAREKETLİLİK UYARISI", threshold_info=threshold)

async def binance_engine():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    now = datetime.utcnow()
                    for c in data:
                        s = c["s"]
                        if s.endswith("USDT"):
                            price_memory[s].append((now, float(c["c"])))
                            price_memory[s] = [(t, p) for (t, p) in price_memory[s] if now - t <= timedelta(minutes=5)]
        except: await asyncio.sleep(5)

async def post_init(app):
    asyncio.create_task(binance_engine())

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("myalarm", myalarm))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    # Filtreyi daha geniş tutarak sembolleri yakalamayı garantiliyoruz
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND | filters.ALL), reply_symbol))

    print("🚀 BOT AKTİF - TEPKİ SORUNU ÇÖZÜLDÜ")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
