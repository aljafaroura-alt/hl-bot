#!/usr/bin/env python3
# ============================================================
# SMART ENTRY ENGINE – HYPERLIQUID (v7.0)
# Atomic State + Circuit Breaker + ThreadPool + Snapshot Layer
# Decoupled Belief/Confidence + Decision Trace
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
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any, Callable
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

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

# ========== ENGINE CONSTANTS (JANGAN DIUBAH) ==========
ENGINE_CONSTANTS = {
    "BELIEF_TRANSITIONS": {
        "SEEKING": ["BUILDING"],
        "BUILDING": ["CONVICTED", "INVALIDATED"],
        "CONVICTED": ["EXECUTING"],
        "EXECUTING": [],
        "INVALIDATED": ["SEEKING"],
    },
    "MIN_EVIDENCE_FAMILIES": 2,
    "UNCLEAR_THRESHOLD": 55,
    "UNCLEAR_DIFF": 15,
    "MIN_DATA_CONFIDENCE": 50,
    "EVIDENCE_MULT_1": 0.4,
    "EVIDENCE_MULT_2": 0.7,
    "EVIDENCE_MULT_3": 1.0,
}

# ========== TUNABLE PARAMETERS (BOLEH DIUBAH) ==========
TUNABLE = {
    "STATE_ENGINE_INTERVAL": 30,
    "TRIGGER_ENGINE_INTERVAL_ACTIVE": 3,
    "COOLDOWN_ENTRY": 900,
    "BASE_EVALUATION_DELAY": 7200,
    "ACCEPTANCE_WINDOW_CANDLES": 2,
    "OI_PERSISTENCE_REQUIRED": 3,
    "ROLLING_DELTA_WINDOW": 6,
    "SHADOW_RETENTION_HOURS": 24,
    "MAX_CANDLE_AGE_MS": 60000,
    "MAX_OB_AGE_MS": 5000,
    "MAX_CVD_AGE_MS": 30000,
    "MAX_OI_AGE_MS": 60000,
    "MAX_PRICE_AGE_MS": 5000,
    "MAX_FUNDING_AGE_MS": 60000,
    "OUTLIER_SIGMA": 3.0,
    "MAX_JUMP_PCT": 10.0,
    "MIN_OB_FLOW_WALL_USD": 500_000,
    "MIN_OB_FLOW_DELTA_SHIFT": 6,
    "MIN_FVG_FLOW_CVD_ACCEL": 0.5,
    "MIN_FVG_FLOW_DELTA_DIVERGENCE": 4,
    "LIQUIDITY_VACUUM_AREA_THRESHOLD": 60,
    "ENTROPY_BASE": 60,
    "ENTROPY_VOLATILITY_FACTOR": 0.3,
    "ENTROPY_TREND_STRENGTH_FACTOR": 0.2,
    "ENTROPY_TTL_FACTOR": 0.5,
    "ENTROPY_RR_FACTOR": 1.2,
    "ENTROPY_THRESHOLD_FACTOR": 0.2,
    "MEMORY_EMA_ALPHA": 0.2,
    "MEMORY_DECAY_RATE": 0.95,
    "SETUP_EXPIRY_SECONDS": 3600,
    "SIZE_MIN": 0.25,
    "SIZE_MAX": 1.0,
    "SIZE_ENTROPY_FACTOR": 0.7,
    "FATIGUE_MAX_PER_HOUR": 5,
    "FATIGUE_COOLDOWN_WINDOW": 3600,
    "ALERT_HISTORY_WINDOW": 3600,
    "MARKET_SANITY_TTL": 60,
    "CIRCUIT_BREAKER_FAILURE_THRESHOLD": 5,
    "CIRCUIT_BREAKER_TIMEOUT": 60,
    "DECISION_ENERGY_AGGRESSIVE_THRESHOLD": 75,
    "DECISION_ENERGY_PRECISION_THRESHOLD": 40,
    "ENTROPY_AGGRESSIVE_MAX": 40,
    "ENTROPY_PRECISION_MIN": 70,
    "MAX_JOURNAL_ENTRIES": 5000,
    "MAX_CACHE_ITEMS": 1000,
    "MAX_TRACES": 1000,
}

# ========== LOGGING ==========
DB_PATH = "signals.db"
LOG_DIR = "logs"
PAPER_MODE = False

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
    
    
# ========== ENUMS ==========
class MarketState(Enum):
    UNKNOWN = 0
    ACCUMULATION = 1
    EXPANSION = 2
    DISTRIBUTION = 3
    REVERSAL = 4

class MarketIntent(Enum):
    SEEK_LIQUIDITY = "seek_liquidity"
    ACCEPT = "accept"
    TRAP = "trap"
    DISTRIBUTE = "distribute"
    CONTINUE = "continue"

class IntentType(Enum):
    GRAB = "grab"
    TRAP = "trap"
    ACCEPT = "accept"
    CONTINUE = "continue"

class SetupState(Enum):
    PENDING = 0
    TRIGGERED = 1
    EXPIRED = 2
    INVALIDATED = 3

class BeliefState(Enum):
    SEEKING = "seeking"
    BUILDING = "building"
    CONVICTED = "convicted"
    EXECUTING = "executing"
    INVALIDATED = "invalidated"

class TimePressure(Enum):
    LOW = "low"
    NORMAL = "normal"
    URGENT = "urgent"

# ========== V7: ATOMIC RUNTIME STATE ==========
@dataclass
class RuntimeState:
    alert_enabled: bool = True
    shutdown: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    
    def enable_alerts(self):
        with self._lock:
            self.alert_enabled = True
    
    def disable_alerts(self):
        with self._lock:
            self.alert_enabled = False
    
    def is_alert_enabled(self) -> bool:
        with self._lock:
            return self.alert_enabled
    
    def is_running(self) -> bool:
        with self._lock:
            return not self.shutdown
    
    def signal_shutdown(self):
        with self._lock:
            self.shutdown = True

RUNTIME = RuntimeState()

# ========== V7: DATACLASSES ==========
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

@dataclass
class MarketSnapshot:
    timestamp: float
    mids: Dict[str, float]
    oi: Dict[str, float]
    funding: Dict[str, float]

@dataclass
class DecisionTrace:
    timestamp: float
    coin: str
    event_type: str
    belief_state: str
    confidence: float
    decision_energy: float
    final_decision: str
    reasons: List[str]
    why_not: List[str]
    what_changed: str
    
    def to_dict(self):
        return asdict(self)

@dataclass
class RuntimeFlags:
    pass
    
  
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

_prediction_memory: Dict[str, Dict] = {}
_prediction_memory_lock = threading.RLock()

_oi_values: Dict[str, deque] = {}
_funding_values: Dict[str, deque] = {}
_price_values: Dict[str, deque] = {}
_data_integrity_lock = threading.RLock()

_decision_energy_history: Dict[str, deque] = {}
_decision_energy_history_lock = threading.RLock()

_belief_state: Dict[str, Dict] = {}
_belief_state_lock = threading.RLock()

_fatigue_memory: Dict[str, deque] = {}
_fatigue_memory_lock = threading.RLock()

_pending_setups: Dict[str, PendingSetup] = {}
_pending_setups_lock = threading.RLock()

_alert_history: Dict[str, deque] = {}
_alert_history_lock = threading.RLock()

_market_sanity: Dict[str, Any] = {"is_sane": True, "last_check": 0.0, "reason": ""}
_market_sanity_lock = threading.RLock()

_decision_traces: deque = deque(maxlen=TUNABLE["MAX_TRACES"])
_trace_lock = threading.RLock()

# V7: Snapshot
_last_snapshot: Optional[MarketSnapshot] = None
_snapshot_lock = threading.RLock()
_SNAPSHOT_TTL = 5

# V7: Circuit Breaker state
_circuit_breaker_state = {"failures": 0, "last_failure": 0, "state": "CLOSED"}
_circuit_breaker_lock = threading.RLock()

# V7: ThreadPoolExecutor
_EVAL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eval_")
_SHADOW_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shadow_")

# Hyperliquid API
info = Info(constants.MAINNET_API_URL)

# Optional psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    
    
# ========== DATABASE ==========
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db_connect()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT UNIQUE, coin TEXT, direction TEXT, score INTEGER,
        entry_price REAL, sl_price REAL, tp_price REAL, rr REAL, reason TEXT,
        timestamp INTEGER, evaluated INTEGER DEFAULT 0, outcome TEXT, pnl REAL,
        exit_price REAL, exit_time INTEGER, mfe REAL, mae REAL, data_confidence INTEGER,
        hypothesis_thesis TEXT, hypothesis_invalidate TEXT, hypothesis_observe TEXT,
        hypothesis_validated INTEGER DEFAULT 0, execution_mode TEXT, intent_type TEXT,
        decision_energy REAL, position_size_mult REAL, filter_score REAL,
        intent_confidence REAL, belief_state TEXT, commitment_score REAL,
        time_pressure TEXT, prediction_quality REAL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER, coin TEXT, market_regime TEXT, volatility_regime TEXT,
        flow_regime TEXT, belief_state TEXT, long_score INTEGER, short_score INTEGER,
        direction TEXT, final_score INTEGER, reason TEXT, negative_evidence TEXT,
        entropy_data INTEGER, entropy_market INTEGER, entropy_decision INTEGER,
        decision_time_ms INTEGER, api_latency_ms INTEGER, data_confidence INTEGER,
        executed INTEGER DEFAULT 0, outcome TEXT, missed_opportunity_pnl REAL,
        contribution TEXT, execution_mode TEXT, intent_type TEXT, decision_energy REAL,
        position_size_mult REAL, filter_score REAL, rejection_strength REAL,
        acceptance_strength REAL, persistence_strength REAL, why_not TEXT,
        wait_value REAL, trigger_strength REAL, time_pressure TEXT, commitment_score REAL,
        decision_acceleration REAL, mode_aggressive REAL, mode_balanced REAL,
        mode_precision REAL, confidence_breakdown TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS counterfactual (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, coin TEXT,
        original_score INTEGER, modified_module TEXT, modified_score INTEGER, reason TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS shadow_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT UNIQUE, coin TEXT,
        direction TEXT, entry_price REAL, sl_price REAL, tp_price REAL, timestamp INTEGER,
        evaluated INTEGER DEFAULT 0, outcome TEXT, pnl REAL, mfe REAL, mae REAL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS hypothesis_validation (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, thesis TEXT,
        outcome TEXT, pnl REAL, validated INTEGER
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS prediction_quality (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, coin TEXT,
        signal_id TEXT, predicted_direction TEXT, actual_direction TEXT,
        entry_zone_accuracy REAL, timing_quality REAL, thesis_validated INTEGER,
        quality_score REAL
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS belief_state_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, coin TEXT,
        state TEXT, duration_seconds REAL, trigger TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS decision_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, coin TEXT,
        event_type TEXT, belief_state TEXT, confidence REAL, decision_energy REAL,
        final_decision TEXT, reasons TEXT, why_not TEXT, what_changed TEXT
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Database ready (V7)")

def save_trace_to_db(trace: DecisionTrace):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO decision_traces 
                 (timestamp, coin, event_type, belief_state, confidence, decision_energy,
                  final_decision, reasons, why_not, what_changed)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (int(trace.timestamp), trace.coin, trace.event_type, trace.belief_state,
               trace.confidence, trace.decision_energy, trace.final_decision,
               ", ".join(trace.reasons), ", ".join(trace.why_not), trace.what_changed))
    conn.commit()
    conn.close()

def log_decision_trace(trace: DecisionTrace):
    with _trace_lock:
        _decision_traces.append(trace)
    if not PAPER_MODE:
        threading.Thread(target=save_trace_to_db, args=(trace,), daemon=True).start()

# ========== DB WRAPPER FUNCTIONS (MINIMAL) ==========
def save_signal_v7(signal_id, coin, direction, score, entry, sl, tp, rr, reason, data_confidence,
                   hypothesis_thesis="", hypothesis_invalidate="", hypothesis_observe="",
                   execution_mode="BALANCED", intent_type="", decision_energy=0.0,
                   position_size_mult=1.0, filter_score=100.0, intent_confidence=0.0,
                   belief_state="SEEKING", commitment_score=0.0, time_pressure="normal",
                   prediction_quality=50.0):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO signals 
                 (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason, 
                  timestamp, data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                  execution_mode, intent_type, decision_energy, position_size_mult, filter_score, 
                  intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()), 
               data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
               execution_mode, intent_type, decision_energy, position_size_mult, filter_score, 
               intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality))
    conn.commit()
    conn.close()

def add_journal_entry_v7(coin, market_regime, volatility_regime, flow_regime,
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

def add_belief_state_log(coin, state, duration_seconds, trigger):
    conn = db_connect()
    c = conn.cursor()
    c.execute('''INSERT INTO belief_state_log (timestamp, coin, state, duration_seconds, trigger)
                 VALUES (?,?,?,?,?)''',
              (int(time.time()), coin, state, duration_seconds, trigger))
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

def update_signal_outcome_v7(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None):
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

# ========== HELPERS ==========
def fmt_price(p): 
    return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"

def get_wib(): 
    return datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")

def get_wib_hour(): 
    return datetime.now(timezone(timedelta(hours=7))).hour

def generate_signal_id(coin, direction): 
    return f"{coin}_{direction}_{int(time.time())}"
    
    
# ========== V7: CIRCUIT BREAKER ==========
class CircuitBreaker:
    def __init__(self, failure_threshold: int = None, timeout: int = None):
        self.failure_threshold = failure_threshold or TUNABLE["CIRCUIT_BREAKER_FAILURE_THRESHOLD"]
        self.timeout = timeout or TUNABLE["CIRCUIT_BREAKER_TIMEOUT"]
        self._state = "CLOSED"
        self._failures = 0
        self._last_failure = 0
        self._lock = threading.RLock()
    
    def call(self, func: Callable, *args, **kwargs):
        with self._lock:
            if self._state == "OPEN":
                if time.time() - self._last_failure > self.timeout:
                    self._state = "HALF_OPEN"
                    logger.info("Circuit breaker HALF_OPEN - testing")
                else:
                    logger.warning("Circuit breaker OPEN - skipping call")
                    return None
        
        try:
            result = func(*args, **kwargs)
            with self._lock:
                if self._state == "HALF_OPEN":
                    self._state = "CLOSED"
                    self._failures = 0
                    logger.info("Circuit breaker CLOSED - recovered")
            return result
        except Exception as e:
            with self._lock:
                self._failures += 1
                self._last_failure = time.time()
                if self._failures >= self.failure_threshold:
                    self._state = "OPEN"
                    logger.error(f"Circuit breaker OPEN after {self._failures} failures")
            raise

_CIRCUIT_BREAKER = CircuitBreaker()

# ========== V7: SNAPSHOT LAYER ==========
def refresh_snapshot():
    global _last_snapshot
    now = time.time()
    with _snapshot_lock:
        if _last_snapshot and now - _last_snapshot.timestamp < _SNAPSHOT_TTL:
            return
        
        try:
            meta = info.meta_and_asset_ctxs()
            mids = {}
            oi = {}
            funding = {}
            
            for asset, ctx in zip(meta[0]["universe"], meta[1]):
                name = asset["name"]
                mids[name] = float(ctx.get("markPx", 0))
                oi_val = float(ctx.get("openInterest", 0))
                oi[name] = oi_val * mids[name] / 1e6 if mids[name] > 0 else 0
                funding[name] = float(ctx.get("funding", 0)) * 100
            
            _last_snapshot = MarketSnapshot(
                timestamp=now,
                mids=mids,
                oi=oi,
                funding=funding
            )
            
            with _last_mids_lock:
                for coin, price in mids.items():
                    _last_mids[coin] = (price, now)
            
            for coin, val in oi.items():
                update_data_integrity_history(coin, val, 0, 0)
                with _oi_lock:
                    if coin not in _oi_history:
                        _oi_history[coin] = deque(maxlen=20)
                    _oi_history[coin].append((now, val))
            
            for coin, val in funding.items():
                with _funding_lock:
                    _funding_cache[coin] = (val, now)
                update_data_integrity_history(coin, 0, val, 0)
                
        except Exception as e:
            logger.error(f"Snapshot refresh error: {e}")

def get_snapshot() -> MarketSnapshot:
    refresh_snapshot()
    with _snapshot_lock:
        return _last_snapshot

# ========== DATA FUNCTIONS (MINIMAL) ==========
def detect_outlier(values: List[float], new_value: float) -> bool:
    if len(values) < 3:
        return False
    mean = np.mean(values)
    std = np.std(values)
    if std == 0:
        return False
    return abs(new_value - mean) > TUNABLE["OUTLIER_SIGMA"] * std

def detect_jump(prev_value: float, new_value: float) -> bool:
    if prev_value == 0:
        return False
    return abs((new_value - prev_value) / prev_value) * 100 > TUNABLE["MAX_JUMP_PCT"]

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
    with _data_integrity_lock:
        if coin in _oi_values and len(_oi_values[coin]) >= 2:
            oi_vals = [v for _, v in _oi_values[coin]]
            latest = oi_vals[-1]
            if detect_outlier(oi_vals[:-1], latest):
                score -= 25
            if len(oi_vals) >= 2 and detect_jump(oi_vals[-2], latest):
                score -= 20
        else:
            score -= 15
        
        if coin in _funding_values and len(_funding_values[coin]) >= 2:
            fund_vals = [v for _, v in _funding_values[coin]]
            latest = fund_vals[-1]
            if detect_outlier(fund_vals[:-1], latest):
                score -= 20
            if len(fund_vals) >= 2 and detect_jump(fund_vals[-2], latest):
                score -= 15
        else:
            score -= 10
        
        if coin in _price_values and len(_price_values[coin]) >= 2:
            price_vals = [v for _, v in _price_values[coin]]
            latest = price_vals[-1]
            if detect_outlier(price_vals[:-1], latest):
                score -= 20
            if len(price_vals) >= 2 and detect_jump(price_vals[-2], latest):
                score -= 15
        else:
            score -= 10
    
    return max(0, min(100, score))

def get_data_confidence(coin: str, current_time: float) -> Tuple[int, Dict[str, int]]:
    ages = {}
    total_score = 100
    
    with _last_mids_lock:
        if coin in _last_mids:
            price_ts = _last_mids[coin][1]
            age_ms = (current_time - price_ts) * 1000
        else:
            age_ms = TUNABLE["MAX_PRICE_AGE_MS"] + 1000
    ages["price_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_PRICE_AGE_MS"]:
        total_score -= 25
    elif age_ms > TUNABLE["MAX_PRICE_AGE_MS"] // 2:
        total_score -= 10
    
    candle_key = f"{coin}_1h_80"
    with _candle_lock:
        if candle_key in _candle_cache:
            _, ts = _candle_cache[candle_key]
            age_ms = (current_time - ts) * 1000
        else:
            age_ms = TUNABLE["MAX_CANDLE_AGE_MS"] + 1000
    ages["candle_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_CANDLE_AGE_MS"]:
        total_score -= 20
    elif age_ms > TUNABLE["MAX_CANDLE_AGE_MS"] // 2:
        total_score -= 8
    
    with _ob_lock:
        if coin in _ob_cache:
            _, ts = _ob_cache[coin]
            age_ms = (current_time - ts) * 1000
        else:
            age_ms = TUNABLE["MAX_OB_AGE_MS"] + 1000
    ages["ob_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_OB_AGE_MS"]:
        total_score -= 15
    elif age_ms > TUNABLE["MAX_OB_AGE_MS"] // 2:
        total_score -= 5
    
    with _cvd_lock:
        if coin in _cvd_cache:
            _, ts = _cvd_cache[coin]
            age_ms = (current_time - ts) * 1000
        else:
            age_ms = TUNABLE["MAX_CVD_AGE_MS"] + 1000
    ages["cvd_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_CVD_AGE_MS"]:
        total_score -= 10
    
    with _oi_lock:
        if coin in _oi_history and len(_oi_history[coin]) > 0:
            oi_ts = _oi_history[coin][-1][0]
            age_ms = (current_time - oi_ts) * 1000
        else:
            age_ms = TUNABLE["MAX_OI_AGE_MS"] + 1000
    ages["oi_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_OI_AGE_MS"]:
        total_score -= 15
    elif age_ms > TUNABLE["MAX_OI_AGE_MS"] // 2:
        total_score -= 5
    
    with _funding_lock:
        if coin in _funding_cache:
            _, ts = _funding_cache[coin]
            age_ms = (current_time - ts) * 1000
        else:
            age_ms = TUNABLE["MAX_FUNDING_AGE_MS"] + 1000
    ages["funding_age_ms"] = int(age_ms)
    if age_ms > TUNABLE["MAX_FUNDING_AGE_MS"]:
        total_score -= 10
    
    total_score = max(0, min(100, total_score))
    integrity_score = get_data_integrity_score(coin)
    final_confidence = int(total_score * 0.7 + integrity_score * 0.3)
    
    return final_confidence, ages

def get_candles(coin: str, timeframe: str, limit: int = 80, master: Dict = None) -> List[dict]:
    if master and coin in master:
        return master[coin]
    
    key = f"{coin}_{timeframe}_{limit}"
    now = time.time()
    ttl = {"5m": 60, "15m": 120, "1h": 300, "4h": 600}.get(timeframe, 300)
    
    with _candle_lock:
        if key in _candle_cache and now - _candle_cache[key][1] < ttl:
            return _candle_cache[key][0]
    
    try:
        end_ms = int(now * 1000)
        tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
        interval = tf_ms.get(timeframe, 3600000)
        start_ms = end_ms - limit * interval
        candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms) or []
    except Exception as e:
        logger.error(f"get_candles failed for {coin}: {e}")
        candles = []
    
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
        alpha = min(0.9, 0.3 + abs(raw - prev) / 60)
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
            _rolling_delta[coin] = deque(maxlen=TUNABLE["ROLLING_DELTA_WINDOW"])
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

def get_volume_spike(coin: str, master: Dict = None) -> float:
    candles = get_candles(coin, "5m", 30, master)
    if not candles or len(candles) < 6:
        return 1.0
    price = float(candles[-1]['c'])
    cur = float(candles[-1]['v']) * price
    prev = [float(c['v']) * float(c['c']) for c in candles[-6:-1]]
    avg = sum(prev)/len(prev) if prev else 1.0
    return cur / avg if avg > 0 else 1.0

def get_atr_pct(coin: str, period: int = 14, timeframe: str = "1h", master: Dict = None) -> float:
    candles = get_candles(coin, timeframe, period+5, master)
    if not candles or len(candles) < period+1:
        return 1.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i]['h']), float(candles[i]['l']), float(candles[i-1]['c'])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[-period:]) / period
    price = float(candles[-1]['c'])
    return (atr / price) * 100 if price > 0 else 1.0

def get_session() -> str:
    h = get_wib_hour()
    if 8 <= h < 15: return "ASIA"
    if 15 <= h < 20: return "LONDON"
    if 20 <= h or h < 2: return "NY"
    return "ASIA"

# ========== REGIMES (CACHED) ==========
_regimes_cache: Dict[str, Any] = {}
_regimes_cache_lock = threading.RLock()
_REGIMES_TTL = 120

def get_market_regime() -> str:
    candles = get_candles("BTC", "4h", 50)
    if not candles:
        return "RANGING"
    closes = [float(c['c']) for c in candles[-30:]]
    if len(closes) < 21:
        return "RANGING"
    ema9, ema21 = sum(closes[-9:])/9, sum(closes[-21:])/21
    if ema9 > ema21 * 1.02: return "TRENDING_UP"
    if ema9 < ema21 * 0.98: return "TRENDING_DOWN"
    return "RANGING"

def get_volatility_regime() -> str:
    atr = get_atr_pct("BTC", period=14, timeframe="4h")
    if atr > 4: return "HIGH_VOLATILITY"
    if atr < 1.5: return "LOW_VOLATILITY"
    return "NORMAL_VOLATILITY"

def get_flow_regime() -> str:
    delta_shift = get_delta_shift("BTC")
    if delta_shift > 4: return "FLOW_ACCELERATING"
    if delta_shift < -4: return "FLOW_DECELERATING"
    return "FLOW_NEUTRAL"

def get_all_regimes() -> Tuple[str, str, str]:
    with _regimes_cache_lock:
        now = time.time()
        if _regimes_cache and now - _regimes_cache.get("ts", 0) < _REGIMES_TTL:
            return _regimes_cache["market"], _regimes_cache["volatility"], _regimes_cache["flow"]
    market, vol, flow = get_market_regime(), get_volatility_regime(), get_flow_regime()
    with _regimes_cache_lock:
        _regimes_cache.update({"market": market, "volatility": vol, "flow": flow, "ts": time.time()})
    return market, vol, flow
    
    
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
    
    recent_highs = highs[-3:]
    recent_lows = lows[-3:]
    
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
    return bos_up or choch, bos_down or choch

# ========== ZONE MEMORY ==========
def update_zone_memory_v7(coin: str, zone_type: str, low: float, high: float, acceptance_strength: float):
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

def get_zone_penalty_v7(coin: str, zone_type: str, low: float, high: float) -> float:
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    with _zone_memory_lock:
        if key not in _zone_memory:
            return 0.0
        data = _zone_memory[key]
        if not data["strengths"]:
            return 0.0
        avg_strength = sum(data["strengths"]) / len(data["strengths"])
        return min(30, max(0, 40 - avg_strength) * 0.5)

# ========== OI PERSISTENCE ==========
def update_oi_persistence(coin: str, oi_roc: float):
    with _oi_persistence_lock:
        if coin not in _oi_persistence:
            _oi_persistence[coin] = {"count": 0, "last_trend": 0, "values": deque(maxlen=TUNABLE["OI_PERSISTENCE_REQUIRED"])}
        pers = _oi_persistence[coin]
        trend = 1 if oi_roc > 1.0 else (-1 if oi_roc < -1.0 else 0)
        pers["values"].append(trend)
        if len(pers["values"]) == TUNABLE["OI_PERSISTENCE_REQUIRED"]:
            if all(v == 1 for v in pers["values"]):
                pers["count"], pers["last_trend"] = TUNABLE["OI_PERSISTENCE_REQUIRED"], 1
            elif all(v == -1 for v in pers["values"]):
                pers["count"], pers["last_trend"] = TUNABLE["OI_PERSISTENCE_REQUIRED"], -1
            else:
                pers["count"] = max(0, pers["count"] - 1)

def get_oi_persistence(coin: str) -> Tuple[bool, int]:
    with _oi_persistence_lock:
        if coin not in _oi_persistence:
            return False, 0
        pers = _oi_persistence[coin]
        if pers["count"] >= TUNABLE["OI_PERSISTENCE_REQUIRED"]:
            return True, pers["last_trend"]
        return False, 0
        
        
# ========== V7: BELIEF STATE (DECOUPLED, INDEPENDENT) ==========
THESIS_FAMILIES = {
    "LIQUIDITY_SWEEP": ["LIQUIDITY"],
    "ORDER_BLOCK": ["OB", "OB_FLOW", "SD"],
    "IMBALANCE": ["FVG", "FVG_FLOW"],
    "VACUUM": ["VACUUM"],
}

def get_thesis_family(event_type: str) -> str:
    for family, types in THESIS_FAMILIES.items():
        if event_type in types:
            return family
    return "OTHER"

def compute_belief(event: TradeEvent, filter_score: float, structure_valid_long: bool, 
                   structure_valid_short: bool, trigger_strength: float) -> Tuple[BeliefState, float, str]:
    """
    V7: BELIEF = kualitas thesis (INDEPENDENT dari confidence)
    HANYA berdasarkan: event type, filter strength, structure validity
    """
    # Valid structure
    if event.direction == "LONG" and not structure_valid_long:
        return BeliefState.INVALIDATED, 0.0, "structure invalid for long"
    if event.direction == "SHORT" and not structure_valid_short:
        return BeliefState.INVALIDATED, 0.0, "structure invalid for short"
    
    # Event type weight
    event_weights = {
        "LIQUIDITY": 25,
        "OB": 20,
        "OB_FLOW": 25,
        "FVG": 15,
        "FVG_FLOW": 20,
        "VACUUM": 15,
        "CLUSTER": 30,
    }
    event_score = event_weights.get(event.type, 10)
    
    # Filter contribution
    filter_score_weighted = filter_score * 0.4
    
    # Trigger contribution (if any)
    trigger_score = min(30, trigger_strength * 0.6)
    
    total_belief = event_score + filter_score_weighted + trigger_score
    
    if total_belief > 70:
        return BeliefState.CONVICTED, total_belief, f"strong belief ({total_belief:.0f})"
    elif total_belief > 45:
        return BeliefState.BUILDING, total_belief, f"building belief ({total_belief:.0f})"
    else:
        return BeliefState.SEEKING, total_belief, f"seeking ({total_belief:.0f})"

def update_belief_state(coin: str, new_belief: BeliefState, belief_score: float, trigger: str):
    with _belief_state_lock:
        now = time.time()
        old = _belief_state.get(coin, {})
        old_state = old.get("state", BeliefState.SEEKING)
        
        # Log state change
        if old_state != new_belief:
            duration = now - old.get("since", now)
            add_belief_state_log(coin, new_belief.value, duration, trigger)
        
        _belief_state[coin] = {
            "state": new_belief,
            "score": belief_score,
            "since": now,
            "family": old.get("family", "unknown")
        }

def get_belief_state(coin: str) -> Tuple[BeliefState, float, float]:
    with _belief_state_lock:
        if coin not in _belief_state:
            return BeliefState.SEEKING, 0.0, 0.0
        data = _belief_state[coin]
        return data["state"], data["score"], time.time() - data["since"]

def reset_belief_state(coin: str, reason: str):
    with _belief_state_lock:
        if coin in _belief_state:
            duration = time.time() - _belief_state[coin]["since"]
            add_belief_state_log(coin, "RESET", duration, reason)
        _belief_state[coin] = {"state": BeliefState.SEEKING, "score": 0.0, "since": time.time(), "family": "unknown"}

# ========== V7: PREDICTION QUALITY (BUKAN WINRATE) ==========
def evaluate_prediction_quality(signal_id: str, coin: str, predicted_direction: str,
                                 actual_direction: str, entry_price: float,
                                 predicted_zone_low: float, predicted_zone_high: float,
                                 mfe: float, mae: float, thesis_validated: bool) -> float:
    quality = 50.0
    
    # Direction accuracy (30%)
    if predicted_direction == actual_direction:
        quality += 30
    else:
        quality -= 20
    
    # Entry zone accuracy (25%)
    if predicted_zone_low <= entry_price <= predicted_zone_high:
        quality += 25
        zone_accuracy = 1.0
    else:
        zone_accuracy = max(0, 1 - abs(entry_price - predicted_zone_high) / max(predicted_zone_high, 1))
        quality += zone_accuracy * 15
    
    # Timing quality (25%) - MFE/MAE ratio
    if mae != 0 and mfe > abs(mae):
        ratio = min(3.0, mfe / abs(mae))
        quality += (ratio / 3.0) * 25
        timing_quality = ratio
    elif mfe > 0:
        quality += 12
        timing_quality = 1.0
    else:
        timing_quality = 0.0
    
    # Thesis validation (20%)
    quality += 20 if thesis_validated else -10
    
    quality = max(0, min(100, quality))
    
    add_prediction_quality_log(coin, signal_id, predicted_direction, actual_direction,
                                zone_accuracy, timing_quality, thesis_validated, quality)
    return quality

def update_prediction_memory(coin: str, prediction_quality: float):
    with _prediction_memory_lock:
        if coin not in _prediction_memory:
            _prediction_memory[coin] = {"ema_quality": 50.0, "last_update": time.time(), "history": deque(maxlen=20)}
        mem = _prediction_memory[coin]
        mem["ema_quality"] = TUNABLE["MEMORY_EMA_ALPHA"] * prediction_quality + (1 - TUNABLE["MEMORY_EMA_ALPHA"]) * mem["ema_quality"]
        mem["last_update"] = time.time()
        mem["history"].append(prediction_quality)

def get_prediction_quality_multiplier(coin: str) -> float:
    with _prediction_memory_lock:
        if coin not in _prediction_memory:
            return 1.0
        ema = _prediction_memory[coin]["ema_quality"] / 100.0
        return 0.6 + (ema * 0.8)
        
        
# ========== V7: 3 JENIS ENTROPY ==========
def compute_data_entropy(ages: Dict[str, int]) -> int:
    score = 0
    if ages.get("price_age_ms", 0) > TUNABLE["MAX_PRICE_AGE_MS"]:
        score += 25
    if ages.get("candle_age_ms", 0) > TUNABLE["MAX_CANDLE_AGE_MS"]:
        score += 25
    if ages.get("ob_age_ms", 0) > TUNABLE["MAX_OB_AGE_MS"]:
        score += 25
    if ages.get("oi_age_ms", 0) > TUNABLE["MAX_OI_AGE_MS"]:
        score += 25
    return min(100, score)

def compute_market_entropy_v7(coin: str, master: Dict) -> int:
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 4:
        return 30
    
    closes = [float(c['c']) for c in candles[-5:]]
    price_changes = [abs(closes[i] - closes[i-1])/max(closes[i-1], 0.01)*100 for i in range(1, len(closes))]
    price_flips = sum(1 for i in range(2, len(closes)) if (closes[i] > closes[i-1]) != (closes[i-1] > closes[i-2]))
    price_magnitude = sum(price_changes) / len(price_changes) if price_changes else 0
    
    delta_vals = [get_ob_delta(coin) for _ in range(3)]
    delta_flips = sum(1 for i in range(1, len(delta_vals)) if (delta_vals[i] > 0) != (delta_vals[i-1] > 0))
    delta_magnitude = sum([abs(delta_vals[i] - delta_vals[i-1]) for i in range(1, len(delta_vals))]) / max(len(delta_vals)-1, 1)
    
    oi_vals = [get_oi_roc(coin) for _ in range(3)]
    oi_flips = sum(1 for i in range(1, len(oi_vals)) if (oi_vals[i] > 0) != (oi_vals[i-1] > 0))
    oi_magnitude = sum([abs(oi_vals[i] - oi_vals[i-1]) for i in range(1, len(oi_vals))]) / max(len(oi_vals)-1, 1)
    
    flip_score = min(100, (price_flips + delta_flips + oi_flips) * 25)
    magnitude_score = min(100, price_magnitude * 20 + delta_magnitude * 10 + oi_magnitude * 10)
    return min(100, max(0, (flip_score + magnitude_score) // 2))

def compute_decision_entropy(score_variance: float, contradictory_signals: bool,
                              multiple_events: bool, event_types: List[str]) -> int:
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

def get_dynamic_entropy_threshold_v7(volatility_regime: str, trend_strength: float) -> int:
    base = TUNABLE["ENTROPY_BASE"]
    if volatility_regime == "HIGH_VOLATILITY":
        base += int(TUNABLE["ENTROPY_VOLATILITY_FACTOR"] * 20)
    elif volatility_regime == "LOW_VOLATILITY":
        base -= int(TUNABLE["ENTROPY_VOLATILITY_FACTOR"] * 15)
    base += int((trend_strength / 100) * TUNABLE["ENTROPY_TREND_STRENGTH_FACTOR"] * 50)
    return max(40, min(85, base))

def compute_trend_strength_v7(coin: str, master: Dict) -> float:
    candles = get_candles(coin, "1h", 50, master)
    if not candles or len(candles) < 21:
        return 50.0
    closes = [float(c['c']) for c in candles]
    ema8, ema21 = np.mean(closes[-8:]), np.mean(closes[-21:])
    slope = (ema8 - ema21) / max(ema21, 0.01) * 100
    return min(100, max(0, (abs(slope) / 2) * 100))

# ========== V7: DECISION ENERGY ==========
def compute_decision_energy_v7(confidence: float, opportunity: float, uncertainty: float) -> float:
    if uncertainty <= 0:
        uncertainty = 0.01
    geometric = (max(0, confidence) * max(0, opportunity)) ** 0.5
    de = geometric - max(0, uncertainty) * 0.3
    return max(0.0, min(100.0, de))

def compute_decision_acceleration(coin: str) -> float:
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

# ========== V7: DECISION ENERGY COMPONENTS ==========
def compute_confidence_from_score(score: int, data_confidence: int, evidence_families: int) -> float:
    conf = score * 0.7 + data_confidence * 0.2 + min(100, (evidence_families / 3) * 100) * 0.1
    return min(100.0, conf)

def compute_opportunity(rr: float, vol_spike: float, momentum: int) -> float:
    rr_score = min(60.0, max(0, rr) * 20)
    vol_score = min(20.0, max(0, (vol_spike - 1.0) * 20))
    mom_score = min(20.0, max(0, momentum) / 5)
    return rr_score + vol_score + mom_score

def compute_uncertainty(entropy_market: int, entropy_decision: int, contradiction: bool, exhaustion: int) -> float:
    unc = entropy_market * 0.6 + entropy_decision * 0.4
    if contradiction:
        unc += 20
    unc += exhaustion * 0.2
    return min(100.0, unc)

def get_entropy_adjusted_min_rr(base_rr: float, entropy_market: int) -> float:
    factor = 1.0 + (entropy_market / 100) * TUNABLE["ENTROPY_RR_FACTOR"]
    return base_rr * factor

def get_entropy_adjusted_threshold(base_threshold: int, entropy_market: int) -> int:
    factor = 1.0 + (entropy_market / 100) * TUNABLE["ENTROPY_THRESHOLD_FACTOR"]
    return max(50, min(85, int(base_threshold * factor)))

# ========== V7: TIME PRESSURE ==========
def compute_time_pressure(setup_age_minutes: float, competitor_setups: int) -> Tuple[TimePressure, float]:
    urgency_score = 0.0
    if setup_age_minutes > 20:
        urgency_score += 40
    elif setup_age_minutes > 10:
        urgency_score += 20
    elif setup_age_minutes > 5:
        urgency_score += 10
    
    if competitor_setups > 5:
        urgency_score += 30
    elif competitor_setups > 3:
        urgency_score += 15
    
    if urgency_score > TUNABLE.get("urgent_threshold", 70):
        return TimePressure.URGENT, urgency_score
    elif urgency_score > TUNABLE.get("normal_threshold", 30):
        return TimePressure.NORMAL, urgency_score
    return TimePressure.LOW, urgency_score

# ========== V7: COMMITMENT SCORE ==========
def compute_commitment_score(belief_state: BeliefState, confidence_score: float,
                              time_pressure: TimePressure, position_size_mult: float,
                              prediction_quality: float) -> float:
    state_scores = {
        BeliefState.CONVICTED: 40,
        BeliefState.BUILDING: 20,
        BeliefState.SEEKING: 0,
        BeliefState.EXECUTING: 35,
        BeliefState.INVALIDATED: 0,
    }
    score = state_scores.get(belief_state, 0)
    score += confidence_score * 0.3
    pressure_scores = {TimePressure.URGENT: 20, TimePressure.NORMAL: 10, TimePressure.LOW: 0}
    score += pressure_scores.get(time_pressure, 0)
    score += prediction_quality * 0.1
    return min(100, score)

# ========== V7: EXECUTION MODE BLEND ==========
EXECUTION_MODES = {
    "PRECISION": {"threshold_boost": 1.15},
    "BALANCED": {"threshold_boost": 1.0},
    "AGGRESSIVE": {"threshold_boost": 0.85}
}

def get_execution_mode_blend(decision_energy: float, entropy_market: int, 
                              decision_acceleration: float, intent) -> Dict[str, float]:
    if intent in [IntentType.TRAP, MarketIntent.TRAP]:
        return {"aggressive": 0.0, "balanced": 0.2, "precision": 0.8}
    
    aggressive, balanced, precision = 0.0, 0.5, 0.0
    
    if intent in [IntentType.GRAB, MarketIntent.SEEK_LIQUIDITY]:
        aggressive += 0.3
    
    if decision_energy >= TUNABLE["DECISION_ENERGY_AGGRESSIVE_THRESHOLD"]:
        aggressive += (decision_energy - 74) / 26
    elif decision_energy <= TUNABLE["DECISION_ENERGY_PRECISION_THRESHOLD"]:
        precision += (41 - decision_energy) / 41
    
    if entropy_market <= TUNABLE["ENTROPY_AGGRESSIVE_MAX"]:
        aggressive += (41 - entropy_market) / 41
    elif entropy_market >= TUNABLE["ENTROPY_PRECISION_MIN"]:
        precision += (entropy_market - 69) / 31
    
    if decision_acceleration > 0.3:
        aggressive *= 1.2
    elif decision_acceleration < -0.3:
        precision *= 1.2
    
    aggressive = min(1.0, aggressive)
    precision = min(1.0, precision)
    balanced = 1.0 - aggressive - precision
    balanced = max(0.0, min(1.0, balanced))
    
    total = aggressive + balanced + precision
    if total > 0:
        aggressive, balanced, precision = aggressive/total, balanced/total, precision/total
    
    return {"aggressive": round(aggressive, 2), "balanced": round(balanced, 2), "precision": round(precision, 2)}

def get_execution_mode_from_blend(weights: Dict[str, float]) -> str:
    if weights["precision"] > 0.5: return "PRECISION"
    if weights["aggressive"] > 0.5: return "AGGRESSIVE"
    return "BALANCED"

def get_mode_threshold_boost(weights: Dict[str, float]) -> float:
    return (weights["aggressive"] * EXECUTION_MODES["AGGRESSIVE"]["threshold_boost"] +
            weights["balanced"] * EXECUTION_MODES["BALANCED"]["threshold_boost"] +
            weights["precision"] * EXECUTION_MODES["PRECISION"]["threshold_boost"])

# ========== V7: FATIGUE PER THESIS FAMILY ==========
def update_fatigue_memory(family: str):
    now = time.time()
    with _fatigue_memory_lock:
        if family not in _fatigue_memory:
            _fatigue_memory[family] = deque(maxlen=TUNABLE["FATIGUE_MAX_PER_HOUR"] + 1)
        _fatigue_memory[family].append(now)
        while _fatigue_memory[family] and now - _fatigue_memory[family][0] > TUNABLE["FATIGUE_COOLDOWN_WINDOW"]:
            _fatigue_memory[family].popleft()

def get_fatigue_penalty_by_family(event_type: str) -> float:
    family = get_thesis_family(event_type)
    with _fatigue_memory_lock:
        if family not in _fatigue_memory:
            return 1.0
        count = len(_fatigue_memory[family])
        if count >= TUNABLE["FATIGUE_MAX_PER_HOUR"]:
            return 0.3
        if count >= 3:
            return 0.6
        return 0.8 if count >= 1 else 1.0

# ========== ACTIVE CANDIDATE ==========
def update_active_candidate_v7(coin: str, current_price: float, entropy_market: int, entry_price: float = None):
    vol_reg = get_volatility_regime()
    base_ttl = 1800 if vol_reg != "HIGH_VOLATILITY" else 900
    if vol_reg == "LOW_VOLATILITY":
        base_ttl = 3600
    
    ttl_adj = max(0.5, 1.0 - (entropy_market / 100) * TUNABLE["ENTROPY_TTL_FACTOR"])
    base_ttl = int(base_ttl * ttl_adj)
    
    if entry_price:
        dist_pct = abs(current_price - entry_price) / max(entry_price, 0.01) * 100
        if dist_pct > 2.0:
            base_ttl = int(base_ttl * 0.5)
        elif dist_pct > 1.0:
            base_ttl = int(base_ttl * 0.8)
    
    with _active_candidates_lock:
        _active_candidates[coin] = {"expire_time": time.time() + base_ttl, "last_price": current_price, "last_entropy": entropy_market}

def cleanup_active_candidates_v7():
    now = time.time()
    with _active_candidates_lock:
        expired = [c for c, d in _active_candidates.items() if now > d["expire_time"]]
        for c in expired:
            del _active_candidates[c]

def cleanup_old_shadow_decisions_v7():
    now = time.time()
    cutoff = now - TUNABLE["SHADOW_RETENTION_HOURS"] * 3600
    with _shadow_lock:
        to_delete = [sid for sid, data in _shadow_decisions.items() if data["timestamp"] < cutoff]
        for sid in to_delete:
            del _shadow_decisions[sid]
            
            
# ========== V7: FILTER GRADIENT (0-100) ==========
FILTER_WEIGHTS_BY_REGIME = {
    "HIGH_VOLATILITY": {"rejection": 0.50, "acceptance": 0.30, "persistence": 0.20},
    "NORMAL_VOLATILITY": {"rejection": 0.40, "acceptance": 0.35, "persistence": 0.25},
    "LOW_VOLATILITY": {"rejection": 0.30, "acceptance": 0.40, "persistence": 0.30},
    "TRENDING": {"rejection": 0.45, "acceptance": 0.30, "persistence": 0.25},
    "RANGING": {"rejection": 0.35, "acceptance": 0.40, "persistence": 0.25},
}

def compute_rejection_strength(coin: str, event, current_price: float, master: Dict) -> float:
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
    
    return min(100, delta_score + vol_score + wick_pct)

def compute_acceptance_strength(coin: str, event, master: Dict) -> float:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < TUNABLE["ACCEPTANCE_WINDOW_CANDLES"] + 2:
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
    
    if last_touch_idx is None or last_touch_idx + TUNABLE["ACCEPTANCE_WINDOW_CANDLES"] >= len(candles_5m):
        return 0.0
    
    accepted, total = 0, 0
    for j in range(last_touch_idx+1, min(last_touch_idx+1+TUNABLE["ACCEPTANCE_WINDOW_CANDLES"], len(candles_5m))):
        close = float(candles_5m[j]['c'])
        total += 1
        if event.direction == "LONG":
            if close > event.price_low * 1.01:
                accepted += 1
        else:
            if close < event.price_high * 0.99:
                accepted += 1
    
    if total == 0:
        return 0.0
    acceptance_pct = (accepted / total) * 100
    update_zone_memory_v7(coin, event.type, event.price_low, event.price_high, acceptance_pct)
    return acceptance_pct

def compute_persistence_strength(coin: str, event, master: Dict) -> float:
    candles_5m = get_candles(coin, "5m", 20, master)
    if not candles_5m or len(candles_5m) < 2:
        return 0.0
    
    consecutive = 0
    for i in range(len(candles_5m)-1, max(0, len(candles_5m)-8), -1):
        close = float(candles_5m[i]['c'])
        if event.direction == "LONG":
            if close > event.price_low * 1.005:
                consecutive += 1
            else:
                break
        else:
            if close < event.price_high * 0.995:
                consecutive += 1
            else:
                break
    return min(100, consecutive * 20)

def get_filter_weights(volatility_regime: str, market_regime: str) -> Dict[str, float]:
    if volatility_regime == "HIGH_VOLATILITY":
        return FILTER_WEIGHTS_BY_REGIME["HIGH_VOLATILITY"]
    if volatility_regime == "LOW_VOLATILITY":
        return FILTER_WEIGHTS_BY_REGIME["LOW_VOLATILITY"]
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        return FILTER_WEIGHTS_BY_REGIME["TRENDING"]
    return FILTER_WEIGHTS_BY_REGIME["NORMAL_VOLATILITY"]

def compute_filter_score(rejection: float, acceptance: float, persistence: float,
                          volatility_regime: str, market_regime: str) -> float:
    weights = get_filter_weights(volatility_regime, market_regime)
    return min(100.0, rejection * weights["rejection"] + acceptance * weights["acceptance"] + persistence * weights["persistence"])

# ========== V7: INTENT ENGINE ==========
def classify_market_intent(coin: str, event_type: str, direction: str,
                            delta_shift: float, oi_roc: float, vol_spike: float,
                            market_state: MarketState, cvd_accel: bool,
                            funding_pct: float) -> Tuple[MarketIntent, str, float]:
    confidence = 60.0
    
    if event_type == "LIQUIDITY" and abs(delta_shift) > 5 and vol_spike > 2.0:
        return MarketIntent.SEEK_LIQUIDITY, f"Liquidity sweep with strong delta ({delta_shift:+.1f}%)", min(90, 70 + abs(delta_shift) * 2)
    
    if event_type == "LIQUIDITY" and abs(delta_shift) < 2 and vol_spike < 1.2:
        return MarketIntent.TRAP, "Liquidity sweep without flow confirmation, potential trap", 65
    
    if event_type in ("OB", "OB_FLOW", "FVG", "FVG_FLOW"):
        if (direction == "LONG" and delta_shift > 3 and oi_roc > 2) or (direction == "SHORT" and delta_shift < -3 and oi_roc > 2):
            return MarketIntent.CONTINUE, f"Continuation: delta {delta_shift:+.1f}%, OI +{oi_roc:.1f}%", min(85, 65 + abs(delta_shift))
        return MarketIntent.ACCEPT, "Acceptance zone, price consolidation", 60
    
    if (direction == "SHORT" and funding_pct > 0.05 and oi_roc > 5) or (direction == "LONG" and funding_pct < -0.05 and oi_roc > 5):
        return MarketIntent.DISTRIBUTE, f"Distribution detected: funding {funding_pct:+.3f}%, OI +{oi_roc:.1f}%", 75
    
    return MarketIntent.ACCEPT, "Standard acceptance, no strong intent detected", 50

# ========== V7: WHY NOT EXPLANATION ==========
def generate_why_not(coin: str, funding_pct: float, entropy_market: int, oi_roc: float,
                      market_intent: MarketIntent, active_candidates_count: int,
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
    return ", ".join(deterrents[:3]) if deterrents else "no strong deterrents"
    
    
# ========== EVENT DETECTION ==========
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
        return volume_ok and (oi_persist or oi_change >= 3.0)
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
        return volume_ok and (abs(delta_shift) > 3 or delta_persist)
    except:
        return True

def detect_displacement(c1: dict, c2: dict, vol_multiplier: float = 1.5) -> bool:
    try:
        range1, range2 = float(c1['h']) - float(c1['l']), float(c2['h']) - float(c2['l'])
        vol1, vol2 = float(c1.get('v', 1)), float(c2.get('v', 1))
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
        c, nxt = candles[i], candles[i+1]
        o, cl, no, nc = float(c['o']), float(c['c']), float(nxt['o']), float(nxt['c'])
        if direction == "LONG" and cl < o and nc > no and nc > float(c['h']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i+2, len(candles)-1):
                if float(candles[j]['c']) < ob_low:
                    fresh = False
                    break
            if fresh:
                mid = (ob_low+ob_high)/2
                dist = abs(mid-current_price)/max(current_price, 0.01)*100
                if dist <= max_dist_pct and validate_ob_with_volume_oi(coin, i, master):
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
                dist = abs(mid-current_price)/max(current_price, 0.01)*100
                if dist <= max_dist_pct and validate_ob_with_volume_oi(coin, i, master):
                    return TradeEvent("OB", ob_low, ob_high, 75, "SHORT", {"idx": i}, confidence=70, source_count=1)
    return None

def find_fvg_advanced(candles, current_price, max_dist_pct=2.0, master=None, coin=None) -> Optional[TradeEvent]:
    for i in range(len(candles)-1, 1, -1):
        c1, c3 = candles[i-2], candles[i]
        c1h, c1l, c3h, c3l = float(c1['h']), float(c1['l']), float(c3['h']), float(c3['l'])
        
        if c3l > c1h:
            gap_low, gap_high = c1h, c3l
            gap_pct = (gap_high - gap_low)/max(gap_low, 0.01)*100
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
                dist = abs(mid-current_price)/max(current_price, 0.01)*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bullish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(coin, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct > 0.3 else 0) + (15 if filled < 0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "LONG", {"fill_ratio": filled}, confidence=conf, source_count=1)
        
        if c3h < c1l:
            gap_low, gap_high = c3h, c1l
            gap_pct = (gap_high - gap_low)/max(gap_low, 0.01)*100
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
                dist = abs(mid-current_price)/max(current_price, 0.01)*100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bearish", "idx": i, "filled": filled}
                    if validate_fvg_with_volume_reaction(coin, fvg_data, master):
                        strength = 65 if gap_pct > 0.3 else 55
                        conf = 55 + (10 if gap_pct > 0.3 else 0) + (15 if filled < 0.3 else 0)
                        return TradeEvent("FVG", gap_low, gap_high, strength, "SHORT", {"fill_ratio": filled}, confidence=conf, source_count=1)
    return None

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
        bids, asks = l2['levels'][0], l2['levels'][1]
        def usd_depth(levels, n):
            return sum(float(x['sz']) * float(x['px']) for x in levels[:n])
        near = usd_depth(bids, 5) + usd_depth(asks, 5)
        total = usd_depth(bids, 20) + usd_depth(asks, 20)
        if total == 0:
            return False, 0, 0, 0, 0
        ratio = near / total
        drop_ratio = 1 - ratio
        return ratio < 0.3, int(drop_ratio * 100), near, total, drop_ratio
    except:
        return False, 0, 0, 0, 0

def find_ob_from_orderbook(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        delta_shift = get_delta_shift(coin)
        bid_wall, bid_price = get_bid_wall_level(coin)
        if bid_wall >= TUNABLE["MIN_OB_FLOW_WALL_USD"] and delta_shift > TUNABLE["MIN_OB_FLOW_DELTA_SHIFT"]:
            if current_price <= bid_price * 1.005:
                conf = min(85, 70 + int(delta_shift / 2))
                return TradeEvent("OB_FLOW", bid_price*0.998, bid_price*1.002, 75, "LONG",
                                  {"wall_usd": bid_wall, "delta_shift": delta_shift}, confidence=conf, source_count=1)
        ask_wall, ask_price = get_ask_wall_level(coin)
        if ask_wall >= TUNABLE["MIN_OB_FLOW_WALL_USD"] and delta_shift < -TUNABLE["MIN_OB_FLOW_DELTA_SHIFT"]:
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
        if abs(cvd_change) < TUNABLE["MIN_FVG_FLOW_CVD_ACCEL"]:
            return None
        if cvd_change > TUNABLE["MIN_FVG_FLOW_CVD_ACCEL"] and delta_shift > TUNABLE["MIN_FVG_FLOW_DELTA_DIVERGENCE"]:
            fair_price = current_price * (1 + cvd_change / 100)
            conf = min(80, 60 + int(cvd_change * 10))
            return TradeEvent("FVG_FLOW", current_price, max(current_price, fair_price), 65, "LONG",
                              {"cvd_change": cvd_change, "delta_shift": delta_shift}, confidence=conf, source_count=1)
        if cvd_change < -TUNABLE["MIN_FVG_FLOW_CVD_ACCEL"] and delta_shift < -TUNABLE["MIN_FVG_FLOW_DELTA_DIVERGENCE"]:
            fair_price = current_price * (1 + cvd_change / 100)
            conf = min(80, 60 + int(abs(cvd_change) * 10))
            return TradeEvent("FVG_FLOW", min(current_price, fair_price), current_price, 65, "SHORT",
                              {"cvd_change": cvd_change, "delta_shift": delta_shift}, confidence=conf, source_count=1)
    except Exception as e:
        logger.debug(f"FVG_FLOW error {coin}: {e}")
    return None

def find_liquidity_vacuum_area(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        is_vacuum, severity, _, _, _ = detect_liquidity_vacuum(coin)
        if is_vacuum and severity >= TUNABLE["LIQUIDITY_VACUUM_AREA_THRESHOLD"]:
            atr_pct = get_atr_pct(coin, 14, "1h", master)
            vacuum_range = atr_pct * 0.5
            low = current_price * (1 - vacuum_range / 100)
            high = current_price * (1 + vacuum_range / 100)
            conf = min(80, 55 + int(severity / 2))
            return TradeEvent("VACUUM", low, high, 60, "BOTH",
                              {"severity": severity, "depth_drop_pct": (1 - severity/100) * 100}, confidence=conf, source_count=1)
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
    if liq: events.append(liq)
    
    ob_long = find_ob(candles_1h, "LONG", current_price, master=master, coin=coin)
    ob_short = find_ob(candles_1h, "SHORT", current_price, master=master, coin=coin)
    if ob_long: events.append(ob_long)
    if ob_short: events.append(ob_short)
    
    fvg = find_fvg_advanced(candles_1h, current_price, master=master, coin=coin)
    if fvg: events.append(fvg)
    
    ob_flow = find_ob_from_orderbook(coin, current_price, master)
    if ob_flow: events.append(ob_flow)
    
    fvg_flow = find_fvg_from_flow(coin, current_price, master)
    if fvg_flow: events.append(fvg_flow)
    
    vacuum_area = find_liquidity_vacuum_area(coin, current_price, master)
    if vacuum_area: events.append(vacuum_area)
    
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
            price_low=low, price_high=high,
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
    dist = abs(mid - current_price) / max(current_price, 0.01) * 100
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
        price_roc = (price_now - price_5m_ago) / max(price_5m_ago, 0.01) * 100 if price_5m_ago else 0
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
        roc5 = (close_now - close_5m) / max(close_5m, 0.01) * 100 if close_5m else 0
        roc15 = (close_now - close_15m) / max(close_15m, 0.01) * 100 if close_15m else 0
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
    vol_score = 90 if vol_spike >= 2.0 else (70 if vol_spike >= 1.5 else (50 if vol_spike >= 1.2 else 30))
    
    delta_shift = get_delta_shift(coin)
    delta_score = 90 if delta_shift > 8 else (70 if delta_shift > 4 else (50 if delta_shift > 2 else 30))
    
    return min(100, max(0, int(roc_score * 0.3 + vol_score * 0.3 + delta_score * 0.4)))

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
    
    risk = abs(mark - sl) / max(mark, 0.01) * 100
    reward = abs(tp - mark) / max(mark, 0.01) * 100
    rr = reward / risk if risk > 0 else 0
    return sl, tp, rr

def get_oi_usd(coin: str) -> float:
    snapshot = get_snapshot()
    if snapshot and coin in snapshot.oi:
        return snapshot.oi[coin]
    return 0.0

def get_funding_pct(coin: str) -> float:
    snapshot = get_snapshot()
    if snapshot and coin in snapshot.funding:
        return snapshot.funding[coin]
    return 0.0
    
    
# ========== V7: LAYER 1 - OBSERVE ==========
def observe_market(coin: str, mark: float, master_candles: Dict) -> Optional[Dict]:
    """Layer 1: Kumpulkan semua data dan event"""
    data_confidence, ages = get_data_confidence(coin, time.time())
    if data_confidence < TUNABLE["MIN_DATA_CONFIDENCE"]:
        return None
    
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
        penalty = get_zone_penalty_v7(coin, ev.type, ev.price_low, ev.price_high)
        ev.score = max(0, ev.score - penalty)
    
    best_event = max(clustered, key=lambda e: e.score) if clustered else None
    if not best_event or best_event.score < 40:
        return None
    
    return {
        "coin": coin, "mark": mark, "best_event": best_event,
        "data_confidence": data_confidence, "ages": ages,
        "atr_pct": atr_pct, "vol_spike": vol_spike, "delta": delta,
        "cvd_accel": cvd_accel, "momentum": momentum,
        "structure_valid_long": structure_valid_long, "structure_valid_short": structure_valid_short,
        "market_state": market_state, "market_regime": market_regime,
        "volatility_regime": volatility_regime, "flow_regime": flow_regime,
        "oi_roc": oi_roc, "funding_pct": funding_pct, "clustered": clustered,
        "master_candles": master_candles
    }
    
    
# ========== V7: LAYER 2 - BUILD THESIS ==========
def build_thesis(obs: Dict) -> Optional[Dict]:
    """Layer 2: Dari event ke thesis (INDEPENDENT dari confidence)"""
    coin = obs["coin"]
    event = obs["best_event"]
    mark = obs["mark"]
    
    # Market state filter
    if obs["market_state"] == MarketState.REVERSAL:
        if event.type != "LIQUIDITY" and "LIQUIDITY" not in event.extra.get("members", []):
            return None
    elif obs["market_state"] == MarketState.EXPANSION:
        if event.type == "LIQUIDITY" or "LIQUIDITY" in event.extra.get("members", []):
            return None
    
    # Structure filter
    if event.direction == "LONG" and not obs["structure_valid_long"]:
        return None
    if event.direction == "SHORT" and not obs["structure_valid_short"]:
        return None
    
    # VACUUM direction resolution
    if event.type == "VACUUM" and event.direction == "BOTH":
        _delta_now = get_ob_delta(coin)
        if _delta_now > 5:
            event.direction = "LONG"
            event.confidence = min(100, event.confidence + 5)
        elif _delta_now < -5:
            event.direction = "SHORT"
            event.confidence = min(100, event.confidence + 5)
        else:
            return None
    
    # Intent classification
    intent, intent_explanation, intent_confidence = classify_market_intent(
        coin, event.type, event.direction,
        obs["delta"], obs["oi_roc"], obs["vol_spike"],
        obs["market_state"], obs["cvd_accel"], obs["funding_pct"]
    )
    
    legacy_intent_map = {
        MarketIntent.SEEK_LIQUIDITY: IntentType.GRAB,
        MarketIntent.TRAP: IntentType.TRAP,
        MarketIntent.ACCEPT: IntentType.ACCEPT,
        MarketIntent.CONTINUE: IntentType.CONTINUE,
        MarketIntent.DISTRIBUTE: IntentType.ACCEPT,
    }
    intent_legacy = legacy_intent_map.get(intent, IntentType.ACCEPT)
    
    # Filter gradient
    rejection = compute_rejection_strength(coin, event, mark, obs["master_candles"])
    acceptance = compute_acceptance_strength(coin, event, obs["master_candles"])
    persistence = compute_persistence_strength(coin, event, obs["master_candles"])
    filter_score = compute_filter_score(rejection, acceptance, persistence,
                                         obs["volatility_regime"], obs["market_regime"])
    
    # Fatigue
    fatigue_penalty = get_fatigue_penalty_by_family(event.type)
    
    # BELIEF (INDEPENDENT)
    belief, belief_score, belief_reason = compute_belief(
        event, filter_score, obs["structure_valid_long"], obs["structure_valid_short"], 0.0
    )
    
    # Update belief state
    update_belief_state(coin, belief, belief_score, belief_reason)
    current_belief, _, _ = get_belief_state(coin)
    
    return {
        "coin": coin, "event": event, "intent": intent, "intent_legacy": intent_legacy,
        "intent_explanation": intent_explanation, "intent_confidence": intent_confidence,
        "rejection": rejection, "acceptance": acceptance, "persistence": persistence,
        "filter_score": filter_score, "fatigue_penalty": fatigue_penalty,
        "belief": belief, "belief_score": belief_score, "current_belief": current_belief,
        "master_candles": obs["master_candles"], "mark": mark,
        "data_confidence": obs["data_confidence"],
        "market_regime": obs["market_regime"], "volatility_regime": obs["volatility_regime"],
        "flow_regime": obs["flow_regime"], "atr_pct": obs["atr_pct"],
        "vol_spike": obs["vol_spike"], "momentum": obs["momentum"],
        "funding_pct": obs["funding_pct"], "oi_roc": obs["oi_roc"],
        "clustered": obs["clustered"], "ages": obs["ages"]
    }
    
    
# ========== V7: LAYER 3 - COMPUTE CONFIDENCE ==========
def compute_confidence(thesis_data: Dict) -> Optional[Dict]:
    """Layer 3: Hitung semua score (INDEPENDENT dari belief)"""
    coin = thesis_data["coin"]
    event = thesis_data["event"]
    clustered = thesis_data["clustered"]
    
    # Score calculation
    score_long, score_short = 0, 0
    if event.direction == "LONG":
        score_long = event.score
        short_events = [e for e in clustered if e.direction == "SHORT"]
        score_short = max([e.score for e in short_events]) if short_events else 0
    else:
        score_short = event.score
        long_events = [e for e in clustered if e.direction == "LONG"]
        score_long = max([e.score for e in long_events]) if long_events else 0
    
    contradiction = (score_long > 55 and score_short > 55)
    
    # Independent evidence families
    price_ok, flow_ok, pos_ok, evidence_reasons = get_independent_evidence_families(
        coin, event.direction, thesis_data["master_candles"]
    )
    evidence_families = (1 if price_ok else 0) + (1 if flow_ok else 0) + (1 if pos_ok else 0)
    exhaustion = compute_exhaustion_score(coin, thesis_data["master_candles"])
    
    # 3 types of entropy
    entropy_data = compute_data_entropy(thesis_data["ages"])
    entropy_market = compute_market_entropy_v7(coin, thesis_data["master_candles"])
    score_variance = abs(score_long - score_short) if score_long > 0 and score_short > 0 else 0
    event_types = [ev.type for ev in clustered]
    entropy_decision = compute_decision_entropy(score_variance, contradiction, len(event_types) > 2, event_types)
    
    trend_strength = compute_trend_strength_v7(coin, thesis_data["master_candles"])
    
    # Decision Vector
    decision_score, ev_mult, _, contributions = compute_decision_vector(
        coin, event, score_long, score_short, evidence_families, entropy_market, exhaustion,
        thesis_data["market_regime"], thesis_data["volatility_regime"], thesis_data["data_confidence"]
    )
    
    # Counterfactual
    cf_adjusted_score, _ = evaluate_counterfactual_influence(
        coin, entropy_market, evidence_families, exhaustion, decision_score, thesis_data["data_confidence"]
    )
    final_score = decision_score
    
    # CONFIDENCE (INDEPENDENT)
    confidence = compute_confidence_from_score(final_score, thesis_data["data_confidence"], evidence_families)
    
    # SL/TP
    sl, tp, rr = calculate_sltp_advanced(coin, thesis_data["mark"], event.direction, event,
                                         thesis_data["atr_pct"], thesis_data["master_candles"])
    min_rr = get_dynamic_min_rr(thesis_data["market_regime"])
    min_rr = get_entropy_adjusted_min_rr(min_rr, entropy_market)
    if rr < min_rr:
        return None
    
    # Opportunity & Uncertainty
    opportunity = compute_opportunity(rr, thesis_data["vol_spike"], thesis_data["momentum"])
    uncertainty = compute_uncertainty(entropy_market, entropy_decision, contradiction, exhaustion)
    
    # Decision Energy
    decision_energy = compute_decision_energy_v7(confidence, opportunity, uncertainty)
    update_decision_energy_history(coin, decision_energy)
    decision_acceleration = compute_decision_acceleration(coin)
    
    # Time Pressure
    setup_age_minutes = (time.time() - event.first_seen) / 60
    competitor_count = len(_active_candidates)
    time_pressure, urgency_score = compute_time_pressure(setup_age_minutes, competitor_count)
    
    # Position Sizing
    prediction_quality_mult = get_prediction_quality_multiplier(coin)
    position_size_mult = get_position_size_multiplier_v7(entropy_market, prediction_quality_mult, thesis_data["intent_legacy"])
    position_size_mult *= thesis_data["fatigue_penalty"]
    
    return {
        "score_long": score_long, "score_short": score_short,
        "contradiction": contradiction, "evidence_families": evidence_families,
        "exhaustion": exhaustion, "entropy_data": entropy_data,
        "entropy_market": entropy_market, "entropy_decision": entropy_decision,
        "final_score": final_score, "confidence": confidence,
        "sl": sl, "tp": tp, "rr": rr,
        "opportunity": opportunity, "uncertainty": uncertainty,
        "decision_energy": decision_energy, "decision_acceleration": decision_acceleration,
        "time_pressure": time_pressure, "position_size_mult": position_size_mult,
        "prediction_quality_mult": prediction_quality_mult,
        "evidence_reasons": evidence_reasons, "negative_reasons": [],
        "contributions": contributions,
        "price_ok": price_ok, "flow_ok": flow_ok, "pos_ok": pos_ok,
        "setup_age_minutes": setup_age_minutes,
        "thesis_data": thesis_data
    }
    
    
# ========== V7: LAYER 4 - EXECUTE DECISION ==========
def execute_decision(coin: str, thesis_data: Dict, confidence_data: Dict, 
                      event: TradeEvent, intent, intent_legacy) -> Optional[dict]:
    """Layer 4: Eksekusi atau wait/skip"""
    mark = thesis_data["mark"]
    belief = thesis_data["current_belief"]
    filter_score = thesis_data["filter_score"]
    fatigue_penalty = thesis_data["fatigue_penalty"]
    
    # Execution mode blend
    mode_weights = get_execution_mode_blend(
        confidence_data["decision_energy"], confidence_data["entropy_market"],
        confidence_data["decision_acceleration"], intent_legacy
    )
    execution_mode = get_execution_mode_from_blend(mode_weights)
    threshold_boost = get_mode_threshold_boost(mode_weights)
    
    # Dynamic threshold
    base_threshold = get_dynamic_threshold(coin, thesis_data["market_regime"], thesis_data["volatility_regime"])
    entropy_adjusted_threshold = get_entropy_adjusted_threshold(base_threshold, confidence_data["entropy_market"])
    
    filter_penalty = 1.0 + ((100 - filter_score) / 100) * 0.5
    adjusted_threshold = int(entropy_adjusted_threshold * threshold_boost * filter_penalty)
    
    size_boost = 1.0 + (1.0 - confidence_data["position_size_mult"]) * 0.2
    final_threshold = int(adjusted_threshold / max(size_boost, 0.1))
    
    # Commitment score
    commitment_score = compute_commitment_score(
        belief, confidence_data["confidence"], confidence_data["time_pressure"],
        confidence_data["position_size_mult"], confidence_data["prediction_quality_mult"]
    )
    
    # Cost of Waiting
    wait_value, _, _ = compute_value_of_waiting_v5(
        confidence_data["confidence"], confidence_data["opportunity"],
        confidence_data["uncertainty"], confidence_data["setup_age_minutes"]
    )
    should_execute, wait_reason, _ = should_wait_or_execute_v5(
        confidence_data["decision_energy"], wait_value, confidence_data["decision_energy"]
    )
    
    # Threshold check
    if confidence_data["final_score"] < final_threshold:
        if confidence_data["position_size_mult"] > 0.3:
            confidence_data["position_size_mult"] = max(0.15, confidence_data["position_size_mult"] * 0.7)
        else:
            update_fatigue_memory(event.type)
            return None
    
    # UNCLEAR check
    if (confidence_data["score_long"] > TUNABLE["UNCLEAR_THRESHOLD"] and 
        confidence_data["score_short"] > TUNABLE["UNCLEAR_THRESHOLD"] and 
        abs(confidence_data["score_long"] - confidence_data["score_short"]) < TUNABLE["UNCLEAR_DIFF"]):
        update_fatigue_memory(event.type)
        return None
    
    # Generate thesis
    thesis_obj = generate_thesis_from_event_v7(coin, event, mark, thesis_data["market_state"], intent, belief)
    
    # Negative evidence
    negative_reasons = []
    if not confidence_data["price_ok"]:
        negative_reasons.append("price")
    if not confidence_data["flow_ok"]:
        negative_reasons.append("flow")
    if not confidence_data["pos_ok"]:
        negative_reasons.append("positioning")
    negative_str = ", ".join(negative_reasons) if negative_reasons else "none"
    
    # WHY NOT
    active_count = len(_active_candidates)
    why_not = generate_why_not(thesis_data["funding_pct"], confidence_data["entropy_market"],
                               thesis_data["oi_roc"], intent, active_count, fatigue_penalty)
    
    confidence_breakdown = f"S:{confidence_data['score_long']:.0f}|F:{filter_score:.0f}|E:{confidence_data['evidence_families']}"
    
    reason = (f"{event.type} | Intent:{intent.value} | Belief:{belief.value} | "
              f"Mode:{execution_mode} | Filter:{filter_score:.0f} | DE:{confidence_data['decision_energy']:.1f} | Score:{confidence_data['final_score']}")
    
    signal_id = generate_signal_id(coin, event.direction)
    eval_delay = get_evaluation_delay(thesis_data["atr_pct"], confidence_data["rr"], thesis_data["market_regime"])
    
    # Save
    if not PAPER_MODE:
        save_signal_v7(signal_id, coin, event.direction, confidence_data["final_score"], mark,
                      confidence_data["sl"], confidence_data["tp"], confidence_data["rr"], reason,
                      thesis_data["data_confidence"], thesis_obj.statement, thesis_obj.invalidation,
                      thesis_obj.confirmation, execution_mode, intent.value,
                      confidence_data["decision_energy"], confidence_data["position_size_mult"],
                      filter_score, thesis_data["intent_confidence"], belief.value,
                      commitment_score, confidence_data["time_pressure"].value,
                      confidence_data["prediction_quality_mult"] * 100)
        
        add_journal_entry_v7(coin, thesis_data["market_regime"], thesis_data["volatility_regime"],
                            thesis_data["flow_regime"], belief.value,
                            confidence_data["score_long"], confidence_data["score_short"],
                            event.direction, confidence_data["final_score"], reason, negative_str,
                            confidence_data["entropy_data"], confidence_data["entropy_market"],
                            confidence_data["entropy_decision"],
                            int((time.time() - 0) * 1000), int((time.time() - 0) * 1000),
                            thesis_data["data_confidence"], True,
                            execution_mode=execution_mode, intent_type=intent.value,
                            decision_energy=confidence_data["decision_energy"],
                            position_size_mult=confidence_data["position_size_mult"],
                            filter_score=filter_score, rejection_strength=thesis_data["rejection"],
                            acceptance_strength=thesis_data["acceptance"],
                            persistence_strength=thesis_data["persistence"],
                            why_not=why_not, wait_value=wait_value,
                            time_pressure=confidence_data["time_pressure"].value,
                            commitment_score=commitment_score,
                            decision_acceleration=confidence_data["decision_acceleration"],
                            mode_aggressive=mode_weights["aggressive"],
                            mode_balanced=mode_weights["balanced"],
                            mode_precision=mode_weights["precision"],
                            confidence_breakdown=confidence_breakdown)
        
        _EVAL_EXECUTOR.submit(evaluate_signal_v7, signal_id, coin, event.direction, mark,
                              confidence_data["sl"], confidence_data["tp"], thesis_data["data_confidence"],
                              confidence_data["entropy_market"], confidence_data["evidence_families"],
                              confidence_data["exhaustion"], thesis_obj.statement,
                              thesis_obj.invalidation, thesis_obj.confirmation, eval_delay,
                              event.price_low, event.price_high, event.direction)
    
    update_active_candidate_v7(coin, mark, confidence_data["entropy_market"], mark)
    
    positive_factors = [event.type] + confidence_data["evidence_reasons"]
    if thesis_data["vol_spike"] >= 1.5:
        positive_factors.append("volume")
    if thesis_data["cvd_accel"]:
        positive_factors.append("cvd_accel")
    
    explanation = explain_decision_with_contribution(
        coin, event.direction, confidence_data["final_score"],
        positive_factors, negative_reasons, confidence_data["contributions"],
        confidence_data["entropy_market"], final_threshold, thesis_data["data_confidence"]
    )
    
    return {
        "coin": coin, "direction": event.direction, "score": confidence_data["final_score"],
        "entry": mark, "sl": confidence_data["sl"], "tp": confidence_data["tp"],
        "rr": confidence_data["rr"], "reason": reason, "area": event.type,
        "label": get_confidence_label(confidence_data["final_score"]),
        "contradiction": confidence_data["contradiction"],
        "exhaustion": confidence_data["exhaustion"],
        "entropy_data": confidence_data["entropy_data"],
        "entropy_market": confidence_data["entropy_market"],
        "entropy_decision": confidence_data["entropy_decision"],
        "evidence_families": confidence_data["evidence_families"],
        "positive_evidence": confidence_data["evidence_reasons"],
        "negative_evidence": negative_str,
        "data_confidence": thesis_data["data_confidence"],
        "contributions": confidence_data["contributions"],
        "execution_mode": execution_mode, "intent_type": intent.value,
        "decision_energy": confidence_data["decision_energy"],
        "position_size_mult": confidence_data["position_size_mult"],
        "filter_score": filter_score, "rejection_strength": thesis_data["rejection"],
        "acceptance_strength": thesis_data["acceptance"],
        "persistence_strength": thesis_data["persistence"],
        "why_not": why_not, "wait_value": wait_value,
        "belief_state": belief.value, "commitment_score": commitment_score,
        "time_pressure": confidence_data["time_pressure"].value,
        "decision_acceleration": confidence_data["decision_acceleration"],
        "fatigue_penalty": fatigue_penalty,
        "mode_aggressive": mode_weights["aggressive"],
        "mode_balanced": mode_weights["balanced"],
        "mode_precision": mode_weights["precision"],
        "confidence_breakdown": confidence_breakdown,
        "hypothesis": {
            "thesis": thesis_obj.statement, "invalidate": thesis_obj.invalidation,
            "observe": thesis_obj.confirmation, "destination": thesis_obj.destination,
            "timeframe": thesis_obj.timeframe
        },
        "explanation": explanation
    }

# ========== V7: CHECK ENTRY ALERT (4 LAYER) ==========
def check_entry_alert_v7(coin: str, mark: float, master_candles: Dict) -> Optional[dict]:
    """V7: 4-layer entry check - clean, decoupled, maintainable"""
    try:
        # Layer 1: Observe
        obs = observe_market(coin, mark, master_candles)
        if not obs:
            return None
        
        # Layer 2: Build Thesis
        thesis_data = build_thesis(obs)
        if not thesis_data:
            return None
        
        # Layer 3: Compute Confidence
        confidence_data = compute_confidence(thesis_data)
        if not confidence_data:
            return None
        
        # Layer 4: Execute
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"]
        )
        
        # Log trace jika ada result
        if result:
            trace = DecisionTrace(
                timestamp=time.time(),
                coin=coin,
                event_type=result["area"],
                belief_state=result["belief_state"],
                confidence=result["decision_energy"],
                decision_energy=result["decision_energy"],
                final_decision="EXECUTE",
                reasons=result["positive_evidence"],
                why_not=[result["why_not"]] if result["why_not"] else [],
                what_changed=f"belief:{result['belief_state']}|mode:{result['execution_mode']}"
            )
            log_decision_trace(trace)
        
        return result
        
    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None
        
        
# ========== V7: HELPER FUNCTIONS (dipindah ke sini) ==========
def compute_decision_vector(coin: str, event: TradeEvent, score_long: int, score_short: int,
                            evidence_families: int, entropy: int, exhaustion: int,
                            market_regime: str, volatility_regime: str, data_confidence: int) -> Tuple[int, float, str, Dict[str, int]]:
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        _base_mults = {3: 1.0, 2: 0.75, 1: 0.45}
    elif market_regime in ("PANIC", "VOLATILE"):
        _base_mults = {3: 0.85, 2: 0.6, 1: 0.35}
    else:
        _base_mults = {3: TUNABLE["EVIDENCE_MULT_3"], 2: TUNABLE["EVIDENCE_MULT_2"], 1: TUNABLE["EVIDENCE_MULT_1"]}
    
    ev_mult = _base_mults.get(min(evidence_families, 3), TUNABLE["EVIDENCE_MULT_1"])
    raw_score = score_long if event.direction == "LONG" else score_short
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
    return max(0, min(100, original_score + total_adj)), adjustments

def explain_decision_with_contribution(coin: str, direction: str, score: int,
                                       positive_factors: List[str], negative_factors: List[str],
                                       contributions: Dict[str, int],
                                       entropy: int, threshold: int, data_confidence: int) -> str:
    pos_str = ", ".join(positive_factors[:3]) if positive_factors else "none"
    neg_str = ", ".join(negative_factors[:3]) if negative_factors else "none"
    contrib_str = " | ".join([f"{k}:{v:+d}" for k, v in contributions.items()]) if contributions else "none"
    return (f"📊 *Decision Explanation*\n"
            f"✅ Positive: {pos_str}\n"
            f"❌ Negative: {neg_str}\n"
            f"📈 Contribution: {contrib_str}\n"
            f"🌀 Market Entropy: {entropy}\n"
            f"📡 Data confidence: {data_confidence}%\n"
            f"🎯 Final score: {score}\n")

def get_position_size_multiplier_v7(entropy: int, prediction_quality_mult: float, intent) -> float:
    entropy_factor = 1.0 - (entropy / 100) * TUNABLE["SIZE_ENTROPY_FACTOR"]
    entropy_size = max(TUNABLE["SIZE_MIN"], min(1.0, entropy_factor))
    quality_size = max(0.6, min(1.4, prediction_quality_mult))
    intent_factors = {
        IntentType.GRAB: 1.2, IntentType.CONTINUE: 1.15,
        IntentType.ACCEPT: 1.0, IntentType.TRAP: 0.5,
        MarketIntent.SEEK_LIQUIDITY: 1.3, MarketIntent.DISTRIBUTE: 0.7,
    }
    intent_factor = intent_factors.get(intent, 1.0)
    return max(TUNABLE["SIZE_MIN"], min(TUNABLE["SIZE_MAX"], entropy_size * quality_size * intent_factor))

def get_evaluation_delay(atr_pct: float, rr: float, regime: str) -> int:
    base = TUNABLE["BASE_EVALUATION_DELAY"]
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

def compute_value_of_waiting_v5(current_confidence: float, current_opportunity: float, 
                                 current_uncertainty: float, setup_age_minutes: float) -> Tuple[float, float, bool]:
    confidence_gain = min(20.0, setup_age_minutes * TUNABLE.get("confidence_gain_rate", 2.0))
    future_confidence = min(100.0, current_confidence + confidence_gain)
    opportunity_decay = 1.0 - (setup_age_minutes * TUNABLE.get("opportunity_decay", 0.08))
    future_opportunity = current_opportunity * max(0.1, opportunity_decay)
    uncertainty_decay = max(0.5, 1.0 - (setup_age_minutes * 0.01))
    future_uncertainty = current_uncertainty * uncertainty_decay
    relevance_prob = max(0.1, 1.0 - (setup_age_minutes * TUNABLE.get("decay_rate", 0.05)))
    expected_decay = max(0.1, 1.0 - (setup_age_minutes * TUNABLE.get("expected_decay_rate", 0.03)))
    if future_uncertainty <= 0:
        future_uncertainty = 0.01
    raw_wait_value = (future_confidence * future_opportunity) / future_uncertainty
    wait_value = raw_wait_value * relevance_prob * expected_decay
    max_wait_reached = setup_age_minutes > TUNABLE.get("max_wait_minutes", 30)
    if max_wait_reached:
        wait_value = 0.0
    return wait_value, expected_decay, max_wait_reached

def should_wait_or_execute_v5(current_value: float, wait_value: float, decision_energy: float) -> Tuple[bool, str, float]:
    threshold = TUNABLE.get("wait_threshold", 0.85)
    if wait_value <= 0:
        return True, "execute (wait_value=0, too old)", 1.0
    ratio = current_value / (wait_value * threshold) if wait_value > 0 else 999
    wait_confidence = min(1.0, ratio / 2)
    if current_value >= wait_value * threshold:
        return True, f"execute (current={current_value:.1f} > wait={wait_value:.1f}*{threshold})", wait_confidence
    return False, f"wait (current={current_value:.1f} < wait={wait_value:.1f}*{threshold})", wait_confidence

def generate_thesis_from_event_v7(coin: str, event: TradeEvent, current_price: float,
                                   market_state: MarketState, intent, belief_state: BeliefState) -> Thesis:
    t, d = event.type, event.direction
    intent_str = intent.value if hasattr(intent, 'value') else str(intent)
    belief_str = belief_state.value if hasattr(belief_state, 'value') else str(belief_state)
    
    if t == "LIQUIDITY":
        lvl = event.price_low if d == "LONG" else event.price_high
        trigger = "Bullish reclaim" if d == "LONG" else "Bearish rejection"
        invalidation = f"Close below {lvl * 0.998:.4f}" if d == "LONG" else f"Close above {lvl * 1.002:.4f}"
        confirmation = "Delta positive >3 for 3x 5m" if d == "LONG" else "Delta negative <-3 for 3x 5m"
        return Thesis(f"Liquidity sweep {'lows' if d == 'LONG' else 'highs'} at {fmt_price(lvl)} - intent: {intent_str}, belief: {belief_str}",
                      trigger, invalidation, confirmation, "Next swing target", d, "15m")
    
    if t == "OB":
        return Thesis(f"Order block {'demand' if d == 'LONG' else 'supply'} at {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                      "Price touches OB zone and shows rejection",
                      f"Close below OB low {event.price_low:.4f}" if d == "LONG" else f"Close above OB high {event.price_high:.4f}",
                      "Volume spike >1.5x and OI persistence", "Previous structure level", d, "1h")
    
    if t in ("FVG", "FVG_FLOW"):
        return Thesis(f"{'Bullish' if d == 'LONG' else 'Bearish'} FVG {fmt_price(event.price_low)}-{fmt_price(event.price_high)} - intent: {intent_str}, belief: {belief_str}",
                      "Price enters FVG zone", "FVG >70% filled without reaction",
                      "CVD acceleration + delta shift", "Premium/Discount side", d, "1h")
    
    if t == "VACUUM":
        return Thesis(f"Liquidity vacuum area - {event.extra.get('severity', 0)}% depth drop - intent: {intent_str}, belief: {belief_str}",
                      "Price enters vacuum with directional delta", "Delta neutral or reverses",
                      "Volume spike confirming move", "Next liquidity cluster", d, "5m")
    
    return Thesis(f"{t} {d} setup at {fmt_price(current_price)} - intent: {intent_str}, belief: {belief_str}",
                  "Price action confirmation", "Invalidation breached", "Flow confirms direction", "ATR target", d, "1h")

# ========== EVALUATE SIGNAL V7 ==========
def evaluate_signal_v7(signal_id, coin, direction, entry, sl, tp, data_confidence,
                       entropy_market, evidence_families, exhaustion, thesis, invalidate, observe, eval_delay,
                       predicted_zone_low, predicted_zone_high, predicted_direction):
    time.sleep(eval_delay)
    if not RUNTIME.is_running():
        return
    try:
        candles = get_candles(coin, "5m", 100)
        if not candles:
            return
        
        entry_time = int(time.time() - eval_delay)
        high_prices, low_prices = [], []
        for c in candles:
            ts = c.get('t', 0)
            if ts >= entry_time * 1000:
                high_prices.append(float(c['h']))
                low_prices.append(float(c['l']))
        
        if high_prices and low_prices:
            if direction == "LONG":
                mfe, mae = (max(high_prices) - entry) / max(entry, 0.01) * 100, (min(low_prices) - entry) / max(entry, 0.01) * 100
            else:
                mfe, mae = (entry - min(low_prices)) / max(entry, 0.01) * 100, (entry - max(high_prices)) / max(entry, 0.01) * 100
        else:
            mfe, mae = 0, 0
        
        snapshot = get_snapshot()
        price = snapshot.mids.get(coin, 0) if snapshot else 0
        if price == 0:
            return
        
        if direction == "LONG":
            if price >= tp:
                outcome, pnl = "TP_HIT", (tp - entry) / max(entry, 0.01) * 100
            elif price <= sl:
                outcome, pnl = "SL_HIT", (sl - entry) / max(entry, 0.01) * 100
            else:
                outcome, pnl = "PARTIAL", (price - entry) / max(entry, 0.01) * 100
        else:
            if price <= tp:
                outcome, pnl = "TP_HIT", (entry - tp) / max(entry, 0.01) * 100
            elif price >= sl:
                outcome, pnl = "SL_HIT", (entry - sl) / max(entry, 0.01) * 100
            else:
                outcome, pnl = "PARTIAL", (entry - price) / max(entry, 0.01) * 100
        
        is_win = outcome in ("TP_HIT", "PARTIAL_WIN")
        hypothesis_validated = is_win or (mfe > abs(mae) * 1.5)
        
        update_signal_outcome_v7(signal_id, outcome, pnl, price, mfe, mae, hypothesis_validated)
        add_hypothesis_validation(signal_id, thesis, outcome, pnl, hypothesis_validated)
        
        # Prediction quality (bukan winrate)
        invalidate_hit = (direction == "LONG" and price > entry) or (direction == "SHORT" and price < entry)
        destination_hit = (direction == "LONG" and price >= tp) or (direction == "SHORT" and price <= tp)
        confirmation_hit = mfe > abs(mae) * 1.2
        
        pred_quality = evaluate_prediction_quality(
            signal_id, coin, predicted_direction, direction, entry,
            predicted_zone_low, predicted_zone_high, mfe, mae, hypothesis_validated
        )
        update_prediction_memory(coin, pred_quality)
        
        logger.info(f"Evaluated {signal_id}: {outcome} pnl={pnl:.2f}% pred_quality={pred_quality:.1f}")
        
        if outcome in ("SL_HIT", "PARTIAL") and pnl < 0:
            reset_belief_state(coin, f"loss {outcome}")
        
    except Exception as e:
        logger.error(f"Eval error {signal_id}: {e}")
        
        
# ========== ENGINE LOOPS ==========
def state_engine_update_v7():
    refresh_snapshot()
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
    alerts = []
    
    for coin in top_coins:
        mark = 0.0
        snapshot = get_snapshot()
        if snapshot and coin in snapshot.mids:
            mark = snapshot.mids[coin]
        if mark == 0 or coin not in master_candles:
            continue
        alert = check_entry_alert_v7(coin, mark, master_candles)
        if alert and not PAPER_MODE:
            alerts.append(alert)
        elif alert and PAPER_MODE:
            logger.info(f"[PAPER] {alert['coin']} {alert['direction']} score={alert['score']} belief={alert.get('belief_state', 'SEEKING')}")
        time.sleep(0.05)
    
    for alert in alerts:
        send_alert_v7(alert)

def trigger_engine_update_v7():
    refresh_snapshot()
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

def scheduled_state_engine_v7():
    while RUNTIME.is_running():
        if not RUNTIME.is_alert_enabled():
            time.sleep(60)
            continue
        state_engine_update_v7()
        vol_reg = get_volatility_regime()
        interval = TUNABLE["STATE_ENGINE_INTERVAL"]
        if vol_reg == "HIGH_VOLATILITY":
            interval = max(15, interval // 2)
        elif vol_reg == "LOW_VOLATILITY":
            interval = min(60, interval * 2)
        logger.info(f"State engine cycle done, next in {interval}s")
        time.sleep(interval)

def scheduled_trigger_engine_v7():
    while RUNTIME.is_running():
        trigger_engine_update_v7()
        time.sleep(TUNABLE["TRIGGER_ENGINE_INTERVAL_ACTIVE"])

def scheduled_shadow_evaluation_v7():
    while RUNTIME.is_running():
        now = time.time()
        with _shadow_lock:
            for sid, shadow in list(_shadow_decisions.items()):
                if not shadow["evaluated"] and now - shadow["timestamp"] > TUNABLE["BASE_EVALUATION_DELAY"]:
                    try:
                        coin, entry, sl, tp, direction = shadow["coin"], shadow["entry"], shadow["sl"], shadow["tp"], shadow["direction"]
                        candles = get_candles(coin, "5m", 100)
                        if not candles:
                            continue
                        entry_time = int(shadow["timestamp"])
                        high_prices, low_prices = [], []
                        for c in candles:
                            ts = c.get('t', 0)
                            if ts >= entry_time * 1000:
                                high_prices.append(float(c['h']))
                                low_prices.append(float(c['l']))
                        if high_prices and low_prices:
                            if direction == "LONG":
                                mfe, mae = (max(high_prices) - entry) / max(entry, 0.01) * 100, (min(low_prices) - entry) / max(entry, 0.01) * 100
                            else:
                                mfe, mae = (entry - min(low_prices)) / max(entry, 0.01) * 100, (entry - max(high_prices)) / max(entry, 0.01) * 100
                        else:
                            mfe, mae = 0, 0
                        snapshot = get_snapshot()
                        price = snapshot.mids.get(coin, 0) if snapshot else 0
                        if price == 0:
                            continue
                        if direction == "LONG":
                            if price >= tp:
                                outcome, pnl = "TP_HIT", (tp - entry) / max(entry, 0.01) * 100
                            elif price <= sl:
                                outcome, pnl = "SL_HIT", (sl - entry) / max(entry, 0.01) * 100
                            else:
                                outcome, pnl = "PARTIAL", (price - entry) / max(entry, 0.01) * 100
                        else:
                            if price <= tp:
                                outcome, pnl = "TP_HIT", (entry - tp) / max(entry, 0.01) * 100
                            elif price >= sl:
                                outcome, pnl = "SL_HIT", (entry - sl) / max(entry, 0.01) * 100
                            else:
                                outcome, pnl = "PARTIAL", (entry - price) / max(entry, 0.01) * 100
                        update_shadow_outcome(sid, outcome, pnl, mfe, mae)
                        logger.info(f"Shadow {sid}: {outcome} pnl={pnl:.2f}%")
                    except Exception as e:
                        logger.error(f"Shadow eval error {sid}: {e}")
        time.sleep(3600)

def scheduled_cleanup_v7():
    while RUNTIME.is_running():
        cleanup_active_candidates_v7()
        cleanup_old_shadow_decisions_v7()
        time.sleep(600)

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
    
    
# ========== V7: TELEGRAM BOT ==========
bot = telebot.TeleBot(TOKEN)

def send_alert_v7(alert: dict):
    if not RUNTIME.is_alert_enabled():
        return
    
    coin = alert["coin"]
    now = time.time()
    
    # Progressive cooldown
    def get_progressive_cooldown(c: str) -> int:
        with _alert_history_lock:
            if c not in _alert_history:
                _alert_history[c] = deque(maxlen=5)
            while _alert_history[c] and now - _alert_history[c][0] > TUNABLE["ALERT_HISTORY_WINDOW"]:
                _alert_history[c].popleft()
            cnt = len(_alert_history[c])
        return 300 if cnt == 0 else (600 if cnt == 1 else (900 if cnt == 2 else 1200))
    
    cooldown = get_progressive_cooldown(coin)
    with _last_alert_lock:
        if coin in _last_alert and now - _last_alert[coin] < cooldown:
            return
        _last_alert[coin] = now
    
    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        _alert_history[coin].append(now)
    
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    belief_emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀","invalidated":"❌"}.get(alert.get("belief_state", "seeking"), "❓")
    pressure_emoji = {"low":"🐢","normal":"⚖️","urgent":"⏰"}.get(alert.get("time_pressure", "normal"), "⚖️")
    
    weights = {"A": alert.get("mode_aggressive", 0), "B": alert.get("mode_balanced", 1), "P": alert.get("mode_precision", 0)}
    mode_emoji = "⚡" if weights["A"] > 0.5 else ("🎯" if weights["P"] > 0.5 else "⚖️")
    mode_bar = ("█"*10+"░░░░") if weights["A"] > 0.5 else ("░░░░"+"█"*10) if weights["P"] > 0.5 else ("░░"+"█"*6+"░░")
    
    intent_emoji = {"seek_liquidity":"🦈","trap":"🪤","continue":"➡️","accept":"🟰","distribute":"📤"}.get(alert.get("intent_type", ""), "📍")
    
    size = alert.get("position_size_mult", 1.0)
    size_bar = "█" * int(size * 10) + "░" * int((1 - size) * 10)
    
    fs = alert.get("filter_score", 0)
    filter_ind = "🟢" if fs >= 80 else ("🟡" if fs >= 60 else "🔴")
    
    e_data, e_market, e_decision = alert.get("entropy_data", 0), alert.get("entropy_market", 0), alert.get("entropy_decision", 0)
    ebar_d, ebar_m, ebar_dec = ("█"*int(e_data/10)+"░"*(10-int(e_data/10))), ("█"*int(e_market/10)+"░"*(10-int(e_market/10))), ("█"*int(e_decision/10)+"░"*(10-int(e_decision/10)))
    
    commit = alert.get("commitment_score", 0)
    commit_bar = "█" * int(commit / 10) + "░" * (10 - int(commit / 10))
    
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

🧠 *Belief*: {belief_emoji} {alert.get('belief_state', 'SEEKING').upper()} | ⏱️ Pressure: {pressure_emoji} {alert.get('time_pressure', 'normal').upper()}

📊 *Setup Quality*
├─ Score: {alert['score']} | {alert['label']}
├─ DE: {alert.get('decision_energy', 0):.1f}
├─ Commitment: {commit:.0f}% [{commit_bar}]
├─ Filter: {fs:.0f} {filter_ind}
└─ Trigger: {alert.get('trigger_strength', 0):.0f}%

🎯 *Execution*
├─ Mode: {alert['execution_mode']} [{mode_bar}]
├─ A:{weights['A']:.0%} B:{weights['B']:.0%} P:{weights['P']:.0%}
└─ Size: {size:.1f}x [{size_bar}]

💰 *Levels*
├─ Entry: {fmt_price(alert['entry'])}
├─ SL: {fmt_price(alert['sl'])} ({abs(alert['entry'] - alert['sl']) / max(alert['entry'], 0.01) * 100:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({abs(alert['tp'] - alert['entry']) / max(alert['entry'], 0.01) * 100:.2f}%)
└─ RR: 1:{alert['rr']:.1f}

🌡️ *Entropy*
├─ Data: {e_data}% [{ebar_d}]
├─ Market: {e_market}% [{ebar_m}]
└─ Decision: {e_decision}% [{ebar_dec}]

📈 *Evidence*
├─ Positive: {', '.join(alert.get('positive_evidence', []))}
├─ Negative: {alert.get('negative_evidence', 'none')}
└─ Why Not: {alert.get('why_not', 'no deterrents')}

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

# ========== COMMAND HANDLERS (V7) ==========
@bot.message_handler(commands=['start'])
def cmd_start(m):
    regimes = get_all_regimes()
    text = f"""
🧠 *Smart Entry Engine v7.0*
━━━━━━━━━━━━━━━━━━━━━━
🏗️ 4-Layer Architecture
🔬 Belief ≠ Confidence (Decoupled)
⚡ Atomic Runtime State
🔄 Circuit Breaker + ThreadPool
📊 Snapshot Layer
📝 Decision Trace

📡 Market: {regimes[0]} | {regimes[1]} | {regimes[2]}
⏰ {get_wib()}

✅ /status /entry BTC /warroom BTC /analytics /journal /belief /fatigue /prediction /traces /health
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
    emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}
    for ts, coin, mreg, belief, dirn, fs, de, commit, pressure, mode, intent, why_not in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
        teks += f"{dt} {coin} [{mreg}] {emoji.get(belief,'❓')}{belief.upper()} | {mode}/{intent}\n"
        teks += f"   Score:{fs} | DE:{de:.0f} | Commit:{commit:.0f} | Pressure:{pressure}\n"
        teks += f"   ⚠️ {why_not[:40]}\n\n"
    bot.reply_to(m, teks, parse_mode='Markdown')

@bot.message_handler(commands=['belief'])
def cmd_belief(m):
    with _belief_state_lock:
        if not _belief_state:
            bot.reply_to(m, "Belum ada data belief state.")
            return
        text = "🧠 *BELIEF STATE SUMMARY*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀","invalidated":"❌"}
        for coin, data in sorted(_belief_state.items(), key=lambda x: x[1]["since"]):
            state = data["state"].value
            dur = int(time.time() - data["since"])
            mins, secs = dur // 60, dur % 60
            text += f"{emoji.get(state,'❓')} {coin}: {state.upper()} ({mins}m {secs}s) | score:{data.get('score',0):.0f}\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['fatigue'])
def cmd_fatigue(m):
    with _fatigue_memory_lock:
        if not _fatigue_memory:
            bot.reply_to(m, "Belum ada data fatigue.")
            return
        text = "💪 *FATIGUE STATUS*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for family, deq in _fatigue_memory.items():
            cnt = len(deq)
            if cnt >= TUNABLE["FATIGUE_MAX_PER_HOUR"]:
                bar, pen = "🔴", 0.3
            elif cnt >= 3:
                bar, pen = "🟡", 0.6
            else:
                bar, pen = "🟢", 0.8
            text += f"{bar} {family}: {cnt}x rejections | penalty {pen:.0%}\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['prediction'])
def cmd_prediction(m):
    with _prediction_memory_lock:
        if not _prediction_memory:
            bot.reply_to(m, "Belum ada data prediction quality.")
            return
        text = "📊 *PREDICTION QUALITY*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for coin, data in sorted(_prediction_memory.items(), key=lambda x: x[1]["ema_quality"], reverse=True)[:10]:
            q = data["ema_quality"]
            bar = "█" * int(q / 10) + "░" * (10 - int(q / 10))
            text += f"{coin}: {q:.0f} [{bar}]\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['traces'])
def cmd_traces(m):
    with _trace_lock:
        if not _decision_traces:
            bot.reply_to(m, "Belum ada decision traces.")
            return
        text = "📝 *DECISION TRACES* (10 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for t in list(_decision_traces)[-10:]:
            dt = datetime.fromtimestamp(t.timestamp, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            text += f"{dt} {t.coin} {t.event_type} | {t.belief_state} | {t.final_decision}\n"
            text += f"   DE:{t.decision_energy:.0f} | {t.what_changed}\n"
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
    with _market_sanity_lock:
        sane = "🟢 SANE" if _market_sanity["is_sane"] else "🔴 CHAOS"
    text = f"""
📊 *STATUS V7*
━━━━━━━━━━━━━━━━━━━━━━
⏰ {get_wib()}
📡 Market: {market[0]} | {market[1]} | {market[2]}
🛡️ Sanity: {sane}

📈 *Today*
├─ Alerts: {alerts_sent}
├─ Signals: {today}
└─ Prediction Quality: {pred_q:.2f}x

⚙️ *System*
├─ Alert: {'🟢 ON' if RUNTIME.is_alert_enabled() else '🔴 OFF'}
└─ Paper: {'📄 YES' if PAPER_MODE else '💎 NO'}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['analytics'])
def cmd_analytics(m):
    stats = get_analytics()
    if stats["total"] == 0:
        bot.reply_to(m, "Belum ada sinyal dievaluasi.")
        return
    bar = "█" * int(stats['win_rate'] / 10) + "░" * (10 - int(stats['win_rate'] / 10))
    text = f"📈 *PERFORMANCE*\n━━━━━━━━━━━━━━━━━━━━━━\n├─ Total: {stats['total']}\n├─ Win: {stats['wins']} | Loss: {stats['losses']}\n├─ Win Rate: {stats['win_rate']}% [{bar}]\n├─ Avg RR: {stats['avg_rr']}\n└─ Total PnL: {stats['total_pnl']:+.2f}%"
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['entry'])
def cmd_entry(m):
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Format: /entry BTC")
        return
    coin = parts[1].upper()
    try:
        snapshot = get_snapshot()
        mark = snapshot.mids.get(coin, 0) if snapshot else 0
        if mark == 0:
            bot.reply_to(m, f"❌ {coin} not found")
            return
        master = {coin: get_candles(coin, "1h", 100)}
        alert = check_entry_alert_v7(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No setup for {coin}")
            return
        w = {"A": alert.get("mode_aggressive",0), "B": alert.get("mode_balanced",1), "P": alert.get("mode_precision",0)}
        be = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}.get(alert.get("belief_state","seeking"),"❓")
        pe = {"low":"🐢","normal":"⚖️","urgent":"⏰"}.get(alert.get("time_pressure","normal"),"⚖️")
        text = f"""
🎯 *Entry {coin}*
━━━━━━━━━━━━━━━━━━━━━━
🧠 Belief: {be} {alert.get('belief_state','SEEKING').upper()} | ⏱️ Pressure: {pe} {alert.get('time_pressure','normal').upper()}

📡 {alert['direction']} | {alert['label']} ({alert['score']})
├─ Mode: {alert['execution_mode']} (A:{w['A']:.0%} B:{w['B']:.0%} P:{w['P']:.0%})
├─ Intent: {alert.get('intent_type','unknown')}
├─ DE: {alert.get('decision_energy',0):.1f}
└─ Commitment: {alert.get('commitment_score',0):.0f}%

💰 *Levels*
├─ Entry: {fmt_price(alert['entry'])}
├─ SL: {fmt_price(alert['sl'])} ({abs(alert['entry']-alert['sl'])/max(alert['entry'],0.01)*100:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({abs(alert['tp']-alert['entry'])/max(alert['entry'],0.01)*100:.2f}%)
└─ RR: 1:{alert['rr']:.1f}

📊 *Quality*
├─ Filter: {alert.get('filter_score',0):.0f}
├─ Size: {alert.get('position_size_mult',1.0):.2f}x
├─ Trigger: {alert.get('trigger_strength',0):.0f}%
└─ Fatigue: {alert.get('fatigue_penalty',1.0):.0%}

🌡️ *Entropy* Data:{alert.get('entropy_data',0)}% Market:{alert.get('entropy_market',0)}% Decision:{alert.get('entropy_decision',0)}%

{alert.get('explanation','')}
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
        snapshot = get_snapshot()
        mark = snapshot.mids.get(coin, 0) if snapshot else 0
        if mark == 0:
            bot.reply_to(m, f"❌ {coin} not found")
            return
        master = {coin: get_candles(coin, "1h", 100)}
        alert = check_entry_alert_v7(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No signal for {coin}")
            return
        delta, cvd, oi = get_ob_delta(coin), get_cvd(coin, 30), snapshot.oi.get(coin, 0) if snapshot else 0
        funding, momentum = snapshot.funding.get(coin, 0) if snapshot else 0, get_composite_momentum(coin, master)
        structure_l, structure_s = get_structure_valid_separate(coin, master)
        exhaustion, entropy = compute_exhaustion_score(coin, master), compute_market_entropy_v7(coin, master)
        dq = get_data_confidence(coin, time.time())[0]
        candles_1h = get_candles(coin, "1h", 60, master)
        state = get_market_state_from_structure(candles_1h, mark).name if candles_1h else "UNKNOWN"
        hyp, market = alert.get('hypothesis', {}), get_all_regimes()
        text = f"""
🧠 *WARROOM {coin} V7*
━━━━━━━━━━━━━━━━━━━━━━
📡 Market: {market[0]} | {market[1]} | {market[2]}
├─ State: {state}
├─ Intent: {alert.get('intent_type','unknown')}
├─ Belief: {alert.get('belief_state','SEEKING')}
└─ Pressure: {alert.get('time_pressure','normal')}

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
├─ Size: {alert.get('position_size_mult',1.0):.2f}x
├─ Filter: {alert.get('filter_score',0):.0f}
├─ Commitment: {alert.get('commitment_score',0):.0f}%
└─ Fatigue: {alert.get('fatigue_penalty',1.0):.0%}

📌 *Hypothesis*
├─ Thesis: {hyp.get('thesis','N/A')[:60]}
├─ Invalidate: {hyp.get('invalidate','N/A')}
└─ Observe: {hyp.get('observe','N/A')}
"""
        bot.reply_to(m, text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['stopalert'])
def cmd_stopalert(m):
    if m.from_user.id != USER_ID:
        return
    if RUNTIME.is_alert_enabled():
        RUNTIME.disable_alerts()
        bot.reply_to(m, "🔴 Alert OFF")
    else:
        RUNTIME.enable_alerts()
        bot.reply_to(m, "🟢 Alert ON")

@bot.message_handler(commands=['health'])
def cmd_health(m):
    if m.from_user.id != USER_ID:
        return
    mem = f"{psutil.Process().memory_percent():.1f}%" if HAS_PSUTIL else "N/A"
    cpu = f"{psutil.cpu_percent():.1f}%" if HAS_PSUTIL else "N/A"
    with _candle_lock: cache_sz = len(_candle_cache)
    with _active_candidates_lock: active_sz = len(_active_candidates)
    with _pending_setups_lock: pending_sz = len(_pending_setups)
    with _market_sanity_lock: sane = "🟢" if _market_sanity["is_sane"] else "🔴"
    with _hypothesis_lock: hyp_sz = len(_hypothesis_store)
    with _trace_lock: trace_sz = len(_decision_traces)
    text = f"🩺 *HEALTH V7*\n━━━━━━━━━━━━━━━━━━━━━━\n🖥️ CPU: {cpu} | RAM: {mem}\n🧵 Threads: {threading.active_count()}\n\n📦 Cache: {cache_sz} | Hypotheses: {hyp_sz}\n🎯 Active: {active_sz} | Pending: {pending_sz}\n📝 Traces: {trace_sz}\n\n🛡️ Sanity: {sane} | {_market_sanity.get('reason','N/A')}\n✅ Running: {RUNTIME.is_running()}"
    bot.reply_to(m, text, parse_mode='Markdown')
    
    
# ========== MONITOR PENDING SETUPS ==========
def monitor_pending_setups_v6():
    """Monitor pending setups dengan trigger probability"""
    while RUNTIME.is_running():
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
                        save_signal_v7(signal_id, setup.coin, thesis.direction, 85,
                                      current_price, setup.sl_price, setup.tp_price, setup.rr,
                                      f"Thesis triggered: {trigger_reason} (strength={trigger_strength:.0f}) | {thesis.statement}",
                                      data_conf, thesis.statement, thesis.invalidation, thesis.confirmation,
                                      "BALANCED", "", 0.0, 1.0, 100.0, 0.0, "SEEKING", 0.0, "normal", 50.0)
                        _EVAL_EXECUTOR.submit(evaluate_signal_v7, signal_id, setup.coin, thesis.direction,
                                              current_price, setup.sl_price, setup.tp_price, data_conf,
                                              0, 0, 0, thesis.statement, thesis.invalidation, thesis.confirmation,
                                              get_evaluation_delay(_atr_pct, setup.rr, "NORMAL"),
                                              0, 0, thesis.direction)
                    
                    alert = {
                        "coin": setup.coin, "direction": thesis.direction, "score": 85,
                        "entry": current_price, "sl": setup.sl_price, "tp": setup.tp_price,
                        "rr": setup.rr, "reason": f"Thesis triggered: {trigger_reason}",
                        "area": setup.event_type, "label": get_confidence_label(85),
                        "contradiction": False, "exhaustion": 0, "entropy_market": 0,
                        "evidence_families": 0, "positive_evidence": ["thesis_trigger"],
                        "negative_evidence": "none", "data_confidence": data_conf,
                        "contributions": {}, "execution_mode": "BALANCED",
                        "intent_type": "", "decision_energy": 0.0, "position_size_mult": 1.0,
                        "filter_score": 100.0, "why_not": "no deterrents",
                        "trigger_strength": trigger_strength, "belief_state": "SEEKING",
                        "commitment_score": 0.0, "time_pressure": "normal",
                        "mode_aggressive": 0.0, "mode_balanced": 1.0, "mode_precision": 0.0,
                        "hypothesis": {"thesis": thesis.statement, "invalidate": thesis.invalidation,
                                       "observe": thesis.confirmation, "destination": thesis.destination,
                                       "timeframe": thesis.timeframe},
                        "explanation": f"⚡ Thesis triggered: {trigger_reason}\n📋 {thesis.statement}"
                    }
                    send_alert_v7(alert)
                    
                    with _pending_setups_lock:
                        _pending_setups.pop(setup_id, None)
                    time.sleep(0.5)
            
            time.sleep(3)
        except Exception as e:
            logger.error(f"monitor_pending_setups error: {e}")
            time.sleep(5)

def compute_trigger_strength_v6(setup: PendingSetup, current_price: float, 
                                 delta: float, candles_5m: List[dict]) -> Tuple[float, str]:
    thesis = setup.thesis
    exp = thesis.expected_trigger.lower()
    d = thesis.direction
    
    strengths, reasons = [], []
    
    if "reclaim" in exp and "above" in exp:
        if d == "LONG" and current_price > setup.entry_price:
            reclaim_dist = (current_price - setup.entry_price) / max(setup.entry_price, 0.01) * 100
            strengths.append(min(40, reclaim_dist * 80))
            reasons.append(f"reclaim {reclaim_dist:.2f}%")
    
    if "rejection" in exp and "below" in exp:
        if d == "SHORT" and current_price < setup.entry_price:
            reclaim_dist = (setup.entry_price - current_price) / max(setup.entry_price, 0.01) * 100
            strengths.append(min(40, reclaim_dist * 80))
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
            strengths.append(min(30, delta * 6))
            reasons.append(f"delta +{delta:.1f}")
        elif d == "SHORT" and delta < 0:
            strengths.append(min(30, abs(delta) * 6))
            reasons.append(f"delta {delta:.1f}")
    
    if not strengths:
        return 0.0, "no trigger"
    return min(100.0, sum(strengths)), " + ".join(reasons[:2])

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

# ========== MAIN ==========
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--paper', action='store_true')
    return p.parse_args()

def signal_handler(sig, frame):
    logger.info(f"Shutdown signal {sig} received, exiting...")
    RUNTIME.signal_shutdown()
    sys.exit(0)

if __name__ == "__main__":
    args = parse_args()
    PAPER_MODE = args.paper
    logger.info(f"Starting Smart Entry Engine V7.0 in {'PAPER' if PAPER_MODE else 'LIVE'} mode")
    init_db()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start threads
    threads = [
        threading.Thread(target=scheduled_state_engine_v7, daemon=True),
        threading.Thread(target=scheduled_trigger_engine_v7, daemon=True),
        threading.Thread(target=scheduled_shadow_evaluation_v7, daemon=True),
        threading.Thread(target=scheduled_cleanup_v7, daemon=True),
        threading.Thread(target=monitor_pending_setups_v6, daemon=True),
    ]
    for t in threads:
        t.start()
    
    # Bot polling
    while RUNTIME.is_running():
        try:
            logger.info("Starting bot polling...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            if not RUNTIME.is_running():
                break
            logger.error(f"Bot polling error: {e}, restarting in 5s...")
            time.sleep(5)