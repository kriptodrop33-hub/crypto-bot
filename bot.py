
Add price change and RSI indicators
27 Şub
·
crypto-bot
·
main

Arşivle

Paylaş

PR oluştur


KODLAMA BİLGİM YOK BU PROJE RAİLWAY ÜZERİNDE ÇALIŞIYOR DEĞİŞTİRMEK İSTEDİĞİM KISIM coin sembolü yazıldığında 24 ,4 , 1 saatlik ve 5 dklık fiyat değişimi yazsın rsi 7 ,14 değerlerini yazsın alta binance grafiğine yönlendiren kutucuk eklesin ve onunda altına paritenin 4 saatlik mum grafiğini png olarak eklesin ve bunları guruptaki alarm tetiklenincede yapsın
Kopyala
Summary
Coin sembolü yazılınca artık bot; 24s, 4s, 1s, 5dk yüzde değişimleri ile birlikte RSI(7) ve RSI(14) değerlerini tek raporda hesaplayıp gönderiyor (1 dakikalık kline verisinden türetiliyor).

Rapor mesajına Binance grafiğine yönlendiren buton eklendi ve hemen ardından ilgili parite için 4 saatlik mum grafiği PNG olarak üretilip gönderiliyor.

Bu yeni içerik hem normal sembol mesajlarında hem de grupta alarm tetiklenince aynı şekilde çalışacak biçimde ortaklaştırıldı (send_symbol_report).

İstediğiniz gibi alarm akışında da artık sadece kısa metin değil, detaylı metrik + Binance butonu + 4s mum görseli birlikte gidiyor.

Testing

✅ python -m py_compile bot.py

Ayrıca değişiklikleri commit ettim ve PR kaydını oluşturdum.


Dosya (1)
değişiklikleri uyguladınmı
Kopyala
Evet, uyguladım. Son commit’te bu istekler kodda mevcut görünüyor (c1bc3c8):

Sembol yazınca 24s/4s/1s/5dk değişimler hesaplanıyor (ch24, ch4, ch1, ch5).

RSI(7) ve RSI(14) mesajda gösteriliyor.

Binance grafiğine yönlendiren buton eklendi.

Altına paritenin 4 saatlik mum grafiği PNG olarak gönderiliyor.

Aynı rapor alarm tetiklenince de gönderiliyor (alarm_job içinde ortak fonksiyon çağrısı).

Kontrol için çalıştırdığım komut:

git log --oneline -n 3 && git status --short && rg -n "send_symbol_report|RSI\(7\)|4 saatlik mum|Binance Grafiğinde Aç|ch4|ch1|ch5" bot.py




Diff
Günlükler

bot.py
bot.py
+156
-72

import os
import io
import json
import aiohttp
import asyncio
import websockets
import sqlite3
import logging
from datetime import datetime, timedelta
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

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
@@ -44,378 +50,456 @@ CREATE TABLE IF NOT EXISTS groups (
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
user_cooldowns = {}

# ================= RSI =================
# ================= HELPERS =================

async def calculate_rsi(symbol, period=14, interval="1m", limit=100):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
            ) as resp:
                data = await resp.json()

        closes = [float(x[4]) for x in data]
def calculate_rsi_from_closes(closes, period=14):
    if len(closes) <= period:
        return 0

    gains = []
    losses = []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

        gains = []
        losses = []
    if avg_loss == 0:
        return 100

        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100
async def fetch_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    except:
def calculate_change_percent(closes, minutes):
    need = minutes + 1
    if len(closes) < need:
        return 0

# ================= HELP =================
    current = closes[-1]
    previous = closes[-need]
    if previous == 0:
        return 0

    return ((current - previous) / previous) * 100


async def fetch_symbol_metrics(symbol):
    async with aiohttp.ClientSession() as session:
        ticker_data = await fetch_json(session, f"{BINANCE_24H}?symbol={symbol}")
        minute_klines = await fetch_json(
            session,
            f"{BINANCE_KLINES}?symbol={symbol}&interval=1m&limit=300",
        )

    closes = [float(x[4]) for x in minute_klines]
    price = float(ticker_data["lastPrice"])

    return {
        "price": price,
        "ch24": float(ticker_data["priceChangePercent"]),
        "ch4": calculate_change_percent(closes, 240),
        "ch1": calculate_change_percent(closes, 60),
        "ch5": calculate_change_percent(closes, 5),
        "rsi7": calculate_rsi_from_closes(closes, 7),
        "rsi14": calculate_rsi_from_closes(closes, 14),
    }


async def generate_4h_chart(symbol):
    async with aiohttp.ClientSession() as session:
        klines = await fetch_json(
            session,
            f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=40",
        )

    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    fig, ax = plt.subplots(figsize=(10, 4), dpi=140)
    width = 0.6

    for idx, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        color = "#26a69a" if c >= o else "#ef5350"
        ax.vlines(idx, l, h, color=color, linewidth=1)
        body_low = min(o, c)
        body_h = max(abs(c - o), 1e-9)
        ax.add_patch(Rectangle((idx - width / 2, body_low), width, body_h, color=color, alpha=0.9))

    ax.set_title(f"{symbol} - 4 Saatlik Mum Grafiği")
    ax.set_xlim(-1, len(closes))
    ax.grid(True, alpha=0.2)
    ax.set_xlabel("Mum")
    ax.set_ylabel("Fiyat")

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    buf.name = f"{symbol}_4h.png"
    return buf


def chart_keyboard(symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Binance Grafiğinde Aç", url=f"https://www.binance.com/en/trade/{symbol}?type=spot")]
    ])


def format_symbol_text(symbol, metrics):
    return (
        f"💎 {symbol}\n"
        f"💰 Fiyat: {metrics['price']}\n\n"
        f"📊 24s: %{metrics['ch24']:.2f}\n"
        f"🕓 4s: %{metrics['ch4']:.2f}\n"
        f"🕐 1s: %{metrics['ch1']:.2f}\n"
        f"⚡ 5dk: %{metrics['ch5']:.2f}\n\n"
        f"📈 RSI(7): {metrics['rsi7']}\n"
        f"📉 RSI(14): {metrics['rsi14']}"
    )


async def send_symbol_report(bot, chat_id, symbol, prefix=None):
    metrics = await fetch_symbol_metrics(symbol)
    text = format_symbol_text(symbol, metrics)
    if prefix:
        text = f"{prefix}\n\n{text}"

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=chart_keyboard(symbol))

    chart = await generate_4h_chart(symbol)
    await bot.send_photo(chat_id=chat_id, photo=chart, caption=f"🕯️ {symbol} 4 saatlik mum grafiği")


# ================= COMMANDS =================

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

# ================= MARKET =================

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
        data = await fetch_json(session, BINANCE_24H)

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)

    await update.effective_message.reply_text(
        f"📊 Market Ortalama: %{avg:.2f}"
    )

# ================= TOP24 =================

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()
        data = await fetch_json(session, BINANCE_24H)

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]

    text = "📊 24 Saat Top 10\n\n"

    for c in top:
        text += f"{c['symbol']} → %{float(c['priceChangePercent']):.2f}\n"

    await update.effective_message.reply_text(text)

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
        await update.effective_message.reply_text("Henüz 5dk veri birikmedi.")
        return

    text = "⚡ 5 Dakika Top 10\n\n"
    for sym, ch in top:
        text += f"{sym} → %{ch:.2f}\n"

    await update.effective_message.reply_text(text)

# ================= STATUS =================

async def status(update: Update, context):
    cursor.execute(
        "SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?",
        (GROUP_CHAT_ID,)
    )
    row = cursor.fetchone()

    if not row:
        await update.effective_message.reply_text("Grup kayıtlı değil.")
        return

    await update.effective_message.reply_text(
        f"Alarm: {'Açık' if row[0] else 'Kapalı'}\n"
        f"Eşik: %{row[1]}\n"
        f"Mod: {row[2]}"
    )


# ================= ADMIN =================

async def alarm_on(update: Update, context):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.effective_message.reply_text("✅ Alarm Açıldı")


async def alarm_off(update: Update, context):
    if update.effective_chat.id != GROUP_CHAT_ID:
        return

    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?", (GROUP_CHAT_ID,))
    conn.commit()
    await update.effective_message.reply_text("❌ Alarm Kapandı")


async def set_threshold(update: Update, context):
    try:
        value = float(context.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?", (value, GROUP_CHAT_ID))
        conn.commit()
        await update.effective_message.reply_text(f"Eşik %{value} yapıldı")
    except:
    except Exception:
        await update.effective_message.reply_text("Kullanım: /set 7")


async def set_mode(update: Update, context):
    try:
        mode = context.args[0].lower()
        cursor.execute("UPDATE groups SET mode=? WHERE chat_id=?", (mode, GROUP_CHAT_ID))
        conn.commit()
        await update.effective_message.reply_text(f"Mod: {mode}")
    except:
    except Exception:
        await update.effective_message.reply_text("Kullanım: /mode pump|dump|both")


# ================= USER ALARM =================

async def myalarm(update: Update, context):
    try:
        symbol = context.args[0].upper()
        threshold = float(context.args[1])
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)",
                       (update.effective_user.id, symbol, threshold))
        conn.commit()
        await update.effective_message.reply_text(
            f"🎯 {symbol} %{threshold} alarm eklendi."
        )
    except:
    except Exception:
        await update.effective_message.reply_text(
            "Kullanım: /myalarm BTCUSDT 3"
        )


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
        await send_symbol_report(context.bot, update.effective_chat.id, symbol)
    except Exception as exc:
        logging.error("symbol reply failed: %s", exc)

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])

        await update.message.reply_text(
            f"💎 {symbol}\nFiyat: {price}\n24s: %{ch24:.2f}"
        )

    except:
        pass

# ================= CALLBACK =================

async def button(update: Update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "top24":
        await top24(update, context)
    elif query.data == "top5":
        await top5(update, context)
    elif query.data == "market":
        await market(update, context)
    elif query.data == "status":
        await status(update, context)


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
                if now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                    continue

            cooldowns[symbol] = now
            trend = "🚀 YÜKSELİŞ ALARMI" if change5 > 0 else "🔻 DÜŞÜŞ ALARMI"

            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                    data = await resp.json()

            price = float(data["lastPrice"])
            change24 = float(data["priceChangePercent"])

            rsi7 = await calculate_rsi(symbol, 7)
            rsi14 = await calculate_rsi(symbol, 14)
            try:
                await send_symbol_report(context.bot, GROUP_CHAT_ID, symbol, prefix=f"{trend}\n🎯 Eşik: %{threshold}")
            except Exception as exc:
                logging.error("alarm report failed: %s", exc)

            trend = "🚀 YÜKSELİŞ" if change5 > 0 else "🔻 DÜŞÜŞ"

            text = (
                f"{trend} ALARMI\n\n"
                f"💎 {symbol}\n"
                f"💰 Fiyat: {price}\n\n"
                f"⚡ 5dk: %{change5:.2f}\n"
                f"📊 24s: %{change24:.2f}\n\n"
                f"📈 RSI(7): {rsi7}\n"
                f"📉 RSI(14): {rsi14}\n\n"
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
        except Exception:
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

    print("🚀 BOT TAM AKTİF")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
