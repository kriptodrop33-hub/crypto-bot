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
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
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
        await context.bot.send_message(chat.id, "✅ Alarm sistemi aktif. Varsayılan %7")

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
    await update.message.reply_text(f"Alarm: {state}\nYüzde: %{threshold}")

# ================= COMMANDS =================

async def alarm_on(update: Update, context):
    if not await is_admin(update, context): return
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?",
                   (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("✅ Alarm açıldı.")

async def alarm_off(update: Update, context):
    if not await is_admin(update, context): return
    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?",
                   (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("❌ Alarm kapatıldı.")

async def set_threshold(update: Update, context):
    if not await is_admin(update, context): return
    try:
        value = float(context.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?",
                       (value, update.effective_chat.id))
        conn.commit()
        await update.message.reply_text(f"🎯 Yüzde %{value} olarak ayarlandı.")
    except:
        await update.message.reply_text("Kullanım: /set 7")

# ================= TOP LIST =================

async def top24(update: Update, context):
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

async def top5(update: Update, context):
    now = datetime.utcnow()
    results = []

    for symbol, prices in price_memory.items():
        if len(prices) < 2:
            continue
        first_time, first_price = prices[0]
        if now - first_time >= timedelta(seconds=FIVE_MIN):
            last_price = prices[-1][1]
            change = ((last_price - first_price) / first_price) * 100
            results.append((symbol, change))

    results = sorted(results, key=lambda x: x[1], reverse=True)[:10]

    text = "⚡ TOP 10 (5 Dakika)\n\n"
    for symbol, change in results:
        text += f"{symbol}  %{change:.2f}\n"

    await update.message.reply_text(text)

# ================= DIRECT COIN REPLY =================

async def reply_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return

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

    now = datetime.utcnow()
    change30 = None
    change5 = None

    if symbol in price_memory and len(price_memory[symbol]) >= 2:

        recent = [(t, p) for t, p in price_memory[symbol]
                  if now - t <= timedelta(seconds=PUMP_WINDOW)]

        if len(recent) >= 2:
            old = recent[0][1]
            change30 = ((price - old) / old) * 100

        first_time, first_price = price_memory[symbol][0]
        if now - first_time >= timedelta(seconds=FIVE_MIN):
            change5 = ((price - first_price) / first_price) * 100

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📈 Binance Grafik",
            url=f"https://www.binance.com/en/trade/{symbol}"
        )]
    ])

    text = f"💎 {symbol}\n\nFiyat: {price}\n24s: %{change24:.2f}"
    if change30:
        text += f"\n30sn: %{change30:.2f}"
    if change5:
        text += f"\n5dk: %{change5:.2f}"

    await update.message.reply_text(text, reply_markup=keyboard)

# ================= ENGINE =================

async def binance_engine(app):
    while True:
        try:
            async with websockets.connect(BINANCE_WS) as ws:
                async for message in ws:
                    data = json.loads(message)
                    now = datetime.utcnow()

                    cursor.execute(
                        "SELECT chat_id, threshold FROM groups WHERE alarm_active=1"
                    )
                    groups = cursor.fetchall()

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

                        if len(price_memory[symbol]) < 2:
                            continue

                        first_time, first_price = price_memory[symbol][0]
                        change5 = ((price - first_price) / first_price) * 100

                        recent = [(t, p) for t, p in price_memory[symbol]
                                  if now - t <= timedelta(seconds=PUMP_WINDOW)]

                        change30 = None
                        if len(recent) >= 2:
                            old = recent[0][1]
                            change30 = ((price - old) / old) * 100

                        for chat_id, threshold in groups:

                            key = (chat_id, symbol)

                            if key in cooldowns:
                                if now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
                                    continue

                            trigger = False
                            label = ""

                            if change30 and abs(change30) >= threshold:
                                trigger = True
                                label = f"🚀 ANLIK PUMP %{change30:.2f}"

                            elif abs(change5) >= threshold:
                                trigger = True
                                label = f"🚨 5DK ALARM %{change5:.2f}"

                            if trigger:
                                cooldowns[key] = now

                                keyboard = InlineKeyboardMarkup([
                                    [InlineKeyboardButton(
                                        "📈 Binance Grafik",
                                        url=f"https://www.binance.com/en/trade/{symbol}"
                                    )]
                                ])

                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=f"{label}\n{symbol}",
                                    reply_markup=keyboard
                                )

        except Exception as e:
            print("WebSocket hata:", e)
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
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_coin))

    print("🚀 FULL PROFESYONEL BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
