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
DATABASE_URL  = os.getenv("DATABASE_URL")

BINANCE_24H    = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COOLDOWN_MINUTES  = 15
DEFAULT_THRESHOLD = 5.0
DEFAULT_MODE      = "both"
MAX_SYMBOLS       = 500

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
            ALTER TABLE groups
            ADD COLUMN IF NOT EXISTS member_delete_delay INTEGER DEFAULT 3600
        """)
        await conn.execute("""
            INSERT INTO groups (chat_id, threshold, mode, delete_delay)
            VALUES ($1, $2, $3, 30)
            ON CONFLICT (chat_id) DO NOTHING
        """, GROUP_CHAT_ID, DEFAULT_THRESHOLD, DEFAULT_MODE)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS price_targets (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                symbol      TEXT,
                target_price REAL,
                direction   TEXT,
                active      INTEGER DEFAULT 1,
                UNIQUE(user_id, symbol, target_price)
            )
        """)
        # Migration: eski "target" kolonunu "target_price" olarak ekle (eğer yoksa)
        await conn.execute("""
            ALTER TABLE price_targets
            ADD COLUMN IF NOT EXISTS target_price REAL
        """)
        await conn.execute("""
            ALTER TABLE price_targets
            ADD COLUMN IF NOT EXISTS direction TEXT
        """)
        await conn.execute("""
            ALTER TABLE price_targets
            ADD COLUMN IF NOT EXISTS active INTEGER DEFAULT 1
        """)
        # NULL olan active değerlerini 1 yap
        await conn.execute("""
            UPDATE price_targets SET active=1 WHERE active IS NULL
        """)
        # UNIQUE constraint yoksa ekle (hata verirse zaten var demek)
        try:
            await conn.execute("""
                ALTER TABLE price_targets
                ADD CONSTRAINT price_targets_user_symbol_target_uniq
                UNIQUE(user_id, symbol, target_price)
            """)
        except Exception:
            pass  # zaten var
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sinyaller (
                id SERIAL PRIMARY KEY, user_id BIGINT, symbol TEXT,
                sinyal TEXT, puan INTEGER, fiyat_giris REAL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sr_alarmlar (
                id SERIAL PRIMARY KEY, user_id BIGINT, symbol TEXT,
                active INTEGER DEFAULT 1, UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id BIGINT, symbol TEXT, not_text TEXT DEFAULT '',
                UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anketler (
                id SERIAL PRIMARY KEY, chat_id BIGINT, soru TEXT,
                secenekler TEXT, aktif INTEGER DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anket_oylar (
                id SERIAL PRIMARY KEY, anket_id INTEGER, user_id BIGINT,
                secim INTEGER, UNIQUE(anket_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS volatilite_alarmlar (
                id SERIAL PRIMARY KEY, user_id BIGINT, symbol TEXT,
                esik REAL DEFAULT 5.0, active INTEGER DEFAULT 1,
                UNIQUE(user_id, symbol)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS brief_ayar (
                user_id BIGINT PRIMARY KEY, saat INTEGER DEFAULT 8,
                dakika INTEGER DEFAULT 0, active INTEGER DEFAULT 1
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_gecmis (
                id SERIAL PRIMARY KEY, user_id BIGINT, symbol TEXT,
                strateji TEXT, sonuc TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
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

price_memory:       dict = {}
cooldowns:          dict = {}
chart_cache:        dict = {}
whale_vol_mem:      dict = {}
scheduled_last_run: dict = {}

# ================= YARDIMCI =================

def get_number_emoji(n):
    emojis = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",
              6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣",10:"🔟"}
    return emojis.get(n, str(n))

def format_price(price):
    return f"{price:,.2f}" if price >= 1 else f"{price:.8g}"

# ================= RANK =================

COINGECKO_API = "https://api.coingecko.com/api/v3"
marketcap_rank_cache: dict = {}  # symbol -> rank (int), "_updated" -> datetime, "_fallback" -> bool

# CoinGecko sembol -> Binance sembol farklı olanlar
CG_TO_BINANCE: dict = {
    "MATICUSDT": "POLUSDT",   # Polygon yeniden adlandı
    "MIOTAUSDT": "IOTAUSDT",  # MIOTA -> IOTA
    "USDCUSDT":  None,        # Binance'de stablecoin, sıralama dışı
    "USDTUSDT":  None,
    "STETHUSDT": None,
    "WSTETHUSDT":None,
    "WEETHUSDT": None,
    "WBTCUSDT":  "WBTCUSDT",
}

def _build_binance_rank_cache(data: list) -> dict:
    """Binance 24hr ticker listesinden quoteVolume sıralaması üretir."""
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    cache = {"_updated": datetime.utcnow(), "_fallback": True}
    for i, c in enumerate(usdt, 1):
        cache[c["symbol"]] = i
    return cache

async def _refresh_marketcap_cache():
    """CoinGecko marketcap sıralaması, başarısız olursa Binance hacim sırası."""
    global marketcap_rank_cache
    now = datetime.utcnow()
    cg_cache = {}
    try:
        async with aiohttp.ClientSession() as session:
            for page in range(1, 6):
                url = (
                    f"{COINGECKO_API}/coins/markets"
                    f"?vs_currency=usd&order=market_cap_desc"
                    f"&per_page=100&page={page}&sparkline=false"
                )
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status == 429:
                            log.warning("CoinGecko rate-limit (429)")
                            break
                        if resp.status != 200:
                            log.warning(f"CoinGecko HTTP {resp.status}")
                            break
                        coins = await resp.json()
                        if not isinstance(coins, list) or not coins:
                            break
                        for coin in coins:
                            raw_sym = (coin.get("symbol") or "").upper()
                            mc_rank = coin.get("market_cap_rank")
                            if not mc_rank:
                                continue
                            cg_sym = raw_sym + "USDT"
                            # Mapping tablosunda varsa Binance sembolüne çevir
                            if cg_sym in CG_TO_BINANCE:
                                binance_sym = CG_TO_BINANCE[cg_sym]
                            else:
                                binance_sym = cg_sym
                            if binance_sym and binance_sym not in cg_cache:
                                cg_cache[binance_sym] = int(mc_rank)
                except asyncio.TimeoutError:
                    log.warning(f"CoinGecko sayfa {page} timeout")
                    break
                await asyncio.sleep(1.5)
    except Exception as e:
        log.warning(f"CoinGecko hata: {e}")

    if len(cg_cache) >= 50:
        marketcap_rank_cache = {"_updated": now, "_fallback": False, **cg_cache}
        log.info(f"MarketCap cache: CoinGecko {len(cg_cache)} coin")
        return

    # Fallback: Binance quoteVolume
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        marketcap_rank_cache = _build_binance_rank_cache(data)
        log.info(f"MarketCap cache: Binance fallback {len(marketcap_rank_cache)-2} coin")
    except Exception as e:
        log.warning(f"Binance fallback hata: {e}")

async def marketcap_refresh_job(context):
    """10 dakikada bir cache'i yenileyen arka plan job'u."""
    await _refresh_marketcap_cache()

async def get_coin_rank(symbol: str):
    """Cache'den anlık okur. Bulamazsa base sembol ile fuzzy arama yapar."""
    if not marketcap_rank_cache.get("_updated"):
        await _refresh_marketcap_cache()

    rank = marketcap_rank_cache.get(symbol)

    # Direkt bulunamadıysa base sembolle ara (leveraged token'ları atla)
    if rank is None and symbol.endswith("USDT"):
        base = symbol[:-4]
        leveraged = any(base.endswith(x) for x in ("3L","3S","UP","DOWN","BULL","BEAR","LONG","SHORT"))
        if not leveraged:
            for key, val in marketcap_rank_cache.items():
                if isinstance(val, int) and not key.startswith("_"):
                    key_base = key[:-4] if key.endswith("USDT") else key
                    if key_base == base:
                        rank = val
                        break

    total = sum(1 for k in marketcap_rank_cache if not k.startswith("_"))
    return rank, total

def rank_emoji(rank):
    if rank is None:   return ""
    if rank <= 10:     return "🥇"
    if rank <= 30:     return "🥈"
    if rank <= 100:    return "🥉"
    return "🏅"

# ================= DİĞER YARDIMCILAR =================

def calc_support_resistance(k4h_data):
    if not k4h_data or len(k4h_data) < 10:
        return None, None
    highs  = [float(c[2]) for c in k4h_data]
    lows   = [float(c[3]) for c in k4h_data]
    closes = [float(c[4]) for c in k4h_data]
    cur    = closes[-1]
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append(lows[i])
    destek = max((v for v in swing_lows  if v < cur), default=None)
    direnc = min((v for v in swing_highs if v > cur), default=None)
    return destek, direnc

def calc_volume_anomaly(k1h_data):
    if not k1h_data or len(k1h_data) < 5:
        return None
    vols = [float(c[5]) for c in k1h_data]
    avg  = sum(vols[:-1]) / len(vols[:-1])
    if avg == 0:
        return None
    return round(vols[-1] / avg, 2)

async def fetch_market_badge():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        usdt = [x for x in data if x["symbol"].endswith("USDT")]
        changes = [float(x["priceChangePercent"]) for x in usdt]
        avg = sum(changes) / len(changes) if changes else 0
        btc = next((x for x in usdt if x["symbol"] == "BTCUSDT"), None)
        btc_vol = float(btc["quoteVolume"]) if btc else 0
        total_vol = sum(float(x["quoteVolume"]) for x in usdt)
        btc_dom = round((btc_vol / total_vol) * 100, 1) if total_vol > 0 else 0
        mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
        return mood, btc_dom, round(avg, 2)
    except Exception:
        return None, None, None

# Bekleyen silme görevleri — restart sonrası kurtarma için
_pending_deletes: list[tuple] = []   # (delete_at_ts, chat_id, message_id)

async def auto_delete(bot, chat_id, message_id, delay=30):
    import time as _t
    delete_at = _t.time() + delay
    _pending_deletes.append((delete_at, chat_id, message_id))
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    finally:
        try:
            _pending_deletes.remove((delete_at, chat_id, message_id))
        except ValueError:
            pass

async def replay_pending_deletes(bot):
    """Bot başlarken bekleyen silme işlemlerini yeniden zamanla."""
    import time as _t
    now = _t.time()
    for (delete_at, chat_id, message_id) in list(_pending_deletes):
        remaining = delete_at - now
        if remaining <= 0:
            # Zaman geçmiş — hemen sil
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            try:
                _pending_deletes.remove((delete_at, chat_id, message_id))
            except ValueError:
                pass
        else:
            asyncio.create_task(auto_delete(bot, chat_id, message_id, remaining))

async def get_delete_delay() -> int:
    try:
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT delete_delay FROM groups WHERE chat_id=$1", GROUP_CHAT_ID
            )
        return int(r["delete_delay"]) if r and r["delete_delay"] else 30
    except Exception:
        return 30

async def send_temp(bot, chat_id, text, delay=None, **kwargs):
    msg = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type in ("group", "supergroup"):
            d = delay if delay is not None else await get_delete_delay()
            asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, d))
    except Exception:
        pass
    return msg

# ================= MUM GRAFİĞİ =================

async def generate_candlestick_chart(symbol: str):
    if symbol in chart_cache:
        cached_at, buf_data = chart_cache[symbol]
        if datetime.utcnow() - cached_at < timedelta(minutes=5):
            return io.BytesIO(buf_data)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval=4h&limit=60",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        if not data or isinstance(data, dict) or len(data) < 10:
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
        buf_data = buf.getvalue()
        chart_cache[symbol] = (datetime.utcnow(), buf_data)
        return io.BytesIO(buf_data)
    except Exception as e:
        log.error(f"Grafik hatasi ({symbol}): {e}")
        return None

# ================= ANALİZ =================

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
    try:
        closes = [float(x[4]) for x in data]
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
    try:
        closes = [float(x[4]) for x in data]
        if len(closes) < slow + signal:
            return 0.0, 0.0
        k_fast = 2 / (fast + 1)
        k_slow = 2 / (slow + 1)
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
        price_up = closes[-1] > closes[mid]
        rsi_up   = rsi_series[-1] > rsi_series[len(rsi_series)//2]
        if price_up and not rsi_up and rsi_series[-1] > 60:
            return "bearish"
        if not price_up and rsi_up and rsi_series[-1] < 40:
            return "bullish"
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
    if rsi <= 0:    return 50.0
    if rsi <= 30:   return _clamp(100 - rsi * 0.93)
    elif rsi <= 50: return _clamp(72 - (rsi - 30) * 1.1)
    elif rsi <= 70: return _clamp(50 - (rsi - 50) * 1.1)
    else:           return _clamp(28 - (rsi - 70) * 0.93)

def _ch_score(ch, scale=5.0):
    raw = 50 + (ch / scale) * 25
    return _clamp(raw)

def _vol_bonus(vol24, ch, pos_bonus=4.0, neg_bonus=-4.0):
    if vol24 <= 0:
        return 0.0
    import math
    vol_factor = max(0, math.log10(vol24 / 1_000_000)) / 3.0
    vol_factor = min(vol_factor, 1.0)
    if ch > 0:   return pos_bonus * vol_factor
    elif ch < 0: return neg_bonus * vol_factor
    return 0.0

def calc_score_hourly(ticker, k1h_series, k15m, k5m, rsi_1h):
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

    s_rsi14  = _rsi_score(rsi14)
    s_rsi7   = _rsi_score(rsi7)
    rsi_mom  = _clamp(50 + (rsi7 - rsi14) * 1.5)
    s_stoch  = _clamp(100 - stoch_rsi) if stoch_rsi > 50 else _clamp(50 + stoch_rsi)
    s_5m     = _ch_score(ch5m,  scale=3.0)
    s_15m    = _ch_score(ch15m, scale=4.0)
    s_1h     = _ch_score(ch1h,  scale=5.0)
    ema_score = 65.0 if ema9 > ema21 else 35.0
    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 20)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 20)
    macd_score = _clamp(macd_score)
    obv_score = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)

    score = (
        s_rsi14   * 0.20 + s_rsi7    * 0.12 + rsi_mom   * 0.08 +
        s_stoch   * 0.10 + s_5m      * 0.12 + s_15m     * 0.08 +
        s_1h      * 0.08 + ema_score * 0.10 + macd_score* 0.08 +
        obv_score * 0.04
    )
    score += _vol_bonus(vol24, ch5m)
    score = _clamp(score)
    label, bar = _score_label(score)
    return round(score), label, bar

def calc_score_daily(ticker, k4h_series, k1h_series, k1d_series):
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

    s_rsi_4h  = _rsi_score(rsi14_4h)
    s_rsi_1h  = _rsi_score(rsi14_1h)
    s_stoch   = _clamp(100 - stoch_4h) if stoch_4h > 50 else _clamp(50 + stoch_4h)
    s_4h      = _ch_score(ch4h,  scale=5.0)
    s_24h     = _ch_score(ch24h, scale=8.0)
    ema_score = 65.0 if ema21_4h > ema55_4h else 35.0
    boll_score = _clamp(100 - boll_pos)
    if 35 < boll_pos < 65:
        boll_score = 50.0
    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 15)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 15)
    macd_score = _clamp(macd_score)
    obv_score  = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)
    vol_dir    = _clamp(50 + (ch24 / max(volat, 0.5)) * 10)

    score = (
        s_rsi_4h  * 0.20 + s_rsi_1h  * 0.10 + s_stoch   * 0.08 +
        s_4h      * 0.15 + s_24h     * 0.12 + ema_score * 0.12 +
        boll_score* 0.08 + macd_score* 0.08 + obv_score * 0.05 +
        vol_dir   * 0.02
    )
    score += _vol_bonus(vol24, ch24, pos_bonus=5.0, neg_bonus=-5.0)
    score = _clamp(score)
    label, bar = _score_label(score)
    return round(score), label, bar

def calc_score_weekly(ticker, k1d_series, k1w_series):
    rsi14_1d  = calc_rsi(k1d_series, 14)
    rsi14_1w  = calc_rsi(k1w_series, 14)
    stoch_1d  = calc_stoch_rsi(k1d_series)
    macd_val, macd_hist = calc_macd(k1d_series)
    ema50_1d  = calc_ema(k1d_series, 50)
    ema200_1d = calc_ema(k1d_series, min(200, len(k1d_series)))
    boll_pos  = calc_bollinger(k1d_series, period=20)
    obv_trend = calc_obv_trend(k1d_series, lookback=20)

    ch7d  = calc_change(k1d_series[-7:]) if k1d_series and len(k1d_series) >= 7  else 0
    ch30d = calc_change(k1d_series)      if k1d_series and len(k1d_series) >= 5  else 0
    ch4w  = calc_change(k1w_series[-4:]) if k1w_series and len(k1w_series) >= 4  else 0
    vol24 = float(ticker.get("quoteVolume", 0))
    ch24  = float(ticker.get("priceChangePercent", 0))

    s_rsi_1d  = _rsi_score(rsi14_1d)
    s_rsi_1w  = _rsi_score(rsi14_1w)
    s_stoch   = _clamp(100 - stoch_1d) if stoch_1d > 50 else _clamp(50 + stoch_1d)
    s_7d      = _ch_score(ch7d,  scale=12.0)
    s_4w      = _ch_score(ch4w,  scale=20.0)
    s_30d     = _ch_score(ch30d, scale=30.0)
    ema_score = 70.0 if ema50_1d > ema200_1d else 30.0
    if macd_hist > 0:
        macd_score = _clamp(55 + abs(macd_hist) / (abs(macd_val) + 1e-10) * 12)
    else:
        macd_score = _clamp(45 - abs(macd_hist) / (abs(macd_val) + 1e-10) * 12)
    macd_score = _clamp(macd_score)
    obv_score = 65.0 if obv_trend == 1 else (35.0 if obv_trend == -1 else 50.0)

    score = (
        s_rsi_1d  * 0.18 + s_rsi_1w  * 0.18 + s_stoch   * 0.06 +
        s_7d      * 0.15 + s_4w      * 0.10 + s_30d     * 0.08 +
        ema_score * 0.12 + macd_score* 0.08 + obv_score * 0.05
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
            fetch_klines(session, symbol, "4h",  limit=2),
            fetch_klines(session, symbol, "1h",  limit=2),
            fetch_klines(session, symbol, "5m",  limit=2),
            fetch_klines(session, symbol, "1h",  limit=100),
            fetch_klines(session, symbol, "1d",  limit=30),
            fetch_klines(session, symbol, "15m", limit=20),
            fetch_klines(session, symbol, "4h",  limit=50),
            fetch_klines(session, symbol, "1h",  limit=24),
            fetch_klines(session, symbol, "1w",  limit=12),
            fetch_klines(session, symbol, "4h",  limit=100),
            fetch_klines(session, symbol, "1d",  limit=100),
        )

    return ticker, k4h, k1h_2, k5m, k1h_100, k1d, k15m, k4h_42, k1h_24, k1w, k4h_100, k1d_100


# ═══════════════════════════════════════════════════════════
# 1. SİNYAL SİSTEMİ
# ═══════════════════════════════════════════════════════════

def calc_signal(ticker, k1h, k4h, k1d):
    try:
        rsi_1h  = calc_rsi(k1h, 14)
        rsi_4h  = calc_rsi(k4h, 14)
        macd_v1, macd_h1 = calc_macd(k1h)
        macd_v4, macd_h4 = calc_macd(k4h)
        ema9_1h  = calc_ema(k1h, 9)
        ema21_1h = calc_ema(k1h, 21)
        ema21_4h = calc_ema(k4h, 21)
        ema55_4h = calc_ema(k4h, 55)
        ch24     = float(ticker.get("priceChangePercent", 0))

        puan = 50
        if rsi_1h < 30:   puan += 15
        elif rsi_1h < 45: puan += 7
        elif rsi_1h > 70: puan -= 15
        elif rsi_1h > 55: puan -= 7
        if rsi_4h < 35:   puan += 10
        elif rsi_4h > 65: puan -= 10
        if macd_h1 > 0 and macd_v1 > 0: puan += 12
        elif macd_h1 < 0 and macd_v1 < 0: puan -= 12
        elif macd_h1 > 0: puan += 6
        elif macd_h1 < 0: puan -= 6
        if macd_h4 > 0: puan += 8
        elif macd_h4 < 0: puan -= 8
        if ema9_1h > ema21_1h:  puan += 8
        else:                    puan -= 8
        if ema21_4h > ema55_4h: puan += 7
        else:                    puan -= 7
        if ch24 > 3:   puan += 5
        elif ch24 < -3: puan -= 5

        puan = max(0, min(100, puan))
        if puan >= 65:
            sinyal = "🟢 AL";    emoji = "🚀"; guc = "Güçlü" if puan >= 75 else "Orta"
        elif puan <= 35:
            sinyal = "🔴 SAT";   emoji = "⚠️"; guc = "Güçlü" if puan <= 25 else "Orta"
        else:
            sinyal = "🟡 BEKLE"; emoji = "⏳"; guc = "Nötr"

        gerekce = []
        if rsi_1h < 35:  gerekce.append("RSI aşırı satımda")
        if rsi_1h > 65:  gerekce.append("RSI aşırı alımda")
        if macd_h1 > 0:  gerekce.append("MACD pozitif")
        if macd_h1 < 0:  gerekce.append("MACD negatif")
        if ema9_1h > ema21_1h: gerekce.append("EMA yukarı kesiş")
        else:                   gerekce.append("EMA aşağı kesiş")
        if ema21_4h > ema55_4h: gerekce.append("4s trend yukarı")
        else:                    gerekce.append("4s trend aşağı")

        return puan, sinyal, emoji, guc, gerekce
    except:
        return 50, "🟡 BEKLE", "⏳", "Nötr", []


async def sinyal_command(update: Update, context):
    if not await check_group_access(update, context, "Sinyal"):
        return
    args = context.args or []
    if not args:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>📡 Sinyal Sistemi</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/sinyal BTCUSDT</code>\n\n"
            "RSI + MACD + EMA kombinasyonu ile\n"
            "🟢 AL / 🔴 SAT / 🟡 BEKLE sinyali\n\n"
            "Geçmiş: /sinyal_gecmis",
            parse_mode="HTML")
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    user_id = update.effective_user.id

    wait = await send_temp(context.bot, update.effective_chat.id,
        f"📡 <code>{symbol}</code> için sinyal hesaplanıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            ticker_r, k1h, k4h, k1d = await asyncio.gather(
                session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5)),
                fetch_klines(session, symbol, "1h", limit=100),
                fetch_klines(session, symbol, "4h", limit=100),
                fetch_klines(session, symbol, "1d", limit=30),
            )
            ticker = await ticker_r.json()

        if "code" in ticker or not ticker.get("lastPrice"):
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id,
                f"⚠️ <code>{symbol}</code> bulunamadı.", parse_mode="HTML")
            return

        price = float(ticker["lastPrice"])
        puan, sinyal, emoji, guc, gerekce = calc_signal(ticker, k1h, k4h, k1d)

        filled = round(puan / 10)
        bar = "█" * filled + "░" * (10 - filled)

        text = (
            f"{emoji} <b>{symbol} — Sinyal Analizi</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Fiyat : <code>{format_price(price)} USDT</code>\n"
            f"📡 Sinyal: <b>{sinyal}</b> ({guc})\n"
            f"🎯 Güç   : <code>{bar}</code> <code>{puan}/100</code>\n\n"
            f"📋 <b>Gerekçe:</b>\n"
        )
        for g in gerekce:
            text += f"  • {g}\n"
        text += "\n⚠️ <i>Bu sinyal yatırım tavsiyesi değildir.</i>"

        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sinyaller(user_id,symbol,sinyal,puan,fiyat_giris) VALUES($1,$2,$3,$4,$5)",
                    user_id, symbol, sinyal, puan, price)
        except Exception:
            pass

        try: await wait.delete()
        except: pass

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 MTF Analiz", callback_data=f"mtf_sym_{symbol}"),
            InlineKeyboardButton("📈 Geçmiş", callback_data="sinyal_gecmis"),
        ]])
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"sinyal_command: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


async def sinyal_gecmis_command(update: Update, context):
    if not await check_group_access(update, context, "Sinyal Geçmişi"):
        return
    user_id = update.effective_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol,sinyal,puan,fiyat_giris,created_at FROM sinyaller WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
            user_id)

    if not rows:
        await send_temp(context.bot, update.effective_chat.id,
            "📈 Henüz sinyal geçmişin yok.\n<code>/sinyal BTCUSDT</code> ile başla.",
            parse_mode="HTML")
        return

    al   = sum(1 for r in rows if "AL"    in r["sinyal"])
    sat  = sum(1 for r in rows if "SAT"   in r["sinyal"])
    bekl = sum(1 for r in rows if "BEKLE" in r["sinyal"])

    text = "<b>📈 Sinyal Geçmişin</b>\n━━━━━━━━━━━━━━━━━━\n"
    text += f"🟢 AL: <code>{al}</code>  🔴 SAT: <code>{sat}</code>  🟡 BEKLE: <code>{bekl}</code>\n\n"
    for r in rows[:10]:
        ts = r["created_at"].strftime("%d.%m %H:%M")
        text += f"<code>{r['symbol']:<12}</code> {r['sinyal']} <code>{r['puan']}/100</code> <i>{ts}</i>\n"

    await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 2. DESTEK/DİRENÇ ALARMI
# ═══════════════════════════════════════════════════════════

async def sr_alarm_command(update: Update, context):
    if not await check_group_access(update, context, "S/R Alarm"):
        return
    user_id = update.effective_user.id
    args = context.args or []

    if not args:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol, active FROM sr_alarmlar WHERE user_id=$1", user_id)
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "<b>🎯 Destek/Direnç Alarmı</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Fiyat destek/direncine yaklaşınca DM alarmı.\n\n"
                "Ekle: <code>/sr_alarm BTCUSDT</code>\n"
                "Sil : <code>/sr_alarm sil BTCUSDT</code>",
                parse_mode="HTML")
            return
        text = "<b>🎯 S/R Alarmlarım</b>\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            durum = "✅" if r["active"] else "⏹"
            text += f"{durum} <code>{r['symbol']}</code>\n"
        text += "\n<code>/sr_alarm sil SYMBOL</code> — sil"
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
        return

    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: <code>/sr_alarm sil BTCUSDT</code>", parse_mode="HTML")
            return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM sr_alarmlar WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 <code>{symbol}</code> S/R alarmı silindi.", parse_mode="HTML")
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sr_alarmlar(user_id,symbol,active) VALUES($1,$2,1) ON CONFLICT(user_id,symbol) DO UPDATE SET active=1",
            user_id, symbol)
    await send_temp(context.bot, update.effective_chat.id,
        f"✅ <code>{symbol}</code> S/R alarmı eklendi!\n"
        "Destek/direncine %2 yaklaşınca DM alacaksın.",
        parse_mode="HTML")


async def sr_alarm_job(context):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, symbol FROM sr_alarmlar WHERE active=1")
        if not rows: return
        now = datetime.utcnow()
        for row in rows:
            user_id = row["user_id"]
            symbol  = row["symbol"]
            key     = f"sr_{user_id}_{symbol}"
            if key in cooldowns and now - cooldowns[key] < timedelta(hours=2):
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    k4h = await fetch_klines(session, symbol, "4h", limit=50)
                    ticker_r = await session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5))
                    ticker = await ticker_r.json()
                price = float(ticker.get("lastPrice", 0))
                if price == 0: continue
                destek, direnc = calc_support_resistance(k4h)
                mesajlar = []
                if destek and abs(price - destek) / price < 0.02:
                    mesajlar.append(
                        f"🔵 <b>{symbol}</b> desteğe yakın!\n"
                        f"Destek: <code>{format_price(destek)}</code> | Fiyat: <code>{format_price(price)}</code>\n"
                        f"<i>%{abs(price-destek)/price*100:.1f} uzakta</i>")
                if direnc and abs(direnc - price) / price < 0.02:
                    mesajlar.append(
                        f"🔴 <b>{symbol}</b> dirençte!\n"
                        f"Direnç: <code>{format_price(direnc)}</code> | Fiyat: <code>{format_price(price)}</code>\n"
                        f"<i>%{abs(direnc-price)/price*100:.1f} uzakta</i>")
                for msg in mesajlar:
                    cooldowns[key] = now
                    await context.bot.send_message(user_id,
                        f"🎯 <b>S/R Alarm</b>\n━━━━━━━━━━━━━━━━━━\n{msg}",
                        parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        log.error(f"sr_alarm_job: {e}")


# ═══════════════════════════════════════════════════════════
# 3. VOLATİLİTE ALARMI
# ═══════════════════════════════════════════════════════════

async def volatilite_job(context):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, symbol, esik FROM volatilite_alarmlar WHERE active=1")
        if not rows: return
        now = datetime.utcnow()
        for row in rows:
            user_id = row["user_id"]
            symbol  = row["symbol"]
            esik    = row["esik"]
            key     = f"vol_{user_id}_{symbol}"
            if key in cooldowns and now - cooldowns[key] < timedelta(hours=4):
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    k1h = await fetch_klines(session, symbol, "1h", limit=48)
                if not k1h or len(k1h) < 24: continue
                son4  = [abs(float(c[4]) - float(c[1])) / float(c[1]) * 100 for c in k1h[-4:]]
                onc20 = [abs(float(c[4]) - float(c[1])) / float(c[1]) * 100 for c in k1h[-24:-4]]
                vol_son = sum(son4) / len(son4) if son4 else 0
                vol_ort = sum(onc20) / len(onc20) if onc20 else 0
                if vol_ort == 0: continue
                oran = vol_son / vol_ort
                if oran >= (1 + esik / 100):
                    cooldowns[key] = now
                    await context.bot.send_message(user_id,
                        f"⚡ <b>Volatilite Artışı — {symbol}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Son 4sa volatilite normalin <code>{oran:.1f}x</code> üstünde!\n"
                        f"Son 4sa: <code>%{vol_son:.2f}</code> | Genel ort: <code>%{vol_ort:.2f}</code>\n\n"
                        f"<i>Büyük hareket yaklaşıyor olabilir!</i>",
                        parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        log.error(f"volatilite_job: {e}")


# ═══════════════════════════════════════════════════════════
# 4. ATH / ATL TAKİBİ
# ═══════════════════════════════════════════════════════════

async def ath_command(update: Update, context):
    if not await check_group_access(update, context, "ATH/ATL"):
        return
    args = context.args or []
    if not args:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>🏆 ATH/ATL Takibi</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/ath BTCUSDT</code>\n"
            "52 haftalık yüksek/düşük ve ATH mesafesi.",
            parse_mode="HTML")
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await send_temp(context.bot, update.effective_chat.id,
        f"🏆 <code>{symbol}</code> ATH/ATL hesaplanıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            ticker_r, k1d = await asyncio.gather(
                session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5)),
                fetch_klines(session, symbol, "1d", limit=365),
            )
            ticker = await ticker_r.json()

        price = float(ticker.get("lastPrice", 0))
        if not price:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id, f"⚠️ <code>{symbol}</code> bulunamadı.", parse_mode="HTML")
            return

        if k1d and len(k1d) >= 7:
            ath_52h = max(float(c[2]) for c in k1d)
            atl_52h = min(float(c[3]) for c in k1d)
            ath_dist = (ath_52h - price) / ath_52h * 100
            atl_dist = (price - atl_52h) / atl_52h * 100
            ath_7d = max(float(c[2]) for c in k1d[-7:])
            atl_7d = min(float(c[3]) for c in k1d[-7:])

            uyari = ""
            if ath_dist < 5:  uyari = "\n🔥 <b>ATH'ye çok yakın!</b>"
            elif atl_dist < 5: uyari = "\n❄️ <b>ATL'ye çok yakın!</b>"

            text = (
                f"🏆 <b>{symbol} — ATH/ATL Analizi</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💵 Güncel: <code>{format_price(price)} USDT</code>\n\n"
                f"📅 <b>52 Haftalık</b>\n"
                f"  🔺 Yüksek: <code>{format_price(ath_52h)}</code> <i>(ATH'ye %{ath_dist:.1f} uzak)</i>\n"
                f"  🔻 Düşük : <code>{format_price(atl_52h)}</code> <i>(ATL'den %{atl_dist:.1f} yukarı)</i>\n\n"
                f"📅 <b>7 Günlük</b>\n"
                f"  🔺 Yüksek: <code>{format_price(ath_7d)}</code>\n"
                f"  🔻 Düşük : <code>{format_price(atl_7d)}</code>"
                f"{uyari}"
            )
        else:
            text = f"⚠️ <code>{symbol}</code> için yeterli veri yok."

        try: await wait.delete()
        except: pass
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"ath_command: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 5. SEKTÖR ANALİZİ
# ═══════════════════════════════════════════════════════════

SEKTOR_COINLER = {
    "🔷 Layer 1": ["BTCUSDT","ETHUSDT","SOLUSDT","AVAXUSDT","ADAUSDT","DOTUSDT","NEARUSDT","APTUSDT"],
    "⚡ Layer 2": ["MATICUSDT","ARBUSDT","OPUSDT","STRKUSDT"],
    "🌊 DeFi":    ["UNIUSDT","AAVEUSDT","MKRUSDT","CRVUSDT","COMPUSDT"],
    "🎮 GameFi":  ["AXSUSDT","SANDUSDT","MANAUSDT","GALAUSDT","IMXUSDT"],
    "🤖 AI":      ["FETUSDT","AGIXUSDT","WLDUSDT"],
    "🐶 Meme":    ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","BONKUSDT"],
}

async def sektor_command(update: Update, context):
    wait = await send_temp(context.bot, update.effective_chat.id,
        "📊 Sektör analizi yapılıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                tickers = await resp.json()

        ticker_map = {t["symbol"]: float(t.get("priceChangePercent", 0)) for t in tickers}
        text = "<b>📊 Sektör Performansı (24s)</b>\n━━━━━━━━━━━━━━━━━━\n"
        sektor_sonuc = []

        for sektor, coinler in SEKTOR_COINLER.items():
            degisimler = [ticker_map.get(c, 0) for c in coinler if c in ticker_map]
            if not degisimler: continue
            ort = sum(degisimler) / len(degisimler)
            sektor_sonuc.append((sektor, ort, degisimler))

        sektor_sonuc.sort(key=lambda x: x[1], reverse=True)

        for sektor, ort, degisimler in sektor_sonuc:
            icon = "🟢" if ort >= 0 else "🔴"
            en_iyi  = max(degisimler)
            en_kotu = min(degisimler)
            text += (
                f"{icon} <b>{sektor}</b>\n"
                f"  Ort: <code>{ort:+.2f}%</code> | 🔺<code>{en_iyi:+.1f}%</code> 🔻<code>{en_kotu:+.1f}%</code>\n"
            )

        usdt_all = [float(t.get("priceChangePercent", 0)) for t in tickers if t["symbol"].endswith("USDT")]
        piyasa_ort = sum(usdt_all) / len(usdt_all) if usdt_all else 0
        mood = "🐂 Boğa" if piyasa_ort > 1 else "🐻 Ayı" if piyasa_ort < -1 else "😐 Yatay"
        text += f"\n📈 <b>Genel Piyasa:</b> <code>{piyasa_ort:+.2f}%</code> — {mood}"

        try: await wait.delete()
        except: pass
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"sektor: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 6. PATTERN TANIMA
# ═══════════════════════════════════════════════════════════

def detect_patterns(k1d):
    patterns = []
    if not k1d or len(k1d) < 20: return patterns
    closes = [float(c[4]) for c in k1d]
    highs  = [float(c[2]) for c in k1d]
    lows   = [float(c[3]) for c in k1d]
    n = len(closes)

    try:
        win_lows  = lows[-20:-10]
        win_lows2 = lows[-10:]
        min1 = min(win_lows); min2 = min(win_lows2)
        if abs(min1 - min2) / min1 < 0.03:
            idx1 = lows.index(min1, n-20, n-10)
            idx2 = lows.index(min2, n-10, n)
            if closes[-1] > max(closes[idx1:idx2]):
                patterns.append("🔵 Çift Dip — Potansiyel yükseliş")
    except: pass

    try:
        win_highs  = highs[-20:-10]
        win_highs2 = highs[-10:]
        max1 = max(win_highs); max2 = max(win_highs2)
        if abs(max1 - max2) / max1 < 0.03:
            idx1 = highs.index(max1, n-20, n-10)
            idx2 = highs.index(max2, n-10, n)
            if closes[-1] < min(closes[idx1:idx2]):
                patterns.append("🔴 Çift Tepe — Potansiyel düşüş")
    except: pass

    try:
        if all(closes[i] > closes[i-1] for i in range(-5, 0)):
            if all(lows[i] > lows[i-1] for i in range(-5, 0)):
                patterns.append("📈 Yükselen Kanal — Trend yukarı")
    except: pass

    try:
        if all(closes[i] < closes[i-1] for i in range(-5, 0)):
            if all(highs[i] < highs[i-1] for i in range(-5, 0)):
                patterns.append("📉 Düşen Kanal — Trend aşağı")
    except: pass

    try:
        onceki10 = (closes[-11] - closes[-20]) / closes[-20] * 100 if closes[-20] else 0
        son5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] else 0
        if onceki10 > 10 and -5 < son5 < 0:
            patterns.append("🚩 Bull Flag — Konsolidasyon, kırılım bekleniyor")
    except: pass

    return patterns


async def pattern_command(update: Update, context):
    if not await check_group_access(update, context, "Pattern"):
        return
    args = context.args or []
    if not args:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>📐 Pattern Tanıma</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/pattern BTCUSDT</code>\n\n"
            "Tespit edilen formasyonlar:\n"
            "• Çift Dip / Çift Tepe\n"
            "• Yükselen / Düşen Kanal\n"
            "• Bull Flag",
            parse_mode="HTML")
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await send_temp(context.bot, update.effective_chat.id,
        f"📐 <code>{symbol}</code> pattern analizi yapılıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            k1d = await fetch_klines(session, symbol, "1d", limit=30)
            k4h = await fetch_klines(session, symbol, "4h", limit=60)

        patterns_1d = detect_patterns(k1d)
        patterns_4h = detect_patterns(k4h)
        try: await wait.delete()
        except: pass

        if not patterns_1d and not patterns_4h:
            await send_temp(context.bot, update.effective_chat.id,
                f"📐 <code>{symbol}</code> — Belirgin pattern bulunamadı.", parse_mode="HTML")
            return

        text = f"📐 <b>{symbol} — Pattern Analizi</b>\n━━━━━━━━━━━━━━━━━━\n"
        if patterns_1d:
            text += "📅 <b>Günlük (1D):</b>\n"
            for p in patterns_1d: text += f"  • {p}\n"
        if patterns_4h:
            text += "\n⏰ <b>4 Saatlik (4H):</b>\n"
            for p in patterns_4h: text += f"  • {p}\n"
        text += "\n⚠️ <i>Pattern yatırım tavsiyesi değildir.</i>"
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"pattern: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 7. DCA HESAPLAYICI
# ═══════════════════════════════════════════════════════════

async def dca_command(update: Update, context):
    if not await check_group_access(update, context, "DCA"):
        return
    args = context.args or []
    if len(args) < 3:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>💹 DCA Hesaplayıcı</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/dca BTCUSDT 100 12</code>\n"
            "<i>(symbol, aylık_dolar, kaç_ay)</i>\n\n"
            "Örnek: <code>/dca BTCUSDT 100 24</code>",
            parse_mode="HTML")
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:
        aylik_dolar = float(args[1].replace(",","."))
        ay_sayisi   = min(int(args[2]), 48)
    except:
        await send_temp(context.bot, update.effective_chat.id,
            "Hatalı format. Örnek: <code>/dca BTCUSDT 100 12</code>", parse_mode="HTML")
        return

    wait = await send_temp(context.bot, update.effective_chat.id, "💹 DCA hesaplanıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            k1m = await fetch_klines(session, symbol, "1M", limit=ay_sayisi+1)
            ticker_r = await session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5))
            ticker = await ticker_r.json()

        current_price = float(ticker.get("lastPrice", 0))
        if not current_price or not k1m:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id, f"⚠️ <code>{symbol}</code> için veri alınamadı.", parse_mode="HTML")
            return

        toplam_yatirim = toplam_adet = 0.0
        for mum in k1m[-ay_sayisi:]:
            fiyat = float(mum[1])
            if fiyat == 0: continue
            toplam_adet    += aylik_dolar / fiyat
            toplam_yatirim += aylik_dolar

        if toplam_adet == 0:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id, "⚠️ Hesaplama yapılamadı.", parse_mode="HTML")
            return

        ort_maliyet  = toplam_yatirim / toplam_adet
        guncel_deger = toplam_adet * current_price
        kar_zarar    = guncel_deger - toplam_yatirim
        kar_pct      = kar_zarar / toplam_yatirim * 100
        icon         = "🟢" if kar_zarar >= 0 else "🔴"

        try: await wait.delete()
        except: pass

        text = (
            f"💹 <b>DCA Analizi — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 Süre          : <code>{ay_sayisi} ay</code>\n"
            f"💰 Aylık yatırım : <code>${aylik_dolar:,.0f}</code>\n"
            f"💼 Toplam yatırım: <code>${toplam_yatirim:,.0f}</code>\n\n"
            f"📊 <b>Sonuç:</b>\n"
            f"  Ort. maliyet: <code>{format_price(ort_maliyet)} USDT</code>\n"
            f"  Güncel fiyat: <code>{format_price(current_price)} USDT</code>\n"
            f"  Toplam adet : <code>{toplam_adet:.6f}</code>\n"
            f"  Güncel değer: <code>${guncel_deger:,.2f}</code>\n"
            f"  {icon} Kar/Zarar: <code>${kar_zarar:+,.2f}</code> <code>({kar_pct:+.1f}%)</code>\n\n"
            f"<i>Geçmiş veriler gelecek performansı garanti etmez.</i>"
        )
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"dca: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 8. RİSK/ÖDÜL HESAPLAYICI
# ═══════════════════════════════════════════════════════════

async def rr_command(update: Update, context):
    if not await check_group_access(update, context, "Risk/Ödül"):
        return
    args = context.args or []
    if len(args) < 3:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>⚖️ Risk/Ödül Hesaplayıcı</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/rr giriş stop hedef</code>\n\n"
            "Örnek: <code>/rr 100 95 115</code>\n"
            "Sermaye ile: <code>/rr 100 95 115 1000</code>",
            parse_mode="HTML")
        return
    try:
        giris   = float(args[0].replace(",","."))
        stop    = float(args[1].replace(",","."))
        hedef   = float(args[2].replace(",","."))
        sermaye = float(args[3].replace(",",".")) if len(args) > 3 else None
    except:
        await send_temp(context.bot, update.effective_chat.id,
            "Hatalı format. Örnek: <code>/rr 100 95 115</code>", parse_mode="HTML")
        return

    risk = abs(giris - stop)
    odül = abs(hedef - giris)
    if risk == 0:
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Stop ve giriş aynı olamaz.", parse_mode="HTML")
        return

    oran     = odül / risk
    risk_pct = risk / giris * 100
    odül_pct = odül / giris * 100
    yon      = "📈 Long" if hedef > giris else "📉 Short"
    renk     = "🟢" if oran >= 2 else "🟡" if oran >= 1 else "🔴"

    text = (
        f"⚖️ <b>Risk/Ödül Analizi</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Yön   : {yon}\n"
        f"Giriş : <code>{format_price(giris)}</code>\n"
        f"Stop  : <code>{format_price(stop)}</code> <i>(-%{risk_pct:.1f})</i>\n"
        f"Hedef : <code>{format_price(hedef)}</code> <i>(+%{odül_pct:.1f})</i>\n\n"
        f"{renk} <b>R/R Oranı: 1 : {oran:.2f}</b>\n"
    )
    if sermaye:
        max_risk = sermaye * 0.02
        poz_byt  = max_risk / (risk / giris)
        adet     = poz_byt / giris
        text += (
            f"\n💼 <b>Pozisyon Boyutu (%2 Kural)</b>\n"
            f"  Sermaye: <code>${sermaye:,.0f}</code>\n"
            f"  Max risk: <code>${max_risk:,.2f}</code>\n"
            f"  Pozisyon: <code>${poz_byt:,.2f}</code>\n"
            f"  Adet: <code>{adet:.4f}</code>\n"
        )
    if oran < 1:    text += "\n❌ <i>R/R 1'den düşük — riskli işlem</i>"
    elif oran < 2:  text += "\n🟡 <i>R/R kabul edilebilir (1-2 arası)</i>"
    else:           text += "\n✅ <i>R/R iyi (2 ve üzeri)</i>"

    await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 9. GLOBAL MAKRO TAKİBİ
# ═══════════════════════════════════════════════════════════

MAKRO_TAKVIM = [
    {"isim": "FED Faiz Kararı", "ay": [1,3,5,6,7,9,11,12], "gun": 15, "aciklama": "Fed faiz kararı piyasaları doğrudan etkiler"},
    {"isim": "CPI Verisi",      "ay": list(range(1,13)),    "gun": 12, "aciklama": "ABD enflasyon verisi"},
    {"isim": "NFP (İstihdam)",  "ay": list(range(1,13)),    "gun": 5,  "aciklama": "ABD tarım dışı istihdam"},
]

async def makro_command(update: Update, context):
    now = datetime.utcnow()
    text = f"<b>🌐 Global Makro Takvim</b>\n━━━━━━━━━━━━━━━━━━\n📅 Bugün: <code>{now.strftime('%d.%m.%Y')}</code>\n\n"

    yaklaşan = []
    for etkinlik in MAKRO_TAKVIM:
        if now.month in etkinlik["ay"]:
            hedef_gun = datetime(now.year, now.month, min(etkinlik["gun"], 28))
            if hedef_gun < now:
                ay = now.month + 1 if now.month < 12 else 1
                yil = now.year if now.month < 12 else now.year + 1
                hedef_gun = datetime(yil, ay, min(etkinlik["gun"], 28))
            kalan = (hedef_gun - now).days
            yaklaşan.append((kalan, etkinlik["isim"], etkinlik["aciklama"], hedef_gun))

    yaklaşan.sort(key=lambda x: x[0])
    for kalan, isim, aciklama, tarih in yaklaşan[:6]:
        icon = "🔥" if kalan <= 3 else "⚠️" if kalan <= 7 else "📅"
        text += (
            f"{icon} <b>{isim}</b>\n"
            f"  Tarih : <code>{tarih.strftime('%d.%m.%Y')}</code>\n"
            f"  Kalan : <code>{kalan} gün</code>\n"
            f"  <i>{aciklama}</i>\n\n"
        )
    text += "<i>Makro veriler kripto volatilitesini artırabilir.</i>"
    await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 10. GÜNLÜK BRIEF
# ═══════════════════════════════════════════════════════════

async def brief_command(update: Update, context):
    if not await check_group_access(update, context, "Brief"):
        return
    user_id = update.effective_user.id
    args = context.args or []

    if args and args[0].lower() == "saat" and len(args) > 1:
        try:
            h, m = map(int, args[1].split(":"))
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO brief_ayar(user_id,saat,dakika,active) VALUES($1,$2,$3,1) ON CONFLICT(user_id) DO UPDATE SET saat=$2,dakika=$3,active=1",
                    user_id, h, m)
            await send_temp(context.bot, update.effective_chat.id,
                f"⏰ Günlük brief her gün <code>{h:02d}:{m:02d}</code> UTC'de gönderilecek!",
                parse_mode="HTML")
            return
        except:
            await send_temp(context.bot, update.effective_chat.id, "Format: <code>/brief saat 08:00</code>", parse_mode="HTML")
            return

    if args and args[0].lower() == "kapat":
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE brief_ayar SET active=0 WHERE user_id=$1", user_id)
        await send_temp(context.bot, update.effective_chat.id, "⏹ Günlük brief kapatıldı.", parse_mode="HTML")
        return

    await _send_brief(context.bot, user_id, update.effective_chat.id)


async def _send_brief(bot, user_id, chat_id):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                tickers = await resp.json()

        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        avg  = sum(float(t.get("priceChangePercent",0)) for t in usdt) / len(usdt)
        btc  = next((t for t in usdt if t["symbol"]=="BTCUSDT"), None)
        eth  = next((t for t in usdt if t["symbol"]=="ETHUSDT"), None)
        mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
        top3 = sorted(usdt, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:3]
        bot3 = sorted(usdt, key=lambda x: float(x.get("priceChangePercent",0)))[:3]

        async with db_pool.acquire() as conn:
            alarmlar    = await conn.fetch("SELECT symbol FROM user_alarms WHERE user_id=$1 AND active=1", user_id)
            hedefler    = await conn.fetch("SELECT symbol FROM price_targets WHERE user_id=$1 AND active=1", user_id)
            pozisyonlar = await conn.fetch("SELECT symbol, amount, buy_price FROM kar_pozisyonlar WHERE user_id=$1", user_id)

        now = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        text = (
            f"☀️ <b>Günlük Brief — {now} TR</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Piyasa:</b> {mood} <code>{avg:+.2f}%</code>\n"
        )
        if btc: text += f"₿ BTC: <code>{format_price(float(btc['lastPrice']))}</code> <code>{float(btc['priceChangePercent']):+.2f}%</code>\n"
        if eth: text += f"Ξ ETH: <code>{format_price(float(eth['lastPrice']))}</code> <code>{float(eth['priceChangePercent']):+.2f}%</code>\n"

        text += "\n🚀 <b>Günün Liderleri:</b>\n"
        for t in top3:
            text += f"  🟢 <code>{t['symbol']:<12}</code> <code>{float(t['priceChangePercent']):+.2f}%</code>\n"
        text += "\n📉 <b>Günün Kayıpları:</b>\n"
        for t in bot3:
            text += f"  🔴 <code>{t['symbol']:<12}</code> <code>{float(t['priceChangePercent']):+.2f}%</code>\n"

        if alarmlar:    text += f"\n🔔 <b>Aktif Alarmların:</b> <code>{len(alarmlar)}</code> adet\n"
        if hedefler:    text += f"🎯 <b>Aktif Hedeflerin:</b> <code>{len(hedefler)}</code> adet\n"

        if pozisyonlar:
            text += "\n💼 <b>Portföy Özeti:</b>\n"
            semboller = [r["symbol"] for r in pozisyonlar]
            canli = await _hedef_canli_fiyat(semboller)
            toplam_yat = toplam_gun = 0.0
            for r in pozisyonlar:
                cur = canli.get(r["symbol"], r["buy_price"])
                toplam_yat += r["amount"] * r["buy_price"]
                toplam_gun += r["amount"] * cur
            pnl = toplam_gun - toplam_yat
            pnl_pct = pnl / toplam_yat * 100 if toplam_yat else 0
            icon = "🟢" if pnl >= 0 else "🔴"
            text += (
                f"  Yatırım: <code>${toplam_yat:,.2f}</code>\n"
                f"  Güncel : <code>${toplam_gun:,.2f}</code>\n"
                f"  {icon} P&amp;L: <code>${pnl:+,.2f}</code> <code>({pnl_pct:+.1f}%)</code>\n"
            )

        text += "\n<i>İyi işlemler! 🎯</i>"
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"brief: {e}")


async def brief_job(context):
    try:
        now = datetime.utcnow()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, saat, dakika FROM brief_ayar WHERE active=1")
        for row in rows:
            if row["saat"] == now.hour and row["dakika"] == now.minute:
                key = f"brief_{row['user_id']}_{now.date()}"
                if key not in cooldowns:
                    cooldowns[key] = now
                    try:
                        await _send_brief(context.bot, row["user_id"], row["user_id"])
                    except Exception:
                        pass
    except Exception as e:
        log.error(f"brief_job: {e}")


# ═══════════════════════════════════════════════════════════
# 11. WATCHLIST
# ═══════════════════════════════════════════════════════════

async def watchlist_command(update: Update, context):
    if not await check_group_access(update, context, "Watchlist"):
        return
    user_id = update.effective_user.id
    args = context.args or []

    if not args or args[0].lower() == "liste":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol, not_text FROM watchlist WHERE user_id=$1 ORDER BY symbol", user_id)
        if not rows:
            await send_temp(context.bot, update.effective_chat.id,
                "<b>👀 Watchlist</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Watchlist boş.\n\n"
                "Ekle: <code>/watchlist ekle BTCUSDT</code>\n"
                "Ekle (notlu): <code>/watchlist ekle ETHUSDT DCA yapıyorum</code>\n"
                "Sil: <code>/watchlist sil BTCUSDT</code>\n"
                "Rapor: <code>/watchlist rapor</code>",
                parse_mode="HTML")
            return

        semboller = [r["symbol"] for r in rows]
        canli = await _hedef_canli_fiyat(semboller)
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                tickers_all = await resp.json()
        ticker_map = {t["symbol"]: float(t.get("priceChangePercent",0)) for t in tickers_all}

        text = "<b>👀 Watchlist</b>\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            price = canli.get(r["symbol"], 0)
            ch24  = ticker_map.get(r["symbol"], 0)
            icon  = "🟢" if ch24 >= 0 else "🔴"
            not_str = f" <i>{r['not_text']}</i>" if r["not_text"] else ""
            text += f"{icon} <code>{r['symbol']:<14}</code> <code>{format_price(price)}</code> <code>{ch24:+.2f}%</code>{not_str}\n"
        text += "\n<code>/watchlist rapor</code> — detaylı rapor"
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
        return

    if args[0].lower() == "ekle":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id, "Kullanım: <code>/watchlist ekle BTCUSDT</code>", parse_mode="HTML")
            return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        not_text = " ".join(args[2:]) if len(args) > 2 else ""
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO watchlist(user_id,symbol,not_text) VALUES($1,$2,$3) ON CONFLICT(user_id,symbol) DO UPDATE SET not_text=$3",
                user_id, symbol, not_text)
        await send_temp(context.bot, update.effective_chat.id,
            f"✅ <code>{symbol}</code> watchlist'e eklendi.", parse_mode="HTML")
        return

    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id, "Kullanım: <code>/watchlist sil BTCUSDT</code>", parse_mode="HTML")
            return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM watchlist WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 <code>{symbol}</code> watchlist'ten silindi.", parse_mode="HTML")
        return

    if args[0].lower() == "rapor":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol FROM watchlist WHERE user_id=$1", user_id)
        if not rows:
            await send_temp(context.bot, update.effective_chat.id, "Watchlist boş.", parse_mode="HTML")
            return
        wait = await send_temp(context.bot, update.effective_chat.id, "📊 Watchlist raporu hazırlanıyor...", parse_mode="HTML")
        text = "<b>📊 Watchlist Raporu</b>\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            try:
                async with aiohttp.ClientSession() as session:
                    k1h = await fetch_klines(session, r["symbol"], "1h", limit=100)
                rsi = calc_rsi(k1h, 14)
                _, macd_hist = calc_macd(k1h)
                ch24_val = calc_change(k1h[-24:]) if len(k1h) >= 24 else 0
                icon = "🟢" if ch24_val >= 0 else "🔴"
                macd_ic = "⬆" if macd_hist > 0 else "⬇"
                text += f"{icon} <code>{r['symbol']:<14}</code> <code>{ch24_val:+.2f}%</code> RSI:<code>{rsi:.0f}</code> MACD{macd_ic}\n"
            except Exception:
                text += f"⚪ <code>{r['symbol']:<14}</code> veri alınamadı\n"
            await asyncio.sleep(0.3)
        try: await wait.delete()
        except: pass
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
        return

    await send_temp(context.bot, update.effective_chat.id,
        "Kullanım: <code>/watchlist ekle/sil/liste/rapor</code>", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 12. BACKTESTİNG
# ═══════════════════════════════════════════════════════════

async def backtest_command(update: Update, context):
    if not await check_group_access(update, context, "Backtest"):
        return
    args = context.args or []
    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "<b>🔬 Backtesting</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/backtest BTCUSDT rsi</code>\n\n"
            "Stratejiler:\n"
            "• <code>rsi</code>  — RSI &lt;30 al, &gt;70 sat\n"
            "• <code>macd</code> — MACD kesişiminde al/sat\n"
            "• <code>ema</code>  — EMA 9/21 kesişiminde al/sat\n\n"
            "<i>Son 90 günlük günlük verilerle test edilir.</i>",
            parse_mode="HTML")
        return

    symbol   = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    strateji = args[1].lower()
    if strateji not in ("rsi","macd","ema"):
        await send_temp(context.bot, update.effective_chat.id,
            "Geçersiz strateji. <code>rsi</code> | <code>macd</code> | <code>ema</code>", parse_mode="HTML")
        return

    wait = await send_temp(context.bot, update.effective_chat.id,
        f"🔬 <code>{symbol}</code> — <code>{strateji}</code> test ediliyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            k1d = await fetch_klines(session, symbol, "1d", limit=90)

        if not k1d or len(k1d) < 20:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id, "⚠️ Yeterli veri yok.", parse_mode="HTML")
            return

        closes = [float(c[4]) for c in k1d]
        islemler = []
        pozisyon = None

        for i in range(20, len(closes)):
            pencere = k1d[:i+1]
            giris_sinyali = cikis_sinyali = False

            if strateji == "rsi":
                rsi = calc_rsi(pencere, 14)
                if rsi < 30:   giris_sinyali = True
                elif rsi > 70: cikis_sinyali = True
            elif strateji == "macd":
                _, hist_cur = calc_macd(pencere)
                _, hist_prv = calc_macd(pencere[:-1])
                if hist_prv < 0 and hist_cur > 0: giris_sinyali = True
                if hist_prv > 0 and hist_cur < 0: cikis_sinyali = True
            elif strateji == "ema":
                ema9  = calc_ema(pencere, 9)
                ema21 = calc_ema(pencere, 21)
                ema9p = calc_ema(pencere[:-1], 9)
                e21p  = calc_ema(pencere[:-1], 21)
                if ema9p < e21p and ema9 > ema21: giris_sinyali = True
                if ema9p > e21p and ema9 < ema21: cikis_sinyali = True

            if giris_sinyali and not pozisyon:
                pozisyon = (closes[i], i)
            elif cikis_sinyali and pozisyon:
                kar = (closes[i] - pozisyon[0]) / pozisyon[0] * 100
                islemler.append(("AL→SAT", pozisyon[0], closes[i], kar))
                pozisyon = None

        if pozisyon:
            kar = (closes[-1] - pozisyon[0]) / pozisyon[0] * 100
            islemler.append(("Açık", pozisyon[0], closes[-1], kar))

        try: await wait.delete()
        except: pass

        if not islemler:
            await send_temp(context.bot, update.effective_chat.id,
                f"🔬 <code>{symbol}</code> — <code>{strateji}</code>: 90 günde sinyal üretilmedi.", parse_mode="HTML")
            return

        kazanc  = sum(i[3] for i in islemler if i[3] > 0)
        kayip   = sum(i[3] for i in islemler if i[3] < 0)
        toplam  = sum(i[3] for i in islemler)
        kazanan = sum(1 for i in islemler if i[3] > 0)
        basari  = kazanan / len(islemler) * 100 if islemler else 0
        icon    = "🟢" if toplam >= 0 else "🔴"

        text = (
            f"🔬 <b>Backtest — {symbol} / {strateji.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 Dönem      : Son 90 gün\n"
            f"📊 İşlem sayısı: <code>{len(islemler)}</code>\n"
            f"✅ Kazanan    : <code>{kazanan}</code> (<code>%{basari:.0f}</code>)\n"
            f"❌ Kaybeden   : <code>{len(islemler)-kazanan}</code>\n\n"
            f"💰 Toplam kazanç: <code>%{kazanc:+.1f}</code>\n"
            f"💸 Toplam kayıp : <code>%{kayip:+.1f}</code>\n"
            f"{icon} <b>Net Sonuç: %{toplam:+.1f}</b>\n\n"
            f"📋 <b>Son İşlemler:</b>\n"
        )
        for tip, giris, cikis, kar in islemler[-3:]:
            ic = "🟢" if kar >= 0 else "🔴"
            text += f"  {ic} <code>{format_price(giris)}</code> → <code>{format_price(cikis)}</code> <code>{kar:+.1f}%</code>\n"
        text += "\n<i>Geçmiş performans gelecek sonuçları garanti etmez.</i>"

        user_id = update.effective_user.id
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO backtest_gecmis(user_id,symbol,strateji,sonuc) VALUES($1,$2,$3,$4)",
                    user_id, symbol, strateji, f"%{toplam:+.1f}")
        except Exception:
            pass

        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"backtest: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Hata oluştu.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 13. ANKET SİSTEMİ
# ═══════════════════════════════════════════════════════════

async def anket_command(update: Update, context):
    chat    = update.effective_chat
    user_id = update.effective_user.id
    args    = context.args or []

    is_admin_user = False
    if chat.type == "private":
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
            is_admin_user = member.status in ("administrator","creator")
        except Exception: pass
    else:
        is_admin_user = await is_group_admin(context.bot, chat.id, user_id)

    if args and args[0].lower() == "oy":
        if len(args) < 3:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: <code>/anket oy &lt;id&gt; &lt;seçim_no&gt;</code>", parse_mode="HTML")
            return
        try:
            anket_id = int(args[1]); secim = int(args[2]) - 1
        except:
            await send_temp(context.bot, update.effective_chat.id, "Hatalı format.", parse_mode="HTML")
            return
        async with db_pool.acquire() as conn:
            anket = await conn.fetchrow("SELECT * FROM anketler WHERE id=$1 AND aktif=1", anket_id)
            if not anket:
                await send_temp(context.bot, update.effective_chat.id, "Anket bulunamadı veya sona erdi.", parse_mode="HTML")
                return
            secenekler = anket["secenekler"].split("|")
            if secim < 0 or secim >= len(secenekler):
                await send_temp(context.bot, update.effective_chat.id,
                    f"Geçersiz seçim. 1-{len(secenekler)} arası girin.", parse_mode="HTML")
                return
            await conn.execute(
                "INSERT INTO anket_oylar(anket_id,user_id,secim) VALUES($1,$2,$3) ON CONFLICT(anket_id,user_id) DO UPDATE SET secim=$3",
                anket_id, user_id, secim)
        await send_temp(context.bot, update.effective_chat.id,
            f"✅ Oyun <b>{secenekler[secim]}</b> için kaydedildi!", parse_mode="HTML")
        return

    if args and args[0].lower() == "sonuc":
        async with db_pool.acquire() as conn:
            anket_id = int(args[1]) if len(args) > 1 else None
            if anket_id:
                anket = await conn.fetchrow("SELECT * FROM anketler WHERE id=$1", anket_id)
            else:
                anket = await conn.fetchrow("SELECT * FROM anketler WHERE chat_id=$1 ORDER BY id DESC LIMIT 1", GROUP_CHAT_ID)
        if not anket:
            await send_temp(context.bot, update.effective_chat.id, "Anket bulunamadı.", parse_mode="HTML")
            return
        async with db_pool.acquire() as conn:
            oylar = await conn.fetch("SELECT secim, COUNT(*) cnt FROM anket_oylar WHERE anket_id=$1 GROUP BY secim", anket["id"])
        secenekler = anket["secenekler"].split("|")
        toplam = sum(r["cnt"] for r in oylar)
        oy_map = {r["secim"]: r["cnt"] for r in oylar}
        text = f"📊 <b>Anket Sonuçları</b>\n━━━━━━━━━━━━━━━━━━\n<b>{anket['soru']}</b>\n\n"
        for i, sec in enumerate(secenekler):
            cnt = oy_map.get(i, 0)
            pct = cnt / toplam * 100 if toplam else 0
            bar = "█" * int(pct/10) + "░" * (10 - int(pct/10))
            text += f"<code>{i+1}.</code> {sec}\n   <code>{bar}</code> <code>{pct:.0f}%</code> ({cnt} oy)\n"
        text += f"\n<i>Toplam oy: {toplam}</i>"
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
        return

    if not is_admin_user:
        await send_temp(context.bot, update.effective_chat.id,
            "📊 <b>Anket Sistemi</b>\n━━━━━━━━━━━━━━━━━━\n"
            "<b>Üye komutları:</b>\n"
            "<code>/anket oy &lt;id&gt; &lt;no&gt;</code> — oy ver\n"
            "<code>/anket sonuc</code> — sonuçları gör",
            parse_mode="HTML")
        return

    await send_temp(context.bot, update.effective_chat.id,
        "📊 <b>Anket Sistemi (Admin)</b>\n━━━━━━━━━━━━━━━━━━\n"
        "<b>Admin komutları:</b>\n"
        "<code>/anket_yeni Soru? | Seç1 | Seç2</code> — yeni anket\n"
        "<code>/anket sonuc</code> — sonuçları gör",
        parse_mode="HTML")


async def anket_yeni_command(update: Update, context):
    chat    = update.effective_chat
    user_id = update.effective_user.id

    is_admin_user = False
    if chat.type == "private":
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
            is_admin_user = member.status in ("administrator","creator")
        except Exception: pass
    else:
        is_admin_user = await is_group_admin(context.bot, chat.id, user_id)

    if not is_admin_user:
        await send_temp(context.bot, update.effective_chat.id, "🚫 Sadece adminler anket oluşturabilir.", parse_mode="HTML")
        return

    if not context.args:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanım: <code>/anket_yeni BTC nereye gider? | 📈 Yükselir | 📉 Düşer | ➡️ Yatay</code>",
            parse_mode="HTML")
        return

    raw     = " ".join(context.args)
    parcalar = [p.strip() for p in raw.split("|")]
    if len(parcalar) < 3:
        await send_temp(context.bot, update.effective_chat.id,
            "En az 1 soru + 2 seçenek gerekli. <code>|</code> ile ayırın.", parse_mode="HTML")
        return

    soru      = parcalar[0]
    secenekler = parcalar[1:]
    sec_str   = "|".join(secenekler)

    async with db_pool.acquire() as conn:
        anket_id = await conn.fetchval(
            "INSERT INTO anketler(chat_id,soru,secenekler,aktif) VALUES($1,$2,$3,1) RETURNING id",
            GROUP_CHAT_ID, soru, sec_str)

    text = f"📊 <b>Anket #{anket_id}</b>\n━━━━━━━━━━━━━━━━━━\n<b>{soru}</b>\n\n"
    for i, sec in enumerate(secenekler):
        text += f"<code>{i+1}.</code> {sec}\n"
    text += (
        f"\n<i>Oy vermek için:</i>\n"
        f"<code>/anket oy {anket_id} &lt;seçim_no&gt;</code>\n"
        f"Sonuçlar: <code>/anket sonuc {anket_id}</code>"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{i+1}. {sec[:20]}", callback_data=f"anket_oy_{anket_id}_{i}")]
        for i, sec in enumerate(secenekler)
    ] + [[InlineKeyboardButton("📊 Sonuçları Gör", callback_data=f"anket_sonuc_{anket_id}")]])

    await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
    if chat.type == "private":
        await context.bot.send_message(chat.id, f"✅ Anket #{anket_id} gruba gönderildi!", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 14. PİYASA YORUMU (Claude API)
# ═══════════════════════════════════════════════════════════

async def yorum_command(update: Update, context):
    args   = context.args or []
    symbol = args[0].upper().replace("#","").replace("/","") if args else "BTCUSDT"
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await send_temp(context.bot, update.effective_chat.id,
        f"🤖 <code>{symbol}</code> için AI yorumu hazırlanıyor...", parse_mode="HTML")
    try:
        async with aiohttp.ClientSession() as session:
            ticker_r, k1h, k4h = await asyncio.gather(
                session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5)),
                fetch_klines(session, symbol, "1h", limit=100),
                fetch_klines(session, symbol, "4h", limit=50),
            )
            ticker = await ticker_r.json()

        if "code" in ticker:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id, f"⚠️ <code>{symbol}</code> bulunamadı.", parse_mode="HTML")
            return

        price  = float(ticker.get("lastPrice", 0))
        ch24   = float(ticker.get("priceChangePercent", 0))
        vol24  = float(ticker.get("quoteVolume", 0))
        rsi    = calc_rsi(k1h, 14)
        rsi_4h = calc_rsi(k4h, 14)
        _, macd_hist = calc_macd(k1h)
        ema9   = calc_ema(k1h, 9)
        ema21  = calc_ema(k1h, 21)

        ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
        if not ANTHROPIC_API_KEY:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id,
                "⚠️ ANTHROPIC_API_KEY ayarlanmamış.\nRailway Variables bölümüne ekleyin.", parse_mode="HTML")
            return

        prompt = (
            f"Kripto para analisti olarak {symbol} için kısa Türkçe piyasa yorumu yap.\n"
            f"Veriler: Fiyat={format_price(price)} USDT, 24s değişim={ch24:+.2f}%, "
            f"RSI(1h)={rsi:.0f}, RSI(4h)={rsi_4h:.0f}, "
            f"MACD histogram={'pozitif' if macd_hist > 0 else 'negatif'}, "
            f"EMA9 {'>' if ema9 > ema21 else '<'} EMA21, "
            f"Hacim={vol24/1_000_000:.1f}M USDT.\n\n"
            f"Maksimum 4 cümle. Teknik analiz ağırlıklı, sade ve anlaşılır yaz. "
            f"Son cümlede AL/SAT/BEKLE yönlendirmesi yap. Yatırım tavsiyesi değil ibaresini ekle."
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()

        ai_yorum = data.get("content", [{}])[0].get("text", "Yorum alınamadı.")
        try: await wait.delete()
        except: pass

        text = (
            f"🤖 <b>AI Piyasa Yorumu — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 <code>{format_price(price)} USDT</code> | <code>{ch24:+.2f}%</code>\n\n"
            f"{ai_yorum}"
        )
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="HTML")
    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error(f"yorum: {e}")
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Yorum alınamadı.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# 15. PORTFÖY ALARMI JOB
# ═══════════════════════════════════════════════════════════

async def portfolyo_alarm_job(context):
    try:
        async with db_pool.acquire() as conn:
            pozlar = await conn.fetch("SELECT user_id, symbol, buy_price, amount FROM kar_pozisyonlar")
        if not pozlar: return

        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                tickers = await resp.json()
        ticker_map = {t["symbol"]: float(t.get("priceChangePercent",0)) for t in tickers}
        price_map  = {t["symbol"]: float(t.get("lastPrice",0)) for t in tickers}

        now = datetime.utcnow()
        for row in pozlar:
            user_id   = row["user_id"]
            symbol    = row["symbol"]
            ch24      = ticker_map.get(symbol, 0)
            cur_price = price_map.get(symbol, 0)
            if abs(ch24) < 5: continue

            key = f"portfoy_{user_id}_{symbol}"
            if key in cooldowns and now - cooldowns[key] < timedelta(hours=4): continue
            cooldowns[key] = now

            pnl_pct = (cur_price - row["buy_price"]) / row["buy_price"] * 100 if row["buy_price"] else 0
            pnl_val = (cur_price - row["buy_price"]) * row["amount"]
            icon    = "🟢" if ch24 >= 0 else "🔴"
            try:
                await context.bot.send_message(user_id,
                    f"💼 <b>Portföy Alarmı — {symbol}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{icon} 24s: <code>{ch24:+.2f}%</code>\n"
                    f"💵 Güncel: <code>{format_price(cur_price)} USDT</code>\n"
                    f"📊 Alış  : <code>{format_price(row['buy_price'])} USDT</code>\n"
                    f"{'🟢' if pnl_val >= 0 else '🔴'} P&amp;L: <code>{pnl_pct:+.1f}%</code> (<code>{pnl_val:+.2f} USDT</code>)",
                    parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        log.error(f"portfolyo_alarm_job: {e}")


# ═══════════════════════════════════════════════════════════
# 16. HAFTALIK PERFORMANS RAPORU
# ═══════════════════════════════════════════════════════════

async def haftalik_rapor_job(context):
    now = datetime.utcnow()
    if now.weekday() != 0: return
    if now.hour != 8 or now.minute > 5: return
    key = f"haftalik_{now.date()}"
    if key in cooldowns: return
    cooldowns[key] = now

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                tickers = await resp.json()

        usdt = [t for t in tickers
                if t["symbol"].endswith("USDT")
                and float(t.get("quoteVolume",0)) > 1_000_000
                and not any(x in t["symbol"] for x in ["UP","DOWN","BULL","BEAR"])]

        top5 = sorted(usdt, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:5]
        bot5 = sorted(usdt, key=lambda x: float(x.get("priceChangePercent",0)))[:5]
        avg  = sum(float(t.get("priceChangePercent",0)) for t in usdt) / len(usdt)
        mood = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
        tarih = now.strftime("%d.%m.%Y")

        text = (
            f"📅 <b>Haftalık Kripto Raporu</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🗓 {tarih} · {mood}\n"
            f"📊 Genel ortalama: <code>{avg:+.2f}%</code>\n\n"
            f"🚀 <b>En Çok Yükselenler</b>\n"
        )
        for i, c in enumerate(top5, 1):
            text += f"{get_number_emoji(i)} <code>{c['symbol']:<14}</code> 🟢 <code>{float(c['priceChangePercent']):+.2f}%</code>\n"
        text += "\n📉 <b>En Çok Düşenler</b>\n"
        for i, c in enumerate(bot5, 1):
            text += f"{get_number_emoji(i)} <code>{c['symbol']:<14}</code> 🔴 <code>{float(c['priceChangePercent']):+.2f}%</code>\n"
        text += "\n<i>İyi haftalar! 🎯</i>"

        await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"haftalik_rapor: {e}")


async def send_full_analysis(bot, chat_id, symbol, extra_title="", threshold_info=None, auto_del=False, ch5_override=None, alarm_mode=False, member_delay=None):
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
        if ch5_override is not None:
            ch5m = ch5_override

        # 7 günlük değişim: k1d son 8 mum (8. mum kapanışı → bugün kapanışı)
        ch7d  = calc_change(k1d[-8:])  if k1d  and len(k1d)  >= 8  else 0.0
        # 30 günlük değişim: k1d tüm 30 mum
        ch30d = calc_change(k1d)       if k1d  and len(k1d)  >= 2  else 0.0

        rank, total = await get_coin_rank(symbol)
        re = rank_emoji(rank)
        is_fallback = marketcap_rank_cache.get("_fallback", True)
        rank_label  = "Hacim Sırası" if is_fallback else "MarketCap Sırası"
        if rank:
            rank_line = f"{re} *{rank_label}:* `#{rank}` _/ {total} coin_\n"
        else:
            rank_line = f"🏅 *{rank_label}:* `—`\n"

        rsi7_1h   = calc_rsi(k1h_100, 7)
        rsi14_1h  = calc_rsi(k1h_100, 14)
        rsi14_4h  = calc_rsi(k4h_100, 14)
        rsi14_1d  = calc_rsi(k1d_100, 14)
        stoch_1h  = calc_stoch_rsi(k1h_100)
        stoch_4h  = calc_stoch_rsi(k4h_100)

        ema9_1h    = calc_ema(k1h_100, 9)
        ema21_1h   = calc_ema(k1h_100, 21)
        ema21_4h   = calc_ema(k4h_100, 21)
        ema55_4h   = calc_ema(k4h_100, 55)
        _, macd_hist_1h = calc_macd(k1h_100)
        _, macd_hist_4h = calc_macd(k4h_100)
        boll_1h    = calc_bollinger(k1h_100)
        obv_1h     = calc_obv_trend(k1h_100, lookback=12)
        diverjans  = calc_rsi_divergence(k1h_100)

        destek, direnc = calc_support_resistance(k4h_42)
        vol_ratio = calc_volume_anomaly(k1h_24)
        mood, btc_dom, mkt_avg = await fetch_market_badge()

        def get_ui(val):
            if val > 0:   return "🟢▲", "+"
            elif val < 0: return "🔴▼", ""
            else:         return "⚪→", ""

        e5,s5   = get_ui(ch5m)
        e1,s1   = get_ui(ch1h)
        e4,s4   = get_ui(ch4h)
        e24,s24 = get_ui(ch24)
        e7,s7   = get_ui(ch7d)
        e30,s30 = get_ui(ch30d)

        def rsi_label(r):
            if r >= 80:   return "🔴 Aşırı Alım"
            elif r >= 70: return "🟠 Alım Bölgesi"
            elif r >= 55: return "🟡 Yükseliş"
            elif r <= 20: return "🔵 Aşırı Satım"
            elif r <= 30: return "🟣 Satım Bölgesi"
            elif r <= 45: return "🟡 Düşüş"
            else:         return "🟢 Normal"

        sh, lh, bh = calc_score_hourly(ticker, k1h_100, k15m, k5m, rsi14_1h)
        sd, ld, bd = calc_score_daily(ticker, k4h_42, k1h_24, k1d)
        sw, lw, bw = calc_score_weekly(ticker, k1d, k1w)

        vol_usdt = float(ticker.get("quoteVolume", 0))
        vol_str  = f"{vol_usdt/1_000_000:.1f}M" if vol_usdt >= 1_000_000 else f"{vol_usdt/1_000:.0f}K"

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

        div_line = ""
        if diverjans == "bearish":
            div_line = "⚠️ *Bearish Diverjans* — Fiyat yükseliyor, RSI düşüyor!\n"
        elif diverjans == "bullish":
            div_line = "💡 *Bullish Diverjans* — Fiyat düşüyor, RSI yükseliyor!\n"

        header = f"*{extra_title}*\n"

        text = header + (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 `{symbol}` 💎\n"
            f"\n"
            f"💵 *Fiyat:* `{format_price(price)} USDT`\n"
            f"{rank_line}"
            f"{vol_anom}"
            f"\n*Performans:*\n"
            f"{e5} `5dk  :` `{s5}{ch5m:+.2f}%`\n"
            f"{e1} `1sa  :` `{s1}{ch1h:+.2f}%`\n"
            f"{e4} `4sa  :` `{s4}{ch4h:+.2f}%`\n"
            f"{e24} `24sa :` `{s24}{ch24:+.2f}%`\n"
            f"{e7} `7gün :` `{s7}{ch7d:+.2f}%`\n"
            f"{e30} `30gün:` `{s30}{ch30d:+.2f}%`\n\n"
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
        # DM'e gönderimde mesajları silme, sadece grup kanallarında sil
        is_group_chat = False
        try:
            chat_obj = await bot.get_chat(chat_id)
            is_group_chat = chat_obj.type in ("group", "supergroup", "channel")
        except Exception:
            pass

        if alarm_mode and is_group_chat:
            alarm_delay = await get_delete_delay()
            asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, alarm_delay))
        elif member_delay is not None and is_group_chat:
            asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, member_delay))
        elif auto_del and is_group_chat:
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
            if alarm_mode and is_group_chat:
                asyncio.create_task(auto_delete(bot, chat_id, photo_msg.message_id, alarm_delay))
            elif member_delay is not None and is_group_chat:
                asyncio.create_task(auto_delete(bot, chat_id, photo_msg.message_id, member_delay))
            elif auto_del and is_group_chat:
                asyncio.create_task(auto_delete(bot, chat_id, photo_msg.message_id, delay))

    except Exception as e:
        err = str(e)
        if any(x in err for x in ("Forbidden", "bot was blocked", "chat not found", "user is deactivated")):
            raise
        log.error(f"Gonderim hatasi ({symbol}): {e}")

# ================= GRUP ERİŞİM KONTROLÜ =================

# Grup üyelerinin kullanabileceği komutlar
GROUP_ALLOWED_CMDS = {"start", "top5", "top24", "mtf"}

async def check_group_access(update: Update, context, feature_name: str = None) -> bool:
    """
    Grupta çalıştırılan bir komutun üye tarafından kullanılıp kullanılamayacağını kontrol eder.
    - Admin/creator → her zaman True
    - Private chat  → her zaman True
    - Grup üyesi + izin verilen komut → True
    - Grup üyesi + yasak komut → DM yönlendirme mesajı gönderir, False döner
    """
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return True

    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return True

    # Admin kontrolü
    if await is_group_admin(context.bot, chat.id, user_id):
        return True

    # İzin verilen komutları kontrol et
    if update.message and update.message.text:
        cmd = update.message.text.lstrip("/").split("@")[0].split()[0].lower()
        if cmd in GROUP_ALLOWED_CMDS:
            return True

    # Üye → yasak → yönlendir
    fname = feature_name or "Bu özellik"

    # Komutu gruptan sil
    if update.message:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=update.message.message_id)
        except Exception:
            pass

    # Fiyat Hedefi ile aynı pattern: DM'e mesaj + gruba kısa uyarı
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🔒 *{fname}* özelliğini kullanmak için buraya tıklayın 👇\n"
                f"Botu DM üzerinden kullanabilirsiniz."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass
    try:
        redir = await context.bot.send_message(
            chat_id=chat.id,
            text=f"🔒 {fname} için lütfen DM'den kullanın 👇 @KriptoDrop_alertbot",
        )
        asyncio.create_task(auto_delete(context.bot, chat.id, redir.message_id, 10))
    except Exception:
        pass
    return False

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

async def is_group_admin(bot, chat_id, user_id) -> bool:
    """Verilen chat_id/user_id için admin mi diye kontrol eder."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def get_member_delete_delay() -> int:
    """Grup üyesi komutları için silme süresini döndürür (saniye)."""
    try:
        async with db_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT member_delete_delay FROM groups WHERE chat_id=$1", GROUP_CHAT_ID
            )
        return int(r["member_delete_delay"]) if r and r["member_delete_delay"] else 3600
    except Exception:
        return 3600

async def group_dm_redirect(bot, chat_id, message_id, feature_name: str):
    """Grup üyesine kullanılamaz özellik için DM yönlendirme mesajı gönderir ve orijinal mesajı siler."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔒 *{feature_name}* özelliği grupta kullanılamaz.\n"
                f"Lütfen botu DM üzerinden kullanın. 👇"
            ),
            parse_mode="Markdown"
        )
        asyncio.create_task(auto_delete(bot, chat_id, msg.message_id, 15))
    except Exception:
        pass

SET_THRESHOLD_PRESETS    = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0]
DELETE_DELAY_PRESETS     = [30, 60, 300, 600, 1800, 3600]
MBR_DELETE_DELAY_PRESETS = [300, 600, 1800, 3600, 7200, 86400]

DELAY_LABEL_MAP = {
    30: "30sn", 60: "1dk", 300: "5dk", 600: "10dk",
    1800: "30dk", 3600: "1sa", 7200: "2sa", 86400: "24sa"
}

async def build_set_panel(context):
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT alarm_active, threshold, delete_delay, member_delete_delay FROM groups WHERE chat_id=$1",
            GROUP_CHAT_ID
        )
    threshold    = r["threshold"]
    alarm_active = r["alarm_active"]
    del_delay    = r["delete_delay"] or 30
    mbr_delay    = r["member_delete_delay"] or 3600

    threshold_buttons = []
    row = []
    for val in SET_THRESHOLD_PRESETS:
        label = f"{'✅ ' if threshold == val else ''}%{val:.0f}"
        row.append(InlineKeyboardButton(label, callback_data=f"set_threshold_{val}"))
        if len(row) == 4:
            threshold_buttons.append(row); row = []
    if row: threshold_buttons.append(row)
    threshold_buttons.append([InlineKeyboardButton("✏️ Manuel Eşik", callback_data="set_threshold_custom")])

    # Alarm silme süresi (admin mesajları)
    threshold_buttons.append([InlineKeyboardButton("── 🗑 Alarm Mesajı Silme Süresi ──", callback_data="noop")])
    delay_rows = []
    delay_row  = []
    for val in DELETE_DELAY_PRESETS:
        label = f"{'✅ ' if del_delay == val else ''}{DELAY_LABEL_MAP.get(val, str(val)+'sn')}"
        delay_row.append(InlineKeyboardButton(label, callback_data=f"set_delay_{val}"))
        if len(delay_row) == 3:
            delay_rows.append(delay_row)
            delay_row = []
    if delay_row:
        delay_rows.append(delay_row)
    threshold_buttons.extend(delay_rows)
    threshold_buttons.append([InlineKeyboardButton("✏️ Manuel Süre Gir", callback_data="set_delay_custom")])

    # Üye komut silme süresi
    threshold_buttons.append([InlineKeyboardButton("── 👥 Üye Komut Silme Süresi ──", callback_data="noop")])
    mbr_rows = []
    mbr_row  = []
    for val in MBR_DELETE_DELAY_PRESETS:
        label = f"{'✅ ' if mbr_delay == val else ''}{DELAY_LABEL_MAP.get(val, str(val)+'sn')}"
        mbr_row.append(InlineKeyboardButton(label, callback_data=f"set_mdelay_{val}"))
        if len(mbr_row) == 3:
            mbr_rows.append(mbr_row)
            mbr_row = []
    if mbr_row:
        mbr_rows.append(mbr_row)
    threshold_buttons.extend(mbr_rows)

    threshold_buttons.append([
        InlineKeyboardButton(
            f"🔔 Alarm: {'AKTİF ✅' if alarm_active else 'KAPALI ❌'}",
            callback_data="set_toggle_alarm"
        )
    ])
    threshold_buttons.append([InlineKeyboardButton("❌ Kapat", callback_data="set_close")])

    def _fmt_delay(secs):
        if secs < 60:
            return f"{secs} saniye"
        elif secs < 3600:
            m = secs // 60
            s = secs % 60
            return f"{m} dakika" + (f" {s} sn" if s else "")
        else:
            h = secs // 3600
            m = (secs % 3600) // 60
            return f"{h} saat" + (f" {m} dk" if m else "")

    alarm_delay_label = _fmt_delay(del_delay)
    mbr_delay_label   = _fmt_delay(mbr_delay)
    text = (
        "⚙️ *Grup Ayarları — Admin Paneli*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTİF' if alarm_active else 'KAPALI'}`\n"
        f"🎯 *Alarm Eşiği:* `%{threshold}`\n"
        f"🗑 *Alarm Mesajı Silme:* `{alarm_delay_label}` sonra\n"
        f"👥 *Üye Komut Silme:* `{mbr_delay_label}` sonra\n\n"
        "Ayarları aşağıdan değiştirin:"
    )
    return text, InlineKeyboardMarkup(threshold_buttons)

async def set_command(update: Update, context):
    if not await check_group_access(update, context, "Admin Ayarları"):
        return
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
        val_str = q.data.replace("set_delay_", "")
        if val_str == "custom":
            context.user_data["awaiting_delay"] = True
            await q.message.reply_text(
                "✏️ *Alarm Mesajı Silme Süresi*\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Süreyi yazın. Örnekler:\n"
                "• `90` → 90 saniye\n"
                "• `5d` veya `5dk` → 5 dakika\n"
                "• `2s` veya `2sa` → 2 saat\n"
                "• `150s` → 150 saniye",
                parse_mode="Markdown"
            )
            return
        try:
            delay_val = int(val_str)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE groups SET delete_delay=$1 WHERE chat_id=$2", delay_val, GROUP_CHAT_ID)
            text, keyboard = await build_set_panel(context)
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            log.warning(f"set_delay: {e}")
        return

    if q.data.startswith("set_mdelay_"):
        try:
            delay_val = int(q.data.replace("set_mdelay_", ""))
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE groups SET member_delete_delay=$1 WHERE chat_id=$2", delay_val, GROUP_CHAT_ID)
            text, keyboard = await build_set_panel(context)
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            log.warning(f"set_mdelay: {e}")
        return

    if q.data == "noop":
        return

    if q.data == "set_open":
        text, keyboard = await build_set_panel(context)
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def _parse_delay_input(text: str):
    """
    Kullanıcı girişini saniyeye çevirir.
    Formatlar: 90  → 90s | 5d/5dk → 300s | 2s/2sa → 7200s
    Geçersizse None döner.
    """
    text = text.strip().lower().replace(",", ".")
    try:
        # Sadece sayı → saniye
        val = int(text)
        if 5 <= val <= 86400:
            return val
        return None
    except ValueError:
        pass
    import re
    m = re.fullmatch(r"(\d+)\s*(s|sa|saat|d|dk|dak|dakika)", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit in ("d", "dk", "dak", "dakika"):
        val = n * 60
    else:  # s, sa, saat
        val = n * 3600
    if 5 <= val <= 86400:
        return val
    return None

async def handle_threshold_input(update: Update, context):
    # Manuel alarm silme süresi girişi
    if context.user_data.get("awaiting_delay"):
        if not await is_admin(update, context):
            context.user_data.pop("awaiting_delay", None)
            return True
        val = await _parse_delay_input(update.message.text)
        if val is None:
            await update.message.reply_text(
                "⚠️ Geçersiz format. Örnekler:\n"
                "`90` → 90 saniye\n`5dk` → 5 dakika\n`2sa` → 2 saat\n"
                "_(5 saniye – 24 saat arası)_",
                parse_mode="Markdown"
            )
            return True
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE groups SET delete_delay=$1 WHERE chat_id=$2", val, GROUP_CHAT_ID)
        context.user_data.pop("awaiting_delay", None)
        # Okunabilir etiket
        if val < 60:
            label = f"{val} saniye"
        elif val < 3600:
            label = f"{val//60} dakika" + (f" {val%60} sn" if val % 60 else "")
        else:
            label = f"{val//3600} saat" + (f" {(val%3600)//60} dk" if (val % 3600) // 60 else "")
        await update.message.reply_text(
            f"✅ Alarm mesajı silme süresi *{label}* olarak güncellendi!",
            parse_mode="Markdown"
        )
        return True

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

    chat     = update.effective_chat
    is_group = chat.type in ("group", "supergroup")

    if is_group:
        try:
            await update.message.delete()
        except Exception:
            pass

    delay = (await get_member_delete_delay()) if is_group else None
    await send_full_analysis(
        context.bot,
        chat.id, symbol, "PIYASA ANALIZ RAPORU",
        auto_del=is_group,
        member_delay=delay
    )

# ================= GELİŞMİŞ KİŞİSEL ALARM =================

async def my_alarm_v2(update: Update, context):
    if not await check_group_access(update, context, "Kişisel Alarmlar"):
        return
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
            "• `%`    : `/alarm_ekle BTCUSDT 3.5`\n"
            "• Fiyat  : `/alarm_ekle BTCUSDT fiyat 70000`\n"
            "• RSI    : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant   : `/alarm_ekle BTCUSDT bant 60000 70000`\n\n"
            "💡 Fiyat alarmı için `/hedef BTCUSDT 70000` da kullanabilirsiniz."
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
    if not await check_group_access(update, context, "Alarm Ekle"):
        return
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    args     = context.args or []

    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "📌 *Alarm Turleri:*\n━━━━━━━━━━━━━━━━━━\n"
            "• `%`    : `/alarm_ekle BTCUSDT 3.5`\n"
            "• Fiyat  : `/alarm_ekle BTCUSDT fiyat 70000`\n"
            "• RSI    : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant   : `/alarm_ekle BTCUSDT bant 60000 70000`",
            parse_mode="Markdown"
        )
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    # ── FİYAT ALARMI (/alarm_ekle BTCUSDT fiyat 70000) ──────────────────
    if args[1].lower() in ("fiyat", "price", "hedef"):
        if len(args) < 3:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/alarm_ekle BTCUSDT fiyat 70000`", parse_mode="Markdown"); return
        try:
            target_price = float(args[2].replace(",","."))
        except:
            await send_temp(context.bot, update.effective_chat.id, "Fiyat degeri sayi olmali.", parse_mode="Markdown"); return

        # Anlık fiyatı al, direction belirle
        fiyat_map = await _hedef_canli_fiyat([symbol])
        cur_price = fiyat_map.get(symbol, 0)
        direction = "up" if (cur_price == 0 or target_price > cur_price) else "down"

        async with db_pool.acquire() as conn:
            # Önce mevcut kaydı sil (varsa), sonra ekle
            await conn.execute("""
                DELETE FROM price_targets
                WHERE user_id=$1 AND symbol=$2 AND target_price=$3
            """, user_id, symbol, target_price)
            await conn.execute("""
                INSERT INTO price_targets(user_id, symbol, target_price, direction, active)
                VALUES($1,$2,$3,$4,1)
            """, user_id, symbol, target_price, direction)

        yon_str = "ulaşınca 📈" if direction == "up" else "düşünce 📉"
        if cur_price > 0:
            pct  = ((target_price - cur_price) / cur_price) * 100
            uzak = f" _(şu andan `{pct:+.2f}%`)_"
        else:
            uzak = ""
        await send_temp(context.bot, update.effective_chat.id,
            f"🎯 *{symbol}* `{format_price(target_price)} USDT` fiyatına {yon_str} DM alacaksınız!{uzak}\n\n"
            f"_Hedeflerinizi görmek için: /hedef_",
            parse_mode="Markdown"
        )
        return

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
    if not await check_group_access(update, context, "Alarm Sil"):
        return
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
    if not await check_group_access(update, context, "Favoriler"):
        return
    user_id = update.effective_user.id
    args    = context.args or []

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
    if not await check_group_access(update, context, "Alarm Duraklat"):
        return
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
    if not await check_group_access(update, context, "Alarm Geçmişi"):
        return
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


# ================= FİYAT HEDEFİ (GELİŞTİRİLMİŞ) =================

async def _hedef_canli_fiyat(semboller: list) -> dict:
    """Verilen sembol listesi için anlık fiyat sözlüğü döner (price_memory + API fallback)."""
    canli = {}
    # Önce price_memory'den al
    for sym in semboller:
        pm = price_memory.get(sym)
        if pm:
            canli[sym] = pm[-1][1]
    # Eksikler için Binance API
    eksik = [s for s in semboller if s not in canli]
    if eksik:
        try:
            async with aiohttp.ClientSession() as session:
                for sym in eksik:
                    try:
                        async with session.get(
                            f"{BINANCE_24H}?symbol={sym}",
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            data = await resp.json()
                            lp = data.get("lastPrice")
                            if lp:
                                canli[sym] = float(lp)
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"_hedef_canli_fiyat: {e}")
    return canli


async def hedef_liste_goster(bot, chat_id, user_id, show_all=False, edit_message=None):
    """Hedefleri anlık fiyat ve uzaklık bilgisiyle göster."""
    try:
        async with db_pool.acquire() as conn:
            if show_all:
                rows = await conn.fetch(
                    """SELECT id, symbol, target_price AS target, direction, active AS triggered
                       FROM price_targets WHERE user_id=$1
                       ORDER BY active DESC, symbol, target_price""",
                    user_id
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, symbol, target_price AS target, direction, active AS triggered
                       FROM price_targets WHERE user_id=$1 AND active=1
                       ORDER BY symbol, target_price""",
                    user_id
                )
    except Exception as e:
        log.error(f"hedef_liste_goster DB: {e}")
        await bot.send_message(chat_id, "⚠️ Hedefler yüklenirken bir hata oluştu.", parse_mode="Markdown")
        return

    async def _send(text, keyboard):
        """Edit veya yeni mesaj gönder — her durumda bir şey çıksın."""
        if edit_message:
            try:
                await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
                return
            except Exception:
                pass
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            log.error(f"hedef_liste_goster send: {e}")

    if not rows:
        msg = (
            "🎯 *Fiyat Hedeflerim*\n━━━━━━━━━━━━━━━━━━\n"
            "Aktif hedef yok.\n\n"
            "➕ *Nasıl Eklenir?*\n"
            "`/hedef BTCUSDT 70000`\n"
            "`/hedef ETHUSDT 3000 4000 5000` _(çoklu)_\n\n"
            "📋 Geçmiş: `/hedef gecmis`\n"
            "🗑 Sil: `/hedef sil BTCUSDT`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Hedef Ekle",  callback_data="hedef_add_help"),
            InlineKeyboardButton("📋 Geçmiş",      callback_data="hedef_gecmis"),
        ]])
        await _send(msg, keyboard)
        return

    # Anlık fiyatları toplu çek
    semboller = list({r["symbol"] for r in rows})
    canli = await _hedef_canli_fiyat(semboller)

    baslik = "🎯 *Tüm Hedeflerim*" if show_all else "🎯 *Aktif Fiyat Hedeflerim*"
    text   = baslik + f" `({len(rows)} adet)`\n━━━━━━━━━━━━━━━━━━\n"

    from collections import defaultdict as _dd
    gruplar = _dd(list)
    for r in rows:
        gruplar[r["symbol"]].append(r)

    sil_buttons = []
    for sym, hedefler in sorted(gruplar.items()):
        cur = canli.get(sym)
        cur_str = f"`{format_price(cur)} USDT`" if cur else "—"
        text += f"\n💎 *{sym}* — Anlık: {cur_str}\n"

        for r in hedefler:
            target    = r["target"]
            yon_icon  = "📈" if r["direction"] == "up" else "📉"
            is_active = r["triggered"]  # alias: active kolonundan geliyor, 1=aktif 0=tetiklendi

            if not is_active:  # active=0 → tetiklenmiş
                durum = "✅"
                uzak  = ""
            else:              # active=1 → bekliyor
                durum = "🟡"
                if cur and cur > 0:
                    pct  = ((target - cur) / cur) * 100
                    uzak = f" `({pct:+.2f}%)`"
                else:
                    uzak = ""

            text += f"  {durum} {yon_icon} `{format_price(target)} USDT`{uzak}\n"

            if is_active:  # active=1 → hâlâ bekliyor, silinebilir
                sil_buttons.append([
                    InlineKeyboardButton(
                        f"🗑 {sym} @ {format_price(target)}",
                        callback_data=f"hedef_sil_id_{r['id']}"
                    )
                ])

    if canli:
        text += "\n_↕️ Yüzde = anlık fiyattan uzaklık_"

    alt_buttons = [
        InlineKeyboardButton("➕ Ekle",      callback_data="hedef_add_help"),
        InlineKeyboardButton("🔄 Yenile",    callback_data="hedef_liste"),
    ]
    if not show_all:
        alt_buttons.append(InlineKeyboardButton("📋 Geçmiş", callback_data="hedef_gecmis"))
    else:
        alt_buttons.append(InlineKeyboardButton("🟡 Aktifler", callback_data="hedef_liste"))

    if sil_buttons:
        sil_buttons.append(alt_buttons)
        keyboard = InlineKeyboardMarkup(sil_buttons)
    else:
        keyboard = InlineKeyboardMarkup([alt_buttons])

    await _send(text, keyboard)


async def hedef_command(update: Update, context):
    if not await check_group_access(update, context, "Fiyat Hedefi"):
        return
    user_id = update.effective_user.id
    args    = context.args or []

    # /hedef  veya  /hedef liste
    if not args or args[0].lower() == "liste":
        await hedef_liste_goster(context.bot, update.effective_chat.id, user_id)
        return

    # /hedef gecmis  →  tüm hedefler (tetiklenmiş dahil)
    if args[0].lower() in ("gecmis", "geçmiş", "hepsi", "tumu", "tümü"):
        await hedef_liste_goster(context.bot, update.effective_chat.id, user_id, show_all=True)
        return

    # /hedef sil BTCUSDT  →  sembol için tüm hedefleri sil
    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim:\n`/hedef sil BTCUSDT` — sembol sil\n`/hedef sil hepsi` — tümünü sil",
                parse_mode="Markdown"); return
        if args[1].lower() in ("hepsi", "tumu", "tümü"):
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM price_targets WHERE user_id=$1", user_id)
            await send_temp(context.bot, update.effective_chat.id,
                "🗑 Tüm hedefleriniz silindi.", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM price_targets WHERE user_id=$1 AND symbol=$2", user_id, symbol
            )
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 `{symbol}` için tüm hedefler silindi.", parse_mode="Markdown"); return

    # /hedef BTCUSDT 70000  veya  /hedef BTCUSDT 60000 70000 80000
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    hedef_fiyatlar = []
    for a in args[1:]:
        try:
            hedef_fiyatlar.append(float(a.replace(",",".")))
        except:
            pass

    if not hedef_fiyatlar:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanim: `/hedef BTCUSDT 70000`\n"
            "Çoklu: `/hedef BTCUSDT 60000 70000 80000`",
            parse_mode="Markdown"); return

    # Anlık fiyat al
    fiyat_map = await _hedef_canli_fiyat([symbol])
    cur_price = fiyat_map.get(symbol, 0)

    # DB'ye ekle
    eklenenler = []
    async with db_pool.acquire() as conn:
        for target in hedef_fiyatlar:
            if cur_price > 0:
                direction = "up" if target > cur_price else "down"
            else:
                direction = "up"
            try:
                # Önce mevcut kaydı sil, sonra ekle (conflict güvenli)
                await conn.execute("""
                    DELETE FROM price_targets
                    WHERE user_id=$1 AND symbol=$2 AND target_price=$3
                """, user_id, symbol, target)
                await conn.execute("""
                    INSERT INTO price_targets(user_id, symbol, target_price, direction, active)
                    VALUES($1,$2,$3,$4,1)
                """, user_id, symbol, target, direction)
                eklenenler.append((target, direction))
            except Exception as e:
                log.warning(f"hedef ekle DB hatasi ({symbol} @ {target}): {e}")

    # Yanıt oluştur
    lines = []
    for target, direction in eklenenler:
        yon_str = "ulaşınca 📈" if direction == "up" else "düşünce 📉"
        if cur_price > 0:
            pct  = ((target - cur_price) / cur_price) * 100
            uzak = f" _(şu andan `{pct:+.2f}%`)_"
        else:
            uzak = ""
        lines.append(f"• `{format_price(target)} USDT` {yon_str}{uzak}")

    text = (
        f"🎯 *{symbol}* — {len(eklenenler)} hedef kaydedildi!\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    text += "\n".join(lines)
    if cur_price > 0:
        text += f"\n\n💵 _Anlık: `{format_price(cur_price)} USDT`_"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Tüm Hedeflerim", callback_data="hedef_liste"),
        InlineKeyboardButton("➕ Daha Fazla Ekle", callback_data="hedef_add_help"),
    ]])
    await send_temp(context.bot, update.effective_chat.id, text,
                    parse_mode="Markdown", reply_markup=keyboard)


async def hedef_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, symbol, target_price AS target, direction FROM price_targets WHERE active=1"
            )
        if not rows:
            return

        # Tüm sembollerin fiyatını toplu çek
        semboller = list({r["symbol"] for r in rows})
        canli     = await _hedef_canli_fiyat(semboller)

        for row in rows:
            cur = canli.get(row["symbol"])
            if not cur or cur <= 0:
                continue

            target    = row["target"]
            # direction'ı anlık olarak yeniden hesapla (eski kayıtlar için güvenlik)
            direction = row["direction"]
            if direction not in ("up", "down"):
                direction = "up" if target > cur else "down"

            hit = (direction == "up"   and cur >= target) or \
                  (direction == "down" and cur <= target)
            if not hit:
                continue

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE price_targets SET active=0 WHERE id=$1", row["id"]
                )

            yon  = "📈 YÜKSELDİ" if row["direction"] == "up" else "📉 DÜŞTÜ"
            pct  = ((cur - row["target"]) / row["target"]) * 100
            text = (
                f"🎯 *FİYAT HEDEFİ ULAŞTI!*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 *{row['symbol']}*\n"
                f"🏁 Hedef : `{format_price(row['target'])} USDT`\n"
                f"💵 Şu an : `{format_price(cur)} USDT` `({pct:+.2f}%)`\n"
                f"{yon}\n\n"
                f"_Yeni hedef eklemek için:_\n"
                f"`/hedef {row['symbol']} <fiyat>`"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Hedeflerim", callback_data="hedef_liste"),
                InlineKeyboardButton(
                    "📈 Binance",
                    url=f"https://www.binance.com/tr/trade/{row['symbol'].replace('USDT','_USDT')}"
                )
            ]])
            try:
                await context.bot.send_message(
                    row["user_id"], text,
                    parse_mode="Markdown", reply_markup=keyboard
                )
            except Exception as e:
                log.warning(f"Hedef bildirimi gönderilemedi ({row['user_id']}): {e}")
    except Exception as e:
        log.error(f"hedef_job hatasi: {e}")


# ================= KAR/ZARAR HESABI =================

async def kar_command(update: Update, context):
    if not await check_group_access(update, context, "Kar/Zarar"):
        return
    user_id = update.effective_user.id
    args    = context.args or []

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
        semboller = [r["symbol"] for r in rows]
        canli = await _hedef_canli_fiyat(semboller)

        for r in rows:
            cur = canli.get(r["symbol"], r["buy_price"])
            invested    = r["amount"] * r["buy_price"]
            current_val = r["amount"] * cur
            pnl         = current_val - invested
            pnl_pct     = ((cur - r["buy_price"]) / r["buy_price"]) * 100
            icon        = "🟢" if pnl >= 0 else "🔴"
            text += (
                f"{icon} `{r['symbol']}`\n"
                f"  Alış: `{format_price(r['buy_price'])}` × `{r['amount']}`\n"
                f"  Şu an: `{format_price(cur)}` → `{pnl_pct:+.2f}%`\n"
                f"  P&L: `{pnl:+.2f} USDT`\n\n"
            )
        await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
        return

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

    if len(args) == 3:
        symbol = args[0].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        try:
            amount    = float(args[1].replace(",","."))
            buy_price = float(args[2].replace(",","."))
        except:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanim: `/kar BTCUSDT 0.5 60000`", parse_mode="Markdown"); return

        canli = await _hedef_canli_fiyat([symbol])
        cur   = canli.get(symbol)
        if not cur:
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


# ================= GELİŞMİŞ MTF ANALİZ =================

async def mtf_command(update: Update, context):
    if not await check_group_access(update, context, "MTF Analiz"):
        return
    # args: komuttan veya callback'ten gelebilir
    args = context.args or []
    # Eğer args boşsa ve mesaj varsa, mesaj metninden sembol almayı dene
    if not args and update.message and update.message.text:
        parts = update.message.text.strip().split()
        if len(parts) > 1:
            args = parts[1:]

    if not args:
        await send_temp(context.bot, update.effective_chat.id,
            "📊 *MTF Analiz*\n━━━━━━━━━━━━━━━━━━\n"
            "Kullanim: `/mtf BTCUSDT`\n"
            "Örnek: `/mtf XRPUSDT`",
            parse_mode="Markdown")
        return

    symbol = args[0].upper().replace("#","").replace("/","").strip()
    if not symbol.endswith("USDT"): symbol += "USDT"

    wait = await send_temp(context.bot, update.effective_chat.id, "⏳ MTF analiz yapılıyor...", parse_mode="Markdown")
    try:
        async with aiohttp.ClientSession() as session:
            ticker_resp, k15m, k1h, k4h, k1d, k1w = await asyncio.gather(
                session.get(f"{BINANCE_24H}?symbol={symbol}", timeout=aiohttp.ClientTimeout(total=5)),
                fetch_klines(session, symbol, "15m", limit=200),
                fetch_klines(session, symbol, "1h",  limit=200),
                fetch_klines(session, symbol, "4h",  limit=200),
                fetch_klines(session, symbol, "1d",  limit=200),
                fetch_klines(session, symbol, "1w",  limit=100),
            )
            ticker = await ticker_resp.json()

        price  = float(ticker.get("lastPrice", 0))
        if price == 0 or "code" in ticker:
            try: await wait.delete()
            except: pass
            await send_temp(context.bot, update.effective_chat.id,
                f"⚠️ *{symbol}* bulunamadı veya Binance'de işlem görmüyor.\n"
                "Sembolü kontrol edin. Örnek: `BTCUSDT`, `ETHUSDT`",
                parse_mode="Markdown")
            return
        ch24   = float(ticker.get("priceChangePercent", 0))
        vol24  = float(ticker.get("quoteVolume", 0))
        vol_str = f"{vol24/1_000_000:.1f}M" if vol24 >= 1_000_000 else f"{vol24/1_000:.0f}K"

        rank, total = await get_coin_rank(symbol)
        re_icon = rank_emoji(rank)
        rank_str = f"#{rank}" if rank else "—"
        is_fallback2 = marketcap_rank_cache.get("_fallback", True)
        rank_label2  = "Hacim" if is_fallback2 else "MCap"

        # ── Zaman Dilimi Özeti ──────────────────────────────────
        def tf_line(data, label):
            if not data or len(data) < 3:
                return f"  {label:<6} `veri yok`\n"
            rsi  = calc_rsi(data, 14)
            stch = calc_stoch_rsi(data)
            _, hist = calc_macd(data)
            ch   = calc_change(data[-2:])
            yon  = "▲" if ch > 0 else "▼"

            if rsi >= 70:   rsi_icon = "🔴"
            elif rsi >= 55: rsi_icon = "🟡"
            elif rsi <= 30: rsi_icon = "🔵"
            elif rsi <= 45: rsi_icon = "🟡"
            else:           rsi_icon = "🟢"

            macd_icon = "⬆" if hist > 0 else "⬇"
            return (
                f"  {label:<5} {yon}`{ch:+.2f}%`  "
                f"RSI{rsi_icon}`{rsi:.0f}`  "
                f"MACD{macd_icon}  "
                f"StRSI`{stch:.0f}`\n"
            )

        # ── Fibonacci + Destek/Direnç ───────────────────────────
        def calc_fibo_levels(data, lookback=200):
            if not data or len(data) < 10:
                return None
            window = data[-min(lookback, len(data)):]
            hi   = max(float(c[2]) for c in window)
            lo   = min(float(c[3]) for c in window)
            diff = hi - lo
            if diff == 0:
                return None
            cur = float(data[-1][4])
            ratios = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
            levels = [(f"{r*100:.1f}%", hi - diff * r) for r in ratios]
            return {"hi": hi, "lo": lo, "cur": cur, "levels": levels}

        def build_sr_fib_block(data):
            fib = calc_fibo_levels(data, lookback=200)
            sw_destek, sw_direnc = calc_support_resistance(data)
            lines = []

            if fib:
                cur  = fib["cur"]
                hi   = fib["hi"]
                lo   = fib["lo"]

                # En yakın alt ve üst Fib seviyeleri
                below = [(k, v) for k, v in fib["levels"] if v <= cur]
                above = [(k, v) for k, v in fib["levels"] if v >  cur]
                fib_sup = max(below, key=lambda x: x[1]) if below else None
                fib_res = min(above, key=lambda x: x[1]) if above else None

                # Fiyatın range içindeki pozisyonu
                pct_pos = ((cur - lo) / (hi - lo)) * 100

                lines.append(f"📐 *Fibonacci Seviyeleri* _(4s · 200 mum)_")
                lines.append(f"  Swing High : `{format_price(hi)}`")
                lines.append(f"  Swing Low  : `{format_price(lo)}`")
                lines.append(f"  Pozisyon   : `{pct_pos:.1f}%` _(alt=0 · üst=100)_")
                lines.append("")

                # Tüm seviyeleri göster, anlık seviyeyi vurgula
                for label, val in fib["levels"]:
                    dist = ((val - cur) / cur) * 100
                    if fib_res and label == fib_res[0]:
                        marker = " ◄ 🔴 Direnç"
                    elif fib_sup and label == fib_sup[0]:
                        marker = " ◄ 🔵 Destek"
                    else:
                        marker = ""
                    dist_str = f"`{dist:+.2f}%`" if abs(dist) < 50 else ""
                    lines.append(f"  `{label:<6}` `{format_price(val)}` {dist_str}{marker}")

            lines.append("")
            lines.append(f"🔵 *Swing Destek / Direnç* _(4s pivot)_")
            if sw_destek:
                d = ((price - sw_destek) / price) * 100
                lines.append(f"  🔵 Destek : `{format_price(sw_destek)}`  `{d:.2f}% altında`")
            else:
                lines.append(f"  🔵 Destek : —")
            if sw_direnc:
                d = ((sw_direnc - price) / price) * 100
                lines.append(f"  🔴 Direnç : `{format_price(sw_direnc)}`  `{d:.2f}% yukarıda`")
            else:
                lines.append(f"  🔴 Direnç : —")

            return "\n".join(lines)

        # ── Diverjans ────────────────────────────────────────────
        div_1h = calc_rsi_divergence(k1h)
        div_4h = calc_rsi_divergence(k4h)
        div_lines = []
        if div_1h == "bearish": div_lines.append("⚠️ 1s Bearish — RSI düşüyor, fiyat çıkıyor")
        if div_1h == "bullish": div_lines.append("💡 1s Bullish — RSI yükseliyor, fiyat düşüyor")
        if div_4h == "bearish": div_lines.append("⚠️ 4s Bearish — RSI düşüyor, fiyat çıkıyor")
        if div_4h == "bullish": div_lines.append("💡 4s Bullish — RSI yükseliyor, fiyat düşüyor")

        # ── Piyasa Skoru ─────────────────────────────────────────
        sh, lh, _ = calc_score_hourly(ticker, k1h, k15m, k15m, calc_rsi(k1h, 14))
        sd, ld, _ = calc_score_daily(ticker, k4h, k1h, k1d)
        sw, lw, _ = calc_score_weekly(ticker, k1d, k1w)

        # ── Yardımcı ─────────────────────────────────────────────
        def ch_icon(v):
            return "🟢▲" if v > 0 else ("🔴▼" if v < 0 else "⚪→")

        def score_bar(s):
            filled = round(s / 20)
            return "█" * filled + "░" * (5 - filled)

        # 7g / 30g değişim
        ch7d  = calc_change(k1d[-8:]) if k1d and len(k1d) >= 8 else 0.0
        ch30d = calc_change(k1d)      if k1d and len(k1d) >= 2 else 0.0

        # ── Mesaj ───────────────────────────────────────────────
        ch24_icon = ch_icon(ch24)
        text  = f"📊 *{symbol} — MTF Analiz*\n"
        text += f"━━━━━━━━━━━━━━━━━━\n"
        text += f"💵 Fiyat\n"
        text += f"  `{format_price(price)} USDT`\n"
        text += f"  {ch24_icon} 24sa: `{ch24:+.2f}%`\n"
        if rank:
            text += f"  {re_icon} {rank_label2}: `#{rank}`  📦 `{vol_str}`\n"
        else:
            text += f"  📦 Hacim: `{vol_str}`\n"
        text += f"\n"

        text += f"📈 *Performans*\n"
        text += f"  {ch_icon(calc_change(k15m[-2:] if k15m and len(k15m)>=2 else []))} 15dk : `{calc_change(k15m[-2:] if k15m and len(k15m)>=2 else []):+.2f}%`\n"
        text += f"  {ch_icon(calc_change(k1h[-2:]  if k1h  and len(k1h) >=2 else []))} 1sa  : `{calc_change(k1h[-2:]  if k1h  and len(k1h) >=2 else []):+.2f}%`\n"
        text += f"  {ch_icon(calc_change(k4h[-2:]  if k4h  and len(k4h) >=2 else []))} 4sa  : `{calc_change(k4h[-2:]  if k4h  and len(k4h) >=2 else []):+.2f}%`\n"
        text += f"  {ch24_icon} 24sa : `{ch24:+.2f}%`\n"
        text += f"  {ch_icon(ch7d)}  7gün : `{ch7d:+.2f}%`\n"
        text += f"  {ch_icon(ch30d)} 30gün: `{ch30d:+.2f}%`\n"
        text += f"\n"

        text += f"🎯 *Piyasa Skoru*\n"
        text += f"  ⏱ Saatlik\n"
        text += f"  `{score_bar(sh)}` `{sh}/100` — _{lh}_\n"
        text += f"  📅 Günlük\n"
        text += f"  `{score_bar(sd)}` `{sd}/100` — _{ld}_\n"
        text += f"  📆 Haftalık\n"
        text += f"  `{score_bar(sw)}` `{sw}/100` — _{lw}_\n"
        text += f"\n"

        text += f"📉 *Zaman Dilimi (RSI · MACD · StochRSI)*\n"
        text += tf_line(k15m, "15dk")
        text += tf_line(k1h,  "1sa ")
        text += tf_line(k4h,  "4sa ")
        text += tf_line(k1d,  "1gün")
        text += tf_line(k1w,  "1hft")
        text += f"\n"

        if div_lines:
            text += f"⚡ *Diverjans*\n"
            for dl in div_lines:
                text += f"  {dl}\n"
            text += f"\n"

        text += f"━━━━━━━━━━━━━━━━━━\n"
        text += build_sr_fib_block(k4h)
        text += f"\n\n_🔵 Aşırı Satım · 🟢 Normal · 🔴 Aşırı Alım_"

        await wait.delete()
        chat     = update.effective_chat
        is_group = chat and chat.type in ("group", "supergroup")
        sent_msg = await send_temp(context.bot, update.effective_chat.id, text, parse_mode="Markdown")
        if is_group and sent_msg:
            delay = await get_member_delete_delay()
            asyncio.create_task(auto_delete(context.bot, chat.id, sent_msg.message_id, delay))
            if update.message:
                asyncio.create_task(auto_delete(context.bot, chat.id, update.message.message_id, 3))

    except Exception as e:
        try: await wait.delete()
        except: pass
        log.error("MTF hatasi: " + str(e))
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Analiz sirasinda hata olustu.", parse_mode="Markdown")


# ================= WHALE ALARMI =================

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
    if not await check_group_access(update, context, "Zamanlanmış Görevler"):
        return
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
    chat    = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None
    in_group = chat and chat.type in ("group", "supergroup")
    admin_in_group = False
    if in_group and user_id:
        admin_in_group = await is_group_admin(context.bot, chat.id, user_id)

    base_buttons = [
        [InlineKeyboardButton("📊 Market",        callback_data="market"),
         InlineKeyboardButton("⚡ 5dk Flashlar",  callback_data="top5")],
        [InlineKeyboardButton("📈 24s Liderleri", callback_data="top24"),
         InlineKeyboardButton("⚙️ Durum",         callback_data="status")],
        [InlineKeyboardButton("🔔 Alarmlarım",    callback_data="my_alarm"),
         InlineKeyboardButton("⭐ Favorilerim",   callback_data="fav_liste")],
        [InlineKeyboardButton("📊 MTF Analiz",    callback_data="mtf_help"),
         InlineKeyboardButton("📅 Zamanla",       callback_data="zamanla_help")],
        [InlineKeyboardButton("🎯 Fiyat Hedefi",  callback_data="hedef_liste"),
         InlineKeyboardButton("💰 Kar/Zarar",     callback_data="kar_help")],
        [InlineKeyboardButton("📡 Sinyal",        callback_data="sinyal_help"),
         InlineKeyboardButton("👀 Watchlist",     callback_data="watchlist_help")],
        [InlineKeyboardButton("📐 Pattern",       callback_data="pattern_help"),
         InlineKeyboardButton("📊 Sektör",        callback_data="sektor_help")],
        [InlineKeyboardButton("🤖 AI Yorum",      callback_data="yorum_help"),
         InlineKeyboardButton("🌐 Makro",         callback_data="makro_help")],
    ]
    if in_group and admin_in_group:
        base_buttons.append([InlineKeyboardButton("📊 Anket Oluştur", callback_data="anket_help")])
        base_buttons.append([InlineKeyboardButton("🛠 Admin Ayarları", callback_data="set_open")])

    keyboard = InlineKeyboardMarkup(base_buttons)
    welcome_text = (
        "👋 <b>Kripto Analiz Asistanı</b>\n━━━━━━━━━━━━━━━━━━\n"
        "7/24 piyasayı izliyorum.\n\n"
        "📡 Sinyal: <code>/sinyal BTCUSDT</code>\n"
        "🤖 AI Yorum: <code>/yorum BTCUSDT</code>\n"
        "📐 Pattern: <code>/pattern BTCUSDT</code>\n"
        "🔬 Backtest: <code>/backtest BTCUSDT rsi</code>\n"
        "📊 Sektör: <code>/sektor</code>\n"
        "⚖️ Risk/Ödül: <code>/rr 100 95 115</code>\n"
        "💹 DCA: <code>/dca BTCUSDT 100 12</code>\n"
        "🔔 Alarm: <code>/alarm_ekle BTCUSDT 3.5</code>\n"
        "🎯 Hedef: <code>/hedef BTCUSDT 70000</code>"
    )

    if in_group:
        try:
            await update.message.delete()
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=chat.id,
            text=welcome_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))
    else:
        await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="HTML")

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
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb    = bool(update.callback_query)
    user_id  = update.effective_user.id if update.effective_user else None

    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    MIN_VOL = 1_000_000
    def safe_pct(c):
        try:
            op = float(c["openPrice"]); lp = float(c["lastPrice"])
            return ((lp - op) / op) * 100 if op > 0 else None
        except Exception: return None
    filtered = []
    for c in data:
        if not c["symbol"].endswith("USDT"): continue
        try: vol = float(c.get("quoteVolume", 0))
        except Exception: vol = 0
        if vol < MIN_VOL: continue
        pct = safe_pct(c)
        if pct is None: continue
        filtered.append((c, pct))
    usdt = sorted(filtered, key=lambda x: x[1], reverse=True)[:10]
    text = "🏆 *24 Saatlik Performans Liderleri*\n━━━━━━━━━━━━━━━━━━━━━\n"
    for i, (c, pct) in enumerate(usdt, 1):
        text += f"{get_number_emoji(i)} `{c['symbol']:<12}` → `%{pct:+6.2f}`\n"

    target = update.callback_query.message if is_cb else update.message
    msg = await target.reply_text(text, parse_mode="Markdown")
    if is_group:
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))
        if not is_cb and update.message:
            asyncio.create_task(auto_delete(context.bot, chat.id, update.message.message_id, 3))

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

    chat  = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb    = bool(update.callback_query)
    target = update.callback_query.message if is_cb else update.message
    msg = await target.reply_text(text, parse_mode="Markdown")
    if is_group:
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))
        if not is_cb and update.message:
            asyncio.create_task(auto_delete(context.bot, chat.id, update.message.message_id, 3))

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

    if q.data.startswith("set_"):
        await set_callback(update, context)
        return

    chat = q.message.chat if q.message else None
    is_group_chat = bool(chat and chat.type in ("group", "supergroup"))
    is_adm = False
    if is_group_chat:
        is_adm = await is_group_admin(context.bot, chat.id, q.from_user.id)

    # Grupta sadece bu callback'ler kısıtlama olmadan çalışır
    GROUP_FREE = {
        "top24", "top5", "market", "status",
        "sektor_help", "makro_help", "yorum_help",
    }

    # Grupta üye bu butonlara tıklayınca DM yönlendirmesi yapılır
    GROUP_DM_REDIRECT = {
        "my_alarm", "fav_liste", "zamanla_help", "kar_help",
        "mtf_help", "alarm_guide", "alarm_history",
    }

    async def dm_redirect(feature_name: str):
        """Fiyat Hedefi ile aynı pattern: DM'e mesaj + gruba kısa uyarı."""
        try:
            await context.bot.send_message(
                chat_id=q.from_user.id,
                text=f"🔒 *{feature_name}* özelliğini kullanmak için buraya tıklayın 👇\nBotu DM üzerinden kullanabilirsiniz.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        try:
            tip = await context.bot.send_message(
                chat_id=chat.id,
                text=f"🔒 {feature_name} için lütfen DM'den kullanın 👇 @KriptoDrop_alertbot",
            )
            asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
        except Exception:
            pass
        await q.answer()

    if is_group_chat and not is_adm:
        if q.data == "my_alarm":
            await dm_redirect("Alarmlarım")
            return
        elif q.data == "fav_liste" or q.data.startswith("fav_"):
            await dm_redirect("Favorilerim")
            return
        elif q.data == "kar_help" or q.data.startswith("kar_"):
            await dm_redirect("Kar/Zarar")
            return
        elif q.data == "zamanla_help":
            await dm_redirect("Zamanla")
            return
        elif q.data == "mtf_help" or q.data.startswith("mtf_sym_"):
            await dm_redirect("MTF Analiz")
            return
        elif q.data in ("alarm_guide", "alarm_history") or q.data.startswith("alarm_deleteall_"):
            await dm_redirect("Alarmlarım")
            return
        elif q.data not in GROUP_FREE \
                and not q.data.startswith("hedef_") \
                and not q.data.startswith("set_"):
            await dm_redirect("Bu özellik")
            return

    await q.answer()

    # ── Yeni özellik butonları ──
    if q.data == "sinyal_help":
        await q.message.reply_text(
            "<b>📡 Sinyal Sistemi</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/sinyal BTCUSDT</code>\n"
            "Geçmiş: /sinyal_gecmis\n\n"
            "🟢 AL / 🔴 SAT / 🟡 BEKLE",
            parse_mode="HTML")
    elif q.data == "watchlist_help":
        await q.message.reply_text(
            "<b>👀 Watchlist</b>\n━━━━━━━━━━━━━━━━━━\n"
            "<code>/watchlist ekle BTCUSDT</code>\n"
            "<code>/watchlist liste</code>\n"
            "<code>/watchlist rapor</code>\n"
            "<code>/watchlist sil BTCUSDT</code>",
            parse_mode="HTML")
    elif q.data == "pattern_help":
        await q.message.reply_text(
            "<b>📐 Pattern Tanıma</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/pattern BTCUSDT</code>\n\n"
            "Çift Dip/Tepe, Kanal, Bull Flag",
            parse_mode="HTML")
    elif q.data == "sektor_help":
        await sektor_command(update, context)
    elif q.data == "makro_help":
        await makro_command(update, context)
    elif q.data == "yorum_help":
        await q.message.reply_text(
            "<b>🤖 AI Piyasa Yorumu</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Kullanım: <code>/yorum BTCUSDT</code>\n\n"
            "Claude AI teknik analiz yorumu",
            parse_mode="HTML")
    elif q.data == "anket_help":
        await q.message.reply_text(
            "<b>📊 Anket Sistemi</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Yeni anket: <code>/anket_yeni Soru? | Seç1 | Seç2</code>\n"
            "Sonuç: <code>/anket sonuc</code>",
            parse_mode="HTML")
    elif q.data.startswith("anket_oy_"):
        try:
            parts = q.data.split("_")
            anket_id = int(parts[2]); secim = int(parts[3])
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO anket_oylar(anket_id,user_id,secim) VALUES($1,$2,$3) ON CONFLICT(anket_id,user_id) DO UPDATE SET secim=$3",
                    anket_id, q.from_user.id, secim)
            await q.answer("✅ Oyun kaydedildi!", show_alert=False)
        except Exception as e:
            log.error(f"anket_oy cb: {e}")
            await q.answer("⚠️ Hata oluştu.", show_alert=True)
    elif q.data.startswith("anket_sonuc_"):
        try:
            anket_id = int(q.data.split("_")[-1])
            async with db_pool.acquire() as conn:
                anket = await conn.fetchrow("SELECT * FROM anketler WHERE id=$1", anket_id)
                oylar = await conn.fetch("SELECT secim, COUNT(*) cnt FROM anket_oylar WHERE anket_id=$1 GROUP BY secim", anket_id)
            secenekler = anket["secenekler"].split("|")
            toplam = sum(r["cnt"] for r in oylar)
            oy_map = {r["secim"]: r["cnt"] for r in oylar}
            text = f"📊 <b>Anket #{anket_id}</b>\n━━━━━━━━━━━━━━━━━━\n<b>{anket['soru']}</b>\n\n"
            for i, sec in enumerate(secenekler):
                cnt = oy_map.get(i, 0)
                pct = cnt / toplam * 100 if toplam else 0
                bar = "█" * int(pct/10) + "░" * (10 - int(pct/10))
                text += f"<code>{i+1}.</code> {sec}\n   <code>{bar}</code> <code>{pct:.0f}%</code> ({cnt} oy)\n"
            text += f"\n<i>Toplam: {toplam} oy</i>"
            await q.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            log.error(f"anket_sonuc cb: {e}")
    # ── Market & genel ──
    elif q.data == "market":
        await market(update, context)
    elif q.data == "top24":
        await top24(update, context)
    elif q.data == "top5":
        await top5(update, context)
    elif q.data == "status":
        await status(update, context)

    # ── Alarm ──
    elif q.data == "my_alarm":
        await my_alarm_v2(update, context)
    elif q.data == "alarm_guide":
        await q.message.reply_text(
            "➕ *Alarm Turleri:*\n━━━━━━━━━━━━━━━━━━\n"
            "• `%` : `/alarm_ekle BTCUSDT 3.5`\n"
            "• Fiyat : `/alarm_ekle BTCUSDT fiyat 70000`\n"
            "• RSI : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant : `/alarm_ekle BTCUSDT bant 60000 70000`\n\n"
            "🗑 *Alarm Silmek Icin:*\n`/alarm_sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data.startswith("alarm_deleteall_"):
        uid = int(q.data.split("_")[-1])
        if q.from_user.id == uid:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM user_alarms WHERE user_id=$1", uid)
            await q.message.reply_text("🗑 Tum kisisel alarmlariniz silindi.")
    elif q.data == "alarm_history":
        await alarm_gecmis(update, context)

    # ── Favori ──
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

    # ── MTF ──
    elif q.data == "mtf_help":
        await q.message.reply_text(
            "📊 *Gelişmiş MTF Analiz*\n━━━━━━━━━━━━━━━━━━\n"
            "Analiz için sembol yazın:\n"
            "`/mtf BTCUSDT`\n"
            "`/mtf XRPUSDT`\n"
            "`/mtf ETHUSDT`\n\n"
            "15dk · 1sa · 4sa · 1gn · 1hf\n"
            "• RSI 14 + StochRSI + MACD\n"
            "• EMA çaprazlaması + OBV\n"
            "• Fibonacci + Destek/Direnç\n"
            "• Diverjans uyarıları",
            parse_mode="Markdown"
        )

    # ── Zamanla ──
    elif q.data == "zamanla_help":
        await q.message.reply_text(
            "⏰ *Zamanlanmış Görevler*\n━━━━━━━━━━━━━━━━━━\n"
            "Coin analizi: `/zamanla analiz BTCUSDT 09:00`\n"
            "Haftalık rapor: `/zamanla rapor 08:00`\n"
            "Liste: `/zamanla liste`\n"
            "Sil: `/zamanla sil`",
            parse_mode="Markdown"
        )

    # ── Fiyat Hedefi ──
    # Hedef butonları grup kısıtlamasından muaf — her yerden DM'e yönlendirir
    elif q.data in ("hedef_liste", "hedef_gecmis", "hedef_add_help") or \
         q.data.startswith("hedef_sil_id_") or q.data.startswith("hedef_sil_hepsi_"):

        # Grup üyesiyse DM'e yönlendir, DM'de devam et
        if is_group_chat and not await is_group_admin(context.bot, chat.id, q.from_user.id):
            try:
                await context.bot.send_message(
                    chat_id=q.from_user.id,
                    text=(
                        "🎯 *Fiyat Hedefi* özelliğini kullanmak için buraya tıklayın 👇\n"
                        "Hedeflerinizi DM üzerinden yönetebilirsiniz."
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            try:
                tip = await context.bot.send_message(
                    chat_id=chat.id,
                    text="🔒 Fiyat Hedefi için lütfen DM'den kullanın 👇 @KriptoDrop_alertbot",
                )
                asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
            except Exception:
                pass
            return

        if q.data == "hedef_liste":
            await hedef_liste_goster(context.bot, q.from_user.id, q.from_user.id, edit_message=None)

        elif q.data == "hedef_gecmis":
            await hedef_liste_goster(context.bot, q.from_user.id, q.from_user.id, show_all=True, edit_message=None)

        elif q.data == "hedef_add_help":
            await q.message.reply_text(
                "🎯 *Fiyat Hedefi Ekle*\n━━━━━━━━━━━━━━━━━━\n"
                "Tek hedef:\n`/hedef BTCUSDT 70000`\n\n"
                "Çoklu hedef (aynı coin, birden fazla fiyat):\n"
                "`/hedef BTCUSDT 65000 70000 80000`\n\n"
                "Hedef listeye ulaşınca DM bildirim alırsınız.\n\n"
                "Sil: `/hedef sil BTCUSDT`\n"
                "Geçmiş: `/hedef gecmis`",
                parse_mode="Markdown"
            )

        elif q.data.startswith("hedef_sil_id_"):
            hedef_id = int(q.data.replace("hedef_sil_id_", ""))
            user_id  = q.from_user.id
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT symbol, target_price AS target FROM price_targets WHERE id=$1 AND user_id=$2",
                    hedef_id, user_id
                )
                if row:
                    await conn.execute(
                        "DELETE FROM price_targets WHERE id=$1 AND user_id=$2",
                        hedef_id, user_id
                    )
                    await q.answer(f"✅ {row['symbol']} @ {format_price(row['target'])} silindi", show_alert=False)
                    await hedef_liste_goster(context.bot, user_id, user_id, edit_message=q.message)
                else:
                    await q.answer("❌ Hedef bulunamadı.", show_alert=True)

        elif q.data.startswith("hedef_sil_hepsi_"):
            uid = int(q.data.split("_")[-1])
            if q.from_user.id == uid:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE price_targets SET active=0 WHERE user_id=$1 AND active=1", uid
                    )
                await q.answer("🗑 Aktif hedefler silindi.", show_alert=False)
                await hedef_liste_goster(context.bot, uid, uid, edit_message=q.message)

    # ── Kar/Zarar ──
    elif q.data == "kar_help":
        await q.message.reply_text(
            "💰 *Kar/Zarar Hesabı*\n━━━━━━━━━━━━━━━━━━\n"
            "Hızlı hesap: `/kar BTCUSDT 0.5 60000`\n"
            "  miktar: 0.5 BTC, alış: 60000 USDT\n\n"
            "Pozisyon kaydet/takip: `/kar liste`\n"
            "Sil: `/kar sil BTCUSDT`",
            parse_mode="Markdown"
        )
    elif q.data.startswith("kar_kaydet_"):
        parts = q.data.split("_")
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

    # ── Admin ──
    elif q.data == "set_open":
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
            "SELECT user_id, symbol, threshold, alarm_type, rsi_level, band_low, band_high, paused_until, trigger_count FROM user_alarms WHERE active=1"
        )

    if group_row and group_row["alarm_active"]:
        threshold = group_row["threshold"]
        mode      = group_row["mode"]
        for symbol, prices in list(price_memory.items()):
            if len(prices) < 2:
                continue
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
            await send_full_analysis(context.bot, GROUP_CHAT_ID, symbol, yon, threshold, ch5_override=round(ch5, 2), alarm_mode=True)

    for row in user_rows:
        symbol     = row["symbol"]
        user_id    = row["user_id"]
        threshold  = row["threshold"]
        alarm_type = row.get("alarm_type", "percent")
        rsi_level  = row.get("rsi_level")
        band_low   = row.get("band_low")
        band_high  = row.get("band_high")
        paused     = row.get("paused_until")

        if paused and paused.replace(tzinfo=None) > now:
            continue

        prices = price_memory.get(symbol)
        if not prices or len(prices) < 2:
            continue
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
    # Bot restart sonrası bekleyen silme işlemlerini yeniden zamanla
    await replay_pending_deletes(app.bot)
    await app.bot.set_my_commands([
        BotCommand("start",          "Botu başlat / Ana menü"),
        BotCommand("hedef",          "Fiyat hedefi ekle / listele"),
        BotCommand("alarmim",        "Kişisel alarmlarım"),
        BotCommand("alarm_ekle",     "Yeni alarm ekle"),
        BotCommand("alarm_sil",      "Alarm sil"),
        BotCommand("alarm_duraklat", "Alarmı duraklat"),
        BotCommand("alarm_gecmis",   "Alarm geçmişi"),
        BotCommand("favori",         "Favori coinler"),
        BotCommand("mtf",            "Gelişmiş MTF analiz"),
        BotCommand("zamanla",        "Zamanlanmış görev"),
        BotCommand("kar",            "Kar/zarar hesabı"),
        BotCommand("top24",          "24s liderleri"),
        BotCommand("top5",           "5dk hareketliler"),
        BotCommand("market",         "Piyasa duyarlılığı"),
        BotCommand("status",         "Bot durumu"),
        BotCommand("set",            "Admin ayarları"),
        BotCommand("sinyal",         "AL/SAT/BEKLE sinyali"),
        BotCommand("sinyal_gecmis",  "Sinyal geçmişim"),
        BotCommand("sr_alarm",       "Destek/Direnç alarmı"),
        BotCommand("ath",            "ATH/ATL analizi"),
        BotCommand("sektor",         "Sektör performansı"),
        BotCommand("pattern",        "Grafik formasyon analizi"),
        BotCommand("dca",            "DCA hesaplayıcı"),
        BotCommand("rr",             "Risk/Ödül hesaplayıcı"),
        BotCommand("makro",          "Global makro takvim"),
        BotCommand("brief",          "Günlük özet brief"),
        BotCommand("watchlist",      "İzleme listesi"),
        BotCommand("backtest",       "Strateji backtesting"),
        BotCommand("anket",          "Anket sistemi"),
        BotCommand("anket_yeni",     "Yeni anket oluştur (admin)"),
        BotCommand("yorum",          "AI piyasa yorumu"),
    ])

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job,            interval=10,   first=30)
    app.job_queue.run_repeating(whale_job,            interval=120,  first=60)
    app.job_queue.run_repeating(scheduled_job,        interval=60,   first=10)
    app.job_queue.run_repeating(hedef_job,            interval=30,   first=45)
    app.job_queue.run_repeating(marketcap_refresh_job, interval=600,  first=5)
    app.job_queue.run_repeating(sr_alarm_job,          interval=600,  first=120)
    app.job_queue.run_repeating(volatilite_job,        interval=1800, first=180)
    app.job_queue.run_repeating(portfolyo_alarm_job,   interval=600,  first=150)
    app.job_queue.run_repeating(brief_job,             interval=60,   first=30)
    app.job_queue.run_repeating(haftalik_rapor_job,    interval=300,  first=60)

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("top24",          top24))
    app.add_handler(CommandHandler("top5",           top5))
    app.add_handler(CommandHandler("market",         market))
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("set",            set_command))
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

    app.add_handler(CommandHandler("sinyal",        sinyal_command))
    app.add_handler(CommandHandler("sinyal_gecmis", sinyal_gecmis_command))
    app.add_handler(CommandHandler("sr_alarm",      sr_alarm_command))
    app.add_handler(CommandHandler("ath",           ath_command))
    app.add_handler(CommandHandler("sektor",        sektor_command))
    app.add_handler(CommandHandler("pattern",       pattern_command))
    app.add_handler(CommandHandler("dca",           dca_command))
    app.add_handler(CommandHandler("rr",            rr_command))
    app.add_handler(CommandHandler("makro",         makro_command))
    app.add_handler(CommandHandler("brief",         brief_command))
    app.add_handler(CommandHandler("watchlist",     watchlist_command))
    app.add_handler(CommandHandler("backtest",      backtest_command))
    app.add_handler(CommandHandler("anket",         anket_command))
    app.add_handler(CommandHandler("anket_yeni",    anket_yeni_command))
    app.add_handler(CommandHandler("yorum",         yorum_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("BOT AKTIF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
