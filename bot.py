import os
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

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
)

# ================= CONFIG =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_WS = "wss://stream.binance.com:9443/ws/!miniTicker@arr"

COOLDOWN_MINUTES = 15
PUMP_WINDOW_SECONDS = 30
FIVE_MIN_SECONDS = 300

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

# ================= MEMORY ENGINE =================

price_memory = defaultdict(list)
cooldowns = {}  # (chat_id, symbol) -> datetime


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
        cursor.execute(
            "INSERT OR IGNORE INTO groups (chat_id) VALUES (?)",
            (chat.id,)
        )
        conn.commit()

        await context.bot.send_message(
            chat.id,
            "✅ Alarm sistemi aktif.\n🎯 Varsayılan yüzde: %7\n\n/admin komutları için /help"
        )


# ================= HELP =================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 Komutlar:\n\n"
        "/alarmon – Alarm aç\n"
        "/alarmoff – Alarm kapat\n"
        "/set 7 – Yüzde ayarla\n"
        "/status – Durumu göster"
    )
    await update.message.reply_text(text)


# ================= STATUS =================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT alarm_active, threshold FROM groups WHERE chat_id=?",
        (update.effective_chat.id,)
    )
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Grup kayıtlı değil.")
        return

    active, threshold = row

    state = "✅ Açık" if active else "❌ Kapalı"

    await update.message.reply_text(
        f"📊 Alarm Durumu: {state}\n🎯 Yüzde: %{threshold}"
    )


# ================= COMMANDS =================

async def alarm_on(update: Update, context):
    if not await is_admin(update, context):
        return

    cursor.execute(
        "UPDATE groups SET alarm_active=1 WHERE chat_id=?",
        (update.effective_chat.id,)
    )
    conn.commit()

    await update.message.reply_text("✅ Alarm açıldı.")


async def alarm_off(update: Update, context):
    if not await is_admin(update, context):
        return

    cursor.execute(
        "UPDATE groups SET alarm_active=0 WHERE chat_id=?",
        (update.effective_chat.id,)
    )
    conn.commit()

    await update.message.reply_text("❌ Alarm kapatıldı.")


async def set_threshold(update: Update, context):
    if not await is_admin(update, context):
        return

    try:
        value = float(context.args[0])
        cursor.execute(
            "UPDATE groups SET threshold=? WHERE chat_id=?",
            (value, update.effective_chat.id)
        )
        conn.commit()

        await update.message.reply_text(
            f"🎯 Alarm yüzdesi %{value} olarak ayarlandı."
        )
    except:
        await update.message.reply_text("Kullanım: /set 7")


# ================= CORE ENGINE =================

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

                    if not groups:
                        continue

                    for coin in data:

                        symbol = coin["s"]

                        if not symbol.endswith("USDT"):
                            continue

                        price = float(coin["c"])

                        price_memory[symbol].append((now, price))

                        # temizleme
                        price_memory[symbol] = [
                            (t, p) for (t, p) in price_memory[symbol]
                            if now - t <= timedelta(seconds=FIVE_MIN_SECONDS)
                        ]

                        if len(price_memory[symbol]) < 2:
                            continue

                        first_time, first_price = price_memory[symbol][0]

                        change_30 = None
                        change_5m = None

                        # 30sn hesap
                        recent_prices = [
                            (t, p) for (t, p) in price_memory[symbol]
                            if now - t <= timedelta(seconds=PUMP_WINDOW_SECONDS)
                        ]

                        if len(recent_prices) >= 2:
                            old_price = recent_prices[0][1]
                            change_30 = ((price - old_price) / old_price) * 100

                        # 5dk hesap
                        if now - first_time >= timedelta(seconds=FIVE_MIN_SECONDS):
                            change_5m = ((price - first_price) / first_price) * 100

                        for chat_id, threshold in groups:

                            key = (chat_id, symbol)

                            if key in cooldowns:
                                if now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
                                    continue

                            triggered = False
                            label = ""

                            if change_30 and abs(change_30) >= threshold:
                                triggered = True
                                label = f"🚀 ANLIK PUMP (30sn)\nDeğişim: %{change_30:.2f}"

                            elif change_5m and abs(change_5m) >= threshold:
                                triggered = True
                                label = f"🚨 5DK ALARM\nDeğişim: %{change_5m:.2f}"

                            if triggered:

                                cooldowns[key] = now

                                keyboard = InlineKeyboardMarkup([
                                    [InlineKeyboardButton(
                                        "📈 Binance Grafik",
                                        url=f"https://www.binance.com/en/trade/{symbol}"
                                    )]
                                ])

                                text = (
                                    f"{label}\n\n"
                                    f"{symbol}\n"
                                    f"🎯 Eşik: %{threshold}"
                                )

                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("alarmon", alarm_on))
    app.add_handler(CommandHandler("alarmoff", alarm_off))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("status", status))

    print("🚀 PROFESYONEL BOT AKTİF")

    app.run_polling(
        drop_pending_updates=True,
        close_loop=False
    )
