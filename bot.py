import telebot
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from cache import get_cache, set_cache
from hyperliquid.info import Info
from hyperliquid.utils import constants

TOKEN = "8966251450:AAGISG43AP1pwgyLfyYjWqNnIbisum1Tig4"
bot = telebot.TeleBot(TOKEN)
info = Info(constants.MAINNET_API_URL)

# =========================
# WARROOM CACHE
# =========================

oi_cache = {}
market_memory = {}

NARRATIVES = {
    "L1": ["BTC","ETH","SOL","AVAX","SUI","APT","SEI","INJ","TIA","NEAR","FTM","ONE","EGLD","KAVA","ROSE","CELO","MOVR"],
    "L2": ["ARB","OP","MATIC","IMX","METIS","BOBA","ZK","STRK","MANTA","BLAST","SCROLL","MODE","BASE"],
    "DeFi": ["AAVE","UNI","CRV","MKR","SNX","COMP","BAL","SUSHI","1INCH","DYDX","GMX","GNS","PENDLE","JOE","CAKE","RDNT","WOO"],
    "Meme": ["DOGE","SHIB","PEPE","FLOKI","BONK","WIF","POPCAT","MYRO","BOME","MEW","NEIRO","MOG","TURBO","BRETT","MOODENG"],
    "AI": ["FET","AGIX","OCEAN","RENDER","WLD","TAO","ARKM","GRT","NMR","AIOZ","ALT","OLAS","VELO"],
    "Gaming":["AXS","SAND","MANA","ENJ","GALA","IMX","BEAM","RON","PYR","MAGIC","TLM","SLP","YGG","PRIME"],
    "RWA": ["ONDO","MPL","CFG","CPOOL","TRU","TRADE","RIO"],
    "Infra": ["LINK","DOT","ATOM","QNT","API3","BAND","PYTH","JTO","W","EIGEN","ETHFI"],
}

schedule_state = {"active": False, "chat_id": None, "interval_min": 60, "thread": None}

def get_narrative(coin):
    for sector, coins in NARRATIVES.items():
        if coin in coins: return sector
    return "Other"

def get_wib():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d/%m %H:%M WIB")

def get_coin(message):
    try:
        args = message.text.split()
        return args[1].upper() if len(args) > 1 else "BTC"
    except:
        return "BTC"

def get_market_data(coin):

    coin = coin.upper()

    mids = info.all_mids()

    current_price = None

    for key, value in mids.items():

        clean_key = (
            key
            .replace("@", "")
            .replace("k", "")
            .upper()
        )

        if clean_key == coin:

            current_price = float(value)
            break

    if not current_price:
        return None

    meta, ctxs = info.meta_and_asset_ctxs()

    funding = 0
    oi = 0
    prev_day = current_price
    volume = 0

    for asset_meta, asset_ctx in zip(
        meta["universe"],
        ctxs
    ):

        if asset_meta["name"].upper() == coin:

            funding = float(
                asset_ctx.get("funding", 0)
            )

            oi = float(
                asset_ctx.get(
                    "openInterest",
                    0
                )
            )

            prev_day = float(
                asset_ctx.get(
                    "prevDayPx",
                    current_price
                )
            )

            volume = float(
                asset_ctx.get(
                    "dayNtlVlm",
                    0
                )
            )

            break

    return {
        "price": current_price,
        "funding": funding,
        "oi": oi,
        "prev_day": prev_day,
        "volume": volume
    }

def calculate_oi_delta(
    coin,
    current_oi
):

    global oi_cache

    old_oi = oi_cache.get(coin)

    oi_cache[coin] = current_oi

    if old_oi is None:
        return 0

    if old_oi == 0:
        return 0

    delta = (
        (current_oi - old_oi)
        / old_oi
    ) * 100

    return delta
# =========================
# MARKET MEMORY ENGINE
# =========================

def update_market_memory(
    coin,
    price,
    funding,
    oi
):

    global market_memory

    if coin not in market_memory:

        market_memory[coin] = []

    market_memory[coin].append({

        "price": price,
        "funding": funding,
        "oi": oi

    })

    # simpan max 5 history
    if len(market_memory[coin]) > 5:

        market_memory[coin].pop(0)


def analyze_market_memory(coin):

    global market_memory

    data = market_memory.get(coin)

    if not data or len(data) < 2:

        return (
            "Belum cukup data memory.\n"
            "AI masih mengamati market."
        )

    first = data[0]
    last = data[-1]

    price_change = (
        (
            last["price"]
            - first["price"]
        )
        / first["price"]
    ) * 100

    funding_change = (
        last["funding"]
        - first["funding"]
    )

    oi_change = (
        (
            last["oi"]
            - first["oi"]
        )
        / first["oi"]
    ) * 100

    narrative = []

    # =========================
    # PRICE + OI
    # =========================

    if (
        price_change > 0
        and oi_change > 0
    ):

        narrative.append(
            "OI terus meningkat seiring kenaikan harga."
        )

        narrative.append(
            "Fresh longs terlihat masuk bertahap."
        )

    elif (
        price_change > 0
        and oi_change < 0
    ):

        narrative.append(
            "Harga naik namun OI menurun."
        )

        narrative.append(
            "Kenaikan kemungkinan berasal dari short squeeze."
        )

    elif (
        price_change < 0
        and oi_change > 0
    ):

        narrative.append(
            "Tekanan short mulai meningkat."
        )

        narrative.append(
            "Seller masih agresif menambah positioning."
        )

    else:

        narrative.append(
            "Belum ada perubahan positioning signifikan."
        )

    # =========================
    # FUNDING ANALYSIS
    # =========================

    if funding_change > 0.01:

        narrative.append(
            "Funding naik perlahan."
        )

        narrative.append(
            "Market mulai crowded ke arah longs."
        )

    elif funding_change < -0.01:

        narrative.append(
            "Funding makin negatif."
        )

        narrative.append(
            "Short positioning mulai dominan."
        )

    else:

        narrative.append(
            "Funding relatif stabil."
        )

    # =========================
    # FINAL
    # =========================

    if (
        oi_change > 5
        and funding_change > 0
    ):

        narrative.append(
            "Risk leverage flush mulai meningkat."
        )

    elif (
        oi_change < -5
    ):

        narrative.append(
            "Leverage mulai keluar dari market."
        )

    return "\n".join(
        f"• {x}" for x in narrative
    )

# =========================
# PSYCHOLOGY ENGINE
# =========================

def analyze_psychology(
    price_change,
    funding,
    oi_delta
):

    psychology = []
    state = "NEUTRAL"

    # =========================
    # GREED
    # =========================

    if (
        price_change > 3
        and funding > 0.03
        and oi_delta > 5
    ):

        state = "GREED"

        psychology.append(
            "Market mulai terlalu bullish."
        )

        psychology.append(
            "Retail cenderung FOMO masuk."
        )

        psychology.append(
            "Leverage mulai crowded."
        )

    # =========================
    # FEAR
    # =========================

    elif (
        price_change < -3
        and funding < -0.03
    ):

        state = "FEAR"

        psychology.append(
            "Market mulai defensif."
        )

        psychology.append(
            "Trader takut downside lanjut."
        )

        psychology.append(
            "Short positioning meningkat."
        )

    # =========================
    # EUPHORIA
    # =========================

    elif (
        funding > 0.05
        and oi_delta > 10
    ):

        state = "EUPHORIA"

        psychology.append(
            "Market terlalu percaya diri."
        )

        psychology.append(
            "Long positioning mulai berlebihan."
        )

        psychology.append(
            "Risk long squeeze meningkat."
        )

    # =========================
    # PANIC
    # =========================

    elif (
        price_change < -5
        and oi_delta < -5
    ):

        state = "PANIC"

        psychology.append(
            "Capitulation mulai terjadi."
        )

        psychology.append(
            "Leverage keluar dari market."
        )

        psychology.append(
            "Trader mulai panic close."
        )

    # =========================
    # EXHAUSTION
    # =========================

    elif (
        abs(price_change) < 1
        and abs(oi_delta) < 1
    ):

        state = "EXHAUSTION"

        psychology.append(
            "Momentum market mulai melemah."
        )

        psychology.append(
            "Belum ada dominasi buyer maupun seller."
        )

    # =========================
    # DEFAULT
    # =========================

    else:

        psychology.append(
            "Psychology market masih mixed."
        )

        psychology.append(
            "Belum ada emosi dominan."
        )

    return state, psychology

# =========================
# ORACLE ENGINE
# =========================

def analyze_oracle(
    price_change,
    funding,
    oi_delta
):

    phase = "NEUTRAL"
    narrative = []

    # =========================
    # EXPANSION PHASE
    # =========================

    if (
        abs(price_change) > 2
        and abs(oi_delta) > 5
    ):

        phase = "EXPANSION PHASE"

        narrative.append(
            "Volatility expansion mulai aktif."
        )

        narrative.append(
            "Market bergerak dengan positioning agresif."
        )

    # =========================
    # COMPRESSION PHASE
    # =========================

    elif (
        abs(price_change) < 1
        and abs(oi_delta) < 2
    ):

        phase = "COMPRESSION PHASE"

        narrative.append(
            "Volatility market mulai mengecil."
        )

        narrative.append(
            "Momentum market sedang tertahan."
        )

    # =========================
    # SQUEEZE PHASE
    # =========================

    if (
        funding < 0
        and price_change > 0
        and oi_delta > 3
    ):

        narrative.append(
            "Short squeeze masih menjadi bahan bakar utama."
        )

        narrative.append(
            "Momentum dapat lanjut selama shorts belum habis."
        )

    elif (
        funding > 0
        and price_change < 0
        and oi_delta > 3
    ):

        narrative.append(
            "Long squeeze mulai aktif."
        )

        narrative.append(
            "Long positioning mulai tertekan."
        )

    # =========================
    # OVERHEATING
    # =========================

    if funding > 0.05:

        narrative.append(
            "Funding mulai overheating."
        )

        narrative.append(
            "Risk leverage flush meningkat."
        )

    elif funding < -0.05:

        narrative.append(
            "Funding terlalu negatif."
        )

        narrative.append(
            "Shorts mulai crowded."
        )

    # =========================
    # MOMENTUM
    # =========================

    if (
        price_change > 0
        and oi_delta > 0
    ):

        narrative.append(
            "Momentum bullish masih bertahan."
        )

    elif (
        price_change < 0
        and oi_delta > 0
    ):

        narrative.append(
            "Tekanan bearish masih dominan."
        )

    else:

        narrative.append(
            "Belum ada dominasi momentum besar."
        )

    # =========================
    # FINAL OUTLOOK
    # =========================

    if (
        oi_delta > 8
        and funding > 0.03
    ):

        narrative.append(
            "Market mulai crowded dan rawan retracement."
        )

    elif (
        oi_delta < -5
    ):

        narrative.append(
            "Leverage mulai keluar dari market."
        )

        narrative.append(
            "Momentum kemungkinan melemah."
        )

    return phase, narrative

# =========================
# PROBABILITY ENGINE
# =========================

def analyze_probability(
    price_change,
    funding,
    oi_delta
):

    bullish = 50
    short_squeeze = 50
    long_squeeze = 50
    fakeout = "LOW"
    volatility = 50

    # =========================
    # BULLISH CONTINUATION
    # =========================

    if (
        price_change > 0
        and oi_delta > 0
    ):

        bullish += 20

    if funding > 0:

        bullish += 10

    if oi_delta > 5:

        volatility += 20

    # =========================
    # SHORT SQUEEZE
    # =========================

    if (
        funding < 0
        and price_change > 0
    ):

        short_squeeze += 25

    if oi_delta > 5:

        short_squeeze += 10

    # =========================
    # LONG SQUEEZE
    # =========================

    if (
        funding > 0
        and price_change < 0
    ):

        long_squeeze += 25

    if funding > 0.05:

        long_squeeze += 10

    # =========================
    # FAKEOUT
    # =========================

    if (
        abs(price_change) > 3
        and abs(oi_delta) < 2
    ):

        fakeout = "HIGH"

    elif (
        abs(price_change) > 2
    ):

        fakeout = "MEDIUM"

    # =========================
    # LIMITS
    # =========================

    bullish = min(bullish, 100)
    short_squeeze = min(short_squeeze, 100)
    long_squeeze = min(long_squeeze, 100)
    volatility = min(volatility, 100)

    return {
        "bullish": bullish,
        "short_squeeze": short_squeeze,
        "long_squeeze": long_squeeze,
        "fakeout": fakeout,
        "volatility": volatility
    }

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = """
🧬 *HYPERLIQUID TERMINAL BOT*
━━━━━━━━━━━━━━━━━━━━━━
`bot by ONE | Realtime HL API`

⚡ *POWER TOOLS* - Institutional Grade
`/warroom <coin>` → All-in-one analysis + Entry/SL/TP
`/delta <coin>` → Orderbook whale bias
`/trap <coin>` → Detect stop hunt MM
`/cluster <coin>` → Liquidation magnet map  
`/correlation <coin>` → BTC correlation + Beta

📊 *MARKET DATA*
`/price <coin>` `/spark <coin>` `/funding <coin>` `/oi <coin>`
`/nukelong <coin>` `/nukeshort <coin>` `/liqmap <coin>`
`/squeeze <coin>` `/sentiment <coin>` `/heatmap` `/narrative`

🎯 *EXECUTION*
`/entry <coin>` → Auto TP/SL calculator
`/scan` `/gainers` `/losers` `/nuke`

🐳 *WHALE TRACKER*
`/whale <coin>` `/whalescan` `/liquidations <coin>`
`/whalewall <coin>` → Orderbook tembok

👤 *TRADER TOOLS*
`/positions <addr>` `/pnl <addr>` `/entrywhale`

🧠 *AI INTELLIGENCE*
`/context <coin>` `/psychology <coin>` 
`/oracle <coin>` `/probability <coin>`

⏰ *AUTO MONITOR*
`/report` `/schedule <min>` `/stopschedule` `/status`

━━━━━━━━━━━━━━━━━━━━━━
💡 *QUICK START:*
1. `/warroom BTC` → Cek bias market
2. `/scan` → Cari setup
3. `/entry SOL` → Eksekusi

⚠️ *DYOR. Not financial advice.*
    """
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['price'])
def price(message):
    try:
        coin = get_coin(message)
        mids = info.all_mids()
        if coin in mids:
            harga = float(mids[coin]) # <-- FIX DI SINI
            txt = f"💰 *{coin}*\n━━━━━━━━━━━━\n`${harga:,.4f}`\n\n⏰ {get_wib()}"
            bot.reply_to(message, txt, parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ {coin} tidak ada")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

import time

@bot.message_handler(commands=['spark', 'sparkline'])
def sparkline(message):
    try:
        coin = get_coin(message)
        msg = bot.reply_to(message, f"📊 Loading sparkline {coin}...")

        # FIX: SDK baru butuh endTime & startTime dalam milidetik
        end_time = int(time.time() * 1000)
        start_time = end_time - (24 * 60 * 60 * 1000) # 24 jam yg lalu

        candles = info.candles_snapshot(coin, "1h", start_time, end_time)
        
        if not candles or len(candles) < 2:
            return bot.edit_message_text(f"❌ Data candle {coin} ga cukup", message.chat.id, msg.message_id)

        closes = [float(c['c']) for c in candles]

        # Bikin sparkline 12 jam terakhir
        last_12h = closes[-12:]
        max_p = max(last_12h)
        min_p = min(last_12h)
        range_p = max_p - min_p

        blocks = "▁▂▃▄▅▆▇█"
        spark = ""
        for p in last_12h:
            level = int((p - min_p) / range_p * 7) if range_p > 0 else 3
            spark += blocks[level]

        change_24h = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] > 0 else 0
        change_12h = ((last_12h[-1] - last_12h[0]) / last_12h[0] * 100) if last_12h[0] > 0 else 0
        trend = "🟢" if change_12h >= 0 else "🔴"

        txt = f"📊 *{coin} Sparkline 12H*\n"
        txt += f"`{spark}` {trend}\n\n"
        txt += f"Price: `${closes[-1]:,.2f}`\n"
        txt += f"12H: `{change_12h:+.2f}%` | 24H: `{change_24h:+.2f}%`\n"
        txt += f"High: `${max_p:,.2f}` | Low: `${min_p:,.2f}`\n\n"
        txt += f"⏰ {get_wib()}"

        bot.edit_message_text(txt, message.chat.id, msg.message_id, parse_mode="Markdown")

    except Exception as e:
        bot.edit_message_text(f"❌ Error: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['delta'])
def orderbook_delta(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: `/delta BTC`", parse_mode='Markdown')
            return

        coin = args[1].upper()
        msg = bot.reply_to(message, f"📊 *Scanning orderbook {coin}...*", parse_mode='Markdown')

        l2 = info.l2_snapshot(coin)
        if not l2 or 'levels' not in l2 or len(l2['levels']) < 2:
            bot.edit_message_text(f"❌ Orderbook `{coin}` ga tersedia", msg.chat.id, msg.message_id)
            return

        bids = l2['levels'][0]
        asks = l2['levels'][1]

        if not bids or not asks:
            bot.edit_message_text(f"📊 *ORDERBOOK DELTA {coin}*\n━━━━━━━━━━━━━━\n\n⚪️ *Status:* ORDERBOOK KOSONG\n\n━━━━━━━━━━━━━━\n⏰ *{get_wib()}*", msg.chat.id, msg.message_id, parse_mode='Markdown')
            return

        # FIX: Support dict & list format
        def get_px_sz(level):
            if isinstance(level, dict):
                return float(level['px']), float(level['sz'])
            else: # list format ['px', 'sz']
                return float(level[0]), float(level[1])

        bid_px, _ = get_px_sz(bids[0])
        ask_px, _ = get_px_sz(asks[0])
        mid_price = (bid_px + ask_px) / 2
        range_pct = 0.02

        bid_vol = 0
        for b in bids:
            px, sz = get_px_sz(b)
            if px >= mid_price * (1 - range_pct):
                bid_vol += sz * px

        ask_vol = 0
        for a in asks:
            px, sz = get_px_sz(a)
            if px <= mid_price * (1 + range_pct):
                ask_vol += sz * px

        total = bid_vol + ask_vol

        if total < 100:
            text = f"📊 *ORDERBOOK DELTA {coin}*\n"
            text += "━━━━━━━━━━━━━━\n"
            text += f"💰 *Harga:* ${mid_price:.4f}\n"
            text += f"📊 *Total Liq 2%:* ${total:,.0f}\n"
            text += "━━━━━━━━━━━━━━\n"
            text += f"⚪️ *Status:* ORDERBOOK SUPER TIPIS\n"
            text += f"💡 *Insight:*\nLikuiditas < $100. Jangan trade\n\n"
            text += "━━━━━━━━━━━━━━\n"
            text += f"⏰ *{get_wib()}*"
            bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')
            return

        bid_pct = (bid_vol / total) * 100
        ask_pct = (ask_vol / total) * 100
        delta = bid_pct - ask_pct

        if delta > 30: bias = "🟢 STRONG BID" ; insight = "Whale akumulasi. Tembok buy tebel"
        elif delta > 10: bias = "🟢 BID" ; insight = "Buyer dominan. Potensi naik"
        elif delta < -30: bias = "🔴 STRONG ASK" ; insight = "Whale distribusi. Tembok sell tebel"
        elif delta < -10: bias = "🔴 ASK" ; insight = "Seller dominan. Potensi turun"
        else: bias = "⚪️ BALANCE" ; insight = "Orderbook seimbang. Sideways"

        text = f"📊 *ORDERBOOK DELTA {coin}*\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"💰 *Harga:* ${mid_price:.4f}\n"
        text += f"⚡ *Delta:* {delta:+.1f}%\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"🟢 *BID:* ${bid_vol:,.0f} [{bid_pct:.0f}%]\n"
        text += f"🔴 *ASK:* ${ask_vol:,.0f} [{ask_pct:.0f}%]\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"📡 *Bias:* {bias}\n"
        text += f"💡 *Insight:*\n{insight}\n\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')

    except Exception as e:
        error_msg = str(e) if str(e) else "Unknown error"
        bot.edit_message_text(f"❌ *Error Delta*\n`{error_msg}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['trap'])
def stop_hunt_trap(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: `/trap BTC`", parse_mode='Markdown')
            return

        coin = args[1].upper()
        msg = bot.reply_to(message, f"🪤 *Scanning trap {coin}...*", parse_mode='Markdown')

        # FIX: candles_snapshot pake 's'
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = end_time - (30 * 60 * 1000) # 30 menit

        candles = info.candles_snapshot(coin, '1m', start_time, end_time)
        if len(candles) < 10:
            bot.edit_message_text(f"❌ Data candle `{coin}` kurang", msg.chat.id, msg.message_id)
            return

        traps = []
        for i in range(2, len(candles)):
            c = candles[i]
            o, h, l, c_price, v = float(c['o']), float(c['h']), float(c['l']), float(c['c']), float(c['v'])

            body = abs(c_price - o)
            if body == 0: continue

            upper_wick = h - max(o, c_price)
            lower_wick = min(o, c_price) - l
            vol_usd = v * c_price

            # Syarat trap: wick > 2x body + volume > $50k
            if lower_wick > body * 2 and vol_usd > 50000 and c_price > o:
                traps.append({'type': 'LONG TRAP', 'level': l, 'vol': vol_usd, 'age': len(candles)-i})
            elif upper_wick > body * 2 and vol_usd > 50000 and c_price < o:
                traps.append({'type': 'SHORT TRAP', 'level': h, 'vol': vol_usd, 'age': len(candles)-i})

        current_price = float(candles[-1]['c'])

        text = f"🪤 *STOP HUNT TRAP {coin}*\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"💰 *Harga:* ${current_price:.4f}\n"
        text += "━━━━━━━━━━━━━━\n"

        if not traps:
            text += "⚪️ *Status:* NO TRAP\n"
            text += "💡 *Insight:*\nBelum ada sweep 30 menit terakhir\n"
        else:
            last_trap = traps[-1]
            text += f"📡 *Status:* {last_trap['type']} DETECTED\n"
            text += f"📍 *Level:* ${last_trap['level']:.4f}\n"
            text += f"📊 *Volume:* ${last_trap['vol']:,.0f}\n"
            text += f"⏱️ *Age:* {last_trap['age']}m ago\n"
            text += "━━━━━━━━━━━━━━\n"
            if 'LONG' in last_trap['type']:
                text += f"💡 *Insight:*\nSL Long udah disapu. Jalan naik bersih\n"
            else:
                text += f"💡 *Insight:*\nSL Short udah disapu. Jalan turun bersih\n"

        text += "\n━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')

    except Exception as e:
        bot.edit_message_text(f"❌ *Error*\n`{str(e)}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['cluster'])
def liquidation_cluster(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: `/cluster BTC`", parse_mode='Markdown')
            return

        coin = args[1].upper()
        msg = bot.reply_to(message, f"🎯 *Mapping cluster {coin}...*", parse_mode='Markdown')

        # Data market
        meta_ctxs = info.meta_and_asset_ctxs()
        idx = next((i for i, x in enumerate(meta_ctxs[0]['universe']) if x['name'] == coin), None)
        if idx is None:
            bot.edit_message_text(f"❌ `{coin}` ga ada", msg.chat.id, msg.message_id)
            return

        ctx = meta_ctxs[1][idx]
        price = float(ctx['markPx'])
        oi = float(ctx['openInterest'])

        # Simulasi cluster dari OI. HL ga kasih level exact, jadi pake estimasi 2% & 4%
        long_liq_1 = price * 0.98
        long_liq_2 = price * 0.96
        short_liq_1 = price * 1.02
        short_liq_2 = price * 1.04

        # Estimasi size: 20% OI di tiap level
        cluster_size = oi * 0.2

        text = f"🎯 *LIQUIDATION CLUSTER {coin}*\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"💰 *Harga:* ${price:.4f}\n"
        text += f"📊 *Total OI:* ${oi:,.2f}M\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"⬆️ ${short_liq_2:.4f} | SHORT LIQ | ${cluster_size:,.2f}M\n"
        text += f"⬆️ ${short_liq_1:.4f} | SHORT LIQ | ${cluster_size:,.2f}M\n\n"
        text += f"📍 ${price:.4f} ← current price\n\n"
        text += f"⬇️ ${long_liq_1:.4f} | LONG LIQ | ${cluster_size:,.2f}M\n"
        text += f"⬇️ ${long_liq_2:.4f} | LONG LIQ | ${cluster_size:,.2f}M\n"
        text += "━━━━━━━━━━━━━━\n"

        if cluster_size > 50_000_000:
            text += "⚠️ *Cluster gede terdeteksi*\n"
            text += "Harga = magnet ke level terdekat\n"
        else:
            text += "⚖️ *Cluster relatif kecil*\n"
            text += "Potensi market ranging\n"

        text += "\n━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')

    except Exception as e:
        bot.edit_message_text(f"❌ *Error*\n`{str(e)}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['correlation', 'corr'])
def correlation_analysis(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: `/correlation SOL`\nCek korelasi vs BTC", parse_mode='Markdown')
            return

        coin = args[1].upper()
        if coin == 'BTC':
            bot.reply_to(message, "😅 BTC vs BTC = 1.0 lah bro", parse_mode='Markdown')
            return

        msg = bot.reply_to(message, f"🔗 *Analyzing correlation {coin}...*", parse_mode='Markdown')

        # 1. Ambil candle 5m BTC + coin target
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = end_time - (100 * 5 * 60 * 1000) # 100 candle 5m = 8 jam

        btc_candles = info.candles_snapshot('BTC', '5m', start_time, end_time)
        coin_candles = info.candles_snapshot(coin, '5m', start_time, end_time)

        if len(btc_candles) < 50 or len(coin_candles) < 50:
            bot.edit_message_text(f"❌ Data candle `{coin}` kurang", msg.chat.id, msg.message_id)
            return

        # 2. Ambil close price & hitung return %
        btc_closes = [float(c['c']) for c in btc_candles[-100:]]
        coin_closes = [float(c['c']) for c in coin_candles[-100:]]

        if len(btc_closes)!= len(coin_closes):
            min_len = min(len(btc_closes), len(coin_closes))
            btc_closes = btc_closes[-min_len:]
            coin_closes = coin_closes[-min_len:]

        btc_returns = [(btc_closes[i] - btc_closes[i-1]) / btc_closes[i-1] for i in range(1, len(btc_closes))]
        coin_returns = [(coin_closes[i] - coin_closes[i-1]) / coin_closes[i-1] for i in range(1, len(coin_closes))]

        # 3. Hitung Pearson Correlation manual
        def pearson_corr(x, y):
            n = len(x)
            sum_x = sum(x)
            sum_y = sum(y)
            sum_xy = sum(x[i] * y[i] for i in range(n))
            sum_x2 = sum(xi * xi for xi in x)
            sum_y2 = sum(yi * yi for yi in y)

            numerator = n * sum_xy - sum_x * sum_y
            denominator = ((n * sum_x2 - sum_x ** 2) * (n * sum_y2 - sum_y ** 2)) ** 0.5
            return numerator / denominator if denominator!= 0 else 0

        corr = pearson_corr(btc_returns, coin_returns)

        # 4. Interpretasi
        if corr >= 0.8:
            status = "🔴 NEMPEL BTC"
            insight = f"{coin} bakal ikut BTC. BTC dump = {coin} dump"
            risk = "HIGH RISK kalo BTC bearish"
        elif corr >= 0.5:
            status = "🟡 IKUT BTC"
            insight = f"{coin} masih ngikut BTC tapi ga 100%"
            risk = "MEDIUM RISK"
        elif corr >= -0.5:
            status = "🟢 DECOUPLING"
            insight = f"{coin} jalan sendiri. Ada narasi kuat"
            risk = "LOW RISK - Alpha potential"
        else:
            status = "🔵 LAWAN BTC"
            insight = f"{coin} naik pas BTC turun. Hedging bagus"
            risk = "HEDGING ASSET"

        # 5. Hitung beta - seberapa kenceng geraknya vs BTC
        btc_vol = (max(btc_closes) - min(btc_closes)) / min(btc_closes) * 100
        coin_vol = (max(coin_closes) - min(coin_closes)) / min(coin_closes) * 100
        beta = coin_vol / btc_vol if btc_vol > 0 else 1

        text = f"🔗 *CORRELATION {coin}/BTC*\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"📊 *Periode:* 8 jam terakhir\n"
        text += f"⚡ *Correlation:* {corr:.2f}\n"
        text += f"📈 *Beta:* {beta:.2f}x\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"📡 *Status:* {status}\n"
        text += f"💰 *BTC Move:* {btc_vol:.1f}%\n"
        text += f"💰 *{coin} Move:* {coin_vol:.1f}%\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"💡 *Insight:*\n{insight}\n\n"
        text += f"⚠️ *Risk:* {risk}\n\n"
        text += "━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')

    except Exception as e:
        error_msg = str(e) if str(e) else "Data error"
        bot.edit_message_text(f"❌ *Error Correlation*\n`{error_msg}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['nukelong'])
def nukelong(message):
    try:
        msg = bot.reply_to(message, "🔍 Scanning nuke setup... 3-5 detik")
        
        # Cek cache dulu biar enteng
        cached = get_cache('nukelong', 60)
        if cached:
            return bot.edit_message_text(cached, message.chat.id, msg.message_id, parse_mode="Markdown")
        
        # List 20 coin gede doang biar enteng
        coins = ["BTC","ETH","SOL","ARB","OP","MATIC","AVAX","BNB","APT","SUI",
                 "LINK","DOGE","WIF","PEPE","TIA","SEI","PYTH","JTO","JUP","W"]
        
        mids = info.all_mids()
        meta = info.meta()
        best_signal = None
        
        for coin_data in meta["universe"]:
            coin = coin_data["name"]
            if coin not in coins: continue
            
            try:
                # Ambil data 1H
                end_time = int(time.time() * 1000)
                start_time = end_time - (60 * 60 * 1000)
                candles = info.candles_snapshot(coin, "1h", start_time, end_time)
                
                if len(candles) < 2: continue
                
                # Data funding + OI
                fund = info.funding_history(coin, start_time, end_time)
                oi_data = info.meta_and_asset_ctxs()
                
                # Itung syarat
                current_price = float(mids[coin])
                hour_low = float(candles[0]['l'])
                hour_open = float(candles[0]['o'])
                
                # 1. Funding minus parah
                funding_rate = float(fund[-1]['fundingRate']) if fund else 0
                if funding_rate > -0.0005: continue # -0.05%
                
                # 2. Price udah mantul >1.5% dari low
                bounce = ((current_price - hour_low) / hour_low) * 100
                if bounce < 1.5: continue
                
                # 3. OI turun >15% - pake data 1h lalu vs sekarang
                oi_now = 0
                for asset in oi_data[1]:
                    if asset['coin'] == coin:
                        oi_now = float(asset['openInterest'])
                if oi_now == 0: continue
                
                # Simpel: cek harga drop >3% = asumsi long kena nuke
                drop = ((hour_open - hour_low) / hour_open) * 100
                if drop < 3: continue
                
                # Lolos semua syarat = SINYAL
                entry = current_price
                sl = hour_low * 0.997 # Low -0.3%
                tp1 = entry * 1.03
                tp2 = entry * 1.06
                rr = (tp2 - entry) / (entry - sl)
                
                txt = f"🚨 *SINYAL BARBAR: NUKELONG*\n"
                txt += f"━━━━━━━━━━━━\n"
                txt += f"Coin: `{coin}/USDC`\n"
                txt += f"Alasan: Funding `{funding_rate*100:.3f}%` | Drop `{drop:.1f}%` | Bounce `{bounce:.1f}%`\n"
                txt += f"Entry: `${entry:,.4f}`\n"
                txt += f"SL: `${sl:,.4f}` = `{((sl-entry)/entry)*100:.2f}%`\n"
                txt += f"TP1: `${tp1:,.4f}` = `+3%`\n"
                txt += f"TP2: `${tp2:,.4f}` = `+6%`\n"
                txt += f"R:R 1:{rr:.1f} | Valid 15 menit\n\n"
                txt += f"⏰ {get_wib()}"
                
                best_signal = txt
                break # Ambil 1 yg paling bagus aja
                
            except: continue
        
        if not best_signal:
            best_signal = "❌ *Ga ada setup NUKELONG sekarang*\n\nMarket normal. Coba lagi 15 menitan."
        
        set_cache('nukelong', best_signal)
        bot.edit_message_text(best_signal, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['nukeshort'])
def nukeshort(message):
    try:
        msg = bot.reply_to(message, "🔍 Scanning SHORT nuke setup... 3-5 detik")
        
        # Cek cache 1 menit biar enteng
        cached = get_cache('nukeshort', 60)
        if cached:
            return bot.edit_message_text(cached, message.chat.id, msg.message_id, parse_mode="Markdown")
        
        # 20 coin gede biar scan cepet
        coins = ["BTC","ETH","SOL","ARB","OP","MATIC","AVAX","BNB","APT","SUI",
                 "LINK","DOGE","WIF","PEPE","TIA","SEI","PYTH","JTO","JUP","W"]
        
        mids = info.all_mids()
        meta = info.meta()
        best_signal = None
        
        for coin_data in meta["universe"]:
            coin = coin_data["name"]
            if coin not in coins: continue
            
            try:
                # Data 1H buat cek FOMO
                end_time = int(time.time() * 1000)
                start_time = end_time - (60 * 60 * 1000)
                candles = info.candles_snapshot(coin, "1h", start_time, end_time)
                
                if len(candles) < 2: continue
                
                # Funding + OI
                fund = info.funding_history(coin, start_time, end_time)
                oi_data = info.meta_and_asset_ctxs()
                
                current_price = float(mids)
                hour_high = float(candles[0]['h'])
                hour_open = float(candles[0]['o'])
                
                # 1. SYARAT: Funding plus parah > +0.05% = Long bayar mahal
                funding_rate = float(fund[-1]['fundingRate']) if fund else 0
                if funding_rate < 0.0005: continue
                
                # 2. SYARAT: Udah dump >1.5% dari high = Mulai distribusi
                dump = ((hour_high - current_price) / hour_high) * 100
                if dump < 1.5: continue
                
                # 3. SYARAT: Naik >3% 1H = Long FOMO masuk banyak
                pump = ((hour_high - hour_open) / hour_open) * 100
                if pump < 3: continue
                
                # 4. OI Check - ambil OI sekarang
                oi_now = 0
                for asset in oi_data[1]:
                    if asset['coin'] == coin:
                        oi_now = float(asset['openInterest'])
                if oi_now == 0: continue
                
                # SINYAL LOLOS
                entry = current_price
                sl = hour_high * 1.003 # High +0.3%
                tp1 = entry * 0.97 # -3%
                tp2 = entry * 0.94 # -6%
                rr = (entry - tp2) / (sl - entry)
                
                txt = f"🚨 *SINYAL BARBAR: NUKESHORT*\n"
                txt += f"━━━━━━━━━━━━\n"
                txt += f"Coin: `{coin}/USDC`\n"
                txt += f"Alasan: Funding `{funding_rate*100:.3f}%` | Pump `{pump:.1f}%` | Dump `{dump:.1f}%`\n"
                txt += f"Entry: `${entry:,.4f}`\n"
                txt += f"SL: `${sl:,.4f}` = `+{((sl-entry)/entry)*100:.2f}%`\n"
                txt += f"TP1: `${tp1:,.4f}` = `-3%`\n"
                txt += f"TP2: `${tp2:,.4f}` = `-6%`\n"
                txt += f"R:R 1:{rr:.1f} | Valid 15 menit\n\n"
                txt += f"⏰ {get_wib()}"
                
                best_signal = txt
                break
                
            except Exception: continue
        
        if not best_signal:
            best_signal = "❌ *Ga ada setup NUKESHORT sekarang*\n\nMarket belum FOMO. Coba lagi 15 menitan."
        
        set_cache('nukeshort', best_signal)
        bot.edit_message_text(best_signal, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['liqmap'])
def liqmap(message):
    try:
        # =========================
        # AMBIL COIN
        # =========================
        args = message.text.split()

        if len(args) < 2:
            coin = "BTC"
        else:
            coin = args[1].upper()

        msg = bot.reply_to(
            message,
            f"💀 Scanning liquidation map {coin}..."
        )

        # =========================
        # AMBIL HARGA
        # =========================
        mids = info.all_mids()

        current_price = None

        for key, value in mids.items():

            clean_key = key.replace("@", "").replace("k", "").upper()

            if clean_key == coin:
                current_price = float(value)
                break

        if not current_price:
            return bot.edit_message_text(
                f"❌ Coin {coin} ga ada di Hyperliquid bro.",
                message.chat.id,
                msg.message_id
            )

        # =========================
        # AMBIL OI
        # =========================
        meta, ctxs = info.meta_and_asset_ctxs()

        oi_total = 0
        found = False

        for asset_meta, asset_ctx in zip(meta["universe"], ctxs):

            asset_name = asset_meta["name"].upper()

            if asset_name == coin:

                found = True

                print("DEBUG ASSET =", asset_ctx)

                oi = 0

                # Kalau dict
                if isinstance(asset_ctx, dict):

                    oi = float(
                        asset_ctx.get("openInterest")
                        or asset_ctx.get("open_interest")
                        or asset_ctx.get("oi")
                        or 0
                    )

                # Kalau list
                elif isinstance(asset_ctx, list):

                    if len(asset_ctx) > 0 and isinstance(asset_ctx[0], dict):

                        oi = float(
                            asset_ctx[0].get("openInterest")
                            or asset_ctx[0].get("open_interest")
                            or asset_ctx[0].get("oi")
                            or 0
                        )

                oi_total = oi * current_price
                break

        if not found:
            return bot.edit_message_text(
                f"❌ {coin} belum ada perp di HL bro.",
                message.chat.id,
                msg.message_id
            )

        if oi_total <= 0:
            return bot.edit_message_text(
                f"❌ {coin} OI masih 0 bro.",
                message.chat.id,
                msg.message_id
            )

        # =========================
        # HITUNG LIQ LEVEL
        # =========================
        levels = []

        leverages = [25, 20, 10, 5]
        weights = [0.4, 0.3, 0.2, 0.1]

        for lev, weight in zip(leverages, weights):

            long_liq_price = current_price * (1 - 0.99 / lev)
            short_liq_price = current_price * (1 + 0.99 / lev)

            long_size = oi_total * weight * 0.5
            short_size = oi_total * weight * 0.5

            levels.append({
                "price": long_liq_price,
                "size": long_size,
                "type": "LONG LIQ"
            })

            levels.append({
                "price": short_liq_price,
                "size": short_size,
                "type": "SHORT LIQ"
            })

        # =========================
        # SORT LEVEL
        # =========================
        above = sorted(
            [x for x in levels if x["price"] > current_price],
            key=lambda x: x["price"]
        )

        below = sorted(
            [x for x in levels if x["price"] < current_price],
            key=lambda x: x["price"],
            reverse=True
        )

        # =========================
        # FORMAT TEXT
        # =========================
        txt = f"""
💀 *LIQUIDATION MAP {coin}*
━━━━━━━━━━━━━━
💰 Harga: `${current_price:,.4f}`
📊 Total OI: `${oi_total/1e6:.2f}M`
━━━━━━━━━━━━━━
"""

        for lvl in above[:2]:

            txt += (
                f"⬆️ `${lvl['price']:,.4f}`"
                f" | {lvl['type']}"
                f" | `${lvl['size']/1e6:.2f}M`\n"
            )

        txt += f"\n📍 `${current_price:,.4f}` ← current price\n\n"

        for lvl in below[:2]:

            txt += (
                f"⬇️ `${lvl['price']:,.4f}`"
                f" | {lvl['type']}"
                f" | `${lvl['size']/1e6:.2f}M`\n"
            )

        txt += "\n━━━━━━━━━━━━━━\n"

        # =========================
        # ANALISIS
        # =========================
        long_liq = below[0]["size"] if below else 0
        short_liq = above[0]["size"] if above else 0

        if long_liq > short_liq * 1.5:

            txt += "📉 *Long liquidation lebih tebel*\n"
            txt += "Biasanya market rawan flush bawah."

        elif short_liq > long_liq * 1.5:

            txt += "📈 *Short liquidation lebih tebel*\n"
            txt += "Biasanya market rawan squeeze atas."

        else:

            txt += "⚖️ *Liquidation relatif imbang*\n"
            txt += "Potensi market ranging."

        txt += f"\n\n⏰ {get_wib()}"

        # =========================
        # SEND
        # =========================
        bot.edit_message_text(
            txt,
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        print("LIQMAP ERROR =", e)

        bot.edit_message_text(
            f"❌ Error liqmap:\n`{str(e)[:300]}`",
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['whalewall'])
def whalewall(message):
    try:
        coin = get_coin(message)
        msg = bot.reply_to(message, f"🧱 Scanning whalewall {coin}...")
        
        # Ambil harga mid
        mids = info.all_mids()
        price_key = None
        for key in [coin, f"k{coin}", f"@{coin}"]:
            if key in mids:
                price_key = key
                break
        
        if not price_key:
            return bot.edit_message_text(f"❌ Coin {coin} ga ada di HL bro.", message.chat.id, msg.message_id)
        
        current_price = float(mids[price_key])
        
        # Ambil orderbook L2
        l2 = info.l2_snapshot(coin)
        
        bids = l2['levels'][0] # [price, size]
        asks = l2['levels'][1]
        
        # Cari tembok gede > $500k
        big_bids = []
        for bid in bids:
            price = float(bid['px'])
            size = float(bid['sz'])
            usd_value = price * size
            if usd_value > 500000: # Tembok $500k+
                big_bids.append({"price": price, "size": size, "usd": usd_value})
        
        big_asks = []
        for ask in asks:
            price = float(ask['px'])
            size = float(ask['sz'])
            usd_value = price * size
            if usd_value > 500000: # Tembok $500k+
                big_asks.append({"price": price, "size": size, "usd": usd_value})
        
        # Sort: bid dari gede ke kecil, ask dari kecil ke gede
        big_bids = sorted(big_bids, key=lambda x: x['price'], reverse=True)[:3]
        big_asks = sorted(big_asks, key=lambda x: x['price'])[:3]
        
        txt = f"🧱 *WHALE WALL {coin}*\n"
        txt += f"━━━━━━━━━━━━\n"
        txt += f"Harga: `${current_price:,.4f}`\n"
        txt += f"Filter: Tembok > $500k\n"
        txt += f"━━━━━━━━━━━━\n"
        
        txt += f"🔴 *ASK WALLS / Resistance:*\n"
        if big_asks:
            for wall in big_asks:
                txt += f"⬆️ `${wall['price']:,.4f}` = `${wall['usd']/1e6:.2f}M`\n"
        else:
            txt += f"Tidak ada tembok > $500k\n"
        
        txt += f"\n📍 `${current_price:,.4f}` ← Harga sekarang\n\n"
        
        txt += f"🟢 *BID WALLS / Support:*\n"
        if big_bids:
            for wall in big_bids:
                txt += f"⬇️ `${wall['price']:,.4f}` = `${wall['usd']/1e6:.2f}M`\n"
        else:
            txt += f"Tidak ada tembok > $500k\n"
        
        txt += f"━━━━━━━━━━━━\n"
        
        # Analisa
        nearest_ask = big_asks[0]['usd'] if big_asks else 0
        nearest_bid = big_bids[0]['usd'] if big_bids else 0
        
        if nearest_ask > nearest_bid * 2:
            txt += f"Kesimpulan: *Tembok jual tebel*. Susah naik, rawan dump.\n"
        elif nearest_bid > nearest_ask * 2:
            txt += f"Kesimpulan: *Tembok beli tebel*. Ada whale jaga bawah.\n"
        elif nearest_bid > 0 and nearest_ask > 0:
            txt += f"Kesimpulan: *Tembok imbang*. Bakal range di sini.\n"
        else:
            txt += f"Kesimpulan: *Orderbook tipis*. Rawan spike 2 arah.\n"
        
        txt += f"⏰ {get_wib()}"
        
        bot.edit_message_text(txt, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error whalewall: {str(e)[:100]}", message.chat.id, msg.message_id)	

@bot.message_handler(commands=['funding'])
def funding(message):
    try:
        coin = get_coin(message)
        data = info.funding_history(coin, 1)
        if not data: return bot.reply_to(message, f"❌ {coin} tidak ada")
        rate = float(data[0]["fundingRate"]) * 100
        arah = "🟢 Long bayar Short" if rate > 0 else "🔴 Short bayar Long"
        if abs(rate) > 0.05: level = "🔥 EKSTREM"
        elif abs(rate) > 0.01: level = "⚠️ TINGGI"
        else: level = "✅ Normal"
        txt = f"💸 *Funding {coin}*\n━━━━━━━━━━━━\nRate: `{rate:.4f}%`/jam\nArah: {arah}\nStatus: {level}\n\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['oi'])
def oi(message):
    try:
        coin = get_coin(message)
        data = info.meta_and_asset_ctxs()
        for asset, ctx in zip(data[0]["universe"], data[1]):
            if asset["name"] == coin:
                oi_usd = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0)) / 1e6
                if oi_usd > 1000: w = "🔥 SANGAT TINGGI"
                elif oi_usd > 500: w = "⚠️ TINGGI"
                else: w = "✅ Normal"
                txt = f"📊 *OI {coin}*\n━━━━━━━━━━━━\n`${oi_usd:.2f}M`\nStatus: {w}\n\n⏰ {get_wib()}"
                bot.reply_to(message, txt, parse_mode="Markdown")
                return
        bot.reply_to(message, f"❌ {coin} tidak ada")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['squeeze'])
def squeeze(message):
    try:
        coin = get_coin(message)
        msg = bot.reply_to(message, f"⚡ Scanning squeeze setup {coin}...")
        
        # 1. Ambil harga
        mids = info.all_mids()
        price_key = None
        for key in [coin, f"k{coin}", f"@{coin}"]:
            if key in mids:
                price_key = key
                break
        if not price_key:
            return bot.edit_message_text(f"❌ Coin {coin} ga ada di HL bro.", message.chat.id, msg.message_id)
        current_price = float(mids[price_key])
        
        # 2. Ambil Funding Rate
        meta, ctxs = info.meta_and_asset_ctxs()
        funding = 0
        oi_now = 0
        for asset_meta, asset_ctx in zip(meta['universe'], ctxs):
            if asset_meta['name'] == coin:
                funding = float(asset_ctx.get('funding', 0)) * 100 # ke %
                oi_now = float(asset_ctx.get('oi', 0)) * current_price
                break
        
        # 3. Ambil Liqmap cluster terdekat
        oi_usd = oi_now
        levels = []
        leverages = [20, 10, 5]
        weights = [0.5, 0.3, 0.2]
        
        for lev, weight in zip(leverages, weights):
            long_liq_price = current_price * (1 - 0.99/lev)
            short_liq_price = current_price * (1 + 0.99/lev)
            long_liq_size = oi_usd * weight * 0.5
            short_liq_size = oi_usd * weight * 0.5
            levels.append({"price": long_liq_price, "size": long_liq_size, "type": "Long"})
            levels.append({"price": short_liq_price, "size": short_liq_size, "type": "Short"})
        
        above = sorted([l for l in levels if l['price'] > current_price], key=lambda x: x['price'])
        below = sorted([l for l in levels if l['price'] < current_price], key=lambda x: x['price'], reverse=True)
        
        short_liq_deket = above[0] if above else {"price": 0, "size": 0}
        long_liq_deket = below[0] if below else {"price": 0, "size": 0}
        
        # 4. Ambil Whalewall terdekat
        l2 = info.l2_snapshot(coin)
        bids, asks = l2['levels'][0], l2['levels'][1]
        
        big_bid_wall = 0
        for bid in bids[:10]: # cek 10 bid terdekat
            if float(bid['px']) * float(bid['sz']) > 500000:
                big_bid_wall = float(bid['px']) * float(bid['sz'])
                break
        
        big_ask_wall = 0
        for ask in asks[:10]: # cek 10 ask terdekat
            if float(ask['px']) * float(ask['sz']) > 500000:
                big_ask_wall = float(ask['px']) * float(ask['sz'])
                break
        
        # 5. SCORING SQUEEZE
        short_score = 0
        long_score = 0
        txt = f"⚡ *SQUEEZE SCANNER {coin}*\n"
        txt += f"━━━━━━━━━━━━\n"
        txt += f"Harga: `${current_price:,.4f}`\n"
        txt += f"━━━━━━━━━━━━\n"
        
        # Faktor 1: Funding
        if funding > 0.05: # >0.05%/jam = ekstrem
            short_score += 40
            txt += f"Funding: `{funding:.4f}%/jam` 🔴 LONG BAYAR MAHAL\n"
        elif funding < -0.05:
            long_score += 40
            txt += f"Funding: `{funding:.4f}%/jam` 🟢 SHORT BAYAR MAHAL\n"
        else:
            txt += f"Funding: `{funding:.4f}%/jam` ⚪ NETRAL\n"
        
        # Faktor 2: Liq Cluster
        if short_liq_deket['size'] > 300_000_000: # $300M+
            short_score += 30
            txt += f"Liq Short: `${short_liq_deket['size']/1e6:.0f}M` di `${short_liq_deket['price']:,.0f}` 🔴 TEBEL\n"
        else:
            txt += f"Liq Short: `${short_liq_deket['size']/1e6:.0f}M` ⚪ TIPIS\n"
            
        if long_liq_deket['size'] > 300_000_000:
            long_score += 30
            txt += f"Liq Long: `${long_liq_deket['size']/1e6:.0f}M` di `${long_liq_deket['price']:,.0f}` 🔴 TEBEL\n"
        else:
            txt += f"Liq Long: `${long_liq_deket['size']/1e6:.0f}M` ⚪ TIPIS\n"
        
        # Faktor 3: Whalewall
        if big_ask_wall < 1_000_000 and short_liq_deket['size'] > 0: # Tembok ask tipis
            short_score += 30
            txt += f"Tembok Ask: `${big_ask_wall/1e6:.1f}M` 🟢 TIPIS = Gampang jebol\n"
        else:
            txt += f"Tembok Ask: `${big_ask_wall/1e6:.1f}M` 🔴 TEBEL\n"
            
        if big_bid_wall < 1_000_000 and long_liq_deket['size'] > 0: # Tembok bid tipis
            long_score += 30
            txt += f"Tembok Bid: `${big_bid_wall/1e6:.1f}M` 🟢 TIPIS = Gampang jebol\n"
        else:
            txt += f"Tembok Bid: `${big_bid_wall/1e6:.1f}M` 🔴 TEBEL\n"
        
        txt += f"━━━━━━━━━━━━\n"
        
        # KESIMPULAN
        if short_score >= 70:
            txt += f"🚨 *SHORT SQUEEZE ALERT {short_score}%* 🚨\n"
            txt += f"Target: `${short_liq_deket['price']:,.0f}` = `${short_liq_deket['size']/1e6:.0f}M`\n"
            txt += f"SL: Di bawah `${long_liq_deket['price']:,.0f}`\n"
            txt += f"Potensi: `+{((short_liq_deket['price']/current_price)-1)*100:.1f}%`\n"
        elif long_score >= 70:
            txt += f"🚨 *LONG SQUEEZE ALERT {long_score}%* 🚨\n"
            txt += f"Target: `${long_liq_deket['price']:,.0f}` = `${long_liq_deket['size']/1e6:.0f}M`\n"
            txt += f"SL: Di atas `${short_liq_deket['price']:,.0f}`\n"
            txt += f"Potensi: `{((long_liq_deket['price']/current_price)-1)*100:.1f}%`\n"
        else:
            txt += f"😴 *TIDAK ADA SETUP SQUEEZE*\n"
            txt += f"Short Score: `{short_score}%` | Long Score: `{long_score}%`\n"
            txt += f"Tunggu funding ekstrem atau liq numpuk dulu.\n"
        
        txt += f"⏰ {get_wib()}"
        
        bot.edit_message_text(txt, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error squeeze: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['sentiment', 'LSratio'])
def sentiment(message):
    try:
        coin = get_coin(message)
        data = info.meta_and_asset_ctxs()
        for asset, ctx in zip(data[0]["universe"], data[1]):
            if asset["name"] == coin:
                funding = float(ctx.get("funding", 0)) * 100
                price = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", price))
                change = ((price - prev) / prev * 100) if prev else 0
                oi = float(ctx.get("openInterest", 0)) * price / 1e6
                skor = 0
                if funding > 0.05: skor += 2
                elif funding > 0.01: skor += 1
                elif funding < -0.05: skor -= 2
                elif funding < -0.01: skor -= 1
                if change > 5: skor += 1
                elif change < -5: skor -= 1
                if skor >= 3: emosi = "🔥 SERAKAH — Long Squeeze"
                elif skor >= 1: emosi = "🟢 OPTIMIS"
                elif skor <= -3: emosi = "💀 PANIK — Short Squeeze"
                elif skor <= -1: emosi = "🔴 KETAKUTAN"
                else: emosi = "⚪ NETRAL"
                txt = f"🧠 *Sentiment {coin}*\n━━━━━━━━━━━━\n"
                txt += f"Price: `${price:,.2f}` ({change:+.1f}%)\n"
                txt += f"Funding: `{funding:.4f}%`\nOI: `${oi:.0f}M`\n\n{emosi}\n\n⏰ {get_wib()}"
                bot.reply_to(message, txt, parse_mode="Markdown")
                return
        bot.reply_to(message, f"❌ {coin} tidak ada")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['entry'])
def entry(message):
    try:
        coin = get_coin(message)
        msg = bot.reply_to(message, f"🎯 Kalkulasi entry {coin}...")
        
        # 1. AMBIL DATA DASAR
        mids = info.all_mids()
        price_key = None
        for key in [coin, f"k{coin}", f"@{coin}"]:
            if key in mids:
                price_key = key
                break
        if not price_key:
            return bot.edit_message_text(f"❌ Coin {coin} ga ada di HL bro.", message.chat.id, msg.message_id)
        current_price = float(mids[price_key])
        
        meta, ctxs = info.meta_and_asset_ctxs()
        funding = 0
        oi_usd = 0
        for asset_meta, asset_ctx in zip(meta['universe'], ctxs):
            if asset_meta['name'] == coin:
                funding = float(asset_ctx.get('funding', 0)) * 100
                oi_usd = float(asset_ctx.get('oi', 0)) * current_price
                break
        
        # 2. HITUNG LIQMAP
        levels = []
        leverages = [20, 10, 5]
        weights = [0.5, 0.3, 0.2]
        for lev, weight in zip(leverages, weights):
            long_liq_price = current_price * (1 - 0.99/lev)
            short_liq_price = current_price * (1 + 0.99/lev)
            long_liq_size = oi_usd * weight * 0.5
            short_liq_size = oi_usd * weight * 0.5
            levels.append({"price": long_liq_price, "size": long_liq_size, "type": "Long"})
            levels.append({"price": short_liq_price, "size": short_liq_size, "type": "Short"})
        
        above = sorted([l for l in levels if l['price'] > current_price], key=lambda x: x['price'])
        below = sorted([l for l in levels if l['price'] < current_price], key=lambda x: x['price'], reverse=True)
        short_liq = above[0] if above else {"price": current_price * 1.05, "size": 0}
        long_liq = below[0] if below else {"price": current_price * 0.95, "size": 0}
        
        # 3. HITUNG WHALEWALL
        l2 = info.l2_snapshot(coin)
        bids, asks = l2['levels'][0], l2['levels'][1]
        
        bid_wall = 0
        bid_wall_price = 0
        for bid in bids[:15]:
            usd = float(bid['px']) * float(bid['sz'])
            if usd > 500000:
                bid_wall = usd
                bid_wall_price = float(bid['px'])
                break
        
        ask_wall = 0
        ask_wall_price = 0
        for ask in asks[:15]:
            usd = float(ask['px']) * float(ask['sz'])
            if usd > 500000:
                ask_wall = usd
                ask_wall_price = float(ask['px'])
                break
        
        # 4. SCORING BUAT NENTUIN BIAS
        short_score = 0
        long_score = 0
        
        if funding > 0.05: short_score += 40
        if funding < -0.05: long_score += 40
        if short_liq['size'] > 300_000_000: short_score += 30
        if long_liq['size'] > 300_000_000: long_score += 30
        if ask_wall < 1_000_000 and ask_wall > 0: short_score += 30
        if bid_wall < 1_000_000 and bid_wall > 0: long_score += 30
        
        # 5. OUTPUT
        txt = f"🎯 *ENTRY SIGNAL {coin}*\n"
        txt += f"━━━━━━━━━━━━\n"
        txt += f"Harga: `${current_price:,.2f}` | Funding: `{funding:.4f}%`\n"
        txt += f"━━━━━━━━━━━━\n"
        
        if short_score >= 70:
            entry_price = current_price
            sl_price = max(long_liq['price'], bid_wall_price) * 0.998 # 0.2% buffer di bawah
            tp1_price = short_liq['price'] * 0.999 # dikit di bawah liq biar kefill
            
            rr = (tp1_price - entry_price) / (entry_price - sl_price) if entry_price > sl_price else 0
            
            txt += f"🚨 *BIAS: SHORT SQUEEZE {short_score}%*\n\n"
            txt += f"🔴 *ENTRY:* `${entry_price:,.2f}` Market\n"
            txt += f"🛑 *SL:* `${sl_price:,.2f}` / Di bawah Liq+Wall\n"
            txt += f"🎯 *TP1:* `${tp1_price:,.2f}` / 50% / RR 1:{rr:.1f}\n"
            txt += f"🎯 *TP2:* `${ask_wall_price:,.2f}` / 30% / Tembok Ask\n\n"
            txt += f"*Risk:* `{((entry_price - sl_price)/entry_price)*100:.2f}%`\n"
            
            if rr < 1.5:
                txt += f"⚠️ *RR < 1:1.5 SKIP DULU*\n"
            else:
                txt += f"✅ *SETUP VALID. SIKAT*\n"
                
        elif long_score >= 70:
            entry_price = current_price
            sl_price = min(short_liq['price'], ask_wall_price) * 1.002 # 0.2% buffer di atas
            tp1_price = long_liq['price'] * 1.001
            
            rr = (entry_price - sl_price) / (tp1_price - entry_price) if tp1_price > entry_price else 0
            
            txt += f"🚨 *BIAS: LONG SQUEEZE {long_score}%*\n\n"
            txt += f"🟢 *ENTRY:* `${entry_price:,.2f}` Market\n"
            txt += f"🛑 *SL:* `${sl_price:,.2f}` / Di atas Liq+Wall\n"
            txt += f"🎯 *TP1:* `${tp1_price:,.2f}` / 50% / RR 1:{rr:.1f}\n"
            txt += f"🎯 *TP2:* `${bid_wall_price:,.2f}` / 30% / Tembok Bid\n\n"
            txt += f"*Risk:* `{((sl_price - entry_price)/entry_price)*100:.2f}%`\n"
            
            if rr < 1.5:
                txt += f"⚠️ *RR < 1:1.5 SKIP DULU*\n"
            else:
                txt += f"✅ *SETUP VALID. SIKAT*\n"
        else:
            txt += f"😴 *NO TRADE ZONE*\n\n"
            txt += f"Short Score: `{short_score}%` | Long Score: `{long_score}%`\n"
            txt += f"Funding netral, liq imbang, tembok tebel.\n"
            txt += f"Tunggu `/squeeze` >70% baru masuk.\n"
        
        txt += f"━━━━━━━━━━━━\n⏰ {get_wib()}"
        
        bot.edit_message_text(txt, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error entry: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['scan'])
def scan(message):
    try:
        msg = bot.reply_to(message, "🔍 Scanning 10 coin top HL...")
        
        # List coin top volume HL - bisa lu ganti
        coins = ["BTC", "ETH", "SOL", "HYPE", "kPEPE", "WIF", "ARB", "OP", "TIA", "SUI"]
        results = []
        
        meta, ctxs = info.meta_and_asset_ctxs()
        mids = info.all_mids()
        
        for coin in coins:
            try:
                # 1. Ambil harga
                price_key = None
                for key in [coin, f"k{coin}", f"@{coin}"]:
                    if key in mids:
                        price_key = key
                        break
                if not price_key: continue
                current_price = float(mids[price_key])
                
                # 2. Funding + OI
                funding = 0
                oi_usd = 0
                for asset_meta, asset_ctx in zip(meta['universe'], ctxs):
                    if asset_meta['name'] == coin:
                        funding = float(asset_ctx.get('funding', 0)) * 100
                        oi_usd = float(asset_ctx.get('oi', 0)) * current_price
                        break
                
                # 3. Liq terdekat
                short_liq_size = oi_usd * 0.5 * 0.5 # simulasi lev 20
                
                # 4. Whalewall tipis ga
                l2 = info.l2_snapshot(coin)
                asks = l2['levels'][1]
                ask_wall = 0
                for ask in asks[:10]:
                    if float(ask['px']) * float(ask['sz']) > 500000:
                        ask_wall = float(ask['px']) * float(ask['sz'])
                        break
                
                # 5. Scoring
                short_score = 0
                long_score = 0
                if funding > 0.05: short_score += 40
                if funding < -0.05: long_score += 40
                if short_liq_size > 300_000_000: short_score += 30
                if ask_wall < 1_000_000 and ask_wall > 0: short_score += 30
                
                score = max(short_score, long_score)
                bias = "SHORT" if short_score > long_score else "LONG"
                
                if score >= 70:
                    results.append(f"🚨 `{coin}` {bias} SQUEEZE `{score}%`")
                    
            except: continue
        
        # OUTPUT
        txt = f"🔍 *SCAN 10 COIN TOP HL*\n"
        txt += f"━━━━━━━━━━━━\n"
        
        if results:
            txt += "\n".join(results)
            txt += f"\n\n✅ *ADA SETUP! Cek `/entry COIN` buat detail*"
        else:
            txt += f"😴 *NO TRADE ZONE SEMUA*\n"
            txt += f"Market choppy. Sabar/turu dulu."
        
        txt += f"\n━━━━━━━━━━━━\n⏰ {get_wib()}"
        
        bot.edit_message_text(txt, message.chat.id, msg.message_id, parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Error scan: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=['gainers'])
def gainers(message):
    try:
        bot.reply_to(message, "🚀 Scanning gainers...")
        data = info.meta_and_asset_ctxs()
        top = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
            if vol < 5: continue
            mark = float(ctx.get("markPx", 0))
            prev = float(ctx.get("prevDayPx", mark))
            change = ((mark - prev) / prev * 100) if prev > 0 else 0
            top.append((asset["name"], vol, change, mark))
        top = sorted(top, key=lambda x: x[2], reverse=True)[:10]
        txt = "🚀 *TOP GAINERS 24H*\n━━━━━━━━━━━━━━━━━━\n"
        for i, (name, vol, change, price) in enumerate(top, 1):
            txt += f"{i}. *{name}* `{change:+.1f}%`\n `${price:,.2f}` | Vol `${vol:.0f}M`\n"
        txt += f"\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['losers'])
def losers(message):
    try:
        bot.reply_to(message, "📉 Scanning losers...")
        data = info.meta_and_asset_ctxs()
        top = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
            if vol < 5: continue
            mark = float(ctx.get("markPx", 0))
            prev = float(ctx.get("prevDayPx", mark))
            change = ((mark - prev) / prev * 100) if prev > 0 else 0
            top.append((asset["name"], vol, change, mark))
        top = sorted(top, key=lambda x: x[2])[:10]
        txt = "📉 *TOP LOSERS 24H*\n━━━━━━━━━━━━━━━━━━\n"
        for i, (name, vol, change, price) in enumerate(top, 1):
            txt += f"{i}. *{name}* `{change:+.1f}%`\n `${price:,.2f}` | Vol `${vol:.0f}M`\n"
        txt += f"\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['nuke'])
def nuke(message):
    try:
        bot.reply_to(message, "💣 Scanning nuke candidates...")
        data = info.meta_and_asset_ctxs()
        candidates = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                oi = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0)) / 1e6
                funding = float(ctx.get("funding", 0)) * 100
                abs_f = abs(funding)
                vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
                mark = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", mark))
                change = ((mark - prev) / prev * 100) if prev > 0 else 0
                score = (oi * abs_f * 10) + (vol * 0.1) + (abs(change) * 2)
                if oi > 30 and abs_f > 0.03:
                    direction = "🔴 LONG SQUEEZE" if funding > 0 else "🟢 SHORT SQUEEZE"
                    candidates.append((asset["name"], oi, funding, vol, change, score, direction))
            except: continue
        candidates = sorted(candidates, key=lambda x: x[5], reverse=True)[:5]
        txt = "💣 *NUKE RADAR*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if not candidates:
            txt += "✅ Aman. Tidak ada coin ekstrem."
        else:
            for i, (name, oi, fund, vol, change, score, direction) in enumerate(candidates, 1):
                txt += f"{'🔥' if i==1 else '⚠️'} *#{i} {name}*\n"
                txt += f" {direction}\n OI `${oi:.0f}M` | Fund `{fund:.4f}%`\n Vol `${vol:.0f}M` | Δ `{change:+.1f}%`\n Skor: `{score:.0f}`\n\n"
        txt += f"⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
@bot.message_handler(commands=['heatmap'])
def heatmap(message):
    try:
        bot.reply_to(message, "🌡️ Generating heatmap...")
        data = info.meta_and_asset_ctxs()
        sector_data = {}
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
                mark = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", mark))
                change = ((mark - prev) / prev * 100) if prev > 0 else 0
                fund = float(ctx.get("funding", 0)) * 100
                sector = get_narrative(name)
                if sector not in sector_data:
                    sector_data[sector] = {"vol": 0, "changes": [], "fundings": []}
                sector_data[sector]["vol"] += vol
                sector_data[sector]["changes"].append(change)
                sector_data[sector]["fundings"].append(fund)
            except: continue
        result = "🌡️ *MARKET HEATMAP*\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        sorted_sectors = sorted(sector_data.items(), key=lambda x: x[1]["vol"], reverse=True)
        for sector, d in sorted_sectors:
            avg_change = sum(d["changes"]) / len(d["changes"]) if d["changes"] else 0
            avg_fund = sum(d["fundings"]) / len(d["fundings"]) if d["fundings"] else 0
            heat = "🔥" if avg_change > 3 else ("🟢" if avg_change > 0 else ("🔴" if avg_change < -3 else "🟡"))
            result += f"{heat} *{sector}*\n Vol: `${d['vol']:.0f}M` | Δ: `{avg_change:+.2f}%` | Fund: `{avg_fund:.4f}%`\n\n"
        result += f"⏰ {get_wib()}"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['narrative'])
def narrative(message):
    try:
        bot.reply_to(message, "🗺️ Scanning narrative...")
        data = info.meta_and_asset_ctxs()
        sector_stats = {}
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
                mark = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", mark))
                change = ((mark - prev) / prev * 100) if prev > 0 else 0
                oi = float(ctx.get("openInterest", 0)) * mark / 1e6
                fund = abs(float(ctx.get("funding", 0)) * 100)
                sector = get_narrative(name)
                if sector not in sector_stats:
                    sector_stats[sector] = {"vol": 0, "oi": 0, "changes": [], "coins": [], "heat": 0}
                sector_stats[sector]["vol"] += vol
                sector_stats[sector]["oi"] += oi
                sector_stats[sector]["changes"].append(change)
                sector_stats[sector]["coins"].append((name, vol, change))
                sector_stats[sector]["heat"] += vol * (abs(change) + fund * 10)
            except: continue
        sorted_s = sorted(sector_stats.items(), key=lambda x: x[1]["heat"], reverse=True)
        result = f"🗺️ *NARRATIVE DOMINAN*\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        for i, (sector, d) in enumerate(sorted_s[:5]):
            avg_change = sum(d["changes"]) / len(d["changes"]) if d["changes"] else 0
            top_coin = sorted(d["coins"], key=lambda x: x[1], reverse=True)[0][0]
            result += f"{medals[i]} *{sector}*\n Vol: `${d['vol']:.0f}M` | Δ: `{avg_change:+.2f}%`\n Leader: `{top_coin}`\n\n"
        result += f"⏰ {get_wib()}"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['whale'])
def whale(message):
    try:
        coin = get_coin(message)
        l2 = info.l2_snapshot(coin)
        bids_raw = l2["levels"][0][:10]
        asks_raw = l2["levels"][1][:10]
        bids = sum([float(x["sz"]) * float(x["px"]) for x in bids_raw]) / 1e6
        asks = sum([float(x["sz"]) * float(x["px"]) for x in asks_raw]) / 1e6
        ratio = bids / asks if asks > 0 else 0
        big_bids = len([x for x in bids_raw if float(x["sz"]) * float(x["px"]) > 500_000])
        big_asks = len([x for x in asks_raw if float(x["sz"]) * float(x["px"]) > 500_000])
        if bids > asks * 2: verdict = "💚 BUY WALL DOMINAN"
        elif asks > bids * 2: verdict = "❤️ SELL WALL DOMINAN"
        else: verdict = "⚖️ BALANCED"
        txt = f"🐳 *Whale {coin}*\n━━━━━━━━━━━━━━━━━━\n"
        txt += f"🟢 Buy: `${bids:.2f}M`\n🔴 Sell: `${asks:.2f}M`\nRatio: `{ratio:.2f}x`\n"
        txt += f"Big Orders: {big_bids} bids / {big_asks} asks\n\n{verdict}\n\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['whalescan'])
def whalescan(message):
    try:
        bot.reply_to(message, "🕵️ Scanning whale activity...")
        data = info.meta_and_asset_ctxs()
        results = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                oi = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0)) / 1e6
                vol = float(ctx.get("dayNtlVlm", 0)) / 1e6
                fund = float(ctx.get("funding", 0)) * 100
                mark = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", mark))
                change = ((mark - prev) / prev * 100) if prev > 0 else 0
                score = 0
                if oi > 20: score += 2
                if vol > 50: score += 2
                if 0 < fund < 0.05: score += 2
                if change > 2: score += 2
                if change > 5: score += 1
                if oi > 100: score += 1
                if score >= 6:
                    results.append((name, oi, vol, fund, change, score, get_narrative(name)))
            except: continue
        results = sorted(results, key=lambda x: x[5], reverse=True)[:7]
        txt = "🕵️ *WHALE ACCUMULATION*\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        if not results:
            txt += "😴 Tidak ada sinyal akumulasi kuat."
        else:
            for i, (name, oi, vol, fund, change, score, sector) in enumerate(results, 1):
                bar = "🟩" * min(score, 9)
                txt += f"{'🔥' if i==1 else '⚡'} *#{i} {name}* `[{sector}]`\n"
                txt += f" OI `${oi:.0f}M` | Vol `${vol:.0f}M`\n Fund `{fund:.4f}%` | Δ `{change:+.1f}%`\n Score: {bar} `{score}/9`\n\n"
        txt += f"⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
@bot.message_handler(commands=['liquidations', 'liq'])
def liquidations(message):
    try:
        coin = get_coin(message) if len(message.text.split()) > 1 else None
        data = info.meta_and_asset_ctxs()
        total_long_liq = total_short_liq = 0
        coin_results = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                if coin and name!= coin: continue
                oi = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0)) / 1e6
                mark = float(ctx.get("markPx", 0))
                prev = float(ctx.get("prevDayPx", mark))
                change = ((mark - prev) / prev * 100) if prev > 0 else 0
                est_liq = oi * abs(change) / 100
                if change < -2:
                    total_long_liq += est_liq
                    direction = "LONG"
                elif change > 2:
                    total_short_liq += est_liq
                    direction = "SHORT"
                else:
                    direction = "MINIMAL"
                if est_liq > 1:
                    coin_results.append((name, est_liq, direction, change))
            except: continue
        coin_results = sorted(coin_results, key=lambda x: x[1], reverse=True)[:5]
        txt = f"🔴 *LIQUIDATION RADAR*{f' — {coin}' if coin else ''}\n━━━━━━━━━━━━━━━━━━━━\n\n"
        txt += f"💥 Long Liq: `${total_long_liq:.1f}M`\n💥 Short Liq: `${total_short_liq:.1f}M`\n\n"
        if coin_results:
            txt += "*Top Liq:*\n"
            for name, liq, direction, change in coin_results:
                icon = "🔴" if direction == "LONG" else "🟢"
                txt += f"{icon} *{name}* `${liq:.1f}M` {direction} `{change:+.1f}%`\n"
        txt += f"\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['positions'])
def positions(message):
    try:
        parts = message.text.split()
        if len(parts) < 2: return bot.reply_to(message, "❌ Format: /positions 0xWallet")
        wallet = parts[1]
        state = info.user_state(wallet)
        pos_list = state.get("assetPositions", [])
        if not pos_list: return bot.reply_to(message, "📋 Tidak ada posisi open.")
        txt = f"📋 *Positions*\n`{wallet[:6]}...{wallet[-4:]}`\n━━━━━━━━━━━━━━━━━━\n\n"
        for p in pos_list[:8]:
            pos = p.get("position", {})
            coin = pos.get("coin", "?")
            sz = float(pos.get("szi", 0))
            entry = float(pos.get("entryPx", 0))
            upnl = float(pos.get("unrealizedPnl", 0))
            lev = pos.get("leverage", {}).get("value", "?")
            side = "🟢 LONG" if sz > 0 else "🔴 SHORT"
            pnl_icon = "✅" if upnl >= 0 else "❌"
            txt += f"{side} *{coin}* `{lev}x`\n Size: `{abs(sz):.4f}` | Entry: `${entry:,.2f}`\n uPnL: {pnl_icon} `${upnl:,.2f}`\n\n"
        txt += f"⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['pnl'])
def pnl(message):
    try:
        parts = message.text.split()
        if len(parts) < 2: return bot.reply_to(message, "❌ Format: /pnl 0xWallet")
        wallet = parts[1]
        state = info.user_state(wallet)
        margin = state.get("marginSummary", {})
        total_val = float(margin.get("accountValue", 0))
        total_margin = float(margin.get("totalMarginUsed", 0))
        total_upnl = float(margin.get("totalUnrealizedPnl", 0))
        pnl_icon = "✅" if total_upnl >= 0 else "❌"
        txt = f"💹 *PnL Summary*\n`{wallet[:6]}...{wallet[-4:]}`\n━━━━━━━━━━━━━━━━━━\n\n"
        txt += f"💰 Account: `${total_val:,.2f}`\n📊 Margin: `${total_margin:,.2f}`\n{pnl_icon} uPnL: `${total_upnl:,.2f}`\n\n⏰ {get_wib()}"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
from datetime import datetime, timezone, timedelta

def get_wib():
    wib = timezone(timedelta(hours=7))
    return datetime.now(wib).strftime('%d/%m %H:%M WIB')

@bot.message_handler(commands=['entrywhale', 'whaleentry'])
def entrywhale(message):
    try:
        msg = bot.reply_to(message, "🐋 *Scanning whale entry live...*", parse_mode='Markdown')

        meta_ctxs = info.meta_and_asset_ctxs()
        if not meta_ctxs or len(meta_ctxs) < 2:
            bot.edit_message_text("❌ Gagal ambil data market", msg.chat.id, msg.message_id)
            return

        coins_meta = meta_ctxs[0]['universe']
        coins_data = meta_ctxs[1]

        whale_entries = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        for i, coin_data in enumerate(coins_meta):
            coin = coin_data['name']
            ctx = coins_data[i]

            oi = float(ctx.get('openInterest', 0))
            day_vol = float(ctx.get('dayNtlVlm', 0))

            if oi < 500000 or day_vol < 2000000:
                continue

            try:
                trades = info.recent_trades(coin)
                if not trades: continue

                for trade in trades[:3]:
                    size_usd = float(trade['px']) * float(trade['sz'])
                    trade_time = int(trade['time'])

                    if size_usd > 30000 and (now_ms - trade_time) < 180000:
                        side = "LONG" if trade['side'] == 'B' else "SHORT"
                        emoji = "🟢" if trade['side'] == 'B' else "🔴"

                        whale_entries.append({
                            'coin': coin,
                            'side': side,
                            'emoji': emoji,
                            'size': size_usd,
                            'price': float(trade['px']),
                            'time': int((now_ms - trade_time) / 1000),
                            'oi': oi
                        })
                        break
            except:
                continue

        if not whale_entries:
            text = f"😴 *WHALE SNIPER*\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
            text += "*Ga ada whale entry >$30k*\n"
            text += "*dalam 3 menit terakhir*\n\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n"
            text += f"⏰ *{get_wib()}*"
            bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')
            return

        whale_entries.sort(key=lambda x: x['size'], reverse=True)

        # UI BARU KAYAK YANG LU MAU
        text = f"🐋 *WHALE SNIPER*\n"
        text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📡 *Status:* DETECTED\n"
        text += f"🎯 *Filter:* >$30k | 3min\n\n"

        for i, w in enumerate(whale_entries[:5], 1):
            text += f"{w['emoji']} *{w['side']} {w['coin']}*\n"
            text += f"💰 *Size:* ${w['size']:,.0f}\n"
            text += f"📍 *Price:* ${w['price']:.4f}\n"
            text += f"⏱️ *Age:* {w['time']}s ago\n"
            text += f"📊 *OI:* ${w['oi']:,.0f}\n"
            text += f"🔗 [Track Trade](https://app.hyperliquid.xyz/trade?market={w['coin']})\n"
            text += "─────────────────────\n"

        text += "\n━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown', disable_web_page_preview=True)

    except Exception as e:
        bot.edit_message_text(f"❌ *Error*\n`{str(e)}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['warroom', 'wr'])
def warroom_pro(message):
    try:
        args = message.text.split()
        coin = args[1].upper() if len(args) > 1 else 'BTC'
        msg = bot.reply_to(message, f"🧠 *WARROOM ANALYSIS {coin}...*", parse_mode='Markdown')

        # 1. KUMPULIN DATA DARI 4 TOOLS BARU
        # Delta
        l2 = info.l2_snapshot(coin)
        delta_bias = "N/A"
        if l2 and 'levels' in l2 and l2['levels'][0] and l2['levels'][1]:
            bids = l2['levels'][0]
            asks = l2['levels'][1]
            mid = (float(bids[0]['px']) + float(asks[0]['px'])) / 2
            bid_vol = sum(float(b['sz'])*float(b['px']) for b in bids if float(b['px']) >= mid*0.98)
            ask_vol = sum(float(a['sz'])*float(a['px']) for a in asks if float(a['px']) <= mid*1.02)
            delta = (bid_vol - ask_vol) / (bid_vol + ask_vol) * 100 if (bid_vol + ask_vol) > 0 else 0
            delta_bias = f"{'🟢 STRONG BID' if delta>30 else '🔴 STRONG ASK' if delta<-30 else '⚪️ BALANCE'} [{delta:+.0f}%]"

        # Trap
        end_time = int(datetime.now().timestamp() * 1000)
        candles = info.candles_snapshot(coin, '1m', end_time - 30*60*1000, end_time)
        trap_status = "NO TRAP"
        for i in range(2, len(candles)):
            c = candles[i]
            o,h,l,c_price,v = float(c['o']),float(c['h']),float(c['l']),float(c['c']),float(c['v'])
            body = abs(c_price - o)
            if body == 0: continue
            if (min(o,c_price)-l) > body*2 and v*c_price>50000 and c_price>o:
                trap_status = f"LONG TRAP {len(candles)-i}m ago"
                break
            elif (h-max(o,c_price)) > body*2 and v*c_price>50000 and c_price<o:
                trap_status = f"SHORT TRAP {len(candles)-i}m ago"
                break

        # Cluster
        meta_ctxs = info.meta_and_asset_ctxs()
        idx = next((i for i, x in enumerate(meta_ctxs[0]['universe']) if x['name'] == coin), None)
        price = float(meta_ctxs[1][idx]['markPx']) if idx is not None else 0
        oi = float(meta_ctxs[1][idx]['openInterest']) if idx is not None else 0
        cluster = f"Short Liq ${price*1.02:.0f} | Long Liq ${price*0.98:.0f}"

        # Correlation vs BTC
        corr_status = "N/A"
        if coin!= 'BTC':
            btc_candles = info.candles_snapshot('BTC', '5m', end_time - 100*5*60*1000, end_time)
            coin_candles = info.candles_snapshot(coin, '5m', end_time - 100*5*60*1000, end_time)
            if len(btc_candles) > 50 and len(coin_candles) > 50:
                btc_ret = [(float(btc_candles[i]['c'])-float(btc_candles[i-1]['c']))/float(btc_candles[i-1]['c']) for i in range(1,100)]
                coin_ret = [(float(coin_candles[i]['c'])-float(coin_candles[i-1]['c']))/float(coin_candles[i-1]['c']) for i in range(1,100)]
                n = len(btc_ret)
                corr = (n*sum(btc_ret[i]*coin_ret[i] for i in range(n)) - sum(btc_ret)*sum(coin_ret)) / (((n*sum(x*x for x in btc_ret)-sum(btc_ret)**2)*(n*sum(y*y for y in coin_ret)-sum(coin_ret)**2))**0.5)
                corr_status = f"{'🔴 NEMPEL' if corr>0.8 else '🟢 DECOUPLE' if corr<0.5 else '🟡 IKUT'} [{corr:.2f}]"

        # Funding
        funding = float(meta_ctxs[1][idx]['funding']) * 100 if idx is not None else 0

        # 2. AI VERDICT
        score = 0
        if 'BID' in delta_bias: score += 2
        if 'LONG TRAP' in trap_status: score += 2
        if 'SHORT TRAP' in trap_status: score -= 2
        if 'DECOUPLE' in corr_status: score += 1
        if funding < 0: score += 1

        if score >= 3: verdict = "✅ BULLISH - Setup Long"; action = f"Entry ${price*0.995:.2f} | SL ${price*0.98:.2f} | TP ${price*1.02:.2f}"
        elif score <= -2: verdict = "❌ BEARISH - Setup Short"; action = f"Entry ${price*1.005:.2f} | SL ${price*1.02:.2f} | TP ${price*0.98:.2f}"
        else: verdict = "⚪️ NEUTRAL - Wait"; action = "Skip dulu. Tunggu setup jelas"

        # 3. OUTPUT
        text = f"🧠 *HL WARROOM {coin}*\n"
        text += "━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📌 *MARKET CONTEXT*\n"
        text += f"💰 Harga: ${price:.4f}\n"
        text += f"🔗 Correlation: {corr_status}\n\n"
        text += f"⚔️ *POSITIONING*\n"
        text += f"⚡ Orderbook: {delta_bias}\n"
        text += f"💸 Funding: {funding:+.3f}% {'Longs bayar' if funding>0 else 'Shorts bayar'}\n"
        text += f"📊 OI: ${oi:,.0f}M\n\n"
        text += f"🎭 *MARKET PSYCHOLOGY*\n"
        text += f"🪤 Trap: {trap_status}\n\n"
        text += f"🧲 *LIQUIDITY*\n"
        text += f"🎯 Cluster: {cluster}\n\n"
        text += f"📊 *AI CONCLUSION*\n"
        text += f"{verdict}\n"
        text += f"🎯 *Action:* {action}\n\n"
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += f"⏰ *{get_wib()}*"

        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode='Markdown')

    except Exception as e:
        bot.edit_message_text(f"❌ *Warroom Error*\n`{str(e)}`", msg.chat.id, msg.message_id)

@bot.message_handler(commands=['context'])
def context(message):

    try:

        args = message.text.split()

        if len(args) < 2:
            coin = "BTC"
        else:
            coin = args[1].upper()

        msg = bot.reply_to(
            message,
            f"🧠 Reading market memory for {coin}..."
        )

        data = get_market_data(coin)

        if not data:

            return bot.edit_message_text(
                f"❌ Coin {coin} tidak ditemukan.",
                message.chat.id,
                msg.message_id
            )

        current_price = data["price"]
        funding = data["funding"]
        oi = data["oi"]

        # update memory
        update_market_memory(
            coin,
            current_price,
            funding,
            oi
        )

        # analyze memory
        analysis = analyze_market_memory(
            coin
        )

        txt = f"""
🧠 *MARKET CONTEXT*
━━━━━━━━━━━━━━━━━━

🪙 *ASSET*
`{coin}`

━━━━━━━━━━━━━━━━━━

📚 *MEMORY ANALYSIS*

{analysis}

━━━━━━━━━━━━━━━━━━

⏰ {get_wib()}
"""

        bot.edit_message_text(
            txt,
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        print("CONTEXT ERROR =", e)

        bot.edit_message_text(
            f"❌ CONTEXT ERROR\n`{str(e)[:300]}`",
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['psychology'])
def psychology(message):

    try:

        args = message.text.split()

        if len(args) < 2:
            coin = "BTC"
        else:
            coin = args[1].upper()

        msg = bot.reply_to(
            message,
            f"🧠 Reading psychology for {coin}..."
        )

        data = get_market_data(coin)

        if not data:

            return bot.edit_message_text(
                f"❌ Coin {coin} tidak ditemukan.",
                message.chat.id,
                msg.message_id
            )

        current_price = data["price"]
        funding = data["funding"]
        oi = data["oi"]
        prev_day = data["prev_day"]

        price_change = (
            (current_price - prev_day)
            / prev_day
        ) * 100

        oi_delta = calculate_oi_delta(
            coin,
            oi
        )

        state, analysis = analyze_psychology(
            price_change,
            funding,
            oi_delta
        )

        txt = f"""
🧠 *MARKET PSYCHOLOGY*
━━━━━━━━━━━━━━━━━━

🪙 *ASSET*
`{coin}`

🎭 *STATE*
`{state}`

━━━━━━━━━━━━━━━━━━

📚 *ANALYSIS*
"""

        for line in analysis:

            txt += f"\n• {line}"

        txt += f"""

━━━━━━━━━━━━━━━━━━

⏰ {get_wib()}
"""

        bot.edit_message_text(
            txt,
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        print("PSYCHOLOGY ERROR =", e)

        bot.edit_message_text(
            f"❌ PSYCHOLOGY ERROR\n`{str(e)[:300]}`",
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['oracle'])
def oracle(message):

    try:

        args = message.text.split()

        if len(args) < 2:
            coin = "BTC"
        else:
            coin = args[1].upper()

        msg = bot.reply_to(
            message,
            f"🔮 Reading future structure for {coin}..."
        )

        data = get_market_data(coin)

        if not data:

            return bot.edit_message_text(
                f"❌ Coin {coin} tidak ditemukan.",
                message.chat.id,
                msg.message_id
            )

        current_price = data["price"]
        funding = data["funding"]
        oi = data["oi"]
        prev_day = data["prev_day"]

        price_change = (
            (current_price - prev_day)
            / prev_day
        ) * 100

        oi_delta = calculate_oi_delta(
            coin,
            oi
        )

        phase, analysis = analyze_oracle(
            price_change,
            funding,
            oi_delta
        )

        txt = f"""
🔮 *MARKET ORACLE*
━━━━━━━━━━━━━━━━━━

🪙 *ASSET*
`{coin}`

🧬 *CURRENT PHASE*
`{phase}`

━━━━━━━━━━━━━━━━━━

📖 *AI OUTLOOK*
"""

        for line in analysis:

            txt += f"\n• {line}"

        txt += f"""

━━━━━━━━━━━━━━━━━━

⏰ {get_wib()}
"""

        bot.edit_message_text(
            txt,
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        print("ORACLE ERROR =", e)

        bot.edit_message_text(
            f"❌ ORACLE ERROR\n`{str(e)[:300]}`",
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['probability'])
def probability(message):

    try:

        args = message.text.split()

        if len(args) < 2:
            coin = "BTC"
        else:
            coin = args[1].upper()

        msg = bot.reply_to(
            message,
            f"📊 Calculating probability for {coin}..."
        )

        data = get_market_data(coin)

        if not data:

            return bot.edit_message_text(
                f"❌ Coin {coin} tidak ditemukan.",
                message.chat.id,
                msg.message_id
            )

        current_price = data["price"]
        funding = data["funding"]
        oi = data["oi"]
        prev_day = data["prev_day"]

        price_change = (
            (current_price - prev_day)
            / prev_day
        ) * 100

        oi_delta = calculate_oi_delta(
            coin,
            oi
        )

        prob = analyze_probability(
            price_change,
            funding,
            oi_delta
        )

        interpretation = (
            "Momentum market masih cukup sehat."
        )

        if prob["fakeout"] == "HIGH":

            interpretation = (
                "Current move terlihat agresif "
                "tetapi belum didukung positioning kuat."
            )

        elif prob["long_squeeze"] > 70:

            interpretation = (
                "Risk long squeeze mulai meningkat."
            )

        elif prob["short_squeeze"] > 70:

            interpretation = (
                "Short squeeze masih menjadi bahan bakar utama."
            )

        txt = f"""
📊 *MARKET PROBABILITY*
━━━━━━━━━━━━━━━━━━

🪙 *ASSET*
`{coin}`

📈 *Bullish Continuation*
`{prob['bullish']}%`

🩸 *Short Squeeze*
`{prob['short_squeeze']}%`

💀 *Long Squeeze*
`{prob['long_squeeze']}%`

⚠️ *Fakeout Risk*
`{prob['fakeout']}`

🔥 *Volatility Expansion*
`{prob['volatility']}%`

━━━━━━━━━━━━━━━━━━

🧠 *AI INTERPRETATION*

{interpretation}

━━━━━━━━━━━━━━━━━━

⏰ {get_wib()}
"""

        bot.edit_message_text(
            txt,
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        print("PROBABILITY ERROR =", e)

        bot.edit_message_text(
            f"❌ PROBABILITY ERROR\n`{str(e)[:300]}`",
            message.chat.id,
            msg.message_id,
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['report'])
def report(message):
    bot.reply_to(message, "📡 Generating report...")
    try:
        bot.reply_to(message, build_market_summary(), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

def schedule_loop():
    while schedule_state["active"]:
        try:
            bot.send_message(schedule_state["chat_id"], build_market_summary(), parse_mode="Markdown")
        except: pass
        for _ in range(schedule_state["interval_min"] * 60):
            if not schedule_state["active"]: break
            time.sleep(1)

@bot.message_handler(commands=['schedule'])
def schedule(message):
    try:
        parts = message.text.split()
        interval = int(parts[1]) if len(parts) > 1 else 60
        interval = max(10, min(interval, 360))
        schedule_state["active"] = True
        schedule_state["chat_id"] = message.chat.id
        schedule_state["interval_min"] = interval
        if not (schedule_state["thread"] and schedule_state["thread"].is_alive()):
            t = threading.Thread(target=schedule_loop, daemon=True)
            t.start()
            schedule_state["thread"] = t
        bot.reply_to(message, f"✅ *Auto Report AKTIF*\n⏱️ Interval: setiap `{interval}` menit\n📢 Report dikirim ke chat ini.\n\n⏰ {get_wib()}", parse_mode="Markdown")
    except:
        bot.reply_to(message, "❌ Format: /schedule 60")

@bot.message_handler(commands=['stopschedule'])
def stopschedule(message):
    schedule_state["active"] = False
    bot.reply_to(message, f"⏹️ *Auto Report dimatikan.*\n\n⏰ {get_wib()}", parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def status(message):
    try:
        info.meta_and_asset_ctxs()
        api_status = "✅ Connected"
    except:
        api_status = "❌ Error"
    
    schedule_status = "✅ AKTIF" if schedule_state["active"] else "⏹️ OFF"
    txt = f"🤖 *Bot Status*\n━━━━━━━━━━━━\n"
    txt += f"API HL: {api_status}\n"
    txt += f"Schedule: {schedule_status}"
    if schedule_state["active"]:
        txt += f" `{schedule_state['interval_min']}m`"
    txt += f"\nTime: {get_wib()}\nMode: Production"
    bot.reply_to(message, txt, parse_mode="Markdown")

print("🤖 HL Intel Bot v3.1 MONSTER — aktif...")
bot.infinity_polling()

