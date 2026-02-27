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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

# ═══════════════════════════════════════════
#                   CONFIG
# ═══════════════════════════════════════════

TOKEN         = os.getenv("TELEGRAM_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_ID"))
DATABASE_URL  = os.getenv("DATABASE_URL")

BINANCE_24H    = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
COINGECKO_TREND= "https://api.coingecko.com/api/v3/search/trending"

COOLDOWN_MINUTES  = 15
DEFAULT_THRESHOLD = 5.0
MAX_SYMBOLS       = 500
DAILY_SUMMARY_HOUR = 8   # UTC — sabah özeti saati

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#                  DATABASE
# ═══════════════════════════════════════════

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
            CREATE TABLE IF NOT EXISTS portfolios (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                symbol     TEXT,
                amount     REAL,
                avg_cost   REAL,
                UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            INSERT INTO groups (chat_id, threshold, mode)
            VALUES ($1, $2, $3) ON CONFLICT (chat_id) DO NOTHING
        """, GROUP_CHAT_ID, DEFAULT_THRESHOLD, "both")
    log.info("✅ PostgreSQL hazır.")

# ═══════════════════════════════════════════
#                   MEMORY
# ═══════════════════════════════════════════

price_memory: dict = {}
cooldowns:    dict = {}
chart_cache:  dict = {}

# ═══════════════════════════════════════════
#                  YARDIMCI
# ═══════════════════════════════════════════

def get_number_emoji(n):
    emojis = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",
              6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟",
              11:"11",12:"12",13:"13",14:"14",15:"15",
              16:"16",17:"17",18:"18",19:"19",20:"20"}
    return emojis.get(n, f"{n}.")

def format_price(price):
    return f"{price:,.4f}" if price < 1 else f"{price:,.2f}"

def trend_bar(val, width=10):
    """RSI/FG için görsel bar."""
    filled = int(abs(val) / 100 * width)
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)

async def is_admin(update: Update, context) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.warning(f"Admin kontrol: {e}")
        return False

# ═══════════════════════════════════════════
#             MUM GRAFİĞİ (önbellekli)
# ═══════════════════════════════════════════

async def generate_candlestick_chart(symbol: str, interval: str = "4h"):
    cache_key = f"{symbol}_{interval}"
    if cache_key in chart_cache:
        cached_at, buf = chart_cache[cache_key]
        if datetime.utcnow() - cached_at < timedelta(minutes=5):
            buf.seek(0)
            return buf
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit=60",
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

        # RSI hesapla (14) — alt panel için
        closes = df["close"].tolist()
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(abs(min(d, 0)))
        period = 14
        rsi_vals = [float("nan")] * len(closes)
        if len(gains) >= period:
            ag = sum(gains[:period]) / period
            al = sum(losses[:period]) / period
            for i in range(period, len(closes)):
                ag = (ag * (period-1) + gains[i-1]) / period
                al = (al * (period-1) + losses[i-1]) / period
                rs = ag / al if al != 0 else 100
                rsi_vals[i] = round(100 - (100/(1+rs)), 2)
        rsi_series = pd.Series(rsi_vals, index=df.index)

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

        apds = [
            mpf.make_addplot(rsi_series, panel=2, color="#f0a500",
                             ylabel="RSI", secondary_y=False, width=1.2),
        ]

        buf = io.BytesIO()
        interval_label = {"4h":"4 Saatlik","1h":"1 Saatlik","1d":"Günlük"}.get(interval, interval)
        mpf.plot(
            df, type="candle", style=style,
            title=f"\n{symbol}  ·  {interval_label} Mum Grafiği  (Son 60 Mum)",
            ylabel="Fiyat (USDT)", volume=True,
            addplot=apds,
            panel_ratios=(4, 1, 1.5),
            figsize=(14, 9),
            savefig=dict(fname=buf, format="png", bbox_inches="tight", dpi=150),
        )
        buf.seek(0)
        chart_cache[cache_key] = (datetime.utcnow(), buf)
        return buf
    except Exception as e:
        log.error(f"Grafik hatası ({symbol}/{interval}): {e}")
        return None

# ═══════════════════════════════════════════
#           ANALİZ — HESAPLAMALAR
# ═══════════════════════════════════════════

async def fetch_klines(session, symbol, interval, limit=2):
    try:
        async with session.get(
            f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            return await resp.json()
    except Exception as e:
        log.warning(f"Klines {symbol}/{interval}: {e}")
        return []

def calc_change(data):
    if not data or len(data) < 2:
        return 0.0
    return round(((float(data[-1][4]) - float(data[0][4])) / float(data[0][4])) * 100, 2)

def calc_rsi(data, period=14):
    try:
        closes = [float(x[4]) for x in data]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(abs(min(d, 0)))
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        if al == 0:
            return 100.0
        return round(100 - (100 / (1 + ag/al)), 2)
    except:
        return 0.0

def rsi_label(rsi):
    if rsi >= 70:   return "🔴 Aşırı Alım"
    if rsi >= 60:   return "🟡 Yüksek"
    if rsi >= 40:   return "🟢 Normal"
    if rsi >= 30:   return "🟡 Düşük"
    return              "🔵 Aşırı Satım"

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

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None, interval="4h"):
    try:
        ticker, k4h, k1h, k5m, krsi = await fetch_all_analysis(symbol)
        if "lastPrice" not in ticker:
            return

        price  = float(ticker["lastPrice"])
        ch24   = float(ticker["priceChangePercent"])
        ch4h   = calc_change(k4h)
        ch1h   = calc_change(k1h)
        ch5m   = calc_change(k5m)
        vol24  = float(ticker.get("quoteVolume", 0))
        rsi7   = calc_rsi(krsi, 7)
        rsi14  = calc_rsi(krsi, 14)

        def ui(v): return ("🟢","+") if v>0 else ("🔴","") if v<0 else ("⚪","")
        e5,s5   = ui(ch5m);  e1,s1 = ui(ch1h)
        e4,s4   = ui(ch4h);  e24,s24 = ui(ch24)

        vol_str = f"{vol24/1_000_000:.1f}M" if vol24 >= 1_000_000 else f"{vol24/1_000:.1f}K"

        text = (
            f"{'🚨' if threshold_info else '📊'} *{extra_title}*\n"
            f"{'━'*20}\n"
            f"💎 *{symbol}*   💵 `{format_price(price)} USDT`\n"
            f"📦 *Hacim 24s:* `{vol_str} USDT`\n"
            f"{'─'*20}\n"
            f"*⏱ Performans:*\n"
            f"{e5} `5dk  ` `{s5}{ch5m:+.2f}%`\n"
            f"{e1} `1sa  ` `{s1}{ch1h:+.2f}%`\n"
            f"{e4} `4sa  ` `{s4}{ch4h:+.2f}%`\n"
            f"{e24} `24sa ` `{s24}{ch24:+.2f}%`\n"
            f"{'─'*20}\n"
            f"*📉 RSI Göstergesi:*\n"
            f"RSI‑7  : `{rsi7:5.1f}`  {rsi_label(rsi7)}\n"
            f"RSI‑14 : `{rsi14:5.1f}`  {rsi_label(rsi14)}\n"
        )
        if threshold_info:
            text += f"{'─'*20}\n🎯 *Alarm Eşiği:* `%{threshold_info}`\n"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📈 Binance'de Aç",
                url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"),
            InlineKeyboardButton("🔄 Yenile", callback_data=f"refresh_{symbol}")
        ]])

        await bot.send_message(chat_id=chat_id, text=text,
                               reply_markup=keyboard, parse_mode="Markdown")

        chart_buf = await generate_candlestick_chart(symbol, interval)
        if chart_buf:
            await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(chart_buf, filename=f"{symbol}_{interval}.png"),
                caption=f"🕯 *{symbol}* — Mum Grafiği + RSI",
                parse_mode="Markdown"
            )
    except Exception as e:
        log.error(f"Analiz gönderilemedi ({symbol}): {e}")

# ═══════════════════════════════════════════
#           KORKU & AÇGÖZLÜLÜK
# ═══════════════════════════════════════════

async def get_fear_greed():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(FEAR_GREED_URL, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        d = data["data"][0]
        val        = int(d["value"])
        classif    = d["value_classification"]
        timestamp  = d["timestamp"]
        dt         = datetime.fromtimestamp(int(timestamp)).strftime("%d.%m.%Y")
        return val, classif, dt
    except Exception as e:
        log.error(f"Fear&Greed: {e}")
        return None, None, None

async def fear_greed_command(update: Update, context):
    val, classif, dt = await get_fear_greed()
    if val is None:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("⚠️ Veri alınamadı.", parse_mode="Markdown")
        return

    if val <= 25:    mood, emoji = "Aşırı Korku",    "😱"
    elif val <= 45:  mood, emoji = "Korku",           "😰"
    elif val <= 55:  mood, emoji = "Nötr",            "😐"
    elif val <= 75:  mood, emoji = "Açgözlülük",      "😏"
    else:            mood, emoji = "Aşırı Açgözlülük","🤑"

    bar = trend_bar(val, 14)
    text = (
        f"{emoji} *Kripto Korku & Açgözlülük Endeksi*\n"
        f"{'━'*22}\n"
        f"📅 *Tarih:* `{dt}`\n\n"
        f"Endeks Değeri: `{val}/100`\n"
        f"`{bar}`\n\n"
        f"Durum: *{mood}*\n\n"
        f"_0 = Aşırı Korku · 100 = Aşırı Açgözlülük_\n"
        f"_Yüksek korku → alım fırsatı sinyali_\n"
        f"_Yüksek açgözlülük → dikkatli olun_"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Güncelle", callback_data="fear_greed")
    ]])
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ═══════════════════════════════════════════
#           COİN TARAYICI (RSI bazlı)
# ═══════════════════════════════════════════

async def scanner_command(update: Update, context):
    msg = update.callback_query.message if update.callback_query else update.message
    wait_msg = await msg.reply_text("🔍 *Piyasa taranıyor... lütfen bekleyin* ⏳", parse_mode="Markdown")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Aşırı Satım (RSI<30)", callback_data="scan_oversold"),
         InlineKeyboardButton("🔴 Aşırı Alım (RSI>70)",  callback_data="scan_overbought")],
        [InlineKeyboardButton("🚀 En Çok Yükselen",      callback_data="scan_top_gain"),
         InlineKeyboardButton("📉 En Çok Düşen",          callback_data="scan_top_loss")],
        [InlineKeyboardButton("❌ Kapat", callback_data="scan_close")]
    ])
    await wait_msg.delete()
    await msg.reply_text(
        "🔍 *Coin Tarayıcı*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Ne aramak istiyorsun?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def run_rsi_scan(bot, chat_id, mode: str):
    """mode: oversold | overbought"""
    wait = await bot.send_message(chat_id, "⏳ *Tarama yapılıyor... (30-60 sn sürebilir)*", parse_mode="Markdown")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                all_tickers = await resp.json()

        # Hacme göre top 80 USDT paritesi — hepsini taramak çok uzun sürer
        candidates = sorted(
            [x for x in all_tickers if x["symbol"].endswith("USDT")],
            key=lambda x: float(x.get("quoteVolume", 0)), reverse=True
        )[:80]

        async def check_rsi(ticker):
            sym = ticker["symbol"]
            try:
                async with aiohttp.ClientSession() as s:
                    data = await fetch_klines(s, sym, "1h", limit=100)
                rsi = calc_rsi(data, 14)
                return sym, rsi, float(ticker["priceChangePercent"])
            except:
                return sym, 50.0, 0.0

        results = await asyncio.gather(*[check_rsi(t) for t in candidates])

        if mode == "oversold":
            filtered = [(s, r, c) for s, r, c in results if r < 30]
            filtered.sort(key=lambda x: x[1])
            title = "🔵 *Aşırı Satım Bölgesindeki Coinler* (RSI < 30)"
            hint  = "_Potansiyel alım bölgesi — kendi araştırmanızı yapın_"
        else:
            filtered = [(s, r, c) for s, r, c in results if r > 70]
            filtered.sort(key=lambda x: x[1], reverse=True)
            title = "🔴 *Aşırı Alım Bölgesindeki Coinler* (RSI > 70)"
            hint  = "_Potansiyel satış bölgesi — dikkatli olun_"

        await wait.delete()

        if not filtered:
            await bot.send_message(chat_id, f"{title}\n\n_Şu an uygun coin bulunamadı._", parse_mode="Markdown")
            return

        text = f"{title}\n{'━'*22}\n"
        for i, (sym, rsi, ch) in enumerate(filtered[:15], 1):
            e = "🔵" if mode == "oversold" else "🔴"
            text += f"{e} `{sym:<14}` RSI:`{rsi:4.1f}`  `{ch:+.2f}%`\n"
        text += f"\n{hint}"

        await bot.send_message(chat_id, text, parse_mode="Markdown")

    except Exception as e:
        await wait.delete()
        log.error(f"RSI tarama hatası: {e}")
        await bot.send_message(chat_id, "⚠️ Tarama sırasında hata oluştu.", parse_mode="Markdown")

async def run_price_scan(bot, chat_id, mode: str):
    """mode: gain | loss"""
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    reverse = mode == "gain"
    top = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=reverse)[:20]

    title = "🚀 *24s En Çok Yükselen — Top 20*" if mode == "gain" else "📉 *24s En Çok Düşen — Top 20*"
    text = f"{title}\n{'━'*22}\n"
    for i, c in enumerate(top, 1):
        sym = c["symbol"]
        ch  = float(c["priceChangePercent"])
        e   = "🟢" if ch > 0 else "🔴"
        num = get_number_emoji(i) if i <= 10 else f"`{i:2d}.`"
        text += f"{num} `{sym:<14}` {e} `{ch:+.2f}%`\n"

    await bot.send_message(chat_id, text, parse_mode="Markdown")

# ═══════════════════════════════════════════
#             PORTFÖY TAKİBİ
# ═══════════════════════════════════════════

async def portfolio_command(update: Update, context):
    """
    Kullanım:
      /portfoy               — portföyü görüntüle
      /portfoy ekle BTC 0.5 65000
      /portfoy sil BTC
    """
    user_id = update.effective_user.id
    args    = context.args

    if args and args[0].lower() == "ekle":
        if len(args) < 4:
            await update.message.reply_text(
                "📝 *Kullanım:*\n`/portfoy ekle BTC 0.5 65000`\n"
                "_(sembol, miktar, ortalama maliyet)_",
                parse_mode="Markdown"
            )
            return
        symbol   = args[1].upper().replace("USDT","")
        try:
            amount   = float(args[2])
            avg_cost = float(args[3])
        except ValueError:
            await update.message.reply_text("⚠️ Miktar ve maliyet sayı olmalıdır.", parse_mode="Markdown")
            return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO portfolios (user_id, symbol, amount, avg_cost)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (user_id, symbol)
                DO UPDATE SET amount=$3, avg_cost=$4
            """, user_id, symbol, amount, avg_cost)
        await update.message.reply_text(
            f"✅ *{symbol}* portföye eklendi!\n"
            f"Miktar: `{amount}` · Ort. Maliyet: `{format_price(avg_cost)} USDT`",
            parse_mode="Markdown"
        )
        return

    if args and args[0].lower() == "sil":
        if len(args) < 2:
            await update.message.reply_text("Kullanım: `/portfoy sil BTC`", parse_mode="Markdown")
            return
        symbol = args[1].upper().replace("USDT","")
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM portfolios WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await update.message.reply_text(f"🗑 `{symbol}` portföyden silindi.", parse_mode="Markdown")
        return

    # Portföyü göster
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT symbol, amount, avg_cost FROM portfolios WHERE user_id=$1", user_id)

    if not rows:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Nasıl Eklenir?", callback_data="portfolio_guide")
        ]])
        await update.message.reply_text(
            "💼 *Portföyünüz Boş*\n━━━━━━━━━━━━━━━━━━\n"
            "Coin eklemek için:\n`/portfoy ekle BTC 0.5 65000`\n"
            "_(sembol · miktar · ortalama alış fiyatı)_",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # Anlık fiyatları paralel çek
    symbols_usdt = [f"{r['symbol']}USDT" for r in rows]
    async def get_price(sym):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{BINANCE_24H}?symbol={sym}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    d = await resp.json()
            return sym.replace("USDT",""), float(d.get("lastPrice", 0))
        except:
            return sym.replace("USDT",""), 0.0

    prices = dict(await asyncio.gather(*[get_price(s) for s in symbols_usdt]))

    text = "💼 *Portföy Özeti*\n" + "━"*22 + "\n"
    total_cost = 0.0
    total_now  = 0.0

    for r in rows:
        sym       = r["symbol"]
        amount    = r["amount"]
        avg_cost  = r["avg_cost"]
        cur_price = prices.get(sym, 0)
        cost      = amount * avg_cost
        now_val   = amount * cur_price
        pnl       = now_val - cost
        pnl_pct   = ((cur_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        total_cost += cost
        total_now  += now_val

        text += (
            f"\n*{sym}*\n"
            f"  Miktar : `{amount}`\n"
            f"  Alış   : `{format_price(avg_cost)} USDT`\n"
            f"  Şimdi  : `{format_price(cur_price)} USDT`\n"
            f"  K/Z    : {pnl_emoji} `{pnl:+.2f} USDT  ({pnl_pct:+.2f}%)`\n"
        )

    total_pnl     = total_now - total_cost
    total_pnl_pct = ((total_now - total_cost) / total_cost * 100) if total_cost > 0 else 0
    total_emoji   = "🟢" if total_pnl >= 0 else "🔴"

    text += (
        f"\n{'━'*22}\n"
        f"📊 *Toplam Maliyet:* `{total_cost:,.2f} USDT`\n"
        f"💵 *Anlık Değer:*    `{total_now:,.2f} USDT`\n"
        f"{total_emoji} *Toplam K/Z:*      `{total_pnl:+,.2f} USDT  ({total_pnl_pct:+.2f}%)`"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Güncelle", callback_data="portfolio_refresh"),
        InlineKeyboardButton("📝 Ekle",     callback_data="portfolio_guide")
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ═══════════════════════════════════════════
#         SABAH ÖZETİ + TRENDING
# ═══════════════════════════════════════════

async def get_trending_coins():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COINGECKO_TREND, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
        coins = data.get("coins", [])[:7]
        return [(c["item"]["symbol"].upper(), c["item"]["name"]) for c in coins]
    except Exception as e:
        log.warning(f"Trending: {e}")
        return []

async def send_daily_summary(bot, chat_id):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()

        usdt = [x for x in data if x["symbol"].endswith("USDT")]
        avg  = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
        top5_gain = sorted(usdt, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]
        top5_loss = sorted(usdt, key=lambda x: float(x["priceChangePercent"]))[:5]

        fg_val, fg_class, fg_dt = await get_fear_greed()
        trending = await get_trending_coins()

        mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"

        now_tr = datetime.utcnow() + timedelta(hours=3)
        text = (
            f"☀️ *Günaydın! Günlük Piyasa Özeti*\n"
            f"📅 {now_tr.strftime('%d.%m.%Y')} · {now_tr.strftime('%H:%M')} (TR)\n"
            f"{'━'*22}\n\n"
            f"🌍 *Piyasa Havası:* {mood}\n"
            f"📊 *Ortalama 24s:* `{avg:+.2f}%`\n"
        )
        if fg_val:
            fg_emoji = "😱" if fg_val<=25 else "😰" if fg_val<=45 else "😐" if fg_val<=55 else "😏" if fg_val<=75 else "🤑"
            text += f"{fg_emoji} *Korku/Açgözlülük:* `{fg_val}/100` — {fg_class}\n"

        text += f"\n🚀 *En Çok Yükselen (24s)*\n"
        for i, c in enumerate(top5_gain, 1):
            text += f"{get_number_emoji(i)} `{c['symbol']:<14}` 🟢 `{float(c['priceChangePercent']):+.2f}%`\n"

        text += f"\n📉 *En Çok Düşen (24s)*\n"
        for i, c in enumerate(top5_loss, 1):
            text += f"{get_number_emoji(i)} `{c['symbol']:<14}` 🔴 `{float(c['priceChangePercent']):+.2f}%`\n"

        if trending:
            text += f"\n🔥 *CoinGecko Trending*\n"
            for sym, name in trending:
                text += f"• `{sym}` — {name}\n"

        text += f"\n_Güzel bir gün dileriz! 🎯_"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("😱 Korku & Açgözlülük", callback_data="fear_greed"),
            InlineKeyboardButton("🔍 Coin Tara",           callback_data="scanner")
        ]])
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        log.error(f"Sabah özeti hatası: {e}")

async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    if now.hour == DAILY_SUMMARY_HOUR and now.minute < 2:
        await send_daily_summary(context.bot, GROUP_CHAT_ID)

async def ozet_command(update: Update, context):
    """Manuel sabah özeti komutu."""
    chat_id = update.effective_chat.id
    await send_daily_summary(context.bot, chat_id)

# ═══════════════════════════════════════════
#              /set — ADMİN PANELİ
# ═══════════════════════════════════════════

SET_PRESETS = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0]

async def set_command(update: Update, context):
    if not await is_admin(update, context):
        await update.message.reply_text("🚫 *Bu komut sadece grup adminlerine açıktır.*", parse_mode="Markdown")
        return
    await show_set_panel(update.message, context)

async def show_set_panel(message, context):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT alarm_active, threshold FROM groups WHERE chat_id=$1", GROUP_CHAT_ID)
    thr   = r["threshold"]
    aktif = r["alarm_active"]

    btns = []
    row  = []
    for val in SET_PRESETS:
        mark = "✅ " if thr == val else ""
        row.append(InlineKeyboardButton(f"{mark}%{val:.0f}", callback_data=f"set_threshold_{val}"))
        if len(row) == 4:
            btns.append(row); row = []
    if row:
        btns.append(row)

    btns += [
        [InlineKeyboardButton("✏️ Manuel Gir", callback_data="set_threshold_custom")],
        [InlineKeyboardButton(f"🔔 Alarm: {'AKTİF ✅' if aktif else 'KAPALI ❌'}", callback_data="set_toggle_alarm")],
        [InlineKeyboardButton("❌ Kapat", callback_data="set_close")],
    ]
    text = (
        "⚙️ *Admin Paneli*\n━━━━━━━━━━━━━━━━━━\n"
        f"🔔 Alarm: `{'AKTİF' if aktif else 'KAPALI'}`\n"
        f"🎯 Eşik: `%{thr}`\n\n"
        "Eşik seçin veya manuel girin:"
    )
    await message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

async def set_callback(update: Update, context):
    q    = update.callback_query
    chat = q.message.chat
    if chat.type != "private":
        try:
            m = await context.bot.get_chat_member(chat.id, q.from_user.id)
            if m.status not in ("administrator","creator"):
                await q.answer("🚫 Sadece adminler.", show_alert=True); return
        except:
            await q.answer("Yetki kontrol edilemedi.", show_alert=True); return
    await q.answer()

    if q.data == "set_toggle_alarm":
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow("SELECT alarm_active FROM groups WHERE chat_id=$1", GROUP_CHAT_ID)
            nv = 0 if r["alarm_active"] else 1
            await conn.execute("UPDATE groups SET alarm_active=$1 WHERE chat_id=$2", nv, GROUP_CHAT_ID)
        await q.message.reply_text(f"🔔 Alarm `{'AKTİF ✅' if nv else 'KAPALI ❌'}` yapıldı.", parse_mode="Markdown")
        await q.message.delete()

    elif q.data == "set_close":
        await q.message.delete()

    elif q.data == "set_threshold_custom":
        context.user_data["awaiting_threshold"] = True
        await q.message.reply_text("✏️ Yeni alarm eşiğini yazın (örn: `4.5`):", parse_mode="Markdown")
        await q.message.delete()

    elif q.data.startswith("set_threshold_"):
        try:   val = float(q.data.replace("set_threshold_",""))
        except: return
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE groups SET threshold=$1 WHERE chat_id=$2", val, GROUP_CHAT_ID)
        await q.message.reply_text(f"✅ Eşik *%{val}* olarak güncellendi.", parse_mode="Markdown")
        await q.message.delete()

async def handle_threshold_input(update: Update, context) -> bool:
    if not context.user_data.get("awaiting_threshold"):
        return False
    if not await is_admin(update, context):
        context.user_data.pop("awaiting_threshold", None)
        return True
    raw = update.message.text.strip().replace(",",".")
    try:
        val = float(raw)
        if not (0.1 <= val <= 100): raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ 0.1 ile 100 arasında bir değer girin. Örn: `4.5`", parse_mode="Markdown")
        return True
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE groups SET threshold=$1 WHERE chat_id=$2", val, GROUP_CHAT_ID)
    context.user_data.pop("awaiting_threshold", None)
    await update.message.reply_text(f"✅ Alarm eşiği *%{val}* olarak güncellendi!", parse_mode="Markdown")
    return True

# ═══════════════════════════════════════════
#             KİŞİSEL ALARM
# ═══════════════════════════════════════════

async def my_alarm(update: Update, context):
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT symbol, threshold, active FROM user_alarms WHERE user_id=$1", user_id)

    if not rows:
        text = (
            "🔔 *Kişisel Alarm Paneli*\n━━━━━━━━━━━━━━━━━━\n"
            "Henüz aktif alarmınız yok.\n\n"
            "➕ Eklemek için:\n`/alarm_ekle BTCUSDT 3.5`"
        )
    else:
        text = "🔔 *Kişisel Alarmlarınız*\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            text += f"{'✅' if r['active'] else '⏸'} `{r['symbol']}` → `%{r['threshold']}`\n"
        text += "\n`/alarm_ekle BTCUSDT 3.5` — ekle\n`/alarm_sil BTCUSDT` — sil"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Ekle",      callback_data="alarm_guide"),
        InlineKeyboardButton("🗑 Tümünü Sil", callback_data=f"alarm_deleteall_{user_id}")
    ]])
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def alarm_ekle(update: Update, context):
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: `/alarm_ekle BTCUSDT 3.5`", parse_mode="Markdown")
        return
    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:   threshold = float(context.args[1])
    except: await update.message.reply_text("Eşik sayı olmalıdır. Örn: `3.5`", parse_mode="Markdown"); return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_alarms (user_id, username, symbol, threshold, active)
            VALUES ($1,$2,$3,$4,1)
            ON CONFLICT (user_id, symbol) DO UPDATE SET threshold=$4, active=1
        """, user_id, username, symbol, threshold)
    await update.message.reply_text(
        f"✅ *{symbol}* için `%{threshold}` alarmı eklendi!", parse_mode="Markdown")

async def alarm_sil(update: Update, context):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Kullanım: `/alarm_sil BTCUSDT`", parse_mode="Markdown"); return
    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    async with db_pool.acquire() as conn:
        r = await conn.execute("DELETE FROM user_alarms WHERE user_id=$1 AND symbol=$2", user_id, symbol)
    if r == "DELETE 0":
        await update.message.reply_text(f"`{symbol}` için alarm bulunamadı.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🗑 `{symbol}` alarmı silindi.", parse_mode="Markdown")

# ═══════════════════════════════════════════
#              ANA KOMUTLAR
# ═══════════════════════════════════════════

async def start(update: Update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Piyasa",        callback_data="market"),
         InlineKeyboardButton("😱 Korku/Açgözlülük", callback_data="fear_greed")],
        [InlineKeyboardButton("🏆 24s Top 20",    callback_data="top24"),
         InlineKeyboardButton("⚡ 5dk Flaş",       callback_data="top5")],
        [InlineKeyboardButton("🔍 Coin Tarayıcı", callback_data="scanner"),
         InlineKeyboardButton("☀️ Günlük Özet",   callback_data="daily_summary")],
        [InlineKeyboardButton("💼 Portföyüm",     callback_data="portfolio"),
         InlineKeyboardButton("🔔 Alarmlarım",    callback_data="my_alarm")],
        [InlineKeyboardButton("⚙️ Durum",         callback_data="status"),
         InlineKeyboardButton("🛠 Admin",          callback_data="set_open")],
    ])
    text = (
        "👋 *Kripto Analiz Asistanı*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "7/24 piyasayı izliyorum.\n\n"
        "💡 Analiz için coin yazın: `BTCUSDT`\n"
        "💼 Portföy: `/portfoy ekle BTC 0.5 65000`\n"
        "🔔 Alarm: `/alarm_ekle BTCUSDT 3.5`"
    )
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg  = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    pos  = sum(1 for x in usdt if float(x["priceChangePercent"]) > 0)
    neg  = len(usdt) - pos
    mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
    bar  = trend_bar(abs(avg)*5, 14)
    text = (
        f"🌍 *Piyasa Genel Durumu*\n{'━'*20}\n"
        f"Duygu: *{mood}*\n"
        f"Ort. Değişim: `{avg:+.2f}%`\n"
        f"`{bar}`\n\n"
        f"🟢 Yükselen: `{pos}` coin\n"
        f"🔴 Düşen:    `{neg}` coin"
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

async def top24(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    usdt = sorted(
        [x for x in data if x["symbol"].endswith("USDT")],
        key=lambda x: float(x["priceChangePercent"]), reverse=True
    )[:20]
    text = "🏆 *24 Saatlik Top 20*\n" + "━"*22 + "\n"
    for i, c in enumerate(usdt, 1):
        ch  = float(c["priceChangePercent"])
        num = get_number_emoji(i) if i <= 10 else f"`{i:2d}.`"
        text += f"{num} `{c['symbol']:<14}` 🟢 `{ch:+.2f}%`\n"
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
        )[:20]
        text = "⚡ *En Hareketliler (24s baz)*\n" + "━"*22 + "\n"
        for i, c in enumerate(usdt, 1):
            ch  = float(c["priceChangePercent"])
            e   = "🟢" if ch > 0 else "🔴"
            num = get_number_emoji(i) if i <= 10 else f"`{i:2d}.`"
            text += f"{num} `{c['symbol']:<14}` {e} `{ch:+.2f}%`\n"
        text += "\n_⏳ WS verisi doluyor..._"
    else:
        changes = [(s, ((p[-1][1]-p[0][1])/p[0][1])*100)
                   for s,p in price_memory.items() if len(p)>=2]
        top = sorted(changes, key=lambda x: abs(x[1]), reverse=True)[:20]
        text = "⚡ *Son 5dk En Hareketliler*\n" + "━"*22 + "\n"
        for i, (s, c) in enumerate(top, 1):
            e   = "🟢" if c > 0 else "🔴"
            num = get_number_emoji(i) if i <= 10 else f"`{i:2d}.`"
            text += f"{num} `{s:<14}` {e} `{c:+.2f}%`\n"
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

async def status(update: Update, context):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT alarm_active, threshold, mode FROM groups WHERE chat_id=$1", GROUP_CHAT_ID)
    fg_val, fg_class, _ = await get_fear_greed()
    fg_line = f"😱 *Korku/Açgözlülük:* `{fg_val}` — {fg_class}\n" if fg_val else ""
    text = (
        "ℹ️ *Sistem Durumu*\n━━━━━━━━━━━━━━━━━━\n"
        f"🔔 Alarm: `{'AKTİF' if r['alarm_active'] else 'KAPALI'}`\n"
        f"🎯 Eşik: `%{r['threshold']}`\n"
        f"📦 Takip: `{len(price_memory)}` sembol\n"
        f"{fg_line}"
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="Markdown")

# ═══════════════════════════════════════════
#          SEMBOL TEPKİSİ (mesaj)
# ═══════════════════════════════════════════

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return
    if await handle_threshold_input(update, context):
        return
    raw    = update.message.text.upper().strip()
    symbol = raw.replace("#","").replace("/","")
    if symbol.endswith("USDT"):
        await send_full_analysis(context.bot, update.effective_chat.id, symbol, "PİYASA ANALİZ RAPORU")

# ═══════════════════════════════════════════
#              CALLBACK ROUTER
# ═══════════════════════════════════════════

async def button_handler(update: Update, context):
    q = update.callback_query

    if q.data.startswith("set_"):
        await set_callback(update, context); return

    await q.answer()

    # Yenile butonu
    if q.data.startswith("refresh_"):
        sym = q.data.replace("refresh_","")
        await send_full_analysis(context.bot, q.message.chat.id, sym, "PİYASA ANALİZ RAPORU")
        return

    if q.data == "market":           await market(update, context)
    elif q.data == "top24":          await top24(update, context)
    elif q.data == "top5":           await top5(update, context)
    elif q.data == "status":         await status(update, context)
    elif q.data == "my_alarm":       await my_alarm(update, context)
    elif q.data == "fear_greed":     await fear_greed_command(update, context)
    elif q.data == "scanner":        await scanner_command(update, context)
    elif q.data == "daily_summary":  await ozet_command(update, context)
    elif q.data == "portfolio":
        class FU:
            message = q.message
            effective_user = q.from_user
            effective_chat = q.message.chat
        ctx_fake = context
        ctx_fake.args = []
        await portfolio_command(FU(), ctx_fake)
    elif q.data == "portfolio_refresh":
        class FU:
            message = q.message
            effective_user = q.from_user
            effective_chat = q.message.chat
        ctx_fake = context
        ctx_fake.args = []
        await portfolio_command(FU(), ctx_fake)
    elif q.data == "portfolio_guide":
        await q.message.reply_text(
            "📝 *Portföye Coin Eklemek İçin:*\n"
            "`/portfoy ekle BTC 0.5 65000`\n\n"
            "🗑 *Silmek İçin:*\n`/portfoy sil BTC`",
            parse_mode="Markdown"
        )
    elif q.data == "alarm_guide":
        await q.message.reply_text(
            "➕ `/alarm_ekle BTCUSDT 3.5`\n🗑 `/alarm_sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data.startswith("alarm_deleteall_"):
        uid = int(q.data.split("_")[-1])
        if q.from_user.id == uid:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM user_alarms WHERE user_id=$1", uid)
            await q.message.reply_text("🗑 Tüm alarmlarınız silindi.")
    elif q.data == "set_open":
        if q.message.chat.type != "private":
            try:
                m = await context.bot.get_chat_member(q.message.chat.id, q.from_user.id)
                if m.status not in ("administrator","creator"):
                    await q.message.reply_text("🚫 Sadece adminler.", parse_mode="Markdown"); return
            except: return
        await show_set_panel(q.message, context)

    # Tarayıcı sonuçları
    elif q.data == "scan_oversold":
        await run_rsi_scan(context.bot, q.message.chat.id, "oversold")
    elif q.data == "scan_overbought":
        await run_rsi_scan(context.bot, q.message.chat.id, "overbought")
    elif q.data == "scan_top_gain":
        await run_price_scan(context.bot, q.message.chat.id, "gain")
    elif q.data == "scan_top_loss":
        await run_price_scan(context.bot, q.message.chat.id, "loss")
    elif q.data == "scan_close":
        await q.message.delete()

# ═══════════════════════════════════════════
#              ALARM JOB
# ═══════════════════════════════════════════

async def alarm_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    async with db_pool.acquire() as conn:
        group_row = await conn.fetchrow(
            "SELECT alarm_active, threshold FROM groups WHERE chat_id=$1", GROUP_CHAT_ID)
        user_rows = await conn.fetch(
            "SELECT user_id, symbol, threshold FROM user_alarms WHERE active=1")

    if group_row and group_row["alarm_active"]:
        thr = group_row["threshold"]
        for symbol, prices in list(price_memory.items()):
            if len(prices) < 2: continue
            ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
            if abs(ch5) >= thr:
                key = f"group_{symbol}"
                if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES): continue
                cooldowns[key] = now
                await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, "🚨 ANI HAREKET UYARISI", thr)

    for row in user_rows:
        sym = row["symbol"]; uid = row["user_id"]; thr = row["threshold"]
        prices = price_memory.get(sym)
        if not prices or len(prices) < 2: continue
        ch5 = ((prices[-1][1] - prices[0][1]) / prices[0][1]) * 100
        if abs(ch5) >= thr:
            key = f"user_{uid}_{sym}"
            if key in cooldowns and now - cooldowns[key] < timedelta(minutes=COOLDOWN_MINUTES): continue
            cooldowns[key] = now
            try:
                await send_full_analysis(context.bot, uid, sym, f"🔔 KİŞİSEL ALARM — {sym}", thr)
            except Exception as e:
                log.warning(f"Kişisel alarm gönderilemedi ({uid}): {e}")

# ═══════════════════════════════════════════
#              WEBSOCKET
# ═══════════════════════════════════════════

async def binance_engine():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                log.info("🔌 Binance WebSocket bağlandı.")
                async for msg in ws:
                    data = json.loads(msg)
                    now  = datetime.utcnow()
                    for c in data:
                        s = c["s"]
                        if not s.endswith("USDT"): continue
                        if s not in price_memory and len(price_memory) >= MAX_SYMBOLS: continue
                        if s not in price_memory: price_memory[s] = []
                        price_memory[s].append((now, float(c["c"])))
                        price_memory[s] = [(t,p) for t,p in price_memory[s]
                                           if now - t <= timedelta(minutes=5)]
        except Exception as e:
            log.error(f"WebSocket koptu: {e} — 5sn sonra tekrar.")
            await asyncio.sleep(5)

async def post_init(app):
    await init_db()
    asyncio.create_task(binance_engine())

# ═══════════════════════════════════════════
#                   MAIN
# ═══════════════════════════════════════════

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job,          interval=60,   first=30)
    app.job_queue.run_repeating(daily_summary_job,  interval=120,  first=60)

    for cmd, fn in [
        ("start",       start),
        ("top24",       top24),
        ("top5",        top5),
        ("market",      market),
        ("status",      status),
        ("alarmim",     my_alarm),
        ("alarm_ekle",  alarm_ekle),
        ("alarm_sil",   alarm_sil),
        ("set",         set_command),
        ("portfoy",     portfolio_command),
        ("tara",        scanner_command),
        ("ozet",        ozet_command),
        ("korku",       fear_greed_command),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("🚀 BOT AKTİF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
