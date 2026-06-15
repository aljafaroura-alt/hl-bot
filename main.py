#!/usr/bin/env python3
# ============================================================
# SMART ENTRY ENGINE – HYPERLIQUID (v6.0)
# 3-Layer Architecture: Belief → Confidence → Execution
# Belief State + Commitment Score + Time Pressure
# Owner: Cryptone Project
# ============================================================

import os
import sys
import time
import signal
import sqlite3
import threading
import logging
import logging.handlers
import argparse
import random
import math
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any
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

# Interval engine
STATE_ENGINE_INTERVAL = 30
TRIGGER_ENGINE_INTERVAL_ACTIVE = 3
COOLDOWN_ENTRY = 900
BASE_EVALUATION_DELAY = 7200

# Database & logging
DB_PATH = "signals.db"
LOG_DIR = "logs"
PAPER_MODE = False

# Filter parameters
ACCEPTANCE_WINDOW_CANDLES = 2
UNCLEAR_THRESHOLD = 55
UNCLEAR_DIFF = 15
MIN_DATA_CONFIDENCE = 50

# Evidence multiplier (untuk ranging market)
EVIDENCE_MULT_1 = 0.4
EVIDENCE_MULT_2 = 0.7
EVIDENCE_MULT_3 = 1.0

# V6: 3 Jenis Entropy parameters
ENTROPY_BASE = 60
ENTROPY_VOLATILITY_FACTOR = 0.3
ENTROPY_TREND_STRENGTH_FACTOR = 0.2
ENTROPY_TTL_FACTOR = 0.5
ENTROPY_RR_FACTOR = 1.2
ENTROPY_THRESHOLD_FACTOR = 0.2

# Memory & persistence
OI_PERSISTENCE_REQUIRED = 3
ROLLING_DELTA_WINDOW = 6
SHADOW_RETENTION_HOURS = 24

# Data quality age limits (ms)
MAX_CANDLE_AGE_MS = 60000
MAX_OB_AGE_MS = 5000
MAX_CVD_AGE_MS = 30000
MAX_OI_AGE_MS = 60000
MAX_PRICE_AGE_MS = 5000
MAX_FUNDING_AGE_MS = 60000

# Outlier detection
OUTLIER_SIGMA = 3.0
MAX_JUMP_PCT = 10.0

# Flow-based detection
MIN_OB_FLOW_WALL_USD = 500_000
MIN_OB_FLOW_DELTA_SHIFT = 6
MIN_FVG_FLOW_CVD_ACCEL = 0.5
MIN_FVG_FLOW_DELTA_DIVERGENCE = 4
LIQUIDITY_VACUUM_AREA_THRESHOLD = 60

# ========== V6: FILTER WEIGHTS (dynamic per regime) ==========
FILTER_WEIGHTS_BY_REGIME = {
    "HIGH_VOLATILITY": {"rejection": 0.50, "acceptance": 0.30, "persistence": 0.20},
    "NORMAL_VOLATILITY": {"rejection": 0.40, "acceptance": 0.35, "persistence": 0.25},
    "LOW_VOLATILITY": {"rejection": 0.30, "acceptance": 0.40, "persistence": 0.30},
    "TRENDING": {"rejection": 0.45, "acceptance": 0.30, "persistence": 0.25},
    "RANGING": {"rejection": 0.35, "acceptance": 0.40, "persistence": 0.25},
}
MIN_FILTER_SCORE = 45

# ========== V6: POSITION SIZING ==========
SIZE_MULTIPLIER_CONFIG = {
    "min_size": 0.25,
    "max_size": 1.0,
    "entropy_factor": 0.7,
}

# ========== V6: COST OF WAITING / TIME PRESSURE ==========
TIME_PRESSURE_CONFIG = {
    "decay_rate": 0.05,
    "confidence_gain_rate": 2.0,
    "opportunity_decay": 0.08,
    "wait_threshold": 0.85,
    "expected_decay_rate": 0.03,
    "max_wait_minutes": 30,
    "urgent_threshold": 70,
    "normal_threshold": 30,
}

# ========== V6: DECISION FATIGUE (per thesis family) ==========
THESIS_FAMILIES = {
    "LIQUIDITY_SWEEP": ["LIQUIDITY"],
    "ORDER_BLOCK": ["OB", "OB_FLOW", "SD"],
    "IMBALANCE": ["FVG", "FVG_FLOW"],
    "VACUUM": ["VACUUM"],
}
MAX_FATIGUE_PER_HOUR = 5
FATIGUE_COOLDOWN_WINDOW = 3600

# ========== V6: BELIEF STATE ==========
class BeliefState(Enum):
    SEEKING = "seeking"
    BUILDING = "building"
    CONVICTED = "convicted"
    EXECUTING = "executing"
    INVALIDATED = "invalidated"

# ========== V6: TIME PRESSURE ==========
class TimePressure(Enum):
    LOW = "low"
    NORMAL = "normal"
    URGENT = "urgent"

# ========== V6: EXECUTION MODE BLEND ==========
EXECUTION_MODES_V6 = {
    "PRECISION": {"threshold_boost": 1.15},
    "BALANCED": {"threshold_boost": 1.0},
    "AGGRESSIVE": {"threshold_boost": 0.85}
}

DECISION_ENERGY_AGGRESSIVE_THRESHOLD = 75
DECISION_ENERGY_PRECISION_THRESHOLD = 40
ENTROPY_AGGRESSIVE_MAX = 40
ENTROPY_PRECISION_MIN = 70

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
    
    
# ========== ENUMS (V6) ==========
class MarketState(Enum):
    UNKNOWN = 0
    ACCUMULATION = 1
    EXPANSION = 2
    DISTRIBUTION = 3
    REVERSAL = 4

class MarketIntent(Enum):
    """V6: Apa yang market coba lakukan"""
    SEEK_LIQUIDITY = "seek_liquidity"
    ACCEPT = "accept"
    TRAP = "trap"
    DISTRIBUTE = "distribute"
    CONTINUE = "continue"

class IntentType(Enum):
    """Legacy compatibility"""
    GRAB = "grab"
    TRAP = "trap"
    ACCEPT = "accept"
    CONTINUE = "continue"

class SetupState(Enum):
    PENDING = 0
    TRIGGERED = 1
    EXPIRED = 2
    INVALIDATED = 3

# ========== GLOBAL STATE & LOCKS ==========
_candle_cache: Dict[str, Tuple[List[dict], float]] = {}
_candle_lock = threading.RLock()

_ob_cache: Dict[str, Tuple[float, float]] = {}
_ob_lock = threading.RLock()

_cvd_cache: Dict[str, Tuple[float, float]] = {}
_cvd_lock = threading.RLock()

_oi_history: Dict[str, deque] = {}
_oi_lock = threading.RLock()

_funding_cache: Dict[str, Tuple[float, float]] = {}
_funding_lock = threading.RLock()

_last_alert: Dict[str, float] = {}
_last_alert_lock = threading.RLock()

_alert_enabled = True
_alert_enabled_lock = threading.RLock()
_shutdown_event = threading.Event()

_last_mids: Dict[str, Tuple[float, float]] = {}
_last_mids_lock = threading.RLock()

_rolling_delta: Dict[str, deque] = {}
_rolling_delta_lock = threading.RLock()

_oi_persistence: Dict[str, Dict] = {}
_oi_persistence_lock = threading.RLock()

_zone_memory: Dict[str, Dict] = {}
_zone_memory_lock = threading.RLock()

_active_candidates: Dict[str, Dict] = {}
_active_candidates_lock = threading.RLock()

_shadow_decisions: Dict[str, Dict] = {}
_shadow_lock = threading.RLock()

_hypothesis_store: Dict[str, Dict] = {}
_hypothesis_lock = threading.RLock()

# V6: Prediction Quality memory (bukan winrate)
_prediction_memory: Dict[str, Dict] = {}
_prediction_memory_lock = threading.RLock()

# Data integrity histories
_oi_values: Dict[str, deque] = {}
_funding_values: Dict[str, deque] = {}
_price_values: Dict[str, deque] = {}
_data_integrity_lock = threading.RLock()

# V6: Decision Energy history untuk acceleration
_decision_energy_history: Dict[str, deque] = {}
_decision_energy_history_lock = threading.RLock()

# V6: Belief State per coin
_belief_state: Dict[str, Dict] = {}
_belief_state_lock = threading.RLock()

# V6: Fatigue per thesis family
_fatigue_memory: Dict[str, deque] = {}
_fatigue_memory_lock = threading.RLock()

# Pending setups (thesis-based)
_pending_setups: Dict[str, 'PendingSetup'] = {}
_pending_setups_lock = threading.RLock()
_SETUP_EXPIRY_SECONDS = 3600

# V6.1: Progressive cooldown tracking
_alert_history: Dict[str, deque] = {}
_alert_history_lock = threading.RLock()
ALERT_HISTORY_WINDOW = 3600  # 1 jam

# V6.1: Market sanity state
_market_sanity: Dict[str, Any] = {"is_sane": True, "last_check": 0.0, "reason": ""}
_market_sanity_lock = threading.RLock()
MARKET_SANITY_TTL = 60

# V6.1: psutil optional
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

info = Info(constants.MAINNET_API_URL)


# ========== DATABASE HELPER ==========
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db_connect()
    c = conn.cursor()
    
    # Signals table (V6)
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
        hypothesis_validated INTEGER DEFAULT 0,
        execution_mode TEXT,
        intent_type TEXT,
        decision_energy REAL,
        position_size_mult REAL,
        filter_score REAL,
        intent_confidence REAL,
        belief_state TEXT,
        commitment_score REAL,
        time_pressure TEXT,
        prediction_quality REAL
    )''')
    
    # Journal table (V6)
    c.execute('''CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        market_regime TEXT,
        volatility_regime TEXT,
        flow_regime TEXT,
        belief_state TEXT,
        long_score INTEGER,
        short_score INTEGER,
        direction TEXT,
        final_score INTEGER,
        reason TEXT,
        negative_evidence TEXT,
        entropy_data INTEGER,
        entropy_market INTEGER,
        entropy_decision INTEGER,
        decision_time_ms INTEGER,
        api_latency_ms INTEGER,
        data_confidence INTEGER,
        executed INTEGER DEFAULT 0,
        outcome TEXT,
        missed_opportunity_pnl REAL DEFAULT NULL,
        contribution TEXT,
        execution_mode TEXT,
        intent_type TEXT,
        decision_energy REAL,
        position_size_mult REAL,
        filter_score REAL,
        rejection_strength REAL,
        acceptance_strength REAL,
        persistence_strength REAL,
        why_not TEXT,
        wait_value REAL,
        trigger_strength REAL,
        time_pressure TEXT,
        commitment_score REAL,
        decision_acceleration REAL,
        mode_aggressive REAL,
        mode_balanced REAL,
        mode_precision REAL,
        confidence_breakdown TEXT
    )''')
    
    # Counterfactual table
    c.execute('''CREATE TABLE IF NOT EXISTS counterfactual (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        original_score INTEGER,
        modified_module TEXT,
        modified_score INTEGER,
        reason TEXT
    )''')
    
    # Shadow decisions
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
    
    # Data freshness log
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
    
    # Hypothesis validation
    c.execute('''CREATE TABLE IF NOT EXISTS hypothesis_validation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT,
        thesis TEXT,
        outcome TEXT,
        pnl REAL,
        validated INTEGER
    )''')
    
    # V6: Prediction quality log
    c.execute('''CREATE TABLE IF NOT EXISTS prediction_quality (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        signal_id TEXT,
        predicted_direction TEXT,
        actual_direction TEXT,
        entry_zone_accuracy REAL,
        timing_quality REAL,
        thesis_validated INTEGER,
        quality_score REAL
    )''')
    
    # V6: Fatigue log
    c.execute('''CREATE TABLE IF NOT EXISTS fatigue_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        family TEXT,
        rejection_count INTEGER,
        fatigue_penalty REAL
    )''')
    
    # V6: Belief state log
    c.execute('''CREATE TABLE IF NOT EXISTS belief_state_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        coin TEXT,
        state TEXT,
        duration_seconds REAL,
        trigger TEXT
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Database ready (V6)")

# ========== DB WRAPPER FUNCTIONS (V6) ==========
def save_signal_v6(signal_id, coin, direction, score, entry, sl, tp, rr, reason, data_confidence,
                   hypothesis_thesis="", hypothesis_invalidate="", hypothesis_observe="",
                   execution_mode="BALANCED", intent_type="", decision_energy=0.0,
                   position_size_mult=1.0, filter_score=100.0, intent_confidence=0.0,
                   belief_state="SEEKING", commitment_score=0.0, time_pressure="normal",
                   prediction_quality=50.0, mode_aggressive=0.0, mode_balanced=1.0, mode_precision=0.0):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO signals 
                 (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason, 
                  timestamp, data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                  execution_mode, intent_type, decision_energy, position_size_mult, filter_score, intent_confidence,
                  belief_state, commitment_score, time_pressure, prediction_quality)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()), 
               data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
               execution_mode, intent_type, decision_energy, position_size_mult, filter_score, intent_confidence,
               belief_state, commitment_score, time_pressure, prediction_quality))
    conn.commit()
    conn.close()
    logger.info(f"Signal saved: {coin} {direction} score={score} mode={execution_mode} belief={belief_state}")

def add_journal_entry_v6(coin, market_regime, volatility_regime, flow_regime,
                         belief_state, long_score, short_score, direction, final_score,
                         reason, negative_evidence, entropy_data, entropy_market, entropy_decision,
                         decision_time_ms, api_latency_ms, data_confidence, executed,
                         missed_opportunity_pnl=None, contribution="",
                         execution_mode="BALANCED", intent_type="", decision_energy=0.0,
                         position_size_mult=1.0, filter_score=100.0,
                         rejection_strength=0.0, acceptance_strength=0.0, persistence_strength=0.0,
                         why_not="", wait_value=0.0, trigger_strength=0.0, time_pressure="normal",
                         commitment_score=0.0, decision_acceleration=0.0,
                         mode_aggressive=0.0, mode_balanced=1.0, mode_precision=0.0,
                         confidence_breakdown=""):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO journal 
                 (timestamp, coin, market_regime, volatility_regime, flow_regime, belief_state,
                  long_score, short_score, direction, final_score, reason, negative_evidence,
                  entropy_data, entropy_market, entropy_decision, decision_time_ms, api_latency_ms,
                  data_confidence, executed, missed_opportunity_pnl, contribution, execution_mode,
                  intent_type, decision_energy, position_size_mult, filter_score, rejection_strength,
                  acceptance_strength, persistence_strength, why_not, wait_value, trigger_strength,
                  time_pressure, commitment_score, decision_acceleration, mode_aggressive,
                  mode_balanced, mode_precision, confidence_breakdown)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (int(time.time()), coin, market_regime, volatility_regime, flow_regime, belief_state,
               long_score, short_score, direction, final_score, reason, negative_evidence,
               entropy_data, entropy_market, entropy_decision, decision_time_ms, api_latency_ms,
               data_confidence, 1 if executed else 0, missed_opportunity_pnl, contribution,
               execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
               rejection_strength, acceptance_strength, persistence_strength, why_not, wait_value,
               trigger_strength, time_pressure, commitment_score, decision_acceleration,
               mode_aggressive, mode_balanced, mode_precision, confidence_breakdown))
    conn.commit()
    conn.close()

def add_prediction_quality_log(coin, signal_id, predicted_direction, actual_direction,
                                entry_zone_accuracy, timing_quality, thesis_validated, quality_score):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO prediction_quality 
                 (timestamp, coin, signal_id, predicted_direction, actual_direction,
                  entry_zone_accuracy, timing_quality, thesis_validated, quality_score)
                 VALUES (?,?,?,?,?,?,?,?,?)''',
              (int(time.time()), coin, signal_id, predicted_direction, actual_direction,
               entry_zone_accuracy, timing_quality, 1 if thesis_validated else 0, quality_score))
    conn.commit()
    conn.close()

def add_fatigue_log(family, rejection_count, fatigue_penalty):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO fatigue_log (timestamp, family, rejection_count, fatigue_penalty)
                 VALUES (?,?,?,?)''',
              (int(time.time()), family, rejection_count, fatigue_penalty))
    conn.commit()
    conn.close()

def add_belief_state_log(coin, state, duration_seconds, trigger):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO belief_state_log (timestamp, coin, state, duration_seconds, trigger)
                 VALUES (?,?,?,?,?)''',
              (int(time.time()), coin, state, duration_seconds, trigger))
    conn.commit()
    conn.close()

def add_counterfactual(coin, original_score, modified_module, modified_score, reason):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO counterfactual (timestamp, coin, original_score, modified_module, modified_score, reason)
                 VALUES (?,?,?,?,?,?)''',
              (int(time.time()), coin, original_score, modified_module, modified_score, reason))
    conn.commit()
    conn.close()

def add_shadow_decision(signal_id, coin, direction, entry, sl, tp):
    with _shadow_lock:
        _shadow_decisions[signal_id] = {
            "coin": coin, "direction": direction, "entry": entry, "sl": sl, "tp": tp,
            "timestamp": time.time(), "evaluated": False, "outcome": None, "pnl": 0.0,
            "mfe": 0.0, "mae": 0.0
        }
    conn = db_connect()
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
    conn = db_connect()
    c = conn.cursor()
    c.execute('''UPDATE shadow_decisions SET evaluated=1, outcome=?, pnl=?, mfe=?, mae=? WHERE signal_id=?''',
              (outcome, pnl, mfe, mae, signal_id))
    conn.commit()
    conn.close()

def add_hypothesis_validation(signal_id, thesis, outcome, pnl, validated):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO hypothesis_validation (signal_id, thesis, outcome, pnl, validated)
                 VALUES (?,?,?,?,?)''',
              (signal_id, thesis, outcome, pnl, 1 if validated else 0))
    conn.commit()
    conn.close()

def update_signal_outcome_v6(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None):
    conn = db_connect()
    c = conn.cursor()
    if hypothesis_validated is not None:
        c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, 
                     mfe=?, mae=?, hypothesis_validated=? WHERE signal_id=?''',
                  (outcome, pnl, exit_price, int(time.time()), mfe, mae, 
                   1 if hypothesis_validated else 0, signal_id))
    else:
        c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, 
                     mfe=?, mae=? WHERE signal_id=?''',
                  (outcome, pnl, exit_price, int(time.time()), mfe, mae, signal_id))
    conn.commit()
    conn.close()

def get_analytics() -> dict:
    conn = db_connect()
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

# ========== HELPER FUNCTIONS ==========
def fmt_price(p): 
    return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"

def get_wib(): 
    return datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")

def get_wib_hour(): 
    return datetime.now(timezone(timedelta(hours=7))).hour

def generate_signal_id(coin, direction): 
    return f"{coin}_{direction}_{int(time.time())}"
    
    
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
    score = 100
    reasons = []
    with _data_integrity_lock:
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

def get_data_confidence(coin: str, current_price: float, current_time: float) -> Tuple[int, Dict[str, int]]:
    ages = {}
    total_score = 100
    
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
    
    if coin in _cvd_cache:
        _, ts = _cvd_cache[coin]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_CVD_AGE_MS + 1000
    ages["cvd_age_ms"] = int(age_ms)
    if age_ms > MAX_CVD_AGE_MS:
        total_score -= 10
    
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
    
    if coin in _funding_cache:
        _, ts = _funding_cache[coin]
        age_ms = (current_time - ts) * 1000
    else:
        age_ms = MAX_FUNDING_AGE_MS + 1000
    ages["funding_age_ms"] = int(age_ms)
    if age_ms > MAX_FUNDING_AGE_MS:
        total_score -= 10
    
    total_score = max(0, min(100, total_score))
    integrity_score = get_data_integrity_score(coin)
    final_confidence = int(total_score * 0.7 + integrity_score * 0.3)
    
    if final_confidence < MIN_DATA_CONFIDENCE:
        logger.warning(f"Data confidence low for {coin}: {final_confidence}%")
    
    return final_confidence, ages

def update_mids_cache():
    try:
        mids = info.all_mids()
        now = time.time()
        with _last_mids_lock:
            for coin, price in mids.items():
                _last_mids[coin] = (float(price), now)
        for coin, price in mids.items():
            update_data_integrity_history(coin, 0, 0, float(price))
    except Exception as e:
        logger.error(f"Update mids cache error: {e}")

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
    
    # Fast path check
    with _candle_lock:
        if key in _candle_cache and now - _candle_cache[key][1] < ttl:
            return _candle_cache[key][0]
    
    # Fetch outside lock to avoid blocking other threads
    end_ms = int(now * 1000)
    tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
    interval = tf_ms.get(timeframe, 3600000)
    start_ms = end_ms - limit * interval
    
    try:
        candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms) or []
    except Exception as e:
        logger.error(f"get_candles failed for {coin}/{timeframe}: {e}")
        candles = []
    
    # Double-check locking: prevent duplicate writes
    with _candle_lock:
        if key in _candle_cache and now - _candle_cache[key][1] < ttl:
            return _candle_cache[key][0]
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
        
        change = abs(raw - prev)
        alpha = min(0.9, 0.3 + change / 60)
        smoothed = alpha * raw + (1 - alpha) * prev
        
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
        h = float(candles[i]['h'])
        l = float(candles[i]['l'])
        pc = float(candles[i-1]['c'])
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

_regimes_cache: Dict[str, Any] = {}
_regimes_cache_lock = threading.RLock()
_REGIMES_TTL = 120

def get_all_regimes() -> Tuple[str, str, str]:
    with _regimes_cache_lock:
        now = time.time()
        if _regimes_cache and now - _regimes_cache.get("ts", 0) < _REGIMES_TTL:
            return _regimes_cache["market"], _regimes_cache["volatility"], _regimes_cache["flow"]
    
    market = get_market_regime()
    volatility = get_volatility_regime()
    flow = get_flow_regime()
    
    with _regimes_cache_lock:
        _regimes_cache["market"] = market
        _regimes_cache["volatility"] = volatility
        _regimes_cache["flow"] = flow
        _regimes_cache["ts"] = time.time()
    
    return market, volatility, flow
    
    
# ========== STRUCTURE DETECTION ==========
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
    if not candles or len(highs) < 2 or len(lows) < 2:
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
    if len(highs) < 2 or len(lows) < 2:
        return False, False
    
    bos_up, bos_down, choch = get_bos_and_choch(candles_1h, highs, lows)
    valid_long = bos_up or choch
    valid_short = bos_down or choch
    return valid_long, valid_short

# ========== V6: ZONE MEMORY (gradient) ==========
def update_zone_memory_v6(coin: str, zone_type: str, low: float, high: float, acceptance_strength: float):
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    now = time.time()
    with _zone_memory_lock:
        if key not in _zone_memory:
            _zone_memory[key] = {"touch_count": 0, "first_touch": now, "last_touch": now, "strengths": []}
        data = _zone_memory[key]
        data["touch_count"] += 1
        data["last_touch"] = now
        data["strengths"].append(acceptance_strength)
        if len(data["strengths"]) > 10:
            data["strengths"] = data["strengths"][-10:]

def get_zone_penalty_v6(coin: str, zone_type: str, low: float, high: float) -> float:
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    with _zone_memory_lock:
        if key not in _zone_memory:
            return 0.0
        data = _zone_memory[key]
        if not data["strengths"]:
            return 0.0
        avg_strength = sum(data["strengths"]) / len(data["strengths"])
        penalty = max(0, 40 - avg_strength) * 0.5
        return min(30, penalty)

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
        
        
# ========== V6: BELIEF STATE MANAGEMENT ==========
def get_thesis_family(event_type: str) -> str:
    for family, types in THESIS_FAMILIES.items():
        if event_type in types:
            return family
    return "OTHER"

def update_belief_state(coin: str, event_type: str, filter_score: float, 
                        trigger_strength: float, event_score: int) -> Tuple[BeliefState, str]:
    with _belief_state_lock:
        current = _belief_state.get(coin, {"state": BeliefState.SEEKING, "since": time.time(), "family": None})
        now = time.time()
        duration = now - current["since"]
        
        # State transition logic
        if current["state"] == BeliefState.SEEKING:
            if event_score > 60 and filter_score > 50:
                new_state = BeliefState.BUILDING
                trigger = f"event_score={event_score}, filter={filter_score:.0f}"
                add_belief_state_log(coin, new_state.value, duration, trigger)
                _belief_state[coin] = {"state": new_state, "since": now, "family": get_thesis_family(event_type)}
                return new_state, trigger
        
        elif current["state"] == BeliefState.BUILDING:
            if trigger_strength > 50:
                new_state = BeliefState.CONVICTED
                trigger = f"trigger_strength={trigger_strength:.0f}"
                add_belief_state_log(coin, new_state.value, duration, trigger)
                _belief_state[coin] = {"state": new_state, "since": now, "family": current["family"]}
                return new_state, trigger
            elif duration > 300:  # 5 menit no progress
                new_state = BeliefState.INVALIDATED
                trigger = f"timeout after {duration:.0f}s"
                add_belief_state_log(coin, new_state.value, duration, trigger)
                _belief_state[coin] = {"state": new_state, "since": now, "family": current["family"]}
                return new_state, trigger
        
        elif current["state"] == BeliefState.CONVICTED:
            if trigger_strength > 70:
                new_state = BeliefState.EXECUTING
                trigger = f"trigger_strength={trigger_strength:.0f}"
                add_belief_state_log(coin, new_state.value, duration, trigger)
                _belief_state[coin] = {"state": new_state, "since": now, "family": current["family"]}
                return new_state, trigger
        
        return current["state"], "no_change"

def reset_belief_state(coin: str, reason: str):
    with _belief_state_lock:
        if coin in _belief_state:
            old_state = _belief_state[coin]["state"]
            duration = time.time() - _belief_state[coin]["since"]
            add_belief_state_log(coin, f"RESET_{old_state.value}", duration, reason)
        _belief_state[coin] = {"state": BeliefState.SEEKING, "since": time.time(), "family": None}

def get_belief_state(coin: str) -> Tuple[BeliefState, float]:
    with _belief_state_lock:
        if coin not in _belief_state:
            return BeliefState.SEEKING, 0.0
        data = _belief_state[coin]
        duration = time.time() - data["since"]
        return data["state"], duration

# ========== V6: PREDICTION QUALITY (bukan winrate) ==========
def evaluate_prediction_quality(signal_id: str, coin: str, predicted_direction: str,
                                 actual_direction: str, entry_price: float,
                                 predicted_zone_low: float, predicted_zone_high: float,
                                 mfe: float, mae: float, thesis_validated: bool) -> float:
    quality = 50.0
    
    # 1. Direction accuracy (30%)
    if predicted_direction == actual_direction:
        quality += 30
    else:
        quality -= 20
    
    # 2. Entry zone accuracy (25%)
    if predicted_zone_low <= entry_price <= predicted_zone_high:
        quality += 25
        zone_accuracy = 1.0
    else:
        zone_accuracy = max(0, 1 - abs(entry_price - predicted_zone_high) / predicted_zone_high)
        quality += zone_accuracy * 15
    
    # 3. Timing quality (25%) - MFE/MAE ratio
    if mae != 0 and mfe > abs(mae):
        ratio = min(3.0, mfe / abs(mae))
        timing_quality = (ratio / 3.0) * 25
        quality += timing_quality
    elif mfe > 0:
        quality += 12
    
    # 4. Thesis validation (20%)
    if thesis_validated:
        quality += 20
    else:
        quality -= 10
    
    quality = max(0, min(100, quality))
    
    add_prediction_quality_log(coin, signal_id, predicted_direction, actual_direction,
                                zone_accuracy, timing_quality if 'timing_quality' in dir() else 0.5,
                                thesis_validated, quality)
    
    return quality

def update_prediction_memory(coin: str, prediction_quality: float):
    with _prediction_memory_lock:
        if coin not in _prediction_memory:
            _prediction_memory[coin] = {"ema_quality": 50.0, "last_update": time.time(), "history": deque(maxlen=20)}
        
        mem = _prediction_memory[coin]
        alpha = 0.2
        mem["ema_quality"] = alpha * prediction_quality + (1 - alpha) * mem["ema_quality"]
        mem["last_update"] = time.time()
        mem["history"].append(prediction_quality)

def get_prediction_quality_multiplier(coin: str) -> float:
    with _prediction_memory_lock:
        if coin not in _prediction_memory:
            return 1.0
        ema = _prediction_memory[coin]["ema_quality"] / 100.0
        return 0.6 + (ema * 0.8)

# ========== V6: 3 JENIS ENTROPY ==========
def compute_data_entropy(ages: Dict[str, int]) -> int:
    """Seberapa outdated data?"""
    score = 0
    if ages.get("price_age_ms", 0) > MAX_PRICE_AGE_MS:
        score += 25
    if ages.get("candle_age_ms", 0) > MAX_CANDLE_AGE_MS:
        score += 25
    if ages.get("ob_age_ms", 0) > MAX_OB_AGE_MS:
        score += 25
    if ages.get("oi_age_ms", 0) > MAX_OI_AGE_MS:
        score += 25
    return min(100, score)

def compute_market_entropy_v6(coin: str, master: Dict) -> int:
    """Seberapa chaotic market?"""
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 4:
        return 30
    
    closes = [float(c['c']) for c in candles[-5:]]
    price_changes = [abs(closes[i] - closes[i-1])/closes[i-1]*100 for i in range(1, len(closes))]
    price_flips = sum(1 for i in range(2, len(closes)) if (closes[i] > closes[i-1]) != (closes[i-1] > closes[i-2]))
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

def compute_decision_entropy(score_variance: float, contradictory_signals: bool,
                              multiple_events: bool, event_types: List[str]) -> int:
    """Seberapa bingung sistem?"""
    entropy = 0
    if score_variance > 30:
        entropy += 30
    if contradictory_signals:
        entropy += 30
    if multiple_events and len(set(event_types)) > 2:
        entropy += 25
    if len(event_types) > 3:
        entropy += 15
    return min(100, entropy)

def get_dynamic_entropy_threshold_v6(volatility_regime: str, trend_strength: float) -> int:
    base = ENTROPY_BASE
    if volatility_regime == "HIGH_VOLATILITY":
        base += int(ENTROPY_VOLATILITY_FACTOR * 20)
    elif volatility_regime == "LOW_VOLATILITY":
        base -= int(ENTROPY_VOLATILITY_FACTOR * 15)
    base += int((trend_strength / 100) * ENTROPY_TREND_STRENGTH_FACTOR * 50)
    return max(40, min(85, base))

def compute_trend_strength_v6(coin: str, master: Dict) -> float:
    candles = get_candles(coin, "1h", 50, master)
    if not candles or len(candles) < 21:
        return 50.0
    closes = [float(c['c']) for c in candles]
    ema8 = np.mean(closes[-8:])
    ema21 = np.mean(closes[-21:])
    slope = (ema8 - ema21) / ema21 * 100 if ema21 != 0 else 0
    strength = min(100, max(0, (abs(slope) / 2) * 100))
    return strength

# ========== V6: DECISION ENERGY (normalized) ==========
def compute_decision_energy_v6(confidence: float, opportunity: float, uncertainty: float) -> float:
    if uncertainty <= 0:
        uncertainty = 0.01
    geometric = (confidence * opportunity) ** 0.5
    de = geometric - uncertainty * 0.3
    return max(0.0, min(100.0, de))

def compute_decision_acceleration(coin: str) -> float:
    """Rate of change of Decision Energy over last 5 minutes"""
    with _decision_energy_history_lock:
        if coin not in _decision_energy_history or len(_decision_energy_history[coin]) < 3:
            return 0.0
        history = list(_decision_energy_history[coin])[-3:]
        if len(history) < 3:
            return 0.0
        acceleration = (history[-1] - history[-2]) - (history[-2] - history[-3])
        return max(-1.0, min(1.0, acceleration / 10))

def update_decision_energy_history(coin: str, decision_energy: float):
    with _decision_energy_history_lock:
        if coin not in _decision_energy_history:
            _decision_energy_history[coin] = deque(maxlen=10)
        _decision_energy_history[coin].append(decision_energy)

# ========== V6: TIME PRESSURE ==========
def compute_time_pressure(setup_age_minutes: float, opportunity_decay_rate: float,
                          competitor_setups: int, urgent_threshold: float = 70,
                          normal_threshold: float = 30) -> Tuple[TimePressure, float]:
    urgency_score = 0.0
    
    if setup_age_minutes > 20:
        urgency_score += 40
    elif setup_age_minutes > 10:
        urgency_score += 20
    elif setup_age_minutes > 5:
        urgency_score += 10
    
    if opportunity_decay_rate > 0.15:
        urgency_score += 30
    elif opportunity_decay_rate > 0.08:
        urgency_score += 15
    
    if competitor_setups > 5:
        urgency_score += 30
    elif competitor_setups > 3:
        urgency_score += 15
    
    if urgency_score > urgent_threshold:
        return TimePressure.URGENT, urgency_score
    elif urgency_score > normal_threshold:
        return TimePressure.NORMAL, urgency_score
    return TimePressure.LOW, urgency_score

# ========== V6: COMMITMENT SCORE ==========
def compute_commitment_score(belief_state: BeliefState, confidence_score: float,
                              time_pressure: TimePressure, position_size_mult: float,
                              prediction_quality: float) -> float:
    """0-100, seberapa berani eksekusi"""
    score = 0.0
    
    # Belief state (40%)
    state_scores = {
        BeliefState.CONVICTED: 40,
        BeliefState.BUILDING: 20,
        BeliefState.SEEKING: 0,
        BeliefState.EXECUTING: 35,
        BeliefState.INVALIDATED: 0,
    }
    score += state_scores.get(belief_state, 0)
    
    # Confidence (30%)
    score += confidence_score * 0.3
    
    # Time pressure (20%)
    pressure_scores = {TimePressure.URGENT: 20, TimePressure.NORMAL: 10, TimePressure.LOW: 0}
    score += pressure_scores.get(time_pressure, 0)
    
    # Prediction quality (10%)
    score += prediction_quality * 0.1
    
    return min(100, score)

# ========== V6: DECISION ENERGY COMPONENTS ==========
def compute_confidence_from_score_v6(score: int, data_confidence: int, evidence_families: int) -> float:
    conf = score * 0.7 + data_confidence * 0.2 + (evidence_families / 3) * 100 * 0.1
    return min(100.0, conf)

def compute_opportunity_v6(rr: float, vol_spike: float, momentum: int) -> float:
    rr_score = min(60.0, rr * 20)
    vol_score = min(20.0, (vol_spike - 1.0) * 20)
    mom_score = min(20.0, momentum / 5)
    return rr_score + vol_score + mom_score

def compute_uncertainty_v6(entropy_market: int, entropy_decision: int, contradiction: bool, exhaustion: int) -> float:
    unc = entropy_market * 0.6 + entropy_decision * 0.4
    if contradiction:
        unc += 20
    unc += exhaustion * 0.2
    return min(100.0, unc)

def get_entropy_adjusted_min_rr_v6(base_rr: float, entropy_market: int) -> float:
    factor = 1.0 + (entropy_market / 100) * ENTROPY_RR_FACTOR
    return base_rr * factor

def get_entropy_adjusted_threshold_v6(base_threshold: int, entropy_market: int) -> int:
    factor = 1.0 + (entropy_market / 100) * ENTROPY_THRESHOLD_FACTOR
    new_th = int(base_threshold * factor)
    return max(50, min(85, new_th))

# ========== V6: EXECUTION MODE BLEND ==========
def get_execution_mode_blend_v6(decision_energy: float, entropy_market: int, 
                                 decision_acceleration: float, intent) -> Dict[str, float]:
    aggressive_score = 0.0
    balanced_score = 0.0
    precision_score = 0.0
    
    if intent in [IntentType.TRAP, MarketIntent.TRAP]:
        return {"aggressive": 0.0, "balanced": 0.2, "precision": 0.8}
    
    if intent in [IntentType.GRAB, MarketIntent.SEEK_LIQUIDITY]:
        aggressive_score += 0.3
    
    if decision_energy >= 75:
        aggressive_score += (decision_energy - 74) / 26
    elif decision_energy <= 40:
        precision_score += (41 - decision_energy) / 41
    else:
        balanced_score += 0.5
    
    if entropy_market <= 40:
        aggressive_score += (41 - entropy_market) / 41
    elif entropy_market >= 70:
        precision_score += (entropy_market - 69) / 31
    
    # Decision acceleration adjustment
    if decision_acceleration > 0.3:
        aggressive_score *= 1.2
    elif decision_acceleration < -0.3:
        precision_score *= 1.2
    
    aggressive_score = min(1.0, aggressive_score)
    precision_score = min(1.0, precision_score)
    
    balanced_score = 1.0 - aggressive_score - precision_score
    balanced_score = max(0.0, min(1.0, balanced_score))
    
    total = aggressive_score + balanced_score + precision_score
    if total > 0:
        aggressive_score /= total
        balanced_score /= total
        precision_score /= total
    
    return {
        "aggressive": round(aggressive_score, 2),
        "balanced": round(balanced_score, 2),
        "precision": round(precision_score, 2)
    }

def get_execution_mode_from_blend_v6(mode_weights: Dict[str, float]) -> str:
    if mode_weights["precision"] > 0.5:
        return "PRECISION"
    elif mode_weights["aggressive"] > 0.5:
        return "AGGRESSIVE"
    else:
        return "BALANCED"

def get_mode_threshold_boost_from_blend_v6(mode_weights: Dict[str, float]) -> float:
    boost = (
        mode_weights["aggressive"] * EXECUTION_MODES_V6["AGGRESSIVE"]["threshold_boost"] +
        mode_weights["balanced"] * EXECUTION_MODES_V6["BALANCED"]["threshold_boost"] +
        mode_weights["precision"] * EXECUTION_MODES_V6["PRECISION"]["threshold_boost"]
    )
    return boost

# ========== V6: FATIGUE PER THESIS FAMILY ==========
def update_fatigue_memory(family: str):
    now = time.time()
    with _fatigue_memory_lock:
        if family not in _fatigue_memory:
            _fatigue_memory[family] = deque(maxlen=MAX_FATIGUE_PER_HOUR + 1)
        _fatigue_memory[family].append(now)
        
        while _fatigue_memory[family] and now - _fatigue_memory[family][0] > FATIGUE_COOLDOWN_WINDOW:
            _fatigue_memory[family].popleft()

def get_fatigue_penalty_by_family(event_type: str) -> float:
    family = get_thesis_family(event_type)
    with _fatigue_memory_lock:
        if family not in _fatigue_memory:
            return 1.0
        count = len(_fatigue_memory[family])
        if count >= MAX_FATIGUE_PER_HOUR:
            penalty = 0.3
        elif count >= 3:
            penalty = 0.6
        elif count >= 1:
            penalty = 0.8
        else:
            penalty = 1.0
        return penalty

# ========== ACTIVE CANDIDATE TRACKING ==========
def update_active_candidate_v6(coin: str, current_price: float, entropy_market: int, entry_price: float = None):
    vol_reg = get_volatility_regime()
    base_ttl = 1800
    if vol_reg == "HIGH_VOLATILITY":
        base_ttl = 900
    elif vol_reg == "LOW_VOLATILITY":
        base_ttl = 3600
    
    ttl_adj = max(0.5, 1.0 - (entropy_market / 100) * ENTROPY_TTL_FACTOR)
    base_ttl = int(base_ttl * ttl_adj)
    
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
            "last_entropy": entropy_market
        }

def cleanup_active_candidates_v6():
    now = time.time()
    with _active_candidates_lock:
        expired = [c for c, d in _active_candidates.items() if now > d["expire_time"]]
        for c in expired:
            del _active_candidates[c]

def cleanup_old_shadow_decisions_v6():
    now = time.time()
    cutoff = now - SHADOW_RETENTION_HOURS * 3600
    with _shadow_lock:
        to_delete = [sid for sid, data in _shadow_decisions.items() if data["timestamp"] < cutoff]
        for sid in to_delete:
            del _shadow_decisions[sid]
            
            
# ========== V6: FILTER GRADIENT (0-100, dynamic weights) ==========
def compute_rejection_strength_v6(coin: str, event, current_price: float, master: Dict) -> float:
    candles_5m = get_candles(coin, "5m", 15, master)
    if not candles_5m or len(candles_5m) < 5:
        return 0.0
    
    last_touch_idx = None
    for i in range(max(0, len(candles_5m)-4), len(candles_5m)):
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
        return 0.0
    
    delta_shift = get_delta_shift(coin)
    delta_score = min(50, abs(delta_shift) * 8)
    
    vol_spike = get_volume_spike(coin, master)
    vol_score = min(30, (vol_spike - 1.0) * 25)
    
    touch_candle = candles_5m[last_touch_idx]
    wick_pct = 0
    if event.direction == "LONG":
        wick = float(touch_candle['l']) - event.price_low
        candle_range = float(touch_candle['h']) - float(touch_candle['l'])
        if candle_range > 0:
            wick_pct = min(20, (wick / candle_range) * 40)
    else:
        wick = event.price_high - float(touch_candle['h'])
        candle_range = float(touch_candle['h']) - float(touch_candle['l'])
        if candle_range > 0:
            wick_pct = min(20, (wick / candle_range) * 40)
    
    strength = delta_score + vol_score + wick_pct
    return min(100, strength)

def compute_acceptance_strength_v6(coin: str, event, master: Dict) -> float:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < ACCEPTANCE_WINDOW_CANDLES + 2:
        return 0.0
    
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
    
    if last_touch_idx is None or last_touch_idx + ACCEPTANCE_WINDOW_CANDLES >= len(candles_5m):
        return 0.0
    
    accepted_candles = 0
    total_candles = 0
    for j in range(last_touch_idx+1, min(last_touch_idx+1+ACCEPTANCE_WINDOW_CANDLES, len(candles_5m))):
        c = candles_5m[j]
        close = float(c['c'])
        total_candles += 1
        if event.direction == "LONG":
            if close > event.price_low * 1.01:
                accepted_candles += 1
        else:
            if close < event.price_high * 0.99:
                accepted_candles += 1
    
    if total_candles == 0:
        return 0.0
    
    acceptance_pct = (accepted_candles / total_candles) * 100
    
    update_zone_memory_v6(coin, event.type, event.price_low, event.price_high, acceptance_pct)
    
    return acceptance_pct

def compute_persistence_strength_v6(coin: str, event, master: Dict) -> float:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < 2:
        return 0.0
    
    consecutive_outside = 0
    for i in range(len(candles_5m)-1, max(0, len(candles_5m)-8), -1):
        c = candles_5m[i]
        close = float(c['c'])
        if event.direction == "LONG":
            if close > event.price_low * 1.005:
                consecutive_outside += 1
            else:
                break
        else:
            if close < event.price_high * 0.995:
                consecutive_outside += 1
            else:
                break
    
    return min(100, consecutive_outside * 20)

def get_filter_weights_v6(volatility_regime: str, market_regime: str) -> Dict[str, float]:
    if volatility_regime == "HIGH_VOLATILITY":
        return FILTER_WEIGHTS_BY_REGIME["HIGH_VOLATILITY"]
    elif volatility_regime == "LOW_VOLATILITY":
        return FILTER_WEIGHTS_BY_REGIME["LOW_VOLATILITY"]
    elif market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        return FILTER_WEIGHTS_BY_REGIME["TRENDING"]
    else:
        return FILTER_WEIGHTS_BY_REGIME["NORMAL_VOLATILITY"]

def compute_filter_score_v6(rejection_strength: float, acceptance_strength: float, 
                             persistence_strength: float, volatility_regime: str, 
                             market_regime: str) -> float:
    weights = get_filter_weights_v6(volatility_regime, market_regime)
    score = (
        rejection_strength * weights["rejection"] +
        acceptance_strength * weights["acceptance"] +
        persistence_strength * weights["persistence"]
    )
    return min(100.0, score)

# ========== V6: INTENT ENGINE ==========
def classify_market_intent_v6(coin: str, event_type: str, direction: str,
                               delta_shift: float, oi_roc: float, vol_spike: float,
                               market_state: MarketState, cvd_accel: bool,
                               funding_pct: float) -> Tuple[MarketIntent, str, float]:
    confidence = 60.0
    
    if event_type == "LIQUIDITY" and abs(delta_shift) > 5 and vol_spike > 2.0:
        intent = MarketIntent.SEEK_LIQUIDITY
        explanation = f"Liquidity sweep with strong delta ({delta_shift:+.1f}%) and volume spike {vol_spike:.1f}x"
        confidence = min(90, 70 + abs(delta_shift) * 2)
        
    elif event_type == "LIQUIDITY" and abs(delta_shift) < 2 and vol_spike < 1.2:
        intent = MarketIntent.TRAP
        explanation = "Liquidity sweep without flow confirmation, potential trap"
        confidence = 65
        
    elif event_type in ("OB", "OB_FLOW", "FVG", "FVG_FLOW"):
        if (direction == "LONG" and delta_shift > 3 and oi_roc > 2) or \
           (direction == "SHORT" and delta_shift < -3 and oi_roc > 2):
            intent = MarketIntent.CONTINUE
            explanation = f"Continuation: delta {delta_shift:+.1f}%, OI +{oi_roc:.1f}%"
            confidence = min(85, 65 + abs(delta_shift))
        else:
            intent = MarketIntent.ACCEPT
            explanation = "Acceptance zone, price consolidation"
            confidence = 60
            
    elif (direction == "SHORT" and funding_pct > 0.05 and oi_roc > 5) or \
         (direction == "LONG" and funding_pct < -0.05 and oi_roc > 5):
        intent = MarketIntent.DISTRIBUTE
        explanation = f"Distribution detected: funding {funding_pct:+.3f}%, OI +{oi_roc:.1f}%"
        confidence = 75
        
    else:
        intent = MarketIntent.ACCEPT
        explanation = "Standard acceptance, no strong intent detected"
        confidence = 50
    
    return intent, explanation, confidence

# ========== V6: WHY NOT EXPLANATION ==========
def generate_why_not_explanation_v6(coin: str, direction: str, funding_pct: float, 
                                     entropy_market: int, oi_roc: float, 
                                     market_intent: MarketIntent,
                                     active_candidates_count: int,
                                     fatigue_penalty: float) -> str:
    deterrents = []
    
    if abs(funding_pct) > 0.04:
        deterrents.append(f"funding {funding_pct:+.3f}% (panas)")
    elif abs(funding_pct) > 0.02:
        deterrents.append(f"funding {funding_pct:+.3f}% (mulai panas)")
    
    if entropy_market > 65:
        deterrents.append(f"entropy {entropy_market} (pasar kacau)")
    elif entropy_market > 50:
        deterrents.append(f"entropy {entropy_market} (cukup chaotic)")
    
    if oi_roc < -3:
        deterrents.append(f"OI unwind {oi_roc:.1f}% (posisi tutup)")
    
    if market_intent == MarketIntent.TRAP:
        deterrents.append("potential trap detected")
    
    if active_candidates_count > 3:
        deterrents.append(f"{active_candidates_count} other active setups")
    
    if fatigue_penalty < 0.7:
        deterrents.append(f"fatigue penalty {fatigue_penalty:.0%}")
    
    if not deterrents:
        return "no strong deterrents"
    
    return ", ".join(deterrents[:3])
    
    
# ========== DATACLASSES FOR EVENTS & THESIS ==========
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
    first_seen: float = field(default_factory=time.time)

@dataclass
class Thesis:
    statement: str
    expected_trigger: str
    invalidation: str
    confirmation: str
    destination: str
    direction: str
    timeframe: str = "1h"

@dataclass
class PendingSetup:
    setup_id: str
    coin: str
    thesis: Thesis
    event_type: str
    entry_price: float
    sl_price: float
    tp_price: float
    rr: float
    created_at: float
    expires_at: float
    state: SetupState = SetupState.PENDING
    trigger_reason: str = ""

# ========== AREA VALIDATION ==========
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

# ========== EVENT DETECTION ==========
def detect_displacement(c1: dict, c2: dict, vol_multiplier: float = 1.5) -> bool:
    try:
        range1 = float(c1['h']) - float(c1['l'])
        range2 = float(c2['h']) - float(c2['l'])
        vol1 = float(c1.get('v', 1))
        vol2 = float(c2.get('v', 1))
        if range1 == 0:
            return False
        return (range2 / range1 >= vol_multiplier) and (vol2 >= vol1 * 1.2)
    except:
        return False

def find_liquidity_sweep(candles, current_price, vol_spike) -> Optional[TradeEvent]:
    highs, lows = detect_swing_points(candles, lookback=3)
    if highs and current_price >= highs[-1][1] * 0.998 and vol_spike > 1.5:
        displaced = len(candles) >= 2 and detect_displacement(candles[-2], candles[-1])
        conf = 70 + (10 if vol_spike > 2 else 0) + (10 if displaced else 0)
        return TradeEvent("LIQUIDITY", highs[-1][1]*0.999, highs[-1][1]*1.001, 80, "SHORT",
                          {"displaced": displaced}, confidence=conf, source_count=1)
    if lows and current_price <= lows[-1][1] * 1.002 and vol_spike > 1.5:
        displaced = len(candles) >= 2 and detect_displacement(candles[-2], candles[-1])
        conf = 70 + (10 if vol_spike > 2 else 0) + (10 if displaced else 0)
        return TradeEvent("LIQUIDITY", lows[-1][1]*0.999, lows[-1][1]*1.001, 80, "LONG",
                          {"displaced": displaced}, confidence=conf, source_count=1)
    return None

def find_ob(candles, direction, current_price, max_dist_pct=2.0, master=None, coin=None) -> Optional[TradeEvent]:
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
                    fresh = False
                    break
            if fresh:
                mid = (ob_low+ob_high)/2
                dist = abs(mid-current_price)/current_price*100 if current_price>0 else 99
                if dist <= max_dist_pct:
                    if validate_ob_with_volume_oi(coin, i, master):
                        return TradeEvent("OB", ob_low, ob_high, 75, "LONG", {"idx": i}, confidence=70, source_count=1)
        if direction == "SHORT" and cl > o and nc < no and nc < float(c['l']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i+2, len(candles)-1):
                if float(candles[j]['c']) > ob_high:
                    fresh = False
                    break
            if fresh:
                mid = (ob_low+ob_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    if validate_ob_with_volume_oi(coin, i, master):
                        return TradeEvent("OB", ob_low, ob_high, 75, "SHORT", {"idx": i}, confidence=70, source_count=1)
    return None

def find_fvg_advanced(candles, current_price, max_dist_pct=2.0, master=None, coin=None) -> Optional[TradeEvent]:
    for i in range(len(candles)-1, 1, -1):
        c1 = candles[i-2]
        c3 = candles[i]
        c1h, c1l = float(c1['h']), float(c1['l'])
        c3h, c3l = float(c3['h']), float(c3['l'])
        
        if c3l > c1h:
            gap_low, gap_high = c1h, c3l
            gap_pct = (gap_high - gap_low)/gap_low*100 if gap_low>0 else 0
            if gap_pct < 0.15:
                continue
            filled = 0.0
            for j in range(i+1, len(candles)-1):
                close = float(candles[j]['c'])
                if close <= gap_low:
                    filled = 1.0
                    break
                elif close < gap_high:
                    filled = max(filled, (close - gap_low)/(gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low+gap_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bullish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(coin, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct>0.3 else 0) + (15 if filled<0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "LONG", 
                                          {"fill_ratio": filled}, confidence=conf, source_count=1)
        
        if c3h < c1l:
            gap_low, gap_high = c3h, c1l
            gap_pct = (gap_high - gap_low)/gap_low*100
            if gap_pct < 0.15:
                continue
            filled = 0.0
            for j in range(i+1, len(candles)-1):
                close = float(candles[j]['c'])
                if close >= gap_high:
                    filled = 1.0
                    break
                elif close > gap_low:
                    filled = max(filled, (gap_high - close)/(gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low+gap_high)/2
                dist = abs(mid-current_price)/current_price*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bearish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(coin, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct>0.3 else 0) + (15 if filled<0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "SHORT", 
                                          {"fill_ratio": filled}, confidence=conf, source_count=1)
    return None

# ========== ORDERBOOK FLOW & VACUUM ==========
def get_bid_wall_level(coin: str):
    try:
        l2 = info.l2_snapshot(coin)
        best_usd, best_px = 0.0, 0.0
        for b in l2['levels'][0][:10]:
            usd = float(b['sz']) * float(b['px'])
            if usd > best_usd:
                best_usd, best_px = usd, float(b['px'])
        return best_usd, best_px
    except:
        return 0.0, 0.0

def get_ask_wall_level(coin: str):
    try:
        l2 = info.l2_snapshot(coin)
        best_usd, best_px = 0.0, 0.0
        for a in l2['levels'][1][:10]:
            usd = float(a['sz']) * float(a['px'])
            if usd > best_usd:
                best_usd, best_px = usd, float(a['px'])
        return best_usd, best_px
    except:
        return 0.0, 0.0

def detect_liquidity_vacuum(coin: str):
    try:
        l2 = info.l2_snapshot(coin)
        bids = l2['levels'][0]
        asks = l2['levels'][1]
        def usd_depth(levels, n):
            return sum(float(x['sz']) * float(x['px']) for x in levels[:n])
        near = usd_depth(bids, 5) + usd_depth(asks, 5)
        total = usd_depth(bids, 20) + usd_depth(asks, 20)
        if total == 0:
            return False, 0, 0, 0, 0
        ratio = near / total
        drop_ratio = 1 - ratio
        severity = int(drop_ratio * 100)
        return ratio < 0.3, severity, near, total, drop_ratio
    except:
        return False, 0, 0, 0, 0

def find_ob_from_orderbook(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        delta_shift = get_delta_shift(coin)
        bid_wall, bid_price = get_bid_wall_level(coin)
        if bid_wall >= MIN_OB_FLOW_WALL_USD and delta_shift > MIN_OB_FLOW_DELTA_SHIFT:
            if current_price <= bid_price * 1.005:
                conf = min(85, 70 + int(delta_shift / 2))
                return TradeEvent("OB_FLOW", bid_price*0.998, bid_price*1.002, 75, "LONG",
                                  {"wall_usd": bid_wall, "delta_shift": delta_shift}, confidence=conf, source_count=1)
        ask_wall, ask_price = get_ask_wall_level(coin)
        if ask_wall >= MIN_OB_FLOW_WALL_USD and delta_shift < -MIN_OB_FLOW_DELTA_SHIFT:
            if current_price >= ask_price * 0.995:
                conf = min(85, 70 + int(abs(delta_shift) / 2))
                return TradeEvent("OB_FLOW", ask_price*0.998, ask_price*1.002, 75, "SHORT",
                                  {"wall_usd": ask_wall, "delta_shift": delta_shift}, confidence=conf, source_count=1)
    except Exception as e:
        logger.debug(f"OB_FLOW error {coin}: {e}")
    return None

def find_fvg_from_flow(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        delta_shift = get_delta_shift(coin)
        cvd_change = get_cvd(coin, 30) - get_cvd(coin, 60)
        if abs(cvd_change) < MIN_FVG_FLOW_CVD_ACCEL:
            return None
        if cvd_change > MIN_FVG_FLOW_CVD_ACCEL and delta_shift > MIN_FVG_FLOW_DELTA_DIVERGENCE:
            fair_price = current_price * (1 + cvd_change / 100)
            conf = min(80, 60 + int(cvd_change * 10))
            return TradeEvent("FVG_FLOW", current_price, max(current_price, fair_price), 65, "LONG",
                              {"cvd_change": cvd_change, "delta_shift": delta_shift}, confidence=conf, source_count=1)
        if cvd_change < -MIN_FVG_FLOW_CVD_ACCEL and delta_shift < -MIN_FVG_FLOW_DELTA_DIVERGENCE:
            fair_price = current_price * (1 + cvd_change / 100)
            conf = min(80, 60 + int(abs(cvd_change) * 10))
            return TradeEvent("FVG_FLOW", min(current_price, fair_price), current_price, 65, "SHORT",
                              {"cvd_change": cvd_change, "delta_shift": delta_shift}, confidence=conf, source_count=1)
    except Exception as e:
        logger.debug(f"FVG_FLOW error {coin}: {e}")
    return None

def find_liquidity_vacuum_area(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        is_vacuum, severity, depth_now, depth_max, drop_ratio = detect_liquidity_vacuum(coin)
        if is_vacuum and severity >= LIQUIDITY_VACUUM_AREA_THRESHOLD:
            atr_pct = get_atr_pct(coin, 14, "1h", master)
            vacuum_range = atr_pct * 0.5
            low = current_price * (1 - vacuum_range / 100)
            high = current_price * (1 + vacuum_range / 100)
            conf = min(80, 55 + int(severity / 2))
            return TradeEvent("VACUUM", low, high, 60, "BOTH",
                              {"severity": severity, "depth_drop_pct": drop_ratio * 100}, confidence=conf, source_count=1)
    except Exception as e:
        logger.debug(f"VACUUM_AREA error {coin}: {e}")
    return None

def collect_all_events(coin: str, current_price: float, master: Dict) -> List[TradeEvent]:
    candles_1h = get_candles(coin, "1h", 100, master)
    if not candles_1h:
        return []
    vol_spike = get_volume_spike(coin, master)
    events = []
    
    liq = find_liquidity_sweep(candles_1h, current_price, vol_spike)
    if liq:
        events.append(liq)
    
    ob_long = find_ob(candles_1h, "LONG", current_price, master=master, coin=coin)
    ob_short = find_ob(candles_1h, "SHORT", current_price, master=master, coin=coin)
    if ob_long:
        events.append(ob_long)
    if ob_short:
        events.append(ob_short)
    
    fvg = find_fvg_advanced(candles_1h, current_price, master=master, coin=coin)
    if fvg:
        events.append(fvg)
    
    ob_flow = find_ob_from_orderbook(coin, current_price, master)
    if ob_flow:
        events.append(ob_flow)
    
    fvg_flow = find_fvg_from_flow(coin, current_price, master)
    if fvg_flow:
        events.append(fvg_flow)
    
    vacuum_area = find_liquidity_vacuum_area(coin, current_price, master)
    if vacuum_area:
        events.append(vacuum_area)
    
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
    
    
# ========== SCORING & FILTERS ==========
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
    
    if event.type in ("OB_FLOW", "FVG_FLOW", "VACUUM"):
        base = min(100, base + 15)
        reasons.append("flow_based")
        if event.type == "OB_FLOW" and event.extra.get("wall_usd", 0) > 1_000_000:
            base = min(100, base + 10)
            reasons.append("big_wall")
        if event.type == "VACUUM":
            severity = event.extra.get("severity", 0)
            base = min(100, base + min(15, severity // 4))
            reasons.append(f"vacuum_{severity}")
    
    return int(base), reasons

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

# ========== EXHAUSTION & MOMENTUM ==========
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
        if delta_shift < 0:
            exhaustion += 30
        if vol_spike < 0.8:
            exhaustion += 20
        if oi_roc < -2:
            exhaustion += 20
    elif price_roc < -0.2:
        if delta_shift > 0:
            exhaustion += 30
        if vol_spike < 0.8:
            exhaustion += 20
        if oi_roc < -2:
            exhaustion += 20
    
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
        if roc5 > 0.5 and roc5 > roc15:
            roc_score = 85
        elif roc5 > 0.2:
            roc_score = 70
        elif roc5 < -0.5 and roc5 < roc15:
            roc_score = 85
        elif roc5 < -0.2:
            roc_score = 70
        else:
            roc_score = 50
    
    vol_spike = get_volume_spike(coin, master)
    if vol_spike >= 2.0:
        vol_score = 90
    elif vol_spike >= 1.5:
        vol_score = 70
    elif vol_spike >= 1.2:
        vol_score = 50
    else:
        vol_score = 30
    
    delta_shift = get_delta_shift(coin)
    if delta_shift > 8:
        delta_score = 90
    elif delta_shift > 4:
        delta_score = 70
    elif delta_shift > 2:
        delta_score = 50
    else:
        delta_score = 30
    
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
    return max(50, min(85, th))

def get_dynamic_min_rr(market_regime: str) -> float:
    return {"TRENDING_UP": 2.0, "TRENDING_DOWN": 2.0, "RANGING": 1.8, "PANIC": 1.2}.get(market_regime, 1.5)

def get_confidence_label(score: int) -> str:
    if score >= 80:
        return "🔥 VERY STRONG"
    if score >= 70:
        return "🟢 STRONG"
    if score >= 60:
        return "🟡 MODERATE"
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
    
    
# ========== V6: THESIS GENERATION ==========
def generate_thesis_from_event_v6(coin: str, event: TradeEvent, current_price: float,
                                  market_state: MarketState, intent, belief_state: BeliefState) -> Thesis:
    t = event.type
    d = event.direction
    intent_str = intent.value if hasattr(intent, 'value') else str(intent)
    belief_str = belief_state.value if hasattr(belief_state, 'value') else str(belief_state)
    
    if t == "LIQUIDITY":
        if d == "LONG":
            lvl = event.price_low
            return Thesis(
                statement=f"Liquidity sweep of lows at {fmt_price(lvl)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Bullish reclaim above sweep level within 1-2 candles",
                invalidation=f"Close below {lvl * 0.998:.4f}",
                confirmation="Delta turns positive and sustains >3 for 3x 5m candles",
                destination="Next swing high / L1 liquidity target",
                direction="LONG", timeframe="15m"
            )
        else:
            lvl = event.price_high
            return Thesis(
                statement=f"Liquidity sweep of highs at {fmt_price(lvl)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Bearish rejection below sweep level within 1-2 candles",
                invalidation=f"Close above {lvl * 1.002:.4f}",
                confirmation="Delta turns negative and sustains <-3 for 3x 5m candles",
                destination="Next swing low / liquidity target",
                direction="SHORT", timeframe="15m"
            )
    
    elif t == "OB":
        if d == "LONG":
            return Thesis(
                statement=f"Order block demand at {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Price touches OB zone and shows wick rejection",
                invalidation=f"Close below OB low {event.price_low:.4f}",
                confirmation="Volume spike >1.5x and OI persistence",
                destination="Previous structure high / L1",
                direction="LONG", timeframe="1h"
            )
        else:
            return Thesis(
                statement=f"Order block supply at {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Price touches OB zone and shows rejection",
                invalidation=f"Close above OB high {event.price_high:.4f}",
                confirmation="Volume spike >1.5x and OI persistence",
                destination="Previous structure low",
                direction="SHORT", timeframe="1h"
            )
    
    elif t in ("FVG", "FVG_FLOW"):
        if d == "LONG":
            return Thesis(
                statement=f"Bullish FVG {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Price enters FVG zone",
                invalidation=f"FVG >70% filled without reaction",
                confirmation="CVD acceleration + delta shift positive",
                destination="Premium side of range",
                direction="LONG", timeframe="1h"
            )
        else:
            return Thesis(
                statement=f"Bearish FVG {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                expected_trigger="Price enters FVG zone",
                invalidation=f"FVG >70% filled without reaction",
                confirmation="CVD acceleration + delta shift negative",
                destination="Discount side of range",
                direction="SHORT", timeframe="1h"
            )
    
    elif t == "VACUUM":
        return Thesis(
            statement=f"Liquidity vacuum area - {event.extra.get('severity', 0)}% depth drop - intent: {intent_str}, belief: {belief_str}",
            expected_trigger="Price enters vacuum with directional delta",
            invalidation="Delta neutral or reverses",
            confirmation="Volume spike confirming move into vacuum",
            destination="Next liquidity cluster",
            direction=d, timeframe="5m"
        )
    
    else:
        return Thesis(
            statement=f"{t} {d} setup at {fmt_price(current_price)} - intent: {intent_str}, belief: {belief_str}",
            expected_trigger="Price action confirmation",
            invalidation="Invalidation level breached",
            confirmation="Flow confirms direction",
            destination="ATR-based target",
            direction=d, timeframe="1h"
        )

# ========== V6: TRIGGER PROBABILITY ==========
def compute_trigger_strength_v6(setup: PendingSetup, current_price: float, 
                                 delta: float, candles_5m: List[dict]) -> Tuple[float, str]:
    thesis = setup.thesis
    exp = thesis.expected_trigger.lower()
    d = thesis.direction
    
    strengths = []
    reasons = []
    
    if "reclaim" in exp and "above" in exp:
        if d == "LONG" and current_price > setup.entry_price:
            reclaim_dist = (current_price - setup.entry_price) / setup.entry_price * 100
            reclaim_strength = min(40, reclaim_dist * 80)
            strengths.append(reclaim_strength)
            reasons.append(f"reclaim {reclaim_dist:.2f}%")
    
    if "rejection" in exp and "below" in exp:
        if d == "SHORT" and current_price < setup.entry_price:
            reclaim_dist = (setup.entry_price - current_price) / setup.entry_price * 100
            reclaim_strength = min(40, reclaim_dist * 80)
            strengths.append(reclaim_strength)
            reasons.append(f"rejection {reclaim_dist:.2f}%")
    
    if "micro bos" in exp:
        if d == "LONG" and is_micro_bos_up(candles_5m):
            strengths.append(30)
            reasons.append("micro BOS up")
        elif d == "SHORT" and is_micro_bos_down(candles_5m):
            strengths.append(30)
            reasons.append("micro BOS down")
    
    if "delta" in exp:
        if d == "LONG" and delta > 0:
            delta_strength = min(30, delta * 6)
            strengths.append(delta_strength)
            reasons.append(f"delta +{delta:.1f}")
        elif d == "SHORT" and delta < 0:
            delta_strength = min(30, abs(delta) * 6)
            strengths.append(delta_strength)
            reasons.append(f"delta {delta:.1f}")
    
    if "touches" in exp or "enters" in exp:
        if d == "LONG" and current_price <= setup.entry_price * 1.005:
            touch_dist = (setup.entry_price - current_price) / setup.entry_price * 100
            touch_strength = min(20, 20 - touch_dist * 10)
            strengths.append(touch_strength)
            reasons.append("touched zone")
        elif d == "SHORT" and current_price >= setup.entry_price * 0.995:
            touch_dist = (current_price - setup.entry_price) / setup.entry_price * 100
            touch_strength = min(20, 20 - touch_dist * 10)
            strengths.append(touch_strength)
            reasons.append("touched zone")
    
    if "sustains" in exp:
        if d == "LONG" and current_price >= setup.entry_price and delta > 0:
            strengths.append(15)
            reasons.append("sustaining")
        elif d == "SHORT" and current_price <= setup.entry_price and delta < 0:
            strengths.append(15)
            reasons.append("sustaining")
    
    if not strengths:
        return 0.0, "no trigger"
    
    total_strength = min(100.0, sum(strengths))
    return total_strength, " + ".join(reasons[:2])

def is_micro_bos_up(candles_5m: List[dict]) -> bool:
    if not candles_5m or len(candles_5m) < 6:
        return False
    recent_lows = [float(candles_5m[i]['l']) for i in range(max(0, len(candles_5m)-6), len(candles_5m)-1)
                   if i >= 2 and float(candles_5m[i]['l']) < float(candles_5m[i-1]['l'])
                   and float(candles_5m[i]['l']) < float(candles_5m[i-2]['l'])]
    if not recent_lows:
        return False
    return float(candles_5m[-1]['c']) > min(recent_lows) * 1.002

def is_micro_bos_down(candles_5m: List[dict]) -> bool:
    if not candles_5m or len(candles_5m) < 6:
        return False
    recent_highs = [float(candles_5m[i]['h']) for i in range(max(0, len(candles_5m)-6), len(candles_5m)-1)
                    if i >= 2 and float(candles_5m[i]['h']) > float(candles_5m[i-1]['h'])
                    and float(candles_5m[i]['h']) > float(candles_5m[i-2]['h'])]
    if not recent_highs:
        return False
    return float(candles_5m[-1]['c']) < max(recent_highs) * 0.998

def monitor_pending_setups_v6():
    """V6: Monitor pending setups dengan trigger probability"""
    while not _shutdown_event.is_set():
        try:
            now = time.time()
            with _pending_setups_lock:
                setups_snapshot = list(_pending_setups.items())
            
            for setup_id, setup in setups_snapshot:
                if now > setup.expires_at:
                    with _pending_setups_lock:
                        _pending_setups.pop(setup_id, None)
                    logger.debug(f"Setup expired: {setup_id}")
                    continue
                
                try:
                    with _last_mids_lock:
                        cached = _last_mids.get(setup.coin)
                    if cached:
                        current_price = cached[0]
                    else:
                        current_price = float(info.all_mids().get(setup.coin, 0))
                except:
                    continue
                
                if current_price == 0:
                    continue
                
                thesis = setup.thesis
                try:
                    inv_parts = thesis.invalidation.split()
                    inv_level = float(inv_parts[-1])
                    if thesis.direction == "LONG" and current_price < inv_level:
                        with _pending_setups_lock:
                            _pending_setups.pop(setup_id, None)
                        logger.info(f"Setup invalidated {setup_id}: price {fmt_price(current_price)} < {fmt_price(inv_level)}")
                        continue
                    if thesis.direction == "SHORT" and current_price > inv_level:
                        with _pending_setups_lock:
                            _pending_setups.pop(setup_id, None)
                        logger.info(f"Setup invalidated {setup_id}: price {fmt_price(current_price)} > {fmt_price(inv_level)}")
                        continue
                except (ValueError, IndexError):
                    pass
                
                delta = get_ob_delta(setup.coin)
                candles_5m = get_candles(setup.coin, "5m", 10)
                trigger_strength, trigger_reason = compute_trigger_strength_v6(setup, current_price, delta, candles_5m or [])
                
                if trigger_strength >= 30:
                    logger.info(f"Setup TRIGGERED {setup_id}: {trigger_reason} (strength={trigger_strength:.0f})")
                    signal_id = generate_signal_id(setup.coin, thesis.direction)
                    data_conf = 75
                    _atr_pct = get_atr_pct(setup.coin, 14, "1h", None)
                    
                    if not PAPER_MODE:
                        save_signal_v6(signal_id, setup.coin, thesis.direction, 85,
                                      current_price, setup.sl_price, setup.tp_price, setup.rr,
                                      f"Thesis triggered: {trigger_reason} (strength={trigger_strength:.0f}) | {thesis.statement}",
                                      data_conf, thesis.statement, thesis.invalidation, thesis.confirmation,
                                      "BALANCED", "", 0.0, 1.0, 100.0, 0.0, "SEEKING", 0.0, "normal", 50.0)
                        threading.Thread(target=evaluate_signal_v6, args=(
                            signal_id, setup.coin, thesis.direction, current_price,
                            setup.sl_price, setup.tp_price, data_conf,
                            0, 0, 0,
                            thesis.statement, thesis.invalidation, thesis.confirmation,
                            get_evaluation_delay(_atr_pct, setup.rr, "NORMAL")
                        ), daemon=True).start()
                    
                    alert = {
                        "coin": setup.coin,
                        "direction": thesis.direction,
                        "score": 85,
                        "entry": current_price,
                        "sl": setup.sl_price,
                        "tp": setup.tp_price,
                        "rr": setup.rr,
                        "reason": f"Thesis triggered: {trigger_reason}",
                        "area": setup.event_type,
                        "label": get_confidence_label(85),
                        "contradiction": False,
                        "exhaustion": 0,
                        "entropy_market": 0,
                        "evidence_families": 0,
                        "positive_evidence": ["thesis_trigger"],
                        "negative_evidence": "none",
                        "data_confidence": data_conf,
                        "contributions": {},
                        "execution_mode": "BALANCED",
                        "intent_type": "",
                        "decision_energy": 0.0,
                        "position_size_mult": 1.0,
                        "filter_score": 100.0,
                        "why_not": "no deterrents",
                        "trigger_strength": trigger_strength,
                        "belief_state": "SEEKING",
                        "commitment_score": 0.0,
                        "time_pressure": "normal",
                        "mode_aggressive": 0.0,
                        "mode_balanced": 1.0,
                        "mode_precision": 0.0,
                        "hypothesis": {
                            "thesis": thesis.statement,
                            "invalidate": thesis.invalidation,
                            "observe": thesis.confirmation,
                            "destination": thesis.destination,
                            "timeframe": thesis.timeframe
                        },
                        "explanation": f"⚡ Thesis triggered: {trigger_reason}\n📋 {thesis.statement}"
                    }
                    send_alert_v6(alert)
                    
                    with _pending_setups_lock:
                        _pending_setups.pop(setup_id, None)
                    time.sleep(0.5)
            
            time.sleep(3)
        except Exception as e:
            logger.error(f"monitor_pending_setups error: {e}")
            time.sleep(5)
            
            
# ========== V6: CHECK ENTRY ALERT - BAGIAN 1/3 ==========
def check_entry_alert_v6(coin: str, mark: float, master_candles: Dict) -> Optional[dict]:
    start_time = time.time()
    api_start = time.time()
    current_time = time.time()
    
    data_confidence, ages = get_data_confidence(coin, mark, current_time)
    if data_confidence < MIN_DATA_CONFIDENCE:
        logger.debug(f"Data confidence too low for {coin}: {data_confidence}% -> skip")
        return None
    
    try:
        atr_pct = get_atr_pct(coin, 14, "1h", master_candles)
        vol_spike = get_volume_spike(coin, master_candles)
        delta = get_ob_delta(coin)
        cvd_accel = get_cvd_acceleration(coin)
        momentum = get_composite_momentum(coin, master_candles)
        structure_valid_long, structure_valid_short = get_structure_valid_separate(coin, master_candles)
        candles_1h = get_candles(coin, "1h", 60, master_candles)
        market_state = get_market_state_from_structure(candles_1h, mark) if candles_1h else MarketState.UNKNOWN
        market_regime, volatility_regime, flow_regime = get_all_regimes()
        
        raw_events = collect_all_events(coin, mark, master_candles)
        if not raw_events:
            return None
        
        clustered = cluster_events(raw_events, price_tolerance=0.005)
        oi_roc = get_oi_roc(coin)
        funding_pct = get_funding_pct(coin)
        update_oi_persistence(coin, oi_roc)
        
        for ev in clustered:
            ev.extra["coin"] = coin
            ev.score, _ = score_event_non_additive(
                ev, mark, delta, vol_spike, oi_roc,
                (structure_valid_long if ev.direction == "LONG" else structure_valid_short),
                cvd_accel, momentum
            )
            penalty = get_zone_penalty_v6(coin, ev.type, ev.price_low, ev.price_high)
            ev.score = max(0, ev.score - penalty)
        
        best_event = max(clustered, key=lambda e: e.score) if clustered else None
        if not best_event or best_event.score < 40:
            return None
        
        # VACUUM direction resolution
        if best_event.type == "VACUUM" and best_event.direction == "BOTH":
            _delta_now = get_ob_delta(coin)
            if _delta_now > 5:
                best_event.direction = "LONG"
                best_event.confidence = min(100, best_event.confidence + 5)
            elif _delta_now < -5:
                best_event.direction = "SHORT"
                best_event.confidence = min(100, best_event.confidence + 5)
            else:
                logger.debug(f"{coin} VACUUM area but delta neutral ({_delta_now:.1f}) -> skip")
                return None
        
        # Market state filter
        if market_state == MarketState.REVERSAL:
            if best_event.type != "LIQUIDITY" and "LIQUIDITY" not in best_event.extra.get("members", []):
                return None
        elif market_state == MarketState.EXPANSION:
            if best_event.type == "LIQUIDITY" or "LIQUIDITY" in best_event.extra.get("members", []):
                return None
        
        # Structure filter
        if best_event.direction == "LONG" and not structure_valid_long:
            logger.debug(f"{coin} LONG rejected: structure_valid_long=False")
            return None
        if best_event.direction == "SHORT" and not structure_valid_short:
            logger.debug(f"{coin} SHORT rejected: structure_valid_short=False")
            return None
        
        # ========== V6: INTENT CLASSIFICATION ==========
        intent, intent_explanation, intent_confidence = classify_market_intent_v6(
            coin, best_event.type, best_event.direction,
            get_delta_shift(coin), oi_roc, vol_spike,
            market_state, cvd_accel, funding_pct
        )
        
        # Convert to legacy IntentType for compatibility
        legacy_intent_map = {
            MarketIntent.SEEK_LIQUIDITY: IntentType.GRAB,
            MarketIntent.TRAP: IntentType.TRAP,
            MarketIntent.ACCEPT: IntentType.ACCEPT,
            MarketIntent.CONTINUE: IntentType.CONTINUE,
            MarketIntent.DISTRIBUTE: IntentType.ACCEPT,
        }
        intent_legacy = legacy_intent_map.get(intent, IntentType.ACCEPT)
        
        # ========== V6: FILTER GRADIENT ==========
        rejection_strength = compute_rejection_strength_v6(coin, best_event, mark, master_candles)
        acceptance_strength = compute_acceptance_strength_v6(coin, best_event, master_candles)
        persistence_strength = compute_persistence_strength_v6(coin, best_event, master_candles)
        
        filter_score = compute_filter_score_v6(
            rejection_strength, acceptance_strength, persistence_strength,
            volatility_regime, market_regime
        )
        
        # ========== V6: FATIGUE PER THESIS FAMILY ==========
        fatigue_penalty = get_fatigue_penalty_by_family(best_event.type)
        
        # Score calculation
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
        
        contradiction = (score_long > 55 and score_short > 55)
        
        # Independent evidence families
        price_ok, flow_ok, pos_ok, evidence_reasons = get_independent_evidence_families(
            coin, best_event.direction, master_candles
        )
        evidence_families = (1 if price_ok else 0) + (1 if flow_ok else 0) + (1 if pos_ok else 0)
        exhaustion = compute_exhaustion_score(coin, master_candles)
        
        # ========== V6: 3 JENIS ENTROPY ==========
        entropy_data = compute_data_entropy(ages)
        entropy_market = compute_market_entropy_v6(coin, master_candles)
        score_variance = abs(score_long - score_short) if score_long > 0 and score_short > 0 else 0
        event_types = [ev.type for ev in clustered]
        entropy_decision = compute_decision_entropy(score_variance, contradiction, len(event_types) > 2, event_types)
        
        trend_strength = compute_trend_strength_v6(coin, master_candles)
        entropy_threshold = get_dynamic_entropy_threshold_v6(volatility_regime, trend_strength)
        
        # Decision Vector
        decision_score, ev_mult, vec_reason, contributions = compute_decision_vector(
            coin, best_event, score_long, score_short, evidence_families, entropy_market, exhaustion,
            market_regime, volatility_regime, data_confidence
        )
        
        # Counterfactual
        cf_adjusted_score, cf_adjustments = evaluate_counterfactual_influence(
            coin, entropy_market, evidence_families, exhaustion, decision_score, data_confidence
        )
        log_counterfactual(coin, decision_score, cf_adjustments)
        final_score = decision_score
        
        # ========== V6: BELIEF STATE ==========
        current_belief_state, belief_duration = get_belief_state(coin)
        new_belief_state, belief_trigger = update_belief_state(
            coin, best_event.type, filter_score, 0, best_event.score
        )
        
        # ========== V6: TIME PRESSURE ==========
        setup_age_minutes = (time.time() - best_event.first_seen) / 60 if hasattr(best_event, 'first_seen') else 0
        opportunity_decay = 0.08
        competitor_count = len(_active_candidates)
        time_pressure, urgency_score = compute_time_pressure(setup_age_minutes, opportunity_decay, competitor_count)
        
        
# ========== V6: CHECK ENTRY ALERT - BAGIAN 2/3 ==========
        # SL/TP calculation
        sl, tp, rr = calculate_sltp_advanced(coin, mark, best_event.direction, best_event, atr_pct, master_candles)
        min_rr = get_dynamic_min_rr(market_regime)
        min_rr = get_entropy_adjusted_min_rr_v6(min_rr, entropy_market)
        if rr < min_rr:
            update_fatigue_memory(best_event.type)
            return None
        
        # ========== V6: DECISION ENERGY ==========
        confidence_val = compute_confidence_from_score_v6(final_score, data_confidence, evidence_families)
        opportunity_val = compute_opportunity_v6(rr, vol_spike, momentum)
        uncertainty_val = compute_uncertainty_v6(entropy_market, entropy_decision, contradiction, exhaustion)
        decision_energy = compute_decision_energy_v6(confidence_val, opportunity_val, uncertainty_val)
        
        # Update decision energy history untuk acceleration
        update_decision_energy_history(coin, decision_energy)
        decision_acceleration = compute_decision_acceleration(coin)
        
        # ========== V6: POSITION SIZING ==========
        prediction_quality_mult = get_prediction_quality_multiplier(coin)
        position_size_mult = get_position_size_multiplier_v6(entropy_market, prediction_quality_mult, intent_legacy)
        
        # Apply fatigue penalty
        position_size_mult *= fatigue_penalty
        
        # ========== V6: EXECUTION MODE BLEND ==========
        mode_weights = get_execution_mode_blend_v6(decision_energy, entropy_market, decision_acceleration, intent_legacy)
        execution_mode = get_execution_mode_from_blend_v6(mode_weights)
        threshold_boost = get_mode_threshold_boost_from_blend_v6(mode_weights)
        
        # ========== V6: COST OF WAITING ==========
        wait_value, expected_decay, max_wait_reached = compute_value_of_waiting_v5(
            confidence_val, opportunity_val, uncertainty_val, setup_age_minutes
        )
        should_execute, wait_reason, wait_confidence = should_wait_or_execute_v5(
            decision_energy, wait_value, decision_energy
        )
        
        # ========== V6: COMMITMENT SCORE ==========
        commitment_score = compute_commitment_score(
            new_belief_state, confidence_val, time_pressure, position_size_mult, prediction_quality_mult
        )
        
        # Dynamic threshold dengan mode boost dan filter score
        base_threshold = get_dynamic_threshold(coin, market_regime, volatility_regime)
        entropy_adjusted_threshold = get_entropy_adjusted_threshold_v6(base_threshold, entropy_market)
        
        filter_penalty = 1.0 + ((100 - filter_score) / 100) * 0.5
        adjusted_threshold = int(entropy_adjusted_threshold * threshold_boost * filter_penalty)
        
        size_boost = 1.0 + (1.0 - position_size_mult) * 0.2
        final_threshold_with_size = int(adjusted_threshold / size_boost)
        
        # ========== V6: THRESHOLD CHECK ==========
        if final_score < final_threshold_with_size:
            if position_size_mult > 0.3:
                position_size_mult = max(0.15, position_size_mult * 0.7)
                logger.debug(f"{coin} score below threshold, reducing size to {position_size_mult:.2f}x")
            else:
                update_fatigue_memory(best_event.type)
                if not should_execute:
                    add_journal_entry_v6(coin, market_regime, volatility_regime, flow_regime,
                                        new_belief_state.value, score_long, score_short, "WAITING", final_score,
                                        wait_reason, "", entropy_data, entropy_market, entropy_decision,
                                        int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                                        data_confidence, False, execution_mode=execution_mode, intent_type=intent.value,
                                        decision_energy=decision_energy, position_size_mult=position_size_mult,
                                        filter_score=filter_score, rejection_strength=rejection_strength,
                                        acceptance_strength=acceptance_strength, persistence_strength=persistence_strength,
                                        why_not="waiting for better entry", wait_value=wait_value, trigger_strength=0.0,
                                        time_pressure=time_pressure.value, commitment_score=commitment_score,
                                        decision_acceleration=decision_acceleration,
                                        mode_aggressive=mode_weights["aggressive"], mode_balanced=mode_weights["balanced"],
                                        mode_precision=mode_weights["precision"], confidence_breakdown="")
                return None
        
        # UNCLEAR check
        if score_long > UNCLEAR_THRESHOLD and score_short > UNCLEAR_THRESHOLD and abs(score_long - score_short) < UNCLEAR_DIFF:
            add_journal_entry_v6(coin, market_regime, volatility_regime, flow_regime,
                                new_belief_state.value, score_long, score_short, "NO_TRADE", final_score,
                                "Uncertain market", "LONG/SHORT both high", entropy_data, entropy_market, entropy_decision,
                                int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                                data_confidence, False, execution_mode=execution_mode, intent_type=intent.value,
                                decision_energy=decision_energy, position_size_mult=position_size_mult,
                                filter_score=filter_score, why_not="contradiction", wait_value=wait_value, trigger_strength=0.0,
                                time_pressure=time_pressure.value, commitment_score=commitment_score,
                                decision_acceleration=decision_acceleration,
                                mode_aggressive=mode_weights["aggressive"], mode_balanced=mode_weights["balanced"],
                                mode_precision=mode_weights["precision"], confidence_breakdown="")
            update_fatigue_memory(best_event.type)
            return None
        
        # ========== V6: THESIS GENERATION ==========
        thesis_obj = generate_thesis_from_event_v6(coin, best_event, mark, market_state, intent, new_belief_state)
        
        # Register pending setup
        _setup_id = generate_signal_id(coin, best_event.direction) + "_setup"
        _pending_setup = PendingSetup(
            setup_id=_setup_id,
            coin=coin,
            thesis=thesis_obj,
            event_type=best_event.type,
            entry_price=mark,
            sl_price=sl,
            tp_price=tp,
            rr=rr,
            created_at=time.time(),
            expires_at=time.time() + _SETUP_EXPIRY_SECONDS,
        )
        with _pending_setups_lock:
            _pending_setups[_setup_id] = _pending_setup
        logger.debug(f"Pending setup registered: {_setup_id}")
        
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
        
        # ========== V6: WHY NOT ==========
        active_count = len(_active_candidates)
        why_not = generate_why_not_explanation_v6(coin, best_event.direction, funding_pct, entropy_market, oi_roc, intent, active_count, fatigue_penalty)
        
        # Confidence breakdown untuk UI
        confidence_breakdown = f"S:{score_long:.0f}|F:{filter_score:.0f}|E:{evidence_families}"
        
        trigger_strength, _ = compute_trigger_strength_v6(_pending_setup, mark, delta, [])
        
        reason = (f"{best_event.type} | Intent:{intent.value} | Belief:{new_belief_state.value} | "
                  f"Mode:{execution_mode} | Filter:{filter_score:.0f} | DE:{decision_energy:.1f} | Score:{final_score}")
        
        signal_id = generate_signal_id(coin, best_event.direction)
        eval_delay = get_evaluation_delay(atr_pct, rr, market_regime)
        
        
# ========== V6: CHECK ENTRY ALERT - BAGIAN 3/3 ==========
        if not PAPER_MODE:
            save_signal_v6(signal_id, coin, best_event.direction, final_score, mark, sl, tp, rr, reason,
                          data_confidence, thesis_obj.statement, thesis_obj.invalidation, thesis_obj.confirmation,
                          execution_mode, intent.value, decision_energy, position_size_mult, filter_score, intent_confidence,
                          new_belief_state.value, commitment_score, time_pressure.value, prediction_quality_mult * 100,
                          mode_weights["aggressive"], mode_weights["balanced"], mode_weights["precision"])
            add_journal_entry_v6(coin, market_regime, volatility_regime, flow_regime,
                                new_belief_state.value, score_long, score_short, best_event.direction, final_score,
                                reason, negative_str, entropy_data, entropy_market, entropy_decision,
                                int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                                data_confidence, True, contribution=str(contributions),
                                execution_mode=execution_mode, intent_type=intent.value, decision_energy=decision_energy,
                                position_size_mult=position_size_mult, filter_score=filter_score,
                                rejection_strength=rejection_strength, acceptance_strength=acceptance_strength,
                                persistence_strength=persistence_strength, why_not=why_not, wait_value=wait_value,
                                trigger_strength=trigger_strength, time_pressure=time_pressure.value,
                                commitment_score=commitment_score, decision_acceleration=decision_acceleration,
                                mode_aggressive=mode_weights["aggressive"], mode_balanced=mode_weights["balanced"],
                                mode_precision=mode_weights["precision"], confidence_breakdown=confidence_breakdown)
            threading.Thread(target=evaluate_signal_v6, args=(
                signal_id, coin, best_event.direction, mark, sl, tp, data_confidence,
                entropy_market, evidence_families, exhaustion, thesis_obj.statement, 
                thesis_obj.invalidation, thesis_obj.confirmation, eval_delay,
                best_event.price_low, best_event.price_high, best_event.direction
            ), daemon=True).start()
        else:
            add_journal_entry_v6(coin, market_regime, volatility_regime, flow_regime,
                                new_belief_state.value, score_long, score_short, best_event.direction, final_score,
                                reason, negative_str, entropy_data, entropy_market, entropy_decision,
                                int((time.time() - start_time) * 1000), int((time.time() - api_start) * 1000),
                                data_confidence, True, contribution=str(contributions),
                                execution_mode=execution_mode, intent_type=intent.value, decision_energy=decision_energy,
                                position_size_mult=position_size_mult, filter_score=filter_score,
                                rejection_strength=rejection_strength, acceptance_strength=acceptance_strength,
                                persistence_strength=persistence_strength, why_not=why_not, wait_value=wait_value,
                                trigger_strength=trigger_strength, time_pressure=time_pressure.value,
                                commitment_score=commitment_score, decision_acceleration=decision_acceleration,
                                mode_aggressive=mode_weights["aggressive"], mode_balanced=mode_weights["balanced"],
                                mode_precision=mode_weights["precision"], confidence_breakdown=confidence_breakdown)
        
        update_active_candidate_v6(coin, mark, entropy_market, mark)
        
        positive_factors = [best_event.type] + evidence_reasons
        if vol_spike >= 1.5:
            positive_factors.append("volume")
        if cvd_accel:
            positive_factors.append("cvd_accel")
        
        explanation = explain_decision_with_contribution(
            coin, best_event.direction, final_score,
            positive_factors, negative_reasons, contributions,
            entropy_market, final_threshold_with_size, data_confidence
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
            "contradiction": contradiction,
            "exhaustion": exhaustion,
            "entropy_data": entropy_data,
            "entropy_market": entropy_market,
            "entropy_decision": entropy_decision,
            "evidence_families": evidence_families,
            "positive_evidence": evidence_reasons,
            "negative_evidence": negative_str,
            "data_confidence": data_confidence,
            "contributions": contributions,
            "execution_mode": execution_mode,
            "intent_type": intent.value,
            "decision_energy": decision_energy,
            "position_size_mult": position_size_mult,
            "filter_score": filter_score,
            "rejection_strength": rejection_strength,
            "acceptance_strength": acceptance_strength,
            "persistence_strength": persistence_strength,
            "why_not": why_not,
            "wait_value": wait_value,
            "trigger_strength": trigger_strength,
            "belief_state": new_belief_state.value,
            "commitment_score": commitment_score,
            "time_pressure": time_pressure.value,
            "decision_acceleration": decision_acceleration,
            "fatigue_penalty": fatigue_penalty,
            "mode_aggressive": mode_weights["aggressive"],
            "mode_balanced": mode_weights["balanced"],
            "mode_precision": mode_weights["precision"],
            "confidence_breakdown": confidence_breakdown,
            "hypothesis": {
                "thesis": thesis_obj.statement,
                "invalidate": thesis_obj.invalidation,
                "observe": thesis_obj.confirmation,
                "destination": thesis_obj.destination,
                "timeframe": thesis_obj.timeframe
            },
            "explanation": explanation
        }
    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None
        
# ========== V6: EVALUATE SIGNAL ==========
def evaluate_signal_v6(signal_id, coin, direction, entry, sl, tp, data_confidence,
                       entropy_market, evidence_families, exhaustion, thesis, invalidate, observe, eval_delay,
                       predicted_zone_low, predicted_zone_high, predicted_direction):
    time.sleep(eval_delay)
    if _shutdown_event.is_set():
        return
    try:
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
        
        mids = info.all_mids()
        price = float(mids.get(coin, 0))
        if price == 0:
            return
        
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
        
        is_win = outcome in ("TP_HIT", "PARTIAL_WIN")
        hypothesis_validated = is_win
        if mfe > abs(mae) * 1.5:
            hypothesis_validated = True
        
        update_signal_outcome_v6(signal_id, outcome, pnl, price, mfe, mae, hypothesis_validated)
        add_hypothesis_validation(signal_id, thesis, outcome, pnl, hypothesis_validated)
        
        # V6: Update prediction quality (bukan winrate)
        direction_match = (direction == predicted_direction)
        zone_accuracy = 1.0 if predicted_zone_low <= entry <= predicted_zone_high else 0.5
        timing_quality = min(1.0, mfe / abs(mae)) if mae != 0 else 0.5
        
        prediction_quality = evaluate_prediction_quality(
            signal_id, coin, predicted_direction, direction, entry,
            predicted_zone_low, predicted_zone_high, mfe, mae, hypothesis_validated
        )
        update_prediction_memory(coin, prediction_quality)
        
        logger.info(f"Evaluated {signal_id}: {outcome} pnl={pnl:.2f}% pred_quality={prediction_quality:.1f}")
        
        # Reset belief state jika loss
        if outcome in ("SL_HIT", "PARTIAL") and pnl < 0:
            reset_belief_state(coin, f"loss {outcome}")
        
    except Exception as e:
        logger.error(f"Eval error {signal_id}: {e}")

def get_evaluation_delay(atr_pct: float, rr: float, regime: str) -> int:
    base = BASE_EVALUATION_DELAY
    if atr_pct > 2.0:
        base = int(base * 0.6)
    elif atr_pct > 1.2:
        base = int(base * 0.8)
    if rr > 2.5:
        base = int(base * 1.2)
    elif rr < 1.8:
        base = int(base * 0.8)
    if regime in ("PANIC", "VOLATILE"):
        base = int(base * 0.7)
    elif regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base = int(base * 1.1)
    return max(1800, min(14400, base))

# ========== V6: DECISION VECTOR & COUNTERFACTUAL ==========
def compute_decision_vector(coin: str, best_event: TradeEvent, score_long: int, score_short: int,
                            evidence_families: int, entropy: int, exhaustion: int,
                            market_regime: str, volatility_regime: str, data_confidence: int) -> Tuple[int, float, str, Dict[str, int]]:
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        _base_mults = {3: 1.0, 2: 0.75, 1: 0.45}
    elif market_regime in ("PANIC", "VOLATILE"):
        _base_mults = {3: 0.85, 2: 0.6, 1: 0.35}
    else:
        _base_mults = {3: EVIDENCE_MULT_3, 2: EVIDENCE_MULT_2, 1: EVIDENCE_MULT_1}
    
    ev_mult = _base_mults.get(min(evidence_families, 3), EVIDENCE_MULT_1)
    raw_score = score_long if best_event.direction == "LONG" else score_short
    contradiction = (score_long > 55 and score_short > 55)
    contra_penalty = 40 if contradiction else 0
    exhaustion_penalty = min(50, exhaustion)
    quality_penalty = max(0, (100 - data_confidence) * 0.2)
    tmp_score = raw_score * ev_mult - contra_penalty - exhaustion_penalty - quality_penalty
    tmp_score = max(0, min(100, int(tmp_score)))
    
    contributions = {
        "evidence": int(raw_score * (ev_mult - 1)),
        "contra": -contra_penalty if contradiction else 0,
        "exhaust": -exhaustion_penalty,
        "data": -int(quality_penalty)
    }
    return tmp_score, ev_mult, "", contributions

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

def explain_decision_with_contribution(coin: str, direction: str, score: int,
                                       positive_factors: List[str], negative_factors: List[str],
                                       contributions: Dict[str, int],
                                       entropy: int, threshold: int, data_confidence: int) -> str:
    pos_str = ", ".join(positive_factors[:3]) if positive_factors else "none"
    neg_str = ", ".join(negative_factors[:3]) if negative_factors else "none"
    contrib_str = " | ".join([f"{k}:{v:+d}" for k, v in contributions.items()]) if contributions else "none"
    explain = (f"📊 *Decision Explanation*\n"
               f"✅ Positive: {pos_str}\n"
               f"❌ Negative: {neg_str}\n"
               f"📈 Contribution: {contrib_str}\n"
               f"🌀 Market Entropy: {entropy}\n"
               f"📡 Data confidence: {data_confidence}%\n"
               f"🎯 Final score: {score}\n")
    return explain

# ========== V6: POSITION SIZING ==========
def get_position_size_multiplier_v6(entropy: int, prediction_quality_mult: float, intent) -> float:
    entropy_factor = 1.0 - (entropy / 100) * SIZE_MULTIPLIER_CONFIG["entropy_factor"]
    entropy_size = max(SIZE_MULTIPLIER_CONFIG["min_size"], min(1.0, entropy_factor))
    
    quality_size = max(0.6, min(1.4, prediction_quality_mult))
    
    intent_factors = {
        IntentType.GRAB: 1.2, IntentType.CONTINUE: 1.15,
        IntentType.ACCEPT: 1.0, IntentType.TRAP: 0.5,
        MarketIntent.SEEK_LIQUIDITY: 1.3, MarketIntent.DISTRIBUTE: 0.7,
    }
    intent_factor = intent_factors.get(intent, 1.0)
    
    final_mult = entropy_size * quality_size * intent_factor
    return max(SIZE_MULTIPLIER_CONFIG["min_size"], min(SIZE_MULTIPLIER_CONFIG["max_size"], final_mult))

# ========== ENGINE LOOPS ==========
def state_engine_update_v6():
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
        alert = check_entry_alert_v6(coin, mark, master_candles)
        if alert and not PAPER_MODE:
            alerts.append(alert)
        elif alert and PAPER_MODE:
            logger.info(f"[PAPER] {alert['coin']} {alert['direction']} score={alert['score']} belief={alert.get('belief_state', 'SEEKING')}")
        time.sleep(0.05)
    
    for alert in alerts:
        send_alert_v6(alert)

def trigger_engine_update_v6():
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
    
    for coin in active:
        if coin in all_top:
            update_rolling_delta(coin)
            get_oi_roc(coin)
            get_volume_spike(coin)
        time.sleep(0.02)
    
    for coin in all_top:
        if coin in active:
            continue
        update_rolling_delta(coin)
        get_oi_roc(coin)
        get_volume_spike(coin)
        time.sleep(0.02)

def scheduled_state_engine_v6():
    while not _shutdown_event.is_set():
        with _alert_enabled_lock:
            if not _alert_enabled:
                time.sleep(60)
                continue
        state_engine_update_v6()
        vol_reg = get_volatility_regime()
        interval = STATE_ENGINE_INTERVAL
        if vol_reg == "HIGH_VOLATILITY":
            interval = max(15, interval // 2)
        elif vol_reg == "LOW_VOLATILITY":
            interval = min(60, interval * 2)
        logger.info(f"State engine cycle done, next in {interval}s")
        time.sleep(interval)

def scheduled_trigger_engine_v6():
    while not _shutdown_event.is_set():
        trigger_engine_update_v6()
        time.sleep(TRIGGER_ENGINE_INTERVAL_ACTIVE)

def scheduled_shadow_evaluation_v6():
    while not _shutdown_event.is_set():
        with _shadow_lock:
            now = time.time()
            for sid, shadow in list(_shadow_decisions.items()):
                if not shadow["evaluated"] and now - shadow["timestamp"] > BASE_EVALUATION_DELAY:
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

def scheduled_cleanup_v6():
    while not _shutdown_event.is_set():
        cleanup_active_candidates_v6()
        cleanup_old_shadow_decisions_v6()
        time.sleep(600)
        
        
# ========== V6.1: HELPER FUNCTIONS ==========

def api_call_with_retry(func, max_retries: int = 3, delay: float = 1.0, *args, **kwargs):
    """Generic retry wrapper untuk API calls"""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"API call failed after {max_retries} attempts: {e}")
                raise
            logger.warning(f"API attempt {attempt+1} failed: {e}, retrying in {delay*(attempt+1):.0f}s")
            time.sleep(delay * (attempt + 1))
    return None

def get_progressive_cooldown(coin: str) -> int:
    """Cooldown meningkat progresif setiap alert dalam 1 jam terakhir"""
    now = time.time()
    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        # Buang entri lama
        while _alert_history[coin] and now - _alert_history[coin][0] > ALERT_HISTORY_WINDOW:
            _alert_history[coin].popleft()
        count = len(_alert_history[coin])
    if count == 0:   return 300
    elif count == 1: return 600
    elif count == 2: return 900
    else:            return 1200

def record_alert_sent(coin: str):
    """Catat alert terkirim untuk progressive cooldown"""
    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        _alert_history[coin].append(time.time())

def check_market_sanity() -> bool:
    """Return False jika market terlalu chaotic untuk trading"""
    now = time.time()
    with _market_sanity_lock:
        if now - _market_sanity["last_check"] < MARKET_SANITY_TTL:
            return _market_sanity["is_sane"]
    try:
        entropy  = compute_market_entropy_v6("BTC", None)
        vol_spike = get_volume_spike("BTC", None)
        btc_delta = get_ob_delta("BTC")
        is_sane = True
        reason  = ""
        if entropy > 80 and vol_spike > 3:
            is_sane, reason = False, f"Market chaos: entropy={entropy:.0f}, vol={vol_spike:.1f}x"
        elif abs(btc_delta) > 15:
            is_sane, reason = False, f"Extreme BTC delta={btc_delta:+.1f}%"
        elif vol_spike > 5:
            is_sane, reason = False, f"Extreme vol spike {vol_spike:.1f}x"
        with _market_sanity_lock:
            _market_sanity.update({"is_sane": is_sane, "last_check": now, "reason": reason})
        if not is_sane:
            logger.warning(f"Market sanity FAIL: {reason}")
        return is_sane
    except Exception as e:
        logger.error(f"check_market_sanity error: {e}")
        return True

def scheduled_memory_cleanup():
    """Cleanup expired data dari semua memory stores — jalan tiap jam"""
    while not _shutdown_event.is_set():
        try:
            now = time.time()
            cutoff_7d  = now - 7  * 24 * 3600
            cutoff_30d = now - 30 * 24 * 3600

            with _hypothesis_lock:
                expired = [k for k, v in _hypothesis_store.items() if v.get("timestamp", 0) < cutoff_7d]
                for k in expired: del _hypothesis_store[k]
                if expired: logger.info(f"Cleaned {len(expired)} expired hypotheses")

            with _prediction_memory_lock:
                expired = [c for c, m in _prediction_memory.items() if m.get("last_update", 0) < cutoff_30d]
                for k in expired: del _prediction_memory[k]

            with _belief_state_lock:
                for coin, data in list(_belief_state.items()):
                    if now - data.get("since", 0) > 24 * 3600:
                        _belief_state[coin] = {"state": BeliefState.SEEKING, "since": now, "family": None}

            with _decision_energy_history_lock:
                for coin, hist in list(_decision_energy_history.items()):
                    if len(hist) > 100:
                        _decision_energy_history[coin] = deque(list(hist)[-100:], maxlen=100)

            with _alert_history_lock:
                cutoff_1h = now - ALERT_HISTORY_WINDOW
                for coin, dq in list(_alert_history.items()):
                    while dq and dq[0] < cutoff_1h:
                        dq.popleft()

        except Exception as e:
            logger.error(f"scheduled_memory_cleanup error: {e}")
        _shutdown_event.wait(3600)

def batch_get_oi_usd(coins: List[str]) -> Dict[str, float]:
    """Fetch OI untuk multiple coins dalam 1 API call"""
    try:
        meta = api_call_with_retry(info.meta_and_asset_ctxs, 2)
        results: Dict[str, float] = {}
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            name = asset["name"]
            if name in coins:
                oi   = float(ctx.get("openInterest", 0))
                mark = float(ctx.get("markPx", 0))
                results[name] = oi * mark / 1e6 if mark > 0 else 0
                with _oi_lock:
                    if name not in _oi_history:
                        _oi_history[name] = deque(maxlen=20)
                    _oi_history[name].append((time.time(), results[name]))
        return results
    except Exception as e:
        logger.error(f"batch_get_oi_usd error: {e}")
        return {}

def batch_get_funding(coins: List[str]) -> Dict[str, float]:
    """Fetch funding untuk multiple coins dalam 1 API call"""
    try:
        meta = api_call_with_retry(info.meta_and_asset_ctxs, 2)
        results: Dict[str, float] = {}
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            name = asset["name"]
            if name in coins:
                funding = float(ctx.get("funding", 0)) * 100
                results[name] = funding
                with _funding_lock:
                    _funding_cache[name] = (funding, time.time())
        return results
    except Exception as e:
        logger.error(f"batch_get_funding error: {e}")
        return {}

# ========== V6: TELEGRAM BOT (UI UPGRADE) ==========
bot = telebot.TeleBot(TOKEN)

def send_alert_v6(alert: dict):
    with _alert_enabled_lock:
        if not _alert_enabled:
            return
    
    # V6.1: Market sanity gate
    if not check_market_sanity():
        logger.warning(f"send_alert_v6 blocked — market sanity FAIL: {_market_sanity['reason']}")
        return

    coin = alert["coin"]
    now = time.time()
    
    # V6.1: Progressive cooldown (replace flat COOLDOWN_ENTRY)
    dynamic_cooldown = get_progressive_cooldown(coin)
    with _last_alert_lock:
        if coin in _last_alert and now - _last_alert[coin] < dynamic_cooldown:
            return
        _last_alert[coin] = now

    # V6.1: Record untuk progressive cooldown tracker
    record_alert_sent(coin)
    
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    
    # Belief state emoji
    belief_emoji = {
        "seeking": "🔍", "building": "🏗️", "convicted": "⚡", "executing": "🚀", "invalidated": "❌"
    }.get(alert.get("belief_state", "seeking"), "❓")
    
    # Time pressure emoji
    pressure_emoji = {"low": "🐢", "normal": "⚖️", "urgent": "⏰"}.get(alert.get("time_pressure", "normal"), "⚖️")
    
    mode_weights = {
        "aggressive": alert.get("mode_aggressive", 0),
        "balanced": alert.get("mode_balanced", 1),
        "precision": alert.get("mode_precision", 0)
    }
    
    if mode_weights["aggressive"] > 0.5:
        mode_bar = "█" * 10 + "░░░░"
        mode_emoji = "⚡"
    elif mode_weights["precision"] > 0.5:
        mode_bar = "░░░░" + "█" * 10
        mode_emoji = "🎯"
    else:
        mode_bar = "░░" + "█" * 6 + "░░"
        mode_emoji = "⚖️"
    
    intent_emoji = {
        "seek_liquidity": "🦈", "trap": "🪤", "continue": "➡️", 
        "accept": "🟰", "distribute": "📤"
    }.get(alert.get("intent_type", ""), "📍")
    
    size_mult = alert.get('position_size_mult', 1.0)
    size_bar = "█" * int(size_mult * 10) + "░" * int((1 - size_mult) * 10)
    
    filter_score = alert.get('filter_score', 0)
    if filter_score >= 80:
        filter_indicator = "🟢"
    elif filter_score >= 60:
        filter_indicator = "🟡"
    else:
        filter_indicator = "🔴"
    
    # Entropy bars
    entropy_data = alert.get('entropy_data', 0)
    entropy_market = alert.get('entropy_market', 0)
    entropy_decision = alert.get('entropy_decision', 0)
    entropy_bar_data = "█" * int(entropy_data / 10) + "░" * (10 - int(entropy_data / 10))
    entropy_bar_market = "█" * int(entropy_market / 10) + "░" * (10 - int(entropy_market / 10))
    entropy_bar_decision = "█" * int(entropy_decision / 10) + "░" * (10 - int(entropy_decision / 10))
    
    commitment = alert.get('commitment_score', 0)
    commitment_bar = "█" * int(commitment / 10) + "░" * (10 - int(commitment / 10))
    
    why_now = ""
    if alert.get("execution_mode") == "AGGRESSIVE":
        why_now = "⚡ *WHY NOW*: High Decision Energy + Low Entropy\n"
    elif alert.get("intent_type") == "seek_liquidity":
        why_now = "🦈 *WHY NOW*: Intent = SEEK_LIQUIDITY (stop hunt expected)\n"
    elif alert.get("time_pressure") == "urgent":
        why_now = "⏰ *WHY NOW*: Time Pressure = URGENT (opportunity fading)\n"
    
    text = f"""
{arrow} {mode_emoji} *ENTRY ALERT* • {coin} {intent_emoji}
━━━━━━━━━━━━━━━━━━━━━━

🧠 *Belief State*: {belief_emoji} {alert.get('belief_state', 'SEEKING').upper()} | ⏱️ Pressure: {pressure_emoji} {alert.get('time_pressure', 'normal').upper()}

📊 *Setup Quality*
├─ Score: {alert['score']} | {alert['label']}
├─ Decision Energy: {alert.get('decision_energy', 0):.1f}
├─ Commitment: {commitment:.0f}% [{commitment_bar}]
├─ Filter Score: {filter_score:.0f} {filter_indicator}
└─ Trigger: {alert.get('trigger_strength', 0):.0f}%

🎯 *Execution*
├─ Mode: {alert['execution_mode']} [{mode_bar}]
├─ A:{mode_weights['aggressive']:.0%} B:{mode_weights['balanced']:.0%} P:{mode_weights['precision']:.0%}
└─ Position Size: {size_mult:.1f}x [{size_bar}]

💰 *Levels*
├─ Entry: {fmt_price(alert['entry'])}
├─ SL: {fmt_price(alert['sl'])} ({abs(alert['entry'] - alert['sl']) / alert['entry'] * 100:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({abs(alert['tp'] - alert['entry']) / alert['entry'] * 100:.2f}%)
└─ RR: 1:{alert['rr']:.1f}

🌡️ *Entropy*
├─ Data: {entropy_data}% [{entropy_bar_data}]
├─ Market: {entropy_market}% [{entropy_bar_market}]
└─ Decision: {entropy_decision}% [{entropy_bar_decision}]

📈 *Evidence*
├─ Positive: {', '.join(alert.get('positive_evidence', []))}
├─ Negative: {alert.get('negative_evidence', 'none')}
└─ Why Not Wait: {alert.get('why_not', 'no deterrents')}

{why_now}
{alert.get('explanation', '')}
🗺️ Target: {alert.get('hypothesis', {}).get('destination', '')}

🎯 /entry {coin}
"""
    try:
        bot.send_message(USER_ID, text, parse_mode='Markdown')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Send alert error: {e}")
        
        
# ========== COMMAND HANDLERS (V6 UI Upgrade) ==========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    regimes = get_all_regimes()
    regime_str = f"{regimes[0]} | {regimes[1]} | {regimes[2]}"
    text = f"""
🧠 *Smart Entry Engine v6.0*
━━━━━━━━━━━━━━━━━━━━━━
🏗️ 3-Layer Architecture
⚡ Belief State + Commitment Score
⏰ Time Pressure State
🌡️ 3 Types of Entropy
🎯 Execution Mode Blend
📊 Prediction Quality (bukan winrate)
💪 Fatigue per Thesis Family

📡 Market: {regime_str}
⏰ {get_wib()}

✅ /status /entry BTC /warroom BTC /analytics /journal /belief /fatigue /prediction
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['journal'])
def cmd_journal(m):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''SELECT timestamp, coin, market_regime, belief_state, direction, final_score, 
                       decision_energy, commitment_score, time_pressure, execution_mode, intent_type, why_not
                 FROM journal ORDER BY timestamp DESC LIMIT 15''')
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.reply_to(m, "Belum ada data journal.")
        return
    teks = "📜 *DECISION JOURNAL* (15 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for ts, coin, mreg, belief, dirn, fs, de, commit, pressure, mode, intent, why_not in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
        belief_emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}.get(belief, "❓")
        teks += f"{dt} {coin} [{mreg}] {belief_emoji}{belief.upper()} | {mode}/{intent}\n"
        teks += f"   Score:{fs} | DE:{de:.0f} | Commit:{commit:.0f} | Pressure:{pressure}\n"
        teks += f"   ⚠️ {why_not[:40]}\n\n"
    bot.reply_to(m, teks, parse_mode='Markdown')

@bot.message_handler(commands=['belief'])
def cmd_belief(m):
    """Show belief state for all tracked coins"""
    with _belief_state_lock:
        if not _belief_state:
            bot.reply_to(m, "Belum ada data belief state.")
            return
        text = "🧠 *BELIEF STATE SUMMARY*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for coin, data in sorted(_belief_state.items(), key=lambda x: x[1]["since"]):
            state = data["state"].value
            duration = int(time.time() - data["since"])
            mins = duration // 60
            secs = duration % 60
            family = data.get("family", "unknown")
            state_emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀","invalidated":"❌"}.get(state, "❓")
            text += f"{state_emoji} {coin}: {state.upper()} ({mins}m {secs}s) | family:{family}\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['fatigue'])
def cmd_fatigue(m):
    """Show fatigue status per thesis family"""
    with _fatigue_memory_lock:
        if not _fatigue_memory:
            bot.reply_to(m, "Belum ada data fatigue.")
            return
        text = "💪 *FATIGUE STATUS (per Thesis Family)*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for family, deq in _fatigue_memory.items():
            count = len(deq)
            if count >= MAX_FATIGUE_PER_HOUR:
                penalty = 0.3
                bar = "🔴"
            elif count >= 3:
                penalty = 0.6
                bar = "🟡"
            else:
                penalty = 0.8
                bar = "🟢"
            text += f"{bar} {family}: {count}x rejections | penalty {penalty:.0%}\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['prediction'])
def cmd_prediction(m):
    """Show prediction quality ranking"""
    with _prediction_memory_lock:
        if not _prediction_memory:
            bot.reply_to(m, "Belum ada data prediction quality.")
            return
        text = "📊 *PREDICTION QUALITY RANKING*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        sorted_coins = sorted(_prediction_memory.items(), key=lambda x: x[1]["ema_quality"], reverse=True)[:10]
        for coin, data in sorted_coins:
            quality = data["ema_quality"]
            bar = "█" * int(quality / 10) + "░" * (10 - int(quality / 10))
            text += f"{coin}: {quality:.0f} [{bar}]\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def cmd_status(m):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM signals WHERE timestamp > ?", (int(time.time()) - 86400,))
    today = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE executed=1 AND timestamp > ?", (int(time.time()) - 86400,))
    alerts_sent = c.fetchone()[0]
    conn.close()
    
    pred_q = get_prediction_quality_multiplier("BTC")
    market = get_all_regimes()
    
    text = f"""
📊 *STATUS V6*
━━━━━━━━━━━━━━━━━━━━━━
⏰ {get_wib()}
📡 Market: {market[0]} | {market[1]} | {market[2]}

📈 *Today*
├─ Alerts: {alerts_sent}
├─ Signals: {today}
└─ Prediction Quality: {pred_q:.2f}x

⚙️ *System*
├─ Alert: {'🟢 ON' if _alert_enabled else '🔴 OFF'}
└─ Paper: {'📄 YES' if PAPER_MODE else '💎 NO'}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['analytics'])
def cmd_analytics(m):
    stats = get_analytics()
    if stats["total"] == 0:
        bot.reply_to(m, "Belum ada sinyal dievaluasi.")
    else:
        win_bar = "█" * int(stats['win_rate'] / 10) + "░" * (10 - int(stats['win_rate'] / 10))
        text = f"""
📈 *PERFORMANCE*
━━━━━━━━━━━━━━━━━━━━━━
├─ Total: {stats['total']}
├─ Win: {stats['wins']} | Loss: {stats['losses']}
├─ Win Rate: {stats['win_rate']}% [{win_bar}]
├─ Avg RR: {stats['avg_rr']}
└─ Total PnL: {stats['total_pnl']:+.2f}%
"""
        bot.reply_to(m, text, parse_mode='Markdown')

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
        alert = check_entry_alert_v6(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No setup for {coin}")
            return
        
        mode_weights = {
            "A": alert.get("mode_aggressive", 0),
            "B": alert.get("mode_balanced", 1),
            "P": alert.get("mode_precision", 0)
        }
        
        belief_emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}.get(alert.get("belief_state", "seeking"), "❓")
        pressure_emoji = {"low":"🐢","normal":"⚖️","urgent":"⏰"}.get(alert.get("time_pressure", "normal"), "⚖️")
        
        text = f"""
🎯 *Entry {coin}*
━━━━━━━━━━━━━━━━━━━━━━
🧠 Belief: {belief_emoji} {alert.get('belief_state', 'SEEKING').upper()} | ⏱️ Pressure: {pressure_emoji} {alert.get('time_pressure', 'normal').upper()}

📡 {alert['direction']} | {alert['label']} ({alert['score']})
├─ Mode: {alert['execution_mode']} (A:{mode_weights['A']:.0%} B:{mode_weights['B']:.0%} P:{mode_weights['P']:.0%})
├─ Intent: {alert.get('intent_type', 'unknown')}
├─ Decision Energy: {alert.get('decision_energy', 0):.1f}
└─ Commitment: {alert.get('commitment_score', 0):.0f}%

💰 *Levels*
├─ Entry: {fmt_price(alert['entry'])}
├─ SL: {fmt_price(alert['sl'])} ({abs(alert['entry'] - alert['sl']) / alert['entry'] * 100:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({abs(alert['tp'] - alert['entry']) / alert['entry'] * 100:.2f}%)
└─ RR: 1:{alert['rr']:.1f}

📊 *Quality*
├─ Filter Score: {alert.get('filter_score', 0):.0f}
├─ Position Size: {alert.get('position_size_mult', 1.0):.2f}x
├─ Trigger Strength: {alert.get('trigger_strength', 0):.0f}%
└─ Fatigue Penalty: {alert.get('fatigue_penalty', 1.0):.0%}

🌡️ *Entropy*
├─ Data: {alert.get('entropy_data', 0)}%
├─ Market: {alert.get('entropy_market', 0)}%
└─ Decision: {alert.get('entropy_decision', 0)}%

📈 *Evidence*
├─ Positive: {', '.join(alert.get('positive_evidence', []))}
├─ Negative: {alert.get('negative_evidence', 'none')}
└─ Why Not Wait: {alert.get('why_not', '')}

{alert.get('explanation', '')}
"""
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
        alert = check_entry_alert_v6(coin, mark, master)
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
        dq = get_data_confidence(coin, mark, time.time())[0]
        candles_1h = get_candles(coin, "1h", 60, master)
        state = get_market_state_from_structure(candles_1h, mark).name if candles_1h else "UNKNOWN"
        hyp = alert.get('hypothesis', {})
        market = get_all_regimes()
        
        text = f"""
🧠 *WARROOM {coin} V6*
━━━━━━━━━━━━━━━━━━━━━━
📡 Market: {market[0]} | {market[1]} | {market[2]}
├─ State: {state}
├─ Intent: {alert.get('intent_type', 'unknown')}
├─ Belief: {alert.get('belief_state', 'SEEKING')}
└─ Time Pressure: {alert.get('time_pressure', 'normal')}

📊 *Metrics*
├─ OB Delta: {delta:+.1f}%
├─ CVD: {cvd:+.2f}M
├─ OI: {oi:.1f}M
├─ Funding: {funding:+.3f}%
├─ Momentum: {momentum}
└─ Exhaustion: {exhaustion}%

🎯 *Setup*
├─ Event: {alert['area']}
├─ Direction: {alert['direction']}
├─ Score: {alert['score']} | {alert['label']}
├─ RR: 1:{alert['rr']:.1f}
├─ Size: {alert.get('position_size_mult', 1.0):.2f}x
├─ Filter: {alert.get('filter_score', 0):.0f}
├─ Commitment: {alert.get('commitment_score', 0):.0f}%
└─ Fatigue: {alert.get('fatigue_penalty', 1.0):.0%}

📌 *Hypothesis*
├─ Thesis: {hyp.get('thesis', 'N/A')[:60]}
├─ Invalidate: {hyp.get('invalidate', 'N/A')}
└─ Observe: {hyp.get('observe', 'N/A')}
"""
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
        bot.reply_to(m, f"Alert {'🟢 ON' if _alert_enabled else '🔴 OFF'}")

@bot.message_handler(commands=['health'])
def cmd_health(m):
    """V6.1: System health check"""
    if m.from_user.id != USER_ID:
        return
    mem_pct = f"{psutil.Process().memory_percent():.1f}%" if HAS_PSUTIL else "N/A"
    cpu_pct = f"{psutil.cpu_percent():.1f}%" if HAS_PSUTIL else "N/A"
    with _candle_lock:
        cache_sz = len(_candle_cache)
    with _active_candidates_lock:
        active_sz = len(_active_candidates)
    with _pending_setups_lock:
        pending_sz = len(_pending_setups)
    with _market_sanity_lock:
        is_sane = _market_sanity["is_sane"]
        sanity_reason = _market_sanity["reason"] or "N/A"
    with _hypothesis_lock:
        hyp_sz = len(_hypothesis_store)
    text = (
        "🩺 *SYSTEM HEALTH* — V6.1\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ CPU: {cpu_pct}  |  RAM: {mem_pct}\n"
        f"🧵 Threads: {threading.active_count()}\n"
        "\n"
        f"📦 Candle cache: {cache_sz}\n"
        f"📦 Hypotheses: {hyp_sz}\n"
        f"🎯 Active candidates: {active_sz}\n"
        f"⏳ Pending setups: {pending_sz}\n"
        "\n"
        f"🛡️ Market sanity: {'🟢 SANE' if is_sane else '🔴 CHAOS'}\n"
        f"└─ {sanity_reason}\n"
        "\n"
        f"✅ Status: {'RUNNING' if not _shutdown_event.is_set() else 'STOPPING'}"
    )
    bot.reply_to(m, text, parse_mode='Markdown')
        
        

# ========== V6: COST OF WAITING HELPER (untuk backward compatibility) ==========
def compute_value_of_waiting_v5(current_confidence: float, current_opportunity: float, 
                                 current_uncertainty: float, setup_age_minutes: float) -> Tuple[float, float, bool]:
    confidence_gain = min(20.0, setup_age_minutes * TIME_PRESSURE_CONFIG["confidence_gain_rate"])
    future_confidence = min(100.0, current_confidence + confidence_gain)
    
    opportunity_decay = 1.0 - (setup_age_minutes * TIME_PRESSURE_CONFIG["opportunity_decay"])
    future_opportunity = current_opportunity * max(0.1, opportunity_decay)
    
    uncertainty_decay = max(0.5, 1.0 - (setup_age_minutes * 0.01))
    future_uncertainty = current_uncertainty * uncertainty_decay
    
    relevance_prob = max(0.1, 1.0 - (setup_age_minutes * TIME_PRESSURE_CONFIG["decay_rate"]))
    
    expected_decay = 1.0 - (setup_age_minutes * TIME_PRESSURE_CONFIG["expected_decay_rate"])
    expected_decay = max(0.1, expected_decay)
    
    if future_uncertainty <= 0:
        future_uncertainty = 0.01
    
    raw_wait_value = (future_confidence * future_opportunity) / future_uncertainty
    wait_value = raw_wait_value * relevance_prob * expected_decay
    
    max_wait_reached = setup_age_minutes > TIME_PRESSURE_CONFIG["max_wait_minutes"]
    if max_wait_reached:
        wait_value = 0.0
    
    return wait_value, expected_decay, max_wait_reached

def should_wait_or_execute_v5(current_value: float, wait_value: float, decision_energy: float) -> Tuple[bool, str, float]:
    threshold = TIME_PRESSURE_CONFIG["wait_threshold"]
    
    if wait_value <= 0:
        return True, "execute (wait_value=0, too old)", 1.0
    
    ratio = current_value / (wait_value * threshold) if wait_value > 0 else 999
    confidence = min(1.0, ratio / 2)
    
    if current_value >= wait_value * threshold:
        return True, f"execute (current={current_value:.1f} > wait={wait_value:.1f}*{threshold})", confidence
    else:
        return False, f"wait (current={current_value:.1f} < wait={wait_value:.1f}*{threshold})", confidence

# ========== MAIN ==========
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--paper', action='store_true')
    return p.parse_args()

def signal_handler(sig, frame):
    logger.info(f"Shutdown signal {sig} received, exiting...")
    _shutdown_event.set()
    sys.exit(0)

if __name__ == "__main__":
    args = parse_args()
    PAPER_MODE = args.paper
    logger.info(f"Starting Smart Entry Engine V6.0 in {'PAPER' if PAPER_MODE else 'LIVE'} mode")
    init_db()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    t_state = threading.Thread(target=scheduled_state_engine_v6, daemon=True)
    t_state.start()
    t_trigger = threading.Thread(target=scheduled_trigger_engine_v6, daemon=True)
    t_trigger.start()
    t_shadow = threading.Thread(target=scheduled_shadow_evaluation_v6, daemon=True)
    t_shadow.start()
    t_clean = threading.Thread(target=scheduled_cleanup_v6, daemon=True)
    t_clean.start()
    t_monitor_setups = threading.Thread(target=monitor_pending_setups_v6, daemon=True)
    t_monitor_setups.start()
    t_mem_cleanup = threading.Thread(target=scheduled_memory_cleanup, daemon=True)
    t_mem_cleanup.start()
    
    while not _shutdown_event.is_set():
        try:
            logger.info("Starting bot polling...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            if _shutdown_event.is_set():
                break
            logger.error(f"Bot polling error: {e}, restarting in 5 seconds...")
            time.sleep(5)