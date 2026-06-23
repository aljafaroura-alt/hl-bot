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
import random
import traceback
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

# ============================================================
# P1+P2 FIX IMPORTS – SCALING EXIT + ADAPTIVE THRESHOLD
# ============================================================
from dataclasses import dataclass as p1p2_dataclass

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
    "MIN_EVIDENCE_FAMILIES": 1,
    "UNCLEAR_THRESHOLD": 55,
    "UNCLEAR_DIFF": 15,
    "MIN_DATA_CONFIDENCE": 45,
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
    "ENTROPY_THRESHOLD_FACTOR": 0.2,
    "RR_FLOOR_ABSOLUTE": 1.40,
    "VELOCITY_ENABLED": True,
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
    "JOURNAL_MAX_BASE": 1000,
    "JOURNAL_MAX_PER_COIN": 100,
    "JOURNAL_MAX_ABS": 10000,
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
    "CONTEXT_STALE_THRESHOLD": 15,
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
    # V10 Shadow Mode
    "SHADOW_MODE": True,
    # ===== INTELLIGENT AGGRESSION (GENIUS MODE) =====
    "ADAPTIVE_MAX_RELAX": 6,
    "RELAX_THESIS_MIN": 10,
    "RELAX_CONF_RATE_THRESHOLD": 0.05,
    "RESET_CONF_RATE_THRESHOLD": 0.15,
    "SHADOW_ENABLED": True,
    "SHADOW_SIZE_RATIO": 0.25,
    "SHADOW_MAX_GAP": 10,
    "SHADOW_MIN_THESIS": 1,
    "SHADOW_MAX_RR_GAP": 0.25,
    "SHADOW_MIN_TRADES": 200,
    "SHADOW_WINRATE_THRESHOLD": 0.55,
    "SHADOW_PROFIT_FACTOR_THRESHOLD": 1.4,
    "SHADOW_MAX_DRAWDOWN_THRESHOLD": 10.0,
}

# ============================================================
# P1+P2: TRADE MANAGER – SCALING EXIT ENGINE
# ============================================================

@dataclass
class PartialTPLevel:
    """Tier untuk partial TP"""
    price: float
    size_pct: float
    label: str
    is_hit: bool = False
    close_time: Optional[float] = None

@dataclass
class OpenPosition:
    """Struktur track posisi open"""
    signal_id: str
    coin: str
    direction: str
    entry: float
    sl: float
    entry_time: float
    
    # Multi-tier TP targets
    tp1: PartialTPLevel
    tp2: PartialTPLevel
    tp3: PartialTPLevel
    
    # Dynamic tracking
    highest: float = field(default_factory=lambda: 0)
    lowest: float = field(default_factory=lambda: 0)
    trailing_activated: bool = False
    trail_distance_pct: float = 0.0
    
    # Status
    status: str = "OPEN"  # OPEN, CLOSED, PARTIAL
    exit_reason: Optional[str] = None
    exit_time: Optional[float] = None
    final_pnl: float = 0.0
    captured_tp_levels: int = 0  # 0-3
    
    def update_extremes(self, current_price: float):
        """Update highest/lowest untuk MFE/MAE tracking"""
        if self.highest == 0:
            self.highest = current_price
            self.lowest = current_price
        else:
            self.highest = max(self.highest, current_price)
            self.lowest = min(self.lowest, current_price)

class TradeManager:
    """LIVE EXIT BRAIN — Process open positions with scaling exit"""
    
    def __init__(self):
        self.positions: Dict[str, OpenPosition] = {}
        self.check_interval = 60  # Cek tiap 60 detik
        self._lock = threading.RLock()
        self._last_check = 0
    
    def add_position(self, signal_id: str, coin: str, direction: str,
                     entry: float, sl: float, tp_targets: Dict, entry_time: float):
        """Register posisi baru dengan scaled targets"""
        with self._lock:
            pos = OpenPosition(
                signal_id=signal_id,
                coin=coin,
                direction=direction,
                entry=entry,
                sl=sl,
                entry_time=entry_time,
                highest=entry,
                lowest=entry,
                tp1=PartialTPLevel(
                    price=tp_targets["tp1"]["price"],
                    size_pct=tp_targets["tp1"]["size_pct"],
                    label=tp_targets["tp1"]["label"]
                ),
                tp2=PartialTPLevel(
                    price=tp_targets["tp2"]["price"],
                    size_pct=tp_targets["tp2"]["size_pct"],
                    label=tp_targets["tp2"]["label"]
                ),
                tp3=PartialTPLevel(
                    price=tp_targets["tp3"]["price"],
                    size_pct=tp_targets["tp3"]["size_pct"],
                    label=tp_targets["tp3"]["label"]
                )
            )
            self.positions[signal_id] = pos
            logger.info(f"✅ P1: Position registered {coin}: entry={entry:.4f}, tp1={pos.tp1.price:.4f}, tp3={pos.tp3.price:.4f}")
    
    def check_all_positions(self, snapshot) -> List[Dict]:
        """Cek semua posisi dan close via partial TPs"""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return []
        
        closed_trades = []
        
        with self._lock:
            for signal_id, pos in list(self.positions.items()):
                if pos.status != "OPEN":
                    continue
                
                if not snapshot or pos.coin not in snapshot.mids:
                    continue
                
                current_price = snapshot.mids[pos.coin]
                pos.update_extremes(current_price)
                
                # ===== CHECK TP1 =====
                if not pos.tp1.is_hit and self._check_tp_hit(pos, "tp1", current_price):
                    self._execute_partial(pos, "tp1")
                    pos.tp1.is_hit = True
                    pos.tp1.close_time = now
                
                # ===== CHECK TP2 (only if TP1 hit) =====
                if pos.tp1.is_hit and not pos.tp2.is_hit and self._check_tp_hit(pos, "tp2", current_price):
                    self._execute_partial(pos, "tp2")
                    pos.tp2.is_hit = True
                    pos.tp2.close_time = now
                
                # ===== TRAILING STOP (after TP2) =====
                if pos.tp2.is_hit and not pos.tp3.is_hit:
                    self._update_trailing_stop(pos, current_price)
                
                # ===== CHECK TP3 OR TRAILING EXIT =====
                if pos.tp2.is_hit and not pos.tp3.is_hit:
                    if self._check_tp_hit(pos, "tp3", current_price):
                        result = self._close_remaining(pos, "tp3_hit", current_price)
                        closed_trades.append(result)
                        pos.status = "CLOSED"
                        continue
                    
                    if self._check_trailing_sl(pos, current_price):
                        result = self._close_remaining(pos, "trailing_sl", current_price)
                        closed_trades.append(result)
                        pos.status = "CLOSED"
                        continue
                
                # ===== TIME-BASED EXIT =====
                age_minutes = (now - pos.entry_time) / 60
                if age_minutes > 120 and pos.tp1.is_hit and not pos.tp2.is_hit:
                    result = self._close_remaining(pos, "timeout_tp2", current_price)
                    closed_trades.append(result)
                    pos.status = "CLOSED"
                    continue
                
                # ===== CHECK SL =====
                if self._check_sl_hit(pos, current_price):
                    result = self._close_remaining(pos, "sl_hit", current_price)
                    closed_trades.append(result)
                    pos.status = "CLOSED"
        
        self._last_check = now
        return closed_trades
    
    def _check_tp_hit(self, pos: OpenPosition, tp_level: str, current_price: float) -> bool:
        tp = getattr(pos, tp_level)
        if pos.direction == "LONG":
            return current_price >= tp.price * 0.99
        else:
            return current_price <= tp.price * 1.01
    
    def _check_sl_hit(self, pos: OpenPosition, current_price: float) -> bool:
        if pos.direction == "LONG":
            return current_price <= pos.sl * 1.001
        else:
            return current_price >= pos.sl * 0.999
    
    def _check_trailing_sl(self, pos: OpenPosition, current_price: float) -> bool:
        if not pos.trailing_activated:
            return False
        if pos.direction == "LONG":
            return current_price <= pos.sl * 1.001
        else:
            return current_price >= pos.sl * 0.999
    
    def _update_trailing_stop(self, pos: OpenPosition, current_price: float):
        if not pos.trailing_activated and \
           ((pos.direction == "LONG" and current_price > pos.entry) or \
            (pos.direction == "SHORT" and current_price < pos.entry)):
            pos.trailing_activated = True
        
        if pos.trailing_activated:
            trail_pct = pos.trail_distance_pct if pos.trail_distance_pct > 0 else 0.5
            
            if pos.direction == "LONG":
                new_sl = current_price * (1 - trail_pct / 100)
                if new_sl > pos.sl:
                    pos.sl = new_sl
            else:
                new_sl = current_price * (1 + trail_pct / 100)
                if new_sl < pos.sl:
                    pos.sl = new_sl
    
    def _execute_partial(self, pos: OpenPosition, tp_level: str):
        tp = getattr(pos, tp_level)
        pnl_pct = ((tp.price - pos.entry) / pos.entry * 100) if pos.direction == "LONG" \
                  else ((pos.entry - tp.price) / pos.entry * 100)
        logger.info(f"🎯 P1: PARTIAL TP {pos.coin} | {tp_level.upper()} ({tp.size_pct*100:.0f}%) | PnL: {pnl_pct:+.2f}%")
        pos.captured_tp_levels += 1
    
    def _close_remaining(self, pos: OpenPosition, reason: str, current_price: float) -> Dict:
        if pos.direction == "LONG":
            final_pnl = (current_price - pos.entry) / pos.entry * 100
            mfe = (pos.highest - pos.entry) / pos.entry * 100
            mae = (pos.lowest - pos.entry) / pos.entry * 100
        else:
            final_pnl = (pos.entry - current_price) / pos.entry * 100
            mfe = (pos.entry - pos.lowest) / pos.entry * 100
            mae = (pos.entry - pos.highest) / pos.entry * 100
        
        age_minutes = (time.time() - pos.entry_time) / 60
        logger.info(f"🚪 P1: CLOSE {pos.coin} | {reason} | PnL: {final_pnl:+.2f}% | MFE: {mfe:+.2f}% | MAE: {mae:+.2f}%")
        
        pos.status = "CLOSED"
        pos.exit_reason = reason
        pos.exit_time = time.time()
        pos.final_pnl = final_pnl
        
        return {
            "signal_id": pos.signal_id,
            "coin": pos.coin,
            "direction": pos.direction,
            "entry": pos.entry,
            "exit": current_price,
            "sl": pos.sl,
            "pnl": final_pnl,
            "reason": reason,
            "tp_levels_captured": pos.captured_tp_levels,
            "mfe": mfe,
            "mae": mae,
            "duration_minutes": age_minutes,
            "exit_time": pos.exit_time
        }
    
    def get_open_count(self) -> int:
        with self._lock:
            return sum(1 for p in self.positions.values() if p.status == "OPEN")
    
    def get_positions_summary(self) -> Dict:
        with self._lock:
            total = len(self.positions)
            open_count = sum(1 for p in self.positions.values() if p.status == "OPEN")
            partial_count = sum(1 for p in self.positions.values() if p.status == "PARTIAL")
            closed_count = sum(1 for p in self.positions.values() if p.status == "CLOSED")
            
            avg_pnl = 0
            if closed_count > 0:
                avg_pnl = sum(p.final_pnl for p in self.positions.values() if p.status == "CLOSED") / closed_count
            
            return {"total": total, "open": open_count, "partial": partial_count, "closed": closed_count, "avg_pnl": avg_pnl}

# Global trade manager instance
TRADE_MANAGER = TradeManager()

# ============================================================
# TRADE AUDIT & LIFECYCLE FUNCTIONS (P0 LIFECYCLE FIX)
# ============================================================

def audit_trade_state() -> Dict[str, int]:
    """
    Compare DB open trades vs TradeManager.positions
    Detect orphan trades blocking inventory
    
    [P0 LIFECYCLE FIX #1]
    """
    try:
        # Count DB open trades - PAKE TABEL signals, BUKAN signal_outcomes
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0")
        db_open = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        logger.error(f"DB audit error: {e}")
        db_open = 0
    
    # Count managed trades
    managed_open = len(TRADE_MANAGER.positions)
    
    # Calculate orphan count
    orphan_count = max(0, db_open - managed_open)
    
    audit_result = {
        "db_open": db_open,
        "manager_open": managed_open,
        "orphan_count": orphan_count
    }
    
    if orphan_count > 0:
        logger.warning(
            f"🔴 TRADE_AUDIT db={db_open} managed={managed_open} orphan={orphan_count} "
            f"⚠️ ORPHANS BLOCKING INVENTORY"
        )
    else:
        logger.info(f"✅ TRADE_AUDIT db={db_open} managed={managed_open} orphan=0")
    
    return audit_result


def get_exposure_adjusted_threshold(base_threshold: int, open_positions: int = None) -> int:
    """
    Adjust threshold based on actual open exposure
    
    More open = more selective (higher threshold)
    Fewer open = more aggressive (lower threshold)
    
    [P0 LIFECYCLE FIX #2]
    """
    if open_positions is None:
        open_positions = len(TRADE_MANAGER.positions)
    
    adjusted = base_threshold
    
    if open_positions > 50:
        # WAY TOO MANY = SUPER CONSERVATIVE
        adjusted = base_threshold + 30
        logger.warning(f"🔴 EXPOSURE LIMIT: {open_positions} positions open → threshold +30")
    elif open_positions > 35:
        # TOO MANY = VERY CONSERVATIVE
        adjusted = base_threshold + 20
        logger.warning(f"🟠 HIGH EXPOSURE: {open_positions} positions → threshold +20")
    elif open_positions > 20:
        # MANY = CONSERVATIVE
        adjusted = base_threshold + 10
        logger.debug(f"🟡 MODERATE EXPOSURE: {open_positions} positions → threshold +10")
    elif open_positions > 10:
        # SOME = SLIGHTLY CONSERVATIVE
        adjusted = base_threshold + 5
        logger.debug(f"🟢 LIGHT EXPOSURE: {open_positions} positions → threshold +5")
    else:
        # FEW = BASELINE OR AGGRESSIVE
        adjusted = base_threshold
    
    return adjusted


def emergency_lifecycle_cleanup():
    """
    Close stale positions that haven't updated in 48 hours
    Prevent orphans from accumulating
    
    [P0 LIFECYCLE FIX #4] STALE_EXPIRY cleanup
    """
    MAX_TRADE_AGE_SECONDS = 48 * 3600  # 48 hours
    now = time.time()
    cleaned = 0
    
    try:
        snapshot = get_snapshot()
        if not snapshot:
            logger.warning("emergency_cleanup: no snapshot, aborting")
            return 0
        
        # Iterate through manager positions
        positions_to_clean = []
        for signal_id, pos in list(TRADE_MANAGER.positions.items()):
            age_seconds = now - pos.entry_time
            if age_seconds > MAX_TRADE_AGE_SECONDS:
                positions_to_clean.append((signal_id, pos, age_seconds))
        
        # Close each stale position
        for signal_id, pos, age_seconds in positions_to_clean:
            try:
                current_price = snapshot.mids.get(pos.coin, pos.entry)
                result = TRADE_MANAGER._close_remaining(pos, "stale_expiry", current_price)
                
                # Log closure
                logger.warning(
                    f"🔄 STALE CLOSE {pos.coin}: age={age_seconds/3600:.1f}h → closed "
                    f"pnl={result.get('pnl', 0):.2f} usd"
                )
                
                # Update DB outcome
                try:
                    update_signal_outcome_v7(
                        signal_id, "STALE_EXPIRY",
                        result.get("pnl", 0),
                        current_price,
                        result.get("mfe", 0),
                        result.get("mae", 0)
                    )
                except Exception as e:
                    logger.error(f"Failed to update DB for stale {pos.coin}: {e}")
                
                cleaned += 1
            except Exception as e:
                logger.error(f"Failed to close stale {pos.coin}: {e}")
        
        if cleaned > 0:
            logger.warning(f"🔄 EMERGENCY CLEANUP: closed {cleaned} stale trades")
        
        return cleaned
    except Exception as e:
        logger.error(f"emergency_cleanup error: {e}")
        return 0



def calculate_scaled_targets(entry: float, direction: str, atr_pct: float, market_regime: str) -> Dict:
    """P1: Multi-TP dengan scaling berdasarkan regime"""
    
    if direction == "LONG":
        tp1 = entry * (1 + atr_pct * 0.015)
        tp2 = entry * (1 + atr_pct * 0.035)
        tp3 = entry * (1 + atr_pct * 0.06)
    else:
        tp1 = entry * (1 - atr_pct * 0.015)
        tp2 = entry * (1 - atr_pct * 0.035)
        tp3 = entry * (1 - atr_pct * 0.06)
    
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        if direction == "LONG":
            tp2 = entry * (1 + atr_pct * 0.045)
            tp3 = entry * (1 + atr_pct * 0.08)
        else:
            tp2 = entry * (1 - atr_pct * 0.045)
            tp3 = entry * (1 - atr_pct * 0.08)
    elif market_regime == "RANGING":
        if direction == "LONG":
            tp1 = entry * (1 + atr_pct * 0.01)
            tp2 = entry * (1 + atr_pct * 0.025)
            tp3 = entry * (1 + atr_pct * 0.04)
        else:
            tp1 = entry * (1 - atr_pct * 0.01)
            tp2 = entry * (1 - atr_pct * 0.025)
            tp3 = entry * (1 - atr_pct * 0.04)
    
    return {
        "tp1": {"price": tp1, "size_pct": 0.25, "label": "TP1 (25%)"},
        "tp2": {"price": tp2, "size_pct": 0.50, "label": "TP2 (50%)"},
        "tp3": {"price": tp3, "size_pct": 0.25, "label": "TP3 (25%)"},
    }

def get_adaptive_threshold(market_regime: str, entropy_market: int, recent_win_rate: float, execution_count: int = 0) -> int:
    """P2: Adaptive threshold lowering (75 → 60-65)
    
    [P0 LIFECYCLE FIX #3] Use actual open_positions instead of exec_count
    """
    
    base = 65  # Baseline turun dari 75
    
    # === INSTRUMENTATION: THRESHOLD_START ===
    logger.info(
        f"THRESHOLD_START "
        f"regime={market_regime} "
        f"entropy={entropy_market} "
        f"wr={recent_win_rate:.2f} "
        f"exec_count={execution_count} "
        f"base={base}"
    )
    
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base -= 5  # 60 — trending = agresif
    elif market_regime == "RANGING":
        base += 3  # 68 — ranging = selective
    elif market_regime == "EXPANDING":
        base -= 2  # 63 — expansion = balanced
    
    if entropy_market < 30:
        base -= 3  # Structure clear
    elif entropy_market > 60:
        base += 5  # Chaos
    
    if recent_win_rate > 0.65:
        base -= 5  # High WR = confidence
    elif recent_win_rate > 0.55:
        base -= 2
    elif recent_win_rate == 0.0:
        base = int(base * 1.3)  # ZERO WR = hukuman keras, threshold +30%
    elif recent_win_rate < 0.35:
        base += 5  # Low WR = protect
    
    # === FIX: Use REAL OPEN POSITIONS instead of exec_count ===
    open_positions = len(TRADE_MANAGER.positions)
    if open_positions > 3:
        exposure_penalty = min(20, open_positions * 2)  # Max +20
        base += exposure_penalty
        logger.debug(f"🔴 EXPOSURE PENALTY: {open_positions} open → +{exposure_penalty} to threshold")
    
    final = max(50, min(95, base))
    
    # === INSTRUMENTATION: THRESHOLD_FINAL ===
    logger.info(
        f"THRESHOLD_FINAL "
        f"value={final} "
        f"exec_count={execution_count} "
        f"open_positions={open_positions} "
        f"regime={market_regime}"
    )
    
    logger.debug(f"📊 P2: THRESHOLD: base={base} (regime={market_regime}, entropy={entropy_market}, wr={recent_win_rate:.0%}, open={open_positions}) → final={final}")
    
    return final

# ========== LOGGING ==========
DB_PATH = "signals.db"
SIGNALS_DB_PATH = DB_PATH
LOG_DIR = "logs"
PAPER_MODE = False

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
TRACE_ENABLED = os.environ.get("TRACE", "0") == "1"

os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("SmartEntryEngine")
logger.setLevel(logging.DEBUG)  # ← logger always DEBUG, handlers filter
logger.propagate = False  # 🔥 cegah log naik ke root logger (sumber duplikasi di terminal)

# Cegah duplikat handler kalau modul ini sempat ke-import ulang / bot di-restart tanpa keluar proses
if logger.handlers:
    logger.handlers.clear()

file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "engine.log"), maxBytes=10*1024*1024, backupCount=5
)
file_handler.setLevel(logging.DEBUG)  # ← file always DEBUG (audit trail)

error_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "error.log"), maxBytes=5*1024*1024, backupCount=3
)
error_handler.setLevel(logging.ERROR)

console = logging.StreamHandler()
console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))  # ← console respects LOG_LEVEL

file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_formatter = logging.Formatter('[%(levelname)s] %(message)s')

file_handler.setFormatter(file_formatter)
error_handler.setFormatter(file_formatter)
console.setFormatter(console_formatter)

logger.addHandler(file_handler)
logger.addHandler(error_handler)
logger.addHandler(console)

def trace(msg: str):
    """Log raw metrics only if TRACE_ENABLED."""
    if TRACE_ENABLED:
        logger.debug(msg)

# ========== LOG CACHE (per cycle) ==========
_cycle_debug: Dict[str, List[str]] = {}
_cycle_debug_lock = threading.RLock()

def cycle_log(section: str, msg: str):
    """Collect logs during one engine cycle."""
    with _cycle_debug_lock:
        if section not in _cycle_debug:
            _cycle_debug[section] = []
        _cycle_debug[section].append(msg)

def flush_cycle_logs():
    """Print all collected logs as one line per section."""
    with _cycle_debug_lock:
        for section, msgs in _cycle_debug.items():
            if msgs:
                unique = list(dict.fromkeys(msgs))  # preserve order, remove dupes
                logger.debug(f"[{section}] " + " | ".join(unique))
        _cycle_debug.clear()

# ========== LOG ONCE (per TTL) ==========
_log_once_cache: Dict[str, float] = {}
_log_once_lock = threading.RLock()

def log_once(key: str, msg: str, ttl: float = 30.0):
    """Log message only once per TTL interval."""
    with _log_once_lock:
        now = time.time()
        if key in _log_once_cache and now - _log_once_cache[key] < ttl:
            return
        _log_once_cache[key] = now
    logger.debug(msg)

# ========== OPERATOR MODE ==========
OPERATOR_MODE = os.environ.get("OPERATOR_MODE", "TRADER")


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
            trigger_api_cooldown(25)
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
class ZoneQuality:
    """Quality score for OB/FVG zones"""
    score: int
    freshness: float      # 0-100
    mitigation: float     # 0-100
    displacement: float   # 0-100
    volume: float         # 0-100
    alignment: float      # 0-100 (HTF alignment)
    components: Dict[str, float] = field(default_factory=dict)
    
    def summary(self) -> str:
        return f"score:{self.score} | F:{self.freshness:.0f} M:{self.mitigation:.0f} D:{self.displacement:.0f} V:{self.volume:.0f} A:{self.alignment:.0f}"

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
    signal_id: Optional[str] = None       # ← NYALA! Buat match dengan TradeManager
    outcome: Optional[str] = None
    pnl: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    closed: bool = False                  # ← NYALA! Tracking status
    close_reason: Optional[str] = None    # ← NYALA! Kenapa closed
    duration_minutes: Optional[float] = None  # ← NYALA! Berapa lama posisi

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
# PATCH 3: Warmup Mode
START_TIME = time.time()

def is_warmup() -> bool:
    """Return True if bot is still in warmup phase (first 30 minutes)"""
    return (time.time() - START_TIME) < 1800

def get_uptime_minutes() -> int:
    """Return uptime in minutes"""
    return int((time.time() - START_TIME) // 60)

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

# GENIUS MODE: Shadow Discovery Stats
_shadow_stats: Dict[str, Any] = {
    "total": 0,
    "coins": {},
    "results": deque(maxlen=200),
}
_shadow_stats_lock = threading.RLock()

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
# V10: OPPORTUNITY ENGINE (Institutional Funnel Tracking)
_opportunity_stats = {
    "scanned": 0,
    "qualified": 0,
    "executed": 0,
    "rejected": 0,
    "last_reset": time.time(),
    "rejection_reasons": {},
    "funnel": {
        "universe": 0,
        "liquid": 0,
        "context_valid": 0,
        "confidence": 0,
        "conviction": 0,
        "executed": 0,
    },
    "session_entries": 0,
}
_opportunity_lock = threading.RLock()

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

# ============================================================
# INTENT PENDING BUFFER (Proposed → Accepted)
# ============================================================

_intent_pending: Dict[str, Dict[str, Any]] = {}
_intent_pending_lock = threading.RLock()
_INTENT_PENDING_TTL = 300  # 5 minutes

def migrate_journal_entries():
    """
    Backfill missing fields on old journal entries.
    Ensures backward compatibility with journal entries created before this patch.
    
    FUNGSI INI BERJALAN SEKALI DI STARTUP
    """
    global _decision_journal
    
    with _journal_lock:  # ← AMAN UNTUK MULTI-THREAD
        migrated = 0
        for entry in _decision_journal:  # ← LOOP SEMUA ENTRI
            # CEK APAKAH FIELD signal_id ADA?
            if not hasattr(entry, "signal_id"):
                entry.signal_id = None  # ← KALO GA ADA, TAMBAHKAN
                migrated += 1
            
            # CEK APAKAH FIELD closed ADA?
            if not hasattr(entry, "closed"):
                entry.closed = False  # ← KALO GA ADA, TAMBAHKAN
                migrated += 1
            
            # CEK APAKAH FIELD close_reason ADA?
            if not hasattr(entry, "close_reason"):
                entry.close_reason = None  # ← KALO GA ADA, TAMBAHKAN
                migrated += 1
            
            # CEK APAKAH FIELD duration_minutes ADA?
            if not hasattr(entry, "duration_minutes"):
                entry.duration_minutes = None  # ← KALO GA ADA, TAMBAHKAN
                migrated += 1
        
        # LOG HASILNYA
        if migrated > 0:
            logger.info(f"✅ Backfilled {migrated} fields on {len(_decision_journal)} journal entries")
        else:
            logger.info(f"✅ No migration needed for {len(_decision_journal)} journal entries")
            
def propose_intent(coin: str, vector: List[float], event_type: str, direction: str):
    """Propose an intent (thesis-level, before execution)."""
    with _intent_pending_lock:
        _intent_pending[coin] = {
            "vector": vector,
            "event_type": event_type,
            "direction": direction,
            "proposed_at": time.time(),
            "accepted": False,
        }

def accept_intent(coin: str, acceptance_score: float):
    """Accept a proposed intent (execution-level, commit to history)."""
    with _intent_pending_lock:
        pending = _intent_pending.get(coin)
        if not pending:
            return False
        if time.time() - pending["proposed_at"] > _INTENT_PENDING_TTL:
            del _intent_pending[coin]
            return False
        with _intent_vector_lock:
            if coin not in _intent_vector_history:
                _intent_vector_history[coin] = deque(maxlen=10)
            vector = pending["vector"].copy()
            vector[1] = acceptance_score / 100.0
            _intent_vector_history[coin].append((time.time(), vector))
        pending["accepted"] = True
        del _intent_pending[coin]
        return True

def cleanup_pending_intents():
    """Clean up expired pending intents."""
    with _intent_pending_lock:
        now = time.time()
        expired = [c for c, p in _intent_pending.items()
                   if now - p["proposed_at"] > _INTENT_PENDING_TTL]
        for c in expired:
            del _intent_pending[c]
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired pending intents")

# ============================================================
# PIPELINE COUNTERS
# ============================================================

_exec_pipeline: Dict[str, int] = {}
_exec_pipeline_lock = threading.RLock()
_exec_pipeline_reset_ts: float = time.time()

def inc_pipeline_counter(key: str, n: int = 1):
    with _exec_pipeline_lock:
        _exec_pipeline[key] = _exec_pipeline.get(key, 0) + n

def reset_pipeline_counter():
    global _exec_pipeline_reset_ts
    with _exec_pipeline_lock:
        _exec_pipeline.clear()
        _exec_pipeline_reset_ts = time.time()

def compute_dcr(events: int, executed: int) -> float:
    """Decision Conversion Ratio — % events yang jadi eksekusi."""
    try:
        events = max(0, int(events))
        executed = max(0, int(executed))
        if events == 0:
            return 0.0
        return round((executed / events) * 100.0, 2)
    except Exception as e:
        logger.warning(f"DCR_FAIL: {e}")
        return 0.0

def get_pipeline_metrics() -> Dict[str, Any]:
    """Get pipeline metrics for monitoring."""
    with _exec_pipeline_lock:
        check = _exec_pipeline.get("check", 1)
        obs = _exec_pipeline.get("obs", 0)
        thesis = _exec_pipeline.get("thesis", 0)
        confidence = _exec_pipeline.get("confidence", 0)
        execute_called = _exec_pipeline.get("execute_called", 0)
        execute_pass = _exec_pipeline.get("execute_pass", 0)
        journal = _exec_pipeline.get("journal", 0)
        reject_obs = _exec_pipeline.get("reject_obs", 0)
        reject_thesis = _exec_pipeline.get("reject_thesis", 0)
        reject_conf = _exec_pipeline.get("reject_conf", 0)
        reject_execute = _exec_pipeline.get("reject_execute", 0)
        dcr = compute_dcr(obs, execute_pass)

        obs_rate = (obs / check * 100) if check > 0 else 0
        thesis_rate = (thesis / obs * 100) if obs > 0 else 0
        conf_rate = (confidence / thesis * 100) if thesis > 0 else 0
        exec_rate = (execute_pass / confidence * 100) if confidence > 0 else 0
        journal_rate = (journal / execute_pass * 100) if execute_pass > 0 else 0
        overall = (journal / check * 100) if check > 0 else 0

        total_reject = reject_obs + reject_thesis + reject_conf + reject_execute

        uptime_minutes = (time.time() - START_TIME) / 60
        is_meaningful = (uptime_minutes > 20 and check > 100)

        if is_meaningful:
            dcr = round(journal / check * 100, 1)
        else:
            dcr = None

        if check < 100:
            funnel_issue = "⏳ COLLECTING DATA"
        elif obs / check * 100 < 10:
            funnel_issue = "⚠️ OBSERVATION TOO STRICT"
        elif thesis > 10 and confidence / thesis * 100 < 20:
            funnel_issue = "⚠️ THESIS → CONFIDENCE DROP"
        elif confidence > 5 and execute_pass / confidence * 100 < 10:
            funnel_issue = "⚠️ EXECUTION DROP"
        elif execute_pass > 0 and journal / execute_pass * 100 < 50:
            funnel_issue = "⚠️ JOURNAL COMMIT ISSUE"
        else:
            funnel_issue = "✅ FUNNEL HEALTHY"

        return {
            "check": check,
            "obs": obs,
            "thesis": thesis,
            "confidence": confidence,
            "execute_called": execute_called,
            "execute_pass": execute_pass,
            "journal": journal,
            "reject_obs": reject_obs,
            "reject_thesis": reject_thesis,
            "reject_conf": reject_conf,
            "reject_execute": reject_execute,
            "obs_rate": round(obs_rate, 1),
            "thesis_rate": round(thesis_rate, 1),
            "conf_rate": round(conf_rate, 1),
            "exec_rate": round(exec_rate, 1),
            "journal_rate": round(journal_rate, 1),
            "overall": round(overall, 1),
            "total_reject": total_reject,
            "uptime_minutes": round(uptime_minutes, 1),
            "is_meaningful": is_meaningful,
            "dcr": dcr,
            "funnel_issue": funnel_issue,
        }

# ============================================================
# GENIUS MODE: ADAPTIVE RELAXATION + SHADOW DISCOVERY
# ============================================================

def get_adaptive_relaxation() -> int:
    """
    Intelligent Aggression: relax threshold berdasarkan funnel conversion,
    bukan waktu. Jika thesis hidup tapi confidence mati → buka keran.
    Jika confidence sehat → reset.
    """
    pipe = get_pipeline_metrics()
    thesis = pipe.get("thesis", 0)
    confidence = pipe.get("confidence", 0)
    scanned = pipe.get("check", 1)
    observed = pipe.get("obs", 0)

    conf_rate = confidence / max(thesis, 1)
    obs_rate = observed / max(scanned, 1)

    relax = 0
    reason = "none"

    # CASE 1: Thesis hidup tapi Confidence mati
    if thesis >= TUNABLE["RELAX_THESIS_MIN"] and conf_rate < TUNABLE["RELAX_CONF_RATE_THRESHOLD"]:
        relax = 4
        reason = f"thesis={thesis}, conf_rate={conf_rate:.1%}"

    # CASE 2: Observe terlalu ketat
    elif obs_rate < 0.1 and scanned > 30:
        relax = 2
        reason = f"obs_rate={obs_rate:.1%}"

    # RESET: conf rate sudah sehat
    if conf_rate > TUNABLE["RESET_CONF_RATE_THRESHOLD"]:
        relax = 0
        reason = "conf_rate_healthy (reset)"

    relax = min(relax, TUNABLE["ADAPTIVE_MAX_RELAX"])

    if relax > 0:
        logger.debug(f"🧠 GENIUS RELAX: -{relax} pts ({reason})")

    return relax

def log_velocity_observer(coin: str, event, candles_5m: List[dict]) -> None:
    """
    Velocity Observer: Logging only. Tidak mengubah keputusan.
    """
    if not TUNABLE.get("VELOCITY_ENABLED", True):
        return

    # Reuse logika velocity, purely untuk debug
    delta_queue = _rolling_delta.get(coin, deque())
    delta_score = 20
    if len(delta_queue) >= 3:
        delta_vals = list(delta_queue)
        delta_slope = (delta_vals[-1] - delta_vals[-2]) / 60  
        if event.direction == "LONG" and delta_slope > 0:
            delta_score = min(100, abs(delta_slope) * 15)
        elif event.direction == "SHORT" and delta_slope < 0:
            delta_score = min(100, abs(delta_slope) * 15)
        else:
            delta_score = min(100, abs(delta_slope) * 7)

    vol_spike = get_volume_spike(coin)
    vol_score = min(100, max(0, (vol_spike - 0.5) * 50))

    oi_roc = get_oi_roc(coin)
    oi_score = min(100, max(20, 20 + (oi_roc / 5) * 16))

    accept_score = 50
    if event.type == "LIQUIDITY" and candles_5m and len(candles_5m) >= 6:
        idx = event.extra.get("idx", len(candles_5m) - 3)
        reclaimed = False
        for i in range(idx + 1, min(idx + 6, len(candles_5m))):
            if event.direction == "LONG" and float(candles_5m[i]['c']) > event.price_high:
                reclaimed = True
                break
            elif event.direction == "SHORT" and float(candles_5m[i]['c']) < event.price_low:
                reclaimed = True
                break
        accept_score = 90 if reclaimed else 30

    composite = (delta_score * 0.4) + (vol_score * 0.3) + (oi_score * 0.15) + (accept_score * 0.15)
    composite = max(0, min(100, composite))
    
    # ===== INSTRUMENTATION: BREAKDOWN SCORING =====
    trace(f"[VELOCITY SCORE {coin}] delta={delta_score:.0f}, vol={vol_score:.0f}, oi={oi_score:.0f}, accept={accept_score:.0f}, composite={composite:.0f}")
    # ================================================
    
    status = "URGENT" if composite > 75 else ("ACTIVE" if composite > 50 else "NORMAL")

    trace(f"⚡ VELOCITY OBSERVER {coin}: comp={composite:.0f} ({status}) | F:{delta_score:.0f} V:{vol_score:.0f} OI:{oi_score:.0f} A:{accept_score:.0f}")


def register_shadow(coin: str, direction: str, mark: float,
                    confidence_data: Dict, event,
                    intent, belief, hl: Dict, micro_acc: Dict, failed_risk: Dict,
                    intent_drift: float, surprise: float, gap: int,
                    final_threshold: int, rr: float) -> None:
    """Shadow Discovery: catat near-miss sebagai shadow (tanpa bypass pipeline)."""
    if not TUNABLE.get("SHADOW_ENABLED", True):
        return

    score = confidence_data["final_score"]
    shadow_size = confidence_data.get("position_size_mult", 1.0) * TUNABLE["SHADOW_SIZE_RATIO"]
    shadow_size = max(0.1, min(0.5, shadow_size))

    # ===== FIX: shadow_signal_id sebelumnya undefined (NameError), bikin
    # register_shadow() selalu silent-fail lewat try/except di caller =====
    shadow_signal_id = f"SHADOW_{generate_signal_id(coin, direction)}"

    logger.info(
        f"👻 SHADOW REGISTER: {coin} {direction} "
        f"(score={score}, gap={gap}, rr={rr:.2f}, size={shadow_size:.2f}x)"
    )

    journal_entry = DecisionJournalEntry(
        timestamp=time.time(),
        coin=coin,
        event_type=event.type,
        direction=direction,
        score=score,
        mode="SHADOW_DISCOVERY",
        executed=False,
        shadow=True,
        entry=mark,
        sl=confidence_data.get("sl", 0.0),
        tp=confidence_data.get("tp", 0.0),
        rr=rr,
        intent=intent.value,
        belief=belief.value,
        decision_energy=confidence_data.get("decision_energy", 0),
        hidden_liquidity=hl.get("score", 0),
        micro_acceptance=micro_acc.get("score"),
        failed_risk=failed_risk.get("risk", 1.0),
        intent_drift=intent_drift,
        surprise=surprise,
        signal_id=shadow_signal_id,
        narrative={
            "decision_type": "SHADOW_DISCOVERY",
            "gap": gap,
            "threshold": final_threshold,
            "score": score,
            "size_mult": shadow_size,
        }
    )
    log_decision_journal(journal_entry)

    with _shadow_stats_lock:
        _shadow_stats["total"] += 1
        _shadow_stats["coins"][coin] = _shadow_stats["coins"].get(coin, 0) + 1

_regimes_cache: Dict[str, Any] = {}
_regimes_cache_lock = threading.RLock()
_REGIMES_TTL = 120

# Hyperliquid API
info = Info(constants.MAINNET_API_URL)

# Optional psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    
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
            final_score REAL DEFAULT 0,
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
            ("journal", "entropy_data", "INTEGER", "0"),
            ("journal", "entropy_market", "INTEGER", "0"),
            ("journal", "entropy_decision", "INTEGER", "0"),
            ("signals", "final_score", "REAL", "0.0"),
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


def log_decision_trace(trace: DecisionTrace):
    """Wrapper untuk save_trace_to_db, mencegah None."""
    if trace is None:
        return
    return save_trace_to_db(trace)


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
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
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


def detect_orphan_signals(limit: int = 50) -> List[Dict[str, Any]]:
    """Detect signals yang executed (ada di TradeManager) tapi gak pernah evaluated/closed.
    NOTE: tabel SQL 'journal' TIDAK punya kolom signal_id, jadi orphan check
    dilakukan terhadap signals table sendiri (evaluated=0 + umur tua) dan
    cross-check ke TRADE_MANAGER.positions (in-memory)."""
    try:
        conn = db_connect()
        c = conn.cursor()
        # Signal yang masih evaluated=0 padahal udah lama (>6 jam) -> kandidat orphan
        cutoff = int(time.time()) - 6 * 3600
        c.execute("""
            SELECT signal_id, coin, direction, timestamp
            FROM signals
            WHERE evaluated = 0 AND timestamp < ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (cutoff, limit))
        rows = c.fetchall()
        conn.close()

        with TRADE_MANAGER._lock:
            tracked_ids = set(TRADE_MANAGER.positions.keys())

        orphans = []
        for signal_id, coin, direction, ts in rows:
            if signal_id not in tracked_ids:
                orphans.append({
                    "signal_id": signal_id, "coin": coin,
                    "direction": direction, "timestamp": ts
                })

        if orphans:
            logger.warning(f"🔴 ORPHAN DETECTED: {len(orphans)} signals stale & not tracked in TradeManager")
            for o in orphans[:10]:
                logger.warning(f"   - {o['signal_id']}: {o['coin']} {o['direction']} @ {o['timestamp']}")

        return orphans
    except Exception as e:
        logger.error(f"Orphan detection error: {e}")
        return []


def check_signal_db_health() -> Dict[str, int]:
    """Diagnostic: bandingkan jumlah signal di DB vs posisi yang ke-track di TradeManager."""
    try:
        conn = db_connect()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM signals")
        total_signals = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0")
        pending_eval = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=1")
        evaluated = c.fetchone()[0]

        conn.close()

        with TRADE_MANAGER._lock:
            tracked_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
            tracked_total = len(TRADE_MANAGER.positions)

        # Orphan = signals yang evaluated=0 (DB bilang masih open) tapi TradeManager
        # gak tracking-nya sama sekali, dan udah lebih tua dari 6 jam.
        orphans = detect_orphan_signals(limit=200)
        orphan_count = len(orphans)

        health = {
            "total_signals": total_signals,
            "pending_eval": pending_eval,
            "evaluated": evaluated,
            "tracked_open_in_manager": tracked_open,
            "tracked_total_in_manager": tracked_total,
            "orphan_count": orphan_count,
            "managed_ratio_pct": int((tracked_open / max(1, pending_eval)) * 100) if pending_eval else 0,
        }

        logger.info(
            f"🏥 SIGNAL HEALTH: db_pending={pending_eval} db_evaluated={evaluated} "
            f"tracked_open={tracked_open} orphan={orphan_count} managed_ratio={health['managed_ratio_pct']}%"
        )
        return health
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {}


def update_signal_outcome_v7(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        # ===== ORPHAN GUARD: pastikan signal_id ada sebelum update =====
        c.execute("SELECT COUNT(*) FROM signals WHERE signal_id=?", (signal_id,))
        if c.fetchone()[0] == 0:
            logger.error(f"🔴 ORPHAN DETECTED: {signal_id} not in signals table, skip update")
            conn.close()
            return
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

# ============================================================
# OPPORTUNITY ENGINE (Institutional Funnel Tracking)
# ============================================================

def record_opportunity_scan(coin: str = None):
    """Record a coin was scanned."""
    with _opportunity_lock:
        _opportunity_stats["scanned"] += 1
        _opportunity_stats["funnel"]["universe"] += 1

def record_opportunity_qualified(coin: str):
    """Record a coin passed context/confidence gates."""
    with _opportunity_lock:
        _opportunity_stats["qualified"] += 1
        _opportunity_stats["funnel"]["confidence"] += 1

def record_opportunity_executed(coin: str):
    """Record an executed trade."""
    with _opportunity_lock:
        _opportunity_stats["executed"] += 1
        _opportunity_stats["session_entries"] += 1
        _opportunity_stats["funnel"]["executed"] += 1

def record_opportunity_rejected(coin: str, reason: str):
    """Record a rejected opportunity with reason."""
    with _opportunity_lock:
        _opportunity_stats["rejected"] += 1
        if reason not in _opportunity_stats["rejection_reasons"]:
            _opportunity_stats["rejection_reasons"][reason] = 0
        _opportunity_stats["rejection_reasons"][reason] += 1

def reset_opportunity_stats():
    """Reset stats daily."""
    with _opportunity_lock:
        now = time.time()
        if now - _opportunity_stats["last_reset"] > 86400:
            _opportunity_stats["scanned"] = 0
            _opportunity_stats["qualified"] = 0
            _opportunity_stats["executed"] = 0
            _opportunity_stats["rejected"] = 0
            _opportunity_stats["rejection_reasons"] = {}
            _opportunity_stats["funnel"] = {k: 0 for k in _opportunity_stats["funnel"]}
            _opportunity_stats["session_entries"] = 0
            _opportunity_stats["last_reset"] = now

def get_opportunity_metrics() -> Dict[str, Any]:
    """Get opportunity metrics for monitoring."""
    with _opportunity_lock:
        reset_opportunity_stats()
        
        scanned = _opportunity_stats["scanned"]
        qualified = _opportunity_stats["qualified"]
        executed = _opportunity_stats["executed"]
        rejected = _opportunity_stats["rejected"]
        
        # Funnel rates
        qualification_rate = (qualified / scanned * 100) if scanned > 0 else 0
        execution_rate = (executed / qualified * 100) if qualified > 0 else 0
        conversion_rate = (executed / scanned * 100) if scanned > 0 else 0
        
        # Top rejection reasons
        top_reasons = sorted(
            _opportunity_stats["rejection_reasons"].items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]
        
        return {
            "scanned": scanned,
            "qualified": qualified,
            "executed": executed,
            "rejected": rejected,
            "qualification_rate": round(qualification_rate, 1),
            "execution_rate": round(execution_rate, 1),
            "conversion_rate": round(conversion_rate, 1),
            "session_entries": _opportunity_stats["session_entries"],
            "top_rejections": top_reasons,
            "funnel": _opportunity_stats["funnel"],
        }

def get_engine_metrics() -> Dict[str, Any]:
    """Compute engine health metrics from funnel stats."""
    opp = get_opportunity_metrics()
    scanned = opp.get("scanned", 0)
    qualified = opp.get("qualified", 0)
    executed = opp.get("executed", 0)
    with _opportunity_lock:
        funnel = _opportunity_stats.get("funnel", {})
        thesis = funnel.get("context_valid", 0)
        confidence = funnel.get("confidence", 0)
    obs_rate = (qualified / scanned * 100) if scanned > 0 else 0
    thesis_yield = (thesis / qualified * 100) if qualified > 0 else 0
    survival = (executed / confidence * 100) if confidence > 0 else 0
    conversion = (executed / scanned * 100) if scanned > 0 else 0
    return {
        "scanned": scanned,
        "observed": qualified,
        "thesis": thesis,
        "confidence": confidence,
        "executed": executed,
        "obs_rate": round(obs_rate, 1),
        "thesis_yield": round(thesis_yield, 1),
        "survival": round(survival, 1),
        "conversion": round(conversion, 1),
        "funnel_health": "✅" if obs_rate > 20 and thesis_yield > 20 and survival > 10 else "⚠️",
    }

# ============================================================
# CONVICTION BUDGET (Institutional Position Sizing)
# ============================================================

def compute_conviction_budget(context: Dict, event: Dict, market: Dict) -> Dict[str, Any]:
    """
    Conviction Budget = base minus penalties.
    Used for POSITION SIZING, not threshold adjustment.
    Institutional principle: Entry quality ≠ position size.
    """
    base = 100
    
    penalties = []
    total_penalty = 0
    
    # 1. Event risk penalty
    event_risk = context.get("event_risk", 0)
    if event_risk > 70:
        penalty = 20
        penalties.append(f"event_risk -{penalty}")
        total_penalty += penalty
    elif event_risk > 40:
        penalty = 10
        penalties.append(f"event_risk -{penalty}")
        total_penalty += penalty
    
    # 2. Drift penalty
    drift = event.get("intent_drift", 0)
    if drift > 0.7:
        penalty = 20
        penalties.append(f"drift -{penalty}")
        total_penalty += penalty
    elif drift > 0.4:
        penalty = 10
        penalties.append(f"drift -{penalty}")
        total_penalty += penalty
    
    # 3. Regime penalty
    regime = context.get("regime", "UNKNOWN")
    if regime in ["CHAOS", "PANIC"]:
        penalty = 25
        penalties.append(f"regime -{penalty}")
        total_penalty += penalty
    elif regime == "VOLATILE":
        penalty = 15
        penalties.append(f"regime -{penalty}")
        total_penalty += penalty
    
    # 4. Breadth penalty
    breath_bull = market.get("breath_bull", 0.5)
    if event.get("direction") == "LONG" and breath_bull < 0.35:
        penalty = 15
        penalties.append(f"breath -{penalty}")
        total_penalty += penalty
    elif event.get("direction") == "SHORT" and breath_bull > 0.65:
        penalty = 15
        penalties.append(f"breath -{penalty}")
        total_penalty += penalty
    
    # 5. Data quality penalty
    data_conf = context.get("data_confidence", 80)
    if data_conf < 60:
        penalty = 20
        penalties.append(f"data -{penalty}")
        total_penalty += penalty
    elif data_conf < 75:
        penalty = 10
        penalties.append(f"data -{penalty}")
        total_penalty += penalty
    
    # 6. Fatigue penalty
    fatigue = context.get("fatigue_penalty", 1.0)
    if fatigue < 0.5:
        penalty = 15
        penalties.append(f"fatigue -{penalty}")
        total_penalty += penalty
    elif fatigue < 0.7:
        penalty = 8
        penalties.append(f"fatigue -{penalty}")
        total_penalty += penalty
    
    conviction = max(0, base - total_penalty)
    
    # ===== POSITION SIZING (NOT THRESHOLD) =====
    if conviction >= 80:
        size_mult = 1.0
        mode = "FULL"
    elif conviction >= 60:
        size_mult = 0.7
        mode = "NORMAL"
    elif conviction >= 45:
        size_mult = 0.4
        mode = "REDUCED"
    else:
        size_mult = 0.0
        mode = "SKIP"
    
    return {
        "conviction": conviction,
        "mode": mode,
        "size_mult": size_mult,
        "penalties": penalties,
        "total_penalty": total_penalty,
        "is_qualified": conviction >= 45,  # Minimum conviction to even consider
    }

# ============================================================
# DECISION TEMPERATURE (Institutional Aggressiveness)
# ============================================================

def compute_decision_temperature(context: Dict, breath: Dict, reaction: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Decision Temperature = composite of market conditions.
    Used for scan speed and size adjustment, NOT threshold.
    Institutional principle: Temperature affects urgency, not quality.
    """
    # Components
    breadth_score = breath.get("bull", 0.5) * 100  # 0-100
    reaction_score = 0.0
    if reaction:
        reaction_score = 50 + (1 - reaction.get("absorption", 0.5)) * 50  # Low absorption = hot
    
    regime_clarity = 50.0
    regime = context.get("regime", "UNKNOWN")
    if regime == "TRENDING_UP":
        regime_clarity = 70
    elif regime == "TRENDING_DOWN":
        regime_clarity = 70
    elif regime == "RANGING":
        regime_clarity = 50
    else:
        regime_clarity = 30
    
    data_confidence = context.get("data_confidence", 50)
    
    # Weighted composite
    temperature = (
        breadth_score * 0.3 +
        reaction_score * 0.2 +
        regime_clarity * 0.3 +
        data_confidence * 0.2
    )
    
    # Determine state
    if temperature >= 60:
        state = "HOT"
        scan_speed = 1.0  # Normal speed
        size_boost = 1.2
    elif temperature >= 30:
        state = "NORMAL"
        scan_speed = 0.7  # Slower scan
        size_boost = 1.0
    else:
        state = "COLD"
        scan_speed = 0.3  # Much slower scan
        size_boost = 0.6
    
    return {
        "temperature": round(temperature, 1),
        "state": state,
        "scan_speed": scan_speed,
        "size_boost": size_boost,
        "components": {
            "breadth": round(breadth_score, 1),
            "reaction": round(reaction_score, 1),
            "regime": round(regime_clarity, 1),
            "data": round(data_confidence, 1),
        }
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
                    trigger_api_cooldown(25)
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
        meta = get_exchange_meta()
        if not meta:
            return cached or {}
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
        

# ============================================================
# V11 DISCOVERY PATCH — CAPITAL ROTATION DETECTOR
# ============================================================
# From discovery_v11_patch.py
# Includes: price history, OI pattern detection, dislocation scoring

# FIX BUG KRITIS: PRICE HISTORY YANG PROPER
# ============================================================
# Problem: _price_values cuma 10 entry (maxlen=10), diisi tiap snapshot refresh.
# Kalau snapshot interval 60s, itu cuma 10 menit history.
# Dislocation butuh 1h history. _price_values = USELESS untuk ini.
#
# Solution: tambah _price_history_1h yang khusus simpen 1h lookback.
# Ini dict: coin -> deque of (timestamp, price), maxlen=120 (120 x 30s = 1 jam)

_price_history_1h: Dict[str, deque] = {}
_price_history_lock = threading.RLock()
_PRICE_HISTORY_MAX = 120  # 120 datapoints, tiap snapshot interval ~30s = 1 jam

def update_price_history_1h(mids: Dict[str, float]):
    """
    Dipanggil dari refresh_snapshot() setiap kali snapshot berhasil.
    Simpen price history per coin untuk keperluan dislocation calculation.
    TANPA candles, TANPA API call tambahan.
    """
    now = time.time()
    with _price_history_lock:
        for coin, price in mids.items():
            if price <= 0:
                continue
            if coin not in _price_history_1h:
                _price_history_1h[coin] = deque(maxlen=_PRICE_HISTORY_MAX)
            _price_history_1h[coin].append((now, price))


def get_price_1h_ago(coin: str) -> Tuple[Optional[float], float]:
    """
    Return (price_1h_ago, coverage)
    coverage = 0-1, seberapa reliable datanya
    """
    with _price_history_lock:
        if coin not in _price_history_1h:
            return None, 0.0
        
        history = list(_price_history_1h[coin])
        
        if len(history) < 2:
            return None, 0.0
        
        now = time.time()
        oldest_age = now - history[0][0]
        coverage = min(1.0, oldest_age / 3600)  # 0-1
        
        cutoff = now - 3600
        
        # Cari data sebelum cutoff (full confidence)
        best_ts = None
        best_price = None
        for ts, price in history:
            if ts <= cutoff:
                if best_ts is None or ts > best_ts:
                    best_ts = ts
                    best_price = price
        
        # Fallback ke data tertua DENGAN coverage penalty
        if best_price is None and len(history) >= 2:
            return history[0][1], coverage
        
        return best_price, 1.0 if best_price else coverage


# ============================================================
# GATE 2: DISLOCATION SCORE (Tanpa Candles)
# ============================================================

def get_dislocation_score_v11(coin: str, snapshot) -> Dict[str, float]:
    """
    Return {
        "value": dislocation_value,
        "coverage": data_reliability_0-1,
        "confidence": confidence_0-1
    }
    """
    oi_growth = get_oi_roc(coin)
    price_now = snapshot.mids.get(coin, 0) if snapshot else 0
    price_1h_ago, coverage = get_price_1h_ago(coin)
    
    # Logging
    with _price_history_lock:
        history_len = len(_price_history_1h.get(coin, deque()))
    
    logger.info(
        f"DIS_DEBUG {coin} "
        f"oi_growth={oi_growth:+.2f}% "
        f"price_now={price_now:.4f} "
        f"price_1h_ago={price_1h_ago if price_1h_ago else 'None'} "
        f"history_len={history_len} coverage={coverage:.2f}"
    )
    
    if price_now <= 0 or price_1h_ago is None or price_1h_ago <= 0:
        return {
            "value": 0.0,
            "coverage": min(0.3, coverage),
            "confidence": min(0.3, coverage)
        }
    
    price_growth = (price_now - price_1h_ago) / price_1h_ago * 100
    dislocation = oi_growth - price_growth
    
    logger.info(
        f"DIS_RESULT {coin} "
        f"price_growth={price_growth:+.2f}% "
        f"dislocation={dislocation:+.2f} "
        f"coverage={coverage:.2f}"
    )
    
    return {
        "value": dislocation,
        "coverage": coverage,
        "confidence": coverage
    }

# ============================================================
# GATE 1: OI PATTERN MULTI-TIMEFRAME
# ============================================================

def get_oi_at_timeframe(history: List[Tuple[float, float]], seconds_ago: int) -> Tuple[float, float, float]:
    """
    Cari OI di timeframe tertentu dari history.
    
    Return: (oi_value, age_from_cutoff, coverage_weight)
    age_from_cutoff: berapa detik data ini dari target cutoff (0 = pas)
    coverage_weight: 1.0 kalau tepat, turun kalau telat
    """
    if not history:
        return 0.0, float('inf'), 0.0
    
    now = time.time()
    cutoff = now - seconds_ago
    
    # Cari entry yang paling dekat dengan cutoff (dari kiri, bukan kanan)
    best_entry = None
    best_dist = float('inf')
    
    for ts, val in history:
        dist = abs(ts - cutoff)
        if dist < best_dist:
            best_dist = dist
            best_entry = (ts, val)
    
    if best_entry is None:
        return 0.0, float('inf'), 0.0
    
    ts, val = best_entry
    age_from_cutoff = abs(ts - cutoff)
    
    # Coverage weight: kalau data terlalu jauh dari cutoff, kurang reliable
    max_allowed_age = seconds_ago * 0.5  # boleh telat 50% dari periode
    if max_allowed_age <= 0:
        max_allowed_age = 3600
    coverage_weight = max(0.0, 1.0 - (age_from_cutoff / max_allowed_age))
    
    return val, age_from_cutoff, coverage_weight


def get_oi_at_timeframe_safe(history: List[Tuple[float, float]], seconds_ago: int) -> Tuple[float, float, float]:
    """
    SAFE VERSION: HANYA PAKAI DATA MASA LALU (ts <= cutoff).
    
    Bug di get_oi_at_timeframe(): cari entry dengan abs(ts - cutoff) terkecil,
    artinya bisa pilih entry yang ts-nya SETELAH cutoff kalau jaraknya kebetulan
    lebih dekat — itu lookahead bias (pakai data yang seharusnya belum "terjadi"
    relatif ke titik waktu yang sedang dievaluasi). Versi ini filter ts <= cutoff
    dulu sebelum cari yang terdekat, supaya hasil murni representasi "OI pada/sebelum
    waktu X", bukan tercemar data setelahnya.
    """
    if not history:
        return 0.0, float('inf'), 0.0
    
    now = time.time()
    cutoff = now - seconds_ago
    
    # HANYA data yang <= cutoff (data masa lalu)
    candidates = [(ts, val) for ts, val in history if ts <= cutoff]
    
    if not candidates:
        return 0.0, float('inf'), 0.0
    
    # Ambil yang PALING DEKAT dengan cutoff (dari kiri)
    best_ts, best_val = candidates[-1]
    age_from_cutoff = cutoff - best_ts  # selalu positif
    
    max_allowed_age = seconds_ago * 0.5
    if max_allowed_age <= 0:
        max_allowed_age = 3600
    
    coverage = max(0.0, 1.0 - (age_from_cutoff / max_allowed_age))
    
    if coverage < 0.05:
        return 0.0, age_from_cutoff, coverage
    
    return best_val, age_from_cutoff, coverage


def get_oi_pattern_v11(coin: str) -> Tuple[str, float, float]:
    """
    OI Pattern detector multi-timeframe: 30m, 1h, 4h, 24h — SAFE VERSION.
    
    Perubahan dari versi lama:
    1. Pakai get_oi_at_timeframe_safe (tidak ada lookahead bias).
    2. WARMUP = data belum cukup tapi tetap masuk pool dengan confidence
       rendah, bukan auto-reject sebagai "UNKNOWN". Versi lama selalu
       reject kalau total_coverage < 0.4 (atau 0.2 untuk data <10 entries),
       artinya di awal hidup bot, hampir semua coin dibuang dari pool karena
       belum cukup histori — bukan karena memang gak menarik.
    3. Akses _oi_history/_oi_lock langsung dari scope module (bukan lewat
       sys.modules introspection yang rapuh dan gagal kalau script di-import,
       bukan dijalankan sebagai __main__).
    
    Pattern:
    EARLY     : 30m↑ 1h↑ 4h↑ 24h flat = best! baru mulai akumulasi
    MOMENTUM  : 30m↑ 1h↑ 4h↑ 24h↑ = udah jalan, masih bisa ikut
    SPIKE     : 30m↑↑ tapi 1h/4h flat = waspada, bisa short squeeze / rumor
    LATE      : 30m flat/turun, 4h↑ 24h↑ = udah peak, skip
    NEUTRAL   : campuran/flat dengan coverage cukup = skip
    WARMUP    : data belum cukup, confidence rendah tapi tetap masuk pool
    
    Return: (pattern, oi_4h_growth_pct, coverage_ratio)
    """
    try:
        with _oi_lock:
            if coin not in _oi_history or len(_oi_history[coin]) < 2:
                return "WARMUP", 0.0, 0.0
            history = list(_oi_history[coin])

        if len(history) < 2:
            return "WARMUP", 0.0, 0.0

        oi_now = history[-1][1]
        if oi_now <= 0:
            return "WARMUP", 0.0, 0.0

        # SAFE: tanpa lookahead bias, coverage-based bukan hardcode threshold
        oi_30m, _, cov_30m = get_oi_at_timeframe_safe(history, 1800)
        oi_1h, _, cov_1h = get_oi_at_timeframe_safe(history, 3600)
        oi_4h, _, cov_4h = get_oi_at_timeframe_safe(history, 14400)
        oi_24h, _, cov_24h = get_oi_at_timeframe_safe(history, 86400)

        # Dynamic-skip: kalau histori coin ini belum cukup panjang untuk
        # capai suatu timeframe (mis. bot baru restart, _oi_history baru
        # punya 10 menit data), timeframe itu di-exclude dari rata-rata
        # coverage — bukan ikut dihitung sebagai 0 yang menjatuhkan
        # total_coverage jadi selalu <0.05 sampai histori benar2 24 jam penuh.
        oldest_ts = history[0][0]
        history_span = time.time() - oldest_ts
        active_coverages = []
        for secs, cov in [(1800, cov_30m), (3600, cov_1h), (14400, cov_4h), (86400, cov_24h)]:
            if history_span >= secs:  # histori harus >= periode itu sendiri, baru cutoff-nya bisa punya data
                active_coverages.append(cov)
        total_coverage = sum(active_coverages) / len(active_coverages) if active_coverages else 0.0

        # Semua coverage nyaris nol -> WARMUP (data baru, bukan "tidak menarik")
        if total_coverage < 0.05:
            return "WARMUP", 0.0, total_coverage

        TH = 2.0

        g_30m = (oi_now - oi_30m) / max(oi_30m, 0.001) * 100 if oi_30m > 0 and cov_30m > 0.1 else 0
        g_1h = (oi_now - oi_1h) / max(oi_1h, 0.001) * 100 if oi_1h > 0 and cov_1h > 0.1 else 0
        g_4h = (oi_now - oi_4h) / max(oi_4h, 0.001) * 100 if oi_4h > 0 and cov_4h > 0.1 else 0
        g_24h = (oi_now - oi_24h) / max(oi_24h, 0.001) * 100 if oi_24h > 0 and cov_24h > 0.1 else 0

        is_up_30m = g_30m > TH
        is_up_1h = g_1h > TH
        is_up_4h = g_4h > TH
        is_up_24h = g_24h > TH

        if is_up_30m and is_up_1h and is_up_4h and not is_up_24h:
            return "EARLY", g_4h, total_coverage
        elif is_up_30m and is_up_1h and is_up_4h and is_up_24h:
            return "MOMENTUM", g_4h, total_coverage
        elif is_up_30m and not is_up_1h and not is_up_4h:
            return "SPIKE", g_30m, total_coverage
        elif not is_up_30m and not is_up_1h and is_up_4h and is_up_24h:
            return "LATE", g_4h, total_coverage
        else:
            # Data sebagian -> kalau coverage masih tipis, tetap WARMUP bukan NEUTRAL
            if total_coverage > 0.3:
                return "NEUTRAL", g_4h, total_coverage
            return "WARMUP", 0.0, total_coverage

    except Exception as e:
        logger.debug(f"get_oi_pattern_v11 error {coin}: {e}")
        return "WARMUP", 0.0, 0.0

# ============================================================
# MAIN: build_candidate_pool_v11_final
# ============================================================


# Sector mapping for narrative boost
# ============================================================
# SECTOR MAP — base reference (dead coins TETAP di sini tapi
# get_coin_sector() validasi ke live snapshot sebelum return)
# ============================================================
_SECTOR_MAP_BASE = {
    # BTC ecosystem
    "BTC": "BTC_ECO", "ORDI": "BTC_ECO",
    # Layer 1
    "ETH": "LAYER1", "SOL": "LAYER1", "ARB": "LAYER1", "OP": "LAYER1",
    "AVAX": "LAYER1", "SUI": "LAYER1", "APT": "LAYER1", "SEI": "LAYER1",
    "BLAST": "LAYER1", "NEAR": "LAYER1", "TON": "LAYER1",
    # DEAD/RENAMED — tetap ada sebagai fallback, disanitize saat runtime
    "FTM": "LAYER1", "MATIC": "LAYER1",  # FTM→S, MATIC→POL
    # SOL ecosystem
    "JUP": "SOL_ECO", "PYTH": "SOL_ECO", "RAY": "SOL_ECO",
    "BONK": "SOL_ECO", "WIF": "SOL_ECO", "POPCAT": "SOL_ECO",
    "TNSR": "SOL_ECO", "W": "SOL_ECO", "JTO": "SOL_ECO",
    # L2
    "STRK": "L2", "MANTA": "L2", "METIS": "L2", "ZKSYNC": "L2",
    # DeFi
    "UNI": "DEFI", "AAVE": "DEFI", "CRV": "DEFI", "LDO": "DEFI",
    "GMX": "DEFI", "DYDX": "DEFI", "SNX": "DEFI", "PENDLE": "DEFI",
    "MORPHO": "DEFI", "ENA": "DEFI",
    # AI/ML
    "TAO": "AI", "FET": "AI", "RNDR": "AI", "AKT": "AI",
    "WLD": "AI", "GRT": "AI", "ARKM": "AI", "OCEAN": "AI",
    "IO": "AI", "GRASS": "AI",
    # DEAD AI — merged/renamed
    "RENDER": "AI", "AGIX": "AI",  # RENDER=RNDR, AGIX→FET
    # Memecoin
    "DOGE": "MEME", "PEPE": "MEME", "WIF": "MEME", "BONK": "MEME",
    "FLOKI": "MEME", "FARTCOIN": "MEME", "MOG": "MEME", "BRETT": "MEME",
    # Gaming/Metaverse
    "AXS": "GAMING", "IMX": "GAMING", "GALA": "GAMING", "BEAM": "GAMING",
    # Infrastructure
    "DOT": "INFRA", "ATOM": "INFRA", "FIL": "INFRA", "AR": "INFRA",
    "LPT": "INFRA", "LINK": "INFRA", "RUNE": "INFRA",
    # Perp DEX
    "HYPE": "PERP_DEX",
    # RWA
    "MKR": "RWA", "ONDO": "RWA",
}

# Live-validated sector map — diupdate tiap sanitize_maps_from_snapshot()
_SECTOR_MAP: Dict[str, str] = dict(_SECTOR_MAP_BASE)
_SECTOR_MAP_LOCK = threading.RLock()

# Live-validated narrative map — diupdate tiap sanitize_maps_from_snapshot()
_NARRATIVE_MAP_LIVE: Dict[str, List[str]] = {}
_NARRATIVE_MAP_LOCK = threading.RLock()

# Timestamp terakhir sanitize
_MAPS_LAST_SANITIZED: float = 0.0


def sanitize_maps_from_snapshot(snapshot) -> None:
    """
    Validasi _SECTOR_MAP dan _NARRATIVE_MAP ke live snapshot.
    Plus: detect coin baru dari snapshot yang belum ada di SECTOR_MAP_BASE
    → assign ke sektor dinamis berdasarkan OI ranking.

    BUG FIX: snapshot.oi unit = juta USD (oi_val * price / 1e6)
    Jadi threshold harus 0.25 (= $250k), BUKAN 250_000.
    Sebelumnya: min_oi=250_000 → nunggu $250 TRILIUN → semua coin dibuang.
    """
    global _SECTOR_MAP, _NARRATIVE_MAP_LIVE, _MAPS_LAST_SANITIZED

    if not snapshot or not snapshot.mids:
        return

    # Throttle: max 1x per 5 menit
    now = time.time()
    if now - _MAPS_LAST_SANITIZED < 300:
        return
    _MAPS_LAST_SANITIZED = now

    live_coins = set(snapshot.mids.keys())
    # OI di snapshot sudah dalam juta USD → 0.25 = $250k minimum
    min_oi_m = 0.25

    # ===== SANITIZE SECTOR MAP (dari BASE) =====
    new_sector: Dict[str, str] = {}
    removed = []
    for coin, sector in _SECTOR_MAP_BASE.items():
        if coin in live_coins and snapshot.oi.get(coin, 0) >= min_oi_m:
            new_sector[coin] = sector
        else:
            removed.append(coin)

    # ===== DYNAMIC: tambah coin baru yang belum ada di BASE =====
    # Klasifikasi otomatis berdasarkan OI size tier
    # Coin baru kayak SAGA, 2Z, EIGEN, TRUMP, MELANIA dll langsung masuk
    new_dynamic = []
    for coin in live_coins:
        if coin in new_sector:
            continue  # udah ada di BASE
        oi_m = snapshot.oi.get(coin, 0)
        if oi_m < min_oi_m:
            continue
        # Auto-assign sector berdasarkan OI size (proxy: lebih gede = lebih established)
        # Bot dapet exposure ke coin baru tanpa perlu update hardcode
        if oi_m >= 50:       # >$50M OI → major
            new_sector[coin] = "MAJOR_ALT"
        elif oi_m >= 5:      # $5-50M OI → mid cap
            new_sector[coin] = "MID_ALT"
        elif oi_m >= 0.25:   # $250k-5M OI → small/emerging
            new_sector[coin] = "EMERGING"
        new_dynamic.append(coin)

    with _SECTOR_MAP_LOCK:
        _SECTOR_MAP = new_sector

    # ===== BUILD NARRATIVE MAP FROM LIVE SECTOR MAP =====
    sector_to_coins: Dict[str, List[str]] = {}
    for coin, sector in new_sector.items():
        sector_to_coins.setdefault(sector, []).append(coin)

    # Sort tiap sektor by OI descending — coin terkuat duluan
    for sector in sector_to_coins:
        sector_to_coins[sector].sort(
            key=lambda c: snapshot.oi.get(c, 0), reverse=True
        )

    # Filter sektor yang punya minimal 2 coin live
    new_narrative: Dict[str, List[str]] = {
        s: coins for s, coins in sector_to_coins.items() if len(coins) >= 2
    }

    with _NARRATIVE_MAP_LOCK:
        _NARRATIVE_MAP_LIVE = new_narrative

    # ===== LOG =====
    active_sectors = list(new_narrative.keys())
    total_mapped = sum(len(v) for v in new_narrative.values())
    logger.info(
        f"🗺️ MAPS SANITIZED | live={len(new_sector)} base={len(new_sector)-len(new_dynamic)} "
        f"dynamic_new={len(new_dynamic)} removed_base={len(removed)} "
        f"sectors={len(active_sectors)} coins_mapped={total_mapped}"
    )
    if new_dynamic:
        logger.info(f"   🆕 Dynamic coins: {sorted(new_dynamic)[:20]}")
    if removed:
        logger.debug(f"   🗑️ Removed dead: {removed[:15]}")


def get_coin_sector(coin: str) -> Optional[str]:
    """Lookup sector dari live-validated map. Fallback ke base map kalau belum disanitize."""
    with _SECTOR_MAP_LOCK:
        result = _SECTOR_MAP.get(coin.upper())
    if result is None:
        # Fallback ke base (e.g. sebelum sanitize pertama jalan)
        result = _SECTOR_MAP_BASE.get(coin.upper())
    return result


def get_live_narrative_map() -> Dict[str, List[str]]:
    """Return live-validated narrative map. Fallback ke hardcode kalau belum siap."""
    with _NARRATIVE_MAP_LOCK:
        if _NARRATIVE_MAP_LIVE:
            return dict(_NARRATIVE_MAP_LIVE)
    # Fallback minimal sebelum sanitize pertama
    return {
        "SOL_ECO": ["JUP", "PYTH", "RAY", "BONK", "WIF"],
        "AI":      ["TAO", "FET", "RNDR", "IO"],
        "DEFI":    ["AAVE", "UNI", "PENDLE", "MORPHO"],
        "MEME":    ["DOGE", "PEPE", "WIF", "BONK"],
        "LAYER1":  ["ETH", "SOL", "SUI", "AVAX", "ARB"],
    }

def get_narrative_boost_v11(coin: str, preliminary_pool: List[str]) -> float:
    """
    Sector narrative boost, calculated from preliminary pool only.
    Return: boost multiplier (0.0 - 0.20)
    """
    sector = get_coin_sector(coin)
    if not sector:
        return 0.0
    
    same_sector_count = sum(
        1 for c in preliminary_pool
        if c != coin and get_coin_sector(c) == sector
    )
    
    if same_sector_count >= 3:
        return 0.20
    elif same_sector_count >= 2:
        return 0.10
    elif same_sector_count >= 1:
        return 0.03
    return 0.0


def get_narrative_boost_v11_direct(coin: str, all_coins: List[str]) -> float:
    """
    Narrative boost dari SEMUA coin yang lolos gate (build_candidate_pool_v11_final),
    bukan dari preliminary pool terpisah seperti get_narrative_boost_v11 di atas —
    biar satu sumber kebenaran, gak ada 2 pool berbeda yang bisa kasih hasil
    inkonsisten antara scoring dan logging.
    """
    sector = get_coin_sector(coin)
    if not sector:
        return 0.0

    same_sector_count = sum(
        1 for c in all_coins
        if c != coin and get_coin_sector(c) == sector
    )

    if same_sector_count >= 3:
        return 0.20
    elif same_sector_count >= 2:
        return 0.10
    elif same_sector_count >= 1:
        return 0.03
    return 0.0

def get_oi_percentile_threshold(snapshot, min_percentile: int = 35) -> float:
    """Calculate OI threshold at given percentile from current snapshot."""
    if not snapshot or not snapshot.oi:
        return 500_000
    
    oi_values = [v for v in snapshot.oi.values() if v > 0]
    if len(oi_values) < 2:
        return 500_000
    
    oi_values.sort()
    idx = max(0, int(len(oi_values) * min_percentile / 100))
    return oi_values[idx]

def get_exchange_meta(force: bool = False):
    """Wrapper terpusat untuk info.meta_and_asset_ctxs() dengan cache TTL 20s
    + can_call_api() guard.

    KENAPA INI PENTING: sebelumnya ada 5 caller berbeda yang masing-masing
    panggil info.meta_and_asset_ctxs() langsung tanpa cache/guard (kecuali
    refresh_snapshot). Salah satu caller, trigger_engine_update_v7(), jalan
    di thread terpisah TIAP 3 DETIK (TRIGGER_ENGINE_INTERVAL_ACTIVE=3) dan
    fetch metadata fresh tiap kali tanpa cek cooldown — itu ~20 request/menit
    ke endpoint metadata DARI SATU FUNGSI INI SAJA, di luar panggilan dari
    4 fungsi lain. Volume 24h (dayNtlVlm) tidak berubah meaningful dalam
    hitungan detik, jadi cache 20s aman dan langsung memangkas beban ini
    drastis tanpa mengubah hasil secara signifikan.

    Return value identik dengan info.meta_and_asset_ctxs() (tuple mentah),
    jadi caller existing bisa redirect kesini tanpa ubah logic parsing.
    """
    cached = CACHE.get("exchange_meta", max_age=20)
    if cached is not None and not force:
        return cached

    if not can_call_api():
        stale = CACHE.get("exchange_meta")
        if stale is not None:
            logger.debug("⏳ API on cooldown, using stale exchange meta")
            return stale
        return None

    meta = info.meta_and_asset_ctxs()
    CACHE.set("exchange_meta", meta)
    return meta


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
        CACHE.set("exchange_meta", meta)  # numpang isi cache bersama get_exchange_meta()
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

        # V11 PATCH: Update price history for dislocation scoring
        update_price_history_1h(mids)
        
        with _last_mids_lock:
            for coin, price in mids.items():
                _last_mids[coin] = (price, now)
        
        for coin, val in oi.items():
            update_data_integrity_history(coin, val, 0, 0)
            with _oi_lock:
                if coin not in _oi_history:
                    _oi_history[coin] = deque(maxlen=1500)  # cukup utk cakupan 24h (was: 20, cuma ~19 menit — bikin coverage 4h/24h SELALU 0)
                _oi_history[coin].append((now, val))
        
        # === [DEBUG] OI HISTORY STATUS ===
        with _oi_lock:
            coins_tracked = len(_oi_history)
            btc_len = len(_oi_history.get("BTC", deque())) if "BTC" in _oi_history else 0
            eth_len = len(_oi_history.get("ETH", deque())) if "ETH" in _oi_history else 0
        logger.info(f"OI_HISTORY coins={coins_tracked} BTC={btc_len} ETH={eth_len}")
        
        for coin, val in funding.items():
            with _funding_lock:
                _funding_cache[coin] = (val, now)
            update_data_integrity_history(coin, 0, val, 0)
        
        return snapshot
        
    except Exception as e:
        if "429" in str(e):
            trigger_api_cooldown(25)
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
            trigger_api_cooldown(25)
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

def get_oi_roc(coin: str, window_minutes: int = 5) -> float:
    """Rate of change of OI — oldest vs newest in window.

    FIX: formula lama pakai avg vs current → oi_current TERMASUK dalam
    window → avg ≈ current → roc collapse ke 0% selalu.
    Fix: oldest sample di window vs newest sample (point-to-point ROC).

    window_minutes=5   → realtime (observe_market, scoring)
    window_minutes=60  → discovery (build_candidate_pool)
    """
    with _oi_lock:
        if coin not in _oi_history or len(_oi_history[coin]) < 2:
            return 0.0
        hist = list(_oi_history[coin])

    now = time.time()
    cutoff = now - (window_minutes * 60)

    samples = [(ts, v) for ts, v in hist if ts >= cutoff]

    if len(samples) < 2:
        # Fallback: pakai seluruh history yang ada
        if len(hist) < 2:
            return 0.0
        oi_old = hist[0][1]
        oi_now = hist[-1][1]
        actual_window = (hist[-1][0] - hist[0][0]) / 60
    else:
        oi_old = samples[0][1]   # tertua di window
        oi_now = samples[-1][1]  # terbaru di window
        actual_window = (samples[-1][0] - samples[0][0]) / 60

    if oi_old <= 0:
        return 0.0

    roc = (oi_now - oi_old) / oi_old * 100

    # Sample log 1% chance
    if random.random() < 0.01:
        logger.debug(
            f"ROC_{coin} w={actual_window:.0f}m "
            f"old={oi_old:.0f} cur={oi_now:.0f} roc={roc:+.2f}%"
        )

    return roc


def get_volume_spike(coin: str, master: Dict = None, use_cache: bool = True) -> float:
    cache_key = f"vol_spike_{coin}"
    if use_cache:
        cached = CACHE.get(cache_key, max_age=20)
        if cached is not None:
            return cached

    candles = get_candles(coin, "5m", 30, master)
    if not candles or len(candles) < 6:
        return 1.0
    
    price = float(candles[-1]['c'])
    cur = float(candles[-1]['v']) * price
    
    # FIX VOL-1: Expand window from 5 to 12 candles for more stable baseline
    prev = [float(c['v']) * float(c['c']) for c in candles[-13:-1]]
    avg = sum(prev)/len(prev) if prev else 1.0
    ratio = cur / avg if avg > 0 else 1.0
    
    # ===== INSTRUMENTATION LOG =====
    trace(f"[VOL RAW {coin}] cur={cur:.0f} avg={avg:.0f} ratio={ratio:.2f}")
    trace(f"[VOL UNIT {coin}] raw_v={candles[-1]['v']} close={candles[-1]['c']}")
    # =================================
    
    if use_cache:
        CACHE.set(cache_key, ratio)
    return ratio

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
    """
    [PATCH P0] Added STRUCT debug logging
    """
    candles_1h = get_candles(coin, "1h", 60, master)
    
    # === INSTRUMENTATION: STRUCT_DEBUG ===
    if not candles_1h:
        logger.warning(f"STRUCT {coin}: no candles")
        return False, False
    
    logger.info(f"STRUCT {coin}: candles={len(candles_1h)}")
    
    if len(candles_1h) < 30:
        logger.warning(f"STRUCT {coin}: insufficient candles ({len(candles_1h)} < 30)")
        return False, False
    
    highs, lows = detect_swing_points(candles_1h, lookback=3)
    
    if len(highs) < 2 or len(lows) < 2:
        logger.info(f"STRUCT {coin}: highs={len(highs)} lows={len(lows)} → insufficient swing points")
        return False, False
    
    bos_up, bos_down, choch = get_bos_and_choch(candles_1h, highs, lows)
    
    # === INSTRUMENTATION: STRUCT_RESULT ===
    result_long = bos_up or choch
    result_short = bos_down or choch
    
    logger.info(
        f"STRUCT_RESULT {coin} "
        f"bos_up={bos_up} "
        f"bos_down={bos_down} "
        f"choch={choch} "
        f"→ long={result_long} short={result_short}"
    )
    
    return result_long, result_short

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
    """Compute belief stability (0-1, lower = more stable) using Coefficient of Variation"""
    try:
        with _belief_history_lock:
            scores = []
            for hist in _belief_history.values():
                for entry in hist:
                    scores.append(entry.get("score", 0))
            if len(scores) < 10:
                return 0.5
            recent = scores[-30:]
            mean = np.mean(recent)
            std = np.std(recent)
            if mean < 0.01:
                return 0.5
            cv = std / mean
            stability = min(1.0, cv / 2)
            return float(stability)
    except:
        return 0.5

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

def compute_decision_energy_v7(confidence: float, opportunity: float, uncertainty: float,
                                recent_wr: float = 0.5) -> float:
    # ZERO WR = uncertainty dihukum berat + confidence di-cap, mencegah energy tetap tinggi meski performa buruk
    if recent_wr == 0.0:
        uncertainty *= 1.5
        confidence = min(confidence, 60.0)

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
    """
    Entropy sebagai peta medan:
    0–25  : Structured (trend jelas) → RR boleh lebih rendah
    25–50 : Normal (standard market) → RR netral
    50–75 : Noisy (chaotic) → RR harus lebih tinggi
    75+   : Extreme Chaos → RR sangat ketat
    """
    if entropy_market <= 25:
        mult = 0.95      # Sedikit longgar, market terstruktur
    elif entropy_market <= 50:
        mult = 1.00      # Normal, pakai base_rr
    elif entropy_market <= 75:
        mult = 1.10      # Cukup berisik, naikkan 10%
    else:
        mult = 1.25      # Chaos, naikkan 25%

    min_rr = base_rr * mult

    # Floor absolut biar ga terlalu rendah
    return max(min_rr, TUNABLE.get("RR_FLOOR_ABSOLUTE", 1.50))

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
# ============================================================
# PART 34.5 – HIDDEN LIQUIDITY (UPDATED)
# ============================================================

def compute_hidden_liquidity(coin: str, candles_5m: list, delta_history: list,
                              oi_history: list) -> dict:
    """
    Soft-scoring hidden liquidity detector dengan confidence weighting.
    Returns score 0-100, side (NONE/POSSIBLE/ABSORBING), breakdown, confidence.
    """
    result = {
        "score": 0,
        "side": "NONE",
        "status": "⏸️ NONE",
        "persistence": 0,
        "eff_score": 0.0,
        "vol_score": 0.0,
        "persist_score": 0.0,
        "oi_score": 0.0,
        "confidence": 0.0,
    }

    # ===== DATA SUFFICIENCY =====
    delta_count = len(delta_history)
    oi_count = len(oi_history)
    candle_count = len(candles_5m) if candles_5m else 0

    # Coverage: GEOMETRIC MEAN (lebih adil)
    if candle_count > 0 and delta_count > 0 and oi_count > 0:
        coverage = (
            min(1.0, delta_count / 5) *
            min(1.0, oi_count / 5) *
            min(1.0, candle_count / 20)
        ) ** (1/3)
    else:
        coverage = 0.0
    result["confidence"] = round(coverage, 2)

    if candle_count < 6 or delta_count < 3 or oi_count < 3:
        result["status"] = "⏸️ INSUFFICIENT"
        return result

    # ===== PRICE MOVE =====
    prices = [float(c['c']) for c in candles_5m[-5:]]
    price_move_pct = abs(prices[-1] - prices[-5]) / max(prices[-5], 0.01) * 100

    # ===== NORMALIZED DELTA =====
    vols = [float(c['v']) * float(c['c']) for c in candles_5m[-5:]]
    delta_abs_sum = sum(abs(d) for d in delta_history[-5:])
    vol_median = np.median(vols) if vols else 1.0
    if vol_median == 0:
        return result
    delta_norm = delta_abs_sum / (vol_median * len(vols))
    delta_norm = np.clip(delta_norm, 0.002, 0.1)

    # ===== EFFICIENCY SCORE (soft) =====
    efficiency = price_move_pct / max(delta_norm, 0.001)
    eff_score = max(0.0, min(1.0, (0.5 - efficiency) / 0.5))
    result["eff_score"] = eff_score

    # ===== VOLUME SCORE (soft) =====
    avg_vol = sum(vols[:-1]) / max(1, len(vols)-1)
    if avg_vol == 0:
        return result
    vol_ratio = vols[-1] / avg_vol
    vol_score = max(0.0, min(1.0, (vol_ratio - 1.1) / 1.2))
    result["vol_score"] = vol_score

    # ===== OI SCORE =====
    oi_start = oi_history[-5] if len(oi_history) >= 5 else oi_history[-1]
    oi_end = oi_history[-1]
    oi_trend = (oi_end - oi_start) / max(oi_start, 0.01) * 100 if oi_start > 0 else 0
    oi_score = max(0.0, min(1.0, 1.0 - abs(oi_trend) / 10))
    result["oi_score"] = oi_score

    # ===== PERSISTENCE (decayed, not linear) =====
    atr_pct = get_atr_pct(coin, 14, "5m", None) if candle_count >= 20 else 0.5
    move_limit = max(0.08, atr_pct * 0.5)

    persistence = 0
    for i in range(1, min(6, candle_count)):
        sub_prices = [float(c['c']) for c in candles_5m[-i-1:]]
        sub_move = abs(sub_prices[-1] - sub_prices[0]) / max(sub_prices[0], 0.01) * 100
        sub_vols = [float(c['v']) * float(c['c']) for c in candles_5m[-i-1:]]
        sub_avg = sum(sub_vols[:-1]) / max(1, len(sub_vols)-1)
        if sub_avg == 0:
            continue
        sub_vol_ratio = sub_vols[-1] / sub_avg
        if sub_move < move_limit and sub_vol_ratio > 1.2:
            persistence += 1
        else:
            break

    if persistence >= 3:
        persist_score = 1.0
    elif persistence >= 2:
        persist_score = 0.8
    elif persistence >= 1:
        persist_score = 0.5
    else:
        persist_score = 0.0
    result["persist_score"] = persist_score
    result["persistence"] = persistence

    # ===== RAW SCORE =====
    raw_score = (
        35 * eff_score +
        25 * vol_score +
        20 * persist_score +
        20 * oi_score
    )

    # ===== OI EFFECTIVE =====
    oi_floor = 0.3
    oi_effective = oi_score * (oi_floor + (1 - oi_floor) * eff_score)
    raw_score = (
        35 * eff_score +
        25 * vol_score +
        20 * persist_score +
        20 * oi_effective
    )

    # ===== SMOOTH COVERAGE (BUKAN DISKRIT) =====
    coverage = (
        (eff_score > 0.05) +
        (vol_score > 0.05) +
        (persist_score > 0.05)
    )
    coverage_ratio = coverage / 3.0
    coverage_penalty = 0.2 + 0.8 * coverage_ratio

    # ===== APPLY =====
    weighted_score = raw_score * coverage_penalty * coverage_ratio
    score = min(100, int(weighted_score))
    result["score"] = score

    # ===== STATUS (Revised - PATCH 1) =====
    if score >= 70:
        result["side"] = "STRONG"
        result["status"] = "🧊 STRONG"
    elif score >= 40:
        result["side"] = "POSSIBLE"
        result["status"] = "👀 POSSIBLE"
    elif score >= 20:
        result["side"] = "WEAK"
        result["status"] = "⚪ WEAK"
    else:
        result["side"] = "NONE"
        result["status"] = "⏸️ NONE"

    return result


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
            del _decision_journal[:-2000] 
            
def get_decision_journal(coin: str = None, mode: str = None, limit: int = 100) -> List[DecisionJournalEntry]:
    with _journal_lock:
        result = list(_decision_journal)  
        if coin:
            result = [e for e in result if e.coin == coin]
        if mode:
            result = [e for e in result if e.mode == mode]
        return result[-limit:]

def get_rejection_reason_counts(window_minutes: int = 60) -> Dict[str, int]:
    """Count rejection reasons from decision journal (last N minutes).
    
    Robust terhadap perubahan struktur dataclass.
    Baca why_not/reason dari narrative field.
    """
    cutoff = time.time() - (window_minutes * 60)
    counts = {}

    with _journal_lock:
        for entry in _decision_journal:
            try:
                # Cek timestamp dengan getattr (aman jika field berubah)
                if getattr(entry, "timestamp", 0) < cutoff:
                    continue

                # Reject = executed=False (bukan entry.decision REJECT)
                if getattr(entry, "executed", True):
                    continue

                # Ambil narrative (new schema) atau extra (old schema)
                narrative = getattr(entry, "narrative", {}) or {}

                # Prioritaskan why_not (prefer), fallback ke reason, default unknown
                reason = (
                    narrative.get("why_not")
                    or narrative.get("reason")
                    or "unknown"
                )

                # Truncate jika terlalu panjang (keep readable)
                reason = str(reason)
                if len(reason) > 40:
                    reason = reason[:40] + "..."

                counts[reason] = counts.get(reason, 0) + 1

            except Exception:
                # Jangan mati hanya karena satu entry rusak
                logger.debug("health rejection parse skipped for one entry")
                continue

    return counts

def auto_review():
    global _review_counter
    with _review_lock:
        _review_counter += 1
        if _review_counter % _AUTO_REVIEW_INTERVAL != 0:
            return

    with _journal_lock:
        entries = list(_decision_journal)[-50:]

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

    text = f"📊 <b>DECISION REVIEW</b> (last {len(entries)})\n━━━━━━━━━━━━━━━━━━━━━━\n"

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
        bot.send_message(USER_ID, text, parse_mode='HTML')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
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
        recent = list(_decision_journal)[-100:]
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

def compute_surprise_index(coin: str, expected_move: float, actual_move: float) -> float:
    if expected_move <= 0:
        return 0.0
    ratio = abs(actual_move) / expected_move
    if ratio < 0.8:
        return 0.0
    return min(100, (ratio - 0.8) * 100)

def update_intent_timeline(coin: str, intent: str):
    with _intent_timeline_lock:
        if coin not in _intent_timeline:
            _intent_timeline[coin] = deque(maxlen=20)
        _intent_timeline[coin].append((intent, time.time()))
    
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


def compute_ob_quality(coin: str, event: TradeEvent, master: Dict) -> ZoneQuality:
    """
    OB Quality =
    25% freshness (how fresh is the OB)
    20% displacement (impulse candle strength)
    20% reaction (price rejection)
    15% mitigation (how much it was tested)
    20% HTF alignment (4H bias)
    """
    candles_1h = get_candles(coin, "1h", 100, master)
    if not candles_1h or len(candles_1h) < 10:
        return ZoneQuality(50, 50, 50, 50, 50, 50, {})
    
    idx = event.extra.get("idx", 0)
    
    # 1. FRESHNESS: how many candles since OB formed
    if idx > 0 and idx < len(candles_1h):
        age_candles = len(candles_1h) - idx
        freshness = max(0, 100 - age_candles * 2)  # 50 candles = 0%
    else:
        freshness = 50
    
    # 2. DISPLACEMENT: impulse candle strength
    if idx + 1 < len(candles_1h):
        imp_candle = candles_1h[idx + 1]
        prev_candle = candles_1h[idx]
        imp_range = float(imp_candle['h']) - float(imp_candle['l'])
        prev_range = float(prev_candle['h']) - float(prev_candle['l'])
        if prev_range > 0:
            displacement = min(100, (imp_range / prev_range) * 50)
        else:
            displacement = 50
    else:
        displacement = 50
    
    # 3. REACTION: price rejection at OB
    reaction = 50
    if idx + 3 < len(candles_1h):
        if event.direction == "LONG":
            # Check if price bounced from OB
            low_after = min(float(c['l']) for c in candles_1h[idx+1:idx+4])
            if low_after < event.price_low * 1.01:
                reaction = 80
            elif low_after < event.price_low * 1.03:
                reaction = 65
        else:
            high_after = max(float(c['h']) for c in candles_1h[idx+1:idx+4])
            if high_after > event.price_high * 0.99:
                reaction = 80
            elif high_after > event.price_high * 0.97:
                reaction = 65
    
    # 4. MITIGATION: how much OB was tested
    mitigation = 50
    touches = 0
    for c in candles_1h[idx+1:]:
        if event.direction == "LONG":
            if float(c['l']) <= event.price_low * 1.01:
                touches += 1
        else:
            if float(c['h']) >= event.price_high * 0.99:
                touches += 1
    if touches == 0:
        mitigation = 90  # untouched = high quality
    elif touches == 1:
        mitigation = 70
    elif touches == 2:
        mitigation = 50
    else:
        mitigation = 30
    
    # 5. HTF ALIGNMENT
    bias, strength, _ = get_bias_4h_advanced(coin)
    if (event.direction == "LONG" and bias == "BULLISH") or (event.direction == "SHORT" and bias == "BEARISH"):
        alignment = 50 + strength * 0.3
    else:
        alignment = 50 - strength * 0.2
    alignment = max(0, min(100, alignment))
    
    # COMPUTE SCORE
    score = int(
        0.25 * freshness +
        0.20 * displacement +
        0.20 * reaction +
        0.15 * mitigation +
        0.20 * alignment
    )
    
    components = {
        "freshness": freshness,
        "displacement": displacement,
        "reaction": reaction,
        "mitigation": mitigation,
        "alignment": alignment,
    }
    
    return ZoneQuality(score, freshness, mitigation, displacement, 0, alignment, components)


def compute_fvg_quality(coin: str, event: TradeEvent, master: Dict) -> ZoneQuality:
    """
    FVG Quality =
    30% impulse (gap size)
    25% imbalance (unfilled gap)
    20% hold_time (how long gap held)
    25% reclaim (price reclaimed)
    """
    candles_1h = get_candles(coin, "1h", 100, master)
    if not candles_1h or len(candles_1h) < 10:
        return ZoneQuality(50, 50, 50, 50, 50, 50, {})
    
    idx = event.extra.get("idx", 0)
    gap_size = (event.price_high - event.price_low) / max(event.price_low, 0.01) * 100
    
    # 1. IMPULSE: gap size
    impulse = min(100, gap_size * 200)  # 0.5% gap = 100%
    
    # 2. IMBALANCE: how unfilled
    fill_ratio = getattr(event, 'fill_ratio', 0.5)
    imbalance = max(0, 100 - fill_ratio * 100)
    
    # 3. HOLD TIME: candles since FVG formed
    if idx > 0 and idx < len(candles_1h):
        age = len(candles_1h) - idx
        hold_time = max(0, 100 - age * 1.5)  # 67 candles = 0%
    else:
        hold_time = 50
    
    # 4. RECLAIM: did price come back to fill?
    reclaim = 50
    if idx + 5 < len(candles_1h):
        closes = [float(c['c']) for c in candles_1h[idx+1:idx+6]]
        if event.direction == "LONG":
            # Bullish FVG: reclaim = price > gap_high
            if max(closes) > event.price_high:
                reclaim = 80
            elif max(closes) > event.price_low:
                reclaim = 60
        else:
            if min(closes) < event.price_low:
                reclaim = 80
            elif min(closes) < event.price_high:
                reclaim = 60
    
    # COMPUTE SCORE
    score = int(
        0.30 * impulse +
        0.25 * imbalance +
        0.20 * hold_time +
        0.25 * reclaim
    )
    
    components = {
        "impulse": impulse,
        "imbalance": imbalance,
        "hold_time": hold_time,
        "reclaim": reclaim,
    }
    
    return ZoneQuality(score, impulse, imbalance, hold_time, 0, reclaim, components)


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
                      fatigue_penalty: float = 0.0) -> str:
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

    # Delta threshold 5 → 4 (slightly relaxed)
    if (event.direction == "LONG" and delta > 4) or (event.direction == "SHORT" and delta < -4):
        evidence_count += 1
        reasons.append("delta")

    oi_persist, oi_trend = get_oi_persistence(event.extra.get("coin", "BTC"))
    if oi_persist and ((event.direction == "LONG" and oi_trend == 1) or (event.direction == "SHORT" and oi_trend == -1)):
        evidence_count += 1
        reasons.append("oi_persistence")
    # OI threshold 5 → 4 (slightly relaxed)
    elif abs(oi_roc) > 4:
        evidence_count += 1
        reasons.append("oi_impulse")

    # Cluster bonus: only if strength >= 70 (no free lunch)
    if event.type == "CLUSTER" and event.strength >= 70:
        evidence_count += 1
        reasons.append("cluster_strong")

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
    # FIX VOL-3: Lower thresholds (was 2.0/1.5/1.2, sadis banget)
    vol_score = 90 if vol_spike >= 1.4 else (70 if vol_spike >= 1.2 else (50 if vol_spike >= 1.05 else 30))

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

# ===== U1: ASYMMETRIC RR BONUS/PENALTY =====
def get_rr_bonus(rr: float) -> float:
    """
    Bonus/penalti threshold berdasarkan risk-reward.
    - rr < 1.0 : penalty (threshold naik, lebih sulit entry)
    - 1.0–1.8 : normal (no adjustment)
    - 1.8–2.5 : bonus kecil (threshold turun, lebih mudah)
    - > 2.5   : cap bonus
    """
    if rr < 1.0:
        return max(-15, (rr - 1.0) * 30)   # -15 s/d 0
    elif rr < 1.8:
        return 0
    elif rr < 2.5:
        return (rr - 1.8) * 5              # 0 s/d 3.5
    else:
        return 5                           # cap bonus +5

# ===== U2: REGIME CONDITIONING ADJUSTMENTS =====
def get_regime_adjustment(regime: str) -> int:
    """Adjustment poin untuk threshold berdasarkan regime."""
    mapping = {
        "TRENDING_UP": 4,
        "TRENDING_DOWN": 4,
        "RANGING": -3,
        "HIGH_VOLATILITY": 0,
        "LOW_VOLATILITY": 0,
        "PANIC": 8,
    }
    return mapping.get(regime, 0)

def get_temperature_adjustment(temp_state: str) -> int:
    """Adjustment berdasarkan decision temperature state."""
    mapping = {
        "HOT": -2,
        "NORMAL": 0,
        "COLD": 3,
    }
    return mapping.get(temp_state, 0)

def get_recent_wr_adjustment(coin: str) -> int:
    """Adjustment berdasarkan win rate 20 terakhir untuk coin tersebut."""
    global _decision_journal, _journal_lock
    try:
        with _journal_lock:
            entries = [e for e in _decision_journal if e.coin == coin]
            recent = entries[-20:] if len(entries) >= 20 else entries
            if len(recent) < 5:
                return 0
            wins = sum(1 for e in recent if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
            wr = wins / len(recent)
            if wr == 0.0 and len(recent) >= 5:
                return 15  # ZERO WR per-coin = hukuman besar, jauh lebih berat dari sekadar WR rendah
            if wr < 0.45:
                return 3
            elif wr > 0.65:
                return -2
            return 0
    except Exception as e:
        logger.warning(f"Error computing recent WR for {coin}: {e}")
        return 0

def get_dynamic_min_rr(market_regime: str) -> float:
    base = 1.8
    if market_regime == "TRENDING_DOWN":
        base = base * 0.9   # 1.62
    elif market_regime == "TRENDING_UP":
        base = 1.6
    elif market_regime == "RANGING":
        base = 1.4
    elif market_regime == "PANIC":
        base = 1.0
    return min(base, 2.0)  # cap 2.0

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
    """Layer 1: Kumpulkan semua data dan event - RETURN WITH STATUS"""

    # ===== STALE MODE CHECK =====
    snapshot = get_snapshot()
    snapshot_age = time.time() - snapshot.timestamp if snapshot else 999

    stale_mode = False
    if snapshot_age > 60:
        logger.debug(f"⚠️ Stale mode: snapshot age {snapshot_age:.1f}s")
        stale_mode = True
    if snapshot_age > 180:
        logger.warning(f"🔴 Degraded mode: snapshot age {snapshot_age:.1f}s, skipping new entries")
        return {"status": "REJECT", "reason": f"snapshot_stale_{int(snapshot_age)}s", "coin": coin, "mark": mark}

    data_confidence, ages = get_data_confidence(coin, time.time())
    if stale_mode:
        data_confidence = int(data_confidence * 0.8)
    
    # Penalty untuk snapshot > 90 detik
    if snapshot_age > 90:
        data_confidence -= 10
        logger.debug(f"📉 Snapshot age {snapshot_age:.1f}s > 90s, -10 confidence → {data_confidence}")

    if data_confidence < ENGINE_CONSTANTS["MIN_DATA_CONFIDENCE"]:
        return {"status": "REJECT", "reason": f"low_data_confidence_{data_confidence}", "coin": coin, "mark": mark, "data_confidence": data_confidence}

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
        return {"status": "REJECT", "reason": "no_events", "coin": coin, "mark": mark, "data_confidence": data_confidence}

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
        return {"status": "REJECT", "reason": f"low_score_{best_event.score if best_event else 0}", "coin": coin, "mark": mark, "data_confidence": data_confidence, "best_event": best_event}

    # === DETAILED OBSERVATION LOGGING ===
    logger.info(
        f"📊 OBS {coin} | score={best_event.score} data_conf={data_confidence} | "
        f"delta={delta:.1f} oi_roc={oi_roc:.1f} | cluster={best_event.type} strength={best_event.strength}"
    )

    return {
        "status": "PASS",
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

    # Propagate REJECT from observe
    if obs.get("status") == "REJECT":
        return {"status": "REJECT", "reason": obs.get("reason", "observe_rejected"), "coin": obs.get("coin")}

    coin = obs["coin"]
    event = obs["best_event"]
    mark = obs["mark"]

    # VELOCITY OBSERVER: Log only, no decision impact
    if TUNABLE.get("VELOCITY_ENABLED", True):
        candles_5m = obs.get("master_candles", [])
        if candles_5m:
            log_velocity_observer(coin, event, candles_5m)

    bias_4h, bias_strength, bias_stability = get_bias_4h_advanced(coin)

    if obs["market_state"] == MarketState.REVERSAL:
        # === P1 FIX: Allow strong clusters in reversal ===
        members = event.extra.get("members", []) if event.extra else []
        has_liquidity = event.type == "LIQUIDITY" or "LIQUIDITY" in members
        
        if not has_liquidity:
            # Cluster strength >= 70 masih boleh lolos
            is_strong_cluster = event.type == "CLUSTER" and event.strength >= 70
            if not is_strong_cluster:
                return {"status": "REJECT", "reason": "reversal_no_liquidity", "coin": coin}
            else:
                # Cluster kuat di reversal: warn tapi lanjut
                logger.debug(f"{coin}: REVERSAL but strong cluster ({event.strength}), allowing")
    elif obs["market_state"] == MarketState.EXPANSION:
        if event.type == "LIQUIDITY" or "LIQUIDITY" in event.extra.get("members", []):
            return {"status": "REJECT", "reason": "expansion_liquidity_skip", "coin": coin}

    if event.direction == "LONG" and not obs["structure_valid_long"]:
        return {"status": "REJECT", "reason": "structure_invalid_long", "coin": coin}
    if event.direction == "SHORT" and not obs["structure_valid_short"]:
        return {"status": "REJECT", "reason": "structure_invalid_short", "coin": coin}

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

    # ===== PROPOSE INTENT (Thesis-level) =====
    try:
        delta = obs.get("delta", 0)
        vol_spike = obs.get("vol_spike", 1.0)
        context_obj = obs.get("context")
        liquidity_score = 1.0 if event.type == "LIQUIDITY" else (0.5 if event.type in ("OB", "FVG") else 0.0)
        displacement_score = 1.0 if event.extra.get("displaced", False) else 0.0
        delta_normalized = float(np.tanh(delta / 10.0)) if delta else 0.0
        regime_map = {"TRENDING_UP": 1.0, "RANGING": 0.0, "TRENDING_DOWN": -1.0}
        regime_val = regime_map.get(context_obj.regime if hasattr(context_obj, "regime") else "RANGING", 0.0)
        breadth_val = context_obj.breath_bull if hasattr(context_obj, "breath_bull") else 0.5
        propose_intent(
            coin=coin,
            vector=[liquidity_score, 0.2, displacement_score, delta_normalized, regime_val, breadth_val],
            event_type=event.type,
            direction=event.direction
        )
    except Exception as e:
        logger.debug(f"Propose intent error: {e}")

    return {
        "status": "PASS",
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
        "delta": obs.get("delta", 0),  # FIX 3: Add missing delta key for execute_decision
        "cvd_accel": obs.get("cvd_accel", False),
        "clustered": obs["clustered"], "ages": obs["ages"],
        "context": obs.get("context"),
        "bias_4h": bias_4h, "bias_strength": bias_strength, "bias_stability": bias_stability
    }
    
# ============================================================
# PART 33 – LAYER 3: COMPUTE CONFIDENCE + HELPER FUNCTIONS
# ============================================================

def compute_confidence(thesis_data: Dict) -> Optional[Dict]:
    # Propagate REJECT from thesis
    if thesis_data.get("status") == "REJECT":
        return {"status": "REJECT", "reason": thesis_data.get("reason", "thesis_rejected"), "coin": thesis_data.get("coin")}

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
    base_rr = get_dynamic_min_rr(thesis_data["market_regime"])
    min_rr = get_entropy_adjusted_min_rr(base_rr, entropy_market)
    entropy_mult = min_rr / base_rr if base_rr > 0 else 1.0
    trace(f"[RR {coin}] rr={rr:.2f} base_rr={base_rr:.2f} entropy={entropy_market} entropy_mult={entropy_mult:.2f} final_min_rr={min_rr:.2f} regime={thesis_data['market_regime']}")
    if rr < min_rr:
        logger.debug(
            f"❌ CONF FAIL [{coin}] low_rr | rr={rr:.2f} min_rr={min_rr:.2f} | "
            f"regime={thesis_data['market_regime']} entropy={entropy_market}"
        )
        return {"status": "REJECT", "reason": f"low_rr_{rr:.2f}_min_{min_rr:.2f}", "coin": coin, "rr": rr}

    opportunity = compute_opportunity(rr, thesis_data["vol_spike"], thesis_data["momentum"])
    uncertainty = compute_uncertainty(entropy_market, entropy_decision, contradiction, exhaustion)

    # ===== WR LOOKUP UNTUK DECISION ENERGY (zero-WR penalty) =====
    de_recent_wr = 0.5
    try:
        with _journal_lock:
            de_entries = [e for e in _decision_journal if e.coin == coin]
            de_recent = de_entries[-20:] if len(de_entries) >= 20 else de_entries
            de_closed = [e for e in de_recent if getattr(e, "executed", False) and getattr(e, "outcome", None) is not None]
            if len(de_closed) >= 5:
                de_wins = sum(1 for e in de_closed if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
                de_recent_wr = de_wins / len(de_closed)
    except Exception:
        de_recent_wr = 0.5

    decision_energy = compute_decision_energy_v7(confidence, opportunity, uncertainty, de_recent_wr)
    update_decision_energy_history(coin, decision_energy)
    decision_acceleration = compute_decision_acceleration(coin)

    setup_age_minutes = (time.time() - event.first_seen) / 60
    competitor_count = len(_active_candidates)
    time_pressure, urgency_score = compute_time_pressure(setup_age_minutes, competitor_count)

    prediction_quality_mult = get_prediction_quality_multiplier(coin)
    position_size_mult = get_position_size_multiplier_v7(entropy_market, prediction_quality_mult, thesis_data["intent_legacy"])
    position_size_mult *= thesis_data["fatigue_penalty"]

    # ===== CLARITY PRE-CHECK (early gate before execute_decision) =====
    breath_snapshot = compute_market_breath_v10()
    clarity_pre = compute_clarity(
        thesis_data.get("context"),
        breath_snapshot,
        score_long,
        score_short,
        entropy_market
    ) if thesis_data.get("context") else {"decision_quality": 1.0, "dominant_factor": "no_context", "severity": 0}

    # ===== ZERO-SCORE REJECT GUARD =====
    if final_score <= 0:
        logger.debug(f"❌ CONF FAIL [{coin}] final_score={final_score} (zero or negative)")
        return {
            "status": "REJECT",
            "reason": f"zero_score_{final_score}",
            "coin": coin
        }

    # ===== DEBUG LOG: semua setup yang lolos ke sini =====
    logger.debug(
        f"✅ CONF PASS [{coin}] "
        f"score={final_score} conf={confidence:.1f} rr={rr:.2f} "
        f"ev_fam={evidence_families} exhaust={exhaustion} "
        f"entropy_mkt={entropy_market} contra={contradiction} "
        f"data_conf={thesis_data.get('data_confidence', 0)} "
        f"clarity_q={clarity_pre.get('decision_quality', 1.0):.2f} "
        f"clarity_dom={clarity_pre.get('dominant_factor', '?')}"
    )

    return {
        "status": "PASS",
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
        adjustments["entropy"] = -5   # was -15
    elif entropy > 50:
        adjustments["entropy"] = 0    # was -5
    if evidence_families < 1:
        adjustments["evidence"] = -20
    elif evidence_families < 2:
        adjustments["evidence"] = -5  # was -20 (when <2, now only penalty when 0)
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
# PART 33B – MICRO STRUCTURE CONFIRMATION (Phase 1 Precision)
# ============================================================

def get_micro_structure_confirmation(coin: str, event: TradeEvent, mark: float, 
                                     candles_5m: List[dict]) -> Tuple[bool, int, List[str]]:
    """
    Weighted micro-structure confirmation inside event zone.
    
    Checks (weighted):
    - Higher Low (LONG) / Lower High (SHORT): +40
    - Volume spike (> 1.3x avg): +30
    - Delta reversal (opposite of entry direction): +30
    
    Confirmed if score >= 60 (need 2/3 signals)
    
    Return: (is_confirmed, score, reasons)
    """
    micro_score = 0
    reasons = []
    
    if not candles_5m or len(candles_5m) < 5:
        return False, 0, ["insufficient_candles"]
    
    # ===== SIGNAL 1: HIGHER LOW / LOWER HIGH =====
    recent_candles = candles_5m[-5:]  # Last 5 candles
    
    if event.direction == "LONG":
        # Looking for higher low: lowest point in last 5 should be higher than event.price_low
        lows = [float(c['l']) for c in recent_candles]
        current_low = min(lows)
        if current_low > event.price_low * 0.998:  # Allow tiny slip
            micro_score += 40
            reasons.append("higher_low")
    else:  # SHORT
        # Looking for lower high: highest point in last 5 should be lower than event.price_high
        highs = [float(c['h']) for c in recent_candles]
        current_high = max(highs)
        if current_high < event.price_high * 1.002:  # Allow tiny slip
            micro_score += 40
            reasons.append("lower_high")
    
    # ===== SIGNAL 2: VOLUME SPIKE =====
    vol_spike = get_volume_spike(coin)
    if vol_spike >= 1.3:  # 30% above average
        micro_score += 30
        reasons.append(f"volume_spike_{vol_spike:.2f}")
    
    # ===== SIGNAL 3: DELTA REVERSAL (flow confirmation) =====
    delta = get_ob_delta(coin)
    delta_shift = get_delta_shift(coin)
    
    if event.direction == "LONG" and delta_shift > 0:
        # LONG entry, delta should be positive
        micro_score += 30
        reasons.append("delta_positive")
    elif event.direction == "SHORT" and delta_shift < 0:
        # SHORT entry, delta should be negative
        micro_score += 30
        reasons.append("delta_negative")
    
    # ===== CALCULATE FINAL SCORE & CONFIRMATION =====
    micro_score = min(100, micro_score)  # Cap at 100
    is_confirmed = micro_score >= 60
    
    logger.debug(
        f"🔬 MICRO STRUCTURE {coin} {event.direction}: score={micro_score} "
        f"(confirmed={is_confirmed}) | {', '.join(reasons)}"
    )
    
    return is_confirmed, micro_score, reasons

# ============================================================
# PART 33C – VELOCITY MODULATOR (Phase 1.5 Adaptive Size/Threshold)
# ============================================================

def get_oi_zscore(coin: str) -> float:
    """
    Z-score untuk OI: berapa standard deviations dari mean recent OI.
    Untuk adaptif antar coin (bukan hardcoded threshold).
    """
    # FIX 1.2: Convert oi_history to list to prevent deque slice crash
    oi_raw = _oi_history.get(coin, deque(maxlen=60))
    oi_history = list(oi_raw) if oi_raw else []
    if len(oi_history) < 10:
        return 0.0
    
    oi_values = [v for ts, v in oi_history]
    mean_oi = sum(oi_values) / len(oi_values)
    variance = sum((x - mean_oi) ** 2 for x in oi_values) / len(oi_values)
    std_dev = variance ** 0.5 if variance > 0 else 1.0
    
    current_oi = oi_values[-1] if oi_values else 0
    z_score = (current_oi - mean_oi) / std_dev if std_dev > 0 else 0
    
    return z_score

def get_delta_persistence(coin: str, direction: str, window: int = 3) -> bool:
    """
    Check if delta maintained direction over last N candles.
    NOT just slope, but actual consistency.
    """
    delta_history = list(_rolling_delta.get(coin, deque()))
    if len(delta_history) < window:
        return False
    
    recent = delta_history[-window:]
    
    if direction == "LONG":
        # All should be positive or trend positive
        return all(d > -0.5 for d in recent) and recent[-1] > recent[0]
    else:  # SHORT
        # All should be negative or trend negative
        return all(d < 0.5 for d in recent) and recent[-1] < recent[0]

def get_velocity_score(coin: str, direction: str) -> Tuple[int, List[str]]:
    """
    Momentum acceleration validator (NOT predictor).
    """
    velocity_score = 0
    reasons = []
    
    # ===== AMBIL SEMUA INPUT SEKALI (untuk log) =====
    delta_shift = get_delta_shift(coin)
    vol_spike = get_volume_spike(coin)
    oi_z = get_oi_zscore(coin)
    
    # ===== INSTRUMENTATION LOG =====
    trace(f"[VELOCITY INPUT {coin}] delta_shift={delta_shift:.2f}, vol_spike={vol_spike:.2f}, oi_z={oi_z:.2f}")
    # ===================================
    
    # ===== SIGNAL 1: DELTA ACCELERATION + PERSISTENCE =====
    delta_accel = False
    
    delta_history = list(_rolling_delta.get(coin, deque()))
    if len(delta_history) >= 3:
        # Slope: recent change
        d_slope = (delta_history[-1] - delta_history[-3]) / 2
        
        # Persistence: consistently in right direction
        delta_persist = get_delta_persistence(coin, direction, window=3)
        
        # LOG persistence juga
        trace(f"[VELOCITY PERSIST {coin}] delta_persist={delta_persist}")
        
        if (direction == "LONG" and d_slope > 0.5 and delta_persist) or \
           (direction == "SHORT" and d_slope < -0.5 and delta_persist):
            velocity_score += 40
            reasons.append("delta_accel+persist")
            delta_accel = True
    
    # ===== SIGNAL 2: VOLUME ACCELERATION =====
    vol_spike = get_volume_spike(coin)  # ← HAPUS baris ini (udah diambil di atas)
    if vol_spike > 1.2:
        velocity_score += 25
        reasons.append(f"vol_accel_{vol_spike:.2f}")
    
    # ===== SIGNAL 3: OI IMPULSE =====
    oi_z = get_oi_zscore(coin)  # ← HAPUS baris ini (udah diambil di atas)
    if oi_z > 1.2:
        velocity_score += 20
        reasons.append(f"oi_impulse_z{oi_z:.2f}")
    
    # ===== SIGNAL 4: DELTA PERSISTENCE (bonus) =====
    if not delta_accel:
        delta_persist = get_delta_persistence(coin, direction, window=3)
        if delta_persist:
            velocity_score += 15
            reasons.append("delta_persist_bonus")
    
    velocity_score = min(100, velocity_score)
    
    trace(f"⚡ VELOCITY {coin} {direction}: score={velocity_score} | {', '.join(reasons)}")
    
    return velocity_score, reasons

def apply_velocity_modifier(velocity_score: int, 
                           position_size_mult: float, 
                           threshold: int) -> Tuple[float, int]:
    """
    Apply velocity as size/threshold modulator (NOT gate).
    
    Velocity_score → size_mult, threshold_adj
    >75    → 1.15x size, -2 threshold (aggressive)
    60-75  → 1.05x size, -1 threshold (normal+)
    40-60  → 0.9x size, 0 threshold (defensive)
    <40    → 0.7x size, +1 threshold (small)
    
    Return: (adjusted_size_mult, adjusted_threshold)
    """
    size_mult = 1.0
    threshold_adj = 0
    
    if velocity_score >= 75:
        size_mult = 1.15
        threshold_adj = -2
        logger.debug(f"⚡ VELOCITY BOOST: size*1.15, threshold-2")
    
    elif velocity_score >= 60:
        size_mult = 1.05
        threshold_adj = -1
        logger.debug(f"⚡ VELOCITY NORMAL+: size*1.05, threshold-1")
    
    elif velocity_score >= 40:
        size_mult = 0.9
        threshold_adj = 0
        logger.debug(f"⚡ VELOCITY DEFENSIVE: size*0.9")
    
    else:  # < 40
        size_mult = 0.7
        threshold_adj = +1
        logger.debug(f"⚡ VELOCITY WEAK: size*0.7, threshold+1")
    
    adjusted_size = position_size_mult * size_mult
    adjusted_threshold = max(50, min(85, threshold + threshold_adj))  # Keep in bounds
    
    return adjusted_size, adjusted_threshold

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
    
    # ===== DEFAULTS (GUARD AGAINST UnboundLocalError) =====
    allow_entry = True
    mode_override = None
    # =======================================================

    # ===== P2: INVENTORY CONTROL GATES =====
    # NOTE: stale position closure SUDAH di-handle oleh emergency_lifecycle_cleanup()
    # (48h threshold, dipanggil di scheduled_cleanup_v7 line ~9904) — jangan duplikat disini.
    MAX_TOTAL_OPEN = 120
    MAX_COIN_OPEN = 15

    # 1. Total open positions guard (hard cap, beda dari get_exposure_adjusted_threshold
    #    yang soft-scale threshold tapi gak pernah dipanggil di codebase)
    with TRADE_MANAGER._lock:
        total_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
    if total_open >= MAX_TOTAL_OPEN:
        logger.warning(f"🔴 INVENTORY LIMIT: {total_open}/{MAX_TOTAL_OPEN} open, blocking {coin}")
        update_fatigue_memory(event.type)
        return None

    # 2. Per-coin open positions guard
    with TRADE_MANAGER._lock:
        coin_open = sum(1 for p in TRADE_MANAGER.positions.values()
                         if p.coin == coin and p.status == "OPEN")
    if coin_open >= MAX_COIN_OPEN:
        logger.warning(f"🔴 COIN LIMIT: {coin}={coin_open}/{MAX_COIN_OPEN}, blocking")
        update_fatigue_memory(event.type)
        return None
    # ===== END INVENTORY CONTROL GATES =====

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

    # ===== ENSURE CONTEXT IS OBJECT =====
    if isinstance(context, dict):
        # Convert dict to ContextSnapshot if needed
        context = ensure_context_fields(context)

    # ===== CLARITY CHECK =====
    clarity = compute_clarity(
        context,
        breath,
        confidence_data["score_long"],
        confidence_data["score_short"],
        context.transition_prob
    )
    
    # FIX 2.1 + FIX 4.1: Reduce clarity gate to 0.45 + use dominant_factor for strong contradiction
    # Only skip on strong_contradiction (signal conflict) or extreme chaos (<0.40)
    if clarity.get("dominant_factor") == "strong_contradiction":
        logger.debug(f"{coin}: strong contradiction detected (long vs short both >55), skipping")
        update_fatigue_memory(event.type)
        return None
    
    if clarity["decision_quality"] < 0.40:  # Only skip on extreme chaos
        logger.debug(f"{coin}: extreme chaos {clarity['decision_quality']:.2f}, skipping")
        update_fatigue_memory(event.type)
        return None

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

    # ===== U4: CALIBRATE CONFIDENCE =====
    raw_conf = confidence_data.get("confidence", 50.0)
    try:
        cal = calibrate_confidence_v10(coin, raw_conf)
        calibrated_conf = cal.calibrated
        confidence_data['confidence_calibrated'] = calibrated_conf
        confidence_data['calibration_factor'] = cal.calibration_factor
    except Exception as e:
        logger.error(f"Calibration failed for {coin}: {e}")
        # FIX 2.2: Fallback to raw confidence instead of zeroing
        confidence_data['confidence_calibrated'] = raw_conf
        confidence_data['calibration_factor'] = 1.0
    
    # Jika calibrated lebih rendah dari raw, kita naikkan threshold (lebih ketat)
    calibration_penalty = (raw_conf - confidence_data.get('confidence_calibrated', raw_conf)) * 0.3 if raw_conf > confidence_data.get('confidence_calibrated', raw_conf) else 0

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

    # Legacy breath filter
    if context.breath_bull < TUNABLE["BREATH_WEAK_THRESHOLD"] and coin != "BTC" and event.direction == "LONG":
        position_size_mult *= 0.6
    if context.breath_bear < TUNABLE["BREATH_WEAK_THRESHOLD"] and coin != "BTC" and event.direction == "SHORT":
        position_size_mult *= 0.6

    # ===== EXECUTION MODE BLEND =====
    if context.shock_score > TUNABLE["SHOCK_AGGRESSIVE_THRESHOLD"] and exec_mode != ExecutionMode.DEFENSIVE:
        blend_weights = {"aggressive": 1.0, "balanced": 0.0, "precision": 0.0}
    else:
        blend_weights = get_execution_mode_blend(
            confidence_data["decision_energy"], confidence_data["entropy_market"],
            confidence_data["decision_acceleration"], intent_legacy
        )

    execution_mode_str = get_execution_mode_from_blend(blend_weights)
    threshold_boost = get_mode_threshold_boost(blend_weights)

    # ===== DECISION TEMPERATURE (EARLY - for U2 use) =====
    breath_v10 = compute_market_breath_v10()
    temp_data = compute_decision_temperature(
        context=context.__dict__ if hasattr(context, "__dict__") else context,
        breath=breath_v10,
        reaction=get_current_reaction()
    )

    # ===== DYNAMIC THRESHOLD WITH U1, U2, U4 =====
    base_threshold = get_dynamic_threshold(coin, thesis_data["market_regime"], thesis_data["volatility_regime"])
    
    # Apply U1 + U2 + U4 adjustments to base threshold
    rr = confidence_data.get("rr", 1.5)
    rr_adj = get_rr_bonus(rr)
    regime_adj = get_regime_adjustment(thesis_data["market_regime"])
    temp_adj = get_temperature_adjustment(temp_data["state"])
    wr_adj = get_recent_wr_adjustment(coin)
    cal_penalty = int(calibration_penalty) if calibration_penalty > 0 else 0
    
    adjusted_base = base_threshold + rr_adj + regime_adj + temp_adj + wr_adj + cal_penalty
    entropy_adjusted_threshold = get_entropy_adjusted_threshold(adjusted_base, confidence_data["entropy_market"])

    filter_penalty = 1.0 + ((100 - filter_score) / 100) * 0.5
    adjusted_threshold = int(entropy_adjusted_threshold * threshold_boost * filter_penalty)

    size_boost = 1.0 + (1.0 - confidence_data["position_size_mult"]) * 0.2
    # ===== P2 FIX: ADAPTIVE THRESHOLD LOWERING (FIXED + SAFE) =====
    recent_wr = 0.5  # default netral
    try:
        recent_entries = (
            list(_decision_journal)[-20:]
            if len(_decision_journal) >= 20
            else list(_decision_journal)
        )
        # Hanya closed executed trades (pakai getattr biar aman)
        closed = [
          e for e in recent_entries
            if getattr(e, "executed", False)
            and getattr(e, "outcome", None) is not None
        ]
        if closed:
            wins = sum(1 for e in closed if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
            recent_wr = wins / len(closed)
        # else: tetap 0.5
    except Exception:
        recent_wr = 0.5

    now_time = time.time()
    hourly_exec = sum(
        1 for e in _decision_journal
        if getattr(e, "timestamp", 0) > now_time - 3600
        and getattr(e, "executed", False)
    )

    final_threshold = get_adaptive_threshold(
        market_regime=thesis_data.get("market_regime", "UNKNOWN"),
        entropy_market=confidence_data.get("entropy_market", 50),
        recent_win_rate=recent_wr,
        execution_count=hourly_exec
    )

    # ===== SYMBOL WR COOLDOWN (cegah overfitting per-coin, mis. AVAX) =====
    try:
        with _journal_lock:
            coin_entries = [e for e in _decision_journal if e.coin == coin and getattr(e, "executed", False)]
            coin_recent = coin_entries[-20:] if len(coin_entries) >= 20 else coin_entries
            coin_closed = [e for e in coin_recent if getattr(e, "outcome", None) is not None]
        if len(coin_closed) >= 5:
            coin_wins = sum(1 for e in coin_closed if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
            coin_wr = coin_wins / len(coin_closed)
            if coin_wr == 0.0:
                logger.warning(f"🔴 ZERO WR {coin}: {len(coin_closed)} trades terakhir semuanya loss, threshold dinaikkan + size diperkecil")
                final_threshold = int(final_threshold * 1.3)
                position_size_mult *= 0.3
                exec_mode = ExecutionMode.DEFENSIVE
    except Exception as e:
        logger.warning(f"Symbol WR cooldown error for {coin}: {e}")


    # ... lanjut ke shadow registration & decision_type = REJECT (kode di bawah tetap sama)
    # ===== V10: BREATH ADJUSTMENT (Advanced) =====
    if breath_v10.get("participation", 0.5) < 0.4 and coin != "BTC":
        position_size_mult *= 0.7
    if breath_v10.get("leadership", 0) > 2 and coin != "BTC" and event.direction == "LONG":
        position_size_mult *= 1.1
    if breath_v10.get("rotation", 0) > 1 and coin not in ["BTC", "ETH"]:
        position_size_mult *= 1.15
    
    # ===== CONVICTION BUDGET (NEW - V10) =====
    conviction_data = compute_conviction_budget(
        context=context.__dict__ if hasattr(context, "__dict__") else context,
        event={
            "intent_drift": intent_drift if "intent_drift" in locals() else 0.0,
            "direction": event.direction
        },
        market={"breath_bull": breath_v10.get("bull", 0.5)}
    )
    
    # ===== OPPORTUNITY TRACKING (NEW - V10) =====
    record_opportunity_scan(coin)
    if confidence_data.get("final_score", 0) > 60:
        record_opportunity_qualified(coin)
    
    # ===== APPLY CONVICTION TO POSITION SIZE (NEW - V10) =====
    # Position size is now based on conviction, not arbitrary
    base_size_for_conviction = position_size_mult
    size_mult_from_conviction = conviction_data["size_mult"] * temp_data["size_boost"]
    position_size_mult = base_size_for_conviction * min(1.0, size_mult_from_conviction)  # Cap at 1.0
    
    # ===== CHECK CONVICTION QUALIFICATION (NEW - V10) =====
    if not conviction_data["is_qualified"]:
        reason = f"conviction_{conviction_data['conviction']:.0f}_lt_45"
        record_opportunity_rejected(coin, "conviction_gate")
        inc_pipeline_counter("reject_conviction")
        logger.debug(f"❌ CONVICTION REJECT {coin}: {reason}")
        update_fatigue_memory(event.type)
        return None

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

    # ===== GENERATE THESIS =====
    market_state = thesis_data.get("market_state", MarketState.UNKNOWN)
    if market_state is None:
        market_state = MarketState.UNKNOWN
    thesis_obj = generate_thesis_from_event_v7(coin, event, mark, market_state, intent, belief)

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
    why_not = generate_why_not(coin, thesis_data["funding_pct"], confidence_data["entropy_market"],
                               thesis_data["oi_roc"], intent, active_count, fatigue_penalty)

    if intent_success < 0.4:
        why_not += f" | intent success {intent_success*100:.0f}%"

    confidence_breakdown = f"S:{confidence_data['score_long']:.0f}|F:{filter_score:.0f}|E:{confidence_data['evidence_families']}"

    reason = (f"{event.type} | Intent:{intent.value} | Belief:{belief.value} | "
              f"Mode:{execution_mode_str} | V10 Mode:{exec_mode.value.upper()} | "
              f"Filter:{filter_score:.0f} | DE:{confidence_data['decision_energy']:.1f} | Score:{confidence_data['final_score']}")

    signal_id = generate_signal_id(coin, event.direction)
    eval_delay = get_evaluation_delay(thesis_data["atr_pct"], confidence_data["rr"], thesis_data["market_regime"])

    # ===== ADVANCED METRICS =====
    candles_5m = get_candles(coin, "5m", 20, thesis_data["master_candles"])
    delta_history = list(_rolling_delta.get(coin, []))[-5:]
    # FIX 1.1: Convert deque to list BEFORE slice operation
    oi_raw = _oi_history.get(coin, deque())
    oi_history = [v for ts, v in list(oi_raw)[-5:]]
    hl = compute_hidden_liquidity(coin, candles_5m, delta_history, oi_history) if candles_5m else {"score": 0, "side": "NONE"}

    micro_acc = compute_micro_acceptance(coin, event, candles_5m) if candles_5m else {"score": None, "status": "INSUFFICIENT"}

    clarity_str = "UNCLEAR" if confidence_data["entropy_market"] > 50 else "CLEAR"
    failed_risk = get_failed_move_risk(
        coin, event.type, thesis_data["delta"], thesis_data["vol_spike"],
        clarity_str, intent.value, event.direction, mark
    )

    intent_drift = compute_intent_drift(coin)

    expected_move = thesis_data.get("atr_pct", 0.5)
    actual_move = thesis_data["vol_spike"] * 0.5
    surprise = compute_surprise_index(coin, expected_move, actual_move)

    update_intent_vector(coin, event, thesis_data["delta"], thesis_data["vol_spike"],
                         micro_acc.get("score", 50), context)

    update_intent_timeline(coin, intent.value)
                          # PATCH 2: DISCOVERY → WATCH
    if intent_drift > 0.7:
        mode_override = "WATCH"
        final_threshold = int(final_threshold * 1.3)
        position_size_mult *= 0.3
        allow_entry = False
        why_not += " | WATCH mode: high intent drift, observing only"
    elif intent_drift > 0.5:
        mode_override = "OBSERVE"
        final_threshold = int(final_threshold * 1.1)
        position_size_mult *= 0.7
        why_not += " | OBSERVE mode: moderate drift, reduced size"
    else:
        mode_override = None
        allow_entry = True
    # ============================================================
    # INTELLIGENT AGGRESSION: ADAPTIVE RELAXATION
    # ============================================================
    adaptive_relax = get_adaptive_relaxation()
    original_threshold = final_threshold
    if adaptive_relax > 0:
        final_threshold = max(50, min(95, final_threshold - adaptive_relax))
        logger.debug(f"🧠 GENIUS RELAX applied: threshold {original_threshold} → {final_threshold} (relax={adaptive_relax})")
    # ============================================================
    # MICRO STRUCTURE CONFIRMATION (Phase 1 Precision Filter)
    # ============================================================
    micro_confirmed, micro_score, micro_reasons = get_micro_structure_confirmation(
        coin, event, mark, candles_5m
    )
    
    # If micro-structure NOT confirmed, reject early (precision filter)
    if not micro_confirmed:
        logger.debug(f"🔬 MICRO REJECT {coin} {event.direction}: score={micro_score} < 60 | {micro_reasons}")
        record_opportunity_rejected(coin, "micro_structure_gate")
        inc_pipeline_counter("reject_micro_structure")
        update_fatigue_memory(event.type)
        return None
    
    # Micro confirmed: boost confidence slightly (quality signal)
    confidence_data["micro_structure_score"] = micro_score
    confidence_data["micro_structure_confirmed"] = True

    # ===== VELOCITY MODULATOR (Phase 1.5: Adaptive Size/Threshold) =====
    velocity_score, velocity_reasons = get_velocity_score(coin, event.direction)
    confidence_data["velocity_score"] = velocity_score
    confidence_data["velocity_reasons"] = velocity_reasons
    
    # Apply velocity adjustments (NOT gate, just modulation)
    position_size_mult_original = position_size_mult
    final_threshold_original = final_threshold
    
    position_size_mult, final_threshold = apply_velocity_modifier(
        velocity_score, position_size_mult, final_threshold
    )
    
    if position_size_mult != position_size_mult_original or final_threshold != final_threshold_original:
        logger.debug(
            f"⚡ VELOCITY APPLIED {coin}: "
            f"size {position_size_mult_original:.2f}→{position_size_mult:.2f}, "
            f"threshold {final_threshold_original}→{final_threshold}"
        )

    # ===== THRESHOLD CHECK + SHADOW REGISTRATION + UNIVERSAL JOURNAL =====
    decision_type = "EXECUTE"
    why_not_final = why_not
    shadow_registered = False
                          # ===== DEBUG: EXECUTION DECISION POINT =====
    logger.info(
        f"🎯 EXEC DECISION {coin}: "
        f"score={confidence_data.get('final_score', 0)}, "
        f"threshold={final_threshold}, "
        f"conviction={conviction_data.get('conviction', 0):.0f}, "
        f"allow_entry={allow_entry}, "
        f"micro_confirmed={micro_confirmed}"
    )

    # === INSTRUMENTATION: EXEC_DECISION ===
    logger.info(
        f"EXEC_DECISION {coin} "
        f"final_score={confidence_data.get('final_score', 0):.0f} "
        f"final_threshold={final_threshold} "
        f"gap={final_threshold - confidence_data.get('final_score', 0):.0f} "
        f"intent={intent.value if hasattr(intent, 'value') else intent} "
        f"belief={belief.value if hasattr(belief, 'value') else belief} "
        f"exec_mode={exec_mode.value if hasattr(exec_mode, 'value') else exec_mode}"
    )
    
    if confidence_data["final_score"] < final_threshold:
        score = confidence_data["final_score"]
        gap = final_threshold - score
        rr = confidence_data.get("rr", 0.0)
        
        # === INSTRUMENTATION: EXEC_SKIP ===
        logger.warning(
            f"EXEC_SKIP {coin} "
            f"score={score:.0f} < threshold={final_threshold} "
            f"gap={gap:.0f} "
            f"reason=score_below_threshold"
        )

        # NEAR-MISS: register shadow tapi JANGAN return, lanjut ke reject
        if gap <= TUNABLE["SHADOW_MAX_GAP"]:
            try:
                register_shadow(
                    coin, event.direction, mark, confidence_data, event,
                    intent, belief, hl, micro_acc, failed_risk,
                    intent_drift, surprise, gap, final_threshold, rr
                )
                shadow_registered = True
            except Exception as _se:
                logger.debug(f"register_shadow error: {_se}")

        decision_type = "REJECT"
        why_not_final = f"score_{score}_lt_{final_threshold} (gap={gap})"
        reason_reject = f"score {score:.0f} < {final_threshold}"
        record_opportunity_rejected(coin, reason_reject)
        inc_pipeline_counter("reject_execute")

    # SELALU LOG KE JOURNAL (executed + rejected)
    _positive_factors_early = [event.type] + confidence_data.get("evidence_reasons", [])
    _narrative = {
        "decision_type": decision_type,
        "why_not": why_not_final,
        "wait_value": wait_value,
        "threshold": final_threshold,
        "score": confidence_data["final_score"],
        "mode": execution_mode_str,
        "v10_mode": exec_mode.value.upper()
    }
    journal_entry_universal = DecisionJournalEntry(
        timestamp=time.time(),
        coin=coin,
        event_type=event.type,
        direction=event.direction,
        score=confidence_data["final_score"],
        mode=execution_mode_str,
        executed=(decision_type == "EXECUTE"),
        shadow=(decision_type != "EXECUTE"),
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
        signal_id=signal_id,
        narrative=_narrative
    )
    log_decision_journal(journal_entry_universal)
    inc_pipeline_counter("journal")

    if decision_type == "REJECT":
        if position_size_mult > 0.3:
            position_size_mult = max(0.15, position_size_mult * 0.7)
        else:
            update_fatigue_memory(event.type)
            return None

    # If DISCOVERY mode, still log but don't execute
    if not allow_entry:
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
            "label": "👀 WATCH",
            "why_not": why_not,
            "hypothesis": thesis_obj,
            "context_age": context_age,
        }
        # Journal already logged universally above
        auto_review()
        return shadow_result

    # ===== RECORD EXECUTION (NEW - V10) =====
    try:
        micro_acc_score = micro_acc.get("score", 50.0) if micro_acc and micro_acc.get("score") is not None else 50.0
        accept_intent(coin=coin, acceptance_score=float(micro_acc_score))
    except Exception as e:
        logger.debug(f"Accept intent error: {e}")

    record_opportunity_executed(coin)

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

        # ===== KIRIM NOTIF OPEN (verify DB commit dulu, anti race condition) =====
        if USER_ID and not PAPER_MODE and decision_type == "EXECUTE":
            try:
                verified = False
                for attempt in range(2):
                    try:
                        vconn = db_connect()
                        vc = vconn.cursor()
                        vc.execute("SELECT COUNT(*) FROM signals WHERE signal_id=?", (signal_id,))
                        verified = vc.fetchone()[0] > 0
                        vconn.close()
                    except Exception as ve:
                        logger.debug(f"Open notif verify attempt {attempt} failed: {ve}")
                    if verified:
                        break
                    time.sleep(0.2)

                if not verified:
                    logger.error(f"🔴 ORPHAN PREVENTION: {signal_id} not persisted yet, skipping OPEN notif")
                else:
                    direction_emoji = "🔼" if event.direction == "LONG" else "🔽"
                    open_msg = f"🟡 <b>OPEN</b> {coin} [{direction_emoji} {event.direction}]\n"
                    open_msg += f"├─ Entry: {fmt_price(mark)}\n"
                    open_msg += f"├─ SL: {fmt_price(confidence_data['sl'])}\n"
                    open_msg += f"├─ TP: {fmt_price(confidence_data['tp'])}\n"
                    open_msg += f"├─ Score: {confidence_data['final_score']}\n"
                    open_msg += f"├─ RR: 1:{confidence_data['rr']:.1f}\n"
                    open_msg += f"└─ Signal: {signal_id}"
                    bot.send_message(USER_ID, open_msg, parse_mode='HTML')
                    logger.info(f"✅ OPEN notif SENT: {coin} {event.direction} signal_id={signal_id}")
            except Exception as e:
                logger.error(f"🔴 OPEN notif FAILED to send for {coin} signal_id={signal_id}: {e}")

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

    # ===== LOG JOURNAL (already logged universally above) =====
    auto_review()

    # ===== RANK / CAPITAL ALLOCATOR =====
    rank_text = "No rank"
    try:
        active_count = len(_active_candidates)
        if active_count > 0:
            # Get score of all active candidates
            with _active_candidates_lock:
                active_scores = [(c, data.get("score", 0)) for c, data in _active_candidates.items()]
            
            # Sort by score descending and find rank
            all_scores = [score for _, score in active_scores if _ != coin]
            all_scores.append(confidence_data["final_score"])
            all_scores_sorted = sorted(all_scores, reverse=True)
            rank = all_scores_sorted.index(confidence_data["final_score"]) + 1
            total = len(all_scores_sorted)
            rank_text = f"#{rank}/{total}"
        else:
            rank_text = "Solo"
    except Exception as e:
        logger.debug(f"Rank calculation error: {e}")

    # ===== RETURN RESULT =====
    # ===== P1 FIX: CALCULATE SCALED TARGETS =====
    targets = calculate_scaled_targets(
        entry=mark,
        direction=event.direction,
        atr_pct=thesis_data.get("atr_pct", 2.0),
        market_regime=thesis_data.get("market_regime", "UNKNOWN")
    )
    
    return {
        "coin": coin,
        "signal_id": signal_id,
        "direction": event.direction,
        "score": confidence_data["final_score"],
        "entry": mark,
        "sl": confidence_data["sl"],
        "tp": confidence_data["tp"],
        "tp_scaled": targets,
        "rr": confidence_data["rr"],
        "reason": reason,
        "area": event.type,
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
        "explanation": explanation,
        "hidden_liquidity": hl.get("score", 0),
        "hidden_side": hl.get("side", "NONE"),
        "micro_acceptance": micro_acc.get("score"),
        "micro_acceptance_status": micro_acc.get("status"),
        "failed_risk": failed_risk.get("risk", 1.0),
        "failed_reason": failed_risk.get("reason"),
        "intent_drift": intent_drift,
        "surprise": surprise,
        "rank": rank_text,
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
        try:
            breath = compute_market_breath_v10()
        except Exception as e:
            logger.warning(f"market_breath unavailable: {e}, using neutral defaults")
            breath = {
                "state": "UNKNOWN",
                "score": 50,
                "bull": 0.5,
                "bear": 0.5,
                "participation": 0.5
            }

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

        # ===== MIN CONFIDENCE GATE (prevent low-quality execute spam) =====
        MIN_EXEC_CONF = 35  # Require at least 35% confidence to execute
        if confidence_data.get("confidence", 0) < MIN_EXEC_CONF:
            logger.debug(f"⏸️ EXECUTE SKIP [{coin}] confidence={confidence_data.get('confidence', 0):.1f} < {MIN_EXEC_CONF}")
            return None

        # ===== LAYER 4: EXECUTE DECISION =====
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"],
            context, breath
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
    """V10 + Phase 1 upgrades: dengan funnel trace lengkap"""
    try:
        # ===== COUNTER: SCAN =====
        record_opportunity_scan(coin)
        inc_pipeline_counter("check")

        # ===== LAYER 0: CONTEXT =====
        try:
            regime = interpret_regime_v10(coin)
            ctx = get_context_snapshot(coin)
            _context_memory.add(ctx)
            breath = compute_market_breath_v10()
        except Exception as e:
            logger.error(f"Context error {coin}: {e}")
            record_opportunity_rejected(coin, "context_error")
            inc_pipeline_counter("reject_obs")
            return None

        # ===== LAYER 1: OBSERVE =====
        obs = observe_market(coin, mark, master_candles)
        if not obs or obs.get("status") == "REJECT":
            reason = obs.get("reason", "observe_failed") if obs else "observe_none"
            logger.debug(f"❌ OBS REJECT {coin}: {reason}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_obs")
            return None
        inc_pipeline_counter("obs")
        logger.debug(f"✅ OBS PASS {coin}: event={obs['best_event'].type if obs.get('best_event') else 'NONE'}")

        
        # ===== LAYER 2: THESIS [DEBUG] =====
        thesis_data = build_thesis(obs)
        if not thesis_data or thesis_data.get("status") == "REJECT":
            reason = thesis_data.get("reason", "thesis_failed") if thesis_data else "thesis_none"
            logger.warning(f"THESIS_REJECT_{coin} reason={reason} obs={obs.get('intent') if obs else 'none'}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_thesis")
            return None
        inc_pipeline_counter("thesis")
        logger.info(f"THESIS_PASS_{coin} intent={thesis_data.get('intent', 'NONE')}")
        inc_pipeline_counter("thesis")
        logger.debug(f"✅ THESIS PASS {coin}: intent={thesis_data.get('intent', 'NONE')}")

        # ===== LAYER 3: CONFIDENCE =====
        # OB / FVG assessment
        event = thesis_data.get('event')
        ob_reaction = None
        fvg_quality = None
        if event:
            if event.type in ("OB", "OB_FLOW"):
                candles_1h = get_candles(coin, "1h", 60, master_candles)
                if candles_1h:
                    ob_reaction = assess_ob_reaction_v10(coin, event, candles_1h)
                    if ob_reaction.is_strong():
                        pass  # boost applied in confidence_data below
            elif event.type in ("FVG", "FVG_FLOW"):
                candles_1h = get_candles(coin, "1h", 60, master_candles)
                if candles_1h:
                    fvg_quality = assess_fvg_quality_v10(coin, event, candles_1h)

        confidence_data = compute_confidence(thesis_data)
        if not confidence_data or confidence_data.get("status") == "REJECT":
            reason = confidence_data.get("reason", "confidence_failed") if confidence_data else "confidence_none"
            logger.debug(f"❌ CONFIDENCE REJECT {coin}: {reason}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_conf")
            return None
        inc_pipeline_counter("confidence")
        logger.debug(f"✅ CONFIDENCE PASS {coin}: score={confidence_data.get('final_score', 0)}")

        # Apply OB/FVG adjustment to confidence post-compute
        if ob_reaction:
            if ob_reaction.is_strong():
                confidence_data['confidence'] = min(100, confidence_data['confidence'] + 10)
            else:
                confidence_data['confidence'] = max(0, confidence_data['confidence'] - 15)
        if fvg_quality:
            if fvg_quality.quality_score > 60:
                confidence_data['confidence'] = min(100, confidence_data['confidence'] + 5)
            else:
                confidence_data['confidence'] = max(0, confidence_data['confidence'] - 10)

        # Calibrate confidence
        cal = calibrate_confidence_v10(coin, confidence_data['confidence'])
        confidence_data['confidence_calibrated'] = cal.calibrated
        confidence_data['calibration_factor'] = cal.calibration_factor
        confidence_data['calibration_samples'] = cal.sample_size

        record_opportunity_qualified(coin)

        # ===== LAYER 4: EXECUTE =====
        inc_pipeline_counter("execute_called")
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"],
            ctx, breath
        )
        
        if result:
            inc_pipeline_counter("execute_pass")
             # ===== TERMINAL LOG =====
            print(f"🚀 EXECUTED {coin} {result['direction']} score={result['score']} RR={result.get('rr', 0):.1f}")
            logger.info(f"🚀 EXECUTED {coin}: {result['direction']} score={result['score']}")
            #trace :
            trace = DecisionTrace(
                timestamp=time.time(),
                coin=coin,
                event_type=result["area"],
                belief_state=result["belief_state"],
                confidence=result.get("confidence_calibrated", result["decision_energy"]),
                decision_energy=result["decision_energy"],
                final_decision="EXECUTE",
                reasons=result.get("positive_evidence", []),
                why_not=[result.get("why_not", "")] if result.get("why_not") else [],
                what_changed=f"regime:{regime.regime}|trans:{regime.transition_prob:.0f}%|score:{result['score']}",
                context_age=result.get("context_age", 0.0),
                execution_mode=result.get("execution_mode_v10", "NORMAL")
            )
            log_decision_trace(trace)

            result['regime_interpretation'] = regime
            result['ob_reaction'] = ob_reaction
            result['fvg_quality'] = fvg_quality
            result['context_memory'] = _context_memory
            result['confidence_calibrated'] = cal.calibrated
            result['calibration_samples'] = cal.sample_size
        else:
            logger.debug(f"⏸️ EXECUTE SKIP {coin}")
            record_opportunity_rejected(coin, "execute_skipped")

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
                pnl = (price - entry) / max(entry, 0.01) * 100
                outcome = "PARTIAL_WIN" if pnl > 0 else "PARTIAL_LOSS"
        else:
            if price <= tp:
                outcome, pnl = "TP_HIT", (entry - tp) / max(entry, 0.01) * 100
            elif price >= sl:
                outcome, pnl = "SL_HIT", (entry - sl) / max(entry, 0.01) * 100
            else:
                pnl = (entry - price) / max(entry, 0.01) * 100
                outcome = "PARTIAL_WIN" if pnl > 0 else "PARTIAL_LOSS"

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

# ============================================================
# DISCOVERY ENGINE V2 — Fully Data-Driven (no hardcode coin lists)
# ============================================================
# Filosofi: bot tidak diberi tahu "coin mana yang menarik" (hardcode),
# tapi "perilaku mana yang menarik" (percentile, z-score, correlation,
# Bayesian prior). Semua threshold relatif terhadap distribusi pasar
# saat itu, bukan angka mutlak yang basi dalam 3 bulan.

_discovery_weights = {
    "oi_change": 0.40,
    "oi_acceleration": 0.25,
    "volume": 0.20,
    "cluster": 0.05,
    "memory": 0.10,
}
_discovery_weights_lock = threading.RLock()


def percentile_rank(values: List[float]) -> Dict[float, float]:
    """Ubah list nilai jadi percentile rank 0-1 (urutan relatif, bukan angka mentah)."""
    if not values:
        return {}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return {sorted_vals[0]: 0.5}
    return {v: i / (n - 1) for i, v in enumerate(sorted_vals)}


def rolling_zscore(value: float, history: List[float]) -> float:
    """Z-score value terhadap history-nya sendiri (bukan dikali angka magic)."""
    if len(history) < 3:
        return 0.0
    mean = float(np.mean(history))
    std = float(np.std(history))
    if std < 1e-9:
        return 0.0
    return (value - mean) / std


def build_oi_correlation_matrix(coins: List[str], lookback: int = 12) -> Dict[str, Dict[str, float]]:
    """Correlation matrix antar coin berdasarkan OI velocity series (tanpa hardcode cluster)."""
    series = {}
    with _oi_lock:
        for coin in coins:
            hist = list(_oi_history.get(coin, deque()))[-lookback:]
            if len(hist) >= 6:
                series[coin] = [v for _, v in hist]

    corr_matrix: Dict[str, Dict[str, float]] = {}
    coin_list = list(series.keys())
    for c1 in coin_list:
        corr_matrix[c1] = {}
        for c2 in coin_list:
            if c1 == c2:
                corr_matrix[c1][c2] = 1.0
                continue
            s1, s2 = series[c1], series[c2]
            min_len = min(len(s1), len(s2))
            if min_len < 6:
                corr_matrix[c1][c2] = 0.0
                continue
            try:
                corr = np.corrcoef(s1[-min_len:], s2[-min_len:])[0, 1]
                corr_matrix[c1][c2] = float(corr) if not np.isnan(corr) else 0.0
            except Exception:
                corr_matrix[c1][c2] = 0.0
    return corr_matrix


def get_cluster_bonus(coin: str, corr_matrix: Dict[str, Dict[str, float]]) -> float:
    """Bonus jika coin bergerak bareng coin lain yang juga aktif (cluster momentum, tanpa hardcode nama)."""
    if coin not in corr_matrix:
        return 0.0
    corrs = [(c, r) for c, r in corr_matrix[coin].items() if c != coin and r > 0.5]
    if not corrs:
        return 0.0
    avg_corr = sum(r for _, r in corrs) / len(corrs)
    count_bonus = min(1.0, len(corrs) / 5)
    return avg_corr * count_bonus


def get_coin_prior(coin: str) -> float:
    """Bayesian prior dari histori WR coin ini: (wins + 5) / (total + 10).
    Smoothing supaya coin baru/jarang trading tidak langsung dapat skor ekstrem."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT COUNT(*),
                    SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END)
                    FROM signals WHERE coin = ? AND evaluated = 1''', (coin,))
        row = c.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return (wins + 5) / (total + 10)
    except Exception as e:
        logger.debug(f"get_coin_prior error {coin}: {e}")
        return 0.5
    finally:
        if conn:
            conn.close()


def get_market_entropy_for_discovery() -> float:
    """Entropy market 0-1: seberapa kacau (dispersi) pergerakan coin secara cross-sectional.
    Dipakai untuk adaptive scan budget — bukan threshold fixed.
    Basis: dispersi price ROC antar coin (5m candle), bukan OI 2-point delta —
    OI antar snapshot 60s biasanya nyaris flat, jadi entropy selalu mendekati 0."""
    try:
        snapshot = get_snapshot()
        if not snapshot or not snapshot.mids:
            return 0.5
        rocs = []
        for coin in list(snapshot.mids.keys())[:60]:
            candles = get_candles(coin, "5m", 3)
            if candles and len(candles) >= 2:
                c_now = float(candles[-1]['c'])
                c_prev = float(candles[-2]['c'])
                if c_prev:
                    rocs.append(abs((c_now - c_prev) / c_prev * 100))
        if len(rocs) < 10:
            return 0.5
        return min(1.0, float(np.std(rocs)) / 1.5)
    except Exception:
        return 0.5


def get_adaptive_scan_budget(entropy: float, base_limit: int = 20) -> int:
    """Entropy tinggi = market lebar/kacau -> scan lebih banyak coin.
    Entropy rendah = market tenang/fokus -> scan lebih sedikit."""
    lo, hi = max(10, int(base_limit * 0.6)), int(base_limit * 1.8)
    budget = int(lo + entropy * (hi - lo))
    return max(lo, min(hi, budget))


def learn_feature_weights_v2(window: int = 200) -> Dict[str, float]:
    """Pelajari feature importance dari closed trades. Karena tidak ada tabel `features`
    terpisah, dipakai kolom yang memang tersimpan di `signals` (final_score, decision_energy)
    sebagai proxy korelasi terhadap pnl. Fallback ke default kalau data belum cukup."""
    with _discovery_weights_lock:
        defaults = _discovery_weights.copy()
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''SELECT final_score, decision_energy, pnl FROM signals
                     WHERE evaluated=1 AND pnl IS NOT NULL
                     ORDER BY timestamp DESC LIMIT ?''', (window,))
        rows = c.fetchall()
        if len(rows) < 20:
            return defaults

        scores = [r[0] or 0 for r in rows]
        energies = [r[1] or 0 for r in rows]
        pnls = [r[2] or 0 for r in rows]

        corr_score = np.corrcoef(scores, pnls)[0, 1]
        corr_energy = np.corrcoef(energies, pnls)[0, 1]
        corr_score = corr_score if not np.isnan(corr_score) else 0.0
        corr_energy = corr_energy if not np.isnan(corr_energy) else 0.0

        # Mapping kasar: score ~ oi_change+volume signal quality, energy ~ acceleration/confidence
        oi_importance = max(0.1, abs(corr_score))
        acc_importance = max(0.1, abs(corr_energy))

        new_weights = defaults.copy()
        new_weights["oi_change"] = round(oi_importance * 0.7 + defaults["oi_change"] * 0.3, 3)
        new_weights["oi_acceleration"] = round(acc_importance * 0.7 + defaults["oi_acceleration"] * 0.3, 3)

        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        with _discovery_weights_lock:
            _discovery_weights.update(new_weights)
        return new_weights
    except Exception as e:
        logger.debug(f"learn_feature_weights_v2 error: {e}")
        return defaults
    finally:
        if conn:
            conn.close()


def compute_discovery_score_v2(coin: str, oi_change_pct: float, oi_acc_z: float,
                                volume_pct: float, corr_matrix: Dict, weights: Dict) -> Tuple[float, str]:
    """Discovery score fully data-driven: percentile + z-score + correlation + Bayesian prior.
    Tidak ada hardcode threshold absolut (mis. 'if oi_change > 10')."""
    acc_sigmoid = 1 / (1 + np.exp(-oi_acc_z))
    cluster = get_cluster_bonus(coin, corr_matrix)
    memory = get_coin_prior(coin)

    score = (
        oi_change_pct * weights.get("oi_change", 0.40)
        + acc_sigmoid * weights.get("oi_acceleration", 0.25)
        + volume_pct * weights.get("volume", 0.20)
        + cluster * weights.get("cluster", 0.05)
        + memory * weights.get("memory", 0.10)
    )
    reason = f"oi={oi_change_pct:.0%} acc={acc_sigmoid:.0%} vol={volume_pct:.0%} clust={cluster:.0%} mem={memory:.0%}"
    return max(0.0, min(1.0, score)), reason


def build_scan_universe_v2(min_vol: int = 5_000_000, base_limit: int = 20) -> List[str]:
    """Discovery Engine V2: ganti seleksi top-N volume statis dengan ranking
    data-driven (percentile + z-score + cluster + memory), budget adaptif by entropy.
    Fallback eksplisit ke hardcode list HANYA kalau exchange data benar-benar gagal."""
    try:
        meta = get_exchange_meta()
        if not meta:
            raise RuntimeError("get_exchange_meta returned None (cooldown active, no stale cache)")
    except Exception as e:
        logger.error(f"build_scan_universe_v2: meta fetch failed: {e}")
        return ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]

    candidates = []
    volumes = {}
    for asset, ctx in zip(meta[0]["universe"], meta[1]):
        coin = asset["name"]
        vol = float(ctx.get("dayNtlVlm", 0))
        if vol >= min_vol:
            candidates.append(coin)
            volumes[coin] = vol

    if not candidates:
        logger.warning("build_scan_universe_v2: no candidates above min_vol, using fallback")
        return ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]

    # 1. Feature extraction: OI change (raw) per coin
    oi_changes = {coin: get_oi_roc(coin, window_minutes=60) for coin in candidates}

    # 2. Cross-sectional ranking (percentile, bukan angka mentah)
    oi_rank_map = percentile_rank(list(oi_changes.values()))
    vol_rank_map = percentile_rank(list(volumes.values()))

    # 3. Z-score acceleration dari oi history masing-masing coin
    oi_acc_z = {}
    with _oi_lock:
        for coin in candidates:
            hist = list(_oi_history.get(coin, deque()))
            if len(hist) >= 4:
                vals = [v for _, v in hist]
                oi_acc_z[coin] = rolling_zscore(vals[-1], vals[-4:-1])
            else:
                oi_acc_z[coin] = 0.0

    # 4. Correlation matrix (tanpa hardcode cluster)
    corr_matrix = build_oi_correlation_matrix(candidates, lookback=12)

    # 5. Adaptive weights (belajar dari closed trades, fallback ke default)
    weights = learn_feature_weights_v2()

    # 6. Discovery score per coin
    scored = []
    for coin in candidates:
        oi_pct = oi_rank_map.get(oi_changes[coin], 0.5)
        vol_pct = vol_rank_map.get(volumes[coin], 0.5)
        score, reason = compute_discovery_score_v2(
            coin, oi_pct, oi_acc_z.get(coin, 0.0), vol_pct, corr_matrix, weights
        )
        scored.append((coin, score, reason))

    scored.sort(key=lambda x: x[1], reverse=True)

    # 7. Adaptive budget berdasarkan entropy market (bukan limit fixed)
    entropy = get_market_entropy_for_discovery()
    top_k = get_adaptive_scan_budget(entropy, base_limit=base_limit)

    for coin, score, reason in scored[:10]:
        logger.debug(f"🔍 DISCOVERY {coin}: {score:.0%} | {reason}")

    result = [c[0] for c in scored[:top_k]]
    logger.info(f"Discovery V2: {len(result)} coins selected (entropy={entropy:.2f}, budget={top_k}, weights={weights})")
    return result


# ============================================================
# V11 — API SCHEDULER (per-endpoint, sliding window, BUKAN global brake)
# ============================================================
# Layering: can_call_api() lama tetap jalan sebagai emergency brake saat 429.
# Ini layer proaktif supaya gak SAMPAI kena 429 di tempat pertama.

_api_window: List[Tuple[str, float]] = []
_api_window_lock = threading.RLock()
API_BUDGET_PER_CYCLE = 20
API_COOLDOWN = {"candles": 2.0, "snapshot": 1.0, "l2": 1.0, "trades": 1.0, "meta": 5.0}


def can_call_api_endpoint(endpoint: str) -> bool:
    """Cek apakah endpoint boleh dipanggil: per-endpoint cooldown + sliding-window budget."""
    with _api_window_lock:
        now = time.time()
        # Cooldown: cek panggilan terakhir untuk endpoint ini
        cooldown = API_COOLDOWN.get(endpoint, 2.0)
        for ep, ts in reversed(_api_window):
            if ep == endpoint:
                if now - ts < cooldown:
                    return False
                break
        # Budget: sliding window 60 detik, bukan cuma 1 timestamp per endpoint
        while _api_window and now - _api_window[0][1] > 60:
            _api_window.pop(0)
        return len(_api_window) < API_BUDGET_PER_CYCLE


def mark_api_call(endpoint: str):
    """Catat API call ke sliding window (bukan overwrite 1 timestamp)."""
    with _api_window_lock:
        now = time.time()
        _api_window.append((endpoint, now))
        while _api_window and now - _api_window[0][1] > 60:
            _api_window.pop(0)


def get_api_used() -> int:
    with _api_window_lock:
        now = time.time()
        return sum(1 for _, ts in _api_window if now - ts < 60)


def get_seconds_until_budget_frees() -> float:
    """Hitung berapa detik sampai entry TERTUA di window keluar dari range 60s
    (artinya budget akan turun 1 slot). Dipakai sebagai wait_time yang tepat
    sasaran, ketimbang tebakan flat 0.5s yang nyaris gak ngaruh untuk window
    sebesar 60 detik (0.5/60 = <1% pergeseran window per percobaan)."""
    with _api_window_lock:
        if not _api_window:
            return 0.0
        oldest_ts = _api_window[0][1]
        remaining = 60 - (time.time() - oldest_ts)
        return max(0.1, min(5.0, remaining))  # cap supaya gak nunggu kelamaan kalau window penuh banget


# ============================================================
# V11 — NARRATIVE MAP + SECTOR EXPLORATION (anti feedback-loop)
# ============================================================

_NARRATIVE_MAP = {}  # legacy alias — pakai get_live_narrative_map() instead

_sector_history: Dict[str, int] = {}
_sector_history_lock = threading.RLock()


def get_top_narrative() -> Tuple[str, float]:
    """Cari sektor dengan rata-rata OI change tertinggi — pakai live map, bukan hardcode."""
    snapshot = get_snapshot()
    if not snapshot:
        return "UNKNOWN", 0.0

    # Pakai live-validated map (sudah disanitize dari snapshot)
    narrative_map = get_live_narrative_map()
    if not narrative_map:
        return "UNKNOWN", 0.0

    sector_scores = {}
    for sector, coins in narrative_map.items():
        valid_coins = [c for c in coins if c in snapshot.mids]
        if not valid_coins:
            continue
        oi_changes = [get_oi_roc(c, window_minutes=60) for c in valid_coins]
        # Coverage = rasio coin live vs total di sektor (penalize sektor yang banyak dead coins)
        coverage = len(valid_coins) / len(coins)
        sector_scores[sector] = (sum(oi_changes) / len(oi_changes)) * 0.7 + coverage * 30

    if not sector_scores:
        return "UNKNOWN", 0.0
    return max(sector_scores.items(), key=lambda x: x[1])


def get_sector_decay(sector: str) -> float:
    """Decay sektor yang berulang kali kepilih, biar gak lock satu narrative selamanya."""
    with _sector_history_lock:
        n = _sector_history.get(sector, 0)
        return math.exp(-0.3 * n)


def get_top_narrative_with_exploration() -> Tuple[str, float]:
    """25% chance explore sektor random (anti feedback-loop)."""
    narrative_map = get_live_narrative_map()
    live_sectors = list(narrative_map.keys())

    if not live_sectors:
        return "UNKNOWN", 0.0

    if random.random() < 0.25:
        sector = random.choice(live_sectors)
        with _sector_history_lock:
            _sector_history[sector] = _sector_history.get(sector, 0) + 1
        return sector, 50.0

    sector, score = get_top_narrative()
    if sector != "UNKNOWN":
        decay = get_sector_decay(sector)
        score *= decay
        with _sector_history_lock:
            _sector_history[sector] = _sector_history.get(sector, 0) + 1
    return sector, score


# ============================================================
# V11 — STAGE A: CHEAP DISCOVERY (candidate pool, no candles)
# ============================================================

_candidate_history: Dict[str, int] = {}
_candidate_history_lock = threading.RLock()


def get_coin_selection_count(coin: str) -> int:
    with _candidate_history_lock:
        return _candidate_history.get(coin, 0)


def apply_memory_decay(coin: str, base_score: float) -> float:
    """Decay berbasis berapa kali coin ini MASUK candidate pool (bukan executed —
    versi `executed` bias survivorship: coin yang sering lolos sampai eksekusi dihukum,
    coin yang gagal di tahap awal terus-menerus tidak pernah kena decay)."""
    n = get_coin_selection_count(coin)
    return base_score * math.exp(-0.2 * n)


def get_alpha_coins(snapshot: MarketSnapshot, limit: int = 4) -> List[str]:
    """Alpha bucket: coin besar yang memang listing (cek snapshot, jangan asumsi)."""
    alpha_pool = ["BTC", "ETH", "SOL", "HYPE", "XRP"]
    return [c for c in alpha_pool if c in snapshot.mids][:limit]


def get_oi_flow_coins(snapshot: MarketSnapshot, limit: int = 6) -> List[str]:
    """OI Flow bucket: top N coin berdasar discovery score (pakai komponen Discovery V2
    yang sudah ada: percentile + z-score acceleration + cluster correlation)."""
    # snapshot.oi unit = juta USD → 1.0 = $1 juta minimum (bukan 1_000_000 = $1 quadrillion)
    candidates = [c for c, oi_usd in snapshot.oi.items() if oi_usd >= 1.0]
    if not candidates:
        return []

    oi_changes = {c: get_oi_roc(c, window_minutes=60) for c in candidates}
    oi_rank_map = percentile_rank(list(oi_changes.values()))

    oi_acc_z = {}
    with _oi_lock:
        for c in candidates:
            hist = list(_oi_history.get(c, deque()))
            if len(hist) >= 4:
                vals = [v for _, v in hist]
                oi_acc_z[c] = rolling_zscore(vals[-1], vals[-4:-1])
            else:
                oi_acc_z[c] = 0.0

    corr_matrix = build_oi_correlation_matrix(candidates[:60], lookback=12)

    scored = []
    for c in candidates:
        oi_pct = oi_rank_map.get(oi_changes[c], 0.5)
        cluster = get_cluster_bonus(c, corr_matrix)
        acc_sigmoid = 1 / (1 + np.exp(-oi_acc_z.get(c, 0.0)))
        score = oi_pct * 0.5 + acc_sigmoid * 0.3 + cluster * 0.2
        score = apply_memory_decay(c, score)
        scored.append((c, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in scored[:limit]]


def get_narrative_coins(snapshot: MarketSnapshot, limit: int = 3) -> List[str]:
    """Narrative bucket: coin dari sektor terpanas — pakai live map."""
    sector, score = get_top_narrative_with_exploration()
    if sector == "UNKNOWN" or score < 5:
        return []
    narrative_map = get_live_narrative_map()
    coins = [c for c in narrative_map.get(sector, []) if c in snapshot.mids]
    if not coins:
        return []
    scored = [(c, get_oi_roc(c, window_minutes=60)) for c in coins]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in scored[:limit]]
    
def build_candidate_pool_v11_final(max_candidates: int = 12) -> List[str]:
    """
    Discovery V11 Final: Capital Rotation Detector (Production-Ready)
    """
    try:
        snapshot = get_snapshot()
        if not snapshot or not snapshot.mids:
            return ["BTC", "ETH", "SOL"]

        scores: Dict[str, float] = {}
        pattern_log: Dict[str, Tuple[str, float, float, float]] = {}

        # ===== GATE TELEMETRY =====
        _reject_oi_min = 0
        _reject_gate = 0
        _reject_late = 0
        _reject_neutral = 0
        _total_scanned = 0

        _DISCOVERY_OI_WINDOW = 60

        for coin in list(snapshot.mids.keys()):
            _total_scanned += 1
            oi_usd = snapshot.oi.get(coin, 0)

            # ===== GATE: OI minimum =====
            if oi_usd < 0.25:
                _reject_oi_min += 1
                continue

            # OI ROC dengan window 60m untuk discovery
            oi_growth = get_oi_roc(coin, window_minutes=_DISCOVERY_OI_WINDOW)

            # ===== DISLOCATION: PAKAI DICT =====
            dislocation_data = get_dislocation_score_v11(coin, snapshot)
            dislocation = dislocation_data["value"]
            dis_confidence = dislocation_data["confidence"]

            # Gate scoring: dislocation cuma dipake kalau confidence > 0.3
            if dis_confidence > 0.3:
                gate_score = oi_growth + max(0, dislocation * 0.5 * dis_confidence)
            else:
                gate_score = oi_growth  # Data belum cukup, abaikan dislocation

            # Sample log: 2% chance
            if random.random() < 0.02:
                logger.info(
                    f"GATE {coin} oi={oi_usd/1e6:.2f}M "
                    f"growth={oi_growth:+.2f}% dis={dislocation:+.2f} "
                    f"conf={dis_confidence:.2f} gate={gate_score:.2f}"
                )

            # EPS-based warmup check
            EPS = 0.15
            is_warmup_data = (
                abs(oi_growth) < EPS
                and abs(dislocation) < EPS
                and dis_confidence < 0.5
            )

            # Gate threshold
            uptime_secs = time.time() - START_TIME
            if uptime_secs < 3600:
                min_gate = 0.0
            else:
                min_gate = 0.15

            # Reject cuma kalau JELAS negatif
            if not is_warmup_data and gate_score < min_gate:
                if dislocation > 2.0:
                    pass
                else:
                    _reject_gate += 1
                    logger.debug(
                        f"  GATE SKIP {coin}: oi={oi_usd/1e6:.1f}M "
                        f"growth_60m={oi_growth:.2f} dis={dislocation:.2f} "
                        f"gate={gate_score:.2f} min={min_gate:.2f}"
                    )
                    continue

            # Pattern
            pattern, oi_4h_growth, coverage = get_oi_pattern_v11(coin)

            if pattern == "LATE":
                _reject_late += 1
                continue

            if pattern == "NEUTRAL" and oi_growth < 1.0 and dislocation < 2.0:
                _reject_neutral += 1
                continue

            # ===== BASE SCORE =====
            if pattern == "EARLY":
                base_score = 70 + min(30, max(0, oi_growth * 5))
            elif pattern == "MOMENTUM":
                base_score = 50 + min(25, max(0, oi_growth * 4))
            elif pattern == "SPIKE":
                base_score = 35 + min(15, max(0, oi_growth * 3))
            elif pattern == "WARMUP":
                base_score = 10 + (15 * coverage)
            else:
                base_score = max(10, min(30, oi_growth * 5))

            # Coverage adjustment
            if pattern != "WARMUP":
                base_score *= (0.4 + 0.6 * coverage)

            # Dislocation bonus/penalty (cuma kalau confidence cukup)
            if dis_confidence > 0.3:
                if dislocation > 1.0:
                    base_score += min(15, dislocation * 3 * dis_confidence)
                elif dislocation < -3.0:
                    base_score -= min(10, abs(dislocation) * dis_confidence)

            # Memory decay
            n = get_coin_selection_count(coin)
            base_score *= math.exp(-0.15 * n)

            scores[coin] = max(0, base_score)
            pattern_log[coin] = (pattern, oi_growth, coverage, dislocation)

        # ===== GATE TELEMETRY LOG =====
        _pass = len(scores)
        logger.info(
            f"GATE DEBUG total={_total_scanned} "
            f"oi_min={_reject_oi_min} gate={_reject_gate} "
            f"late={_reject_late} neutral={_reject_neutral} "
            f"pass={_pass}"
        )

        if not scores:
            logger.warning("Discovery V11: no coins passed gates, using fallback")
            return ["BTC", "ETH", "SOL"]

        # ===== NARRATIVE BOOST =====
        all_coins = list(scores.keys())
        for coin, base_score in list(scores.items()):
            boost = get_narrative_boost_v11_direct(coin, all_coins)
            scores[coin] = base_score * (1 + boost)

        # ===== FINAL SORT =====
        final_sorted = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        candidates = [c for c, _ in final_sorted[:max_candidates]]

        if "BTC" not in candidates:
            candidates.insert(0, "BTC")
            candidates = candidates[:max_candidates]

        # Record selection
        with _candidate_history_lock:
            for coin in candidates:
                _candidate_history[coin] = _candidate_history.get(coin, 0) + 1

        # ===== LOGGING =====
        warmup = sum(1 for c in candidates if pattern_log.get(c, ("?", 0, 0, 0))[0] == "WARMUP")
        early = sum(1 for c in candidates if pattern_log.get(c, ("?", 0, 0, 0))[0] == "EARLY")
        momentum = sum(1 for c in candidates if pattern_log.get(c, ("?", 0, 0, 0))[0] == "MOMENTUM")
        spike = sum(1 for c in candidates if pattern_log.get(c, ("?", 0, 0, 0))[0] == "SPIKE")

        logger.info(f"🔍 DISCOVERY | candidates={len(candidates)} warmup={warmup} early={early} momentum={momentum} spike={spike}")

        for coin, score in final_sorted[:8]:
            pattern, growth, coverage, dis = pattern_log.get(coin, ("?", 0, 0, 0))
            logger.debug(f"  {coin}: {score:.0f} | {pattern} oi_growth={growth:+.1f}% dis={dis:+.1f} cov={coverage:.2f}")

        return candidates

    except Exception as e:
        logger.error(f"build_candidate_pool_v11_final error: {e}")
        return ["BTC", "ETH", "SOL"]

def build_candidate_pool(max_candidates: int = 12) -> List[str]:
    """Wrapper for Discovery V11 Final."""
    return build_candidate_pool_v11_final(max_candidates)

def process_candidates_deep(candidates: List[str], snapshot: MarketSnapshot) -> Tuple[List[dict], int]:
    """Stage B: deep analysis untuk kandidat terpilih.
    [DEBUG: Add comprehensive logging]
    """
    results = []
    scan_count = 0
    last_snapshot_refresh = time.time()
    SNAPSHOT_REFRESH_INTERVAL = 30  # refresh kalau snapshot udah >30s dipakai

    logger.info(f"""
╔════════════════════════════════════════════╗
║ DEEP_START
║ candidates={len(candidates)}
║ api_used={get_api_used()}/{API_BUDGET_PER_CYCLE}
║ snapshot_mids={len(snapshot.mids) if snapshot else 0}
╚════════════════════════════════════════════╝
""")

    for i, coin in enumerate(candidates):
        logger.info(f"┌─ STEP_{i}: coin={coin}")

        # ===== P5: STALE SNAPSHOT REFRESH =====
        # Loop ini bisa kena time.sleep() dari budget/cooldown wait di bawah,
        # jadi snapshot yang diambil sekali di awal bisa stale untuk coin
        # yang discan belakangan. Refresh tiap 30s, fallback ke snapshot lama
        # kalau refresh gagal (jangan skip coin gara-gara refresh doang).
        if time.time() - last_snapshot_refresh > SNAPSHOT_REFRESH_INTERVAL:
            fresh_snapshot = refresh_snapshot()
            last_snapshot_refresh = time.time()
            if fresh_snapshot:
                snapshot = fresh_snapshot
                logger.debug(f"   🔄 snapshot refreshed (age>{SNAPSHOT_REFRESH_INTERVAL}s)")
            else:
                logger.warning(f"   ⚠️ snapshot refresh failed, continuing with stale snapshot")
        # =======================================

        # Check 1: API Budget
        budget_val = get_api_used()
        logger.info(f"   budget={budget_val}/{API_BUDGET_PER_CYCLE}")
        if budget_val >= API_BUDGET_PER_CYCLE:
            wait_time = get_seconds_until_budget_frees()
            logger.warning(f"   BUDGET_HIT waiting={wait_time:.1f}s")
            time.sleep(wait_time)
            if get_api_used() >= API_BUDGET_PER_CYCLE:
                logger.warning(f"   SKIP_{coin}_budget_exhausted")
                continue

        # Check 2: Mark price in snapshot
        mark = snapshot.mids.get(coin, 0) if snapshot else 0
        logger.info(f"   mark={mark}")
        if mark == 0:
            logger.warning(f"   SKIP_{coin}_mark_zero")
            continue

        # Check 3: Global API cooldown
        api_ok = can_call_api()
        logger.info(f"   api_cooldown={api_ok}")
        if not api_ok:
            wait_time = min(2.0, 0.5 + (API_COOLDOWN.get("candles", 2.0) * 0.5))
            logger.debug(f"   waiting={wait_time:.1f}s")
            time.sleep(wait_time)
            if not can_call_api():
                logger.warning(f"   SKIP_{coin}_api_cooldown")
                continue

        # Check 4: Endpoint available [REMOVED per-endpoint cooldown]
        # OLD: endpoint_ok = can_call_api_endpoint("candles")
        # REASON: Per-endpoint 2s cooldown gates fast scanning
        #         Kills ETH/SOL when arriving 0.1-0.2s after BTC
        #         Budget (20/60s) is sufficient throttle

        # Check 5: Get candles
        candles_1h = get_candles(coin, "1h", 100)
        candles_len = len(candles_1h) if candles_1h else 0
        logger.info(f"   candles={candles_len}")
        mark_api_call("candles")
        # ✅ API call tracked in sliding window (budget throttle)
        # ✅ No per-endpoint cooldown gating (fast scanning enabled)
        
        if not candles_1h:
            logger.warning(f"   SKIP_{coin}_no_candles")
            continue

        # === SCAN SUCCESS ===
        scan_count += 1
        logger.info(f"   SCAN_OK")
        
        master_candles = {coin: candles_1h}
        alert = check_entry_alert_v10_phase1(coin, mark, master_candles)
        
        if alert:
            results.append(alert)
            logger.info(f"   ALERT_YES")
        else:
            logger.info(f"   ALERT_NO")
        
        logger.info(f"└─ STEP_{i}_done")
        time.sleep(0.1)

    logger.info(f"""
╔════════════════════════════════════════════╗
║ DEEP_END
║ results={len(results)}
║ scanned={scan_count}/{len(candidates)}
║ api_used={get_api_used()}
╚════════════════════════════════════════════╝
""")
    return results, scan_count


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
        """Get top coins by 24h volume dynamically from exchange (fallback path)."""
        try:
            meta = get_exchange_meta()
            if not meta:
                return None
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
    
    # ===== TOP COINS (DISCOVERY ENGINE V2: data-driven, bukan top-N volume mentah) =====
    try:
        top_coins = build_scan_universe_v2(min_vol=5_000_000, base_limit=20)
    except Exception as e:
        logger.error(f"Discovery V2 failed, fallback to raw volume: {e}")
        top_coins = None
    if not top_coins:
        top_coins = get_top_coins_by_volume(limit=20, min_vol=5_000_000)
    if not top_coins:
        logger.warning("Using fallback top coins list (hardcoded)")
        top_coins = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "POL", "LINK", "UNI", "AAVE", "ZEC", "HYPE"]
    
    
    # ===== BATCH SCAN (PATCH 4) =====
    BATCH_SIZE = 3
    BATCH_WAIT = 5  # detik antar batch
    
    master_candles = fetch_candles_master(top_coins, "1h", 100)
    alerts = []
    scan_count = 0
    exec_count = 0

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

            scan_count += 1
            alert = check_entry_alert_v10_phase1(coin, mark, master_candles)
            if alert:
                exec_count += 1
                if not PAPER_MODE:
                    alerts.append(alert)
                else:
                    logger.info(f"[PAPER] {alert['coin']} {alert['direction']} score={alert['score']}")
            time.sleep(0.1)  # 100ms antar coin

        # WAIT ANTAR BATCH
        if i + BATCH_SIZE < len(top_coins):
            logger.debug(f"⏳ Waiting {BATCH_WAIT}s before next batch...")
            time.sleep(BATCH_WAIT)

    pipe = get_pipeline_metrics()
    logger.info(
        f"📊 FUNNEL: scan={scan_count} events={pipe.get('obs',0)} "
        f"thesis={pipe.get('thesis',0)} conf={pipe.get('confidence',0)} exec={exec_count} "
        f"| DCR={pipe.get('dcr','?')} funnel={pipe.get('funnel_issue','?')}"
    )
    
    # ===== PIPELINE ONE-LINE SUMMARY =====
    if alerts:
        avg_score = sum(a.get('score', 0) for a in alerts) / len(alerts)
        avg_rr = sum(a.get('rr', 0) for a in alerts) / len(alerts)
    else:
        avg_score = avg_rr = 0.0
    
    logger.info(
        f"📊 PIPELINE | scan:{pipe['check']} OBS:{pipe['obs']} TH:{pipe['thesis']} CF:{pipe['confidence']} EX:{pipe['execute_pass']} "
        f"| avg_score:{avg_score:.1f} avg_rr:{avg_rr:.2f} | reject:{pipe['total_reject']}"
    )
    
    # ===== FLUSH CYCLE LOGS =====
    flush_cycle_logs()
    
    for alert in alerts:
        send_alert_v10(alert)
        
        # ===== P1 FIX: REGISTER POSITION TO TRADE MANAGER =====
        try:
            if alert.get("tp_scaled"):
                _sig_id = alert.get("signal_id")
                if not _sig_id:
                    logger.error(f"🔴 ORPHAN PREVENTION: missing signal_id for {alert['coin']}, skip TradeManager register")
                else:
                    TRADE_MANAGER.add_position(
                        signal_id=_sig_id,
                        coin=alert["coin"],
                        direction=alert["direction"],
                        entry=alert["entry"],
                        sl=alert["sl"],
                        tp_targets=alert["tp_scaled"],
                        entry_time=time.time()
                    )
        except Exception as e:
            logger.warning(f"P1: Failed to register {alert['coin']}: {e}") 
    # ===== P1 FIX: CHECK ALL OPEN POSITIONS PERIODICALLY =====
    try:
        snapshot = get_snapshot()
        closed_trades = TRADE_MANAGER.check_all_positions(snapshot)
    
        for trade in closed_trades:
            try:
                # ===== STEP 4A: SAFE LOOKUP DENGAN getattr() =====
                with _journal_lock:
                    for entry in _decision_journal:
                        entry_sig = getattr(entry, "signal_id", None)  # ← AMAN!
                        if entry_sig and entry_sig == trade["signal_id"]:
                            entry.outcome = "TP_HIT" if trade["pnl"] > 0 else "SL_HIT"
                            entry.pnl = trade["pnl"]
                            entry.mfe = trade["mfe"]
                            entry.mae = trade["mae"]
                            entry.closed = getattr(entry, "closed", True)
                            entry.close_reason = getattr(entry, "close_reason", trade["reason"])
                            entry.duration_minutes = getattr(entry, "duration_minutes", trade.get("duration_minutes", 0))
                            break
                            
                            # ===== TERMINAL LOG =====
                print(f"📊 CLOSE {trade['coin']} {trade['direction']} | {trade['reason']} | PnL: {trade['pnl']:+.2f}%")
                logger.info(f"✅ P1: Trade closed {trade['coin']} | {trade['reason']} | PnL: {trade['pnl']:+.2f}%")
                # Send Telegram alert
                if USER_ID and not PAPER_MODE:
                    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
                    direction_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
                    msg = f"{emoji} <b>CLOSE</b> {trade['coin']} [{direction_emoji} {trade['direction']}]\n"
                    msg += f"├─ Reason: {trade['reason']}\n"
                    msg += f"├─ PnL: {trade['pnl']:+.2f}%\n"
                    msg += f"├─ MFE: {trade['mfe']:+.2f}% | MAE: {trade['mae']:+.2f}%\n"
                    msg += f"└─ Time: {trade['duration_minutes']:.0f}m | TP Levels: {trade['tp_levels_captured']}/3"
                    try:
                        bot.send_message(USER_ID, msg, parse_mode='HTML')
                    except Exception as close_notif_err:
                        logger.error(f"P1: CLOSE notif send failed for {trade['coin']}: {close_notif_err}")
            except Exception as e:
                logger.error(f"P1: Error processing closed trade {trade.get('signal_id', 'unknown')}: {e}")
    except Exception as e:
        logger.warning(f"P1: Position check error: {e}")

def state_engine_update_v11():
    """State Engine V11: 2-stage architecture.
    Stage A (cheap discovery, no candles) -> Stage B (deep analysis, API budget).
    Menggantikan scan top-N-volume statis dari V10 dengan 3-bucket candidate pool
    (alpha + OI flow + narrative) plus per-endpoint API budget supaya gak kena 429."""
    context = get_context_snapshot("BTC")
    snap = refresh_snapshot()
    compute_market_breath_v10()

    # Sanitize sector/narrative maps dari live snapshot (throttled 5 menit)
    if snap:
        sanitize_maps_from_snapshot(snap)

    with _oi_lock:
        oi_hist_coins = len(_oi_history)
        oi_hist_btc = len(_oi_history.get("BTC", []))
    logger.debug(f"OI HIST | coins={oi_hist_coins} BTC={oi_hist_btc}")

    # Reset API budget window tiap cycle
    with _api_window_lock:
        _api_window.clear()

    # ===== REACTION ENGINE (sama seperti V10) =====
    with _event_risk_lock:
        if _EVENT_RISK_DATA:
            latest_event = max(_EVENT_RISK_DATA, key=lambda e: e.ts)
            if time.time() - latest_event.ts < TUNABLE["EVENT_RISK_DECAY_HOURS"] * 3600:
                snap_check = get_snapshot()
                if snap_check and "BTC" in snap_check.mids:
                    with _last_mids_lock:
                        if "BTC" in _last_mids:
                            prev_price, prev_ts = _last_mids["BTC"]
                            current_price = snap_check.mids["BTC"]
                            if prev_price > 0 and prev_ts > time.time() - 300:
                                btc_move = (current_price - prev_price) / prev_price * 100
                                vol_spike = get_volume_spike("BTC")
                                reaction = compute_reaction(latest_event, btc_move, vol_spike)
                                update_reaction_history(reaction)

    # ===== STAGE A: CHEAP DISCOVERY (no candles) =====
    candidates = build_candidate_pool(max_candidates=12)
    if not candidates:
        logger.warning("V11: No candidates from discovery, using fallback")
        candidates = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "POL", "LINK", "UNI", "AAVE", "ZEC", "HYPE", "TAO"]

    # ===== STAGE B: DEEP ANALYSIS (candles + budget) =====
    snapshot = get_snapshot()
    alerts, scan_count = process_candidates_deep(candidates, snapshot)

    pipe = get_pipeline_metrics()
    logger.info(
        f"📊 V11 FUNNEL: candidates={len(candidates)} scanned={scan_count} events={pipe.get('obs',0)} "
        f"thesis={pipe.get('thesis',0)} conf={pipe.get('confidence',0)} exec={len(alerts)} "
        f"api_used={get_api_used()} | DCR={pipe.get('dcr','?')} funnel={pipe.get('funnel_issue','?')}"
    )

    # ===== FLUSH CYCLE LOGS =====
    flush_cycle_logs()

    # ===== SEND ALERTS + REGISTER (sama seperti V10) =====
    for alert in alerts:
        send_alert_v10(alert)
        try:
            if alert.get("tp_scaled"):
                _sig_id = alert.get("signal_id")
                if not _sig_id:
                    logger.error(f"🔴 ORPHAN PREVENTION: missing signal_id for {alert['coin']}, skip TradeManager register")
                else:
                    TRADE_MANAGER.add_position(
                        signal_id=_sig_id,
                        coin=alert["coin"],
                        direction=alert["direction"],
                        entry=alert["entry"],
                        sl=alert["sl"],
                        tp_targets=alert["tp_scaled"],
                        entry_time=time.time()
                    )
        except Exception as e:
            logger.warning(f"V11: Failed to register {alert['coin']}: {e}")
    # ===== CHECK OPEN POSITIONS (sama seperti V10) =====
    try:
        snap_for_check = get_snapshot()
        closed_trades = TRADE_MANAGER.check_all_positions(snap_for_check)
        for trade in closed_trades:
            try:
            # ===== STEP 4B: SAFE LOOKUP DENGAN getattr() =====
                with _journal_lock:
                    for entry in _decision_journal:
                        entry_sig = getattr(entry, "signal_id", None)  # ← AMAN!
                        if entry_sig and entry_sig == trade["signal_id"]:
                            entry.outcome = "TP_HIT" if trade["pnl"] > 0 else "SL_HIT"
                            entry.pnl = trade["pnl"]
                            entry.mfe = trade["mfe"]
                            entry.mae = trade["mae"]
                            entry.closed = getattr(entry, "closed", True)
                            entry.close_reason = getattr(entry, "close_reason", trade["reason"])
                            entry.duration_minutes = getattr(entry, "duration_minutes", trade.get("duration_minutes", 0))
                            break
                # ===== TERMINAL LOG =====
                print(f"📊 CLOSE {trade['coin']} {trade['direction']} | {trade['reason']} | PnL: {trade['pnl']:+.2f}%")
                logger.info(f"✅ V11: Trade closed {trade['coin']} | {trade['reason']} | PnL: {trade['pnl']:+.2f}%")
                
                if USER_ID and not PAPER_MODE:
                    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
                    direction_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
                    msg = f"{emoji} <b>CLOSE</b> {trade['coin']} [{direction_emoji} {trade['direction']}]\n"
                    msg += f"├─ Reason: {trade['reason']}\n"
                    msg += f"├─ PnL: {trade['pnl']:+.2f}%\n"
                    msg += f"├─ MFE: {trade['mfe']:+.2f}% | MAE: {trade['mae']:+.2f}%\n"
                    msg += f"└─ Time: {trade['duration_minutes']:.0f}m | TP Levels: {trade['tp_levels_captured']}/3"
                    try:
                        bot.send_message(USER_ID, msg, parse_mode='HTML')
                    except Exception as close_notif_err:
                        logger.error(f"V11: CLOSE notif send failed for {trade['coin']}: {close_notif_err}")
            except Exception as e:
                logger.error(f"V11: Error processing closed trade {trade.get('signal_id', 'unknown')}: {e}")
    except Exception as e:
        logger.warning(f"V11: Position check error: {e}")
        
def trigger_engine_update_v7():
    refresh_snapshot()
    all_top = None
    try:
        meta = get_exchange_meta()
        if not meta:
            raise RuntimeError("get_exchange_meta returned None (cooldown active, no stale cache)")
        coins_vol = []
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            vol = float(ctx.get("dayNtlVlm", 0))
            if vol > 5_000_000:
                coins_vol.append((asset["name"], vol))
        coins_vol.sort(key=lambda x: x[1], reverse=True)
        all_top = [c[0] for c in coins_vol[:20]]
    except Exception as e:
        logger.warning(f"Using fallback top coins list in trigger_engine_update_v7: {type(e).__name__}: {e}")
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

def scheduled_state_engine_v11():
    while RUNTIME.is_running():
        interval = TUNABLE.get("STATE_ENGINE_INTERVAL", 30)
        try:
            if not RUNTIME.is_alert_enabled():
                RUNTIME.wait(60)
                continue
            
            # === P0 LIFECYCLE: AUDIT TRADE STATE ===
            audit_result = audit_trade_state()

            # === P0 LIFECYCLE: STALE CLEANUP TIAP CYCLE (aman, sudah ada 48h age-guard internal) ===
            # Sebelumnya cuma trigger kalau orphan_count > 100, jadi reactive banget.
            # emergency_lifecycle_cleanup() sendiri udah filter age>48h, jadi aman dijalankan tiap cycle.
            cleaned = emergency_lifecycle_cleanup()
            if cleaned > 0:
                logger.warning(f"🔄 Cleaned {cleaned} stale trades (age>48h)")
            if audit_result["orphan_count"] > 100:
                logger.warning(f"🔴 CRITICAL: {audit_result['orphan_count']} orphans still present after cleanup")

            state_engine_update_v11()
            vol_reg = get_volatility_regime()
            if vol_reg == "HIGH_VOLATILITY":
                interval = max(15, interval // 2)
            elif vol_reg == "LOW_VOLATILITY":
                interval = min(60, interval * 2)
            logger.info(f"State engine V11 cycle done, next in {interval}s")
        except Exception:
            logger.exception("STATE_ENGINE_CRASH — cycle skipped, thread alive")
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
        for attempt in range(3):
            try:
                end_ms = int(time.time() * 1000)
                tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
                interval = tf_ms.get(timeframe, 3600000)
                start_ms = end_ms - limit * interval
                candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms)
                return coin, (candles if candles else [])
            except Exception as e:
                if "429" in str(e) or "rate limit" in str(e).lower():
                    delay = 5 * (2 ** attempt)
                    logger.debug(f"Rate limit on {coin} candles, retry {attempt+1}/3 in {delay}s")
                    time.sleep(delay)
                    continue
                logger.error(f"Fetch {coin} {timeframe}: {e}")
                return coin, []
        logger.warning(f"Fetch {coin} {timeframe}: gave up after 3 attempts (rate limit)")
        return coin, []

    BATCH_SIZE = 3
    BATCH_WAIT = 5  # detik antar batch

    results = {}
    for i in range(0, len(coins), BATCH_SIZE):
        batch = coins[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=min(BATCH_SIZE, 4)) as ex:
            futures = [ex.submit(fetch_one, c) for c in batch]
            for f in futures:
                coin, candles = f.result()
                if candles:
                    results[coin] = candles
        if i + BATCH_SIZE < len(coins):
            time.sleep(BATCH_WAIT)
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

def _get_alert_level(alert: dict) -> int:
    """Classify alert into 3 levels: 0=silent, 1=compact, 2=full detail"""
    score = alert.get("score", 0)
    rr = alert.get("rr", 0)
    value = alert.get("value_score", 0)
    commitment = alert.get("commitment_score", 0)
    e_market = alert.get("entropy_market", 100)

    # Level 2 (Priority) – top tier
    if (score >= 75 and rr >= 2.0 and commitment >= 60 and e_market <= 20):
        return 2

    # Level 1 (Default) – normal entry
    if score >= 45 and rr >= 1.5 and value >= 50 and commitment >= 30:
        return 1

    # Level 0 (Silent) – below bar
    return 0


def _build_compact_alert(alert: dict) -> str:
    """Build compact 6-8 line summary for Level 1 & 2"""
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    direction_emoji = arrow
    score = alert["score"]
    rr = alert.get("rr", 0)
    size = alert.get("position_size_mult", 1.0)
    label = alert.get("value_label", "")
    entry = fmt_price(alert["entry"])
    sl = fmt_price(alert["sl"])
    tp = fmt_price(alert["tp"])
    
    # Top 2 reasons
    reasons = ", ".join(alert.get("positive_evidence", [])[:2])
    neg_list = alert.get("negative_evidence", "").split(",") if alert.get("negative_evidence") else []
    neg = ", ".join([x.strip() for x in neg_list[:2] if x.strip()]) if neg_list else "none"
    
    # Rank info
    rank_text = alert.get("rank", "No rank")
    
    # Decision stability (inverted entropy)
    decision_stability = 100 - alert.get("entropy_decision", 0)
    
    compact = (
        f"{direction_emoji} <b>{alert['coin']} {alert['direction']}</b>\n"
        f"├─ Score: {score} {label} | RR: 1:{rr:.1f}\n"
        f"├─ Entry {entry} | SL {sl} | TP {tp}\n"
        f"├─ Why: +{reasons} | –{neg}\n"
        f"├─ Rank: {rank_text} | Stability: {decision_stability}%\n"
        f"└─ Size: {size:.1f}x | /entry {alert['coin']}"
    )
    return compact


def send_alert_v10(alert: dict):
    if not RUNTIME.is_alert_enabled():
        return

    # alert yang punya tp_scaled artinya udah lolos execute_decision() dan
    # POSISI SUDAH BENAR-BENAR TERBUKA di TradeManager — beda dengan alert
    # discovery/thesis-trigger yang cuma notifikasi minat, bukan posisi real.
    is_real_position = bool(alert.get("tp_scaled"))

    value, label = compute_alert_value(alert)
    if value < TUNABLE["ALERT_VALUE_MIN"]:
        if is_real_position:
            logger.warning(f"⚠️ COMPACT ALERT SKIPPED (value={value:.0f} < {TUNABLE['ALERT_VALUE_MIN']}) untuk POSISI REAL {alert['coin']} — OPEN notif utama tetap jalan terpisah di execute_decision()")
        else:
            logger.debug(f"Alert value too low ({value:.0f}), skip {alert['coin']}")
        return

    alert["value_label"] = label
    alert["value_score"] = value
    
    # Compute alert level
    level = _get_alert_level(alert)

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
    
    # === INSTRUMENTATION: COOLDOWN_CHECK ===
    with _last_alert_lock:
        last_alert_time = _last_alert.get(coin, None)
        cooldown_active = coin in _last_alert and now - _last_alert[coin] < cooldown
        logger.info(
            f"COOLDOWN_CHECK {coin} "
            f"last_alert_time={last_alert_time if last_alert_time else 'never'} "
            f"now={now} "
            f"cooldown_secs={cooldown} "
            f"cooldown_active={cooldown_active}"
        )
        
        if cooldown_active:
            if is_real_position:
                logger.warning(
                    f"⚠️ COMPACT ALERT SKIPPED (cooldown) untuk POSISI REAL {coin} "
                    f"remaining_secs={cooldown - (now - _last_alert[coin]):.0f} — "
                    f"OPEN notif utama tetap jalan terpisah di execute_decision()"
                )
            else:
                logger.warning(
                    f"COOLDOWN_SKIP {coin} "
                    f"remaining_secs={cooldown - (now - _last_alert[coin]):.0f}"
                )
            return
        _last_alert[coin] = now

    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        _alert_history[coin].append(now)

    # ===== LEVEL 0: SILENT (journal only, no send) =====
    if level == 0:
        if is_real_position:
            logger.warning(f"⚠️ COMPACT ALERT SKIPPED (level=0/silent) untuk POSISI REAL {alert['coin']} score:{alert['score']} rr:{alert.get('rr',0):.2f} — OPEN notif utama tetap jalan terpisah di execute_decision()")
        else:
            logger.debug(f"📦 Alert {alert['coin']} level 0 (silent) – score:{alert['score']} rr:{alert.get('rr',0):.2f}")
        return

    # --- Ambil data Phase 1 ---
    regime = alert.get('regime_interpretation')
    ob_reaction = alert.get('ob_reaction')
    fvg_quality = alert.get('fvg_quality')
    context_memory = alert.get('context_memory')
    cal_conf = alert.get('confidence_calibrated', alert.get('score', 50))
    cal_samples = alert.get('calibration_samples', 0)

    # --- LEVEL 1 & 2: Build compact alert first ---
    compact = _build_compact_alert(alert)

    # ===== LEVEL 1: COMPACT ONLY =====
    if level == 1:
        try:
            bot.send_message(USER_ID, compact, parse_mode='HTML')
            if CHANNEL_ID:
                bot.send_message(CHANNEL_ID, compact, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Send compact alert error: {e}")
        return

    # ===== LEVEL 2: COMPACT + FULL DETAIL =====
    # Send compact first
    try:
        bot.send_message(USER_ID, compact, parse_mode='HTML')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, compact, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Send compact alert error: {e}")

    # Then send full detail

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

    # Convert entropy_decision to stability (100% = zero noise)
    decision_stability = 100 - e_decision
    ebar_stab = "█"*int(decision_stability/10)+"░"*(10-int(decision_stability/10))

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
        why_now = "⚡ <b>WHY NOW</b>: Market reaction strong + low entropy\n"
    elif mode_v10 == "PREPARE":
        why_now = "🔧 <b>WHY NOW</b>: Transition detected, preparing for move\n"
    elif mode_v10 == "DEFENSIVE":
        why_now = "🛡️ <b>WHY NOW</b>: High event risk, defensive mode\n"
    elif mode_v10 == "CAUTIOUS":
        why_now = "⚠️ <b>WHY NOW</b>: Intent success low, cautious entry\n"
    elif alert.get("intent_type") == "seek_liquidity":
        why_now = "🦈 <b>WHY NOW</b>: Intent = SEEK_LIQUIDITY (stop hunt expected)\n"
    elif alert.get("time_pressure") == "urgent":
        why_now = "⏰ <b>WHY NOW</b>: Time Pressure = URGENT (opportunity fading)\n"

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
└─ Decision Stability: {decision_stability}% [{ebar_stab}]

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
        bot.send_message(USER_ID, text, parse_mode='HTML')
        if CHANNEL_ID:
            bot.send_message(CHANNEL_ID, text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Send alert error: {e}")
        
# ============================================================
# PART 38 – BOT COMMANDS (START, CONTEXT, REACTION, INTENT, SHOCK, BREATH, EVENTS, SETEVENT)
# ============================================================

def get_bot_personality(pipe: dict, ctx) -> str:
    """Determine bot's current trading personality."""
    exec_rate = pipe.get('execute_pass', 0) / max(1, pipe.get('check', 1))
    
    if exec_rate < 0.05:
        base = "🧊 DEFENSIVE"
    elif exec_rate < 0.15:
        base = "🟡 SELECTIVE"
    elif exec_rate < 0.30:
        base = "⚡ ACTIVE"
    else:
        base = "🔥 AGGRESSIVE"
    
    # Modifier based on entropy
    if ctx.shock_score > 70:
        base += " (cautious)"
    elif ctx.shock_score < 30 and ctx.transition_prob > 60:
        base += " (hunting)"
    
    return base

@bot.message_handler(commands=['start'])
def cmd_start(m):
    ctx = get_context_snapshot("BTC")
    warmup = is_warmup()
    uptime_m = get_uptime_minutes()
    
    with _journal_lock:
        journal_size = len(_decision_journal)
    
    pipe = get_pipeline_metrics()
    exec_rate = pipe.get('execute_pass', 0) / max(1, pipe.get('check', 1)) * 100
    personality = get_bot_personality(pipe, ctx)
    
    # Regime emoji
    regime_emoji = {"TRENDING_UP": "📈", "TRENDING_DOWN": "📉", "RANGING": "➡️"}.get(ctx.regime, "⚪")

    text = f"""
🚀 <b>HL BOT V10</b> • REACTION ENGINE
━━━━━━━━━━━━━━━━━━━━━━
{'🟡 WARMUP' if warmup else '🟢 ONLINE'} • {uptime_m}m
{regime_emoji} Regime: {ctx.regime} | Shock: {ctx.shock_score:.0f}%
🧠 Personality: {personality}

📊 <b>Pipeline</b> (last cycle)
├─ OBS: {pipe.get('obs', 0)}
├─ THESIS: {pipe.get('thesis', 0)}
├─ CONF: {pipe.get('confidence', 0)}
└─ EXEC: {pipe.get('execute_pass', 0)} | Conv: {exec_rate:.1f}%

📚 Memory: {journal_size} decisions

━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>Start Here</b>
/status   → What to do now
/entry    → Check setup (e.g., /entry BTC)
/analytics → Performance dashboard

/help → Full command reference
"""
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['help'])
def cmd_help(m):
    text = """
📖 <b>COMMAND REFERENCE</b> (V10.3)
━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>DAILY (Layer 1)</b>
/status     → Control tower, what to do now
/entry      → Check setup (e.g., /entry BTC)
/warroom    → Full analysis for a coin

📊 <b>PERFORMANCE</b>
/analytics  → Win rate, PnL, Score Buckets
/analytics_open → Open trades cohort
/regret     → Rejected setups analysis
/journal    → Recent decisions + learning trend
/traces     → Decision traces (why we did what)

🔍 <b>ANALYSIS (Layer 2)</b>
/debug      → Why bot thinks that
/context    → Market context snapshot
/shock      → Shock & tension metrics
/breath     → Advanced market breath
/reaction   → Latest catalyst data

🧠 <b>PSYCHOLOGY (Layer 3)</b>
/belief     → Thesis state per coin
/fatigue    → Fatigue penalty per family
/intel      → Intelligence dashboard
/events     → List active events

🩺 <b>SYSTEM (Layer 4)</b>
/health     → System health checklist
/quiet      → INFO mode (production)
/noise      → DEBUG mode (5 min)
/trace      → TRACE raw metrics (5 min)
/stopalert  → Toggle alerts ON/OFF
/setevent   → Set event (admin only)

🆕 <b>EXPERIMENTAL</b>
/analytics_open → Open trades cohort
/regret     → Rejected setups analysis
/traces     → Decision traces

━━━━━━━━━━━━━━━━━━━━━━
💡 Start with: /status
"""
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['context'])
def cmd_context(m):
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    text = f"""
🧠 <b>CONTEXT SNAPSHOT</b> (V10)
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
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['reaction'])
def cmd_reaction(m):
    reaction = get_current_reaction()
    if not reaction:
        bot.reply_to(m, "Belum ada data reaction.")
        return
    text = f"""
⚡ <b>REACTION ENGINE</b> (V10)
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
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['intent'])
def cmd_intent(m):
    parts = m.text.split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"

    with _intent_memory_lock:
        if coin not in _intent_memory:
            bot.reply_to(m, f"Belum ada intent memory untuk {coin}.")
            return
        text = f"🧠 <b>INTENT MEMORY</b> ({coin})\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for entry in list(_intent_memory[coin])[-10:]:
            dt = datetime.fromtimestamp(entry.ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            outcome_emoji = "✅" if entry.outcome in ("TP_HIT", "PARTIAL_WIN") else "❌"
            text += f"{dt} {entry.intent} {outcome_emoji} pnl:{entry.pnl:+.1f}%\n"

        for intent in set(e.intent for e in _intent_memory[coin]):
            rate = get_intent_success_rate(coin, intent)
            text += f"\n{intent}: {rate*100:.0f}% success"

        bot.reply_to(m, text, parse_mode='HTML')

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
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['breath'])
def cmd_breath(m):
    breath = compute_market_breath_v10()
    text = f"""
🌍 <b>ADVANCED MARKET BREATH</b> (V10)
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
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['intel'])
def cmd_intel(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Intel dashboard hanya untuk owner")
        return
    
    # ===== EXISTING INTELLIGENCE METRICS =====
    intel = get_intelligence_metrics() or {}
    defaults = {
        "transition_accuracy": 0.0,
        "shock_precision": 0.0,
        "preparation_recall": 0.0,
        "decision_consistency": 0.0,
        "belief_stability": 0.0,
        "execution_precision": 0.0,
    }
    intel = {**defaults, **intel}
    
    # ===== OPPORTUNITY METRICS (NEW) =====
    opp = get_opportunity_metrics()
    
    # ===== DECISION TEMPERATURE (NEW) =====
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    temp = compute_decision_temperature(
        context=ctx.__dict__ if hasattr(ctx, "__dict__") else ctx,
        breath=breath,
        reaction=get_current_reaction()
    )
    
    # ===== BUILD TEXT =====
    text = f"""🧠 <b>INTELLIGENCE</b> (V10)
━━━━━━━━━━━━━━━━━━━━━━

🌡️ <b>Decision Temperature</b>
├─ State: {temp['state']}
├─ Temp: {temp['temperature']:.0f}°
├─ Scan Speed: {temp['scan_speed']:.1f}x
└─ Size Boost: {temp['size_boost']:.1f}x

📊 <b>Opportunity Funnel</b>
├─ Scanned: {opp['scanned']}
├─ Qualified: {opp['qualified']}
├─ Executed: {opp['executed']}
├─ Qualification: {opp['qualification_rate']:.1f}%
├─ Execution: {opp['execution_rate']:.1f}%
└─ Conversion: {opp['conversion_rate']:.1f}%

🚫 <b>Top Rejections</b> (today)
{chr(10).join([f"├─ {r}: {c}x" for r, c in opp['top_rejections'][:5]]) if opp['top_rejections'] else '├─ none'}

📈 <b>Engine Metrics</b>
├─ Transition Acc: {intel['transition_accuracy']:.0f}%
├─ Shock Precision: {intel['shock_precision']:.0f}%
├─ Preparation Recall: {intel['preparation_recall']:.0f}%
├─ Decision Consistency: {intel['decision_consistency']:.0f}%
├─ Belief Stability: {intel['belief_stability']:.2f}
└─ Execution Precision: {intel['execution_precision']:.1f}%

📌 <b>Health Check</b>
{'🟢 HEALTHY' if opp['qualification_rate'] > 10 and opp['conversion_rate'] > 1 else '🟡 REVIEW'}
├─ {'⚠️ No opportunities' if opp['qualified'] == 0 else '✅ Opportunities found'}
├─ {'⚠️ No executions' if opp['executed'] == 0 else '✅ Trades executed'}
└─ {'⚠️ Funnel too tight' if opp['qualification_rate'] < 5 and opp['scanned'] > 100 else '✅ Funnel healthy'}

💡 /status - System status
   /debug BTC - Deep dive
"""
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['events'])
def cmd_events(m):
    with _event_risk_lock:
        if not _EVENT_RISK_DATA:
            bot.reply_to(m, "Tidak ada event risk terdaftar.")
            return
        text = "📅 <b>EVENT RISK</b> (V10)\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for ev in _EVENT_RISK_DATA[-10:]:
            dt = datetime.fromtimestamp(ev.ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            importance_bar = "█" * int(ev.importance / 10) + "░" * (10 - int(ev.importance / 10))
            text += f"{dt} {ev.label}\n"
            text += f"   ├─ Importance: {ev.importance}% [{importance_bar}]\n"
            text += f"   ├─ Expected Vol: {ev.expected_vol}%\n"
            text += f"   └─ Bias: {ev.bias}\n\n"
        bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['setevent'])
def cmd_setevent(m):
    if m.from_user.id != USER_ID:
        return
    try:
        parts = m.text.split()
        if len(parts) < 5:
            bot.reply_to(m, "Format: /setevent YYYY-MM-DD HH:MM IMPORTANCE VOL BIAS LABEL")
            return
        try:
            dt_obj = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M")
        except ValueError:
            bot.reply_to(m, "❌ Format tanggal salah. Gunakan YYYY-MM-DD HH:MM")
            return
        dt_str = f"{parts[1]} {parts[2]}"
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
    except ValueError as e:
        bot.reply_to(m, f"❌ Parsing error: {e}")
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
    
    # ===== PATCH 9: Learning Trend =====
    with _journal_lock:
        all_entries = list(_decision_journal)
        recent = all_entries[-20:] if len(all_entries) >= 20 else all_entries
        old = all_entries[-40:-20] if len(all_entries) >= 40 else []
        
        recent_wr = sum(1 for e in recent if e.outcome in ("TP_HIT", "PARTIAL_WIN")) / max(1, len(recent))
        old_wr = sum(1 for e in old if e.outcome in ("TP_HIT", "PARTIAL_WIN")) / max(1, len(old))
        trend = recent_wr - old_wr
        trend_arrow = '▲' if trend > 0.05 else '▼' if trend < -0.05 else '—'
    
    teks = "📜 <b>DECISION JOURNAL</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀"}
    
    for ts, coin, mreg, belief, dirn, fs, de, commit, pressure, mode, intent, why_not in rows:
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
        teks += f"{dt} {coin} [{mreg}] {emoji.get(belief,'❓')}{belief.upper()} | {mode}/{intent}\n"
        teks += f"   Score:{fs} | DE:{de:.0f} | Commit:{commit:.0f} | Pressure:{pressure}\n"
        teks += f"   ⚠️ {why_not[:40]}\n\n"
    
    # ===== PATCH 9: Learning Trend =====
    teks += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    teks += f"📈 *Learning Trend*\n"
    teks += f"├─ Last20 WR: {recent_wr*100:.0f}%\n"
    if old:
        teks += f"├─ Prev20 WR: {old_wr*100:.0f}%\n"
        teks += f"└─ {trend_arrow} {abs(trend)*100:.0f}% {'improvement' if trend > 0 else 'decline' if trend < 0 else 'stable'}"
    else:
        teks += f"└─ ⏳ Need 40 entries for trend"
    
    bot.reply_to(m, teks, parse_mode='HTML')

@bot.message_handler(commands=['belief'])
def cmd_belief(m):
    with _belief_state_lock:
        if not _belief_state:
            bot.reply_to(m, "Belum ada data belief state.")
            return
        text = "🧠 <b>BELIEF STATE SUMMARY</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        emoji = {"seeking":"🔍","building":"🏗️","convicted":"⚡","executing":"🚀","invalidated":"❌"}
        for coin, data in sorted(_belief_state.items(), key=lambda x: x[1]["since"]):
            state = data["state"].value
            dur = int(time.time() - data["since"])
            mins, secs = dur // 60, dur % 60
            text += f"{emoji.get(state,'❓')} {coin}: {state.upper()} ({mins}m {secs}s) | score:{data.get('score',0):.0f}\n"
        bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['fatigue'])
def cmd_fatigue(m):
    with _fatigue_memory_lock:
        if not _fatigue_memory:
            bot.reply_to(m, "Belum ada data fatigue.")
            return
        text = "💪 <b>FATIGUE STATUS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for family, deq in _fatigue_memory.items():
            cnt = len(deq)
            if cnt >= TUNABLE["FATIGUE_MAX_PER_HOUR"]:
                bar, pen = "🔴", 0.3
            elif cnt >= 3:
                bar, pen = "🟡", 0.6
            else:
                bar, pen = "🟢", 0.8
            text += f"{bar} {family}: {cnt}x rejections | penalty {pen:.0%}\n"
        bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['prediction'])
def cmd_prediction(m):
    with _prediction_memory_lock:
        if not _prediction_memory:
            bot.reply_to(m, "Belum ada data prediction quality.")
            return
        text = "📊 <b>PREDICTION QUALITY</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for coin, data in sorted(_prediction_memory.items(), key=lambda x: x[1]["ema_quality"], reverse=True)[:10]:
            q = data["ema_quality"]
            bar = "█" * int(q / 10) + "░" * (10 - int(q / 10))
            text += f"{coin}: {q:.0f} [{bar}]\n"
        bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['traces'])
def cmd_traces(m):
    with _trace_lock:
        if not _decision_traces:
            bot.reply_to(m, "Belum ada decision traces.")
            return
        text = "📝 <b>DECISION TRACES</b> (10 terakhir)\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for t in list(_decision_traces)[-10:]:
            dt = datetime.fromtimestamp(t.timestamp, tz=timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
            text += f"{dt} {t.coin} {t.event_type} | {t.belief_state} | {t.final_decision}\n"
            text += f"   DE:{t.decision_energy:.0f} | Mode:{t.execution_mode} | Context age:{t.context_age:.1f}s\n"
            text += f"   {t.what_changed}\n\n"
        bot.reply_to(m, text, parse_mode='HTML')
        
# ============================================================
# PART 40 – BOT COMMANDS (Status, Analytics, Entry, Warroom, Stopalert, Health, Intel)
# ============================================================

@bot.message_handler(commands=['status'])
def cmd_status(m):
    ctx = get_context_snapshot("BTC")
    breath = compute_market_breath_v10()
    reaction = get_current_reaction()
    event_adj = get_event_risk_adjustment()
    mode = get_execution_mode_v10(ctx, reaction, 0.5, event_adj)[0].value.upper()
    
    warmup = is_warmup()
    uptime_m = get_uptime_minutes()
    
    with _journal_lock:
        journal_size = len(_decision_journal)
    
    # Hidden Liquidity
    delta_raw = _rolling_delta.get("BTC", deque())
    oi_raw = _oi_history.get("BTC", deque())
    delta_history = list(delta_raw)[-5:] if delta_raw else []
    oi_history = [v for ts, v in list(oi_raw)[-5:]] if oi_raw else []
    candles_5m = get_candles("BTC", "5m", 20)
    hl = compute_hidden_liquidity("BTC", candles_5m, delta_history, oi_history) if candles_5m else {"score": 0, "status": "⏸️ NONE"}
    
    drift = compute_intent_drift("BTC")
    pipe = get_pipeline_metrics()
    exec_rate = pipe.get('execute_pass', 0) / max(1, pipe.get('check', 1)) * 100
    eff_emoji, eff_label = get_efficiency_interpretation(pipe.get('execute_pass', 0), pipe.get('check', 1))
    personality = get_bot_personality(pipe, ctx)
    
    # == NEXT BEST ACTION ==
    if warmup:
        action = "⏳ Warming up... (30m needed)"
        action_reason = "Collecting data"
    elif hl.get('score', 0) > 40 and journal_size > 5:
        action = "✅ SCAN SETUP"
        action_reason = f"Absorption {hl['score']}% detected"
    elif reaction and reaction.absorption < 0.3:
        action = "⚡ CATALYST ACTIVE"
        action_reason = f"Market absorbing event: {reaction.event}"
    elif ctx.shock_score > 60:
        action = "⚠️ WAIT"
        action_reason = f"High stress {ctx.shock_score:.0f}%"
    elif ctx.transition_prob > 60:
        action = "🔧 PREPARE"
        action_reason = f"Regime transition {ctx.transition_prob:.0f}%"
    elif journal_size < 3:
        action = "👀 OBSERVING"
        action_reason = "Building memory"
    else:
        action = "👀 OBSERVING"
        action_reason = f"Market {ctx.regime} | Breath {breath['bull']*100:.0f}%"

    # Risk level
    imp = event_adj.get('importance', 0)
    risk = "🟢 LOW" if imp < 30 else "🟡 MODERATE" if imp < 60 else "🔴 HIGH"

    text = f"""
🧠 <b>CONTROL TOWER</b> • {get_wib()}
━━━━━━━━━━━━━━━━━━━━━━
{'🟡 WARMUP' if warmup else '🟢 ONLINE'} • {uptime_m}m
Mode: {mode} | Regime: {ctx.regime}
Personality: {personality}

📊 <b>Market Pulse</b>
├─ Stress: {ctx.shock_score:.0f}%
├─ Transition: {ctx.transition_prob:.0f}%
├─ Bull Breath: {breath['bull']*100:.0f}%
└─ Risk: {risk}

⚙️ <b>Pipeline</b> (last cycle)
├─ Observed: {pipe.get('obs', 0)}
├─ Thesis: {pipe.get('thesis', 0)}
├─ Confidence: {pipe.get('confidence', 0)}
└─ Executed: {pipe.get('execute_pass', 0)} ({exec_rate:.1f}%)

📈 <b>Efficiency</b>
├─ Status: {eff_emoji} {eff_label}
└─ {pipe.get('check', 0)} scans total

🧊 <b>Absorption</b>: {hl.get('score', 0)}% {hl.get('status', '⏸️ NONE')}
🧠 <b>Drift</b>: {drift:.2f} {'🔄' if drift > 0.3 else '✅' if drift < 0.1 else '⚪'}

━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>Next Best Action</b>
{action}
Reason: {action_reason}

💡 /entry BTC  - Check setup
   /analytics   - Performance
   /debug BTC   - Deep dive

━━━━━━━━━━━━━━━━━━━━━━
Last update: {get_wib()} | v10.3.2
"""
    bot.reply_to(m, text, parse_mode='HTML')

def get_efficiency_interpretation(executed: int, total_signals: int) -> Tuple[str, str]:
    """Return (emoji, label) based on signal conversion rate."""
    if total_signals == 0:
        return "⚪", "No data"
    rate = executed / total_signals
    if rate > 0.40:
        return "🔴", "Too Loose"
    elif rate > 0.15:
        return "🟡", "Balanced"
    elif rate > 0.05:
        return "🟢", "Selective"
    else:
        return "🔵", "Ultra Selective"


@bot.message_handler(commands=['analytics'])
def cmd_analytics(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    conn = db_connect()
    c = conn.cursor()

    # ===== DOMAIN 1: TRADE REALITY (DB = ground truth) =====
    c.execute("SELECT COUNT(*) FROM signals")
    signals_total = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=1")
    closed = c.fetchone()[0] or 0

    open_trades = signals_total - closed

    c.execute('''SELECT
                SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome IN ('SL_HIT','PARTIAL_LOSS') THEN 1 ELSE 0 END) as losses,
                AVG(rr) as avg_rr,
                SUM(pnl) as total_pnl
                FROM signals WHERE evaluated=1''')
    row = c.fetchone()
    wins = row[0] or 0
    losses = row[1] or 0
    avg_rr = row[2] or 0.0
    total_pnl = row[3] or 0.0

    # BE = sisa (residual) dari closed - win - loss
    be = closed - wins - losses
    if be < 0:
        be = 0

    win_rate = (wins / closed * 100) if closed > 0 else 0

    # ===== DOMAIN 2: DECISION FUNNEL (runtime, bukan trade) =====
    pipe = get_pipeline_metrics()
    observed = pipe.get('obs', 0)
    thesis = pipe.get('thesis', 0)
    confidence = pipe.get('confidence', 0)
    execute_pass = pipe.get('execute_pass', 0)

    try:
        with _shadow_stats_lock:
            shadow_total = _shadow_stats.get('total', 0)
    except:
        shadow_total = 0

    decision_yield = (execute_pass / observed * 100) if observed > 0 else 0

    # ===== DOMAIN 3: EXECUTION HEALTH (bridge, derived) =====
    # Execution Yield = berapa banyak yang lolos jadi posisi (closed dipakai sbg proxy konversi penuh)
    execution_yield = (closed / execute_pass * 100) if execute_pass > 0 else 0
    # Close Rate = berapa dari total signal yang sudah closed
    close_rate = (closed / signals_total * 100) if signals_total > 0 else 0
    # Open Load = porsi posisi yang masih nganggur dari semua signal yang sudah pernah masuk
    open_load = (open_trades / signals_total * 100) if signals_total > 0 else 0
    # Shadow Pressure = porsi near-miss dibanding total yang lolos confidence (execute_pass + shadow)
    shadow_denom = execute_pass + shadow_total
    shadow_pressure = (shadow_total / shadow_denom * 100) if shadow_denom > 0 else 0

    # Managed (TradeManager-tracked) vs DB-open, ini metric lifecycle bottleneck yang asli
    with TRADE_MANAGER._lock:
        managed_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
    managed_ratio = (managed_open / open_trades * 100) if open_trades > 0 else 0
    orphan_count = max(0, open_trades - managed_open)

    if open_load > 80:
        funnel_status = "🔴 Bottleneck: Trade Lifecycle"
    elif open_load > 60:
        funnel_status = "🟡 Balanced"
    else:
        funnel_status = "🟢 Healthy Turnover"

    # ===== BUILD TEXT =====
    text = f"""📈 <b>PERFORMANCE</b>
━━━━━━━━━━━━━━━━━━━━━━

💾 <b>Trade Reality (DB)</b>
├─ Signals: {signals_total}
├─ Open: {open_trades}
├─ Closed: {closed}
├─ Win: {wins}
├─ Loss: {losses}
├─ BE: {be}
├─ WR: {win_rate:.1f}%
├─ Avg RR: {avg_rr:.2f}
└─ Total PnL: {total_pnl:+.2f}%

🧠 <b>Decision Funnel (Runtime)</b>
├─ Observed: {observed}
├─ Thesis: {thesis}
├─ Confidence: {confidence}
├─ Execute Pass: {execute_pass}
├─ Shadow: {shadow_total}
└─ Decision Yield: {decision_yield:.1f}%

⚙️ <b>Execution Health</b>
├─ Close Rate: {close_rate:.1f}%
├─ Open Load: {open_load:.1f}%
├─ Managed: {managed_open}/{open_trades} ({managed_ratio:.0f}%)
├─ Orphan: {orphan_count}
├─ Shadow Pressure: {shadow_pressure:.1f}%
├─ Execution Yield: {execution_yield:.1f}%
└─ Funnel Status: {funnel_status}
"""

    # Score Buckets (closed trades) — tetap dipertahankan, masih domain Trade Reality
    c.execute('''SELECT
                    CASE
                        WHEN score BETWEEN 0 AND 30 THEN '0-30'
                        WHEN score BETWEEN 31 AND 50 THEN '31-50'
                        WHEN score BETWEEN 51 AND 70 THEN '51-70'
                        WHEN score BETWEEN 71 AND 85 THEN '71-85'
                        WHEN score >= 86 THEN '86+'
                    END as bucket,
                    COUNT(*) as count,
                    SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
                    AVG(rr) as avg_rr,
                    AVG(pnl) as avg_pnl
                FROM signals WHERE evaluated=1 GROUP BY bucket ORDER BY bucket''')
    score_buckets = c.fetchall()

    if score_buckets:
        text += "\n📊 <b>Score Buckets (Closed)</b>\n"
        for bucket, count, wins_b, avg_rr_b, avg_pnl_b in score_buckets:
            wr = (wins_b / count * 100) if count > 0 else 0
            avg_rr_b = avg_rr_b or 0.0
            avg_pnl_b = avg_pnl_b or 0.0
            text += f"├─ {bucket}: n={count} WR={wr:.0f}% RR={avg_rr_b:.2f} PnL={avg_pnl_b:+.2f}%\n"
    else:
        text += "\n📊 <b>Score Buckets</b>: belum ada closed trade\n"

    conn.close()
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['analytics_open'])
def cmd_analytics_open(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    snapshot = get_snapshot()
    if not snapshot:
        bot.reply_to(m, "❌ Snapshot unavailable")
        return

    conn = db_connect()
    c = conn.cursor()
    c.execute('''SELECT coin, direction, entry_price, sl_price, tp_price, rr, score, timestamp, position_size_mult
                 FROM signals WHERE evaluated = 0''')
    rows = c.fetchall()
    conn.close()

    if not rows:
        bot.reply_to(m, "✅ No open trades.")
        return

    buckets = {
        "near_tp": 0,
        "mid_tp": 0,
        "early_tp": 0,
        "near_sl": 0,
        "mid_sl": 0,
        "early_sl": 0,
        "undefined": 0
    }

    rr_values = []
    drift_values = []
    exposure_total = 0.0
    coin_counts: Dict[str, int] = {}
    now_ts = time.time()
    age_buckets = {"<1h": 0, "1-4h": 0, "4-12h": 0, "12-24h": 0, ">24h": 0}

    for coin, direction, entry, sl, tp, rr, score, ts, pos_mult in rows:
        coin_counts[coin] = coin_counts.get(coin, 0) + 1
        if rr:
            rr_values.append(rr)
        exposure_total += pos_mult if pos_mult else 1.0

        if ts:
            age_h = (now_ts - ts) / 3600
            if age_h < 1:
                age_buckets["<1h"] += 1
            elif age_h < 4:
                age_buckets["1-4h"] += 1
            elif age_h < 12:
                age_buckets["4-12h"] += 1
            elif age_h < 24:
                age_buckets["12-24h"] += 1
            else:
                age_buckets[">24h"] += 1

        price = snapshot.mids.get(coin, 0)
        if not price:
            buckets["undefined"] += 1
            continue

        if direction == "LONG":
            tp_dist = (tp - entry) / max(entry, 0.01) * 100
            sl_dist = (entry - sl) / max(entry, 0.01) * 100
            current_profit = (price - entry) / max(entry, 0.01) * 100
        else:
            tp_dist = (entry - tp) / max(entry, 0.01) * 100
            sl_dist = (sl - entry) / max(entry, 0.01) * 100
            current_profit = (entry - price) / max(entry, 0.01) * 100

        drift_values.append(current_profit)

        if tp_dist <= 0: tp_dist = 0.01
        if sl_dist <= 0: sl_dist = 0.01

        tp_ratio = current_profit / tp_dist if tp_dist != 0 else 0
        sl_ratio = abs(current_profit) / sl_dist if sl_dist != 0 else 0

        if current_profit > 0:
            if tp_ratio > 0.75:
                buckets["near_tp"] += 1
            elif tp_ratio > 0.25:
                buckets["mid_tp"] += 1
            else:
                buckets["early_tp"] += 1
        else:
            if sl_ratio > 0.75:
                buckets["near_sl"] += 1
            elif sl_ratio > 0.25:
                buckets["mid_sl"] += 1
            else:
                buckets["early_sl"] += 1

    profit_total = buckets['near_tp'] + buckets['mid_tp'] + buckets['early_tp']
    loss_total = buckets['near_sl'] + buckets['mid_sl'] + buckets['early_sl']

    avg_rr_open = sum(rr_values) / len(rr_values) if rr_values else 0.0
    avg_drift = sum(drift_values) / len(drift_values) if drift_values else 0.0

    # Coin Concentration: top 3 coin berdasarkan jumlah open trades
    top_coins = sorted(coin_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    n_open = len(rows)
    concentration_lines = "\n".join(
        f"├─ {c_name}: {c_cnt} ({c_cnt/n_open*100:.0f}%)" for c_name, c_cnt in top_coins
    )

    # ===== P1 FIX: ADD TRADEMANAGER STATS =====
    tm_stats = TRADE_MANAGER.get_positions_summary()
    
    text = f"""📊 <b>OPEN TRADES COHORT</b> (n={len(rows)})
━━━━━━━━━━━━━━━━━━━━━━

🟢 <b>Profit Zone</b> ({profit_total})
├─ Near TP (&gt;75%): {buckets['near_tp']}
├─ Mid TP (25-75%): {buckets['mid_tp']}
└─ Early TP (&lt;25%): {buckets['early_tp']}

🔴 <b>Loss Zone</b> ({loss_total})
├─ Near SL (&gt;75%): {buckets['near_sl']}
├─ Mid SL (25-75%): {buckets['mid_sl']}
└─ Early SL (&lt;25%): {buckets['early_sl']}

⚪ Undefined (no price): {buckets['undefined']}

📐 <b>Cohort Stats</b>
├─ Avg RR: {avg_rr_open:.2f}
├─ Avg Drift: {avg_drift:+.2f}%
└─ Exposure (Σ size_mult): {exposure_total:.2f}

⏱️ <b>Age Distribution</b>
├─ &lt;1h: {age_buckets['<1h']}
├─ 1-4h: {age_buckets['1-4h']}
├─ 4-12h: {age_buckets['4-12h']}
├─ 12-24h: {age_buckets['12-24h']}
└─ &gt;24h: {age_buckets['>24h']}

🎯 <b>Coin Concentration</b> (top 3)
{concentration_lines}

💡 <b>Interpretasi</b>:
{'✅ Healthy: Majority near TP' if buckets['near_tp'] > buckets['near_sl'] else '⚠️ Warning: Many near SL!'}

━━━━━━━━━━━━━━━━━━━━━━
🔥 <b>P1 TradeManager</b>
├─ Open (managed): {tm_stats['open']}
├─ Partial TP hit: {tm_stats['partial']}
├─ Closed: {tm_stats['closed']}
└─ Avg PnL (closed): {tm_stats['avg_pnl']:+.2f}%
"""
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['regret'])
def cmd_regret(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    with _journal_lock:
        recent = [e for e in _decision_journal if not e.executed][-50:]

    if not recent:
        bot.reply_to(m, "No rejected decisions found.")
        return

    winners = 0
    losers = 0
    still_open = 0

    for entry in recent:
        coin = entry.coin
        direction = entry.direction
        entry_price = entry.entry
        sl = entry.sl
        tp = entry.tp

        candles = get_candles(coin, "5m", 5)
        if not candles or len(candles) < 2:
            still_open += 1
            continue

        high = max(float(c['h']) for c in candles)
        low = min(float(c['l']) for c in candles)

        hit_tp = False
        hit_sl = False

        if direction == "LONG":
            if high >= tp: hit_tp = True
            if low <= sl: hit_sl = True
        else:
            if low <= tp: hit_tp = True
            if high >= sl: hit_sl = True

        if hit_tp:
            winners += 1
        elif hit_sl:
            losers += 1
        else:
            still_open += 1

    total_evaluated = winners + losers
    regret_rate = (winners / total_evaluated * 100) if total_evaluated > 0 else 0

    text = f"""😭 <b>REGRET ANALYSIS</b> (Rejected Setups)
━━━━━━━━━━━━━━━━━━━━━━
Total Rejected (checked): {len(recent)}
├─ Would have WON (TP hit): {winners}
├─ Would have LOST (SL hit): {losers}
└─ Still Open / Undecided: {still_open}

📊 <b>Regret Rate</b>: {regret_rate:.1f}%

💡 <b>Interpretasi</b>:
{'🔴 HIGH REGRET! Threshold too tight!' if regret_rate > 60 else '🟢 OK. Filter working well.' if regret_rate < 40 else '🟡 Moderate. Fine tune.'}
"""
    bot.reply_to(m, text, parse_mode='HTML')


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
        bot.reply_to(m, text, parse_mode='HTML')
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
        bot.reply_to(m, text, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['stopalert'])
def cmd_stopalert(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    if RUNTIME.is_alert_enabled():
        RUNTIME.disable_alerts()
        # Ambil konteks buat feedback
        ctx = get_context_snapshot("BTC")
        text = f"""
🔴 <b>ALERT OFF</b>
━━━━━━━━━━━━━━━━━━━━━━
Bot alert telah dimatikan.

📊 *Market saat alert OFF*
├─ Regime: {ctx.regime}
├─ Shock: {ctx.shock_score:.0f}%
└─ Transition: {ctx.transition_prob:.0f}%

💡 Untuk mengaktifkan kembali:
/stopalert
"""
        bot.reply_to(m, text, parse_mode='HTML')
    else:
        RUNTIME.enable_alerts()
        ctx = get_context_snapshot("BTC")
        text = f"""
🟢 <b>ALERT ON</b>
━━━━━━━━━━━━━━━━━━━━━━
Bot alert telah diaktifkan kembali.

📊 *Market saat alert ON*
├─ Regime: {ctx.regime}
├─ Shock: {ctx.shock_score:.0f}%
└─ Transition: {ctx.transition_prob:.0f}%

💡 Untuk mematikan:
/stopalert
"""
        bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['health'])
def cmd_health(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    # === AMBIL PIPELINE METRICS ===
    pipe = get_pipeline_metrics()
    
    # === REAL-TIME REJECTION REASON COUNTER (from journal) ===
    journal_rejections = get_rejection_reason_counts(window_minutes=60)
    top_journal_reject = sorted(journal_rejections.items(), key=lambda x: x[1], reverse=True)[:5]

    # === ENTRY_BLOCK BREAKDOWN (from opportunity_stats — nangkep gate yang
    # return None SEBELUM journal_entry_universal dibuat, misal conviction_gate
    # dan micro_structure_gate, yang gak pernah masuk _decision_journal sama sekali) ===
    opp_metrics = get_opportunity_metrics()
    top_entry_block = opp_metrics.get("top_rejections", [])
    
    # === BOTTLENECK DETECTION ===
    stages = [
        ("scan", pipe.get("check", 0)),
        ("events", pipe.get("obs", 0)),
        ("thesis", pipe.get("thesis", 0)),
        ("conf", pipe.get("confidence", 0)),
        ("exec", pipe.get("execute_pass", 0)),
    ]
    
    # Cari bottleneck: stage dengan drop terbesar
    bottleneck = "✅ none"
    for i in range(1, len(stages)):
        if stages[i-1][1] > 0 and stages[i][1] / stages[i-1][1] < 0.2:
            bottleneck = f"🔴 {stages[i-1][0]}→{stages[i][0]}"
            break
    
    # === BUILD RESPONSE ===
    text = f"""🧠 <b>ENGINE HEALTH</b>
━━━━━━━━━━━━━━━━━━━━━━

📊 *Pipeline*
├─ scan: {pipe.get('check', 0)}
├─ events: {pipe.get('obs', 0)}
├─ thesis: {pipe.get('thesis', 0)}
├─ conf: {pipe.get('confidence', 0)}
└─ exec: {pipe.get('execute_pass', 0)}

{bottleneck}

🚫 *Top Rejections (Last 1h Journal)*
"""
    for reason, count in top_journal_reject:
        text += f"├─ {reason}: {count}\n"
    if not top_journal_reject:
        text += "├─ (none)\n"

    text += "\n🛑 *ENTRY_BLOCK (Early Gates, Session)*\n"
    for reason, count in top_entry_block:
        text += f"├─ {reason}: {count}\n"
    if not top_entry_block:
        text += "├─ (none)\n"
    
    # === SIGNAL CONVERSION ===
    exec_count = pipe.get('execute_pass', 0)
    scan_count = pipe.get('check', 1)
    eff_emoji, eff_label = get_efficiency_interpretation(exec_count, scan_count)
    exec_rate = exec_count / max(1, scan_count) * 100
    text += f"""
📊 <b>Signal Conversion</b>
├─ Efficiency: {exec_rate:.1f}%
└─ Status: {eff_emoji} {eff_label}

━━━━━━━━━━━━━━━━━━━━━━
💡 /status - Full dashboard
   /debug BTC - Deep dive
"""
    bot.reply_to(m, text, parse_mode='HTML')




@bot.message_handler(commands=['debug'])
def cmd_debug(m):
    import traceback
    parts = m.text.split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"

    try:
        snapshot = get_snapshot()
        mark = snapshot.mids.get(coin, 0) if snapshot else 0
        if mark == 0:
            bot.reply_to(m, f"❌ {coin} not found")
            return

        context = get_context_snapshot(coin)
        breath = compute_market_breath_v10()
        reaction = get_current_reaction()

        # ===== DATA BUFFER =====
        delta_raw = _rolling_delta.get(coin, deque())
        oi_raw = _oi_history.get(coin, deque())

        delta_history = list(delta_raw)[-5:] if delta_raw else []
        oi_history = [v for ts, v in list(oi_raw)[-5:]] if oi_raw else []

        # ===== LAST DECISION =====
        with _journal_lock:
            recent = [e for e in list(_decision_journal) if e.coin == coin]
            last = recent[-1] if recent else None
            journal_size = len(_decision_journal)

        # ===== INTENT DRIFT =====
        drift = compute_intent_drift(coin)
        with _intent_vector_lock:
            vec_history = _intent_vector_history.get(coin, deque())
            vec_count = len(vec_history)

        # ===== EVENT RISK =====
        with _event_risk_lock:
            event_active = len(_EVENT_RISK_DATA) > 0
            event_count = len(_EVENT_RISK_DATA)

        # ===== CANDLES + HIDDEN LIQUIDITY =====
        master = {coin: get_candles(coin, "1h", 100)}
        candles_5m = get_candles(coin, "5m", 20, master)

        hl = compute_hidden_liquidity(coin, candles_5m, delta_history, oi_history) if candles_5m else {
            "score": 0, "side": "NONE", "status": "⏸️ NONE",
            "eff_score": 0, "vol_score": 0,
            "persist_score": 0, "oi_score": 0, "confidence": 0
        }

        # ===== TOP BLOCKER (PATCH 7) =====
        blockers = []
        if hl.get('eff_score', 0) < 0.2:
            blockers.append(("efficiency", hl.get('eff_score', 0)))
        if hl.get('vol_score', 0) < 0.2:
            blockers.append(("volume", hl.get('vol_score', 0)))
        if hl.get('persist_score', 0) < 0.2:
            blockers.append(("persistence", hl.get('persist_score', 0)))
        if hl.get('oi_score', 0) < 0.2:
            blockers.append(("oi", hl.get('oi_score', 0)))
        blockers.sort(key=lambda x: x[1])

        # ===== CLAMP SHOCK =====
        shock_display = max(0.0, min(100.0, context.shock_score))

        # ===== BUILD OUTPUT =====
        text = f"🔍 <b>DEBUG</b> ({coin})\n━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"💰 Price: {fmt_price(mark)}\n"
        text += f"📊 Regime: {context.regime} | Stress: {shock_display:.0f}%\n"
        text += f"🔄 Transition: {context.transition_prob:.0f}% | Tension: {context.tension:.0f}%\n"
        text += f"🌍 Breath: Bull {breath['bull']*100:.0f}% | Part {breath['participation']*100:.0f}%\n"

        # ===== HIDDEN LIQUIDITY =====
        text += f"\n🧊 <b>Absorption</b>: {hl['score']}% ({hl.get('status', '⏸️ NONE')})\n"
        text += f"   ├─ Inputs: delta={len(delta_history)}/5, oi={len(oi_history)}/5, candles={len(candles_5m) if candles_5m else 0}\n"
        text += f"   ├─ eff={hl.get('eff_score',0):.2f} | vol={hl.get('vol_score',0):.2f} | persist={hl.get('persist_score',0):.2f} | oi={hl.get('oi_score',0):.2f}\n"
        text += f"   └─ Confidence: {hl.get('confidence',0)*100:.0f}%\n"

        # ===== TOP BLOCKER =====
        if blockers:
            text += f"\n🚫 <b>TOP BLOCKER</b>\n"
            text += f"└─ {blockers[0][0]} = {blockers[0][1]:.2f}\n"
            if len(blockers) > 1:
                text += f"   (next: {', '.join([f'{b[0]}={b[1]:.2f}' for b in blockers[1:3]])})\n"
        else:
            text += f"\n✅ No major blockers\n"

        # ===== INTENT DRIFT =====
        text += f"\n📊 <b>Drift</b>: {drift:.2f} (history: {vec_count}/4 min)"
        if vec_count < 4:
            text += " ⏳ collecting"

        # ===== REACTION =====
        if reaction:
            text += f"\n⚡ <b>Catalyst</b>: {reaction.event} | Absorption: {reaction.absorption*100:.0f}% | Conf: {reaction.confidence*100:.0f}%"
        else:
            if event_active:
                text += f"\n⚡ <b>Catalyst</b>: ⏳ Waiting (event risk active: {event_count})"
            else:
                text += f"\n⚡ <b>Catalyst</b>: none"

        # ===== LAST DECISION =====
        if last:
            text += f"\n\n📝 <b>Last Decision</b>\n"
            text += f"├─ Mode: {last.mode} | Score: {last.score}\n"
            text += f"├─ Intent: {last.intent} | Thesis: {last.belief}\n"
            if last.outcome:
                text += f"├─ Outcome: {last.outcome} | PnL: {last.pnl:+.2f}%\n"
            else:
                text += f"├─ Status: ⏳ PENDING\n"
            text += f"└─ Drift: {last.intent_drift:.2f}"

        # ===== EVENT RISK =====
        event_adj = get_event_risk_adjustment()
        if event_adj.get("importance", 0) > 20:
            text += f"\n\n📅 <b>Event Risk</b>: {event_adj['importance']:.0f}% | Bias: {event_adj['bias']:+.0f}"

        text += f"\n\n📚 Journal: {journal_size} entries"

        bot.reply_to(m, text, parse_mode='HTML')

    except Exception as e:
        tb = traceback.format_exc()
        if len(tb) > 4000:
            tb = tb[-4000:]
        bot.reply_to(m, f"❌ <b>Error</b>\n<code>{tb}</code>", parse_mode='HTML')

@bot.message_handler(commands=['quiet'])
def cmd_quiet(m):
    """Set log level to INFO (production mode)"""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    global LOG_LEVEL
    LOG_LEVEL = "INFO"
    os.environ["LOG_LEVEL"] = "INFO"
    console.setLevel(logging.INFO)  # ← only console, not logger
    logger.info("🔇 QUIET MODE activated")
    
    bot.reply_to(m, "🔇 <b>QUIET MODE</b>\n🎚️ Log level set to INFO\n⚠️ Only final decisions + errors will appear", parse_mode='HTML')
    
# ============================================================
# NOISE / TRACE GATE
# ============================================================

NOISE_MIN_SAMPLES = 60

def allow_noise_mode() -> bool:
    """
    Gate debug/noise mode supaya ga aktif saat warmup.
    Cuma butuh OI history BTC cukup.
    """
    try:
        with _oi_lock:
            hist = _oi_history.get("BTC")
            if hist is None:
                return False
            return len(hist) >= NOISE_MIN_SAMPLES
    except Exception as e:
        logger.error(f"allow_noise_mode: {e}")
        return False

@bot.message_handler(commands=['noise'])
def cmd_noise(m):
    try:
        if m.from_user.id != USER_ID:
            bot.reply_to(m, "⛔ Admin only")
            return
        
        if not allow_noise_mode():
            with _oi_lock:
                hist_len = len(_oi_history.get("BTC", deque()))
            bot.reply_to(
                m, 
                f"⏳ Not enough data for noise mode.\n"
                f"   Need 60 samples, have {hist_len}\n"
                f"   ≈ {(60 - hist_len) * 0.5:.0f} seconds remaining",
                parse_mode='HTML'
            )
            return
            
        global LOG_LEVEL
        LOG_LEVEL = "DEBUG"
        os.environ["LOG_LEVEL"] = "DEBUG"
        console.setLevel(logging.DEBUG)  # ← only console, not logger
        logger.info("🔊 NOISE MODE activated")
        
        bot.reply_to(m, "🔊 <b>DEBUG MODE</b>\n🎚️ Log level set to DEBUG\n⏱️ Will auto-reset to INFO in 5 minutes", parse_mode='HTML')
        
        # Auto-reset after 5 minutes
        def reset_to_quiet():
            time.sleep(300)
            global LOG_LEVEL
            LOG_LEVEL = "INFO"
            os.environ["LOG_LEVEL"] = "INFO"
            console.setLevel(logging.INFO)  # ← only console
            logger.info("✅ DEBUG mode reset to INFO")
        
        threading.Thread(target=reset_to_quiet, daemon=True).start()

    except Exception as e:
        logger.exception(e)
        bot.reply_to(m, f"❌ Noise failed\n{e}", parse_mode='HTML')

@bot.message_handler(commands=['trace'])
def cmd_trace(m):
    try:
        if m.from_user.id != USER_ID:
            bot.reply_to(m, "⛔ Admin only")
            return
        
        if not allow_noise_mode():
            bot.reply_to(m, "⏳ Not enough data for trace mode (need 60 samples)")
            return
        
        # ... existing trace logic
        os.environ["TRACE"] = "1"
        bot.reply_to(m, "🔍 <b>TRACE MODE</b>\n📡 Raw metrics enabled\n⏱️ Will auto-disable in 5 minutes", parse_mode='HTML')
        
        # Auto-disable after 5 minutes
        def reset_trace():
            time.sleep(300)
            os.environ["TRACE"] = "0"
            logger.info("✅ TRACE mode disabled")
        
        threading.Thread(target=reset_trace, daemon=True).start()

    except Exception as e:
        logger.exception(e)
        bot.reply_to(m, f"❌ Trace failed\n{e}", parse_mode='HTML')

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


# ============================================================
# TIER 🔴 — INSTITUTIONAL FOUNDATIONS (RED)
# ============================================================

_cycle_times: deque = deque(maxlen=20)
_cycle_lock = threading.RLock()

def ensure_signals_schema():
    """Safe schema migration with data integrity preservation."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        cols = [row[1] for row in c.fetchall()]
        
        if "final_score" not in cols:
            logger.info("📦 Migrating signals.final_score (safe mode)...")
            c.execute("ALTER TABLE signals ADD COLUMN final_score REAL DEFAULT NULL")
            c.execute("UPDATE signals SET final_score = score WHERE final_score IS NULL AND score IS NOT NULL")
            logger.info(f"✅ signals.final_score migrated ({c.rowcount} rows)")
        
        safe_columns = {
            "market_regime": "TEXT DEFAULT NULL",
            "volatility_regime": "TEXT DEFAULT NULL",
            "flow_regime": "TEXT DEFAULT NULL",
        }
        
        for col, col_def in safe_columns.items():
            if col not in cols:
                try:
                    c.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_def}")
                    logger.info(f"✅ Added: {col}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed {col}: {e}")
        
        conn.commit()
    except Exception as e:
        logger.error(f"ensure_signals_schema error: {e}")
    finally:
        if conn:
            conn.close()


def ensure_context_fields(ctx: Any) -> Any:
    """Ensure context object has all required fields with safe defaults."""
    if ctx is None:
        return ctx
    
    if isinstance(ctx, dict):
        class ContextWrapper:
            pass
        obj = ContextWrapper()
        for k, v in ctx.items():
            setattr(obj, k, v)
        ctx = obj
    
    defaults = {
        "market_state": "UNKNOWN",
        "regime": "UNKNOWN",
        "shock_score": 0.0,
        "transition_prob": 0.0,
        "tension": 0.0,
        "vol_forecast": 1.0,
        "breath_bull": 0.5,
        "breath_bear": 0.5,
        "event_risk": 0.0,
        "dominance": 50.0,
        "timestamp": time.time(),
    }
    
    for field, default in defaults.items():
        if not hasattr(ctx, field):
            setattr(ctx, field, default)
    
    if hasattr(ctx, "regime") and ctx.regime and ctx.regime != "UNKNOWN":
        ctx.market_state = ctx.regime
    elif not hasattr(ctx, "market_state") or not ctx.market_state:
        ctx.market_state = "UNKNOWN"
    
    return ctx


def get_adaptive_stale_threshold() -> int:
    """Adaptive stale threshold based on actual pipeline latency."""
    with _cycle_lock:
        if len(_cycle_times) < 5:
            return 5
        avg_cycle = sum(_cycle_times) / len(_cycle_times)
        scan_interval = TUNABLE.get("STATE_ENGINE_INTERVAL", 30)
        allowed_age = max(scan_interval * 0.75, avg_cycle * 2)
        return int(max(3, min(30, allowed_age)))


def record_cycle_time(duration: float):
    """Record a cycle duration for adaptive threshold."""
    with _cycle_lock:
        _cycle_times.append(duration)


# ============================================================
# TIER 🟡 — CONFIDENCE CALIBRATION (YELLOW)
# ============================================================

_SIGNAL_SCORE_COLUMN = "final_score"

def detect_signal_score_column() -> str:
    """Detect which column to use for signal scoring at startup."""
    global _SIGNAL_SCORE_COLUMN
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        cols = [row[1] for row in c.fetchall()]
        conn.close()
        
        if "final_score" in cols:
            _SIGNAL_SCORE_COLUMN = "final_score"
            logger.info("📊 Using signals.final_score for calibration")
        else:
            _SIGNAL_SCORE_COLUMN = "score"
            logger.info("📊 Using signals.score for calibration (legacy)")
    except Exception as e:
        logger.error(f"detect_signal_score_column error: {e}")
        _SIGNAL_SCORE_COLUMN = "score"
    
    return _SIGNAL_SCORE_COLUMN


# ============================================================
# TIER 🔵 — UNIVERSE STABILITY (BLUE)
# ============================================================

_UNIVERSE_MEMORY: deque = deque(maxlen=10)
_UNIVERSE_MEMORY_LOCK = threading.RLock()

def get_stable_universe(candidates: List[str], min_consensus: int = 3) -> List[str]:
    """Get stable universe based on consensus across multiple scans."""
    with _UNIVERSE_MEMORY_LOCK:
        _UNIVERSE_MEMORY.append(set(candidates))
        
        from collections import Counter
        counter = Counter()
        for snap in _UNIVERSE_MEMORY:
            counter.update(snap)
        
        stable = [coin for coin, count in counter.items() if count >= min_consensus]
        
        if len(stable) < 5:
            last_good = _UNIVERSE_MEMORY[-1] if _UNIVERSE_MEMORY else set()
            stable = list(last_good)
        
        logger.info(f"🌍 Stable universe: {len(stable)} coins (consensus: {min_consensus})")
        return stable[:20]


def reset_universe_memory():
    """Reset universe memory for testing or recovery."""
    with _UNIVERSE_MEMORY_LOCK:
        _UNIVERSE_MEMORY.clear()



def summary_loop():
    """Print engine summary every 10 minutes to log and optionally to Telegram."""
    while RUNTIME.is_running():
        time.sleep(600)
        if not RUNTIME.is_running():
            break
        try:
            pipe = get_pipeline_metrics()
            with _journal_lock:
                journal_size = len(_decision_journal)
            
            summary = (
                f"🧠 ENGINE SUMMARY (10m)\n"
                f"├─ scan: {pipe.get('check', 0)}\n"
                f"├─ obs_pass: {pipe.get('obs', 0)}\n"
                f"├─ thesis_pass: {pipe.get('thesis', 0)}\n"
                f"├─ conf_pass: {pipe.get('confidence', 0)}\n"
                f"└─ executed: {pipe.get('execute_pass', 0)}\n\n"
                f"📚 Journal size: {journal_size}"
            )
            logger.info(summary)
            # Optionally send to Telegram (commented out)
            # if USER_ID:
            #     bot.send_message(USER_ID, summary, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Summary loop error: {e}")

def bootstrap():
    """Proper startup order - RUN ONCE BEFORE ENGINE"""
    logger.info("🚀 BOOTSTRAP STARTING...")
    
    # ===== STEP 1: DATABASE =====
    logger.info("  ├─ Step 1/6: Database init...")
    init_db()
    ensure_signals_schema()
    detect_signal_score_column()
    
    # ===== STEP 2: RESTORE OPEN TRADES =====
    logger.info("  ├─ Step 2/6: Restoring open trades from DB...")
    restore_open_trades()
    
    # ===== STEP 2.5: MIGRATE JOURNAL (TAMBAHKAN INI!) =====
    logger.info("  ├─ Step 2.5/6: Migrating journal entries...")
    migrate_journal_entries()  # ← TAMBAHKAN BARIS INI
    
    # ===== STEP 3: AUDIT =====
    logger.info("  ├─ Step 3/6: Auditing trade state post-restore...")
    audit_result = audit_trade_state()
    logger.info(f"     db_open={audit_result['db_open']}, managed={audit_result['manager_open']}, orphan={audit_result['orphan_count']}")
    
    # Kalau masih ada orphan setelah restore → emergency archive (bukan abort)
    if audit_result["orphan_count"] > 0:
        logger.warning(f"⚠️ {audit_result['orphan_count']} orphans remain after restore — archiving...")
        try:
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE signals
                SET evaluated=1,
                    outcome='ORPHAN_RECOVERED',
                    exit_time=CAST(strftime('%s', 'now') AS INTEGER)
                WHERE evaluated=0
                  AND signal_id NOT IN ({})
            """.format(
                ",".join(f"'{sid}'" for sid in TRADE_MANAGER.positions.keys()) or "'__none__'"
            ))
            conn.commit()
            archived = cursor.rowcount
            conn.close()
            logger.warning(f"🔄 Archived {archived} orphan trades → continuing startup")
        except Exception as e:
            logger.error(f"Emergency orphan archive error: {e}")
    
    # Hard abort hanya kalau orphan masih ada DAN sangat ekstrem (DB corruption suspected)
    post_orphan = audit_trade_state()["orphan_count"]
    if post_orphan > 500:
        logger.critical(f"🔴 BOOT ABORT: {post_orphan} orphans still present after cleanup — DB corruption suspected")
        sys.exit(1)
    
    # ===== STEP 4: MARKET DATA =====
    logger.info("  ├─ Step 4/6: Fetching market data...")
    snapshot = refresh_snapshot()
    if snapshot:
        sanitize_maps_from_snapshot(snapshot)
    
    # ===== STEP 5: WARMUP HISTORIES =====
    logger.info("  ├─ Step 5/6: Warming up histories...")
    for i in range(5):
        refresh_snapshot()
        logger.debug(f"     Warmup {i+1}/5")
        time.sleep(0.5)
    
    # ===== STEP 6: START ENGINE =====
    logger.info("  └─ Step 6/6: Starting engine threads...")
    
    logger.info("✅ BOOTSTRAP COMPLETE")

def restore_open_trades():
    """Restore open trades from DB into TradeManager at startup"""
    try:
        conn = db_connect()  # pakai db_connect() biar WAL + timeout aktif
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0")
        total_in_db = cursor.fetchone()[0]
        logger.info(f"  ├─ RESTORE: {total_in_db} open trades in DB")
        
        cursor.execute("""
            SELECT signal_id, coin, direction, entry_price, sl_price, timestamp
            FROM signals WHERE evaluated=0
        """)
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            logger.info("  └─ No open trades to restore")
            return
        
        restored = 0
        skipped = 0
        failed = 0
        
        for signal_id, coin, direction, entry, sl, ts in rows:
            try:
                if signal_id in TRADE_MANAGER.positions:
                    skipped += 1
                    continue
                
                # Guard: skip row kalau data corrupt
                if not coin or not direction or not entry or not sl:
                    logger.warning(f"  │  ⚠️ RESTORE SKIP corrupt row: {signal_id}")
                    failed += 1
                    continue
                
                entry = float(entry)
                sl = float(sl)
                
                atr_pct = get_atr_pct(coin, 14, "1h") or 2.0
                regime = get_market_regime()
                targets = calculate_scaled_targets(entry, direction, atr_pct, regime)
                
                TRADE_MANAGER.add_position(
                    signal_id=signal_id,
                    coin=coin,
                    direction=direction,
                    entry=entry,
                    sl=sl,
                    tp_targets=targets,
                    entry_time=float(ts) if ts else time.time()
                )
                restored += 1
                
            except Exception as row_err:
                failed += 1
                logger.error(f"  │  ❌ RESTORE FAIL {signal_id}: {row_err}")
        
        logger.info(f"  └─ RESTORE DONE: restored={restored} skipped={skipped} failed={failed} / total={total_in_db}")
        
    except Exception as e:
        logger.error(f"restore_open_trades error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    args = parse_args()
    PAPER_MODE = args.paper
    
    logger.info(f"Starting Smart Entry Engine V10 - {'PAPER' if PAPER_MODE else 'LIVE'} mode")
    logger.info("🔥 P1+P2 FIX ACTIVE: Scaling Exit Engine + Adaptive Threshold")
    
    # ===== BOOTSTRAP =====
    bootstrap()
    
    # ===== SIGNAL HANDLERS =====
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # ===== START THREADS =====
    threads = [
        threading.Thread(target=scheduled_state_engine_v11, daemon=True),
        threading.Thread(target=scheduled_trigger_engine_v7, daemon=True),
        threading.Thread(target=scheduled_shadow_evaluation_v7, daemon=True),
        threading.Thread(target=scheduled_cleanup_v7, daemon=True),
        threading.Thread(target=monitor_pending_setups_v6, daemon=True),
        threading.Thread(target=cleanup_memory_v10, daemon=True, name="mem_cleanup"),
        threading.Thread(target=_db_writer_loop, daemon=True, name="db_writer"),
        threading.Thread(target=log_snapshot_metrics, daemon=True, name="metrics_logger"),
        threading.Thread(target=summary_loop, daemon=True, name="summary"),
    ]
    for t in threads:
        t.start()
    
    # ===== START POLLING (SATU WHILE LOOP AJA) =====
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


# ============================================================
# GUARDRAIL SYSTEM: ADAPTIVE FUNCTIONS (V10)
# ============================================================

def get_dynamic_min_volume(vols: List[float], breath: Dict[str, float]) -> float:
    """Hybrid: percentile-based + market regime floor"""
    if not vols:
        return 5_000_000
    p35 = np.percentile(vols, 35)
    participation = breath.get("participation", 0.5)
    if participation < 0.3:
        floor = 500_000
    elif participation < 0.5:
        floor = 1_000_000
    elif participation < 0.7:
        floor = 2_500_000
    else:
        floor = 5_000_000
    min_vol = max(p35, floor)
    return max(500_000, min(20_000_000, min_vol))


def get_top_coins_by_volume_dynamic(limit: int = 12) -> List[str]:
    """Wrapper untuk get_top_coins_by_volume dengan default params."""
    return get_top_coins_by_volume(limit=limit, min_vol=5_000_000)


def get_universe_v10(limit: int = 15) -> List[str]:
    """60% dynamic scan + 40% historical universe"""
    dynamic = get_top_coins_by_volume_dynamic(limit=int(limit * 0.6))
    historical = get_last_good_universe(limit=int(limit * 0.4))
    result = []
    seen = set()
    for coin in dynamic:
        if coin not in seen and len(result) < limit:
            result.append(coin)
            seen.add(coin)
    for coin in historical:
        if coin not in seen and len(result) < limit:
            result.append(coin)
            seen.add(coin)
    if len(result) < 5:
        fallback = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "LINK", "UNI", "AAVE"]
        for coin in fallback:
            if coin not in seen and len(result) < limit:
                result.append(coin)
                seen.add(coin)
    logger.info(f"Universe: {len(result)} coins ({len(dynamic)} dynamic + {len(historical)} historical)")
    return result[:limit]


def get_last_good_universe(limit: int = 8) -> List[str]:
    """Get historical universe from trades"""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT coin FROM journal WHERE timestamp > ? GROUP BY coin ORDER BY COUNT(*) DESC LIMIT ?", 
                  (int(time.time()) - 86400 * 3, limit))
        rows = c.fetchall()
        conn.close()
        return [r[0] for r in rows] if rows else []
    except:
        return []


def get_dynamic_candidate_limit(breath: Dict[str, float], entropy: int) -> int:
    """Adaptive candidate limit: participation + entropy"""
    participation = breath.get("participation", 0.5)
    base = int(participation * 30)
    entropy_adjustment = max(0, int((100 - entropy) / 10))
    limit = base + entropy_adjustment
    return max(10, min(35, limit))


_warmup_state = {
    "is_warmup": True,
    "confidence": 0.0,
    "last_check": 0,
    "delta_points": 0,
    "oi_points": 0,
    "snapshot_age": 999,
    "data_confidence": 0,
}
_warmup_lock = threading.RLock()

# ===== SHADOW TRADING STATS =====
_shadow_stats = {
    "total": 0,
    "wins": 0,
    "losses": 0,
    "pnl": [],
    "coins": {},
    "results": deque(maxlen=200),
}
_shadow_stats_lock = threading.RLock()

def is_warmup() -> bool:
    """Data-driven warmup with decay - can re-enter if data degrades"""
    with _warmup_lock:
        now = time.time()
        if now - _warmup_state["last_check"] > 10:
            delta_raw = _rolling_delta.get("BTC", deque())
            _warmup_state["delta_points"] = len(delta_raw)
            oi_raw = _oi_history.get("BTC", deque())
            _warmup_state["oi_points"] = len(oi_raw)
            snapshot = get_snapshot()
            if snapshot:
                _warmup_state["snapshot_age"] = now - snapshot.timestamp
                _warmup_state["data_confidence"], _ = get_data_confidence("BTC", now)
            delta_score = min(1.0, _warmup_state["delta_points"] / 20)
            oi_score = min(1.0, _warmup_state["oi_points"] / 20)
            snapshot_score = 1.0 if _warmup_state["snapshot_age"] < 60 else max(0, 1.0 - (_warmup_state["snapshot_age"] - 60) / 60)
            confidence = 0.5 * delta_score + 0.2 * oi_score + 0.3 * snapshot_score
            _warmup_state["confidence"] = confidence * 100
            _warmup_state["last_check"] = now
        return _warmup_state["confidence"] < 40


def get_warmup_status() -> Dict[str, Any]:
    """Get detailed warmup status for UI"""
    with _warmup_lock:
        return {
            "is_warmup": is_warmup(),
            "confidence": _warmup_state["confidence"],
            "delta_points": _warmup_state["delta_points"],
            "oi_points": _warmup_state["oi_points"],
            "snapshot_age": _warmup_state["snapshot_age"],
            "data_confidence": _warmup_state["data_confidence"],
        }


def get_dynamic_journal_max() -> int:
    """Dynamic journal size with buffer"""
    with _active_candidates_lock:
        active_coins = len(_active_candidates)
    max_size = max(
        TUNABLE["JOURNAL_MAX_BASE"],
        active_coins * TUNABLE["JOURNAL_MAX_PER_COIN"]
    )
    return min(TUNABLE["JOURNAL_MAX_ABS"], max_size)


_drift_ema_history: Dict[str, deque] = {}
_drift_ema_lock = threading.RLock()

def get_dynamic_drift_threshold(coin: str) -> Tuple[float, float]:
    """Adaptive drift thresholds using EMA + percentile"""
    with _drift_ema_lock:
        if coin not in _drift_ema_history:
            _drift_ema_history[coin] = deque(maxlen=100)
        ema_history = list(_drift_ema_history[coin])
        if len(ema_history) < 20:
            return 0.7, 0.5
        arr = np.array(ema_history)
        p90 = np.percentile(arr, 90)
        p70 = np.percentile(arr, 70)
        watch_threshold = max(0.3, min(0.9, p90))
        observe_threshold = max(0.2, min(0.7, p70))
        if watch_threshold - observe_threshold < 0.15:
            observe_threshold = max(0.2, watch_threshold - 0.15)
        return watch_threshold, observe_threshold


def update_drift_ema(coin: str, drift: float):
    """Update EMA history for drift"""
    with _drift_ema_lock:
        if coin not in _drift_ema_history:
            _drift_ema_history[coin] = deque(maxlen=100)
        prev = _drift_ema_history[coin][-1] if _drift_ema_history[coin] else drift
        ema = 0.3 * drift + 0.7 * prev
        _drift_ema_history[coin].append(ema)


def get_adaptive_clip(coin: str, values: list) -> Tuple[float, float]:
    """Adaptive clipping with floor"""
    if len(values) < 10:
        return 0.002, 0.1
    arr = np.array(values)
    low = np.percentile(arr, 5)
    high = np.percentile(arr, 95)
    low = max(low, 0.002)
    high = min(high, 0.15)
    if high - low < 0.01:
        low = max(0.001, low - 0.005)
        high = min(0.2, high + 0.005)
    return low, high


def get_dynamic_entry_threshold(coin: str, fatigue: float, volatility_regime: str, entropy_market: int) -> int:
    """Runtime-based entry threshold"""
    base = 70
    fatigue_penalty = int((1.0 - fatigue) * 15)
    vol_penalty = 10 if volatility_regime == "HIGH_VOLATILITY" else (-5 if volatility_regime == "LOW_VOLATILITY" else 0)
    entropy_penalty = int(entropy_market / 10)
    threshold = base + fatigue_penalty + vol_penalty + entropy_penalty
    return max(55, min(85, threshold))


def get_dynamic_cooldown(coin: str, alert: dict) -> int:
    """Adaptive cooldown: ATR% + event risk + density"""
    atr_pct = get_atr_pct(coin, 14, "1h")
    if atr_pct > 3:
        base = 120
    elif atr_pct > 1.5:
        base = 200
    else:
        base = 300
    event_importance = alert.get("event_importance", 0)
    if event_importance > 70:
        base = int(base * 1.5)
    elif event_importance > 40:
        base = int(base * 1.2)
    with _alert_history_lock:
        now = time.time()
        recent_alerts = [t for t in _alert_history.get(coin, []) if now - t < 3600]
        density = len(recent_alerts) / 3
    density_mult = 1.0 + density * 0.3
    mode = alert.get("execution_mode_v10", "NORMAL")
    mode_mult = {"NORMAL": 1.0, "PREPARE": 0.7, "CAUTIOUS": 1.3, "AGGRESSIVE": 0.5, "DEFENSIVE": 2.0}.get(mode, 1.0)
    cooldown = int(base * density_mult * mode_mult)
    return max(120, min(1800, cooldown))


def get_dynamic_lookback(coin: str, base: int = 5, min_lookback: int = 2, max_lookback: int = 15) -> int:
    """Dynamic lookback based on ATR volatility"""
    atr_pct = get_atr_pct(coin, 14, "1h")
    if atr_pct > 3:
        return max(min_lookback, base - 3)
    elif atr_pct > 1.5:
        return base
    else:
        return min(max_lookback, base + 5)


def decay_coin_memory(coin: str, decay_hours: float = 12):
    """Decay coin memory over time"""
    with _belief_state_lock:
        if coin in _belief_state:
            data = _belief_state[coin]
            age_hours = (time.time() - data["since"]) / 3600
            decay = np.exp(-age_hours / decay_hours)
            data["score"] = data.get("score", 0) * decay
            if data["score"] < 5:
                data["state"] = BeliefState.SEEKING
                data["score"] = 0.0
                data["since"] = time.time()


def get_dynamic_event_ttl(event: EventRisk) -> float:
    """Dynamic event TTL based on importance and market reaction"""
    if event.importance > 70:
        base_hours = 6
    elif event.importance > 40:
        base_hours = 3
    else:
        base_hours = 1
    reaction = get_current_reaction()
    if reaction and reaction.event == event.label:
        if reaction.absorption > 0.7:
            return base_hours * 1.5
        elif reaction.absorption < 0.3:
            return base_hours * 0.5
    return base_hours

