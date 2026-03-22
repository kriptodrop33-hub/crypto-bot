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
GROUP_CHAT_ID = int(os.getenv("GROUP_ID"))
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

    log.info("PostgreSQL baglantisi kuruldu.")

# ================= MEMORY =================

price_memory:       dict = {}
cooldowns:          dict = {}
chart_cache:        dict = {}
whale_vol_mem:      dict = {}
scheduled_last_run: dict = {}
coin_image_cache:   dict = {}

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
            figcolor="#0d1117", gridcolor="#21262d", gridstyle="--",
            rc={"axes.labelcolor":"#8b949e","xtick.color":"#8b949e",
                "ytick.color":"#8b949e","font.size":8}
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
            f"📐 *{symbol} — Fibonacci Retracement* ({interval})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{trend_icon} *Trend:* {trend_lbl}  |  *Retracement:* `%{retrace_pct}`\n"
            f"📊 *Swing High:* `{fp(swing_high)}` USDT\n"
            f"📊 *Swing Low:*  `{fp(swing_low)}` USDT\n"
            f"🔵 *Mevcut Fiyat:* `{fp(cur)}` USDT\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )

        # Destek / Direnç özeti
        if sup_fib:
            dist_sup = round((cur - sup_fib[1]) / cur * 100, 2)
            text += f"🟢 *Destek:* Fib `{sup_fib[0]:.3f}` → `{fp(sup_fib[1])}` USDT  `(-{dist_sup}%)`\n"
        if res_fib:
            dist_res = round((res_fib[1] - cur) / cur * 100, 2)
            text += f"🔴 *Direnç:* Fib `{res_fib[0]:.3f}` → `{fp(res_fib[1])}` USDT  `(+{dist_res}%)`\n"
        text += f"━━━━━━━━━━━━━━━━━━━━━\n"

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
                marker = "  "
            text += f"{marker} `{lvl:.3f}` → `{lp}` USDT  {dist_str}\n"

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
    msg = await context.bot.send_photo(chat_id=chat.id, photo=buf, caption=text,
                                        parse_mode="Markdown", reply_markup=keyboard)
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
                                except: pass
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
            "🧠 *Sentiment Kullanımı:*\n`/sentiment BTCUSDT`\n`/sentiment ETH`",
            parse_mode="Markdown")
        return
    await register_user(update)
    symbol = args[0].upper().replace("#","").replace("/","")
    if not symbol.endswith("USDT"): symbol += "USDT"

    loading = await send_temp(context.bot, chat.id,
        f"🧠 `{symbol}` haber analizi yapılıyor...", parse_mode="Markdown")
    result  = await fetch_sentiment(symbol)
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    bar = "🟩" * int(result["score"]*10) + "⬜" * (10 - int(result["score"]*10))
    text = (
        f"🧠 *{symbol} — Sentiment Analizi*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💭 *Genel Duygu:* {result['label']}\n"
        f"📊 *Skor:* `{result['score']:.2f}` / 1.00\n"
        f"{bar}\n"
        f"📰 *Haber Sayısı:* `{result['news_count']}`\n"
        f"🔍 *Kaynak:* `{result['source']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 _{result['summary']}_\n"
        f"⏰ _{datetime.utcnow().strftime('%H:%M UTC')}_"
    )
    is_group = chat.type in ("group", "supergroup")
    delay    = (await get_member_delete_delay()) if is_group else None
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",  callback_data=f"sent_{symbol}"),
        InlineKeyboardButton("📊 Analiz",  callback_data=f"analyse_{symbol}"),
    ]])
    msg = await context.bot.send_message(chat.id, text, parse_mode="Markdown", reply_markup=keyboard)
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
            f"📚 *Kripto Terim Sözlüğü*\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"Kullanım: `/ne MACD`\n\n📖 *Terimler:*\n{terimler}",
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
            text = f"❓ `{arama}` bulunamadı.\n\nMevcut terimler:\n{terimler}"

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
        log.warning(f"Takvim cevirisi basarisiz: {e}")
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
                        "Bu özelliği DM üzerinden kullanabilirsiniz.\n\n"
                        "Aşağıdaki butona tıklayarak takvimi görebilir "
                        "veya Dashboard Mini App'i açabilirsiniz 👇"
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
    loading = await send_temp(context.bot, chat.id, "📅 Ekonomik takvim yükleniyor...", parse_mode="Markdown")
    events  = await fetch_crypto_calendar()
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    now = datetime.utcnow()
    text = "📅 *EKONOMİK & KRİPTO TAKVİM*\n━━━━━━━━━━━━━━━━━━━━━\n"
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
    text += f"\n━━━━━━━━━━━━━━━━━━━━━\n🔴 Yüksek  🟡 Orta  🟢 Düşük etki\n⏰ _{now.strftime('%d.%m.%Y %H:%M')} UTC_"

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
        except:
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
        except:
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
        except:
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
    except:
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
    except:
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
        except:
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
        except:
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
            "📅 *Haftalık Kripto Raporu*\n━━━━━━━━━━━━━━━━━━\n"
            "🗓 " + now_str + " · " + mood + "\n"
            "📊 Ort. Değişim: `" + ("%+.2f" % avg) + "%`\n\n"
            "🚀 *En Çok Yükselen 5*\n"
        )
        for i, c in enumerate(top5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🟢 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n📉 *En Çok Düşen 5*\n"
        for i, c in enumerate(bot5, 1):
            text += get_number_emoji(i) + " `" + c["symbol"] + "` 🔴 `" + ("%+.2f" % float(c["priceChangePercent"])) + "%`\n"
        text += "\n_İyi haftalar! 🎯_"
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
        # Grup için tüm butonlar + DM yönlendirme butonu
        group_full_buttons = [
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
            [InlineKeyboardButton("➡️ Bota DM At (Tüm Özellikler)", url=f"https://t.me/{BOT_USERNAME}?start=hello")],
        ]
        # Mini App butonu: grupta web_app desteklenmez, callback ile DM'e yönlendir
        _murl_group = get_miniapp_url()
        if _murl_group:
            group_full_buttons.insert(len(group_full_buttons) - 1, [InlineKeyboardButton(
                "🖥 Dashboard Mini App", callback_data="miniapp_dm"
            )])

        keyboard    = InlineKeyboardMarkup(group_full_buttons)
        welcome_text = (
            "👋 *Kripto Analiz Asistanı*\n━━━━━━━━━━━━━━━━━━\n"
            "7/24 piyasayı izliyorum.\n\n"
            "💡 *Analiz:* `BTCUSDT` yaz\n"
            "🔔 *Alarm:* `/alarm_ekle BTCUSDT 3.5`\n"
            "🎯 *Hedef:* `/hedef BTCUSDT 70000`\n"
            "📐 *Fibonacci:* `/fib BTCUSDT`\n"
            "🧠 *Sentiment:* `/sentiment BTCUSDT`\n"
            "📅 *Takvim:* `/takvim`\n"
            "💰 *Kar/Zarar:* `/kar BTCUSDT 0.5 60000`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📢 *Topluluğumuza katıl:*\n"
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
            "👋 *Kripto Analiz Asistanı*\n━━━━━━━━━━━━━━━━━━\n"
            "7/24 piyasayı izliyorum.\n\n"
            "💡 *Analiz:* `BTCUSDT` yaz\n"
            "🔔 *Alarm:* `/alarm_ekle BTCUSDT 3.5`\n"
            "🎯 *Hedef:* `/hedef BTCUSDT 70000`\n"
            "📐 *Fibonacci:* `/fib BTCUSDT`\n"
            "🧠 *Sentiment:* `/sentiment BTCUSDT`\n"
            "📅 *Takvim:* `/takvim`\n"
            "📚 *Sözlük:* `/ne MACD`\n"
            "💰 *Kar/Zarar:* `/kar BTCUSDT 0.5 60000`\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📢 *Topluluğumuza katıl:*\n"
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
    msg_text = f"{status_emoji} *Piyasa Duyarliligi:* `%{avg:+.2f}`"
    chat = update.effective_chat
    is_group = chat and chat.type in ("group", "supergroup")
    is_cb = bool(update.callback_query)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",     callback_data="market"),
        InlineKeyboardButton("📈 24s Lider",  callback_data="top24"),
        InlineKeyboardButton("⚡ 5dk Flash",  callback_data="top5"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    sent = await target.reply_text(msg_text, parse_mode="Markdown", reply_markup=keyboard)
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
    text += "🟢 *YÜKSELENLER*\n"
    for i, (c, pct) in enumerate(top_gainers, 1):
        text += f"{get_number_emoji(i)} 🟢▲ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
    text += "\n🔴 *DÜŞENLER*\n"
    for i, (c, pct) in enumerate(top_losers, 1):
        text += f"{get_number_emoji(i)} 🔴▼ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"

    kb24 = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yenile",    callback_data="top24"),
        InlineKeyboardButton("⚡ 5dk Flash", callback_data="top5"),
        InlineKeyboardButton("📊 Market",    callback_data="market"),
    ]])
    target = update.callback_query.message if is_cb else update.message
    msg = await target.reply_text(text, parse_mode="Markdown", reply_markup=kb24)
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

        text = "⚡ *Piyasanın En Hareketlileri (24s baz)*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🟢 *YÜKSELENLER*\n"
        for i, c in enumerate(positives, 1):
            pct = float(c["priceChangePercent"])
            text += f"{get_number_emoji(i)} 🟢▲ `{c['symbol']:<12}` `%{pct:+6.2f}`\n"
        text += "\n🔴 *DÜŞENLER*\n"
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

        text = "⚡ *Son 5 Dakikanın En Hareketlileri*\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += "🟢 *YÜKSELENLER — En Hızlı 5*\n"
        for i, (s, c) in enumerate(positives, 1):
            text += f"{get_number_emoji(i)} 🟢▲ `{s:<12}` `%{c:+6.2f}`\n"
        if not positives:
            text += "_Yükseliş yok_\n"
        text += "\n🔴 *DÜŞENLER — En Hızlı 5*\n"
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
    msg = await target.reply_text(text, parse_mode="Markdown", reply_markup=kb5)
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
        "ℹ️ *Sistem Yapılandırması*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *Alarm Durumu:* `{'AKTIF' if r['alarm_active'] else 'KAPALI'}`\n"
        f"🎯 *Eşik Değeri:* `%{r['threshold']}`\n"
        f"🔄 *İzleme Modu:* `{r['mode'].upper()}`\n"
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
    sent = await target.reply_text(text, parse_mode="Markdown", reply_markup=kb_st)
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
                    chat_id=q.message.chat.id, photo=buf,
                    caption=text, parse_mode="Markdown", reply_markup=keyboard
                )
        elif q.data == "fib_help":
            await q.message.reply_text(
                "📐 *Fibonacci Retracement*\n━━━━━━━━━━━━━━━━━━━━━\n"
                "Kullanım: `/fib BTCUSDT`\n"
                "Zaman dilimleri: `1h` `4h` `1d` `1w`\n\n"
                "Fibonacci seviyeleri destek/direnç tahmini için kullanılır.\n"
                "📖 Detaylı bilgi: `/ne fibonacci`",
                parse_mode="Markdown"
            )
        return

    # Sentiment callback
    if q.data.startswith("sent_"):
        await q.answer()
        if q.data == "sent_help":
            await q.message.reply_text(
                "🧠 *Sentiment Analizi*\n━━━━━━━━━━━━━━━━━━━━━\n"
                "Kullanım: `/sentiment BTCUSDT`\n\n"
                "Haber ve topluluk verilerinden coin duygu analizi yapılır.\n"
                "Groq AI + CryptoPanic entegrasyonu ile çalışır.",
                parse_mode="Markdown"
            )
        elif q.data.startswith("sent_") and len(q.data) > 5:
            symbol = q.data[5:]
            loading_msg = await q.message.reply_text(f"🧠 `{symbol}` analiz ediliyor...")
            result = await fetch_sentiment(symbol)
            try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
            except Exception: pass
            bar  = "🟩" * int(result["score"]*10) + "⬜" * (10 - int(result["score"]*10))
            text = (
                f"🧠 *{symbol} — Sentiment Analizi*\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"💭 *Genel Duygu:* {result['label']}\n"
                f"📊 *Skor:* `{result['score']:.2f}` / 1.00\n{bar}\n"
                f"📰 *Haber:* `{result['news_count']}`  🔍 *Kaynak:* `{result['source']}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n💬 _{result['summary']}_"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Yenile", callback_data=f"sent_{symbol}"),
                InlineKeyboardButton("📊 Analiz",  callback_data=f"analyse_{symbol}"),
            ]])
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
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
            except: pass
            await q.message.reply_text(f"⚠️ Veri alınamadı: {e}")
            return

        try: await context.bot.delete_message(q.message.chat.id, loading_msg.message_id)
        except: pass

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
            f"🔍 *{symbol} — Teknik Analiz*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Fiyat:* `${fmt(price)}`  {pct_icon} `{pct24:+.2f}%`\n"
            f"📊 *24s Hacim:* `${vol24/1e6:.1f}M`\n"
            f"📈 *24s Yüksek:* `${fmt(high24)}`\n"
            f"📉 *24s Düşük:*  `${fmt(low24)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔮 *RSI (14):* `{rsi}` — {rsi_label}\n"
            f"📐 *EMA 20:* `${fmt(ema20)}` — {trend}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ _{datetime.utcnow().strftime('%H:%M UTC')}_"
        )
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
        import random
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
        "ne_help", "miniapp_dm",
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
            await q.message.reply_text("🗑 Tum favorileriniz silindi.")

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
            log.warning(f"Kişisel alarm gönderilemedi ({user_id}): {e}")

# ================= MINI APP WEB SUNUCUSU =================

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Kripto Drop Pro</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@400;500;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#060a12;--surface:#0b1220;--card:#0f1a2e;--card2:#131f33;--card3:#17263d;
  --border:#1a2d47;--border2:#243d5e;--text:#e8f4ff;--muted:#4a6a8a;--muted2:#3a5570;
  --g:#05d890;--gd:rgba(5,216,144,.1);--gs:rgba(5,216,144,.18);
  --r:#ff2d55;--rd:rgba(255,45,85,.1);--rs:rgba(255,45,85,.18);
  --y:#ffd60a;--yd:rgba(255,214,10,.1);
  --b:#0a84ff;--b2:#64b5f6;--bd:rgba(10,132,255,.12);--bs:0 0 20px rgba(10,132,255,.25);
  --p:#bf5af2;--o:#ff9f0a;--t:#5ac8fa;
  --nav-w:60px;--radius:14px;--radius-sm:10px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{font-family:'DM Sans',system-ui,sans-serif;color:var(--text);font-size:13px}
#app{height:100dvh;display:flex;flex-direction:column;overflow:hidden}
#app::before{content:'';position:fixed;top:-40%;left:-20%;width:60%;height:70%;background:radial-gradient(ellipse,rgba(10,132,255,.06) 0%,transparent 70%);pointer-events:none;z-index:0}
#app::after{content:'';position:fixed;bottom:-30%;right:-10%;width:50%;height:60%;background:radial-gradient(ellipse,rgba(5,216,144,.03) 0%,transparent 70%);pointer-events:none;z-index:0}
#main{flex:1;display:flex;overflow:hidden;position:relative;z-index:1}

/* HEADER */
.hdr{height:50px;flex-shrink:0;background:rgba(11,18,32,.96);border-bottom:1px solid rgba(255,255,255,.07);display:flex;align-items:center;justify-content:space-between;padding:0 14px;backdrop-filter:blur(24px);position:relative;z-index:100}
.hdr::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(10,132,255,.3),transparent)}
.logo{display:flex;align-items:center;gap:10px}
.logo-box{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#0a84ff,#005fcc);box-shadow:0 0 16px rgba(10,132,255,.4);display:flex;align-items:center;justify-content:center;font-size:16px}
.logo-txt{font-family:'Space Mono',monospace;font-size:13px;font-weight:700;background:linear-gradient(135deg,#64b5f6,#5ac8fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-sub{font-size:7px;color:var(--muted);font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-top:1px}
.hdr-r{display:flex;align-items:center;gap:8px}
.live-pill{display:flex;align-items:center;gap:5px;background:rgba(5,216,144,.08);border:1px solid rgba(5,216,144,.22);border-radius:20px;padding:4px 10px}
.ldot{width:6px;height:6px;border-radius:50%;background:var(--g);animation:blink 1.4s ease infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1;box-shadow:0 0 8px var(--g)}50%{opacity:.3;box-shadow:none}}
.ltxt{font-size:9px;font-weight:700;color:var(--g);letter-spacing:.8px;font-family:'Space Mono',monospace}
#clk{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace;letter-spacing:.3px}

/* TICKER */
.ticker{height:26px;flex-shrink:0;background:rgba(6,10,18,.97);border-bottom:1px solid rgba(255,255,255,.05);display:flex;align-items:center;overflow:hidden;position:relative;z-index:10}
.ticker::before,.ticker::after{content:'';position:absolute;top:0;bottom:0;width:32px;z-index:2;pointer-events:none}
.ticker::before{left:0;background:linear-gradient(90deg,var(--bg),transparent)}
.ticker::after{right:0;background:linear-gradient(-90deg,var(--bg),transparent)}
.t-inner{display:flex;animation:ts 42s linear infinite}
.t-inner:hover{animation-play-state:paused}
@keyframes ts{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.t-item{display:flex;align-items:center;gap:5px;padding:0 13px;border-right:1px solid rgba(255,255,255,.04);white-space:nowrap;height:26px;flex-shrink:0;cursor:pointer;transition:background .12s}
.t-item:hover{background:rgba(255,255,255,.03)}
.t-sym{font-size:9px;font-weight:700;color:var(--muted);font-family:'Space Mono',monospace}
.t-px{font-size:10px;font-weight:700;font-family:'Space Mono',monospace}
.t-ch{font-size:8px;font-weight:700;margin-left:1px}

/* SIDEBAR */
#sidenav{width:var(--nav-w);flex-shrink:0;background:rgba(8,13,24,.97);border-right:1px solid rgba(255,255,255,.05);display:flex;flex-direction:column;align-items:center;padding:10px 0;gap:2px;overflow-y:auto;scrollbar-width:none;position:relative;z-index:10;touch-action:none}
#sidenav::-webkit-scrollbar{display:none}
#sidenav::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,rgba(10,132,255,.5),transparent)}
.nb{width:50px;height:52px;border-radius:13px;background:transparent;border:none;color:var(--muted2);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:7.5px;font-weight:700;transition:all .2s;padding:0;letter-spacing:.4px;text-transform:uppercase;position:relative;-webkit-tap-highlight-color:transparent;outline:none;user-select:none;-webkit-user-select:none;touch-action:manipulation}
.nb *{pointer-events:none}
.nb:active{transform:scale(.86)}
.nb.on{background:rgba(10,132,255,.14);color:var(--b2);box-shadow:inset 0 0 0 1px rgba(10,132,255,.28)}
.nb.on::before{content:'';position:absolute;left:-1px;top:50%;transform:translateY(-50%);width:3px;height:55%;background:linear-gradient(180deg,var(--b),var(--t));border-radius:0 3px 3px 0;box-shadow:0 0 8px rgba(10,132,255,.5)}
.nb .ic{font-size:19px;line-height:1}
.nb-div{width:30px;height:1px;background:rgba(255,255,255,.05);margin:5px 0;flex-shrink:0;pointer-events:none}

/* SCROLL */
#scroll{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding-bottom:12px}
#scroll::-webkit-scrollbar{width:2px}
#scroll::-webkit-scrollbar-thumb{background:var(--border2);border-radius:1px}

/* PAGES */
.page{display:none;padding:12px;animation:fadeIn .2s ease}
.page.on{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* CARDS */
.card{background:var(--card);border:1px solid rgba(255,255,255,.06);border-radius:var(--radius);padding:14px;margin-bottom:10px;position:relative;overflow:hidden;backdrop-filter:blur(12px)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.09),transparent)}
.cg{border-color:rgba(5,216,144,.2);background:linear-gradient(145deg,var(--card) 65%,rgba(5,216,144,.04))}
.cb{border-color:rgba(10,132,255,.2);background:linear-gradient(145deg,var(--card) 65%,rgba(10,132,255,.04))}
.cy{border-color:rgba(255,214,10,.2);background:linear-gradient(145deg,var(--card) 65%,rgba(255,214,10,.04))}
.cp{border-color:rgba(191,90,242,.2);background:linear-gradient(145deg,var(--card) 70%,rgba(191,90,242,.03))}
.co{border-color:rgba(255,159,10,.2);background:linear-gradient(145deg,var(--card) 70%,rgba(255,159,10,.03))}
.cr-card{border-color:rgba(255,45,85,.2);background:linear-gradient(145deg,var(--card) 65%,rgba(255,45,85,.04))}

/* GRID */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}

/* STAT */
.sb{background:var(--card2);border:1px solid rgba(255,255,255,.05);border-radius:var(--radius-sm);padding:11px 7px;text-align:center;transition:border-color .15s}
.sv{font-size:15px;font-weight:800;line-height:1.1;font-family:'Space Mono',monospace}
.sl{font-size:8px;color:var(--muted);margin-top:4px;font-weight:600;letter-spacing:.3px;text-transform:uppercase}

/* COLORS */
.up{color:var(--g)}.dn{color:var(--r)}.nu{color:var(--y)}.bl{color:var(--b)}.or{color:var(--o)}

/* BADGE */
.bdg{display:inline-flex;align-items:center;padding:4px 10px;border-radius:8px;font-size:12px;font-weight:800;font-family:'Space Mono',monospace;letter-spacing:-.2px}
.bg{background:var(--gs);color:var(--g);border:1px solid rgba(5,216,144,.28)}
.br{background:var(--rs);color:var(--r);border:1px solid rgba(255,45,85,.28)}
.by{background:var(--yd);color:var(--y);border:1px solid rgba(255,214,10,.2)}
.bb{background:var(--bd);color:var(--b);border:1px solid rgba(10,132,255,.28)}

/* COIN ROW */
.cr{display:flex;align-items:center;gap:10px;padding:10px 6px;border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer;border-radius:var(--radius-sm);margin:0 -6px;transition:all .14s}
.cr:last-child{border-bottom:none}
.cr:active{background:rgba(255,255,255,.05);transform:scale(.99)}
.cico{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:900;border:1px solid;flex-shrink:0;font-family:'Space Mono',monospace;overflow:hidden}
.cinfo{flex:1;min-width:0}
.csym{font-size:13px;font-weight:800;letter-spacing:-.2px}
.cname{font-size:9px;color:var(--muted);margin-top:2px;font-family:'Space Mono',monospace}
.cr-r{text-align:right;flex-shrink:0}
.cpct{font-size:14px;font-weight:800;font-family:'Space Mono',monospace;letter-spacing:-.3px}
.cprice{font-size:11px;color:var(--muted);margin-top:2px;font-family:'Space Mono',monospace}
.crank{font-size:9px;color:var(--muted2);width:18px;text-align:center;flex-shrink:0;font-weight:700;font-family:'Space Mono',monospace}

/* SECTION HEADER */
.sh{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.sh-t{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.9px;color:var(--muted);display:flex;align-items:center;gap:6px;font-family:'Space Mono',monospace}
.sh-t span{color:var(--text);font-size:11px}
.sh-btn{font-size:10px;color:var(--b);font-weight:700;cursor:pointer;padding:5px 10px;border-radius:8px;border:1px solid rgba(10,132,255,.3);background:rgba(10,132,255,.08);transition:all .15s}
.sh-btn:active{background:rgba(10,132,255,.16)}

/* FORMS */
.row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.inp{flex:1;background:rgba(19,31,51,.9);border:1px solid rgba(255,255,255,.09);border-radius:var(--radius-sm);padding:13px 14px;color:var(--text);font-size:14px;font-family:'DM Sans',sans-serif;outline:none;min-width:0;transition:border-color .15s,box-shadow .15s}
.inp:focus{border-color:rgba(10,132,255,.55);box-shadow:0 0 0 3px rgba(10,132,255,.1)}
.inp::placeholder{color:var(--muted2)}
.sel{background:rgba(19,31,51,.9);border:1px solid rgba(255,255,255,.09);border-radius:var(--radius-sm);padding:13px 9px;color:var(--text);font-size:12px;font-family:'DM Sans',sans-serif;outline:none;flex-shrink:0}
.btn{background:linear-gradient(135deg,#0a84ff,#0058cc);border:none;border-radius:var(--radius-sm);padding:13px 18px;color:#fff;font-size:13px;font-weight:700;cursor:pointer;flex-shrink:0;box-shadow:0 4px 18px rgba(10,132,255,.35);transition:transform .12s,box-shadow .12s}
.btn:active{transform:scale(.95);box-shadow:0 2px 8px rgba(10,132,255,.2)}
.btn-g{background:linear-gradient(135deg,#05d890,#00a86b);box-shadow:0 4px 18px rgba(5,216,144,.28)}

.frow{display:flex;gap:6px;margin-bottom:11px;flex-wrap:wrap}
.fc{background:rgba(19,31,51,.8);border:1px solid rgba(255,255,255,.08);border-radius:20px;padding:7px 14px;font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;white-space:nowrap;transition:all .15s;outline:none;-webkit-appearance:none;font-family:'DM Sans',sans-serif;display:inline-flex;align-items:center;gap:4px}
.fc:active{transform:scale(.93)}
.fc:focus{outline:none}
.fc.on{background:rgba(10,132,255,.15);border-color:rgba(10,132,255,.4);color:var(--b2);box-shadow:0 0 12px rgba(10,132,255,.15)}

/* MISC */
.pb{background:rgba(255,255,255,.06);border-radius:6px;height:6px;overflow:hidden}
.pbf{height:6px;border-radius:6px;transition:width .7s cubic-bezier(.4,0,.2,1)}
.mt{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;gap:10px;opacity:.5}
.mt-i{font-size:40px}.mt-t{font-size:14px;font-weight:700}.mt-s{font-size:11px;color:var(--muted);text-align:center;line-height:1.5}
.spin{width:20px;height:20px;border:2px solid rgba(255,255,255,.06);border-top-color:var(--b);border-radius:50%;animation:sp .65s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.ld{display:flex;align-items:center;justify-content:center;gap:9px;padding:28px;color:var(--muted);font-size:11px}

/* SKELETON PULSE */
@keyframes skPulse{0%,100%{opacity:.3}50%{opacity:.7}}
.sk{background:rgba(255,255,255,.06);border-radius:6px;animation:skPulse 1.5s ease infinite}

/* TOAST */
#toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(14px);background:rgba(15,24,44,.97);border:1px solid rgba(255,255,255,.12);border-radius:26px;padding:10px 20px;font-size:12px;font-weight:700;opacity:0;transition:all .24s cubic-bezier(.4,0,.2,1);pointer-events:none;z-index:9999;white-space:nowrap;backdrop-filter:blur(24px);box-shadow:0 8px 32px rgba(0,0,0,.4)}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

/* KZ / ALARM TABS */
.kz-tab{display:flex;gap:6px;margin-bottom:13px;background:rgba(10,16,30,.6);border:1px solid rgba(255,255,255,.06);border-radius:var(--radius-sm);padding:4px}
.kz-t{flex:1;padding:10px;border-radius:8px;border:none;background:transparent;text-align:center;font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;transition:all .15s;outline:none;-webkit-appearance:none;font-family:'DM Sans',sans-serif;-webkit-tap-highlight-color:transparent;user-select:none}
.kz-t:focus{outline:none}
.kz-t.on{background:rgba(10,132,255,.18);border-radius:8px;color:var(--b2);box-shadow:inset 0 0 0 1px rgba(10,132,255,.3)}
.kz-result{background:rgba(15,26,46,.9);border:1px solid rgba(255,255,255,.07);border-radius:var(--radius);padding:15px;margin-top:10px}
.kz-row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.kz-row:last-child{border-bottom:none}
.kz-label{font-size:12px;color:var(--muted);font-weight:600}
.kz-val{font-size:13px;font-weight:800;font-family:'Space Mono',monospace}
.kz-big{font-size:28px;font-weight:900;text-align:center;padding:14px 0;font-family:'Space Mono',monospace;letter-spacing:-1px}
.pos-card{background:var(--card);border:1px solid rgba(255,255,255,.07);border-radius:var(--radius);padding:14px;margin-bottom:9px;transition:border-color .15s}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pos-sym{font-size:15px;font-weight:800;font-family:'Space Mono',monospace}
.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.pos-item{background:rgba(20,32,54,.9);border-radius:var(--radius-sm);padding:10px;border:1px solid rgba(255,255,255,.05)}
.pos-item-l{font-size:8px;color:var(--muted);margin-bottom:3px;font-weight:600;letter-spacing:.3px;text-transform:uppercase}
.pos-item-v{font-size:13px;font-weight:800;font-family:'Space Mono',monospace}

/* COIN DETAIL PAGE */
#p-coin{display:none;padding:0}
#p-coin.on{display:block}
.coin-detail-hdr{display:flex;align-items:center;gap:11px;padding:13px 13px 10px;border-bottom:1px solid rgba(255,255,255,.06);background:linear-gradient(180deg,rgba(15,26,46,.6),transparent)}
.back-btn{width:34px;height:34px;border-radius:10px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.08);color:var(--text);font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .12s}
.back-btn:active{background:rgba(255,255,255,.12)}
.coin-detail-sym{font-size:17px;font-weight:900;font-family:'Space Mono',monospace;letter-spacing:-.3px}
.coin-detail-name{font-size:10px;color:var(--muted);margin-top:1px}

/* CANDLE CHART */
.chart-wrap{position:relative;background:#05080f;border-radius:var(--radius-sm);overflow:hidden;border:1px solid rgba(255,255,255,.07)}
.chart-toolbar{display:flex;gap:5px;padding:9px 11px;border-bottom:1px solid rgba(255,255,255,.04);align-items:center}
.tf-btn{background:transparent;border:none;color:var(--muted);font-size:10px;font-weight:700;cursor:pointer;padding:5px 9px;border-radius:7px;transition:all .14s;font-family:'Space Mono',monospace}
.tf-btn.on{background:rgba(10,132,255,.2);color:var(--b2);box-shadow:inset 0 0 0 1px rgba(10,132,255,.35)}
#candleCanvas{display:block;width:100%}

/* NEWS */
.news-card{background:var(--card2);border:1px solid rgba(255,255,255,.05);border-radius:var(--radius-sm);margin-bottom:8px;overflow:hidden;cursor:pointer;transition:all .15s}
.news-card:active{background:var(--card3);transform:scale(.99)}
.news-head{display:flex;gap:11px;padding:12px}
.news-icon{width:42px;height:42px;border-radius:9px;background:linear-gradient(135deg,rgba(10,132,255,.15),rgba(10,132,255,.05));border:1px solid rgba(10,132,255,.2);display:flex;align-items:center;justify-content:center;font-size:19px;flex-shrink:0}
.news-title{font-size:12px;font-weight:700;line-height:1.4;flex:1;letter-spacing:-.1px}
.news-src{font-size:8.5px;color:var(--muted);margin-top:4px;font-family:'Space Mono',monospace;display:flex;align-items:center;gap:4px}
.news-body{padding:0 12px 12px;font-size:11px;color:var(--muted);line-height:1.6;display:none;border-top:1px solid rgba(255,255,255,.04);padding-top:10px;margin-top:-2px}
.news-body.open{display:block}

/* TAKVIM */
.cal-item{display:flex;gap:11px;align-items:flex-start;padding:11px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.cal-item:last-child{border-bottom:none}
.cal-imp{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:4px}
.imp-high{background:var(--r);box-shadow:0 0 8px rgba(255,45,85,.5)}
.imp-med{background:var(--y);box-shadow:0 0 6px rgba(255,214,10,.3)}
.imp-low{background:var(--muted)}
.cal-name{font-size:12.5px;font-weight:700;margin-bottom:3px;letter-spacing:-.1px}
.cal-date{font-size:9px;color:var(--muted);font-family:'Space Mono',monospace}

/* MARKET SUMMARY BAR */
.mkt-bar{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:11px}
.mkt-bar-item{background:var(--card2);border:1px solid rgba(255,255,255,.05);border-radius:var(--radius-sm);padding:9px 8px;text-align:center}
.mkt-bar-v{font-size:13px;font-weight:800;font-family:'Space Mono',monospace}
.mkt-bar-l{font-size:7.5px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.3px}

/* PULSE ANIMATION for new data */
@keyframes pulseBg{0%{background-color:rgba(10,132,255,.12)}100%{background-color:transparent}}
.flash-update{animation:pulseBg .6s ease-out}

/* ALARM CARD */
.alarm-card{background:var(--card2);border:1px solid rgba(255,255,255,.07);border-radius:var(--radius);padding:13px;margin-bottom:9px;cursor:pointer;transition:all .15s;display:flex;justify-content:space-between;align-items:center;gap:10px}
.alarm-card:active{transform:scale(.99);border-color:rgba(10,132,255,.3)}
</style>
</head>
<body>
<div id="app">

<div class="hdr">
  <div class="logo">
    <div class="logo-box">📊</div>
    <div><div class="logo-txt">KriptoDrop</div><div class="logo-sub">Pro</div></div>
  </div>
  <div class="hdr-r">
    <div class="live-pill"><div class="ldot"></div><span class="ltxt">CANLI</span></div>
    <span id="clk"></span>
  </div>
</div>

<div class="ticker">
  <div class="t-inner">
    <div class="t-item" onclick="openCoin('BTC')"><span class="t-sym">BTC</span><span class="t-px" id="tBTC">$--</span><span class="t-ch" id="tBTCch"></span></div>
    <div class="t-item" onclick="openCoin('ETH')"><span class="t-sym">ETH</span><span class="t-px" id="tETH">$--</span><span class="t-ch" id="tETHch"></span></div>
    <div class="t-item" onclick="openCoin('BNB')"><span class="t-sym">BNB</span><span class="t-px" id="tBNB">$--</span></div>
    <div class="t-item" onclick="openCoin('SOL')"><span class="t-sym">SOL</span><span class="t-px" id="tSOL">$--</span></div>
    <div class="t-item" onclick="openCoin('XRP')"><span class="t-sym">XRP</span><span class="t-px" id="tXRP">$--</span></div>
    <div class="t-item" onclick="openCoin('DOGE')"><span class="t-sym">DOGE</span><span class="t-px" id="tDOGE">$--</span></div>
    <div class="t-item" onclick="openCoin('AVAX')"><span class="t-sym">AVAX</span><span class="t-px" id="tAVAX">$--</span></div>
    <div class="t-item" onclick="openCoin('BTC')"><span class="t-sym">BTC</span><span class="t-px" id="tBTC2">$--</span></div>
    <div class="t-item" onclick="openCoin('ETH')"><span class="t-sym">ETH</span><span class="t-px" id="tETH2">$--</span></div>
    <div class="t-item" onclick="openCoin('BNB')"><span class="t-sym">BNB</span><span class="t-px" id="tBNB2">$--</span></div>
    <div class="t-item" onclick="openCoin('SOL')"><span class="t-sym">SOL</span><span class="t-px" id="tSOL2">$--</span></div>
    <div class="t-item" onclick="openCoin('XRP')"><span class="t-sym">XRP</span><span class="t-px" id="tXRP2">$--</span></div>
    <div class="t-item" onclick="openCoin('DOGE')"><span class="t-sym">DOGE</span><span class="t-px" id="tDOGE2">$--</span></div>
    <div class="t-item" onclick="openCoin('AVAX')"><span class="t-sym">AVAX</span><span class="t-px" id="tAVAX2">$--</span></div>
  </div>
</div>

<div id="main">
<nav id="sidenav">
  <button class="nb on"  data-page="home"   ><span class="ic">🏠</span>Ana</button>
  <button class="nb"     data-page="mkt"    ><span class="ic">📈</span>Piyasa</button>
  <button class="nb"     data-page="top"    ><span class="ic">🏆</span>Lider</button>
  <button class="nb"     data-page="analiz" style="margin-top:8px"><span class="ic">🔬</span>Analiz</button>
  <button class="nb"     data-page="fib"    ><span class="ic">📐</span>Fib</button>
  <button class="nb"     data-page="kar"    style="margin-top:8px"><span class="ic">💰</span>K/Z</button>
  <button class="nb"     data-page="alarmlar"><span class="ic">🔔</span>Alarm</button>
  <button class="nb"     data-page="takvim" ><span class="ic">📅</span>Takvim</button>
</nav>

<div id="scroll">

<!-- ANA SAYFA -->
<div id="p-home" class="page on">
  <div id="homeLoad" class="ld"><div class="spin"></div>Yükleniyor...</div>
  <div id="homeContent" style="display:none">
    <!-- BTC / ETH -->
    <div class="g2" style="margin-bottom:10px">
      <div class="card cg" style="padding:14px;cursor:pointer" onclick="openCoin('BTC')">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:10px">
          <div style="width:26px;height:26px;border-radius:50%;background:rgba(247,147,26,.15);border:1px solid rgba(247,147,26,.3);display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0">₿</div>
          <div>
            <div style="font-size:10px;font-weight:800;letter-spacing:-.1px">Bitcoin</div>
            <div style="font-size:7.5px;color:var(--muted);font-weight:600;letter-spacing:.5px;text-transform:uppercase">BTC</div>
          </div>
        </div>
        <div style="font-size:20px;font-weight:900;font-family:'Space Mono',monospace;color:var(--g);letter-spacing:-.5px" id="hBP">--</div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:7px">
          <div id="hBB" style="font-size:12px">--</div>
          <div style="font-size:8.5px;color:var(--muted);font-family:'Space Mono',monospace" id="hBV">--</div>
        </div>
      </div>
      <div class="card cb" style="padding:14px;cursor:pointer" onclick="openCoin('ETH')">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:10px">
          <div style="width:26px;height:26px;border-radius:50%;background:rgba(98,126,234,.15);border:1px solid rgba(98,126,234,.3);display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0">Ξ</div>
          <div>
            <div style="font-size:10px;font-weight:800;letter-spacing:-.1px">Ethereum</div>
            <div style="font-size:7.5px;color:var(--muted);font-weight:600;letter-spacing:.5px;text-transform:uppercase">ETH</div>
          </div>
        </div>
        <div style="font-size:20px;font-weight:900;font-family:'Space Mono',monospace;color:var(--b2);letter-spacing:-.5px" id="hEP">--</div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:7px">
          <div id="hEB" style="font-size:12px">--</div>
          <div style="font-size:8.5px;color:var(--muted);font-family:'Space Mono',monospace" id="hEV">--</div>
        </div>
      </div>
    </div>

    <!-- PIYASA DUYARLILIĞI -->
    <div class="card" style="margin-bottom:10px;padding:13px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:9.5px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;font-family:'Space Mono',monospace">Piyasa Duyarlılığı</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="hMood" style="font-size:11px;font-weight:800">--</span>
          <span class="bdg bb" id="hAvgPct" style="font-size:10px;padding:3px 7px">--</span>
        </div>
      </div>
      <div style="height:7px;background:rgba(255,255,255,.05);border-radius:6px;overflow:hidden;margin-bottom:10px;position:relative">
        <div style="position:absolute;inset:0;background:linear-gradient(90deg,var(--r) 0%,var(--y) 50%,var(--g) 100%);opacity:.3;border-radius:6px"></div>
        <div id="hSentBar" style="height:100%;border-radius:6px;width:50%;background:linear-gradient(90deg,rgba(10,132,255,.7),rgba(10,132,255,1));transition:width .9s cubic-bezier(.4,0,.2,1);position:relative">
          <div style="position:absolute;right:-1px;top:-2px;width:3px;height:11px;background:#fff;border-radius:2px;box-shadow:0 0 8px rgba(255,255,255,.6)"></div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">
        <div style="text-align:center;background:rgba(6,9,15,.7);border-radius:9px;padding:9px 4px;border:1px solid rgba(255,255,255,.04)">
          <div style="font-size:14px;font-weight:900;color:var(--o);font-family:'Space Mono',monospace;letter-spacing:-.3px" id="hDom">--</div>
          <div style="font-size:7.5px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.3px">BTC Dom</div>
        </div>
        <div style="text-align:center;background:rgba(6,9,15,.7);border-radius:9px;padding:9px 4px;border:1px solid rgba(255,255,255,.04)">
          <div style="font-size:12px;font-weight:900;font-family:'Space Mono',monospace" id="hAvg">--</div>
          <div style="font-size:7.5px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.3px">Ort Değ</div>
        </div>
        <div style="text-align:center;background:rgba(5,216,144,.06);border-radius:9px;padding:9px 4px;border:1px solid rgba(5,216,144,.1)">
          <div style="font-size:14px;font-weight:900;color:var(--g);font-family:'Space Mono',monospace;letter-spacing:-.3px" id="hUp">--</div>
          <div style="font-size:7.5px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.3px">↑ Yükselen</div>
        </div>
        <div style="text-align:center;background:rgba(255,45,85,.06);border-radius:9px;padding:9px 4px;border:1px solid rgba(255,45,85,.1)">
          <div style="font-size:14px;font-weight:900;color:var(--r);font-family:'Space Mono',monospace;letter-spacing:-.3px" id="hDn">--</div>
          <div style="font-size:7.5px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.3px">↓ Düşen</div>
        </div>
      </div>
    </div>

    <!-- YÜKSELENLER -->
    <div class="card cg" style="margin-bottom:10px;padding:13px">
      <div class="sh" style="margin-bottom:8px"><div class="sh-t">🚀 <span>Yükselenler</span></div><span class="sh-btn" onclick="go('top')">Tümü →</span></div>
      <div id="hGain"></div>
    </div>
    <!-- DÜŞENLER -->
    <div class="card cr-card" style="margin-bottom:10px;padding:13px">
      <div class="sh" style="margin-bottom:8px"><div class="sh-t">💥 <span>Düşenler</span></div><span class="sh-btn" onclick="go('top')">Tümü →</span></div>
      <div id="hLose"></div>
    </div>

    <!-- PORTFÖY ÖZET -->
    <div class="card" style="margin-bottom:10px;border-color:rgba(5,216,144,.18);background:linear-gradient(145deg,var(--card) 65%,rgba(5,216,144,.04))" id="hPortfoyCard">
      <div class="sh">
        <div class="sh-t">💼 <span>Portföy</span></div>
        <span class="sh-btn" onclick="go('kar')">Detay →</span>
      </div>
      <div id="hPortfoy">
        <div style="font-size:11px;color:var(--muted);padding:4px 0">Yükleniyor...</div>
      </div>
    </div>

    <!-- AKTİF ALARMLAR -->
    <div class="card cp" style="margin-bottom:10px">
      <div class="sh"><div class="sh-t">🔔 <span>Aktif Alarmlar</span></div><span class="sh-btn" onclick="go('alarmlar')">Tümü →</span></div>
      <div id="hAlarm"><div class="ld" style="padding:10px"><div class="spin"></div></div></div>
    </div>

    <!-- HABERLER -->
    <div class="card">
      <div class="sh"><div class="sh-t">📰 <span>Son Haberler</span></div></div>
      <div id="hNews"><div class="ld" style="padding:10px"><div class="spin"></div></div></div>
    </div>
  </div>
</div>

<!-- PİYASA -->
<div id="p-mkt" class="page">
  <!-- Market özet mini bar -->
  <div class="mkt-bar" id="mktStatBar" style="display:none">
    <div class="mkt-bar-item"><div class="mkt-bar-v" id="mktStatTotal">--</div><div class="mkt-bar-l">Toplam</div></div>
    <div class="mkt-bar-item"><div class="mkt-bar-v up" id="mktStatUp">--</div><div class="mkt-bar-l">↑ Yükselen</div></div>
    <div class="mkt-bar-item"><div class="mkt-bar-v dn" id="mktStatDn">--</div><div class="mkt-bar-l">↓ Düşen</div></div>
  </div>
  <div class="row">
    <input class="inp" id="mQ" placeholder="🔍 BTC, ETH, SOL..." oninput="fltMkt()">
    <select class="sel" id="mSrt" onchange="srtMkt()">
      <option value="mc">🏆 MarketCap</option>
      <option value="vol">📊 Hacim</option>
      <option value="up">↑ Yükselen</option>
      <option value="dn">↓ Düşen</option>
    </select>
  </div>
  <div style="display:flex;gap:6px;margin-bottom:11px;overflow-x:auto;scrollbar-width:none;padding-bottom:3px;-webkit-overflow-scrolling:touch">
    <button class="fc on" id="fAll" onclick="setF('all')">🌐 Tümü</button>
    <button class="fc" id="fUp" onclick="setF('up')">🟢 Yükselen</button>
    <button class="fc" id="fDn" onclick="setF('dn')">🔴 Düşen</button>
    <button class="fc" id="fPump" onclick="setF('flash5up')">⚡ 5dk ↑</button>
    <button class="fc" id="fDump" onclick="setF('flash5dn')">💥 5dk ↓</button>
  </div>
  <div id="mktList"><div class="ld"><div class="spin"></div>Yükleniyor...</div></div>
</div>

<!-- LİDERLER -->
<div id="p-top" class="page">
  <div class="frow" style="margin-bottom:9px">
    <div class="fc on" id="tG" onclick="showTop('g')">🚀 Yükselen</div>
    <div class="fc" id="tL" onclick="showTop('l')">💥 Düşen</div>
    <div class="fc" id="tV" onclick="showTop('v')">💧 Hacim</div>
  </div>
  <div id="topL"><div class="ld"><div class="spin"></div></div></div>
</div>

<!-- ANALİZ -->
<div id="p-analiz" class="page">
  <div class="row">
    <input class="inp" id="aIn" placeholder="BTC veya BTCUSDT" maxlength="15" onkeydown="if(event.key==='Enter')doAnaliz()">
    <button class="btn" onclick="doAnaliz()">🔬 Analiz</button>
  </div>
  <div id="aOut"><div class="mt"><div class="mt-i">🔬</div><div class="mt-t">Teknik Analiz</div><div class="mt-s">RSI · EMA · Sinyal skoru</div></div></div>
</div>

<!-- FİBONACCİ -->
<div id="p-fib" class="page">
  <div class="row">
    <input class="inp" id="fibIn" placeholder="BTC veya BTCUSDT" maxlength="15" onkeydown="if(event.key==='Enter')doFibPage()">
    <select class="sel" id="fibTF">
      <option value="1h">1s</option>
      <option value="4h" selected>4s</option>
      <option value="1d">1g</option>
      <option value="1w">1h</option>
    </select>
    <button class="btn" onclick="doFibPage()">📐 Çiz</button>
  </div>
  <div id="fibOut"><div class="mt"><div class="mt-i">📐</div><div class="mt-t">Fibonacci + Mum Grafik</div><div class="mt-s">Sembol girin ve Çiz'e basın</div></div></div>
</div>

<!-- KAR/ZARAR -->
<div id="p-kar" class="page">
  <div class="kz-tab">
    <button class="kz-t on" id="kzT1">🧮 Hesap</button>
    <button class="kz-t"    id="kzT2">📦 Pozisyonlarım</button>
  </div>
  <div id="kzHesap">
    <div class="card co">
      <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.8px;text-transform:uppercase;margin-bottom:12px;font-family:'Space Mono',monospace">💰 Kar / Zarar Hesapla</div>
      <div class="row"><input class="inp" id="kzSym" placeholder="BTC veya BTCUSDT" maxlength="15" style="text-transform:uppercase"></div>
      <div class="g2" style="margin-bottom:10px">
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.3px">Alış Fiyatı</div><input class="inp" id="kzBuy" placeholder="60000" type="number" style="width:100%"></div>
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.3px">Miktar</div><input class="inp" id="kzAmt" placeholder="0.5" type="number" style="width:100%"></div>
      </div>
      <div style="margin-bottom:10px"><div style="font-size:9px;color:var(--muted);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.3px">Satış Fiyatı (boş = canlı)</div><input class="inp" id="kzSell" placeholder="Canlı fiyat" type="number" style="width:100%"></div>
      <div style="display:flex;gap:8px">
        <button class="btn" style="flex:1;padding:14px" onclick="kzHesapla()">🧮 Hesapla</button>
        <button class="btn btn-g" style="flex:1;padding:14px" onclick="kzKaydet()">💾 Kaydet</button>
      </div>
    </div>
    <div id="kzResult"></div>
  </div>
  <div id="kzPoz" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.8px;text-transform:uppercase;font-family:'Space Mono',monospace">📦 Pozisyonlarım</div>
      <button class="btn" style="padding:7px 12px;font-size:11px" onclick="loadKarPoz()">🔄</button>
    </div>
    <div id="kzPozList"><div class="ld"><div class="spin"></div></div></div>
  </div>
</div>

<!-- ALARMLAR -->
<div id="p-alarmlar" class="page">
  <div class="kz-tab">
    <button class="kz-t on" id="alTL">🔔 Alarmlarım</button>
    <button class="kz-t"    id="alTE">➕ Alarm Ekle</button>
  </div>
  <div id="alListe"><div class="ld"><div class="spin"></div></div></div>
  <div id="alEkle" style="display:none">
    <div class="card co">
      <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.8px;text-transform:uppercase;margin-bottom:12px;font-family:'Space Mono',monospace">🔔 Yeni Alarm</div>
      <div class="row"><input class="inp" id="alSym" placeholder="BTC veya BTCUSDT" maxlength="15" style="text-transform:uppercase"></div>
      <div class="g2" style="margin-bottom:10px">
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.3px">Alarm Türü</div><select class="sel" id="alType" style="width:100%"><option value="percent">📊 Yüzde %</option><option value="price">💰 Fiyat $</option></select></div>
        <div><div style="font-size:9px;color:var(--muted);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.3px">Değer</div><input class="inp" id="alThr" placeholder="3.5" type="number" step="0.1" style="width:100%"></div>
      </div>
      <button class="btn" style="width:100%;padding:14px" onclick="alarmEkle()">🔔 Alarm Ekle</button>
      <div style="font-size:10px;color:var(--muted);margin-top:10px;text-align:center;line-height:1.5">Yüzde: fiyat %X değişince<br>Fiyat: hedefe ulaşınca tetiklenir</div>
    </div>
  </div>
</div>

<!-- EKONOMİK TAKVİM -->
<div id="p-takvim" class="page">
  <div class="card" style="margin-bottom:9px;padding:11px 13px">
    <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;margin-bottom:10px;font-family:'Space Mono',monospace">📅 Yaklaşan Önemli Ekonomik Veriler</div>
    <div id="calList"><div class="ld"><div class="spin"></div></div></div>
  </div>
  <div class="card">
    <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;margin-bottom:10px;font-family:'Space Mono',monospace">📰 Kripto Haberleri (TR)</div>
    <div id="newsListFull"><div class="ld"><div class="spin"></div></div></div>
  </div>
</div>

<!-- COİN DETAY -->
<div id="p-coin" class="page">
  <div class="coin-detail-hdr">
    <button class="back-btn" onclick="goBack()">←</button>
    <div id="cdIco" style="width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:900;border:1px solid;flex-shrink:0;font-family:'Space Mono',monospace;overflow:hidden"></div>
    <div style="flex:1;min-width:0">
      <div class="coin-detail-sym" id="cdSym">--</div>
      <div class="coin-detail-name" id="cdName">--</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:19px;font-weight:900;font-family:'Space Mono',monospace;letter-spacing:-.5px" id="cdPrice">--</div>
      <div id="cdChange" style="font-size:12px;margin-top:3px">--</div>
    </div>
  </div>

  <!-- 24s HIGH / LOW mini bar -->
  <div id="cdHiLo" style="display:none;padding:8px 13px;background:rgba(6,10,18,.8);border-bottom:1px solid rgba(255,255,255,.05)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
      <span style="font-size:8.5px;color:var(--r);font-family:'Space Mono',monospace;font-weight:700" id="cdLo">--</span>
      <span style="font-size:7.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">24s Aralık</span>
      <span style="font-size:8.5px;color:var(--g);font-family:'Space Mono',monospace;font-weight:700" id="cdHi">--</span>
    </div>
    <div style="height:4px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden">
      <div id="cdRangeBar" style="height:100%;background:linear-gradient(90deg,var(--r),var(--g));border-radius:4px;width:50%"></div>
    </div>
    <div style="display:flex;justify-content:center;margin-top:3px">
      <span style="font-size:7.5px;color:var(--muted)" id="cdRangePct">--</span>
    </div>
  </div>

  <!-- Mum grafik -->
  <div style="padding:0 12px 10px">
    <div class="chart-wrap">
      <div class="chart-toolbar">
        <span style="font-size:8.5px;color:var(--muted);font-family:'Space Mono',monospace;margin-right:4px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="cdOHLC"></span>
        <button class="tf-btn" onclick="loadCandleChart('15m')">15d</button>
        <button class="tf-btn on" onclick="loadCandleChart('1h')">1s</button>
        <button class="tf-btn" onclick="loadCandleChart('4h')">4s</button>
        <button class="tf-btn" onclick="loadCandleChart('1d')">1g</button>
      </div>
      <canvas id="candleCanvas" height="220"></canvas>
    </div>
  </div>

  <div style="padding:0 12px">
    <!-- İstatistikler -->
    <div class="g2" style="margin-bottom:10px" id="cdStats"></div>

    <!-- Teknik analiz özeti -->
    <div class="card" style="margin-bottom:10px" id="cdAnaliz"></div>

    <!-- Fibonacci seviyeleri -->
    <div class="card" style="margin-bottom:10px" id="cdFib"></div>

    <!-- Coin haberleri -->
    <div class="card" id="cdNews">
      <div class="sh"><div class="sh-t">📰 <span>Haberler</span></div></div>
      <div id="cdNewsContent"><div class="ld"><div class="spin"></div></div></div>
    </div>
  </div>
</div>

</div><!-- /scroll -->
</div><!-- /main -->
</div><!-- /app -->
<div id="toast"></div>

<script>
const tg=window.Telegram?.WebApp;
if(tg){tg.ready();tg.expand();}
const UID=tg?.initDataUnsafe?.user?.id||0;

// ── FORMAT ──
function fp(p){p=parseFloat(p);if(isNaN(p)||p===0)return'--';if(p>=100000)return p.toLocaleString('tr-TR',{maximumFractionDigits:0});if(p>=1000)return p.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});if(p>=1)return p.toFixed(4);if(p>=0.001)return p.toFixed(6);return p.toFixed(8);}
function fv(v){v=parseFloat(v)||0;if(v>=1e9)return(v/1e9).toFixed(1)+'B$';if(v>=1e6)return(v/1e6).toFixed(1)+'M$';if(v>=1e3)return(v/1e3).toFixed(0)+'K$';return v.toFixed(0)+'$';}
function pc(p){return p>0?'up':p<0?'dn':'nu';}
function pb(p){const c=p>0?'bg':p<0?'br':'by',s=p>0?'+':'';return`<span class="bdg ${c}">${s}${p.toFixed(2)}%</span>`;}
const PAL=['#0a84ff','#bf5af2','#05d890','#ffd60a','#ff9f0a','#5ac8fa','#ff2d55','#4ecdc4'];

const COIN_COL={
  BTC:'#f7931a',ETH:'#627eea',BNB:'#f3ba2f',SOL:'#9945ff',XRP:'#0085c0',
  DOGE:'#c2a633',ADA:'#0033ad',AVAX:'#e84142',DOT:'#e6007a',LINK:'#2a5ada',
  UNI:'#ff007a',ATOM:'#4a4d6b',LTC:'#a8a8a8',BCH:'#8dc351',TRX:'#ef0027',
  NEAR:'#00c08b',MATIC:'#8247e5',ARB:'#2d374b',OP:'#ff0420',SUI:'#4ca3ff',
  INJ:'#00b4d8',SHIB:'#ffa409',HBAR:'#00b388',FIL:'#0090ff',ICP:'#f15a24',
  VET:'#15bdff',ALGO:'#000',PEPE:'#00a86b',WLD:'#191c1e',FET:'#1d2c4a',
  RNDR:'#e6394a',AAVE:'#b6509e',MKR:'#1aab9b',XLM:'#14b6e7',
  ETC:'#328332',ZEC:'#ecb244',XMR:'#ff6600',DASH:'#008ce7',
  USDC:'#2775ca',USDT:'#26a17b',CHZ:'#cd0124',GRT:'#6747ed',
  ANKR:'#0066ff',CHESS:'#ff6b35',ROBO:'#00d4aa',DEGO:'#f4c542',
  VANRY:'#8b5cf6',POLYX:'#eb3f3f',STO:'#2cd4d4',
};

function _colFor(sym){return COIN_COL[sym]||PAL[sym.charCodeAt(0)%PAL.length];}

// Inline SVG - hic bağımlılık yok, her zaman çalışır
function _svgIco(sym){
  const col=_colFor(sym);
  const r=parseInt(col.slice(1,3)||'40',16),g=parseInt(col.slice(3,5)||'90',16),b=parseInt(col.slice(5,7)||'ff',16);
  const lum=(r*299+g*587+b*114)/1000;
  const txt=lum>140?'#000':'#fff';
  const lbl=sym.slice(0,3);
  const fs=lbl.length<=2?'13':'9.5';
  const y=lbl.length<=2?'22':'21.5';
  const darker=`#${Math.max(0,r-30).toString(16).padStart(2,'0')}${Math.max(0,g-30).toString(16).padStart(2,'0')}${Math.max(0,b-30).toString(16).padStart(2,'0')}`;
  return `<svg width="34" height="34" viewBox="0 0 34 34" xmlns="http://www.w3.org/2000/svg" style="display:block;border-radius:50%;flex-shrink:0"><defs><radialGradient id="rg_${sym}" cx="38%" cy="30%" r="70%"><stop offset="0%" stop-color="${col}"/><stop offset="100%" stop-color="${darker}"/></radialGradient></defs><circle cx="17" cy="17" r="17" fill="url(#rg_${sym})"/><circle cx="13" cy="10" r="8" fill="#fff" opacity=".1"/><text x="17" y="${y}" text-anchor="middle" fill="${txt}" font-size="${fs}" font-weight="800" font-family="'Space Mono',monospace" letter-spacing="-0.5">${lbl}</text></svg>`;
}

// Ikon cache - başarılı yüklenenler
const _icoOk={};
const _icoFail=new Set();

// imgUrl: CoinGecko direct URL — yoksa /api/icon proxy dene, o da başarısız olursa SVG
function cIco(sym, imgUrl){
  const col=_colFor(sym);
  if(_icoFail.has(sym)) return `<div class="cico" style="padding:0;border-color:${col}20;overflow:hidden">${_svgIco(sym)}</div>`;
  const src=imgUrl||`/api/icon?sym=${sym.toLowerCase()}`;
  const id=`ico_${sym}_${Math.random().toString(36).slice(2,6)}`;
  return `<div class="cico" style="padding:0;border-color:${col}20;overflow:hidden" id="${id}">
    <img src="${src}" width="34" height="34"
      style="border-radius:50%;display:block;object-fit:cover;width:34px;height:34px"
      onload="_icoOk['${sym}']=1"
      onerror="_icoErr(this,'${sym}','${id}')">
  </div>`;
}
function _icoErr(img,sym,id){
  _icoFail.add(sym);
  const el=document.getElementById(id);
  if(el)el.innerHTML=_svgIco(sym);
}

function toast(m,d=2400){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');setTimeout(()=>e.classList.remove('on'),d);}

// ── CLOCK ──
setInterval(()=>{const e=document.getElementById('clk');if(e)e.textContent=new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});},1000);

// ── API ──
async function api(path,ms=15000){try{const ctrl=new AbortController();const t=setTimeout(()=>ctrl.abort(),ms);const r=await fetch(path,{signal:ctrl.signal});clearTimeout(t);if(!r.ok)return null;return await r.json();}catch(e){return null;}}

// ── STATE ──
let allCoins=[],coinFilter='all',flash5Up=[],flash5Dn=[];
let topData={g:[],l:[],v:[]},topMode='g';
let prevPage='home',curCoinSym='';
const coin_image_cache_js={};
let candleTF='1h',candleData=[];
const PAGES=['home','mkt','top','analiz','kar','alarmlar','takvim','coin'];
let CUR='home';

// ── NAV ──
function go(t){
  if(CUR===t&&t!=='coin')return;
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('#sidenav .nb').forEach(x=>x.classList.remove('on'));
  const pg=document.getElementById('p-'+t);if(!pg)return;
  pg.classList.add('on');
  const nb=document.querySelector('#sidenav .nb[data-page="'+t+'"]');
  if(nb)nb.classList.add('on');
  if(t!=='coin')prevPage=CUR;
  CUR=t;
  document.getElementById('scroll').scrollTop=0;
  if(t==='mkt')loadMkt();
  if(t==='top')loadTop();
  if(t==='alarmlar')loadAlarms();
  if(t==='kar'&&kzMode==='pozisyon')loadKarPoz();
  if(t==='takvim')loadTakvim();
}
function goBack(){go(prevPage||'home');}

// ── COIN DETAY ──
function openCoin(symBase){
  curCoinSym=symBase.replace('USDT','').toUpperCase();
  const sym=curCoinSym+'USDT';
  const ci=curCoinSym.charCodeAt(0)%PAL.length;const col=PAL[ci];
  // İkon
  const ico=document.getElementById('cdIco');
  if(ico){
    ico.style.background=col+'20';ico.style.borderColor=col+'40';
    ico.innerHTML='';
    const _imgUrl=coin_image_cache_js[curCoinSym.toLowerCase()]||'';
    if(_imgUrl){
      const _im=document.createElement('img');
      _im.src=_imgUrl;_im.width=36;_im.height=36;
      _im.style.cssText='border-radius:50%;display:block;object-fit:cover;width:36px;height:36px';
      _im.onerror=function(){ico.innerHTML=_svgIco(curCoinSym);};
      ico.appendChild(_im);
    } else {
      ico.innerHTML=_svgIco(curCoinSym);
    }
  }
  document.getElementById('cdSym').textContent=curCoinSym;
  document.getElementById('cdName').textContent=sym;
  document.getElementById('cdPrice').textContent='Yükleniyor...';
  document.getElementById('cdChange').textContent='';
  document.getElementById('cdStats').innerHTML='<div class="ld" style="grid-column:span 2"><div class="spin"></div></div>';
  document.getElementById('cdAnaliz').innerHTML='<div class="ld"><div class="spin"></div></div>';
  document.getElementById('cdFib').innerHTML='<div class="ld"><div class="spin"></div></div>';
  document.getElementById('cdNewsContent').innerHTML='<div class="ld"><div class="spin"></div></div>';
  go('coin');
  // Toolbar aktif
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.tf-btn')[1]?.classList.add('on');
  candleTF='1h';
  loadCoinDetail(sym);
}
async function loadCoinDetail(sym){
  const [tick, analiz, fib, news24] = await Promise.all([
    api(`/api/price?symbol=${sym}`),
    api(`/api/analiz?symbol=${sym}`),
    api(`/api/fib?symbol=${sym}&interval=4h`),
    api(`/api/coin_news?symbol=${sym.replace('USDT','')}`),
  ]);
  const base=sym.replace('USDT','');
  // Fiyat + değişim
  if(tick){
    document.getElementById('cdPrice').textContent='$'+fp(tick.price);
    if(tick.change!==undefined){
      const ch=parseFloat(tick.change);
      document.getElementById('cdChange').innerHTML=pb(ch);
    }
    // 24s Hi/Lo bar
    if(tick.high&&tick.low){
      const hi=tick.high,lo=tick.low,cur=tick.price||0;
      const pct=hi>lo?Math.round(((cur-lo)/(hi-lo))*100):50;
      const rangeW=hi>lo?((hi-lo)/lo*100).toFixed(2):0;
      document.getElementById('cdLo').textContent='$'+fp(lo);
      document.getElementById('cdHi').textContent='$'+fp(hi);
      document.getElementById('cdRangePct').textContent=`Aralık: %${rangeW} · Konumunuz: %${pct}`;
      document.getElementById('cdRangeBar').style.width=pct+'%';
      document.getElementById('cdHiLo').style.display='block';
    }
    // Stats
    document.getElementById('cdStats').innerHTML=`
      <div class="card" style="margin:0;padding:11px;text-align:center"><div style="font-size:15px;font-weight:900;font-family:'Space Mono',monospace;letter-spacing:-.3px">$${fp(tick.price)}</div><div style="font-size:8px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.3px">Fiyat</div></div>
      <div class="card" style="margin:0;padding:11px;text-align:center"><div style="font-size:15px;font-weight:900;font-family:'Space Mono',monospace;letter-spacing:-.3px;${parseFloat(tick.change||0)>=0?'color:var(--g)':'color:var(--r)'}">${parseFloat(tick.change||0)>=0?'+':''}${parseFloat(tick.change||0).toFixed(2)}%</div><div style="font-size:8px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.3px">24s Değişim</div></div>`;
  }
  // Teknik analiz
  if(analiz){
    const {rsi1,rsi4,rsiD,score,signals}=analiz;
    const col=score>=70?'var(--g)':score>=55?'var(--b)':score>=45?'var(--y)':score>=30?'var(--o)':'var(--r)';
    const lbl=score>=70?'🟢 AL':score>=55?'🔵 ZAYIF AL':score>=45?'⚪ NÖTR':score>=30?'🟠 ZAYIF SAT':'🔴 SAT';
    document.getElementById('cdAnaliz').innerHTML=`
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:9px">
        <span style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;font-family:'Space Mono',monospace">📊 Teknik Analiz</span>
        <span style="font-size:14px;font-weight:900;color:${col}">${lbl}</span>
      </div>
      <div class="pb" style="margin-bottom:9px"><div class="pbf" style="width:${score}%;background:${col}"></div></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px">
        <div style="background:rgba(6,9,15,.7);border-radius:8px;padding:8px;text-align:center"><div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:${rsi1<30?'var(--g)':rsi1>70?'var(--r)':'var(--y)'}">${Math.round(rsi1)}</div><div style="font-size:8px;color:var(--muted);margin-top:2px">RSI 1s</div></div>
        <div style="background:rgba(6,9,15,.7);border-radius:8px;padding:8px;text-align:center"><div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:${rsi4<30?'var(--g)':rsi4>70?'var(--r)':'var(--y)'}">${Math.round(rsi4)}</div><div style="font-size:8px;color:var(--muted);margin-top:2px">RSI 4s</div></div>
        <div style="background:rgba(6,9,15,.7);border-radius:8px;padding:8px;text-align:center"><div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:${rsiD<30?'var(--g)':rsiD>70?'var(--r)':'var(--y)'}">${Math.round(rsiD)}</div><div style="font-size:8px;color:var(--muted);margin-top:2px">RSI 1g</div></div>
      </div>`;
  }
  // Fibonacci
  if(fib&&fib.levels){
    const FCOL={'0':'#ff2d55','23.6':'#ff9f0a','38.2':'#ffd60a','50':'#8a9ab0','61.8':'#05d890','78.6':'#0a84ff','100':'#bf5af2'};
    document.getElementById('cdFib').innerHTML=`
      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;margin-bottom:9px;font-family:'Space Mono',monospace">📐 Fibonacci 4s</div>
      ${fib.levels.map(l=>{const isCur=Math.abs(l.price-fib.cur)/fib.cur<0.008;const col=FCOL[String(l.pct)]||'#5577aa';
        return`<div style="display:flex;justify-content:space-between;align-items:center;padding:7px ${isCur?'9':'0'}px;border-bottom:1px solid rgba(255,255,255,.04);border-radius:${isCur?'8':'0'}px;background:${isCur?'rgba(10,132,255,.08)':'transparent'};margin:${isCur?'0 -9px':'0'}">
          <div style="display:flex;align-items:center;gap:6px"><div style="width:3px;height:14px;border-radius:2px;background:${col}"></div><span style="font-size:9.5px;color:${col};font-weight:700;font-family:'Space Mono',monospace">%${l.pct}</span></div>
          <span style="font-size:12px;font-weight:800;font-family:'Space Mono',monospace">$${fp(l.price)}</span>
          <span style="font-size:9.5px;${l.dist>=0?'color:var(--g)':'color:var(--r)'};font-family:'Space Mono',monospace">${l.dist>=0?'+':''}${l.dist.toFixed(2)}%</span>
        </div>`;}).join('')}`;
  }
  // Haberler
  if(news24&&news24.news){
    document.getElementById('cdNewsContent').innerHTML=news24.news.length
      ?news24.news.map((n,i)=>newsCard(n,`cn${i}`)).join('')
      :'<div style="font-size:11px;color:var(--muted);padding:8px 0">Bu coin için haber bulunamadı</div>';
  }
  // Mum grafik
  loadCandleChart(candleTF);
}

// ── MUM GRAFİK ──
let candleRAF=null;
let curFibData=null;
async function loadCandleChart(tf){
  candleTF=tf;
  const tfMap={'15m':'15d','1h':'1s','4h':'4s','1d':'1g'};
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('on',b.textContent===tfMap[tf]));
  const sym=curCoinSym+'USDT';
  // Hem klines hem fib paralel çek (4h fib hep göster)
  const [klRes, fibRes] = await Promise.all([
    api(`/api/klines?symbol=${sym}&interval=${tf}&limit=80`),
    (tf==='4h'||!curFibData)?api(`/api/fib?symbol=${sym}&interval=${tf}`):Promise.resolve(curFibData)
  ]);
  if(!klRes||!klRes.klines||klRes.klines.length<2)return;
  candleData=klRes.klines;
  if(fibRes&&fibRes.levels)curFibData=fibRes;
  requestAnimationFrame(()=>drawCandleFib('candleCanvas', candleData, curFibData, 'cdOHLC'));
}
// ── EVRENSEL MUM + FİBONACCİ GRAFİK FONKSİYONU ──
const FCOL_FIB={'0':'#ff2d55','23.6':'#ff9f0a','38.2':'#ffd60a','50':'#8a9ab0','61.8':'#05d890','78.6':'#0a84ff','100':'#bf5af2'};

function drawCandleFib(canvasId, klines, fibData, ohlcElId){
  const canvas=document.getElementById(canvasId);
  if(!canvas||!klines||klines.length<2)return;
  const wrap=canvas.parentElement;
  const W=(wrap?wrap.clientWidth:0)||canvas.offsetWidth||320;
  const H=220;
  canvas.style.width=W+'px';
  canvas.style.height=H+'px';
  canvas.width=Math.round(W*devicePixelRatio);
  canvas.height=Math.round(H*devicePixelRatio);
  const ctx=canvas.getContext('2d');
  ctx.scale(devicePixelRatio,devicePixelRatio);

  const O=0,HI=1,LO=2,CL=3,VO=4;
  const hasFib=!!(fibData&&fibData.levels&&fibData.levels.length);

  const PL=50, PR=hasFib?46:8, PT=8, PB=22, VH=32;
  const CW=W-PL-PR;
  const CH=H-PT-PB-VH;

  const n=klines.length;
  const cW=Math.max(1.5, CW/n);
  const bW=Math.max(1, cW*0.6);

  let allP=klines.flatMap(k=>[k[HI],k[LO]]);
  if(hasFib) fibData.levels.forEach(l=>allP.push(l.price));
  const rawMin=Math.min(...allP), rawMax=Math.max(...allP);
  const padP=(rawMax-rawMin)*0.05;
  const pMin=rawMin-padP, pMax=rawMax+padP, pRng=pMax-pMin||1;

  const yP=(p)=>PT+CH*(1-(p-pMin)/pRng);
  const volTop=PT+CH+2;
  const vols=klines.map(k=>k[VO]||0);
  const maxV=Math.max(...vols,1);

  // ── BG ──
  ctx.fillStyle='#050810';
  ctx.fillRect(0,0,W,H);

  // ── FİBONACCİ ZONE DOLGUSU (fiyat bölgesi) ──
  if(hasFib && fibData.zone_lo && fibData.zone_hi){
    const zy1=yP(fibData.zone_hi.price);
    const zy2=yP(fibData.zone_lo.price);
    const zH=zy2-zy1;
    if(zH>0){
      ctx.save();
      ctx.globalAlpha=0.07;
      ctx.fillStyle='#0a84ff';
      ctx.fillRect(PL, zy1, CW, zH);
      ctx.restore();
    }
  }

  // ── GRID ──
  ctx.strokeStyle='rgba(255,255,255,.05)';
  ctx.lineWidth=0.5;
  for(let i=0;i<=4;i++){
    const gy=PT+i*(CH/4);
    ctx.beginPath();ctx.moveTo(PL,gy);ctx.lineTo(PL+CW,gy);ctx.stroke();
  }

  // ── FİBONACCİ ÇİZGİLERİ ──
  if(hasFib){
    const cur=fibData.cur;
    fibData.levels.forEach(l=>{
      if(l.price<pMin||l.price>pMax) return;
      const ly=yP(l.price);
      if(ly<PT-2||ly>PT+CH+2) return;
      const col=FCOL_FIB[String(l.pct)]||'#5577aa';
      const isNearest=Math.abs(l.price-cur)/cur<0.012;
      ctx.save();
      ctx.globalAlpha=isNearest?0.9:0.5;
      ctx.strokeStyle=col;
      ctx.lineWidth=isNearest?1.4:0.7;
      ctx.setLineDash(isNearest?[6,2]:[4,4]);
      ctx.beginPath(); ctx.moveTo(PL,ly); ctx.lineTo(PL+CW,ly); ctx.stroke();
      ctx.setLineDash([]);
      // Sağ etiket
      ctx.globalAlpha=isNearest?1:0.75;
      ctx.fillStyle=col;
      ctx.font=`${isNearest?'bold ':''}${isNearest?8.5:7.5}px Space Mono,monospace`;
      ctx.textAlign='left';
      ctx.fillText(l.pct+'%', PL+CW+3, ly+3);
      // Yakın seviyeye nokta işareti
      if(isNearest){
        ctx.fillStyle=col;
        ctx.beginPath();
        ctx.arc(PL+CW+1, ly, 2.5, 0, Math.PI*2);
        ctx.fill();
      }
      ctx.restore();
    });
  }

  // ── HACİM BARLARI ──
  klines.forEach((k,i)=>{
    const x=PL+i*cW;
    const isUp=k[CL]>=k[O];
    const vh=Math.max(1,(k[VO]/maxV)*VH);
    ctx.fillStyle=isUp?'rgba(5,216,144,.3)':'rgba(255,45,85,.25)';
    ctx.fillRect(x+(cW-bW)/2, PT+CH+VH-vh+2, bW, vh);
  });

  // ── MUM GRAFİĞİ ──
  klines.forEach((k,i)=>{
    const x=PL+i*cW+cW/2;
    const isUp=k[CL]>=k[O];
    const col=isUp?'#05d890':'#ff2d55';
    const yO=yP(k[O]), yC=yP(k[CL]), yH=yP(k[HI]), yL=yP(k[LO]);
    ctx.strokeStyle=col; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,yH); ctx.lineTo(x,yL); ctx.stroke();
    const top=Math.min(yO,yC);
    const bh=Math.max(1.5,Math.abs(yC-yO));
    ctx.fillStyle=col;
    ctx.fillRect(x-bW/2, top, bW, bh);
  });

  // ── SOL Y EKSENİ ──
  ctx.fillStyle='rgba(74,106,138,.7)';
  ctx.font='8px Space Mono,monospace';
  ctx.textAlign='right';
  for(let i=0;i<=4;i++){
    const p=pMin+i*(pRng/4);
    const gy=PT+CH*(1-i/4);
    ctx.fillText('$'+fp(p), PL-3, gy+3);
  }

  // ── SON FİYAT ÇİZGİSİ — belirgin ──
  const last=klines[klines.length-1];
  const lastY=yP(last[CL]);

  // Glow efekti (birden fazla stroke)
  ctx.save();
  ctx.strokeStyle='rgba(10,132,255,.15)'; ctx.lineWidth=5; ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(PL,lastY); ctx.lineTo(PL+CW,lastY); ctx.stroke();
  ctx.strokeStyle='rgba(10,132,255,.4)'; ctx.lineWidth=1.5; ctx.setLineDash([4,3]);
  ctx.beginPath(); ctx.moveTo(PL,lastY); ctx.lineTo(PL+CW,lastY); ctx.stroke();
  ctx.setLineDash([]);

  // Sol ok işareti — "fiyat burada"
  const arrowX=PL+4;
  ctx.fillStyle='#0a84ff';
  ctx.beginPath();
  ctx.moveTo(arrowX,lastY);
  ctx.lineTo(arrowX+8,lastY-5);
  ctx.lineTo(arrowX+8,lastY+5);
  ctx.closePath();
  ctx.fill();

  // Fiyat balonu — sağda belirgin
  const lbl='$'+fp(last[CL]);
  ctx.font='bold 9.5px Space Mono,monospace';
  const tw=ctx.measureText(lbl).width+12;
  const bx=PL+CW-tw-2, by=lastY-9, bh2=16;
  // Glow
  ctx.shadowColor='rgba(10,132,255,.6)';
  ctx.shadowBlur=8;
  ctx.fillStyle='rgba(10,132,255,.95)';
  if(ctx.roundRect) ctx.roundRect(bx,by,tw,bh2,3);
  else ctx.rect(bx,by,tw,bh2);
  ctx.fill();
  ctx.shadowBlur=0;
  ctx.fillStyle='#ffffff'; ctx.textAlign='left';
  ctx.fillText(lbl, bx+5, by+11);

  // Sol kenar "▶ FİYAT" etiketi
  ctx.font='bold 7px Space Mono,monospace';
  ctx.fillStyle='rgba(10,132,255,.9)';
  ctx.textAlign='right';
  ctx.fillText('▶', PL-2, lastY+3);

  ctx.restore();

  // ── OHLC ──
  const oEl=document.getElementById(ohlcElId);
  if(oEl) oEl.textContent=`O:${fp(last[O])} H:${fp(last[HI])} L:${fp(last[LO])} C:${fp(last[CL])}`;
}

// drawCandles artık drawCandleFib'i çağırıyor (geriye uyumluluk)
function drawCandles(klines){
  drawCandleFib('candleCanvas', klines, null, 'cdOHLC');
}

// ── HOME ──
async function loadHome(){
  document.getElementById('homeLoad').style.display='flex';
  document.getElementById('homeContent').style.display='none';
  const d=await api(`/api/dashboard${UID?'?uid='+UID:''}`);
  document.getElementById('homeLoad').style.display='none';
  document.getElementById('homeContent').style.display='block';
  if(!d){document.getElementById('hGain').innerHTML='<div style="color:var(--r);font-size:11px">⚠️ Veri yüklenemedi</div>';return;}
  // Ticker
  const TSYMS=['BTC','ETH','BNB','SOL','XRP','DOGE','AVAX'];
  TSYMS.forEach(s=>{
    const px=d.prices&&d.prices[s+'USDT'];if(!px)return;
    const txt='$'+fp(px);
    [s,s+'2'].forEach(k=>{const e=document.getElementById('t'+k);if(e)e.textContent=txt;});
    if(s==='BTC'||s==='ETH'){const ce=document.getElementById('t'+s+'ch');if(ce){const ch=s==='BTC'?d.btc?.change:d.eth?.change;if(ch){ce.textContent=(ch>0?'+':'')+parseFloat(ch).toFixed(2)+'%';ce.style.color=ch>0?'var(--g)':'var(--r)';}}}
  });
  // BTC/ETH
  const btc=d.btc||{},eth=d.eth||{};
  document.getElementById('hBP').textContent='$'+fp(btc.price||0);
  document.getElementById('hBB').innerHTML=pb(parseFloat(btc.change||0));
  document.getElementById('hBV').textContent='Vol: '+fv(btc.volume||0);
  document.getElementById('hEP').textContent='$'+fp(eth.price||0);
  document.getElementById('hEB').innerHTML=pb(parseFloat(eth.change||0));
  document.getElementById('hEV').textContent='Vol: '+fv(eth.volume||0);
  // Sentiment
  const avg=parseFloat(d.avg_change||0);
  const sent=Math.max(0,Math.min(100,(avg+3)/6*100));
  document.getElementById('hSentBar').style.width=sent+'%';
  const mood=avg>1.5?'🐂 Boğa':avg<-1.5?'🐻 Ayı':'😐 Nötr';
  const mc=avg>1.5?'var(--g)':avg<-1.5?'var(--r)':'var(--y)';
  document.getElementById('hMood').innerHTML=`<span style="color:${mc};font-weight:700">${mood}</span>`;
  document.getElementById('hAvgPct').textContent=(avg>0?'+':'')+avg.toFixed(2)+'%';
  document.getElementById('hDom').textContent=(d.btc_dom||0).toFixed(1)+'%';
  document.getElementById('hAvg').innerHTML=pb(avg);
  document.getElementById('hUp').textContent=d.rising||'--';
  document.getElementById('hDn').textContent=d.falling||'--';
  // Market stat bar (piyasa sayfası için)
  const mktBar=document.getElementById('mktStatBar');
  if(mktBar){
    const tot=(d.rising||0)+(d.falling||0);
    document.getElementById('mktStatTotal').textContent=tot||'--';
    document.getElementById('mktStatUp').textContent=d.rising||'--';
    document.getElementById('mktStatDn').textContent=d.falling||'--';
    mktBar.style.display='grid';
  }
  // Liderler
  const mkRow=(c,i)=>`<div class="cr" onclick="openCoin('${c.s.replace('USDT','')}')"><span class="crank">${i+1}</span>${cIco(c.s.replace('USDT',''),c.img||'')}<div class="cinfo"><div class="csym">${c.s.replace('USDT','')}</div></div>${pb(c.ch)}</div>`;
  document.getElementById('hGain').innerHTML=(d.top5||[]).slice(0,4).map(mkRow).join('');
  document.getElementById('hLose').innerHTML=((d.top_data&&d.top_data.l)||[]).slice(0,4).map(mkRow).join('');
  // Alarmlar
  const alarms=d.alarms||[];
  document.getElementById('hAlarm').innerHTML=alarms.length
    ?alarms.slice(0,4).map(a=>`<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px;cursor:pointer" onclick="openCoin('${a.symbol.replace('USDT','')}')"><span style="font-weight:700;font-family:'Space Mono',monospace">${a.symbol.replace('USDT','')}</span><span style="color:var(--muted)">${a.type==='percent'?'%'+a.threshold:'$'+a.threshold}</span></div>`).join('')
    :'<div style="font-size:11px;color:var(--muted);padding:4px 0">Aktif alarm yok</div>';
  // Haberler
  document.getElementById('hNews').innerHTML=(d.news||[]).slice(0,3).map((n,i)=>newsCard(n,`hn${i}`)).join('');
  // Cache
  if(d.coins){allCoins=d.coins;d.coins.forEach(c=>{if(c.img)coin_image_cache_js[c.s.replace('USDT','').toLowerCase()]=c.img;});}
  if(d.top_data){topData=d.top_data;['g','l','v'].forEach(k=>{(d.top_data[k]||[]).forEach(c=>{if(c.img)coin_image_cache_js[c.s.replace('USDT','').toLowerCase()]=c.img;});});}
  if(d.flash5up)flash5Up=d.flash5up;if(d.flash5dn)flash5Dn=d.flash5dn;
  // Portföy özeti
  loadHomePortfoy();
}

async function loadHomePortfoy(){
  const el=document.getElementById('hPortfoy');
  if(!el)return;
  if(!UID){
    el.innerHTML='<div style="font-size:11px;color:var(--muted)">Telegram üzerinden açın</div>';
    return;
  }
  // Pozisyonları çek
  const d=await api(`/api/kar_pozisyon?uid=${UID}`);
  const poz=d?.positions||[];
  if(!poz.length){
    el.innerHTML=`<div style="font-size:11px;color:var(--muted);padding:4px 0">Pozisyon yok — <span style="color:var(--b);cursor:pointer" onclick="go('kar')">ekle →</span></div>`;
    return;
  }
  // Canlı fiyatları çek
  const syms=poz.map(p=>p.symbol).join(',');
  const pr=await api(`/api/prices?symbols=${syms}`);
  const prices=pr?.prices||{};
  // Hesapla
  let totalInv=0,totalCur=0;
  const rows=poz.map(p=>{
    const cur=prices[p.symbol]||p.buy_price;
    const inv=p.amount*p.buy_price;
    const curV=p.amount*cur;
    const pnl=curV-inv;
    const pct=((cur-p.buy_price)/p.buy_price)*100;
    totalInv+=inv;totalCur+=curV;
    return{sym:p.symbol.replace('USDT',''),pnl,pct,curV,cur,isUp:pnl>=0};
  });
  const totalPnl=totalCur-totalInv;
  const totalPct=totalInv>0?((totalCur-totalInv)/totalInv)*100:0;
  const isUp=totalPnl>=0;

  el.innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:8px">
      <div>
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Toplam Değer</div>
        <div style="font-size:18px;font-weight:800;font-family:'Space Mono',monospace">$${totalCur.toFixed(2)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">${isUp?'🟢 Toplam Kar':'🔴 Toplam Zarar'}</div>
        <div style="font-size:16px;font-weight:800;font-family:'Space Mono',monospace;color:${isUp?'var(--g)':'var(--r)'}">${totalPnl>=0?'+':''}$${totalPnl.toFixed(2)}</div>
        <div style="font-size:10px;color:${isUp?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace">${totalPct>=0?'+':''}${totalPct.toFixed(2)}%</div>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:5px">
      ${rows.slice(0,4).map(r=>`
        <div style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer" onclick="openCoin('${r.sym}')">
          ${cIco(r.sym)}
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:700">${r.sym}</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:11px;font-weight:700;color:${r.isUp?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace">${r.pnl>=0?'+':''}$${r.pnl.toFixed(2)}</div>
            <div style="font-size:9px;color:${r.isUp?'var(--g)':'var(--r)'}">${r.pct>=0?'+':''}${r.pct.toFixed(2)}%</div>
          </div>
        </div>`).join('')}
      ${rows.length>4?`<div style="font-size:10px;color:var(--muted);text-align:center;padding:4px 0;cursor:pointer" onclick="go('kar')">+${rows.length-4} pozisyon daha →</div>`:''}
    </div>`;
}

// ── HABER KARTI ──
function newsCard(n,id){
  return`<div class="news-card" onclick="toggleNews('${id}')">
    <div class="news-head">
      <div class="news-icon">📰</div>
      <div style="flex:1"><div class="news-title">${n.title_tr||n.title}</div><div class="news-src">${n.source} ${n.published_at||''}</div></div>
    </div>
    ${n.summary?`<div class="news-body" id="nb_${id}"><p style="margin-top:0">${n.summary}</p>${n.url?`<a href="${n.url}" target="_blank" style="color:var(--b);font-size:10px;display:block;margin-top:6px">Haberin Tamamı →</a>`:''}</div>`:''}
  </div>`;
}
function toggleNews(id){
  const el=document.getElementById('nb_'+id);
  if(el)el.classList.toggle('open');
}

// ── PİYASA ──
async function loadMkt(){
  if(allCoins.length)applyAndRender();
  else document.getElementById('mktList').innerHTML='<div class="ld"><div class="spin"></div>Yükleniyor...</div>';
  const d=await api(`/api/dashboard${UID?'?uid='+UID:''}`);
  if(d&&d.coins){allCoins=d.coins;if(d.top_data)topData=d.top_data;if(d.flash5up)flash5Up=d.flash5up;if(d.flash5dn)flash5Dn=d.flash5dn;applyAndRender();}
}
let mktPage=0; const MKT_PAGE_SIZE=50;
function applyAndRender(resetPage){
  if(resetPage!==false)mktPage=0;
  if(coinFilter==='flash5up'||coinFilter==='flash5dn'){
    const src=coinFilter==='flash5up'?flash5Up:flash5Dn;
    const q=(document.getElementById('mQ').value||'').toUpperCase().trim();
    let c=q?src.filter(x=>x.s.includes(q)):src;
    if(!c.length){document.getElementById('mktList').innerHTML='<div class="mt"><div class="mt-i">⏳</div><div class="mt-t">Veri Yok</div><div class="mt-s">WebSocket verisi doluyor</div></div>';return;}
    renderCoinList(c, 'mktList', true);
    return;
  }
  // Yükselen/Düşen filtrelerinde topData'yı kullan (ana sayfayla tutarlı)
  if(coinFilter==='up'){
    const q=(document.getElementById('mQ').value||'').toUpperCase().trim();
    let c=topData.g&&topData.g.length?[...topData.g]:[...allCoins].filter(x=>x.ch>0).sort((a,b)=>b.ch-a.ch);
    if(q)c=c.filter(x=>x.s.includes(q));
    renderCoinList(c,'mktList',false);
    return;
  }
  if(coinFilter==='dn'){
    const q=(document.getElementById('mQ').value||'').toUpperCase().trim();
    let c=topData.l&&topData.l.length?[...topData.l]:[...allCoins].filter(x=>x.ch<0).sort((a,b)=>a.ch-b.ch);
    if(q)c=c.filter(x=>x.s.includes(q));
    renderCoinList(c,'mktList',false);
    return;
  }
  let c=[...allCoins];
  const q=(document.getElementById('mQ').value||'').toUpperCase().trim();
  if(q)c=c.filter(x=>x.s.includes(q));
  const sv=document.getElementById('mSrt')?.value||'vol';
  if(sv==='up') c.sort((a,b)=>b.ch-a.ch);
  else if(sv==='dn') c.sort((a,b)=>a.ch-b.ch);
  else if(sv==='mc') c.sort((a,b)=>(a.rank||9999)-(b.rank||9999));
  else c.sort((a,b)=>b.v-a.v);
  renderCoinList(c, 'mktList', false);
}

function renderCoinList(coins, listId, isFlash5){
  const el=document.getElementById(listId);
  if(!coins.length){el.innerHTML='<div class="mt"><div class="mt-i">🔍</div><div class="mt-t">Sonuç yok</div></div>';return;}
  const start=mktPage*MKT_PAGE_SIZE;
  const slice=coins.slice(start, start+MKT_PAGE_SIZE);
  const hasMore=coins.length>start+MKT_PAGE_SIZE;
  const hasPrev=mktPage>0;
  let html=slice.map((x,i)=>{
    const base=x.s.replace('USDT','');
    const rank=x.rank&&x.rank<9000?`<span style="font-size:8px;color:var(--muted);font-family:'Space Mono',monospace;width:22px;text-align:right;flex-shrink:0">#${x.rank}</span>`:'';
    const chVal=isFlash5?x.ch5:x.ch;
    const chCls=isFlash5?(x.ch5>0?'up':'dn'):pc(chVal);
    return`<div class="cr" onclick="openCoin('${base}')">
      ${!isFlash5&&x.rank&&x.rank<9000?`<span style="font-size:8px;color:var(--muted2);font-family:'Space Mono',monospace;width:20px;text-align:center;flex-shrink:0">${start+i+1}</span>`:''}
      ${cIco(base,x.img||"")}
      <div class="cinfo">
        <div class="csym">${base}</div>
        <div class="cname">${isFlash5?`<span style="color:${chVal>0?'var(--g)':'var(--r)'}">5dk</span>`:fv(x.v)}</div>
      </div>
      <div class="cr-r">
        <div class="cpct ${chCls}">${pb(chVal)}</div>
        <div class="cprice">$${fp(x.p)}</div>
      </div>
    </div>`;
  }).join('');
  // Sayfalama
  if(hasPrev||hasMore){
    html+=`<div style="display:flex;gap:8px;padding:10px 0;justify-content:center">
      ${hasPrev?`<button class="btn" style="padding:8px 16px;font-size:12px" onclick="mktPage--;applyAndRender(false)">← Önceki</button>`:''}
      <span style="font-size:11px;color:var(--muted);align-self:center">${start+1}–${Math.min(start+MKT_PAGE_SIZE,coins.length)} / ${coins.length}</span>
      ${hasMore?`<button class="btn" style="padding:8px 16px;font-size:12px" onclick="mktPage++;applyAndRender(false)">Sonraki →</button>`:''}
    </div>`;
  }
  el.innerHTML=html;
}
function fltMkt(){applyAndRender();}
function srtMkt(){applyAndRender();}
function setF(f){coinFilter=f;['All','Up','Dn','Pump','Dump'].forEach(x=>{const e=document.getElementById('f'+x);if(e)e.classList.remove('on');});const m={all:'fAll',up:'fUp',dn:'fDn',flash5up:'fPump',flash5dn:'fDump'};const e=document.getElementById(m[f]);if(e)e.classList.add('on');applyAndRender();}

// ── LİDERLER ──
async function loadTop(){
  if(topData.g.length)showTop(topMode);
  else document.getElementById('topL').innerHTML='<div class="ld"><div class="spin"></div></div>';
  const d=await api(`/api/dashboard${UID?'?uid='+UID:''}`);
  if(d&&d.top_data){topData=d.top_data;if(d.coins)allCoins=d.coins;showTop(topMode);}
}
function showTop(m){
  topMode=m;
  ['G','L','V'].forEach(x=>{const e=document.getElementById('t'+x);if(e)e.classList.remove('on');});
  const mp={g:'tG',l:'tL',v:'tV'};const e=document.getElementById(mp[m]);if(e)e.classList.add('on');
  const data=topData[m]||[];
  document.getElementById('topL').innerHTML=data.length
    ?data.map((c,i)=>`<div class="cr" onclick="openCoin('${c.s.replace('USDT','')}')"><span class="crank">${i+1}</span>${cIco(c.s.replace('USDT',''),c.img||'')}<div class="cinfo"><div class="csym">${c.s.replace('USDT','')}</div><div class="cname">${fv(c.v)}</div></div><div class="cr-r"><div class="cpct ${pc(c.ch)}">${pb(c.ch)}</div><div class="cprice">$${fp(c.p)}</div></div></div>`).join('')
    :'<div class="ld">Veri yok</div>';
}

// ── ANALİZ ──
function _fp2(v){return v<1?v.toFixed(4):v>=1000?v.toLocaleString('tr-TR',{maximumFractionDigits:2}):v.toFixed(2);}
function _rsiCol(r){return r<30?'var(--g)':r>70?'var(--r)':r<45?'#ff9f0a':r>55?'#5ac8fa':'var(--muted)';}
function _rsiLbl(r){return r<30?'Aşırı Satım':r>70?'Aşırı Alım':r<45?'Zayıf':r>55?'Güçlü':'Nötr';}

async function doAnaliz(){
  const raw=(document.getElementById('aIn').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const sym=raw.endsWith('USDT')?raw:raw+'USDT';
  const out=document.getElementById('aOut');
  out.innerHTML='<div class="ld"><div class="spin"></div>Analiz yapılıyor...</div>';
  const d=await api(`/api/analiz?symbol=${sym}`);
  if(!d||d.error){out.innerHTML='<div class="mt"><div class="mt-i">⚠️</div><div class="mt-t">Veri alınamadı</div></div>';return;}

  const {score,bull_cnt,bear_cnt,signals,
         rsi1,rsi4,rsiD,rsi7_1h,
         macd1h,sig1h,hist1h,macd4h,sig4h,hist4h,
         srsi1h,srsi4h,
         bb_up1h,bb_mid1h,bb_lo1h,bb_pct1h,
         bb_up4h,bb_mid4h,bb_lo4h,
         e9_1h,e21_1h,e9_4h,e21_4h,e50_4h,e200_4h,e50_1d,e200_1d,
         atr1h,atr4h,atr_pct,vol_ratio1h,vol_ratio4h,
         ch1h,ch4h,ch24h,ch7d,sup,res,cur}=d;

  const col=score>=70?'var(--g)':score>=60?'#5ac8fa':score>=50?'#ffd60a':score>=40?'#ff9f0a':'var(--r)';
  const lbl=score>=70?'GÜÇLÜ AL':score>=60?'AL':score>=50?'NÖTR':score>=40?'SAT':'GÜÇLÜ SAT';
  const lbl_em=score>=70?'🟢':score>=60?'🔵':score>=50?'⚪':score>=40?'🟠':'🔴';

  // Sinyalleri kategoriye göre grupla
  const cats={trend:[],momentum:[],osc:[],volume:[],perf:[]};
  (signals||[]).forEach(s=>{ if(cats[s.cat]) cats[s.cat].push(s); });

  const catNames={trend:'📈 Trend',momentum:'⚡ Momentum',osc:'🔄 Osilatör',volume:'📦 Hacim',perf:'⏱ Performans'};

  function sigRows(arr){
    return arr.map(s=>`
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:.5px solid rgba(255,255,255,.05)">
        <span style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text)">
          <span style="width:7px;height:7px;border-radius:50%;background:${s.bull?'var(--g)':'var(--r)'};flex-shrink:0"></span>
          ${s.label}
        </span>
        <span style="font-size:10px;color:${s.bull?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace;font-weight:700">${s.val}</span>
      </div>`).join('');
  }

  function catBlock(key){
    const arr=cats[key]||[];
    if(!arr.length) return '';
    const bc=arr.filter(s=>s.bull).length;
    const br=arr.length-bc;
    return `<div style="margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.05em">${catNames[key]}</span>
        <span style="font-size:10px;font-family:'Space Mono',monospace">
          <span style="color:var(--g)">▲${bc}</span>
          <span style="color:var(--r);margin-left:4px">▼${br}</span>
        </span>
      </div>
      <div style="background:rgba(255,255,255,.03);border-radius:8px;padding:0 10px">${sigRows(arr)}</div>
    </div>`;
  }

  // RSI mini bar helper
  function rsiBar(val){
    const c=_rsiCol(val);
    const pct=val;
    return `<div style="position:relative;height:4px;background:rgba(255,255,255,.08);border-radius:2px;margin-top:4px">
      <div style="position:absolute;left:0;top:0;height:4px;width:${pct}%;background:${c};border-radius:2px;transition:width .4s"></div>
      <div style="position:absolute;left:30%;top:-1px;width:.5px;height:6px;background:rgba(255,255,255,.2)"></div>
      <div style="position:absolute;left:70%;top:-1px;width:.5px;height:6px;background:rgba(255,255,255,.2)"></div>
    </div>`;
  }

  out.innerHTML=`
  <!-- SKOR KARTI -->
  <div style="background:rgba(255,255,255,.04);border-radius:12px;padding:14px 16px;margin-bottom:8px;text-align:center;border:.5px solid ${col}40">
    <div style="font-size:11px;color:var(--muted);margin-bottom:6px">${sym} — Teknik Skor</div>
    <div style="font-size:32px;font-weight:900;color:${col};font-family:'Space Mono',monospace;line-height:1">${lbl_em} ${lbl}</div>
    <div style="font-size:13px;font-weight:700;color:${col};margin:6px 0 10px;font-family:'Space Mono',monospace">${score}/100</div>
    <div style="height:6px;background:rgba(255,255,255,.08);border-radius:3px;overflow:hidden">
      <div style="height:6px;width:${score}%;background:linear-gradient(90deg,var(--r),var(--y),var(--g));border-radius:3px;transition:width .6s;clip-path:inset(0 ${100-score}% 0 0 round 3px)"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:8px;font-size:10px">
      <span style="color:var(--r)">🔴 SAT</span>
      <span style="color:var(--muted)">🟢 ${bull_cnt} AL · 🔴 ${bear_cnt} SAT · Toplam ${(signals||[]).length} sinyal</span>
      <span style="color:var(--g)">AL 🟢</span>
    </div>
  </div>

  <!-- RSI KARTI -->
  <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:12px 14px;margin-bottom:8px">
    <div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:10px;letter-spacing:.05em">🔄 RSI GÖSTERGELERİ</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
      ${[['RSI 14','1s',rsi1],['RSI 14','4s',rsi4],['RSI 14','1g',rsiD]].map(([n,tf,v])=>`
        <div style="text-align:center">
          <div style="font-size:18px;font-weight:900;color:${_rsiCol(v)};font-family:'Space Mono',monospace">${Math.round(v)}</div>
          <div style="font-size:9px;color:var(--muted);margin:2px 0">${n} · ${tf}</div>
          <div style="font-size:9px;color:${_rsiCol(v)}">${_rsiLbl(v)}</div>
          ${rsiBar(v)}
        </div>`).join('')}
    </div>
  </div>

  <!-- MACD + BOLLINGER -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">⚡ MACD (1s)</div>
      <div style="font-size:11px;display:grid;gap:4px">
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">MACD</span><span style="color:${macd1h>0?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace">${macd1h.toFixed(4)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Sinyal</span><span style="font-family:'Space Mono',monospace;color:var(--text)">${sig1h.toFixed(4)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Hist</span><span style="color:${hist1h>0?'var(--g)':'var(--r)'};font-weight:700;font-family:'Space Mono',monospace">${hist1h>0?'▲':'▼'} ${Math.abs(hist1h).toFixed(4)}</span></div>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">⚡ MACD (4s)</div>
      <div style="font-size:11px;display:grid;gap:4px">
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">MACD</span><span style="color:${macd4h>0?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace">${macd4h.toFixed(4)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Sinyal</span><span style="font-family:'Space Mono',monospace;color:var(--text)">${sig4h.toFixed(4)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Hist</span><span style="color:${hist4h>0?'var(--g)':'var(--r)'};font-weight:700;font-family:'Space Mono',monospace">${hist4h>0?'▲':'▼'} ${Math.abs(hist4h).toFixed(4)}</span></div>
      </div>
    </div>
  </div>

  <!-- BOLLINGER + STOCH RSI -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">📊 BOLLINGER (1s)</div>
      <div style="font-size:11px;display:grid;gap:4px">
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Üst</span><span style="font-family:'Space Mono',monospace;color:var(--r)">${_fp2(bb_up1h)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Orta</span><span style="font-family:'Space Mono',monospace;color:var(--text)">${_fp2(bb_mid1h)}</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Alt</span><span style="font-family:'Space Mono',monospace;color:var(--g)">${_fp2(bb_lo1h)}</span></div>
        <div style="display:flex;justify-content:space-between;margin-top:2px"><span style="color:var(--muted)">%B</span><span style="font-weight:700;color:${bb_pct1h>80?'var(--r)':bb_pct1h<20?'var(--g)':'var(--y)'}">${bb_pct1h}%</span></div>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">🎯 STOCH RSI</div>
      <div style="font-size:11px;display:grid;gap:6px">
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--muted)">1 Saat</span><span style="color:${srsi1h<20?'var(--g)':srsi1h>80?'var(--r)':'var(--y)'};font-weight:700">${Math.round(srsi1h)}</span></div>
          <div style="height:4px;background:rgba(255,255,255,.08);border-radius:2px"><div style="height:4px;width:${srsi1h}%;background:${srsi1h<20?'var(--g)':srsi1h>80?'var(--r)':'var(--y)'};border-radius:2px"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="color:var(--muted)">4 Saat</span><span style="color:${srsi4h<20?'var(--g)':srsi4h>80?'var(--r)':'var(--y)'};font-weight:700">${Math.round(srsi4h)}</span></div>
          <div style="height:4px;background:rgba(255,255,255,.08);border-radius:2px"><div style="height:4px;width:${srsi4h}%;background:${srsi4h<20?'var(--g)':srsi4h>80?'var(--r)':'var(--y)'};border-radius:2px"></div></div>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px">ATR(14): <span style="color:var(--text)">${_fp2(atr1h)}</span> · <span style="color:${atr_pct>3?'var(--r)':atr_pct>1.5?'var(--y)':'var(--g)'}">${atr_pct}%</span></div>
      </div>
    </div>
  </div>

  <!-- EMA SEVİYELERİ -->
  <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 14px;margin-bottom:8px">
    <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">📐 EMA SEVİYELERİ</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px">
      ${[['EMA 9','1s',e9_1h],['EMA 21','1s',e21_1h],['EMA 9','4s',e9_4h],['EMA 21','4s',e21_4h],
         ['EMA 50','4s',e50_4h],['EMA 200','4s',e200_4h],['EMA 50','1g',e50_1d],['EMA 200','1g',e200_1d]].map(([n,tf,v])=>`
        <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:.5px solid rgba(255,255,255,.04)">
          <span style="color:var(--muted)">${n} <span style="font-size:9px">(${tf})</span></span>
          <span style="font-family:'Space Mono',monospace;color:${cur>v?'var(--g)':'var(--r)'};font-weight:700">${_fp2(v)}</span>
        </div>`).join('')}
    </div>
  </div>

  <!-- DESTEK/DİRENÇ + PERFORMANS -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">🎯 DESTEK / DİRENÇ</div>
      <div style="font-size:11px;display:grid;gap:5px">
        ${res?`<div style="display:flex;justify-content:space-between"><span style="color:var(--r)">Direnç</span><span style="font-family:'Space Mono',monospace;color:var(--r)">${_fp2(res)}</span></div>`:''}
        <div style="display:flex;justify-content:space-between"><span style="color:var(--muted)">Fiyat</span><span style="font-family:'Space Mono',monospace;color:var(--text);font-weight:700">${_fp2(cur)}</span></div>
        ${sup?`<div style="display:flex;justify-content:space-between"><span style="color:var(--g)">Destek</span><span style="font-family:'Space Mono',monospace;color:var(--g)">${_fp2(sup)}</span></div>`:''}
        <div style="display:flex;justify-content:space-between;border-top:.5px solid rgba(255,255,255,.05);padding-top:5px;margin-top:2px">
          <span style="color:var(--muted)">Hacim(1s)</span>
          <span style="color:${vol_ratio1h>2?'var(--g)':vol_ratio1h>1?'var(--y)':'var(--muted)'}">${vol_ratio1h}x</span>
        </div>
      </div>
    </div>
    <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:8px">⏱ PERFORMANS</div>
      <div style="font-size:11px;display:grid;gap:5px">
        ${[['1 Saat',ch1h],['4 Saat',ch4h],['24 Saat',ch24h],['7 Gün',ch7d]].map(([lbl,v])=>`
          <div style="display:flex;justify-content:space-between">
            <span style="color:var(--muted)">${lbl}</span>
            <span style="color:${v>0?'var(--g)':'var(--r)'};font-weight:700;font-family:'Space Mono',monospace">${v>0?'+':''}${v.toFixed(2)}%</span>
          </div>`).join('')}
      </div>
    </div>
  </div>

  <!-- TÜM SİNYALLER -->
  <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 14px;margin-bottom:8px">
    <div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:10px;letter-spacing:.05em">📊 TÜM SİNYALLER (${(signals||[]).length})</div>
    ${['trend','momentum','osc','volume','perf'].map(catBlock).join('')}
  </div>
  `;
}

// ── TAKVİM & HABERLER ──
async function loadTakvim(){
  document.getElementById('calList').innerHTML='<div class="ld"><div class="spin"></div></div>';
  document.getElementById('newsListFull').innerHTML='<div class="ld"><div class="spin"></div></div>';
  const d=await api('/api/takvim_news');
  if(d){
    // Takvim
    const events=d.events||[];
    document.getElementById('calList').innerHTML=events.length
      ?events.map(e=>`<div class="cal-item">
          <div class="cal-imp ${e.importance==='high'?'imp-high':e.importance==='medium'?'imp-med':'imp-low'}"></div>
          <div style="flex:1"><div class="cal-name">${e.name}</div><div class="cal-date">${e.date} · ${e.country||''}${e.forecast?` · Beklenti: ${e.forecast}`:''}</div></div>
          ${e.importance==='high'?'<span class="bdg br">Kritik</span>':e.importance==='medium'?'<span class="bdg by">Önemli</span>':''}
        </div>`).join('')
      :'<div style="font-size:11px;color:var(--muted)">Takvim verisi yüklenemedi</div>';
    // Haberler
    const news=d.news||[];
    document.getElementById('newsListFull').innerHTML=news.length
      ?news.map((n,i)=>newsCard(n,`tn${i}`)).join('')
      :'<div style="font-size:11px;color:var(--muted)">Haber yüklenemedi</div>';
  }
}

// ── KAR/ZARAR ──
let kzMode='hesap';
function kzSwitch(m){kzMode=m;document.getElementById('kzT1').classList.toggle('on',m==='hesap');document.getElementById('kzT2').classList.toggle('on',m==='pozisyon');document.getElementById('kzHesap').style.display=m==='hesap'?'block':'none';document.getElementById('kzPoz').style.display=m==='pozisyon'?'block':'none';if(m==='pozisyon')loadKarPoz();}
async function kzHesapla(){
  const sym=(document.getElementById('kzSym').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const symF=sym.endsWith('USDT')?sym:sym+'USDT';
  const buy=parseFloat(document.getElementById('kzBuy').value);
  const amt=parseFloat(document.getElementById('kzAmt').value);
  const sellIn=parseFloat(document.getElementById('kzSell').value);
  const res=document.getElementById('kzResult');
  if(!sym||isNaN(buy)||isNaN(amt)||buy<=0||amt<=0){toast('⚠️ Sembol, alış ve miktar zorunlu');return;}
  res.innerHTML='<div class="ld"><div class="spin"></div>Fiyat alınıyor...</div>';
  const d=await api(`/api/price?symbol=${symF}`);
  const cur=(!isNaN(sellIn)&&sellIn>0)?sellIn:(d?.price||0);
  if(!cur){res.innerHTML='<div style="color:var(--r);padding:10px">⚠️ Fiyat alınamadı</div>';return;}
  const inv=amt*buy,curV=amt*cur,pnl=curV-inv,pct=((cur-buy)/buy)*100,isUp=pnl>=0;
  res.innerHTML=`<div class="kz-result">
    <div class="kz-big ${isUp?'up':'dn'}">${isUp?'🟢':'🔴'} ${pct>=0?'+':''}${pct.toFixed(2)}%</div>
    <div class="kz-row"><span class="kz-label">Alış Fiyatı</span><span class="kz-val">$${fp(buy)}</span></div>
    <div class="kz-row"><span class="kz-label">Miktar</span><span class="kz-val">${amt}</span></div>
    <div class="kz-row"><span class="kz-label">Şu An</span><span class="kz-val ${isUp?'up':'dn'}">$${fp(cur)}</span></div>
    <div class="kz-row"><span class="kz-label">Yatırılan</span><span class="kz-val">$${inv.toFixed(2)}</span></div>
    <div class="kz-row"><span class="kz-label">Güncel Değer</span><span class="kz-val">$${curV.toFixed(2)}</span></div>
    <div class="kz-row"><span class="kz-label">${isUp?'🟢 KAR':'🔴 ZARAR'}</span><span class="kz-val ${isUp?'up':'dn'}" style="font-size:17px">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span></div>
  </div>`;
}
async function kzKaydet(){
  const sym=(document.getElementById('kzSym').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const symF=sym.endsWith('USDT')?sym:sym+'USDT';
  const buy=parseFloat(document.getElementById('kzBuy').value);
  const amt=parseFloat(document.getElementById('kzAmt').value);
  if(!sym||isNaN(buy)||isNaN(amt)||buy<=0||amt<=0){toast('⚠️ Eksik bilgi');return;}
  if(!UID){toast('⚠️ Telegram hesabı gerekli');return;}
  const d=await api(`/api/kar_kaydet?uid=${UID}&symbol=${symF}&amount=${amt}&buy_price=${buy}`);
  if(d?.ok)toast('✅ Kaydedildi!');else toast('⚠️ '+(d?.error||'Hata'));
}
async function loadKarPoz(){
  if(!UID){document.getElementById('kzPozList').innerHTML='<div class="mt"><div class="mt-i">🔒</div><div class="mt-s">Telegram üzerinden açın</div></div>';return;}
  document.getElementById('kzPozList').innerHTML='<div class="ld"><div class="spin"></div></div>';
  const d=await api(`/api/kar_pozisyon?uid=${UID}`);
  const poz=d?.positions||[];
  if(!poz.length){document.getElementById('kzPozList').innerHTML='<div class="mt"><div class="mt-i">📭</div><div class="mt-t">Pozisyon Yok</div></div>';return;}
  const syms=poz.map(p=>p.symbol).join(',');
  const pr=await api(`/api/prices?symbols=${syms}`);
  const prices=pr?.prices||{};
  document.getElementById('kzPozList').innerHTML=poz.map(p=>{
    const cur=prices[p.symbol]||p.buy_price;
    const inv=p.amount*p.buy_price,curV=p.amount*cur,pnl=curV-inv,pct=((cur-p.buy_price)/p.buy_price)*100,isUp=pnl>=0;
    return`<div class="pos-card">
      <div class="pos-head"><div class="pos-sym">${p.symbol.replace('USDT','')}</div>
        <div style="display:flex;align-items:center;gap:9px"><span class="bdg ${isUp?'bg':'br'}">${pct>=0?'+':''}${pct.toFixed(2)}%</span>
        <span style="font-size:19px;cursor:pointer;opacity:.5" onclick="kzDel('${p.symbol}')">🗑</span></div>
      </div>
      <div class="pos-grid">
        <div class="pos-item"><div class="pos-item-l">Alış</div><div class="pos-item-v">$${fp(p.buy_price)}</div></div>
        <div class="pos-item"><div class="pos-item-l">Miktar</div><div class="pos-item-v">${p.amount}</div></div>
        <div class="pos-item"><div class="pos-item-l">Şu An</div><div class="pos-item-v ${isUp?'up':'dn'}">$${fp(cur)}</div></div>
        <div class="pos-item"><div class="pos-item-l">${isUp?'🟢 Kar':'🔴 Zarar'}</div><div class="pos-item-v ${isUp?'up':'dn'}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div></div>
      </div>
    </div>`;
  }).join('');
}
async function kzDel(sym){if(!UID||!confirm('Silinsin mi?'))return;const d=await api(`/api/kar_sil?uid=${UID}&symbol=${sym}`);if(d?.ok){toast('🗑 Silindi');loadKarPoz();}else toast('⚠️ Silinemedi');}

// ── ALARMLAR ──
let alarmTab='liste';
function alarmSwitch(m){alarmTab=m;document.getElementById('alTL').classList.toggle('on',m==='liste');document.getElementById('alTE').classList.toggle('on',m==='ekle');document.getElementById('alListe').style.display=m==='liste'?'block':'none';document.getElementById('alEkle').style.display=m==='ekle'?'block':'none';if(m==='liste')loadAlarms();}
async function alarmEkle(){
  if(!UID){toast('⚠️ Telegram hesabı gerekli');return;}
  const sym=(document.getElementById('alSym').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const symF=sym.endsWith('USDT')?sym:sym+'USDT';
  const type=document.getElementById('alType').value;
  const thr=parseFloat(document.getElementById('alThr').value);
  if(!sym||isNaN(thr)||thr<=0){toast('⚠️ Sembol ve değer zorunlu');return;}
  const d=await api(`/api/alarm_ekle?uid=${UID}&symbol=${symF}&type=${type}&threshold=${thr}`);
  if(d?.ok){toast('✅ Alarm eklendi!');document.getElementById('alSym').value='';document.getElementById('alThr').value='';alarmSwitch('liste');}
  else toast('⚠️ '+(d?.error||'Hata'));
}
async function alarmSil(sym){if(!UID||!confirm('Alarm silinsin mi?'))return;const d=await api(`/api/alarm_sil?uid=${UID}&symbol=${sym}`);if(d?.ok){toast('🗑 Silindi');loadAlarms();}else toast('⚠️ Silinemedi');}
async function loadAlarms(){
  if(!UID){document.getElementById('alListe').innerHTML='<div class="mt"><div class="mt-i">🔒</div><div class="mt-t">Giriş Gerekli</div><div class="mt-s">Telegram üzerinden açın</div></div>';return;}
  document.getElementById('alListe').innerHTML='<div class="ld"><div class="spin"></div></div>';
  const d=await api(`/api/alarms?uid=${UID}`);
  const alarms=d?.alarms||[];
  if(!alarms.length){document.getElementById('alListe').innerHTML='<div class="mt"><div class="mt-i">🔔</div><div class="mt-t">Alarm Yok</div><div class="mt-s">+ Ekle sekmesinden ekleyin</div></div>';return;}
  document.getElementById('alListe').innerHTML=alarms.map(a=>{
    const st=a.active?'<span class="bdg bg" style="font-size:9px;padding:2px 7px">Aktif</span>':a.paused?'<span class="bdg by" style="font-size:9px;padding:2px 7px">Duraklı</span>':'<span class="bdg br" style="font-size:9px;padding:2px 7px">Pasif</span>';
    const typeIcon=a.type==='percent'?'📊':'💰';
    const val=a.type==='percent'?`%${a.threshold}`:`$${a.threshold}`;
    return`<div class="alarm-card" onclick="openCoin('${a.symbol.replace('USDT','')}')">
      <div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0">
        ${cIco(a.symbol.replace('USDT',''),coin_image_cache_js[a.symbol.replace('USDT','').toLowerCase()]||'')}
        <div style="min-width:0">
          <div style="font-size:14px;font-weight:800;font-family:'Space Mono',monospace;letter-spacing:-.2px">${a.symbol.replace('USDT','')}</div>
          <div style="font-size:10px;color:var(--muted);margin-top:2px;font-family:'Space Mono',monospace">${typeIcon} ${val} tetiklemede</div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
        ${st}
        <span style="font-size:19px;cursor:pointer;opacity:.45;transition:opacity .15s" onclick="event.stopPropagation();alarmSil('${a.symbol}')">🗑</span>
      </div>
    </div>`;
  }).join('');
}

// ── FİBONACCİ SAYFA ──
async function doFibPage(){
  const raw=(document.getElementById('fibIn').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const sym=raw.endsWith('USDT')?raw:raw+'USDT';
  const tf=document.getElementById('fibTF').value||'4h';
  const out=document.getElementById('fibOut');
  out.innerHTML='<div class="ld"><div class="spin"></div>Yükleniyor...</div>';

  const [klinesRes, fib] = await Promise.all([
    api(`/api/klines?symbol=${sym}&interval=${tf}&limit=80`),
    api(`/api/fib?symbol=${sym}&interval=${tf}`)
  ]);

  if(!klinesRes||!klinesRes.klines||klinesRes.klines.length<2){
    out.innerHTML='<div class="mt"><div class="mt-i">⚠️</div><div class="mt-t">Veri alınamadı</div></div>';return;
  }

  const klines=klinesRes.klines;
  const FCOL={'0':'#ff2d55','23.6':'#ff9f0a','38.2':'#ffd60a','50':'#8a9ab0','61.8':'#05d890','78.6':'#0a84ff','100':'#bf5af2'};

  const hasFib=fib&&fib.levels&&fib.levels.length;
  const trendUp=fib&&fib.trend_up;
  const trendLbl=trendUp?'📈 Yukarı Trend':'📉 Aşağı Trend';
  const trendCol=trendUp?'var(--g)':'var(--r)';

  // Fib seviyeleri içinde fiyatın yüzdesi (range bar için)
  const rangePos = hasFib ? Math.min(100, Math.max(0,
    ((fib.cur - fib.low) / (fib.high - fib.low)) * 100
  )) : 50;

  out.innerHTML=`
  <!-- GRAFIK -->
  <div class="chart-wrap" style="margin-bottom:9px">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 11px;border-bottom:1px solid rgba(255,255,255,.06)">
      <span style="font-size:9px;color:var(--muted);font-family:'Space Mono',monospace;font-weight:700">${sym} · ${tf.toUpperCase()} · FİBONACCİ</span>
      <span style="font-size:9px;font-family:'Space Mono',monospace" id="fibOHLC"></span>
    </div>
    <canvas id="fibCanvas" height="220" style="display:block;width:100%"></canvas>
  </div>

  ${hasFib ? `
  <!-- ÖZET KART -->
  <div style="background:rgba(255,255,255,.04);border-radius:12px;padding:12px 14px;margin-bottom:8px;border:.5px solid rgba(255,255,255,.08)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div>
        <div style="font-size:11px;font-weight:700;color:${trendCol}">${trendLbl}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px">Retracement: <span style="color:var(--text);font-weight:700">${fib.retrace_pct}%</span></div>
      </div>
      <div style="text-align:right">
        <div style="font-size:9px;color:var(--muted)">Bölge</div>
        <div style="font-size:11px;font-weight:700;color:var(--b);font-family:'Space Mono',monospace">${fib.zone_label}</div>
      </div>
    </div>

    <!-- Swing range bar -->
    <div style="margin-bottom:6px">
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-bottom:4px">
        <span>Low $${fp(fib.low)}</span>
        <span style="color:#0a84ff;font-weight:700">▼ $${fp(fib.cur)}</span>
        <span>High $${fp(fib.high)}</span>
      </div>
      <div style="position:relative;height:8px;background:rgba(255,255,255,.08);border-radius:4px;overflow:visible">
        <!-- Fib seviyeleri bar üzerinde -->
        ${fib.levels.map(l=>{
          const pos=((l.price-fib.low)/(fib.high-fib.low)*100).toFixed(1);
          const col=FCOL[String(l.pct)]||'#5577aa';
          return`<div style="position:absolute;left:${pos}%;top:-1px;width:1.5px;height:10px;background:${col};opacity:.7"></div>`;
        }).join('')}
        <!-- Dolgu -->
        <div style="height:8px;width:${rangePos.toFixed(1)}%;background:linear-gradient(90deg,var(--r),var(--y),var(--g));border-radius:4px;max-width:100%"></div>
        <!-- Fiyat işareti -->
        <div style="position:absolute;left:${rangePos.toFixed(1)}%;top:-3px;transform:translateX(-50%);width:4px;height:14px;background:#0a84ff;border-radius:2px;box-shadow:0 0 6px rgba(10,132,255,.8)"></div>
      </div>
    </div>

    <!-- En yakın destek/direnç -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px">
      <div style="background:rgba(5,216,144,.08);border:.5px solid rgba(5,216,144,.2);border-radius:8px;padding:8px 10px">
        <div style="font-size:9px;color:var(--g);font-weight:700;margin-bottom:3px">▲ En Yakın Destek</div>
        ${fib.nearest_sup ? `
          <div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:var(--g)">$${fp(fib.nearest_sup.price)}</div>
          <div style="font-size:9px;color:var(--muted)">Fib %${fib.nearest_sup.pct} · <span style="color:var(--g)">${fib.nearest_sup.dist.toFixed(2)}%</span></div>
        ` : '<div style="font-size:10px;color:var(--muted)">—</div>'}
      </div>
      <div style="background:rgba(255,45,85,.08);border:.5px solid rgba(255,45,85,.2);border-radius:8px;padding:8px 10px">
        <div style="font-size:9px;color:var(--r);font-weight:700;margin-bottom:3px">▼ En Yakın Direnç</div>
        ${fib.nearest_res ? `
          <div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:var(--r)">$${fp(fib.nearest_res.price)}</div>
          <div style="font-size:9px;color:var(--muted)">Fib %${fib.nearest_res.pct} · <span style="color:var(--r)">+${fib.nearest_res.dist.toFixed(2)}%</span></div>
        ` : '<div style="font-size:10px;color:var(--muted)">—</div>'}
      </div>
    </div>
  </div>

  <!-- TÜM SEVİYELER -->
  <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 14px;margin-bottom:8px">
    <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.08em;margin-bottom:8px">📊 FİBONACCİ SEVİYELERİ</div>
    ${fib.levels.map(l=>{
      const col=FCOL[String(l.pct)]||'#5577aa';
      const isZoneLo=fib.zone_lo&&fib.zone_lo.pct===l.pct;
      const isZoneHi=fib.zone_hi&&fib.zone_hi.pct===l.pct;
      const isCur=Math.abs(l.price-fib.cur)/fib.cur<0.012;
      const isSup=fib.nearest_sup&&fib.nearest_sup.pct===l.pct;
      const isRes=fib.nearest_res&&fib.nearest_res.pct===l.pct;
      const bg=isCur?'rgba(10,132,255,.12)':isZoneLo||isZoneHi?'rgba(255,255,255,.03)':'transparent';
      const border=isCur?'.5px solid rgba(10,132,255,.4)':'none';
      const badge=isCur
        ?`<span style="font-size:8px;background:rgba(10,132,255,.2);color:#0a84ff;padding:2px 6px;border-radius:10px;font-weight:700">◀ FİYAT</span>`
        :isSup?`<span style="font-size:8px;background:rgba(5,216,144,.15);color:var(--g);padding:2px 6px;border-radius:10px">DESTEK</span>`
        :isRes?`<span style="font-size:8px;background:rgba(255,45,85,.15);color:var(--r);padding:2px 6px;border-radius:10px">DİRENÇ</span>`:'';
      return`<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 8px;margin:0 -8px;border-radius:8px;background:${bg};border:${border}">
        <div style="display:flex;align-items:center;gap:7px">
          <div style="width:3px;height:16px;border-radius:2px;background:${col};flex-shrink:0"></div>
          <div>
            <span style="font-size:10px;color:${col};font-weight:700;font-family:'Space Mono',monospace">%${l.pct}</span>
            ${badge}
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-size:13px;font-weight:800;font-family:'Space Mono',monospace;color:${isCur?'#0a84ff':col}">$${fp(l.price)}</div>
          <div style="font-size:9px;color:${l.dist>=0?'var(--g)':'var(--r)'};font-family:'Space Mono',monospace">${l.dist>=0?'+':''}${l.dist.toFixed(2)}%</div>
        </div>
      </div>`;
    }).join('')}
  </div>

  <!-- VOLATİLİTE -->
  <div style="background:rgba(255,255,255,.03);border-radius:10px;padding:10px 14px;margin-bottom:8px">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:10px;color:var(--muted)">Hacim Trendi</span>
      <span style="font-size:11px;font-weight:700;color:${fib.vol_ratio>1.2?'var(--g)':fib.vol_ratio<0.8?'var(--r)':'var(--muted)'}">
        ${fib.vol_ratio}x ${fib.vol_ratio>1.2?'↑ Artıyor':fib.vol_ratio<0.8?'↓ Azalıyor':'→ Normal'}
      </span>
    </div>
  </div>
  ` : ''}
  `;

  requestAnimationFrame(()=>drawCandleFib('fibCanvas', klines, fib, 'fibOHLC'));
}

// ── INIT ──
// Sidebar - touchend kullan (Telegram WebView click offset sorununu cözer)
document.querySelectorAll('#sidenav .nb').forEach(btn=>{
  let _tStart=0;
  btn.addEventListener('touchstart', function(e){
    _tStart=Date.now();
  }, {passive:true});
  btn.addEventListener('touchend', function(e){
    e.preventDefault();
    e.stopPropagation();
    if(Date.now()-_tStart>500) return; // Uzun basma degil
    const page=this.closest('[data-page]')||this;
    const p=page.dataset.page||this.dataset.page;
    if(p) go(p);
  }, {passive:false});
  // Desktop fallback
  btn.addEventListener('click', function(e){
    const p=this.dataset.page;
    if(p) go(p);
  });
});

// kz-tab ve alarm-tab butonlari da ayni sekilde
function bindTabBtns(selector, handler){
  document.querySelectorAll(selector).forEach(btn=>{
    let _ts=0;
    btn.addEventListener('touchstart',()=>_ts=Date.now(),{passive:true});
    btn.addEventListener('touchend',function(e){
      e.preventDefault();e.stopPropagation();
      if(Date.now()-_ts>500)return;
      handler(this);
    },{passive:false});
    btn.addEventListener('click',function(){handler(this);});
  });
}
bindTabBtns('#kzT1,#kzT2', btn=>{
  kzSwitch(btn.id==='kzT1'?'hesap':'pozisyon');
});
bindTabBtns('#alTL,#alTE', btn=>{
  alarmSwitch(btn.id==='alTL'?'liste':'ekle');
});

loadHome();
setInterval(()=>{if(CUR==='home')loadHome();},60000);
</script>
</body>
</html>"""
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

        import json as _json

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
                log.warning(f"Haber cevirisi basarisiz: {e}")
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
                    import json as _json5
                    syms_param = _json5.dumps(top_syms)
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

        async def handle_price(request):
            """Tek sembol anlık fiyat."""
            sym = request.rel_url.query.get("symbol","BTCUSDT").upper()
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}",
                                     timeout=aiohttp.ClientTimeout(total=6)) as r:
                        d = await r.json()
                return aiohttp_web.Response(
                    text=_json.dumps({"price":float(d.get("price",0))}),
                    content_type="application/json", headers=CORS_HEADERS
                )
            except Exception as e:
                return aiohttp_web.Response(text=_json.dumps({"error":str(e)}),
                    content_type="application/json", headers=CORS_HEADERS)

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
            result = {
                "events": [
                    {"name":"FED Faiz Karari","date":"2025-05-07","country":"ABD","importance":"high","forecast":"Sabit bekleniyor"},
                    {"name":"ABD TUFE (Enflasyon)","date":"2025-04-10","country":"ABD","importance":"high","forecast":""},
                    {"name":"ABD Tarim Disi Istihdam","date":"2025-05-02","country":"ABD","importance":"high","forecast":""},
                    {"name":"ECB Faiz Karari","date":"2025-04-17","country":"Avrupa","importance":"high","forecast":""},
                    {"name":"ABD GSYH (Buyume)","date":"2025-04-30","country":"ABD","importance":"medium","forecast":""},
                    {"name":"ABD Hazine Borc Tavani","date":"2025-06-01","country":"ABD","importance":"high","forecast":""},
                    {"name":"Kripto Duzenleme Haberleri","date":"Surekli","country":"Global","importance":"medium","forecast":""},
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

    app.job_queue.run_repeating(alarm_job,            interval=10,   first=30)
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
