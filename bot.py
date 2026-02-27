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
            ALTER TABLE user_alarms
            ADD COLUMN IF NOT EXISTS alarm_type    TEXT    DEFAULT 'percent',
            ADD COLUMN IF NOT EXISTS rsi_level     REAL    DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS band_low      REAL    DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS band_high     REAL    DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS paused_until  TIMESTAMPTZ DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS trigger_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_triggered TIMESTAMPTZ DEFAULT NULL
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alarm_history (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT,
                symbol       TEXT,
                alarm_type   TEXT,
                trigger_val  REAL,
                direction    TEXT,
                triggered_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT,
                symbol  TEXT,
                UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id        SERIAL PRIMARY KEY,
                user_id   BIGINT,
                chat_id   BIGINT,
                task_type TEXT,
                symbol    TEXT    NOT NULL DEFAULT '',
                hour      INTEGER,
                minute    INTEGER,
                active    INTEGER DEFAULT 1,
                UNIQUE(chat_id, task_type, symbol)
            )
        """)
        await conn.execute("""
            INSERT INTO groups (chat_id, threshold, mode)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO NOTHING
        """, GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE)

    log.info("PostgreSQL baglantisi kuruldu.")

# ================= MEMORY =================

price_memory:      dict = {}
cooldowns:         dict = {}
chart_cache:       dict = {}
whale_vol_mem:     dict = {}
scheduled_last_run:dict = {}

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
    if first == 0:
        return 0.0
    return round(((last - first) / first) * 100, 2)

def calc_rsi(data, period=14):
    try:
        closes = [float(x[4]) for x in data]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        if len(gains) < period:
            return 0.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return 0.0

def calc_market_score(ticker, ch5m, ch1h, ch4h, ch24, rsi7, rsi14):
    """
    Piyasa verilerini 100 uzerinden puanlar.
    Skor: 0-39 = Zayif/Sat, 40-59 = Notr, 60-79 = Güçlü, 80-100 = Cok Güçlü/Al
    """
    score = 50  # Baslangic

    # RSI bazli puan (max +-20)
    if rsi14 <= 30:
        score += 20   # Asiri satim -> al firsati
    elif rsi14 <= 45:
        score += 10
    elif rsi14 >= 70:
        score -= 20   # Asiri alim -> dikkat
    elif rsi14 >= 55:
        score -= 10

    # RSI7 hizli tepki (max +-10)
    if rsi7 < 30:
        score += 10
    elif rsi7 > 70:
        score -= 10

    # 5 dakikalik momentum (max +-10)
    if ch5m > 3:    score += 10
    elif ch5m > 1:  score += 5
    elif ch5m < -3: score -= 10
    elif ch5m < -1: score -= 5

    # 1 saatlik trend (max +-10)
    if ch1h > 5:    score += 10
    elif ch1h > 2:  score += 5
    elif ch1h < -5: score -= 10
    elif ch1h < -2: score -= 5

    # 4 saatlik trend (max +-10)
    if ch4h > 5:    score += 10
    elif ch4h > 2:  score += 5
    elif ch4h < -5: score -= 10
    elif ch4h < -2: score -= 5

    # Hacim/24s degisim dogrulama (max +-5)
    vol_change = float(ticker.get("quoteVolume", 0))
    if ch24 > 5 and ch5m > 0:  score += 5
    elif ch24 < -5 and ch5m < 0: score -= 5

    score = max(0, min(100, round(score)))

    if score >= 80:
        label = "🚀 Cok Guçlu — AL sinyali"
        bar = "🟢🟢🟢🟢🟢"
    elif score >= 60:
        label = "💪 Guçlu — Pozitif"
        bar = "🟢🟢🟢🟡➖"
    elif score >= 40:
        label = "😐 Notr — Bekle"
        bar = "🟡🟡🟡➖➖"
    elif score >= 20:
        label = "⚠️ Zayif — Dikkat"
        bar = "🔴🔴➖➖➖"
    else:
        label = "🚨 Cok Zayif — SAT sinyali"
        bar = "🔴🔴🔴🔴🔴"

    return score, label, bar

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
            if val > 0:   return "🟢▲", "+"
            elif val < 0: return "🔴▼", ""
            else:         return "⚪→", ""

        e5,s5   = get_ui(ch5m)
        e1,s1   = get_ui(ch1h)
        e4,s4   = get_ui(ch4h)
        e24,s24 = get_ui(ch24)

        # RSI yorumu
        def rsi_label(r):
            if r >= 70:   return "🔴 Asiri Alim"
            elif r >= 55: return "🟡 Yukselis"
            elif r <= 30: return "🔵 Asiri Satim"
            elif r <= 45: return "🟡 Dusus"
            else:         return "🟢 Normal"

        # 100 uzerinden piyasa skoru
        score, score_label, score_bar = calc_market_score(ticker, ch5m, ch1h, ch4h, ch24, rsi7, rsi14)

        # Hacim bilgisi
        vol_usdt = float(ticker.get("quoteVolume", 0))
        vol_str  = f"{vol_usdt/1_000_000:.1f}M" if vol_usdt >= 1_000_000 else f"{vol_usdt/1_000:.0f}K"

        text = (
            f"📊 *{extra_title}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Parite:* `#{symbol}`\n"
            f"💵 *Fiyat:* `{format_price(price)} USDT`\n"
            f"📦 *24s Hacim:* `{vol_str} USDT`\n\n"
            f"*📈 Performans Degisimleri:*\n"
            f"{e5} `5dk  :` `{s5}{ch5m:+.2f}%`\n"
            f"{e1} `1sa  :` `{s1}{ch1h:+.2f}%`\n"
            f"{e4} `4sa  :` `{s4}{ch4h:+.2f}%`\n"
            f"{e24} `24sa :` `{s24}{ch24:+.2f}%`\n\n"
            f"📉 *RSI Analizi:*\n"
            f"• RSI 7  : `{rsi7}` — {rsi_label(rsi7)}\n"
            f"• RSI 14 : `{rsi14}` — {rsi_label(rsi14)}\n\n"
            f"🎯 *Piyasa Skoru: {score}/100*\n"
            f"{score_bar}\n"
            f"_{score_label}_\n"
            f"──────────────────"
        )
        if threshold_info:
            text += f"\n🔔 *Alarm Esigi:* `%{threshold_info}`"

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
                caption=f"🕯️ *{symbol}* — 4 Saatlik Mum Grafigi | Skor: {score}/100 {score_bar}",
                parse_mode="Markdown"
            )

    except Exception as e:
        log.error(f"Gonderim hatasi ({symbol}): {e}")

# ================= ADMIN KONTROL =================

async def is_admin(update: Update, context) -> bool:
    """
    - Ozel mesaj (DM): her zaman True (admin kontrolu gerek yok)
    - Grupta: o grubun adminlerini kontrol et
    """
    chat = update.effective_chat
    # DM veya kanal ise direkt izin ver
    if chat.type == "private":
        return True
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.warning(f"Admin kontrol hatasi: {e}")
        return False

# ================= /set KOMUTU =================

SET_THRESHOLD_PRESETS = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0]

async def build_set_panel(context):
    """Panel metnini ve klavyesini oluşturur."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT alarm_active, threshold FROM groups WHERE chat_id=$1",
            GROUP_CHAT_ID
        )
    threshold    = r["threshold"]
    alarm_active = r["alarm_active"]

    threshold_buttons = []
    row = []
    for val in SET_THRESHOLD_PRESETS:
        label = f"{'✅ ' if threshold == val else ''}%{val:.0f}"
        row.append(InlineKeyboardButton(label, callback_data=f"set_threshold_{val}"))
        if len(row) == 4:
            threshold_buttons.append(row)
            row = []
    if row:
        threshold_buttons.append(row)

    threshold_buttons.append([
        InlineKeyboardButton("✏️ Manuel Gir", callback_data="set_threshold_custom")
    ])
    threshold_buttons.append([
        InlineKeyboardButton(
            f"🔔 Alarm: {'AKTİF ✅' if alarm_active else 'KAPALI ❌'}",
            callback_data="set_toggle_alarm"
        )
    ])
    threshold_buttons.append([
        InlineKeyboardButton("❌ Kapat", callback_data="set_close")
    ])

    text = (
        "⚙️ *Grup Ayarları — Admin Paneli*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTİF' if alarm_active else 'KAPALI'}`\n"
        f"🎯 *Mevcut Eşik:* `%{threshold}`\n\n"
        "Eşik seçin veya manuel girin:"
    )
    return text, InlineKeyboardMarkup(threshold_buttons)


async def set_command(update: Update, context):
    """/set komutu — sadece grup admini veya DM."""
    # DM'de her zaman izin ver, grupta admin kontrolü
    chat = update.effective_chat
    if chat.type != "private":
        try:
            member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text(
                    "🚫 *Bu komut sadece grup adminlerine açıktır.*",
                    parse_mode="Markdown"
                )
                return
        except Exception as e:
            log.warning(f"Admin kontrol: {e}")
            await update.message.reply_text("⚠️ Yetki kontrol edilemedi.", parse_mode="Markdown")
            return

    text, keyboard = await build_set_panel(context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def set_callback(update: Update, context):
    """set_ ile başlayan callback'leri işler."""
    q = update.callback_query

    # Her zaman GROUP_CHAT_ID üzerinden admin kontrolü
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, q.from_user.id)
        if member.status not in ("administrator", "creator"):
            await q.answer("🚫 Bu işlem sadece grup adminlerine açıktır.", show_alert=True)
            return
    except Exception as e:
        log.warning(f"set_callback admin kontrol: {e}")
        await q.answer("🚫 Yetki kontrol edilemedi.", show_alert=True)
        return

    await q.answer()

    # set_open: Paneli göster
    if q.data == "set_open":
        text, keyboard = await build_set_panel(context)
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    # ── Alarm toggle ──
    if q.data == "set_toggle_alarm":
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT alarm_active FROM groups WHERE chat_id=$1", GROUP_CHAT_ID
            )
            new_val = 0 if r["alarm_active"] else 1
            await conn.execute(
                "UPDATE groups SET alarm_active=$1 WHERE chat_id=$2",
                new_val, GROUP_CHAT_ID
            )
        durum = "AKTİF ✅" if new_val else "KAPALI ❌"
        await q.message.reply_text(
            f"🔔 Grup alarmı *{durum}* olarak ayarlandı.",
            parse_mode="Markdown"
        )
        await q.message.delete()
        return

    # ── Paneli kapat ──
    if q.data == "set_close":
        await q.message.delete()
        return

    # ── Manuel eşik girişi ──
    if q.data == "set_threshold_custom":
        context.user_data["awaiting_threshold"] = True
        await q.message.reply_text(
            "✏️ Yeni alarm eşiğini yazın (örnek: `4.5`):",
            parse_mode="Markdown"
        )
        await q.message.delete()
        return

    # ── Hazır eşik seçimi ──
    if q.data.startswith("set_threshold_"):
        try:
            val = float(q.data.replace("set_threshold_", ""))
        except ValueError:
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET threshold=$1 WHERE chat_id=$2",
                val, GROUP_CHAT_ID
            )

        await q.message.reply_text(
            f"✅ Alarm eşiği *%{val}* olarak güncellendi.\n"
            f"Artık 5 dakikada bu oranı aşan coinlerde alarm tetiklenecek.",
            parse_mode="Markdown"
        )
        await q.message.delete()


async def handle_threshold_input(update: Update, context):
    """Manuel eşik girişini yakalar."""
    if not context.user_data.get("awaiting_threshold"):
        return False  # Bu mesajı biz işlemedik

    # Admin kontrolü
    if not await is_admin(update, context):
        context.user_data.pop("awaiting_threshold", None)
        return True

    text = update.message.text.strip().replace(",", ".")
    try:
        val = float(text)
        if not (0.1 <= val <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Geçersiz değer. 0.1 ile 100 arasında bir sayı girin.\nÖrnek: `4.5`",
            parse_mode="Markdown"
        )
        return True

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE groups SET threshold=$1 WHERE chat_id=$2",
            val, GROUP_CHAT_ID
        )

    context.user_data.pop("awaiting_threshold", None)
    await update.message.reply_text(
        f"✅ Alarm eşiği *%{val}* olarak güncellendi!",
        parse_mode="Markdown"
    )
    return True

# ================= SEMBOL TEPKI =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return

    # Once manuel esik girisini kontrol et
    if await handle_threshold_input(update, context):
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


# ================= FAVORİLER =================

async def favori_command(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []
    msg     = update.callback_query.message if update.callback_query else update.message

    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol FROM favorites WHERE user_id=$1 ORDER BY symbol", user_id)
        if not rows:
            await msg.reply_text(
                "⭐ *Favori Listeniz Bos*\n━━━━━━━━━━━━━━━━━━\n"
                "Eklemek icin:\n`/favori ekle BTCUSDT`",
                parse_mode="Markdown"
            )
            return
        syms = [r["symbol"] for r in rows]
        text = "⭐ *Favorileriniz*\n━━━━━━━━━━━━━━━━━━\n"
        for s in syms:
            text += "• `" + s + "`\n"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Hepsini Analiz Et", callback_data="fav_analiz"),
            InlineKeyboardButton("🗑 Tumunu Sil",        callback_data="fav_deleteall_" + str(user_id))
        ]])
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if args[0].lower() == "ekle":
        if len(args) < 2:
            await msg.reply_text("Kullanim: `/favori ekle BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO favorites(user_id,symbol) VALUES($1,$2) ON CONFLICT DO NOTHING",
                user_id, symbol)
        await msg.reply_text("⭐ `" + symbol + "` favorilere eklendi!", parse_mode="Markdown")
        return

    if args[0].lower() == "sil":
        if len(args) < 2:
            await msg.reply_text("Kullanim: `/favori sil BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM favorites WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await msg.reply_text("🗑 `" + symbol + "` favorilerden silindi.", parse_mode="Markdown")
        return

    if args[0].lower() == "analiz":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM favorites WHERE user_id=$1", user_id)
        if not rows:
            await msg.reply_text("⭐ Favori listeniz bos.", parse_mode="Markdown"); return
        await msg.reply_text("📊 *" + str(len(rows)) + " coin analiz ediliyor...*", parse_mode="Markdown")
        for r in rows:
            await send_full_analysis(context.bot, update.effective_chat.id, r["symbol"], "⭐ FAVORİ ANALİZ")
            await asyncio.sleep(1.5)
        return

    await msg.reply_text(
        "Kullanim:\n`/favori ekle BTCUSDT`\n`/favori sil BTCUSDT`\n"
        "`/favori liste`\n`/favori analiz`",
        parse_mode="Markdown"
    )


# ================= GELİŞMİŞ KİŞİSEL ALARM =================

async def my_alarm_v2(update: Update, context):
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, threshold, alarm_type, rsi_level, band_low, band_high,
                   active, paused_until, trigger_count, last_triggered
            FROM user_alarms WHERE user_id=$1 ORDER BY symbol
        """, user_id)

    now = datetime.utcnow()

    if not rows:
        text = (
            "🔔 *Kisisel Alarm Paneli*\n━━━━━━━━━━━━━━━━━━\n"
            "Henuz alarm yok.\n\n"
            "Alarm turleri:\n"
            "• `%`  : `/alarm_ekle BTCUSDT 3.5`\n"
            "• RSI  : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant : `/alarm_ekle BTCUSDT bant 60000 70000`"
        )
    else:
        text = "🔔 *Kisisel Alarmlariniz*\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            if not r["active"]:
                durum = "⏹ Pasif"
            elif r["paused_until"] and r["paused_until"].replace(tzinfo=None) > now:
                durum = "⏸ " + r["paused_until"].strftime("%H:%M") + " UTC duraklat"
            else:
                durum = "✅ Aktif"

            atype = r["alarm_type"] or "percent"
            if atype == "rsi":
                detail = "RSI `" + str(r["rsi_level"]) + "`"
            elif atype == "band":
                detail = "Bant `" + format_price(r["band_low"]) + "-" + format_price(r["band_high"]) + "`"
            else:
                detail = "`%" + str(r["threshold"]) + "`"

            count = r["trigger_count"] or 0
            text += "• `" + r["symbol"] + "` " + detail + " — " + durum + " _" + str(count) + "x_\n"

        text += (
            "\n`/alarm_ekle` — ekle\n"
            "`/alarm_sil BTCUSDT` — sil\n"
            "`/alarm_duraklat BTCUSDT 2` — duraklat\n"
            "`/alarm_gecmis` — gecmis"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ekle",       callback_data="alarm_guide"),
         InlineKeyboardButton("📋 Gecmis",      callback_data="alarm_history")],
        [InlineKeyboardButton("🗑 Tumunu Sil", callback_data="alarm_deleteall_" + str(user_id)),
         InlineKeyboardButton("🔄 Yenile",      callback_data="my_alarm")]
    ])
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def alarm_ekle_v2(update: Update, context):
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    args     = context.args or []

    if len(args) < 2:
        await update.message.reply_text(
            "📌 *Alarm Turleri:*\n━━━━━━━━━━━━━━━━━━\n"
            "• `%`  : `/alarm_ekle BTCUSDT 3.5`\n"
            "• RSI  : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant : `/alarm_ekle BTCUSDT bant 60000 70000`",
            parse_mode="Markdown"
        )
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    # RSI alarmı
    if args[1].lower() == "rsi":
        if len(args) < 3:
            await update.message.reply_text(
                "Kullanim: `/alarm_ekle BTCUSDT rsi 30 asagi`", parse_mode="Markdown"); return
        try:    rsi_lvl = float(args[2])
        except:
            await update.message.reply_text("RSI degeri sayi olmali.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,rsi_level,active)
                VALUES($1,$2,$3,0,'rsi',$4,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='rsi', rsi_level=$4, threshold=0, active=1
            """, user_id, username, symbol, rsi_lvl)
        direction_str = "asagi" if len(args) < 4 or args[3].lower() in ("asagi","aşağı") else "yukari"
        yon_str = "altina dusunce" if direction_str == "asagi" else "ustune cikinca"
        await update.message.reply_text(
            "✅ *" + symbol + "* RSI `" + str(rsi_lvl) + "` " + yon_str + " alarm verilecek!",
            parse_mode="Markdown"
        )
        return

    # Bant alarmı
    if args[1].lower() == "bant":
        if len(args) < 4:
            await update.message.reply_text(
                "Kullanim: `/alarm_ekle BTCUSDT bant 60000 70000`", parse_mode="Markdown"); return
        try:
            band_low  = float(args[2].replace(",","."))
            band_high = float(args[3].replace(",","."))
        except:
            await update.message.reply_text("Fiyat degerleri sayi olmali.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,band_low,band_high,active)
                VALUES($1,$2,$3,0,'band',$4,$5,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='band', band_low=$4, band_high=$5, threshold=0, active=1
            """, user_id, username, symbol, band_low, band_high)
        await update.message.reply_text(
            "✅ *" + symbol + "* `" + format_price(band_low) + " - " + format_price(band_high) +
            " USDT` bandından cikinca alarm verilecek!",
            parse_mode="Markdown"
        )
        return

    # % alarmı
    try:    threshold = float(args[1])
    except:
        await update.message.reply_text("Esik sayi olmalidir. Ornek: `3.5`", parse_mode="Markdown"); return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,active)
            VALUES($1,$2,$3,$4,'percent',1)
            ON CONFLICT(user_id,symbol) DO UPDATE
            SET threshold=$4, alarm_type='percent', active=1
        """, user_id, username, symbol, threshold)
    await update.message.reply_text(
        "✅ *" + symbol + "* icin `%" + str(threshold) + "` alarmi eklendi!",
        parse_mode="Markdown"
    )


async def alarm_duraklat(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Kullanim: `/alarm_duraklat BTCUSDT 2` (saat)", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:    saat = float(args[1])
    except:
        await update.message.reply_text("Saat sayi olmali.", parse_mode="Markdown"); return
    until = datetime.utcnow() + timedelta(hours=saat)
    async with db_pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE user_alarms SET paused_until=$1 WHERE user_id=$2 AND symbol=$3",
            until, user_id, symbol
        )
    if r == "UPDATE 0":
        await update.message.reply_text("`" + symbol + "` icin alarm bulunamadi.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "⏸ *" + symbol + "* alarmi `" + str(int(saat)) + " saat` duraklatildi. "
            "Tekrar aktif: `" + until.strftime("%H:%M") + " UTC`",
            parse_mode="Markdown"
        )


async def alarm_gecmis(update: Update, context):
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, alarm_type, trigger_val, direction, triggered_at
            FROM alarm_history WHERE user_id=$1
            ORDER BY triggered_at DESC LIMIT 15
        """, user_id)
    if not rows:
        await update.message.reply_text(
            "📋 *Alarm Gecmisi*\n━━━━━━━━━━━━━━━━━━\nHenuz tetiklenen alarm yok.",
            parse_mode="Markdown"
        )
        return
    text = "📋 *Son 15 Alarm*\n━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        dt  = r["triggered_at"].strftime("%d.%m %H:%M")
        yon = "📈" if r["direction"] == "up" else "📉"
        if r["alarm_type"] == "rsi":
            detail = "RSI:" + str(round(r["trigger_val"], 1))
        elif r["alarm_type"] == "band":
            detail = "Bant cikisi"
        else:
            detail = "%" + str(round(r["trigger_val"], 2))
        text += yon + " `" + r["symbol"] + "` " + detail + "  `" + dt + "`\n"
    await update.message.reply_text(text, parse_mode="Markdown")


# ================= ÇOKLU ZAMAN DİLİMİ =================

async def mtf_command(update: Update, context):
    msg  = update.callback_query.message if update.callback_query else update.message
    args = context.args or []
    if not args:
        await msg.reply_text("Kullanim: `/mtf BTCUSDT`", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await msg.reply_text("⏳ Analiz yapiliyor...", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            k15m, k1h, k4h, k1d, k1w = await asyncio.gather(
                fetch_klines(session, symbol, "15m", limit=50),
                fetch_klines(session, symbol, "1h",  limit=50),
                fetch_klines(session, symbol, "4h",  limit=50),
                fetch_klines(session, symbol, "1d",  limit=50),
                fetch_klines(session, symbol, "1w",  limit=20),
            )

        def tf_row(data, label):
            if not data or len(data) < 5:
                return label, "❓", 0, 0
            ch  = calc_change(data)
            rsi = calc_rsi(data, 14)
            if   rsi >= 70: emoji = "🔴"
            elif rsi >= 55: emoji = "🟡"
            elif rsi <= 30: emoji = "🔵"
            elif rsi <= 45: emoji = "🟡"
            else:           emoji = "🟢"
            return label, emoji, rsi, ch

        rows = [
            tf_row(k15m, "15dk"),
            tf_row(k1h,  " 1sa"),
            tf_row(k4h,  " 4sa"),
            tf_row(k1d,  " 1gn"),
            tf_row(k1w,  " 1hf"),
        ]
        text = "📊 *" + symbol + " — Coklu Zaman Dilimi*\n━━━━━━━━━━━━━━━━━━\n"
        for label, emoji, rsi, ch in rows:
            yon = "📈" if ch > 0 else "📉" if ch < 0 else "↔️"
            text += emoji + " `" + label + "` RSI:`" + str(round(rsi,1)) + "` " + yon + "`" + ("+%.2f" % ch) + "%`\n"
        text += "\n_🔵 Asiri Satim  🟢 Normal  🔴 Asiri Alim_"

        await wait.delete()
        await msg.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await wait.delete()
        log.error("MTF hatasi: " + str(e))
        await msg.reply_text("⚠️ Analiz sirasinda hata olustu.", parse_mode="Markdown")


# ================= WHALE ALARMI =================

whale_vol_mem: dict = {}

async def whale_job(context):
    now = datetime.utcnow()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()

        for c in [x for x in data if x["symbol"].endswith("USDT")]:
            sym = c["symbol"]
            vol = float(c.get("quoteVolume", 0))
            if sym not in whale_vol_mem:
                whale_vol_mem[sym] = []
            whale_vol_mem[sym].append(vol)
            whale_vol_mem[sym] = whale_vol_mem[sym][-3:]
            if len(whale_vol_mem[sym]) < 2: continue

            prev, curr = whale_vol_mem[sym][-2], whale_vol_mem[sym][-1]
            if prev <= 0: continue
            pct = ((curr - prev) / prev) * 100
            if pct < 200 or curr < 10_000_000: continue

            key = "whale_" + sym
            if key in cooldowns and now - cooldowns[key] < timedelta(minutes=30): continue
            cooldowns[key] = now

            price = float(c["lastPrice"])
            ch24  = float(c["priceChangePercent"])
            text  = (
                "🐋 *WHALE ALARM!*\n━━━━━━━━━━━━━━━━━━\n"
                "💎 *" + sym + "*\n"
                "💵 Fiyat: `" + format_price(price) + " USDT`\n"
                "📦 Hacim: `" + ("%.1f" % (curr/1_000_000)) + "M USDT`\n"
                "📈 Hacim Artisi: `+" + ("%.0f" % pct) + "%`\n"
                "🔄 24s: `" + ("%+.2f" % ch24) + "%`\n"
                "_Buyuk oyuncu hareketi!_"
            )
            await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error("Whale job: " + str(e))


# ================= HAFTALIK RAPOR + ZAMANLANMIŞ =================

scheduled_last_run: dict = {}

async def send_weekly_report(bot, chat_id):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        usdt    = [x for x in data if x["symbol"].endswith("USDT")]
        top5    = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]
        bot5    = sorted(usdt, key=lambda x: float(x["priceChangePercent"]))[:5]
        avg     = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
        mood    = "🐂 Boga" if avg > 1 else "🐻 Ayi" if avg < -1 else "😐 Yatay"
        now_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y")

        text = (
            "📅 *Haftalik Kripto Raporu*\n━━━━━━━━━━━━━━━━━━\n"
            "🗓 " + now_str + " · " + mood + "\n"
            "📊 Ort. Degisim: `" + ("%+.2f" % avg) + "%`\n\n"
            "🚀 *En Cok Yukselen 5*\n"
        )
        for i, c in enumerate(top5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🟢 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n📉 *En Cok Dusen 5*\n"
        for i, c in enumerate(bot5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🔴 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n_Iyi haftalar! 🎯_"
        await bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception as e:
        log.error("Haftalik rapor: " + str(e))


async def zamanla_command(update: Update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args    = context.args or []

    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT task_type, symbol, hour, minute FROM scheduled_tasks WHERE chat_id=$1 AND active=1",
                chat_id)
        if not rows:
            await update.message.reply_text(
                "⏰ *Zamanlanmis Gorevler*\n━━━━━━━━━━━━━━━━━━\nGorev yok.\n\n"
                "Eklemek icin:\n`/zamanla analiz BTCUSDT 09:00`\n`/zamanla rapor 08:00`",
                parse_mode="Markdown")
        else:
            text = "⏰ *Zamanlanmis Gorevler*\n━━━━━━━━━━━━━━━━━━\n"
            for r in rows:
                sym_str = "`" + r["symbol"] + "` " if r["symbol"] else ""
                text += "• " + r["task_type"] + " " + sym_str + "— `" + ("%02d:%02d" % (r["hour"],r["minute"])) + "` UTC\n"
            await update.message.reply_text(text, parse_mode="Markdown")
        return

    if args[0].lower() == "sil":
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE scheduled_tasks SET active=0 WHERE chat_id=$1", chat_id)
        await update.message.reply_text("🗑 Gorevler silindi.", parse_mode="Markdown"); return

    if args[0].lower() == "analiz" and len(args) >= 3:
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        try:    h, m = map(int, args[2].split(":"))
        except:
            await update.message.reply_text("Saat formati: `09:00`", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scheduled_tasks(user_id,chat_id,task_type,symbol,hour,minute,active)
                VALUES($1,$2,'analiz',$3,$4,$5,1)
                ON CONFLICT(chat_id,task_type,symbol) DO UPDATE SET hour=$4,minute=$5,active=1
            """, user_id, chat_id, symbol, h, m)
        await update.message.reply_text(
            "⏰ Her gun `" + ("%02d:%02d" % (h,m)) + "` UTC'de *" + symbol + "* analizi gonderilecek!",
            parse_mode="Markdown"); return

    if args[0].lower() == "rapor" and len(args) >= 2:
        try:    h, m = map(int, args[1].split(":"))
        except:
            await update.message.reply_text("Saat formati: `08:00`", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scheduled_tasks(user_id,chat_id,task_type,symbol,hour,minute,active)
                VALUES($1,$2,'rapor','',$3,$4,1)
                ON CONFLICT(chat_id,task_type,symbol) DO UPDATE SET hour=$3,minute=$4,active=1
            """, user_id, chat_id, h, m)
        await update.message.reply_text(
            "⏰ Her Pazartesi `" + ("%02d:%02d" % (h,m)) + "` UTC'de haftalik rapor gonderilecek!",
            parse_mode="Markdown"); return

    await update.message.reply_text(
        "Kullanim:\n`/zamanla analiz BTCUSDT 09:00`\n`/zamanla rapor 08:00`\n"
        "`/zamanla liste`\n`/zamanla sil`",
        parse_mode="Markdown")


async def scheduled_job(context):
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("SELECT * FROM scheduled_tasks WHERE active=1")
    for t in tasks:
        if t["hour"] != now.hour or t["minute"] != now.minute: continue
        run_key = str(t["id"]) + "_" + str(now.date()) + "_" + str(now.hour) + "_" + str(now.minute)
        if run_key in scheduled_last_run: continue
        scheduled_last_run[run_key] = True
        if t["task_type"] == "analiz" and t["symbol"]:
            await send_full_analysis(context.bot, t["chat_id"], t["symbol"], "⏰ ZAMANLANMIS ANALİZ")
        elif t["task_type"] == "rapor" and now.weekday() == 0:
            await send_weekly_report(context.bot, t["chat_id"])

# ================= KOMUTLAR =================

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market",          callback_data="market"),
         InlineKeyboardButton("⚡ 5dk Flashlar",    callback_data="top5")],
        [InlineKeyboardButton("📈 24s Liderleri",   callback_data="top24"),
         InlineKeyboardButton("⚙️ Durum",           callback_data="status")],
        [InlineKeyboardButton("🔔 Alarmlarim",      callback_data="my_alarm"),
         InlineKeyboardButton("⭐ Favorilerim",     callback_data="fav_liste")],
        [InlineKeyboardButton("📊 MTF Analiz",      callback_data="mtf_help"),
         InlineKeyboardButton("📅 Zamanla",         callback_data="zamanla_help")],
        [InlineKeyboardButton("🛠 Admin Ayarlari",  callback_data="set_open")]
    ])
    welcome_text = (
        "👋 *Kripto Analiz Asistani*\n━━━━━━━━━━━━━━━━━━\n"
        "7/24 piyasayi izliyorum.\n\n"
        "💡 Analiz: `BTCUSDT` yaz\n"
        "🔔 % Alarm: `/alarm_ekle BTCUSDT 3.5`\n"
        "📉 RSI Alarm: `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
        "📊 Bant Alarm: `/alarm_ekle BTCUSDT bant 60000 70000`\n"
        "⭐ Favori: `/favori ekle BTCUSDT`\n"
        "⏰ Zamanla: `/zamanla analiz BTCUSDT 09:00`"
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
        usdt_list = [x for x in data if x["symbol"].endswith("USDT")]
        positives = sorted(usdt_list, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]
        negatives = sorted(usdt_list, key=lambda x: float(x["priceChangePercent"]))[:5]

        text = "⚡ *Piyasanin En Hareketlileri (24s baz)*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🟢 *YUKSELENLER*\n"
        for i, c in enumerate(positives, 1):
            pct = float(c["priceChangePercent"])
            text += f"{get_number_emoji(i)} 🟢▲ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
        text += "\n🔴 *DUSENLER*\n"
        for i, c in enumerate(negatives, 1):
            pct = float(c["priceChangePercent"])
            text += f"{get_number_emoji(i)} 🔴▼ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
        text += "\n_⏳ WebSocket verisi henuz doluyor..._"
    else:
        changes = []
        for s, p in price_memory.items():
            if len(p) >= 2:
                changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))

        positives = sorted([x for x in changes if x[1] > 0], key=lambda x: x[1], reverse=True)[:5]
        negatives = sorted([x for x in changes if x[1] < 0], key=lambda x: x[1])[:5]

        text = "⚡ *Son 5 Dakikanin En Hareketlileri*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🟢 *YUKSELENLER — En Hizli 5*\n"
        for i, (s, c) in enumerate(positives, 1):
            text += f"{get_number_emoji(i)} 🟢▲ `{s:<12}` `%{c:+6.2f}`\n"
        if not positives:
            text += "_Yukseliş yok_\n"
        text += "\n🔴 *DUSENLER — En Hizli 5*\n"
        for i, (s, c) in enumerate(negatives, 1):
            text += f"{get_number_emoji(i)} 🔴▼ `{s:<12}` `%{c:+6.2f}`\n"
        if not negatives:
            text += "_Dusus yok_\n"

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

    # set_ callbacklerini ayri handler'a yonlendir
    if q.data.startswith("set_"):
        await set_callback(update, context)
        return

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
    elif q.data == "fav_liste":
        await favori_command(update, context)
    elif q.data == "fav_analiz":
        user_id = q.from_user.id
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM favorites WHERE user_id=$1", user_id)
        if not rows:
            await q.message.reply_text("⭐ Favori listeniz bos.", parse_mode="Markdown")
        else:
            await q.message.reply_text(f"📊 *{len(rows)} coin analiz ediliyor...*", parse_mode="Markdown")
            for r in rows:
                await send_full_analysis(context.bot, q.message.chat.id, r["symbol"], "⭐ FAVORİ ANALİZ")
                await asyncio.sleep(1.5)
    elif q.data.startswith("fav_deleteall_"):
        uid = int(q.data.split("_")[-1])
        if q.from_user.id == uid:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM favorites WHERE user_id=$1", uid)
            await q.message.reply_text("🗑 Tum favorileriniz silindi.")
    elif q.data == "mtf_help":
        await q.message.reply_text(
            "📊 *Coklu Zaman Dilimi Analizi*\n━━━━━━━━━━━━━━━━━━\n"
            "Kullanim: `/mtf BTCUSDT`\n\n"
            "15dk · 1sa · 4sa · 1gn · 1hf\n"
            "RSI ve trend yonunu gosterir.",
            parse_mode="Markdown"
        )
    elif q.data == "zamanla_help":
        await q.message.reply_text(
            "⏰ *Zamanlanmis Gorevler*\n━━━━━━━━━━━━━━━━━━\n"
            "`/zamanla analiz BTCUSDT 09:00`\n"
            "`/zamanla rapor 08:00`\n"
            "`/zamanla liste`\n`/zamanla sil`",
            parse_mode="Markdown"
        )
    elif q.data == "alarm_history":
        await alarm_gecmis(update, context)
    elif q.data == "set_open":
        # Grup ise admin kontrolü yap
        if q.message.chat.type != "private":
            try:
                member = await context.bot.get_chat_member(q.message.chat.id, q.from_user.id)
                if member.status not in ("administrator", "creator"):
                    await q.message.reply_text(
                        "🚫 *Bu panel sadece grup adminlerine açıktır.*",
                        parse_mode="Markdown"
                    )
                    return
            except Exception as e:
                log.warning(f"set_open admin kontrol: {e}")
                return
        # Paneli doğrudan gönder — FakeUpdate yok
        text, keyboard = await build_set_panel(context)
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

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

    # ── Grup alarmları ──
    if group_row and group_row["alarm_active"]:
        threshold = group_row["threshold"]
        mode      = group_row["mode"]
        for symbol, prices in list(price_memory.items()):
            if len(prices) < 2:
                continue
            ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
            if mode == "both":   triggered = abs(ch5) >= threshold
            elif mode == "up":   triggered = ch5 >= threshold
            elif mode == "down": triggered = ch5 <= -threshold
            else:                triggered = abs(ch5) >= threshold
            if not triggered:
                continue
            key = f"group_{symbol}"
            if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
                continue
            cooldowns[key] = now
            yon = "📈 5dk YUKSELIS UYARISI" if ch5 > 0 else "📉 5dk DUSUS UYARISI"
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, yon, threshold)

    # ── Kişisel alarmlar (gelişmiş) ──
    for row in user_rows:
        symbol     = row["symbol"]
        user_id    = row["user_id"]
        threshold  = row["threshold"]
        alarm_type = row.get("alarm_type", "percent")
        rsi_level  = row.get("rsi_level")
        band_low   = row.get("band_low")
        band_high  = row.get("band_high")
        paused     = row.get("paused_until")

        # Duraklatma kontrolü
        if paused and paused.replace(tzinfo=None) > now:
            continue

        prices = price_memory.get(symbol)
        if not prices or len(prices) < 2:
            continue

        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
        triggered = False
        direction = "up" if ch5 > 0 else "down"

        if alarm_type == "percent":
            triggered = abs(ch5) >= threshold
        elif alarm_type == "rsi" and rsi_level is not None:
            try:
                async with aiohttp.ClientSession() as sess:
                    kdata = await fetch_klines(sess, symbol, "1h", limit=50)
                rsi_now = calc_rsi(kdata, 14)
                triggered = rsi_now <= rsi_level or rsi_now >= (100 - rsi_level)
                direction = "down" if rsi_now <= rsi_level else "up"
            except:
                pass
        elif alarm_type == "band" and band_low is not None and band_high is not None:
            cur_price = prices[-1][1]
            triggered = cur_price < band_low or cur_price > band_high
            direction = "down" if cur_price < band_low else "up"

        if not triggered:
            continue

        key = f"user_{user_id}_{symbol}"
        if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES):
            continue
        cooldowns[key] = now

        # DB: trigger sayacını artır + geçmişe kaydet
        trigger_val = ch5 if alarm_type == "percent" else (rsi_level or 0)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE user_alarms SET trigger_count=COALESCE(trigger_count,0)+1, last_triggered=$1 WHERE user_id=$2 AND symbol=$3",
                    now, user_id, symbol
                )
                await conn.execute(
                    "INSERT INTO alarm_history(user_id,symbol,alarm_type,trigger_val,direction) VALUES($1,$2,$3,$4,$5)",
                    user_id, symbol, alarm_type, trigger_val, direction
                )
                # Akıllı tekrar önerisi: 5+ kez tetiklendiyse
                count_row = await conn.fetchrow(
                    "SELECT trigger_count, threshold FROM user_alarms WHERE user_id=$1 AND symbol=$2",
                    user_id, symbol
                )
                suggest_msg = ""
                if count_row and (count_row["trigger_count"] or 0) >= 5 and alarm_type == "percent":
                    yeni_esik = round((count_row["threshold"] or threshold) * 1.5, 1)
                    suggest_msg = (
                        "\n\n💡 *Akilli Oneri:* `" + symbol + "` alarminiz 5 kez tetiklendi.\n"
                        "Esigi `%" + str(yeni_esik) + "` yapmayi dusunebilirsiniz.\n"
                        "`/alarm_ekle " + symbol + " " + str(yeni_esik) + "`"
                    )
        except Exception as e:
            log.warning(f"Alarm DB guncelleme: {e}")
            suggest_msg = ""

        yon = "📈" if direction == "up" else "📉"
        try:
            await send_full_analysis(
                context.bot, user_id, symbol,
                f"🔔 KISISEL ALARM {yon} — {symbol}", threshold
            )
            if suggest_msg:
                await context.bot.send_message(user_id, suggest_msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Kisisel alarm gonderilemedi ({user_id}): {e}")

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

    app.job_queue.run_repeating(alarm_job,       interval=10,   first=30)
    app.job_queue.run_repeating(whale_job,       interval=120,  first=60)
    app.job_queue.run_repeating(scheduled_job,   interval=60,   first=10)

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("top24",          top24))
    app.add_handler(CommandHandler("top5",           top5))
    app.add_handler(CommandHandler("market",         market))
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("alarmim",        my_alarm_v2))
    app.add_handler(CommandHandler("alarm_ekle",     alarm_ekle_v2))
    app.add_handler(CommandHandler("alarm_sil",      alarm_sil))
    app.add_handler(CommandHandler("alarm_duraklat", alarm_duraklat))
    app.add_handler(CommandHandler("alarm_gecmis",   alarm_gecmis))
    app.add_handler(CommandHandler("favori",         favori_command))
    app.add_handler(CommandHandler("mtf",            mtf_command))
    app.add_handler(CommandHandler("zamanla",        zamanla_command))
    app.add_handler(CommandHandler("set",            set_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("BOT AKTIF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
