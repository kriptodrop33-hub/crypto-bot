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


# ================= FİBONACCİ =================

FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_COLORS = ["#FFD700","#FF8C00","#FF4500","#00CED1","#1E90FF","#9370DB","#32CD32"]

async def generate_fib_chart(symbol: str, interval: str = "4h", limit: int = 100):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

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

        fib_prices = {}
        for lvl in FIB_LEVELS:
            fib_prices[lvl] = swing_high - diff * lvl if trend_up else swing_low + diff * lvl

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
            title=f"\n{symbol} - Fibonacci Retracement ({interval})",
            ylabel="Fiyat (USDT)", volume=True, figsize=(10, 6),
            returnfig=True
        )
        ax = axes[0]

        for lvl, color in zip(FIB_LEVELS, FIB_COLORS):
            price = fib_prices[lvl]
            ax.axhline(y=price, color=color, linewidth=1.0, linestyle="--", alpha=0.85)
            label_price = f"{price:,.4f}" if price < 1 else f"{price:,.2f}"
            ax.text(
                len(df) * 0.01, price,
                f" {lvl:.3f} — {label_price}",
                color=color, fontsize=7, va="bottom", alpha=0.95
            )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=100, facecolor="#0d1117")
        plt.close(fig)
        buf.seek(0)

        cur = df["close"].iloc[-1]
        nearest = min(fib_prices.items(), key=lambda x: abs(x[1]-cur))
        text = (
            f"📐 *{symbol} — Fibonacci Retracement* ({interval})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *Swing High:* `{swing_high:,.4f}` USDT\n" if swing_high < 1 else
            f"📐 *{symbol} — Fibonacci Retracement* ({interval})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *Swing High:* `{swing_high:,.2f}` USDT\n"
        )
        text += (
            f"📉 *Swing Low:*  `{swing_low:,.2f}` USDT\n"
            f"🎯 *Mevcut:* `{cur:,.2f}` USDT\n"
            f"🔍 *En yakın Fib:* `{nearest[0]:.3f}` → `{nearest[1]:,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        for lvl in FIB_LEVELS:
            p = fib_prices[lvl]
            marker = "◀️" if lvl == nearest[0] else "  "
            lp = f"{p:,.4f}" if p < 1 else f"{p:,.2f}"
            text += f"{marker}`{lvl:.3f}` → `{lp}` USDT\n"

        return buf, text
    except Exception as e:
        log.error(f"Fib grafik hatasi ({symbol}): {e}")
        return None, None

async def fib_command(update: Update, context):
    chat    = update.effective_chat
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
                "model": "llama3-8b-8192",
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
    msg      = await context.bot.send_message(chat.id, text, parse_mode="Markdown")
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

    return unique[:20]

async def takvim_command(update: Update, context):
    await register_user(update)
    chat = update.effective_chat
    loading = await send_temp(context.bot, chat.id, "📅 Ekonomik takvim yükleniyor...", parse_mode="Markdown")
    events  = await fetch_crypto_calendar()
    try: await context.bot.delete_message(chat.id, loading.message_id)
    except Exception: pass

    now = datetime.utcnow()
    text = "📅 *Ekonomik & Kripto Takvim*\n━━━━━━━━━━━━━━━━━━━━━\n"
    for ev in events[:8]:
        try:
            ev_dt  = datetime.strptime(ev["date"], "%Y-%m-%d")
            diff   = (ev_dt.date() - now.date()).days
            zamanl = "⚡ *BUGÜN*" if diff==0 else ("🔜 Yarın" if diff==1 else (f"📆 {diff}g sonra" if diff<7 else f"📆 {ev['date']}"))
            imp    = ev.get("importance", 0)
            imp_str= "🔴" if imp>=80 else ("🟡" if imp>=50 else "🟢")
            coins  = f" `{ev['coins']}`" if ev.get("coins") else ""
            text  += f"\n{imp_str} {zamanl}\n📌 {ev['title']}{coins}\n"
            if ev.get("desc"):
                text += f"   _{ev['desc']}_\n"
        except Exception:
            pass
    text += f"\n━━━━━━━━━━━━━━━━━━━━━\n⏰ _{now.strftime('%d.%m.%Y %H:%M')} UTC_"

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
        text = "📅 *Bugünkü Önemli Ekonomik Olaylar*\n━━━━━━━━━━━━━━━━━━━━━\n"
        for ev in bugun_evs:
            text += f"\n🔴 *{ev['title']}*\n"
            if ev.get("desc"): text += f"_{ev['desc']}_\n"
        text += "\n💡 _Kapatmak için /takvim → 'Bildirim Kapat'_"
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
    await register_user(update)
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

    await register_user(update)

    # ── Grup butonları (herkes görür) ──
    group_buttons = [
        [InlineKeyboardButton("📊 Market",        callback_data="market"),
         InlineKeyboardButton("⚡ 5dk Flashlar",  callback_data="top5")],
        [InlineKeyboardButton("📈 24s Liderleri", callback_data="top24"),
         InlineKeyboardButton("⚙️ Durum",         callback_data="status")],
        [InlineKeyboardButton("💬 Gruba Katıl",   url="https://t.me/kriptodroptr"),
         InlineKeyboardButton("📢 Kanala Katıl",  url="https://t.me/kriptodropduyuru")],
    ]

    # ── DM butonları (tam menü) ──
    dm_buttons = [
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
        [InlineKeyboardButton("📐 Fibonacci",      callback_data="fib_help"),
         InlineKeyboardButton("🧠 Sentiment",      callback_data="sent_help")],
        [InlineKeyboardButton("📅 Ekonomik Takvim",callback_data="takvim_refresh"),
         InlineKeyboardButton("📚 Terim Sözlüğü", callback_data="ne_help")],
        [InlineKeyboardButton("💬 Gruba Katıl",   url="https://t.me/kriptodroptr"),
         InlineKeyboardButton("📢 Kanala Katıl",  url="https://t.me/kriptodropduyuru")],
    ]

    # Mini App butonu — URL runtime'da alınır (server başladıktan sonra da çalışır)
    _murl = get_miniapp_url()
    if _murl:
        dm_buttons.insert(-1, [InlineKeyboardButton(
            "🖥 Dashboard Mini App", web_app={"url": _murl}
        )])

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
        keyboard    = InlineKeyboardMarkup(group_buttons)
        welcome_text = (
            "👋 *Kripto Analiz Asistanı*\n━━━━━━━━━━━━━━━━━━\n"
            "7/24 piyasayı izliyorum.\n\n"
            "💡 Coin analizi için sembol yaz: `BTCUSDT`\n"
            "📌 Tüm özellikler için bota *DM* yaz!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💬 [Kripto Drop Grubu](https://t.me/kriptodroptr)\n"
            "📣 [Kripto Drop Duyuru](https://t.me/kriptodropduyuru)"
        )
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

async def dashboard_command(update: Update, context):
    """/dashboard — Mini App'i açar veya URL bilgisi verir."""
    await register_user(update)
    chat    = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else None
    is_group = chat and chat.type in ("group", "supergroup")

    murl = get_miniapp_url()

    if murl:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🖥 Dashboard'u Aç", web_app={"url": murl})
        ]])
        msg = await context.bot.send_message(
            chat.id,
            "🖥 *Kripto Drop Dashboard*\nAşağıdaki butona tıklayarak açın:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        # URL henüz hazır değil — kullanıcıya bilgi ver
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
    user_id = update.effective_user.id if update.effective_user else None
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
        await q.answer()
        if q.data == "takvim_refresh":
            events = await fetch_crypto_calendar()
            now    = datetime.utcnow()
            text   = "📅 *Ekonomik & Kripto Takvim*\n━━━━━━━━━━━━━━━━━━━━━\n"
            for ev in events[:8]:
                try:
                    ev_dt  = datetime.strptime(ev["date"], "%Y-%m-%d")
                    diff   = (ev_dt.date() - now.date()).days
                    zamanl = "⚡ *BUGÜN*" if diff==0 else ("🔜 Yarın" if diff==1 else f"📆 {diff}g sonra")
                    imp    = ev.get("importance", 0)
                    text  += f"\n{'🔴' if imp>=80 else '🟡'} {zamanl}\n📌 {ev['title']}\n"
                    if ev.get("desc"): text += f"   _{ev['desc']}_\n"
                except Exception: pass
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
        elif q.data not in GROUP_FREE \
                and not q.data.startswith("hedef_") \
                and not q.data.startswith("set_"):
            await dm_redirect("Bu özellik")
            return

    await q.answer()

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

# ================= MINI APP WEB SUNUCUSU =================

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Kripto Drop Pro</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#07090f;--card:#0e141f;--card2:#131c2a;--card3:#192436;
  --border:#1d2d42;--border2:#253d58;
  --text:#ddeeff;--muted:#5577aa;--dim:#050810;
  --g:#00e5a0;--g2:#00ffb3;--gd:rgba(0,229,160,.12);
  --r:#ff3d6b;--r2:#ff6b8a;--rd:rgba(255,61,107,.12);
  --y:#f0c040;--yd:rgba(240,192,64,.12);
  --b:#3a9fff;--b2:#6bbfff;--bd:rgba(58,159,255,.12);
  --p:#9b6fff;--pd:rgba(155,111,255,.12);
  --o:#ff8c42;--t:#00d4e8;--td:rgba(0,212,232,.12);
  --acc:#1a6fff;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text);font-size:12px}
#app{height:100dvh;display:flex;flex-direction:column}
#scroll{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding-bottom:62px}
#scroll::-webkit-scrollbar{display:none}

/* TICKER */
.ticker{height:26px;background:#050d1a;border-bottom:1px solid var(--border);
  display:flex;align-items:center;overflow:hidden;flex-shrink:0}
.t-inner{display:flex;animation:tick 35s linear infinite}
@keyframes tick{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.t-item{display:flex;align-items:center;gap:4px;padding:0 14px;
  border-right:1px solid var(--border);white-space:nowrap;height:26px;flex-shrink:0;font-size:10px}
.t-sym{font-weight:700;color:var(--muted);font-size:9px;letter-spacing:.4px}

/* HEADER */
.hdr{height:46px;flex-shrink:0;background:linear-gradient(180deg,#0a1628,#060d1a);
  border-bottom:1px solid var(--border);display:flex;align-items:center;
  justify-content:space-between;padding:0 13px;z-index:200}
.logo{display:flex;align-items:center;gap:7px}
.logo-box{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#1040a0,#0a2880);
  display:flex;align-items:center;justify-content:center;font-size:15px}
.logo-txt{font-size:14px;font-weight:800;letter-spacing:-.3px;
  background:linear-gradient(135deg,var(--b2),var(--t));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-sub{font-size:8px;color:var(--muted);font-weight:600;letter-spacing:.4px;margin-top:-2px}
.hdr-r{display:flex;align-items:center;gap:6px}
.live{display:flex;align-items:center;gap:3px;background:rgba(0,229,160,.08);
  border:1px solid rgba(0,229,160,.2);border-radius:18px;padding:3px 7px}
.ldot{width:5px;height:5px;border-radius:50%;background:var(--g);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}60%{opacity:.2}}
.ltxt{font-size:9px;font-weight:700;color:var(--g);letter-spacing:.4px}
#clk{font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums}

/* TABS */
.tabs{flex-shrink:0;display:flex;background:var(--dim);border-bottom:1px solid var(--border);overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:0 0 auto;padding:8px 13px;font-size:11px;font-weight:600;color:var(--muted);
  cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .18s}
.tab.on{color:var(--b2);border-bottom-color:var(--b)}

/* PAGES */
.page{display:none;padding:10px}
.page.on{display:block}

/* CARD */
.card{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:11px;margin-bottom:8px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(58,159,255,.15),transparent)}
.cg{border-color:rgba(0,229,160,.18)}.cb{border-color:rgba(58,159,255,.18)}.cy{border-color:rgba(240,192,64,.18)}.cp{border-color:rgba(155,111,255,.18)}

/* GRID */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:5px}

/* STAT BOX */
.sb{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:9px 7px;text-align:center}
.sv{font-size:16px;font-weight:800;line-height:1.1;letter-spacing:-.3px}
.sl{font-size:9px;color:var(--muted);margin-top:3px;font-weight:600}

/* COLORS */
.up{color:var(--g)}.dn{color:var(--r)}.nu{color:var(--y)}.bl{color:var(--b)}.or{color:var(--o)}.tl{color:var(--t)}

/* BADGE */
.bdg{display:inline-flex;align-items:center;padding:2px 6px;border-radius:6px;font-size:10px;font-weight:700}
.bg{background:var(--gd);color:var(--g);border:1px solid rgba(0,229,160,.2)}
.br{background:var(--rd);color:var(--r);border:1px solid rgba(255,61,107,.2)}
.by{background:var(--yd);color:var(--y);border:1px solid rgba(240,192,64,.2)}
.bb{background:var(--bd);color:var(--b);border:1px solid rgba(58,159,255,.2)}

/* COIN ROW */
.cr{display:flex;align-items:center;gap:7px;padding:7px 0;border-bottom:1px solid var(--border);cursor:pointer}
.cr:last-child{border-bottom:none}
.cr:active{opacity:.65}
.cico{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:900;border:1px solid;flex-shrink:0}
.cinfo{flex:1;min-width:0}
.csym{font-size:12px;font-weight:800}
.cname{font-size:9px;color:var(--muted);margin-top:1px}
.cr-r{text-align:right;flex-shrink:0}
.cpct{font-size:12px;font-weight:800}
.cprice{font-size:9px;color:var(--muted);margin-top:1px;font-variant-numeric:tabular-nums}
.crank{font-size:9px;color:var(--muted);width:18px;text-align:center;flex-shrink:0;font-weight:600}

/* FORM */
.row{display:flex;gap:6px;align-items:center;margin-bottom:8px}
.inp{flex:1;background:var(--card2);border:1px solid var(--border);border-radius:8px;
  padding:8px 10px;color:var(--text);font-size:12px;outline:none;-webkit-appearance:none;min-width:0}
.inp:focus{border-color:var(--b)}
.inp::placeholder{color:var(--muted)}
.sel{background:var(--card2);border:1px solid var(--border);border-radius:8px;
  padding:8px 6px;color:var(--text);font-size:11px;outline:none;-webkit-appearance:none}
.btn{background:linear-gradient(135deg,#1052d0,#0838a8);border:none;border-radius:8px;
  padding:8px 13px;color:#fff;font-size:12px;cursor:pointer;font-weight:700;flex-shrink:0}
.btn:active{opacity:.8}
.frow{display:flex;gap:5px;margin-bottom:8px;overflow-x:auto;scrollbar-width:none}
.frow::-webkit-scrollbar{display:none}
.fc{background:var(--card2);border:1px solid var(--border);border-radius:7px;padding:5px 9px;
  font-size:10px;font-weight:700;color:var(--muted);cursor:pointer;white-space:nowrap;flex-shrink:0}
.fc.on{background:var(--bd);border-color:rgba(58,159,255,.35);color:var(--b2)}

/* SECTION HEADER */
.sh{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
.sh-t{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);display:flex;align-items:center;gap:5px}
.sh-t span{color:var(--text)}
.sh-btn{font-size:10px;color:var(--b);font-weight:600;cursor:pointer;padding:2px 6px;border-radius:5px;border:1px solid rgba(58,159,255,.2)}

/* PROGRESS */
.pb{background:rgba(255,255,255,.06);border-radius:3px;height:5px;overflow:hidden}
.pbf{height:5px;border-radius:3px;transition:width .5s ease}

/* FEAR GREED RING */
.fg-ring{width:88px;height:88px;position:relative;margin:0 auto}
.fg-ring svg{transform:rotate(-90deg)}
.fg-ov{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;pointer-events:none}
.fg-n{font-size:22px;font-weight:900;line-height:1}
.fg-l{font-size:8px;color:var(--muted);font-weight:700;letter-spacing:.2px;white-space:nowrap}

/* CHART */
.ch{position:relative}
.ohlcv{display:flex;gap:5px;flex-wrap:wrap;padding:5px 9px;background:var(--dim);border-bottom:1px solid var(--border);font-size:9px}
.oi{display:flex;gap:2px;align-items:center}
.ok{color:var(--muted);font-weight:600}

/* HEATMAP */
.hmap{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}
.hm{border-radius:7px;padding:7px 4px;text-align:center;cursor:pointer}
.hm:active{opacity:.7}
.hm-s{font-size:10px;font-weight:700;color:var(--text)}
.hm-p{font-size:11px;font-weight:800;margin-top:2px}

/* ALARM ROW */
.alr{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)}
.alr:last-child{border-bottom:none}

/* COPY */
.cp-btn{cursor:pointer;font-size:10px;color:var(--muted);padding:1px 4px;border-radius:3px}
.cp-btn:active{background:var(--bd);color:var(--b)}

/* LOADER */
.ld{text-align:center;padding:20px;color:var(--muted);font-size:11px}
.spin{width:20px;height:20px;border:2px solid var(--border2);border-top-color:var(--b);
  border-radius:50%;animation:sp .6s linear infinite;margin:0 auto 7px}
@keyframes sp{to{transform:rotate(360deg)}}

/* EMPTY */
.mt{text-align:center;padding:24px 14px}
.mt-i{font-size:32px;margin-bottom:8px;opacity:.5}
.mt-t{font-size:12px;font-weight:700;margin-bottom:4px}
.mt-s{font-size:10px;color:var(--muted);line-height:1.5}

/* ERROR BANNER */
.err{background:var(--rd);border:1px solid rgba(255,61,107,.3);border-radius:8px;
  padding:10px 12px;display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.err-txt{font-size:11px;color:var(--r);font-weight:600}
.err-btn{font-size:10px;color:var(--b);font-weight:700;cursor:pointer;background:var(--bd);
  padding:3px 8px;border-radius:5px;border:1px solid rgba(58,159,255,.25)}

/* NAV */
.nav{flex-shrink:0;height:58px;background:rgba(5,8,16,.96);backdrop-filter:blur(10px);
  border-top:1px solid var(--border);display:flex}
.nb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:2px;cursor:pointer;font-size:8px;font-weight:700;color:var(--muted);border:none;background:none;letter-spacing:.2px}
.nb .ic{font-size:18px;line-height:1}
.nb.on{color:var(--b2)}

/* TOAST */
#toast{position:fixed;bottom:68px;left:50%;transform:translateX(-50%);
  background:var(--card2);border:1px solid var(--border2);padding:7px 15px;
  border-radius:20px;font-size:11px;font-weight:700;opacity:0;transition:opacity .22s;
  pointer-events:none;white-space:nowrap;z-index:999}
#toast.on{opacity:1}
</style>
</head>
<body>
<div id="app">

<!-- TICKER -->
<div class="ticker">
  <div class="t-inner" id="tI">
    <div class="t-item"><span class="t-sym">BTC</span><span id="tBTC" style="color:var(--g)">--</span></div>
    <div class="t-item"><span class="t-sym">ETH</span><span id="tETH" style="color:var(--b)">--</span></div>
    <div class="t-item"><span class="t-sym">BNB</span><span id="tBNB" style="color:var(--y)">--</span></div>
    <div class="t-item"><span class="t-sym">SOL</span><span id="tSOL" style="color:var(--p)">--</span></div>
    <div class="t-item"><span class="t-sym">XRP</span><span id="tXRP">--</span></div>
    <div class="t-item"><span class="t-sym">DOGE</span><span id="tDOGE">--</span></div>
    <div class="t-item"><span class="t-sym">ADA</span><span id="tADA">--</span></div>
    <div class="t-item"><span class="t-sym">AVAX</span><span id="tAVAX">--</span></div>
    <div class="t-item"><span class="t-sym">BTC</span><span id="tBTC2" style="color:var(--g)">--</span></div>
    <div class="t-item"><span class="t-sym">ETH</span><span id="tETH2" style="color:var(--b)">--</span></div>
    <div class="t-item"><span class="t-sym">BNB</span><span id="tBNB2" style="color:var(--y)">--</span></div>
    <div class="t-item"><span class="t-sym">SOL</span><span id="tSOL2" style="color:var(--p)">--</span></div>
    <div class="t-item"><span class="t-sym">XRP</span><span id="tXRP2">--</span></div>
    <div class="t-item"><span class="t-sym">DOGE</span><span id="tDOGE2">--</span></div>
    <div class="t-item"><span class="t-sym">ADA</span><span id="tADA2">--</span></div>
    <div class="t-item"><span class="t-sym">AVAX</span><span id="tAVAX2">--</span></div>
  </div>
</div>

<!-- HEADER -->
<div class="hdr">
  <div class="logo">
    <div class="logo-box">🪙</div>
    <div><div class="logo-txt">Kripto Drop</div><div class="logo-sub">PRO DASHBOARD</div></div>
  </div>
  <div class="hdr-r">
    <div class="live"><div class="ldot"></div><span class="ltxt">CANLI</span></div>
    <div id="clk">--:--</div>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <div class="tab on" onclick="go('home')">🏠 Ana</div>
  <div class="tab" onclick="go('mkt')">📈 Piyasa</div>
  <div class="tab" onclick="go('chart')">🕯️ Grafik</div>
  <div class="tab" onclick="go('nabiz')">🌡️ Nabız</div>
  <div class="tab" onclick="go('top')">🏆 Liderler</div>
  <div class="tab" onclick="go('analiz')">🔬 Analiz</div>
  <div class="tab" onclick="go('fib')">📐 Fibonacci</div>
  <div class="tab" onclick="go('sent')">🧠 Duygu</div>
  <div class="tab" onclick="go('alarmlar')">🔔 Alarmlar</div>
</div>

<!-- SCROLL -->
<div id="scroll">

<!-- ANA SAYFA -->
<div id="p-home" class="page on">
  <div id="homeErr" style="display:none" class="err">
    <span class="err-txt">⚠️ Bağlantı hatası</span>
    <span class="err-btn" onclick="retryHome()">🔄 Tekrar Dene</span>
  </div>
  <div class="g2" style="margin-bottom:7px">
    <div class="card cg" style="padding:12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.4px;margin-bottom:4px">₿ BITCOIN</div>
      <div class="sv up" id="hBP" style="font-size:20px">--</div>
      <div id="hBB" style="margin-top:4px;font-size:10px">--</div>
      <div style="font-size:9px;color:var(--muted);margin-top:3px" id="hBV">Vol: --</div>
    </div>
    <div class="card cb" style="padding:12px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;letter-spacing:.4px;margin-bottom:4px">Ξ ETHEREUM</div>
      <div class="sv bl" id="hEP" style="font-size:20px">--</div>
      <div id="hEB" style="margin-top:4px;font-size:10px">--</div>
      <div style="font-size:9px;color:var(--muted);margin-top:3px" id="hEV">Vol: --</div>
    </div>
  </div>
  <div class="g4" style="margin-bottom:8px">
    <div class="sb"><div class="sv or" id="hDom">--</div><div class="sl">BTC Dom.</div></div>
    <div class="sb"><div class="sv" id="hAvg">--</div><div class="sl">Ort. Değ.</div></div>
    <div class="sb"><div class="sv up" id="hUp">--</div><div class="sl">↑ Yükselen</div></div>
    <div class="sb"><div class="sv dn" id="hDn">--</div><div class="sl">↓ Düşen</div></div>
  </div>
  <div class="g2" style="margin-bottom:8px">
    <div class="card cy" style="padding:10px;text-align:center">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:7px;letter-spacing:.4px">😱 KORKU & AÇGÖZLÜLÜK</div>
      <div class="fg-ring">
        <svg width="88" height="88" viewBox="0 0 88 88">
          <circle cx="44" cy="44" r="35" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="7"/>
          <circle id="fgA" cx="44" cy="44" r="35" fill="none" stroke="var(--muted)"
            stroke-width="7" stroke-linecap="round" stroke-dasharray="219.9" stroke-dashoffset="219.9"
            style="transition:stroke-dashoffset .8s ease,stroke .4s"/>
        </svg>
        <div class="fg-ov">
          <div class="fg-n" id="fgN">--</div>
          <div class="fg-l" id="fgL">Yükleniyor</div>
        </div>
      </div>
      <div style="font-size:9px;color:var(--muted);margin-top:5px" id="fgY">Dün: --</div>
    </div>
    <div class="card" style="padding:10px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:7px;letter-spacing:.4px">📊 PİYASA NABZI</div>
      <div id="hNabiz"><div class="spin" style="width:16px;height:16px;margin:10px auto"></div></div>
    </div>
  </div>
  <div class="card cb" style="margin-bottom:8px">
    <div class="sh"><div class="sh-t">⭐ <span>FAVORİLERİM</span></div><span class="sh-btn" onclick="go('mkt')">Tüm Piyasa →</span></div>
    <div id="hFav"><div class="ld" style="padding:8px"><div class="spin"></div></div></div>
  </div>
  <div class="card cp" style="margin-bottom:8px">
    <div class="sh"><div class="sh-t">🔔 <span>AKTİF ALARMLARIM</span></div><span class="sh-btn" onclick="go('alarmlar')">Tümü →</span></div>
    <div id="hAlarm"><div class="ld" style="padding:8px"><div class="spin"></div></div></div>
  </div>
  <div class="card" style="margin-bottom:8px">
    <div class="sh"><div class="sh-t">🚀 <span>LİDERLER</span></div><span class="sh-btn" onclick="go('top')">Tümü →</span></div>
    <div id="hGain"><div class="ld" style="padding:8px"><div class="spin"></div></div></div>
  </div>
  <!-- Takvim Ana Sayfada -->
  <div class="card cy" style="margin-bottom:8px">
    <div class="sh"><div class="sh-t">📅 <span>EKONOMİK TAKVİM</span></div><span style="font-size:9px;color:var(--muted)" id="hTakT"></span></div>
    <div id="hTak"><div class="ld" style="padding:8px"><div class="spin"></div></div></div>
  </div>
  <div class="card">
    <div class="sh"><div class="sh-t">📰 <span>SON HABERLER</span></div><span style="font-size:9px;color:var(--muted)" id="hNT">--:--</span></div>
    <div id="hNews"><div class="ld"><div class="spin"></div></div></div>
  </div>
</div>

<!-- PİYASA -->
<div id="p-mkt" class="page">
  <div class="row">
    <input class="inp" id="mQ" placeholder="🔍 Coin ara... BTC, ETH" oninput="fltMkt()">
    <select class="sel" id="mSrt" onchange="srtMkt()">
      <option value="vol">📊 Hacim</option>
      <option value="up">🟢 Yükselen</option>
      <option value="dn">🔴 Düşen</option>
      <option value="px">💰 Fiyat</option>
    </select>
  </div>
  <div class="frow">
    <div class="fc on" id="fAll" onclick="setF('all')">🌐 Tümü</div>
    <div class="fc" id="fUp" onclick="setF('up')">🟢 Yükselen</div>
    <div class="fc" id="fDn" onclick="setF('dn')">🔴 Düşen</div>
    <div class="fc" id="fHot" onclick="setF('hot')">🔥 Büyük Hacim</div>
    <div class="fc" id="fPump" onclick="setF('pump')">⚡ +5% Pump</div>
    <div class="fc" id="fDump" onclick="setF('dump')">💥 -5% Dump</div>
  </div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:6px;text-align:right" id="mCnt"></div>
  <div id="mktList"><div class="ld"><div class="spin"></div>Yükleniyor...</div></div>
</div>

<!-- GRAFİK -->
<div id="p-chart" class="page">
  <div class="row">
    <input class="inp" id="gSym" placeholder="BTCUSDT" maxlength="15" onkeydown="if(event.key==='Enter')drawChart()">
    <select class="sel" id="gTF" onchange="drawChart()">
      <option value="15m">15d</option><option value="1h">1s</option>
      <option value="4h" selected>4s</option><option value="1d">1g</option><option value="1w">1h</option>
    </select>
    <button class="btn" onclick="drawChart()">🕯️ Çiz</button>
  </div>
  <div id="chartOut"><div class="mt"><div class="mt-i">🕯️</div><div class="mt-t">Mum Grafiği</div><div class="mt-s">Sembol girin ve Çiz'e basın</div></div></div>
</div>

<!-- NABIZ -->
<div id="p-nabiz" class="page">
  <div class="g2" style="margin-bottom:8px">
    <div class="card cy" style="padding:10px;text-align:center">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:6px">😱 FEAR &amp; GREED</div>
      <canvas id="fgG" width="130" height="72"></canvas>
      <div style="font-size:18px;font-weight:900;margin-top:4px" id="fgGV">--</div>
      <div style="font-size:10px;color:var(--muted)" id="fgGL">--</div>
    </div>
    <div class="card" style="padding:10px">
      <div style="font-size:9px;color:var(--muted);font-weight:700;margin-bottom:6px">📈 YUKARI / AŞAĞI</div>
      <canvas id="adC" width="120" height="120" style="display:block;margin:0 auto"></canvas>
      <div style="text-align:center;font-size:10px;margin-top:4px" id="adL">--</div>
    </div>
  </div>
  <div class="card" style="margin-bottom:8px"><div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.5px;margin-bottom:8px">🗺️ SEKTÖR HEATMAP</div><div class="hmap" id="sHeat"><div class="ld"><div class="spin"></div></div></div></div>
  <div class="card" style="margin-bottom:8px"><div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.5px;margin-bottom:8px">💧 HACİM DAĞILIMI</div><div style="position:relative;height:130px"><canvas id="domPie"></canvas></div></div>
  <div class="card"><div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.5px;margin-bottom:8px">🔮 RSI AŞIRI BÖLGELER (1s)</div><div id="rsiExt"><div class="ld" style="padding:8px"><div class="spin"></div></div></div></div>
</div>

<!-- LİDERLER -->
<div id="p-top" class="page">
  <div class="frow" style="margin-bottom:8px">
    <div class="fc on" id="tG" onclick="showTop('g')">🚀 Yükselen</div>
    <div class="fc" id="tL" onclick="showTop('l')">💥 Düşen</div>
    <div class="fc" id="tV" onclick="showTop('v')">💧 Hacim</div>
  </div>
  <div class="card" id="topL"><div class="ld"><div class="spin"></div></div></div>
</div>

<!-- ANALİZ -->
<div id="p-analiz" class="page">
  <div class="row">
    <input class="inp" id="aIn" placeholder="BTCUSDT veya BTC" maxlength="15" onkeydown="if(event.key==='Enter')doAnaliz()">
    <button class="btn" onclick="doAnaliz()">🔬 Analiz</button>
  </div>
  <div id="aOut"><div class="mt"><div class="mt-i">🔬</div><div class="mt-t">Teknik Analiz</div><div class="mt-s">RSI, EMA, sinyal skoru</div></div></div>
</div>

<!-- FİBONACCİ -->
<div id="p-fib" class="page">
  <div class="row">
    <input class="inp" id="fIn" placeholder="BTCUSDT" maxlength="15" onkeydown="if(event.key==='Enter')doFib()">
    <select class="sel" id="fTF"><option value="1h">1s</option><option value="4h" selected>4s</option><option value="1d">1g</option><option value="1w">1h</option></select>
    <button class="btn" onclick="doFib()">📐 Çiz</button>
  </div>
  <div id="fOut"><div class="mt"><div class="mt-i">📐</div><div class="mt-t">Fibonacci</div><div class="mt-s">Destek ve direnç seviyeleri</div></div></div>
</div>

<!-- DUYGU -->
<div id="p-sent" class="page">
  <div class="row">
    <input class="inp" id="sIn" placeholder="BTC, ETH, SOL..." maxlength="15" onkeydown="if(event.key==='Enter')doSent()">
    <button class="btn" onclick="doSent()">🧠 Analiz</button>
  </div>
  <div id="sOut"><div class="mt"><div class="mt-i">🧠</div><div class="mt-t">Sentiment Analizi</div><div class="mt-s">Topluluk duygu skoru</div></div></div>
</div>

<!-- ALARMLAR -->
<div id="p-alarmlar" class="page">
  <div id="alarmOut"><div class="ld"><div class="spin"></div></div></div>
</div>

<!-- TAKVİM -->
<div id="p-takvim" class="page">
  <div id="takvimOut"><div class="ld"><div class="spin"></div></div></div>
</div>

</div><!-- /scroll -->

<div class="nav">
  <button class="nb on" onclick="go('home')"><span class="ic">🏠</span>Ana</button>
  <button class="nb" onclick="go('mkt')"><span class="ic">📈</span>Piyasa</button>
  <button class="nb" onclick="go('chart')"><span class="ic">🕯️</span>Grafik</button>
  <button class="nb" onclick="go('nabiz')"><span class="ic">🌡️</span>Nabız</button>
  <button class="nb" onclick="go('top')"><span class="ic">🏆</span>Liderler</button>
  <button class="nb" onclick="go('alarmlar')"><span class="ic">🔔</span>Alarmlar</button>
</div>
</div>
<div id="toast"></div>

<script>
// ─── CONFIG ───
const tg=window.Telegram?.WebApp;
if(tg){tg.ready();tg.expand();}
const UID=tg?.initDataUnsafe?.user?.id||0;

// Kendi sunucumuz üzerinden proxy — CORS sorunu yok
const PROXY='/api/proxy?url=';
// Binance direkt (genellikle çalışır)
const BIN='https://api.binance.com/api/v3';
// CoinGecko proxy üzerinden
const CGP=PROXY+encodeURIComponent('https://api.coingecko.com/api/v3');
// Fear&Greed proxy üzerinden  
const FGP=PROXY+encodeURIComponent('https://api.alternative.me/fng/?limit=2');

const PAGES=['home','mkt','chart','nabiz','top','analiz','fib','sent','alarmlar'];
let CUR='home';
let allCoins=[],filtCoins=[],coinFilter='all';
let topData={g:[],l:[],v:[]},topMode='g';
let cChart=null,rChart=null,vChart=null,nabLoaded=false;
let domPieInst=null,adInst=null;

// ─── CLOCK ───
setInterval(()=>{
  const now=new Date();
  const el=document.getElementById('clk');
  if(el)el.textContent=now.toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
},1000);

// ─── TOAST ───
function toast(m,d=2200){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');setTimeout(()=>e.classList.remove('on'),d);}

// ─── COPY ───
function cp(text,label){
  try{navigator.clipboard.writeText(text);}catch(e){}
  toast('📋 Kopyalandı'+(label?' — '+label:''));
}

// ─── FORMAT ───
function fp(p){
  p=parseFloat(p);if(isNaN(p)||p===0)return'--';
  if(p>=100000)return p.toLocaleString('tr-TR',{maximumFractionDigits:0});
  if(p>=1000)return p.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(p>=1)return p.toFixed(4);
  if(p>=0.001)return p.toFixed(6);
  return p.toFixed(8);
}
function fv(v){
  v=parseFloat(v)||0;
  if(v>=1e12)return(v/1e12).toFixed(2)+'T$';
  if(v>=1e9)return(v/1e9).toFixed(2)+'B$';
  if(v>=1e6)return(v/1e6).toFixed(1)+'M$';
  if(v>=1e3)return(v/1e3).toFixed(0)+'K$';
  return v.toFixed(0)+'$';
}
function pc(p){return p>0?'up':p<0?'dn':'nu';}
function pb(p){const c=p>0?'bg':p<0?'br':'by',s=p>0?'+':'';return`<span class="bdg ${c}">${s}${p.toFixed(2)}%</span>`;}
const PAL=['#3a9fff','#9b6fff','#00e5a0','#f0c040','#ff8c42','#00d4e8','#ff3d6b','#4ecdc4'];
function cIco(sym){const ci=sym.charCodeAt(0)%PAL.length;const col=PAL[ci];return`<div class="cico" style="background:${col}18;color:${col};border-color:${col}30">${sym[0]}</div>`;}

// ─── SAFE FETCH ───
// Önce direkt dene, başarısız olursa proxy üzerinden
async function sf(url, ms=9000){
  return new Promise(resolve=>{
    let done=false;
    const timer=setTimeout(()=>{if(!done){done=true;resolve(null);}},ms);
    fetch(url)
      .then(r=>{if(r.ok)return r.json();throw new Error('HTTP '+r.status);})
      .then(d=>{if(!done){done=true;clearTimeout(timer);resolve(d);}})
      .catch(()=>{if(!done){done=true;clearTimeout(timer);resolve(null);}});
  });
}

// ─── NAVIGATION ───
function go(t){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.nb').forEach(x=>x.classList.remove('on'));
  const pg=document.getElementById('p-'+t);if(!pg)return;
  pg.classList.add('on');
  const i=PAGES.indexOf(t);
  if(i>=0){
    const tabs=document.querySelectorAll('.tab');
    if(tabs[i]){tabs[i].classList.add('on');tabs[i].scrollIntoView({behavior:'smooth',inline:'center',block:'nearest'});}
  }
  const nm={home:0,mkt:1,chart:2,nabiz:3,top:4,analiz:5,alarmlar:5};
  const nbs=document.querySelectorAll('.nb');
  const ni=nm[t];if(ni!==undefined&&nbs[ni])nbs[ni].classList.add('on');
  CUR=t;
  document.getElementById('scroll').scrollTop=0;
  if(t==='mkt'&&!allCoins.length)loadMkt();
  if(t==='nabiz')loadNabiz();
  if(t==='top'&&!topData.g.length)loadTop();
  if(t==='alarmlar')loadAlarms();
}
function openChart(sym){document.getElementById('gSym').value=sym;go('chart');drawChart();}

// ─── TICKER ───
const TSYMS=['BTC','ETH','BNB','SOL','XRP','DOGE','ADA','AVAX'];
async function loadTicker(){
  const d=await sf(`${BIN}/ticker/price`);
  if(!d||!Array.isArray(d))return;
  TSYMS.forEach(s=>{
    const c=d.find(x=>x.symbol===s+'USDT');if(!c)return;
    const txt='$'+fp(c.price);
    ['t'+s,'t'+s+'2'].forEach(id=>{const e=document.getElementById(id);if(e)e.textContent=txt;});
  });
}

// ─── ANA SAYFA ───
function retryHome(){document.getElementById('homeErr').style.display='none';loadHome();}

async function loadHome(){
  const errEl=document.getElementById('homeErr');
  errEl.style.display='none';

  // Binance 24hr verisini çek
  const t24=await sf(`${BIN}/ticker/24hr`);

  if(!t24||!Array.isArray(t24)||t24.length<10){
    errEl.style.display='flex';
    // Yine de diğer bölümleri yükle
    loadFG();loadHomeFav();loadHomeAlarms();loadHomeNews();
    return;
  }

  try{
    const u=t24.filter(x=>x&&x.symbol&&x.symbol.endsWith('USDT'));
    const btc=u.find(x=>x.symbol==='BTCUSDT')||{};
    const eth=u.find(x=>x.symbol==='ETHUSDT')||{};
    const bv=parseFloat(btc.quoteVolume||0);
    const tv=u.reduce((a,x)=>a+parseFloat(x.quoteVolume||0),0);
    const dom=tv>0?(bv/tv*100):0;
    const chs=u.map(x=>parseFloat(x.priceChangePercent||0)).filter(x=>!isNaN(x));
    const avg=chs.length?chs.reduce((a,b)=>a+b,0)/chs.length:0;
    const ri=chs.filter(x=>x>0).length;
    const bp=parseFloat(btc.priceChangePercent||0);
    const ep=parseFloat(eth.priceChangePercent||0);

    const set=(id,val)=>{const e=document.getElementById(id);if(e)e.innerHTML=val;};
    const setT=(id,val)=>{const e=document.getElementById(id);if(e)e.textContent=val;};

    setT('hBP','$'+fp(btc.lastPrice||0));
    document.getElementById('hBP').className='sv '+(bp>=0?'up':'dn');
    set('hBB',pb(bp));
    setT('hBV','Vol: '+fv(btc.quoteVolume||0));
    setT('hEP','$'+fp(eth.lastPrice||0));
    document.getElementById('hEP').className='sv '+(ep>=0?'up':'dn');
    set('hEB',pb(ep));
    setT('hEV','Vol: '+fv(eth.quoteVolume||0));
    setT('hDom',dom.toFixed(1)+'%');
    set('hAvg',`<span class="${pc(avg)}">${avg>=0?'+':''}${avg.toFixed(1)}%</span>`);
    setT('hUp',ri);
    setT('hDn',u.length-ri);

    const pct=u.length?(ri/u.length*100).toFixed(0):50;
    const mood=avg>3?'🐂 Çok Güçlü':avg>1?'🐂 Boğa':avg<-3?'🐻 Çok Zayıf':avg<-1?'🐻 Ayı':'😐 Yatay';
    const mc=avg>1?'var(--g)':avg<-1?'var(--r)':'var(--y)';
    set('hNabiz',`
      <div style="font-size:16px;font-weight:900;color:${mc};margin-bottom:6px">${mood}</div>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-bottom:3px">
        <span>🔴 ${u.length-ri}</span><span>🟢 ${ri}</span>
      </div>
      <div class="pb" style="margin-bottom:7px"><div class="pbf" style="width:${pct}%;background:linear-gradient(90deg,var(--r),var(--g))"></div></div>
      <div style="display:flex;justify-content:space-between">
        <div style="text-align:center"><div style="font-size:12px;font-weight:800;color:var(--t)">${dom.toFixed(1)}%</div><div style="font-size:8px;color:var(--muted)">BTC Dom.</div></div>
        <div style="text-align:center"><div style="font-size:12px;font-weight:800;color:var(--b)">${fv(tv)}</div><div style="font-size:8px;color:var(--muted)">24s Hacim</div></div>
        <div style="text-align:center"><div style="font-size:12px;font-weight:800;color:${mc}">${avg>=0?'+':''}${avg.toFixed(1)}%</div><div style="font-size:8px;color:var(--muted)">Ort. Değ.</div></div>
      </div>`);

    const gainers=[...u].filter(x=>parseFloat(x.quoteVolume||0)>2e6)
      .sort((a,b)=>parseFloat(b.priceChangePercent||0)-parseFloat(a.priceChangePercent||0)).slice(0,5);
    const medals=['🥇','🥈','🥉','④','⑤'];
    set('hGain',gainers.map((c,i)=>{
      const sym=c.symbol.replace('USDT','');const p=parseFloat(c.priceChangePercent||0);
      return`<div class="cr" onclick="openChart('${c.symbol}')">
        <span style="font-size:${i<3?'16':'11'}px;width:20px;text-align:center;flex-shrink:0">${medals[i]}</span>
        ${cIco(sym)}
        <div class="cinfo">
          <div style="display:flex;align-items:center;gap:4px"><span class="csym">${sym}</span>
          <span class="cp-btn" onclick="event.stopPropagation();cp('${sym}USDT','${sym}USDT')">📋</span></div>
          <div class="cname">${fv(c.quoteVolume||0)}</div>
        </div>
        <div class="cr-r"><div class="cpct ${pc(p)}">${p>0?'+':''}${p.toFixed(2)}%</div>
          <div class="cprice">$${fp(c.lastPrice||0)}</div></div>
      </div>`;}).join(''));

    // Ticker güncelle
    TSYMS.forEach(s=>{
      const c=u.find(x=>x.symbol===s+'USDT');if(!c)return;
      const p=parseFloat(c.priceChangePercent||0);
      const col=p>=0?'var(--g)':'var(--r)';
      const txt=`${p>=0?'▲':'▼'} $${fp(c.lastPrice)}`;
      ['t'+s,'t'+s+'2'].forEach(id=>{const e=document.getElementById(id);if(e){e.textContent=txt;e.style.color=col;}});
    });

  }catch(e){console.error('home render:',e);errEl.style.display='flex';}

  // Bağımsız yükle
  loadFG();
  loadHomeFav();
  loadHomeAlarms();
  loadHomeTakvim();
  loadHomeNews();
}

async function loadFG(){
  try{
    // Önce direkt dene, sonra proxy
    let fg=await sf('https://api.alternative.me/fng/?limit=2',5000);
    if(!fg?.data)fg=await sf(FGP,5000);
    if(!fg?.data?.[0])return;
    const f=fg.data[0],f1=fg.data[1];
    const val=parseInt(f.value||50);
    const lbl=f.value_classification||'--';
    const cols={'Extreme Fear':'#ff3d6b','Fear':'#ff8c42','Neutral':'#f0c040','Greed':'#00e5a0','Extreme Greed':'#00ffb3'};
    const col=cols[lbl]||'var(--muted)';
    const arc=document.getElementById('fgA');
    if(arc){const c2=2*Math.PI*35;arc.style.strokeDashoffset=c2-(val/100)*c2;arc.style.stroke=col;}
    const nn=document.getElementById('fgN');if(nn){nn.textContent=val;nn.style.color=col;}
    const nl=document.getElementById('fgL');if(nl)nl.textContent=lbl;
    const ny=document.getElementById('fgY');if(ny)ny.textContent=`Dün: ${f1?.value||'--'} — ${f1?.value_classification||''}`;
  }catch(e){}
}

async function loadHomeFav(){
  const el=document.getElementById('hFav');
  if(!el)return;
  if(!UID){
    el.innerHTML=`<div style="text-align:center;padding:10px;font-size:10px;color:var(--muted)">
      🔒 Favoriler için Telegram üzerinden açın<br><span style="font-size:9px">Botta /favori ile ekleyin</span></div>`;
    return;
  }
  try{
    const r=await sf('/api/favorites?uid='+UID,5000);
    const favs=r?.favorites||[];
    if(!favs.length){
      el.innerHTML=`<div style="text-align:center;padding:10px;font-size:10px;color:var(--muted)">
        ⭐ Favori yok — Botta /favori ekle BTCUSDT yazın</div>`;return;
    }
    // Tüm fiyatları tek seferde al
    const all=await sf(`${BIN}/ticker/24hr`,8000);
    if(!all||!Array.isArray(all)){
      el.innerHTML=`<div style="text-align:center;padding:8px;font-size:10px;color:var(--muted)">⚠️ Fiyatlar alınamadı</div>`;return;
    }
    el.innerHTML=favs.slice(0,6).map(sym=>{
      const c=all.find(x=>x.symbol===sym);
      if(!c){const s=sym.replace('USDT','');return`<div class="cr">${cIco(s)}<div class="cinfo"><span class="csym">${s}</span></div><span class="bdg by">--</span></div>`;}
      const s=c.symbol.replace('USDT','');const p=parseFloat(c.priceChangePercent||0);
      return`<div class="cr" onclick="openChart('${c.symbol}')">
        ${cIco(s)}
        <div class="cinfo">
          <div style="display:flex;align-items:center;gap:4px"><span class="csym">${s}</span>
          <span class="cp-btn" onclick="event.stopPropagation();cp('${s}USDT','${s}USDT')">📋</span></div>
          <div class="cname">${fv(c.quoteVolume||0)}</div>
        </div>
        <div class="cr-r"><div class="cpct ${pc(p)}">${p>0?'+':''}${p.toFixed(2)}%</div>
          <div class="cprice">$${fp(c.lastPrice||0)}</div></div>
      </div>`;}).join('');
  }catch(e){el.innerHTML=`<div style="text-align:center;padding:8px;font-size:10px;color:var(--muted)">⚠️ Yüklenemedi</div>`;}
}

async function loadHomeAlarms(){
  const el=document.getElementById('hAlarm');if(!el)return;
  if(!UID){
    el.innerHTML=`<div style="text-align:center;padding:10px;font-size:10px;color:var(--muted)">🔒 Alarmlar için Telegram üzerinden açın</div>`;return;
  }
  try{
    const r=await sf('/api/alarms?uid='+UID,5000);
    const alarms=(r?.alarms||[]).filter(a=>a.active).slice(0,4);
    if(!alarms.length){
      el.innerHTML=`<div style="text-align:center;padding:10px;font-size:10px;color:var(--muted)">🔕 Aktif alarm yok — Botta /alarm_ekle yazın</div>`;return;
    }
    const ico={'percent':'📊','rsi':'🔮','band':'📏','price':'🎯'};
    el.innerHTML=alarms.map(a=>{
      const sym=a.symbol.replace('USDT','');
      const lbl=a.type==='percent'?`%${a.threshold}`:a.type==='rsi'?`RSI ${a.rsi_level}`:`${a.type}`;
      return`<div class="alr">
        <div style="display:flex;align-items:center;gap:7px">
          ${cIco(sym)}<div>
          <div style="font-size:11px;font-weight:700">${ico[a.type]||'🔔'} ${sym}</div>
          <div style="font-size:9px;color:var(--muted)">${lbl} • ${a.trigger_count||0}× tetiklendi</div></div>
        </div>
        <span class="bdg bg">Aktif</span>
      </div>`;}).join('');
  }catch(e){el.innerHTML=`<div style="text-align:center;padding:8px;font-size:10px;color:var(--muted)">⚠️ Yüklenemedi</div>`;}
}

async function loadHomeTakvim(){
  const el=document.getElementById('hTak');
  if(!el)return;
  // Statik takvim — API gerektirmez, her zaman çalışır
  const now=new Date(),y=now.getFullYear(),m=now.getMonth();
  const evs=[
    {t:'🏦 FOMC',d:18,imp:'h',desc:'Fed faiz kararı'},
    {t:'📊 CPI',d:12,imp:'h',desc:'Enflasyon verisi'},
    {t:'💼 NFP',d:7,imp:'m',desc:'İstihdam raporu'},
    {t:'📈 PCE',d:28,imp:'h',desc:'Fiyat endeksi'},
  ].map(e=>{
    let dt=new Date(y,m,e.d);
    if(dt<now)dt=new Date(y,m>=11?0:m+1,m>=11?y+1:y,e.d);
    // Ay taşması düzelt
    try{dt=new Date(y,m,e.d);if(dt<now){const nm=m+1>11?0:m+1;const ny=m+1>11?y+1:y;dt=new Date(ny,nm,e.d);}}catch(ex){}
    return{...e,dt};
  }).sort((a,b)=>a.dt-b.dt).slice(0,3);
  const ic={h:'var(--r)',m:'var(--y)',l:'var(--g)'};
  el.innerHTML=evs.map(e=>{
    const diff=Math.ceil((e.dt-now)/86400000);
    const w=diff===0?'⚡ BUGÜN':diff===1?'🔜 YARIN':`${diff} gün`;
    const urgent=diff<=3;
    const col=urgent?ic[e.imp]:'var(--muted)';
    return`<div style="display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <span style="font-size:12px;font-weight:700">${e.t}</span>
        <span style="font-size:9px;color:var(--muted);margin-left:5px">${e.desc}</span>
      </div>
      <span style="font-size:10px;font-weight:800;color:${col};white-space:nowrap;margin-left:8px">${w}</span>
    </div>`;}).join('')+
    `<div style="text-align:center;padding-top:6px;font-size:9px;color:var(--muted)">Bildirim için botta <strong>/takvim</strong></div>`;
}

async function loadHomeNews(){
  const el=document.getElementById('hNews');if(!el)return;
  // /api/news endpoint'i üzerinden XML parse (sunucu tarafında)
  try{
    const d=await sf('/api/news',9000);
    if(d?.items?.length){
      const nt=document.getElementById('hNT');
      if(nt)nt.textContent=new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'});
      el.innerHTML=d.items.slice(0,4).map((item,i)=>`
        <div style="display:flex;gap:7px;padding:7px 0;${i<3?'border-bottom:1px solid var(--border)':''}">
          <div style="font-size:16px;font-weight:900;color:var(--border2);flex-shrink:0;line-height:1.2;min-width:20px;text-align:center">${i+1}</div>
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;font-weight:600;line-height:1.4;
              display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">
              ${item.title||'--'}
            </div>
            <div style="font-size:9px;color:var(--muted);margin-top:2px">
              <span style="color:var(--b);font-weight:700">${item.source||'Kripto'}</span>
              ${item.date?' • '+item.date:''}
            </div>
          </div>
        </div>`).join('');
    }else{
      el.innerHTML=`<div style="text-align:center;padding:12px;font-size:10px;color:var(--muted)">
        📡 Haber servisleri şu an erişilemiyor</div>`;
    }
  }catch(e){
    el.innerHTML=`<div style="text-align:center;padding:12px;font-size:10px;color:var(--muted)">
      📡 Geçici bağlantı hatası</div>`;
  }
}


// ─── PİYASA ───
async function loadMkt(){
  const el=document.getElementById('mktList');
  const d=await sf(`${BIN}/ticker/24hr`,12000);
  if(!d||!Array.isArray(d)){el.innerHTML='<div style="text-align:center;padding:20px;color:var(--muted)">⚠️ Veri alınamadı — <span onclick="loadMkt()" style="color:var(--b);cursor:pointer">Tekrar Dene</span></div>';return;}
  allCoins=d.filter(x=>{
    if(!x.symbol.endsWith('USDT'))return false;
    const b=x.symbol.replace('USDT','');
    if(/UP$|DOWN$|BULL$|BEAR$|3L$|3S$/.test(b))return false;
    return parseFloat(x.quoteVolume||0)>200000;
  }).sort((a,b)=>parseFloat(b.quoteVolume||0)-parseFloat(a.quoteVolume||0)).slice(0,100);
  filtCoins=[...allCoins];
  renderMkt();
}
function fltMkt(){
  const q=document.getElementById('mQ').value.toUpperCase().replace('/USDT','').replace('USDT','').trim();
  filtCoins=allCoins.filter(c=>c.symbol.replace('USDT','').includes(q));
  applyF();renderMkt();
}
function setF(f){
  coinFilter=f;
  ['All','Up','Dn','Hot','Pump','Dump'].forEach(x=>{const e=document.getElementById('f'+x);if(e)e.classList.remove('on');});
  const m={all:'fAll',up:'fUp',dn:'fDn',hot:'fHot',pump:'fPump',dump:'fDump'};
  const e=document.getElementById(m[f]);if(e)e.classList.add('on');
  applyF();renderMkt();
}
function applyF(){
  let base=allCoins;
  const q=document.getElementById('mQ').value.toUpperCase().replace('/USDT','').replace('USDT','').trim();
  if(q)base=base.filter(c=>c.symbol.replace('USDT','').includes(q));
  if(coinFilter==='up')filtCoins=base.filter(c=>parseFloat(c.priceChangePercent||0)>0);
  else if(coinFilter==='dn')filtCoins=base.filter(c=>parseFloat(c.priceChangePercent||0)<0);
  else if(coinFilter==='hot')filtCoins=base.filter(c=>parseFloat(c.quoteVolume||0)>1e8);
  else if(coinFilter==='pump')filtCoins=base.filter(c=>parseFloat(c.priceChangePercent||0)>5);
  else if(coinFilter==='dump')filtCoins=base.filter(c=>parseFloat(c.priceChangePercent||0)<-5);
  else filtCoins=base;
}
function srtMkt(){
  const s=document.getElementById('mSrt').value;
  if(s==='vol')filtCoins.sort((a,b)=>parseFloat(b.quoteVolume||0)-parseFloat(a.quoteVolume||0));
  else if(s==='up')filtCoins.sort((a,b)=>parseFloat(b.priceChangePercent||0)-parseFloat(a.priceChangePercent||0));
  else if(s==='dn')filtCoins.sort((a,b)=>parseFloat(a.priceChangePercent||0)-parseFloat(b.priceChangePercent||0));
  else if(s==='px')filtCoins.sort((a,b)=>parseFloat(b.lastPrice||0)-parseFloat(a.lastPrice||0));
  renderMkt();
}
function renderMkt(){
  const cnt=document.getElementById('mCnt');if(cnt)cnt.textContent=filtCoins.length+' coin';
  const el=document.getElementById('mktList');if(!el)return;
  if(!filtCoins.length){el.innerHTML='<div class="mt"><div class="mt-i">🔍</div><div class="mt-t">Sonuç yok</div></div>';return;}
  el.innerHTML=filtCoins.slice(0,100).map((c,i)=>{
    const sym=c.symbol.replace('USDT','');const p=parseFloat(c.priceChangePercent||0);
    return`<div class="cr" onclick="openChart('${c.symbol}')">
      <span class="crank">${i+1}</span>
      ${cIco(sym)}
      <div class="cinfo">
        <div style="display:flex;align-items:center;gap:4px">
          <span class="csym">${sym}</span>
          <span class="cp-btn" onclick="event.stopPropagation();cp('${c.symbol}','${sym}')">📋</span>
        </div>
        <div class="cname">${fv(c.quoteVolume||0)}</div>
      </div>
      <div class="cr-r"><div class="cpct ${pc(p)}">${p>0?'+':''}${p.toFixed(2)}%</div>
        <div class="cprice">$${fp(c.lastPrice||0)}</div></div>
    </div>`;}).join('');
}

// ─── MUM GRAFİĞİ ───
async function drawChart(){
  let sym=document.getElementById('gSym').value.toUpperCase().trim();
  if(!sym){toast('⚠️ Sembol girin!');return;}
  if(!sym.endsWith('USDT'))sym+='USDT';
  const tf=document.getElementById('gTF').value;
  const el=document.getElementById('chartOut');
  el.innerHTML='<div class="ld"><div class="spin"></div>Grafik yükleniyor...</div>';
  [cChart,rChart,vChart].forEach(c=>{try{c?.destroy();}catch(e){}});
  cChart=rChart=vChart=null;
  // Eski SVG mum grafiğini temizle
  document.querySelectorAll('.cndl-svg').forEach(el=>el.remove());
  const cdlCanvas = document.getElementById('cdl');
  if(cdlCanvas) cdlCanvas.style.display = 'block';
  try{
    const lim=tf==='15m'?96:tf==='1h'?80:tf==='4h'?80:tf==='1d'?80:52;
    const k=await sf(`${BIN}/klines?symbol=${sym}&interval=${tf}&limit=${lim}`,12000);
    if(!Array.isArray(k)||k.length<10){el.innerHTML='<div class="ld">❌ Veri alınamadı</div>';return;}
    const opens=k.map(x=>parseFloat(x[1]));const highs=k.map(x=>parseFloat(x[2]));
    const lows=k.map(x=>parseFloat(x[3]));const closes=k.map(x=>parseFloat(x[4]));
    const vols=k.map(x=>parseFloat(x[5]));const times=k.map(x=>x[0]);
    const cur=closes[closes.length-1];const op=opens[opens.length-1];
    const hi=highs[highs.length-1];const lo=lows[lows.length-1];
    const vl=vols[vols.length-1];
    const pct=(cur-closes[0])/closes[0]*100;
    function fmtT(ts){const d=new Date(ts);
      if(tf==='1d'||tf==='1w')return d.toLocaleDateString('tr-TR',{month:'short',day:'numeric'});
      return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
    const labels=times.map(fmtT);
    function ema(arr,n){const m=2/(n+1);let e=arr.slice(0,n).reduce((a,b)=>a+b,0)/n;
      const out=new Array(Math.min(n-1,arr.length)).fill(null);out.push(e);
      for(let i=n;i<arr.length;i++){e=arr[i]*m+e*(1-m);out.push(e);}return out;}
    const e20=ema(closes,20);const e50=ema(closes,Math.min(50,closes.length-1));
    function rsiArr(arr,n=14){const out=new Array(Math.min(n,arr.length)).fill(null);
      for(let i=n;i<arr.length;i++){let g=0,l=0;
        for(let j=i-n;j<i;j++){const d=arr[j+1]-arr[j];d>0?g+=d:l-=d;}
        out.push(100-100/(1+(g/n)/((l/n)||.0001)));}return out;}
    const rsiD=rsiArr(closes);
    const rsiNow=rsiD.filter(x=>x!=null).pop()||50;
    const bClr=k.map(x=>parseFloat(x[4])>=parseFloat(x[1])?'rgba(0,229,160,.8)':'rgba(255,61,107,.8)');
    const bBrd=k.map(x=>parseFloat(x[4])>=parseFloat(x[1])?'#00e5a0':'#ff3d6b');
    const bodyD=k.map(x=>[Math.min(parseFloat(x[1]),parseFloat(x[4])),Math.max(parseFloat(x[1]),parseFloat(x[4]))]);
    el.innerHTML=`
      <div class="card" style="padding:0;overflow:hidden">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 11px;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:7px">
            ${cIco(sym.replace('USDT',''))}
            <div>
              <div style="display:flex;align-items:center;gap:5px">
                <span style="font-size:14px;font-weight:800">${sym.replace('USDT','')}</span>
                <span class="cp-btn" onclick="cp('${sym}','${sym}')">📋</span>
                <span style="font-size:9px;color:var(--muted)">/USDT • ${tf}</span>
              </div>
            </div>
          </div>
          <div style="text-align:right">
            <div style="font-size:16px;font-weight:800;color:${cur>=op?'var(--g)':'var(--r)'}">${fp(cur)}</div>
            ${pb(pct)}
          </div>
        </div>
        <div class="ohlcv">
          <div class="oi"><span class="ok">A</span><span class="up">${fp(op)}</span></div>
          <div class="oi"><span class="ok">Y</span><span class="up">${fp(hi)}</span></div>
          <div class="oi"><span class="ok">D</span><span class="dn">${fp(lo)}</span></div>
          <div class="oi"><span class="ok">K</span><span>${fp(cur)}</span></div>
          <div class="oi"><span class="ok">H</span><span class="bl">${fv(vl)}</span></div>
        </div>
        <div style="display:flex;gap:3px;padding:6px 10px;border-bottom:1px solid var(--border)">
          ${['15m','1h','4h','1d','1w'].map(t=>`<button onclick="document.getElementById('gTF').value='${t}';drawChart()"
            style="flex:1;padding:5px 0;border-radius:6px;border:1px solid var(--border);
            background:${t===tf?'var(--bd)':'var(--card2)'};color:${t===tf?'var(--b2)':'var(--muted)'};
            font-size:10px;font-weight:700;cursor:pointer">${t}</button>`).join('')}
        </div>
        <div style="padding:8px 9px">
          <div class="ch" id="cdlWrap" style="height:200px;position:relative"><canvas id="cdl" style="width:100%;height:200px"></canvas></div>
          <div style="font-size:8px;color:var(--muted);margin:4px 0 2px;font-weight:700;letter-spacing:.4px">RSI (14) = <span style="color:${rsiNow>70?'var(--r)':rsiNow<30?'var(--g)':'var(--p)'}">${rsiNow.toFixed(1)}</span> ${rsiNow>70?'🔴 Aşırı Alım':rsiNow<30?'🟢 Aşırı Satım':'🟡 Nötr'}</div>
          <div class="ch" style="height:60px"><canvas id="cdlR"></canvas></div>
          <div style="font-size:8px;color:var(--muted);margin:4px 0 2px;font-weight:700;letter-spacing:.4px">HACİM</div>
          <div class="ch" style="height:40px"><canvas id="cdlV"></canvas></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;padding:0 9px 9px">
          ${[['EMA20',e20[e20.length-1],'var(--y)'],['EMA50',e50[e50.length-1],'var(--o)'],['RSI',rsiNow,'var(--p)']].map(([n,v,c])=>`
          <div style="background:var(--card2);border-radius:7px;padding:7px;text-align:center;border:1px solid var(--border)">
            <div style="font-size:9px;color:${c};font-weight:700">${n}</div>
            <div style="font-size:11px;font-weight:700;margin:2px 0;color:${n==='RSI'?(v>70?'var(--r)':v<30?'var(--g)':'var(--y)'):(cur>=v?'var(--g)':'var(--r)')}">
              ${n==='RSI'?v.toFixed(1):'$'+fp(v||0)}
            </div>
            <div style="font-size:8px;color:var(--muted)">${n==='RSI'?(v>70?'Aşırı Alım':v<30?'Aşırı Satım':'Nötr'):(cur>=v?'↑ Üstünde':'↓ Altında')}</div>
          </div>`).join('')}
        </div>
      </div>`;

    const scaleOpts={
      x:{ticks:{color:'#5577aa',font:{size:8},maxTicksLimit:7,maxRotation:0},grid:{color:'rgba(29,45,66,.6)'},border:{display:false}},
      y:{position:'right',ticks:{color:'#5577aa',font:{size:8},maxTicksLimit:6,callback:v=>fp(v)},grid:{color:'rgba(29,45,66,.6)'},border:{display:false}}
    };

    // ── SVG Mum Grafiği (gerçek candlestick) ──
    function drawCandleSVG(canvasId, opens, highs, lows, closes, ema20, ema50, labels){
      const canvas = document.getElementById(canvasId);
      if(!canvas) return;
      const wrap = document.getElementById('cdlWrap') || canvas.parentElement;
      const W = Math.max(280, wrap.offsetWidth || wrap.clientWidth || 340);
      const H = 200;
      canvas.style.display = 'none'; // canvas'ı gizle

      // Eski SVG varsa kaldır
      const oldSvg = canvas.parentElement.querySelector('.cndl-svg');
      if(oldSvg) oldSvg.remove();

      const n = closes.length;
      const padL = 4, padR = 45, padT = 8, padB = 20;
      const chartW = W - padL - padR;
      const chartH = H - padT - padB;

      const allPrices = [...highs, ...lows, ...ema20.filter(x=>x), ...ema50.filter(x=>x)];
      const minP = Math.min(...allPrices) * 0.999;
      const maxP = Math.max(...allPrices) * 1.001;
      const priceRange = maxP - minP;

      function px(price){ return padT + chartH - ((price - minP) / priceRange) * chartH; }
      function xPos(i){ return padL + (i + 0.5) * (chartW / n); }
      const candleW = Math.max(1, (chartW / n) * 0.6);

      let svg = `<svg class="cndl-svg" width="${W}" height="${H}" style="display:block;overflow:visible" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;

      // Grid yatay çizgiler
      const yTicks = 5;
      for(let i = 0; i <= yTicks; i++){
        const price = minP + (priceRange * i / yTicks);
        const y = px(price);
        const label = fp(price);
        svg += `<line x1="${padL}" y1="${y}" x2="${W - padR + 2}" y2="${y}" stroke="rgba(29,45,66,.7)" stroke-width="1"/>`;
        svg += `<text x="${W - padR + 5}" y="${y + 3}" fill="#5577aa" font-size="8" font-family="monospace">${label}</text>`;
      }

      // X ekseni etiketleri (her 8 mumda bir)
      const step = Math.max(1, Math.floor(n / 7));
      for(let i = 0; i < n; i += step){
        const x = xPos(i);
        svg += `<text x="${x}" y="${H - 4}" fill="#5577aa" font-size="8" text-anchor="middle" font-family="sans-serif">${labels[i]}</text>`;
      }

      // EMA çizgileri
      const drawEMA = (emaArr, color) => {
        let d = '';
        for(let i = 0; i < emaArr.length; i++){
          if(emaArr[i] == null) continue;
          const x = xPos(i), y = px(emaArr[i]);
          d += (d === '' ? `M${x},${y}` : ` L${x},${y}`);
        }
        if(d) svg += `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linejoin="round"/>`;
      };
      drawEMA(ema20, '#f0c040');
      drawEMA(ema50, '#ff8c42');

      // Mumlar
      for(let i = 0; i < n; i++){
        const x = xPos(i);
        const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
        const isBull = c >= o;
        const color = isBull ? '#00e5a0' : '#ff3d6b';
        const bodyTop = px(Math.max(o, c));
        const bodyBot = px(Math.min(o, c));
        const bodyH = Math.max(1, bodyBot - bodyTop);
        const wickTop = px(h);
        const wickBot = px(l);

        // Gövde
        svg += `<rect x="${x - candleW/2}" y="${bodyTop}" width="${candleW}" height="${bodyH}" fill="${color}" rx="0.5"/>`;
        // Üst wick
        if(wickTop < bodyTop)
          svg += `<line x1="${x}" y1="${wickTop}" x2="${x}" y2="${bodyTop}" stroke="${color}" stroke-width="1.2"/>`;
        // Alt wick
        if(wickBot > bodyBot)
          svg += `<line x1="${x}" y1="${bodyBot}" x2="${x}" y2="${wickBot}" stroke="${color}" stroke-width="1.2"/>`;
      }

      svg += '</svg>';

      const div = document.createElement('div');
      div.innerHTML = svg;
      canvas.parentElement.insertBefore(div.firstChild, canvas);
    }

    drawCandleSVG('cdl', opens, highs, lows, closes, e20, e50, labels);

    // RSI Chart (Chart.js ile — sorunsuz)
    rChart=new Chart(document.getElementById('cdlR').getContext('2d'),{
      type:'line',data:{labels,datasets:[
        {data:rsiD,borderColor:'#9b6fff',borderWidth:1.5,pointRadius:0,tension:.3,fill:false,spanGaps:true},
        {data:Array(k.length).fill(70),borderColor:'rgba(255,61,107,.35)',borderWidth:1,borderDash:[3,2],pointRadius:0,fill:false},
        {data:Array(k.length).fill(30),borderColor:'rgba(0,229,160,.35)',borderWidth:1,borderDash:[3,2],pointRadius:0,fill:false},
      ]},
      options:{responsive:true,maintainAspectRatio:false,animation:{duration:200},
        plugins:{legend:{display:false},tooltip:{enabled:false}},
        scales:{x:{display:false},y:{min:0,max:100,position:'right',ticks:{color:'#5577aa',font:{size:8},maxTicksLimit:3,callback:v=>v},grid:{color:'rgba(29,45,66,.5)'},border:{display:false}}}}
    });
    vChart=new Chart(document.getElementById('cdlV').getContext('2d'),{
      type:'bar',data:{labels,datasets:[{data:vols,
        backgroundColor:k.map(x=>parseFloat(x[4])>=parseFloat(x[1])?'rgba(0,229,160,.3)':'rgba(255,61,107,.3)'),
        borderWidth:0,barPercentage:.9,categoryPercentage:1}]},
      options:{responsive:true,maintainAspectRatio:false,animation:{duration:200},
        plugins:{legend:{display:false},tooltip:{enabled:false}},
        scales:{x:{display:false},y:{display:false}}}
    });
  }catch(e){if(el)el.innerHTML=`<div class="ld">⚠️ ${e.message}</div>`;}
}

// ─── NABIZ ───
async function loadNabiz(){
  if(nabLoaded)return;nabLoaded=true;
  const t24=await sf(`${BIN}/ticker/24hr`,12000);
  if(!t24||!Array.isArray(t24))return;
  const u=t24.filter(x=>x.symbol.endsWith('USDT')&&parseFloat(x.quoteVolume||0)>1e6);
  const ri=u.filter(x=>parseFloat(x.priceChangePercent||0)>0).length;
  // FG gauge
  loadFG_gauge();
  // Donut
  if(adInst)adInst.destroy();
  const adEl=document.getElementById('adC');
  if(adEl){adInst=new Chart(adEl.getContext('2d'),{type:'doughnut',
    data:{datasets:[{data:[ri,u.length-ri],backgroundColor:['rgba(0,229,160,.8)','rgba(255,61,107,.8)'],borderWidth:0,hoverOffset:4}]},
    options:{responsive:false,cutout:'68%',plugins:{legend:{display:false}},animation:{duration:600}}});}
  const adl=document.getElementById('adL');if(adl)adl.innerHTML=`<span class="up">↑${ri}</span>&nbsp;<span class="dn">↓${u.length-ri}</span>`;
  // Sektör
  const secs={'₿ BTC':['BTCUSDT'],'Ξ ETH':['ETHUSDT'],'🟣 L1':['SOLUSDT','ADAUSDT','AVAXUSDT','NEARUSDT','APTUSDT','SUIUSDT'],'💎 DeFi':['UNIUSDT','AAVEUSDT','INJUSDT','LDOUSDT'],'🐸 Meme':['DOGEUSDT','SHIBUSDT','PEPEUSDT','WIFUSDT','BONKUSDT'],'⚡ L2':['ARBUSDT','OPUSDT','STRKUSDT'],'🤖 AI':['FETUSDT','RENDERUSDT','WLDUSDT'],'🔗 Infra':['LINKUSDT','ATOMUSDT','TONUSDT']};
  const sh=document.getElementById('sHeat');
  if(sh){sh.innerHTML=Object.entries(secs).map(([sec,syms])=>{
    const coins=u.filter(c=>syms.includes(c.symbol));if(!coins.length)return'';
    const avg=coins.reduce((a,c)=>a+parseFloat(c.priceChangePercent||0),0)/coins.length;
    const intensity=Math.min(Math.abs(avg)/8,1);
    const bg=avg>=0?`rgba(0,229,160,${.08+intensity*.65})`:`rgba(255,61,107,${.08+intensity*.65})`;
    const col=avg>=0?'var(--g)':'var(--r)';
    return`<div class="hm" style="background:${bg}" onclick="toast('${sec}: ${avg>=0?'+':''}${avg.toFixed(2)}%',2000)">
      <div class="hm-s">${sec}</div><div class="hm-p" style="color:${col}">${avg>=0?'+':''}${avg.toFixed(1)}%</div></div>`}).join('');}
  // Dominans pie
  const bv=parseFloat(u.find(x=>x.symbol==='BTCUSDT')?.quoteVolume||0);
  const ev=parseFloat(u.find(x=>x.symbol==='ETHUSDT')?.quoteVolume||0);
  const bnbv=parseFloat(u.find(x=>x.symbol==='BNBUSDT')?.quoteVolume||0);
  const tv=u.reduce((a,x)=>a+parseFloat(x.quoteVolume||0),0);
  const dpEl=document.getElementById('domPie');
  if(dpEl){if(domPieInst)domPieInst.destroy();
    domPieInst=new Chart(dpEl.getContext('2d'),{type:'doughnut',
      data:{labels:['BTC','ETH','BNB','Diğerleri'],
        datasets:[{data:[bv,ev,bnbv,tv-bv-ev-bnbv].map(x=>+(x/tv*100).toFixed(1)),
          backgroundColor:['#f0c040','#3a9fff','#ff8c42','#5577aa'],borderWidth:0,hoverOffset:5}]},
      options:{responsive:true,maintainAspectRatio:false,
        plugins:{legend:{position:'right',labels:{color:'#5577aa',font:{size:9},padding:6,boxWidth:9}}},
        animation:{duration:600}}});}
  // RSI extremes
  const sample=u.sort((a,b)=>parseFloat(b.quoteVolume||0)-parseFloat(a.quoteVolume||0)).slice(0,15);
  const rsiRes=[];
  for(const coin of sample){
    try{
      const kk=await sf(`${BIN}/klines?symbol=${coin.symbol}&interval=1h&limit=20`,6000);
      if(!Array.isArray(kk)||kk.length<15)continue;
      const c=kk.map(x=>parseFloat(x[4]));let g=0,l=0;
      for(let i=c.length-14;i<c.length;i++){const d=c[i]-c[i-1];d>0?g+=d:l-=d;}
      const rsi=100-100/(1+(g/14)/((l/14)||.0001));
      rsiRes.push({sym:coin.symbol.replace('USDT',''),rsi,p:parseFloat(coin.priceChangePercent||0)});
    }catch(e){}
  }
  rsiRes.sort((a,b)=>b.rsi-a.rsi);
  const rx=document.getElementById('rsiExt');
  if(rx){rx.innerHTML=`
    <div style="font-size:9px;font-weight:800;color:var(--r);margin-bottom:5px">🔴 AŞIRI ALIM (RSI &gt; 65)</div>
    ${rsiRes.filter(x=>x.rsi>65).slice(0,3).map(x=>`<div class="cr" onclick="openChart('${x.sym}USDT')">
      ${cIco(x.sym)}<div class="cinfo"><span class="csym">${x.sym}</span></div>
      <div class="cr-r"><span style="color:var(--r);font-weight:800">${x.rsi.toFixed(0)}</span>&nbsp;${pb(x.p)}</div></div>`).join('')||'<div style="font-size:10px;color:var(--muted);padding:4px">Tespit edilemedi</div>'}
    <div style="height:1px;background:var(--border);margin:7px 0"></div>
    <div style="font-size:9px;font-weight:800;color:var(--g);margin-bottom:5px">🟢 AŞIRI SATIM (RSI &lt; 35)</div>
    ${rsiRes.filter(x=>x.rsi<35).slice(-3).map(x=>`<div class="cr" onclick="openChart('${x.sym}USDT')">
      ${cIco(x.sym)}<div class="cinfo"><span class="csym">${x.sym}</span></div>
      <div class="cr-r"><span style="color:var(--g);font-weight:800">${x.rsi.toFixed(0)}</span>&nbsp;${pb(x.p)}</div></div>`).join('')||'<div style="font-size:10px;color:var(--muted);padding:4px">Tespit edilemedi</div>'}`;}
}

async function loadFG_gauge(){
  let fg=await sf('https://api.alternative.me/fng/?limit=1',5000);
  if(!fg?.data)fg=await sf(PROXY+encodeURIComponent('https://api.alternative.me/fng/?limit=1'),5000);
  if(!fg?.data?.[0])return;
  const val=parseInt(fg.data[0].value||50);const lbl=fg.data[0].value_classification||'--';
  const cols={'Extreme Fear':'#ff3d6b','Fear':'#ff8c42','Neutral':'#f0c040','Greed':'#00e5a0','Extreme Greed':'#00ffb3'};
  const col=cols[lbl]||'#8b949e';
  const cv=document.getElementById('fgG');if(!cv)return;
  drawGauge('fgG',val,col);
  const gv=document.getElementById('fgGV');if(gv){gv.textContent=val;gv.style.color=col;}
  const gl=document.getElementById('fgGL');if(gl)gl.textContent=lbl;
}

function drawGauge(id,val,col){
  const cv=document.getElementById(id);if(!cv)return;
  const ctx=cv.getContext('2d'),w=cv.width,h=cv.height;
  ctx.clearRect(0,0,w,h);const cx=w/2,cy=h,r=Math.min(w,h*2)/2-7;
  ctx.beginPath();ctx.arc(cx,cy,r,Math.PI,0);ctx.strokeStyle='rgba(29,45,66,.8)';ctx.lineWidth=9;ctx.lineCap='round';ctx.stroke();
  const grd=ctx.createLinearGradient(0,0,w,0);
  grd.addColorStop(0,'#ff3d6b');grd.addColorStop(.5,'#f0c040');grd.addColorStop(1,'#00e5a0');
  const angle=Math.PI+(val/100)*Math.PI;
  ctx.beginPath();ctx.arc(cx,cy,r,Math.PI,angle);ctx.strokeStyle=grd;ctx.lineWidth=9;ctx.lineCap='round';ctx.stroke();
  ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(cx+Math.cos(angle)*(r-15),cy+Math.sin(angle)*(r-15));
  ctx.strokeStyle='rgba(255,255,255,.9)';ctx.lineWidth=2;ctx.lineCap='round';ctx.stroke();
  ctx.beginPath();ctx.arc(cx,cy,4,0,Math.PI*2);ctx.fillStyle='white';ctx.fill();
}

// ─── LİDERLER ───
async function loadTop(){
  const d=await sf(`${BIN}/ticker/24hr`,10000);
  if(!d||!Array.isArray(d))return;
  const u=d.filter(x=>{const b=x.symbol.replace('USDT','');return x.symbol.endsWith('USDT')&&!/UP$|DOWN$|BULL$|BEAR$/.test(b)&&parseFloat(x.quoteVolume||0)>500000;});
  topData.g=[...u].sort((a,b)=>parseFloat(b.priceChangePercent||0)-parseFloat(a.priceChangePercent||0)).slice(0,15);
  topData.l=[...u].sort((a,b)=>parseFloat(a.priceChangePercent||0)-parseFloat(b.priceChangePercent||0)).slice(0,15);
  topData.v=[...u].sort((a,b)=>parseFloat(b.quoteVolume||0)-parseFloat(a.quoteVolume||0)).slice(0,15);
  showTop(topMode);
}
function showTop(mode){
  topMode=mode;
  ['G','L','V'].forEach(x=>{const e=document.getElementById('t'+x);if(e)e.classList.remove('on');});
  const e=document.getElementById('t'+mode.toUpperCase());if(e)e.classList.add('on');
  const list=topData[mode];if(!list?.length){loadTop();return;}
  const medals=['🥇','🥈','🥉'];
  const el=document.getElementById('topL');if(!el)return;
  el.innerHTML=list.map((c,i)=>{const sym=c.symbol.replace('USDT','');const p=parseFloat(c.priceChangePercent||0);
    return`<div class="cr" onclick="openChart('${c.symbol}')">
      <span style="font-size:${i<3?'15':'10'}px;width:22px;text-align:center;flex-shrink:0;font-weight:700">${i<3?medals[i]:i+1}</span>
      ${cIco(sym)}
      <div class="cinfo">
        <div style="display:flex;align-items:center;gap:4px"><span class="csym">${sym}</span>
        <span class="cp-btn" onclick="event.stopPropagation();cp('${c.symbol}','${sym}')">📋</span></div>
        <div class="cname">${fv(c.quoteVolume||0)}</div>
      </div>
      <div class="cr-r"><div class="cpct ${pc(p)}">${p>0?'+':''}${p.toFixed(2)}%</div>
        <div class="cprice">$${fp(c.lastPrice||0)}</div></div>
    </div>`;}).join('');
}

// ─── ANALİZ ───
async function doAnaliz(){
  let s=document.getElementById('aIn').value.toUpperCase().trim();
  if(!s){toast('⚠️ Sembol girin!');return;}
  if(!s.endsWith('USDT'))s+='USDT';
  const el=document.getElementById('aOut');
  el.innerHTML='<div class="ld"><div class="spin"></div>Analiz yapılıyor...</div>';
  try{
    const[tk,k1,k4,k1d]=await Promise.all([
      sf(`${BIN}/ticker/24hr?symbol=${s}`,8000),
      sf(`${BIN}/klines?symbol=${s}&interval=1h&limit=50`,8000),
      sf(`${BIN}/klines?symbol=${s}&interval=4h&limit=50`,8000),
      sf(`${BIN}/klines?symbol=${s}&interval=1d&limit=50`,8000),
    ]);
    if(!tk||tk.code){el.innerHTML=`<div class="mt"><div class="mt-i">❌</div><div class="mt-t">${s} bulunamadı</div></div>`;return;}
    const pr=parseFloat(tk.lastPrice||0),p24=parseFloat(tk.priceChangePercent||0);
    function rsi(k,n=14){if(!Array.isArray(k)||k.length<n+1)return 50;
      const c=k.map(x=>parseFloat(x[4]));let g=0,l=0;
      for(let i=c.length-n;i<c.length;i++){const d=c[i]-c[i-1];d>0?g+=d:l-=d;}
      return 100-100/(1+(g/n)/((l/n)||.0001));}
    function ema(k,n){if(!Array.isArray(k)||k.length<n)return 0;
      const c=k.map(x=>parseFloat(x[4])),m=2/(n+1);
      let e=c.slice(0,n).reduce((a,b)=>a+b,0)/n;
      for(let i=n;i<c.length;i++)e=c[i]*m+e*(1-m);return e;}
    const r1=rsi(k1),r4=rsi(k4),r1d=rsi(k1d);
    const e9=ema(k1,9),e20=ema(k1,20),e50=ema(k1,50),e200=ema(k1d,50);
    const rl=r=>r>70?'🔴 Aşırı Alım':r<30?'🟢 Aşırı Satım':'🟡 Nötr';
    let sc=0;
    if(r1<35)sc+=2;else if(r1>65)sc-=2;
    if(r4<35)sc+=2;else if(r4>65)sc-=2;
    if(pr>e9)sc++;if(pr>e20)sc++;if(pr>e50)sc++;if(pr>e200)sc+=2;if(p24>0)sc++;
    const sl=sc>=5?['🟢','AL','var(--g)']:sc<=-2?['🔴','SAT','var(--r)']:['🟡','BEKLE','var(--y)'];
    const sym=s.replace('USDT','');
    el.innerHTML=`
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:7px">
            ${cIco(sym)}
            <div>
              <div style="display:flex;align-items:center;gap:5px">
                <span style="font-size:15px;font-weight:800">${sym}</span>
                <span class="cp-btn" onclick="cp('${s}','${s}')">📋</span>
              </div>
              <div style="font-size:9px;color:var(--muted)">/USDT</div>
            </div>
          </div>
          <div style="text-align:center;background:var(--card2);border-radius:9px;padding:8px 12px;border:1px solid var(--border2)">
            <div style="font-size:20px;font-weight:900;color:${sl[2]}">${sl[0]} ${sl[1]}</div>
            <div style="font-size:9px;color:var(--muted)">${sc}/9 puan</div>
          </div>
        </div>
        <div style="font-size:22px;font-weight:900;color:${p24>=0?'var(--g)':'var(--r)'};margin-bottom:5px">${fp(pr)} <span class="cp-btn" onclick="cp('${fp(pr)}','Fiyat')">📋</span></div>
        <div style="margin-bottom:10px">${pb(p24)} <span style="font-size:9px;color:var(--muted)">24 saat</span></div>
        <div class="g3" style="margin-bottom:10px">
          ${[['RSI 1s',r1],['RSI 4s',r4],['RSI 1g',r1d]].map(([n,v])=>`
          <div style="background:var(--card2);border-radius:7px;padding:7px;text-align:center;border:1px solid var(--border)">
            <div style="font-size:9px;color:var(--muted);font-weight:700">${n}</div>
            <div style="font-size:15px;font-weight:800;color:${v>70?'var(--r)':v<30?'var(--g)':'var(--y)'};">${v.toFixed(0)}</div>
            <div style="font-size:8px;color:var(--muted)">${rl(v)}</div>
          </div>`).join('')}
        </div>
        <div style="background:var(--card2);border-radius:8px;border:1px solid var(--border);overflow:hidden;margin-bottom:10px">
          ${[['EMA 9',e9,'var(--t)'],['EMA 20',e20,'var(--y)'],['EMA 50',e50,'var(--o)'],['EMA 200 (1g)',e200,'var(--p)'],
             ['24s Hacim',null,'var(--b)'],['24s Yüksek',null,'var(--g)'],['24s Düşük',null,'var(--r)']].map(([n,v,c],idx)=>{
            const val=idx===4?fv(tk.quoteVolume||0):idx===5?`$${fp(tk.highPrice||0)}`:idx===6?`$${fp(tk.lowPrice||0)}`:`${pr>=(v||0)?'🟢':'🔴'} $${fp(v||0)}`;
            const valColor=idx===4?c:idx===5?'var(--g)':idx===6?'var(--r)':pr>=(v||0)?'var(--g)':'var(--r)';
            return`<div style="display:flex;justify-content:space-between;padding:6px 9px;border-bottom:1px solid var(--border)">
              <span style="font-size:10px;color:var(--muted)">${n}</span>
              <span style="font-size:10px;font-weight:700;color:${valColor}">${val}</span></div>`}).join('')}
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px">
          <button class="btn" onclick="openChart('${s}')" style="font-size:10px;padding:7px 0">🕯️ Grafik</button>
          <button class="btn" onclick="document.getElementById('fIn').value='${s}';go('fib');doFib()" style="background:linear-gradient(135deg,#1a3a80,#102060);font-size:10px;padding:7px 0">📐 Fib</button>
          <button class="btn" onclick="document.getElementById('sIn').value='${sym}';go('sent');doSent()" style="background:linear-gradient(135deg,#401a80,#280a60);font-size:10px;padding:7px 0">🧠 Duygu</button>
        </div>
      </div>`;
  }catch(e){if(el)el.innerHTML=`<div class="ld">⚠️ ${e.message}</div>`;}
}

// ─── FİBONACCİ ───
async function doFib(){
  let s=document.getElementById('fIn').value.toUpperCase().trim();
  if(!s){toast('⚠️ Sembol girin!');return;}
  if(!s.endsWith('USDT'))s+='USDT';
  const tf=document.getElementById('fTF').value;
  const el=document.getElementById('fOut');
  el.innerHTML='<div class="ld"><div class="spin"></div>Fibonacci hesaplanıyor...</div>';
  try{
    const k=await sf(`${BIN}/klines?symbol=${s}&interval=${tf}&limit=100`,10000);
    if(!Array.isArray(k)||k.length<20){el.innerHTML='<div class="ld">❌ Veri yok</div>';return;}
    const hi=k.map(x=>parseFloat(x[2])),lo=k.map(x=>parseFloat(x[3])),cl=k.map(x=>parseFloat(x[4]));
    const H=Math.max(...hi),L=Math.min(...lo),CUR=cl[cl.length-1],D=H-L,up=CUR>cl[0];
    const FL=[0,.236,.382,.5,.618,.786,1];
    const FC=['#ffd700','#ff8c42','#ff3d6b','#00d4e8','#3a9fff','#9b6fff','#00e5a0'];
    const FN=['0%','23.6%','38.2%','50%','61.8% 🏆','78.6%','100%'];
    const FP=FL.map(l=>up?H-D*l:L+D*l);
    const ni=FP.reduce((b,p,i)=>Math.abs(p-CUR)<Math.abs(FP[b]-CUR)?i:b,0);
    const p2p=p=>Math.max(0,Math.min(100,((p-L)/(H-L))*100));
    const sym=s.replace('USDT','');
    el.innerHTML=`
      <div class="card cb">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:11px">
          <div style="display:flex;align-items:center;gap:7px">
            ${cIco(sym)}
            <div>
              <div style="display:flex;align-items:center;gap:4px">
                <span style="font-size:14px;font-weight:800">${sym}</span>
                <span class="cp-btn" onclick="cp('${s}')">📋</span>
              </div>
              <div style="font-size:9px;color:var(--muted)">${tf} • ${up?'📈 Yükseliş':'📉 Düşüş'}</div>
            </div>
          </div>
          <div style="font-size:16px;font-weight:800">$${fp(CUR)}</div>
        </div>
        <div style="margin:12px 0 10px">
          <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-bottom:5px">
            <span>📉 $${fp(L)}</span><span>📈 $${fp(H)}</span>
          </div>
          <div style="height:9px;background:rgba(255,255,255,.04);border-radius:5px;position:relative;border:1px solid var(--border)">
            <div style="position:absolute;inset:0;border-radius:5px;background:linear-gradient(90deg,var(--r),var(--y),var(--g));opacity:.12"></div>
            ${FL.map((l,i)=>`<div style="position:absolute;top:-3px;left:${p2p(FP[i])}%;width:2px;height:15px;background:${FC[i]};border-radius:1px;transform:translateX(-50%)"></div>`).join('')}
            <div style="position:absolute;top:-5px;left:${p2p(CUR)}%;width:3px;height:19px;background:white;border-radius:2px;transform:translateX(-50%);box-shadow:0 0 7px rgba(255,255,255,.5)"></div>
          </div>
          <div style="text-align:center;font-size:9px;color:var(--muted);margin-top:5px">▲ Mevcut konum (${p2p(CUR).toFixed(0)}%)</div>
        </div>
        <div style="background:var(--card2);border-radius:8px;border:1px solid var(--border);overflow:hidden">
          ${FL.map((l,i)=>{const p=FP[i];const isN=i===ni;return`
            <div style="display:flex;align-items:center;padding:7px 9px;${isN?'background:rgba(58,159,255,.08);border-left:3px solid var(--b);':'border-left:3px solid transparent;'}border-bottom:1px solid var(--border)">
              <div style="width:7px;height:7px;border-radius:50%;background:${FC[i]};flex-shrink:0;margin-right:8px"></div>
              <span style="font-size:11px;font-weight:${isN?700:400};color:${isN?'var(--b2)':'var(--muted)'};flex:1">${FN[i]}${isN?' ◀ En Yakın':''}</span>
              <span style="font-size:11px;font-weight:700;color:${p<CUR?'var(--g)':p>CUR?'var(--r)':'var(--text)'}">$${fp(p)}</span>
            </div>`}).join('')}
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:8px;font-size:9px;color:var(--muted)">
          <span>📈 High: $${fp(H)}</span><span>📉 Low: $${fp(L)}</span>
        </div>
      </div>`;
  }catch(e){if(el)el.innerHTML=`<div class="ld">⚠️ ${e.message}</div>`;}
}

// ─── DUYGU ───
async function doSent(){
  let s=document.getElementById('sIn').value.toUpperCase().trim();
  if(!s){toast('⚠️ Sembol girin!');return;}
  const base=s.replace('USDT','');
  const el=document.getElementById('sOut');
  el.innerHTML='<div class="ld"><div class="spin"></div>Analiz yapılıyor...</div>';
  try{
    const cgMap={BTC:'bitcoin',ETH:'ethereum',BNB:'binancecoin',SOL:'solana',XRP:'ripple',
      ADA:'cardano',DOGE:'dogecoin',DOT:'polkadot',AVAX:'avalanche-2',MATIC:'matic-network',
      LINK:'chainlink',UNI:'uniswap',LTC:'litecoin',SHIB:'shiba-inu',TON:'the-open-network',
      NEAR:'near',ARB:'arbitrum',OP:'optimism',SUI:'sui',APT:'aptos',INJ:'injective-protocol',
      PEPE:'pepe',WIF:'dogwifcoin',BONK:'bonk',RUNE:'thorchain',TIA:'celestia',
      FET:'fetch-ai',WLD:'worldcoin-wld',RENDER:'render-token',ATOM:'cosmos',TRX:'tron'};
    const id=cgMap[base]||base.toLowerCase();
    const cgUrl=`https://api.coingecko.com/api/v3/coins/${id}?localization=false&tickers=false&market_data=true&community_data=true`;
    // Proxy üzerinden dene
    let cg=await sf('/api/proxy?url='+encodeURIComponent(cgUrl),8000);
    if(!cg||cg.error)cg=await sf(cgUrl,8000);
    let up=50,dn=50,pr=0,p24=0,p7=0,p30=0,mcap=0,rank=0;
    if(cg&&!cg.error){
      up=cg.sentiment_votes_up_percentage||50;dn=cg.sentiment_votes_down_percentage||50;
      const md=cg.market_data||{};
      pr=md.current_price?.usd||0;p24=md.price_change_percentage_24h||0;
      p7=md.price_change_percentage_7d||0;p30=md.price_change_percentage_30d||0;
      mcap=md.market_cap?.usd||0;rank=cg.market_cap_rank||0;
    }
    const sc=up/100,bf=Math.round(sc*10);
    const lbl=up>65?'🚀 Çok Pozitif':up>55?'🟢 Pozitif':up<35?'💀 Çok Negatif':up<45?'🔴 Negatif':'🟡 Nötr';
    const lc=up>55?'var(--g)':up<45?'var(--r)':'var(--y)';
    const bar='█'.repeat(bf)+'░'.repeat(10-bf);
    el.innerHTML=`
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:11px">
          <div style="display:flex;align-items:center;gap:7px">
            ${cIco(base)}
            <div>
              <div style="display:flex;align-items:center;gap:4px">
                <span style="font-size:14px;font-weight:800">${base}</span>
                <span class="cp-btn" onclick="cp('${base}USDT')">📋</span>
                ${rank?`<span class="bdg bb" style="font-size:9px">#${rank}</span>`:''}
              </div>
              <div style="font-size:9px;color:var(--muted)">CoinGecko Topluluk Verisi</div>
            </div>
          </div>
          ${pr?`<div style="text-align:right"><div style="font-size:16px;font-weight:800">$${fp(pr)}</div>${pb(p24)}</div>`:''}
        </div>
        <div style="text-align:center;background:var(--card2);border-radius:9px;border:1px solid var(--border);padding:13px;margin-bottom:10px">
          <div style="font-size:28px;margin-bottom:4px">${lbl.split(' ')[0]}</div>
          <div style="font-size:16px;font-weight:900;color:${lc};margin-bottom:7px">${lbl.substring(2)}</div>
          <div style="font-family:monospace;font-size:15px;letter-spacing:3px;color:${lc};margin-bottom:4px">${bar}</div>
          <div style="font-size:10px;color:var(--muted)">${up.toFixed(1)}% yükseliş beklentisi</div>
        </div>
        <div class="g2" style="margin-bottom:10px">
          <div style="text-align:center;background:var(--gd);border-radius:7px;padding:9px;border:1px solid rgba(0,229,160,.15)">
            <div style="font-size:18px;font-weight:800;color:var(--g)">${up.toFixed(1)}%</div>
            <div style="font-size:9px;color:var(--muted)">🟢 Yükseliş</div>
          </div>
          <div style="text-align:center;background:var(--rd);border-radius:7px;padding:9px;border:1px solid rgba(255,61,107,.15)">
            <div style="font-size:18px;font-weight:800;color:var(--r)">${dn.toFixed(1)}%</div>
            <div style="font-size:9px;color:var(--muted)">🔴 Düşüş</div>
          </div>
        </div>
        <div style="background:var(--card2);border-radius:8px;border:1px solid var(--border);padding:8px">
          ${[['7g Performans',p7],['30g Performans',p30]].filter(x=>x[1]).map(([n,v])=>`
            <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:10px;border-bottom:1px solid var(--border)">
              <span style="color:var(--muted)">${n}</span>
              <span style="font-weight:700;color:${v>=0?'var(--g)':'var(--r)'}">${v>=0?'+':''}${v.toFixed(2)}%</span>
            </div>`).join('')}
          ${mcap?`<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:10px">
            <span style="color:var(--muted)">Market Cap</span><span style="font-weight:700;color:var(--b)">${fv(mcap)}</span></div>`:''}
        </div>
      </div>`;
  }catch(e){if(el)el.innerHTML=`<div class="ld">⚠️ ${e.message}</div>`;}
}

// ─── ALARMLAR ───
async function loadAlarms(){
  const el=document.getElementById('alarmOut');if(!el)return;
  if(!UID){
    el.innerHTML=`<div class="mt"><div class="mt-i">🔒</div><div class="mt-t">Telegram Üzerinden Açın</div><div class="mt-s">Alarmları görmek için botu<br>Telegram'dan açın</div></div>`;return;
  }
  el.innerHTML='<div class="ld"><div class="spin"></div>Alarmlar yükleniyor...</div>';
  try{
    const r=await sf('/api/alarms?uid='+UID,6000);
    const alarms=r?.alarms||[];
    if(!alarms.length){el.innerHTML=`<div class="mt"><div class="mt-i">🔕</div><div class="mt-t">Aktif Alarm Yok</div><div class="mt-s">Botta /alarm_ekle BTCUSDT 3.5 yazın</div></div>`;return;}
    const active=alarms.filter(a=>a.active);const paused=alarms.filter(a=>a.paused);
    const ico={'percent':'📊','rsi':'🔮','band':'📏','price':'🎯'};
    el.innerHTML=`
      <div class="g3" style="margin-bottom:9px">
        <div class="sb"><div class="sv up">${active.length}</div><div class="sl">✅ Aktif</div></div>
        <div class="sb"><div class="sv nu">${paused.length}</div><div class="sl">⏸ Duraklı</div></div>
        <div class="sb"><div class="sv bl">${alarms.length}</div><div class="sl">📋 Toplam</div></div>
      </div>
      ${active.length?`<div class="card cg" style="margin-bottom:8px">
        <div class="sh"><div class="sh-t">✅ <span>AKTİF</span></div></div>
        ${active.map(a=>{const sym=a.symbol.replace('USDT','');const lbl=a.type==='percent'?`%${a.threshold}`:a.type==='rsi'?`RSI ${a.rsi_level}`:`${a.type}`;
          return`<div class="alr">
            <div style="display:flex;align-items:center;gap:7px">${cIco(sym)}<div>
              <div style="display:flex;align-items:center;gap:4px">
                <span style="font-size:11px;font-weight:700">${ico[a.type]||'🔔'} ${sym}</span>
                <span class="cp-btn" onclick="cp('${a.symbol}')">📋</span>
              </div>
              <div style="font-size:9px;color:var(--muted)">${lbl} • ${a.trigger_count||0}× tetiklendi${a.last_triggered?' • '+a.last_triggered:''}</div>
            </div></div>
            <span class="bdg bg">Aktif</span>
          </div>`;}).join('')}
      </div>`:''}
      ${paused.length?`<div class="card cy">
        <div class="sh"><div class="sh-t">⏸ <span>DURAKLATILMIŞ</span></div></div>
        ${paused.map(a=>{const sym=a.symbol.replace('USDT','');return`<div class="alr">
          <div style="display:flex;align-items:center;gap:7px">${cIco(sym)}<span style="font-size:11px;font-weight:700">${sym}</span></div>
          <span class="bdg by">Duraklı</span>
        </div>`;}).join('')}
      </div>`:''}
      <div class="card" style="text-align:center;border-color:rgba(155,111,255,.2)">
        <div style="font-size:10px;color:var(--muted);line-height:1.7">
          🔔 Yeni: <strong>/alarm_ekle BTCUSDT 3.5</strong><br>
          📋 Listele: <strong>/alarmim</strong><br>
          🗑️ Sil: <strong>/alarm_sil BTCUSDT</strong>
        </div>
      </div>`;
  }catch(e){if(el)el.innerHTML=`<div class="ld">⚠️ ${e.message}</div>`;}
}

// ─── TAKVİM ───
function loadTakvim(){
  const el=document.getElementById('takvimOut');if(!el||el.querySelector('.card'))return;
  const now=new Date(),y=now.getFullYear(),m=now.getMonth();
  const evs=[
    {t:'🏦 FOMC Toplantısı',d:18,imp:'h',desc:'Fed faiz kararı — en kritik makro olay.'},
    {t:'📊 ABD CPI Verisi',d:12,imp:'h',desc:'Enflasyon. Yüksek CPI → risk baskısı.'},
    {t:'💼 NFP İstihdam',d:7,imp:'m',desc:'Tarım dışı istihdam raporu.'},
    {t:'📈 PCE Endeksi',d:28,imp:'h',desc:"Fed'in tercih ettiği enflasyon göstergesi."},
  ].map(e=>{let dt=new Date(y,m,e.d);if(dt<now)dt=new Date(y,m+1,e.d);return{...e,dt};}).sort((a,b)=>a.dt-b.dt);
  const ic={h:'var(--r)',m:'var(--y)',l:'var(--g)'};
  el.innerHTML=evs.map(e=>{
    const diff=Math.ceil((e.dt-now)/86400000);
    const w=diff===0?'⚡ BUGÜN':diff===1?'🔜 YARIN':`${diff}g sonra`;
    return`<div class="card" style="border-color:${ic[e.imp]};margin-bottom:7px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div style="flex:1"><div style="font-size:13px;font-weight:800;margin-bottom:3px">${e.t}</div>
        <div style="font-size:10px;color:var(--muted)">${e.desc}</div></div>
        <div style="text-align:right;margin-left:9px;flex-shrink:0">
          <div style="font-size:11px;font-weight:800;color:${ic[e.imp]}">${w}</div>
          <div style="font-size:9px;color:var(--muted);margin-top:2px">${e.dt.toLocaleDateString('tr-TR')}</div>
        </div>
      </div>
    </div>`;}).join('')+`<div class="card" style="border-color:var(--bd);text-align:center">
      <div style="font-size:10px;color:var(--muted)">📅 Bildirimler için botta <span style="color:var(--b2);font-weight:700">/takvim</span> yazın</div>
    </div>`;
}

// ─── INIT ───
loadHome();
loadTicker();
setInterval(loadTicker,20000);
setInterval(()=>{if(CUR==='home')loadHome();},50000);
setInterval(()=>{
  if(CUR==='mkt'&&allCoins.length){
    sf(`${BIN}/ticker/24hr`,10000).then(d=>{
      if(!d||!Array.isArray(d))return;
      allCoins=d.filter(x=>{const b=x.symbol.replace('USDT','');
        return x.symbol.endsWith('USDT')&&!/UP$|DOWN$|BULL$|BEAR$/.test(b)&&parseFloat(x.quoteVolume||0)>200000;
      }).sort((a,b)=>parseFloat(b.quoteVolume||0)-parseFloat(a.quoteVolume||0)).slice(0,100);
      applyF();renderMkt();
    });
  }
},30000);
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
                return aiohttp_web.Response(text='{"error":"no url"}', content_type="application/json")
            allowed = ["api.binance.com", "api.alternative.me", "api.coingecko.com",
                       "api.rss2json.com", "cryptopanic.com", "tradingeconomics.com",
                       "www.coindesk.com"]
            from urllib.parse import urlparse
            parsed = urlparse(target_url)
            if not any(parsed.netloc.endswith(d) for d in allowed):
                return aiohttp_web.Response(text='{"error":"domain not allowed"}', content_type="application/json")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        target_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        body = await resp.text()
                return aiohttp_web.Response(text=body, content_type="application/json",
                                            headers=CORS_HEADERS)
            except Exception as e:
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

        web_app = aiohttp_web.Application()
        web_app.router.add_get("/",                handle_index)
        web_app.router.add_get("/miniapp",         handle_index)
        web_app.router.add_get("/health",          handle_health)
        web_app.router.add_get("/api/proxy",       handle_proxy)
        web_app.router.add_get("/api/news",        handle_news)
        web_app.router.add_get("/api/favorites",   handle_favorites)
        web_app.router.add_get("/api/alarms",      handle_alarms)

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
