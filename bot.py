import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}") as resp:
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

# ================= ANA GÖNDERİM MERKEZİ =================

async def send_full_analysis(bot, chat_id, symbol, extra_title=""):
    """Fiyatlar, RSI, Grafik ve Butonu bir arada gönderir."""
    try:
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

        # Görsel Grafik (TradingView Snapshot)
        # 4 Saatlik mumu temsil eden snapshot linki
        chart_url = f"https://s3.tradingview.com/snapshots/c/{symbol.lower()}.png"

        text = (
            f"🔔 *{extra_title}*\n\n"
            f"💎 **Sembol:** #{symbol}\n"
            f"💰 **Güncel Fiyat:** `{price}`\n\n"
            f"📊 **Zaman Bazlı Değişimler:**\n"
            f"• 24 Saat: `% {ch24}`\n"
            f"• 4 Saat:  `% {ch4h}`\n"
            f"• 1 Saat:  `% {ch1h}`\n"
            f"• 5 Dak:   `% {ch5m}`\n\n"
            f"📉 **RSI Göstergeleri:**\n"
            f"• RSI (7): `{rsi7}`\n"
            f"• RSI (14): `{rsi14}`\n"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Binance'de İşlem Yap", url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT', '_USDT')}")]
        ])

        # Fotoğraf ve metni birlikte gönderiyoruz
        await bot.send_photo(
            chat_id=chat_id,
            photo=chart_url,
            caption=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Gönderim hatası: {e}")

# ================= KOMUTLAR =================

async def start(update: Update, context):
    """Zenginleştirilmiş Start Menüsü"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Genel Market", callback_data="market"), InlineKeyboardButton("📈 Top 24s", callback_data="top24")],
        [InlineKeyboardButton("⚡ Hızlı 5dk", callback_data="top5"), InlineKeyboardButton("ℹ️ Bot Durumu", callback_data="status")],
        [InlineKeyboardButton("🛠 Ayarlar / Admin", callback_data="admin_help")]
    ])
    
    welcome_text = (
        "👋 **Kripto Analiz & Alarm Botuna Hoş Geldiniz!**\n\n"
        "Bu bot Binance üzerindeki pariteleri saniyelik izler ve ani hareketlerde sizi uyarır.\n\n"
        "💡 **Neler Yapabilirim?**\n"
        "• Direkt bir coin adı yazın (Örn: `BTCUSDT`) detaylı analizini atayım.\n"
        "• Gruplarda %5 ve üzeri ani hareketleri otomatik yakalarım.\n"
        "• Teknik göstergeleri ve 4 saatlik grafikleri sunarım.\n\n"
        "👇 Menüden keşfetmeye başlayın!"
    )
    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

async def admin_help(update: Update, context):
    text = (
        "⚙️ **Admin & Kullanıcı Komutları**\n\n"
        "• `/alarmon` / `/alarmoff` - Gruba alarmı aç/kapat\n"
        "• `/set 5` - Alarm eşiğini %5 yap\n"
        "• `/mode pump` - Sadece yükselişleri bildir\n"
        "• `/myalarm BTCUSDT 2` - Şahsi alarm kur"
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

# --- Eski Fonksiyonlar (Aynen Korundu) ---
async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    msg = f"📊 Market Ortalama: %{avg:.2f}"
    if update.callback_query: await update.callback_query.message.reply_text(msg)
    else: await update.effective_message.reply_text(msg)

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "📊 **24 Saat Top 10**\n\n"
    for c in top: text += f"`{c['symbol']}` → %{float(c['priceChangePercent']):.2f}\n"
    if update.callback_query: await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else: await update.effective_message.reply_text(text, parse_mode="Markdown")

async def top5(update: Update, context):
    changes = []
    for symbol, prices in price_memory.items():
        if len(prices) >= 2:
            old, new = prices[0][1], prices[-1][1]
            ch = ((new - old) / old) * 100
            changes.append((symbol, ch))
    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]
    if not top:
        msg = "Henüz 5dk veri birikmedi."
        if update.callback_query: await update.callback_query.message.reply_text(msg)
        else: await update.effective_message.reply_text(msg)
        return
    text = "⚡ **5 Dakika Top 10**\n\n"
    for sym, ch in top: text += f"`{sym}` → %{ch:.2f}\n"
    if update.callback_query: await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else: await update.effective_message.reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    text = f"📢 **Bot Durumu**\n\nAlarm: `{'AÇIK' if row[0] else 'KAPALI'}`\nEşik: `%{row[1]}`\nMod: `{row[2]}`"
    if update.callback_query: await update.callback_query.message.reply_text(text, parse_mode="Markdown")
    else: await update.effective_message.reply_text(text, parse_mode="Markdown")

async def alarm_on(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.message.reply_text("✅ Alarm Açıldı")

async def alarm_off(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.message.reply_text("❌ Alarm Kapandı")

async def set_threshold(update: Update, context):
    try:
        val = float(context.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?", (val, GROUP_CHAT_ID))
        conn.commit()
        await update.message.reply_text(f"🎯 Eşik %{val} olarak güncellendi.")
    except: await update.message.reply_text("Kullanım: /set 5")

async def set_mode(update: Update, context):
    try:
        m = context.args[0].lower()
        cursor.execute("UPDATE groups SET mode=? WHERE chat_id=?", (m, GROUP_CHAT_ID))
        conn.commit()
        await update.message.reply_text(f"🔄 Mod: {m}")
    except: await update.message.reply_text("Kullanım: /mode pump|dump|both")

async def myalarm(update: Update, context):
    try:
        s, t = context.args[0].upper(), float(context.args[1])
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)", (update.effective_user.id, s, t))
        conn.commit()
        await update.message.reply_text(f"🎯 {s} için %{t} alarmın kuruldu.")
    except: await update.message.reply_text("Kullanım: /myalarm BTCUSDT 3")

async def reply_symbol(update: Update, context):
    if not update.message: return
    symbol = update.message.text.upper().strip()
    if not symbol.endswith("USDT"): return
    await send_full_analysis(context.bot, update.effective_chat.id, symbol, "SEMBOL SORGUSU")

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
            trend = "🚀 SERİ YÜKSELİŞ" if change5 > 0 else "🔻 SERİ DÜŞÜŞ"
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, f"{trend}")

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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("mode", set_mode))
    app.add_handler(CommandHandler("myalarm", myalarm))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT TAM AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
