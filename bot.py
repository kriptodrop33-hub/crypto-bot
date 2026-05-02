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

from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand, WebAppInfo
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
GROUP_CHAT_ID = int(os.getenv("GROUP_ID", "0"))
DATABASE_URL  = os.getenv("DATABASE_URL")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))        # Bot sahibinin Telegram ID'si
BOT_USERNAME  = os.getenv("BOT_USERNAME", "botunuz")   # Örnek: KriptoDrop_alertbot (@ olmadan)
GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")     # Groq ücretsiz GPT (llama3)
CRYPTOPANIC_KEY    = os.getenv("CRYPTOPANIC_KEY", "")  # CryptoPanic ücretsiz API
# Mini App URL — Railway otomatik verir, elle girmeye gerek yok
# Eğer RAILWAY_STATIC_URL veya RAILWAY_PUBLIC_DOMAIN varsa otomatik kullanılır
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_URL", "").replace("https://","").replace("http://","")
MINIAPP_URL = os.getenv("MINIAPP_URL") or (f"https://{_railway_domain}" if _railway_domain else "")

def get_miniapp_url() -> str:
    """Runtime'da MINIAPP_URL'yi döndürür. _start_miniapp_server set ettikten sonra da çalışır."""
    return MINIAPP_URL

BINANCE_24H    = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COOLDOWN_MINUTES  = 15
DEFAULT_THRESHOLD = 5.0
DEFAULT_MODE      = "both"
MAX_SYMBOLS       = 500

# ── Grup alarmı filtreleri ──
MIN_ALARM_VOLUME_24H = 1_000_000   # Minimum 24s hacim (USDT) — altındaki coinler grup alarmı tetiklemez
MIN_ALARM_RANK       = 300         # MarketCap sıralamasında ilk N coin (cache'de yoksa filtre atlanır)
ALARM_BLACKLIST      = {           # Bu semboller grup alarmı tetiklemez (küçük/manipüle coinler)
    "DEGOUSDT", "LEVERUSDT", "LOOKSUSDT",
}

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id      BIGINT PRIMARY KEY,
                username     TEXT,
                full_name    TEXT,
                first_seen   TIMESTAMPTZ DEFAULT NOW(),
                last_active  TIMESTAMPTZ DEFAULT NOW(),
                command_count INTEGER DEFAULT 1,
                chat_type    TEXT DEFAULT 'private'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sentiment_cache (
                symbol      TEXT PRIMARY KEY,
                score       REAL,
                label       TEXT,
                summary     TEXT,
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS takvim_subscribers (
                user_id  BIGINT PRIMARY KEY,
                active   INTEGER DEFAULT 1
            )
        """)

    log.info("PostgreSQL bağlantısı kuruldu.")

# ================= MEMORY =================

price_memory:       dict = {}
cooldowns:          dict = {}
chart_cache:        dict = {}
whale_vol_mem:      dict = {}
scheduled_last_run: dict = {}
coin_image_cache:   dict = {}
volume_24h_cache:   dict = {}   # symbol -> float (24s quote volume, WebSocket'ten güncellenir)

# ================= HTTP SESSION =================
# Global aiohttp session — TCP bağlantı havuzu için tek session kullan (#25)
_http_session: aiohttp.ClientSession = None

async def get_http_session() -> aiohttp.ClientSession:
    """Tekil global aiohttp session döndürür. Socket exhaustion'dan kaçınmak için."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=20)
        )
    return _http_session

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
    global marketcap_rank_cache, coin_image_cache
    now = datetime.utcnow()
    cg_cache = {}
    new_img_cache = {}
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
                            _img = coin.get('image') or ''
                            if _img and raw_sym and raw_sym.lower() not in new_img_cache:
                                new_img_cache[raw_sym.lower()] = _img
                except asyncio.TimeoutError:
                    log.warning(f"CoinGecko sayfa {page} timeout")
                    break
                await asyncio.sleep(1.5)
    except Exception as e:
        log.warning(f"CoinGecko hata: {e}")

    if new_img_cache:
        coin_image_cache.update(new_img_cache)

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
MAX_PENDING_DELETES = 500
_pending_deletes: list[tuple] = []   # (delete_at_ts, chat_id, message_id)

async def auto_delete(bot, chat_id, message_id, delay=30):
    import time as _t
    delete_at = _t.time() + delay
    # Max boyut kontrolü — eski kayıtları temizle
    if len(_pending_deletes) >= MAX_PENDING_DELETES:
        _pending_deletes[:] = _pending_deletes[-MAX_PENDING_DELETES // 2:]
    _pending_deletes.append((delete_at, chat_id, message_id))
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        log.info(f"[auto_delete] ✅ Mesaj silindi: chat={chat_id} msg={message_id}")
    except Exception as e:
        log.warning(f"[auto_delete] ❌ Silinemedi: chat={chat_id} msg={message_id} | Hata: {e}")
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
    # Cache boyut sınırı — en eski girişleri temizle
    if len(chart_cache) >= 100:
        oldest = sorted(chart_cache.items(), key=lambda x: x[1][0])[:20]
        for k, _ in oldest:
            del chart_cache[k]
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
            figcolor="#0d1117", gridcolor="#30363d", gridstyle="--",
            rc={"axes.labelcolor":"#8b949e","xtick.color":"#8b949e",
                "ytick.color":"#8b949e","font.size":9,
                "grid.alpha": 0.4}
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
        log.error(f"Grafik hatası ({symbol}): {e}")
        return None


# ================= FİBONACCİ =================

FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_COLORS = ["#FFD700","#FF8C00","#FF4500","#00CED1","#1E90FF","#9370DB","#32CD32"]

async def generate_fib_chart(symbol: str, interval: str = "4h", limit: int = 100):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.lines as mlines

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()

        if not data or isinstance(data, dict) or len(data) < 20:
            return None, None

        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        df = df[["open","high","low","close","volume"]].astype(float)

        swing_high = df["high"].max()
        swing_low  = df["low"].min()
        diff       = swing_high - swing_low
        trend_up   = df["close"].iloc[-1] > df["close"].iloc[0]
        cur        = df["close"].iloc[-1]

        fib_prices = {}
        for lvl in FIB_LEVELS:
            fib_prices[lvl] = swing_high - diff * lvl if trend_up else swing_low + diff * lvl

        # En yakın fib seviyeleri (destek + direnç)
        nearest     = min(fib_prices.items(), key=lambda x: abs(x[1] - cur))
        sup_fib     = max(((lvl, p) for lvl, p in fib_prices.items() if p <= cur), key=lambda x: x[1], default=None)
        res_fib     = min(((lvl, p) for lvl, p in fib_prices.items() if p > cur),  key=lambda x: x[1], default=None)

        # Hangi zone içinde
        zone_lo = sup_fib[1] if sup_fib else swing_low
        zone_hi = res_fib[1] if res_fib else swing_high
        zone_lo_lvl = sup_fib[0] if sup_fib else 1.0
        zone_hi_lvl = res_fib[0] if res_fib else 0.0

        # Retracement yüzdesi
        retrace_pct = round((swing_high - cur) / diff * 100, 1) if trend_up else round((cur - swing_low) / diff * 100, 1)

        # ── GRAFİK ──
        mc = mpf.make_marketcolors(
            up="#00e676", down="#ff1744",
            edge="inherit", wick="inherit",
            volume={"up":"#00e676","down":"#ff1744"},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            facecolor="#0d1117", edgecolor="#30363d",
            figcolor="#0d1117", gridcolor="#30363d", gridstyle="--",
            rc={"axes.labelcolor":"#8b949e","xtick.color":"#8b949e",
                "ytick.color":"#8b949e","font.size":8,
                "grid.alpha": 0.4}
        )

        fig, axes = mpf.plot(
            df, type="candle", style=style,
            title=f"\n{symbol} — Fibonacci Retracement ({interval})",
            ylabel="Fiyat (USDT)", volume=True, figsize=(10, 6),
            returnfig=True
        )
        ax = axes[0]
        n = len(df)

        # Zone dolgusu (fiyatın bulunduğu iki fib arasını renklendir)
        ax.axhspan(zone_lo, zone_hi, alpha=0.07, color="#0a84ff", zorder=0)

        # Fib çizgileri
        for lvl, color in zip(FIB_LEVELS, FIB_COLORS):
            price = fib_prices[lvl]
            is_nearest = (lvl == nearest[0])
            is_sup = sup_fib and lvl == sup_fib[0]
            is_res = res_fib and lvl == res_fib[0]

            lw        = 1.6 if is_nearest else (1.1 if (is_sup or is_res) else 0.7)
            alpha_val = 1.0 if is_nearest else (0.9 if (is_sup or is_res) else 0.65)
            ls        = "-" if is_nearest else "--"

            ax.axhline(y=price, color=color, linewidth=lw, linestyle=ls, alpha=alpha_val, zorder=1)

            lp = f"{price:,.4f}" if price < 1 else f"{price:,.2f}"
            badge = ""
            if is_nearest:   badge = " ◀"
            elif is_sup:     badge = " ▲"
            elif is_res:     badge = " ▼"

            ax.text(
                n * 0.005, price,
                f" {lvl:.3f} — {lp}{badge}",
                color=color, fontsize=7.5 if is_nearest else 6.5,
                va="bottom", alpha=alpha_val,
                fontweight="bold" if is_nearest else "normal"
            )

        # Mevcut fiyat yatay çizgisi — belirgin mavi
        ax.axhline(y=cur, color="#0a84ff", linewidth=1.8, linestyle="-", alpha=0.9, zorder=5)

        # Fiyat balonu — sağ kenarda
        fp_str = f"{cur:,.4f}" if cur < 1 else f"{cur:,.2f}"
        ax.text(
            n * 1.001, cur,
            f" ${fp_str}",
            color="#ffffff",
            fontsize=8, fontweight="bold", va="center",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#0a84ff", edgecolor="none", alpha=0.92)
        )

        # Sol kenarda "▶" ok işareti
        ax.text(
            n * 0.005, cur,
            "▶",
            color="#0a84ff", fontsize=9, va="center", fontweight="bold"
        )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=110, facecolor="#0d1117")
        plt.close(fig)
        buf.seek(0)

        # ── MESAJ METNİ ──
        def fp(v): return f"{v:,.4f}" if v < 1 else f"{v:,.2f}"

        trend_icon = "📈" if trend_up else "📉"
        trend_lbl  = "Yukarı Trend" if trend_up else "Aşağı Trend"

        text = (
            f"📐 *{symbol} — Fibonacci Haritası*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕰️ *Periyot:* `{interval}`\n"
            f"{trend_icon} *Trend:* {trend_lbl}\n"
            f"↕️ *Geri Çekilme:* `%{retrace_pct}`\n"
            f"📈 *Swing High:* `{fp(swing_high)} USDT`\n"
            f"📉 *Swing Low:* `{fp(swing_low)} USDT`\n"
            f"📍 *Anlık Fiyat:* `{fp(cur)} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧭 *Yakın Seviyeler*\n"
        )

        # Destek / Direnç özeti
        if sup_fib:
            dist_sup = round((cur - sup_fib[1]) / cur * 100, 2)
            text += f"🟢 *Destek:* Fib `{sup_fib[0]:.3f}` → `{fp(sup_fib[1])} USDT`  `(-{dist_sup}%)`\n"
        if res_fib:
            dist_res = round((res_fib[1] - cur) / cur * 100, 2)
            text += f"🔴 *Direnc:* Fib `{res_fib[0]:.3f}` → `{fp(res_fib[1])} USDT`  `(+{dist_res}%)`\n"
        text += f"━━━━━━━━━━━━━━━━━━━━━\n📌 *Fib Seviyeleri*\n"

        # Tüm seviyeler
        for lvl in FIB_LEVELS:
            p = fib_prices[lvl]
            lp = fp(p)
            dist = round((p - cur) / cur * 100, 2)
            dist_str = f"`{dist:+.2f}%`"
            if lvl == nearest[0]:
                marker = "🟢"
            elif sup_fib and lvl == sup_fib[0]:
                marker = "🟢"
            elif res_fib and lvl == res_fib[0]:
                marker = "🔴"
            else:
                marker = "⚪"
            text += f"{marker} Fib `{lvl:.3f}` → `{lp} USDT`  •  {dist_str}\n"

        return buf, text
    except Exception as e:
        log.error(f"Fib grafik hatasi ({symbol}): {e}")
        return None, None

async def fib_command(update: Update, context):
    chat    = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        if update.message:
            try: await update.message.delete()
            except Exception: pass
        if not await is_group_admin(context.bot, chat.id, update.effective_user.id):
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"🔒 *Fibonacci Analizi* özelliğini kullanmak için buraya tıklayın 👇\nBotu DM üzerinden kullanabilirsiniz.",
                    parse_mode="Markdown"
                )
            except Exception: pass
            try:
                tip = await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"🔒 Fibonacci Analizi için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
                )
                asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
            except Exception: pass
            return
    args    = context.args or []
    if not args:
        await send_temp(context.bot, chat.id,
            "📐 *Fibonacci Kullanımı:*\n"
            "`/fib BTCUSDT` — 4 saatlik\n"
            "`/fib BTCUSDT 1h` — 1 saatlik\n"
            "`/fib BTCUSDT 1d` — Günlük",
            parse_mode="Markdown")
        return
    await register_user(update)
    symbol   = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    interval = args[1] if len(args) > 1 else "4h"
    if interval not in ["1h","2h","4h","6h","12h","1d","3d","1w"]: interval = "4h"

    loading = await send_temp(context.bot, chat.id, f"📐 `{symbol}` Fibonacci hesaplanıyor...", parse_mode="Markdown")
    buf, text = await generate_fib_chart(symbol, interval)
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    if buf is None:
        await send_temp(context.bot, chat.id, f"⚠️ `{symbol}` için veri alınamadı.", parse_mode="Markdown")
        return

    is_group = chat.type in ("group", "supergroup")
    delay    = (await get_member_delete_delay()) if is_group else None
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("1h",  callback_data=f"fib_{symbol}_1h"),
        InlineKeyboardButton("4h",  callback_data=f"fib_{symbol}_4h"),
        InlineKeyboardButton("1d",  callback_data=f"fib_{symbol}_1d"),
        InlineKeyboardButton("1w",  callback_data=f"fib_{symbol}_1w"),
    ]])
    msg = await context.bot.send_photo(
        chat_id=chat.id,
        photo=buf,
        caption=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    if is_group and delay:
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))

# ================= SENTIMENT ANALİZİ =================

async def fetch_sentiment(symbol: str) -> dict:
    base = symbol.replace("USDT","").upper()

    # CoinGecko ID mapping (sembol → CoinGecko ID)
    CG_ID_MAP = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "ADA": "cardano", "XRP": "ripple",
        "DOT": "polkadot", "DOGE": "dogecoin", "AVAX": "avalanche-2",
        "MATIC": "matic-network", "POL": "matic-network",
        "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
        "LTC": "litecoin", "BCH": "bitcoin-cash", "FIL": "filecoin",
        "TRX": "tron", "NEAR": "near", "APT": "aptos",
        "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
        "TON": "the-open-network", "SHIB": "shiba-inu",
        "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk",
    }
    news_items = []

    # 1. CryptoPanic API (key varsa)
    if CRYPTOPANIC_KEY:
        try:
            url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_KEY}&currencies={base}&kind=news&public=true"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in (data.get("results") or [])[:10]:
                            title = item.get("title","")
                            votes = item.get("votes",{})
                            if title:
                                news_items.append({
                                    "title": title,
                                    "positive": votes.get("positive",0),
                                    "negative": votes.get("negative",0),
                                })
        except Exception as e:
            log.warning(f"CryptoPanic hata: {e}")

    # 2. CoinGecko topluluk sentiment (her zaman çalışır, key gerektirmez)
    cg_id = CG_ID_MAP.get(base, base.lower())
    try:
        url = (f"https://api.coingecko.com/api/v3/coins/{cg_id}"
               f"?localization=false&tickers=false&market_data=true"
               f"&community_data=true&developer_data=false")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    up_pct   = data.get("sentiment_votes_up_percentage") or None
                    down_pct = data.get("sentiment_votes_down_percentage") or None
                    price    = (data.get("market_data") or {}).get("current_price",{}).get("usd", 0)
                    pct24    = (data.get("market_data") or {}).get("price_change_percentage_24h", 0)
                    desc     = ((data.get("description") or {}).get("en") or "")[:200]

                    # Eğer CryptoPanic da yoksa CoinGecko ile bitir
                    if not news_items and up_pct is not None:
                        score = round(up_pct / 100, 2)
                        label = "🟢 Pozitif" if up_pct > 55 else ("🔴 Negatif" if up_pct < 45 else "🟡 Nötr")
                        trend = "📈 Yükseliş" if pct24 > 0 else "📉 Düşüş"
                        summary = (f"Topluluk oylaması: %{up_pct:.1f} yükseliş / %{down_pct:.1f} düşüş beklentisi. "
                                   f"24s {trend}: %{abs(pct24):.2f}")
                        return {
                            "score": score, "label": label, "summary": summary,
                            "news_count": 0, "source": "CoinGecko Topluluk",
                            "price": price, "pct24": pct24,
                        }
                    # CryptoPanic haberleri varsa CoinGecko fiyat verisini ekle
                    elif up_pct is not None:
                        # Haber listesine CoinGecko sentiment'i de faktör olarak ekle
                        if up_pct > 55:
                            news_items.append({"title": f"{base} community bullish sentiment %{up_pct:.0f}", "positive": 3, "negative": 0})
                        elif up_pct < 45:
                            news_items.append({"title": f"{base} community bearish sentiment %{down_pct:.0f}", "positive": 0, "negative": 3})
    except Exception as e:
        log.warning(f"CoinGecko sentiment hata: {e}")

    # 3. Haber yoksa RSS fallback
    if not news_items:
        try:
            import xml.etree.ElementTree as ET
            rss_url = f"https://cryptopanic.com/news/{base.lower()}/rss/"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    rss_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        xml_text = await resp.text()
                        root = ET.fromstring(xml_text)
                        ch   = root.find("channel")
                        if ch is not None:
                            for item in (ch.findall("item") or [])[:8]:
                                t = item.find("title")
                                if t is not None and t.text:
                                    news_items.append({"title": t.text.strip(), "positive": 1, "negative": 0})
        except Exception as e:
            log.warning(f"RSS sentiment fallback hata: {e}")

    # Hiç veri yoksa
    if not news_items:
        return {
            "score": 0.5, "label": "🟡 Veri Yok",
            "summary": f"{base} için şu an yeterli haber verisi bulunamadı. Daha sonra tekrar deneyin.",
            "news_count": 0, "source": "-", "price": 0, "pct24": 0,
        }

    # 4. Groq AI analizi (key varsa)
    if GROQ_API_KEY:
        try:
            headlines = "\n".join([f"- {n['title']}" for n in news_items[:8]])
            prompt = (
                f"{base} kripto parası hakkındaki haberleri analiz et.\n"
                f"YALNIZCA şu formatta yanıt ver, başka hiçbir şey yazma:\n"
                f"SKOR: (0.0 ile 1.0 arası ondalık sayı)\n"
                f"ETIKET: (Pozitif veya Negatif veya Notr)\n"
                f"OZET: (Türkçe, maksimum 2 cümle)\n\n"
                f"Haberler:\n{headlines}"
            )
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200, "temperature": 0.3
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        result  = await resp.json()
                        content = result["choices"][0]["message"]["content"]
                        score, label_raw, ozet = 0.5, "Notr", "-"
                        for line in content.strip().split("\n"):
                            ll = line.strip()
                            if ll.startswith("SKOR:"):
                                try: score = max(0.0, min(1.0, float(ll.split(":",1)[1].strip())))
                                except Exception: pass
                            elif ll.startswith("ETIKET:"):
                                label_raw = ll.split(":",1)[1].strip()
                            elif ll.startswith("OZET:"):
                                ozet = ll.split(":",1)[1].strip()
                        label = ("🟢 Pozitif" if any(x in label_raw.lower() for x in ["pozitif","positive"])
                                 else "🔴 Negatif" if any(x in label_raw.lower() for x in ["negatif","negative"])
                                 else "🟡 Nötr")
                        return {
                            "score": score, "label": label, "summary": ozet,
                            "news_count": len(news_items), "source": "Groq AI (Llama3)",
                            "price": 0, "pct24": 0,
                        }
        except Exception as e:
            log.warning(f"Groq hata: {e}")

    # 5. Basit oy bazlı hesaplama (Groq yoksa)
    total_pos = sum(n["positive"] for n in news_items)
    total_neg = sum(n["negative"] for n in news_items)
    total     = total_pos + total_neg or 1
    score     = round(total_pos / total, 2)
    label     = "🟢 Pozitif" if score > 0.55 else ("🔴 Negatif" if score < 0.45 else "🟡 Nötr")
    return {
        "score": score, "label": label,
        "summary": f"{len(news_items)} haber tarandı. {total_pos} olumlu / {total_neg} olumsuz sinyal.",
        "news_count": len(news_items), "source": "CryptoPanic RSS",
        "price": 0, "pct24": 0,
    }

async def sentiment_command(update: Update, context):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        if update.message:
            try: await update.message.delete()
            except Exception: pass
        if not await is_group_admin(context.bot, chat.id, update.effective_user.id):
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"🔒 *Sentiment Analizi* özelliğini kullanmak için buraya tıklayın 👇\nBotu DM üzerinden kullanabilirsiniz.",
                    parse_mode="Markdown"
                )
            except Exception: pass
            try:
                tip = await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"🔒 Sentiment Analizi için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
                )
                asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
            except Exception: pass
            return
    args = context.args or []
    if not args:
        await send_temp(context.bot, chat.id,
            "🧠 *Sentiment Analizi*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Kullanim icin bir sembol gir:\n"
            "`/sentiment BTCUSDT`\n"
            "`/sentiment ETH`",
            parse_mode="Markdown")
        return
    await register_user(update)
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    loading = await send_temp(context.bot, chat.id,
        f"🧠 `{symbol}` icin sentiment verileri toplanıyor...", parse_mode="Markdown")
    result  = await fetch_sentiment(symbol)
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    bar = "🟩" * int(result["score"]*10) + "⬜" * (10 - int(result["score"]*10))
    text = (
        f"🧠 *{symbol} — Sentiment Analizi*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💭 *Genel Duygu:* {result['label']}\n"
        f"📊 *Skor:* `{result['score']:.2f}` / `1.00`\n"
        f"{bar}\n"
        f"📰 *Haber Sayısı:* `{result['news_count']}`\n"
        f"🔎 *Kaynak:* `{result['source']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 *Ozet:* _{result['summary']}_\n"
        f"🕒 _Güncelleme: {datetime.utcnow().strftime('%H:%M UTC')}_"
    )
    is_group = chat.type in ("group", "supergroup")
    is_private = chat.type == "private"
    delay    = (await get_member_delete_delay()) if is_group else None
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",  callback_data=f"sent_{symbol}"),
        InlineKeyboardButton("📊 Analiz",  callback_data=f"analyse_{symbol}"),
    ]])
    msg = await context.bot.send_message(
        chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=keyboard if is_private else None,
    )
    if is_group and delay:
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))

# ================= TERİM SÖZLÜĞÜ =================

SOZLUK = {
    "macd": "📘 *MACD — Moving Average Convergence Divergence*\n━━━━━━━━━━━━━━━━━━━━━\n12 ve 26 günlük EMA farkından üretilen momentum göstergesi.\n\n📌 *Yorumu:*\n• MACD sinyal çizgisini yukarı keser → 🟢 Alım\n• MACD sinyal çizgisini aşağı keser → 🔴 Satım\n• Histogram (+) → Yükseliş momentum\n• Histogram (-) → Düşüş momentum",
    "rsi": "📘 *RSI — Relative Strength Index*\n━━━━━━━━━━━━━━━━━━━━━\n0-100 arası osilatör. Aşırı alım/satım bölgelerini gösterir.\n\n📌 *Seviyeleri:*\n• RSI > 70 → 🔴 Aşırı alım\n• RSI < 30 → 🟢 Aşırı satım\n• RSI = 50 → Nötr\n\n⚠️ Güçlü trendlerde uzun süre aşırı bölgede kalabilir.",
    "bollinger": "📘 *Bollinger Bantları*\n━━━━━━━━━━━━━━━━━━━━━\n20 günlük SMA ±2 standart sapma ile çizilen 3 bant.\n\n📌 *Yorumu:*\n• Üst banda temas → 🔴 Aşırı alım\n• Alt banda temas → 🟢 Aşırı satım\n• Bantlar daralıyor → ⚡ Büyük hareket yaklaşıyor",
    "ema": "📘 *EMA — Exponential Moving Average*\n━━━━━━━━━━━━━━━━━━━━━\nSon fiyatlara daha fazla ağırlık veren hareketli ortalama.\n\n📌 *Kullanım:*\n• EMA 9/21 → Kısa vade sinyal\n• EMA 50/200 kesişimi → Altın/Ölüm Çarpazı\n• Fiyat EMA200 üstünde → 🟢 Uzun vade yükseliş",
    "sma": "📘 *SMA — Simple Moving Average*\n━━━━━━━━━━━━━━━━━━━━━\nKapanış fiyatlarının aritmetik ortalaması.\n\n📌 *Kullanım:*\n• Destek/direnç olarak işlev görür\n• SMA50 ve SMA200 en yaygın\n• Fiyat SMA üstündeyse → Trend yukarı",
    "fibonacci": "📘 *Fibonacci Retracement*\n━━━━━━━━━━━━━━━━━━━━━\nTrendin geri çekileceği olası seviyeleri gösteren yatay çizgiler.\n\n📌 *Kritik Seviyeler:*\n• %23.6 — Hafif geri çekilme\n• %38.2 — Orta düzey\n• %50.0 — Psikolojik yarı\n• %61.8 — 🏆 Altın oran (en kritik)\n• %78.6 — Derin geri çekilme\n\n📐 Kullanmak için: `/fib BTCUSDT`",
    "whale": "📘 *Whale (Balina)*\n━━━━━━━━━━━━━━━━━━━━━\nPiyasayı etkileyebilecek büyük miktarda kripto tutan varlık.\n\n📌 *Önemi:*\n• Borsa girişi → Satış baskısı sinyali\n• Borsa çıkışı → Uzun vade tutma sinyali\n• On-chain veriden takip edilir",
    "funding": "📘 *Funding Rate*\n━━━━━━━━━━━━━━━━━━━━━\nPerpetual futures'ta long/short arasında 8 saatte bir ödenen ücret.\n\n📌 *Yorumu:*\n• Pozitif → 🔴 Long'lar öder, aşırı iyimserlik\n• Negatif → 🟢 Short'lar öder, aşırı kötümserlik\n• Yüksek pozitif funding → Düzeltme riski",
    "liquidation": "📘 *Likidaasyon*\n━━━━━━━━━━━━━━━━━━━━━\nKaldıraçlı işlemde teminat yetersiz kalınca pozisyonun zorla kapatılması.\n\n📌 *Örnek:*\n• 10x long, fiyat %10 düşerse → Likide edilir\n• Büyük likidasyonlar ani fiyat düşüşü yaratır",
    "dca": "📘 *DCA — Dollar Cost Averaging*\n━━━━━━━━━━━━━━━━━━━━━\nSabit aralıklarla sabit miktarda yatırım yapma stratejisi.\n\n📌 *Avantajları:*\n• Zamanlama riskini azaltır\n• Düşüşlerde daha fazla coin alınır\n• Duygusal kararları engeller",
    "dominans": "📘 *BTC Dominansı*\n━━━━━━━━━━━━━━━━━━━━━\nBitcoin'in toplam kripto market cap içindeki yüzde payı.\n\n📌 *Yorumu:*\n• Dominans yükseliyor → 🟠 Altcoinler zayıf\n• Dominans düşüyor → 🟢 Altcoin sezonu olabilir\n• %40 altı → Güçlü altseason sinyali",
    "altseason": "📘 *Altseason*\n━━━━━━━━━━━━━━━━━━━━━\nBTC dominansının düştüğü, altcoinlerin BTC'den iyi performans gösterdiği dönem.\n\n📌 *İşaretleri:*\n• BTC dominansı %40 altına iner\n• Küçük cap coinler hızla yükselir\n• Yüksek market geneli hacim",
    "support": "📘 *Destek (Support)*\n━━━━━━━━━━━━━━━━━━━━━\nFiyatın düşerken duraksadığı veya geri döndüğü bölge.\n\n📌 *Kurallar:*\n• Destek kırılırsa yeni destek arar\n• Tutunursa → 🟢 Alım fırsatı olabilir\n• Kırılan eski destek → Yeni direnç olur",
    "resistance": "📘 *Direnç (Resistance)*\n━━━━━━━━━━━━━━━━━━━━━\nFiyatın yükselirken zorlandığı veya geri döndüğü bölge.\n\n📌 *Kurallar:*\n• Direnç kırılırsa → 🟢 Yeni hedef arar\n• Tekrar test güçlendirir\n• Kırılan eski direnç → Yeni destek",
    "marketcap": "📘 *Piyasa Değeri (Market Cap)*\n━━━━━━━━━━━━━━━━━━━━━\nDolaşımdaki coin × fiyat formülüyle hesaplanır.\n\n📌 *Kategoriler:*\n• Large Cap → +10B$\n• Mid Cap → 1-10B$\n• Small Cap → 100M-1B$\n• Micro Cap → -100M$\n\n⚠️ Düşük mcap = Yüksek manipülasyon riski",
    "stoploss": "📘 *Stop Loss*\n━━━━━━━━━━━━━━━━━━━━━\nBelirli fiyata ulaşınca pozisyonu kapatarak zararı sınırlayan emir.\n\n📌 *Kullanım:*\n• 100$ aldıysan 90$'a stop koy → Maks %10 kayıp\n• Trailing stop → Fiyat yükselirken stop da yükselir",
    "fomc": "📘 *FOMC — Federal Open Market Committee*\n━━━━━━━━━━━━━━━━━━━━━\nABD Merkez Bankası (Fed) para politikası kurulu. Yılda 8 kez toplanır.\n\n📌 *Kripto Etkisi:*\n• Faiz artırımı → 🔴 Risk varlıkları düşer\n• Faiz indirimi → 🟢 Risk iştahı artar\n• Beklentiden sürpriz → Yüksek volatilite",
    "cpi": "📘 *CPI — Consumer Price Index*\n━━━━━━━━━━━━━━━━━━━━━\nTüketici fiyat endeksi. Her ay ABD İstatistik Bürosu yayınlar.\n\n📌 *Kripto Etkisi:*\n• Yüksek CPI → Fed şahinleşir → 🔴 Risk varlıkları baskı\n• Düşük CPI → Fed güvercin → 🟢 Risk iştahı artar",
    "halving": "📘 *Bitcoin Halving*\n━━━━━━━━━━━━━━━━━━━━━\nYaklaşık 4 yılda bir BTC madenci ödülünü yarıya indiren event.\n\n📌 *Önemi:*\n• Arz azalır → Tarihsel olarak yükseliş dönemleriyle örtüşür\n• 2024 Halving: Nisan 2024\n• Bir sonraki: ~2028",
}

SOZLUK_ALIAS = {
    "bb": "bollinger", "boll": "bollinger", "fib": "fibonacci", "fibo": "fibonacci",
    "destek": "support", "direnc": "resistance", "direnç": "resistance",
    "sl": "stoploss", "stop": "stoploss", "mcap": "marketcap",
    "alt": "altseason", "dom": "dominans", "liq": "liquidation",
    "likidaasyon": "liquidation", "fed": "fomc", "enflasyon": "cpi",
}

async def ne_command(update: Update, context):
    await register_user(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        if update.message:
            try: await update.message.delete()
            except Exception: pass
        if not await is_group_admin(context.bot, chat.id, update.effective_user.id):
            try:
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"🔒 *Terim Sözlüğü* özelliğini kullanmak için buraya tıklayın 👇\nBotu DM üzerinden kullanabilirsiniz.",
                    parse_mode="Markdown"
                )
            except Exception: pass
            try:
                tip = await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"🔒 Terim Sözlüğü için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
                )
                asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
            except Exception: pass
            return
    args = context.args or []

    if not args:
        terimler = " • ".join(f"`{k}`" for k in sorted(SOZLUK.keys()))
        await send_temp(context.bot, chat.id,
            f"📚 *Kripto Terim Sozlugu*\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"Bir terim yazarak aciklamasini gorebilirsin.\n"
            f"Ornek: `/ne MACD`\n\n"
            f"📖 *Mevcut Terimler:*\n{terimler}",
            parse_mode="Markdown")
        return

    arama = " ".join(args).lower().strip()
    arama = SOZLUK_ALIAS.get(arama, arama)

    if arama in SOZLUK:
        text = SOZLUK[arama]
    else:
        eslesme = [k for k in SOZLUK if arama in k or k in arama]
        if eslesme:
            text = SOZLUK[eslesme[0]]
        else:
            terimler = " • ".join(f"`{k}`" for k in sorted(SOZLUK.keys()))
            text = (
                f"❓ `{arama}` bulunamadı.\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"📚 *Mevcut Terimler:*\n{terimler}"
            )

    is_group = chat.type in ("group", "supergroup")
    delay    = (await get_member_delete_delay()) if is_group else None
    # İlgili terimler için hızlı butonlar (en fazla 4 rastgele)
    import random
    diger = [k for k in SOZLUK if k != arama][:8]
    random.shuffle(diger)
    ilgili = diger[:4]
    kb_ne_rows = [[InlineKeyboardButton(f"📖 {k}", callback_data=f"ne_{k}") for k in ilgili[:2]]]
    if len(ilgili) > 2:
        kb_ne_rows.append([InlineKeyboardButton(f"📖 {k}", callback_data=f"ne_{k}") for k in ilgili[2:4]])
    kb_ne = InlineKeyboardMarkup(kb_ne_rows)
    msg = await context.bot.send_message(chat.id, text, parse_mode="Markdown", reply_markup=kb_ne)
    if is_group and delay:
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))

# ================= EKONOMİK TAKVİM =================

async def _fetch_te_rss() -> list:
    """
    TradingEconomics RSS feed'inden ekonomik takvim olaylarını çeker.
    Kayıt gerektirmez — tamamen ücretsiz.
    """
    import xml.etree.ElementTree as ET
    events = []
    # TE'nin kamuya açık RSS feed'leri
    feeds = [
        ("https://tradingeconomics.com/rss/news.aspx?i=united+states", "ABD Makro"),
        ("https://tradingeconomics.com/rss/news.aspx?i=euro+area",     "Euro Bölgesi"),
    ]
    # Kripto haberleri için ek kaynak (CryptoPanic public RSS — API key gerektirmez)
    crypto_feeds = [
        ("https://cryptopanic.com/news/rss/",                         "Kripto"),
        ("https://www.coindesk.com/arc/outboundfeeds/rss/",           "CoinDesk"),
    ]
    all_feeds = feeds + crypto_feeds

    # TE'de önem derecesini belirleyen anahtar kelimeler
    HIGH_IMP = ["FOMC", "Fed", "interest rate", "faiz", "CPI", "inflation",
                "NFP", "nonfarm", "PCE", "GDP", "ECB", "Bank of England",
                "halving", "SEC", "ETF approval", "rate decision"]
    MED_IMP  = ["PMI", "retail sales", "unemployment", "jobless",
                "trade balance", "housing", "consumer confidence"]

    now = datetime.utcnow()

    async with aiohttp.ClientSession() as session:
        for url, source in all_feeds:
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; KriptoBot/1.0)"},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()

                root = ET.fromstring(text)
                channel = root.find("channel")
                if channel is None:
                    continue

                for item in (channel.findall("item") or [])[:15]:
                    title_el = item.find("title")
                    date_el  = item.find("pubDate")
                    desc_el  = item.find("description")
                    if title_el is None:
                        continue

                    title = (title_el.text or "").strip()
                    desc  = ""
                    if desc_el is not None:
                        import re
                        desc = re.sub(r"<[^>]+>", "", desc_el.text or "").strip()[:120]

                    # Tarih parse
                    ev_date = now.strftime("%Y-%m-%d")
                    if date_el is not None and date_el.text:
                        try:
                            from email.utils import parsedate_to_datetime
                            dt = parsedate_to_datetime(date_el.text)
                            ev_date = dt.strftime("%Y-%m-%d")
                        except Exception:
                            pass

                    # Sadece gelecekteki veya bugünkü olaylar
                    try:
                        ev_dt = datetime.strptime(ev_date, "%Y-%m-%d")
                        if (ev_dt.date() - now.date()).days < -1:
                            continue
                    except Exception:
                        continue

                    # Önem derecesi
                    title_upper = title.upper()
                    importance  = 40
                    for kw in HIGH_IMP:
                        if kw.upper() in title_upper:
                            importance = 90
                            break
                    if importance < 90:
                        for kw in MED_IMP:
                            if kw.upper() in title_upper:
                                importance = 60
                                break

                    # Kriptoya etkisi olan haberleri filtrele
                    crypto_kw = ["bitcoin","btc","crypto","fed","fomc","cpi","inflation",
                                 "rate","sec","etf","halving","blockchain","ethereum","eth"]
                    is_relevant = any(kw in title.lower() for kw in crypto_kw) or importance >= 80

                    if not is_relevant and source not in ("Kripto", "CoinDesk"):
                        continue

                    # Emoji + kategori
                    if "FOMC" in title_upper or "Fed" in title or "rate" in title.lower():
                        prefix, coins = "🏦", "BTC, ETH, Tüm Piyasa"
                    elif "CPI" in title_upper or "inflation" in title.lower():
                        prefix, coins = "📊", "BTC, ETH, Tüm Piyasa"
                    elif "NFP" in title_upper or "nonfarm" in title.lower() or "jobs" in title.lower():
                        prefix, coins = "💼", "BTC, ETH, Tüm Piyasa"
                    elif "bitcoin" in title.lower() or "btc" in title.lower():
                        prefix, coins = "₿", "BTC"
                    elif "ethereum" in title.lower() or "eth" in title.lower():
                        prefix, coins = "Ξ", "ETH"
                    elif "sec" in title.lower() or "etf" in title.lower():
                        prefix, coins = "⚖️", "BTC, ETH"
                    else:
                        prefix, coins = "📌", "Kripto Piyasa"

                    events.append({
                        "title":      f"{prefix} {title}",
                        "date":       ev_date,
                        "coins":      coins,
                        "importance": importance,
                        "desc":       desc[:100] if desc else "",
                        "source":     source,
                    })
            except Exception as e:
                log.warning(f"RSS feed hatasi ({source}): {e}")
                continue

    return events

async def translate_calendar_events(events: list) -> list:
    """
    RSS'ten gelen İngilizce takvim başlıklarını VE açıklamalarını Türkçe'ye çevirir.
    Statik (zaten Türkçe) olaylar atlanır.
    """
    if not GROQ_API_KEY or not events:
        return events
    # Sadece RSS kaynaklı (İngilizce) olayları çevir
    to_translate = [e for e in events if e.get("source") not in ("Makro Takvim",)]
    if not to_translate:
        return events
    try:
        # Başlık + açıklamaları tek seferde çevir
        # Format: "T: başlık\nD: açıklama" her olay için ayrı blok
        blocks = []
        for e in to_translate:
            t = e.get("title", "")
            d = e.get("desc", "")
            blocks.append(f"T: {t}\nD: {d}" if d else f"T: {t}\nD: -")
        combined = "\n---\n".join(blocks)
        prompt = (
            "Translate the following economic/crypto news titles (T:) and descriptions (D:) to Turkish. "
            "Keep the exact format: T: and D: prefixes, --- separators. "
            "If description is '-', keep it as '-'. "
            "Output ONLY the translated blocks, nothing else.\n\n"
            + combined
        )
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500, "temperature": 0.1
                },
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    result_blocks = [b.strip() for b in raw.split("---") if b.strip()]
                    for i, ev in enumerate(to_translate):
                        if i >= len(result_blocks):
                            break
                        block = result_blocks[i]
                        for line in block.split("\n"):
                            line = line.strip()
                            if line.startswith("T:"):
                                ev["title"] = line[2:].strip()
                            elif line.startswith("D:"):
                                desc_tr = line[2:].strip()
                                if desc_tr and desc_tr != "-":
                                    ev["desc"] = desc_tr
                    log.info(f"Takvim cevirisi OK: {len(to_translate)} olay (baslik+aciklama)")
                else:
                    log.warning(f"Takvim cevirisi Groq hata: {r.status}")
    except Exception as e:
        log.warning(f"Takvim çevirisi başarısız: {e}")
    return events

async def fetch_crypto_calendar() -> list:
    """
    Ekonomik takvim verilerini toplar:
    1. TradingEconomics + CryptoPanic RSS (kayıtsız, ücretsiz)
    2. Statik makro takvim (FOMC, CPI, NFP, PCE — her zaman gösterilir)
    Sonuçları birleştirir, sıralar ve tekilleştirir.
    """
    now    = datetime.utcnow()
    events = []

    # 1. RSS kaynaklarından canlı veriler
    try:
        rss_events = await asyncio.wait_for(_fetch_te_rss(), timeout=12)
        events.extend(rss_events)
        log.info(f"RSS takvim: {len(rss_events)} olay alındı")
    except asyncio.TimeoutError:
        log.warning("RSS takvim timeout")
    except Exception as e:
        log.warning(f"RSS takvim genel hata: {e}")

    # 2. Statik makro takvim — Her zaman eklenir (RSS'te yoksa)
    y, m = now.year, now.month
    static = [
        {"title": "🏦 FOMC Toplantısı — Fed Faiz Kararı", "day": 18, "importance": 95,
         "desc": "ABD Merkez Bankası faiz kararı. Kripto piyasaları için en kritik makro olay.",
         "coins": "BTC, ETH, Tüm Piyasa"},
        {"title": "📊 ABD CPI Enflasyon Verisi", "day": 12, "importance": 90,
         "desc": "Yüksek CPI → Fed şahinleşir → Risk varlıkları baskı altında kalır.",
         "coins": "BTC, ETH, Tüm Piyasa"},
        {"title": "💼 ABD NFP İstihdam Raporu", "day": 7, "importance": 80,
         "desc": "Güçlü rapor → Dolar güçlenir → Kripto kısa vadeli baskı görebilir.",
         "coins": "BTC, ETH, Tüm Piyasa"},
        {"title": "📈 ABD PCE Fiyat Endeksi", "day": 28, "importance": 85,
         "desc": "Fed'in tercih ettiği enflasyon göstergesi. FOMC öncesi en kritik veri.",
         "coins": "BTC, ETH, Tüm Piyasa"},
    ]

    # RSS'ten gelen başlıklar (tekilleştirme için)
    existing_titles = {e["title"].lower()[:30] for e in events}

    for ev in static:
        try:
            ev_dt = datetime(y, m, ev["day"])
            if ev_dt < now:
                if m == 12:
                    ev_dt = datetime(y + 1, 1, ev["day"])
                else:
                    ev_dt = datetime(y, m + 1, ev["day"])

            # RSS'te zaten benzer başlık varsa ekleme
            short_title = ev["title"].lower()[:30]
            if short_title not in existing_titles:
                events.append({
                    "title":      ev["title"],
                    "date":       ev_dt.strftime("%Y-%m-%d"),
                    "coins":      ev["coins"],
                    "importance": ev["importance"],
                    "desc":       ev["desc"],
                    "source":     "Makro Takvim",
                })
        except Exception:
            pass

    # Sırala: önce yakın tarih, sonra önem derecesi
    events.sort(key=lambda x: (x["date"], -x.get("importance", 0)))

    # Tekilleştir (aynı günde çok benzer başlıklar)
    seen, unique = set(), []
    for ev in events:
        key = f"{ev['date']}_{ev['title'][:25].lower()}"
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    # RSS'ten gelen İngilizce başlıkları Türkçe'ye çevir
    unique = await translate_calendar_events(unique)

    return unique[:20]

async def takvim_command(update: Update, context):
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")

    # Grupta üyeler için DM yönlendirme
    if is_group:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and not await is_group_admin(context.bot, chat.id, user_id):
            if update.message:
                try: await context.bot.delete_message(chat.id, update.message.message_id)
                except Exception: pass
            _murl = get_miniapp_url()
            dm_keyboard_rows = []
            if _murl:
                dm_keyboard_rows.append([InlineKeyboardButton("🖥 Dashboard Mini App", web_app=WebAppInfo(url=_murl))])
            dm_keyboard_rows.append([InlineKeyboardButton("📅 Ekonomik Takvim (DM)", callback_data="takvim_refresh")])
            dm_keyboard_rows.append([InlineKeyboardButton("🤖 Bota DM'den Başla", url=f"https://t.me/{BOT_USERNAME}?start=takvim")])
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "📅 *Ekonomik Takvim*\n━━━━━━━━━━━━━━━━━━\n"
                        "Bu ozellik DM uzerinden daha rahat kullanilir.\n\n"
                        "Asagidaki butonlarla takvimi acabilir veya Dashboard Mini App'e gecebilirsin."
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(dm_keyboard_rows)
                )
            except Exception: pass
            try:
                tip = await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"📅 Ekonomik Takvim için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
                )
                asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
            except Exception: pass
            return

    await register_user(update)
    loading = await send_temp(context.bot, chat.id, "📅 Yaklasan ekonomik takvim verileri yukleniyor...", parse_mode="Markdown")
    events  = await fetch_crypto_calendar()
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    now = datetime.utcnow()
    text = "📅 *Ekonomik ve Kripto Takvimi*\n━━━━━━━━━━━━━━━━━━━━━\n"
    for ev in events[:8]:
        try:
            ev_dt  = datetime.strptime(ev["date"], "%Y-%m-%d")
            diff   = (ev_dt.date() - now.date()).days
            if diff == 0:
                zamanl = "⚡ *BUGÜN*"
            elif diff == 1:
                zamanl = "🔜 *Yarın*"
            elif diff < 0:
                zamanl = f"📌 _{abs(diff)}g önce_"
            elif diff < 7:
                zamanl = f"📆 *{diff}g sonra*"
            else:
                zamanl = f"📆 {ev['date']}"
            imp     = ev.get("importance", 0)
            imp_str = "🔴" if imp >= 80 else ("🟡" if imp >= 50 else "🟢")
            coins   = f"\n🪙 _{ev['coins']}_" if ev.get("coins") else ""
            desc    = f"\n💬 _{ev['desc']}_" if ev.get("desc") else ""
            text   += f"\n{imp_str} {zamanl}\n📌 *{ev['title']}*{coins}{desc}\n"
        except Exception:
            pass
    text += f"\n━━━━━━━━━━━━━━━━━━━━━\n🔴 Yüksek  •  🟡 Orta  •  🟢 Düşük etki\n🕒 _Güncelleme: {now.strftime('%d.%m.%Y %H:%M')} UTC_"

    is_group = chat.type in ("group", "supergroup")
    delay    = (await get_member_delete_delay()) if is_group else None
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Bildirim Aç/Kapat", callback_data="takvim_toggle"),
        InlineKeyboardButton("🔄 Yenile",             callback_data="takvim_refresh"),
    ]])
    msg = await context.bot.send_message(chat.id, text, parse_mode="Markdown", reply_markup=keyboard)
    if is_group and delay:
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))

async def takvim_job(context):
    try:
        events     = await fetch_crypto_calendar()
        bugun      = datetime.utcnow().strftime("%Y-%m-%d")
        bugun_evs  = [e for e in events if e["date"]==bugun and e.get("importance",0)>=70]
        if not bugun_evs: return
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM takvim_subscribers WHERE active=1")
        text = "📅 *BUGÜNKÜ ÖNEMLİ EKONOMİK OLAYLAR*\n━━━━━━━━━━━━━━━━━━━━━\n"
        for ev in bugun_evs:
            imp     = ev.get("importance", 0)
            imp_str = "🔴" if imp >= 80 else ("🟡" if imp >= 50 else "🟢")
            text += f"\n{imp_str} 📌 *{ev['title']}*\n"
            if ev.get("desc"):
                text += f"💬 _{ev['desc']}_\n"
        text += "\n━━━━━━━━━━━━━━━━━━━━━\n💡 _Kapatmak için /takvim → 'Bildirim Kapat'_"
        for row in rows:
            try: await context.bot.send_message(row["user_id"], text, parse_mode="Markdown")
            except Exception: pass
    except Exception as e:
        log.warning(f"takvim_job hata: {e}")

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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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
    except Exception:
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

        market_ctx = ""
        if mood is not None and btc_dom is not None and mkt_avg is not None:
            market_ctx = f"🌍 *Piyasa Nabzi:* {mood}  •  BTC Dominansi `{btc_dom}%`  •  Ort. `{mkt_avg:+.2f}%`\n━━━━━━━━━━━━━━━━━━\n"

        text = header + (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 `{symbol}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 *Anlık Fiyat:* `{format_price(price)} USDT`\n"
            f"{rank_line}"
            f"{vol_anom}"
            f"{market_ctx}"
            f"📈 *Performans*\n"
            f"{e5} `5dk  `  `{s5}{ch5m:+.2f}%`\n"
            f"{e1} `1sa  `  `{s1}{ch1h:+.2f}%`\n"
            f"{e4} `4sa  `  `{s4}{ch4h:+.2f}%`\n"
            f"{e24} `24sa `  `{s24}{ch24:+.2f}%`\n"
            f"{e7} `7gun `  `{s7}{ch7d:+.2f}%`\n"
            f"{e30} `30gun`  `{s30}{ch30d:+.2f}%`\n\n"
            f"📊 *RSI Ozeti*\n"
            f"• 4sa RSI 14  : `{rsi14_4h}` — {rsi_label(rsi14_4h)}\n"
            f"• 1gun RSI 14 : `{rsi14_1d}` — {rsi_label(rsi14_1d)}\n"
        )
        if div_line:
            text += f"{div_line}\n"
        else:
            text += "\n"
        text += (
            f"📌 *Piyasa Skoru*\n"
            f"⏱ Saatlik  : `{sh}/100` — _{lh}_\n"
            f"📅 Gunluk   : `{sd}/100` — _{ld}_\n"
            f"🗓 Haftalik : `{sw}/100` — _{lw}_\n"
            f"──────────────────"
        )
        if threshold_info:
            text += f"\n🔔 *Alarm Eşiği:* `%{threshold_info}`"

        # DM'e gönderimde mesajları silme, sadece grup kanallarında sil
        is_group_chat = False
        try:
            chat_obj = await bot.get_chat(chat_id)
            is_group_chat = chat_obj.type in ("group", "supergroup", "channel")
        except Exception:
            pass

        if is_group_chat:
            # Grupta sadece Binance butonu
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📈 Binance'de Görüntüle",
                        url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
                    )
                ]
            ])
        else:
            # DM'de tüm butonlar
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📐 Fibonacci", callback_data=f"fib_{symbol}_4h"),
                    InlineKeyboardButton("🧠 Sentiment", callback_data=f"sent_{symbol}"),
                ],
                [
                    InlineKeyboardButton(
                        "📈 Binance'de Görüntüle",
                        url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
                    )
                ]
            ])

        msg = await bot.send_message(chat_id=chat_id, text=text,
                                     reply_markup=keyboard, parse_mode="Markdown")

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

# Grup üyelerinin komut olarak kullanabileceği özellikler
# (buton tıklamalarından bağımsız — command handler seviyesinde)
GROUP_ALLOWED_CMDS = {"start", "top5", "top24", "top5", "market", "status", "mtf"}

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
            text=f"🔒 {fname} için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
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

def is_bot_admin(user_id: int) -> bool:
    """Kullanıcı botun sahibi (ADMIN_ID) mi?"""
    return ADMIN_ID != 0 and user_id == ADMIN_ID

async def register_user(update: Update):
    """Her komutta kullanıcıyı bot_users tablosuna kaydet / güncelle."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return
    try:
        chat_type = chat.type if chat else "private"
        full_name = ((user.first_name or "") + " " + (user.last_name or "")).strip()
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_users (user_id, username, full_name, first_seen, last_active, command_count, chat_type)
                VALUES ($1, $2, $3, NOW(), NOW(), 1, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET username     = EXCLUDED.username,
                    full_name    = EXCLUDED.full_name,
                    last_active  = NOW(),
                    command_count = bot_users.command_count + 1,
                    chat_type    = EXCLUDED.chat_type
            """, user.id, user.username, full_name, chat_type)
    except Exception as e:
        log.warning(f"register_user hata: {e}")

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
    chat    = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None

    # Grupta /set yazılırsa komutu sil ve sessizce geç
    if chat and chat.type in ("group", "supergroup"):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    # Private chat: sadece bot sahibi veya grup admini erişebilir
    if not is_bot_admin(user_id):
        try:
            member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text(
                    "🚫 *Bu panel sadece grup adminlerine açıktır.*",
                    parse_mode="Markdown"
                )
                return
        except Exception as e:
            log.warning(f"set_command admin kontrol: {e}")
            await update.message.reply_text("⚠️ Yetki kontrol edilemedi.", parse_mode="Markdown")
            return

    text, keyboard = await build_set_panel(context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def set_callback(update: Update, context):
    q = update.callback_query
    # Bot sahibi her zaman erişebilir
    if not is_bot_admin(q.from_user.id):
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
        except Exception: pass
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

    await register_user(update)   # kullanıcıyı kaydet/güncelle

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
    await register_user(update)
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
            "🔔 *Kişisel Alarm Paneli*\n━━━━━━━━━━━━━━━━━━\n"
            "Henuz alarm yok.\n\n"
            "Alarm turleri:\n"
            "• `%`    : `/alarm_ekle BTCUSDT 3.5`\n"
            "• Fiyat  : `/alarm_ekle BTCUSDT fiyat 70000`\n"
            "• RSI    : `/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "• Bant   : `/alarm_ekle BTCUSDT bant 60000 70000`\n\n"
            "💡 Fiyat alarmı için `/hedef BTCUSDT 70000` da kullanabilirsiniz."
        )
    else:
        text = "🔔 *Kişisel Alarmlarınız*\n━━━━━━━━━━━━━━━━━━\n"
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
    await register_user(update)
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    args     = context.args or []

    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "🔔 *Alarm Ekle — Kullanım*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📊 *Yüzde Alarmı* _(5dk harekette)_\n"
            "`/alarm_ekle BTCUSDT 3.5`\n\n"
            "🎯 *Fiyat Alarmı* _(hedefe ulaşınca)_\n"
            "`/alarm_ekle BTCUSDT fiyat 70000`\n\n"
            "📈 *RSI Alarmı* _(seviyeye girince)_\n"
            "`/alarm_ekle BTCUSDT rsi 30 asagi`\n"
            "`/alarm_ekle BTCUSDT rsi 70 yukari`\n\n"
            "📦 *Bant Alarmı* _(bant dışına çıkınca)_\n"
            "`/alarm_ekle BTCUSDT bant 60000 70000`\n\n"
            "💡 _Alarmlarınız: /alarmim_",
            parse_mode="Markdown"
        )
        return

    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    # ── FİYAT ALARMI (/alarm_ekle BTCUSDT fiyat 70000) ──────────────────
    if args[1].lower() in ("fiyat", "price", "hedef"):
        if len(args) < 3:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: `/alarm_ekle BTCUSDT fiyat 70000`", parse_mode="Markdown"); return
        try:
            target_price = float(args[2].replace(",","."))
        except Exception:
            await send_temp(context.bot, update.effective_chat.id, "Fiyat değeri sayı olmalı.", parse_mode="Markdown"); return

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
                "Kullanım: `/alarm_ekle BTCUSDT rsi 30 asagi`", parse_mode="Markdown"); return
        try:    rsi_lvl = float(args[2])
        except Exception:
            await send_temp(context.bot, update.effective_chat.id, "RSI değeri sayı olmalı.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,rsi_level,active)
                VALUES($1,$2,$3,0,'rsi',$4,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='rsi', rsi_level=$4, threshold=0, active=1
            """, user_id, username, symbol, rsi_lvl)
        direction_str = "asagi" if len(args) < 4 or args[3].lower() in ("asagi","aşağı","down") else "yukari"
        yon_str = "altına düşünce 📉" if direction_str == "asagi" else "üstüne çıkınca 📈"
        # Yön bilgisini DB'ye kaydet (alarm_job'un doğru tetikleyebilmesi için)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_alarms SET rsi_level=$1
                WHERE user_id=$2 AND symbol=$3
            """, rsi_lvl * (-1 if direction_str == "asagi" else 1), user_id, symbol)
        kb_rsi = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm"),
            InlineKeyboardButton("➕ Başka Ekle", callback_data="alarm_guide"),
        ]])
        await send_temp(context.bot, update.effective_chat.id,
            "✅ *" + symbol + "* RSI `" + str(rsi_lvl) + "` " + yon_str + " alarm verilecek!\n"
            "_Yön: " + ("aşağı 📉" if direction_str == "asagi" else "yukarı 📈") + "_",
            parse_mode="Markdown", reply_markup=kb_rsi
        )
        return

    if args[1].lower() == "bant":
        if len(args) < 4:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: `/alarm_ekle BTCUSDT bant 60000 70000`", parse_mode="Markdown"); return
        try:
            band_low  = float(args[2].replace(",","."))
            band_high = float(args[3].replace(",","."))
        except Exception:
            await send_temp(context.bot, update.effective_chat.id, "Fiyat değerleri sayı olmalı.", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,band_low,band_high,active)
                VALUES($1,$2,$3,0,'band',$4,$5,1)
                ON CONFLICT(user_id,symbol) DO UPDATE
                SET alarm_type='band', band_low=$4, band_high=$5, threshold=0, active=1
            """, user_id, username, symbol, band_low, band_high)
        kb_bant = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm"),
            InlineKeyboardButton("➕ Başka Ekle", callback_data="alarm_guide"),
        ]])
        await send_temp(context.bot, update.effective_chat.id,
            "✅ *" + symbol + "* `" + format_price(band_low) + " — " + format_price(band_high) +
            " USDT` bandından çıkınca alarm verilecek!",
            parse_mode="Markdown", reply_markup=kb_bant
        )
        return

    try:    threshold = float(args[1])
    except Exception:
        await send_temp(context.bot, update.effective_chat.id, "Eşik sayı olmalıdır. Örnek: `3.5`", parse_mode="Markdown"); return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_alarms(user_id,username,symbol,threshold,alarm_type,active)
            VALUES($1,$2,$3,$4,'percent',1)
            ON CONFLICT(user_id,symbol) DO UPDATE
            SET threshold=$4, alarm_type='percent', active=1
        """, user_id, username, symbol, threshold)
    kb_ekle = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm"),
        InlineKeyboardButton("➕ Başka Ekle", callback_data="alarm_guide"),
    ]])
    await send_temp(context.bot, update.effective_chat.id,
        "✅ *" + symbol + "* için `%" + str(threshold) + "` alarmı eklendi!",
        parse_mode="Markdown", reply_markup=kb_ekle
    )

async def alarm_sil(update: Update, context):
    if not await check_group_access(update, context, "Alarm Sil"):
        return
    user_id = update.effective_user.id
    if not context.args:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanım: `/alarm_sil BTCUSDT`", parse_mode="Markdown")
        return
    symbol = context.args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_alarms WHERE user_id=$1 AND symbol=$2", user_id, symbol
        )
    kb_sil = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm"),
        InlineKeyboardButton("➕ Alarm Ekle", callback_data="alarm_guide"),
    ]])
    if result == "DELETE 0":
        await send_temp(context.bot, update.effective_chat.id,
            f"⚠️ `{symbol}` için kayıtlı alarm bulunamadı.",
            parse_mode="Markdown", reply_markup=kb_sil)
    else:
        await send_temp(context.bot, update.effective_chat.id,
            f"🗑 `{symbol}` alarmı silindi.",
            parse_mode="Markdown", reply_markup=kb_sil)

async def favori_command(update: Update, context):
    if not await check_group_access(update, context, "Favoriler"):
        return
    await register_user(update)
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
                "Kullanım: `/favori ekle BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO favorites(user_id,symbol) VALUES($1,$2) ON CONFLICT DO NOTHING", user_id, symbol)
        kb_fav = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Favorilerim",     callback_data="fav_liste"),
            InlineKeyboardButton("📊 Analiz Et",       callback_data="fav_analiz"),
        ]])
        await send_temp(context.bot, update.effective_chat.id,
            "⭐ `" + symbol + "` favorilere eklendi!",
            parse_mode="Markdown", reply_markup=kb_fav); return

    if args[0].lower() == "sil":
        if len(args) < 2:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: `/favori sil BTCUSDT`", parse_mode="Markdown"); return
        symbol = args[1].upper().replace("#","").replace("/","")
        if not symbol.endswith("USDT"): symbol += "USDT"
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM favorites WHERE user_id=$1 AND symbol=$2", user_id, symbol)
        kb_fav_sil = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Favorilerim", callback_data="fav_liste"),
        ]])
        await send_temp(context.bot, update.effective_chat.id,
            "🗑 `" + symbol + "` favorilerden silindi.",
            parse_mode="Markdown", reply_markup=kb_fav_sil); return

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
        "Kullanım:\n`/favori ekle BTCUSDT`\n`/favori sil BTCUSDT`\n`/favori liste`\n`/favori analiz`",
        parse_mode="Markdown"
    )

async def alarm_duraklat(update: Update, context):
    if not await check_group_access(update, context, "Alarm Duraklat"):
        return
    user_id = update.effective_user.id
    args    = context.args or []
    if len(args) < 2:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanım: `/alarm_duraklat BTCUSDT 2` (saat)", parse_mode="Markdown"); return
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"
    try:    saat = float(args[1])
    except Exception:
        await send_temp(context.bot, update.effective_chat.id, "Saat sayı olmalı.", parse_mode="Markdown"); return
    until = datetime.utcnow() + timedelta(hours=saat)
    async with db_pool.acquire() as conn:
        r = await conn.execute(
            "UPDATE user_alarms SET paused_until=$1 WHERE user_id=$2 AND symbol=$3",
            until, user_id, symbol
        )
    kb_dur = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Alarmlarım",  callback_data="my_alarm"),
        InlineKeyboardButton("▶️ Devam Ettir", callback_data=f"alarm_unpause_{symbol}"),
    ]])
    if r == "UPDATE 0":
        await send_temp(context.bot, update.effective_chat.id,
            f"⚠️ `{symbol}` için alarm bulunamadı.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm")]]))
    else:
        await send_temp(context.bot, update.effective_chat.id,
            f"⏸ *{symbol}* alarmı `{int(saat)} saat` duraklatıldı.\n"
            f"⏰ Tekrar aktif: `{until.strftime('%H:%M')} UTC`",
            parse_mode="Markdown", reply_markup=kb_dur
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
    kb_gec = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Alarmlarım", callback_data="my_alarm"),
        InlineKeyboardButton("➕ Alarm Ekle", callback_data="alarm_guide"),
    ]])
    if not rows:
        await send_temp(context.bot, update.effective_chat.id,
            "📋 *Alarm Geçmişi*\n━━━━━━━━━━━━━━━━━━\nHenüz tetiklenen alarm yok.",
            parse_mode="Markdown", reply_markup=kb_gec
        )
        return
    text = "📋 *Son 15 Alarm Tetiklenmesi*\n━━━━━━━━━━━━━━━━━━\n"
    for r in rows:
        dt  = r["triggered_at"].strftime("%d.%m %H:%M")
        yon = "📈" if r["direction"] == "up" else "📉"
        if r["alarm_type"] == "rsi":
            detail = "RSI:" + str(round(r["trigger_val"], 1))
        elif r["alarm_type"] == "band":
            detail = "Bant çıkışı"
        else:
            detail = "%" + str(round(r["trigger_val"], 2))
        text += yon + " `" + r["symbol"] + "` " + detail + "  `" + dt + "`\n"
    await send_temp(context.bot, update.effective_chat.id, text,
                    parse_mode="Markdown", reply_markup=kb_gec)


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
    await register_user(update)
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
                "Kullanım:\n`/hedef sil BTCUSDT` — sembol sil\n`/hedef sil hepsi` — tümünü sil",
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
        except Exception:
            pass

    if not hedef_fiyatlar:
        await send_temp(context.bot, update.effective_chat.id,
            "Kullanım: `/hedef BTCUSDT 70000`\n"
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
    await register_user(update)
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
                "Eklemek için:\n`/kar BTCUSDT 0.5 60000` — miktar alış_fiyatı\n"
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
                "Kullanım: `/kar sil BTCUSDT`", parse_mode="Markdown"); return
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
        except Exception:
            await send_temp(context.bot, update.effective_chat.id,
                "Kullanım: `/kar BTCUSDT 0.5 60000`", parse_mode="Markdown"); return

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
            "Kullanım: `/mtf BTCUSDT`\n"
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
            except Exception: pass
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
        except Exception: pass
        log.error("MTF hatası: " + str(e))
        await send_temp(context.bot, update.effective_chat.id, "⚠️ Analiz sırasında hata oluştu.", parse_mode="Markdown")


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
                "📈 Hacim Artışı: `+" + ("%.0f" % pct) + "%`\n"
                "🔄 24s: `" + ("%+.2f" % ch24) + "%`\n"
                "_Büyük oyuncu hareketi!_"
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
        mood    = "🐂 Boğa" if avg > 1 else "🐻 Ayı" if avg < -1 else "😐 Yatay"
        now_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y")

        text = (
            "📅 *Haftalik Kripto Ozeti*\n━━━━━━━━━━━━━━━━━━\n"
            "🗓 " + now_str + "  •  " + mood + "\n"
            "📊 Ortalama Degisim: `" + ("%+.2f" % avg) + "%`\n\n"
            "🚀 *Haftanin Yukselenleri*\n"
        )
        for i, c in enumerate(top5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🟢 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n📉 *Haftanin Düşenleri*\n"
        for i, c in enumerate(bot5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🔴 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n✨ _Iyi haftalar ve bol kazanc!_"
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
        except Exception:
            await send_temp(context.bot, update.effective_chat.id, "Saat formatı: `09:00`", parse_mode="Markdown"); return
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
        except Exception:
            await send_temp(context.bot, update.effective_chat.id, "Saat formatı: `08:00`", parse_mode="Markdown"); return
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO scheduled_tasks(user_id,chat_id,task_type,symbol,hour,minute,active)
                VALUES($1,$2,'rapor','',$3,$4,1)
                ON CONFLICT(chat_id,task_type,symbol) DO UPDATE SET hour=$3,minute=$4,active=1
            """, user_id, chat_id, h, m)
        await send_temp(context.bot, update.effective_chat.id,
            "⏰ Her Pazartesi `" + ("%02d:%02d" % (h,m)) + "` UTC'de haftalik rapor gonderilecek!",
            parse_mode="Markdown"); return

    kb_zaman = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Listemi Gör",  callback_data="zamanla_help"),
        InlineKeyboardButton("🗑 Görevi Sil",    callback_data="zamanla_help"),
    ]])
    await send_temp(context.bot, update.effective_chat.id,
        "Kullanım:\n`/zamanla analiz BTCUSDT 09:00`\n`/zamanla rapor 08:00`\n"
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

    await register_user(update)

    # ── DM butonları (tam menü) ──
    _murl = get_miniapp_url()
    dm_buttons = []

    # Mini App butonu en üstte (URL varsa)
    if _murl:
        dm_buttons.append([InlineKeyboardButton(
            "🖥 Dashboard Mini App", web_app=WebAppInfo(url=_murl)
        )])

    dm_buttons += [
        [InlineKeyboardButton("📊 Market",        callback_data="market"),
         InlineKeyboardButton("⚡ 5dk Flashlar",  callback_data="top5")],
        [InlineKeyboardButton("📈 24s Liderleri", callback_data="top24"),
         InlineKeyboardButton("⚙️ Durum",         callback_data="status")],
        [InlineKeyboardButton("🔔 Alarmlarım",    callback_data="my_alarm"),
         InlineKeyboardButton("⭐ Favorilerim",   callback_data="fav_liste")],
        [InlineKeyboardButton("📉 MTF Analiz",    callback_data="mtf_help"),
         InlineKeyboardButton("📅 Zamanla",       callback_data="zamanla_help")],
        [InlineKeyboardButton("🎯 Fiyat Hedefi",  callback_data="hedef_liste"),
         InlineKeyboardButton("💰 Kar/Zarar",     callback_data="kar_help")],
        [InlineKeyboardButton("📐 Fibonacci",      callback_data="fib_help"),
         InlineKeyboardButton("🧠 Sentiment",      callback_data="sent_help")],
        [InlineKeyboardButton("📅 Ekonomik Takvim",callback_data="takvim_refresh"),
         InlineKeyboardButton("📚 Terim Sözlüğü", callback_data="ne_help")],
        [InlineKeyboardButton("💬 Gruba Katıl",   url="https://t.me/kriptodroptr"),
         InlineKeyboardButton("📢 Kanala Katıl",  url="https://t.me/kriptodropduyuru")],
    ]

    # Admin / Bot sahibi DM butonları
    if not in_group and user_id:
        if is_bot_admin(user_id):
            dm_buttons.append([InlineKeyboardButton("🛠 Admin Ayarları", callback_data="set_open"),
                                InlineKeyboardButton("📊 İstatistikler",  callback_data="stat_refresh")])
        else:
            try:
                member = await context.bot.get_chat_member(GROUP_CHAT_ID, user_id)
                if member.status in ("administrator", "creator"):
                    dm_buttons.append([InlineKeyboardButton("🛠 Admin Ayarları", callback_data="set_open")])
            except Exception:
                pass

    if in_group:
        # Grup için DM ile aynı butonlar (Mini App hariç callback ile)
        group_full_buttons = []

        # Mini App butonu: grupta web_app desteklenmez, callback ile DM'e yönlendir
        _murl_group = get_miniapp_url()
        if _murl_group:
            group_full_buttons.append([InlineKeyboardButton(
                "🖥 Dashboard Mini App", callback_data="miniapp_dm"
            )])

        group_full_buttons += [
            [InlineKeyboardButton("📊 Market",        callback_data="market"),
             InlineKeyboardButton("⚡ 5dk Flashlar",  callback_data="top5")],
            [InlineKeyboardButton("📈 24s Liderleri", callback_data="top24"),
             InlineKeyboardButton("⚙️ Durum",         callback_data="status")],
            [InlineKeyboardButton("🔔 Alarmlarım",    callback_data="my_alarm"),
             InlineKeyboardButton("⭐ Favorilerim",   callback_data="fav_liste")],
            [InlineKeyboardButton("📉 MTF Analiz",    callback_data="mtf_help"),
             InlineKeyboardButton("📅 Zamanla",       callback_data="zamanla_help")],
            [InlineKeyboardButton("🎯 Fiyat Hedefi",  callback_data="hedef_liste"),
             InlineKeyboardButton("💰 Kar/Zarar",     callback_data="kar_help")],
            [InlineKeyboardButton("📐 Fibonacci",      callback_data="fib_help"),
             InlineKeyboardButton("🧠 Sentiment",      callback_data="sent_help")],
            [InlineKeyboardButton("📅 Ekonomik Takvim",callback_data="takvim_refresh"),
             InlineKeyboardButton("📚 Terim Sözlüğü", callback_data="ne_help")],
            [InlineKeyboardButton("💬 Gruba Katıl",   url="https://t.me/kriptodroptr"),
             InlineKeyboardButton("📢 Kanala Katıl",  url="https://t.me/kriptodropduyuru")],
            [InlineKeyboardButton("📈 Binance'de Görüntüle", url="https://www.binance.com/tr/markets/overview")],
            [InlineKeyboardButton("➡️ Bota DM At (Tüm Özellikler)", url=f"https://t.me/{BOT_USERNAME}?start=hello")],
        ]

        keyboard    = InlineKeyboardMarkup(group_full_buttons)
        welcome_text = (
            "👋 *Kripto Analiz Asistani*\n━━━━━━━━━━━━━━━━━━\n"
            "7/24 piyasayi izliyor, hizli analizler uretiyorum.\n\n"
            "💡 *Analiz:* `BTCUSDT` yaz\n"
            "🔔 *Alarm:* `/alarm_ekle BTCUSDT 3.5`\n"
            "🎯 *Hedef:* `/hedef BTCUSDT 70000`\n"
            "📐 *Fibonacci:* `/fib BTCUSDT`\n"
            "🧠 *Sentiment:* `/sentiment BTCUSDT`\n"
            "📅 *Takvim:* `/takvim`\n"
            "💰 *Kar/Zarar:* `/kar BTCUSDT 0.5 60000`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📢 *Toplulugumuza Katil:*\n"
            "💬 [Kripto Drop Grubu](https://t.me/kriptodroptr)\n"
            "📣 [Kripto Drop Duyuru](https://t.me/kriptodropduyuru)"
        )
        # /start komutunu gruptan sil (update.message bazen None olabilir)
        if update.message:
            try:
                await update.message.delete()
            except Exception:
                pass
        msg = await context.bot.send_message(
            chat_id=chat.id, text=welcome_text,
            reply_markup=keyboard, parse_mode="Markdown",
            disable_web_page_preview=True
        )
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))
    else:
        keyboard    = InlineKeyboardMarkup(dm_buttons)
        welcome_text = (
            "👋 *Kripto Analiz Asistani*\n━━━━━━━━━━━━━━━━━━\n"
            "7/24 piyasayi izliyor, tum araclari tek ekranda sunuyorum.\n\n"
            "💡 *Analiz:* `BTCUSDT` yaz\n"
            "🔔 *Alarm:* `/alarm_ekle BTCUSDT 3.5`\n"
            "🎯 *Hedef:* `/hedef BTCUSDT 70000`\n"
            "📐 *Fibonacci:* `/fib BTCUSDT`\n"
            "🧠 *Sentiment:* `/sentiment BTCUSDT`\n"
            "📅 *Takvim:* `/takvim`\n"
            "📚 *Sozluk:* `/ne MACD`\n"
            "💰 *Kar/Zarar:* `/kar BTCUSDT 0.5 60000`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📢 *Toplulugumuza Katil:*\n"
            "💬 [Kripto Drop Grubu](https://t.me/kriptodroptr)\n"
            "📣 [Kripto Drop Duyuru](https://t.me/kriptodropduyuru)"
        )
        await update.message.reply_text(
            welcome_text, reply_markup=keyboard,
            parse_mode="Markdown", disable_web_page_preview=True
        )

async def market(update: Update, context):
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
    usdt = [x for x in data if x["symbol"].endswith("USDT")]
    avg  = sum(float(x["priceChangePercent"]) for x in usdt) / len(usdt)
    status_emoji = "🐂" if avg > 0 else "🐻"
    msg_text = (
        f"{status_emoji} *Piyasa Duyarliligi*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Ortalama degisim: `%{avg:+.2f}`"
    )
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb = bool(update.callback_query)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",     callback_data="market"),
        InlineKeyboardButton("📈 24s Lider",  callback_data="top24"),
        InlineKeyboardButton("⚡ 5dk Flash",  callback_data="top5"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    sent = await target.reply_text(
        msg_text,
        parse_mode="Markdown",
        reply_markup=None if is_group else keyboard,
    )
    if is_group:
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, sent.message_id, delay))
        if not is_cb and update.message:
            asyncio.create_task(auto_delete(context.bot, chat.id, update.message.message_id, 3))

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
    top_gainers = sorted(filtered, key=lambda x: x[1], reverse=True)[:10]
    top_losers  = sorted(filtered, key=lambda x: x[1])[:5]

    text = "🏆 *24 Saatlik Performans Liderleri*\n━━━━━━━━━━━━━━━━━━━━━\n"
    text += "🟢 *Yukselenler*\n"
    for i, (c, pct) in enumerate(top_gainers, 1):
        text += f"{get_number_emoji(i)} 🟢▲ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
    text += "\n🔴 *Dusenler*\n"
    for i, (c, pct) in enumerate(top_losers, 1):
        text += f"{get_number_emoji(i)} 🔴▼ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"

    kb24 = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",    callback_data="top24"),
        InlineKeyboardButton("⚡ 5dk Flash", callback_data="top5"),
        InlineKeyboardButton("📊 Market",    callback_data="market"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    msg = await target.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=None if is_group else kb24,
    )
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

        text = "⚡ *Piyasanin En Hareketlileri*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🕒 _5 dakikalik veri henuz hazir degil, 24s bazli liste gosteriliyor._\n\n"
        text += "🟢 *Yukselenler*\n"
        for i, c in enumerate(positives, 1):
            pct = float(c["priceChangePercent"])
            text += f"{get_number_emoji(i)} 🟢▲ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
        text += "\n🔴 *Dusenler*\n"
        for i, c in enumerate(negatives, 1):
            pct = float(c["priceChangePercent"])
            text += f"{get_number_emoji(i)} 🔴▼ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
        text += "\n⏳ _Canli akıs oturunca liste otomatik daha isabetli olur._"
    else:
        changes = []
        for s, p in price_memory.items():
            if len(p) >= 2:
                changes.append((s, ((p[-1][1]-p[0][1])/p[0][1])*100))

        positives = sorted([x for x in changes if x[1] > 0], key=lambda x: x[1], reverse=True)[:5]
        negatives = sorted([x for x in changes if x[1] < 0], key=lambda x: x[1])[:5]

        text = "⚡ *Son 5 Dakikanin En Hareketlileri*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🟢 *Yukselenler — En Hizli 5*\n"
        for i, (s, c) in enumerate(positives, 1):
            text += f"{get_number_emoji(i)} 🟢▲ `{s:<12}` `%{c:+6.2f}`\n"
        if not positives:
            text += "_Yükseliş yok_\n"
        text += "\n🔴 *Dusenler — En Hizli 5*\n"
        for i, (s, c) in enumerate(negatives, 1):
            text += f"{get_number_emoji(i)} 🔴▼ `{s:<12}` `%{c:+6.2f}`\n"
        if not negatives:
            text += "_Düşüş yok_\n"

    chat  = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb    = bool(update.callback_query)
    kb5 = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",     callback_data="top5"),
        InlineKeyboardButton("📈 24s Lider",  callback_data="top24"),
        InlineKeyboardButton("📊 Market",     callback_data="market"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    msg = await target.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=None if is_group else kb5,
    )
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
        "ℹ️ *Sistem Durumu*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTIF' if r['alarm_active'] else 'KAPALI'}`\n"
        f"🎯 *Esik Degeri:* `%{r['threshold']}`\n"
        f"🔄 *Izleme Modu:* `{r['mode'].upper()}`\n"
        f"📦 *Takip Edilen Sembol:* `{len(price_memory)}`"
    )
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb = bool(update.callback_query)
    kb_st = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",    callback_data="status"),
        InlineKeyboardButton("📊 Market",    callback_data="market"),
        InlineKeyboardButton("📈 24s Lider", callback_data="top24"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    sent = await target.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=None if is_group else kb_st,
    )
    if is_group:
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, sent.message_id, delay))
        if not is_cb and update.message:
            asyncio.create_task(auto_delete(context.bot, chat.id, update.message.message_id, 3))

async def dashboard_command(update: Update, context):
    """/dashboard — Mini App'i açar; grupta üyeler için DM yönlendirme yapar."""
    await register_user(update)
    chat    = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None
    is_group = chat and chat.type in ("group", "supergroup")

    # Grupta üyeler için DM yönlendirme
    if is_group and user_id and not await is_group_admin(context.bot, chat.id, user_id):
        if update.message:
            try: await context.bot.delete_message(chat.id, update.message.message_id)
            except Exception: pass
        murl = get_miniapp_url()
        try:
            dm_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🖥 Dashboard'u Aç", web_app=WebAppInfo(url=murl))
            ]]) if murl else None
            await context.bot.send_message(
                chat_id=user_id,
                text="🖥 *Kripto Drop Dashboard*\nAşağıdaki butona tıklayarak açın 👇",
                parse_mode="Markdown",
                reply_markup=dm_kb
            )
        except Exception: pass
        try:
            tip = await context.bot.send_message(
                chat_id=chat.id,
                text=f"🖥 Dashboard için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
            )
            asyncio.create_task(auto_delete(context.bot, chat.id, tip.message_id, 10))
        except Exception: pass
        return

    murl = get_miniapp_url()

    if murl:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🖥 Dashboard'u Aç", web_app=WebAppInfo(url=murl))
        ]])
        msg = await context.bot.send_message(
            chat.id,
            "🖥 *Kripto Drop Dashboard*\nAşağıdaki butona tıklayarak açın:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        msg = await context.bot.send_message(
            chat.id,
            "⚙️ *Dashboard Kurulum Gerekiyor*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Mini App aktif etmek için:\n\n"
            "1️⃣ Railway → Projen → *Settings*\n"
            "2️⃣ *Networking* sekmesi → *Generate Domain*\n"
            "3️⃣ Oluşan URL'yi kopyala\n"
            "4️⃣ *Variables* → `MINIAPP_URL` = `https://xxx.railway.app`\n"
            "5️⃣ Redeploy yap\n\n"
            "✅ Bundan sonra `/dashboard` butonu aktif olur.",
            parse_mode="Markdown"
        )

    if is_group:
        delay = await get_member_delete_delay()
        asyncio.create_task(auto_delete(context.bot, chat.id, msg.message_id, delay))

async def istatistik(update: Update, context):
    """Bot istatistiklerini sadece ADMIN_ID'ye gösterir."""
    chat    = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None

    # Grupta yazılırsa sessizce sil
    if chat and chat.type in ("group", "supergroup"):
        if update.message:
            try: await update.message.delete()
            except Exception: pass
        return

    if not is_bot_admin(user_id):
        await update.message.reply_text("🚫 Bu komut sadece bot sahibine açıktır.", parse_mode="Markdown")
        return
    await send_istatistik(update.message, context)

async def send_istatistik(target, context):
    """İstatistik mesajını oluşturur ve gönderir (mesaj veya callback_query.message)."""
    try:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM bot_users")
            today = await conn.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE last_active >= NOW() - INTERVAL '1 day'"
            )
            week = await conn.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE last_active >= NOW() - INTERVAL '7 days'"
            )
            new_today = await conn.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE first_seen >= NOW() - INTERVAL '1 day'"
            )
            top_users = await conn.fetch("""
                SELECT user_id, username, full_name, command_count, last_active, first_seen
                FROM bot_users
                ORDER BY command_count DESC
                LIMIT 10
            """)
            total_alarms = await conn.fetchval("SELECT COUNT(*) FROM user_alarms WHERE active=1")
            total_favs   = await conn.fetchval("SELECT COUNT(*) FROM favorites")
            total_hedef  = await conn.fetchval("SELECT COUNT(*) FROM price_targets WHERE active=1")
            total_zamanla = await conn.fetchval("SELECT COUNT(*) FROM scheduled_tasks WHERE active=1")
            alarm_hist   = await conn.fetchval("SELECT COUNT(*) FROM alarm_history")

        now = datetime.utcnow()
        text = (
            "📊 *BOT İSTATİSTİKLERİ*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 *Toplam Kullanıcı:* `{total}`\n"
            f"🆕 *Bugün Yeni:* `{new_today}`\n"
            f"🟢 *Bugün Aktif:* `{today}`\n"
            f"📅 *7 Günde Aktif:* `{week}`\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔔 *Aktif Alarmlar:* `{total_alarms}`\n"
            f"⭐ *Toplam Favori:* `{total_favs}`\n"
            f"🎯 *Aktif Fiyat Hedefi:* `{total_hedef}`\n"
            f"⏰ *Zamanlanmış Görev:* `{total_zamanla}`\n"
            f"📜 *Alarm Tetiklenme (toplam):* `{alarm_hist}`\n"
            f"📡 *Takip Edilen Sembol:* `{len(price_memory)}`\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🏆 *En Aktif 10 Kullanıcı*\n"
        )
        for i, row in enumerate(top_users, 1):
            name = row["full_name"] or row["username"] or f"id:{row['user_id']}"
            uname = f"@{row['username']}" if row["username"] else f"`{row['user_id']}`"
            last = row["last_active"]
            diff = now - last.replace(tzinfo=None) if last else None
            if diff:
                if diff.total_seconds() < 3600:
                    ago = f"{int(diff.total_seconds()//60)}dk önce"
                elif diff.days == 0:
                    ago = f"{int(diff.total_seconds()//3600)}sa önce"
                else:
                    ago = f"{diff.days}g önce"
            else:
                ago = "?"
            medal = ["🥇","🥈","🥉"] [i-1] if i <= 3 else f"{i}."
            text += f"{medal} {uname} — `{row['command_count']}` komut — _{ago}_\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Tüm Kullanıcı Listesi", callback_data="stat_users_0")],
            [InlineKeyboardButton("🔄 Yenile", callback_data="stat_refresh")],
        ])
        if hasattr(target, "edit_text"):
            try:
                await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
                return
            except Exception:
                pass
        await target.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        log.error(f"istatistik hata: {e}")
        try:
            await target.reply_text(f"⚠️ İstatistik alınamadı: {e}")
        except Exception:
            pass

async def send_user_list(target, context, page: int = 0):
    """Tüm kullanıcıları sayfalı listeler (sayfa başı 20 kullanıcı)."""
    PAGE_SIZE = 20
    try:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM bot_users")
            rows = await conn.fetch("""
                SELECT user_id, username, full_name, command_count, last_active, first_seen, chat_type
                FROM bot_users
                ORDER BY last_active DESC
                LIMIT $1 OFFSET $2
            """, PAGE_SIZE, page * PAGE_SIZE)

        now = datetime.utcnow()
        start_idx = page * PAGE_SIZE + 1
        text = (
            f"👥 *TÜM KULLANICILAR* (Sayfa {page+1})\n"
            f"Toplam: `{total}` kullanıcı — Son aktife göre\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
        )
        for i, row in enumerate(rows, start_idx):
            uname = f"@{row['username']}" if row["username"] else f"`{row['user_id']}`"
            fname = row["full_name"] or ""
            last  = row["last_active"]
            diff  = now - last.replace(tzinfo=None) if last else None
            if diff:
                if diff.total_seconds() < 3600:
                    ago = f"{int(diff.total_seconds()//60)}dk"
                elif diff.days == 0:
                    ago = f"{int(diff.total_seconds()//3600)}sa"
                else:
                    ago = f"{diff.days}g"
            else:
                ago = "?"
            ct_icon = "👤" if row["chat_type"] == "private" else "👥"
            text += f"`{i}.` {ct_icon} {uname}"
            if fname:
                text += f" _{fname}_"
            text += f" — `{row['command_count']}` — _{ago}_\n"

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Önceki", callback_data=f"stat_users_{page-1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav_buttons.append(InlineKeyboardButton("Sonraki ➡️", callback_data=f"stat_users_{page+1}"))

        keyboard_rows = []
        if nav_buttons:
            keyboard_rows.append(nav_buttons)
        keyboard_rows.append([InlineKeyboardButton("🔙 İstatistiklere Dön", callback_data="stat_refresh")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)

        if hasattr(target, "edit_text"):
            try:
                await target.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
                return
            except Exception:
                pass
        await target.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        log.error(f"send_user_list hata: {e}")
        try:
            await target.reply_text(f"⚠️ Liste alınamadı: {e}")
        except Exception:
            pass

# ================= CALLBACK =================

async def button_handler(update: Update, context):
    q = update.callback_query

    if q.data.startswith("set_"):
        await set_callback(update, context)
        return

    # İstatistik callback'leri — sadece bot adminine
    if q.data.startswith("stat_"):
        if not is_bot_admin(q.from_user.id):
            await q.answer("🚫 Bu panel sadece bot sahibine açıktır.", show_alert=True)
            return
        await q.answer()
        if q.data == "stat_refresh":
            await send_istatistik(q.message, context)
        elif q.data.startswith("stat_users_"):
            page = int(q.data.split("_")[-1])
            await send_user_list(q.message, context, page)
        return

    # Fibonacci callback
    if q.data.startswith("fib_"):
        await q.answer()
        # fib_help özel kontrol — parts kontrolünden ÖNCE
        if q.data == "fib_help":
            await q.message.reply_text(
                "📐 *Fibonacci Rehberi*\n━━━━━━━━━━━━━━━━━━━━━\n"
                "Kullanım: `/fib BTCUSDT`\n"
                "Zaman dilimleri: `1h` `4h` `1d` `1w`\n\n"
                "Fibonacci seviyeleri destek ve direnç tahmini için kullanılır.\n"
                "📖 Detaylı bilgi: `/ne fibonacci`",
                parse_mode="Markdown"
            )
            return
        parts  = q.data.split("_")   # fib_BTCUSDT_4h
        if len(parts) >= 3:
            symbol   = parts[1]
            interval = parts[2]
            loading_msg = await q.message.reply_text(f"📐 `{symbol}` {interval} Fibonacci hesaplanıyor...")
            buf, text = await generate_fib_chart(symbol, interval)
            try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
            except Exception: pass
            if buf:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("1h", callback_data=f"fib_{symbol}_1h"),
                    InlineKeyboardButton("4h", callback_data=f"fib_{symbol}_4h"),
                    InlineKeyboardButton("1d", callback_data=f"fib_{symbol}_1d"),
                    InlineKeyboardButton("1w", callback_data=f"fib_{symbol}_1w"),
                ]])
                await context.bot.send_photo(
                    chat_id=q.message.chat.id,
                    photo=buf,
                    caption=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
        return

    # Sentiment callback
    if q.data.startswith("sent_"):
        await q.answer()
        if q.data == "sent_help":
            await q.message.reply_text(
                "🧠 *Sentiment Rehberi*\n━━━━━━━━━━━━━━━━━━━━━\n"
                "Kullanım: `/sentiment BTCUSDT`\n\n"
                "Haber ve topluluk verilerinden duygu analizi üretir.\n"
                "Groq AI + CryptoPanic entegrasyonu ile çalışır.",
                parse_mode="Markdown"
            )
        elif len(q.data) > 5:
            symbol = q.data[5:]
            loading_msg = await q.message.reply_text(f"🧠 `{symbol}` analiz ediliyor...")
            result = await fetch_sentiment(symbol)
            try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
            except Exception: pass
            bar  = "🟩" * int(result["score"]*10) + "⬜" * (10 - int(result["score"]*10))
            text = (
                f"🧠 *{symbol} — Sentiment Analizi*\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"💭 *Genel Duygu:* {result['label']}\n"
                f"📊 *Skor:* `{result['score']:.2f}` / `1.00`\n{bar}\n"
                f"📰 *Haber Sayısı:* `{result['news_count']}`\n"
                f"🔎 *Kaynak:* `{result['source']}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💬 *Özet:* _{result['summary']}_"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Yenile", callback_data=f"sent_{symbol}"),
                InlineKeyboardButton("📊 Analiz",  callback_data=f"analyse_{symbol}"),
            ]])
            _sent_private = q.message.chat.type == "private"
            await q.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=keyboard if _sent_private else None,
            )
        return

    # Analiz callback (sentiment butonundan açılır)
    if q.data.startswith("analyse_"):
        await q.answer()
        symbol = q.data[8:]  # "analyse_BTCUSDT" → "BTCUSDT"
        if not symbol:
            return
        loading_msg = await q.message.reply_text(f"🔍 `{symbol}` analiz ediliyor...", parse_mode="Markdown")
        try:
            async with aiohttp.ClientSession() as session:
                ticker_resp = await session.get(
                    f"{BINANCE_24H.replace('24hr','24hr')}?symbol={symbol}",
                    timeout=aiohttp.ClientTimeout(total=8)
                )
                ticker = await ticker_resp.json()
                klines_resp = await session.get(
                    f"{BINANCE_KLINES}?symbol={symbol}&interval=1h&limit=50",
                    timeout=aiohttp.ClientTimeout(total=8)
                )
                klines = await klines_resp.json()
        except Exception as e:
            try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
            except Exception: pass
            await q.message.reply_text(f"⚠️ Veri alınamadı: {e}")
            return

        try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
        except Exception: pass

        if ticker.get("code") or not isinstance(klines, list) or len(klines) < 14:
            await q.message.reply_text(f"⚠️ `{symbol}` için yeterli veri yok.", parse_mode="Markdown")
            return

        price  = float(ticker.get("lastPrice", 0))
        pct24  = float(ticker.get("priceChangePercent", 0))
        vol24  = float(ticker.get("quoteVolume", 0))
        high24 = float(ticker.get("highPrice", 0))
        low24  = float(ticker.get("lowPrice", 0))

        # RSI hesapla
        closes = [float(k[4]) for k in klines]
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        period = 14
        avg_g = sum(gains[-period:]) / period
        avg_l = sum(losses[-period:]) / period or 0.0001
        rsi   = round(100 - 100 / (1 + avg_g / avg_l), 1)

        # EMA 20
        k2  = 2 / 21
        ema = sum(closes[:20]) / 20
        for c in closes[20:]: ema = c * k2 + ema * (1 - k2)
        ema20 = round(ema, 4)

        rsi_label = "🔴 Aşırı Alım" if rsi > 70 else ("🟢 Aşırı Satım" if rsi < 30 else "🟡 Nötr")
        trend     = "🟢 Yükseliş" if price > ema20 else "🔴 Düşüş"
        pct_icon  = "📈" if pct24 >= 0 else "📉"

        def fmt(p):
            return f"{p:,.4f}" if p < 1 else f"{p:,.2f}"

        text = (
            f"🔍 *{symbol} — Teknik Özet*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Anlık Fiyat:* `${fmt(price)}`  {pct_icon} `{pct24:+.2f}%`\n"
            f"📦 *24s Hacim:* `${vol24/1e6:.1f}M`\n"
            f"📈 *24s Yüksek:* `${fmt(high24)}`\n"
            f"📉 *24s Düşük:* `${fmt(low24)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔮 *RSI (14):* `{rsi}` — {rsi_label}\n"
            f"📐 *EMA 20:* `${fmt(ema20)}` — {trend}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 _Güncelleme: {datetime.utcnow().strftime('%H:%M UTC')}_"
        )
        _cb_chat = q.message.chat if q.message else None
        _cb_in_group = bool(_cb_chat and _cb_chat.type in ("group", "supergroup"))
        if _cb_in_group:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📈 Binance'de Görüntüle",
                    url=f"https://www.binance.com/tr/trade/{symbol.replace('USDT','_USDT')}"
                )
            ]])
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📐 Fibonacci", callback_data=f"fib_{symbol}_4h"),
                InlineKeyboardButton("🧠 Sentiment", callback_data=f"sent_{symbol}"),
                InlineKeyboardButton("🔄 Yenile",    callback_data=f"analyse_{symbol}"),
            ]])
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    # Takvim callback
    if q.data.startswith("takvim_"):
        # Grupta üye ise DM'e yönlendir
        _takvim_chat = q.message.chat if q.message else None
        _takvim_in_group = bool(_takvim_chat and _takvim_chat.type in ("group", "supergroup"))
        if _takvim_in_group:
            _takvim_is_adm = await is_group_admin(context.bot, _takvim_chat.id, q.from_user.id)
            if not _takvim_is_adm:
                try:
                    await context.bot.send_message(
                        chat_id=q.from_user.id,
                        text="📅 *Ekonomik Takvim* özelliğini kullanmak için buraya tıklayın 👇\nBotu DM üzerinden kullanabilirsiniz.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                try:
                    tip = await context.bot.send_message(
                        chat_id=_takvim_chat.id,
                        text=f"📅 Ekonomik Takvim için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
                    )
                    asyncio.create_task(auto_delete(context.bot, _takvim_chat.id, tip.message_id, 10))
                except Exception:
                    pass
                await q.answer()
                return
        await q.answer()
        if q.data == "takvim_refresh":
            events = await fetch_crypto_calendar()
            now    = datetime.utcnow()
            text   = "📅 *EKONOMİK & KRİPTO TAKVİM*\n━━━━━━━━━━━━━━━━━━━━━\n"
            for ev in events[:8]:
                try:
                    ev_dt  = datetime.strptime(ev["date"], "%Y-%m-%d")
                    diff   = (ev_dt.date() - now.date()).days
                    if diff == 0:
                        zamanl = "⚡ *BUGÜN*"
                    elif diff == 1:
                        zamanl = "🔜 *Yarın*"
                    elif diff < 0:
                        zamanl = f"📌 _{abs(diff)}g önce_"
                    elif diff < 7:
                        zamanl = f"📆 *{diff}g sonra*"
                    else:
                        zamanl = f"📆 {ev['date']}"
                    imp     = ev.get("importance", 0)
                    imp_str = "🔴" if imp >= 80 else ("🟡" if imp >= 50 else "🟢")
                    coins   = f"\n🪙 _{ev['coins']}_" if ev.get("coins") else ""
                    desc    = f"\n💬 _{ev['desc']}_" if ev.get("desc") else ""
                    text   += f"\n{imp_str} {zamanl}\n📌 *{ev['title']}*{coins}{desc}\n"
                except Exception: pass
            text += f"\n━━━━━━━━━━━━━━━━━━━━━\n🔴 Yüksek  🟡 Orta  🟢 Düşük etki\n⏰ _{now.strftime('%d.%m.%Y %H:%M')} UTC_"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔔 Bildirim Aç/Kapat", callback_data="takvim_toggle"),
                InlineKeyboardButton("🔄 Yenile",             callback_data="takvim_refresh"),
            ]])
            try:
                await q.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        elif q.data == "takvim_toggle":
            user_id = q.from_user.id
            async with db_pool.acquire() as conn:
                existing = await conn.fetchrow("SELECT active FROM takvim_subscribers WHERE user_id=$1", user_id)
                if existing:
                    new_val = 0 if existing["active"] else 1
                    await conn.execute("UPDATE takvim_subscribers SET active=$1 WHERE user_id=$2", new_val, user_id)
                    status = "açıldı ✅" if new_val else "kapatıldı ❌"
                else:
                    await conn.execute("INSERT INTO takvim_subscribers(user_id, active) VALUES($1,1)", user_id)
                    status = "açıldı ✅"
            await q.answer(f"📅 Takvim bildirimleri {status}", show_alert=True)
        return

    # Terim sözlüğü direkt açma callback — ne_TERIM formatı
    if q.data.startswith("ne_") and q.data != "ne_help":
        await q.answer()
        terim = q.data[3:].lower()
        if terim in SOZLUK:
            text_ne = SOZLUK[terim]
        else:
            eslesme = [k for k in SOZLUK if terim in k or k in terim]
            text_ne = SOZLUK[eslesme[0]] if eslesme else f"❓ `{terim}` bulunamadı."
        diger2 = [k for k in SOZLUK if k != terim][:8]
        random.shuffle(diger2)
        ilgili2 = diger2[:4]
        kb_ne2_rows = [[InlineKeyboardButton(f"📖 {k}", callback_data=f"ne_{k}") for k in ilgili2[:2]]]
        if len(ilgili2) > 2:
            kb_ne2_rows.append([InlineKeyboardButton(f"📖 {k}", callback_data=f"ne_{k}") for k in ilgili2[2:4]])
        await q.message.reply_text(text_ne, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb_ne2_rows))
        return

    # Terim sözlüğü help callback
    if q.data == "ne_help":
        await q.answer()
        terimler = " • ".join(f"`{k}`" for k in sorted(SOZLUK.keys()))
        await q.message.reply_text(
            f"📚 *Kripto Terim Sözlüğü*\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"Kullanım: `/ne MACD`\n\n📖 *Mevcut Terimler:*\n{terimler}",
            parse_mode="Markdown"
        )
        return

    chat = q.message.chat if q.message else None
    is_group_chat = bool(chat and chat.type in ("group", "supergroup"))
    is_adm = False
    if is_group_chat:
        is_adm = await is_group_admin(context.bot, chat.id, q.from_user.id)

    # Grupta sadece bu callback'ler kısıtlama olmadan çalışır
    GROUP_FREE = {
        "top24", "top5", "market", "status",
        "miniapp_dm",
    }

    async def dm_redirect(feature_name: str):
        """DM'e mesaj + gruba kısa uyarı."""
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
                text=f"🔒 {feature_name} için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
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
        elif q.data == "fib_help":
            await dm_redirect("Fibonacci Analizi")
            return
        elif q.data == "sent_help":
            await dm_redirect("Sentiment Analizi")
            return
        elif q.data == "ne_help":
            await dm_redirect("Terim Sözlüğü")
            return
        elif q.data not in GROUP_FREE \
                and not q.data.startswith("hedef_") \
                and not q.data.startswith("set_") \
                and not q.data.startswith("fib_") \
                and not q.data.startswith("sent_"):
            await dm_redirect("Bu özellik")
            return

    await q.answer()

    # ── Mini App DM yönlendirme ──
    if q.data == "miniapp_dm":
        murl = get_miniapp_url()
        if murl:
            try:
                await context.bot.send_message(
                    chat_id=q.from_user.id,
                    text="🖥 *Kripto Dashboard* — Mini App'i açmak için aşağıdaki butona tıklayın:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚀 Dashboard'u Aç", web_app=WebAppInfo(url=murl))
                    ]])
                )
                tip = await context.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=f"📩 @{q.from_user.username or q.from_user.first_name} DM'inize Mini App bağlantısı gönderildi!",
                )
                asyncio.create_task(auto_delete(context.bot, q.message.chat.id, tip.message_id, 8))
            except Exception:
                try:
                    tip = await context.bot.send_message(
                        chat_id=q.message.chat.id,
                        text=f"🖥 Dashboard: {murl}",
                    )
                    asyncio.create_task(auto_delete(context.bot, q.message.chat.id, tip.message_id, 15))
                except Exception:
                    pass
        return

    # ── Market & genel ──
    if q.data == "market":
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
            "➕ *Alarm Türleri:*\n━━━━━━━━━━━━━━━━━━\n"
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
            await q.message.reply_text("🗑 Tüm kişisel alarmlarınız silindi.")
    elif q.data == "alarm_history":
        await alarm_gecmis(update, context)
    elif q.data.startswith("alarm_unpause_"):
        symbol_up = q.data.replace("alarm_unpause_", "")
        user_id_up = q.from_user.id
        async with db_pool.acquire() as conn:
            r_up = await conn.execute(
                "UPDATE user_alarms SET paused_until=NULL WHERE user_id=$1 AND symbol=$2",
                user_id_up, symbol_up
            )
        if r_up == "UPDATE 0":
            await q.answer(f"⚠️ {symbol_up} için alarm bulunamadı.", show_alert=True)
        else:
            await q.answer(f"▶️ {symbol_up} alarmı yeniden aktif!", show_alert=True)
        await my_alarm_v2(update, context)

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
            await q.message.reply_text("🗑 Tüm favorileriniz silindi.")

    # ── MTF ──
    elif q.data.startswith("mtf_sym_"):
        symbol = q.data.replace("mtf_sym_", "")
        # mtf_command'u callback üzerinden çağır
        context.args = [symbol]
        await mtf_command(update, context)
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
                    text=f"🔒 Fiyat Hedefi için lütfen DM'den kullanın 👇 @{BOT_USERNAME}",
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
        # Grupta admin paneli açılmaz — kullanıcıyı DM'e yönlendir
        if q.message.chat.type in ("group", "supergroup"):
            await q.answer(f"⚙️ Admin paneli için bota DM'den yazın: @{BOT_USERNAME}", show_alert=True)
            return
        # DM'de: bot sahibi veya grubun admini olmalı
        if not is_bot_admin(q.from_user.id):
            try:
                member = await context.bot.get_chat_member(GROUP_CHAT_ID, q.from_user.id)
                if member.status not in ("administrator", "creator"):
                    await q.answer("🚫 Bu panel sadece grup adminlerine açıktır.", show_alert=True)
                    return
            except Exception as e:
                log.warning(f"set_open admin kontrol: {e}")
                await q.answer("🚫 Yetki kontrol edilemedi.", show_alert=True)
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

            # ── Düşük hacimli / kara listedeki coinleri filtrele ──
            if symbol in ALARM_BLACKLIST:
                continue
            vol24 = volume_24h_cache.get(symbol, 0)
            if vol24 < MIN_ALARM_VOLUME_24H:
                continue
            rank = marketcap_rank_cache.get(symbol)
            if rank is not None and rank > MIN_ALARM_RANK:
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
            yon = "⚡🟢 5dk YÜKSELİŞ UYARISI 🟢⚡" if ch5 > 0 else "⚡🔴 5dk DÜŞÜŞ UYARISI 🔴⚡"
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
                # rsi_level negatifse "aşağı" yön, pozitifse "yukarı" yön
                abs_level = abs(rsi_level)
                if rsi_level < 0:   # aşağı alarm
                    triggered = rsi_now <= abs_level
                    direction = "down"
                else:               # yukarı alarm
                    triggered = rsi_now >= abs_level
                    direction = "up"
            except Exception:
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
            log.warning(f"Kişisel alarm gönderilemedi ({user_id}): {e}")

    # Cooldown temizliği — 1 saatten eski kayıtları sil (#11)
    expired_keys = [k for k, v in cooldowns.items() if now - v > timedelta(hours=1)]
    for k in expired_keys:
        del cooldowns[k]

# ================= MINI APP WEB SUNUCUSU =================
# Mini App HTML ayrı bir dosyada tutulur — bakım kolaylığı için.

def _load_miniapp_html() -> str:
    """miniapp.html dosyasını oku. Dosya bulunamazsa boş sayfa döndür."""
    _base = os.path.dirname(os.path.abspath(__file__))
    _path = os.path.join(_base, "miniapp.html")
    try:
        with open(_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        log.warning(f"miniapp.html bulunamadı: {_path}")
        return "<html><body><h1>Mini App yüklenemedi</h1></body></html>"

MINIAPP_HTML = _load_miniapp_html()

async def _start_miniapp_server(bot):
    """
    Mini App'i bot ile aynı process içinde çalıştırır.
    Railway otomatik PORT atar ve public URL verir.
    /api/favorites ve /api/alarms endpointleri ile bot verilerine erişim sağlar.
    """
    global MINIAPP_URL
    try:
        from aiohttp import web as aiohttp_web
        import json as _json

        port = int(os.getenv("PORT", 8080))

        CORS_HEADERS = {
            "X-Frame-Options": "ALLOWALL",
            "Content-Security-Policy": "frame-ancestors *",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

        async def handle_index(request):
            return aiohttp_web.Response(
                text=MINIAPP_HTML, content_type="text/html",
                charset="utf-8", headers=CORS_HEADERS
            )

        async def handle_health(request):
            return aiohttp_web.Response(text="OK")

        async def handle_proxy(request):
            """Dış API'lere proxy — Telegram WebView CORS sorununu çözer."""
            target_url = request.rel_url.query.get("url", "")
            if not target_url:
                return aiohttp_web.Response(text='{"error":"no url"}', content_type="application/json", headers=CORS_HEADERS)
            allowed = [
                "api.binance.com", "api.alternative.me", "api.coingecko.com",
                "api.rss2json.com", "cryptopanic.com", "tradingeconomics.com",
                "www.coindesk.com", "cointelegraph.com", "decrypt.co",
            ]
            from urllib.parse import urlparse
            parsed = urlparse(target_url)
            if not any(parsed.netloc.endswith(d) for d in allowed):
                return aiohttp_web.Response(text='{"error":"domain not allowed"}', content_type="application/json", headers=CORS_HEADERS)
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(
                        target_url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; KriptoDrop/1.0)",
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Encoding": "identity",
                        },
                        timeout=aiohttp.ClientTimeout(total=15),
                        allow_redirects=True,
                    ) as resp:
                        # Büyük yanıtları da tam oku
                        body = await resp.read()
                        try:
                            text_body = body.decode("utf-8")
                        except Exception:
                            text_body = body.decode("latin-1", errors="replace")
                        ct = resp.headers.get("Content-Type", "application/json").split(";")[0].strip()
                        if not ct:
                            ct = "application/json"
                log.info(f"Proxy OK: {parsed.netloc} — {len(text_body)} bytes")
                return aiohttp_web.Response(
                    text=text_body,
                    content_type="application/json",
                    headers=CORS_HEADERS
                )
            except Exception as e:
                log.warning(f"Proxy hata: {target_url} — {e}")
                return aiohttp_web.Response(
                    text=f'{{"error":"{str(e)}"}}',
                    content_type="application/json", headers=CORS_HEADERS
                )

        async def handle_news(request):
            """RSS haberlerini server tarafında parse eder — CORS sorunu olmaz."""
            import xml.etree.ElementTree as ET
            import json as _json2
            feeds = [
                ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
                ("https://cointelegraph.com/rss", "CoinTelegraph"),
                ("https://decrypt.co/feed", "Decrypt"),
            ]
            items = []
            for feed_url, source in feeds:
                if items:
                    break
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            feed_url,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; KriptoDrop/1.0)"},
                            timeout=aiohttp.ClientTimeout(total=8)
                        ) as resp:
                            if resp.status != 200:
                                continue
                            xml_text = await resp.text()
                    root = ET.fromstring(xml_text)
                    channel = root.find("channel")
                    if channel is None:
                        channel = root
                    for item in (channel.findall("item") or [])[:6]:
                        title_el = item.find("title")
                        date_el  = item.find("pubDate")
                        if title_el is None or not title_el.text:
                            continue
                        date_str = ""
                        if date_el is not None and date_el.text:
                            try:
                                from email.utils import parsedate_to_datetime
                                dt = parsedate_to_datetime(date_el.text)
                                date_str = dt.strftime("%d %b")
                            except Exception:
                                pass
                        items.append({
                            "title": title_el.text.strip(),
                            "date":  date_str,
                            "source": source,
                        })
                except Exception as e:
                    log.warning(f"News feed {source} hata: {e}")
                    continue
            result = {"items": items}
            return aiohttp_web.Response(
                text=_json2.dumps(result, ensure_ascii=False),
                content_type="application/json",
                headers=CORS_HEADERS
            )

        async def handle_favorites(request):
            """Kullanıcının favori coinlerini döndürür."""
            uid_str = request.rel_url.query.get("uid", "")
            result  = {"favorites": [], "error": None}
            if uid_str and uid_str.isdigit() and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT symbol FROM favorites WHERE user_id=$1 ORDER BY symbol",
                            int(uid_str)
                        )
                    result["favorites"] = [r["symbol"] for r in rows]
                except Exception as e:
                    result["error"] = str(e)
            return aiohttp_web.Response(
                text=_json.dumps(result), content_type="application/json",
                headers=CORS_HEADERS
            )

        async def handle_alarms(request):
            """Kullanıcının aktif alarmlarını döndürür."""
            uid_str = request.rel_url.query.get("uid", "")
            result  = {"alarms": [], "error": None}
            if uid_str and uid_str.isdigit() and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        rows = await conn.fetch("""
                            SELECT symbol, threshold, alarm_type, rsi_level,
                                   band_low, band_high, active, trigger_count,
                                   last_triggered, paused_until
                            FROM user_alarms WHERE user_id=$1
                            ORDER BY active DESC, symbol
                        """, int(uid_str))
                    now = datetime.utcnow()
                    alarms = []
                    for r in rows:
                        paused = r["paused_until"]
                        is_paused = paused and paused.replace(tzinfo=None) > now
                        last = r["last_triggered"]
                        last_str = last.strftime("%d.%m %H:%M") if last else None
                        alarms.append({
                            "symbol":       r["symbol"],
                            "threshold":    r["threshold"],
                            "type":         r["alarm_type"] or "percent",
                            "rsi_level":    r["rsi_level"],
                            "band_low":     r["band_low"],
                            "band_high":    r["band_high"],
                            "active":       bool(r["active"]) and not is_paused,
                            "paused":       bool(is_paused),
                            "trigger_count":r["trigger_count"] or 0,
                            "last_triggered":last_str,
                        })
                    result["alarms"] = alarms
                except Exception as e:
                    result["error"] = str(e)
            return aiohttp_web.Response(
                text=_json.dumps(result), content_type="application/json",
                headers=CORS_HEADERS
            )

        async def handle_kar_pozisyon(request):
            uid_str = request.rel_url.query.get("uid","")
            result = {"positions":[],"error":None}
            if uid_str and uid_str.isdigit() and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT symbol,amount,buy_price,note FROM kar_pozisyonlar WHERE user_id=$1 ORDER BY symbol",
                            int(uid_str)
                        )
                    result["positions"] = [{"symbol":r["symbol"],"amount":float(r["amount"]),"buy_price":float(r["buy_price"]),"note":r["note"] or ""} for r in rows]
                except Exception as e:
                    result["error"] = str(e)
            return aiohttp_web.Response(text=_json.dumps(result),content_type="application/json",headers=CORS_HEADERS)

        async def handle_kar_kaydet(request):
            uid_str = request.rel_url.query.get("uid","")
            symbol  = request.rel_url.query.get("symbol","").upper()
            try:
                amount    = float(request.rel_url.query.get("amount","0"))
                buy_price = float(request.rel_url.query.get("buy_price","0"))
            except Exception:
                return aiohttp_web.Response(text='{"ok":false,"error":"invalid params"}',content_type="application/json",headers=CORS_HEADERS)
            if uid_str and uid_str.isdigit() and symbol and amount>0 and buy_price>0 and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO kar_pozisyonlar(user_id,symbol,amount,buy_price) VALUES($1,$2,$3,$4) ON CONFLICT(user_id,symbol) DO UPDATE SET amount=$3,buy_price=$4",
                            int(uid_str),symbol,amount,buy_price
                        )
                    return aiohttp_web.Response(text='{"ok":true}',content_type="application/json",headers=CORS_HEADERS)
                except Exception as e:
                    return aiohttp_web.Response(text=_json.dumps({"ok":False,"error":str(e)}),content_type="application/json",headers=CORS_HEADERS)
            return aiohttp_web.Response(text='{"ok":false,"error":"missing params"}',content_type="application/json",headers=CORS_HEADERS)

        async def handle_kar_sil(request):
            uid_str = request.rel_url.query.get("uid","")
            symbol  = request.rel_url.query.get("symbol","").upper()
            if uid_str and uid_str.isdigit() and symbol and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("DELETE FROM kar_pozisyonlar WHERE user_id=$1 AND symbol=$2",int(uid_str),symbol)
                    return aiohttp_web.Response(text='{"ok":true}',content_type="application/json",headers=CORS_HEADERS)
                except Exception as e:
                    return aiohttp_web.Response(text=_json.dumps({"ok":False,"error":str(e)}),content_type="application/json",headers=CORS_HEADERS)
            return aiohttp_web.Response(text='{"ok":false,"error":"missing params"}',content_type="application/json",headers=CORS_HEADERS)

        # json zaten dosya başında import edildi, _json alias olarak kullan
        _json = json

        async def _translate_news_items(items):
            if not GROQ_API_KEY or not items:
                log.info(f"Haber cevirisi atlanıyor: GROQ_KEY={'var' if GROQ_API_KEY else 'yok'}, items={len(items)}")
                return items
            try:
                titles = "\n".join(f"{i+1}. {n['title']}" for i,n in enumerate(items))
                prompt = (
                    "Translate the following English crypto news headlines to Turkish. "
                    "Output ONLY the translations, one per line, same order, no numbers or extra text.\n\n"
                    + titles
                )
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                        json={
                            "model": "llama-3.1-8b-instant",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 800, "temperature": 0.1
                        },
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            raw = data["choices"][0]["message"]["content"].strip()
                            translated = [l.strip() for l in raw.split("\n") if l.strip()]
                            for i, item in enumerate(items):
                                if i < len(translated):
                                    item["title_tr"] = translated[i]
                            log.info(f"Haber cevirisi OK: {len(items)} baslik")
                        else:
                            log.warning(f"Groq hata: {r.status}")
            except Exception as e:
                log.warning(f"Haber çevirisi başarısız: {e}")
            return items

        async def _fetch_rss(feeds_list, max_per=6):
            items = []
            for feed_url, src_name in feeds_list:
                if len(items) >= 10:
                    break
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(
                            feed_url,
                            headers={"User-Agent": "Mozilla/5.0"},
                            timeout=aiohttp.ClientTimeout(total=7)
                        ) as r:
                            if r.status != 200:
                                continue
                            xml_text = await r.text()
                    root = _ET_news.fromstring(xml_text)
                    ch = root.find("channel") or root
                    for item in list(ch.findall("item"))[:max_per]:
                        title = item.findtext("title","").strip()
                        if not title:
                            continue
                        desc = _clean_html_text(item.findtext("description",""))[:280]
                        link = item.findtext("link","").strip()
                        pubdate = item.findtext("pubDate","").strip()
                        items.append({
                            "title": title,
                            "title_tr": title,
                            "summary": desc,
                            "source": src_name,
                            "url": link,
                            "published_at": pubdate[:16] if pubdate else "",
                        })
                except Exception:
                    pass
            return items

        async def handle_dashboard(request):
            """Ana sayfa için tüm veriyi sunucuda toplar — tek istek."""
            uid_str = request.rel_url.query.get("uid","")
            result = {}
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get("https://api.binance.com/api/v3/ticker/24hr",
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        t24 = await r.json()
            except Exception as e:
                log.warning(f"dashboard binance hata: {e}")
                t24 = []

            usdt = [x for x in t24 if x.get("symbol","").endswith("USDT")]
            btc = next((x for x in usdt if x["symbol"]=="BTCUSDT"),{})
            eth = next((x for x in usdt if x["symbol"]=="ETHUSDT"),{})
            bv = float(btc.get("quoteVolume",0))
            tv = sum(float(x.get("quoteVolume",0)) for x in usdt) or 1
            chs = [float(x.get("priceChangePercent",0)) for x in usdt]
            avg = sum(chs)/len(chs) if chs else 0

            # Fiyatlar (ticker için)
            prices = {x["symbol"]: float(x.get("lastPrice",0)) for x in usdt}

            # Top5
            filtered = [x for x in usdt if float(x.get("quoteVolume",0))>1e6]
            top5g = sorted(filtered, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:5]

            # Tüm coin listesi — marketcap sıralamasına göre, rank dahil
            coins_raw = [{"s":x["symbol"],"p":float(x.get("lastPrice",0)),
                          "ch":float(x.get("priceChangePercent",0)),
                          "v":float(x.get("quoteVolume",0)),
                          "rank": marketcap_rank_cache.get(x["symbol"], 9999),
                          "img": coin_image_cache.get(x["symbol"].replace("USDT","").lower(),"")}
                         for x in usdt if float(x.get("quoteVolume",0))>100000]
            # Marketcap'e göre sırala, rank yoksa hacme göre
            coins_raw.sort(key=lambda x: (x["rank"] if x["rank"]<9000 else 9999, -x["v"]))
            coins_base = coins_raw[:120]

            # Top gainers/losers'ı coins listesine ekle (piyasa sayfasıyla tutarlılık)
            coins_syms = {c["s"] for c in coins_base}
            extra_top = sorted(filtered, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:30]
            extra_bot = sorted(filtered, key=lambda x: float(x.get("priceChangePercent",0)))[:30]
            for x in extra_top + extra_bot:
                if x["symbol"] not in coins_syms:
                    base = x["symbol"].replace("USDT","").lower()
                    coins_base.append({"s":x["symbol"],"p":float(x.get("lastPrice",0)),
                                       "ch":float(x.get("priceChangePercent",0)),
                                       "v":float(x.get("quoteVolume",0)),
                                       "rank": marketcap_rank_cache.get(x["symbol"], 9999),
                                       "img": coin_image_cache.get(base,"")})
                    coins_syms.add(x["symbol"])
            coins = coins_base

            # Top data
            top_g = sorted(filtered, key=lambda x: float(x.get("priceChangePercent",0)), reverse=True)[:20]
            top_l = sorted(filtered, key=lambda x: float(x.get("priceChangePercent",0)))[:20]
            top_v = sorted(filtered, key=lambda x: float(x.get("quoteVolume",0)), reverse=True)[:20]
            def mkcoin(x):
                base = x["symbol"].replace("USDT","").lower()
                return {"s":x["symbol"],"p":float(x.get("lastPrice",0)),
                        "ch":float(x.get("priceChangePercent",0)),"v":float(x.get("quoteVolume",0)),
                        "img":coin_image_cache.get(base,"")}

            # Alarmlar
            alarms = []
            if uid_str and uid_str.isdigit() and db_pool:
                try:
                    async with db_pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT symbol,threshold,alarm_type,active,paused_until FROM user_alarms WHERE user_id=$1 AND active=1 ORDER BY symbol",
                            int(uid_str)
                        )
                    now = datetime.utcnow()
                    for r in rows:
                        pu = r["paused_until"]
                        if pu and pu.replace(tzinfo=None) > now:
                            continue
                        alarms.append({"symbol":r["symbol"],"threshold":r["threshold"],"type":r["alarm_type"] or "percent"})
                except Exception as e:
                    log.warning(f"dashboard alarm hata: {e}")

            # Haberler — _fetch_rss + Groq ceviri
            try:
                raw_news = await _fetch_rss(
                    [("https://cointelegraph.com/rss","CoinTelegraph")], max_per=5
                )
                news = await _translate_news_items(raw_news)
            except Exception:
                news = []

            # 5dk flash verileri — price_memory WebSocket verisinden
            flash5up_list = []
            flash5dn_list = []
            if price_memory:
                changes5 = []
                for sym5, pts in price_memory.items():
                    if len(pts) >= 2:
                        ch5 = ((pts[-1][1] - pts[0][1]) / pts[0][1]) * 100
                        cur5 = pts[-1][1]
                        base5 = sym5.replace("USDT","").lower()
                        changes5.append({"s": sym5, "p": cur5, "ch5": round(ch5, 2), "img": coin_image_cache.get(base5,"")})
                flash5up_list = sorted([x for x in changes5 if x["ch5"] > 0],
                                       key=lambda x: x["ch5"], reverse=True)[:30]
                flash5dn_list = sorted([x for x in changes5 if x["ch5"] < 0],
                                       key=lambda x: x["ch5"])[:30]

            # Fallback: WebSocket verisi yetersizse Binance REST 5m rolling ticker kullan
            if not flash5up_list and not flash5dn_list:
                try:
                    top_syms = [x["symbol"] for x in sorted(usdt, key=lambda x: float(x.get("quoteVolume",0)), reverse=True)[:100]]
                    syms_param = json.dumps(top_syms)
                    async with aiohttp.ClientSession() as s5:
                        async with s5.get(
                            f"https://api.binance.com/api/v3/ticker?symbols={syms_param}&windowSize=5m",
                            timeout=aiohttp.ClientTimeout(total=8)) as r5:
                            if r5.status == 200:
                                ticker5 = await r5.json()
                                changes5f = []
                                for t5 in ticker5:
                                    sym5f = t5.get("symbol","")
                                    if not sym5f.endswith("USDT"): continue
                                    ch5f = float(t5.get("priceChangePercent", 0) or 0)
                                    cur5f = float(t5.get("lastPrice", 0) or 0)
                                    base5f = sym5f.replace("USDT","").lower()
                                    if ch5f != 0 and cur5f > 0:
                                        changes5f.append({"s": sym5f, "p": cur5f, "ch5": round(ch5f,2), "img": coin_image_cache.get(base5f,"")})
                                flash5up_list = sorted([x for x in changes5f if x["ch5"] > 0], key=lambda x: x["ch5"], reverse=True)[:30]
                                flash5dn_list = sorted([x for x in changes5f if x["ch5"] < 0], key=lambda x: x["ch5"])[:30]
                except Exception as e5:
                    log.warning(f"flash5 REST fallback hata: {e5}")

            result = {
                "btc": {"price":float(btc.get("lastPrice",0)),"change":float(btc.get("priceChangePercent",0)),"volume":bv},
                "eth": {"price":float(eth.get("lastPrice",0)),"change":float(eth.get("priceChangePercent",0)),"volume":float(eth.get("quoteVolume",0))},
                "btc_dom": round(bv/tv*100,1),
                "avg_change": round(avg,2),
                "rising": sum(1 for c in chs if c>0),
                "falling": sum(1 for c in chs if c<0),
                "top5": [mkcoin(x) for x in top5g],
                "top_data": {"g":[mkcoin(x) for x in top_g],"l":[mkcoin(x) for x in top_l],"v":[mkcoin(x) for x in top_v]},
                "coins": coins,
                "prices": prices,
                "alarms": alarms,
                "news": news,
                "flash5up": flash5up_list,
                "flash5dn": flash5dn_list,
            }
            log.info(f"dashboard OK: {len(usdt)} coin, {len(alarms)} alarm")
            return aiohttp_web.Response(
                text=_json.dumps(result), content_type="application/json", headers=CORS_HEADERS
            )

        # handle_price kaldırıldı — handle_price_with_change kullanılıyor (#6)

        async def handle_prices(request):
            """Çoklu sembol fiyatları."""
            syms_raw = request.rel_url.query.get("symbols","")
            syms = [s.strip().upper() for s in syms_raw.split(",") if s.strip()]
            result = {"prices":{}}
            if not syms:
                return aiohttp_web.Response(text=_json.dumps(result),
                    content_type="application/json", headers=CORS_HEADERS)
            try:
                async with aiohttp.ClientSession() as s:
                    sym_json = _json.dumps(syms)
                    async with s.get(f"https://api.binance.com/api/v3/ticker/price?symbols={sym_json}",
                                     timeout=aiohttp.ClientTimeout(total=8)) as r:
                        data = await r.json()
                for item in data:
                    result["prices"][item["symbol"]] = float(item.get("price",0))
            except Exception as e:
                log.warning(f"prices hata: {e}")
            return aiohttp_web.Response(text=_json.dumps(result),
                content_type="application/json", headers=CORS_HEADERS)

        async def handle_analiz(request):
            """Sunucu taraflı kapsamlı teknik analiz — 20+ gösterge."""
            sym = request.rel_url.query.get("symbol","BTCUSDT").upper()

            def _rsi(closes, p=14):
                g,l2=[],[]
                for i in range(1,len(closes)):
                    d=closes[i]-closes[i-1];g.append(max(d,0));l2.append(max(-d,0))
                if len(g)<p: return 50.0
                ag=sum(g[:p])/p; al=sum(l2[:p])/p
                for i in range(p,len(g)): ag=(ag*(p-1)+g[i])/p; al=(al*(p-1)+l2[i])/p
                return round(100-100/(1+ag/al) if al else 100, 2)

            def _ema(closes, p):
                if len(closes)<p: return closes[-1]
                e=sum(closes[:p])/p; k=2/(p+1)
                for c in closes[p:]: e=c*k+e*(1-k)
                return e

            def _macd(closes, fast=12, slow=26, sig_p=9):
                if len(closes)<slow+sig_p: return 0,0,0
                ef=sum(closes[:fast])/fast; es=sum(closes[:slow])/slow
                kf=2/(fast+1); ks=2/(slow+1)
                macd_vals=[]
                for i,c in enumerate(closes):
                    ef=c*kf+ef*(1-kf); es=c*ks+es*(1-ks)
                    if i>=slow-1: macd_vals.append(ef-es)
                if len(macd_vals)<sig_p: return 0,0,0
                sg=sum(macd_vals[:sig_p])/sig_p; ks2=2/(sig_p+1)
                for m in macd_vals[sig_p:]: sg=m*ks2+sg*(1-ks2)
                hist=macd_vals[-1]-sg
                return round(macd_vals[-1],8), round(sg,8), round(hist,8)

            def _boll(closes, p=20, mult=2.0):
                if len(closes)<p: return closes[-1],closes[-1],closes[-1]
                w=closes[-p:]; mn=sum(w)/p
                std=(sum((c-mn)**2 for c in w)/p)**0.5
                return round(mn+mult*std,6), round(mn,6), round(mn-mult*std,6)

            def _stoch_rsi(closes, rsi_p=14, stoch_p=14):
                g,l2=[],[]
                for i in range(1,len(closes)):
                    d=closes[i]-closes[i-1]; g.append(max(d,0)); l2.append(max(-d,0))
                rsi_vals=[]
                ag=sum(g[:rsi_p])/rsi_p; al=sum(l2[:rsi_p])/rsi_p
                rsi_vals.append(100-100/(1+ag/al) if al else 100)
                for i in range(rsi_p, len(g)):
                    ag=(ag*(rsi_p-1)+g[i])/rsi_p; al=(al*(rsi_p-1)+l2[i])/rsi_p
                    rsi_vals.append(100-100/(1+ag/al) if al else 100)
                if len(rsi_vals)<stoch_p: return 50.0
                w=rsi_vals[-stoch_p:]; lo=min(w); hi=max(w)
                return round((rsi_vals[-1]-lo)/(hi-lo)*100,2) if hi>lo else 50.0

            def _vol_ratio(vols):
                if len(vols)<10: return 1.0
                avg=sum(vols[:-1])/len(vols[:-1])
                return round(vols[-1]/avg,2) if avg else 1.0

            def _atr(klines, p=14):
                trs=[]
                for i in range(1,len(klines)):
                    h=float(klines[i][2]); l=float(klines[i][3]); pc=float(klines[i-1][4])
                    trs.append(max(h-l, abs(h-pc), abs(l-pc)))
                if not trs: return 0
                return round(sum(trs[-p:])/min(p,len(trs)),6)

            def _fp(v):
                return f"${v:,.4f}" if v<1 else f"${v:,.2f}"

            try:
                async with aiohttp.ClientSession() as s:
                    r1h = await (await s.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=100",timeout=aiohttp.ClientTimeout(total=8))).json()
                    r4h = await (await s.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=4h&limit=100",timeout=aiohttp.ClientTimeout(total=8))).json()
                    r1d = await (await s.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=100",timeout=aiohttp.ClientTimeout(total=8))).json()
                    tic = await (await s.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",timeout=aiohttp.ClientTimeout(total=6))).json()

                c1=[float(x[4]) for x in r1h]; v1=[float(x[5]) for x in r1h]
                c4=[float(x[4]) for x in r4h]; v4=[float(x[5]) for x in r4h]
                cd=[float(x[4]) for x in r1d]
                cur=c1[-1]

                # ── RSI ──
                rsi1=_rsi(c1,14); rsi4=_rsi(c4,14); rsiD=_rsi(cd,14)
                rsi7_1h=_rsi(c1,7); rsi7_4h=_rsi(c4,7)

                # ── EMA ──
                e9_1h =_ema(c1,9);  e21_1h=_ema(c1,21)
                e9_4h =_ema(c4,9);  e21_4h=_ema(c4,21)
                e50_4h=_ema(c4,50); e200_4h=_ema(c4,200) if len(c4)>=200 else _ema(c4,len(c4))
                e50_1d=_ema(cd,50); e200_1d=_ema(cd,200) if len(cd)>=200 else _ema(cd,len(cd))

                # ── MACD ──
                macd1h, sig1h, hist1h = _macd(c1)
                macd4h, sig4h, hist4h = _macd(c4)

                # ── Bollinger ──
                bb_up1h, bb_mid1h, bb_lo1h = _boll(c1,20)
                bb_up4h, bb_mid4h, bb_lo4h = _boll(c4,20)
                bb_pct1h = round((cur-bb_lo1h)/(bb_up1h-bb_lo1h)*100,1) if bb_up1h!=bb_lo1h else 50

                # ── Stoch RSI ──
                srsi1h=_stoch_rsi(c1); srsi4h=_stoch_rsi(c4)

                # ── Hacim ──
                vol_ratio1h=_vol_ratio(v1); vol_ratio4h=_vol_ratio(v4)

                # ── ATR ──
                atr1h=_atr(r1h); atr4h=_atr(r4h)
                atr_pct=round(atr1h/cur*100,2) if cur else 0

                # ── Performans ──
                ch1h  = round((c1[-1]-c1[-2])/c1[-2]*100,2) if len(c1)>=2 else 0
                ch4h  = round((c1[-1]-c1[-5])/c1[-5]*100,2) if len(c1)>=5 else 0
                ch24h = float(tic.get("priceChangePercent",0))
                ch7d  = round((cd[-1]-cd[-8])/cd[-8]*100,2) if len(cd)>=8 else 0

                # ── Destek / Direnç ──
                highs4h=[float(x[2]) for x in r4h]; lows4h=[float(x[3]) for x in r4h]
                res=min((h for h in highs4h if h>cur), default=None)
                sup=max((l for l in lows4h if l<cur), default=None)

                # ══════════════════════════════════
                # SİNYAL HESAPLAMA — 20 gösterge
                # ══════════════════════════════════
                sc=0; tot=0; signals=[]

                def sig(cat, label, bull, val, w=1):
                    nonlocal sc,tot
                    sc+=(w if bull else -w); tot+=w
                    signals.append({"cat":cat,"label":label,"bull":bull,"val":val,"w":w})

                # TREND (ağırlık 2-3)
                sig("trend","EMA9 vs EMA21 (1s)",  e9_1h>e21_1h,  f"9:{_fp(e9_1h)} / 21:{_fp(e21_1h)}",2)
                sig("trend","EMA9 vs EMA21 (4s)",  e9_4h>e21_4h,  f"9:{_fp(e9_4h)} / 21:{_fp(e21_4h)}",3)
                sig("trend","Fiyat vs EMA50 (4s)", cur>e50_4h,    _fp(e50_4h),2)
                sig("trend","Fiyat vs EMA200 (4s)",cur>e200_4h,   _fp(e200_4h),3)
                sig("trend","Fiyat vs EMA50 (1g)", cur>e50_1d,    _fp(e50_1d),2)
                sig("trend","Fiyat vs EMA200 (1g)",cur>e200_1d,   _fp(e200_1d),3)

                # MOMENTUM (ağırlık 1-2)
                sig("momentum","RSI 14 (1s)",  rsi1>50, str(round(rsi1)),1)
                sig("momentum","RSI 14 (4s)",  rsi4>50, str(round(rsi4)),2)
                sig("momentum","RSI 14 (1g)",  rsiD>50, str(round(rsiD)),2)
                sig("momentum","RSI 7 (1s)",   rsi7_1h>50, str(round(rsi7_1h)),1)
                sig("momentum","MACD Hist (1s)",hist1h>0, f"{hist1h:+.6f}",1)
                sig("momentum","MACD Hist (4s)",hist4h>0, f"{hist4h:+.6f}",2)
                sig("momentum","Stoch RSI (1s)",srsi1h>50, f"{round(srsi1h)}",1)
                sig("momentum","Stoch RSI (4s)",srsi4h>50, f"{round(srsi4h)}",2)

                # OSİLATÖR — özel koşullar
                if rsi1<30:  sig("osc","RSI 1s Aşırı Satım 🟢",True, str(round(rsi1)),2)
                elif rsi1>70:sig("osc","RSI 1s Aşırı Alım 🔴",False,str(round(rsi1)),2)
                if rsi4<30:  sig("osc","RSI 4s Aşırı Satım 🟢",True, str(round(rsi4)),3)
                elif rsi4>70:sig("osc","RSI 4s Aşırı Alım 🔴",False,str(round(rsi4)),3)
                sig("osc","Bollinger %B (1s)", bb_pct1h>50, f"%{bb_pct1h}",1)
                sig("osc","Bollinger %B (4s)", (cur-bb_lo4h)/(bb_up4h-bb_lo4h)*100>50 if bb_up4h!=bb_lo4h else False,
                    f"%{round((cur-bb_lo4h)/(bb_up4h-bb_lo4h)*100,1) if bb_up4h!=bb_lo4h else 50}",1)

                # HACİM
                sig("volume","Hacim Artışı (1s)", vol_ratio1h>1.0, f"{vol_ratio1h}x",1)
                sig("volume","Hacim Artışı (4s)", vol_ratio4h>1.0, f"{vol_ratio4h}x",1)

                # PERFORMANS
                sig("perf","Değişim 1s",  ch1h>0,  f"{ch1h:+.2f}%",1)
                sig("perf","Değişim 4s",  ch4h>0,  f"{ch4h:+.2f}%",1)
                sig("perf","Değişim 24s", ch24h>0, f"{ch24h:+.2f}%",2)
                sig("perf","Değişim 7g",  ch7d>0,  f"{ch7d:+.2f}%",2)

                score=max(0, min(100, round((sc/tot)*50+50))) if tot else 50
                bull_cnt=sum(1 for s in signals if s["bull"])
                bear_cnt=len(signals)-bull_cnt

                return aiohttp_web.Response(
                    text=_json.dumps({
                        "rsi1":rsi1,"rsi4":rsi4,"rsiD":rsiD,
                        "rsi7_1h":rsi7_1h,"rsi7_4h":rsi7_4h,
                        "macd1h":macd1h,"sig1h":sig1h,"hist1h":hist1h,
                        "macd4h":macd4h,"sig4h":sig4h,"hist4h":hist4h,
                        "srsi1h":srsi1h,"srsi4h":srsi4h,
                        "bb_up1h":bb_up1h,"bb_mid1h":bb_mid1h,"bb_lo1h":bb_lo1h,"bb_pct1h":bb_pct1h,
                        "bb_up4h":bb_up4h,"bb_mid4h":bb_mid4h,"bb_lo4h":bb_lo4h,
                        "e9_1h":e9_1h,"e21_1h":e21_1h,
                        "e9_4h":e9_4h,"e21_4h":e21_4h,"e50_4h":e50_4h,"e200_4h":e200_4h,
                        "e50_1d":e50_1d,"e200_1d":e200_1d,
                        "atr1h":atr1h,"atr4h":atr4h,"atr_pct":atr_pct,
                        "vol_ratio1h":vol_ratio1h,"vol_ratio4h":vol_ratio4h,
                        "ch1h":ch1h,"ch4h":ch4h,"ch24h":ch24h,"ch7d":ch7d,
                        "sup":sup,"res":res,"cur":cur,
                        "score":score,"bull_cnt":bull_cnt,"bear_cnt":bear_cnt,
                        "signals":signals
                    }),
                    content_type="application/json", headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"error":str(e)}),
                    content_type="application/json", headers=CORS_HEADERS)

        async def handle_fib(request):
            """Sunucu taraflı Fibonacci — genişletilmiş."""
            sym = request.rel_url.query.get("symbol","BTCUSDT").upper()
            iv  = request.rel_url.query.get("interval","4h")
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={iv}&limit=100",
                                     timeout=aiohttp.ClientTimeout(total=8)) as r:
                        data = await r.json()
                hs=[float(x[2]) for x in data]; ls=[float(x[3]) for x in data]
                cs=[float(x[4]) for x in data]; vs=[float(x[5]) for x in data]
                high=max(hs); low=min(ls); cur=cs[-1]; diff=high-low

                # Trend: son kapanış ilk kapanıştan yüksekse yukarı
                trend_up = cs[-1] > cs[0]

                # Retracement yüzdesi
                retrace_pct = round((high-cur)/(high-low)*100, 1) if trend_up else round((cur-low)/(high-low)*100, 1)

                lvls=[0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]
                levels=[]
                for l in lvls:
                    p = high-diff*l if trend_up else low+diff*l
                    dist = (p-cur)/cur*100
                    levels.append({"pct":round(l*100,1), "price":p, "dist":round(dist,2)})

                # Fiyata göre nearest sup/res fib seviyeleri
                prices_sorted = sorted(levels, key=lambda x: x["price"])
                nearest_sup = next((l for l in reversed(prices_sorted) if l["price"] <= cur), None)
                nearest_res = next((l for l in prices_sorted if l["price"] > cur), None)

                # Hangi zone'da?
                below = [l for l in levels if l["price"] <= cur]
                above = [l for l in levels if l["price"] > cur]
                zone_lo = max(below, key=lambda x: x["price"]) if below else levels[0]
                zone_hi = min(above, key=lambda x: x["price"]) if above else levels[-1]
                zone_label = f"%{zone_lo['pct']} — %{zone_hi['pct']}"

                # Fib range içinde fiyatın pozisyonu (0-100%)
                zone_range = zone_hi["price"] - zone_lo["price"]
                zone_pos = round((cur - zone_lo["price"]) / zone_range * 100, 1) if zone_range else 50

                # Momentum: son 5 mum hacim ortalaması vs önceki
                vol_now = sum(vs[-5:])/5 if len(vs)>=5 else vs[-1]
                vol_prev = sum(vs[-15:-5])/10 if len(vs)>=15 else vol_now
                vol_ratio = round(vol_now/vol_prev, 2) if vol_prev else 1.0

                return aiohttp_web.Response(
                    text=_json.dumps({
                        "high": high, "low": low, "cur": cur,
                        "trend_up": trend_up,
                        "retrace_pct": retrace_pct,
                        "zone_label": zone_label,
                        "zone_pos": zone_pos,
                        "zone_lo": zone_lo,
                        "zone_hi": zone_hi,
                        "nearest_sup": nearest_sup,
                        "nearest_res": nearest_res,
                        "vol_ratio": vol_ratio,
                        "levels": levels
                    }),
                    content_type="application/json", headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"error":str(e)}),
                    content_type="application/json", headers=CORS_HEADERS)

        web_app = aiohttp_web.Application()
        web_app.router.add_get("/",                   handle_index)
        web_app.router.add_get("/miniapp",            handle_index)
        web_app.router.add_get("/health",             handle_health)
        web_app.router.add_get("/api/proxy",          handle_proxy)
        web_app.router.add_get("/api/news",           handle_news)
        web_app.router.add_get("/api/favorites",      handle_favorites)
        web_app.router.add_get("/api/alarms",         handle_alarms)
        web_app.router.add_get("/api/kar_pozisyon",   handle_kar_pozisyon)
        web_app.router.add_get("/api/kar_kaydet",     handle_kar_kaydet)
        web_app.router.add_get("/api/kar_sil",        handle_kar_sil)
        async def handle_alarm_ekle_api(request):
            uid_str   = request.rel_url.query.get("uid","")
            symbol    = request.rel_url.query.get("symbol","").upper()
            alarm_type= request.rel_url.query.get("type","percent")
            try: threshold = float(request.rel_url.query.get("threshold","0"))
            except Exception: return aiohttp_web.Response(text='{"ok":false,"error":"invalid threshold"}',content_type="application/json",headers=CORS_HEADERS)
            if not uid_str or not uid_str.isdigit() or not symbol or threshold<=0:
                return aiohttp_web.Response(text='{"ok":false,"error":"missing params"}',content_type="application/json",headers=CORS_HEADERS)
            if alarm_type not in ("percent","price"):
                alarm_type = "percent"
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO user_alarms(user_id,symbol,threshold,alarm_type,active)
                           VALUES($1,$2,$3,$4,1)
                           ON CONFLICT(user_id,symbol) DO UPDATE
                           SET threshold=$3,alarm_type=$4,active=1,paused_until=NULL""",
                        int(uid_str), symbol, threshold, alarm_type
                    )
                return aiohttp_web.Response(text='{"ok":true}',content_type="application/json",headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"ok":False,"error":str(e)}),content_type="application/json",headers=CORS_HEADERS)

        async def handle_alarm_sil_api(request):
            uid_str = request.rel_url.query.get("uid","")
            symbol  = request.rel_url.query.get("symbol","").upper()
            if not uid_str or not uid_str.isdigit() or not symbol:
                return aiohttp_web.Response(text='{"ok":false,"error":"missing params"}',content_type="application/json",headers=CORS_HEADERS)
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute("DELETE FROM user_alarms WHERE user_id=$1 AND symbol=$2",int(uid_str),symbol)
                return aiohttp_web.Response(text='{"ok":true}',content_type="application/json",headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"ok":False,"error":str(e)}),content_type="application/json",headers=CORS_HEADERS)

        async def handle_klines(request):
            sym = request.rel_url.query.get("symbol","BTCUSDT").upper()
            iv  = request.rel_url.query.get("interval","1h")
            lim = min(int(request.rel_url.query.get("limit","80")),200)
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={iv}&limit={lim}",
                        timeout=aiohttp.ClientTimeout(total=8)) as r:
                        data = await r.json()
                # [open_time, open, high, low, close, volume, ...]
                klines = [[float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5])] for k in data]
                return aiohttp_web.Response(
                    text=_json.dumps({"klines":klines}),
                    content_type="application/json", headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"error":str(e)}),
                    content_type="application/json", headers=CORS_HEADERS)

        import xml.etree.ElementTree as _ET_news
        import re as _re_news
        import html as _html_news

        def _clean_html_text(text):
            t = _html_news.unescape(text or "").replace("<![CDATA[","").replace("]]>","")
            return _re_news.sub(r"<[^>]+>", "", t).strip()

        async def handle_coin_news(request):
            sym = request.rel_url.query.get("symbol","BTC").upper().replace("USDT","")
            feeds = [
                (f"https://cointelegraph.com/rss/tag/{sym.lower()}", "CoinTelegraph"),
                ("https://cointelegraph.com/rss", "CoinTelegraph"),
            ]
            items = await _fetch_rss(feeds, max_per=8)
            items = await _translate_news_items(items[:6])
            return aiohttp_web.Response(
                text=_json.dumps({"news": items}),
                content_type="application/json", headers=CORS_HEADERS)

        async def handle_takvim_news(request):
            # Dinamik tarihler — tekrarlayan ekonomik olayları gelecek tarihlerle hesapla
            from datetime import date as _date
            _today = _date.today()
            _year = _today.year
            _month = _today.month

            def _next_event_date(day, months_ahead=0):
                """Bir sonraki olay tarihini hesapla. Geçmişteyse gelecek aya ata."""
                try:
                    target_month = _month + months_ahead
                    target_year = _year
                    while target_month > 12:
                        target_month -= 12
                        target_year += 1
                    d = _date(target_year, target_month, min(day, 28))
                    if d < _today:
                        target_month += 1
                        if target_month > 12:
                            target_month = 1
                            target_year += 1
                        d = _date(target_year, target_month, min(day, 28))
                    return d.isoformat()
                except Exception:
                    return _today.isoformat()

            result = {
                "events": [
                    {"name":"FED Faiz Kararı","date":_next_event_date(7),"country":"ABD","importance":"high","forecast":"Sabit bekleniyor"},
                    {"name":"ABD TÜFE (Enflasyon)","date":_next_event_date(10),"country":"ABD","importance":"high","forecast":""},
                    {"name":"ABD Tarım Dışı İstihdam","date":_next_event_date(2),"country":"ABD","importance":"high","forecast":""},
                    {"name":"ECB Faiz Kararı","date":_next_event_date(17),"country":"Avrupa","importance":"high","forecast":""},
                    {"name":"ABD GSYİH (Büyüme)","date":_next_event_date(30),"country":"ABD","importance":"medium","forecast":""},
                    {"name":"Kripto Düzenleme Haberleri","date":"Sürekli","country":"Global","importance":"medium","forecast":""},
                ],
                "news": []
            }
            feeds = [
                ("https://cointelegraph.com/rss", "CoinTelegraph"),
                ("https://coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
            ]
            items = await _fetch_rss(feeds, max_per=5)
            items = await _translate_news_items(items[:8])
            result["news"] = items
            return aiohttp_web.Response(
                text=_json.dumps(result),
                content_type="application/json", headers=CORS_HEADERS)

        # handle_price'a change eklendi
        async def handle_price_with_change(request):
            sym = request.rel_url.query.get("symbol","BTCUSDT").upper()
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
                                     timeout=aiohttp.ClientTimeout(total=6)) as r:
                        d = await r.json()
                return aiohttp_web.Response(
                    text=_json.dumps({"price":float(d.get("lastPrice",0)),"change":float(d.get("priceChangePercent",0)),"volume":float(d.get("quoteVolume",0)),"high":float(d.get("highPrice",0)),"low":float(d.get("lowPrice",0))}),
                    content_type="application/json", headers=CORS_HEADERS)
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"error":str(e)}),
                    content_type="application/json", headers=CORS_HEADERS)

        _icon_cache = {}

        async def handle_icon(request):
            sym = request.rel_url.query.get("sym","btc").lower()
            import re as _re2
            if not _re2.match(r'^[a-z0-9]{1,12}$', sym):
                return aiohttp_web.Response(status=404)

            # Cache'den don
            if sym in _icon_cache:
                data, ct = _icon_cache[sym]
                return aiohttp_web.Response(body=data, content_type=ct,
                    headers={"Cache-Control":"public,max-age=604800","Access-Control-Allow-Origin":"*"})

            # Kaynak listesi - sirayla dene
            sources = [
                (f"https://cdn.jsdelivr.net/gh/vadimmalykhin/binance-icons/crypto/{sym}.svg", "image/svg+xml"),
                (f"https://cdn.jsdelivr.net/npm/cryptocurrency-icons@latest/32/color/{sym}.png", "image/png"),
                (f"https://cdn.jsdelivr.net/npm/cryptocurrency-icons@latest/svg/color/{sym}.svg", "image/svg+xml"),
                (f"https://assets.coingecko.com/coins/images/1/thumb/{sym}.png", "image/png"),
            ]
            for url, ct in sources:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url,
                            headers={"User-Agent":"Mozilla/5.0"},
                            timeout=aiohttp.ClientTimeout(total=4)) as r:
                            if r.status == 200:
                                data = await r.read()
                                if len(data) > 100:  # Bos dosya degil
                                    _icon_cache[sym] = (data, ct)
                                    return aiohttp_web.Response(body=data, content_type=ct,
                                        headers={"Cache-Control":"public,max-age=604800","Access-Control-Allow-Origin":"*"})
                except Exception:
                    pass
            return aiohttp_web.Response(status=404)

        web_app.router.add_get("/api/dashboard",      handle_dashboard)
        web_app.router.add_get("/api/icon",           handle_icon)
        web_app.router.add_get("/api/price",          handle_price_with_change)
        web_app.router.add_get("/api/prices",         handle_prices)
        web_app.router.add_get("/api/analiz",         handle_analiz)
        web_app.router.add_get("/api/fib",            handle_fib)
        web_app.router.add_get("/api/klines",         handle_klines)
        web_app.router.add_get("/api/coin_news",      handle_coin_news)
        web_app.router.add_get("/api/takvim_news",    handle_takvim_news)
        web_app.router.add_get("/api/alarm_ekle",     handle_alarm_ekle_api)
        web_app.router.add_get("/api/alarm_sil",      handle_alarm_sil_api)

        runner = aiohttp_web.AppRunner(web_app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        # Railway domain tespiti — birden fazla env var dene
        domain = (
            os.getenv("RAILWAY_PUBLIC_DOMAIN") or
            os.getenv("RAILWAY_STATIC_URL","").replace("https://","").replace("http://","") or
            os.getenv("RAILWAY_SERVICE_URL","").replace("https://","").replace("http://","") or
            ""
        ).strip("/")

        if domain:
            MINIAPP_URL = f"https://{domain}"
            log.info(f"✅ Mini App aktif: {MINIAPP_URL}")
        else:
            log.info(f"✅ Mini App sunucu başladı port {port} — Railway domain henüz yok")
            log.info("💡 Railway → Settings → Networking → Generate Domain yapın, ardından MINIAPP_URL variable ekleyin")

    except Exception as e:
        log.warning(f"Mini App başlatılamadı: {e}")

# ================= WEBSOCKET =================

async def binance_engine():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    backoff = 5  # başlangıç bekleme süresi (saniye)
    max_backoff = 300  # maksimum 5 dakika
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20) as ws:
                log.info("Binance WebSocket bağlandı.")
                backoff = 5  # başarılı bağlantıda reset
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
                        # Volume cache güncelle (miniTicker "q" = 24h quote volume)
                        try:
                            volume_24h_cache[s] = float(c.get("q", 0))
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            log.error(f"WebSocket hatası: {e} — {backoff}s sonra yeniden bağlanılıyor.")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)  # Exponential backoff

async def post_init(app):
    await init_db()
    asyncio.create_task(binance_engine())
    await replay_pending_deletes(app.bot)

    # ── Mini App web sunucusu (bot ile aynı process) ──
    asyncio.create_task(_start_miniapp_server(app.bot))

    from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats

    # ── Private chat komutları (tüm kullanıcılar) ──
    private_commands = [
        BotCommand("start",          "Botu başlat / Ana menü"),
        BotCommand("dashboard",      "📊 Canlı kripto dashboard (Mini App)"),
        BotCommand("hedef",          "Fiyat hedefi ekle / listele"),
        BotCommand("alarmim",        "Kişisel alarmlarım"),
        BotCommand("alarm_ekle",     "Yeni alarm ekle"),
        BotCommand("alarm_sil",      "Alarm sil"),
        BotCommand("alarm_duraklat", "Alarmı duraklat"),
        BotCommand("alarm_gecmis",   "Alarm geçmişi"),
        BotCommand("favori",         "Favori coinler"),
        BotCommand("mtf",            "Gelişmiş MTF analiz"),
        BotCommand("fib",            "Fibonacci retracement analizi"),
        BotCommand("sentiment",      "Coin sentiment / duygu analizi"),
        BotCommand("takvim",         "Ekonomik takvim & FOMC/CPI takibi"),
        BotCommand("ne",             "Kripto terim sözlüğü"),
        BotCommand("zamanla",        "Zamanlanmış görev"),
        BotCommand("kar",            "Kar/zarar hesabı"),
        BotCommand("top24",          "24s liderleri"),
        BotCommand("top5",           "5dk hareketliler"),
        BotCommand("market",         "Piyasa duyarlılığı"),
        BotCommand("status",         "Bot durumu"),
        BotCommand("istatistik",     "Bot istatistikleri (sadece admin)"),
    ]
    await app.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())

    # ── Grup komutları: hiç komut gösterilmesin ──
    await app.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())

# ================= MAIN =================

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.job_queue.run_repeating(alarm_job,            interval=30,   first=30)
    app.job_queue.run_repeating(whale_job,            interval=120,  first=60)
    app.job_queue.run_repeating(scheduled_job,        interval=60,   first=10)
    app.job_queue.run_repeating(hedef_job,            interval=30,   first=45)
    app.job_queue.run_repeating(marketcap_refresh_job,interval=600,  first=5)
    # Her gün 08:00 UTC - ekonomik takvim bildirimi
    app.job_queue.run_daily(takvim_job, time=dtime(8, 0))

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("dashboard",      dashboard_command))
    app.add_handler(CommandHandler("istatistik",     istatistik))
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
    # Yeni komutlar
    app.add_handler(CommandHandler("fib",            fib_command))
    app.add_handler(CommandHandler("sentiment",      sentiment_command))
    app.add_handler(CommandHandler("ne",             ne_command))
    app.add_handler(CommandHandler("takvim",         takvim_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_symbol))

    log.info("BOT AKTIF")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
