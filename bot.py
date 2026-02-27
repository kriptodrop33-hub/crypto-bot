import os
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

import aiohttp
import websockets

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

# ================= CONFIG =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_WS = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"

COOLDOWN_MINUTES = 15
PUMP_WINDOW = 30
FIVE_MIN = 300

# ================= DATABASE =================

conn = sqlite3.connect("groups.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    alarm_active INTEGER DEFAULT 1,
    threshold REAL DEFAULT 7
)
""")
conn.commit()

# ================= MEMORY =================

price_memory = defaultdict(list)
cooldowns = {}

# ================= BOT COMMAND MENU =================

async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Bot menüsünü aç"),
        BotCommand("alarmon", "Alarmı aç"),
        BotCommand("alarmoff", "Alarmı kapat"),
        BotCommand("set", "Alarm yüzdesi ayarla"),
        BotCommand("top24", "24 saat Top 10"),
        BotCommand("top5", "5 dakika Top 10"),
        BotCommand("status", "Alarm durumunu göster"),
    ]
    await app.bot.set_my_commands(commands)

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 Top 10 (24s)", callback_data="top24"),
            InlineKeyboardButton("⚡ Top 10 (5dk)", callback_data="top5"),
        ],
        [
            InlineKeyboardButton("📊 Alarm Durumu", callback_data="status")
        ]
    ])

    await update.message.reply_text(
        "🚀 Crypto Alarm Bot\n\n"
        "• Sembol yaz → BTCUSDT\n"
        "• Alarm sistemi aktif\n"
        "• Menüden seçim yap",
        reply_markup=keyboard
    )

# ================= TOP 24 =================

async def top24(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            data = await resp.json()

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    sorted_coins = sorted(usdt,
                          key=lambda x: float(x["priceChangePercent"]),
                          reverse=True)[:10]

    text = "🔥 TOP 10 (24 Saat)\n\n"
    for coin in sorted_coins:
        text += f"{coin['symbol']}  %{float(coin['priceChangePercent']):.2f}\n"

    await update.message.reply_text(text)

# ================= TOP 5 DK =================

async def top5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    results = []

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue

        first_time, first_price = prices[0]
        last_price = prices[-1][1]

        if now - first_time >= timedelta(seconds=FIVE_MIN):
            change = ((last_price - first_price) / first_price) * 100
            results.append((symbol, change))

    results = sorted(results, key=lambda x: x[1], reverse=True)[:10]

    text = "⚡ TOP 10 (5 Dakika)\n\n"
    for symbol, change in results:
        text += f"{symbol}  %{change:.2f}\n"

    await update.message.reply_text(text)

# ================= STATUS =================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT alarm_active, threshold FROM groups WHERE chat_id=?",
                   (update.effective_chat.id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Grup kayıtlı değil.")
        return

    active, threshold = row
    state = "✅ Açık" if active else "❌ Kapalı"

    await update.message.reply_text(
        f"Alarm: {state}\nYüzde: %{threshold}"
    )

# ================= ALARM COMMANDS =================

async def alarm_on(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?",
                   (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("✅ Alarm açıldı.")

async def alarm_off(update: Update, context):
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?",
                   (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("❌ Alarm kapatıldı.")

async def set_threshold(update: Update, context):
    try:
        value = float(context.args[0])
        cursor.execute(
            "UPDATE groups SET threshold=? WHERE chat_id=?",
            (value, update.effective_chat.id)
        )
        conn.commit()
        await update.message.reply_text(f"🎯 Yeni alarm yüzdesi %{value}")
    except:
        await update.message.reply_text("Kullanım: /set 7")

# ================= SYMBOL REPLY =================

async def reply_coin(update: Update, context):
    symbol = update.message.text.upper().strip()
    if not symbol.endswith("USDT"):
        return

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
            data = await resp.json()

    if "lastPrice" not in data:
        return

    price = float(data["lastPrice"])
    change24 = float(data["priceChangePercent"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📈 Binance Grafik",
            url=f"https://www.binance.com/en/trade/{symbol}"
        )]
    ])

    text = (
        f"💎 {symbol}\n\n"
        f"Fiyat: {price}\n"
        f"24s: %{change24:.2f}"
    )

    await update.message.reply_text(text, reply_markup=keyboard)

# ================= ENGINE =================

async def binance_engine(app):
    while True:
        try:
            async with websockets.connect(BINANCE_WS) as ws:
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
                            (t, p) for t, p in price_memory[symbol]
                            if now - t <= timedelta(seconds=FIVE_MIN)
                        ]

        except:
            await asyncio.sleep(5)

# ================= GROUP REGISTER =================

async def added_to_group(update: Update, context):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat.id,))
        conn.commit()

# ================= MAIN =================

async def post_init(app):
    await set_bot_commands(app)
    asyncio.create_task(binance_engine(app))

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(ChatMemberHandler(added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_coin))

    print("🚀 BOT V4 AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
