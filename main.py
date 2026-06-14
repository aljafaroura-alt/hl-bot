#!/usr/bin/env python3
# ============================================================
# SMART ENTRY ENGINE – HYPERLIQUID (v1.2)
# Data Integrity, Entropy Magnitude, Hypothesis Engine
# Owner: Cryptone Project
# ============================================================

import os
import time
import sqlite3
import threading
import logging
import logging.handlers
import argparse
import math
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any, Set
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

import telebot
import numpy as np
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ========== KONFIGURASI ==========
TOKEN = os.environ.get("TOKEN")
USER_ID = int(os.environ.get("USER_ID", "0"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0")) if os.environ.get("CHANNEL_ID") else None
if not TOKEN:
    raise ValueError("❌ TOKEN environment variable not set")

STATE_ENGINE_INTERVAL = 30
TRIGGER_ENGINE_INTERVAL_ACTIVE = 3
TRIGGER_ENGINE_INTERVAL_BACKGROUND = 15
COOLDOWN_ENTRY = 900
BASE_EVALUATION_DELAY = 7200
DB_PATH = "signals.db"
LOG_DIR = "logs"
PAPER_MODE = False

ACCEPTANCE_WINDOW_CANDLES = 2
PERSISTENCE_SECONDS = 30
UNCLEAR_THRESHOLD = 55
UNCLEAR_DIFF = 15
MIN_EVIDENCE_FAMILIES = 2
MIN_DATA_CONFIDENCE = 50

# Evidence multiplier
EVIDENCE_MULT_1 = 0.4
EVIDENCE_MULT_2 = 0.7
EVIDENCE_MULT_3 = 1.0

# Dynamic entropy parameters
ENTROPY_BASE = 60
ENTROPY_VOLATILITY_FACTOR = 0.3
ENTROPY_TREND_STRENGTH_FACTOR = 0.2
ENTROPY_TTL_FACTOR = 0.5
ENTROPY_RR_FACTOR = 1.2
ENTROPY_THRESHOLD_FACTOR = 0.2

# EMA smoothing untuk memory
MEMORY_EMA_ALPHA = 0.2
MEMORY_DECAY_RATE = 0.95

# OI persistence
OI_PERSISTENCE_REQUIRED = 3

# Rolling delta window
ROLLING_DELTA_WINDOW = 6

# Shadow retention
SHADOW_RETENTION_HOURS = 24

# Data quality age limits (ms)
MAX_CANDLE_AGE_MS = 60000
MAX_OB_AGE_MS = 5000
MAX_CVD_AGE_MS = 30000
MAX_OI_AGE_MS = 60000
MAX_PRICE_AGE_MS = 5000
MAX_FUNDING_AGE_MS = 60000

# Outlier detection threshold (standard deviations)
OUTLIER_SIGMA = 3.0
MAX_JUMP_PCT = 10.0  # maksimal perubahan persen untuk dianggap wajar

# ========== LOGGING ==========
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("SmartEntryEngine")
logger.setLevel(logging.DEBUG)
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "engine.log"), maxBytes=10*1024*1024, backupCount=5
)
file_handler.setLevel(logging.DEBUG)
error_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "error.log"), maxBytes=5*1024*1024, backupCount=3
)
error_handler.setLevel(logging.ERROR)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
for h in [file_handler, error_handler, console]:
    h.setFormatter(formatter)
    logger.addHandler(h)

# ========== GLOBAL STATE & LOCKS ==========
_candle_cache = {}
_candle_lock = threading.RLock()
_ob_cache = {}
_ob_lock = threading.RLock()
_cvd_cache = {}
_cvd_lock = threading.RLock()
_oi_history = {}
_oi_lock = threading.RLock()
_funding_cache = {}
_funding_lock = threading.RLock()
_last_alert = {}
_last_alert_lock = threading.RLock()
_alert_enabled = True
_alert_enabled_lock = threading.RLock()
_shutdown_event = threading.Event()

_last_mids = {}
_last_mids_lock = threading.RLock()

_rolling_delta = {}
_rolling_delta_lock = threading.RLock()

_oi_persistence = {}
_oi_persistence_lock = threading.RLock()

_zone_memory = {}
_zone_memory_lock = threading.RLock()

_coin_ema_memory = {}
_coin_memory_lock = threading.RLock()

_active_candidates = {}
_active_candidates_lock = threading.RLock()

_journal_cache = deque(maxlen=100)
_journal_lock = threading.RLock()

_counterfactual_store = deque(maxlen=50)
_counterfactual_lock = threading.RLock()

_shadow_decisions = {}
_shadow_lock = threading.RLock()

_module_credits = {}
_module_credits_lock = threading.RLock()

# Hypothesis engine: menyimpan thesis untuk setiap sinyal
_hypothesis_store = {}  # {signal_id: {"thesis": str, "invalidate": str, "observe": str, "validated": bool}}
_hypothesis_lock = threading.RLock()

# Untuk data integrity: riwayat OI, funding, price
_oi_values = {}   # {coin: deque([(timestamp, oi_usd)])}
_funding_values = {}  # {coin: deque([(timestamp, funding_pct)])}
_price_values = {}  # {coin: deque([(timestamp, price)])}
_data_integrity_lock = threading.RLock()

info = Info(constants.MAINNET_API_URL)


# ========== DATABASE ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT UNIQUE,
        coin TEXT NOT NULL,
        direction TEXT NOT NULL,
        score INTEGER,
        entry_price REAL,
        sl_price REAL,
        tp_price REAL,
        rr REAL,
        reason TEXT,
        timestamp INTEGER,
        evaluated INTEGER DEFAULT 0,
        outcome TEXT,
        pnl REAL,
        exit_price REAL,
        exit_time INTEGER,
        mfe REAL,
        mae REAL,
        data_confidence INTEGER,
        hypothesis_thesis TEXT,
        hypothesis_invalidate TEXT,
        hypothesis_observe TEXT,
        hypothesis_validated INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        market_regime TEXT,
        volatility_regime TEXT,
        flow_regime TEXT,
        long_score INTEGER,
        short_score INTEGER,
        direction TEXT,
        final_score INTEGER,
        reason TEXT,
        negative_evidence TEXT,
        entropy INTEGER,
        decision_time_ms INTEGER,
        api_latency_ms INTEGER,
        data_confidence INTEGER,
        executed INTEGER DEFAULT 0,
        outcome TEXT,
        missed_opportunity_pnl REAL DEFAULT NULL,
        contribution TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS counterfactual (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        original_score INTEGER,
        modified_module TEXT,
        modified_score INTEGER,
        reason TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS shadow_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT UNIQUE,
        coin TEXT,
        direction TEXT,
        entry_price REAL,
        sl_price REAL,
        tp_price REAL,
        timestamp INTEGER,
        evaluated INTEGER DEFAULT 0,
        outcome TEXT,
        pnl REAL,
        mfe REAL,
        mae REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS module_credits (
        module TEXT PRIMARY KEY,
        total_impact REAL,
        hit_count INTEGER,
        last_updated INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS data_freshness_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        price_age_ms INTEGER,
        oi_age_ms INTEGER,
        funding_age_ms INTEGER,
        candle_age_ms INTEGER,
        ob_age_ms INTEGER,
        overall_score INTEGER,
        integrity_score INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS hypothesis_validation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT,
        thesis TEXT,
        outcome TEXT,
        pnl REAL,
        validated INTEGER
    )''')
    conn.commit()
    conn.close()
    logger.info("Database ready")

def save_signal(signal_id, coin, direction, score, entry, sl, tp, rr, reason, data_confidence,
                hypothesis_thesis="", hypothesis_invalidate="", hypothesis_observe=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO signals 
                 (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason, timestamp, data_confidence,
                  hypothesis_thesis, hypothesis_invalidate, hypothesis_observe)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()), data_confidence,
               hypothesis_thesis, hypothesis_invalidate, hypothesis_observe))
    conn.commit()
    conn.close()
    logger.info(f"Signal saved: {coin} {direction} score={score} dq={data_confidence}")

def update_signal_outcome(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if hypothesis_validated is not None:
        c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, mfe=?, mae=?, hypothesis_validated=?
                     WHERE signal_id=?''',
                  (outcome, pnl, exit_price, int(time.time()), mfe, mae, 1 if hypothesis_validated else 0, signal_id))
    else:
        c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, mfe=?, mae=?
                     WHERE signal_id=?''',
                  (outcome, pnl, exit_price, int(time.time()), mfe, mae, signal_id))
    conn.commit()
    conn.close()

def add_journal_entry(coin, market_regime, volatility_regime, flow_regime,
                      long_score, short_score, direction, final_score,
                      reason, negative_evidence, entropy, decision_time_ms, api_latency_ms,
                      data_confidence, executed, missed_opportunity_pnl=None, contribution=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO journal 
                 (timestamp, coin, market_regime, volatility_regime, flow_regime,
                  long_score, short_score, direction, final_score, reason, negative_evidence,
                  entropy, decision_time_ms, api_latency_ms, data_confidence, executed, missed_opportunity_pnl, contribution)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (int(time.time()), coin, market_regime, volatility_regime, flow_regime,
               long_score, short_score, direction, final_score, reason, negative_evidence,
               entropy, decision_time_ms, api_latency_ms, data_confidence, 1 if executed else 0, missed_opportunity_pnl, contribution))
    conn.commit()
    conn.close()

def add_counterfactual(coin, original_score, modified_module, modified_score, reason):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO counterfactual (timestamp, coin, original_score, modified_module, modified_score, reason)
                 VALUES (?,?,?,?,?,?)''',
              (int(time.time()), coin, original_score, modified_module, modified_score, reason))
    conn.commit()
    conn.close()

def update_module_credit(module: str, impact: float):
    with _module_credits_lock:
        if module not in _module_credits:
            _module_credits[module] = {"total_impact": 0.0, "hit_count": 0, "last_updated": int(time.time())}
        _module_credits[module]["total_impact"] += impact
        _module_credits[module]["hit_count"] += 1
        _module_credits[module]["last_updated"] = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO module_credits (module, total_impact, hit_count, last_updated)
                 VALUES (?,?,?,?)''',
              (module, _module_credits[module]["total_impact"], _module_credits[module]["hit_count"], _module_credits[module]["last_updated"]))
    conn.commit()
    conn.close()

def get_module_credits(module: str) -> float:
    with _module_credits_lock:
        if module not in _module_credits or _module_credits[module]["hit_count"] == 0:
            return 0.0
        return _module_credits[module]["total_impact"] / _module_credits[module]["hit_count"]

def add_shadow_decision(signal_id, coin, direction, entry, sl, tp):
    with _shadow_lock:
        _shadow_decisions[signal_id] = {
            "coin": coin, "direction": direction, "entry": entry, "sl": sl, "tp": tp,
            "timestamp": time.time(), "evaluated": False, "outcome": None, "pnl": 0.0,
            "mfe": 0.0, "mae": 0.0
        }
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO shadow_decisions (signal_id, coin, direction, entry_price, sl_price, tp_price, timestamp)
                 VALUES (?,?,?,?,?,?,?)''',
              (signal_id, coin, direction, entry, sl, tp, int(time.time())))
    conn.commit()
    conn.close()

def update_shadow_outcome(signal_id, outcome, pnl, mfe, mae):
    with _shadow_lock:
        if signal_id in _shadow_decisions:
            _shadow_decisions[signal_id]["evaluated"] = True
            _shadow_decisions[signal_id]["outcome"] = outcome
            _shadow_decisions[signal_id]["pnl"] = pnl
            _shadow_decisions[signal_id]["mfe"] = mfe
            _shadow_decisions[signal_id]["mae"] = mae
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''UPDATE shadow_decisions SET evaluated=1, outcome=?, pnl=?, mfe=?, mae=? WHERE signal_id=?''',
              (outcome, pnl, mfe, mae, signal_id))
    conn.commit()
    conn.close()

def log_data_freshness(coin, price_age_ms, oi_age_ms, funding_age_ms, candle_age_ms, ob_age_ms, overall_score, integrity_score):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO data_freshness_log
                 (timestamp, coin, price_age_ms, oi_age_ms, funding_age_ms, candle_age_ms, ob_age_ms, overall_score, integrity_score)
                 VALUES (?,?,?,?,?,?,?,?,?)''',
              (int(time.time()), coin, price_age_ms, oi_age_ms, funding_age_ms, candle_age_ms, ob_age_ms, overall_score, integrity_score))
    conn.commit()
    conn.close()

def add_hypothesis_validation(signal_id, thesis, outcome, pnl, validated):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO hypothesis_validation (signal_id, thesis, outcome, pnl, validated)
                 VALUES (?,?,?,?,?)''',
              (signal_id, thesis, outcome, pnl, 1 if validated else 0))
    conn.commit()
    conn.close()

def get_analytics() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT COUNT(*), SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END),
                       AVG(rr), SUM(pnl) FROM signals WHERE evaluated=1''')
    total, wins, avg_rr, total_pnl = c.fetchone()
    total = total or 0
    wins = wins or 0
    win_rate = (wins / total * 100) if total > 0 else 0
    conn.close()
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": round(win_rate, 1), "avg_rr": round(avg_rr or 0, 2),
        "total_pnl": round(total_pnl or 0, 2)
    }
    

# ========== HELPER ==========
def fmt_price(p): return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"
def get_wib(): return datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
def get_wib_hour(): return datetime.now(timezone(timedelta(hours=7))).hour
def generate_signal_id(coin, direction): return f"{coin}_{direction}_{int(time.time())}"

# ========== DATA INTEGRITY ENGINE ==========
def detect_outlier(values: List[float], new_value: float) -> bool:
    if len(values) < 3:
        return False
    mean = np.mean(values)
    std = np.std(values)
    if std == 0:
        return False
    return abs(new_value - mean) > OUTLIER_SIGMA * std

def detect_jump(prev_value: float, new_value: float) -> bool:
    if prev_value == 0:
        return False
    pct_change = abs((new_value - prev_value) / prev_value) * 100
    return pct_change > MAX_JUMP_PCT

def update_data_integrity_history(coin: str, oi_usd: float, funding_pct: float, price: float):
    with _data_integrity_lock:
        if coin not in _oi_values:
            _oi_values[coin] = deque(maxlen=10)
        if coin not in _funding_values:
            _funding_values[coin] = deque(maxlen=10)
        if coin not in _price_values:
            _price_values[coin] = deque(maxlen=10)
        _oi_values[coin].append((time.time(), oi_usd))
        _funding_values[coin].append((time.time(), funding_pct))
        _price_values[coin].append((time.time(), price))

def get_data_integrity_score(coin: str) -> int:
    """
    Skor 0-100, semakin tinggi semakin baik.
    Memeriksa: missing data, outlier, jump.
    """
    score = 100
    reasons = []
    with _data_integrity_lock:
        # OI integrity
        if coin in _oi_values and len(_oi_values[coin]) >= 2:
            oi_vals = [v for _, v in _oi_values[coin]]
            latest_oi = oi_vals[-1]
            if detect_outlier(oi_vals[:-1], latest_oi):
                score -= 25
                reasons.append("oi_outlier")
            if len(oi_vals) >= 2 and detect_jump(oi_vals[-2], latest_oi):
                score -= 20
                reasons.append("oi_jump")
        else:
            score -= 15
            reasons.append("oi_insufficient")
        # Funding integrity
        if coin in _funding_values and len(_funding_values[coin]) >= 2:
            fund_vals = [v for _, v in _funding_values[coin]]
            latest_fund = fund_vals[-1]
            if detect_outlier(fund_vals[:-1], latest_fund):
                score -= 20
                reasons.append("funding_outlier")
            if len(fund_vals) >= 2 and detect_jump(fund_vals[-2], latest_fund):
                score -= 15
                reasons.append("funding_jump")
        else:
            score -= 10
            reasons.append("funding_insufficient")
        # Price integrity
        if coin in _price_values and len(_price_values[coin]) >= 2:
            price_vals = [v for _, v in _price_values[coin]]
            latest_price = price_vals[-1]
            if detect_outlier(price_vals[:-1], latest_price):
                score -= 20
                reasons.append("price_outlier")
            if len(price_vals) >= 2 and detect_jump(price_vals[-2], latest_price):
                score -= 15
                reasons.append("price_jump")
        else:
            score -= 10
            reasons.append("price_insufficient")
    if score < 60:
        logger.warning(f"Data integrity low for {coin}: {score}% ({', '.join(reasons)})")
    return max(0, min(100, score))

# ========== DATA FRESHNESS ENGINE (dengan integrity) ==========
def get_data_confidence(coin: str, current_price: float, current_time: float) -> Tuple[int, Dict[str, int]]:
    ages = {}
    total_score = 100
    # Price age
    with _last_mids_lock:
        if coin in _last_mids:
            price_ts = _last_mids[coin][1]
            age_ms = (current_time - price_ts) * 1000
        else:
            age_ms = MAX_PRICE_AGE_MS + 1000
    ages["price_age_ms"] = int(age_ms)
    if age_ms > MAX_PRICE_AGE_MS:
        total_score -= 25
    elif age_ms > MAX_PRICE_AGE_MS // 2:
        total_score -= 10
    # Candle age (1h)
    candle_key = f"{coin}_1h_80"
    if candle_key in _candle_cache:
        _, ts = _candle_cache[candle_key]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_CANDLE_AGE_MS + 1000
    ages["candle_age_ms"] = int(age_ms)
    if age_ms > MAX_CANDLE_AGE_MS:
        total_score -= 20
    elif age_ms > MAX_CANDLE_AGE_MS // 2:
        total_score -= 8
    # OB age
    if coin in _ob_cache:
        _, ts = _ob_cache[coin]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_OB_AGE_MS + 1000
    ages["ob_age_ms"] = int(age_ms)
    if age_ms > MAX_OB_AGE_MS:
        total_score -= 15
    elif age_ms > MAX_OB_AGE_MS // 2:
        total_score -= 5
    # CVD age
    if coin in _cvd_cache:
        _, ts = _cvd_cache[coin]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_CVD_AGE_MS + 1000
    ages["cvd_age_ms"] = int(age_ms)
    if age_ms > MAX_CVD_AGE_MS:
        total_score -= 10
    # OI age
    if coin in _oi_history and len(_oi_history[coin]) > 0:
        oi_ts = _oi_history[coin][-1][0]
        age_ms = (current_time - oi_ts) * 1000
    else:
        age_ms = MAX_OI_AGE_MS + 1000
    ages["oi_age_ms"] = int(age_ms)
    if age_ms > MAX_OI_AGE_MS:
        total_score -= 15
    elif age_ms > MAX_OI_AGE_MS // 2:
        total_score -= 5
    # Funding age
    if coin in _funding_cache:
        _, ts = _funding_cache[coin]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_FUNDING_AGE_MS + 1000
    ages["funding_age_ms"] = int(age_ms)
    if age_ms > MAX_FUNDING_AGE_MS:
        total_score -= 10
    total_score = max(0, min(100, total_score))
    # Integrity score
    integrity_score = get_data_integrity_score(coin)
    # Combine freshness and integrity (weighted)
    final_confidence = int(total_score * 0.7 + integrity_score * 0.3)
    if final_confidence < MIN_DATA_CONFIDENCE:
        logger.warning(f"Data confidence low for {coin}: {final_confidence}% (fresh={total_score}, integrity={integrity_score})")
    if int(current_time) % 300 < 5:
        log_data_freshness(coin, ages.get("price_age_ms",0), ages.get("oi_age_ms",0),
                           ages.get("funding_age_ms",0), ages.get("candle_age_ms",0),
                           ages.get("ob_age_ms",0), total_score, integrity_score)
    return final_confidence, ages

def update_mids_cache():
    try:
        mids = info.all_mids()
        now = time.time()
        with _last_mids_lock:
            for coin, price in mids.items():
                _last_mids[coin] = (float(price), now)
        # Also update price integrity history
        for coin, price in mids.items():
            update_data_integrity_history(coin, 0, 0, float(price))
    except Exception as e:
        logger.error(f"Update mids cache error: {e}")

# ========== MODULE ATTRIBUTION (sama) ==========
def compute_module_impact(outcome_pnl: float, mfe: float, mae: float,
                          entropy: int, evidence_families: int, exhaustion: int, data_confidence: int) -> Dict[str, float]:
    success = (outcome_pnl > 0.5) or (mfe > abs(mae) * 1.5)
    impact = {}
    if entropy > 70:
        impact["entropy"] = 10 if success else -15
    elif entropy > 50:
        impact["entropy"] = 5 if success else -8
    else:
        impact["entropy"] = 2 if success else -2
    if evidence_families >= 3:
        impact["evidence"] = 15 if success else -10
    elif evidence_families == 2:
        impact["evidence"] = 5 if success else -5
    else:
        impact["evidence"] = -10 if success else -20
    if exhaustion > 50:
        impact["exhaustion"] = -20 if success else -5
    elif exhaustion > 30:
        impact["exhaustion"] = -10 if success else 0
    else:
        impact["exhaustion"] = 5 if success else 5
    if data_confidence < 60:
        impact["data_quality"] = -15 if success else -25
    elif data_confidence < 80:
        impact["data_quality"] = -5 if success else -10
    else:
        impact["data_quality"] = 5 if success else 5
    return impact

def apply_module_credits(coin: str, outcome_pnl: float, mfe: float, mae: float,
                         entropy: int, evidence_families: int, exhaustion: int, data_confidence: int):
    impacts = compute_module_impact(outcome_pnl, mfe, mae, entropy, evidence_families, exhaustion, data_confidence)
    for module, impact in impacts.items():
        update_module_credit(module, impact)

# ========== EMA MEMORY PER COIN ==========
def update_coin_ema_memory(coin: str, outcome_win: bool):
    with _coin_memory_lock:
        if coin not in _coin_ema_memory:
            _coin_ema_memory[coin] = {"ema_winrate": 0.5, "last_update": time.time()}
        mem = _coin_ema_memory[coin]
        outcome_val = 1.0 if outcome_win else 0.0
        mem["ema_winrate"] = MEMORY_EMA_ALPHA * outcome_val + (1 - MEMORY_EMA_ALPHA) * mem["ema_winrate"]
        mem["last_update"] = time.time()

def get_coin_aggression_mult(coin: str) -> float:
    with _coin_memory_lock:
        if coin not in _coin_ema_memory:
            return 1.0
        ema = _coin_ema_memory[coin]["ema_winrate"]
        if ema < 0.4:
            return 0.7
        elif ema < 0.45:
            return 0.85
        elif ema > 0.6:
            return 1.2
        elif ema > 0.55:
            return 1.1
        return 1.0

def decay_coin_memories():
    with _coin_memory_lock:
        for coin, mem in _coin_ema_memory.items():
            age_days = (time.time() - mem["last_update"]) / 86400
            if age_days > 1:
                decay_factor = MEMORY_DECAY_RATE ** age_days
                mem["ema_winrate"] = 0.5 + (mem["ema_winrate"] - 0.5) * decay_factor

# ========== ACTIVE CANDIDATE DENGAN DISTANCE_TO_ENTRY ==========
def update_active_candidate(coin: str, current_price: float, entropy: int, entry_price: float = None):
    vol_reg = get_volatility_regime()
    base_ttl = 1800
    if vol_reg == "HIGH_VOLATILITY":
        base_ttl = 900
    elif vol_reg == "LOW_VOLATILITY":
        base_ttl = 3600
    ttl_adj = max(0.5, 1.0 - (entropy / 100) * ENTROPY_TTL_FACTOR)
    base_ttl = int(base_ttl * ttl_adj)
    # Distance to entry: jika sudah terlalu jauh, lebih cepat expire
    if entry_price:
        dist_pct = abs(current_price - entry_price) / entry_price * 100
        if dist_pct > 2.0:
            base_ttl = int(base_ttl * 0.5)
        elif dist_pct > 1.0:
            base_ttl = int(base_ttl * 0.8)
    with _active_candidates_lock:
        _active_candidates[coin] = {
            "expire_time": time.time() + base_ttl,
            "last_price": current_price,
            "last_entropy": entropy
        }

def is_active_candidate(coin: str, current_price: float, entropy: int) -> bool:
    with _active_candidates_lock:
        if coin not in _active_candidates:
            return False
        cand = _active_candidates[coin]
        if time.time() > cand["expire_time"]:
            del _active_candidates[coin]
            return False
        price_move = abs(current_price - cand["last_price"]) / cand["last_price"] * 100 if cand["last_price"] > 0 else 0
        if price_move > 3.0 or abs(entropy - cand["last_entropy"]) > 20:
            del _active_candidates[coin]
            return False
        return True

def cleanup_active_candidates():
    now = time.time()
    with _active_candidates_lock:
        expired = [c for c, d in _active_candidates.items() if now > d["expire_time"]]
        for c in expired:
            del _active_candidates[c]

# ========== OI PERSISTENCE ==========
def update_oi_persistence(coin: str, oi_roc: float):
    with _oi_persistence_lock:
        if coin not in _oi_persistence:
            _oi_persistence[coin] = {"count": 0, "last_trend": 0, "values": deque(maxlen=OI_PERSISTENCE_REQUIRED)}
        pers = _oi_persistence[coin]
        if oi_roc > 1.0:
            trend = 1
        elif oi_roc < -1.0:
            trend = -1
        else:
            trend = 0
        pers["values"].append(trend)
        if len(pers["values"]) == OI_PERSISTENCE_REQUIRED:
            if all(v == 1 for v in pers["values"]):
                pers["count"] = OI_PERSISTENCE_REQUIRED
                pers["last_trend"] = 1
            elif all(v == -1 for v in pers["values"]):
                pers["count"] = OI_PERSISTENCE_REQUIRED
                pers["last_trend"] = -1
            else:
                pers["count"] = max(0, pers["count"] - 1)

def get_oi_persistence(coin: str) -> Tuple[bool, int]:
    with _oi_persistence_lock:
        if coin not in _oi_persistence:
            return False, 0
        pers = _oi_persistence[coin]
        if pers["count"] >= OI_PERSISTENCE_REQUIRED:
            return True, pers["last_trend"]
        return False, 0

def cleanup_old_shadow_decisions():
    now = time.time()
    cutoff = now - SHADOW_RETENTION_HOURS * 3600
    with _shadow_lock:
        to_delete = [sid for sid, data in _shadow_decisions.items() if data["timestamp"] < cutoff]
        for sid in to_delete:
            del _shadow_decisions[sid]

# ========== ENTROPY WITH MAGNITUDE ==========
def compute_market_entropy(coin: str, master: Dict) -> int:
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 4:
        return 30
    closes = [float(c['c']) for c in candles[-5:]]
    price_changes = [abs(closes[i] - closes[i-1])/closes[i-1]*100 for i in range(1, len(closes))]
    price_flips = sum(1 for i in range(1, len(price_changes)) if (closes[i] > closes[i-1]) != (closes[i-1] > closes[i-2]) if i>=2 else 0)
    price_magnitude = sum(price_changes) / len(price_changes) if price_changes else 0
    delta_vals = [get_ob_delta(coin) for _ in range(3)]
    delta_changes = [abs(delta_vals[i] - delta_vals[i-1]) for i in range(1, len(delta_vals))]
    delta_flips = sum(1 for i in range(1, len(delta_vals)) if (delta_vals[i] > 0) != (delta_vals[i-1] > 0))
    delta_magnitude = sum(delta_changes) / len(delta_changes) if delta_changes else 0
    oi_vals = [get_oi_roc(coin) for _ in range(3)]
    oi_changes = [abs(oi_vals[i] - oi_vals[i-1]) for i in range(1, len(oi_vals))]
    oi_flips = sum(1 for i in range(1, len(oi_vals)) if (oi_vals[i] > 0) != (oi_vals[i-1] > 0))
    oi_magnitude = sum(oi_changes) / len(oi_changes) if oi_changes else 0
    flip_score = min(100, (price_flips + delta_flips + oi_flips) * 25)
    magnitude_score = min(100, (price_magnitude * 20 + delta_magnitude * 10 + oi_magnitude * 10))
    entropy = (flip_score + magnitude_score) // 2
    return min(100, max(0, entropy))

def get_entropy_adjusted_min_rr(base_rr: float, entropy: int) -> float:
    factor = 1.0 + (entropy / 100) * ENTROPY_RR_FACTOR
    return base_rr * factor

def get_entropy_adjusted_threshold(base_threshold: int, entropy: int) -> int:
    factor = 1.0 + (entropy / 100) * ENTROPY_THRESHOLD_FACTOR
    new_th = int(base_threshold * factor)
    return max(50, min(85, new_th))

def get_entropy_adjusted_aggression(agg_mult: float, entropy: int) -> float:
    factor = 1.0 - (entropy / 100) * 0.3
    return max(0.5, agg_mult * factor)

# ========== DYNAMIC EVALUATION HORIZON ==========
def get_evaluation_delay(coin: str, atr_pct: float, rr: float, regime: str) -> int:
    """
    Dynamic delay berdasarkan ATR, RR, market regime.
    Scalp (volatile, RR kecil) -> cepat. Swing (trend, RR besar) -> lambat.
    """
    base = BASE_EVALUATION_DELAY
    # ATR adjustment: semakin volatile semakin cepat evaluasi
    if atr_pct > 2.0:
        base = int(base * 0.6)
    elif atr_pct > 1.2:
        base = int(base * 0.8)
    # RR adjustment: RR besar butuh waktu lebih lama
    if rr > 2.5:
        base = int(base * 1.2)
    elif rr < 1.8:
        base = int(base * 0.8)
    # Regime adjustment
    if regime in ("PANIC", "VOLATILE"):
        base = int(base * 0.7)
    elif regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base = int(base * 1.1)
    return max(1800, min(14400, base))  # antara 30 menit - 4 jam
    

# ========== DATA FETCHING ==========
def fetch_candles_master(coins: List[str], timeframe: str, limit: int = 80) -> Dict[str, List[dict]]:
    def fetch_one(coin):
        try:
            end_ms = int(time.time() * 1000)
            tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
            interval = tf_ms.get(timeframe, 3600000)
            start_ms = end_ms - limit * interval
            candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms)
            return coin, (candles if candles else [])
        except Exception as e:
            logger.error(f"Fetch {coin} {timeframe}: {e}")
            return coin, []
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(fetch_one, c) for c in coins]
        for f in futures:
            coin, candles = f.result()
            if candles:
                results[coin] = candles
    return results

def get_candles(coin: str, timeframe: str, limit: int = 80, master: Dict = None) -> List[dict]:
    if master and coin in master:
        return master[coin]
    key = f"{coin}_{timeframe}_{limit}"
    now = time.time()
    ttl = {"5m": 60, "15m": 120, "1h": 300, "4h": 600}.get(timeframe, 300)
    with _candle_lock:
        if key in _candle_cache and now - _candle_cache[key][1] < ttl:
            return _candle_cache[key][0]
    end_ms = int(now * 1000)
    tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
    interval = tf_ms.get(timeframe, 3600000)
    start_ms = end_ms - limit * interval
    candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms) or []
    with _candle_lock:
        _candle_cache[key] = (candles, now)
    return candles

def get_ob_delta(coin: str) -> float:
    now = time.time()
    with _ob_lock:
        if coin in _ob_cache and now - _ob_cache[coin][1] < 5:
            return _ob_cache[coin][0]
    try:
        l2 = info.l2_snapshot(coin)
        bids = sum(float(b['sz'])*float(b['px']) for b in l2['levels'][0][:5])
        asks = sum(float(a['sz'])*float(a['px']) for a in l2['levels'][1][:5])
        if bids + asks == 0:
            return 0
        raw = (bids - asks) / (bids + asks) * 100
        raw = max(-60, min(60, raw))
        prev = _ob_cache.get(coin, (raw, now))[0]
        smoothed = 0.3 * raw + 0.7 * prev
        with _ob_lock:
            _ob_cache[coin] = (smoothed, now)
        return smoothed
    except:
        return 0

def update_rolling_delta(coin: str):
    delta = get_ob_delta(coin)
    with _rolling_delta_lock:
        if coin not in _rolling_delta:
            _rolling_delta[coin] = deque(maxlen=ROLLING_DELTA_WINDOW)
        _rolling_delta[coin].append(delta)

def get_delta_shift(coin: str) -> float:
    with _rolling_delta_lock:
        if coin not in _rolling_delta or len(_rolling_delta[coin]) < 2:
            return 0.0
        recent = list(_rolling_delta[coin])
        return recent[-1] - recent[0]

def get_cvd(coin: str, minutes: int = 30) -> float:
    now = time.time()
    with _cvd_lock:
        if coin in _cvd_cache and now - _cvd_cache[coin][1] < 30:
            return _cvd_cache[coin][0]
    try:
        trades = info.recent_trades(coin)
        if not trades:
            return 0
        cutoff = int((now - minutes*60) * 1000)
        cvd = 0.0
        for t in trades:
            if t['time'] < cutoff:
                continue
            usd = float(t['px']) * float(t['sz'])
            cvd += usd if t['side'] == 'B' else -usd
        cvd_val = cvd / 1e6
        with _cvd_lock:
            _cvd_cache[coin] = (cvd_val, now)
        return cvd_val
    except:
        return 0

def get_oi_usd(coin: str) -> float:
    try:
        meta = info.meta_and_asset_ctxs()
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            if asset["name"] == coin:
                oi = float(ctx.get("openInterest", 0))
                mark = float(ctx.get("markPx", 0))
                oi_usd = oi * mark / 1e6 if mark > 0 else 0
                with _oi_lock:
                    if coin not in _oi_history:
                        _oi_history[coin] = deque(maxlen=20)
                    _oi_history[coin].append((time.time(), oi_usd))
                # Update data integrity history
                update_data_integrity_history(coin, oi_usd, 0, 0)
                return oi_usd
    except:
        pass
    return 0

def get_oi_roc(coin: str) -> float:
    with _oi_lock:
        if coin not in _oi_history or len(_oi_history[coin]) < 2:
            return 0.0
        now = time.time()
        cutoff = now - 300
        oi_vals = [v for ts, v in _oi_history[coin] if ts >= cutoff]
        if not oi_vals:
            return 0.0
        oi_avg = sum(oi_vals) / len(oi_vals)
        oi_current = _oi_history[coin][-1][1]
        if oi_avg == 0:
            return 0.0
        return (oi_current - oi_avg) / oi_avg * 100

def get_funding_pct(coin: str) -> float:
    now = time.time()
    with _funding_lock:
        if coin in _funding_cache and now - _funding_cache[coin][1] < 60:
            return _funding_cache[coin][0]
    try:
        meta = info.meta_and_asset_ctxs()
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            if asset["name"] == coin:
                funding = float(ctx.get("funding", 0)) * 100
                with _funding_lock:
                    _funding_cache[coin] = (funding, now)
                update_data_integrity_history(coin, 0, funding, 0)
                return funding
    except:
        pass
    return 0

def get_atr_pct(coin: str, period: int = 14, timeframe: str = "1h", master: Dict = None) -> float:
    candles = get_candles(coin, timeframe, period+5, master)
    if not candles or len(candles) < period+1:
        return 1.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]['h']); l = float(candles[i]['l']); pc = float(candles[i-1]['c'])
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    price = float(candles[-1]['c'])
    return (atr / price) * 100 if price > 0 else 1.0

def get_volume_spike(coin: str, master: Dict = None) -> float:
    candles = get_candles(coin, "5m", 30, master)
    if not candles or len(candles) < 6:
        return 1.0
    price = float(candles[-1]['c'])
    cur = float(candles[-1]['v']) * price
    prev = [float(c['v']) * float(c['c']) for c in candles[-6:-1]]
    avg = sum(prev)/len(prev) if prev else 1.0
    return cur / avg if avg > 0 else 1.0

def get_session() -> str:
    h = get_wib_hour()
    if 8 <= h < 15: return "ASIA"
    if 15 <= h < 20: return "LONDON"
    if 20 <= h or h < 2: return "NY"
    return "ASIA"

def get_market_regime() -> str:
    candles = get_candles("BTC", "4h", 50)
    if not candles:
        return "RANGING"
    closes = [float(c['c']) for c in candles[-30:]]
    if len(closes) < 21:
        return "RANGING"
    ema9 = sum(closes[-9:])/9
    ema21 = sum(closes[-21:])/21
    if ema9 > ema21 * 1.02:
        return "TRENDING_UP"
    if ema9 < ema21 * 0.98:
        return "TRENDING_DOWN"
    return "RANGING"

def get_volatility_regime() -> str:
    atr = get_atr_pct("BTC", period=14, timeframe="4h")
    if atr > 4:
        return "HIGH_VOLATILITY"
    elif atr < 1.5:
        return "LOW_VOLATILITY"
    else:
        return "NORMAL_VOLATILITY"

def get_flow_regime() -> str:
    delta_shift = get_delta_shift("BTC")
    if delta_shift > 4:
        return "FLOW_ACCELERATING"
    elif delta_shift < -4:
        return "FLOW_DECELERATING"
    else:
        return "FLOW_NEUTRAL"

# ========== MARKET STATE ==========
class MarketState(Enum):
    UNKNOWN = 0
    ACCUMULATION = 1
    EXPANSION = 2
    DISTRIBUTION = 3
    REVERSAL = 4

def detect_swing_points(candles, lookback=3):
    highs, lows = [], []
    for i in range(lookback, len(candles)-lookback):
        left_high = all(float(candles[i]['h']) > float(candles[i-j]['h']) for j in range(1, lookback+1))
        right_high = all(float(candles[i]['h']) > float(candles[i+j]['h']) for j in range(1, lookback+1))
        if left_high and right_high:
            highs.append((i, float(candles[i]['h'])))
        left_low = all(float(candles[i]['l']) < float(candles[i-j]['l']) for j in range(1, lookback+1))
        right_low = all(float(candles[i]['l']) < float(candles[i+j]['l']) for j in range(1, lookback+1))
        if left_low and right_low:
            lows.append((i, float(candles[i]['l'])))
    return highs, lows

def get_market_state_from_structure(candles, current_price) -> MarketState:
    if not candles or len(candles) < 30:
        return MarketState.UNKNOWN
    highs, lows = detect_swing_points(candles, lookback=3)
    if len(highs) < 2 or len(lows) < 2:
        return MarketState.UNKNOWN
    recent_highs = [h for h in highs[-3:]]
    recent_lows = [l for l in lows[-3:]]
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1][1] > recent_highs[-2][1]
        hl = recent_lows[-1][1] > recent_lows[-2][1]
        lh = recent_highs[-1][1] < recent_highs[-2][1]
        ll = recent_lows[-1][1] < recent_lows[-2][1]
        if hh and hl:
            return MarketState.EXPANSION if current_price > recent_highs[-1][1] else MarketState.ACCUMULATION
        elif lh and ll:
            return MarketState.DISTRIBUTION if current_price < recent_lows[-1][1] else MarketState.REVERSAL
    return MarketState.UNKNOWN

def get_bos_and_choch(candles, highs, lows):
    if not candles or len(highs)<2 or len(lows)<2:
        return False, False, False
    last_close = float(candles[-1]['c'])
    prev_high = highs[-2][1]
    prev_low = lows[-2][1]
    bos_up = last_close > prev_high
    bos_down = last_close < prev_low
    choch = False
    if len(highs) >= 3 and len(lows) >= 3:
        prev2_high = highs[-3][1]
        prev2_low = lows[-3][1]
        hh_before = highs[-2][1] > prev2_high and lows[-2][1] > lows[-3][1]
        lh_now = last_close < highs[-2][1] and last_close < lows[-2][1]
        if hh_before and lh_now:
            choch = True
    return bos_up, bos_down, choch

def get_structure_valid_separate(coin: str, master: Dict) -> Tuple[bool, bool]:
    candles_1h = get_candles(coin, "1h", 60, master)
    if not candles_1h:
        return False, False
    highs, lows = detect_swing_points(candles_1h, lookback=3)
    if len(highs)<2 or len(lows)<2:
        return False, False
    bos_up, bos_down, choch = get_bos_and_choch(candles_1h, highs, lows)
    valid_long = bos_up or choch
    valid_short = bos_down or choch
    return valid_long, valid_short

# ========== ZONE MEMORY ==========
def update_zone_memory(coin: str, zone_type: str, low: float, high: float, reaction: str):
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    now = time.time()
    with _zone_memory_lock:
        if key not in _zone_memory:
            _zone_memory[key] = {"touch_count": 0, "first_touch": now, "last_touch": now, "reactions": []}
        data = _zone_memory[key]
        data["touch_count"] += 1
        data["last_touch"] = now
        data["reactions"].append(reaction)
        if len(data["reactions"]) > 10:
            data["reactions"] = data["reactions"][-10:]

def get_zone_penalty(coin: str, zone_type: str, low: float, high: float) -> int:
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    with _zone_memory_lock:
        if key not in _zone_memory:
            return 0
        data = _zone_memory[key]
        age_hours = max(0.1, (time.time() - data["first_touch"]) / 3600)
        density = data["touch_count"] / age_hours
        if density > 5:
            penalty = 30
        elif density > 2:
            penalty = 15
        elif density > 1:
            penalty = 5
        else:
            penalty = 0
        reactions = data["reactions"]
        if len(reactions) >= 3 and len(set(reactions)) > 1:
            penalty += 15
        return min(50, penalty)
        
        
# ========== VALIDASI AREA (OB: volume AND (oi OR oi persistence); FVG: delta+volume+hold) ==========
def validate_ob_with_volume_oi(coin, ob_idx, master_candles) -> bool:
    try:
        candles = get_candles(coin, "1h", 100, master_candles)
        if ob_idx+1 >= len(candles):
            return False
        imp_candle = candles[ob_idx+1]
        imp_vol = float(imp_candle['v']) * float(imp_candle['c'])
        prev_vols = [float(candles[i]['v']) * float(candles[i]['c']) for i in range(max(0, ob_idx-5), ob_idx)]
        avg_vol = sum(prev_vols)/len(prev_vols) if prev_vols else 1
        volume_ok = (imp_vol / avg_vol) >= 1.5
        oi_persist, oi_trend = get_oi_persistence(coin)
        oi_ok = oi_persist
        oi_change = get_oi_roc(coin)
        oi_spike_ok = oi_change >= 3.0
        return volume_ok and (oi_persist or oi_spike_ok)
    except:
        return False

def validate_fvg_with_volume_reaction(coin, fvg_data: dict, master_candles) -> bool:
    try:
        if "idx" not in fvg_data:
            return True
        idx = fvg_data["idx"]
        candles = get_candles(coin, "1h", 100, master_candles)
        if idx+1 >= len(candles):
            return False
        imp_candle = candles[idx+1]
        imp_vol = float(imp_candle['v']) * float(imp_candle['c'])
        prev_vols = [float(candles[i]['v']) * float(candles[i]['c']) for i in range(max(0, idx-5), idx)]
        avg_vol = sum(prev_vols)/len(prev_vols) if prev_vols else 1
        volume_ok = (imp_vol / avg_vol) >= 1.5
        delta_shift = get_delta_shift(coin)
        time.sleep(0.1)
        delta_shift2 = get_delta_shift(coin)
        delta_persist = (delta_shift > 2 and delta_shift2 > 1) if fvg_data["type"] == "bullish" else (delta_shift < -2 and delta_shift2 < -1)
        reaction_ok = volume_ok and (abs(delta_shift) > 3 or delta_persist)
        return reaction_ok
    except:
        return True

# ========== EVENT ENGINE (ENHANCED) ==========
@dataclass
class TradeEvent:
    type: str
    price_low: float
    price_high: float
    strength: float
    direction: str
    extra: Dict = field(default_factory=dict)
    confidence: float = 0.0
    source_count: int = 1

def find_ob(candles, direction, current_price, max_dist_pct=2.0, master=None) -> Optional[TradeEvent]:
    for i in range(len(candles)-3, 1, -1):
        c = candles[i]
        o, cl = float(c['o']), float(c['c'])
        nxt = candles[i+1]
        no, nc = float(nxt['o']), float(nxt['c'])
        if direction == "LONG" and cl < o and nc > no and nc > float(c['h']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i+2, len(candles)-1):
                if float(candles[j]['c']) < ob_low:
                    fresh = False; break
            if fresh:
                mid = (ob_low+ob_high)/2
                dist = abs(mid-current_price)/current_price*100 if current_price>0 else 99
                if dist <= max_dist_pct:
                    if validate_ob_with_volume_oi(None, i, master):
                        conf = 60 + (10 if True else 0)
                        return TradeEvent("OB", ob_low, ob_high, 75, "LONG", {"idx": i}, confidence=conf, source_count=1)
        if direction == "SHORT" and cl > o and nc < no and nc < float(c['l']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i+2, len(candles)-1):
                if float(candles[j]['c']) > ob_high:
                    fresh = False; break
            if fresh:
                mid = (ob_low+ob_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    if validate_ob_with_volume_oi(None, i, master):
                        conf = 60
                        return TradeEvent("OB", ob_low, ob_high, 75, "SHORT", {"idx": i}, confidence=conf, source_count=1)
    return None

def find_fvg_advanced(candles, current_price, max_dist_pct=2.0, master=None) -> Optional[TradeEvent]:
    for i in range(len(candles)-1, 1, -1):
        c1 = candles[i-2]; c3 = candles[i]
        c1h, c1l = float(c1['h']), float(c1['l'])
        c3h, c3l = float(c3['h']), float(c3['l'])
        if c3l > c1h:
            gap_low, gap_high = c1h, c3l
            gap_pct = (gap_high - gap_low)/gap_low*100 if gap_low>0 else 0
            if gap_pct < 0.15: continue
            filled = 0.0
            for j in range(i+1, len(candles)-1):
                close = float(candles[j]['c'])
                if close <= gap_low:
                    filled = 1.0; break
                elif close < gap_high:
                    filled = max(filled, (close - gap_low)/(gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low+gap_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bullish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(None, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct>0.3 else 0) + (15 if filled<0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "LONG", {"fill_ratio": filled}, confidence=conf, source_count=1)
        if c3h < c1l:
            gap_low, gap_high = c3h, c1l
            gap_pct = (gap_high - gap_low)/gap_low*100
            if gap_pct < 0.15: continue
            filled = 0.0
            for j in range(i+1, len(candles)-1):
                close = float(candles[j]['c'])
                if close >= gap_high:
                    filled = 1.0; break
                elif close > gap_low:
                    filled = max(filled, (gap_high - close)/(gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low+gap_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bearish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(None, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct>0.3 else 0) + (15 if filled<0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "SHORT", {"fill_ratio": filled}, confidence=conf, source_count=1)
    return None

def find_sd_zone(candles, direction, current_price, max_dist_pct=2.0) -> Optional[TradeEvent]:
    for i in range(len(candles)-5, 1, -1):
        base = candles[i-3:i]
        imp = candles[i]
        base_low = min(float(c['l']) for c in base)
        base_high = max(float(c['h']) for c in base)
        imp_open, imp_close = float(imp['o']), float(imp['c'])
        if direction == "LONG" and imp_close > imp_open and imp_close > base_high:
            mid = (base_low+base_high)/2
            dist = abs(mid-current_price)/current_price*100
            if dist <= max_dist_pct:
                conf = 60 + (10 if len(base)>=3 else 0)
                return TradeEvent("SD", base_low, base_high, 75, "LONG", {"base_candles": len(base)}, confidence=conf, source_count=1)
        if direction == "SHORT" and imp_close < imp_open and imp_close < base_low:
            mid = (base_low+base_high)/2
            dist = abs(mid-current_price)/current_price*100
            if dist <= max_dist_pct:
                conf = 60 + (10 if len(base)>=3 else 0)
                return TradeEvent("SD", base_low, base_high, 75, "SHORT", {"base_candles": len(base)}, confidence=conf, source_count=1)
    return None

def find_liquidity_sweep(candles, current_price, vol_spike) -> Optional[TradeEvent]:
    highs, lows = detect_swing_points(candles, lookback=3)
    if highs and current_price >= highs[-1][1] * 0.998 and vol_spike > 1.5:
        conf = 70 + (10 if vol_spike>2 else 0)
        return TradeEvent("LIQUIDITY", highs[-1][1]*0.999, highs[-1][1]*1.001, 80, "SHORT", confidence=conf, source_count=1)
    if lows and current_price <= lows[-1][1] * 1.002 and vol_spike > 1.5:
        conf = 70 + (10 if vol_spike>2 else 0)
        return TradeEvent("LIQUIDITY", lows[-1][1]*0.999, lows[-1][1]*1.001, 80, "LONG", confidence=conf, source_count=1)
    return None

def collect_all_events(coin: str, current_price: float, master: Dict) -> List[TradeEvent]:
    candles_1h = get_candles(coin, "1h", 100, master)
    if not candles_1h:
        return []
    vol_spike = get_volume_spike(coin, master)
    events = []
    liq = find_liquidity_sweep(candles_1h, current_price, vol_spike)
    if liq: events.append(liq)
    ob_long = find_ob(candles_1h, "LONG", current_price, master=master)
    ob_short = find_ob(candles_1h, "SHORT", current_price, master=master)
    if ob_long: events.append(ob_long)
    if ob_short: events.append(ob_short)
    fvg = find_fvg_advanced(candles_1h, current_price, master=master)
    if fvg: events.append(fvg)
    sd_long = find_sd_zone(candles_1h, "LONG", current_price)
    sd_short = find_sd_zone(candles_1h, "SHORT", current_price)
    if sd_long: events.append(sd_long)
    if sd_short: events.append(sd_short)
    return events

def cluster_events(events: List[TradeEvent], price_tolerance=0.005) -> List[TradeEvent]:
    if not events:
        return []
    for e in events:
        e.mid = (e.price_low + e.price_high) / 2
    events.sort(key=lambda x: x.mid)
    clusters = []
    used = [False] * len(events)
    for i, e in enumerate(events):
        if used[i]:
            continue
        cluster = [e]
        used[i] = True
        for j in range(i+1, len(events)):
            if used[j]:
                continue
            if events[j].direction != e.direction:
                continue
            if max(e.price_low, events[j].price_low) <= min(e.price_high, events[j].price_high) * (1+price_tolerance):
                cluster.append(events[j])
                used[j] = True
        avg_strength = sum(ev.strength for ev in cluster) / len(cluster)
        low = min(ev.price_low for ev in cluster)
        high = max(ev.price_high for ev in cluster)
        avg_conf = sum(ev.confidence for ev in cluster) / len(cluster)
        cluster_event = TradeEvent(
            type="CLUSTER",
            price_low=low,
            price_high=high,
            strength=min(100, avg_strength + 10 * (len(cluster)-1)),
            direction=e.direction,
            extra={"members": [ev.type for ev in cluster], "count": len(cluster)},
            confidence=min(100, avg_conf + 5 * (len(cluster)-1)),
            source_count=len(cluster)
        )
        clusters.append(cluster_event)
    return clusters

# ========== NON‑ADDITIVE SCORING ==========
def score_event_non_additive(event: TradeEvent, current_price: float, delta: float,
                             vol_spike: float, oi_roc: float,
                             structure_valid: bool, cvd_accel: bool, momentum: int) -> Tuple[int, List[str]]:
    reasons = []
    evidence_count = 0
    if (event.direction == "LONG" and delta > 5) or (event.direction == "SHORT" and delta < -5):
        evidence_count += 1
        reasons.append("delta")
    oi_persist, oi_trend = get_oi_persistence(event.extra.get("coin", "BTC"))
    if oi_persist and ((event.direction == "LONG" and oi_trend == 1) or (event.direction == "SHORT" and oi_trend == -1)):
        evidence_count += 1
        reasons.append("oi_persistence")
    elif abs(oi_roc) > 5:
        evidence_count += 1
        reasons.append("oi_impulse")
    if evidence_count < 1:
        return 0, ["no_evidence"]
    base = event.strength
    mid = (event.price_low + event.price_high) / 2
    dist = abs(mid - current_price) / current_price * 100
    if dist < 0.3:
        base += 15
    elif dist < 0.6:
        base += 10
    elif dist < 1.0:
        base += 5
    if vol_spike >= 1.5:
        base += 15
        reasons.append("volume_spike")
    if cvd_accel or momentum >= 70:
        base += 10
        reasons.append("momentum")
    if structure_valid:
        base += 10
        reasons.append("structure")
    base = min(100, base + event.confidence * 0.1)
    return int(base), reasons
    

# ========== REJECTION, ACCEPTANCE, PERSISTENCE ==========
def rejection_confirmation_flow(coin: str, event: TradeEvent, current_price: float, master: Dict) -> Tuple[bool, str]:
    candles_5m = get_candles(coin, "5m", 15, master)
    if not candles_5m or len(candles_5m) < 5:
        return False, "insufficient data"
    touched_idx = None
    for i in range(max(0, len(candles_5m)-4), len(candles_5m)):
        c = candles_5m[i]
        low, high = float(c['l']), float(c['h'])
        if event.direction == "LONG":
            if low <= event.price_low * 1.002:
                touched_idx = i; break
        else:
            if high >= event.price_high * 0.998:
                touched_idx = i; break
    if touched_idx is None:
        return False, "area not touched"
    delta_shift = get_delta_shift(coin)
    vol_spike = get_volume_spike(coin, master)
    if event.direction == "LONG":
        is_rejection = (delta_shift > 3) or (vol_spike > 1.5)
        reason = "flow rejection" if is_rejection else "no rejection"
        return is_rejection, reason
    else:
        is_rejection = (delta_shift < -3) or (vol_spike > 1.5)
        reason = "flow rejection" if is_rejection else "no rejection"
        return is_rejection, reason

def acceptance_window_check(coin: str, event: TradeEvent, master: Dict) -> Tuple[bool, str]:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < ACCEPTANCE_WINDOW_CANDLES + 2:
        return False, "insufficient candles"
    last_touch_idx = None
    for i in range(len(candles_5m)-1, max(0, len(candles_5m)-10), -1):
        c = candles_5m[i]
        low, high = float(c['l']), float(c['h'])
        if event.direction == "LONG":
            if low <= event.price_low * 1.002:
                last_touch_idx = i
                break
        else:
            if high >= event.price_high * 0.998:
                last_touch_idx = i
                break
    if last_touch_idx is None:
        return False, "no touch found"
    if last_touch_idx + ACCEPTANCE_WINDOW_CANDLES >= len(candles_5m):
        return False, "window not complete"
    accepted = True
    for j in range(last_touch_idx+1, last_touch_idx+1+ACCEPTANCE_WINDOW_CANDLES):
        c = candles_5m[j]
        close = float(c['c'])
        if event.direction == "LONG":
            if close <= event.price_low * 1.01:
                accepted = False
                break
        else:
            if close >= event.price_high * 0.99:
                accepted = False
                break
    reaction = "accept" if accepted else "reject"
    update_zone_memory(coin, event.type, event.price_low, event.price_high, reaction)
    return accepted, "acceptance" if accepted else "fakeout"

def persistence_check(coin: str, event: TradeEvent, master: Dict) -> bool:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < 2:
        return False
    last_candle = candles_5m[-1]
    last_close = float(last_candle['c'])
    if event.direction == "LONG":
        outside = last_close > event.price_low * 1.005
    else:
        outside = last_close < event.price_high * 0.995
    if not outside:
        return False
    if len(candles_5m) >= 2:
        prev_candle = candles_5m[-2]
        prev_close = float(prev_candle['c'])
        if event.direction == "LONG":
            prev_outside = prev_close > event.price_low * 1.005
        else:
            prev_outside = prev_close < event.price_high * 0.995
        return prev_outside
    return True

# ========== INDEPENDENT EVIDENCE FAMILIES ==========
def get_independent_evidence_families(coin: str, direction: str, master: Dict) -> Tuple[bool, bool, bool, List[str]]:
    reasons = []
    structure_long, structure_short = get_structure_valid_separate(coin, master)
    momentum = get_composite_momentum(coin, master)
    price_ok = (direction == "LONG" and (structure_long or momentum >= 70)) or (direction == "SHORT" and (structure_short or momentum >= 70))
    if price_ok:
        reasons.append("price")
    delta_shift = get_delta_shift(coin)
    cvd_accel = get_cvd_acceleration(coin)
    flow_ok = (direction == "LONG" and (delta_shift > 3 or cvd_accel)) or (direction == "SHORT" and (delta_shift < -3 or cvd_accel))
    if flow_ok:
        reasons.append("flow")
    oi_roc = abs(get_oi_roc(coin))
    funding = get_funding_pct(coin)
    positioning_ok = (direction == "LONG" and (oi_roc > 5 or funding < -0.03)) or (direction == "SHORT" and (oi_roc > 5 or funding > 0.03))
    if positioning_ok:
        reasons.append("positioning")
    return price_ok, flow_ok, positioning_ok, reasons

# ========== EXHAUSTION, MOMENTUM ==========
def compute_exhaustion_score(coin: str, master: Dict) -> int:
    delta_shift = get_delta_shift(coin)
    vol_spike = get_volume_spike(coin, master)
    oi_roc = get_oi_roc(coin)
    candles = get_candles(coin, "5m", 10, master)
    if candles and len(candles) >= 2:
        price_now = float(candles[-1]['c'])
        price_5m_ago = float(candles[-2]['c'])
        price_roc = (price_now - price_5m_ago) / price_5m_ago * 100 if price_5m_ago else 0
    else:
        price_roc = 0
    exhaustion = 0
    if price_roc > 0.2:
        if delta_shift < 0: exhaustion += 30
        if vol_spike < 0.8: exhaustion += 20
        if oi_roc < -2: exhaustion += 20
    elif price_roc < -0.2:
        if delta_shift > 0: exhaustion += 30
        if vol_spike < 0.8: exhaustion += 20
        if oi_roc < -2: exhaustion += 20
    return min(100, exhaustion)

def get_composite_momentum(coin: str, master: Dict) -> int:
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 4:
        roc_score = 50
    else:
        close_now = float(candles[-1]['c'])
        close_5m = float(candles[-2]['c'])
        close_15m = float(candles[-4]['c'])
        roc5 = (close_now - close_5m) / close_5m * 100 if close_5m else 0
        roc15 = (close_now - close_15m) / close_15m * 100 if close_15m else 0
        if roc5 > 0.5 and roc5 > roc15: roc_score = 85
        elif roc5 > 0.2: roc_score = 70
        elif roc5 < -0.5 and roc5 < roc15: roc_score = 85
        elif roc5 < -0.2: roc_score = 70
        else: roc_score = 50
    vol_spike = get_volume_spike(coin, master)
    if vol_spike >= 2.0: vol_score = 90
    elif vol_spike >= 1.5: vol_score = 70
    elif vol_spike >= 1.2: vol_score = 50
    else: vol_score = 30
    delta_shift = get_delta_shift(coin)
    if delta_shift > 8: delta_score = 90
    elif delta_shift > 4: delta_score = 70
    elif delta_shift > 2: delta_score = 50
    else: delta_score = 30
    composite = int(roc_score * 0.3 + vol_score * 0.3 + delta_score * 0.4)
    return min(100, max(0, composite))

def get_oi_impulse_bool(coin: str) -> bool:
    return abs(get_oi_roc(coin)) > 5

def get_cvd_acceleration(coin: str) -> bool:
    cvd_30 = get_cvd(coin, 30)
    cvd_60 = get_cvd(coin, 60)
    return (cvd_30 - cvd_60) > 0.3

def get_dynamic_threshold(coin: str, market_regime: str, volatility_regime: str) -> int:
    session = get_session()
    th = 75
    if volatility_regime == "HIGH_VOLATILITY":
        th = int(th * 0.85)
    elif volatility_regime == "LOW_VOLATILITY":
        th = int(th * 1.1)
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        th = int(th * 0.95)
    if session == "ASIA":
        th = int(th * 1.1)
    btc_atr = get_atr_pct("BTC", 14, "1h")
    if btc_atr > 2.0:
        th = int(th * 0.9)
    agg_mult = get_coin_aggression_mult(coin)
    th = int(th / agg_mult)
    return max(50, min(85, th))

def get_dynamic_min_rr(market_regime: str) -> float:
    return {"TRENDING_UP": 2.0, "TRENDING_DOWN": 2.0, "RANGING": 1.8, "PANIC": 1.2}.get(market_regime, 1.5)

def get_confidence_label(score: int) -> str:
    if score >= 80: return "🔥 VERY STRONG"
    if score >= 70: return "🟢 STRONG"
    if score >= 60: return "🟡 MODERATE"
    return "⚪ WEAK"
    
    
# ========== SL/TP ADVANCED ==========
def get_nearest_liquidation(coin: str, mark: float, direction: str) -> Optional[float]:
    oi_usd = get_oi_usd(coin)
    if oi_usd < 5:
        return None
    if direction == "LONG":
        for lev in [25, 20, 10]:
            liq = mark * (1 + 0.99 / lev)
            if liq > mark:
                return liq
    else:
        for lev in [25, 20, 10]:
            liq = mark * (1 - 0.99 / lev)
            if liq < mark:
                return liq
    return None

def get_nearest_swing(coin: str, direction: str, current_price: float, master: Dict) -> Optional[float]:
    candles = get_candles(coin, "1h", 60, master)
    if not candles:
        return None
    highs, lows = detect_swing_points(candles, lookback=3)
    if direction == "LONG":
        candidates = [l[1] for l in lows if l[1] < current_price]
        if candidates:
            return max(candidates)
    else:
        candidates = [h[1] for h in highs if h[1] > current_price]
        if candidates:
            return min(candidates)
    return None

def calculate_sltp_advanced(coin: str, mark: float, direction: str, event: TradeEvent,
                            atr_pct: float, master: Dict) -> Tuple[float, float, float]:
    if direction == "LONG":
        sl_area = event.price_low * 0.995
        swing_sl = get_nearest_swing(coin, "LONG", mark, master)
        sl_swing = swing_sl * 0.998 if swing_sl else mark * (1 - atr_pct / 100 * 1.2)
        sl = min(sl_area, sl_swing)
        liq_tp = get_nearest_liquidation(coin, mark, "LONG")
        tp = liq_tp * 0.998 if liq_tp else mark + (mark - sl) * 2.0
    else:
        sl_area = event.price_high * 1.005
        swing_sl = get_nearest_swing(coin, "SHORT", mark, master)
        sl_swing = swing_sl * 1.002 if swing_sl else mark * (1 + atr_pct / 100 * 1.2)
        sl = max(sl_area, sl_swing)
        liq_tp = get_nearest_liquidation(coin, mark, "SHORT")
        tp = liq_tp * 1.002 if liq_tp else mark - (sl - mark) * 2.0
    risk = abs(mark - sl) / mark * 100
    reward = abs(tp - mark) / mark * 100
    rr = reward / risk if risk > 0 else 0
    return sl, tp, rr

# ========== ENTROPY WITH MAGNITUDE (v1.2) ==========
def compute_market_entropy(coin: str, master: Dict) -> int:
    """
    Menghitung entropy dengan mempertimbangkan flip frequency dan magnitude perubahan.
    Semakin tinggi entropy, semakin acak market.
    """
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 4:
        return 30
    closes = [float(c['c']) for c in candles[-5:]]
    price_changes = [abs(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]
    # Flip count
    price_flips = 0
    for i in range(2, len(closes)):
        if (closes[i] > closes[i-1]) != (closes[i-1] > closes[i-2]):
            price_flips += 1
    # Magnitude
    price_magnitude = sum(price_changes) / len(price_changes) if price_changes else 0
    # Delta
    delta_vals = [get_ob_delta(coin) for _ in range(4)]
    delta_changes = [abs(delta_vals[i] - delta_vals[i-1]) for i in range(1, len(delta_vals))]
    delta_flips = 0
    for i in range(2, len(delta_vals)):
        if (delta_vals[i] > 0) != (delta_vals[i-1] > 0):
            delta_flips += 1
    delta_magnitude = sum(delta_changes) / len(delta_changes) if delta_changes else 0
    # OI
    oi_vals = [get_oi_roc(coin) for _ in range(4)]
    oi_changes = [abs(oi_vals[i] - oi_vals[i-1]) for i in range(1, len(oi_vals))]
    oi_flips = 0
    for i in range(2, len(oi_vals)):
        if (oi_vals[i] > 0) != (oi_vals[i-1] > 0):
            oi_flips += 1
    oi_magnitude = sum(oi_changes) / len(oi_changes) if oi_changes else 0
    # Komposit flip score (max 50)
    flip_score = min(50, (price_flips + delta_flips + oi_flips) * 12)
    # Komposit magnitude score (max 50)
    magnitude_score = min(50, price_magnitude * 15 + delta_magnitude * 5 + oi_magnitude * 5)
    entropy = flip_score + magnitude_score
    return min(100, max(0, int(entropy)))

def get_dynamic_entropy_threshold(volatility_regime: str, trend_strength: float) -> int:
    base = ENTROPY_BASE
    if volatility_regime == "HIGH_VOLATILITY":
        base += int(ENTROPY_VOLATILITY_FACTOR * 20)
    elif volatility_regime == "LOW_VOLATILITY":
        base -= int(ENTROPY_VOLATILITY_FACTOR * 15)
    base += int((trend_strength / 100) * ENTROPY_TREND_STRENGTH_FACTOR * 50)
    return max(40, min(85, base))

def compute_trend_strength(coin: str, master: Dict) -> float:
    candles = get_candles(coin, "1h", 50, master)
    if not candles or len(candles) < 21:
        return 50.0
    closes = [float(c['c']) for c in candles]
    ema8 = np.mean(closes[-8:])
    ema21 = np.mean(closes[-21:])
    slope = (ema8 - ema21) / ema21 * 100 if ema21 != 0 else 0
    strength = min(100, max(0, (abs(slope) / 2) * 100))
    return strength

def get_entropy_adjusted_min_rr(base_rr: float, entropy: int) -> float:
    factor = 1.0 + (entropy / 100) * ENTROPY_RR_FACTOR
    return base_rr * factor

def get_entropy_adjusted_threshold(base_threshold: int, entropy: int) -> int:
    factor = 1.0 + (entropy / 100) * ENTROPY_THRESHOLD_FACTOR
    new_th = int(base_threshold * factor)
    return max(50, min(85, new_th))

def get_entropy_adjusted_aggression(agg_mult: float, entropy: int) -> float:
    factor = 1.0 - (entropy / 100) * 0.3
    return max(0.5, agg_mult * factor)

# ========== DYNAMIC EVALUATION HORIZON ==========
def get_evaluation_delay(atr_pct: float, rr: float, regime: str) -> int:
    """
    Dynamic delay berdasarkan ATR, RR, market regime.
    Scalp (volatile, RR kecil) -> cepat. Swing (trend, RR besar) -> lambat.
    """
    base = BASE_EVALUATION_DELAY
    # ATR adjustment: semakin volatile semakin cepat evaluasi
    if atr_pct > 2.0:
        base = int(base * 0.6)
    elif atr_pct > 1.2:
        base = int(base * 0.8)
    # RR adjustment: RR besar butuh waktu lebih lama
    if rr > 2.5:
        base = int(base * 1.2)
    elif rr < 1.8:
        base = int(base * 0.8)
    # Regime adjustment
    if regime in ("PANIC", "VOLATILE"):
        base = int(base * 0.7)
    elif regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base = int(base * 1.1)
    return max(1800, min(14400, base))  # antara 30 menit - 4 jam

# ========== EXPLAIN DECISION DENGAN CONTRIBUTION ==========
def explain_decision_with_contribution(coin: str, direction: str, score: int,
                                       positive_factors: List[str], negative_factors: List[str],
                                       contributions: Dict[str, int],
                                       entropy: int, threshold: int, data_confidence: int) -> str:
    pos_str = ", ".join(positive_factors[:3]) if positive_factors else "none"
    neg_str = ", ".join(negative_factors[:3]) if negative_factors else "none"
    # Format contributions
    contrib_str = " | ".join([f"{k}:{v:+d}" for k, v in contributions.items()]) if contributions else "none"
    explain = (f"📊 *Decision Explanation* for {coin} {direction}\n"
               f"━━━━━━━━━━━━━━━━━━━━━━\n"
               f"✅ Positive: {pos_str}\n"
               f"❌ Negative: {neg_str}\n"
               f"📈 Contribution: {contrib_str}\n"
               f"🌀 Entropy: {entropy} (thr {threshold})\n"
               f"📡 Data confidence: {data_confidence}%\n"
               f"🎯 Final score: {score}\n")
    return explain

# ========== COUNTERFACTUAL INFLUENCE ==========
def evaluate_counterfactual_influence(coin: str, entropy: int, evidence_families: int,
                                      exhaustion: int, original_score: int, data_confidence: int) -> Tuple[int, Dict[str, int]]:
    adjustments = {}
    if entropy > 70:
        adjustments["entropy"] = -15
    elif entropy > 50:
        adjustments["entropy"] = -5
    if evidence_families < 2:
        adjustments["evidence"] = -20
    elif evidence_families == 2:
        adjustments["evidence"] = 0
    else:
        adjustments["evidence"] = +10
    if exhaustion > 50:
        adjustments["exhaustion"] = -20
    elif exhaustion > 30:
        adjustments["exhaustion"] = -10
    if data_confidence < 60:
        adjustments["data_quality"] = -30
    elif data_confidence < 80:
        adjustments["data_quality"] = -10
    total_adj = sum(adjustments.values())
    adjusted_score = max(0, min(100, original_score + total_adj))
    return adjusted_score, adjustments

def log_counterfactual(coin, original_score, adjustments):
    for mod, delta in adjustments.items():
        new_score = original_score + delta
        reason = f"If {mod} were adjusted, score would be {new_score} (delta {delta:+d})"
        add_counterfactual(coin, original_score, mod, new_score, reason)
        logger.debug(f"[COUNTERFACTUAL] {coin} {mod}: {original_score} -> {new_score}")

# ========== DECISION VECTOR ==========
def compute_decision_vector(coin: str, best_event: TradeEvent, score_long: int, score_short: int,
                            evidence_families: int, entropy: int, exhaustion: int,
                            market_regime: str, volatility_regime: str, data_confidence: int) -> Tuple[int, float, str, Dict[str, int]]:
    if evidence_families >= 3:
        ev_mult = EVIDENCE_MULT_3
    elif evidence_families >= 2:
        ev_mult = EVIDENCE_MULT_2
    else:
        ev_mult = EVIDENCE_MULT_1
    raw_score = score_long if best_event.direction == "LONG" else score_short
    contradiction = (score_long > 55 and score_short > 55)
    contra_penalty = 40 if contradiction else 0
    exhaustion_penalty = min(50, exhaustion)
    quality_penalty = max(0, (100 - data_confidence) * 0.2)
    tmp_score = raw_score * ev_mult - contra_penalty - exhaustion_penalty - quality_penalty
    tmp_score = max(0, min(100, int(tmp_score)))
    reason_extra = f"ev_mult={ev_mult:.1f} contra={contra_penalty} exh={exhaustion_penalty} dq={quality_penalty:.0f}"
    # Hitung kontribusi per komponen untuk explainability
    contributions = {
        "evidence": int(raw_score * (ev_mult - 1)),
        "contra": -contra_penalty if contradiction else 0,
        "exhaust": -exhaustion_penalty,
        "data": -int(quality_penalty)
    }
    return tmp_score, ev_mult, reason_extra, contributions
    
    
# ========== ENTRY ALERT CORE (v1.2 dengan Hypothesis Engine) ==========
def check_entry_alert(coin: str, mark: float, master_candles: Dict) -> Optional[dict]:
    start_time = time.time()
    api_start = time.time()
    current_time = time.time()
    
    # Data confidence (freshness + integrity)
    data_confidence, ages = get_data_confidence(coin, mark, current_time)
    if data_confidence < MIN_DATA_CONFIDENCE:
        logger.debug(f"Data confidence too low for {coin}: {data_confidence}% -> skip")
        return None
    
    try:
        atr_pct = get_atr_pct(coin, 14, "1h", master_candles)
        vol_spike = get_volume_spike(coin, master_candles)
        delta = get_ob_delta(coin)
        cvd_accel = get_cvd_acceleration(coin)
        oi_impulse = get_oi_impulse_bool(coin)
        momentum = get_composite_momentum(coin, master_candles)
        structure_valid_long, structure_valid_short = get_structure_valid_separate(coin, master_candles)
        candles_1h = get_candles(coin, "1h", 60, master_candles)
        market_state = get_market_state_from_structure(candles_1h, mark) if candles_1h else MarketState.UNKNOWN
        market_regime = get_market_regime()
        volatility_regime = get_volatility_regime()
        flow_regime = get_flow_regime()
        
        # Kumpulkan events
        raw_events = collect_all_events(coin, mark, master_candles)
        if not raw_events:
            return None
        
        clustered = cluster_events(raw_events, price_tolerance=0.005)
        oi_roc = get_oi_roc(coin)
        update_oi_persistence(coin, oi_roc)
        
        for ev in clustered:
            ev.extra["coin"] = coin
            ev.score, _ = score_event_non_additive(
                ev, mark, delta, vol_spike, oi_roc,
                (structure_valid_long if ev.direction == "LONG" else structure_valid_short),
                cvd_accel, momentum
            )
            penalty = get_zone_penalty(coin, ev.type, ev.price_low, ev.price_high)
            ev.score = max(0, ev.score - penalty)
        
        best_event = max(clustered, key=lambda e: e.score)
        if not best_event:
            return None
        
        # Market state filter
        if market_state == MarketState.REVERSAL:
            if best_event.type != "LIQUIDITY" and "LIQUIDITY" not in best_event.extra.get("members", []):
                return None
        elif market_state == MarketState.EXPANSION:
            if best_event.type == "LIQUIDITY" or "LIQUIDITY" in best_event.extra.get("members", []):
                return None
        
        # Rejection, acceptance, persistence
        reject_ok, reject_reason = rejection_confirmation_flow(coin, best_event, mark, master_candles)
        if not reject_ok:
            return None
        accept_ok, accept_reason = acceptance_window_check(coin, best_event, master_candles)
        if not accept_ok:
            return None
        persist_ok = persistence_check(coin, best_event, master_candles)
        if not persist_ok:
            return None
        
        # Hitung skor LONG dan SHORT
        score_long = 0
        score_short = 0
        if best_event.direction == "LONG":
            score_long = best_event.score
            short_events = [e for e in clustered if e.direction == "SHORT"]
            score_short = max([e.score for e in short_events]) if short_events else 0
        else:
            score_short = best_event.score
            long_events = [e for e in clustered if e.direction == "LONG"]
            score_long = max([e.score for e in long_events]) if long_events else 0
        
        # Independent evidence families
        price_ok, flow_ok, pos_ok, evidence_reasons = get_independent_evidence_families(
            coin, best_event.direction, master_candles
        )
        evidence_families = (1 if price_ok else 0) + (1 if flow_ok else 0) + (1 if pos_ok else 0)
        exhaustion = compute_exhaustion_score(coin, master_candles)
        entropy = compute_market_entropy(coin, master_candles)
        trend_strength = compute_trend_strength(coin, master_candles)
        entropy_threshold = get_dynamic_entropy_threshold(volatility_regime, trend_strength)
        
        # Decision Vector
        decision_score, ev_mult, vec_reason, contributions = compute_decision_vector(
            coin, best_event, score_long, score_short, evidence_families, entropy, exhaustion,
            market_regime, volatility_regime, data_confidence
        )
        
        # Counterfactual
        cf_adjusted_score, cf_adjustments = evaluate_counterfactual_influence(
            coin, entropy, evidence_families, exhaustion, decision_score, data_confidence
        )
        log_counterfactual(coin, decision_score, cf_adjustments)
        final_score = decision_score
        
        # Dynamic threshold
        base_threshold = get_dynamic_threshold(coin, market_regime, volatility_regime)
        final_threshold = get_entropy_adjusted_threshold(base_threshold, entropy)
        
        # UNCLEAR check
        if score_long > UNCLEAR_THRESHOLD and score_short > UNCLEAR_THRESHOLD and abs(score_long - score_short) < UNCLEAR_DIFF:
            neg_evidence = "Uncertain (LONG/SHORT both high)"
            add_journal_entry(coin, market_regime, volatility_regime, flow_regime,
                              score_long, score_short, "NO_TRADE", final_score,
                              "Uncertain market", neg_evidence, entropy,
                              int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                              data_confidence, False)
            return None
        
        # Threshold check
        if final_score < final_threshold:
            if final_score > 70:
                add_journal_entry(coin, market_regime, volatility_regime, flow_regime,
                                  score_long, score_short, "NO_TRADE", final_score,
                                  f"Below threshold {final_threshold}", "", entropy,
                                  int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                                  data_confidence, False)
            return None
        
        # Hitung SL/TP
        sl, tp, rr = calculate_sltp_advanced(coin, mark, best_event.direction, best_event, atr_pct, master_candles)
        min_rr = get_dynamic_min_rr(market_regime)
        min_rr = get_entropy_adjusted_min_rr(min_rr, entropy)
        if rr < min_rr:
            return None
        
        # ========== HYPOTHESIS ENGINE ==========
        # Thesis berdasarkan event type
        if best_event.type == "LIQUIDITY":
            thesis = f"Liquidity sweep {best_event.direction.lower()}"
            invalidate = "OI collapse or price reclaims sweep level"
            observe = f"Delta sustain >5 for 3 candles"
        elif best_event.type == "OB":
            thesis = f"Order block {best_event.direction.lower()} with volume+OI validation"
            invalidate = "OB level breached with high volume"
            observe = "CVD acceleration continues"
        elif best_event.type == "FVG":
            thesis = f"Fair value gap {best_event.direction.lower()} with reaction"
            invalidate = "FVG fully filled (>70%)"
            observe = "Price holds beyond FVG"
        elif best_event.type == "SD":
            thesis = f"Supply/demand zone {best_event.direction.lower()}"
            invalidate = "Zone breached with volume"
            observe = "Rejection holds"
        else:
            thesis = f"{best_event.type} {best_event.direction.lower()} setup"
            invalidate = "Invalidation level breached"
            observe = "Price action confirms"
        
        # Shadow decision
        shadow_id = generate_signal_id(coin, best_event.direction)
        add_shadow_decision(shadow_id, coin, best_event.direction, mark, sl, tp)
        
        # Negative evidence
        negative_reasons = []
        if not price_ok:
            negative_reasons.append("price")
        if not flow_ok:
            negative_reasons.append("flow")
        if not pos_ok:
            negative_reasons.append("positioning")
        negative_str = ", ".join(negative_reasons) if negative_reasons else "none"
        
        reason = (f"{best_event.type} (members: {best_event.extra.get('members', [])}) | "
                  f"Rej:{reject_reason} Acc:{accept_reason} | "
                  f"Evidence: {evidence_reasons} | Entropy:{entropy}/{entropy_threshold} | {vec_reason} | Score:{final_score}")
        
        signal_id = generate_signal_id(coin, best_event.direction)
        
        # Dynamic evaluation delay
        eval_delay = get_evaluation_delay(atr_pct, rr, market_regime)
        
        if not PAPER_MODE:
            save_signal(signal_id, coin, best_event.direction, final_score, mark, sl, tp, rr, reason,
                       data_confidence, thesis, invalidate, observe)
            add_journal_entry(coin, market_regime, volatility_regime, flow_regime,
                              score_long, score_short, best_event.direction, final_score,
                              reason, negative_str, entropy,
                              int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                              data_confidence, True, contribution=str(contributions))
            threading.Thread(target=evaluate_signal, args=(
                signal_id, coin, best_event.direction, mark, sl, tp, data_confidence,
                entropy, evidence_families, exhaustion, thesis, invalidate, observe, eval_delay
            ), daemon=True).start()
        else:
            add_journal_entry(coin, market_regime, volatility_regime, flow_regime,
                              score_long, score_short, best_event.direction, final_score,
                              reason, negative_str, entropy,
                              int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                              data_confidence, True, contribution=str(contributions))
        
        update_active_candidate(coin, mark, entropy, mark)
        
        positive_factors = [best_event.type] + evidence_reasons
        if vol_spike >= 1.5:
            positive_factors.append("volume")
        if cvd_accel:
            positive_factors.append("cvd_accel")
        
        explanation = explain_decision_with_contribution(
            coin, best_event.direction, final_score,
            positive_factors, negative_reasons, contributions,
            entropy, final_threshold, data_confidence
        )
        
        return {
            "coin": coin,
            "direction": best_event.direction,
            "score": final_score,
            "entry": mark,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "reason": reason,
            "area": best_event.type,
            "label": get_confidence_label(final_score),
            "contradiction": (score_long > 55 and score_short > 55),
            "exhaustion": exhaustion,
            "entropy": entropy,
            "evidence_families": evidence_families,
            "positive_evidence": evidence_reasons,
            "negative_evidence": negative_str,
            "data_confidence": data_confidence,
            "contributions": contributions,
            "hypothesis": {"thesis": thesis, "invalidate": invalidate, "observe": observe},
            "explanation": explanation
        }
    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None


# ========== EVALUASI SINYAL (dengan hypothesis validation & dynamic delay) ==========
def evaluate_signal(signal_id, coin, direction, entry, sl, tp, data_confidence,
                    entropy, evidence_families, exhaustion, thesis, invalidate, observe, eval_delay):
    """Evaluasi sinyal setelah dynamic delay dengan MFE/MAE dan hypothesis validation"""
    time.sleep(eval_delay)
    if _shutdown_event.is_set():
        return
    try:
        # Ambil candles untuk MFE/MAE
        candles = get_candles(coin, "5m", 100)
        if not candles:
            return
        entry_time = int(time.time() - eval_delay)
        high_prices = []
        low_prices = []
        for c in candles:
            ts = c.get('t', 0)
            if ts >= entry_time * 1000:
                high_prices.append(float(c['h']))
                low_prices.append(float(c['l']))
        if high_prices and low_prices:
            if direction == "LONG":
                mfe = (max(high_prices) - entry) / entry * 100
                mae = (min(low_prices) - entry) / entry * 100
            else:
                mfe = (entry - min(low_prices)) / entry * 100
                mae = (entry - max(high_prices)) / entry * 100
        else:
            mfe, mae = 0, 0
        
        # Harga saat evaluasi
        mids = info.all_mids()
        price = float(mids.get(coin, 0))
        if price == 0:
            return
        
        # Hitung outcome
        if direction == "LONG":
            if price >= tp:
                outcome, pnl = "TP_HIT", (tp - entry) / entry * 100
            elif price <= sl:
                outcome, pnl = "SL_HIT", (sl - entry) / entry * 100
            else:
                outcome, pnl = "PARTIAL", (price - entry) / entry * 100
        else:
            if price <= tp:
                outcome, pnl = "TP_HIT", (entry - tp) / entry * 100
            elif price >= sl:
                outcome, pnl = "SL_HIT", (entry - sl) / entry * 100
            else:
                outcome, pnl = "PARTIAL", (entry - price) / entry * 100
        
        # Hypothesis validation
        is_win = outcome in ("TP_HIT", "PARTIAL_WIN")
        hypothesis_validated = is_win
        # Jika thesis terpenuhi (sederhana: profit atau MFE > 2x MAE)
        if mfe > abs(mae) * 1.5:
            hypothesis_validated = True
        
        update_signal_outcome(signal_id, outcome, pnl, price, mfe, mae, hypothesis_validated)
        add_hypothesis_validation(signal_id, thesis, outcome, pnl, hypothesis_validated)
        
        # Module attribution
        apply_module_credits(coin, pnl, mfe, mae, entropy, evidence_families, exhaustion, data_confidence)
        
        logger.info(f"Evaluated {signal_id}: {outcome} pnl={pnl:.2f}% mfe={mfe:.2f}% mae={mae:.2f}% hypothesis_validated={hypothesis_validated}")
        decay_coin_memories()
    except Exception as e:
        logger.error(f"Eval error {signal_id}: {e}")
        
        
# ========== ENGINE LOOPS ==========
def state_engine_update():
    """Update data berat: candle, structure, area - dipanggil setiap STATE_ENGINE_INTERVAL detik"""
    update_mids_cache()
    try:
        meta = info.meta_and_asset_ctxs()
        coins_vol = []
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            vol = float(ctx.get("dayNtlVlm", 0))
            if vol > 5_000_000:
                coins_vol.append((asset["name"], vol))
        coins_vol.sort(key=lambda x: x[1], reverse=True)
        top_coins = [c[0] for c in coins_vol[:20]]
    except Exception as e:
        logger.error(f"State engine top coins error: {e}")
        top_coins = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]
    
    master_candles = fetch_candles_master(top_coins, "1h", 100)
    mids = info.all_mids()
    alerts = []
    
    for coin in top_coins:
        mark = float(mids.get(coin, 0))
        if mark == 0 or coin not in master_candles:
            continue
        alert = check_entry_alert(coin, mark, master_candles)
        if alert and not PAPER_MODE:
            alerts.append(alert)
        elif alert and PAPER_MODE:
            logger.info(f"[PAPER] {alert['coin']} {alert['direction']} score={alert['score']}")
        time.sleep(0.05)
    
    for alert in alerts:
        send_alert(alert)


def trigger_engine_update():
    """Update data cepat: rolling delta, OI, volume - dipanggil setiap TRIGGER_ENGINE_INTERVAL_ACTIVE detik"""
    update_mids_cache()
    try:
        meta = info.meta_and_asset_ctxs()
        coins_vol = []
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            vol = float(ctx.get("dayNtlVlm", 0))
            if vol > 5_000_000:
                coins_vol.append((asset["name"], vol))
        coins_vol.sort(key=lambda x: x[1], reverse=True)
        all_top = [c[0] for c in coins_vol[:20]]
    except:
        all_top = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]
    
    with _active_candidates_lock:
        active = list(_active_candidates.keys())
    
    # Active candidates diprioritaskan
    for coin in active:
        if coin in all_top:
            update_rolling_delta(coin)
            get_oi_roc(coin)
            get_volume_spike(coin)
        time.sleep(0.02)
    
    # Background coins update lebih lambat
    for coin in all_top:
        if coin in active:
            continue
        update_rolling_delta(coin)
        get_oi_roc(coin)
        get_volume_spike(coin)
        time.sleep(0.02)


def scheduled_state_engine():
    """Loop untuk state engine dengan interval adaptif"""
    while not _shutdown_event.is_set():
        with _alert_enabled_lock:
            if not _alert_enabled:
                time.sleep(60)
                continue
        state_engine_update()
        vol_reg = get_volatility_regime()
        interval = STATE_ENGINE_INTERVAL
        if vol_reg == "HIGH_VOLATILITY":
            interval = max(15, interval // 2)
        elif vol_reg == "LOW_VOLATILITY":
            interval = min(60, interval * 2)
        logger.info(f"State engine cycle done, next in {interval}s")
        time.sleep(interval)


def scheduled_trigger_engine():
    """Loop untuk trigger engine (update data cepat)"""
    while not _shutdown_event.is_set():
        trigger_engine_update()
        time.sleep(TRIGGER_ENGINE_INTERVAL_ACTIVE)


def scheduled_shadow_evaluation():
    """Evaluasi shadow decisions (missed opportunities) setiap jam"""
    while not _shutdown_event.is_set():
        with _shadow_lock:
            now = time.time()
            for sid, shadow in list(_shadow_decisions.items()):
                if not shadow["evaluated"] and now - shadow["timestamp"] > EVALUATION_DELAY:
                    try:
                        coin = shadow["coin"]
                        entry = shadow["entry"]
                        sl = shadow["sl"]
                        tp = shadow["tp"]
                        direction = shadow["direction"]
                        candles = get_candles(coin, "5m", 100)
                        if not candles:
                            continue
                        entry_time = int(shadow["timestamp"])
                        high_prices = []
                        low_prices = []
                        for c in candles:
                            ts = c.get('t', 0)
                            if ts >= entry_time * 1000:
                                high_prices.append(float(c['h']))
                                low_prices.append(float(c['l']))
                        if high_prices and low_prices:
                            if direction == "LONG":
                                mfe = (max(high_prices) - entry) / entry * 100
                                mae = (min(low_prices) - entry) / entry * 100
                            else:
                                mfe = (entry - min(low_prices)) / entry * 100
                                mae = (entry - max(high_prices)) / entry * 100
                        else:
                            mfe, mae = 0, 0
                        mids = info.all_mids()
                        price = float(mids.get(coin, 0))
                        if price == 0:
                            continue
                        if direction == "LONG":
                            if price >= tp:
                                outcome, pnl = "TP_HIT", (tp - entry) / entry * 100
                            elif price <= sl:
                                outcome, pnl = "SL_HIT", (sl - entry) / entry * 100
                            else:
                                outcome, pnl = "PARTIAL", (price - entry) / entry * 100
                        else:
                            if price <= tp:
                                outcome, pnl = "TP_HIT", (entry - tp) / entry * 100
                            elif price >= sl:
                                outcome, pnl = "SL_HIT", (entry - sl) / entry * 100
                            else:
                                outcome, pnl = "PARTIAL", (entry - price) / entry * 100
                        update_shadow_outcome(sid, outcome, pnl, mfe, mae)
                        logger.info(f"Shadow {sid}: {outcome} pnl={pnl:.2f}% mfe={mfe:.2f}% mae={mae:.2f}%")
                    except Exception as e:
                        logger.error(f"Shadow eval error {sid}: {e}")
        time.sleep(3600)


def scheduled_cleanup():
    """Cleanup expired data setiap 10 menit"""
    while not _shutdown_event.is_set():
        cleanup_active_candidates()
        cleanup_old_shadow_decisions()
        time.sleep(600)


# ========== TELEGRAM BOT ==========
bot = telebot.TeleBot(TOKEN)


def send_alert(alert: dict):
    """Kirim alert ke Telegram"""
    with _alert_enabled_lock:
        if not _alert_enabled:
            return
    coin = alert["coin"]
    now = time.time()
    with _last_alert_lock:
        if coin in _last_alert and now - _last_alert[coin] < COOLDOWN_ENTRY:
            return
        _last_alert[coin] = now
    
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    contra_warn = "⚠️ CONTRADICTION DETECTED\n" if alert.get("contradiction") else ""
    exhaust_warn = f"💨 Exhaustion: {alert.get('exhaustion', 0)}%\n" if alert.get('exhaustion', 0) > 30 else ""
    entropy_warn = f"🌀 Entropy: {alert.get('entropy', 0)}\n"
    dq_warn = f"📡 Data confidence: {alert.get('data_confidence', 0)}%\n"
    evidence_warn = f"🔍 Evidence families: {alert.get('evidence_families', 0)}/3 ({', '.join(alert.get('positive_evidence', []))})\n"
    neg_evidence_warn = f"❌ Negative: {alert.get('negative_evidence', 'none')}\n"
    contrib = alert.get('contributions', {})
    contrib_str = f"📈 Contribution: {contrib.get('evidence', 0):+d} | contra:{contrib.get('contra', 0):+d} | exh:{contrib.get('exhaust', 0):+d} | data:{contrib.get('data', 0):+d}\n"
    
    text = (
        f"{arrow} *ENTRY ALERT* • {coin}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{contra_warn}{exhaust_warn}{entropy_warn}{dq_warn}{evidence_warn}{neg_evidence_warn}{contrib_str}"
        f"📡 {alert['direction']} | {alert['label']} ({alert['score']})\n"
        f"💰 Entry: {fmt_price(alert['entry'])}\n"
        f"🛑 SL: {fmt_price(alert['sl'])} ({abs(alert['entry'] - alert['sl']) / alert['entry'] * 100:.2f}%)\n"
        f"✅ TP: {fmt_price(alert['tp'])} ({abs(alert['tp'] - alert['entry']) / alert['entry'] * 100:.2f}%)\n"
        f"⚓ RR: 1:{alert['rr']:.1f}\n"
        f"💡 {alert['reason']}\n"
        f"{alert.get('explanation', '')}\n"
        f"🎯 /entry {coin}"
    )
    try:
        bot.send_message(USER_ID, text, parse_mode='Markdown')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Send alert error: {e}")


# ========== COMMAND HANDLERS ==========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    bot.reply_to(m, f"🧠 Smart Entry Engine v1.2 (Hypothesis + Integrity)\n⏰ {get_wib()}\n📡 Market: {get_market_regime()} | Volatility: {get_volatility_regime()} | Flow: {get_flow_regime()}\n✅ /status /entry BTC /warroom BTC /analytics /journal /counterfactual /hypothesis")


@bot.message_handler(commands=['journal'])
def cmd_journal(m):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT timestamp, coin, market_regime, long_score, short_score, direction, final_score, 
                       negative_evidence, entropy, decision_time_ms, data_confidence, executed, missed_opportunity_pnl, contribution
                 FROM journal ORDER BY timestamp DESC LIMIT 20''')
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.reply_to(m, "Belum ada data journal.")
        return
    teks = "📜 *DECISION JOURNAL* (20 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for ts, coin, mreg, ls, ss, dirn, fs, neg_ev, entropy, dt_ms, dq, exec_flag, missed, contrib in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
        exec_mark = "✅" if exec_flag else "❌"
        neg_str = f" [-{neg_ev}]" if neg_ev and neg_ev != 'none' else ""
        missed_str = f" 💔missed:{missed:.1f}%" if missed else ""
        contrib_str = f" [{contrib[:50]}]" if contrib else ""
        teks += f"{dt} {coin} [{mreg}] L:{ls} S:{ss} → {dirn}{neg_str} (entropy {entropy} dq {dq} {dt_ms}ms) {exec_mark} {missed_str}{contrib_str} (score {fs})\n"
    bot.reply_to(m, teks, parse_mode='Markdown')


@bot.message_handler(commands=['counterfactual'])
def cmd_counterfactual(m):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT timestamp, coin, original_score, modified_module, modified_score FROM counterfactual ORDER BY timestamp DESC LIMIT 15''')
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.reply_to(m, "Belum ada data counterfactual.")
        return
    teks = "🔮 *COUNTERFACTUAL SIMULATIONS* (15 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for ts, coin, orig, mod, new_score in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
        teks += f"{dt} {coin} | {mod}: {orig} → {new_score} (Δ{new_score - orig:+d})\n"
    bot.reply_to(m, teks, parse_mode='Markdown')


@bot.message_handler(commands=['hypothesis'])
def cmd_hypothesis(m):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT signal_id, thesis, outcome, pnl, validated FROM hypothesis_validation ORDER BY id DESC LIMIT 10''')
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.reply_to(m, "Belum ada data hypothesis validation.")
        return
    teks = "🧪 *HYPOTHESIS VALIDATION* (10 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for sid, thesis, outcome, pnl, validated in rows:
        valid_mark = "✅" if validated else "❌"
        sid_short = sid[-12:] if len(sid) > 12 else sid
        teks += f"{valid_mark} {sid_short} | {outcome} ({pnl:+.1f}%)\n   📌 {thesis[:60]}\n\n"
    bot.reply_to(m, teks, parse_mode='Markdown')


@bot.message_handler(commands=['status'])
def cmd_status(m):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM signals WHERE timestamp > ?", (int(time.time()) - 86400,))
    today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE executed=1 AND timestamp > ?", (int(time.time()) - 86400,))
    alerts_sent = c.fetchone()[0]
    conn.close()
    agg = get_coin_aggression_mult("BTC")
    bot.reply_to(m, f"📊 Status\n⏰ {get_wib()}\nMarket: {get_market_regime()} | Volatility: {get_volatility_regime()} | Flow: {get_flow_regime()}\nAlert: {'ON' if _alert_enabled else 'OFF'}\nAlert hari ini: {alerts_sent}\nTotal sinyal: {today}\nPaper: {'YES' if PAPER_MODE else 'NO'}\nAggression: {agg:.2f}x")


@bot.message_handler(commands=['analytics'])
def cmd_analytics(m):
    stats = get_analytics()
    if stats["total"] == 0:
        bot.reply_to(m, "Belum ada sinyal dievaluasi.")
    else:
        bot.reply_to(m, f"📈 PERFORMANCE\nTotal: {stats['total']}\nWin: {stats['wins']} Loss: {stats['losses']}\nWin Rate: {stats['win_rate']}%\nAvg RR: {stats['avg_rr']}\nTotal PnL: {stats['total_pnl']:+.2f}%")


@bot.message_handler(commands=['entry'])
def cmd_entry(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Format: /entry BTC")
        return
    coin = parts[1].upper()
    try:
        meta = info.meta_and_asset_ctxs()
        mark = 0.0
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            if asset["name"] == coin:
                mark = float(ctx.get("markPx", 0))
                break
        if mark == 0:
            bot.reply_to(m, f"❌ {coin} not found")
            return
        master = {coin: get_candles(coin, "1h", 100)}
        alert = check_entry_alert(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No setup for {coin}")
            return
        contrib = alert.get('contributions', {})
        text = (f"🎯 *Entry {coin}*\n{alert['direction']} | {alert['label']} ({alert['score']})\n"
                f"Entry: {fmt_price(alert['entry'])}\nSL: {fmt_price(alert['sl'])} ({abs(alert['entry'] - alert['sl']) / alert['entry'] * 100:.2f}%)\n"
                f"TP: {fmt_price(alert['tp'])} ({abs(alert['tp'] - alert['entry']) / alert['entry'] * 100:.2f}%)\nRR: 1:{alert['rr']:.1f}\n"
                f"🔍 Positive: {', '.join(alert.get('positive_evidence', []))}\n"
                f"❌ Negative: {alert.get('negative_evidence', 'none')}\n"
                f"📈 Contribution: ev:{contrib.get('evidence', 0):+d} contra:{contrib.get('contra', 0):+d} exh:{contrib.get('exhaust', 0):+d} data:{contrib.get('data', 0):+d}\n"
                f"🌀 Entropy: {alert.get('entropy', 0)} | 📡 Data confidence: {alert.get('data_confidence', 0)}%\n"
                f"{alert.get('explanation', '')}")
        bot.reply_to(m, text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")


@bot.message_handler(commands=['warroom'])
def cmd_warroom(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Format: /warroom BTC")
        return
    coin = parts[1].upper()
    try:
        meta = info.meta_and_asset_ctxs()
        mark = 0.0
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            if asset["name"] == coin:
                mark = float(ctx.get("markPx", 0))
                break
        if mark == 0:
            bot.reply_to(m, f"❌ {coin} not found")
            return
        master = {coin: get_candles(coin, "1h", 100)}
        alert = check_entry_alert(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No signal for {coin}")
            return
        delta = get_ob_delta(coin)
        cvd = get_cvd(coin, 30)
        oi = get_oi_usd(coin)
        funding = get_funding_pct(coin)
        structure_long, structure_short = get_structure_valid_separate(coin, master)
        momentum = get_composite_momentum(coin, master)
        exhaustion = compute_exhaustion_score(coin, master)
        entropy = compute_market_entropy(coin, master)
        dq = get_data_confidence(coin, mark, time.time())[0]
        candles_1h = get_candles(coin, "1h", 60, master)
        state = get_market_state_from_structure(candles_1h, mark).name if candles_1h else "UNKNOWN"
        hyp = alert.get('hypothesis', {})
        text = (f"🧠 *Warroom {coin}*\nMarket: {get_market_regime()} | Volatility: {get_volatility_regime()} | Flow: {get_flow_regime()}\n"
                f"State: {state} | Event: {alert['area']} | Direction: {alert['direction']}\n"
                f"OB Delta: {delta:+.1f}% | CVD: {cvd:+.2f}M | OI: {oi:.1f}M | Funding: {funding:+.3f}%\n"
                f"Structure (L/S): {structure_long}/{structure_short} | Momentum: {momentum}\n"
                f"Exhaustion: {exhaustion}% | Entropy: {entropy} | Data confidence: {dq}%\n"
                f"Score: {alert['score']} | {alert['label']}\nRR: 1:{alert['rr']:.1f}\n"
                f"📌 *Hypothesis*\n   Thesis: {hyp.get('thesis', 'N/A')}\n   Invalidate: {hyp.get('invalidate', 'N/A')}\n   Observe: {hyp.get('observe', 'N/A')}")
        bot.reply_to(m, text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")


@bot.message_handler(commands=['stopalert'])
def cmd_stopalert(m):
    global _alert_enabled
    if m.from_user.id != USER_ID:
        return
    with _alert_enabled_lock:
        _alert_enabled = not _alert_enabled
        bot.reply_to(m, f"Alert {'ON' if _alert_enabled else 'OFF'}")


# ========== MAIN ==========
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--paper', action='store_true')
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    PAPER_MODE = args.paper
    logger.info(f"Starting Smart Entry Engine v1.2 in {'PAPER' if PAPER_MODE else 'LIVE'} mode")
    init_db()
    
    # Start semua thread
    t_state = threading.Thread(target=scheduled_state_engine, daemon=True)
    t_state.start()
    t_trigger = threading.Thread(target=scheduled_trigger_engine, daemon=True)
    t_trigger.start()
    t_shadow = threading.Thread(target=scheduled_shadow_evaluation, daemon=True)
    t_shadow.start()
    t_clean = threading.Thread(target=scheduled_cleanup, daemon=True)
    t_clean.start()
    
    # Start bot polling (blocking)
    bot.infinity_polling()
    
