#!/usr/bin/env python3
# ============================================================
# SMART ENTRY ENGINE V10 – REACTION ENGINE (FIXED)
# ============================================================
# Filosofi: Baca MARKET REACTION, bukan baca berita
# DNA: Market Interpretation → Market Anticipation
# Owner: Cryptone 
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
import json
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
    # V10 tambahan
    "SHOCK_AGGRESSIVE_THRESHOLD": 80,
    "TRANSITION_AGGRESSIVE_THRESHOLD": 75,
    "TENSION_SIZE_BOOST": 1.2,
    "EVENT_RISK_DECAY_HOURS": 3,
    "BREATH_WEAK_THRESHOLD": 0.3,
    "ZONE_DECAY_DAYS": 1,
    "ZONE_STRENGTH_DECAY_PER_TOUCH": 0.15,
    "FVG_FILL_SPEED_FAST_MINUTES": 10,
    "FVG_FILL_SLOW_MINUTES": 30,
    "MAX_DB_RETRIES": 5,
    "CONTEXT_STALE_THRESHOLD": 5,
    "REGIME_INERTIA_WINDOW": 300,
    "ALERT_VALUE_MIN": 30,
    "ALERT_VALUE_HIGH": 70,
    "ALERT_VALUE_MEDIUM": 50,
    "INTENT_MEMORY_MAX": 20,
    "INTENT_MEMORY_HOURS": 24,
    "REACTION_HISTORY_MAX": 50,
    "EVENT_IMPORTANCE_HIGH": 70,
    "EVENT_IMPORTANCE_MEDIUM": 40,
    "EVENT_IMPORTANCE_LOW": 20,
    "UNCLEAR_THRESOLD": 55,
    "UNCLEAR_DIFF": 25,
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

# ========== V10.1 CACHE MANAGER ==========
class CacheManager:
    """Centralized cache dengan TTL dan lock per key"""
    def __init__(self):
        self._data: Dict[str, Tuple[Any, float]] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()
    
    def get(self, key: str, max_age: float = None) -> Optional[Any]:
        with self._get_lock(key):
            if key not in self._data:
                return None
            value, ts = self._data[key]
            if max_age and time.time() - ts > max_age:
                return None
            return value
    
    def set(self, key: str, value: Any):
        with self._get_lock(key):
            self._data[key] = (value, time.time())
    
    def invalidate(self, key: str):
        with self._get_lock(key):
            self._data.pop(key, None)
    
    def clear(self):
        with self._global_lock:
            self._data.clear()
    
    def _get_lock(self, key: str) -> threading.RLock:
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.RLock()
            return self._locks[key]
    
    def size(self) -> int:
        with self._global_lock:
            return len(self._data)
    
    def keys(self) -> List[str]:
        with self._global_lock:
            return list(self._data.keys())

# GLOBAL CACHE
CACHE = CacheManager()

# ========== GLOBAL API COOLDOWN ==========
_api_cooldown_until: float = 0.0
_api_cooldown_lock = threading.RLock()

def can_call_api() -> bool:
    """Cek apakah API boleh dipanggil"""
    with _api_cooldown_lock:
        return time.time() >= _api_cooldown_until

def trigger_api_cooldown(seconds: int = 30):
    """Trigger cooldown ketika kena 429"""
    global _api_cooldown_until
    with _api_cooldown_lock:
        _api_cooldown_until = time.time() + seconds
        logger.warning(f"🔴 API cooldown triggered for {seconds}s")

def api_call_wrapper(func: Callable, *args, **kwargs):
    """Wrapper untuk semua API calls dengan cooldown"""
    if not can_call_api():
        logger.debug("⏳ API on cooldown, skipping request")
        return None
    
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            trigger_api_cooldown(30)
            logger.error(f"🚫 Rate limit hit on {func.__name__}, cooldown activated")
        raise
    
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

class BotHealthState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERY = "recovery"
    FAILED = "failed"
    
class ExecutionMode(Enum):
    NORMAL = "normal"
    PREPARE = "prepare"
    CAUTIOUS = "cautious"
    AGGRESSIVE = "aggressive"
    DEFENSIVE = "defensive"
    DISCOVERY = "discovery"   
    OBSERVE = "observe"      
    
# ========== V10: DATACLASSES ==========
@dataclass
class ContextSnapshot:
    timestamp: float
    shock_score: float
    transition_prob: float
    tension: float
    vol_forecast: float
    breath_bull: float
    breath_bear: float
    event_risk: float
    dominance: float
    regime: str

@dataclass
class EventRisk:
    importance: int          
    expected_vol: int        
    scope: str               
    bias: str                
    label: str
    ts: float

@dataclass
class MarketReaction:
    event: str
    expected_vol: float
    expected_direction: str
    actual_vol: float
    actual_direction: str
    actual_move: float
    absorption: float
    confidence: float
    timestamp: float

@dataclass
class IntentMemory:
    intent: str
    outcome: str
    pnl: float
    ts: float

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
    fill_ratio: float = 0.0
    fill_time_minutes: float = 0.0

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
    context_age: float = 0.0
    execution_mode: str = "NORMAL"

@dataclass
class DecisionJournalEntry:
    timestamp: float
    coin: str
    event_type: str
    direction: str
    score: int
    mode: str
    executed: bool
    shadow: bool
    entry: float
    sl: float
    tp: float
    rr: float
    intent: str
    belief: str
    decision_energy: float
    hidden_liquidity: int
    micro_acceptance: Optional[float]
    failed_risk: float
    intent_drift: float
    surprise: float
    narrative: Dict[str, str]
    outcome: Optional[str] = None
    pnl: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None

@dataclass
class FailedMoveFingerprint:
    coin: str
    event_type: str
    delta_bucket: str
    vol_bucket: str
    clarity: str
    intent: str
    direction: str
    price: float
    timestamp: float
    reason: str

# ============================================================
# PHASE 1 — INTERPRETATION ENGINE DATACLASSES
# ============================================================

@dataclass
class RegimeInterpretation:
    regime: str
    strength: float
    stability: float
    confidence: float
    age_minutes: float
    transition_prob: float
    transition_direction: str
    is_breaking: bool
    breaking_strength: float

    def summary(self) -> str:
        return (f"{self.regime} (str:{self.strength:.0f}%, stab:{self.stability:.0f}%, "
                f"conf:{self.confidence:.0f}%, age:{self.age_minutes:.0f}m, "
                f"trans:{self.transition_prob:.0f}% {self.transition_direction})")

@dataclass
class OBReaction:
    touch_count: int
    first_touch_time: float
    last_touch_time: float
    max_reaction_strength: float
    avg_reaction: float
    followthrough: float
    confidence: float

    def is_strong(self) -> bool:
        return self.max_reaction_strength > 60 and self.followthrough > 50

@dataclass
class FVGQuality:
    size: float
    fill_ratio: float
    fill_speed: float
    reaction: float
    age_minutes: float
    quality_score: float

    def summary(self) -> str:
        return (f"size:{self.size:.0f}%, fill:{self.fill_ratio:.0%}, "
                f"speed:{self.fill_speed:.0f}%, react:{self.reaction:.0f}%, "
                f"age:{self.age_minutes:.0f}m, Q:{self.quality_score:.0f}")

@dataclass
class ContextMemory:
    snapshots: List[ContextSnapshot] = field(default_factory=list)
    max_size: int = 5

    def add(self, ctx: ContextSnapshot):
        self.snapshots.append(ctx)
        if len(self.snapshots) > self.max_size:
            self.snapshots.pop(0)

    def get_trend(self, key: str) -> str:
        if len(self.snapshots) < 2:
            return "STABLE"
        values = [getattr(s, key, 0) for s in self.snapshots]
        if values[-1] > values[0] * 1.05:
            return "RISING"
        elif values[-1] < values[0] * 0.95:
            return "FALLING"
        return "STABLE"

    def get_volatility_trend(self) -> str:
        return self.get_trend("vol_forecast")

    def get_regime_sequence(self) -> List[str]:
        return [s.regime for s in self.snapshots]

    def is_transitioning(self) -> bool:
        if len(self.snapshots) < 3:
            return False
        regimes = self.get_regime_sequence()
        changes = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i-1])
        return changes >= 2

@dataclass
class CalibratedConfidence:
    raw: float
    calibrated: float
    calibration_factor: float
    sample_size: int
    last_update: float
    

    def to_dict(self):
        return asdict(self)
        
# ========== V10: RUNTIME STATE ==========
@dataclass
class RuntimeState:
    alert_enabled: bool = True
    _shutdown: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

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
            return not self._shutdown

    def signal_shutdown(self):
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                self._event.set()

    def wait(self, timeout: float = None) -> bool:
        return self._event.wait(timeout)

RUNTIME = RuntimeState()
_context_memory = ContextMemory()

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

# V10: context snapshot
_last_context: Optional[ContextSnapshot] = None
_context_lock = threading.RLock()
_CONTEXT_TTL = 10

# V10: snapshot state
_SNAPSHOT_TTL: int = 5
_last_snapshot: Optional[MarketSnapshot] = None
_snapshot_lock = threading.RLock()

# V10: belief history per coin (for drift tracking)
_belief_history: Dict[str, deque] = {}
_belief_history_lock = threading.RLock()

# V10: bot health state
_bot_health: Dict[str, Any] = {
    "state": BotHealthState.HEALTHY,
    "failures": 0,
    "last_failure": 0.0,
    "reason": ""
}
_bot_health_lock = threading.RLock()

# V10: DB write queue (async DB writes)
from queue import Queue as _Queue
_db_queue: "_Queue[tuple]" = _Queue(maxsize=2000)
MAX_DB_RETRIES = TUNABLE["MAX_DB_RETRIES"]

# V10: confidence-weighted context cache
_CONTEXT_CACHE: Dict[str, Any] = {}
_CONTEXT_CACHE_LOCK = threading.RLock()

# V10: regime history (for inertia)
_regime_history: deque = deque(maxlen=20)
_regime_history_lock = threading.RLock()

# V10: event risk data (V10: list of EventRisk objects)
_EVENT_RISK_DATA: List[EventRisk] = []
_event_risk_lock = threading.RLock()

# V10: reaction history (for Reaction Engine)
_reaction_history: deque = deque(maxlen=TUNABLE["REACTION_HISTORY_MAX"])
_reaction_lock = threading.RLock()

# V10: intent memory (per coin)
_intent_memory: Dict[str, deque] = {}
_intent_memory_lock = threading.RLock()

# V10: market breath cache (V10: advanced metrics)
_breath_cache: Dict[str, Any] = {}
_breath_lock = threading.RLock()

# V10: circuit breaker & executors
_circuit_breaker_state = {"failures": 0, "last_failure": 0, "state": "CLOSED"}
_circuit_breaker_lock = threading.RLock()

_EVAL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eval_")
_SHADOW_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shadow_")

_decision_journal: List[DecisionJournalEntry] = []
_journal_lock = threading.RLock()
_review_counter = 0
_review_lock = threading.RLock()
_AUTO_REVIEW_INTERVAL = 50
_failed_memory: Dict[str, List[FailedMoveFingerprint]] = {}
_failed_lock = threading.RLock()
_smoothed_drift: Dict[str, float] = {}
_smoothed_drift_lock = threading.RLock()
_intent_timeline: Dict[str, deque] = {}
_intent_timeline_lock = threading.RLock()
_intent_vector_history: Dict[str, deque] = {}
_intent_vector_lock = threading.RLock()

# Hyperliquid API
info = Info(constants.MAINNET_API_URL)

# Optional psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    
# ========== DATABASE ==========
# ============================================================
# PART 10 – DATABASE + JOURNAL (FULL V10)
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
def init_db():
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()

        # ========== CREATE TABLES ==========
        
        # 1. signals
        c.execute('''CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT UNIQUE,
            coin TEXT,
            direction TEXT,
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

        # 2. journal
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
            missed_opportunity_pnl REAL,
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

        # 3. counterfactual
        c.execute('''CREATE TABLE IF NOT EXISTS counterfactual (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            coin TEXT,
            original_score INTEGER,
            modified_module TEXT,
            modified_score INTEGER,
            reason TEXT
        )''')

        # 4. shadow_decisions
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

        # 5. hypothesis_validation
        c.execute('''CREATE TABLE IF NOT EXISTS hypothesis_validation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT,
            thesis TEXT,
            outcome TEXT,
            pnl REAL,
            validated INTEGER
        )''')

        # 6. prediction_quality
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

        # 7. belief_state_log
        c.execute('''CREATE TABLE IF NOT EXISTS belief_state_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            coin TEXT,
            state TEXT,
            duration_seconds REAL,
            trigger TEXT
        )''')

        # 8. decision_traces
        c.execute('''CREATE TABLE IF NOT EXISTS decision_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            coin TEXT,
            event_type TEXT,
            belief_state TEXT,
            confidence REAL,
            decision_energy REAL,
            final_decision TEXT,
            reasons TEXT,
            why_not TEXT,
            what_changed TEXT,
            context_age REAL,
            execution_mode TEXT
        )''')

        # 9. context_log
        c.execute('''CREATE TABLE IF NOT EXISTS context_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            shock_score REAL,
            transition_prob REAL,
            tension REAL,
            vol_forecast REAL,
            breath_bull REAL,
            breath_bear REAL,
            event_risk REAL,
            dominance REAL,
            regime TEXT
        )''')

        # 10. intent_memory
        c.execute('''CREATE TABLE IF NOT EXISTS intent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            coin TEXT,
            intent TEXT,
            outcome TEXT,
            pnl REAL
        )''')

        # 11. reaction_log
        c.execute('''CREATE TABLE IF NOT EXISTS reaction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            event TEXT,
            expected_vol REAL,
            expected_direction TEXT,
            actual_vol REAL,
            actual_direction TEXT,
            actual_move REAL,
            absorption REAL,
            confidence REAL
        )''')

        conn.commit()

        # ========== MIGRASI OTOMATIS ==========
        MIGRATIONS = [
            ("journal", "execution_mode", "TEXT", "''"),
            ("journal", "intent_type", "TEXT", "''"),
            ("journal", "why_not", "TEXT", "''"),
            ("journal", "wait_value", "REAL", "0.0"),
            ("journal", "trigger_strength", "REAL", "0.0"),
            ("journal", "rejection_strength", "REAL", "0.0"),
            ("journal", "acceptance_strength", "REAL", "0.0"),
            ("journal", "persistence_strength", "REAL", "0.0"),
            ("journal", "filter_score", "REAL", "100.0"),
            ("journal", "position_size_mult", "REAL", "1.0"),
            ("journal", "decision_acceleration", "REAL", "0.0"),
            ("journal", "mode_aggressive", "REAL", "0.0"),
            ("journal", "mode_balanced", "REAL", "1.0"),
            ("journal", "mode_precision", "REAL", "0.0"),
            ("journal", "confidence_breakdown", "TEXT", "''"),
            ("journal", "belief_state", "TEXT", "'SEEKING'"),
            ("journal", "decision_energy", "REAL", "0.0"),
            ("journal", "commitment_score", "REAL", "0.0"),
            ("journal", "time_pressure", "TEXT", "'normal'"),
            ("signals", "execution_mode", "TEXT", "'BALANCED'"),
            ("signals", "intent_type", "TEXT", "''"),
            ("signals", "decision_energy", "REAL", "0.0"),
            ("signals", "position_size_mult", "REAL", "1.0"),
            ("signals", "filter_score", "REAL", "100.0"),
            ("signals", "intent_confidence", "REAL", "0.0"),
            ("signals", "belief_state", "TEXT", "'SEEKING'"),
            ("signals", "commitment_score", "REAL", "0.0"),
            ("signals", "time_pressure", "TEXT", "'normal'"),
            ("signals", "prediction_quality", "REAL", "50.0"),
            ("decision_traces", "context_age", "REAL", "0.0"),
            ("decision_traces", "execution_mode", "TEXT", "'NORMAL'"),
        ]

        for table, col, col_type, default in MIGRATIONS:
            try:
                c.execute(f"PRAGMA table_info({table})")
                existing_cols = [row[1] for row in c.fetchall()]
                if col not in existing_cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} DEFAULT {default}")
                    logger.info(f"✅ Migrated {table}: added column {col}")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    pass
                else:
                    logger.warning(f"Migration failed for {table}.{col}: {e}")
            except Exception as e:
                logger.warning(f"Migration error for {table}.{col}: {e}")

        conn.commit()
        logger.info("✅ Database ready (V10)")

    except Exception as e:
        logger.error(f"init_db error: {e}")
        raise
    finally:
        if conn:
            conn.close()


# ========== SAVE FUNCTIONS ==========

def save_trace_to_db(trace: DecisionTrace):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO decision_traces 
                     (timestamp, coin, event_type, belief_state, confidence, decision_energy,
                      final_decision, reasons, why_not, what_changed, context_age, execution_mode)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (int(trace.timestamp), trace.coin, trace.event_type, trace.belief_state,
                   trace.confidence, trace.decision_energy, trace.final_decision,
                   ", ".join(trace.reasons), ", ".join(trace.why_not), trace.what_changed,
                   trace.context_age, trace.execution_mode))
        conn.commit()
    except Exception as e:
        logger.error(f"save_trace_to_db error: {e}")
    finally:
        if conn:
            conn.close()


def log_context(ctx: ContextSnapshot):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO context_log
                     (timestamp, shock_score, transition_prob, tension,
                      vol_forecast, breath_bull, breath_bear, event_risk, dominance, regime)
                     VALUES (?,?,?,?,?,?,?,?,?,?)''',
                  (int(ctx.timestamp), ctx.shock_score, ctx.transition_prob, ctx.tension,
                   ctx.vol_forecast, ctx.breath_bull, ctx.breath_bear,
                   ctx.event_risk, ctx.dominance, ctx.regime))
        conn.commit()
    except Exception as e:
        logger.error(f"log_context error: {e}")
    finally:
        if conn:
            conn.close()


def log_reaction(reaction: MarketReaction):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO reaction_log
                     (timestamp, event, expected_vol, expected_direction,
                      actual_vol, actual_direction, actual_move, absorption, confidence)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
                  (int(reaction.timestamp), reaction.event, reaction.expected_vol,
                   reaction.expected_direction, reaction.actual_vol, reaction.actual_direction,
                   reaction.actual_move, reaction.absorption, reaction.confidence))
        conn.commit()
    except Exception as e:
        logger.error(f"log_reaction error: {e}")
    finally:
        if conn:
            conn.close()

# ========== DB WRAPPER FUNCTIONS ==========

def save_signal_v7(signal_id, coin, direction, score, entry, sl, tp, rr, reason, data_confidence,
                   hypothesis_thesis="", hypothesis_invalidate="", hypothesis_observe="",
                   execution_mode="BALANCED", intent_type="", decision_energy=0.0,
                   position_size_mult=1.0, filter_score=100.0, intent_confidence=0.0,
                   belief_state="SEEKING", commitment_score=0.0, time_pressure="normal",
                   prediction_quality=50.0):
    conn = None
    try:
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
    except Exception as e:
        logger.error(f"save_signal_v7 error: {e}")
    finally:
        if conn:
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
    conn = None
    try:
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
    except Exception as e:
        logger.error(f"add_journal_entry_v7 error: {e}")
    finally:
        if conn:
            conn.close()

def add_prediction_quality_log(coin, signal_id, predicted_direction, actual_direction,
                                entry_zone_accuracy, timing_quality, thesis_validated, quality_score):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO prediction_quality 
                     (timestamp, coin, signal_id, predicted_direction, actual_direction,
                      entry_zone_accuracy, timing_quality, thesis_validated, quality_score)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
                  (int(time.time()), coin, signal_id, predicted_direction, actual_direction,
                   entry_zone_accuracy, timing_quality, 1 if thesis_validated else 0, quality_score))
        conn.commit()
    except Exception as e:
        logger.error(f"add_prediction_quality_log error: {e}")
    finally:
        if conn:
            conn.close()


def add_belief_state_log(coin, state, duration_seconds, trigger):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO belief_state_log (timestamp, coin, state, duration_seconds, trigger)
                     VALUES (?,?,?,?,?)''',
                  (int(time.time()), coin, state, duration_seconds, trigger))
        conn.commit()
    except Exception as e:
        logger.error(f"add_belief_state_log error: {e}")
    finally:
        if conn:
            conn.close()


def add_shadow_decision(signal_id, coin, direction, entry, sl, tp):
    with _shadow_lock:
        _shadow_decisions[signal_id] = {
            "coin": coin, "direction": direction, "entry": entry, "sl": sl, "tp": tp,
            "timestamp": time.time(), "evaluated": False, "outcome": None, "pnl": 0.0,
            "mfe": 0.0, "mae": 0.0
        }
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO shadow_decisions (signal_id, coin, direction, entry_price, sl_price, tp_price, timestamp)
                     VALUES (?,?,?,?,?,?,?)''',
                  (signal_id, coin, direction, entry, sl, tp, int(time.time())))
        conn.commit()
    except Exception as e:
        logger.error(f"add_shadow_decision error: {e}")
    finally:
        if conn:
            conn.close()


def update_shadow_outcome(signal_id, outcome, pnl, mfe, mae):
    with _shadow_lock:
        if signal_id in _shadow_decisions:
            _shadow_decisions[signal_id]["evaluated"] = True
            _shadow_decisions[signal_id]["outcome"] = outcome
            _shadow_decisions[signal_id]["pnl"] = pnl
            _shadow_decisions[signal_id]["mfe"] = mfe
            _shadow_decisions[signal_id]["mae"] = mae
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''UPDATE shadow_decisions SET evaluated=1, outcome=?, pnl=?, mfe=?, mae=? WHERE signal_id=?''',
                  (outcome, pnl, mfe, mae, signal_id))
        conn.commit()
    except Exception as e:
        logger.error(f"update_shadow_outcome error: {e}")
    finally:
        if conn:
            conn.close()

def add_hypothesis_validation(signal_id, thesis, outcome, pnl, validated):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO hypothesis_validation (signal_id, thesis, outcome, pnl, validated)
                     VALUES (?,?,?,?,?)''',
                  (signal_id, thesis, outcome, pnl, 1 if validated else 0))
        conn.commit()
    except Exception as e:
        logger.error(f"add_hypothesis_validation error: {e}")
    finally:
        if conn:
            conn.close()


def update_signal_outcome_v7(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None):
    conn = None
    try:
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
    except Exception as e:
        logger.error(f"update_signal_outcome_v7 error: {e}")
    finally:
        if conn:
            conn.close()


def get_analytics() -> dict:
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT COUNT(*), SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END),
                           AVG(rr), SUM(pnl) FROM signals WHERE evaluated=1''')
        result = c.fetchone()
        
        total = result[0] or 0
        wins = result[1] or 0
        avg_rr = result[2] or 0
        total_pnl = result[3] or 0
        win_rate = (wins / total * 100) if total > 0 else 0
        
        return {
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(win_rate, 1),
            "avg_rr": round(avg_rr, 2),
            "total_pnl": round(total_pnl, 2)
        }
    except Exception as e:
        logger.error(f"get_analytics error: {e}")
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_rr": 0, "total_pnl": 0}
    finally:
        if conn:
            conn.close()
        
# ========== HELPERS ==========
def fmt_price(p):
    return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"

def get_wib():
    return datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")

def get_wib_hour():
    return datetime.now(timezone(timedelta(hours=7))).hour

def generate_signal_id(coin, direction):
    return f"{coin}_{direction}_{int(time.time())}"

# ========== V10: INTELLIGENCE METRICS ==========
def compute_transition_accuracy() -> float:
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT transition_prob, regime FROM context_log
                     ORDER BY timestamp DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
        if len(rows) < 10:
            return 50.0
        correct = 0
        for i in range(len(rows) - 1):
            if rows[i][0] > 70:
                if rows[i][1] != rows[i + 1][1]:
                    correct += 1
            elif rows[i][0] < 30:
                if rows[i][1] == rows[i + 1][1]:
                    correct += 1
        return (correct / max(1, len(rows) - 1)) * 100
    except:
        return 50.0

def compute_shock_precision() -> float:
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT shock_score, vol_forecast FROM context_log
                     ORDER BY timestamp DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
        if len(rows) < 10:
            return 50.0
        correct = 0
        for i in range(len(rows) - 1):
            if rows[i][0] > 80:
                if rows[i + 1][1] > rows[i][1] * 1.2:
                    correct += 1
            elif rows[i][0] < 30:
                if rows[i + 1][1] < rows[i][1] * 1.2:
                    correct += 1
        return (correct / max(1, len(rows) - 1)) * 100
    except:
        return 50.0

def compute_preparation_recall() -> float:
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT decision_energy, timestamp FROM journal
                     WHERE executed=1 ORDER BY timestamp DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
        if len(rows) < 10:
            return 50.0
        ready = sum(1 for r in rows if (r[0] or 0) > 70)
        return (ready / len(rows)) * 100
    except:
        return 50.0

def compute_decision_consistency() -> float:
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT decision_energy, pnl FROM signals
                     WHERE evaluated=1 AND pnl IS NOT NULL
                     ORDER BY timestamp DESC LIMIT 100''')
        rows = c.fetchall()
        conn.close()
        if len(rows) < 10:
            return 50.0
        de_high = [r for r in rows if (r[0] or 0) > 70]
        if not de_high:
            return 50.0
        win_high = sum(1 for r in de_high if r[1] > 0)
        return (win_high / len(de_high)) * 100
    except:
        return 50.0

def get_intelligence_metrics() -> dict:
    return {
        "transition_accuracy": compute_transition_accuracy(),
        "shock_precision": compute_shock_precision(),
        "preparation_recall": compute_preparation_recall(),
        "decision_consistency": compute_decision_consistency(),
        "belief_stability": _compute_belief_drift(),
        "execution_precision": get_analytics()["win_rate"],
    }

# ========== RETRY WITH BACKOFF ==========
def retry_with_backoff(func: Callable, max_retries: int = 3, base_delay: float = 15, *args, **kwargs):
    """Retry dengan exponential backoff"""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                delay = base_delay * (2 ** attempt) + random.uniform(0, 5)
                logger.warning(f"🔄 Retry {attempt+1}/{max_retries} in {delay:.1f}s: {func.__name__}")
                time.sleep(delay)
                if attempt == max_retries - 1:
                    trigger_api_cooldown(60)
                    raise
            else:
                raise
    return None
# ============================================================
# PART 11 – CONTEXT ENGINE V10 (Bagian 1)
# ============================================================
def compute_vol_forecast(coin: str) -> float:
    try:
        atr5 = get_atr_pct(coin, 5, "5m")
        atr50 = get_atr_pct(coin, 50, "5m")
        if atr50 <= 0.001:
            return 1.0
        return atr5 / atr50
    except:
        return 1.0

def compute_market_tension(coin: str) -> float:
    try:
        oi_roc = get_oi_roc(coin)
        funding = abs(get_funding_pct(coin))
        delta_shift = get_delta_shift(coin)
        tension = 0.0
        if oi_roc > 2:
            tension += 30
        elif oi_roc > 0.5:
            tension += 15
        if funding > 0.05:
            tension += 20
        elif funding > 0.02:
            tension += 10
        if abs(delta_shift) < 2 and oi_roc > 3:
            tension += 30
        return min(100.0, tension)
    except:
        return 0.0

def compute_regime_transition(coin: str) -> float:
    try:
        candles = get_candles(coin, "5m", 30)
        if not candles or len(candles) < 20:
            return 0.0
        closes = [float(c['c']) for c in candles]
        roc5 = (closes[-1] - closes[-5]) / max(closes[-5], 0.01) * 100
        roc20 = (closes[-1] - closes[-20]) / max(closes[-20], 0.01) * 100
        accel = roc5 - roc20
        vol_spike = get_volume_spike(coin)
        oi_roc = get_oi_roc(coin)
        score = 0.0
        if abs(accel) > 0.5:
            score += 30
        if vol_spike > 1.8:
            score += 30
        if abs(oi_roc) > 5:
            score += 40
        return min(100.0, score)
    except:
        return 0.0

def compute_shock_score(coin: str) -> float:
    try:
        candles = get_candles(coin, "5m", 50)
        if not candles or len(candles) < 20:
            return 0.0
        ranges = [float(c['h']) - float(c['l']) for c in candles[-20:]]
        range_avg = sum(ranges) / len(ranges) if ranges else 0.001
        last_range = ranges[-1] if ranges else 0.001
        compression = 1 - (last_range / (range_avg + 0.001))
        oi_roc = get_oi_roc(coin)
        oi_build = min(1.0, max(0.0, oi_roc / 10))
        high_20 = max(float(c['h']) for c in candles[-20:])
        low_20 = min(float(c['l']) for c in candles[-20:])
        range_pct = (high_20 - low_20) / max(high_20, 0.01) * 100
        range_age = 0.0
        if range_pct < 2:
            count = 0
            for c in reversed(candles[-20:]):
                if float(c['h']) <= high_20 and float(c['l']) >= low_20:
                    count += 1
                else:
                    break
            range_age = min(1.0, count / 20)
        funding = abs(get_funding_pct(coin))
        funding_stretch = min(1.0, funding / 0.1)
        score = compression * 40 + oi_build * 30 + range_age * 15 + funding_stretch * 15
        return min(100.0, score)
    except:
        return 0.0
        
# ============================================================
# PART 12 – CONTEXT ENGINE V10 (Bagian 2)
# ============================================================
def compute_market_breath_v10() -> Dict[str, float]:
    # PAKAI CACHE MANAGER, BUKAN GLOBAL
    cached = CACHE.get("breath", max_age=60)
    if cached:
        return cached

    try:
        meta = info.meta_and_asset_ctxs()
        coins_data = []
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            vol = float(ctx.get("dayNtlVlm", 0))
            if vol > 5_000_000:
                price = float(ctx.get("markPx", 0))
                candles = get_candles(asset["name"], "5m", 5)
                if candles and len(candles) >= 2:
                    roc = (float(candles[-1]['c']) - float(candles[-2]['c'])) / max(float(candles[-2]['c']), 0.01) * 100
                    coins_data.append({
                        "name": asset["name"],
                        "roc": roc,
                        "vol": vol,
                        "price": price
                    })

        coins_data.sort(key=lambda x: x["vol"], reverse=True)
        top_20 = coins_data[:20]

        green = sum(1 for c in top_20 if c["roc"] > 0)
        bull_pct = green / len(top_20) if top_20 else 0.5

        btc_roc = next((c["roc"] for c in top_20 if c["name"] == "BTC"), 0)
        btc_direction = 1 if btc_roc > 0 else -1 if btc_roc < 0 else 0
        if btc_direction != 0:
            aligned = sum(1 for c in top_20 if (c["roc"] > 0) == (btc_roc > 0))
            participation = aligned / len(top_20) if top_20 else 0.5
        else:
            participation = 0.5

        top_5_avg = np.mean([c["roc"] for c in top_20[:5]]) if len(top_20) >= 5 else 0
        bottom_5_avg = np.mean([c["roc"] for c in top_20[-5:]]) if len(top_20) >= 5 else 0
        leadership = top_5_avg - bottom_5_avg

        dispersion = np.std([c["roc"] for c in top_20]) if len(top_20) > 1 else 0

        if len(top_20) >= 10:
            large_avg = np.mean([c["roc"] for c in top_20[:10]])
            small_avg = np.mean([c["roc"] for c in top_20[10:]])
            rotation = small_avg - large_avg
        else:
            rotation = 0

        result = {
            "bull": bull_pct,
            "bear": 1 - bull_pct,
            "participation": participation,
            "leadership": leadership,
            "dispersion": dispersion,
            "rotation": rotation,
            "ts": time.time()
        }

        # SIMPAN KE CACHE MANAGER
        CACHE.set("breath", result)
        return result

    except Exception as e:
        logger.error(f"compute_market_breath_v10 error: {e}")
        return {"bull": 0.5, "bear": 0.5, "participation": 0.5,
                "leadership": 0, "dispersion": 0, "rotation": 0, "ts": time.time()}

def get_context_snapshot(coin: str = "BTC") -> ContextSnapshot:
    global _last_context
    with _context_lock:
        now = time.time()
        if _last_context and now - _last_context.timestamp < _CONTEXT_TTL:
            return _last_context

        shock = compute_shock_score(coin)
        trans = compute_regime_transition(coin)
        tension = compute_market_tension(coin)
        vol_f = compute_vol_forecast(coin)
        breath = compute_market_breath_v10()
        event_adj = get_event_risk_adjustment()
        event_r = event_adj.get("importance", 0)
        regime = get_market_regime()
        dom = breath.get("dom", 50.0)

        ctx = ContextSnapshot(
            timestamp=now,
            shock_score=shock,
            transition_prob=trans,
            tension=tension,
            vol_forecast=vol_f,
            breath_bull=breath.get("bull", 0.5),
            breath_bear=breath.get("bear", 0.5),
            event_risk=event_r,
            dominance=dom,
            regime=regime
        )
        _last_context = ctx
        threading.Thread(target=log_context, args=(ctx,), daemon=True).start()
        return ctx
        
# ============================================================
# PART 13 – EVENT RISK + REACTION ENGINE (V10 CORE)
# ============================================================
def get_event_risk_adjustment() -> Dict[str, float]:
    with _event_risk_lock:
        now = time.time()
        total_importance = 0
        total_vol = 0
        bias_score = 0.0

        for ev in _EVENT_RISK_DATA:
            diff_hours = (ev.ts - now) / 3600
            if 0 < diff_hours < TUNABLE["EVENT_RISK_DECAY_HOURS"]:
                decay = 1.0 - (diff_hours / TUNABLE["EVENT_RISK_DECAY_HOURS"])
                total_importance += ev.importance * decay
                total_vol += ev.expected_vol * decay
                if ev.bias == "bullish":
                    bias_score += ev.importance * decay
                elif ev.bias == "bearish":
                    bias_score -= ev.importance * decay

        return {
            "importance": min(100, total_importance),
            "volatility": min(100, total_vol),
            "bias": max(-100, min(100, bias_score))
        }

def set_event_risk_v10(importance: int, expected_vol: int, scope: str, bias: str, label: str, ts: float = None):
    if ts is None:
        ts = time.time()
    with _event_risk_lock:
        _EVENT_RISK_DATA.append(EventRisk(
            importance=min(100, max(0, importance)),
            expected_vol=min(100, max(0, expected_vol)),
            scope=scope,
            bias=bias,
            label=label,
            ts=ts
        ))
        now = time.time()
        _EVENT_RISK_DATA[:] = [e for e in _EVENT_RISK_DATA if e.ts > now - 86400]
        logger.info(f"Event risk set: {label} importance={importance} vol={expected_vol} bias={bias}")

def compute_reaction(event_risk: EventRisk, btc_move: float, vol_spike: float) -> MarketReaction:
    expected_direction = event_risk.bias if event_risk.bias != "neutral" else "neutral"

    if btc_move > 0.5:
        actual_direction = "up"
    elif btc_move < -0.5:
        actual_direction = "down"
    else:
        actual_direction = "neutral"

    actual_vol = min(100, vol_spike * 50)

    if event_risk.expected_vol > 0:
        vol_ratio = actual_vol / event_risk.expected_vol
        absorption = max(0, min(1, 1 - vol_ratio))
    else:
        absorption = 0.5

    confidence = 0.5
    if abs(btc_move) > 1:
        confidence += 0.3
    if vol_spike > 1.5:
        confidence += 0.2
    confidence = min(1, confidence)

    return MarketReaction(
        event=event_risk.label,
        expected_vol=float(event_risk.expected_vol),
        expected_direction=expected_direction,
        actual_vol=actual_vol,
        actual_direction=actual_direction,
        actual_move=btc_move,
        absorption=absorption,
        confidence=confidence,
        timestamp=time.time()
    )

def get_reaction_adjustment() -> Dict[str, float]:
    with _reaction_lock:
        if not _reaction_history:
            return {"mode": "NORMAL", "factor": 1.0}

        latest = _reaction_history[-1]

        if latest.absorption > 0.7 and latest.confidence < 0.4:
            return {"mode": "NORMAL", "factor": 1.0}

        if abs(latest.actual_move) > 1 and latest.confidence > 0.6:
            if latest.actual_direction == "up":
                return {"mode": "AGGRESSIVE", "factor": 1.2, "bias": "bullish"}
            else:
                return {"mode": "DEFENSIVE", "factor": 0.8, "bias": "bearish"}

        if latest.expected_vol > 70 and latest.actual_vol < 30:
            return {"mode": "PREPARE", "factor": 0.9}

        return {"mode": "NORMAL", "factor": 1.0}

def update_reaction_history(reaction: MarketReaction):
    with _reaction_lock:
        _reaction_history.append(reaction)
    threading.Thread(target=log_reaction, args=(reaction,), daemon=True).start()

def get_current_reaction() -> Optional[MarketReaction]:
    with _reaction_lock:
        if _reaction_history:
            return _reaction_history[-1]
        return None
        
# ============================================================
# PART 14 – INTENT MEMORY (V10)
# ============================================================
def update_intent_memory(coin: str, intent: str, outcome: str, pnl: float):
    with _intent_memory_lock:
        if coin not in _intent_memory:
            _intent_memory[coin] = deque(maxlen=TUNABLE["INTENT_MEMORY_MAX"])
        _intent_memory[coin].append(IntentMemory(
            intent=intent,
            outcome=outcome,
            pnl=pnl,
            ts=time.time()
        ))
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO intent_memory (timestamp, coin, intent, outcome, pnl)
                     VALUES (?,?,?,?,?)''',
                  (int(time.time()), coin, intent, outcome, pnl))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"update_intent_memory DB error: {e}")

def get_intent_success_rate(coin: str, intent: str) -> float:
    with _intent_memory_lock:
        if coin not in _intent_memory:
            return 0.5
        cutoff = time.time() - TUNABLE["INTENT_MEMORY_HOURS"] * 3600
        recent = [e for e in _intent_memory[coin]
                  if e.intent == intent and e.ts > cutoff]
        if not recent:
            return 0.5
        success = sum(1 for e in recent if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
        return success / len(recent)

def get_intent_success_rate_all(intent: str) -> float:
    with _intent_memory_lock:
        cutoff = time.time() - TUNABLE["INTENT_MEMORY_HOURS"] * 3600
        all_entries = []
        for coin, deq in _intent_memory.items():
            all_entries.extend([e for e in deq if e.intent == intent and e.ts > cutoff])
        if not all_entries:
            return 0.5
        success = sum(1 for e in all_entries if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
        return success / len(all_entries)
        
# ============================================================
# PART 15 – EXECUTION MODE V10 (5 Mode)
# ============================================================
def get_execution_mode_v10(context: ContextSnapshot, reaction: Optional[MarketReaction],
                            intent_success: float, event_adjust: Dict[str, float]) -> Tuple[ExecutionMode, Dict[str, float]]:
    if event_adjust.get("importance", 0) > 70 or context.shock_score > 80:
        return ExecutionMode.DEFENSIVE, {"threshold": 1.3, "size": 0.4, "cooldown": 2.0}

    if context.shock_score > 60 and context.tension > 70:
        if reaction and reaction.actual_move > 1 and reaction.confidence > 0.6:
            return ExecutionMode.AGGRESSIVE, {"threshold": 0.8, "size": 1.4, "cooldown": 0.5}

    if context.transition_prob > 60 and context.tension > 50:
        return ExecutionMode.PREPARE, {"threshold": 0.9, "size": 0.8, "cooldown": 0.7}

    if intent_success < 0.4 or context.breath_bull < 0.3:
        return ExecutionMode.CAUTIOUS, {"threshold": 1.15, "size": 0.6, "cooldown": 1.3}

    return ExecutionMode.NORMAL, {"threshold": 1.0, "size": 1.0, "cooldown": 1.0}

def get_mode_adjustment(mode: ExecutionMode) -> Dict[str, float]:
    adjustments = {
        ExecutionMode.NORMAL: {"threshold": 1.0, "size": 1.0, "cooldown": 1.0},
        ExecutionMode.PREPARE: {"threshold": 0.9, "size": 0.8, "cooldown": 0.7},
        ExecutionMode.CAUTIOUS: {"threshold": 1.15, "size": 0.6, "cooldown": 1.3},
        ExecutionMode.AGGRESSIVE: {"threshold": 0.8, "size": 1.4, "cooldown": 0.5},
        ExecutionMode.DEFENSIVE: {"threshold": 1.3, "size": 0.4, "cooldown": 2.0},
    }
    return adjustments.get(mode, adjustments[ExecutionMode.NORMAL])

def get_mode_emoji(mode: ExecutionMode) -> str:
    emojis = {
        ExecutionMode.NORMAL: "⚖️",
        ExecutionMode.PREPARE: "🔧",
        ExecutionMode.CAUTIOUS: "⚠️",
        ExecutionMode.AGGRESSIVE: "⚡",
        ExecutionMode.DEFENSIVE: "🛡️",
    }
    return emojis.get(mode, "⚖️")

def get_mode_color(mode: ExecutionMode) -> str:
    colors = {
        ExecutionMode.NORMAL: "🟡",
        ExecutionMode.PREPARE: "🟠",
        ExecutionMode.CAUTIOUS: "🟡",
        ExecutionMode.AGGRESSIVE: "🔴",
        ExecutionMode.DEFENSIVE: "🔵",
    }
    return colors.get(mode, "🟡")
    
# ============================================================
# PART 16 – REGIME INERTIA + CONTEXT WITH CONFIDENCE
# ============================================================
def get_regime_with_inertia(coin: str) -> Tuple[str, float]:
    current = get_market_regime()
    with _regime_history_lock:
        _regime_history.append((time.time(), current))
        cutoff = time.time() - TUNABLE["REGIME_INERTIA_WINDOW"]
        recent = [r for ts, r in _regime_history if ts >= cutoff]
        if len(recent) < 2:
            return current, 0.0
        changes = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
        change_rate = changes / len(recent) if recent else 0
        penalty = min(30, change_rate * 50)
        return current, penalty

def get_context_with_confidence(coin: str, confidence: float) -> ContextSnapshot:
    # PAKAI CACHE MANAGER
    bucket = int(confidence / 10) * 10
    cache_key = f"context_{coin}_{bucket}"
    
    # Cek cache
    cached = CACHE.get(cache_key, max_age=10 if confidence > 70 else 5 if confidence > 40 else 3)
    if cached:
        return cached
    
    # Hitung ulang
    ctx = get_context_snapshot(coin)
    CACHE.set(cache_key, ctx)
    return ctx
    
# ============================================================
# PART 17 – SNAPSHOT + DATA FUNCTIONS
# ============================================================
def _get_adaptive_snapshot_ttl() -> int:
    try:
        vol = get_volatility_regime()
        return {"LOW_VOLATILITY": 15, "HIGH_VOLATILITY": 3}.get(vol, 8)
    except:
        return _SNAPSHOT_TTL
        
def refresh_snapshot():
    now = time.time()
    ttl = _get_adaptive_snapshot_ttl()
    
    # PAKAI CACHE DULU
    cached = CACHE.get("snapshot", max_age=ttl)
    if cached:
        return cached
    
    # CEK COOLDOWN
    if not can_call_api():
        logger.debug("⏳ API on cooldown, using stale snapshot")
        return CACHE.get("snapshot")  # return stale
    
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
        
        snapshot = MarketSnapshot(timestamp=now, mids=mids, oi=oi, funding=funding)
        CACHE.set("snapshot", snapshot)
        
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
        
        return snapshot
        
    except Exception as e:
        if "429" in str(e):
            trigger_api_cooldown(30)
            # Return stale snapshot
            stale = CACHE.get("snapshot")
            if stale:
                logger.warning(f"⚠️ Using stale snapshot due to rate limit")
                return stale
        logger.error(f"Snapshot refresh error: {e}")
        return None

def get_snapshot() -> MarketSnapshot:
    snapshot = refresh_snapshot()
    if snapshot:
        return snapshot
    # Fallback: return empty tapi jangan request lagi
    stale = CACHE.get("snapshot")
    if stale:
        return stale
    return MarketSnapshot(timestamp=time.time(), mids={}, oi={}, funding={})
        
# ========== DATA FUNCTIONS ==========
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
    
    key = f"candles_{coin}_{timeframe}_{limit}"
    ttl = {"5m": 60, "15m": 120, "1h": 300, "4h": 600}.get(timeframe, 300)
    
    cached = CACHE.get(key, max_age=ttl)
    if cached:
        return cached
    
    if not can_call_api():
        return []
    
    try:
        end_ms = int(time.time() * 1000)
        tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
        interval = tf_ms.get(timeframe, 3600000)
        start_ms = end_ms - limit * interval
        candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms) or []
    except Exception as e:
        if "429" in str(e):
            trigger_api_cooldown(30)
            logger.error(f"🚫 Rate limit on candles {coin}")
            return []
        logger.error(f"get_candles failed for {coin}: {e}")
        candles = []
    
    CACHE.set(key, candles)
    return candles
    
def get_ob_delta(coin: str) -> float:
    key = f"ob_{coin}"
    
    # PAKAI CACHE MANAGER
    cached = CACHE.get(key, max_age=5)
    if cached:
        return cached
    
    try:
        l2 = info.l2_snapshot(coin)
        bids = sum(float(b['sz'])*float(b['px']) for b in l2['levels'][0][:5])
        asks = sum(float(a['sz'])*float(a['px']) for a in l2['levels'][1][:5])
        if bids + asks == 0:
            return 0
        raw = (bids - asks) / (bids + asks) * 100
        raw = max(-60, min(60, raw))
        
        # Smoothing dengan cache sebelumnya
        prev = CACHE.get(f"ob_raw_{coin}")
        if prev is None:
            prev = raw
        alpha = min(0.9, 0.3 + abs(raw - prev) / 60)
        smoothed = alpha * raw + (1 - alpha) * prev
        
        # Simpan raw dan smoothed
        CACHE.set(f"ob_raw_{coin}", raw)
        CACHE.set(key, smoothed)
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
    key = f"cvd_{coin}_{minutes}"
    
    # PAKAI CACHE MANAGER
    cached = CACHE.get(key, max_age=30)
    if cached:
        return cached
    
    try:
        trades = info.recent_trades(coin)
        if not trades:
            return 0
        cutoff = int((time.time() - minutes*60) * 1000)
        cvd = 0.0
        for t in trades:
            if t['time'] < cutoff:
                continue
            usd = float(t['px']) * float(t['sz'])
            cvd += usd if t['side'] == 'B' else -usd
        cvd_val = cvd / 1e6
        CACHE.set(key, cvd_val)
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
    
# ========== REGIMES ==========
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

# ========== PHASE 1 — REGIME INTERPRETATION ==========

def interpret_regime_v10(coin: str = "BTC") -> RegimeInterpretation:
    candles = get_candles(coin, "1h", 100)
    if not candles or len(candles) < 30:
        return RegimeInterpretation("UNKNOWN", 0, 0, 0, 0, 0, "NONE", False, 0)

    closes = [float(c['c']) for c in candles[-30:]]
    highs = [float(c['h']) for c in candles[-30:]]
    lows = [float(c['l']) for c in candles[-30:]]

    ema8 = np.mean(closes[-8:])
    ema21 = np.mean(closes[-21:])
    trend_diff = (ema8 - ema21) / max(ema21, 0.01) * 100
    trend_strength = min(100, max(0, abs(trend_diff) * 20))

    range_pct = (max(highs[-5:]) - min(lows[-5:])) / max(closes[-1], 0.01) * 100
    range_factor = min(100, range_pct * 20)
    trend_factor = 100 - range_factor

    changes = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
    consistency = abs(changes / len(closes) - 0.5) * 200
    stability = min(100, max(0, consistency))

    age_minutes = 0
    for i in range(1, len(candles)):
        if (closes[-i] > closes[-i-1]) != (closes[-1] > closes[-2]):
            break
        age_minutes += 60

    vol_spike = get_volume_spike(coin)
    oi_roc = get_oi_roc(coin)
    delta_shift = get_delta_shift(coin)

    trans_score = 0.0
    if vol_spike > 2.0:
        trans_score += 30
    if abs(oi_roc) > 5:
        trans_score += 25
    if abs(delta_shift) > 6:
        trans_score += 25
    if abs(trend_diff) < 0.5 and range_pct < 1:
        trans_score += 20
    trans_prob = min(100, trans_score)

    if trend_diff > 1.5:
        regime = "TRENDING_UP"
        direction = "TO_DOWN" if trans_prob > 70 else "TO_UP"
    elif trend_diff < -1.5:
        regime = "TRENDING_DOWN"
        direction = "TO_UP" if trans_prob > 70 else "TO_DOWN"
    else:
        regime = "RANGING"
        direction = "TO_UP" if trend_diff > 0.5 else "TO_DOWN"

    is_breaking = False
    breaking_strength = 0.0
    if trans_prob > 60:
        recent_high = max(highs[-5:])
        recent_low = min(lows[-5:])
        price = closes[-1]
        if regime == "RANGING":
            if price > recent_high * 1.002:
                is_breaking = True
                breaking_strength = min(100, (price / recent_high - 1) * 5000)
            elif price < recent_low * 0.998:
                is_breaking = True
                breaking_strength = min(100, (1 - price / recent_low) * 5000)

    agree = 0
    if (regime == "TRENDING_UP" and trend_diff > 0) or (regime == "TRENDING_DOWN" and trend_diff < 0):
        agree += 1
    if vol_spike < 1.5 and stability > 50:
        agree += 1
    if trans_prob < 30:
        agree += 1
    confidence = 50 + (agree / 3) * 40 + stability * 0.1
    confidence = min(100, max(0, confidence))

    return RegimeInterpretation(
        regime=regime,
        strength=trend_strength,
        stability=stability,
        confidence=confidence,
        age_minutes=age_minutes,
        transition_prob=trans_prob,
        transition_direction=direction,
        is_breaking=is_breaking,
        breaking_strength=breaking_strength
    )
    
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

# ========== PHASE 1 — OB & FVG ASSESSMENT ==========

def assess_ob_reaction_v10(coin: str, event: TradeEvent, candles: List[dict]) -> OBReaction:
    if not candles:
        return OBReaction(0, 0, 0, 0, 0, 0, 0)

    touch_count = 0
    first_touch = 0
    last_touch = 0
    reaction_strengths = []

    price_low = event.price_low
    price_high = event.price_high

    for i, c in enumerate(candles):
        low = float(c['l'])
        high = float(c['h'])

        if event.direction == "LONG":
            touched = low <= price_low * 1.002
        else:
            touched = high >= price_high * 0.998

        if touched:
            touch_count += 1
            if first_touch == 0:
                first_touch = c.get('t', 0) / 1000
            last_touch = c.get('t', 0) / 1000

            if i + 3 < len(candles):
                if event.direction == "LONG":
                    move_pct = (float(candles[i+3]['c']) - price_low) / max(price_low, 0.01) * 100
                    strength = min(100, max(0, move_pct * 50))
                else:
                    move_pct = (price_high - float(candles[i+3]['c'])) / max(price_high, 0.01) * 100
                    strength = min(100, max(0, move_pct * 50))
                reaction_strengths.append(strength)

    if not reaction_strengths:
        return OBReaction(0, 0, 0, 0, 0, 0, 20)

    if len(candles) > 5:
        first_price = float(candles[0]['c'])
        last_price = float(candles[-1]['c'])
        if event.direction == "LONG":
            followthrough = min(100, max(0, (last_price - first_price) / max(first_price, 0.01) * 200))
        else:
            followthrough = min(100, max(0, (first_price - last_price) / max(first_price, 0.01) * 200))
    else:
        followthrough = 50

    max_reaction = max(reaction_strengths)
    avg_reaction = sum(reaction_strengths) / len(reaction_strengths)
    confidence = 30 + avg_reaction * 0.3 + followthrough * 0.2 + min(50, touch_count * 10)
    confidence = min(100, confidence)

    return OBReaction(
        touch_count=touch_count,
        first_touch_time=first_touch,
        last_touch_time=last_touch,
        max_reaction_strength=max_reaction,
        avg_reaction=avg_reaction,
        followthrough=followthrough,
        confidence=confidence
    )

def assess_fvg_quality_v10(coin: str, event: TradeEvent, candles: List[dict]) -> FVGQuality:
    if not candles:
        return FVGQuality(0, 0, 0, 0, 0, 0)

    gap_low = event.price_low
    gap_high = event.price_high
    gap_size = (gap_high - gap_low) / max(gap_low, 0.01) * 100
    size_score = min(100, gap_size * 200)

    fill_ratio = getattr(event, 'fill_ratio', 0.0)
    fill_time = getattr(event, 'fill_time_minutes', 30)
    if fill_time < 1:
        fill_speed = 100
    elif fill_time < 5:
        fill_speed = 80
    elif fill_time < 15:
        fill_speed = 50
    elif fill_time < 30:
        fill_speed = 30
    else:
        fill_speed = 10

    idx = event.extra.get('idx', 0)
    reaction = 50
    if idx + 5 < len(candles):
        if event.direction == "LONG":
            move = (float(candles[idx+5]['c']) - gap_low) / max(gap_low, 0.01) * 100
        else:
            move = (gap_high - float(candles[idx+5]['c'])) / max(gap_high, 0.01) * 100
        reaction = min(100, max(0, move * 100))

    age_minutes = 0
    if idx < len(candles):
        age_minutes = (time.time() - candles[idx].get('t', 0) / 1000) / 60

    quality = 0
    quality += size_score * 0.25
    quality += (1 - fill_ratio) * 30
    quality += (100 - fill_speed) * 0.2
    quality += reaction * 0.25
    quality = min(100, max(0, quality))

    return FVGQuality(
        size=size_score,
        fill_ratio=fill_ratio,
        fill_speed=fill_speed,
        reaction=reaction,
        age_minutes=age_minutes,
        quality_score=quality
    )

def get_bias_4h_advanced(coin: str) -> Tuple[str, float, float]:
    candles = get_candles(coin, "4h", 25)
    if not candles or len(candles) < 15:
        return "NEUTRAL", 0.0, 0.0

    closes = [float(c['c']) for c in candles[-15:]]
    ema8 = np.mean(closes[-8:])
    ema21 = np.mean(closes[-21:]) if len(closes) >= 21 else ema8

    diff_pct = (ema8 - ema21) / max(ema21, 0.01) * 100
    if diff_pct > 1.5:
        bias = "BULLISH"
        strength = min(100, 50 + diff_pct * 10)
    elif diff_pct < -1.5:
        bias = "BEARISH"
        strength = min(100, 50 + abs(diff_pct) * 10)
    else:
        bias = "NEUTRAL"
        strength = max(0, 50 - abs(diff_pct) * 10)

    recent_bias = []
    for i in range(max(0, len(closes)-6), len(closes)-1):
        ema8_i = np.mean(closes[i-7:i+1]) if i >= 8 else ema8
        ema21_i = np.mean(closes[i-20:i+1]) if i >= 21 else ema21
        if ema8_i > ema21_i * 1.01:
            recent_bias.append("BULLISH")
        elif ema8_i < ema21_i * 0.99:
            recent_bias.append("BEARISH")
        else:
            recent_bias.append("NEUTRAL")

    if len(recent_bias) < 3:
        stability = 0.5
    else:
        consistent = sum(1 for b in recent_bias if b == bias)
        stability = consistent / len(recent_bias)

    return bias, strength, stability
    
# ============================================================
# PART 23 – ZONE MEMORY DECAY + FVG FILL SPEED + OI PERSISTENCE
# ============================================================

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

def get_zone_penalty_v8(coin: str, zone_type: str, low: float, high: float) -> float:
    key = f"{coin}_{zone_type}_{round(low,6)}_{round(high,6)}"
    with _zone_memory_lock:
        if key not in _zone_memory:
            return 0.0
        data = _zone_memory[key]
        if not data.get("strengths"):
            return 0.0
        age = time.time() - data.get("last_touch", time.time())
        touch_count = data.get("touch_count", 0)
        age_factor = max(0.2, 1.0 - (age / (TUNABLE["ZONE_DECAY_DAYS"] * 86400)))
        strength_decay = max(0.1, 1.0 - (touch_count * TUNABLE["ZONE_STRENGTH_DECAY_PER_TOUCH"]))
        avg_strength = sum(data["strengths"]) / len(data["strengths"])
        base_penalty = max(0.0, 40 - avg_strength) * 0.5
        return base_penalty * age_factor * strength_decay

def validate_fvg_with_fill_speed(coin: str, fvg_data: dict, candles: List[dict]) -> Tuple[bool, float]:
    try:
        gap_low = fvg_data.get("gap_low", 0)
        gap_high = fvg_data.get("gap_high", 0)
        idx = fvg_data.get("idx", 0)
        fvg_type = fvg_data.get("type", "bullish")
        if idx + 1 >= len(candles):
            return True, 1.0
        fill_ratio = 0.0
        first_fill_time = None
        now_ts = time.time()
        for i in range(idx+1, len(candles)):
            c = candles[i]
            close = float(c['c'])
            if fvg_type == "bullish":
                if close <= gap_low:
                    fill_ratio = 1.0
                    first_fill_time = c.get('t', 0) / 1000
                    break
                elif close < gap_high:
                    fill_ratio = max(fill_ratio, (close - gap_low) / (gap_high - gap_low))
            else:
                if close >= gap_high:
                    fill_ratio = 1.0
                    first_fill_time = c.get('t', 0) / 1000
                    break
                elif close > gap_low:
                    fill_ratio = max(fill_ratio, (gap_high - close) / (gap_high - gap_low))
        if fill_ratio < 0.3:
            if first_fill_time:
                time_to_fill = (now_ts - first_fill_time) / 60
                if time_to_fill < TUNABLE["FVG_FILL_SPEED_FAST_MINUTES"]:
                    return True, 1.5
                elif time_to_fill < TUNABLE["FVG_FILL_SLOW_MINUTES"]:
                    return True, 1.2
                else:
                    return True, 0.8
            return True, 1.0
        elif fill_ratio > 0.7:
            vol_spike = get_volume_spike(coin)
            if vol_spike > 1.5:
                return True, 0.8
            return False, 0.0
        else:
            return True, 0.9
    except:
        return True, 1.0

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
        
# ============================================================
# PART 24 – BELIEF STATE + PREDICTION QUALITY
# ============================================================

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

def _compute_belief_drift() -> float:
    try:
        with _belief_history_lock:
            scores = []
            for hist in _belief_history.values():
                for entry in hist:
                    scores.append(entry.get("score", 0))
            if len(scores) < 2:
                return 0.0
            return float(np.std(scores[-20:]))
    except:
        return 0.0

def compute_belief(event: TradeEvent, filter_score: float, structure_valid_long: bool,
                   structure_valid_short: bool, trigger_strength: float) -> Tuple[BeliefState, float, str]:
    if event.direction == "LONG" and not structure_valid_long:
        return BeliefState.INVALIDATED, 0.0, "structure invalid for long"
    if event.direction == "SHORT" and not structure_valid_short:
        return BeliefState.INVALIDATED, 0.0, "structure invalid for short"
    event_weights = {
        "LIQUIDITY": 25, "OB": 20, "OB_FLOW": 25, "FVG": 15,
        "FVG_FLOW": 20, "VACUUM": 15, "CLUSTER": 30,
    }
    event_score = event_weights.get(event.type, 10)
    filter_score_weighted = filter_score * 0.4
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
        if old_state != new_belief:
            duration = now - old.get("since", now)
            add_belief_state_log(coin, new_belief.value, duration, trigger)
        _belief_state[coin] = {
            "state": new_belief,
            "score": belief_score,
            "since": now,
            "family": old.get("family", "unknown")
        }
    with _belief_history_lock:
        if coin not in _belief_history:
            _belief_history[coin] = deque(maxlen=10)
        _belief_history[coin].append({
            "state": new_belief.value,
            "score": belief_score,
            "ts": time.time()
        })

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

def evaluate_prediction_quality(signal_id: str, coin: str, predicted_direction: str,
                                 actual_direction: str, entry_price: float,
                                 predicted_zone_low: float, predicted_zone_high: float,
                                 mfe: float, mae: float, thesis_validated: bool) -> float:
    quality = 50.0
    if predicted_direction == actual_direction:
        quality += 30
    else:
        quality -= 20
    if predicted_zone_low <= entry_price <= predicted_zone_high:
        quality += 25
        zone_accuracy = 1.0
    else:
        zone_accuracy = max(0, 1 - abs(entry_price - predicted_zone_high) / max(predicted_zone_high, 1))
        quality += zone_accuracy * 15
    if mae != 0 and mfe > abs(mae):
        ratio = min(3.0, mfe / abs(mae))
        quality += (ratio / 3.0) * 25
        timing_quality = ratio
    elif mfe > 0:
        quality += 12
        timing_quality = 1.0
    else:
        timing_quality = 0.0
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
       
# ============================================================
# PART 25 – ENTROPY + DECISION ENERGY
# ============================================================

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

def compute_clarity(context: ContextSnapshot, breath: Dict[str, float],
                    score_long: int, score_short: int,
                    transition_prob: float) -> Dict[str, Any]:
    """
    Compute decision clarity based on market breadth, participation, leadership,
    rotation, dispersion, and contradiction between long and short scores.
    Returns severity (0-100), decision_quality (0-1), dominant_factor, reasons.
    """
    result = {
        "severity": 0,
        "decision_quality": 1.0,
        "dominant_factor": "none",
        "reasons": []
    }
    severity = 0

    # 1. Participation
    participation = breath.get("participation", 0.5)
    if participation < 0.35:
        severity += 25
        if 25 > result["severity"]:
            result["dominant_factor"] = "low_participation"
        result["reasons"].append(f"participation {participation*100:.0f}% <35%")
    elif participation < 0.5:
        severity += 10
        result["reasons"].append(f"participation {participation*100:.0f}% <50%")

    # 2. Leadership (gap between top and bottom coins)
    leadership = breath.get("leadership", 0)
    if leadership < 0.5:
        severity += 15
        if 15 > result["severity"]:
            result["dominant_factor"] = "weak_leadership"
        result["reasons"].append(f"leadership {leadership:.1f}% <0.5")
    elif leadership < 1.0:
        severity += 5
        result["reasons"].append(f"leadership {leadership:.1f}% <1.0")

    # 3. Rotation (small caps vs large caps)
    rotation = breath.get("rotation", 0)
    if abs(rotation) > 1.5:
        severity += 15
        if 15 > result["severity"]:
            result["dominant_factor"] = "high_rotation"
        result["reasons"].append(f"rotation {rotation:+.1f}% >1.5")
    elif abs(rotation) > 1.0:
        severity += 8
        result["reasons"].append(f"rotation {rotation:+.1f}% >1.0")

    # 4. Dispersion (volatility spread)
    dispersion = breath.get("dispersion", 0)
    if dispersion > 0.8:
        severity += 15
        if 15 > result["severity"]:
            result["dominant_factor"] = "high_dispersion"
        result["reasons"].append(f"dispersion {dispersion:.2f} >0.8")
    elif dispersion > 0.5:
        severity += 8
        result["reasons"].append(f"dispersion {dispersion:.2f} >0.5")

    # 5. Contradiction between long and short scores
    diff = abs(score_long - score_short)
    if score_long > 55 and score_short > 55:
        if diff < 20:                       # PRIORITAS: contradiction parah
            severity += 50
            if 50 > result["severity"]:
                result["dominant_factor"] = "strong_contradiction"
            result["reasons"].append(f"strong contradiction diff={diff:.0f}")
        elif diff < 30:                     # moderate
            severity += 35
            if 35 > result["severity"]:
                result["dominant_factor"] = "moderate_contradiction"
            result["reasons"].append(f"moderate contradiction diff={diff:.0f}")
        else:
            severity += 15
            result["reasons"].append(f"weak contradiction diff={diff:.0f}")

    # 6. Transition probability
    if transition_prob > 70:
        severity += 20
        if 20 > result["severity"]:
            result["dominant_factor"] = "high_transition"
        result["reasons"].append(f"transition {transition_prob:.0f}% >70%")
    elif transition_prob > 50:
        severity += 10
        result["reasons"].append(f"transition {transition_prob:.0f}% >50%")

    # Cap severity
    severity = min(100, max(0, severity))
    result["severity"] = severity

    # Decision quality dengan easing (power 1.5) → turun makin keras kalau chaos tinggi
    decision_quality = 1.0 - (severity / 100) ** 1.5
    result["decision_quality"] = max(0.0, min(1.0, decision_quality))

    return result

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

def compute_confidence_from_score(score: int, data_confidence: int, evidence_families: int) -> float:
    conf = score * 0.7 + data_confidence * 0.2 + min(100, (evidence_families / 3) * 100) * 0.1
    return min(100.0, conf)

# ========== PHASE 1 — CONFIDENCE CALIBRATION ==========

def calibrate_confidence_v10(coin: str, raw_confidence: float) -> CalibratedConfidence:
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT final_score, outcome FROM signals 
                     WHERE coin = ? AND evaluated = 1 
                     ORDER BY timestamp DESC LIMIT 100''', (coin,))
        rows = c.fetchall()
        conn.close()

        if len(rows) < 10:
            conn2 = db_connect()
            c2 = conn2.cursor()
            c2.execute('''SELECT final_score, outcome FROM signals 
                          WHERE evaluated = 1 
                          ORDER BY timestamp DESC LIMIT 500''')
            rows = c2.fetchall()
            conn2.close()

        if len(rows) < 10:
            return CalibratedConfidence(raw_confidence, raw_confidence, 1.0, len(rows), time.time())

        buckets = {}
        for score, outcome in rows:
            bucket = int(score / 10) * 10
            if bucket not in buckets:
                buckets[bucket] = {'total': 0, 'wins': 0}
            buckets[bucket]['total'] += 1
            if outcome in ('TP_HIT', 'PARTIAL_WIN'):
                buckets[bucket]['wins'] += 1

        closest_bucket = min(buckets.keys(), key=lambda x: abs(x - raw_confidence))
        bucket_data = buckets[closest_bucket]
        win_rate = bucket_data['wins'] / max(1, bucket_data['total'])
        calibrated = win_rate * 100

        sample_size = bucket_data['total']
        if sample_size < 20:
            calibration_factor = max(0.5, sample_size / 40)
        else:
            calibration_factor = 1.0

        final_calibrated = raw_confidence * (1 - calibration_factor * 0.3) + calibrated * (calibration_factor * 0.3)
        final_calibrated = min(100, max(0, final_calibrated))

        return CalibratedConfidence(
            raw=raw_confidence,
            calibrated=final_calibrated,
            calibration_factor=calibration_factor,
            sample_size=sample_size,
            last_update=time.time()
        )
    except Exception as e:
        logger.error(f"Calibrate confidence error: {e}")
        return CalibratedConfidence(raw_confidence, raw_confidence, 1.0, 0, time.time())

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

    if urgency_score > 70:
        return TimePressure.URGENT, urgency_score
    elif urgency_score > 30:
        return TimePressure.NORMAL, urgency_score
    return TimePressure.LOW, urgency_score

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


def bucket_value(value, low=3, high=8):
    if abs(value) > high: return "HIGH"
    if abs(value) > low: return "MID"
    return "LOW"

def store_failed_move(coin, event_type, delta, vol_spike, clarity, intent, direction, price, reason):
    with _failed_lock:
        fp = FailedMoveFingerprint(
            coin=coin,
            event_type=event_type,
            delta_bucket=bucket_value(delta),
            vol_bucket=bucket_value(vol_spike, low=1.5, high=2.5),
            clarity=clarity,
            intent=intent,
            direction=direction,
            price=price,
            timestamp=time.time(),
            reason=reason
        )
        if coin not in _failed_memory:
            _failed_memory[coin] = []
        _failed_memory[coin].append(fp)
        if len(_failed_memory[coin]) > 50:
            _failed_memory[coin] = _failed_memory[coin][-50:]

def get_failed_move_risk(coin, event_type, delta, vol_spike, clarity, intent, direction, current_price):
    with _failed_lock:
        if coin not in _failed_memory:
            return {"risk": 1.0, "reason": None}
        now = time.time()
        similarities = []
        for fp in _failed_memory[coin]:
            age_h = (now - fp.timestamp) / 3600
            if age_h > 48:
                continue
            time_weight = np.exp(-age_h / 8)
            if fp.direction != direction:
                continue
            price_dist = abs(fp.price - current_price) / max(fp.price, 0.01) * 100
            if price_dist > 5:
                continue
            sim = 0.0
            sim += 0.3 if fp.event_type == event_type else 0.0
            sim += 0.3 if fp.delta_bucket == bucket_value(delta) else 0.0
            sim += 0.2 if fp.vol_bucket == bucket_value(vol_spike, low=1.5, high=2.5) else 0.0
            sim += 0.2 if fp.clarity == clarity else 0.0
            if fp.intent == intent:
                sim += 0.1
            sim *= time_weight
            similarities.append((sim, price_dist, fp.reason))

        if not similarities:
            return {"risk": 1.0, "reason": None}
        max_sim = max(s[0] for s in similarities)
        if max_sim > 0.5:
            best_dist = min(s[1] for s in similarities if s[0] > 0.5)
            best_reason = next((s[2] for s in similarities if s[0] > 0.5 and s[1] == best_dist), None)
            if best_dist < 1.0:
                return {"risk": 0.7, "reason": best_reason or "similar failed setup (price near)"}
            elif best_dist < 2.0:
                return {"risk": 0.8, "reason": best_reason or "similar failed setup (price nearby)"}
            else:
                return {"risk": 0.9, "reason": best_reason or "similar failed setup (price distant)"}
        return {"risk": 1.0, "reason": None}

def compute_hidden_liquidity(coin: str, candles_5m: List[dict], delta_history: List[float],
                              oi_history: List[float]) -> Dict[str, Any]:
    if len(candles_5m) < 6 or len(delta_history) < 5:
        return {"score": 0, "side": "NONE", "persistence": 0}

    prices = [float(c['c']) for c in candles_5m[-5:]]
    price_move_pct = abs(prices[-1] - prices[-5]) / max(prices[-5], 0.01) * 100

    vols = [float(c['v']) * float(c['c']) for c in candles_5m[-5:]]
    delta_abs_sum = sum(abs(d) for d in delta_history[-5:])
    vol_median = np.median(vols) if vols else 1.0
    if vol_median == 0:
        return {"score": 0, "side": "NONE", "persistence": 0}
    delta_norm = delta_abs_sum / (vol_median * len(vols))
    delta_norm = np.clip(delta_norm, 0.002, 0.1)

    efficiency = price_move_pct / max(delta_norm, 0.001)
    if efficiency > 0.3:
        return {"score": 0, "side": "NONE", "persistence": 0}

    avg_vol = sum(vols[:-1]) / max(1, len(vols)-1)
    vol_ratio = vols[-1] / max(avg_vol, 0.01)
    if vol_ratio < 1.5:
        return {"score": 0, "side": "NONE", "persistence": 0}

    oi_start = oi_history[-5] if len(oi_history) >= 5 else oi_history[-1]
    oi_end = oi_history[-1]
    oi_trend = (oi_end - oi_start) / max(oi_start, 0.01) * 100 if oi_start > 0 else 0
    oi_component = max(0, 20 - abs(oi_trend) * 2)

    persistence = 0
    for i in range(1, min(6, len(candles_5m))):
        sub_prices = [float(c['c']) for c in candles_5m[-i-1:]]
        sub_move = abs(sub_prices[-1] - sub_prices[0]) / max(sub_prices[0], 0.01) * 100
        sub_vols = [float(c['v']) * float(c['c']) for c in candles_5m[-i-1:]]
        sub_avg = sum(sub_vols[:-1]) / max(1, len(sub_vols)-1)
        if sub_avg == 0:
            continue
        sub_vol_ratio = sub_vols[-1] / sub_avg
        if sub_move < 0.3 and sub_vol_ratio > 1.2:
            persistence += 1
        else:
            break

    delta_side = "BUYER" if delta_history[-1] > 0 else "SELLER"
    score = (
        35 * max(0, min(1, 1 - efficiency)) +
        25 * np.log1p(vol_ratio) +
        20 * min(1, persistence / 3) +
        20 * (oi_component / 20)
    )
    score = min(100, int(score))
    side = f"{delta_side}_ABSORBING" if score > 30 else "NONE"
    return {"score": int(score), "side": side, "persistence": persistence}

def compute_micro_acceptance(coin: str, event: TradeEvent, candles_5m: List[dict]) -> Dict[str, Any]:
    if not candles_5m or len(candles_5m) < 5:
        return {"score": None, "status": "INSUFFICIENT"}

    touched = False
    for i in range(len(candles_5m)-1, max(0, len(candles_5m)-8), -1):
        low, high = float(candles_5m[i]['l']), float(candles_5m[i]['h'])
        if event.direction == "LONG" and low <= event.price_low * 1.002:
            touched = True
            break
        if event.direction == "SHORT" and high >= event.price_high * 0.998:
            touched = True
            break

    if not touched:
        return {"score": None, "status": "UNTESTED"}

    touch_idx = None
    for i in range(len(candles_5m)-1, max(0, len(candles_5m)-8), -1):
        low, high = float(candles_5m[i]['l']), float(candles_5m[i]['h'])
        if event.direction == "LONG" and low <= event.price_low * 1.002:
            touch_idx = i
            break
        if event.direction == "SHORT" and high >= event.price_high * 0.998:
            touch_idx = i
            break

    if touch_idx is None:
        return {"score": None, "status": "PENDING"}

    if touch_idx >= len(candles_5m) - 3:
        return {"score": None, "status": "PENDING"}

    zone_low = event.price_low if event.direction == "LONG" else event.price_high * 0.99
    zone_high = event.price_high if event.direction == "SHORT" else event.price_low * 1.01

    inside_time = 0
    volume_inside = 0
    retest_count = 0
    rejection_count = 0
    total_candles_after = 0

    max_lookback = min(touch_idx + 5, len(candles_5m))
    for j in range(touch_idx+1, max_lookback):
        c = candles_5m[j]
        close = float(c['c'])
        low = float(c['l'])
        high = float(c['h'])
        total_candles_after += 1

        if event.direction == "LONG":
            if close > zone_low:
                inside_time += 1
            if low <= zone_low and close > zone_low:
                retest_count += 1
            if high > zone_high and close < zone_low:
                rejection_count += 1
        else:
            if close < zone_high:
                inside_time += 1
            if high >= zone_high and close < zone_high:
                retest_count += 1
            if low < zone_low and close > zone_high:
                rejection_count += 1

        if (event.direction == "LONG" and close > zone_low) or (event.direction == "SHORT" and close < zone_high):
            volume_inside += float(c['v']) * float(c['c'])

    if total_candles_after == 0:
        return {"score": None, "status": "PENDING"}

    time_score = min(30, inside_time * 6)
    vol_avg = sum(float(candles_5m[j]['v']) * float(candles_5m[j]['c']) for j in range(max(0, touch_idx-5), touch_idx)) / 5
    vol_score = min(40, (volume_inside / max(vol_avg, 1)) * 10) if vol_avg > 0 else 20
    retest_score = min(20, retest_count * 5)
    rejection_penalty = min(20, rejection_count * 5)
    score = max(0, min(100, time_score + vol_score + retest_score - rejection_penalty))

    if score > 60:
        status = "ACCEPTED"
    elif score < 30 and rejection_count > 0:
        status = "REJECTED"
    else:
        status = "MIXED"

    return {"score": score, "status": status}
    

def update_intent_vector(coin: str, event: TradeEvent, delta: float, vol_spike: float,
                          acceptance_score: float, context: ContextSnapshot):
    liquidity_score = 1.0 if event.type == "LIQUIDITY" else (0.5 if event.type in ("OB", "FVG") else 0.0)
    acceptance_norm = acceptance_score / 100.0 if acceptance_score else 0.5
    displacement_score = 1.0 if event.extra.get("displaced", False) else 0.0
    delta_normalized = np.tanh(delta / 10.0)

    regime_map = {"TRENDING_UP": 1.0, "RANGING": 0.0, "TRENDING_DOWN": -1.0}
    regime_val = regime_map.get(context.regime, 0.0)
    breadth_val = context.breath_bull

    vector = [
        liquidity_score,
        acceptance_norm,
        displacement_score,
        delta_normalized,
        regime_val,
        breadth_val
    ]

    with _intent_vector_lock:
        if coin not in _intent_vector_history:
            _intent_vector_history[coin] = deque(maxlen=10)
        _intent_vector_history[coin].append((time.time(), vector))

def compute_intent_drift(coin: str) -> float:
    with _intent_vector_lock:
        if coin not in _intent_vector_history or len(_intent_vector_history[coin]) < 4:
            return 0.0
        recent = list(_intent_vector_history[coin])
        now = time.time()
        new_vectors = [v for ts, v in recent if now - ts < 600]
        old_vectors = [v for ts, v in recent if now - ts >= 600 and now - ts < 3600]
        if len(new_vectors) < 2 or len(old_vectors) < 2:
            return 0.0
        new_avg = np.mean(new_vectors, axis=0)
        old_avg = np.mean(old_vectors, axis=0)
        dot = np.dot(new_avg, old_avg)
        norm_new = np.linalg.norm(new_avg)
        norm_old = np.linalg.norm(old_avg)
        if norm_new == 0 or norm_old == 0:
            return 0.0
        sim = dot / (norm_new * norm_old)
        raw_drift = min(1.0, max(0.0, 1.0 - sim))

        with _smoothed_drift_lock:
            prev = _smoothed_drift.get(coin, raw_drift)
            smoothed = 0.7 * prev + 0.3 * raw_drift
            _smoothed_drift[coin] = smoothed
            return smoothed

def log_decision_journal(entry: DecisionJournalEntry):
    with _journal_lock:
        _decision_journal.append(entry)
        if len(_decision_journal) > 2000:
            _decision_journal = _decision_journal[-2000:]

def get_decision_journal(coin: str = None, mode: str = None, limit: int = 100) -> List[DecisionJournalEntry]:
    with _journal_lock:
        result = _decision_journal
        if coin:
            result = [e for e in result if e.coin == coin]
        if mode:
            result = [e for e in result if e.mode == mode]
        return result[-limit:]

def auto_review():
    global _review_counter
    with _review_lock:
        _review_counter += 1
        if _review_counter % _AUTO_REVIEW_INTERVAL != 0:
            return

    with _journal_lock:
        entries = _decision_journal[-50:]

    if len(entries) < 20:
        return

    modes = {}
    for e in entries:
        mode = e.mode if e.mode else "UNKNOWN"
        if mode not in modes:
            modes[mode] = {"total": 0, "wins": 0, "pnl": [], "rr": [], "drifts": [], "executed": 0, "shadow": 0}
        modes[mode]["total"] += 1
        if e.outcome in ("TP_HIT", "PARTIAL_WIN"):
            modes[mode]["wins"] += 1
        if e.pnl is not None:
            modes[mode]["pnl"].append(e.pnl)
        if e.rr is not None:
            modes[mode]["rr"].append(e.rr)
        modes[mode]["drifts"].append(e.intent_drift)
        if e.executed:
            modes[mode]["executed"] += 1
        else:
            modes[mode]["shadow"] += 1

    text = f"📊 *DECISION REVIEW* (last {len(entries)})\n━━━━━━━━━━━━━━━━━━━━━━\n"

    reject_reasons = {}
    for e in entries:
        if not e.executed and e.narrative and "what_now" in e.narrative:
            reason = e.narrative["what_now"].split(":")[0] if ":" in e.narrative["what_now"] else e.narrative["what_now"][:30]
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
    if reject_reasons:
        top_reason = max(reject_reasons, key=reject_reasons.get)
        text += f"🚫 *Top reject reason*: {top_reason} ({reject_reasons[top_reason]}x)\n\n"

    for mode, stats in modes.items():
        if stats["total"] < 3:
            continue
        win_rate = stats["wins"] / max(1, stats["total"])
        avg_pnl = sum(stats["pnl"]) / max(1, len(stats["pnl"]))
        avg_rr = sum(stats["rr"]) / max(1, len(stats["rr"]))
        avg_drift = sum(stats["drifts"]) / max(1, len(stats["drifts"]))
        bar = "█" * int(win_rate * 10) + "░" * (10 - int(win_rate * 10))

        emoji = "🟢" if win_rate > 0.55 else "🟡" if win_rate > 0.45 else "🔴"
        text += f"{emoji} *{mode}*\n"
        text += f"├─ Total: {stats['total']} | Exec: {stats['executed']} | Shadow: {stats['shadow']}\n"
        text += f"├─ WR: {win_rate*100:.0f}% [{bar}] | AvgRR: {avg_rr:.2f}\n"
        text += f"├─ AvgPnL: {avg_pnl:+.2f}% | AvgDrift: {avg_drift:.2f}\n\n"

    total_executed = sum(s["executed"] for s in modes.values())
    total_shadow = sum(s["shadow"] for s in modes.values())
    text += f"📌 Summary: {total_executed} executed, {total_shadow} shadow avoided"

    try:
        bot.send_message(USER_ID, text, parse_mode='Markdown')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Auto review send error: {e}")

def snapshot_metrics() -> Dict[str, Any]:
    with _journal_lock:
        journal_size = len(_decision_journal)

    with _failed_lock:
        failed_memory = sum(len(v) for v in _failed_memory.values())

    with _smoothed_drift_lock:
        avg_drift = np.mean(list(_smoothed_drift.values())) if _smoothed_drift else 0.0

    with _journal_lock:
        recent = _decision_journal[-100:]
        if recent:
            shadows = sum(1 for e in recent if not e.executed)
            discovery_ratio = shadows / len(recent)
        else:
            discovery_ratio = 0.0

    now = time.time()
    with _journal_lock:
        last_24h = [e for e in _decision_journal if now - e.timestamp < 86400]

    return {
        "journal_size": journal_size,
        "failed_memory": failed_memory,
        "avg_drift": round(avg_drift, 3),
        "discovery_ratio": round(discovery_ratio, 3),
        "last_24h_decisions": len(last_24h),
        "timestamp": now
    }

def log_snapshot_metrics():
    while RUNTIME.is_running():
        metrics = snapshot_metrics()
        logger.info(f"📊 Metrics: journal={metrics['journal_size']}, "
                   f"failed_mem={metrics['failed_memory']}, "
                   f"avg_drift={metrics['avg_drift']:.2f}, "
                   f"disc_ratio={metrics['discovery_ratio']:.2f}, "
                   f"24h_dec={metrics['last_24h_decisions']}")

        if metrics["discovery_ratio"] > 0.4:
            logger.warning(f"⚠️ High discovery ratio: {metrics['discovery_ratio']:.2f} (too many shadows)")

        RUNTIME.wait(1800)
    
# ============================================================
# PART 26 – EXECUTION MODE BLEND + FATIGUE + FILTER GRADIENT
# ============================================================

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

# ========== FILTER GRADIENT ==========
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
    
# ============================================================
# PART 27 – INTENT ENGINE + WHY NOT + POSITION SIZE + THESIS GENERATOR
# ============================================================

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
    confidence_gain = min(20.0, setup_age_minutes * 2.0)
    future_confidence = min(100.0, current_confidence + confidence_gain)
    opportunity_decay = 1.0 - (setup_age_minutes * 0.08)
    future_opportunity = current_opportunity * max(0.1, opportunity_decay)
    uncertainty_decay = max(0.5, 1.0 - (setup_age_minutes * 0.01))
    future_uncertainty = current_uncertainty * uncertainty_decay
    relevance_prob = max(0.1, 1.0 - (setup_age_minutes * 0.05))
    expected_decay = max(0.1, 1.0 - (setup_age_minutes * 0.03))
    if future_uncertainty <= 0:
        future_uncertainty = 0.01
    raw_wait_value = (future_confidence * future_opportunity) / future_uncertainty
    wait_value = raw_wait_value * relevance_prob * expected_decay
    max_wait_reached = setup_age_minutes > 30
    if max_wait_reached:
        wait_value = 0.0
    return wait_value, expected_decay, max_wait_reached

def should_wait_or_execute_v5(current_value: float, wait_value: float, decision_energy: float) -> Tuple[bool, str, float]:
    threshold = 0.85
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
                  
              
# ============================================================
# PART 28 – EVENT DETECTION (LIQUIDITY, OB, FVG)
# ============================================================

def validate_ob_with_volume_oi(coin, ob_idx, master_candles) -> bool:
    try:
        candles = get_candles(coin, "1h", 100, master_candles)
        if ob_idx + 1 >= len(candles):
            return False
        imp_candle = candles[ob_idx + 1]
        imp_vol = float(imp_candle['v']) * float(imp_candle['c'])
        prev_vols = [float(candles[i]['v']) * float(candles[i]['c']) for i in range(max(0, ob_idx - 5), ob_idx)]
        avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 1
        volume_ok = (imp_vol / avg_vol) >= 1.5
        oi_persist, oi_trend = get_oi_persistence(coin)
        oi_change = get_oi_roc(coin)
        return volume_ok and (oi_persist or oi_change >= 3.0)
    except:
        return False

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
        return TradeEvent("LIQUIDITY", highs[-1][1] * 0.999, highs[-1][1] * 1.001, 80, "SHORT",
                          {"displaced": displaced}, confidence=conf, source_count=1)
    if lows and current_price <= lows[-1][1] * 1.002 and vol_spike > 1.5:
        displaced = len(candles) >= 2 and detect_displacement(candles[-2], candles[-1])
        conf = 70 + (10 if vol_spike > 2 else 0) + (10 if displaced else 0)
        return TradeEvent("LIQUIDITY", lows[-1][1] * 0.999, lows[-1][1] * 1.001, 80, "LONG",
                          {"displaced": displaced}, confidence=conf, source_count=1)
    return None

def find_ob(candles, direction, current_price, max_dist_pct=2.0, master=None, coin=None) -> Optional[TradeEvent]:
    for i in range(len(candles) - 3, 1, -1):
        c, nxt = candles[i], candles[i + 1]
        o, cl, no, nc = float(c['o']), float(c['c']), float(nxt['o']), float(nxt['c'])
        if direction == "LONG" and cl < o and nc > no and nc > float(c['h']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i + 2, len(candles) - 1):
                if float(candles[j]['c']) < ob_low:
                    fresh = False
                    break
            if fresh:
                mid = (ob_low + ob_high) / 2
                dist = abs(mid - current_price) / max(current_price, 0.01) * 100
                if dist <= max_dist_pct and validate_ob_with_volume_oi(coin, i, master):
                    return TradeEvent("OB", ob_low, ob_high, 75, "LONG", {"idx": i}, confidence=70, source_count=1)
        if direction == "SHORT" and cl > o and nc < no and nc < float(c['l']):
            ob_low, ob_high = float(c['l']), float(c['h'])
            fresh = True
            for j in range(i + 2, len(candles) - 1):
                if float(candles[j]['c']) > ob_high:
                    fresh = False
                    break
            if fresh:
                mid = (ob_low + ob_high) / 2
                dist = abs(mid - current_price) / max(current_price, 0.01) * 100
                if dist <= max_dist_pct and validate_ob_with_volume_oi(coin, i, master):
                    return TradeEvent("OB", ob_low, ob_high, 75, "SHORT", {"idx": i}, confidence=70, source_count=1)
    return None

def find_fvg_advanced(candles, current_price, max_dist_pct=2.0, master=None, coin=None) -> Optional[TradeEvent]:
    for i in range(len(candles) - 1, 1, -1):
        c1, c3 = candles[i - 2], candles[i]
        c1h, c1l, c3h, c3l = float(c1['h']), float(c1['l']), float(c3['h']), float(c3['l'])

        if c3l > c1h:
            gap_low, gap_high = c1h, c3l
            gap_pct = (gap_high - gap_low) / max(gap_low, 0.01) * 100
            if gap_pct < 0.15:
                continue
            filled = 0.0
            first_fill_time = None
            now_ts = time.time()
            for j in range(i + 1, len(candles) - 1):
                close = float(candles[j]['c'])
                if close <= gap_low:
                    filled = 1.0
                    first_fill_time = candles[j].get('t', 0) / 1000
                    break
                elif close < gap_high:
                    filled = max(filled, (close - gap_low) / (gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low + gap_high) / 2
                dist = abs(mid - current_price) / max(current_price, 0.01) * 100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bullish", "idx": i, "gap_low": gap_low, "gap_high": gap_high}
                    valid, mult = validate_fvg_with_fill_speed(coin, fvg_data, candles)
                    if valid:
                        strength = int(65 * mult) if mult else 65
                        if gap_pct > 0.3:
                            strength = min(100, strength + 10)
                        conf = 55 + (10 if gap_pct > 0.3 else 0) + (15 if filled < 0.3 else 0)
                        if mult > 1.2:
                            conf = min(100, conf + 10)
                        event = TradeEvent("FVG", gap_low, gap_high, strength, "LONG",
                                           {"fill_ratio": filled, "idx": i}, confidence=conf, source_count=1)
                        event.fill_ratio = filled
                        if first_fill_time:
                            event.fill_time_minutes = (now_ts - first_fill_time) / 60
                        return event

        if c3h < c1l:
            gap_low, gap_high = c3h, c1l
            gap_pct = (gap_high - gap_low) / max(gap_low, 0.01) * 100
            if gap_pct < 0.15:
                continue
            filled = 0.0
            first_fill_time = None
            now_ts = time.time()
            for j in range(i + 1, len(candles) - 1):
                close = float(candles[j]['c'])
                if close >= gap_high:
                    filled = 1.0
                    first_fill_time = candles[j].get('t', 0) / 1000
                    break
                elif close > gap_low:
                    filled = max(filled, (gap_high - close) / (gap_high - gap_low))
            if filled < 0.7:
                mid = (gap_low + gap_high) / 2
                dist = abs(mid - current_price) / max(current_price, 0.01) * 100
                if dist <= max_dist_pct:
                    fvg_data = {"type": "bearish", "idx": i, "gap_low": gap_low, "gap_high": gap_high}
                    valid, mult = validate_fvg_with_fill_speed(coin, fvg_data, candles)
                    if valid:
                        strength = int(65 * mult) if mult else 65
                        if gap_pct > 0.3:
                            strength = min(100, strength + 10)
                        conf = 55 + (10 if gap_pct > 0.3 else 0) + (15 if filled < 0.3 else 0)
                        if mult > 1.2:
                            conf = min(100, conf + 10)
                        event = TradeEvent("FVG", gap_low, gap_high, strength, "SHORT",
                                           {"fill_ratio": filled, "idx": i}, confidence=conf, source_count=1)
                        event.fill_ratio = filled
                        if first_fill_time:
                            event.fill_time_minutes = (now_ts - first_fill_time) / 60
                        return event
    return None
    
# ============================================================
# PART 29 – EVENT DETECTION (OB_FLOW, FVG_FLOW, VACUUM, CLUSTER)
# ============================================================

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

def find_ob_from_orderbook(coin: str, current_price: float, master: Dict) -> Optional[TradeEvent]:
    try:
        delta_shift = get_delta_shift(coin)
        bid_wall, bid_price = get_bid_wall_level(coin)
        if bid_wall >= TUNABLE["MIN_OB_FLOW_WALL_USD"] and delta_shift > TUNABLE["MIN_OB_FLOW_DELTA_SHIFT"]:
            if current_price <= bid_price * 1.005:
                conf = min(85, 70 + int(delta_shift / 2))
                return TradeEvent("OB_FLOW", bid_price * 0.998, bid_price * 1.002, 75, "LONG",
                                  {"wall_usd": bid_wall, "delta_shift": delta_shift}, confidence=conf, source_count=1)
        ask_wall, ask_price = get_ask_wall_level(coin)
        if ask_wall >= TUNABLE["MIN_OB_FLOW_WALL_USD"] and delta_shift < -TUNABLE["MIN_OB_FLOW_DELTA_SHIFT"]:
            if current_price >= ask_price * 0.995:
                conf = min(85, 70 + int(abs(delta_shift) / 2))
                return TradeEvent("OB_FLOW", ask_price * 0.998, ask_price * 1.002, 75, "SHORT",
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
                              {"severity": severity, "depth_drop_pct": (1 - severity / 100) * 100}, confidence=conf, source_count=1)
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
        for j in range(i + 1, len(events)):
            if used[j]:
                continue
            if events[j].direction != e.direction:
                continue
            if max(e.price_low, events[j].price_low) <= min(e.price_high, events[j].price_high) * (1 + price_tolerance):
                cluster.append(events[j])
                used[j] = True
        avg_strength = sum(ev.strength for ev in cluster) / len(cluster)
        low = min(ev.price_low for ev in cluster)
        high = max(ev.price_high for ev in cluster)
        avg_conf = sum(ev.confidence for ev in cluster) / len(cluster)
        cluster_event = TradeEvent(
            type="CLUSTER",
            price_low=low, price_high=high,
            strength=min(100, avg_strength + 10 * (len(cluster) - 1)),
            direction=e.direction,
            extra={"members": [ev.type for ev in cluster], "count": len(cluster)},
            confidence=min(100, avg_conf + 5 * (len(cluster) - 1)),
            source_count=len(cluster)
        )
        clusters.append(cluster_event)
    return clusters
    
    
# ============================================================
# PART 30 – SCORING + MOMENTUM + SL/TP
# ============================================================

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

    if event.type == "FVG" and event.fill_ratio < 0.3 and event.fill_time_minutes < 10:
        base += 15
        reasons.append("fast_fvg_fill")

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
    
# ============================================================
# PART 31 – LAYER 1: OBSERVE MARKET
# ============================================================
def observe_market(coin: str, mark: float, master_candles: Dict) -> Optional[Dict]:
    """Layer 1: Kumpulkan semua data dan event"""
    
    # ===== STALE MODE CHECK =====
    snapshot = get_snapshot()
    snapshot_age = time.time() - snapshot.timestamp if snapshot else 999
    
    stale_mode = False
    if snapshot_age > 60:
        logger.debug(f"⚠️ Stale mode: snapshot age {snapshot_age:.1f}s")
        stale_mode = True
    if snapshot_age > 180:
        logger.warning(f"🔴 Degraded mode: snapshot age {snapshot_age:.1f}s, skipping new entries")
        return None
    
    data_confidence, ages = get_data_confidence(coin, time.time())
    if stale_mode:
        
        data_confidence = int(data_confidence * 0.8)
    
    if data_confidence < ENGINE_CONSTANTS["MIN_DATA_CONFIDENCE"]:
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

    context = get_context_with_confidence(coin, 50.0)

    for ev in clustered:
        ev.extra["coin"] = coin
        ev.score, _ = score_event_non_additive(
            ev, mark, delta, vol_spike, oi_roc,
            (structure_valid_long if ev.direction == "LONG" else structure_valid_short),
            cvd_accel, momentum
        )
        penalty = get_zone_penalty_v8(coin, ev.type, ev.price_low, ev.price_high)
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
        "master_candles": master_candles, "context": context
    }
    
# ============================================================
# PART 32 – LAYER 2: BUILD THESIS (dengan Macro Hierarchy V10)
# ============================================================

def build_thesis(obs: Dict) -> Optional[Dict]:
    """Layer 2: Dari event ke thesis dengan macro inheritance V10"""
    coin = obs["coin"]
    event = obs["best_event"]
    mark = obs["mark"]

    bias_4h, bias_strength, bias_stability = get_bias_4h_advanced(coin)

    if obs["market_state"] == MarketState.REVERSAL:
        if event.type != "LIQUIDITY" and "LIQUIDITY" not in event.extra.get("members", []):
            return None
    elif obs["market_state"] == MarketState.EXPANSION:
        if event.type == "LIQUIDITY" or "LIQUIDITY" in event.extra.get("members", []):
            return None

    if event.direction == "LONG" and not obs["structure_valid_long"]:
        return None
    if event.direction == "SHORT" and not obs["structure_valid_short"]:
        return None

    if bias_4h == "BEARISH" and event.direction == "LONG":
        downgrade = 20 if bias_strength > 70 else 10
        if bias_stability < 0.4:
            downgrade = int(downgrade * 0.6)
        logger.debug(f"{coin}: 4H BEARISH (str:{bias_strength:.0f}, stab:{bias_stability:.2f}), LONG downgraded")
        # BUAT EVENT BARU, JANGAN MUTATE YANG LAMA
        event = TradeEvent(
            type=event.type,
            price_low=event.price_low,
            price_high=event.price_high,
            strength=max(30, event.strength - downgrade),
            direction=event.direction,
            extra=event.extra.copy() if event.extra else {},
            confidence=max(30, event.confidence - downgrade * 0.75),
            source_count=event.source_count,
            first_seen=event.first_seen,
            fill_ratio=getattr(event, 'fill_ratio', 0.0),
            fill_time_minutes=getattr(event, 'fill_time_minutes', 0.0)
        )
    elif bias_4h == "BULLISH" and event.direction == "SHORT":
        downgrade = 20 if bias_strength > 70 else 10
        if bias_stability < 0.4:
            downgrade = int(downgrade * 0.6)
        logger.debug(f"{coin}: 4H BULLISH (str:{bias_strength:.0f}, stab:{bias_stability:.2f}), SHORT downgraded")
        # BUAT EVENT BARU, JANGAN MUTATE YANG LAMA
        event = TradeEvent(
            type=event.type,
            price_low=event.price_low,
            price_high=event.price_high,
            strength=max(30, event.strength - downgrade),
            direction=event.direction,
            extra=event.extra.copy() if event.extra else {},
            confidence=max(30, event.confidence - downgrade * 0.75),
            source_count=event.source_count,
            first_seen=event.first_seen,
            fill_ratio=getattr(event, 'fill_ratio', 0.0),
            fill_time_minutes=getattr(event, 'fill_time_minutes', 0.0)
        )

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

    rejection = compute_rejection_strength(coin, event, mark, obs["master_candles"])
    acceptance = compute_acceptance_strength(coin, event, obs["master_candles"])
    persistence = compute_persistence_strength(coin, event, obs["master_candles"])
    filter_score = compute_filter_score(rejection, acceptance, persistence,
                                         obs["volatility_regime"], obs["market_regime"])

    fatigue_penalty = get_fatigue_penalty_by_family(event.type)

    belief, belief_score, belief_reason = compute_belief(
        event, filter_score, obs["structure_valid_long"], obs["structure_valid_short"], 0.0
    )

    if bias_4h == "BEARISH" and event.direction == "LONG":
        factor = 0.7 if bias_strength > 70 else 0.85
        if bias_stability < 0.4:
            factor = min(0.95, factor + 0.15)
        belief_score *= factor
        if belief == BeliefState.CONVICTED:
            belief = BeliefState.BUILDING
    elif bias_4h == "BULLISH" and event.direction == "SHORT":
        factor = 0.7 if bias_strength > 70 else 0.85
        if bias_stability < 0.4:
            factor = min(0.95, factor + 0.15)
        belief_score *= factor
        if belief == BeliefState.CONVICTED:
            belief = BeliefState.BUILDING

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
        "clustered": obs["clustered"], "ages": obs["ages"],
        "context": obs.get("context"),
        "bias_4h": bias_4h, "bias_strength": bias_strength, "bias_stability": bias_stability
    }
    
# ============================================================
# PART 33 – LAYER 3: COMPUTE CONFIDENCE + HELPER FUNCTIONS
# ============================================================

def compute_confidence(thesis_data: Dict) -> Optional[Dict]:
    coin = thesis_data["coin"]
    event = thesis_data["event"]
    clustered = thesis_data["clustered"]

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

    price_ok, flow_ok, pos_ok, evidence_reasons = get_independent_evidence_families(
        coin, event.direction, thesis_data["master_candles"]
    )
    evidence_families = (1 if price_ok else 0) + (1 if flow_ok else 0) + (1 if pos_ok else 0)
    exhaustion = compute_exhaustion_score(coin, thesis_data["master_candles"])

    entropy_data = compute_data_entropy(thesis_data["ages"])
    entropy_market = compute_market_entropy_v7(coin, thesis_data["master_candles"])
    score_variance = abs(score_long - score_short) if score_long > 0 and score_short > 0 else 0
    event_types = [ev.type for ev in clustered]
    entropy_decision = compute_decision_entropy(score_variance, contradiction, len(event_types) > 2, event_types)

    trend_strength = compute_trend_strength_v7(coin, thesis_data["master_candles"])

    decision_score, ev_mult, _, contributions = compute_decision_vector(
        coin, event, score_long, score_short, evidence_families, entropy_market, exhaustion,
        thesis_data["market_regime"], thesis_data["volatility_regime"], thesis_data["data_confidence"]
    )

    cf_adjusted_score, _ = evaluate_counterfactual_influence(
        coin, entropy_market, evidence_families, exhaustion, decision_score, thesis_data["data_confidence"]
    )
    final_score = decision_score

    confidence = compute_confidence_from_score(final_score, thesis_data["data_confidence"], evidence_families)

    sl, tp, rr = calculate_sltp_advanced(coin, thesis_data["mark"], event.direction, event,
                                         thesis_data["atr_pct"], thesis_data["master_candles"])
    min_rr = get_dynamic_min_rr(thesis_data["market_regime"])
    min_rr = get_entropy_adjusted_min_rr(min_rr, entropy_market)
    if rr < min_rr:
        return None

    opportunity = compute_opportunity(rr, thesis_data["vol_spike"], thesis_data["momentum"])
    uncertainty = compute_uncertainty(entropy_market, entropy_decision, contradiction, exhaustion)

    decision_energy = compute_decision_energy_v7(confidence, opportunity, uncertainty)
    update_decision_energy_history(coin, decision_energy)
    decision_acceleration = compute_decision_acceleration(coin)

    setup_age_minutes = (time.time() - event.first_seen) / 60
    competitor_count = len(_active_candidates)
    time_pressure, urgency_score = compute_time_pressure(setup_age_minutes, competitor_count)

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

# ========== HELPER FUNCTIONS UNTUK CONFIDENCE ==========
def compute_decision_vector(coin: str, event: TradeEvent, score_long: int, score_short: int,
                            evidence_families: int, entropy: int, exhaustion: int,
                            market_regime: str, volatility_regime: str, data_confidence: int) -> Tuple[int, float, str, Dict[str, int]]:
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        _base_mults = {3: 1.0, 2: 0.75, 1: 0.45}
    elif market_regime in ("PANIC", "VOLATILE"):
        _base_mults = {3: 0.85, 2: 0.6, 1: 0.35}
    else:
        _base_mults = {3: ENGINE_CONSTANTS["EVIDENCE_MULT_3"], 2: ENGINE_CONSTANTS["EVIDENCE_MULT_2"], 1: ENGINE_CONSTANTS["EVIDENCE_MULT_1"]}

    ev_mult = _base_mults.get(min(evidence_families, 3), ENGINE_CONSTANTS["EVIDENCE_MULT_1"])
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
                                       entropy: int, threshold: int, data_confidence: int,
                                       execution_mode: ExecutionMode, intent_success: float) -> str:
    pos_str = ", ".join(positive_factors[:3]) if positive_factors else "none"
    neg_str = ", ".join(negative_factors[:3]) if negative_factors else "none"
    contrib_str = " | ".join([f"{k}:{v:+d}" for k, v in contributions.items()]) if contributions else "none"
    mode_emoji = get_mode_emoji(execution_mode)
    return (f"📊 *Decision Explanation*\n"
            f"✅ Positive: {pos_str}\n"
            f"❌ Negative: {neg_str}\n"
            f"📈 Contribution: {contrib_str}\n"
            f"🌀 Market Entropy: {entropy}\n"
            f"📡 Data confidence: {data_confidence}%\n"
            f"🎯 Final score: {score}\n"
            f"{mode_emoji} Mode: {execution_mode.value.upper()} | Intent success: {intent_success*100:.0f}%")
            
            
# ============================================================
# PART 34 – LAYER 4: EXECUTE DECISION (V10)
# ============================================================

def execute_decision(coin: str, thesis_data: Dict, confidence_data: Dict,
                      event: TradeEvent, intent, intent_legacy,
                      context: ContextSnapshot, breath: Dict[str, float]) -> Optional[dict]:
    mark = thesis_data["mark"]
    belief = thesis_data["current_belief"]
    filter_score = thesis_data["filter_score"]
    fatigue_penalty = thesis_data["fatigue_penalty"]

    # ===== V10: AMBIL CONTEXT DENGAN CONFIDENCE-WEIGHTED CACHE =====
    context = thesis_data.get("context")
    if not context:
        context = get_context_with_confidence(coin, confidence_data.get("confidence", 50.0))

    # V10: Context age tracking (context drift)
    context_age = time.time() - context.timestamp
    if context_age > TUNABLE["CONTEXT_STALE_THRESHOLD"]:
        logger.warning(f"Context stale {context_age:.1f}s for {coin}, refreshing...")
        context = get_context_snapshot(coin)
        context_age = time.time() - context.timestamp

    # ===== CLARITY CHECK =====
    clarity = compute_clarity(
        context,
        breath,
        confidence_data["score_long"],
        confidence_data["score_short"],
        context.transition_prob
    )
    if clarity["decision_quality"] < 0.6:   # threshold, bisa disesuaikan
        logger.debug(f"{coin}: low clarity {clarity['decision_quality']:.2f} (dominant: {clarity['dominant_factor']}), skipping")
        update_fatigue_memory(event.type)
        return None

    # Simpan clarity untuk log
    confidence_data["clarity"] = clarity

    # ===== V10: EVENT RISK ADJUSTMENT =====
    event_adjust = get_event_risk_adjustment()

    # ===== V10: REACTION ENGINE =====
    snapshot = get_snapshot()
    btc_move = 0.0
    if snapshot and "BTC" in snapshot.mids:
        with _last_mids_lock:
            if "BTC" in _last_mids:
                prev_price, prev_ts = _last_mids["BTC"]
                current_price = snapshot.mids["BTC"]
                if prev_price > 0 and prev_ts > time.time() - 300:
                    btc_move = (current_price - prev_price) / prev_price * 100

    vol_spike = get_volume_spike("BTC")

    latest_event = None
    with _event_risk_lock:
        if _EVENT_RISK_DATA:
            latest_event = max(_EVENT_RISK_DATA, key=lambda e: e.ts)

    if latest_event and abs(btc_move) > 0.3:
        reaction = compute_reaction(latest_event, btc_move, vol_spike)
        update_reaction_history(reaction)

    reaction_adj = get_reaction_adjustment()

    # ===== V10: INTENT MEMORY =====
    intent_success = get_intent_success_rate(coin, intent.value)

    # ===== V10: 5 EXECUTION MODE =====
    exec_mode, mode_adj = get_execution_mode_v10(context, get_current_reaction(), intent_success, event_adjust)

    # ===== V10: APPLY MODE ADJUSTMENTS =====
    final_threshold = confidence_data.get("final_threshold", 75)
    final_threshold = int(final_threshold * mode_adj["threshold"])

    position_size_mult = confidence_data.get("position_size_mult", 1.0)
    position_size_mult *= mode_adj["size"]

    # ===== V10: EVENT RISK ADJUSTMENT =====
    if event_adjust.get("importance", 0) > TUNABLE["EVENT_IMPORTANCE_HIGH"]:
        position_size_mult *= 0.6
        final_threshold = int(final_threshold * 1.2)
    elif event_adjust.get("importance", 0) > TUNABLE["EVENT_IMPORTANCE_MEDIUM"]:
        position_size_mult *= 0.8
        final_threshold = int(final_threshold * 1.1)

    if event_adjust.get("bias", 0) > 20 and event.direction == "LONG":
        final_threshold = int(final_threshold * 0.95)
        position_size_mult *= 1.1
    elif event_adjust.get("bias", 0) < -20 and event.direction == "SHORT":
        final_threshold = int(final_threshold * 0.95)
        position_size_mult *= 1.1

    # ===== V10: REACTION ADJUSTMENT =====
    if reaction_adj.get("mode") == "AGGRESSIVE":
        final_threshold = int(final_threshold * 0.85)
        position_size_mult *= 1.2
    elif reaction_adj.get("mode") == "DEFENSIVE":
        final_threshold = int(final_threshold * 1.15)
        position_size_mult *= 0.7
    elif reaction_adj.get("mode") == "PREPARE":
        final_threshold = int(final_threshold * 0.95)

    # ===== V10: INTENT MEMORY ADJUSTMENT =====
    if intent_success > 0.7:
        final_threshold = int(final_threshold * 0.9)
        position_size_mult *= 1.15
    elif intent_success < 0.3:
        final_threshold = int(final_threshold * 1.15)
        position_size_mult *= 0.7

    # ===== V10: REGIME INERTIA =====
    regime, inertia_penalty = get_regime_with_inertia(coin)
    if inertia_penalty > 15:
        final_threshold = int(final_threshold * (1 + inertia_penalty / 100))

    # ===== V10: BREATH ADJUSTMENT (Advanced) =====
    breath_v10 = compute_market_breath_v10()
    if breath_v10.get("participation", 0.5) < 0.4 and coin != "BTC":
        position_size_mult *= 0.7
    if breath_v10.get("leadership", 0) > 2 and coin != "BTC" and event.direction == "LONG":
        position_size_mult *= 1.1
    if breath_v10.get("rotation", 0) > 1 and coin not in ["BTC", "ETH"]:
        position_size_mult *= 1.15

    # Legacy breath filter (tetap jalan)
    if context.breath_bull < TUNABLE["BREATH_WEAK_THRESHOLD"] and coin != "BTC" and event.direction == "LONG":
        position_size_mult *= 0.6
    if context.breath_bear < TUNABLE["BREATH_WEAK_THRESHOLD"] and coin != "BTC" and event.direction == "SHORT":
        position_size_mult *= 0.6

    # ===== EXECUTION MODE BLEND (V7) =====
    if context.shock_score > TUNABLE["SHOCK_AGGRESSIVE_THRESHOLD"] and exec_mode != ExecutionMode.DEFENSIVE:
        blend_weights = {"aggressive": 1.0, "balanced": 0.0, "precision": 0.0}
    else:
        blend_weights = get_execution_mode_blend(
            confidence_data["decision_energy"], confidence_data["entropy_market"],
            confidence_data["decision_acceleration"], intent_legacy
        )

    execution_mode_str = get_execution_mode_from_blend(blend_weights)
    threshold_boost = get_mode_threshold_boost(blend_weights)

    # ===== DYNAMIC THRESHOLD =====
    base_threshold = get_dynamic_threshold(coin, thesis_data["market_regime"], thesis_data["volatility_regime"])
    entropy_adjusted_threshold = get_entropy_adjusted_threshold(base_threshold, confidence_data["entropy_market"])

    filter_penalty = 1.0 + ((100 - filter_score) / 100) * 0.5
    adjusted_threshold = int(entropy_adjusted_threshold * threshold_boost * filter_penalty)

    size_boost = 1.0 + (1.0 - confidence_data["position_size_mult"]) * 0.2
    final_threshold = min(85, max(50, int(adjusted_threshold / max(size_boost, 0.1))))

    # ===== COMMITMENT SCORE =====
    commitment_score = compute_commitment_score(
        belief, confidence_data["confidence"], confidence_data["time_pressure"],
        position_size_mult, confidence_data["prediction_quality_mult"]
    )

    # ===== COST OF WAITING =====
    wait_value, _, _ = compute_value_of_waiting_v5(
        confidence_data["confidence"], confidence_data["opportunity"],
        confidence_data["uncertainty"], confidence_data["setup_age_minutes"]
    )
    should_execute, wait_reason, _ = should_wait_or_execute_v5(
        confidence_data["decision_energy"], wait_value, confidence_data["decision_energy"]
    )

    # ===== THRESHOLD CHECK =====
    if confidence_data["final_score"] < final_threshold:
        if position_size_mult > 0.3:
            position_size_mult = max(0.15, position_size_mult * 0.7)
        else:
            update_fatigue_memory(event.type)
            return None

    # ===== GENERATE THESIS =====
    thesis_obj = generate_thesis_from_event_v7(coin, event, mark, thesis_data["market_state"], intent, belief)

    # ===== NEGATIVE EVIDENCE =====
    negative_reasons = []
    if not confidence_data["price_ok"]:
        negative_reasons.append("price")
    if not confidence_data["flow_ok"]:
        negative_reasons.append("flow")
    if not confidence_data["pos_ok"]:
        negative_reasons.append("positioning")
    negative_str = ", ".join(negative_reasons) if negative_reasons else "none"

    # ===== WHY NOT =====
    active_count = len(_active_candidates)
    why_not = generate_why_not(thesis_data["funding_pct"], confidence_data["entropy_market"],
                               thesis_data["oi_roc"], intent, active_count, fatigue_penalty)

    if intent_success < 0.4:
        why_not += f" | intent success {intent_success*100:.0f}%"

    confidence_breakdown = f"S:{confidence_data['score_long']:.0f}|F:{filter_score:.0f}|E:{confidence_data['evidence_families']}"

    reason = (f"{event.type} | Intent:{intent.value} | Belief:{belief.value} | "
              f"Mode:{execution_mode_str} | V10 Mode:{exec_mode.value.upper()} | "
              f"Filter:{filter_score:.0f} | DE:{confidence_data['decision_energy']:.1f} | Score:{confidence_data['final_score']}")

    signal_id = generate_signal_id(coin, event.direction)
    eval_delay = get_evaluation_delay(thesis_data["atr_pct"], confidence_data["rr"], thesis_data["market_regime"])

    # ===== SAVE =====
    if not PAPER_MODE:
        save_signal_v7(signal_id, coin, event.direction, confidence_data["final_score"], mark,
                      confidence_data["sl"], confidence_data["tp"], confidence_data["rr"], reason,
                      thesis_data["data_confidence"], thesis_obj.statement, thesis_obj.invalidation,
                      thesis_obj.confirmation, execution_mode_str, intent.value,
                      confidence_data["decision_energy"], position_size_mult,
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
                            execution_mode=execution_mode_str, intent_type=intent.value,
                            decision_energy=confidence_data["decision_energy"],
                            position_size_mult=position_size_mult,
                            filter_score=filter_score, rejection_strength=thesis_data["rejection"],
                            acceptance_strength=thesis_data["acceptance"],
                            persistence_strength=thesis_data["persistence"],
                            why_not=why_not, wait_value=wait_value,
                            time_pressure=confidence_data["time_pressure"].value,
                            commitment_score=commitment_score,
                            decision_acceleration=confidence_data["decision_acceleration"],
                            mode_aggressive=blend_weights["aggressive"],
                            mode_balanced=blend_weights["balanced"],
                            mode_precision=blend_weights["precision"],
                            confidence_breakdown=confidence_breakdown)

        _EVAL_EXECUTOR.submit(evaluate_signal_v7, signal_id, coin, event.direction, mark,
                              confidence_data["sl"], confidence_data["tp"], thesis_data["data_confidence"],
                              confidence_data["entropy_market"], confidence_data["evidence_families"],
                              confidence_data["exhaustion"], thesis_obj.statement,
                              thesis_obj.invalidation, thesis_obj.confirmation, eval_delay,
                              event.price_low, event.price_high, event.direction)

    update_active_candidate_v7(coin, mark, confidence_data["entropy_market"], mark)

    # ===== POSITIVE FACTORS =====
    positive_factors = [event.type] + confidence_data["evidence_reasons"]
    if thesis_data["vol_spike"] >= 1.5:
        positive_factors.append("volume")
    if thesis_data["cvd_accel"]:
        positive_factors.append("cvd_accel")
    if event_adjust.get("bias", 0) > 20:
        positive_factors.append("event_bias_bullish")
    elif event_adjust.get("bias", 0) < -20:
        positive_factors.append("event_bias_bearish")

    # ===== EXPLANATION =====
    explanation = explain_decision_with_contribution(
        coin, event.direction, confidence_data["final_score"],
        positive_factors, negative_reasons, confidence_data["contributions"],
        confidence_data["entropy_market"], final_threshold, thesis_data["data_confidence"],
        exec_mode, intent_success
    )
    explanation += f"\n🧼 *Clarity*: {clarity['decision_quality']:.2f} (dom: {clarity['dominant_factor']})"
    # ===== ADVANCED METRICS =====
    # Hidden liquidity
    candles_5m = get_candles(coin, "5m", 20, thesis_data["master_candles"])
    delta_history = list(_rolling_delta.get(coin, []))[-5:]
    oi_history = [v for ts, v in _oi_history.get(coin, [])[-5:]]
    hl = compute_hidden_liquidity(coin, candles_5m, delta_history, oi_history) if candles_5m else {"score": 0, "side": "NONE"}

    # Micro acceptance
    micro_acc = compute_micro_acceptance(coin, event, candles_5m) if candles_5m else {"score": None, "status": "INSUFFICIENT"}

    # Failed move risk
    clarity_str = "UNCLEAR" if confidence_data["entropy_market"] > 50 else "CLEAR"
    failed_risk = get_failed_move_risk(
    coin, event.type, thesis_data["delta"], thesis_data["vol_spike"],
    clarity_str, intent.value, event.direction, mark
    )

    # Intent drift
    intent_drift = compute_intent_drift(coin)

    # Surprise
    expected_move = thesis_data.get("atr_pct", 0.5)
    actual_move = thesis_data["vol_spike"] * 0.5
    surprise = compute_surprise_index(coin, expected_move, actual_move)

    # Update intent vector
    update_intent_vector(coin, event, thesis_data["delta"], thesis_data["vol_spike"],
                         micro_acc.get("score", 50), context)

    # Update intent timeline
    update_intent_timeline(coin, intent.value)

    # ===== DISCOVERY / OBSERVE MODE =====
    allow_entry = True
    if intent_drift > 0.7:
        mode_override = "DISCOVERY"
        final_threshold = int(final_threshold * 1.3)
        position_size_mult *= 0.3
        allow_entry = False
        why_not += " | DISCOVERY mode: high intent drift, observing only"
    elif intent_drift > 0.5:
        mode_override = "OBSERVE"
        final_threshold = int(final_threshold * 1.1)
        position_size_mult *= 0.7
        why_not += " | OBSERVE mode: moderate drift, reduced size"
    else:
        mode_override = None
        # ===== THRESHOLD CHECK =====
if confidence_data["final_score"] < final_threshold:
    if position_size_mult > 0.3:
        position_size_mult = max(0.15, position_size_mult * 0.7)
    else:
        update_fatigue_memory(event.type)
        return None

# If DISCOVERY mode, still log but don't execute
if not allow_entry:
    # Build shadow result
    shadow_result = {
        "executed": False,
        "mode": "DISCOVERY",
        "shadow": True,
        "coin": coin,
        "direction": event.direction,
        "entry": mark,
        "sl": confidence_data["sl"],
        "tp": confidence_data["tp"],
        "rr": confidence_data["rr"],
        "score": confidence_data["final_score"],
        "area": event.type,
        "label": "🔍 DISCOVERY",
        "why_not": why_not,
        "hypothesis": thesis_obj,
        "context_age": context_age,
        # ... tambahkan field lain sesuai kebutuhan
    }
    # Log journal for shadow
    journal_entry = DecisionJournalEntry(
        timestamp=time.time(),
        coin=coin,
        event_type=event.type,
        direction=event.direction,
        score=confidence_data["final_score"],
        mode="DISCOVERY",
        executed=False,
        shadow=True,
        entry=mark,
        sl=confidence_data["sl"],
        tp=confidence_data["tp"],
        rr=confidence_data["rr"],
        intent=intent.value,
        belief=belief.value,
        decision_energy=confidence_data["decision_energy"],
        hidden_liquidity=hl.get("score", 0),
        micro_acceptance=micro_acc.get("score"),
        failed_risk=failed_risk.get("risk", 1.0),
        intent_drift=intent_drift,
        surprise=surprise,
        narrative={}
    )
    log_decision_journal(journal_entry)
    auto_review()
    return shadow_result
    # ===== LOG JOURNAL =====
journal_entry = DecisionJournalEntry(
    timestamp=time.time(),
    coin=coin,
    event_type=event.type,
    direction=event.direction,
    score=confidence_data["final_score"],
    mode=execution_mode_str,
    executed=True,
    shadow=False,
    entry=mark,
    sl=confidence_data["sl"],
    tp=confidence_data["tp"],
    rr=confidence_data["rr"],
    intent=intent.value,
    belief=belief.value,
    decision_energy=confidence_data["decision_energy"],
    hidden_liquidity=hl.get("score", 0),
    micro_acceptance=micro_acc.get("score"),
    failed_risk=failed_risk.get("risk", 1.0),
    intent_drift=intent_drift,
    surprise=surprise,
    narrative={}
)
log_decision_journal(journal_entry)
auto_review()
    
    # ===== RETURN RESULT =====
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
        "execution_mode": execution_mode_str,
        "execution_mode_v10": exec_mode.value.upper(),
        "intent_type": intent.value,
        "decision_energy": confidence_data["decision_energy"],
        "position_size_mult": position_size_mult,
        "filter_score": filter_score,
        "rejection_strength": thesis_data["rejection"],
        "acceptance_strength": thesis_data["acceptance"],
        "persistence_strength": thesis_data["persistence"],
        "why_not": why_not,
        "wait_value": wait_value,
        "belief_state": belief.value,
        "commitment_score": commitment_score,
        "time_pressure": confidence_data["time_pressure"].value,
        "decision_acceleration": confidence_data["decision_acceleration"],
        "fatigue_penalty": fatigue_penalty,
        "mode_aggressive": blend_weights["aggressive"],
        "mode_balanced": blend_weights["balanced"],
        "mode_precision": blend_weights["precision"],
        "confidence_breakdown": confidence_breakdown,
        "intent_success": intent_success,
        "context_age": context_age,
        "event_importance": event_adjust.get("importance", 0),
        "event_bias": event_adjust.get("bias", 0),
        "reaction_mode": reaction_adj.get("mode", "NORMAL"),
        "hypothesis": {
            "thesis": thesis_obj.statement,
            "invalidate": thesis_obj.invalidation,
            "observe": thesis_obj.confirmation,
            "destination": thesis_obj.destination,
            "timeframe": thesis_obj.timeframe
        },
        "clarity_severity": clarity["severity"],
        "clarity_quality": clarity["decision_quality"],
        "clarity_dominant_factor": clarity["dominant_factor"],
        "clarity_reasons": ", ".join(clarity["reasons"][:3]),
        "explanation": explanation
        "hidden_liquidity": hl.get("score", 0),
"hidden_side": hl.get("side", "NONE"),
"micro_acceptance": micro_acc.get("score"),
"micro_acceptance_status": micro_acc.get("status"),
"failed_risk": failed_risk.get("risk", 1.0),
"failed_reason": failed_risk.get("reason"),
"intent_drift": intent_drift,
"surprise": surprise,
    }
    
# ============================================================
# PART 35 – CHECK ENTRY V10 + GLOBAL MARKET INTENT + EVALUATE SIGNAL
# ============================================================

def classify_global_market_intent_v10(context: ContextSnapshot, breath: Dict[str, float]) -> Tuple[str, float]:
    score = 0.0
    intent = "NEUTRAL"

    if context.shock_score > 80:
        return "CHAOS", context.shock_score

    if context.regime in ("TRENDING_UP", "TRENDING_DOWN"):
        intent = "TRENDING"
        score += 30

    if breath.get("participation", 0.5) > 0.7:
        score += 15
    elif breath.get("participation", 0.5) < 0.3:
        score -= 10

    if breath.get("leadership", 0) > 2:
        score += 15
    elif breath.get("leadership", 0) < -2:
        score -= 15

    if breath.get("rotation", 0) > 1:
        intent = "ROTATION"
        score += 10

    if context.transition_prob > 70:
        intent = "TRANSITION"
        score += 20
    elif context.transition_prob > 50:
        score += 10

    if context.tension > 70:
        intent = "VOLATILE"
        score += 15

    event_adj = get_event_risk_adjustment()
    if event_adj.get("importance", 0) > 70:
        if intent != "CHAOS":
            intent = "EVENT_RISK"
        score += 20

    return intent, min(100, score)

def _event_member_types(event) -> List[str]:
    members = event.extra.get("members", []) if event.extra else []
    return [event.type] + list(members)

def validate_local_vs_global_v10(obs: Dict, global_intent: str, global_score: float, breath: Dict[str, float]) -> bool:
    event = obs["best_event"]
    types = _event_member_types(event)

    if global_intent == "CHAOS":
        return False

    if global_intent == "TRENDING":
        if "LIQUIDITY" in types:
            return False
        if "OB_FLOW" in types or "FVG_FLOW" in types:
            return True
        if ("OB" in types or "FVG" in types) and obs["structure_valid_long" if event.direction == "LONG" else "structure_valid_short"]:
            return True

    if global_intent == "RANGING":
        if "LIQUIDITY" in types or "OB" in types or "VACUUM" in types:
            return True
        if "FVG" in types or "FVG_FLOW" in types:
            return False

    if global_intent == "VOLATILE":
        struct_ok = obs["structure_valid_long"] if event.direction == "LONG" else obs["structure_valid_short"]
        if ("FVG" in types or "OB" in types) and struct_ok:
            return True
        if "LIQUIDITY" in types:
            return False

    if global_intent == "ROTATION":
        if obs["coin"] not in ["BTC", "ETH"]:
            return True
        return False

    if global_intent == "TRANSITION":
        if "OB_FLOW" in types or "FVG_FLOW" in types:
            return True
        if "LIQUIDITY" in types:
            return False

    if global_intent == "EVENT_RISK":
        if "OB" in types and obs["structure_valid_long" if event.direction == "LONG" else "structure_valid_short"]:
            return True
        return False

    return True

def check_entry_alert_v10(coin: str, mark: float, master_candles: Dict) -> Optional[dict]:
    """V10: 5-layer entry check dengan Reaction Engine + Intent Memory"""
    try:
        # ===== LAYER 0: CONTEXT =====
        context = get_context_snapshot(coin)
        context_age = time.time() - context.timestamp

        # ===== LAYER 0.5: BREATH V10 =====
        breath = compute_market_breath_v10()

        # ===== LAYER 0.7: GLOBAL INTENT =====
        global_intent, global_score = classify_global_market_intent_v10(context, breath)
        if global_intent == "CHAOS":
            logger.debug(f"Global CHAOS ({global_score:.0f}), skip {coin}")
            return None

        # ===== LAYER 1: OBSERVE =====
        obs = observe_market(coin, mark, master_candles)
        if not obs:
            return None

        # ===== LAYER 1.5: VALIDATE LOCAL vs GLOBAL =====
        if not validate_local_vs_global_v10(obs, global_intent, global_score, breath):
            logger.debug(f"{coin}: local setup incompatible with global {global_intent}")
            return None

        # ===== LAYER 2: BUILD THESIS =====
        thesis_data = build_thesis(obs)
        if not thesis_data:
            return None

        # ===== LAYER 3: COMPUTE CONFIDENCE =====
        confidence_data = compute_confidence(thesis_data)
        if not confidence_data:
            return None

        # ===== LAYER 4: EXECUTE DECISION =====
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"]
        )

        # ===== LOG TRACE =====
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
                what_changed=f"belief:{result['belief_state']}|mode:{result['execution_mode']}|v10_mode:{result['execution_mode_v10']}|global:{global_intent}",
                context_age=result.get("context_age", 0.0),
                execution_mode=result.get("execution_mode_v10", "NORMAL")
            )
            log_decision_trace(trace)

        return result

    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None

# ========== PHASE 1 — ENTRY CHECK UPGRADED ==========

def check_entry_alert_v10_phase1(coin: str, mark: float, master_candles: Dict) -> Optional[dict]:
    """V10 + Phase 1 upgrades: regime interpretation, OB/FVG assessment, confidence calibration"""
    try:
        # 1. Regime (upgraded)
        regime = interpret_regime_v10(coin)

        # 2. Context memory
        ctx = get_context_snapshot(coin)
        _context_memory.add(ctx)

        # 3. Standard observe
        obs = observe_market(coin, mark, master_candles)
        if not obs:
            return None

        # 4. Thesis
        thesis_data = build_thesis(obs)
        if not thesis_data:
            return None

        # 5. Confidence
        confidence_data = compute_confidence(thesis_data)
        if not confidence_data:
            return None

        # 6. OB / FVG assessment
        event = thesis_data.get('event')
        ob_reaction = None
        fvg_quality = None
        if event:
            if event.type in ("OB", "OB_FLOW"):
                candles_1h = get_candles(coin, "1h", 60, master_candles)
                if candles_1h:
                    ob_reaction = assess_ob_reaction_v10(coin, event, candles_1h)
                    if ob_reaction.is_strong():
                        confidence_data['confidence'] = min(100, confidence_data['confidence'] + 10)
                    else:
                        confidence_data['confidence'] = max(0, confidence_data['confidence'] - 15)
            elif event.type in ("FVG", "FVG_FLOW"):
                candles_1h = get_candles(coin, "1h", 60, master_candles)
                if candles_1h:
                    fvg_quality = assess_fvg_quality_v10(coin, event, candles_1h)
                    if fvg_quality.quality_score > 60:
                        confidence_data['confidence'] = min(100, confidence_data['confidence'] + 5)
                    else:
                        confidence_data['confidence'] = max(0, confidence_data['confidence'] - 10)

        # 7. Calibrate confidence
        cal = calibrate_confidence_v10(coin, confidence_data['confidence'])
        confidence_data['confidence_calibrated'] = cal.calibrated
        confidence_data['calibration_factor'] = cal.calibration_factor
        confidence_data['calibration_samples'] = cal.sample_size

        # 8. Compute breath and context for clarity check
        breath = compute_market_breath_v10()
        context = ctx  # reuse dari step 2

        # 9. Execute decision
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"],
            context, breath   # <-- pass context & breath
        )

        if result:
            # Attach upgrade data
            result['regime_interpretation'] = regime
            result['ob_reaction'] = ob_reaction
            result['fvg_quality'] = fvg_quality
            result['context_memory'] = _context_memory
            result['confidence_calibrated'] = cal.calibrated
            result['calibration_samples'] = cal.sample_size

            # Trace
            trace = DecisionTrace(
                timestamp=time.time(),
                coin=coin,
                event_type=result["area"],
                belief_state=result["belief_state"],
                confidence=cal.calibrated,
                decision_energy=result["decision_energy"],
                final_decision="EXECUTE",
                reasons=result["positive_evidence"],
                why_not=[result["why_not"]] if result["why_not"] else [],
                what_changed=f"regime:{regime.regime}|trans:{regime.transition_prob:.0f}%|cal:{cal.calibrated:.0f}%",
                context_age=result.get("context_age", 0.0),
                execution_mode=result.get("execution_mode_v10", "NORMAL")
            )
            log_decision_trace(trace)

        return result

    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None

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

        pred_quality = evaluate_prediction_quality(
            signal_id, coin, predicted_direction, direction, entry,
            predicted_zone_low, predicted_zone_high, mfe, mae, hypothesis_validated
        )
        update_prediction_memory(coin, pred_quality)

        # V10: Update intent memory
        try:
            conn = db_connect()
            c = conn.cursor()
            c.execute('''SELECT intent_type FROM signals WHERE signal_id = ?''', (signal_id,))
            row = c.fetchone()
            conn.close()
            if row:
                intent = row[0]
                update_intent_memory(coin, intent, outcome, pnl)
        except Exception as e:
            logger.error(f"Intent memory update error: {e}")

        logger.info(f"Evaluated {signal_id}: {outcome} pnl={pnl:.2f}% pred_quality={pred_quality:.1f}")

        if outcome in ("SL_HIT", "PARTIAL") and pnl < 0:
            reset_belief_state(coin, f"loss {outcome}")

    except Exception as e:
        logger.error(f"Eval error {signal_id}: {e}")
        
# ============================================================
# PART 36 – ENGINE LOOPS + DB QUEUE WRITER + GRACEFUL SHUTDOWN
# ============================================================

def _db_writer_loop():
    """Background DB writer dengan retry dan drop"""
    while RUNTIME.is_running():
        try:
            batch = []
            while not _db_queue.empty() and len(batch) < 10:
                try:
                    item = _db_queue.get_nowait()
                    batch.append(item)
                except:
                    break

            if batch:
                conn = None
                try:
                    conn = db_connect()
                    c = conn.cursor()
                    failed = []
                    for func, args, kwargs, retry in batch:
                        try:
                            func(c, *args, **kwargs)
                        except Exception as e:
                            logger.error(f"DB item error {func.__name__}: {e}")
                            failed.append((func, args, kwargs, retry + 1))
                    conn.commit()

                    for item in failed:
                        func, args, kwargs, retry = item
                        if retry < MAX_DB_RETRIES:
                            try:
                                _db_queue.put((func, args, kwargs, retry))
                            except:
                                pass
                        else:
                            logger.warning(f"DB item dropped after {retry} retries: {func.__name__}")

                except Exception as e:
                    logger.error(f"DB commit error: {e}")
                    for func, args, kwargs, retry in batch:
                        if retry < MAX_DB_RETRIES:
                            try:
                                _db_queue.put((func, args, kwargs, retry + 1))
                            except:
                                pass
                        else:
                            logger.warning(f"DB item dropped (commit fail): {func.__name__}")
                finally:
                    if conn:
                        conn.close()

        except Exception as e:
            logger.error(f"_db_writer_loop error: {e}")
        RUNTIME.wait(0.1)

def enqueue_db(func, *args, **kwargs):
    """Enqueue DB write; fallback ke direct write kalau queue penuh"""
    try:
        _db_queue.put_nowait((func, args, kwargs, 0))
    except Exception:
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"enqueue_db direct fallback error: {e}")

def _db_queue_force_flush():
    """Force flush semua item di DB queue"""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        while not _db_queue.empty():
            try:
                func, args, kwargs, _ = _db_queue.get_nowait()
                func(c, *args, **kwargs)
            except Exception as e:
                logger.error(f"Flush DB error: {e}")
        conn.commit()
    except Exception as e:
        logger.error(f"DB flush failed: {e}")
    finally:
        if conn:
            conn.close()

def graceful_shutdown():
    """Graceful shutdown semua komponen"""
    logger.info("Graceful shutdown initiated...")
    RUNTIME.signal_shutdown()

    # 1. Stop accepting new tasks
    _EVAL_EXECUTOR.shutdown(wait=False, cancel_futures=False)
    _SHADOW_EXECUTOR.shutdown(wait=False, cancel_futures=False)

    # 2. Flush DB queue
    logger.info("Flushing DB queue...")
    _db_queue_force_flush()

    # 3. Wait for executors to finish (max 5 seconds)
    _EVAL_EXECUTOR.shutdown(wait=True, timeout=5)
    _SHADOW_EXECUTOR.shutdown(wait=True, timeout=5)

    logger.info("Shutdown complete.")

def state_engine_update_v10():
    """State Engine V10: refresh context + scan top coins + reaction engine update"""
    context = get_context_snapshot("BTC")
    refresh_snapshot()
    compute_market_breath_v10()
    
    # ===== REACTION ENGINE =====
    with _event_risk_lock:
        if _EVENT_RISK_DATA:
            latest_event = max(_EVENT_RISK_DATA, key=lambda e: e.ts)
            if time.time() - latest_event.ts < TUNABLE["EVENT_RISK_DECAY_HOURS"] * 3600:
                snapshot = get_snapshot()
                if snapshot and "BTC" in snapshot.mids:
                    with _last_mids_lock:
                        if "BTC" in _last_mids:
                            prev_price, prev_ts = _last_mids["BTC"]
                            current_price = snapshot.mids["BTC"]
                            if prev_price > 0 and prev_ts > time.time() - 300:
                                btc_move = (current_price - prev_price) / prev_price * 100
                                vol_spike = get_volume_spike("BTC")
                                reaction = compute_reaction(latest_event, btc_move, vol_spike)
                                update_reaction_history(reaction)
    
    # ===== GET TOP COINS DYNAMICALLY =====
    def get_top_coins_by_volume(limit=20, min_vol=5_000_000):
        """Get top coins by 24h volume dynamically from exchange"""
        try:
            meta = info.meta_and_asset_ctxs()
            coins_vol = []
            for asset, ctx in zip(meta[0]["universe"], meta[1]):
                vol = float(ctx.get("dayNtlVlm", 0))
                if vol > min_vol:
                    coins_vol.append((asset["name"], vol))
            coins_vol.sort(key=lambda x: x[1], reverse=True)
            result = [c[0] for c in coins_vol[:limit]]
            logger.info(f"Loaded {len(result)} top coins dynamically")
            return result
        except Exception as e:
            logger.error(f"Failed to get top coins dynamically: {e}")
            return None
    
    # ===== TOP COINS =====
    top_coins = get_top_coins_by_volume(limit=20, min_vol=5_000_000)
    if not top_coins:
        logger.warning("Using fallback top coins list (hardcoded)")
        top_coins = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]
    
    
    # ===== BATCH SCAN (PATCH 4) =====
    BATCH_SIZE = 5
    BATCH_WAIT = 3  # detik antar batch
    
    master_candles = fetch_candles_master(top_coins, "1h", 100)
    alerts = []
    
    for i in range(0, len(top_coins), BATCH_SIZE):
        batch = top_coins[i:i+BATCH_SIZE]
        logger.debug(f"📊 Scanning batch {i//BATCH_SIZE + 1}: {batch}")
        
        for coin in batch:
            mark = 0.0
            snapshot = get_snapshot()
            if snapshot and coin in snapshot.mids:
                mark = snapshot.mids[coin]
            if mark == 0 or coin not in master_candles:
                continue
            
            # CEK COOLDOWN SEBELUM ENTRY CHECK
            if not can_call_api():
                logger.warning(f"⏳ API cooldown, skipping {coin}")
                continue
            
            alert = check_entry_alert_v10_phase1(coin, mark, master_candles)
            if alert and not PAPER_MODE:
                alerts.append(alert)
            elif alert and PAPER_MODE:
                logger.info(f"[PAPER] {alert['coin']} {alert['direction']} score={alert['score']}")
            time.sleep(0.1)  # 100ms antar coin
        
        # WAIT ANTAR BATCH
        if i + BATCH_SIZE < len(top_coins):
            logger.debug(f"⏳ Waiting {BATCH_WAIT}s before next batch...")
            time.sleep(BATCH_WAIT)
    
    for alert in alerts:
        send_alert_v10(alert)

def trigger_engine_update_v7():
    refresh_snapshot()
    all_top = get_top_coins_by_volume(limit=20, min_vol=5_000_000) if 'get_top_coins_by_volume' in globals() else None
    if not all_top:
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
            logger.warning("Using fallback top coins list in trigger_engine_update_v7")
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

def scheduled_state_engine_v10():
    while RUNTIME.is_running():
        if not RUNTIME.is_alert_enabled():
            RUNTIME.wait(60)
            continue
        state_engine_update_v10()
        vol_reg = get_volatility_regime()
        interval = TUNABLE["STATE_ENGINE_INTERVAL"]
        if vol_reg == "HIGH_VOLATILITY":
            interval = max(15, interval // 2)
        elif vol_reg == "LOW_VOLATILITY":
            interval = min(60, interval * 2)
        logger.info(f"State engine cycle done, next in {interval}s")
        RUNTIME.wait(interval)

def scheduled_trigger_engine_v7():
    while RUNTIME.is_running():
        trigger_engine_update_v7()
        RUNTIME.wait(TUNABLE["TRIGGER_ENGINE_INTERVAL_ACTIVE"])

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
        RUNTIME.wait(3600)

def scheduled_cleanup_v7():
    while RUNTIME.is_running():
        cleanup_active_candidates_v7()
        cleanup_old_shadow_decisions_v7()
        try:
            now = time.time()
            with _CONTEXT_CACHE_LOCK:
                stale = [k for k, (_, ts, _) in _CONTEXT_CACHE.items() if now - ts > 60]
                for k in stale:
                    del _CONTEXT_CACHE[k]
        except Exception as e:
            logger.error(f"ctx_cache cleanup error: {e}")
        RUNTIME.wait(600)

def cleanup_memory_v10():
    while RUNTIME.is_running():
        try:
            now = time.time()
            cutoff_7d = now - 7 * 86400
            cutoff_1d = now - 86400

            with _hypothesis_lock:
                expired = [k for k, v in _hypothesis_store.items()
                           if v.get("ts", v.get("timestamp", 0)) < cutoff_7d]
                for k in expired:
                    del _hypothesis_store[k]
                if expired:
                    logger.info(f"[cleanup_v10] removed {len(expired)} hypotheses")

            with _belief_history_lock:
                stale = [c for c, dq in _belief_history.items()
                         if dq and dq[-1].get("ts", 0) < cutoff_1d]
                for c in stale:
                    del _belief_history[c]

            with _CONTEXT_CACHE_LOCK:
                stale_ctx = [k for k, (_, ts, _) in _CONTEXT_CACHE.items()
                             if now - ts > 60]
                for k in stale_ctx:
                    del _CONTEXT_CACHE[k]

            cutoff_30d = now - 30 * 86400
            with _prediction_memory_lock:
                stale_pred = [c for c, m in _prediction_memory.items()
                              if m.get("last_update", 0) < cutoff_30d]
                for c in stale_pred:
                    del _prediction_memory[c]

            # V10: Cleanup intent memory (expired entries)
            with _intent_memory_lock:
                for coin, deq in list(_intent_memory.items()):
                    cutoff = now - TUNABLE["INTENT_MEMORY_HOURS"] * 3600
                    _intent_memory[coin] = deque(
                        [e for e in deq if e.ts > cutoff],
                        maxlen=TUNABLE["INTENT_MEMORY_MAX"]
                    )
                    if not _intent_memory[coin]:
                        del _intent_memory[coin]

        except Exception as e:
            logger.error(f"cleanup_memory_v10 error: {e}")
        RUNTIME.wait(3600)

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
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_one, c) for c in coins]
        for f in futures:
            coin, candles = f.result()
            if candles:
                results[coin] = candles
    return results
    
# ============================================================
# PART 37 – ALERT VALUE + TELEGRAM BOT V10
# ============================================================

def compute_alert_value(alert: dict) -> Tuple[float, str]:
    """Compute alert value untuk menentukan apakah alert layak dikirim"""
    score = alert.get("score", 0)
    belief = alert.get("belief_state", "SEEKING")
    de = alert.get("decision_energy", 0)
    pressure = alert.get("time_pressure", "normal")
    fatigue = alert.get("fatigue_penalty", 1.0)

    value = 0.0
    if score >= 80:
        value += 40
    elif score >= 70:
        value += 30
    elif score >= 60:
        value += 20
    else:
        value += 10

    belief_values = {"CONVICTED": 30, "BUILDING": 20, "SEEKING": 10, "EXECUTING": 25}
    value += belief_values.get(belief.upper(), 10)
    value += min(30, de * 0.3)
    value += {"urgent": 20, "normal": 10, "low": 0}.get(pressure, 0)
    value *= fatigue

    if value >= TUNABLE["ALERT_VALUE_HIGH"]:
        label = "🔥 HIGH VALUE"
    elif value >= TUNABLE["ALERT_VALUE_MEDIUM"]:
        label = "🟡 MEDIUM VALUE"
    else:
        label = "⚪ LOW VALUE"

    return min(100, value), label

bot = telebot.TeleBot(TOKEN)

def send_alert_v10(alert: dict):
    if not RUNTIME.is_alert_enabled():
        return

    value, label = compute_alert_value(alert)
    if value < TUNABLE["ALERT_VALUE_MIN"]:
        logger.debug(f"Alert value too low ({value:.0f}), skip {alert['coin']}")
        return

    alert["value_label"] = label
    alert["value_score"] = value

    coin = alert["coin"]
    now = time.time()

    def get_progressive_cooldown(c: str) -> int:
        with _alert_history_lock:
            if c not in _alert_history:
                _alert_history[c] = deque(maxlen=5)
            while _alert_history[c] and now - _alert_history[c][0] > TUNABLE["ALERT_HISTORY_WINDOW"]:
                _alert_history[c].popleft()
            cnt = len(_alert_history[c])
        base_cooldown = 300 if cnt == 0 else (600 if cnt == 1 else (900 if cnt == 2 else 1200))
        mode = alert.get("execution_mode_v10", "NORMAL")
        mode_cooldown_factor = {
            "NORMAL": 1.0,
            "PREPARE": 0.7,
            "CAUTIOUS": 1.3,
            "AGGRESSIVE": 0.5,
            "DEFENSIVE": 2.0
        }.get(mode, 1.0)
        return int(base_cooldown * mode_cooldown_factor)

    cooldown = get_progressive_cooldown(coin)
    with _last_alert_lock:
        if coin in _last_alert and now - _last_alert[coin] < cooldown:
            return
        _last_alert[coin] = now

    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        _alert_history[coin].append(now)

    # --- Ambil data Phase 1 ---
    regime = alert.get('regime_interpretation')
    ob_reaction = alert.get('ob_reaction')
    fvg_quality = alert.get('fvg_quality')
    context_memory = alert.get('context_memory')
    cal_conf = alert.get('confidence_calibrated', alert.get('score', 50))
    cal_samples = alert.get('calibration_samples', 0)

    # --- Mulai build text ---
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    belief_emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀","invalidated":"❌"}.get(alert.get("belief_state", "seeking"), "❓")
    pressure_emoji = {"low":"🐢","normal":"⚖️","urgent":"⏰"}.get(alert.get("time_pressure", "normal"), "⚖️")
    mode_v10 = alert.get("execution_mode_v10", "NORMAL")
    mode_emoji_v10 = get_mode_emoji(ExecutionMode(mode_v10.lower()))
    mode_color_v10 = get_mode_color(ExecutionMode(mode_v10.lower()))

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
    context_age = alert.get("context_age", 0)
    context_warn = "⚠️" if context_age > 3 else ""

    intent_success = alert.get("intent_success", 0.5) * 100
    success_emoji = "🟢" if intent_success > 70 else ("🟡" if intent_success > 40 else "🔴")

    event_importance = alert.get("event_importance", 0)
    event_info = f"📅 Event Risk: {event_importance:.0f}%" if event_importance > 20 else ""
    reaction_mode = alert.get("reaction_mode", "NORMAL")
    reaction_info = f"⚡ Reaction: {reaction_mode}" if reaction_mode != "NORMAL" else ""

    # === REGIME SECTION ===
    regime_text = ""
    if regime:
        regime_text = (
            f"📈 *Regime*: {regime.regime}\n"
            f"├─ Strength: {regime.strength:.0f}% | Stability: {regime.stability:.0f}%\n"
            f"├─ Confidence: {regime.confidence:.0f}% | Age: {regime.age_minutes:.0f}m\n"
            f"└─ Transition: {regime.transition_prob:.0f}% {regime.transition_direction}"
        )

    # === OB REACTION ===
    ob_text = ""
    if ob_reaction and ob_reaction.touch_count > 0:
        ob_text = (
            f"📊 *OB Reaction*\n"
            f"├─ Touches: {ob_reaction.touch_count}\n"
            f"├─ Max reaction: {ob_reaction.max_reaction_strength:.0f}%\n"
            f"├─ Followthrough: {ob_reaction.followthrough:.0f}%\n"
            f"└─ Confidence: {ob_reaction.confidence:.0f}%"
        )

    # === FVG QUALITY ===
    fvg_text = ""
    if fvg_quality and fvg_quality.quality_score > 0:
        fvg_text = (
            f"📊 *FVG Quality*\n"
            f"├─ Size: {fvg_quality.size:.0f}%\n"
            f"├─ Fill: {fvg_quality.fill_ratio:.0%} (speed: {fvg_quality.fill_speed:.0f}%)\n"
            f"├─ Reaction: {fvg_quality.reaction:.0f}%\n"
            f"└─ Quality: {fvg_quality.quality_score:.0f}"
        )

    # === CONTEXT MEMORY ===
    ctx_text = ""
    if context_memory and context_memory.snapshots:
        ctx_text = (
            f"🧠 *Context Memory*\n"
            f"├─ Regimes: {' → '.join(context_memory.get_regime_sequence()[-5:])}\n"
            f"├─ Shock trend: {context_memory.get_trend('shock_score')}\n"
            f"├─ Volatility trend: {context_memory.get_volatility_trend()}\n"
            f"└─ Transitioning: {'Yes' if context_memory.is_transitioning() else 'No'}"
        )

    # === WHY NOW ===
    why_now = ""
    if mode_v10 == "AGGRESSIVE":
        why_now = "⚡ *WHY NOW*: Market reaction strong + low entropy\n"
    elif mode_v10 == "PREPARE":
        why_now = "🔧 *WHY NOW*: Transition detected, preparing for move\n"
    elif mode_v10 == "DEFENSIVE":
        why_now = "🛡️ *WHY NOW*: High event risk, defensive mode\n"
    elif mode_v10 == "CAUTIOUS":
        why_now = "⚠️ *WHY NOW*: Intent success low, cautious entry\n"
    elif alert.get("intent_type") == "seek_liquidity":
        why_now = "🦈 *WHY NOW*: Intent = SEEK_LIQUIDITY (stop hunt expected)\n"
    elif alert.get("time_pressure") == "urgent":
        why_now = "⏰ *WHY NOW*: Time Pressure = URGENT (opportunity fading)\n"

    # === BUILD FINAL TEXT ===
    sl_pct = abs(alert['entry'] - alert['sl']) / max(alert['entry'], 0.01) * 100
    tp_pct = abs(alert['tp'] - alert['entry']) / max(alert['entry'], 0.01) * 100
    
    text = f"""
{arrow} {mode_emoji} *V10 ALERT* • {coin} {intent_emoji}
━━━━━━━━━━━━━━━━━━━━━━
{label} | {mode_color_v10} Mode: {mode_emoji_v10} {mode_v10}
Context age: {context_age:.1f}s {context_warn}
{event_info} {reaction_info}

🧠 *Belief*: {belief_emoji} {alert.get('belief_state', 'SEEKING').upper()} | ⏱️ Pressure: {pressure_emoji} {alert.get('time_pressure', 'normal').upper()}
📊 Intent Success: {success_emoji} {intent_success:.0f}%

📊 *Setup Quality*
├─ Score: {alert['score']} | {alert['label']}
├─ DE: {alert.get('decision_energy', 0):.1f}
├─ Commitment: {commit:.0f}% [{commit_bar}]
├─ Filter: {fs:.0f} {filter_ind}
└─ Value: {value:.0f}%

🎯 *Execution*
├─ V10 Mode: {mode_color_v10} {mode_emoji_v10} {mode_v10}
├─ Blend: {mode_emoji} [{mode_bar}]
├─ A:{weights['A']:.0%} B:{weights['B']:.0%} P:{weights['P']:.0%}
└─ Size: {size:.1f}x [{size_bar}]

💰 *Levels*
├─ Entry: {fmt_price(alert['entry'])}
├─ SL: {fmt_price(alert['sl'])} ({sl_pct:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({tp_pct:.2f}%)
└─ RR: 1:{alert['rr']:.1f}

🌡️ *Entropy*
├─ Data: {e_data}% [{ebar_d}]
├─ Market: {e_market}% [{ebar_m}]
└─ Decision: {e_decision}% [{ebar_dec}]

📈 *Evidence*
├─ Positive: {', '.join(alert.get('positive_evidence', []))}
├─ Negative: {alert.get('negative_evidence', 'none')}
└─ Why Not: {alert.get('why_not', 'no deterrents')}

{regime_text + chr(10) if regime_text else ''}
{ob_text + chr(10) if ob_text else ''}
{fvg_text + chr(10) if fvg_text else ''}
{ctx_text + chr(10) if ctx_text else ''}

🎯 *Confidence Calibrated*: {cal_conf:.0f}% (raw: {alert.get('score', 0)}%, samples: {cal_samples})

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
        
# ============================================================
# PART 38 – BOT COMMANDS (START, CONTEXT, REACTION, INTENT, SHOCK, BREATH, EVENTS, SETEVENT)
# ============================================================

@bot.message_handler(commands=['start'])
def cmd_start(m):
    regimes = get_all_regimes()
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    reaction = get_current_reaction()
    reaction_text = reaction.event if reaction else "None"
    absorption_text = f"{reaction.absorption*100:.0f}%" if reaction else "N/A"
    confidence_text = f"{reaction.confidence*100:.0f}%" if reaction else "N/A"

    text = f"""
🧠 *SMART ENTRY ENGINE V10 – REACTION ENGINE*
━━━━━━━━━━━━━━━━━━━━━━
🚀 Market Anticipation Engine
🔬 Belief ≠ Confidence (Decoupled)
⚡ Pre-Shock + Reaction Engine
🔄 Regime Transition + Inertia
🌍 Advanced Market Breath
📅 Event Risk + Expectation
🧠 Intent Memory
📊 5 Execution Modes

📡 *Market Snapshot*
├─ Regime: {ctx.regime}
├─ Shock: {ctx.shock_score:.0f}%
├─ Transition: {ctx.transition_prob:.0f}%
├─ Tension: {ctx.tension:.0f}%
├─ Breath: Bull {breath['bull']*100:.0f}% / Part {breath['participation']*100:.0f}%
├─ Leadership: {breath['leadership']:+.1f}%
├─ Dispersion: {breath['dispersion']:.2f}%
└─ Rotation: {breath['rotation']:+.1f}%

📅 *Events*
├─ Importance: {event_adj.get('importance', 0):.0f}%
├─ Volatility: {event_adj.get('volatility', 0):.0f}%
└─ Bias: {event_adj.get('bias', 0):+.0f}

⚡ *Reaction*
├─ Latest: {reaction_text}
├─ Absorption: {absorption_text}
└─ Confidence: {confidence_text}

⏰ {get_wib()}

✅ /status /entry BTC /warroom BTC /analytics /journal /belief /fatigue /prediction /traces /health /intel
✅ /context /shock /breath /events /setevent /reaction /intent
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['context'])
def cmd_context(m):
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    text = f"""
🧠 *CONTEXT SNAPSHOT* (V10)
━━━━━━━━━━━━━━━━━━━━━━
⏰ {get_wib()}
📈 *Regime*: {ctx.regime}

⚡ *Shock Score*: {ctx.shock_score:.1f}%
🔄 *Transition Prob*: {ctx.transition_prob:.1f}%
💢 *Tension*: {ctx.tension:.1f}%
📊 *Vol Forecast*: {ctx.vol_forecast:.2f}x

🌍 *Advanced Market Breath*
├─ Bull: {breath['bull']*100:.1f}%
├─ Bear: {breath['bear']*100:.1f}%
├─ Participation: {breath['participation']*100:.1f}%
├─ Leadership: {breath['leadership']:+.2f}%
├─ Dispersion: {breath['dispersion']:.2f}%
└─ Rotation: {breath['rotation']:+.2f}%

📅 *Event Risk*
├─ Importance: {event_adj.get('importance', 0):.1f}%
├─ Volatility: {event_adj.get('volatility', 0):.1f}%
└─ Bias: {event_adj.get('bias', 0):+.1f}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['reaction'])
def cmd_reaction(m):
    reaction = get_current_reaction()
    if not reaction:
        bot.reply_to(m, "Belum ada data reaction.")
        return
    text = f"""
⚡ *REACTION ENGINE* (V10)
━━━━━━━━━━━━━━━━━━━━━━
📅 Event: {reaction.event}
📊 Expected Vol: {reaction.expected_vol:.0f}%
📈 Expected Direction: {reaction.expected_direction.upper()}

📉 *Actual*
├─ Move: {reaction.actual_move:+.2f}%
├─ Vol: {reaction.actual_vol:.0f}%
└─ Direction: {reaction.actual_direction.upper()}

🧠 *Analysis*
├─ Absorption: {reaction.absorption*100:.0f}%
├─ Confidence: {reaction.confidence*100:.0f}%
└─ Interpretasi: {'Market ignored' if reaction.absorption > 0.7 else 'Market responded' if reaction.confidence > 0.6 else 'Mixed'}

⏰ {datetime.fromtimestamp(reaction.timestamp, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['intent'])
def cmd_intent(m):
    parts = m.text.split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"

    with _intent_memory_lock:
        if coin not in _intent_memory:
            bot.reply_to(m, f"Belum ada intent memory untuk {coin}.")
            return
        text = f"🧠 *INTENT MEMORY* ({coin})\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for entry in list(_intent_memory[coin])[-10:]:
            dt = datetime.fromtimestamp(entry.ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            outcome_emoji = "✅" if entry.outcome in ("TP_HIT", "PARTIAL_WIN") else "❌"
            text += f"{dt} {entry.intent} {outcome_emoji} pnl:{entry.pnl:+.1f}%\n"

        for intent in set(e.intent for e in _intent_memory[coin]):
            rate = get_intent_success_rate(coin, intent)
            text += f"\n{intent}: {rate*100:.0f}% success"

        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['shock'])
def cmd_shock(m):
    parts = m.text.split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"
    shock = compute_shock_score(coin)
    trans = compute_regime_transition(coin)
    tension = compute_market_tension(coin)
    vol = compute_vol_forecast(coin)
    regime, penalty = get_regime_with_inertia(coin)
    text = f"""
📊 *SHOCK & TENSION* ({coin})
━━━━━━━━━━━━━━━━━━━━━━
⚡ Shock: {shock:.1f}%  {'🔴' if shock>80 else '🟡' if shock>60 else '🟢'}
🔄 Transition: {trans:.1f}%
💢 Tension: {tension:.1f}%
📈 Vol Forecast: {vol:.2f}x
📊 Regime: {regime} (inertia penalty: {penalty:.0f}%)
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['breath'])
def cmd_breath(m):
    breath = compute_market_breath_v10()
    text = f"""
🌍 *ADVANCED MARKET BREATH* (V10)
━━━━━━━━━━━━━━━━━━━━━━
🟢 Bull: {breath['bull']*100:.1f}%
🔴 Bear: {breath['bear']*100:.1f}%

📊 *Quality Metrics*
├─ Participation: {breath['participation']*100:.1f}%
├─ Leadership: {breath['leadership']:+.2f}%
├─ Dispersion: {breath['dispersion']:.2f}%
└─ Rotation: {breath['rotation']:+.2f}%

💡 *Interpretasi*
├─ {'✅ Market luas' if breath['participation'] > 0.6 else '⚠️ Market sempit'}
├─ {'✅ Top coins kuat' if breath['leadership'] > 1 else '⚠️ Top coins lemah'}
└─ {'🔄 Rotation ke small cap' if breath['rotation'] > 0.5 else '⚖️ No rotation'}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['events'])
def cmd_events(m):
    with _event_risk_lock:
        if not _EVENT_RISK_DATA:
            bot.reply_to(m, "Tidak ada event risk terdaftar.")
            return
        text = "📅 *EVENT RISK* (V10)\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for ev in _EVENT_RISK_DATA[-10:]:
            dt = datetime.fromtimestamp(ev.ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            importance_bar = "█" * int(ev.importance / 10) + "░" * (10 - int(ev.importance / 10))
            text += f"{dt} {ev.label}\n"
            text += f"   ├─ Importance: {ev.importance}% [{importance_bar}]\n"
            text += f"   ├─ Expected Vol: {ev.expected_vol}%\n"
            text += f"   └─ Bias: {ev.bias}\n\n"
        bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['setevent'])
def cmd_setevent(m):
    if m.from_user.id != USER_ID:
        return
    try:
        parts = m.text.split()
        if len(parts) < 5:
            bot.reply_to(m, "Format: /setevent YYYY-MM-DD HH:MM IMPORTANCE VOL BIAS LABEL\n"
                           "BIAS: bullish | bearish | neutral\n"
                           "Contoh: /setevent 2026-06-16 19:30 80 75 bullish CPI")
            return
        dt_str = f"{parts[1]} {parts[2]}"
        dt_obj = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        ts = dt_obj.replace(tzinfo=timezone(timedelta(hours=7))).timestamp()
        importance = int(parts[3])
        vol = int(parts[4])
        bias = parts[5].lower()
        if bias not in ["bullish", "bearish", "neutral"]:
            bot.reply_to(m, "Bias harus: bullish | bearish | neutral")
            return
        label = " ".join(parts[6:]) if len(parts) > 6 else "Event"
        set_event_risk_v10(importance, vol, "macro", bias, label, ts)
        bot.reply_to(m, f"✅ Event set: {label}\n"
                       f"   📅 {dt_str}\n"
                       f"   📊 Importance: {importance}% | Vol: {vol}% | Bias: {bias}")
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")
        
# ============================================================
# PART 39 – BOT COMMANDS (Journal, Belief, Fatigue, Prediction, Traces)
# ============================================================

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
            text += f"   DE:{t.decision_energy:.0f} | Mode:{t.execution_mode} | Context age:{t.context_age:.1f}s\n"
            text += f"   {t.what_changed}\n\n"
        bot.reply_to(m, text, parse_mode='Markdown')
        
# ============================================================
# PART 40 – BOT COMMANDS (Status, Analytics, Entry, Warroom, Stopalert, Health, Intel)
# ============================================================

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
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    reaction = get_current_reaction()
    reaction_text = reaction.event if reaction else "None"
    absorption_text = f"{reaction.absorption*100:.0f}%" if reaction else "N/A"
    with _market_sanity_lock:
        sane = "🟢 SANE" if _market_sanity["is_sane"] else "🔴 CHAOS"
    regime, penalty = get_regime_with_inertia("BTC")
    mode = get_execution_mode_v10(ctx, reaction, 0.5, event_adj)[0].value.upper()

    text = f"""
📊 *STATUS V10 – REACTION ENGINE*
━━━━━━━━━━━━━━━━━━━━━━
⏰ {get_wib()}
📡 Market: {market[0]} | {market[1]} | {market[2]}
🛡️ Sanity: {sane}
⚡ Shock: {ctx.shock_score:.0f}% | 🔄 Transition: {ctx.transition_prob:.0f}%
📊 Regime: {regime} (inertia: {penalty:.0f}%)
🎯 Mode: {mode}

🌍 *Breath*
├─ Bull: {breath['bull']*100:.0f}%
├─ Participation: {breath['participation']*100:.0f}%
└─ Rotation: {breath['rotation']:+.1f}%

📅 *Event*
├─ Importance: {event_adj.get('importance', 0):.0f}%
└─ Bias: {event_adj.get('bias', 0):+.0f}

⚡ *Reaction*
├─ Latest: {reaction_text}
└─ Absorption: {absorption_text}

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
        alert = check_entry_alert_v10(coin, mark, master)
        if not alert:
            bot.reply_to(m, f"❌ No setup for {coin}")
            return
        w = {"A": alert.get("mode_aggressive",0), "B": alert.get("mode_balanced",1), "P": alert.get("mode_precision",0)}
        be = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}.get(alert.get("belief_state","seeking"),"❓")
        pe = {"low":"🐢","normal":"⚖️","urgent":"⏰"}.get(alert.get("time_pressure","normal"),"⚖️")
        ctx = get_context_snapshot(coin)
        mode_v10 = alert.get("execution_mode_v10", "NORMAL")
        mode_emoji_v10 = get_mode_emoji(ExecutionMode(mode_v10.lower()))
        text = f"""
🎯 *Entry {coin}* (V10)
━━━━━━━━━━━━━━━━━━━━━━
🧠 Belief: {be} {alert.get('belief_state','SEEKING').upper()} | ⏱️ Pressure: {pe} {alert.get('time_pressure','normal').upper()}
⚡ Shock: {ctx.shock_score:.0f}% | 🔄 Transition: {ctx.transition_prob:.0f}%
{mode_emoji_v10} Mode: {mode_v10} | Context age: {alert.get('context_age', 0):.1f}s
📊 Intent Success: {alert.get('intent_success', 0.5)*100:.0f}%

📡 {alert['direction']} | {alert['label']} ({alert['score']})
├─ Blend: {alert['execution_mode']} (A:{w['A']:.0%} B:{w['B']:.0%} P:{w['P']:.0%})
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
        alert = check_entry_alert_v10(coin, mark, master)
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
        ctx = get_context_snapshot(coin)
        breath = compute_market_breath_v10()
        regime, penalty = get_regime_with_inertia(coin)
        mode_v10 = alert.get("execution_mode_v10", "NORMAL")
        mode_emoji_v10 = get_mode_emoji(ExecutionMode(mode_v10.lower()))
        text = f"""
🧠 *WARROOM {coin} V10*
━━━━━━━━━━━━━━━━━━━━━━
📡 Market: {market[0]} | {market[1]} | {market[2]}
├─ State: {state}
├─ Intent: {alert.get('intent_type','unknown')}
├─ Belief: {alert.get('belief_state','SEEKING')}
├─ Mode: {mode_emoji_v10} {mode_v10}
└─ Pressure: {alert.get('time_pressure','normal')}

⚡ *Context*
├─ Shock: {ctx.shock_score:.0f}%
├─ Transition: {ctx.transition_prob:.0f}%
├─ Tension: {ctx.tension:.0f}%
├─ Event Risk: {ctx.event_risk:.0f}%
└─ Regime: {regime} (inertia: {penalty:.0f}%)

🌍 *Breath (V10)*
├─ Bull: {breath['bull']*100:.0f}%
├─ Participation: {breath['participation']*100:.0f}%
├─ Leadership: {breath['leadership']:+.1f}%
├─ Dispersion: {breath['dispersion']:.2f}%
└─ Rotation: {breath['rotation']:+.1f}%

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
    with _trace_lock:
        trace_sz = len(_decision_traces)
        last_trace = _decision_traces[-1] if _decision_traces else None
    ctx = get_context_snapshot("BTC")
    now = time.time()
    with _snapshot_lock:
        snap_age = f"{now - _last_snapshot.timestamp:.1f}s" if _last_snapshot else "N/A"
    decision_latency = f"{now - last_trace.timestamp:.1f}s" if last_trace and hasattr(last_trace,'timestamp') else "N/A"
    belief_drift = _compute_belief_drift()
    with _bot_health_lock:
        bot_st = _bot_health["state"].value.upper()
        bot_fail = _bot_health["failures"]
        bot_reason = _bot_health["reason"] or "OK"
    db_queue_sz = _db_queue.qsize()
    with _CONTEXT_CACHE_LOCK:
        ctx_cache_sz = len(_CONTEXT_CACHE)
    intel = get_intelligence_metrics()
    regime, penalty = get_regime_with_inertia("BTC")
    reaction = get_current_reaction()
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    mode = get_execution_mode_v10(ctx, reaction, 0.5, event_adj)[0].value.upper()
    
    # ========== CACHE MANAGER STATS ==========
    cache_size = CACHE.size()
    cache_keys = ", ".join(CACHE.keys()[:10]) if cache_size > 0 else "empty"

    text = f"""
🩺 *HEALTH V10 – PRODUCTION*
━━━━━━━━━━━━━━━━━━━━━━
🖥️ CPU: {cpu} | RAM: {mem}
🧵 Threads: {threading.active_count()}

📦 *Cache*
├─ Candles: {cache_sz} | Ctx: {ctx_cache_sz}
├─ Hypotheses: {hyp_sz}
├─ DB queue: {db_queue_sz}
├─ CacheManager items: {cache_size}
├─ CacheManager keys: {cache_keys[:60]}{'...' if len(cache_keys) > 60 else ''}
🎯 Active: {active_sz} | Pending: {pending_sz}
📝 Traces: {trace_sz}

⚡ *Context (BTC)*
├─ Shock: {ctx.shock_score:.0f}% | Transition: {ctx.transition_prob:.0f}%
├─ Tension: {ctx.tension:.0f}% | Snap age: {snap_age}
├─ Regime: {regime} (inertia: {penalty:.0f}%)
├─ Mode: {mode}
└─ Decision latency: {decision_latency}

🌍 *Market*
├─ Breath Bull: {breath['bull']*100:.0f}%
├─ Participation: {breath['participation']*100:.0f}%
└─ Rotation: {breath['rotation']:+.1f}%

📅 *Events*
├─ Importance: {event_adj.get('importance', 0):.0f}%
└─ Bias: {event_adj.get('bias', 0):+.0f}

🤖 *Bot Health*
├─ State: {bot_st} | Failures: {bot_fail}
└─ Reason: {bot_reason[:60]}

📊 *Intelligence*
├─ Transition Acc: {intel['transition_accuracy']:.0f}%
├─ Shock Precision: {intel['shock_precision']:.0f}%
├─ Prep Recall: {intel['preparation_recall']:.0f}%
├─ Decision Consistency: {intel['decision_consistency']:.0f}%
├─ Belief drift: {belief_drift:.2f}
└─ Sanity: {sane} | {_market_sanity.get('reason','OK')}

✅ Running: {RUNTIME.is_running()}
"""
    bot.reply_to(m, text, parse_mode='Markdown')

@bot.message_handler(commands=['intel'])
def cmd_intel(m):
    if m.from_user.id != USER_ID:
        return
    intel = get_intelligence_metrics()
    text = f"""
🧠 *INTELLIGENCE METRICS* (V10)
━━━━━━━━━━━━━━━━━━━━━━
🔄 Transition Accuracy: {intel['transition_accuracy']:.0f}%
⚡ Shock Precision: {intel['shock_precision']:.0f}%
🎯 Preparation Recall: {intel['preparation_recall']:.0f}%
📊 Decision Consistency: {intel['decision_consistency']:.0f}%
📉 Belief Drift: {intel['belief_stability']:.2f}
🎯 Execution Precision: {intel['execution_precision']:.1f}%

💡 *Interpretasi*
├─ Transition > 70%: bagus baca perubahan regime
├─ Shock > 70%: bagus prediksi volatility
├─ Preparation > 70%: siap sebelum gerak
└─ Consistency > 70%: DE berkorelasi dengan outcome
"""
    bot.reply_to(m, text, parse_mode='Markdown')
    

# ============================================================
# PART 41 – MAIN + SIGNAL HANDLER + GRACEFUL SHUTDOWN + PENDING SETUPS
# ============================================================

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--paper', action='store_true')
    return p.parse_args()

def signal_handler(sig, frame):
    graceful_shutdown()
    sys.exit(0)

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
                        "execution_mode_v10": "NORMAL",
                        "intent_type": "", "decision_energy": 0.0, "position_size_mult": 1.0,
                        "filter_score": 100.0, "why_not": "no deterrents",
                        "trigger_strength": trigger_strength, "belief_state": "SEEKING",
                        "commitment_score": 0.0, "time_pressure": "normal",
                        "mode_aggressive": 0.0, "mode_balanced": 1.0, "mode_precision": 0.0,
                        "intent_success": 0.5, "context_age": 0.0,
                        "event_importance": 0, "event_bias": 0,
                        "reaction_mode": "NORMAL",
                        "hypothesis": {"thesis": thesis.statement, "invalidate": thesis.invalidation,
                                       "observe": thesis.confirmation, "destination": thesis.destination,
                                       "timeframe": thesis.timeframe},
                        "explanation": f"⚡ Thesis triggered: {trigger_reason}\n📋 {thesis.statement}"
                    }
                    send_alert_v10(alert)

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
if __name__ == "__main__":
    args = parse_args()
    PAPER_MODE = args.paper
    logger.info(f"Starting Smart Entry Engine V10 - Reaction Engine in {'PAPER' if PAPER_MODE else 'LIVE'} mode")
    init_db()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    threads = [
        threading.Thread(target=scheduled_state_engine_v10, daemon=True),
        threading.Thread(target=scheduled_trigger_engine_v7, daemon=True),
        threading.Thread(target=scheduled_shadow_evaluation_v7, daemon=True),
        threading.Thread(target=scheduled_cleanup_v7, daemon=True),
        threading.Thread(target=monitor_pending_setups_v6, daemon=True),
        threading.Thread(target=cleanup_memory_v10, daemon=True, name="mem_cleanup"),
        threading.Thread(target=_db_writer_loop, daemon=True, name="db_writer"),
        threads = [
    threading.Thread(target=scheduled_state_engine_v10, daemon=True),
    threading.Thread(target=scheduled_trigger_engine_v7, daemon=True),
    threading.Thread(target=scheduled_shadow_evaluation_v7, daemon=True),
    threading.Thread(target=scheduled_cleanup_v7, daemon=True),
    threading.Thread(target=monitor_pending_setups_v6, daemon=True),
    threading.Thread(target=cleanup_memory_v10, daemon=True, name="mem_cleanup"),
    threading.Thread(target=_db_writer_loop, daemon=True, name="db_writer"),
    threading.Thread(target=log_snapshot_metrics, daemon=True, name="metrics_logger"),  # NEW
    ]
    ]
    for t in threads:
        t.start()

    poll_failures = 0
    while RUNTIME.is_running():
        try:
            logger.info(f"Starting bot polling V10... (failures so far: {poll_failures})")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
            poll_failures = 0
            with _bot_health_lock:
                _bot_health["state"] = BotHealthState.HEALTHY
                _bot_health["failures"] = 0
                _bot_health["reason"] = ""
        except Exception as e:
            if not RUNTIME.is_running():
                break
            poll_failures += 1
            backoff = min(60, poll_failures * 5)
            with _bot_health_lock:
                _bot_health["failures"] = poll_failures
                _bot_health["last_failure"] = time.time()
                _bot_health["reason"] = str(e)
                if poll_failures >= 10:
                    _bot_health["state"] = BotHealthState.FAILED
                elif poll_failures >= 5:
                    _bot_health["state"] = BotHealthState.DEGRADED
                else:
                    _bot_health["state"] = BotHealthState.RECOVERY
            logger.error(f"Bot polling error (fail#{poll_failures}): {e}, retry in {backoff}s")
            time.sleep(backoff)
