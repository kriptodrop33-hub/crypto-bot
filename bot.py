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

logging.basicConfig(level=logging.INFO)

# ================= DATABASE =================

conn = sqlite3.connect("groups.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, alarm_active INTEGER DEFAULT 1, threshold REAL DEFAULT 5, mode TEXT DEFAULT 'both')")
cursor.execute("CREATE TABLE IF NOT EXISTS user_alarms (user_id INTEGER, symbol TEXT, threshold REAL)")
conn.commit()

cursor.execute("INSERT OR IGNORE INTO groups (chat_id, threshold, mode) VALUES (?, ?, ?)", (GROUP_CHAT_ID, DEFAULT_THRESHOLD, "both"))
conn.commit()

price_memory = defaultdict(list)
cooldowns = {}

# ================= YARDIMCI GÖRSEL FONKSİYONLAR =================

def format_price(price):
    """Fiyatı okunaklı hale getirir: 65,541.14 veya 0.00045 gibi."""
    if price >= 1:
        return f"{price:,.2f}"
    else:
        return f"{price:.8g}"

def get_trend_indicator(val):
    """Değişime göre emoji ve yön işareti döndürür."""
    if val > 0: return "🟢", "+"
    if val < 0: return "🔴", ""
    return "⚪", ""

# ================= ANALİZ FONKSİYONLARI =================

async def get_price_change(symbol, interval, limit=2):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}", timeout=5) as resp:
                data = await resp.json()
                if not data or len(data) < 2: return 0.0
                f_close, l_close = float(data[0][4]), float(data[-1][4])
                return round(((l_close - f_close) / f_close) * 100, 2)
    except: return 0.0

async def calculate_rsi(symbol, period, interval="1h", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}", timeout=5) as resp:
                data = await resp.json()
        closes = [float(x[4]) for x in data]
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [abs(min(closes[i] - closes[i-1], 0)) for i in range(1, len(closes))]
        avg_g, avg_l = sum(gains[-period:]) / period, sum(losses[-period:]) / period
        if avg_l == 0: return 100
        return round(100 - (100 / (1 + (avg_g / avg_l))), 2)
    except: return 0

# ================= ANA MESAJ MOTORU (FOTOĞRAFLI) =================

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=5) as resp:
                data = await resp.json()

        if "lastPrice" not in data: return

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])
        ch4h = await get_price_change(symbol, "4h")
        ch1h = await get_price_change(symbol, "1h")
        ch5m = await get_price_change(symbol, "5m")
        
        # RSI 100 saatlik (yaklaşık 4 gün) veri üzerinden hesaplanır
        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)

        e24, s24 = get_trend_indicator(ch24)
        e4h, s4h = get_trend_indicator(ch4h)
        e1h, s1h = get_trend_indicator(ch1h)
        e5m, s5m = get_trend_indicator(ch5m)

        # TradingView 4 Saatlik Grafik Snapshot URL
        chart_url = f"https://s3.tradingview.com/snapshots/c/{symbol.lower()}.png"

        text = (
            f"🔍 *{extra_title}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Sembol:* `#{symbol}`\n"
            f"💰 *Fiyat:* `{format_price(price)} USDT`\n\n"
            f"*Anlık Değişim Performansı:*\n"
            f"{e5m} `5dk  :` `% {s5m}{ch5m}`\n"
            f"{e1h} `1sa  :` `% {s1h}{ch1h}`\n"
            f"{e4h} `4sa  :` `% {s4h}{ch4h}`\n"
            f"{e24} `24sa :` `% {s24}{ch24}`\n\n"
            f"📉 *RSI Analizi (Son 100 Saatlik Veri):*\n"
            f"• RSI (7)  : `{rsi7}`\n"
            f"• RSI (14) : `{rsi14}`\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        if threshold_info:
            text += f"\n🎯 *Alarm Eşiği:* `% {threshold_info}`"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Binance Detay", url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}")]])

        # Fotoğraf olarak gönder (Grafik + Açıklama)
        await bot.send_photo(
            chat_id=chat_id,
            photo=chart_url,
            caption=text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Hata: {e}")

# ================= TEPKİ & KOMUTLAR =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text: return
    raw = update.message.text.upper().strip()
    symbol = raw.replace("#", "").replace("/", "")
    if symbol.endswith("USDT"):
        await send_full_analysis(context.bot, update.effective_chat.id, symbol, "PİYASA RAPORU")

async def start(u, c):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Market", callback_data="market"), InlineKeyboardButton("📈 Top 24s", callback_data="top24")], [InlineKeyboardButton("⚡ Top 5dk", callback_data="top5"), InlineKeyboardButton("ℹ️ Durum", callback_data="status")]])
    await u.message.reply_text("👋 *Kripto Analiz Botu Aktif!*\nParite yazarak analiz alabilirsiniz. Örn: `BTCUSDT`", reply_markup=kb, parse_mode="Markdown")

async def market(update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    emoji = "🐂" if avg > 0 else "🐻"
    await (update.callback_query.message if update.callback_query else update.message).reply_text(f"{emoji} *Piyasa Ortalaması:* `%{avg:+.2f}`", parse_mode="Markdown")

async def top24(update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp: data = await resp.json()
    usdt = sorted([x for x in data if x["symbol"].endswith("USDT")], key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "🚀 *24 Saatlik Liderler*\n\n"
    for c in usdt:
        text += f"`{c['symbol']:<10}` → `%{float(c['priceChangePercent']):+.2f}`\n"
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

async def top5(update, context):
    changes = sorted([(s, ((p[-1][1]-p[0][1])/p[0][1])*100) for s, p in price_memory.items() if len(p)>=2], key=lambda x: x[1], reverse=True)[:10]
    if not changes: text = "⌛ *Veri toplanıyor...*"
    else:
        text = "⚡ *5 Dakikalık Flaş Değişimler*\n\n"
        for s, c in changes: text += f"`{s:<10}` → `%{c:+.2f}`\n"
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

async def status(update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    r = cursor.fetchone()
    text = f"⚙️ *Sistem Yapılandırması*\n━━━━━━━━━━━━━━\n🔔 *Alarm:* `{'AÇIK' if r[0] else 'KAPALI'}`\n🎯 *Eşik:* `%{r[1]}`\n🔄 *Mod:* `{r[2].upper()}`"
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

async def button_handler(update: Update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "market": await market(update, context)
    elif q.data == "top24": await top24(update, context)
    elif q.data == "top5": await top5(update, context)
    elif q.data == "status": await status(update, context)

# ================= ENGINE & ALARM =================

async def alarm_job(context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    if not row or row[0] == 0: return
    now = datetime.utcnow()
    for symbol, prices in price_memory.items():
        if len(prices) < 2: continue
        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
        if abs(ch5) >= row[1]:
            if symbol in cooldowns and now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES): continue
            cooldowns[symbol] = now
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, "🚨 HAREKETLİLİK SİNYALİ", row[1])

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

async def post_init(app): asyncio.create_task(binance_engine())

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.job_queue.run_repeating(alarm_job, interval=60)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))
    print("🚀 BOT GÖRSEL GÜNCELLEME İLE AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
