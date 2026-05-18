import telebot
import threading
import time
import requests
from datetime import datetime, timezone
from hyperliquid.info import Info
from hyperliquid.utils import constants

TOKEN = "8966251450:AAGISG43AP1pwgyLfyYjWqNnIbisum1Tig4"
bot = telebot.TeleBot(TOKEN)
info = Info(constants.MAINNET_API_URL)

NARRATIVES = {
    "L1":    ["BTC","ETH","SOL","AVAX","SUI","APT","SEI","INJ","TIA","NEAR"],
    "L2":    ["ARB","OP","MATIC","IMX","METIS","ZK","STRK","MANTA","BLAST"],
    "DeFi":  ["AAVE","UNI","CRV","MKR","SNX","DYDX","GMX","GNS","PENDLE"],
    "Meme":  ["DOGE","SHIB","PEPE","FLOKI","BONK","WIF","POPCAT","BOME"],
    "AI":    ["FET","AGIX","OCEAN","RENDER","WLD","TAO","ARKM","GRT"],
    "Gaming":["AXS","SAND","MANA","ENJ","GALA","BEAM","RON","MAGIC"],
    "RWA":   ["ONDO","MPL","CFG","TRU"],
    "Infra": ["LINK","DOT","ATOM","QNT","PYTH","JTO","EIGEN","ETHFI"],
}

def get_narrative(coin):
    for sector, coins in NARRATIVES.items():
        if coin in coins:
            return sector
    return "Other"

schedule_state = {
    "active": False,
    "chat_id": None,
    "interval_min": 60,
    "thread": None
}

@bot.message_handler(commands=['start', 'help'])
def start(message):
    text  = "🤖 *HL Intel Bot MONSTER EDITION*\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "📊 *MARKET*\n"
    text += "  💸 /funding BTC — Funding rate\n"
    text += "  💰 /price ETH — Harga live\n"
    text += "  📈 /gainers — Top 5 volume 24h\n"
    text += "  📉 /losers — Top 5 dump 24h\n"
    text += "  🌡️ /heatmap — Snapshot semua sektor\n"
    text += "  🗺️ /narrative — Sektor terpanas\n\n"
    text += "🔍 *ANALISIS*\n"
    text += "  📊 /oi BTC — Open Interest\n"
    text += "  💣 /nuke — Coin siap meledak\n"
    text += "  🔴 /liquidations BTC — Data likuidasi\n"
    text += "  ⚖️ /sentiment BTC — Sentimen ritel\n\n"
    text += "🐳 *WHALE*\n"
    text += "  🐳 /whale BTC — Orderbook whale\n"
    text += "  🕵️ /whalescan — Deteksi akumulasi whale\n\n"
    text += "⏰ *SCHEDULE*\n"
    text += "  ▶️ /schedule 60 — Auto report tiap N menit\n"
    text += "  ⏹️ /stopschedule — Stop auto report\n"
    text += "  📡 /report — Report manual\n\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += "🔧 _Bot by one_"
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['funding'])
def funding(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else "BTC"
        data = info.funding_history(coin, 1)
        if not data:
            bot.reply_to(message, f"❌ {coin} ga ada di HL")
            return
        rate = float(data[0]["fundingRate"]) * 100
        arah = "🟢 Long bayar Short" if rate > 0 else "🔴 Short bayar Long"
        if abs(rate) > 0.05:
            level = "🔥 EKSTREM — Squeeze alert!"
        elif abs(rate) > 0.01:
            level = "⚠️ TINGGI — Waspada"
        else:
            level = "✅ Normal"
        txt  = f"💸 *Funding Rate — {coin}*\n"
        txt += "━━━━━━━━━━━━━━━━\n"
        txt += f"Rate   : `{rate:.4f}%` /jam\n"
        txt += f"Arah   : {arah}\n"
        txt += f"Status : {level}\n\n"
        txt += "📌 _>0.01% rawan squeeze_"
        bot.reply_to(message, txt, parse_mode="Markdown")
    except:
        bot.reply_to(message, "❌ Format: /funding BTC")

@bot.message_handler(commands=['price'])
def price(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else "BTC"
        data = info.all_mids()
        if coin in data:
            harga = float(data[coin])
            txt  = f"💰 *Harga {coin}*\n"
            txt += "━━━━━━━━━━━━━━━━\n"
            txt += f"`${harga:,.4f}`"
            bot.reply_to(message, txt, parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ {coin} ga ada di HL")
    except:
        bot.reply_to(message, "❌ Format: /price BTC")

@bot.message_handler(commands=['oi'])
def oi(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else "BTC"

        data = info.meta_and_asset_ctxs()

        for asset, ctx in zip(data[0]["universe"], data[1]):

            if asset["name"] == coin:

                oi_usd = float(ctx["openInterest"]) * float(ctx["markPx"]) / 1e6

                if oi_usd > 1000:
                    w = "🔥 SANGAT TINGGI — Squeeze kapan aja"

                elif oi_usd > 500:
                    w = "⚠️ TINGGI — Hati2"

                else:
                    w = "✅ Normal"

                txt = f"📊 *Open Interest — {coin}*\n"
                txt += "────────────────\n"
                txt += f"OI     : ${oi_usd:.2f}M\n"
                txt += f"Status : {w}"

                bot.reply_to(message, txt, parse_mode="Markdown")
                return

        bot.reply_to(message, f"❌ {coin} ga ada")

    except:
        bot.reply_to(message, "❌ Format: /oi BTC")

@bot.message_handler(commands=['gainers'])
def gainers(message):
    try:
        data = info.meta_and_asset_ctxs()
        top = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                vol = float(ctx.get("dayNtlVlm") or 0)
                if vol <= 0: continue
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                top.append((asset["name"], vol/1e6, change))
            except: continue
        top = sorted(top, key=lambda x: x[1], reverse=True)[:5]
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        result  = "🏆 *TOP 5 VOLUME 24 JAM*\n"
        result += "━━━━━━━━━━━━━━━━━━\n\n"
        for i, (name, vol, change) in enumerate(top):
            arrow = "🟢" if change >= 0 else "🔴"
            sector = get_narrative(name)
            result += f"{medals[i]} *{name}* `[{sector}]`\n"
            result += f"   Vol    : `${vol:.1f}M`\n"
            result += f"   Change : {arrow} `{change:+.2f}%`\n\n"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['losers'])
def losers(message):
    try:
        data = info.meta_and_asset_ctxs()
        items = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                vol = float(ctx.get("dayNtlVlm") or 0)
                if vol < 1e6: continue
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                items.append((asset["name"], vol/1e6, change))
            except: continue
        items = sorted(items, key=lambda x: x[2])[:5]
        medals = ["💀","😭","📉","4️⃣","5️⃣"]
        result  = "📉 *TOP 5 DUMP 24 JAM*\n"
        result += "━━━━━━━━━━━━━━━━━━\n\n"
        for i, (name, vol, change) in enumerate(items):
            sector = get_narrative(name)
            result += f"{medals[i]} *{name}* `[{sector}]`\n"
            result += f"   Vol    : `${vol:.1f}M`\n"
            result += f"   Dump   : 🔴 `{change:+.2f}%`\n\n"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['heatmap'])
def heatmap(message):
    try:
        data = info.meta_and_asset_ctxs()
        sector_data = {}
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                vol = float(ctx.get("dayNtlVlm") or 0) / 1e6
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                sector = get_narrative(name)
                if sector not in sector_data:
                    sector_data[sector] = {"vol": 0, "changes": []}
                sector_data[sector]["vol"] += vol
                sector_data[sector]["changes"].append(change)
            except: continue
        result  = "🌡️ *MARKET HEATMAP*\n"
        result += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        sorted_sectors = sorted(sector_data.items(), key=lambda x: x[1]["vol"], reverse=True)
        for sector, d in sorted_sectors:
            avg = sum(d["changes"]) / len(d["changes"]) if d["changes"] else 0
            heat = "🔥" if avg > 3 else ("🟢" if avg > 0 else ("🔴" if avg < -3 else "🟡"))
            result += f"{heat} *{sector}* — `${d['vol']:.0f}M` | `{avg:+.2f}%`\n"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['narrative'])
def narrative(message):
    try:
        data = info.meta_and_asset_ctxs()
        sector_stats = {}
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                vol = float(ctx.get("dayNtlVlm") or 0) / 1e6
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                fund = abs(float(ctx.get("funding") or 0) * 100)
                sector = get_narrative(name)
                if sector not in sector_stats:
                    sector_stats[sector] = {"vol": 0, "changes": [], "coins": [], "heat": 0}
                sector_stats[sector]["vol"] += vol
                sector_stats[sector]["changes"].append(change)
                sector_stats[sector]["coins"].append((name, vol, change))
                sector_stats[sector]["heat"] += vol * (abs(change) + fund * 10)
            except: continue
        sorted_s = sorted(sector_stats.items(), key=lambda x: x[1]["heat"], reverse=True)
        hour = datetime.now(timezone.utc).hour
        if 7 <= hour < 12:
            sesi = "🌅 London Open"
        elif 12 <= hour < 17:
            sesi = "🌍 London/NY Overlap — VOLUME MAX"
        elif 17 <= hour < 22:
            sesi = "🗽 New York Session"
        else:
            sesi = "🌙 Asia Session"
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣"]
        result  = f"🗺️ *NARRATIVE DOMINAN*\n"
        result += f"⏰ Sesi : {sesi}\n"
        result += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        for i, (sector, d) in enumerate(sorted_s[:8]):
            avg = sum(d["changes"]) / len(d["changes"]) if d["changes"] else 0
            arrow = "🟢" if avg >= 0 else "🔴"
            top_coin = sorted(d["coins"], key=lambda x: x[1], reverse=True)[0][0]
            result += f"{medals[i]} *{sector}* {arrow} `{avg:+.2f}%`\n"
            result += f"   Vol    : `${d['vol']:.0f}M` | Leader: `{top_coin}`\n\n"
        result += "📌 _Rank by heat score_"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['LSratio', 'sentiment'])
def ls_ratio(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else "BTC"
        data = info.meta_and_asset_ctxs()
        for asset, ctx in zip(data[0]["universe"], data[1]):
            if asset["name"] == coin:
                funding = float(ctx["funding"]) * 100
                if funding > 0.05:
                    status = "😈 RITEL SERAKAH — Rawan Long Squeeze"
                    emoji = "🔴"
                elif funding < -0.05:
                    status = "😱 RITEL KETAKUTAN — Rawan Short Squeeze"
                    emoji = "🟢"
                elif funding > 0.01:
                    status = "⚠️ Agak Greedy"
                    emoji = "🟠"
                else:
                    status = "😐 NETRAL"
                    emoji = "🟡"
                txt  = f"🧠 *Sentiment — {coin}*\n"
                txt += "━━━━━━━━━━━━━━━━\n"
                txt += f"Funding : `{funding:.4f}%`\n"
                txt += f"Status  : {emoji} {status}"
                bot.reply_to(message, txt, parse_mode="Markdown")
                return
        bot.reply_to(message, f"❌ {coin} ga ada")
    except:
        bot.reply_to(message, "❌ Format: /sentiment BTC")

@bot.message_handler(commands=['whale'])
def whale(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else "BTC"
        l2 = info.l2_snapshot(coin)
        bids_raw = l2["levels"][0][:10]
        asks_raw = l2["levels"][1][:10]
        bids = sum([float(x["sz"]) * float(x["px"]) for x in bids_raw]) / 1e6
        asks = sum([float(x["sz"]) * float(x["px"]) for x in asks_raw]) / 1e6
        ratio = bids / asks if asks > 0 else 0
        big_bids = [x for x in bids_raw if float(x["sz"]) * float(x["px"]) > 500_000]
        big_asks = [x for x in asks_raw if float(x["sz"]) * float(x["px"]) > 500_000]
        if bids > asks * 2:
            verdict = "💚 BUY WALL DOMINAN — Akumulasi kuat"
        elif asks > bids * 2:
            verdict = "❤️ SELL WALL DOMINAN — Distribusi"
        else:
            verdict = "⚖️ BALANCED"
        txt  = f"🐳 *Whale Orderbook — {coin}*\n"
        txt += "━━━━━━━━━━━━━━━━━━\n"
        txt += f"🟢 Buy Wall  : `${bids:.2f}M`\n"
        txt += f"🔴 Sell Wall : `${asks:.2f}M`\n"
        txt += f"📐 Ratio     : `{ratio:.2f}x`\n"
        txt += f"🐋 Big Bids  : `{len(big_bids)} order >$500K`\n"
        txt += f"🦈 Big Asks  : `{len(big_asks)} order >$500K`\n\n"
        txt += verdict
        bot.reply_to(message, txt, parse_mode="Markdown")
    except:
        bot.reply_to(message, "❌ Format: /whale BTC")

@bot.message_handler(commands=['whalescan'])
def whalescan(message):
    bot.reply_to(message, "🔍 _Scanning whale activity..._", parse_mode="Markdown")
    try:
        data = info.meta_and_asset_ctxs()
        results = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                oi = float(ctx["openInterest"]) * float(ctx["markPx"]) / 1e6
                vol = float(ctx.get("dayNtlVlm") or 0) / 1e6
                fund = float(ctx.get("funding") or 0) * 100
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                score = 0
                if oi > 20: score += 2
                if vol > 50: score += 2
                if 0 < fund < 0.05: score += 2
                if change > 2: score += 2
                if change > 5: score += 1
                if oi > 100: score += 1
                if score >= 6:
                    sector = get_narrative(name)
                    results.append((name, oi, vol, fund, change, score, sector))
            except: continue
        results = sorted(results, key=lambda x: x[5], reverse=True)[:7]
        result  = "🕵️ *WHALE ACCUMULATION SCAN*\n"
        result += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        if not results:
            result += "😴 Ga ada sinyal akumulasi whale kuat sekarang.\n"
            result += "_Coba pas London/NY overlap_"
        else:
            for i, (name, oi, vol, fund, change, score, sector) in enumerate(results, 1):
                bar = "🟩" * min(score, 9)
                result += f"{'🔥' if i==1 else '⚡'} *#{i} {name}* `[{sector}]`\n"
                result += f"   OI    : `${oi:.0f}M` | Vol: `${vol:.0f}M`\n"
                result += f"   Fund  : `{fund:.4f}%` | Pump: `{change:+.2f}%`\n"
                result += f"   Score : {bar} `{score}/9`\n\n"
            result += "━━━━━━━━━━━━━━━━━━━━━\n"
            result += "📌 _Score tinggi = whale akum = potential long_\n"
            result += "⚠️ _DYOR, bukan financial advice_"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['liquidations', 'liq'])
def liquidations(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper() if len(parts) > 1 else None
        data = info.meta_and_asset_ctxs()
        total_long_liq = 0
        total_short_liq = 0
        coin_results = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                name = asset["name"]
                if coin and name != coin: continue
                oi = float(ctx["openInterest"]) * float(ctx["markPx"]) / 1e6
                vol = float(ctx.get("dayNtlVlm") or 0) / 1e6
                mark = float(ctx.get("markPx") or 0)
                prev = float(ctx.get("prevDayPx") or 0)
                change = ((mark - prev) / prev * 100) if prev > 0 else 0.0
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
        result  = f"🔴 *LIQUIDATION RADAR*"
        result += f"{' — ' + coin if coin else ' — ALL'}\n"
        result += "━━━━━━━━━━━━━━━━━━━━\n\n"
        result += f"💥 Est. Long Liq  : `${total_long_liq:.1f}M`\n"
        result += f"💥 Est. Short Liq : `${total_short_liq:.1f}M`\n\n"
        if coin_results:
            result += "*Top Liq Candidates:*\n\n"
            for name, liq, direction, change in coin_results:
                icon = "🔴" if direction == "LONG" else "🟢"
                result += f"{icon} *{name}* — `{direction} LIQ`\n"
                result += f"   Est : `${liq:.1f}M` | Move: `{change:+.2f}%`\n\n"
        result += "📌 _Estimasi dari OI × price move_"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['nuke'])
def nuke(message):
    try:
        data = info.meta_and_asset_ctxs()
        candidates = []
        for asset, ctx in zip(data[0]["universe"], data[1]):
            try:
                oi      = float(ctx["openInterest"]) * float(ctx["markPx"]) / 1e6
                funding = float(ctx["funding"]) * 100
                abs_f   = abs(funding)
                vol     = float(ctx.get("dayNtlVlm") or 0) / 1e6
                mark    = float(ctx.get("markPx") or 0)
                prev    = float(ctx.get("prevDayPx") or 0)
                change  = ((mark - prev) / prev * 100) if prev > 0 else 0.0
                score   = (oi * abs_f * 10) + (vol * 0.1) + (abs(change) * 2)
                if oi > 30 and abs_f > 0.03:
                    direction = "🔴 LONG SQUEEZE" if funding > 0 else "🟢 SHORT SQUEEZE"
                    candidates.append((asset["name"], oi, funding, vol, change, score, direction))
            except: continue
        candidates = sorted(candidates, key=lambda x: x[5], reverse=True)[:5]
        result  = "💣 *COIN SIAP NUKE*\n"
        result += "━━━━━━━━━━━━━━━━━━━━\n\n"
        if not candidates:
            result += "✅ Aman bro. Market ga ada yang ekstrem."
        else:
            for i, (name, oi, fund, vol, change, score, direction) in enumerate(candidates, 1):
                fire = "🔥" if i == 1 else "⚠️"
                result += f"{fire} *#{i} {name}*\n"
                result += f"   Squeeze : {direction}\n"
                result += f"   OI      : `${oi:.0f}M`\n"
                result += f"   Funding : `{fund:.4f}%`\n"
                result += f"   Vol     : `${vol:.0f}M`\n"
                result += f"   Move    : `{change:+.2f}%`\n"
                result += f"   🎯 Skor  : `{score:.0f}`\n\n"
            result += "📌 _Skor makin tinggi = makin rawan_"
        bot.reply_to(message, result, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

@bot.message_handler(commands=['report'])
def report(message):
    bot.reply_to(message, "📡 _Generating report..._", parse_mode="Markdown")
    try:
        summary = build_market_summary()
        bot.reply_to(message, summary, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: _{str(e)}_", parse_mode="Markdown")

def schedule_loop():
    while schedule_state["active"]:
        try:
            summary = build_market_summary()
            bot.send_message(schedule_state["chat_id"], summary, parse_mode="Markdown")
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
        if not schedule_state["thread"] or not schedule_state["thread"].is_alive():
            t = threading.Thread(target=schedule_loop, daemon=True)
            t.start()
            schedule_state["thread"] = t
        bot.reply_to(message, f"✅ *Auto Report AKTIF*\n⏱️ Setiap `{interval}` menit", parse_mode="Markdown")
    except:
        bot.reply_to(message, "❌ Format: /schedule 60")

@bot.message_handler(commands=['stopschedule'])
def stopschedule(message):
    schedule_state["active"] = False
    bot.reply_to(message, "⏹️ *Auto Report dimatikan.*", parse_mode="Markdown")

print("🤖 HL Intel Bot MONSTER — aktif...")
bot.infinity_polling()

