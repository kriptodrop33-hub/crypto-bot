import os
import io
import json
import aiohttp
import asyncio
import websockets
import asyncpg
import logging
import pandas as pd
import mplfinance as mpf
import matplotlib
matplotlib.use("Agg")

from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ================= CONFIG =================

TOKEN         = os.getenv("TELEGRAM_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_ID"))
DATABASE_URL  = os.getenv("DATABASE_URL")          # Railway PostgreSQL URL

BINANCE_24H    = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COOLDOWN_MINUTES  = 15
DEFAULT_THRESHOLD = 5.0
DEFAULT_MODE      = "both"
MAX_SYMBOLS       = 500          # price_memory bellek limiti

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ================= DATABASE (PostgreSQL) =================

db_pool: asyncpg.Pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id      BIGINT PRIMARY KEY,
                alarm_active INTEGER DEFAULT 1,
                threshold    REAL    DEFAULT 5,
                mode         TEXT    DEFAULT 'both'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_alarms (
                id        SERIAL PRIMARY KEY,
                user_id   BIGINT,
                username  TEXT,
                symbol    TEXT,
                threshold REAL,
                active    INTEGER DEFAULT 1,
                UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            INSERT INTO groups (chat_id, threshold, mode)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO NOTHING
        """, GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE)

    log.info("PostgreSQL baglantisi kuruldu.")

# ================= MEMORY =================

price_memory: dict = {}   # {symbol: [(datetime, float), ...]}
cooldowns:    dict = {}
chart_cache:  dict = {}   # {symbol: (datetime, BytesIO)}

# ================= YARDIMCI =================

def get_number_emoji(n):
    emojis = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",
              6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}
    return emojis.get(n, str(n))

def format_price(price):
    return f"{price:,.2f}" if price >= 1 else f"{price:.8g}"

# ================= MUM GRAFIGI (onbellekli) =================

async def generate_candlestick_chart(symbol: str):
    if symbol in chart_cache:
        cached_at, buf = chart_cache[symbol]
        if datetime.utcnow() - cached_at < timedelta(minutes=5):
            buf.seek(0)
            return buf

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=60",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()

        if not data or isinstance(data, dict):
            return None

        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        df = df[["open","high","low","close","volume"]].astype(float)

        mc = mpf.make_marketcolors(
            up="#00e676", down="#ff1744",
            edge="inherit", wick="inherit",
            volume={"up":"#00e676","down":"#ff1744"},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            facecolor="#0d1117", edgecolor="#30363d",
            figcolor="#0d1117", gridcolor="#21262d", gridstyle="--",
            rc={"axes.labelcolor":"#8b949e","xtick.color":"#8b949e",
                "ytick.color":"#8b949e","font.size":9}
        )

        buf = io.BytesIO()
        mpf.plot(
            df, type="candle", style=style,
            title=f"\n{symbol} - 4 Saatlik Mum Grafigi (Son 60 Mum)",
            ylabel="Fiyat (USDT)", volume=True, figsize=(12,7),
            savefig=dict(fname=buf, format="png", bbox_inches="tight", dpi=150),
        )
        buf.seek(0)
        chart_cache[symbol] = (datetime.utcnow(), buf)
        return buf

    except Exception as e:
        log.error(f"Grafik hatasi ({symbol}): {e}")
        return None

# ================= ANALIZ (paralel istekler) =================

async def fetch_klines(session, symbol, interval, limit=2):
    try:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            return await resp.json()
    except Exception as e:
        log.warning(f"Klines hatasi {symbol}/{interval}: {e}")
        return []

def calc_change(data):
    if not data or len(data) < 2:
        return 0.0
    first = float(data[0][4])
    last  = float(data[-1][4])
    return round(((last - first) / first) * 100, 2)

def calc_rsi(data, period=14):
    try:
        closes = [float(x[4]) for x in data]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return 0.0

async def fetch_all_analysis(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_24H}?symbol={symbol}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            ticker = await resp.json()

        k4h, k1h, k5m, krsi = await asyncio.gather(
            fetch_klines(session, symbol, "4h",  limit=2),
            fetch_klines(session, symbol, "1h",  limit=2),
            fetch_klines(session, symbol, "5m",  limit=2),
            fetch_klines(session, symbol, "1h",  limit=100),
        )

    return ticker, k4h, k1h, k5m, krsi

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None):
    try:
        ticker, k4h, k1h, k5m, krsi = await fetch_all_analysis(symbol)

        if "lastPrice" not in ticker:
            return

        price  = float(ticker["lastPrice"])
        ch24   = float(ticker["priceChangePercent"])
        ch4h   = calc_change(k4h)
        ch1h   = calc_change(k1h)
        ch5m   = calc_change(k5m)
        rsi7   = calc_rsi(krsi, 7)
        rsi14  = calc_rsi(krsi, 14)

        def get_ui(val):
            return ("🟢","+") if val > 0 else ("🔴","") if val < 0 else ("⚪","")

        e5,s5   = get_ui(ch5m)
        e1,s1   = get_ui(ch1h)
        e4,s4   = get_ui(ch4h)
        e24,s24 = get_ui(ch24)

        text = (
            f"📊 *{extra_title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Parite:* `#{symbol}`\n"
            f"💵 *Fiyat:* `{format_price(price)} USDT`\n\n"
            f"*Performans Degisimleri:*\n"
            f"{e5} `5dk  :` `% {s5}{ch5m:+.2f}`\n"
            f"{e1} `1sa  :` `% {s1}{ch1h:+.2f}`\n"
            f"{e4} `4sa  :` `% {s4}{ch4h:+.2f}`\n"
            f"{e24} `24sa :` `% {s24}{ch24:+.2f}`\n\n"
            f"📉 *RSI (Son 100 Saatlik Veri):*\n"
            f"• RSI 7  : `{rsi7}`\n"
            f"• RSI 14 : `{rsi14}`\n"
            f"──────────────────"
        )
        if threshold_info:
            text += f"\n🎯 *Alarm Esigi:* `% {threshold_info}`"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📈 Binance'de Goruntule",
                url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
            )
        ]])

        await bot.send_message(chat_id=chat_id, text=text,
                               reply_markup=keyboard, parse_mode="Markdown")

        chart_buf = await generate_candlestick_chart(symbol)
        if chart_buf:
            await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(chart_buf, filename=f"{symbol}_4h.png"),
                caption=f"🕯️ *{symbol}* — 4 Saatlik Mum Grafigi",
                parse_mode="Markdown"
            )

    except Exception as e:
        log.error(f"Gonderim hatasi ({symbol}): {e}")

# ================= SEMBOL TEPKI =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return
    raw    = update.message.text.upper().strip()
    symbol = raw.replace("#","").replace("/","")
    if symbol.endswith("USDT"):
        await send_full_analysis(
            context.bot, update.effective_chat.id,
            symbol, "PIYASA ANALIZ RAPORU"
        )

# ================= KISISEL ALARM =================

async def my_alarm(update: Update, context):
    user    = update.effective_user
    user_id = user.id

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, threshold, active FROM user_alarms WHERE user_id=$1",
            user_id
        )

    if not rows:
        text = (
            "🔔 *Kisisel Alarm Paneli*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Henuz aktif alarminiz yok.\n\n"
            "➕ Alarm eklemek icin:\n"
            "`/alarm_ekle BTCUSDT 3.5`\n"
            "_(sembol ve % esik degeri)_"
        )
    else:
        text = "🔔 *Kisisel Alarmlariniz*\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            durum = "✅ Aktif" if r["active"] else "⏸ Durduruldu"
            text += f"• `{r['symbol']}` → `%{r['threshold']}` — {durum}\n"
        text += "\n`/alarm_ekle BTCUSDT 3.5` — yeni ekle\n`/alarm_sil BTCUSDT` — sil"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Alarm Ekle",  callback_data="alarm_guide"),
        InlineKeyboardButton("🗑 Tumunu Sil", callback_data=f"alarm_deleteall_{user_id}")
    ]])

    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def alarm_ekle(update: Update, context):
    user    = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanim: `/alarm_ekle BTCUSDT 3.5`\n_(sembol ve % esik degeri)_",
            parse_mode="Markdown"
        )
        return

    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    try:
        threshold = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Esik degeri sayi olmalidir. Ornek: `3.5`", parse_mode="Markdown")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_alarms (user_id, username, symbol, threshold, active)
            VALUES ($1, $2, $3, $4, 1)
            ON CONFLICT (user_id, symbol)
            DO UPDATE SET threshold=$4, active=1
        """, user_id, username, symbol, threshold)

    await update.message.reply_text(
        f"✅ *{symbol}* icin `%{threshold}` esikli alarm eklendi!\n"
        f"5 dakikalik harekette bu esigi asarsa size ozel mesaj atacagim.",
        parse_mode="Markdown"
    )


async def alarm_sil(update: Update, context):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Kullanim: `/alarm_sil BTCUSDT`", parse_mode="Markdown")
        return

    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_alarms WHERE user_id=$1 AND symbol=$2",
            user_id, symbol
        )

    if result == "DELETE 0":
        await update.message.reply_text(f"`{symbol}` icin kayitli alarm bulunamadi.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🗑 `{symbol}` alarmi silindi.", parse_mode="Markdown")

# ================= KOMUTLAR =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Genel Market",    callback_data="market")],
        [InlineKeyboardButton("📈 24s Liderleri",   callback_data="top24"),
         InlineKeyboardButton("⚡ 5dk Flashlar",    callback_data="top5")],
        [InlineKeyboardButton("🔔 Kisisel Alarmim", callback_data="my_alarm")],
        [InlineKeyboardButton("⚙️ Sistem Durumu",   callback_data="status")]
    ])
    welcome_text = (
        "👋 *Kripto Analiz Asistanina Hos Geldin!*\n\n"
        "Sanal asistan 7/24 piyasayi takip eder. "
        "Analiz almak icin parite ismini yazman yeterli.\n\n"
        "💡 *Ornek:* `BTCUSDT`\n\n"
        "🔔 *Kisisel alarm* kurmak icin:\n"
        "`/alarm_ekle BTCUSDT 3.5`"
    )
    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg  = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    status_emoji = "🐂" if avg > 0 else "🐻"
    msg = f"{status_emoji} *Piyasa Duyarliligi:* `%{avg:+.2f}`"
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(msg, parse_mode="Markdown")

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    usdt = sorted(
        [x for x in data if x["symbol"].endswith("USDT")],
        key=lambda x: float(x["priceChangePercent"]), reverse=True
    )[:10]
    text = "🏆 *24 Saatlik Performans Liderleri*\n━━━━━━━━━━━━━━━━━━━━━\n"
    for i, c in enumerate(usdt, 1):
        text += f"{get_number_emoji(i)} `{c['symbol']:<12}` → `%{float(c['priceChangePercent']):+6.2f}`\n"
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

async def top5(update: Update, context):
    if not price_memory:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        usdt = sorted(
            [x for x in data if x["symbol"].endswith("USDT")],
            key=lambda x: abs(float(x["priceChangePercent"])), reverse=True
        )[:10]
        text = "⚡ *Piyasanin En Hareketlileri (24s baz)*\n━━━━━━━━━━━━━━━━━━━━━\n"
        for i, c in enumerate(usdt, 1):
            text += f"{get_number_emoji(i)} `{c['symbol']:<12}` → `%{float(c['priceChangePercent']):+6.2f}`\n"
        text += "\n_⏳ WebSocket verisi henuz doluyor..._"
    else:
        changes = []
        for s, p in price_memory.items():
            if len(p) >= 2:
                changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))
        top = sorted(changes, key=lambda x: abs(x[1]), reverse=True)[:10]
        text = "⚡ *Son 5 Dakikanin En Hareketlileri*\n━━━━━━━━━━━━━━━━━━━━━\n"
        for i, (s, c) in enumerate(top, 1):
            text += f"{get_number_emoji(i)} `{s:<12}` → `%{c:+6.2f}`\n"

    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=$1",
            GROUP_CHAT_ID
        )
    text = (
        "ℹ️ *Sistem Yapilandirmasi*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTIF' if r['alarm_active'] else 'KAPALI'}`\n"
        f"🎯 *Esik Degeri:* `% {r['threshold']}`\n"
        f"🔄 *Izleme Modu:* `{r['mode'].upper()}`\n"
        f"📦 *Takip Edilen Sembol:* `{len(price_memory)}`"
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

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
    elif q.data == "my_alarm":
        await my_alarm(update, context)
    elif q.data == "alarm_guide":
        await q.message.reply_text(
            "➕ *Alarm Eklemek Icin:*\n`/alarm_ekle BTCUSDT 3.5`\n\n"
            "🗑 *Alarm Silmek Icin:*\n`/alarm_sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data.startswith("alarm_deleteall_"):
        uid = int(q.data.split("_")[-1])
        if q.from_user.id == uid:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM user_alarms WHERE user_id=$1", uid)
            await q.message.reply_text("🗑 Tum kisisel alarmlariniz silindi.")

# ================= ALARM JOB =================

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()

    async with db_pool.acquire() as conn:
        group_row = await conn.fetchrow(
            "SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=$1",
            GROUP_CHAT_ID
        )
        user_rows = await conn.fetch(
            "SELECT user_id, symbol, threshold FROM user_alarms WHERE active=1"
        )

    # Grup alarmlari
    if group_row and group_row["alarm_active"]:
        threshold = group_row["threshold"]
        for symbol, prices in list(price_memory.items()):
            if len(prices) < 2:
                continue
            ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
            if abs(ch5) >= threshold:
                key = f"group_{symbol}"
                if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
                    continue
                cooldowns[key] = now
                await send_full_analysis(
                    context.bot, GROUP_CHAT_ID,
                    symbol, "ANLIK SINYAL UYARISI", threshold
                )

    # Kisisel alarmlar
    for row in user_rows:
        symbol    = row["symbol"]
        user_id   = row["user_id"]
        threshold = row["threshold"]

        prices = price_memory.get(symbol)
        if not prices or len(prices) < 2:
            continue

        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
        if abs(ch5) >= threshold:
            key = f"user_{user_id}_{symbol}"
            if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
                continue
            cooldowns[key] = now
            try:
                await send_full_analysis(
                    context.bot, user_id,
                    symbol,
                    f"KISISEL ALARM — {symbol}",
                    threshold
                )
            except Exception as e:
                log.warning(f"Kisisel alarm gonderilemedi (user={user_id}): {e}")

# ================= WEBSOCKET =================

async def binance_engine():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                log.info("Binance WebSocket baglandi.")
                async for msg in ws:
                    data = json.loads(msg)
                    now  = datetime.utcnow()
                    for c in data:
                        s = c["s"]
                        if not s.endswith("USDT"):
                            continue
                        if s not in price_memory and len(price_memory) >= MAX_SYMBOLS:
                            continue
                        if s not in price_memory:
                            price_memory[s] = []
                        price_memory[s].append((now, float(c["c"])))
                        price_memory[s] = [
                            (t, p) for (t, p) in price_memory[s]
                            if now - t <= timedelta(minutes=5)
                        ]
        except Exception as e:
            log.error(f"WebSocket hatasi: {e} — 5 saniye sonra yeniden baglaniliyor.")
            await asyncio.sleep(5)

async def post_init(app):
    await init_db()
    asyncio.create_task(binance_engine())

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job, interval=60, first=30)

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("top24",      top24))
    app.add_handler(CommandHandler("top5",       top5))
    app.add_handler(CommandHandler("market",     market))
    app.add_handler(CommandHandler("status",     status))
    app.add_handler(CommandHandler("alarmim",    my_alarm))
    app.add_handler(CommandHandler("alarm_ekle", alarm_ekle))
    app.add_handler(CommandHandler("alarm_sil",  alarm_sil))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("BOT AKTIF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
