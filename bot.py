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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
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
                mode         TEXT    DEFAULT 'both',
                delete_delay INTEGER DEFAULT 30
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
            ALTER TABLE groups
            ADD COLUMN IF NOT EXISTS delete_delay INTEGER DEFAULT 30
        """)
        await conn.execute("""
            INSERT INTO groups (chat_id, threshold, mode, delete_delay)
            VALUES ($1, $2, $3, 30)
            ON CONFLICT (chat_id) DO NOTHING
        """, GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS price_targets (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                symbol     TEXT,
                target     REAL,
                direction  TEXT,
                triggered  INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, symbol, target)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kar_pozisyonlar (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                symbol     TEXT,
                amount     REAL,
                buy_price  REAL,
                note       TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, symbol)
            )
        """)

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

def calc_support_resistance(k4h_data):
    """4h mumlardan yakın destek ve direnç hesaplar."""
    if not k4h_data or len(k4h_data) < 10:
        return None, None
    highs  = [float(c[2]) for c in k4h_data]
    lows   = [float(c[3]) for c in k4h_data]
    closes = [float(c[4]) for c in k4h_data]
    cur    = closes[-1]

    # Swing high/low tespiti (komşularından büyük/küçük olanlar)
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append(lows[i])

    # Mevcut fiyatın altındaki en yakın destek, üstündeki en yakın direnç
    destek   = max((v for v in swing_lows  if v < cur), default=None)
    direnc   = min((v for v in swing_highs if v > cur), default=None)
    return destek, direnc

def calc_volume_anomaly(k1h_data):
    """Son mumdaki hacmi geçmiş ortalamayla karşılaştırır. (oran döner)"""
    if not k1h_data or len(k1h_data) < 5:
        return None
    vols = [float(c[5]) for c in k1h_data]
    avg  = sum(vols[:-1]) / len(vols[:-1])
    if avg == 0:
        return None
    return round(vols[-1] / avg, 2)

async def fetch_market_badge():
    """BTC dominansı ve piyasa geneli yönünü döner."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT")]
        changes = [float(x["priceChangePercent"]) for x in usdt]
        avg = sum(changes) / len(changes) if changes else 0

        # BTC fiyatı ve hacminden basit dominans tahmini
        btc = next((x for x in usdt if x["symbol"] == "BTCUSDT"), None)
        btc_vol = float(btc["quoteVolume"]) if btc else 0
        total_vol = sum(float(x["quoteVolume"]) for x in usdt)
        btc_dom = round((btc_vol / total_vol) * 100, 1) if total_vol > 0 else 0

        mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
        return mood, btc_dom, round(avg, 2)
    except Exception:
        return None, None, None

async def auto_delete(bot, chat_id, message_id, delay=30):
    """Mesajı delay saniye sonra siler. Sadece grup mesajları için kullanılır."""
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def get_delete_delay() -> int:
    """DB'den grup silme gecikmesini okur."""
    try:
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT delete_delay FROM groups WHERE chat_id=$1", GROUP_CHAT_ID
            )
        return int(r["delete_delay"]) if r and r["delete_delay"] else 30
    except Exception:
        return 30

async def send_temp(bot, chat_id, text, delay=None, **kwargs):
    """Grupta geçici mesaj gönderir, delay sn sonra siler. DM'de silmez."""
    msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type in ("group", "supergroup"):
            d = delay if delay is not None else await get_delete_delay()
            asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, d))
    except Exception:
        pass
    return msg

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
            ylabel="Fiyat (USDT)", volume=True, figsize=(8,4),
            savefig=dict(fname=buf, format="png", bbox_inches="tight", dpi=90),
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

def calc_stoch_rsi(data, rsi_period=14, stoch_period=14):
    """Stochastic RSI hesaplar. 0-100 arası döner."""
    try:
        closes = [float(x[4]) for x in data]
        # Önce RSI serisi oluştur
        rsi_vals = []
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        for i in range(rsi_period - 1, len(gains)):
            ag = sum(gains[i-rsi_period+1:i+1]) / rsi_period
            al = sum(losses[i-rsi_period+1:i+1]) / rsi_period
            if al == 0:
                rsi_vals.append(100.0)
            else:
                rs = ag / al
                rsi_vals.append(100 - (100 / (1 + rs)))
        if len(rsi_vals) < stoch_period:
            return 50.0
        window = rsi_vals[-stoch_period:]
        lo, hi = min(window), max(window)
        if hi == lo:
            return 50.0
        return round((rsi_vals[-1] - lo) / (hi - lo) * 100, 2)
    except:
        return 50.0

def calc_ema(data, period):
    """EMA hesaplar, son değeri döner."""
    try:
        closes = [float(x[4]) for x in data]
        if len(closes) < period:
            return closes[-1] if closes else 0
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for c in closes[period:]:
            ema = c * k + ema * (1 - k)
        return ema
    except:
        return 0

def calc_macd(data, fast=12, slow=26, signal=9):
    """MACD ve sinyal farkını döner. Pozitif = bullish."""
    try:
        closes = [float(x[4]) for x in data]
        if len(closes) < slow + signal:
            return 0.0, 0.0
        k_fast = 2 / (fast + 1)
        k_slow = 2 / (slow + 1)
        ema_fast = sum(closes[:fast]) / fast
        ema_slow = sum(closes[:slow]) / slow
        for c in closes[fast:]:
            ema_fast = c * k_fast + ema_fast * (1 - k_fast)
        for c in closes[slow:]:
            ema_slow = c * k_slow + ema_slow * (1 - k_slow)
        # MACD serisi için her noktada hesapla
        macd_vals = []
        ef = sum(closes[:fast]) / fast
        es = sum(closes[:slow]) / slow
        for i, c in enumerate(closes):
            ef = c * k_fast + ef * (1 - k_fast)
            es = c * k_slow + es * (1 - k_slow)
            if i >= slow - 1:
                macd_vals.append(ef - es)
        if len(macd_vals) < signal:
            return 0.0, 0.0
        k_sig = 2 / (signal + 1)
        sig_ema = sum(macd_vals[:signal]) / signal
        for m in macd_vals[signal:]:
            sig_ema = m * k_sig + sig_ema * (1 - k_sig)
        histogram = macd_vals[-1] - sig_ema
        return round(macd_vals[-1], 8), round(histogram, 8)
    except:
        return 0.0, 0.0

def calc_bollinger(data, period=20, std_mult=2.0):
    """Bollinger Band pozisyonu: 0=alt bant, 50=orta, 100=üst bant üstü."""
    try:
        closes = [float(x[4]) for x in data]
        if len(closes) < period:
            return 50.0
        window = closes[-period:]
        mean = sum(window) / period
        std  = (sum((c - mean) ** 2 for c in window) / period) ** 0.5
        upper = mean + std_mult * std
        lower = mean - std_mult * std
        cur   = closes[-1]
        if upper == lower:
            return 50.0
        pos = (cur - lower) / (upper - lower) * 100
        return round(_clamp(pos, -10, 110), 2)
    except:
        return 50.0

def calc_obv_trend(data, lookback=10):
    """OBV trendini hesaplar. +1 yükselen, -1 düşen, 0 nötr."""
    try:
        closes  = [float(x[4]) for x in data]
        volumes = [float(x[5]) for x in data]
        obv = 0
        obv_series = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
            obv_series.append(obv)
        if len(obv_series) < lookback:
            return 0
        early = sum(obv_series[:lookback//2]) / (lookback//2)
        late  = sum(obv_series[-lookback//2:]) / (lookback//2)
        if late > early * 1.02:
            return 1
        elif late < early * 0.98:
            return -1
        return 0
    except:
        return 0

def calc_rsi_divergence(data, period=14, lookback=20):
    """Basit RSI diverjans tespiti. 'bullish'/'bearish'/None döner."""
    try:
        closes = [float(x[4]) for x in data[-lookback:]]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(abs(min(d, 0)))
        if len(gains) < period:
            return None
        rsi_series = []
        for i in range(period - 1, len(gains)):
            ag = sum(gains[i-period+1:i+1]) / period
            al = sum(losses[i-period+1:i+1]) / period
            if al == 0:
                rsi_series.append(100.0)
            else:
                rsi_series.append(100 - 100 / (1 + ag/al))
        if len(rsi_series) < 4:
            return None
        mid = len(closes) // 2
        price_up   = closes[-1] > closes[mid]
        rsi_up     = rsi_series[-1] > rsi_series[len(rsi_series)//2]
        if price_up and not rsi_up and rsi_series[-1] > 60:
            return "bearish"   # fiyat yükseliyor ama RSI düşüyor
        if not price_up and rsi_up and rsi_series[-1] < 40:
            return "bullish"   # fiyat düşüyor ama RSI yükseliyor
        return None
    except:
        return None



def _score_label(score):
    if score >= 75: return "🚀 Güçlü Al",  "🟢🟢🟢🟢🟢"
    if score >= 60: return "📈 Pozitif",    "🟢🟢🟢🟡➖"
    if score >= 45: return "😐 Nötr",       "🟡🟡🟡➖➖"
    if score >= 30: return "📉 Zayıf",      "🔴🔴➖➖➖"
    return              "🚨 Güçlü Sat",  "🔴🔴🔴🔴🔴"

def _clamp(val, lo=0.0, hi=100.0):
    return max(lo, min(hi, val))

def _rsi_score(rsi):
    """RSI'yi 0-100 puana çevirir. 50 nötr, <30 güçlü al, >70 güçlü sat."""
    if rsi <= 0:
        return 50.0
    if rsi <= 30:
        # 0→100, 30→72
        return _clamp(100 - rsi * 0.93)
    elif rsi <= 50:
        # 30→72, 50→50
        return _clamp(72 - (rsi - 30) * 1.1)
    elif rsi <= 70:
        # 50→50, 70→28
        return _clamp(50 - (rsi - 50) * 1.1)
    else:
        # 70→28, 100→0
        return _clamp(28 - (rsi - 70) * 0.93)

def _ch_score(ch, scale=5.0):
    """Değişim yüzdesini 0-100 puana çevirir. 0%→50, +scale%→75, -scale%→25."""
    raw = 50 + (ch / scale) * 25
    return _clamp(raw)

def _vol_bonus(vol24, ch, pos_bonus=4.0, neg_bonus=-4.0):
    """Hacim yönü uyumuna göre küçük sürekli katkı."""
    if vol24 <= 0:
        return 0.0
    import math
    # log scale: 1M→0, 10M→1, 100M→2, 1B→3
    vol_factor = max(0, math.log10(vol24 / 1_000_000)) / 3.0  # 0→1
    vol_factor = min(vol_factor, 1.0)
    if ch > 0:
        return pos_bonus * vol_factor
    elif ch < 0:
        return neg_bonus * vol_factor
    return 0.0

def calc_score_hourly(ticker, k1h_series, k15m, k5m, rsi_1h):
    """SAATLİK SKOR — RSI, StochRSI, EMA, MACD, OBV, momentum."""
    rsi14     = calc_rsi(k1h_series, 14)
    rsi7      = calc_rsi(k1h_series, 7)
    stoch_rsi = calc_stoch_rsi(k1h_series)
    macd_val, macd_hist = calc_macd(k1h_series, fast=12, slow=26, signal=9)
    ema9      = calc_ema(k1h_series, 9)
    ema21     = calc_ema(k1h_series, 21)
    obv_trend = calc_obv_trend(k1h_series, lookback=12)
    boll_pos  = calc_bollinger(k1h_series, period=20)

    ch15m = calc_change(k15m) if k15m and len(k15m) >= 2 else 0
    ch5m  = calc_change(k5m)  if k5m  and len(k5m)  >= 2 else 0
    ch1h  = calc_change(k1h_series[-2:]) if k1h_series and len(k1h_series) >= 2 else 0
    vol24 = float(ticker.get("quoteVolume", 0))

    # RSI bileşenleri
    s_rsi14   = _rsi_score(rsi14)                           # 0.20
    s_rsi7    = _rsi_score(rsi7)                            # 0.12
    rsi_mom   = _clamp(50 + (rsi7 - rsi14) * 1.5)          # 0.08
    s_stoch   = _clamp(100 - stoch_rsi) if stoch_rsi > 50 else _clamp(50 + stoch_rsi)  # 0.10

    # Fiyat değişim bileşenleri
    s_5m  = _ch_score(ch5m,  scale=3.0)                    # 0.12
    s_15m = _ch_score(ch15m, scale=4.0)                    # 0.08
    s_1h  = _ch_score(ch1h,  scale=5.0)                    # 0.08

    # EMA trend: EMA9 > EMA21 bullish
    ema_score = 65.0 if ema9 > ema21 else 35.0             # 0.10

    # MACD histogram yönü
    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 20)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 20)
    macd_score = _clamp(macd_score)                        # 0.08

    # OBV trendi
    obv_score = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)  # 0.04

    score = (
        s_rsi14   * 0.20 +
        s_rsi7    * 0.12 +
        rsi_mom   * 0.08 +
        s_stoch   * 0.10 +
        s_5m      * 0.12 +
        s_15m     * 0.08 +
        s_1h      * 0.08 +
        ema_score * 0.10 +
        macd_score* 0.08 +
        obv_score * 0.04
    )
    score += _vol_bonus(vol24, ch5m)
    score = _clamp(score)
    label, bar = _score_label(score)
    return round(score), label, bar

def calc_score_daily(ticker, k4h_series, k1h_series, k1d_series):
    """GÜNLÜK SKOR — 4h RSI, EMA, Bollinger, MACD, OBV."""
    rsi14_4h  = calc_rsi(k4h_series, 14)
    rsi14_1h  = calc_rsi(k1h_series, 14)
    stoch_4h  = calc_stoch_rsi(k4h_series)
    macd_val, macd_hist = calc_macd(k4h_series)
    ema21_4h  = calc_ema(k4h_series, 21)
    ema55_4h  = calc_ema(k4h_series, 55)
    boll_pos  = calc_bollinger(k4h_series, period=20)
    obv_trend = calc_obv_trend(k4h_series, lookback=14)

    ch4h  = calc_change(k4h_series[-2:]) if k4h_series and len(k4h_series) >= 2 else 0
    ch24h = calc_change(k1h_series)      if k1h_series and len(k1h_series) >= 2 else 0
    ch24  = float(ticker.get("priceChangePercent", 0))
    vol24 = float(ticker.get("quoteVolume", 0))
    high  = float(ticker.get("highPrice", 1)) or 1
    low   = float(ticker.get("lowPrice",  1)) or 1
    volat = ((high - low) / low) * 100

    s_rsi_4h  = _rsi_score(rsi14_4h)                       # 0.20
    s_rsi_1h  = _rsi_score(rsi14_1h)                       # 0.10
    s_stoch   = _clamp(100 - stoch_4h) if stoch_4h > 50 else _clamp(50 + stoch_4h)  # 0.08

    s_4h      = _ch_score(ch4h,  scale=5.0)                 # 0.15
    s_24h     = _ch_score(ch24h, scale=8.0)                 # 0.12

    ema_score = 65.0 if ema21_4h > ema55_4h else 35.0      # 0.12

    # Bollinger: fiyat alt bantta → alım fırsatı, üst bantta → satış baskısı
    boll_score = _clamp(100 - boll_pos)                    # 0.08
    # ama ortada (40-60) nötr olmalı:
    if 35 < boll_pos < 65:
        boll_score = 50.0

    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 15)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 15)
    macd_score = _clamp(macd_score)                        # 0.08

    obv_score  = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)  # 0.05
    vol_dir    = _clamp(50 + (ch24 / max(volat, 0.5)) * 10)  # 0.02

    score = (
        s_rsi_4h  * 0.20 +
        s_rsi_1h  * 0.10 +
        s_stoch   * 0.08 +
        s_4h      * 0.15 +
        s_24h     * 0.12 +
        ema_score * 0.12 +
        boll_score* 0.08 +
        macd_score* 0.08 +
        obv_score * 0.05 +
        vol_dir   * 0.02
    )
    score += _vol_bonus(vol24, ch24, pos_bonus=5.0, neg_bonus=-5.0)
    score = _clamp(score)
    label, bar = _score_label(score)
    return round(score), label, bar

def calc_score_weekly(ticker, k1d_series, k1w_series):
    """HAFTALIK SKOR — günlük/haftalık RSI, EMA200, MACD, OBV."""
    rsi14_1d  = calc_rsi(k1d_series, 14)
    rsi14_1w  = calc_rsi(k1w_series, 14)
    stoch_1d  = calc_stoch_rsi(k1d_series)
    macd_val, macd_hist = calc_macd(k1d_series)
    ema50_1d  = calc_ema(k1d_series, 50)
    ema200_1d = calc_ema(k1d_series, min(200, len(k1d_series)))
    boll_pos  = calc_bollinger(k1d_series, period=20)
    obv_trend = calc_obv_trend(k1d_series, lookback=20)

    ch7d   = calc_change(k1d_series[-7:]) if k1d_series and len(k1d_series) >= 7  else 0
    ch30d  = calc_change(k1d_series)      if k1d_series and len(k1d_series) >= 5  else 0
    ch4w   = calc_change(k1w_series[-4:]) if k1w_series and len(k1w_series) >= 4  else 0
    vol24  = float(ticker.get("quoteVolume", 0))
    ch24   = float(ticker.get("priceChangePercent", 0))

    s_rsi_1d  = _rsi_score(rsi14_1d)                       # 0.18
    s_rsi_1w  = _rsi_score(rsi14_1w)                       # 0.18
    s_stoch   = _clamp(100 - stoch_1d) if stoch_1d > 50 else _clamp(50 + stoch_1d)  # 0.06

    s_7d      = _ch_score(ch7d,  scale=12.0)                # 0.15
    s_4w      = _ch_score(ch4w,  scale=20.0)                 # 0.10
    s_30d     = _ch_score(ch30d, scale=30.0)                  # 0.08

    # EMA50 > EMA200 → güçlü uzun vade trend (golden cross)
    ema_score = 70.0 if ema50_1d > ema200_1d else 30.0     # 0.12

    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 12)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 12)
    macd_score = _clamp(macd_score)                        # 0.08

    obv_score = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)   # 0.05

    score = (
        s_rsi_1d  * 0.18 +
        s_rsi_1w  * 0.18 +
        s_stoch   * 0.06 +
        s_7d      * 0.15 +
        s_4w      * 0.10 +
        s_30d     * 0.08 +
        ema_score * 0.12 +
        macd_score* 0.08 +
        obv_score * 0.05
    )
    score += _vol_bonus(vol24, ch24, pos_bonus=3.0, neg_bonus=-3.0)
    score = _clamp(score)
    label, bar = _score_label(score)
    return round(score), label, bar

async def fetch_all_analysis(symbol):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BINANCE_24H}?symbol={symbol}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            ticker = await resp.json()

        (k4h, k1h_2, k5m, k1h_100,
         k1d, k15m, k4h_42, k1h_24,
         k1w, k4h_100, k1d_100) = await asyncio.gather(
            fetch_klines(session, symbol, "4h",  limit=2),     # anlık 4sa
            fetch_klines(session, symbol, "1h",  limit=2),     # anlık 1sa
            fetch_klines(session, symbol, "5m",  limit=2),     # anlık 5dk
            fetch_klines(session, symbol, "1h",  limit=100),   # 1h RSI + indikatör
            fetch_klines(session, symbol, "1d",  limit=30),    # günlük 30 gün
            fetch_klines(session, symbol, "15m", limit=20),    # 15dk seri
            fetch_klines(session, symbol, "4h",  limit=50),    # 4sa seri (skor)
            fetch_klines(session, symbol, "1h",  limit=24),    # 24sa seri (skor)
            fetch_klines(session, symbol, "1w",  limit=12),    # haftalık 12 hafta
            fetch_klines(session, symbol, "4h",  limit=100),   # 4h RSI + indikatör
            fetch_klines(session, symbol, "1d",  limit=100),   # 1d RSI + indikatör
        )

    return ticker, k4h, k1h_2, k5m, k1h_100, k1d, k15m, k4h_42, k1h_24, k1w, k4h_100, k1d_100

async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None, auto_del=False, ch5_override=None):
    try:
        (ticker, k4h, k1h_2, k5m, k1h_100,
         k1d, k15m, k4h_42, k1h_24, k1w,
         k4h_100, k1d_100) = await fetch_all_analysis(symbol)

        if "lastPrice" not in ticker:
            return

        price  = float(ticker["lastPrice"])
        ch24   = float(ticker["priceChangePercent"])
        ch4h   = calc_change(k4h)
        ch1h   = calc_change(k1h_2)
        ch5m   = calc_change(k5m)
        # Alarm'dan gelen gerçek 5dk değeri varsa onu kullan (API değeriyle tutarsızlığı önler)
        if ch5_override is not None:
            ch5m = ch5_override

        # ── RSI çok zaman dilimli ──
        rsi7_1h    = calc_rsi(k1h_100, 7)
        rsi14_1h   = calc_rsi(k1h_100, 14)
        rsi14_4h   = calc_rsi(k4h_100, 14)
        rsi14_1d   = calc_rsi(k1d_100, 14)
        stoch_1h   = calc_stoch_rsi(k1h_100)
        stoch_4h   = calc_stoch_rsi(k4h_100)

        # ── Teknik indikatörler ──
        ema9_1h    = calc_ema(k1h_100, 9)
        ema21_1h   = calc_ema(k1h_100, 21)
        ema21_4h   = calc_ema(k4h_100, 21)
        ema55_4h   = calc_ema(k4h_100, 55)
        _, macd_hist_1h = calc_macd(k1h_100)
        _, macd_hist_4h = calc_macd(k4h_100)
        boll_1h    = calc_bollinger(k1h_100)
        obv_1h     = calc_obv_trend(k1h_100, lookback=12)
        diverjans  = calc_rsi_divergence(k1h_100)

        # Destek / Direnç
        destek, direnc = calc_support_resistance(k4h_42)

        # Hacim Anomali
        vol_ratio = calc_volume_anomaly(k1h_24)

        # Piyasa Rozeti
        mood, btc_dom, mkt_avg = await fetch_market_badge()

        def get_ui(val):
            if val > 0:   return "🟢▲", "+"
            elif val < 0: return "🔴▼", ""
            else:         return "⚪→", ""

        e5,s5   = get_ui(ch5m)
        e1,s1   = get_ui(ch1h)
        e4,s4   = get_ui(ch4h)
        e24,s24 = get_ui(ch24)

        def rsi_label(r):
            if r >= 80:   return "🔴 Aşırı Alım"
            elif r >= 70: return "🟠 Alım Bölgesi"
            elif r >= 55: return "🟡 Yükseliş"
            elif r <= 20: return "🔵 Aşırı Satım"
            elif r <= 30: return "🟣 Satım Bölgesi"
            elif r <= 45: return "🟡 Düşüş"
            else:         return "🟢 Normal"

        def stoch_label(s):
            if s >= 80:   return "🔴 Aşırı"
            elif s >= 60: return "🟡 Yüksek"
            elif s <= 20: return "🔵 Aşırı"
            elif s <= 40: return "🟡 Düşük"
            else:         return "🟢 Nötr"

        sh, lh, bh = calc_score_hourly(ticker, k1h_100, k15m, k5m, rsi14_1h)
        sd, ld, bd = calc_score_daily(ticker, k4h_42, k1h_24, k1d)
        sw, lw, bw = calc_score_weekly(ticker, k1d, k1w)

        vol_usdt = float(ticker.get("quoteVolume", 0))
        vol_str  = f"{vol_usdt/1_000_000:.1f}M" if vol_usdt >= 1_000_000 else f"{vol_usdt/1_000:.0f}K"

        # Hacim Anomali — son 1 saati önceki 23 saatin ortalamasıyla kıyaslar
        if vol_ratio is not None:
            if vol_ratio >= 3.0:
                vol_anom = f"⚡ *Hacim:* `{vol_str} USDT`  `{vol_ratio}x` _(son 1sa / önceki 23sa ort.)_ — Çok Yüksek!\n"
            elif vol_ratio >= 2.0:
                vol_anom = f"🔶 *Hacim:* `{vol_str} USDT`  `{vol_ratio}x` _(son 1sa / önceki 23sa ort.)_ — Yüksek\n"
            elif vol_ratio >= 1.5:
                vol_anom = f"🟡 *Hacim:* `{vol_str} USDT`  `{vol_ratio}x` _(son 1sa / önceki 23sa ort.)_ — Normal Üstü\n"
            else:
                vol_anom = f"📦 *Hacim:* `{vol_str} USDT`\n"
        else:
            vol_anom = f"📦 *Hacim:* `{vol_str} USDT`\n"

        # Diverjans uyarısı (sadece varsa göster)
        div_line = ""
        if diverjans == "bearish":
            div_line = "⚠️ *Bearish Diverjans* — Fiyat yükseliyor, RSI düşüyor!\n"
        elif diverjans == "bullish":
            div_line = "💡 *Bullish Diverjans* — Fiyat düşüyor, RSI yükseliyor!\n"

        # Başlık — alarm mesajlarında renkli, analizde nötr
        is_alarm = "UYARISI" in extra_title or "ALARM" in extra_title or "YUKSELIS" in extra_title or "DUSUS" in extra_title
        if is_alarm:
            header = f"*{extra_title}*\n"
        else:
            header = f"*{extra_title}*\n"

        text = header + (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 `{symbol}` 💎\n"
            f"\n"
            f"💵 *Fiyat:* `{format_price(price)} USDT`\n"
            f"{vol_anom}"
            f"\n*Performans:*\n"
            f"{e5} `5dk  :` `{s5}{ch5m:+.2f}%`\n"
            f"{e1} `1sa  :` `{s1}{ch1h:+.2f}%`\n"
            f"{e4} `4sa  :` `{s4}{ch4h:+.2f}%`\n"
            f"{e24} `24sa :` `{s24}{ch24:+.2f}%`\n\n"
            f"*RSI:*\n"
            f"• 4sa  RSI 14 : `{rsi14_4h}` — {rsi_label(rsi14_4h)}\n"
            f"• 1gün RSI 14 : `{rsi14_1d}` — {rsi_label(rsi14_1d)}\n"
        )
        if div_line:
            text += f"{div_line}\n"
        else:
            text += "\n"
        text += (
            f"*Piyasa Skoru:*\n"
            f"⏱ Saatlik : `{sh}/100` — _{lh}_\n"
            f"📅 Günlük  : `{sd}/100` — _{ld}_\n"
            f"📆 Haftalık: `{sw}/100` — _{lw}_\n"
            f"──────────────────"
        )
        if threshold_info:
            text += f"\n🔔 *Alarm Eşiği:* `%{threshold_info}`"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📈 Binance'de Goruntule",
                url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
            )
        ]])

        msg = await bot.send_message(chat_id=chat_id, text=text,
                                     reply_markup=keyboard, parse_mode="Markdown")
        if auto_del:
            delay = await get_delete_delay()
            asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, delay))

        chart_buf = await generate_candlestick_chart(symbol)
        if chart_buf:
            photo_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(chart_buf, filename=f"{symbol}_4h.png"),
                caption=f"🕯️ *{symbol}* — 4 Saatlik",
                parse_mode="Markdown"
            )
            if auto_del:
                asyncio.create_task(auto_delete(bot, chat_id, photo_msg.message_id, delay))

    except Exception as e:
        err = str(e)
        # Forbidden = kullanıcı bota DM açmamış → çağırana fırlat
        if any(x in err for x in ("Forbidden", "bot was blocked", "chat not found", "user is deactivated")):
            raise
        log.error(f"Gonderim hatasi ({symbol}): {e}")

# ================= ADMIN KONTROL =================

async def is_admin(update: Update, context) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        log.warning(f"Admin kontrol hatasi: {e}")
        return False

SET_THRESHOLD_PRESETS = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0]
DELETE_DELAY_PRESETS  = [30, 60, 300, 600, 1800, 3600]   # saniye

async def build_set_panel(context):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT alarm_active, threshold, delete_delay FROM groups WHERE chat_id=$1",
            GROUP_CHAT_ID
        )
    threshold    = r["threshold"]
    alarm_active = r["alarm_active"]
    del_delay    = r["delete_delay"] or 30

    # Eşik butonları
    threshold_buttons = []
    row = []
    for val in SET_THRESHOLD_PRESETS:
        label = f"{'✅ ' if threshold == val else ''}%{val:.0f}"
        row.append(InlineKeyboardButton(label, callback_data=f"set_threshold_{val}"))
        if len(row) == 4:
            threshold_buttons.append(row); row = []
    if row: threshold_buttons.append(row)
    threshold_buttons.append([InlineKeyboardButton("✏️ Manuel Eşik", callback_data="set_threshold_custom")])

    # Silme süresi butonları
    delay_rows = []
    delay_row  = []
    label_map  = {30: "30sn", 60: "1dk", 300: "5dk", 600: "10dk", 1800: "30dk", 3600: "1sa"}
    for val in DELETE_DELAY_PRESETS:
        label = f"{'✅ ' if del_delay == val else ''}{label_map.get(val, str(val)+'sn')}"
        delay_row.append(InlineKeyboardButton(label, callback_data=f"set_delay_{val}"))
        if len(delay_row) == 3:
            delay_rows.append(delay_row)
            delay_row = []
    if delay_row:
        delay_rows.append(delay_row)
    threshold_buttons.extend(delay_rows)

    threshold_buttons.append([
        InlineKeyboardButton(
            f"🔔 Alarm: {'AKTİF ✅' if alarm_active else 'KAPALI ❌'}",
            callback_data="set_toggle_alarm"
        )
    ])
    threshold_buttons.append([InlineKeyboardButton("❌ Kapat", callback_data="set_close")])

    delay_label = {15: "15 sn", 30: "30 sn", 60: "1 dk", 120: "2 dk", 300: "5 dk"}.get(del_delay, f"{del_delay} sn")
    text = (
        "⚙️ *Grup Ayarları — Admin Paneli*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTİF' if alarm_active else 'KAPALI'}`\n"
        f"🎯 *Alarm Eşiği:* `%{threshold}`\n"
        f"🗑 *Mesaj Silme:* `{delay_label}` sonra\n\n"
        "Eşik seçin, silme süresi ayarlayın:"
    )
    return text, InlineKeyboardMarkup(threshold_buttons)


async def set_command(update: Update, context):
    chat = update.effective_chat
    if chat.type != "private":
        try:
            member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("🚫 *Bu komut sadece grup adminlerine açıktır.*", parse_mode="Markdown")
                return
        except Exception as e:
            log.warning(f"Admin kontrol: {e}")
            await update.message.reply_text("⚠️ Yetki kontrol edilemedi.", parse_mode="Markdown")
            return
    text, keyboard = await build_set_panel(context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def set_callback(update: Update, context):
    q = update.callback_query
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, q.from_user.id)
        if member.status not in ("administrator", "creator"):
            await q.answer("🚫 Sadece grup adminleri.", show_alert=True)
            return
    except Exception as e:
        log.warning(f"set_callback admin: {e}")
        await q.answer("🚫 Yetki kontrol edilemedi.", show_alert=True)
        return

    await q.answer()

    if q.data == "set_close":
        try: await q.message.delete()
        except: pass
        return

    if q.data == "set_toggle_alarm":
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow("SELECT alarm_active FROM groups WHERE chat_id=$1", GROUP_CHAT_ID)
            new_val = 0 if r["alarm_active"] else 1
            await conn.execute("UPDATE groups SET alarm_active=$1 WHERE chat_id=$2", new_val, GROUP_CHAT_ID)
        text, keyboard = await build_set_panel(context)
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if q.data.startswith("set_threshold_"):
        val_str = q.data.replace("set_threshold_", "")
        if val_str == "custom":
            context.user_data["awaiting_threshold"] = True
            await q.message.reply_text("✏️ Yeni eşik değeri girin (0.1 – 100):\nÖrnek: `4.5`", parse_mode="Markdown")
            return
        try:
            val = float(val_str)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE groups SET threshold=$1 WHERE chat_id=$2", val, GROUP_CHAT_ID)
            text, keyboard = await build_set_panel(context)
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            log.warning(f"set_threshold: {e}")
        return

    if q.data.startswith("set_delay_"):
        try:
            delay_val = int(q.data.replace("set_delay_", ""))
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE groups SET delete_delay=$1 WHERE chat_id=$2", delay_val, GROUP_CHAT_ID)
            text, keyboard = await build_set_panel(context)
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            log.warning(f"set_delay: {e}")
        return

    if q.data == "set_open":
        text, keyboard = await build_set_panel(context)
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def handle_threshold_input(update: Update, context):
    if not context.user_data.get("awaiting_threshold"):
        return False
    if not await is_admin(update, context):
        context.user_data.pop("awaiting_threshold", None)
        return True
    text = update.message.text.strip().replace(",", ".")
    try:
        val = float(text)
        if not (0.1 <= val <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ 0.1 ile 100 arasında sayı girin. Örnek: `4.5`", parse_mode="Markdown")
        return True
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE groups SET threshold=$1 WHERE chat_id=$2", val, GROUP_CHAT_ID)
    context.user_data.pop("awaiting_threshold", None)
    await update.message.reply_text(f"✅ Alarm eşiği *%{val}* olarak güncellendi!", parse_mode="Markdown")
    return True

# ================= SEMBOL TEPKİ =================

async def reply_symbol(update: Update, context):
    if not update.message or not update.message.text:
        return
    if await handle_threshold_input(update, context):
        return

    raw    = update.message.text.upper().strip()
    symbol = raw.replace("#", "").replace("/", "")
    if not symbol.endswith("USDT"):
        return

    chat = update.effective_chat
    is_group = chat.type in ("group", "supergroup")

    # Kullanıcı mesajını sil (grupta)
    if is_group:
        try:
            await update.message.delete()
        except Exception:
            pass

    await send_full_analysis(
        context.bot,
        chat.id, symbol, "PIYASA ANALIZ RAPORU",
        auto_del=is_group
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
    await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown", reply_markup=keyboard)


async def alarm_ekle_v2(update: Update, context):
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    args     = context.args or []

    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id, 
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
            await send_temp(context.bot, update.effective_chat.id, 
                "Kullanim: `/alarm_ekle BTCUSDT rsi 30 asagi`", parse_mode="Markdown"); return
        try:    rsi_lvl = float(args[2])
        except:
            await send_temp(context.bot, update.effective_chat.id, "RSI degeri sayi olmali.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,rsi_level,active)
                VALUES($1,$2,$3,0,'rsi',$4,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='rsi', rsi_level=$4, threshold=0, active=1
            """, user_id, username, symbol, rsi_lvl)
        direction_str = "asagi" if len(args) < 4 or args[3].lower() in ("asagi","aşağı") else "yukari"
        yon_str = "altina dusunce" if direction_str == "asagi" else "ustune cikinca"
        await send_temp(context.bot, update.effective_chat.id, 
            "✅ *" + symbol + "* RSI `" + str(rsi_lvl) + "` " + yon_str + " alarm verilecek!",
            parse_mode="Markdown"
        )
        return

    # Bant alarmı
    if args[1].lower() == "bant":
        if len(args) < 4:
            await send_temp(context.bot, update.effective_chat.id, 
                "Kullanim: `/alarm_ekle BTCUSDT bant 60000 70000`", parse_mode="Markdown"); return
        try:
            band_low  = float(args[2].replace(",","."))
            band_high = float(args[3].replace(",","."))
        except:
            await send_temp(context.bot, update.effective_chat.id, "Fiyat degerleri sayi olmali.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,band_low,band_high,active)
                VALUES($1,$2,$3,0,'band',$4,$5,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='band', band_low=$4, band_high=$5, threshold=0, active=1
            """, user_id, username, symbol, band_low, band_high)
        await send_temp(context.bot, update.effective_chat.id, 
            "✅ *" + symbol + "* `" + format_price(band_low) + " - " + format_price(band_high) +
            " USDT` bandından cikinca alarm verilecek!",
            parse_mode="Markdown"
        )
        return

    # % alarmı
    try:    threshold = float(args[1])
    except:
        await send_temp(context.bot, update.effective_chat.id, "Esik sayi olmalidir. Ornek: `3.5`", parse_mode="Markdown"); return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,active)
            VALUES($1,$2,$3,$4,'percent',1)
            ON CONFLICT(user_id,symbol) DO UPDATE
            SET threshold=$4, alarm_type='percent', active=1
        """, user_id, username, symbol, threshold)
    await send_temp(context.bot, update.effective_chat.id, 
        "✅ *" + symbol + "* icin `%" + str(threshold) + "` alarmi eklendi!",
        parse_mode="Markdown"
    )


async def alarm_sil(update: Update, context):
    user_id = update.effective_user.id
    if not context.args:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanim: `/alarm_sil BTCUSDT`", parse_mode="Markdown")
        return
    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_alarms WHERE user_id=$1 AND symbol=$2", user_id, symbol
        )
    if result == "DELETE 0":
        await send_temp(context.bot, update.effective_chat.id,
            f"`{symbol}` icin kayitli alarm bulunamadi.", parse_mode="Markdown")
    else:
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 `{symbol}` alarmi silindi.", parse_mode="Markdown")


async def my_alarm(update: Update, context):
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, threshold, active FROM user_alarms WHERE user_id=$1", user_id
        )
    if not rows:
        text = (
            "🔔 *Kisisel Alarm Paneli*\n━━━━━━━━━━━━━━━━━━\n"
            "Henuz aktif alarminiz yok.\n\n"
            "➕ Alarm eklemek icin:\n`/alarm_ekle BTCUSDT 3.5`"
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


async def favori_command(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []
    msg     = update.callback_query.message if update.callback_query else update.message

    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM favorites WHERE user_id=$1 ORDER BY symbol", user_id)
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "⭐ *Favori Listeniz Bos*\n━━━━━━━━━━━━━━━━━━\nEklemek icin:\n`/favori ekle BTCUSDT`",
                parse_mode="Markdown"); return
        syms = [r["symbol"] for r in rows]
        text = "⭐ *Favorileriniz*\n━━━━━━━━━━━━━━━━━━\n" + "".join(f"• `{s}`\n" for s in syms)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Hepsini Analiz Et", callback_data="fav_analiz"),
            InlineKeyboardButton("🗑 Tumunu Sil",        callback_data=f"fav_deleteall_{user_id}")
        ]])
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if args[0].lower() == "ekle":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/favori ekle BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO favorites(user_id,symbol) VALUES($1,$2) ON CONFLICT DO NOTHING", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            "⭐ `" + symbol + "` favorilere eklendi!", parse_mode="Markdown"); return

    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/favori sil BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM favorites WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            "🗑 `" + symbol + "` favorilerden silindi.", parse_mode="Markdown"); return

    if args[0].lower() == "analiz":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM favorites WHERE user_id=$1", user_id)
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "⭐ Favori listeniz bos.", parse_mode="Markdown"); return
        await send_temp(context.bot, update.effective_chat.id,
            "📊 *" + str(len(rows)) + " coin analiz ediliyor...*", parse_mode="Markdown")
        for r in rows:
            await send_full_analysis(context.bot, update.effective_chat.id, r["symbol"], "⭐ FAVORİ ANALİZ")
            await asyncio.sleep(1.5)
        return

    await send_temp(context.bot, update.effective_chat.id,
        "Kullanim:\n`/favori ekle BTCUSDT`\n`/favori sil BTCUSDT`\n`/favori liste`\n`/favori analiz`",
        parse_mode="Markdown"
    )


async def alarm_duraklat(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []
    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanim: `/alarm_duraklat BTCUSDT 2` (saat)", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:    saat = float(args[1])
    except:
        await send_temp(context.bot, update.effective_chat.id, "Saat sayi olmali.", parse_mode="Markdown"); return
    until = datetime.utcnow() + timedelta(hours=saat)
    async with db_pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE user_alarms SET paused_until=$1 WHERE user_id=$2 AND symbol=$3",
            until, user_id, symbol
        )
    if r == "UPDATE 0":
        await send_temp(context.bot, update.effective_chat.id,
            f"`{symbol}` icin alarm bulunamadi.", parse_mode="Markdown")
    else:
        await send_temp(context.bot, update.effective_chat.id,
            f"⏸ *{symbol}* alarmi `{int(saat)} saat` duraklatildi. "
            f"Tekrar aktif: `{until.strftime('%H:%M')} UTC`",
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
        await send_temp(context.bot, update.effective_chat.id, 
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
    await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")


# ================= FİYAT HEDEFİ =================

async def hedef_command(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []

    # Liste göster
    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, target, direction FROM price_targets WHERE user_id=$1 AND triggered=0 ORDER BY symbol",
                user_id
            )
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "🎯 *Fiyat Hedefleri*\n━━━━━━━━━━━━━━━━━━\n"
                "Henuz hedef yok.\n\n"
                "Eklemek icin:\n`/hedef BTCUSDT 70000` — fiyata ulaşınca bildir\n"
                "`/hedef sil BTCUSDT` — hedefi sil",
                parse_mode="Markdown")
            return
        text = "🎯 *Aktif Fiyat Hedefleriniz*\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            yon = "📈 Yukari" if r["direction"] == "up" else "📉 Asagi"
            text += f"• `{r['symbol']}` → `{format_price(r['target'])} USDT` {yon}\n"
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
        return

    # Sil
    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/hedef sil BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM price_targets WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 `{symbol}` hedefleri silindi.", parse_mode="Markdown")
        return

    # Ekle: /hedef BTCUSDT 70000
    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanim: `/hedef BTCUSDT 70000`", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:
        target = float(args[1].replace(",","."))
    except:
        await send_temp(context.bot, update.effective_chat.id,
            "Hedef fiyat sayi olmali. Ornek: `70000`", parse_mode="Markdown"); return

    # Mevcut fiyatı çek, yön belirle
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINANCE_24H}?symbol={symbol}",
                                   timeout=aiohttp.ClientTimeout(total=5)) as resp:
                ticker = await resp.json()
        cur_price = float(ticker["lastPrice"])
        direction = "up" if target > cur_price else "down"
    except:
        direction = "up"
        cur_price = 0

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO price_targets(user_id, symbol, target, direction)
            VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id,symbol,target) DO UPDATE SET triggered=0, direction=$4
        """, user_id, symbol, target, direction)

    yon_str = "ulaşınca" if direction == "up" else "düşünce"
    await send_temp(context.bot, update.effective_chat.id,
        f"🎯 *{symbol}* `{format_price(target)} USDT` fiyatına {yon_str} bildirim alacaksınız!\n"
        f"_Şu an: `{format_price(cur_price)} USDT`_",
        parse_mode="Markdown")


async def hedef_job(context: ContextTypes.DEFAULT_TYPE):
    """Fiyat hedeflerini kontrol eder, tetiklenenleri bildirir."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, symbol, target, direction FROM price_targets WHERE triggered=0"
            )
        if not rows:
            return

        for row in rows:
            prices = price_memory.get(row["symbol"])
            if not prices:
                continue
            cur = prices[-1][1]
            hit = (row["direction"] == "up"   and cur >= row["target"]) or \
                  (row["direction"] == "down"  and cur <= row["target"])
            if not hit:
                continue

            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE price_targets SET triggered=1 WHERE id=$1", row["id"])

            yon = "📈 YUKSELDİ" if row["direction"] == "up" else "📉 DÜŞTÜ"
            text = (
                f"🎯 *FİYAT HEDEFİ ULAŞTI!*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 *{row['symbol']}*\n"
                f"🏁 Hedef: `{format_price(row['target'])} USDT`\n"
                f"💵 Şu an: `{format_price(cur)} USDT`\n"
                f"{yon}"
            )
            try:
                await context.bot.send_message(row["user_id"], text, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Hedef bildirimi gönderilemedi ({row['user_id']}): {e}")
    except Exception as e:
        log.error(f"hedef_job hatasi: {e}")


# ================= KAR/ZARAR HESABI =================

async def kar_command(update: Update, context):
    user_id = update.effective_user.id
    args    = context.args or []

    # Kayıtlı pozisyonları göster
    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, amount, buy_price, note FROM kar_pozisyonlar WHERE user_id=$1 ORDER BY symbol",
                user_id
            )
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "💰 *Kar/Zarar Takibi*\n━━━━━━━━━━━━━━━━━━\n"
                "Kayıtlı pozisyon yok.\n\n"
                "Eklemek icin:\n`/kar BTCUSDT 0.5 60000` — miktar alış_fiyatı\n"
                "`/kar sil BTCUSDT` — pozisyonu sil",
                parse_mode="Markdown")
            return

        text = "💰 *Pozisyonlarınız*\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            prices = price_memory.get(r["symbol"])
            if prices:
                cur = prices[-1][1]
            else:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{BINANCE_24H}?symbol={r['symbol']}",
                                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            t = await resp.json()
                    cur = float(t["lastPrice"])
                except:
                    cur = r["buy_price"]

            invested = r["amount"] * r["buy_price"]
            current_val = r["amount"] * cur
            pnl = current_val - invested
            pnl_pct = ((cur - r["buy_price"]) / r["buy_price"]) * 100
            icon = "🟢" if pnl >= 0 else "🔴"
            text += (
                f"{icon} `{r['symbol']}`\n"
                f"  Alış: `{format_price(r['buy_price'])}` × `{r['amount']}`\n"
                f"  Şu an: `{format_price(cur)}` → `{pnl_pct:+.2f}%`\n"
                f"  P&L: `{pnl:+.2f} USDT`\n\n"
            )
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
        return

    # Sil
    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/kar sil BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM kar_pozisyonlar WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 `{symbol}` pozisyonu silindi.", parse_mode="Markdown")
        return

    # Hızlı hesap: /kar BTCUSDT 0.5 60000 (kaydetmeden)
    if len(args) == 3:
        symbol = args[0].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        try:
            amount    = float(args[1].replace(",","."))
            buy_price = float(args[2].replace(",","."))
        except:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/kar BTCUSDT 0.5 60000`", parse_mode="Markdown"); return

        prices = price_memory.get(symbol)
        if prices:
            cur = prices[-1][1]
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{BINANCE_24H}?symbol={symbol}",
                                           timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        t = await resp.json()
                cur = float(t["lastPrice"])
            except:
                await send_temp(context.bot, update.effective_chat.id,
                    f"⚠️ `{symbol}` fiyatı alınamadı.", parse_mode="Markdown"); return

        invested    = amount * buy_price
        current_val = amount * cur
        pnl         = current_val - invested
        pnl_pct     = ((cur - buy_price) / buy_price) * 100
        icon        = "🟢" if pnl >= 0 else "🔴"

        text = (
            f"{icon} *{symbol} Kar/Zarar*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Alış Fiyatı : `{format_price(buy_price)} USDT`\n"
            f"📦 Miktar      : `{amount}`\n"
            f"💵 Şu An       : `{format_price(cur)} USDT`\n"
            f"📊 Değişim     : `{pnl_pct:+.2f}%`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💼 Yatırılan   : `{invested:.2f} USDT`\n"
            f"📈 Güncel Değer: `{current_val:.2f} USDT`\n"
            f"{'🟢 Kar' if pnl >= 0 else '🔴 Zarar'}        : `{pnl:+.2f} USDT`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Pozisyonu Kaydet", callback_data=f"kar_kaydet_{symbol}_{amount}_{buy_price}")
        ]])
        await send_temp(context.bot, update.effective_chat.id, text,
                        parse_mode="Markdown", reply_markup=keyboard)
        return

    await send_temp(context.bot, update.effective_chat.id,
        "💰 *Kar/Zarar Komutu*\n━━━━━━━━━━━━━━━━━━\n"
        "Hızlı hesap: `/kar BTCUSDT 0.5 60000`\n"
        "Liste: `/kar liste`\n"
        "Sil: `/kar sil BTCUSDT`",
        parse_mode="Markdown")


# ================= ÇOKLU ZAMAN DİLİMİ =================

async def mtf_command(update: Update, context):
    msg  = update.callback_query.message if update.callback_query else update.message
    args = context.args or []
    if not args:
        await send_temp(context.bot, update.effective_chat.id, "Kullanim: `/mtf BTCUSDT`", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await send_temp(context.bot, update.effective_chat.id, "⏳ Analiz yapiliyor...", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            k15m, k1h, k4h, k1d, k1w = await asyncio.gather(
                fetch_klines(session, symbol, "15m", limit=100),
                fetch_klines(session, symbol, "1h",  limit=100),
                fetch_klines(session, symbol, "4h",  limit=100),
                fetch_klines(session, symbol, "1d",  limit=100),
                fetch_klines(session, symbol, "1w",  limit=52),
            )

        def rsi_emoji(r):
            if r >= 70: return "🔴"
            elif r >= 55: return "🟡"
            elif r <= 30: return "🔵"
            elif r <= 45: return "🟡"
            else: return "🟢"

        def macd_str(data):
            _, hist = calc_macd(data)
            if hist > 0:   return "🟢+"
            elif hist < 0: return "🔴-"
            else:          return "⚪"

        def boll_str(data):
            pos = calc_bollinger(data, period=20)
            pos = max(0, min(100, pos))
            if pos >= 80:   return f"🔴üst({pos:.0f}%)"
            elif pos <= 20: return f"🔵alt({pos:.0f}%)"
            else:           return f"🟢ort({pos:.0f}%)"

        def ema_str(data):
            e9  = calc_ema(data, 9)
            e21 = calc_ema(data, 21)
            return "🟢" if e9 > e21 else "🔴"

        def obv_str(data):
            o = calc_obv_trend(data, lookback=10)
            return "🟢" if o == 1 else ("🔴" if o == -1 else "⚪")

        def tf_section(data, label):
            if not data or len(data) < 10:
                return f"`{label}` — veri yetersiz\n"
            ch   = calc_change(data)
            rsi  = calc_rsi(data, 14)
            yon  = "📈" if ch > 0 else "📉"
            re   = rsi_emoji(rsi)
            ms   = macd_str(data)
            bs   = boll_str(data)
            es   = ema_str(data)
            os_  = obv_str(data)
            return (
                f"*{label}* {yon} `{ch:+.2f}%`\n"
                f"  RSI `{rsi:.1f}` {re}  MACD {ms}  EMA {es}  OBV {os_}\n"
                f"  Boll: {bs}\n"
            )

        # Fibonacci seviyeleri (4h mumlardan — son 50 mum)
        def calc_fibo(data, lookback=50):
            if not data or len(data) < lookback:
                lookback = len(data)
            window = data[-lookback:]
            hi  = max(float(c[2]) for c in window)
            lo  = min(float(c[3]) for c in window)
            diff = hi - lo
            levels = {
                "0.0%":   hi,
                "23.6%":  hi - diff * 0.236,
                "38.2%":  hi - diff * 0.382,
                "50.0%":  hi - diff * 0.500,
                "61.8%":  hi - diff * 0.618,
                "78.6%":  hi - diff * 0.786,
                "100%":   lo,
            }
            cur = float(data[-1][4])
            # Fiyatın hemen altındaki ve üstündeki seviyeyi bul
            below = {k: v for k, v in levels.items() if v <= cur}
            above = {k: v for k, v in levels.items() if v >  cur}
            sup = max(below.items(), key=lambda x: x[1]) if below else None
            res = min(above.items(), key=lambda x: x[1]) if above else None
            return sup, res, hi, lo

        fib_sup, fib_res, swing_hi, swing_lo = calc_fibo(k4h)

        text = f"📊 *{symbol} — Çoklu Zaman Dilimi*\n━━━━━━━━━━━━━━━━━━\n\n"
        text += tf_section(k15m, "15 Dakika")
        text += "\n"
        text += tf_section(k1h,  "1 Saat")
        text += "\n"
        text += tf_section(k4h,  "4 Saat")
        text += "\n"
        text += tf_section(k1d,  "1 Gün")
        text += "\n"
        text += tf_section(k1w,  "1 Hafta")
        text += "\n━━━━━━━━━━━━━━━━━━\n"
        text += f"📐 *Fibonacci (4h — Son 50 mum)*\n"
        text += f"  Swing High: `{format_price(swing_hi)}`  Low: `{format_price(swing_lo)}`\n"
        if fib_sup:
            text += f"  🔵 Destek : `{format_price(fib_sup[1])}` _(Fib {fib_sup[0]})_\n"
        if fib_res:
            text += f"  🔴 Direnç : `{format_price(fib_res[1])}` _(Fib {fib_res[0]})_\n"
        text += "\n_🔵 Aşırı Satım  🟢 Normal  🔴 Aşırı Alım_"

        await wait.delete()
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
    except Exception as e:
        await wait.delete()
        log.error("MTF hatasi: " + str(e))
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Analiz sirasinda hata olustu.", parse_mode="Markdown")


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
            await send_temp(context.bot, update.effective_chat.id, 
                "⏰ *Zamanlanmis Gorevler*\n━━━━━━━━━━━━━━━━━━\nGorev yok.\n\n"
                "Eklemek icin:\n`/zamanla analiz BTCUSDT 09:00`\n`/zamanla rapor 08:00`",
                parse_mode="Markdown")
        else:
            text = "⏰ *Zamanlanmis Gorevler*\n━━━━━━━━━━━━━━━━━━\n"
            for r in rows:
                sym_str = "`" + r["symbol"] + "` " if r["symbol"] else ""
                text += "• " + r["task_type"] + " " + sym_str + "— `" + ("%02d:%02d" % (r["hour"],r["minute"])) + "` UTC\n"
            await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
        return

    if args[0].lower() == "sil":
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE scheduled_tasks SET active=0 WHERE chat_id=$1", chat_id)
        await send_temp(context.bot, update.effective_chat.id, "🗑 Gorevler silindi.", parse_mode="Markdown"); return

    if args[0].lower() == "analiz" and len(args) >= 3:
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        try:    h, m = map(int, args[2].split(":"))
        except:
            await send_temp(context.bot, update.effective_chat.id, "Saat formati: `09:00`", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scheduled_tasks(user_id,chat_id,task_type,symbol,hour,minute,active)
                VALUES($1,$2,'analiz',$3,$4,$5,1)
                ON CONFLICT(chat_id,task_type,symbol) DO UPDATE SET hour=$4,minute=$5,active=1
            """, user_id, chat_id, symbol, h, m)
        await send_temp(context.bot, update.effective_chat.id, 
            "⏰ Her gun `" + ("%02d:%02d" % (h,m)) + "` UTC'de *" + symbol + "* analizi gonderilecek!",
            parse_mode="Markdown"); return

    if args[0].lower() == "rapor" and len(args) >= 2:
        try:    h, m = map(int, args[1].split(":"))
        except:
            await send_temp(context.bot, update.effective_chat.id, "Saat formati: `08:00`", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scheduled_tasks(user_id,chat_id,task_type,symbol,hour,minute,active)
                VALUES($1,$2,'rapor','',$3,$4,1)
                ON CONFLICT(chat_id,task_type,symbol) DO UPDATE SET hour=$3,minute=$4,active=1
            """, user_id, chat_id, h, m)
        await send_temp(context.bot, update.effective_chat.id, 
            "⏰ Her Pazartesi `" + ("%02d:%02d" % (h,m)) + "` UTC'de haftalik rapor gonderilecek!",
            parse_mode="Markdown"); return

    await send_temp(context.bot, update.effective_chat.id, 
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
        [InlineKeyboardButton("🎯 Fiyat Hedefi",    callback_data="hedef_help"),
         InlineKeyboardButton("💰 Kar/Zarar",       callback_data="kar_help")],
        [InlineKeyboardButton("🛠 Admin Ayarlari",  callback_data="set_open")]
    ])
    welcome_text = (
        "👋 *Kripto Analiz Asistani*\n━━━━━━━━━━━━━━━━━━\n"
        "7/24 piyasayi izliyorum.\n\n"
        "💡 Analiz: `BTCUSDT` yaz\n"
        "🔔 % Alarm: `/alarm_ekle BTCUSDT 3.5`\n"
        "🎯 Fiyat Hedefi: `/hedef BTCUSDT 70000`\n"
        "💰 Kar/Zarar: `/kar BTCUSDT 0.5 60000`\n"
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
    elif q.data == "hedef_help":
        await q.message.reply_text(
            "🎯 *Fiyat Hedefi Bildirimi*\n━━━━━━━━━━━━━━━━━━\n"
            "Hedef fiyata ulaşınca DM bildirim alırsınız.\n\n"
            "Ekle: `/hedef BTCUSDT 70000`\n"
            "Liste: `/hedef liste`\n"
            "Sil: `/hedef sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data == "kar_help":
        await q.message.reply_text(
            "💰 *Kar/Zarar Hesabı*\n━━━━━━━━━━━━━━━━━━\n"
            "Hızlı hesap: `/kar BTCUSDT 0.5 60000`\n"
            "  miktar: 0.5 BTC, alış: 60000 USDT\n\n"
            "Pozisyon kaydet/takip: `/kar liste`\n"
            "Sil: `/kar sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data == "alarm_history":
        await alarm_gecmis(update, context)
    elif q.data.startswith("kar_kaydet_"):
        # /kar BTCUSDT 0.5 60000 → kaydet butonu
        parts = q.data.split("_")
        # format: kar_kaydet_SYMBOL_AMOUNT_BUYPRICE
        try:
            symbol    = parts[2]
            amount    = float(parts[3])
            buy_price = float(parts[4])
            user_id   = q.from_user.id
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO kar_pozisyonlar(user_id, symbol, amount, buy_price)
                    VALUES($1,$2,$3,$4)
                    ON CONFLICT(user_id,symbol) DO UPDATE SET amount=$3, buy_price=$4
                """, user_id, symbol, amount, buy_price)
            await q.message.reply_text(f"💾 `{symbol}` pozisyonu kaydedildi! `/kar liste` ile takip edebilirsiniz.",
                                       parse_mode="Markdown")
        except Exception as e:
            log.warning(f"kar_kaydet callback: {e}")
            await q.answer("Kayıt sırasında hata oluştu.", show_alert=True)
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
            # prices[0] en az 4 dakika önce olmalı (gerçek 5dk değişimi ölç)
            if now - prices[0][0] < timedelta(minutes=4):
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
            yon = "⚡🟢 5dk YUKSELIS UYARISI 🟢⚡" if ch5 > 0 else "⚡🔴 5dk DUSUS UYARISI 🔴⚡"
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, yon, threshold, ch5_override=round(ch5, 2))

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
        # prices[0] en az 4 dakika önce olmalı
        if now - prices[0][0] < timedelta(minutes=4):
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

        yon = "📈🟢🟢" if direction == "up" else "📉🔴🔴"
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
    # Slash menüsünde sadece /start göster
    await app.bot.set_my_commands([
        BotCommand("start", "Botu başlat / Ana menü")
    ])

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job,       interval=10,   first=30)
    app.job_queue.run_repeating(whale_job,       interval=120,  first=60)
    app.job_queue.run_repeating(scheduled_job,   interval=60,   first=10)
    app.job_queue.run_repeating(hedef_job,       interval=30,   first=45)

    # Grup komutları → doğrudan
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("top24",  top24))
    app.add_handler(CommandHandler("top5",   top5))
    app.add_handler(CommandHandler("market", market))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("set",    set_command))

    app.add_handler(CommandHandler("alarmim",        my_alarm_v2))
    app.add_handler(CommandHandler("alarm_ekle",     alarm_ekle_v2))
    app.add_handler(CommandHandler("alarm_sil",      alarm_sil))
    app.add_handler(CommandHandler("alarm_duraklat", alarm_duraklat))
    app.add_handler(CommandHandler("alarm_gecmis",   alarm_gecmis))
    app.add_handler(CommandHandler("favori",         favori_command))
    app.add_handler(CommandHandler("mtf",            mtf_command))
    app.add_handler(CommandHandler("zamanla",        zamanla_command))
    app.add_handler(CommandHandler("hedef",          hedef_command))
    app.add_handler(CommandHandler("kar",            kar_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("BOT AKTIF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
