import os
import json
import aiohttp
import asyncio
import websockets
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

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
    ChatMemberHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

TOKEN = os.getenv("TELEGRAM_TOKEN")

BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
COOLDOWN_MINUTES = 15
DEFAULT_THRESHOLD = 5
DEFAULT_MODE = "both"

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

# ================= MEMORY =================

price_memory = defaultdict(list)
cooldowns = {}
user_cooldowns = {}

# ================= ADMIN CHECK =================

async def is_admin(update: Update, context):
    if update.effective_chat.type == "private":
        return True
    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    return member.status in ["administrator", "creator"]

# ================= GROUP AUTO REGISTER =================

async def added_to_group(update: Update, context):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        cursor.execute(
            "INSERT OR IGNORE INTO groups (chat_id, threshold, mode) VALUES (?, ?, ?)",
            (chat.id, DEFAULT_THRESHOLD, DEFAULT_MODE)
        )
        conn.commit()
        await context.bot.send_message(chat.id, "✅ Kripto Alarm Aktif (%5 varsayılan)")

# ================= FETCH =================

async def fetch_all():
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            return await resp.json()

# ================= MARKET =================

async def market(update: Update, context):
    data = await fetch_all()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    await update.message.reply_text(f"📊 Market Ortalama: %{avg:.2f}")

# ================= TOP24 =================

async def top24(update: Update, context):
    data = await fetch_all()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    text = "📊 24 Saat Top 10\n\n"
    for c in top:
        text += f"{c['symbol']} → %{float(c['priceChangePercent']):.2f}\n"
    await update.message.reply_text(text)

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
    text = "⚡ 5 Dakika Top 10\n\n"
    for sym, ch in top:
        text += f"{sym} → %{ch:.2f}\n"
    await update.message.reply_text(text)

# ================= SYMBOL REPLY =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return

    symbol = update.message.text.upper().strip()
    if not symbol.endswith("USDT"):
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
                data = await resp.json()

        price = float(data["lastPrice"])
        ch24 = float(data["priceChangePercent"])

        ch5 = 0
        ch30 = 0

        if symbol in price_memory and len(price_memory[symbol]) >= 2:
            old = price_memory[symbol][0][1]
            ch5 = ((price - old) / old) * 100

            recent = [
                (t, p) for (t, p) in price_memory[symbol]
                if datetime.utcnow() - t <= timedelta(seconds=30)
            ]
            if len(recent) >= 2:
                ch30 = ((price - recent[0][1]) / recent[0][1]) * 100

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📈 Binance Grafik",
                url=f"https://www.binance.com/en/trade/{symbol}"
            )]
        ])

        text = (
            f"💎 {symbol}\n\n"
            f"Fiyat: {price}\n"
            f"30sn: %{ch30:.2f}\n"
            f"5dk: %{ch5:.2f}\n"
            f"24s: %{ch24:.2f}"
        )

        await update.message.reply_text(text, reply_markup=keyboard)

    except:
        return

# ================= USER ALARM =================

async def myalarm(update: Update, context):
    try:
        symbol = context.args[0].upper()
        threshold = float(context.args[1])
        cursor.execute("INSERT INTO user_alarms VALUES (?, ?, ?)",
                       (update.effective_user.id, symbol, threshold))
        conn.commit()
        await update.message.reply_text(f"🎯 {symbol} %{threshold} alarm eklendi.")
    except:
        await update.message.reply_text("Kullanım: /myalarm BTCUSDT 3")

# ================= HELP =================

async def help_command(update: Update, context):
    text = (
        "📘 KOMUTLAR\n\n"
        "/start → Menü\n"
        "/help → Yardım\n"
        "/top24 → 24s top 10\n"
        "/top5 → 5dk top 10\n"
        "/market → Market ortalama\n"
        "/status → Alarm durumu\n\n"
        "ADMIN:\n"
        "/alarmon\n"
        "/alarmoff\n"
        "/set 7\n"
        "/mode pump|dump|both\n\n"
        "KULLANICI:\n"
        "/myalarm BTCUSDT 3"
    )
    await update.message.reply_text(text)

# ================= STATUS =================

async def status(update: Update, context):
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?",
                   (update.effective_chat.id,))
    row = cursor.fetchone()
    if not row:
        await update.message.reply_text("Grup kayıtlı değil.")
        return
    await update.message.reply_text(
        f"Aktif: {row[0]}\nThreshold: %{row[1]}\nMode: {row[2]}"
    )

# ================= ALARM =================

async def alarm_job(context):
    cursor.execute("SELECT chat_id, threshold, mode FROM groups WHERE alarm_active=1")
    groups = cursor.fetchall()
    if not groups:
        return

    now = datetime.utcnow()

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue

        old = prices[0][1]
        new = prices[-1][1]
        change = ((new - old) / old) * 100

        for chat_id, threshold, mode in groups:

            if mode == "pump" and change < 0:
                continue
            if mode == "dump" and change > 0:
                continue

            if abs(change) >= threshold:

                if symbol in cooldowns:
                    if now - cooldowns[symbol] < timedelta(minutes=COOLDOWN_MINUTES):
                        continue

                cooldowns[symbol] = now

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📈 Binance Grafik",
                        url=f"https://www.binance.com/en/trade/{symbol}"
                    )]
                ])

                await context.bot.send_message(
                    chat_id,
                    f"🚨 5DK ALARM\n{symbol}\n%{change:.2f}",
                    reply_markup=keyboard
                )

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
                            (t, p) for (t, p) in price_memory[symbol]
                            if now - t <= timedelta(minutes=5)
                        ]
        except:
            await asyncio.sleep(5)

# ================= START =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Top 24", callback_data="top24")],
        [InlineKeyboardButton("⚡ Top 5dk", callback_data="top5")]
    ])
    await update.message.reply_text("🚀 Kripto Alarm Botu", reply_markup=keyboard)

async def button(update: Update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "top24":
        await top24(update, context)
    if query.data == "top5":
        await top5(update, context)

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

    app.add_handler(ChatMemberHandler(added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("myalarm", myalarm))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
