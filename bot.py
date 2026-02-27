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

# ================= ANALİZ FONKSİYONLARI =================

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
                return round(((last_close - first_close) / first_close) * 100, 2)
    except Exception as e:
        logging.error(f"Change error {symbol}: {e}")
        return 0.0


async def calculate_rsi(symbol, period=14, interval="1h", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}",
                timeout=5
            ) as resp:
                data = await resp.json()

        closes = [float(x[4]) for x in data]
        gains, losses = [], []

        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except Exception as e:
        logging.error(f"RSI error {symbol}: {e}")
        return 0

# ================= ANALİZ GÖNDER =================

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=5) as resp:
                data = await resp.json()

        if "lastPrice" not in data:
            return

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])
        ch4h = await get_price_change(symbol, "4h")
        ch1h = await get_price_change(symbol, "1h")
        ch5m = await get_price_change(symbol, "5m")
        rsi7 = await calculate_rsi(symbol, 7)
        rsi14 = await calculate_rsi(symbol, 14)
        
        # Değişimlere göre emoji seçimi
        def get_trend_emoji(val):
            return "🟢" if val > 0 else "🔴" if val < 0 else "⚪"

        text = (
            f"📊 *{extra_title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Parite:* `#{symbol}`\n"
            f"💵 *Fiyat:* `{price:,.4f}`\n\n"
            f"*Performans Değişimleri:*\n"
            f"{get_trend_emoji(ch5m)} `5dk :` `% {ch5m:+.2f}`\n"
            f"{get_trend_emoji(ch1h)} `1sa :` `% {ch1h:+.2f}`\n"
            f"{get_trend_emoji(ch4h)} `4sa :` `% {ch4h:+.2f}`\n"
            f"{get_trend_emoji(ch24)} `24sa:` `% {ch24:+.2f}`\n\n"
            f"📉 *RSI Göstergeleri:*\n"
            f"• RSI 7  : `{rsi7}`\n"
            f"• RSI 14 : `{rsi14}`\n"
            f"──────────────────"
        )

        if threshold_info:
            text += f"\n🎯 *Alarm Eşiği:* `% {threshold_info}`"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📈 Binance'de Görüntüle",
                url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
            )]
        ])

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Gönderim hatası ({symbol}): {e}")

# ================= SEMBOL TEPKİ =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return

    raw = update.message.text.upper().strip()
    symbol = raw.replace("#", "").replace("/", "")

    if symbol.endswith("USDT"):
        await send_full_analysis(
            context.bot,
            update.effective_chat.id,
            symbol,
            "PİYASA ANALİZ RAPORU"
        )

# ================= KOMUTLAR =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Genel Market", callback_data="market")],
        [InlineKeyboardButton("📈 24s Liderleri", callback_data="top24"),
         InlineKeyboardButton("⚡ 5dk Flaşlar", callback_data="top5")],
        [InlineKeyboardButton("⚙️ Sistem Durumu", callback_data="status")]
    ])
    welcome_text = (
        "👋 *Kripto Analiz Asistanına Hoş Geldin!*\n\n"
        "Sanal asistanın 7/24 piyasayı takip eder. Analiz almak için parite ismini yazman yeterli.\n\n"
        "💡 *Örnek:* `BTCUSDT`"
    )
    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    
    status_emoji = "🐂" if avg > 0 else "🐻"
    msg = f"{status_emoji} *Piyasa Duyarlılığı:* `%{avg:+.2f}`"
    await (update.callback_query.message if update.callback_query else update.message).reply_text(msg, parse_mode="Markdown")

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
    usdt = sorted([x for x in data if x["symbol"].endswith("USDT")],
                  key=lambda x: float(x["priceChangePercent"]),
                  reverse=True)[:10]
    
    text = "🏆 *24 Saatlik Performans Liderleri*\n"
    text += "━━━━━━━━━━━━━━━━━━━━━\n"
    for i, c in enumerate(usdt, 1):
        text += f"{i}️⃣ `{c['symbol']:<10}` → `%{float(c['priceChangePercent']):+6.2f}`\n"
    
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

async def top5(update: Update, context):
    changes = []
    for s, p in price_memory.items():
        if len(p) >= 2:
            changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))
    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]
    
    if not top:
        text = "⏳ *Veri toplanıyor, lütfen bekleyin...*"
    else:
        text = "⚡ *Son 5 Dakikanın En Hareketlileri*\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n"
        for i, (s, c) in enumerate(top, 1):
            text += f"{i}️⃣ `{s:<10}` → `%{c:+6.2f}`\n"
            
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    r = cursor.fetchone()
    text = (
        "ℹ️ *Sistem Yapılandırması*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTİF' if r[0] else 'KAPALI'}`\n"
        f"🎯 *Eşik Değeri:* `% {r[1]}`\n"
        f"🔄 *İzleme Modu:* `{r[2].upper()}`"
    )
    await (update.callback_query.message if update.callback_query else update.message).reply_text(text, parse_mode="Markdown")

# ================= CALLBACK =================

async def button_handler(update: Update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "market":
        await market(update, context)
    elif q.data == "top24":
        await top24(update, context)
    elif q.data == "top5":
        await top5(update, context)
    elif q.data == "status":
        await status(update, context)

# ================= ALARM =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (GROUP_CHAT_ID,))
    row = cursor.fetchone()
    if not row or row[0] == 0:
        return

    threshold, mode = row[1], row[2]
    now = datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue

        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100

        if abs(ch5) >= threshold:
            if symbol in cooldowns and now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                continue
            cooldowns[symbol] = now

            await send_full_analysis(
                context.bot,
                GROUP_CHAT_ID,
                symbol,
                "🚀 ANLIK SİNYAL UYARISI",
                threshold
            )

# ================= WEBSOCKET =================

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
                            price_memory[s] = [
                                (t, p) for (t, p) in price_memory[s]
                                if now - t <= timedelta(minutes=5)
                            ]
        except:
            await asyncio.sleep(5)

async def post_init(app):
    asyncio.create_task(binance_engine())

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(CallbackQueryHandler(button_handler))

    # 🔥 SENİN DÜZELTTİĞİN FİLTRE (KORUNDU)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT GÖRSEL GÜNCELLEME İLE AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
