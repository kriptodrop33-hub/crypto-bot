import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
import re
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

# ================= ANALİZ YARDIMCILARI =================

async def get_price_change(symbol, interval, limit=2):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}") as resp:
                data = await resp.json()
                if not data or len(data) < 2: return 0.0
                f_close = float(data[0][4])
                l_close = float(data[-1][4])
                return round(((l_close - f_close) / f_close) * 100, 2)
    except: return 0.0

async def calculate_rsi(symbol, period=14, interval="1h"):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit=100") as resp:
                data = await resp.json()
        closes = [float(x[4]) for x in data]
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [abs(min(closes[i] - closes[i-1], 0)) for i in range(1, len(closes))]
        avg_g = sum(gains[-period:]) / period
        avg_l = sum(losses[-period:]) / period
        if avg_l == 0: return 100
        return round(100 - (100 / (1 + (avg_g / avg_l))), 2)
    except: return 0

# ================= ANA GÖNDERİCİ (PNG GRAFİK + VERİLER) =================

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                data = await resp.json()
                if "lastPrice" not in data: return
        
        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])
        ch4h = await get_price_change(symbol, "4h")
        ch1h = await get_price_change(symbol, "1h")
        ch5m = await get_price_change(symbol, "5m")
        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)

        # TradingView Snapshot (PNG Görüntüsü)
        chart_url = f"https://s3.tradingview.com/snapshots/c/{symbol.lower()}.png"

        text = (
            f"🚨 *{extra_title}*\n\n"
            f"🔥 **Sembol:** `#{symbol}`\n"
            f"💰 **Fiyat:** `{price}`\n\n"
            f"📊 **Değişimler:**\n"
            f"• 5 Dak: `% {ch5m}` | 1 Saat: `% {ch1h}`\n"
            f"• 4 Saat: `% {ch4h}` | 24 Saat: `% {ch24}`\n\n"
            f"📉 **RSI (7/14):** `{rsi7}` / `{rsi14}`\n"
        )
        if threshold_info:
            text += f"🎯 **Alarm Eşiği:** `% {threshold_info}`\n"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Binance'de İşlem Yap", url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT', '_USDT')}")]])

        await bot.send_photo(chat_id=chat_id, photo=chart_url, caption=text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Analiz hatası ({symbol}): {e}")

# ================= TÜM KOMUT FONKSİYONLARI =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market", callback_data="market"), InlineKeyboardButton("📈 Top 24s", callback_data="top24")],
        [InlineKeyboardButton("⚡ Top 5dk", callback_data="top5"), InlineKeyboardButton("ℹ️ Durum", callback_data="status")],
        [InlineKeyboardButton("🛠 Admin", callback_data="admin_help")]
    ])
    await update.message.reply_text("👋 **Kripto Analiz & Alarm Botuna Hoş Geldiniz!**\n\nBir sembol yazın (Örn: `BTCUSDT`) veya aşağıdaki menüyü kullanın.", reply_markup=keyboard, parse_mode="Markdown")

async def admin_help(update: Update, context):
    text = (
        "⚙️ **Admin & Ayar Komutları**\n\n"
        "• `/alarmon` / `/alarmoff` - Alarmı Yönet\n"
        "• `/set 5` - Eşiği %5 yap\n"
        "• `/mode pump|dump|both` - Yön seçimi"
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt_pairs) / len(usdt_pairs)
    msg = f"📊 Market Ortalama Değişim: %{avg:.2f}"
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(msg)

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    top = sorted([x for x in data if x["symbol"].endswith("USDT")], key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "📊 **24 Saat Top 10**\n\n" + "\n".join([f"`{c['symbol']}` → %{float(c['priceChangePercent']):.2f}" for c in top])
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(text, parse_mode="Markdown")

async def top5(update: Update, context):
    changes = []
    for s, p in price_memory.items():
        if len(p) >= 2: changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))
    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]
    text = "⚡ **5 Dakika Top 10**\n\n" + "\n".join([f"`{s}` → %{c:.2f}" for s, c in top]) if top else "Henüz yeterli veri birikmedi..."
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    r = cursor.fetchone()
    text = f"📢 **Sistem Durumu**\n\nAlarm: `{'AÇIK' if r[0] else 'KAPALI'}`\nEşik: `%{r[1]}`\nMod: `{r[2]}`"
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(text, parse_mode="Markdown")

async def alarm_on(u, c):
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?", (GROUP_CHAT_ID,)); conn.commit()
    await u.message.reply_text("✅ Otomatik alarmlar açıldı.")

async def alarm_off(u, c):
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?", (GROUP_CHAT_ID,)); conn.commit()
    await u.message.reply_text("❌ Otomatik alarmlar kapatıldı.")

async def set_threshold(u, c):
    try:
        val = float(c.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?", (val, GROUP_CHAT_ID)); conn.commit()
        await u.message.reply_text(f"🎯 Alarm eşiği %{val} olarak güncellendi.")
    except: await u.message.reply_text("Kullanım: `/set 5`", parse_mode="Markdown")

async def set_mode(u, c):
    try:
        m = c.args[0].lower()
        cursor.execute("UPDATE groups SET mode=? WHERE chat_id=?", (m, GROUP_CHAT_ID)); conn.commit()
        await u.message.reply_text(f"🔄 Alarm modu: {m}")
    except: await u.message.reply_text("Kullanım: `/mode pump|dump|both`", parse_mode="Markdown")

async def myalarm(u, c):
    try:
        s, t = c.args[0].upper(), float(c.args[1])
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)", (u.effective_user.id, s, t)); conn.commit()
        await u.message.reply_text(f"🎯 {s} için %{t} şahsi alarmınız kuruldu.")
    except: await u.message.reply_text("Kullanım: `/myalarm BTCUSDT 3`", parse_mode="Markdown")

# ================= KESİN ÇÖZÜM: TEPKİ MOTORU =================

async def global_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.upper().strip()
    
    # regex: Mesajın herhangi bir yerinde USDT paritesi var mı bak
    match = re.search(r"(/[A-Z0-9]+USDT|[A-Z0-9]+USDT)", text)
    if match:
        symbol = match.group(0).replace("/", "")
        await send_full_analysis(context.bot, update.effective_chat.id, symbol, "🔍 ANALİZ SONUCU")

async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "market": await market(update, context)
    elif query.data == "top24": await top24(update, context)
    elif query.data == "top5": await top5(update, context)
    elif query.data == "status": await status(update, context)
    elif query.data == "admin_help": await admin_help(update, context)

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
            trend = "🚀 SERİ YÜKSELİŞ" if ch5 > 0 else "🔻 SERİ DÜŞÜŞ"
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, f"{trend} UYARISI", threshold_info=threshold)

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
                        if symbol.endswith("USDT"):
                            price_memory[symbol].append((now, float(coin["c"])))
                            price_memory[symbol] = [(t, p) for (t, p) in price_memory[symbol] if now - t <= timedelta(minutes=5)]
        except: await asyncio.sleep(5)

# ================= MAIN =================

async def post_init(app):
    asyncio.create_task(binance_engine())

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.job_queue.run_repeating(alarm_job, interval=60)

    # Komut Kayıtları
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("mode", set_mode))
    app.add_handler(CommandHandler("myalarm", myalarm))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("status", status))
    
    # Etkileşim Kayıtları
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL, global_message_handler))

    print("🚀 BOT TAM GAZ VE EKSİKSİZ ÇALIŞIYOR")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
