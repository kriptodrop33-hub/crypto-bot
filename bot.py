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
    threshold REAL DEFAULT 10
)
""")
conn.commit()

# ================= GLOBAL MEMORY =================

price_memory = defaultdict(list)
cooldowns = {}

# ================= ASYNC BINANCE =================

async def fetch_json(session, url):
    async with session.get(url) as response:
        return await response.json()

async def get_top500():
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(session, BINANCE_24H)

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    sorted_coins = sorted(usdt, key=lambda x: float(x["quoteVolume"]), reverse=True)
    return sorted_coins[:500]

# ================= ADMIN CHECK =================

async def is_admin(update: Update, context):
    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    return member.status in ["administrator", "creator"]

# ================= GROUP REGISTER =================

async def added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat.id,))
        conn.commit()
        await context.bot.send_message(chat.id, "✅ Alarm sistemi aktif.")

# ================= 5DK ALARM =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):

    cursor.execute("SELECT chat_id, threshold FROM groups WHERE alarm_active = 1")
    groups = cursor.fetchall()
    if not groups:
        return

    coins = await get_top500()
    now = datetime.utcnow()

    for coin in coins:
        symbol = coin["symbol"]
        change_24h = float(coin["priceChangePercent"])
        last_price = float(coin["lastPrice"])
        open_price = float(coin["openPrice"])

        change_5m = ((last_price - open_price) / open_price) * 100

        for chat_id, threshold in groups:

            if abs(change_5m) >= threshold:

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

                text = (
                    f"🚨 5DK ALARM (%{threshold})\n\n"
                    f"{symbol}\n"
                    f"5dk: %{change_5m:.2f}\n"
                    f"24s: %{change_24h:.2f}"
                )

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard
                )

# ================= 30SN PUMP DETECTION =================

async def pump_watcher(app):

    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

    while True:
        try:
            async with websockets.connect(uri) as ws:
                async for message in ws:
                    data = json.loads(message)
                    now = datetime.utcnow()

                    cursor.execute("SELECT chat_id, threshold FROM groups WHERE alarm_active = 1")
                    groups = cursor.fetchall()
                    if not groups:
                        continue

                    for coin in data:
                        symbol = coin["s"]

                        if not symbol.endswith("USDT"):
                            continue

                        price = float(coin["c"])

                        price_memory[symbol].append((now, price))

                        price_memory[symbol] = [
                            (t, p) for (t, p) in price_memory[symbol]
                            if now - t <= timedelta(seconds=30)
                        ]

                        if len(price_memory[symbol]) < 2:
                            continue

                        old_price = price_memory[symbol][0][1]
                        change = ((price - old_price) / old_price) * 100

                        for chat_id, threshold in groups:

                            if abs(change) >= threshold:

                                keyboard = InlineKeyboardMarkup([
                                    [InlineKeyboardButton(
                                        "📈 Binance Grafik",
                                        url=f"https://www.binance.com/en/trade/{symbol}"
                                    )]
                                ])

                                text = (
                                    f"🚀 ANLIK PUMP (%{threshold})\n\n"
                                    f"{symbol}\n"
                                    f"30sn: %{change:.2f}"
                                )

                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
                                    reply_markup=keyboard
                                )

                                price_memory[symbol] = []

        except Exception as e:
            print("WebSocket hata:", e)
            await asyncio.sleep(5)

# ================= COMMANDS =================

async def alarm_on(update: Update, context):
    if not await is_admin(update, context):
        return
    cursor.execute("UPDATE groups SET alarm_active = 1 WHERE chat_id = ?", (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("✅ Alarm açıldı.")

async def alarm_off(update: Update, context):
    if not await is_admin(update, context):
        return
    cursor.execute("UPDATE groups SET alarm_active = 0 WHERE chat_id = ?", (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("❌ Alarm kapatıldı.")

async def set_threshold(update: Update, context):
    if not await is_admin(update, context):
        return

    try:
        value = float(context.args[0])
        cursor.execute(
            "UPDATE groups SET threshold = ? WHERE chat_id = ?",
            (value, update.effective_chat.id)
        )
        conn.commit()
        await update.message.reply_text(f"🎯 Alarm yüzdesi %{value} olarak ayarlandı.")
    except:
        await update.message.reply_text("Kullanım: /set 7")

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(ChatMemberHandler(added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))

    app.job_queue.run_repeating(alarm_job, interval=300, first=20)

    async def post_init(app):
        app.create_task(pump_watcher(app))

    app.post_init = post_init

    print("🚀 BOT AKTİF")
    app.run_polling()

if __name__ == "__main__":
    main()