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
    filters,
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
COOLDOWN_MINUTES = 15

# ================= DATABASE =================

conn = sqlite3.connect("bot.db", check_same_thread=False)
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

async def is_admin(update: Update, context):
    chat = update.effective_chat
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
    return member.status in ["administrator", "creator"]

# ================= GROUP REGISTER =================

async def added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat.id,))
        conn.commit()
        await context.bot.send_message(chat.id, "✅ Alarm sistemi aktif edildi.")

# ================= FETCH =================

async def fetch_symbol(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BINANCE_24H}?symbol={symbol}") as resp:
            return await resp.json()

async def fetch_all():
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as resp:
            return await resp.json()

# ================= START =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Top 24 Saat", callback_data="dummy")],
        [InlineKeyboardButton("⚡ Top 5 Dakika", callback_data="dummy")]
    ])
    await update.message.reply_text(
        "🚀 Kripto Alarm Botu Aktif\n\n/help yazarak komutları görebilirsin.",
        reply_markup=keyboard
    )

# ================= HELP =================

async def help_command(update: Update, context):
    text = """
📌 KOMUTLAR

/start → Botu başlat
/help → Bu menü
/status → Alarm durumu
/top24 → 24 saat top 10
/top5 → 5 dk top 10
/myalarm BTCUSDT 3 → Kişisel alarm

👮 ADMIN (grupta admin şart)
/alarmon → Alarm aç
/alarmoff → Alarm kapat
/set 7 → Yüzde ayarla
/mode both|pump|dump → Alarm modu
"""
    await update.message.reply_text(text)

# ================= STATUS =================

async def status(update: Update, context):
    chat_id = update.effective_chat.id
    cursor.execute("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=?", (chat_id,))
    row = cursor.fetchone()

    if not row:
        return await update.message.reply_text("Bu sohbet için alarm kaydı yok.")

    await update.message.reply_text(
        f"Alarm: {'Açık' if row[0] else 'Kapalı'}\n"
        f"Threshold: %{row[1]}\n"
        f"Mod: {row[2]}"
    )

# ================= ADMIN COMMANDS =================

async def alarmon(update: Update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("⛔ Admin gerekli.")

    cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (update.effective_chat.id,))
    cursor.execute("UPDATE groups SET alarm_active=1 WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("✅ Alarm açıldı.")

async def alarmoff(update: Update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("⛔ Admin gerekli.")

    cursor.execute("UPDATE groups SET alarm_active=0 WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    await update.message.reply_text("❌ Alarm kapatıldı.")

async def set_threshold(update: Update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("⛔ Admin gerekli.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /set 7")

    try:
        value = float(context.args[0])
        cursor.execute("UPDATE groups SET threshold=? WHERE chat_id=?",
                       (value, update.effective_chat.id))
        conn.commit()
        await update.message.reply_text(f"Yeni threshold: %{value}")
    except:
        await update.message.reply_text("Geçerli sayı gir.")

async def mode(update: Update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("⛔ Admin gerekli.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /mode both|pump|dump")

    value = context.args[0].lower()
    if value not in ["both", "pump", "dump"]:
        return await update.message.reply_text("Seçenek: both | pump | dump")

    cursor.execute("UPDATE groups SET mode=? WHERE chat_id=?",
                   (value, update.effective_chat.id))
    conn.commit()
    await update.message.reply_text(f"Mod ayarlandı: {value}")

# ================= TOP =================

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

# ================= SYMBOL REPLY =================

async def reply_symbol(update: Update, context):
    if not update.message:
        return

    symbol = update.message.text.upper().strip()
    if not symbol.endswith("USDT"):
        return

    try:
        data = await fetch_symbol(symbol)
        last_price = float(data["lastPrice"])
        change_24 = float(data["priceChangePercent"])

        change_5m = 0
        change_30 = 0

        if symbol in price_memory and len(price_memory[symbol]) >= 2:
            old = price_memory[symbol][0][1]
            change_5m = ((last_price - old) / old) * 100

            recent = [
                (t, p) for (t, p) in price_memory[symbol]
                if datetime.utcnow() - t <= timedelta(seconds=30)
            ]
            if len(recent) >= 2:
                change_30 = ((last_price - recent[0][1]) / recent[0][1]) * 100

        await update.message.reply_text(
            f"💎 {symbol}\n\n"
            f"Fiyat: {last_price}\n"
            f"30sn: %{change_30:.2f}\n"
            f"5dk: %{change_5m:.2f}\n"
            f"24s: %{change_24:.2f}"
        )

    except:
        pass

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

    app.add_handler(ChatMemberHandler(added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("alarmon", alarmon))
    app.add_handler(CommandHandler("alarmoff", alarmoff))
    app.add_handler(CommandHandler("set", set_threshold))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("top24", top24))
    app.add_handler(CommandHandler("top5", top5))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    print("🚀 BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
