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

# ================= ADMIN CHECK =================

async def is_admin(chat_id, user_id, bot):
    chat = await bot.get_chat(chat_id)
    if chat.type == "private":
        return True
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ["administrator", "creator"]

# ================= GROUP REGISTER =================

async def added_to_group(update: Update, context):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat.id,))
        conn.commit()
        await context.bot.send_message(chat.id, "✅ Alarm sistemi aktif.")

# ================= BINANCE FETCH =================

async def fetch_all():
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            return await resp.json()

# ================= SYMBOL REPLY =================

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

        last_price = float(data["lastPrice"])
        change_24 = float(data["priceChangePercent"])

        change_30 = 0
        change_5m = 0

        if symbol in price_memory and len(price_memory[symbol]) >= 2:
            old_price = price_memory[symbol][0][1]
            change_5m = ((last_price - old_price) / old_price) * 100

            recent = [
                (t, p) for (t, p) in price_memory[symbol]
                if datetime.utcnow() - t <= timedelta(seconds=30)
            ]
            if len(recent) >= 2:
                change_30 = ((last_price - recent[0][1]) / recent[0][1]) * 100

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📈 Binance Grafik",
                url=f"https://www.binance.com/en/trade/{symbol}"
            )]
        ])

        text = (
            f"💎 {symbol}\n\n"
            f"Fiyat: {last_price}\n"
            f"30sn: %{change_30:.2f}\n"
            f"5dk: %{change_5m:.2f}\n"
            f"24s: %{change_24:.2f}"
        )

        await update.message.reply_text(text, reply_markup=keyboard)

    except:
        pass

# ================= TOP LIST =================

async def top24(update: Update, context):
    data = await fetch_all()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]

    text = "📊 24 SAAT TOP 10\n\n"
    for c in top:
        text += f"{c['symbol']} → %{float(c['priceChangePercent']):.2f}\n"

    await update.message.reply_text(text)

async def top5(update: Update, context):
    changes = []
    for symbol in price_memory:
        prices = price_memory[symbol]
        if len(prices) >= 2:
            old = prices[0][1]
            new = prices[-1][1]
            change = ((new - old) / old) * 100
            changes.append((symbol, change))

    top = sorted(changes, key=lambda x: x[1], reverse=True)[:10]

    text = "⚡ 5DK TOP 10\n\n"
    for s, c in top:
        text += f"{s} → %{c:.2f}\n"

    await update.message.reply_text(text)

# ================= USER ALARM =================

async def myalarm(update: Update, context):
    try:
        symbol = context.args[0].upper()
        threshold = float(context.args[1])
        cursor.execute(
            "INSERT INTO user_alarms VALUES (?,?,?)",
            (update.effective_user.id, symbol, threshold)
        )
        conn.commit()
        await update.message.reply_text("✅ Kişisel alarm eklendi.")
    except:
        await update.message.reply_text("Kullanım: /myalarm BTCUSDT 3")

# ================= STATUS =================

async def status(update: Update, context):
    chat_id = update.effective_chat.id
    cursor.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Alarm kaydı yok.")
        return

    text = (
        f"Alarm Aktif: {row[1]}\n"
        f"Threshold: %{row[2]}\n"
        f"Mod: {row[3]}"
    )
    await update.message.reply_text(text)

# ================= HELP =================

async def help_command(update: Update, context):
    text = (
        "📌 KOMUTLAR\n\n"
        "/top24 → 24 saat top 10\n"
        "/top5 → 5 dk top 10\n"
        "/market → Market özeti\n"
        "/status → Alarm durumu\n"
        "/myalarm BTCUSDT 3 → Kişisel alarm\n\n"
        "ADMIN:\n"
        "/alarmon\n"
        "/alarmoff\n"
        "/set 7\n"
        "/mode both|pump|dump"
    )
    await update.message.reply_text(text)

# ================= ALARM ENGINE =================

async def alarm_job(context):
    cursor.execute("SELECT chat_id, threshold, mode FROM groups WHERE alarm_active=1")
    groups = cursor.fetchall()
    now = datetime.utcnow()

    for symbol in price_memory:
        prices = price_memory[symbol]
        if len(prices) < 2:
            continue

        old = prices[0][1]
        new = prices[-1][1]
        change = ((new - old) / old) * 100

        for chat_id, threshold, mode in groups:

            if mode == "pump" and change < threshold:
                continue
            if mode == "dump" and change > -threshold:
                continue
            if mode == "both" and abs(change) < threshold:
                continue

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
                f"🚨 ALARM\n{symbol}\n%{change:.2f}",
                reply_markup=keyboard
            )

# ================= WEBSOCKET =================

async def binance_engine(app):
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

# ================= MAIN =================

async def post_init(app):
    asyncio.create_task(binance_engine(app))

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(ChatMemberHandler(added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myalarm", myalarm))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    app.job_queue.run_repeating(alarm_job, interval=60, first=20)

    print("🚀 BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
