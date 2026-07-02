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
import shutil
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
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any, Callable
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from queue import Queue, Empty
from queue import Queue as _Queue

import asyncio
import websockets

import telebot
import numpy as np
import warnings

# ===== FIX: SUPPRESS BENIGN NUMPY WARNINGS DURING BOOTSTRAP WARM-UP =====
# "Mean of empty slice" & "invalid value encountered in scalar divide" muncul
# pas OI/candle history masih kosong di awal boot. Semua consumer array ini
# udah guard len()<N di code, jadi warning ini kosmetik doang — bukan bug.
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="invalid value encountered in scalar divide")
warnings.filterwarnings("ignore", message="invalid value encountered in divide")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ============================================================
# P1+P2 FIX IMPORTS – SCALING EXIT + ADAPTIVE THRESHOLD
# ============================================================
from dataclasses import dataclass as p1p2_dataclass

# ============================================================
# P3.1 — EXIT EFFICIENCY (MINIMAL)
# ============================================================

def compute_exit_eff(realized_pnl: float, mfe: float) -> Optional[float]:
    """
    Exit efficiency = realized / max(MFE, 0.01)
    Returns None if MFE is 0 or negative (never went positive).
    """
    if not mfe or mfe <= 0:
        return None
    eff = (realized_pnl / mfe) * 100
    return round(max(0, min(100, eff)), 1)


def get_exit_eff_label(eff: Optional[float]) -> str:
    """Get emoji label for exit efficiency."""
    if eff is None:
        return "⚪"
    if eff >= 70:
        return "🚀"
    if eff >= 40:
        return "⚖️"
    return "🐢"


# ============================================================
# P4.4a — OUTCOME AUTHORITY (SHADOW MODE)
# ============================================================
# TIDAK overwrite historical outcome. Cuma audit + log mismatch.
# ============================================================

def compute_outcome_from_path(
    entry: float,
    sl: float,
    tp: float,
    direction: str,
    mfe: float,
    mae: float,
    exit_price: float,
    exit_reason: str = "",
) -> Dict[str, Any]:
    """
    Compute outcome berdasarkan price PATH (via MFE/MAE), bukan hanya exit_price.
    Shadow mode: only used for auditing, never overwrites DB.
    """
    if exit_reason in ("stale_expiry", "STALE_EXPIRY"):
        return {"outcome": "STALE_EXPIRY", "pnl": 0.0, "reason": "stale"}
    if exit_reason in ("timeout", "timeout_tp2"):
        return {"outcome": "TIMEOUT", "pnl": 0.0, "reason": "timeout"}

    denom = max(entry, 0.01)
    if direction == "LONG":
        tp_pct  = (tp - entry) / denom * 100
        sl_pct  = (sl - entry) / denom * 100
        touched_tp = mfe >= tp_pct * 0.98
        touched_sl = mae <= sl_pct * 1.02
    else:
        tp_pct  = (entry - tp) / denom * 100
        sl_pct  = (entry - sl) / denom * 100
        touched_tp = mfe >= tp_pct * 0.98
        touched_sl = mae <= sl_pct * 1.02

    if touched_tp:
        pnl = tp_pct if direction == "LONG" else (entry - tp) / denom * 100
        return {"outcome": "TP_HIT", "pnl": round(pnl, 4), "reason": "tp_touched"}
    if touched_sl:
        pnl = sl_pct if direction == "LONG" else (entry - sl) / denom * 100
        return {"outcome": "SL_HIT", "pnl": round(pnl, 4), "reason": "sl_touched"}

    if direction == "LONG":
        pnl = (exit_price - entry) / denom * 100
    else:
        pnl = (entry - exit_price) / denom * 100

    if pnl > 0.01:
        return {"outcome": "PARTIAL_WIN",  "pnl": round(pnl, 4), "reason": "partial_win"}
    elif pnl < -0.01:
        return {"outcome": "PARTIAL_LOSS", "pnl": round(pnl, 4), "reason": "partial_loss"}
    else:
        return {"outcome": "BREAK_EVEN",   "pnl": round(pnl, 4), "reason": "break_even"}


def audit_outcome_authority(signal_id: str) -> Dict[str, Any]:
    """
    Audit: compare stored outcome vs path-computed outcome.
    SHADOW MODE — TIDAK overwrite, cuma log mismatch.
    """
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT outcome, pnl, entry_price, sl_price, tp_price, direction,
                   mfe, mae, exit_price
            FROM signals WHERE signal_id = ?
        """, (signal_id,))
        row = c.fetchone()
        conn.close()
        conn = None
        if not row:
            return {"error": "not_found"}
        stored_outcome, stored_pnl, entry, sl, tp, direction, mfe, mae, exit_price = row
        if None in (entry, sl, tp, direction, mfe, mae, exit_price):
            return {"error": "incomplete_data"}
        computed = compute_outcome_from_path(
            entry=entry, sl=sl, tp=tp, direction=direction,
            mfe=mfe, mae=mae, exit_price=exit_price,
            exit_reason="",
        )
        is_match = stored_outcome == computed["outcome"]
        if not is_match:
            logger.warning(
                f"OUTCOME_AUTHORITY {signal_id}: "
                f"stored={stored_outcome} → computed={computed['outcome']} "
                f"(entry={entry:.4f} exit={exit_price:.4f} "
                f"mfe={mfe:.2f}% mae={mae:.2f}%)"
            )
        return {
            "signal_id": signal_id,
            "stored": stored_outcome,
            "computed": computed["outcome"],
            "match": is_match,
            "stored_pnl": stored_pnl,
            "computed_pnl": computed["pnl"],
            "reason": computed["reason"],
        }
    except Exception as e:
        logger.error(f"audit_outcome_authority error: {e}")
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()


def batch_audit_outcomes(limit: int = 1000) -> Dict[str, Any]:
    """Audit all evaluated outcomes in DB (shadow, no writes)."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT signal_id FROM signals
            WHERE evaluated = 1 AND outcome IS NOT NULL
            ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        conn = None
        results = []
        mismatches = 0
        for (sid,) in rows:
            r = audit_outcome_authority(sid)
            if r.get("error"):
                continue
            results.append(r)
            if not r["match"]:
                mismatches += 1
        total = len(results)
        mismatch_pct = (mismatches / total * 100) if total > 0 else 0
        logger.info(
            f"OUTCOME_AUDIT_BATCH: total={total} "
            f"mismatches={mismatches} ({mismatch_pct:.1f}%)"
        )
        return {
            "total": total,
            "mismatches": mismatches,
            "mismatch_pct": mismatch_pct,
            "results": results[:50],
        }
    except Exception as e:
        logger.error(f"batch_audit_outcomes error: {e}")
        return {"error": str(e)}
    finally:
        if conn:
            conn.close()

# ============================================================
# END P4.4a
# ============================================================

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
    "ENABLE_STALE_CLEANUP": True,    # P4.x: enabled — orphan=0, open=4, audits clean
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
    "ADAPTIVE_MAX_RELAX": 10,
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
    # ===== P4.x FEATURE GATES =====
    "ALLOW_WEAK_STRUCTURE": True,    # soft-pass structure + penalty -20 (was hard reject)
    "MICRO_SOFT_PASS": True,         # micro gate: score 30-59 = warn+continue, not block
    "MICRO_SOFT_THRESHOLD": 30,      # minimum micro score untuk soft-pass
}

# ============================================================
# P4.51 — LOG LAYERING (UI CLEANUP FINAL)
# ============================================================
LOG_LAYER = {
    "OPERATOR": 1,      # /status, /dashboard — minimal
    "RUNTIME": 2,       # ENGINE SUMMARY, FUNNEL — DEFAULT
    "DEVELOPER": 3,     # VELOCITY_TRACE, STRUCT_RESULT
    "RESEARCH": 4,      # DIS_DEBUG, dynamic coins
    "EXPERIMENT": 5,    # TM_SAMPLE, OI_HISTORY spam
}

_active_log_layer = LOG_LAYER["RUNTIME"]
_layer_lock = threading.RLock()

def set_log_layer(layer: str):
    """Set active log layer."""
    global _active_log_layer
    with _layer_lock:
        _active_log_layer = LOG_LAYER.get(layer.upper(), LOG_LAYER["RUNTIME"])
        logger.info(f"📊 LOG LAYER: {layer.upper()}")

def should_log(layer: str) -> bool:
    """Check if a log line should be emitted."""
    with _layer_lock:
        return LOG_LAYER.get(layer.upper(), 999) <= _active_log_layer


# ============================================================
# COLLAPSED LOGS — GANTI SPAM DENGAN RINGKASAN
# ============================================================

def log_dynamic_coins_summary(added: int, removed: int, total: int):
    """🗺️ MAP UPDATED — ringkas."""
    if should_log("RUNTIME"):
        logger.info(f"🗺️ MAP updated +{added} −{removed} ={total}")

def log_trade_manager_summary():
    """📂 TM — ringkas."""
    if should_log("RUNTIME"):
        with TRADE_MANAGER._lock:
            open_count = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
            closed_count = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "CLOSED")
        logger.info(f"📂 TM OPEN={open_count} CLOSED={closed_count}")

def log_oi_summary():
    """🧠 OI_BUFFER — ringkas."""
    if should_log("RUNTIME"):
        with _oi_lock:
            total = len(_oi_history)
            # FIX: dulu hardcode BTC doang buat sample depth. Sekarang
            # ambil coin dengan history TERDALAM (paling lama ke-track)
            # secara dinamis — representasi warm-up progress yang lebih
            # jujur, gak asumsi BTC selalu paling relevan buat ditampilin.
            if _oi_history:
                _deepest_coin, _deepest_hist = max(_oi_history.items(), key=lambda kv: len(kv[1]))
                deepest_len = len(_deepest_hist)
            else:
                _deepest_coin, deepest_len = "n/a", 0
            oldest = min((ts for hist in _oi_history.values() for ts, _ in hist), default=0)
            age_min = (time.time() - oldest) / 60 if oldest else 0
        logger.info(f"🧠 OI {total} coins | {_deepest_coin}={deepest_len} | age={age_min:.0f}m")

# Cache untuk dislocation summary
_dislocation_cache = {}
_dislocation_cache_lock = threading.RLock()

def log_dislocation_summary(coin: str, score: float, dis: float, growth: float):
    """🧭 DISCOVERY — ringkas (summary setiap 10 coin)."""
    with _dislocation_cache_lock:
        _dislocation_cache[coin] = (score, dis, growth)
        if len(_dislocation_cache) % 10 == 0 and should_log("RUNTIME"):
            top = sorted(_dislocation_cache.items(), key=lambda x: x[1][0], reverse=True)[:3]
            best = [f"{c} +{s[2]:.1f}%" for c, s in top if s[2] > 0]
            logger.info(f"🧭 DISCOVERY best: {' '.join(best)}" if best else "🧭 DISCOVERY none")

def log_funnel_compact():
    """📊 FLOW — scan→obs→thesis→conf→exec dengan bar chart."""
    if should_log("OPERATOR"):
        pipe = get_pipeline_metrics()
        scan = pipe.get('check', 0)
        obs = pipe.get('obs', 0)
        thesis = pipe.get('thesis', 0)
        conf = pipe.get('confidence', 0)
        exec_count = pipe.get('execute_pass', 0)
        
        bar_len = 10
        def bar(v, total):
            if total == 0:
                return "░" * bar_len
            filled = int((v / total) * bar_len)
            return "█" * filled + "░" * (bar_len - filled)
        
        max_val = max(scan, obs, thesis, conf, exec_count, 1)
        logger.info(f"📊 FLOW")
        logger.info(f"   scan {bar(scan, max_val)} {scan}")
        logger.info(f"   obs  {bar(obs, max_val)} {obs}")
        logger.info(f"   th   {bar(thesis, max_val)} {thesis}")
        logger.info(f"   conf {bar(conf, max_val)} {conf}")
        logger.info(f"   exec {bar(exec_count, max_val)} {exec_count}")

def log_velocity_trace_compact(coin: str, decision: str, score: float, threshold: float):
    """⚡ VELOCITY — 1 line, readable."""
    if should_log("DEVELOPER"):
        if score is not None and threshold is not None:
            gap = score - threshold
            emoji = "✅" if gap >= 0 else ("⚪" if gap >= -5 else "❌")
            logger.info(f"⚡ {coin} {decision} {emoji} score={score:.0f} th={threshold:.0f} gap={gap:+.0f}")
        else:
            logger.info(f"⚡ {coin} {decision} (no score)")

def log_engine_summary_compact():
    """🧠 SUMMARY — 1 line."""
    if should_log("OPERATOR"):
        pipe = get_pipeline_metrics()
        with _journal_lock:
            journal_size = len(_decision_journal)
        
        scan = pipe.get('check', 0)
        obs = pipe.get('obs', 0)
        exec_count = pipe.get('execute_pass', 0)
        
        if exec_count == 0 and obs > 20:
            status = "🟡 blocked"
        elif obs / max(scan, 1) < 0.1 and scan > 50:
            status = "🔵 selective"
        elif exec_count / max(obs, 1) > 0.3:
            status = "🟢 active"
        else:
            status = "⚪ scanning"
        
        logger.info(f"🧠 {scan}→{obs}→{exec_count} | journal={journal_size} | {status}")

# ============================================================
# V2 STRUCTURE ENGINE — ROLLING HISTORIES
# ============================================================
# Rolling metrics untuk scoring dinamis per coin (dipakai detector _v2).
# Semua threshold statis diganti dengan percentile + zscore.
# NAMESPACE: semua fungsi/helper V2 detector pakai suffix _v2, KECUALI
# util generik (pct_score, z_score, combined_score) yang gak collide
# dengan apapun di file ini (dicek sebelum implementasi).

_wall_history: Dict[str, deque] = {}
_gap_history: Dict[str, deque] = {}
_fill_history: Dict[str, deque] = {}
_depth_history: Dict[str, deque] = {}
_depth_recovery_history: Dict[str, deque] = {}
_sweep_vol_history: Dict[str, deque] = {}
_ob_reaction_history: Dict[str, deque] = {}
_ob_vol_history: Dict[str, deque] = {}
_fvg_flow_cvd_history: Dict[str, deque] = {}

_rolling_histories_lock = threading.RLock()

def update_metric(store: Dict[str, deque], coin: str, value: float, maxlen: int = 100):
    """Update rolling metric for a coin."""
    with _rolling_histories_lock:
        if coin not in store:
            store[coin] = deque(maxlen=maxlen)
        store[coin].append(value)

def get_metric_history(store: Dict[str, deque], coin: str) -> deque:
    """Get metric history for a coin."""
    with _rolling_histories_lock:
        return store.get(coin, deque())

# ============================================================
# MARKET DNA — STATE VECTOR (Continuous, bukan kategori)
# ============================================================
# Filosofi: Engine membaca angka, manusia membaca label.
# Semua komponen 0-100 kecuali directionality (-100..+100).
#
# PENTING: ini BUKAN pengganti get_market_regime()/get_volatility_regime()/
# get_flow_regime() — tiga fungsi itu dipakai 17+ call site (target sizing,
# adaptive threshold, trailing exit, journaling) dan TIDAK disentuh sama
# sekali. MarketStateVector adalah layer BARU, dipakai khusus oleh Context
# Engine untuk mengubah .strength event SEBELUM masuk score_event_non_additive()
# (evidence engine, tetap jadi hakim terakhir — tidak diubah).
#
# Pipeline: raw_strength -> Context Engine -> context_strength ->
#           score_event_non_additive() -> final_score -> Thesis
# ============================================================

@dataclass
class MarketStateVector:
    """Market DNA — state vector untuk decision making."""

    # Core components (0-100)
    momentum: float          # Trend strength (0-100)
    compression: float       # Range compression / squeeze (0-100)
    shock: float            # Market shock/stress (0-100)
    entropy: float          # Market chaos (0-100)
    participation: float    # Breadth participation (0-100)
    tension: float          # Tension (0-100)

    # Directional ( -100 .. +100 )
    directionality: float   # -100 = strong down, +100 = strong up

    # Derived (0-100)
    volatility: float       # Volatility level
    liquidity: float        # Liquidity depth (inverse of spread)

    # Metadata
    confidence: float       # How reliable is this state (0-100)
    timestamp: float
    regime_label: str = ""  # Hanya untuk display — BUKAN dipakai keputusan

    def to_dict(self) -> Dict[str, float]:
        return {
            "momentum": self.momentum,
            "compression": self.compression,
            "shock": self.shock,
            "entropy": self.entropy,
            "participation": self.participation,
            "tension": self.tension,
            "directionality": self.directionality,
            "volatility": self.volatility,
            "liquidity": self.liquidity,
            "confidence": self.confidence,
        }

    def get_regime_label(self) -> str:
        """Human-readable label — HANYA untuk display (/marketdna, log).
        Tidak dipakai untuk keputusan; keputusan pakai raw dimensions."""
        if self.shock > 70:
            return "PANIC"
        if self.momentum > 70 and self.directionality > 30:
            return "TRENDING_UP"
        if self.momentum > 70 and self.directionality < -30:
            return "TRENDING_DOWN"
        if self.compression > 70 and self.entropy < 30:
            return "SQUEEZE"
        if self.participation < 30:
            return "CONTRACTION"
        if self.directionality > 20 and self.compression < 40:
            return "ACCUMULATION"
        if self.directionality < -20 and self.compression < 40:
            return "DISTRIBUTION"
        if abs(self.directionality) < 20 and self.entropy > 50:
            return "CHAOS"
        return "RANGING"


def compute_market_state_vector(coin: str = "BTC") -> MarketStateVector:
    """Compute market DNA state vector dari data yang sudah ada.
    Dipanggil dari observe_market() (Tier 3 — sudah lolos Attention filter,
    REST access legitimate di titik ini, TIDAK melanggar Tier 1 cheap-only
    contract yang berlaku untuk Attention Engine)."""

    # ===== 1. MOMENTUM =====
    try:
        candles_4h = get_candles(coin, "4h", 30)
        if candles_4h and len(candles_4h) >= 15:
            closes = [float(c['c']) for c in candles_4h[-15:]]
            ema8 = np.mean(closes[-8:])
            ema15 = np.mean(closes[-15:])
            momentum = max(0, min(100, (ema8 - ema15) / max(ema15, 0.01) * 100 * 10 + 50))
        else:
            momentum = 50
    except Exception:
        momentum = 50

    # ===== 2. COMPRESSION =====
    try:
        candles_1h = get_candles(coin, "1h", 50)
        if candles_1h and len(candles_1h) >= 20:
            ranges = [float(c['h']) - float(c['l']) for c in candles_1h[-20:]]
            range_avg = sum(ranges) / len(ranges)
            current_range = ranges[-1] if ranges else 1
            if range_avg > 0:
                compression = 100 - min(100, (current_range / range_avg) * 100)
            else:
                compression = 50
            closes = [float(c['c']) for c in candles_1h[-20:]]
            if len(closes) >= 10:
                mean = np.mean(closes)
                std = np.std(closes)
                if mean > 0:
                    bb_width = (2 * std / mean) * 100
                    compression = (compression + (100 - min(100, bb_width * 50))) / 2
        else:
            compression = 50
    except Exception:
        compression = 50

    # ===== 3. SHOCK =====
    try:
        shock = compute_shock_score(coin)
        shock = max(0, min(100, shock))
    except Exception:
        shock = 50

    # ===== 4. ENTROPY =====
    try:
        entropy = compute_market_entropy_v7(coin, None)
        entropy = max(0, min(100, entropy))
    except Exception:
        entropy = 50

    # ===== 5. PARTICIPATION ===== (market-wide breadth, bukan per-coin)
    try:
        breath = compute_market_breath_v10()
        participation = breath.get("participation", 0.5) * 100
    except Exception:
        participation = 50

    # ===== 6. TENSION =====
    try:
        tension = compute_market_tension(coin)
        tension = max(0, min(100, tension))
    except Exception:
        tension = 50

    # ===== 7. DIRECTIONALITY =====
    try:
        candles_4h = get_candles(coin, "4h", 30)
        if candles_4h and len(candles_4h) >= 10:
            closes = [float(c['c']) for c in candles_4h[-10:]]
            x = list(range(len(closes)))
            n = len(x)
            sum_x = sum(x)
            sum_y = sum(closes)
            sum_xy = sum(x[i] * closes[i] for i in range(n))
            sum_xx = sum(x[i] * x[i] for i in range(n))
            denom = n * sum_xx - sum_x * sum_x
            if denom != 0:
                slope = (n * sum_xy - sum_x * sum_y) / denom
                price = closes[-1]
                slope_pct = (slope / price) * 100 if price > 0 else 0
                directionality = max(-100, min(100, slope_pct * 200))
            else:
                directionality = 0
        else:
            directionality = 0
    except Exception:
        directionality = 0

    # ===== 8. VOLATILITY =====
    try:
        atr = get_atr_pct(coin, 14, "1h")
        volatility = min(100, atr * 20)
    except Exception:
        volatility = 50

    # ===== 9. LIQUIDITY =====
    try:
        snapshot = get_snapshot()
        if snapshot and coin in snapshot.oi:
            oi = snapshot.oi[coin]
            liquidity = min(100, (oi / 100) * 100)
        else:
            liquidity = 50
    except Exception:
        liquidity = 50

    state = MarketStateVector(
        momentum=round(momentum, 1),
        compression=round(compression, 1),
        shock=round(shock, 1),
        entropy=round(entropy, 1),
        participation=round(participation, 1),
        tension=round(tension, 1),
        directionality=round(directionality, 1),
        volatility=round(volatility, 1),
        liquidity=round(liquidity, 1),
        confidence=70.0,
        timestamp=time.time(),
        regime_label=""
    )
    state.regime_label = state.get_regime_label()

    return state


# ============================================================
# DETECTOR DISTRIBUTION — OBSERVATION ONLY
# ============================================================
# Tracking untuk dashboard/analytics, BUKAN untuk decision making

_detector_distributions: Dict[str, deque] = {}
_detector_dist_lock = threading.RLock()
_DETECTOR_DIST_MAXLEN = 300

def update_detector_distribution(detector_type: str, strength: float):
    """Update rolling distribution untuk observasi (bukan keputusan)."""
    with _detector_dist_lock:
        if detector_type not in _detector_distributions:
            _detector_distributions[detector_type] = deque(maxlen=_DETECTOR_DIST_MAXLEN)
        _detector_distributions[detector_type].append(strength)

def get_detector_percentile(detector_type: str, percentile: float = 0.70) -> Optional[float]:
    """Get percentile dari rolling distribution — untuk OBSERVASI saja."""
    with _detector_dist_lock:
        if detector_type not in _detector_distributions:
            return None
        hist = list(_detector_distributions[detector_type])
        if len(hist) < 20:
            return None
        sorted_hist = sorted(hist)
        idx = int(len(sorted_hist) * percentile)
        return sorted_hist[idx]

def get_detector_stats(detector_type: str) -> Dict[str, Any]:
    """Stats untuk dashboard — OBSERVASI saja."""
    with _detector_dist_lock:
        if detector_type not in _detector_distributions:
            return {"n": 0}
        hist = list(_detector_distributions[detector_type])
        if not hist:
            return {"n": 0}
        sorted_hist = sorted(hist)
        n = len(sorted_hist)
        return {
            "n": n,
            "min": round(sorted_hist[0], 1),
            "max": round(sorted_hist[-1], 1),
            "p50": round(sorted_hist[int(n * 0.50)], 1),
            "p70": round(sorted_hist[int(n * 0.70)], 1),
            "p90": round(sorted_hist[int(n * 0.90)], 1),
            "mean": round(sum(hist) / n, 1),
        }


# ============================================================
# CONTEXT ENGINE — Adaptive Interpretation (Phase B)
# ============================================================
# Mengubah .strength (dan HANYA .strength/.confidence) event berdasarkan
# MarketStateVector. Field lain (.score, .mid, .first_seen, dll) TIDAK
# disentuh — event yang di-return tetap object yang SAMA (mutate in place),
# BUKAN reconstruct baru, supaya .mid (di-set oleh cluster_events) dan
# .score (belum di-set sampai score_event_non_additive jalan di build_thesis)
# tidak pernah ke-drop diam-diam.
#
# CONTEXT_ENGINE_ENABLED flag — kalau False, contextualize jadi no-op
# (strength dikembalikan apa adanya). Default False sampai hasil observasi
# cukup untuk dipercaya (roadmap: audit 300-500 observasi dulu).

CONTEXT_ENGINE_ENABLED = False   # Phase B toggle — lihat /marketdna & /detectorstats dulu sebelum ON

def contextualize_event_score_v2(
    event: TradeEvent,
    state: MarketStateVector,
) -> Tuple[float, float, List[str]]:
    """
    Transform raw score menggunakan Market State Vector.
    BUKAN if-else regime. Menggunakan continuous signals untuk smooth
    adjustment. Ini HANYA mengubah strength/confidence — evidence check
    (delta, oi_persistence, dll) tetap di score_event_non_additive(),
    yang jalan SETELAH ini di build_thesis(), dan tetap jadi hakim
    terakhir untuk reject/accept.
    """
    raw_score = event.strength
    raw_conf = event.confidence
    adjustments = []
    score = raw_score
    conf = raw_conf

    # ===== 1. MOMENTUM (0-100) → smoother transition =====
    if event.type in ("OB", "FVG", "OB_FLOW"):
        momentum_factor = 0.9 + (state.momentum / 100) * 0.4  # 0.9 - 1.3
        score *= momentum_factor
        if state.momentum > 60:
            conf *= (1.0 + (state.momentum - 60) / 200)
            adjustments.append(f"momentum_{state.momentum:.0f}")
        elif state.momentum < 30:
            conf *= (1.0 - (30 - state.momentum) / 150)
            adjustments.append(f"low_momentum_{state.momentum:.0f}")

    # ===== 2. COMPRESSION (0-100) → breakout anticipation =====
    if event.type in ("OB", "FVG"):
        compression_factor = 0.85 + (state.compression / 100) * 0.3  # 0.85 - 1.15
        score *= compression_factor
        adjustments.append(f"compression_{state.compression:.0f}")

    # ===== 3. SHOCK (0-100) =====
    if event.type == "LIQUIDITY":
        score *= (1.0 + (state.shock / 100) * 0.3)  # 1.0 - 1.3
        conf *= (1.0 + (state.shock / 100) * 0.15)
        adjustments.append(f"shock_{state.shock:.0f}")
    else:
        score *= (1.0 - (state.shock / 100) * 0.15)
        adjustments.append(f"shock_penalty_{state.shock:.0f}")

    # ===== 4. ENTROPY (0-100) → chaos factor =====
    if state.entropy > 60:
        entropy_factor = 1.0 - (state.entropy / 100) * 0.25  # 1.0 - 0.75
        score *= entropy_factor
        conf *= entropy_factor
        adjustments.append(f"entropy_{state.entropy:.0f}")

    # ===== 5. PARTICIPATION (0-100) =====
    if event.direction == "LONG" and state.participation > 60:
        score *= (1.0 + (state.participation - 60) / 200)
        adjustments.append(f"participation_{state.participation:.0f}")
    elif event.direction == "SHORT" and state.participation > 60:
        score *= (1.0 - (state.participation - 60) / 300)
        adjustments.append("participation_short_penalty")

    # ===== 6. DIRECTIONALITY =====
    if state.directionality > 30 and event.direction == "LONG":
        score *= 1.10
        conf *= 1.05
        adjustments.append(f"dir_align_{state.directionality:.0f}")
    elif state.directionality < -30 and event.direction == "SHORT":
        score *= 1.10
        conf *= 1.05
        adjustments.append(f"dir_align_{state.directionality:.0f}")
    elif abs(state.directionality) < 20:
        pass  # No strong direction: neutral
    else:
        score *= 0.85
        adjustments.append("dir_against")

    # ===== 7. TENSION =====
    if state.tension > 60 and event.type in ("OB", "FVG"):
        score *= 1.10
        conf *= 1.05
        adjustments.append(f"tension_{state.tension:.0f}")

    # ===== 8. VOLATILITY =====
    if state.volatility > 60:
        if event.type == "LIQUIDITY":
            score *= 1.10
            adjustments.append("vol_liquidity_boost")
        elif event.type in ("OB", "FVG"):
            score *= 0.90
            adjustments.append("vol_penalty")

    # ===== CLAMP =====
    score = max(0.0, min(100.0, score))
    conf = max(0.0, min(100.0, conf))

    return round(score, 1), round(conf, 1), adjustments


def contextualize_events_v2(
    events: List[TradeEvent],
    state: MarketStateVector,
) -> List[TradeEvent]:
    """Apply contextualization menggunakan Market State Vector.
    MUTATES event.strength/.confidence IN PLACE — tidak reconstruct
    TradeEvent baru, supaya .mid/.score/.first_seen/.fill_ratio/dll yang
    di-set oleh layer lain (cluster_events, score_event_non_additive)
    tidak pernah ke-drop. Kalau CONTEXT_ENGINE_ENABLED == False, ini
    no-op murni (strength/confidence tetap apa adanya) — aman dipanggil
    kapan saja untuk logging/observasi."""
    if not events:
        return events

    for e in events:
        if not CONTEXT_ENGINE_ENABLED:
            if e.extra is None:
                e.extra = {}
            e.extra["contextualized"] = False
            continue

        adj_score, adj_conf, reasons = contextualize_event_score_v2(e, state)

        if e.extra is None:
            e.extra = {}
        e.extra["raw_strength"] = e.strength
        e.extra["raw_confidence"] = e.confidence
        e.extra["contextualized"] = True
        e.extra["context_reasons"] = reasons
        e.extra["state_snapshot"] = state.to_dict()

        e.strength = adj_score
        e.confidence = adj_conf

    return events


def select_best_cluster_for_thesis(
    clusters: List[TradeEvent],
    state: MarketStateVector,
) -> Optional[TradeEvent]:
    """
    Pilih cluster terbaik untuk thesis (OBSERVASI/logging only saat ini —
    build_thesis() masih pakai max(clustered, key=lambda e: e.score) yang
    jalan SETELAH score_event_non_additive, karena itu tetap hakim
    terakhir). Fungsi ini disediakan untuk audit perbandingan, bukan
    dipakai langsung menggantikan pemilihan final di build_thesis().

    Kriteria:
    1. Contextualized strength (utama)
    2. Diversity bonus (multiple evidence types)
    """
    if not clusters:
        return None

    sorted_clusters = sorted(clusters, key=lambda e: e.strength, reverse=True)
    best = sorted_clusters[0]
    best_score = best.strength

    for c in sorted_clusters[:3]:
        members = c.extra.get("members", []) if c.extra else []
        unique_types = len(set(members))

        if unique_types >= 2:
            diversity_bonus = 1.0 + (unique_types - 1) * 0.05
            adjusted_score = c.strength * diversity_bonus

            if adjusted_score > best_score * 1.05:
                best = c
                best_score = adjusted_score
                if best.extra is None:
                    best.extra = {}
                best.extra["selected_by"] = "diversity"

    return best

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

    # ===== P4: CORRELATION FIELDS (persisted at OPEN) =====
    score: int = 0
    size: float = 1.0
    regime: str = "UNKNOWN"
    source: str = "UNKNOWN"
    cache_age: float = 0.0

    # ===== P4.50: CONVICTION + MEM SNAPSHOT (persisted at OPEN) =====
    # NOTE: restore/reconciliation call sites (bot restart) don't have an
    # `alert` dict, so these MUST default safely — never required params.
    conviction: float = 0.0
    conviction_mode: str = "UNKNOWN"
    conviction_penalty: float = 0.0
    mem_outcome_boost: float = 0.0
    mem_cooldown_mult: float = 1.0
    mem_stability: Optional[float] = None
    mem_edge: float = 0.0

    # ===== P4.56: ATR snapshot at entry (drives dynamic trailing distance) =====
    entry_atr_pct: float = 0.0

    # ===== HIGH-LEV: leverage yang dipakai saat entry (untuk PnL calculation) =====
    leverage: float = 1.0
    
    # ===== P3: EXIT STATE MACHINE =====
    exit_state: str = "SEEK"  # SEEK, CAPTURE, DEFEND, HARVEST

    # ===== L1: ENTRY WINDOW =====
    entry_quality: float = 0.0

    # ===== L4: FIRST PROFIT TIME =====
    first_profit_time: Optional[float] = None  # timestamp saat posisi pertama kali hijau
    first_profit_seen: bool = False
    
    def update_extremes(self, current_price: float):
        """Update highest/lowest untuk MFE/MAE tracking + First Profit Time"""
        if self.highest == 0:
            self.highest = current_price
            self.lowest = current_price
        else:
            self.highest = max(self.highest, current_price)
            self.lowest = min(self.lowest, current_price)

        # ===== L4: FIRST PROFIT TIME =====
        if not self.first_profit_seen:
            if self.direction == "LONG" and current_price > self.entry:
                self.first_profit_time = time.time()
                self.first_profit_seen = True
            elif self.direction == "SHORT" and current_price < self.entry:
                self.first_profit_time = time.time()
                self.first_profit_seen = True

# ============================================================
# P4.31 — RECENT OUTCOME MEMORY
# ============================================================
# Tujuan: ingat hasil beberapa trade terakhir per coin.
# Struktur: Dict[str, deque] — bounded per-coin history.
# ============================================================

_recent_outcome_memory: Dict[str, deque] = {}
_recent_outcome_lock = threading.RLock()
_OUTCOME_MEMORY_MAX = 10

def update_recent_outcome(coin: str, pnl: float, mfe: float, mae: float, exit_eff: Optional[float] = None,
                           duration_minutes: Optional[float] = None):
    """Update outcome memory untuk coin. Dipanggil dari _close_remaining()."""
    with _recent_outcome_lock:
        if coin not in _recent_outcome_memory:
            _recent_outcome_memory[coin] = deque(maxlen=_OUTCOME_MEMORY_MAX)
        _recent_outcome_memory[coin].append({
            "pnl": pnl,
            "mfe": mfe,
            "mae": mae,
            "exit_eff": exit_eff,
            "duration_minutes": duration_minutes,
            "ts": time.time(),
        })

def get_last_outcome(coin: str) -> Optional[Dict[str, Any]]:
    """Ambil outcome terakhir untuk coin."""
    with _recent_outcome_lock:
        if coin not in _recent_outcome_memory or not _recent_outcome_memory[coin]:
            return None
        return _recent_outcome_memory[coin][-1]

def get_recent_outcome_boost(coin: str) -> float:
    """
    Hitung boost/penalty dari outcome terakhir.
    Return: -4 .. +4 (informational/learning-context only, tidak dipakai untuk gating).
    """
    last = get_last_outcome(coin)
    if not last:
        return 0.0
    boost = 0.0
    if last["pnl"] > 2.0:
        boost += 2.0   # momentum+
    elif last["pnl"] < -2.0:
        boost -= 3.0   # recovery

    if last.get("exit_eff") and last["exit_eff"] > 70:
        boost += 1.0
    # NOTE: mae di codebase ini disimpan sebagai angka non-positif (adverse
    # excursion), bukan magnitude positif — beda dari asumsi draft awal.
    # Loss dalam yang dalam ditandai mae yang lebih negatif (mis. -4.02%).
    if last["mae"] < -2.0:
        boost -= 1.0
    return max(-4.0, min(4.0, boost))


def get_recent_outcome_history(coin: str) -> List[Dict[str, Any]]:
    """P4.50: snapshot list (bukan deque live ref) dari outcome memory coin ini."""
    with _recent_outcome_lock:
        if coin not in _recent_outcome_memory:
            return []
        return list(_recent_outcome_memory[coin])


def compute_mem_stability(coin: str, min_samples: int = 3) -> Optional[float]:
    """
    P4.50: Stabilitas edge per-coin = std-dev pnl dari outcome memory (max 10 terakhir).
    Rendah = konsisten (edge bisa dipercaya). Tinggi = liar/noisy (conviction harusnya
    discount). Return None kalau sample belum cukup (jangan tampilin angka palsu).
    Observational only — tidak dipakai untuk gating di Phase 1/2.
    """
    history = get_recent_outcome_history(coin)
    if len(history) < min_samples:
        return None
    pnls = [h["pnl"] for h in history]
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
    return round(variance ** 0.5, 2)  # std-dev, dalam % pnl


def compute_trade_feedback(coin: str, final_pnl: float, mfe: float, mae: float,
                            age_minutes: float, min_samples: int = 3) -> Dict[str, Any]:
    """
    P4.50: Hitung sinyal feedback exit untuk CLOSE alert.
    HARUS dipanggil SEBELUM update_recent_outcome() untuk trade ini, supaya
    'mature' membandingkan ke rata-rata historis, bukan termasuk diri sendiri.

    - early: 1 kalau exit cepat (<15m) DAN merugi/MFE tipis (reuse logika
      P4.2 FAST_FAIL yang sebelumnya cuma jadi log, sekarang jadi sinyal numerik).
    - shape: MAE depth relatif ke final_pnl POSITIF. Winner dengan mae dalam
      (banyak floating loss dulu) = shape jelek (>1.0 berarti mae lebih besar
      dari pnl akhir, exit-nya 'untung tapi ngeri'). None kalau bukan winner
      (shape gak relevan buat losing trade).
    - mature: rasio age_minutes trade ini terhadap rata-rata duration_minutes
      historis coin ini. >1.0 = hidup lebih lama dari biasanya. None kalau
      data durasi historis belum cukup.
    """
    early = 1 if (age_minutes < 15 and mfe <= 0.3 and final_pnl < 0) else 0

    shape = None
    if final_pnl > 0 and mae < 0:
        shape = round(abs(mae) / final_pnl, 2)
    elif final_pnl > 0:
        shape = 0.0  # winner tanpa floating loss sama sekali — shape sempurna

    history = get_recent_outcome_history(coin)
    durations = [h["duration_minutes"] for h in history if h.get("duration_minutes")]
    mature = None
    if len(durations) >= min_samples:
        avg_duration = sum(durations) / len(durations)
        if avg_duration > 0:
            mature = round(age_minutes / avg_duration, 2)

    return {"early": early, "shape": shape, "mature": mature}


# ============================================================
# P4.33 — COIN COOLDOWN
# ============================================================
# Tujuan: coin habis loss besar → jangan langsung dihajar lagi.
# Trigger: pnl < -2% → cooldown 2 jam, decay linear ke size.
# ============================================================

_coin_cooldown: Dict[str, float] = {}
_coin_cooldown_lock = threading.RLock()
_COOLDOWN_DURATION_HOURS = 2.0

def apply_coin_cooldown(coin: str, pnl: float):
    """Jika loss < -2%, cooldown 2 jam. Dipanggil dari _close_remaining()."""
    if pnl < -2.0:
        with _coin_cooldown_lock:
            _coin_cooldown[coin] = time.time() + _COOLDOWN_DURATION_HOURS * 3600
            logger.info(f"❄️ COOLDOWN {coin}: loss {pnl:.2f}% → {_COOLDOWN_DURATION_HOURS}h")

def get_coin_cooldown_penalty(coin: str) -> float:
    """Return penalty multiplier (1.0 = no penalty, 0.3 = max penalty, decay linear ke 0)."""
    with _coin_cooldown_lock:
        if coin not in _coin_cooldown:
            return 1.0
        remaining = _coin_cooldown[coin] - time.time()
        if remaining <= 0:
            del _coin_cooldown[coin]
            return 1.0
        max_penalty_hours = _COOLDOWN_DURATION_HOURS
        penalty_ratio = remaining / (max_penalty_hours * 3600)  # 1.0 di awal, 0.0 di akhir
        multiplier = 0.3 + 0.7 * penalty_ratio  # 0.3..1.0
        return max(0.3, min(1.0, multiplier))


# ============================================================
# P4.46 — PASSIVE LEARNING ALERT CONTEXT (GELOMBANG 1)
# ============================================================
# Tujuan: tampilkan konteks belajar (outcome memory + cooldown) di
# OPEN/CLOSE alert — hanya kalau ada perubahan berarti (gak noise).
#
# NOTE: sub-fitur "fast start" dari draft awal (pos.first_green_ts)
# di-drop karena field itu tidak ada di OpenPosition. Kalau mau
# diaktifkan nanti, perlu nambah field + logic set-nya di TradeManager
# pas first candle hijau setelah entry.
# ============================================================

def build_learning_context_open(coin: str) -> List[str]:
    """Learning context untuk OPEN message — hanya tampilkan perubahan berarti."""
    learn = []

    boost = get_recent_outcome_boost(coin)
    if boost >= 2.0:
        learn.append("🧠 momentum+")
    elif boost <= -2.0:
        learn.append("🧠 recovery")

    cooldown_penalty = get_coin_cooldown_penalty(coin)
    if cooldown_penalty < 0.8:
        learn.append(f"❄️ cooldown {cooldown_penalty:.0%}")
    elif cooldown_penalty < 1.0:
        learn.append("❄️ cooldown")

    return learn


def build_learning_context_close(coin: str, pnl: float, exit_eff: Optional[float]) -> List[str]:
    """Learning context untuk CLOSE message."""
    learn = []

    if exit_eff is not None:
        if exit_eff >= 70:
            learn.append("🎯 strong capture")
        elif exit_eff < 35:
            learn.append("⚠️ weak capture")

    if pnl < -2.0:
        learn.append("❄️ cooldown")

    return learn


# ========== P3: EXIT STATE MACHINE (SEEK → CAPTURE → DEFEND → HARVEST) ==========
# Purpose: Replace fixed TP/BE with momentum-based adaptive exit that lets winners run longer

from enum import Enum

class ExitState(Enum):
    """Position lifecycle states for adaptive exit."""
    SEEK = "SEEK"          # Age <3m, building momentum, NO trailing yet
    CAPTURE = "CAPTURE"    # MFE >0.5×ATR, momentum peaking, BEGIN trailing
    DEFEND = "DEFEND"      # Velocity declining, delta weakening, AGGRESSIVE trail
    HARVEST = "HARVEST"    # Reversal signal, take profits, EXIT


def _compute_exit_state(pos: OpenPosition, current_price: float, atr_pct: float) -> ExitState:
    """
    Determine which exit state position is in based on:
    - Age (time in trade)
    - MFE (max favorable excursion)
    - Velocity (momentum strength)
    - Delta (flow conviction)
    
    Returns: ExitState enum
    """
    try:
        age_minutes = (time.time() - pos.entry_time) / 60
        
        # Calculate MFE in percentage
        if pos.direction == "LONG":
            mfe_pct = (pos.highest - pos.entry) / pos.entry * 100 if pos.highest > pos.entry else 0
        else:
            mfe_pct = (pos.entry - pos.lowest) / pos.entry * 100 if pos.lowest < pos.entry else 0
        
        # HARVEST: Reversal signal or duration exhaustion
        # Check reversal probability or major momentum loss
        try:
            # If position has been open >4h AND hasn't made profit OR momentum dead
            if age_minutes > 240:  # 4 hours
                if mfe_pct < 1.0:  # Haven't made real progress
                    return ExitState.HARVEST  # Time decay exit
            
            # Reversal signal would be checked via market_regime/delta changes
            # For now, we use velocity as proxy
        except Exception:
            pass
        
        # DEFEND: Velocity declining, delta weakening
        # Try to get velocity from position metadata or estimate from MFE trend
        try:
            # If we have volatility data and MFE is good but momentum slowing
            if mfe_pct > atr_pct * 0.8:  # Made decent progress
                # Check if position momentum is declining (position age >1.5h with same MFE)
                if age_minutes > 90 and mfe_pct < atr_pct * 1.5:  # Stalled
                    return ExitState.DEFEND  # Momentum dying
        except Exception:
            pass
        
        # CAPTURE: MFE building, momentum still strong
        if mfe_pct > atr_pct * 0.5:  # Decent MFE (at least 50% of ATR)
            return ExitState.CAPTURE
        
        # SEEK: Young position, building momentum
        if age_minutes < 3:
            return ExitState.SEEK
        
        # Default: if MFE small but position open, still in CAPTURE mode
        # (early stage of profit taking)
        return ExitState.CAPTURE
        
    except Exception as e:
        logger.debug(f"_compute_exit_state error: {e}")
        return ExitState.CAPTURE  # Safe default


# ========== L3: REVERSIBLE EXIT STATE MACHINE ==========
def _compute_exit_state_reversible(pos: OpenPosition, current_price: float, atr_pct: float) -> ExitState:
    """
    L3 HOLD CONVICTION — Reversible Exit State Machine.

    Filosofi: posisi itu kayak "pegang bola panas". CAPTURE ↔ DEFEND
    bisa bolak-balik tergantung flow (velocity + delta trend). Kalau
    momentum balik sehat setelah masuk DEFEND, posisi bisa naik lagi
    ke CAPTURE — gak langsung digiring ke exit cuma karena sempat lesu
    sebentar. Satu-satunya state irreversible: HARVEST (exit).

    Zero new filter/threshold baru — pakai sinyal yang sudah ada di
    sistem (get_velocity_alignment, get_delta_persistence_score) sebagai
    proxy kualitas flow 0-1, plus MFE/age yang sudah dipakai versi linear.
    """
    try:
        current = pos.exit_state if pos.exit_state in [s.value for s in ExitState] else ExitState.SEEK.value
        current_state = ExitState(current)

        # HARVEST → IRREVERSIBLE
        if current_state == ExitState.HARVEST:
            return ExitState.HARVEST

        age_minutes = (time.time() - pos.entry_time) / 60
        if pos.direction == "LONG":
            mfe_pct = (pos.highest - pos.entry) / pos.entry * 100 if pos.highest > pos.entry else 0
        else:
            mfe_pct = (pos.entry - pos.lowest) / pos.entry * 100 if pos.lowest < pos.entry else 0

        # Time-decay exit (dipertahankan dari versi linear)
        if age_minutes > 240 and mfe_pct < 1.0:
            return ExitState.HARVEST

        # ===== FLOW QUALITY PROXIES (0-1, dari sinyal existing) =====
        try:
            velocity = get_velocity_alignment(pos.coin, pos.direction, None)
        except Exception:
            velocity = 0.5
        try:
            delta_trend = get_delta_persistence_score(pos.coin, pos.direction, window=5)
        except Exception:
            delta_trend = 0.5

        # SEEK → CAPTURE
        if current_state == ExitState.SEEK:
            if mfe_pct > atr_pct * 0.5:
                return ExitState.CAPTURE
            return ExitState.SEEK

        # CAPTURE ↔ DEFEND (REVERSIBLE)
        if current_state == ExitState.CAPTURE:
            if velocity <= 0.2 or delta_trend <= 0.3:
                return ExitState.DEFEND
            return ExitState.CAPTURE

        if current_state == ExitState.DEFEND:
            # Flow sehat lagi → balik CAPTURE
            if velocity >= 0.6 and delta_trend >= 0.7:
                logger.info(
                    f"🔄 STATE REVERSAL {pos.coin}: DEFEND → CAPTURE "
                    f"(velocity={velocity:.2f}, delta_trend={delta_trend:.2f})"
                )
                return ExitState.CAPTURE
            # Makin lemah → HARVEST
            if velocity <= 0.2 and delta_trend <= 0.3:
                logger.info(
                    f"🍗 STATE {pos.coin}: DEFEND → HARVEST "
                    f"(velocity={velocity:.2f}, delta_trend={delta_trend:.2f})"
                )
                return ExitState.HARVEST
            # Bertahan di DEFEND
            return ExitState.DEFEND

        return ExitState.CAPTURE  # fallback

    except Exception as e:
        logger.debug(f"_compute_exit_state_reversible error: {e}")
        return ExitState.CAPTURE  # Safe default
# ========== END L3 REVERSIBLE EXIT STATE MACHINE ==========


def _get_trail_trigger(pos: OpenPosition, state: ExitState, atr_pct: float, regime: str) -> Dict[str, float]:
    """
    Compute trailing stop trigger based on exit state.
    
    SEEK: No trailing yet (only SL hard stop)
    CAPTURE: Begin trailing when MFE > 0.5×ATR
    DEFEND: Aggressive trailing (tighter protection)
    HARVEST: Prepare to exit, tight trailing
    
    Returns: {
        "trigger_pct": MFE % needed to activate trail,
        "trail_pct": How tight to trail (% from current price),
        "should_trail": Boolean if trailing should be active,
        "mfe_pct": Current MFE percentage,
        "reason": Human-readable reason
    }
    """
    try:
        # Calculate current MFE
        if pos.direction == "LONG":
            mfe_pct = (pos.highest - pos.entry) / pos.entry * 100 if pos.highest > pos.entry else 0
        else:
            mfe_pct = (pos.entry - pos.lowest) / pos.entry * 100 if pos.lowest < pos.entry else 0
        
        if state == ExitState.SEEK:
            # No trailing yet, just SL guard
            return {
                "trigger_pct": 999,  # Never triggers
                "trail_pct": 0,
                "should_trail": False,
                "mfe_pct": mfe_pct,
                "reason": "SEEK: building momentum, no trail yet"
            }
        
        elif state == ExitState.CAPTURE:
            # Begin trailing at 50% of ATR
            trigger = atr_pct * 0.5
            # Trail loosely (0.5× ATR) in trending, tighter (0.8× ATR) in ranging
            trail = atr_pct * (0.5 if regime in ["TRENDING_UP", "TRENDING_DOWN"] else 0.8)
            return {
                "trigger_pct": trigger,
                "trail_pct": trail,
                "should_trail": mfe_pct > trigger,
                "mfe_pct": mfe_pct,
                "reason": f"CAPTURE: trail when MFE>{trigger:.2f}%, trail_pct={trail:.2f}%"
            }
        
        elif state == ExitState.DEFEND:
            # Tighter protection, more aggressive trailing
            trigger = atr_pct * 0.3
            trail = atr_pct * 0.3  # Much tighter
            return {
                "trigger_pct": trigger,
                "trail_pct": trail,
                "should_trail": True,
                "mfe_pct": mfe_pct,
                "reason": f"DEFEND: aggressive trail at {trail:.2f}% (momentum dying)"
            }
        
        elif state == ExitState.HARVEST:
            # Very tight, prepare for exit
            trigger = atr_pct * 0.2
            trail = atr_pct * 0.2  # Very tight
            return {
                "trigger_pct": trigger,
                "trail_pct": trail,
                "should_trail": True,
                "mfe_pct": mfe_pct,
                "reason": f"HARVEST: prepare exit, trail={trail:.2f}%"
            }
        
        else:
            # Unknown state, safe default
            return {
                "trigger_pct": atr_pct,
                "trail_pct": atr_pct * 0.6,
                "should_trail": mfe_pct > atr_pct * 0.5,
                "mfe_pct": mfe_pct,
                "reason": "DEFAULT: unknown state"
            }
    
    except Exception as e:
        logger.debug(f"_get_trail_trigger error: {e}")
        return {
            "trigger_pct": atr_pct,
            "trail_pct": atr_pct * 0.6,
            "should_trail": False,
            "mfe_pct": 0,
            "reason": f"ERROR: {e}"
        }
# ========== END P3 EXIT STATE MACHINE ==========


class TradeManager:
    """LIVE EXIT BRAIN — Process open positions with scaling exit"""
    
    def __init__(self):
        self.positions: Dict[str, OpenPosition] = {}
        self.check_interval = 60  # Cek tiap 60 detik
        self._lock = threading.RLock()
        self._last_check = 0
    
    def add_position(self, signal_id: str, coin: str, direction: str,
                     entry: float, sl: float, tp_targets: Dict, entry_time: float,
                     score: int = 0, size: float = 1.0,
                     regime: str = "UNKNOWN", source: str = "UNKNOWN", cache_age: float = 0.0,
                     conviction: float = 0.0, conviction_mode: str = "UNKNOWN",
                     conviction_penalty: float = 0.0, mem_outcome_boost: float = 0.0,
                     mem_cooldown_mult: float = 1.0, mem_stability: Optional[float] = None,
                     mem_edge: float = 0.0, entry_atr_pct: float = 0.0,
                     leverage: float = 1.0,
                     entry_quality: float = 0.0):
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
                entry_quality=entry_quality,
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
                ),
                # ===== P4: SIMPAN SEMUA =====
                score=score,
                size=size,
                regime=regime,
                source=source,
                cache_age=cache_age,
                # ===== P4.50: CONVICTION + MEM SNAPSHOT =====
                conviction=conviction,
                conviction_mode=conviction_mode,
                conviction_penalty=conviction_penalty,
                mem_outcome_boost=mem_outcome_boost,
                mem_cooldown_mult=mem_cooldown_mult,
                mem_stability=mem_stability,
                mem_edge=mem_edge,
                # ===== P4.56: ATR snapshot untuk dynamic trailing =====
                entry_atr_pct=entry_atr_pct,
                leverage=leverage,
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

                # ===== EXIT STATE MACHINE (SEEK→CAPTURE→DEFEND→HARVEST) =====
                # FIX: P4.32 profit-lock dicabut — dulu jalan PARALEL dengan
                # Exit State Machine dan SELALU menang duluan (line ini
                # eksekusi sebelum trailing check), karena formula floor-nya
                # (lock_fraction=0.15 di MFE kecil) menghasilkan SL ~0.02%
                # dari entry — efektif breakeven, exact hal yang ingin
                # dihindari sejak awal. Exit State Machine di bawah sudah
                # mencakup seluruh lifecycle (state SEEK menahan diri di MFE
                # kecil, baru CAPTURE mulai trail di >0.5x ATR), jadi gak
                # perlu dua mekanisme paralel.
                self._update_trailing_stop(pos, current_price)

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
                
                # ===== CHECK TP3 OR TRAILING EXIT (trailing now active for whole lifecycle, not just post-TP2) =====
                if not pos.tp3.is_hit:
                    if pos.tp2.is_hit and self._check_tp_hit(pos, "tp3", current_price):
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
                if age_minutes > 240 and pos.tp1.is_hit and not pos.tp2.is_hit:  # FIX: 240m (dari 120m)
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
            hit = current_price >= tp.price * 0.99
        else:
            hit = current_price <= tp.price * 1.01

        if hit:
            logger.info(
                f"TP_HIT_TRACE "
                f"signal={pos.signal_id} "
                f"coin={pos.coin} "
                f"level={tp_level} "
                f"price={current_price:.4f} "
                f"target={tp.price:.4f} "
                f"age={int(time.time()-pos.entry_time)}s"
            )

        return hit
    
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
        """
        L3: Reversible Exit State Machine based trailing stop.
        
        SEEK → CAPTURE ↔ DEFEND → HARVEST (irreversible)
        
        Replaces: Linear state machine (DEFEND was a one-way street to
        HARVEST). Sekarang CAPTURE ↔ DEFEND bisa bolak-balik kalau flow
        (velocity + delta persistence) balik sehat, jadi posisi gak
        digiring exit cuma karena sempat lesu sebentar.
        """
        # ===== DETERMINE CURRENT EXIT STATE =====
        atr_pct = pos.entry_atr_pct if pos.entry_atr_pct > 0 else 1.0  # Fallback to 1% if missing
        regime = pos.regime if pos.regime else "UNKNOWN"
        exit_state = _compute_exit_state_reversible(pos, current_price, atr_pct)
        pos.exit_state = exit_state.value  # Store state on position for logging
        
        # ===== GET TRAILING PARAMETERS FOR THIS STATE =====
        trail_info = _get_trail_trigger(pos, exit_state, atr_pct, regime)
        
        # ===== ACTIVATE/DEACTIVATE TRAILING BASED ON STATE =====
        should_trail = trail_info["should_trail"]
        
        if not should_trail:
            # SEEK state: don't activate trailing yet
            return
        
        # Activate trailing if needed
        if not pos.trailing_activated:
            if ((pos.direction == "LONG" and current_price > pos.entry) or 
                (pos.direction == "SHORT" and current_price < pos.entry)):
                pos.trailing_activated = True
                logger.info(
                    f"🚀 TRAIL_ACTIVATED {pos.coin} state={exit_state.value} "
                    f"mfe={trail_info.get('mfe_pct', 0):.2f}% "
                    f"reason={trail_info['reason']}"
                )
        
        if pos.trailing_activated:
            trail_pct = trail_info["trail_pct"]
            
            if pos.direction == "LONG":
                new_sl = current_price * (1 - trail_pct / 100)
                if new_sl > pos.sl:
                    old_sl = pos.sl
                    pos.sl = new_sl
                    logger.debug(
                        f"📉 TRAIL {pos.coin}: {old_sl:.8f}→{new_sl:.8f} "
                        f"(trail={trail_pct:.2f}%, state={exit_state.value})"
                    )
            else:
                new_sl = current_price * (1 + trail_pct / 100)
                if new_sl < pos.sl:
                    old_sl = pos.sl
                    pos.sl = new_sl
                    logger.debug(
                        f"📈 TRAIL {pos.coin}: {old_sl:.8f}→{new_sl:.8f} "
                        f"(trail={trail_pct:.2f}%, state={exit_state.value})"
                    )

    def _update_profit_lock(self, pos: OpenPosition, current_price: float):
        """
        ⚠️ SUPERSEDED (lihat _update_trailing_stop / Exit State Machine).
        Tidak lagi dipanggil dari check_all_positions.

        Histori masalah: versi awal (lock_ratio=min(0.3, mfe/10)) cuma
        ngunci ~10% dari MFE, lalu versi berikutnya (lock_fraction + ATR
        floor 0.2*entry_atr_pct) masih mengunci SL ke ~0.8% dari entry
        bahkan di MFE kecil (kasus trade ALT: MFE 0.14%, SL struktural
        4.18% — locked ke 0.836%, masih jauh lebih ketat dari yang perlu).

        Exit State Machine (_update_trailing_stop, state SEEK→CAPTURE→
        DEFEND→HARVEST) terbukti lebih sesuai: trailing baru aktif kalau
        MFE > 0.5×ATR (untuk kasus ALT: trigger 2.09%, MFE 0.14% tidak
        memicu apa-apa, SL tetap di level struktural asli). Dibiarkan ada
        untuk referensi historis P4.32/P4.57, bukan dihapus.
        """
        return  # no-op, superseded

    def _execute_partial(self, pos: OpenPosition, tp_level: str):
        tp = getattr(pos, tp_level)
        pnl_pct = ((tp.price - pos.entry) / pos.entry * 100) if pos.direction == "LONG" \
                  else ((pos.entry - tp.price) / pos.entry * 100)
        logger.info(f"🎯 P1: PARTIAL TP {pos.coin} | {tp_level.upper()} ({tp.size_pct*100:.0f}%) | PnL: {pnl_pct:+.2f}%")
        pos.captured_tp_levels += 1
    
        # ===== RECORD FUNNEL: TP HIT =====
        if tp_level == "tp1":
            record_funnel_stage("tp1_hit")
        elif tp_level == "tp2":
            record_funnel_stage("tp2_hit")
        elif tp_level == "tp3":
            record_funnel_stage("tp3_hit")
        
    def _close_remaining(self, pos: OpenPosition, reason: str, current_price: float) -> Dict:
        # ===== EXIT_TRACE =====
        logger.info(
            f"CLOSE_DETAIL {pos.coin} "
            f"signal={pos.signal_id} "
            f"reason={reason} "
            f"captured={pos.captured_tp_levels} "
            f"tp1_hit={pos.tp1.is_hit} "
            f"tp2_hit={pos.tp2.is_hit} "
            f"tp3_hit={pos.tp3.is_hit} "
            f"price={current_price:.4f} "
            f"entry={pos.entry:.4f} "
            f"age={int(time.time()-pos.entry_time)}s"
        )
        # ===== END EXIT_TRACE =====

        if pos.direction == "LONG":
            final_pnl = (current_price - pos.entry) / pos.entry * 100
            mfe = (pos.highest - pos.entry) / pos.entry * 100
            mae = (pos.lowest - pos.entry) / pos.entry * 100
        else:
            final_pnl = (pos.entry - current_price) / pos.entry * 100
            mfe = (pos.entry - pos.lowest) / pos.entry * 100
            mae = (pos.entry - pos.highest) / pos.entry * 100

        # ===== HIGH-LEV: PnL dengan leverage =====
        _lev = getattr(pos, "leverage", 1.0) or 1.0
        leveraged_pnl = final_pnl * _lev
        
        age_minutes = (time.time() - pos.entry_time) / 60

        # ===== P3.1: EXIT EFFICIENCY =====
        eff = compute_exit_eff(final_pnl, mfe)

        logger.info(
            f"🚪 P1: CLOSE {pos.coin} | {reason} | "
            f"PnL: {final_pnl:+.2f}% (x{_lev:.1f} lev → {leveraged_pnl:+.2f}%) | MFE: {mfe:+.2f}% | MAE: {mae:+.2f}% | "
            f"ExitEff: {eff if eff is not None else 'N/A'}% | State: {pos.exit_state}"
        )
        logger.info(
            f"EXIT_TRACE "
            f"coin={pos.coin} "
            f"signal={pos.signal_id} "
            f"reason={reason} "
            f"pnl={final_pnl:.2f} "
            f"mfe={mfe:.2f} "
            f"mae={mae:.2f} "
            f"exit_eff={eff if eff is not None else 0:.0f}"
        )

        pos.status = "CLOSED"
        pos.exit_reason = reason
        pos.exit_time = time.time()
        pos.final_pnl = final_pnl

        # ===== P4.7: EXIT OBSERVER =====
        try:
            EXIT_OBSERVER.observe(pos.coin, pos, current_price, final_pnl, mfe, mae)
        except Exception as _obs_err:
            logger.debug(f"P4.7 EXIT_OBSERVER error: {_obs_err}")
        # ===== END P4.7 =====

        # ===== P4: OUTCOME_TRACE =====
        logger.info(
            f"OUTCOME_TRACE "
            f"signal={pos.signal_id} "
            f"coin={pos.coin} "
            f"score={pos.score} "
            f"regime={pos.regime} "
            f"source={pos.source} "
            f"cache={pos.cache_age:.0f} "
            f"exit_eff={eff if eff is not None else 0:.0f} "
            f"outcome={reason} "
            f"duration={age_minutes:.0f} "
            f"mfe={mfe:.2f} "
            f"mae={mae:.2f} "
            f"pnl={final_pnl:.2f}"
        )
        # ===== END P4 =====

        # ===== P4.50: TRADE FEEDBACK (early/shape/mature) =====
        # Dipanggil SEBELUM update_recent_outcome() di bawah, supaya 'mature'
        # membandingkan terhadap rata-rata historis SEBELUM trade ini masuk.
        feedback = compute_trade_feedback(pos.coin, final_pnl, mfe, mae, age_minutes)
        # ===== END P4.50 =====

        # ===== P4.31: RECENT OUTCOME MEMORY =====
        update_recent_outcome(pos.coin, final_pnl, mfe, mae, eff, duration_minutes=age_minutes)
        # ===== END P4.31 =====

        # ===== P4.33: COIN COOLDOWN (triggers only on loss < -2%) =====
        apply_coin_cooldown(pos.coin, final_pnl)
        # ===== END P4.33 =====

        # ===== P4.2: FAST_FAIL TAG (LOG ONLY) =====
        if (
            age_minutes < 15
            and mfe <= 0.3
            and final_pnl < 0
        ):
            logger.info(
                f"P4_FAST_FAIL "
                f"signal={pos.signal_id} "
                f"coin={pos.coin} "
                f"score={getattr(pos, 'score', 0)} "
                f"regime={getattr(pos, 'regime', None)}"
            )
        # ===== END P4.2 =====

        # ===== P4.50: FEEDBACK_TRACE =====
        logger.info(
            f"FEEDBACK_TRACE "
            f"signal={pos.signal_id} "
            f"coin={pos.coin} "
            f"early={feedback['early']} "
            f"shape={feedback['shape']} "
            f"mature={feedback['mature']}"
        )
        # ===== END P4.50 =====
    
        # ===== L1: ENTRY QUALITY IN CLOSE =====
        eq = getattr(pos, "entry_quality", 0.0)

        # ===== L4: FIRST PROFIT TIME (seconds from entry to first green tick) =====
        fpt = None
        if getattr(pos, "first_profit_time", None):
            fpt = pos.first_profit_time - pos.entry_time
        # else: never went green (loss trajectory) → fpt stays None

        return {
            "signal_id": pos.signal_id,
            "coin": pos.coin,
            "direction": pos.direction,
            "entry": pos.entry,
            "exit": current_price,
            "sl": pos.sl,
            "pnl": final_pnl,
            "leveraged_pnl": leveraged_pnl,
            "leverage": _lev,
            "reason": reason,
            "tp_levels_captured": pos.captured_tp_levels,
            "mfe": mfe,
            "mae": mae,
            "exit_eff": eff,
            "duration_minutes": age_minutes,
            "exit_time": pos.exit_time,
            # ===== P4: CORRELATION PASS-THROUGH (untuk persist ke DB) =====
            "score": pos.score,
            "regime": pos.regime,
            "data_source": pos.source,
            "cache_age": pos.cache_age,
            # ===== P4.50: FEEDBACK (post-exit) =====
            "early": feedback["early"],
            "shape": feedback["shape"],
            "mature": feedback["mature"],
            # ===== P4.50: ENTRY-TIME SNAPSHOT PASS-THROUGH (untuk Phase 2 segmented WR) =====
            "conviction": pos.conviction,
            "conviction_mode": pos.conviction_mode,
            "conviction_penalty": pos.conviction_penalty,
            "mem_outcome_boost_at_entry": pos.mem_outcome_boost,
            "mem_cooldown_mult_at_entry": pos.mem_cooldown_mult,
            "mem_stability_at_entry": pos.mem_stability,
            "entry_quality": eq,
            # ===== L4: ALERT QUALITY =====
            "fpt": fpt,
            "exit_state": pos.exit_state,
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
    
    def get_exit_health(self) -> Dict[str, Any]:
        """Check if exit engine is functioning."""
        with self._lock:
            now = time.time()
            closed_last_hour = 0
            stale_6h = 0
            stale_24h = 0
            
            for pos in self.positions.values():
                if pos.status == "CLOSED":
                    if pos.exit_time and now - pos.exit_time < 3600:
                        closed_last_hour += 1
                elif pos.status == "OPEN":
                    age = now - pos.entry_time
                    if age > 6 * 3600:
                        stale_6h += 1
                    if age > 24 * 3600:
                        stale_24h += 1
            
            return {
                "closed_last_hour": closed_last_hour,
                "stale_gt_6h": stale_6h,
                "stale_gt_24h": stale_24h,
                "is_stalled": closed_last_hour == 0 and stale_6h > 10,
            }

def record_confidence_score(score: int):
    """Record confidence score into rolling histogram."""
    with _conf_histogram_lock:
        bucket = int(score // 10) * 10
        bucket = min(100, max(0, bucket))
        _conf_histogram[bucket] = _conf_histogram.get(bucket, 0) + 1
        _conf_histogram_window.append(score)
        
        if len(_conf_histogram_window) > 1000:
            _conf_histogram.clear()
            for s in list(_conf_histogram_window)[-1000:]:
                b = int(s // 10) * 10
                b = min(100, max(0, b))
                _conf_histogram[b] = _conf_histogram.get(b, 0) + 1

def get_confidence_histogram() -> Dict[int, int]:
    with _conf_histogram_lock:
        return dict(_conf_histogram)

def get_confidence_summary() -> str:
    hist = get_confidence_histogram()
    with _conf_histogram_lock:
        scores = list(_conf_histogram_window)
    
    if not scores:
        return "No confidence data"
    
    sorted_scores = sorted(scores)
    median = sorted_scores[len(sorted_scores)//2]
    p90 = sorted_scores[int(len(sorted_scores)*0.9)] if len(sorted_scores) > 10 else median
    
    lines = [f"Median: {median:.0f}, P90: {p90:.0f}"]
    total = len(scores)
    for bucket in sorted(hist.keys()):
        count = hist[bucket]
        pct = (count / total * 100) if total > 0 else 0
        bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        lines.append(f"  {bucket}-{bucket+9}: {count:3d} ({pct:4.1f}%) {bar}")
    
    return "\n".join(lines)

# Global trade manager instance
TRADE_MANAGER = TradeManager()


# ============================================================
# WEBSOCKET STREAMS — PHASE WS-1
# ============================================================

class HyperliquidWebSocket:
    """WebSocket manager buat Hyperliquid."""

    WS_URL = "wss://api.hyperliquid.xyz/ws"

    def __init__(self):
        self._ws = None
        self._loop = None
        self._thread = None
        self._connected = False
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._handlers: Dict[str, List[Callable]] = {}
        self._subscribed = {"allMids": False, "l2Book": set(), "trades": set()}
        self._last_ping = 0.0
        self._last_pong = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_async, daemon=True)
        self._thread.start()
        logger.info("🌐 WS Manager started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("🌐 WS Manager stopped")

    def _run_async(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._websocket_loop())
        except Exception as e:
            logger.error(f"WS loop error: {e}")

    async def _websocket_loop(self):
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_pong = time.time()
                    logger.info("🌐 WS connected")

                    await self._resubscribe_all()

                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            data = json.loads(msg)
                            if data.get("type") == "pong":
                                self._last_pong = time.time()
                                continue
                            await self._process_message(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.debug(f"WS message error: {e}")

                    self._connected = False

            except websockets.ConnectionClosed:
                logger.warning("🌐 WS disconnected, reconnecting...")
                self._connected = False
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"WS error: {e}")
                self._connected = False
                await asyncio.sleep(5)

    async def _resubscribe_all(self):
        if self._subscribed["allMids"]:
            await self._subscribe("allMids")
        for coin in self._subscribed["l2Book"]:
            await self._subscribe("l2Book", coin=coin)
        for coin in self._subscribed["trades"]:
            await self._subscribe("trades", coin=coin)

    async def _subscribe(self, sub_type: str, coin: str = None):
        if not self._ws:
            return
        sub = {"type": sub_type}
        if coin:
            sub["coin"] = coin
        try:
            await self._ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
        except Exception as e:
            logger.debug(f"Subscribe error: {e}")

    async def _process_message(self, data: Dict):
        msg_type = data.get("type") or data.get("channel")
        if msg_type and msg_type in self._handlers:
            for handler in self._handlers[msg_type]:
                try:
                    handler(data)
                except Exception as e:
                    logger.debug(f"Handler error: {e}")

    def subscribe_mids(self):
        self._subscribed["allMids"] = True
        if self._connected and self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._subscribe("allMids"), self._loop)

    def subscribe_l2(self, coin: str):
        self._subscribed["l2Book"].add(coin)
        if self._connected and self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._subscribe("l2Book", coin=coin), self._loop)

    def subscribe_trades(self, coin: str):
        self._subscribed["trades"].add(coin)
        if self._connected and self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._subscribe("trades", coin=coin), self._loop)

    def on(self, msg_type: str, handler: Callable):
        with self._lock:
            if msg_type not in self._handlers:
                self._handlers[msg_type] = []
            self._handlers[msg_type].append(handler)

    @property
    def is_connected(self) -> bool:
        return self._connected


# ============================================================
# WS-1.1: MID PRICE STREAM
# ============================================================

class MidPriceStream:
    """Real-time harga dari WebSocket."""

    def __init__(self, ws: HyperliquidWebSocket):
        self._ws = ws
        self._prices: Dict[str, float] = {}
        self._price_history: Dict[str, deque] = {}
        self._lock = threading.RLock()
        self._dirty_coins: set = set()
        self._dirty_lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable]] = {}
        self._last_update: Dict[str, float] = {}

        self._ws.on("allMids", self._on_mids_update)
        self._ws.subscribe_mids()
        logger.info("📊 MidPriceStream ready")

    def _on_mids_update(self, data: Dict):
        mids = data.get("data", {}).get("mids", data.get("data", {}))
        if not isinstance(mids, dict):
            return
        now = time.time()
        with self._lock:
            for coin, price_str in mids.items():
                try:
                    price = float(price_str)
                except (TypeError, ValueError):
                    continue
                old = self._prices.get(coin)
                self._prices[coin] = price
                self._last_update[coin] = now

                if coin not in self._price_history:
                    self._price_history[coin] = deque(maxlen=20)
                self._price_history[coin].append((now, price))

                if old:
                    change = abs((price - old) / old * 100)
                    if change >= 0.1:
                        with self._dirty_lock:
                            self._dirty_coins.add(coin)

                if coin in self._subscribers:
                    for cb in self._subscribers[coin]:
                        try:
                            cb(coin, old, price)
                        except Exception:
                            pass
                if "*" in self._subscribers:
                    for cb in self._subscribers["*"]:
                        try:
                            cb(coin, old, price)
                        except Exception:
                            pass

    def get_price(self, coin: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(coin)

    def get_all_prices(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_dirty_coins(self) -> List[str]:
        with self._dirty_lock:
            return list(self._dirty_coins)

    def mark_clean(self, coin: str):
        with self._dirty_lock:
            self._dirty_coins.discard(coin)

    def on_price_update(self, coin: str, cb: Callable):
        with self._lock:
            if coin not in self._subscribers:
                self._subscribers[coin] = []
            self._subscribers[coin].append(cb)

    def on_any_price_update(self, cb: Callable):
        with self._lock:
            if "*" not in self._subscribers:
                self._subscribers["*"] = []
            self._subscribers["*"].append(cb)

    @property
    def is_connected(self) -> bool:
        return self._ws.is_connected

    def is_fresh(self, coin: str, max_age: float = 10.0) -> bool:
        """Cek apakah harga coin ini masih fresh (buat WS-priority gate)."""
        with self._lock:
            ts = self._last_update.get(coin)
            return ts is not None and (time.time() - ts) <= max_age


# ============================================================
# WS-1.2: ORDERBOOK L2 STREAM
# ============================================================

class OrderBookStream:
    """Real-time orderbook dari WebSocket."""

    def __init__(self, ws: HyperliquidWebSocket):
        self._ws = ws
        self._books: Dict[str, Dict] = {}
        self._delta_cache: Dict[str, float] = {}
        self._delta_history: Dict[str, deque] = {}
        self._lock = threading.RLock()
        self._dirty_coins: set = set()
        self._dirty_lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable]] = {}
        self._subscribed_coins: set = set()
        self._last_update: Dict[str, float] = {}

        self._ws.on("l2Book", self._on_l2_update)
        logger.info("📊 OrderBookStream ready")

    def subscribe_coins(self, coins: List[str]):
        for coin in coins:
            if coin not in self._subscribed_coins:
                self._subscribed_coins.add(coin)
                self._ws.subscribe_l2(coin)
        logger.info(f"📊 L2 subscribed: {len(self._subscribed_coins)} coins")

    def _on_l2_update(self, data: Dict):
        book_data = data.get("data", {})
        coin = book_data.get("coin")
        levels = book_data.get("levels", {})
        if not coin or not levels:
            return

        # Hyperliquid l2Book "levels" biasanya [bids_list, asks_list]
        if isinstance(levels, list) and len(levels) == 2:
            bids, asks = levels[0], levels[1]
        else:
            bids = levels.get("bids", [])
            asks = levels.get("asks", [])

        delta = self._compute_delta(bids, asks)
        old = self._delta_cache.get(coin, 0.0)
        now = time.time()

        with self._lock:
            self._books[coin] = {"bids": bids, "asks": asks, "delta": delta, "ts": now}
            self._delta_cache[coin] = delta
            self._last_update[coin] = now
            if coin not in self._delta_history:
                self._delta_history[coin] = deque(maxlen=TUNABLE["ROLLING_DELTA_WINDOW"])
            self._delta_history[coin].append(delta)

        if abs(delta - old) >= 1.0:
            with self._dirty_lock:
                self._dirty_coins.add(coin)

        if coin in self._subscribers:
            for cb in self._subscribers[coin]:
                try:
                    cb(coin, bids, asks, delta)
                except Exception:
                    pass

    def _compute_delta(self, bids: List, asks: List) -> float:
        try:
            bid_val = sum(float(b.get('sz', b[1])) * float(b.get('px', b[0])) if isinstance(b, dict) else float(b[1]) * float(b[0]) for b in bids[:5])
            ask_val = sum(float(a.get('sz', a[1])) * float(a.get('px', a[0])) if isinstance(a, dict) else float(a[1]) * float(a[0]) for a in asks[:5])
            if bid_val + ask_val == 0:
                return 0.0
            return (bid_val - ask_val) / (bid_val + ask_val) * 100
        except Exception:
            return 0.0

    def get_delta(self, coin: str) -> float:
        with self._lock:
            return self._delta_cache.get(coin, 0.0)

    def get_delta_history(self, coin: str, n: int = 6) -> List[float]:
        with self._lock:
            if coin not in self._delta_history:
                return []
            return list(self._delta_history[coin])[-n:]

    def get_dirty_coins(self) -> List[str]:
        with self._dirty_lock:
            return list(self._dirty_coins)

    def mark_clean(self, coin: str):
        with self._dirty_lock:
            self._dirty_coins.discard(coin)

    def on_book_update(self, coin: str, cb: Callable):
        with self._lock:
            if coin not in self._subscribers:
                self._subscribers[coin] = []
            self._subscribers[coin].append(cb)

    def is_fresh(self, coin: str, max_age: float = 8.0) -> bool:
        with self._lock:
            ts = self._last_update.get(coin)
            return ts is not None and (time.time() - ts) <= max_age

    @property
    def is_connected(self) -> bool:
        return self._ws.is_connected


# ============================================================
# WS-1.3: TRADE STREAM — sumber volume ASLI (bukan tick-count)
# ============================================================

class TradeStream:
    """
    Real-time fills dari WS "trades" channel. Ini sumber volume yang
    dipakai LiveCandleBuilder — px*sz aggregate beneran, bukan proxy.
    """

    def __init__(self, ws: HyperliquidWebSocket):
        self._ws = ws
        self._lock = threading.RLock()
        self._subscribed_coins: set = set()
        self._subscribers: Dict[str, List[Callable]] = {}
        self._last_update: Dict[str, float] = {}

        self._ws.on("trades", self._on_trades)
        logger.info("📊 TradeStream ready")

    def subscribe_coins(self, coins: List[str]):
        for coin in coins:
            if coin not in self._subscribed_coins:
                self._subscribed_coins.add(coin)
                self._ws.subscribe_trades(coin)
        logger.info(f"📊 Trades subscribed: {len(self._subscribed_coins)} coins")

    def _on_trades(self, data: Dict):
        fills = data.get("data", [])
        if not isinstance(fills, list):
            return
        now = time.time()
        by_coin: Dict[str, List[Dict]] = {}
        for f in fills:
            coin = f.get("coin")
            if not coin:
                continue
            try:
                px = float(f.get("px", 0))
                sz = float(f.get("sz", 0))
                ts = float(f.get("time", now * 1000)) / 1000.0
            except (TypeError, ValueError):
                continue
            if px <= 0 or sz <= 0:
                continue
            by_coin.setdefault(coin, []).append({"px": px, "sz": sz, "ts": ts, "side": f.get("side")})

        for coin, trades in by_coin.items():
            with self._lock:
                self._last_update[coin] = now
            if coin in self._subscribers:
                for cb in self._subscribers[coin]:
                    try:
                        cb(coin, trades)
                    except Exception:
                        pass

    def on_trades(self, coin: str, cb: Callable):
        with self._lock:
            if coin not in self._subscribers:
                self._subscribers[coin] = []
            self._subscribers[coin].append(cb)

    def is_fresh(self, coin: str, max_age: float = 20.0) -> bool:
        with self._lock:
            ts = self._last_update.get(coin)
            return ts is not None and (time.time() - ts) <= max_age

    @property
    def is_connected(self) -> bool:
        return self._ws.is_connected


# ============================================================
# WS-1.4: LIVE CANDLE BUILDER — dibangun dari trade fills ASLI
# ============================================================

class LiveCandleBuilder:
    """
    Build candle real-time dari TradeStream (bukan dari tick harga).
    Volume ('v') = sum(sz) trade beneran di dalam bucket, sama semantik
    kayak REST candles_snapshot Hyperliquid (base-asset volume, dipakai
    di seluruh codebase sebagai float(c['v']) * float(c['c']) buat
    convert ke USD volume).

    Cuma nge-cover coin yang di-subscribe (top-N by OI) — sisanya tetap
    lewat REST get_candles() yang udah ada, gak disentuh.
    """

    TIMEFRAMES = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    MIN_HISTORY_FOR_USE = 10  # candle history minimal sebelum WS dipercaya gantiin REST

    def __init__(self, trades: TradeStream):
        self._trades = trades
        self._candles: Dict[str, Dict[str, Dict]] = {}  # coin -> tf -> {history, live, bucket_start}
        self._lock = threading.RLock()
        self._seeded: set = set()  # (coin, tf) yang udah di-seed dari REST

    def init_candle(self, coin: str, tf: str, history: List[dict]):
        """Seed dari REST candles (wajib dipanggil sebelum WS candle dipercaya)."""
        if tf not in self.TIMEFRAMES or not history:
            return
        with self._lock:
            if coin not in self._candles:
                self._candles[coin] = {}
            # Convert semua candle jadi float-consistent dict, potong ke 100 terakhir
            hist = [dict(c) for c in history[-100:]]
            self._candles[coin][tf] = {"history": hist, "live": None, "bucket_start": None}
            self._seeded.add((coin, tf))
            self._trades.on_trades(coin, self._make_handler(coin))

    def _make_handler(self, coin: str) -> Callable:
        def handler(c: str, trades: List[Dict]):
            self._ingest(c, trades)
        return handler

    def _ingest(self, coin: str, trades: List[Dict]):
        with self._lock:
            if coin not in self._candles:
                return
            for tf, data in self._candles[coin].items():
                interval = self.TIMEFRAMES[tf]
                for t in trades:
                    self._apply_trade(coin, tf, data, t["px"], t["sz"], t["ts"], interval)

    def _apply_trade(self, coin: str, tf: str, data: Dict, px: float, sz: float, ts: float, interval: int):
        bucket_start = int(ts // interval) * interval

        if data["live"] is None:
            data["live"] = {"o": px, "h": px, "l": px, "c": px, "v": sz, "t": bucket_start * 1000}
            data["bucket_start"] = bucket_start
            return

        if bucket_start > data["bucket_start"]:
            # Bucket baru → tutup candle lama, masukin ke history
            closed = data["live"]
            data["history"].append(closed)
            if len(data["history"]) > 100:
                data["history"].pop(0)
            data["live"] = {"o": px, "h": px, "l": px, "c": px, "v": sz, "t": bucket_start * 1000}
            data["bucket_start"] = bucket_start
        elif bucket_start == data["bucket_start"]:
            live = data["live"]
            live["h"] = max(live["h"], px)
            live["l"] = min(live["l"], px)
            live["c"] = px
            live["v"] += sz
        # bucket_start < current: trade telat/out-of-order, abaikan (edge case, gak signifikan)

    def get_candles(self, coin: str, tf: str, n: int = 100) -> List[dict]:
        with self._lock:
            if coin not in self._candles or tf not in self._candles[coin]:
                return []
            data = self._candles[coin][tf]
            result = list(data["history"])
            if data["live"]:
                result.append(dict(data["live"]))
            return result[-n:]

    def has_sufficient_history(self, coin: str, tf: str) -> bool:
        with self._lock:
            if coin not in self._candles or tf not in self._candles[coin]:
                return False
            return len(self._candles[coin][tf]["history"]) >= self.MIN_HISTORY_FOR_USE

    def is_seeded(self, coin: str, tf: str) -> bool:
        return (coin, tf) in self._seeded

    @property
    def is_connected(self) -> bool:
        return self._trades.is_connected


# ============================================================
# WS GLOBALS
# ============================================================
_ws_manager: Optional[HyperliquidWebSocket] = None
_ws_mid: Optional[MidPriceStream] = None
_ws_ob: Optional[OrderBookStream] = None
_ws_trades: Optional[TradeStream] = None
_ws_candle: Optional[LiveCandleBuilder] = None
_ws_lock = threading.RLock()
_ws_healthy = False


def init_websocket():
    """Init WebSocket layer. Non-fatal kalau gagal — bot tetap jalan via REST.

    NOTE: subscribe L2/trades + seed candle history SENGAJA gak dilakuin
    di sini — snapshot OI belum tentu ready pas bootstrap Step 1.5 (masih
    kosong), jadi watchlist bakal 0 coin. Panggil ws_subscribe_watchlist()
    terpisah setelah snapshot pertama ready (bootstrap Step 4).
    """
    global _ws_manager, _ws_mid, _ws_ob, _ws_trades, _ws_candle, _ws_healthy

    with _ws_lock:
        if _ws_manager:
            return

        try:
            logger.info("🌐 Starting WebSocket...")
            _ws_manager = HyperliquidWebSocket()
            _ws_manager.start()

            for _ in range(5):
                time.sleep(0.5)
                if _ws_manager.is_connected:
                    break

            _ws_mid = MidPriceStream(_ws_manager)
            _ws_ob = OrderBookStream(_ws_manager)
            _ws_trades = TradeStream(_ws_manager)
            _ws_candle = LiveCandleBuilder(_ws_trades)
            _ws_healthy = _ws_manager.is_connected

            logger.info(f"✅ WS init: {'CONNECTED' if _ws_healthy else 'FALLBACK to REST (will retry in background)'}")
        except Exception as e:
            logger.error(f"⚠️ WS init failed (non-fatal, bot lanjut via REST): {e}")
            _ws_healthy = False


def ws_subscribe_watchlist(snapshot: 'MarketSnapshot', top_n: int = 8, candle_timeframes: Tuple[str, ...] = ("5m", "1h")):
    """
    Subscribe L2 + trades buat top-N coin by OI, dan seed LiveCandleBuilder
    dari REST candles yang udah ada. Dipanggil SETELAH snapshot pertama
    ready (bootstrap Step 4) — bukan di init_websocket(), biar gak 0
    subscribed kayak sebelumnya.
    """
    if not (_ws_ob and _ws_trades and _ws_candle):
        return
    if not snapshot or not snapshot.oi:
        logger.debug("ws_subscribe_watchlist: snapshot kosong, skip")
        return
    try:
        top = sorted(snapshot.oi.items(), key=lambda x: x[1], reverse=True)[:top_n]
        coins = [c for c, _ in top if c in snapshot.mids]
        if not coins:
            return

        _ws_ob.subscribe_coins(coins)
        _ws_trades.subscribe_coins(coins)

        # Seed candle history dari REST (WS trades cuma nambah dari sekarang,
        # tanpa seed history-nya kosong dan gak kepake sampai 10 candle close)
        for coin in coins:
            for tf in candle_timeframes:
                try:
                    hist = get_candles(coin, tf, 100, force=False)
                    if hist:
                        _ws_candle.init_candle(coin, tf, hist)
                except Exception as e:
                    logger.debug(f"WS candle seed error {coin}/{tf}: {e}")

        logger.info(f"🌐 WS watchlist: {len(coins)} coins subscribed (L2+trades), candle seeded for {candle_timeframes}")
    except Exception as e:
        logger.error(f"ws_subscribe_watchlist error: {e}")


# ============================================================
# P4.7 — EXIT OBSERVER (SHADOW MODE, LOG ONLY)
# ============================================================
# Observer pattern: log actual vs virtual exit. Tidak mengubah exit.
# Setelah _min_samples terpenuhi, is_enabled() → True.
# P4.8 baru aktif kalau trail_avg > actual_avg.
# ============================================================

class ExitObserver:
    """Observe exit performance — shadow mode, no action."""

    def __init__(self, min_samples: int = 100):
        self._samples: List[Dict] = []
        self._lock = threading.RLock()
        self._min_samples = min_samples
        self._enabled = False

    def observe(self, coin: str, pos: "OpenPosition", current_price: float,
                final_pnl: float, mfe: float, mae: float):
        with self._lock:
            denom = max(pos.entry, 0.01)
            if pos.direction == "LONG":
                trail_pnl = (pos.highest * 0.985 - pos.entry) / denom * 100
            else:
                trail_pnl = (pos.entry - pos.lowest * 1.015) / denom * 100

            if pos.direction == "LONG":
                hold_pnl = (current_price - pos.entry) / denom * 100
            else:
                hold_pnl = (pos.entry - current_price) / denom * 100

            self._samples.append({
                "coin": coin,
                "actual": final_pnl,
                "trail": trail_pnl,
                "hold": hold_pnl,
                "mfe": mfe,
                "mae": mae,
                "ts": time.time(),
            })
            if not self._enabled and len(self._samples) >= self._min_samples:
                self._enabled = True
                logger.info(f"EXIT_OBSERVER: enabled after {self._min_samples} samples")

            logger.info(
                f"EXIT_OBSERVER {coin}: "
                f"actual={final_pnl:+.2f}% "
                f"trail={trail_pnl:+.2f}% "
                f"hold={hold_pnl:+.2f}%"
            )

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._samples)
            if n < 10:
                return {"status": "insufficient", "samples": n}
            actuals = [s["actual"] for s in self._samples]
            trails  = [s["trail"]  for s in self._samples]
            holds   = [s["hold"]   for s in self._samples]
            return {
                "status": "enabled" if self._enabled else "observing",
                "samples": n,
                "actual_avg": round(sum(actuals) / n, 3),
                "trail_avg":  round(sum(trails)  / n, 3),
                "hold_avg":   round(sum(holds)   / n, 3),
                "trail_better": sum(1 for a, t in zip(actuals, trails) if t > a),
                "hold_better":  sum(1 for a, h in zip(actuals, holds)  if h > a),
            }

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled


EXIT_OBSERVER = ExitObserver()


# P4.8 — gate check (baru unlock setelah 100 sample + trail_avg > actual_avg)
def should_enable_adaptive_exit() -> bool:
    """P4.8: True only when shadow is both better AND statistically significant."""
    if not EXIT_OBSERVER.is_enabled():
        return False
    stats = EXIT_OBSERVER.get_stats()
    n = stats.get("samples", 0)

    if n < 100:
        logger.info(f"P4.8: Shadow samples {n} < 100, keeping current exit")
        return False

    trail_avg = stats.get("trail_avg", 0)
    actual_avg = stats.get("actual_avg", 0)
    if trail_avg <= actual_avg * 1.10:
        logger.info(
            f"P4.8: Shadow not significantly better "
            f"(trail={trail_avg:.2f}% vs actual={actual_avg:.2f}%), keeping current exit"
        )
        return False

    shadow_wr = get_shadow_win_rate()
    live_wr = get_recent_win_rate(50)
    if live_wr and shadow_wr < live_wr * 1.10:
        logger.info(
            f"P4.8: Shadow WR {shadow_wr:.1f}% not significantly better than "
            f"live {live_wr:.1f}%, keeping current exit"
        )
        return False

    logger.info(
        f"P4.8: ENABLING adaptive exit (trail={trail_avg:.2f}% > actual={actual_avg:.2f}%, "
        f"shadow_wr={shadow_wr:.1f}%)"
    )
    return True


def get_shadow_win_rate() -> float:
    """Shadow win rate dari EXIT_OBSERVER samples (trail_pnl > 0 = win proxy)."""
    try:
        with EXIT_OBSERVER._lock:
            samples = EXIT_OBSERVER._samples
            if len(samples) < 50:
                return 0.0
            wins = sum(1 for s in samples if s["trail"] > 0)
            return wins / len(samples) * 100
    except Exception:
        return 0.0

# ============================================================
# END P4.7/P4.8
# ============================================================

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


def audit_inventory() -> Dict:
    """Real inventory audit: DB pending vs real vs shadow vs TradeManager."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0")
            db_pending = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0 AND execute_accept=1 AND shadow=0")
            pending_real = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM signals WHERE evaluated=0 AND shadow=1")
            pending_shadow = c.fetchone()[0]
        with TRADE_MANAGER._lock:
            tm_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
            tm_total = len(TRADE_MANAGER.positions)
        logger.warning(
            f"INV_AUDIT "
            f"db_pending={db_pending} "
            f"pending_real={pending_real} "
            f"pending_shadow={pending_shadow} "
            f"tm_open={tm_open} "
            f"tm_total={tm_total}"
        )
        return {
            "db_pending": db_pending,
            "pending_real": pending_real,
            "pending_shadow": pending_shadow,
            "tm_open": tm_open,
            "tm_total": tm_total,
        }
    except Exception as e:
        logger.error(f"audit_inventory error: {e}")
        return {}


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

def record_threshold(threshold: int):
    """Record final threshold value into distribution."""
    with _threshold_stats_lock:
        bucket = int(threshold // 5) * 5
        bucket = min(100, max(50, bucket))
        _threshold_stats[bucket] = _threshold_stats.get(bucket, 0) + 1

def get_threshold_distribution() -> Dict[int, int]:
    """Get threshold distribution snapshot."""
    with _threshold_stats_lock:
        return dict(_threshold_stats)

def get_threshold_summary() -> str:
    """Format threshold distribution for display."""
    hist = get_threshold_distribution()
    if not hist:
        return "No threshold data"
    
    lines = []
    total = sum(hist.values())
    for bucket in sorted(hist.keys()):
        count = hist[bucket]
        pct = (count / total * 100) if total > 0 else 0
        bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        lines.append(f"  {bucket}-{bucket+4}: {count:3d} ({pct:4.1f}%) {bar}")
    
    sorted_thresholds = []
    for b, c in hist.items():
        sorted_thresholds.extend([b] * c)
    sorted_thresholds.sort()
    median = sorted_thresholds[len(sorted_thresholds)//2] if sorted_thresholds else 0
    p90 = sorted_thresholds[int(len(sorted_thresholds)*0.9)] if sorted_thresholds else 0
    
    return f"Median: {median}, P90: {p90}\n" + "\n".join(lines)


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



def calculate_scaled_targets(entry: float, direction: str, atr_pct: float, market_regime: str,
                              rr_multiplier: float = 1.0) -> Dict:
    """P1 + TP-RR Boost: Multi-TP dengan scaling berdasarkan regime.
    rr_multiplier dari compute_tp_rr_boost() di-apply ke SEMUA level TP
    secara proporsional — tp1/tp2/tp3 tetap preserve spacing relatifnya,
    tapi seluruh struktur shift lebih jauh dari entry kalau boost positif.
    """
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

    # ===== TP-RR BOOST: scale semua level TP secara proporsional =====
    if rr_multiplier != 1.0:
        for _tp_ref, _tp_val in [("tp1", tp1), ("tp2", tp2), ("tp3", tp3)]:
            _dist = abs(_tp_val - entry) * rr_multiplier
            if direction == "LONG":
                if _tp_ref == "tp1": tp1 = entry + _dist
                elif _tp_ref == "tp2": tp2 = entry + _dist
                else: tp3 = entry + _dist
            else:
                if _tp_ref == "tp1": tp1 = entry - _dist
                elif _tp_ref == "tp2": tp2 = entry - _dist
                else: tp3 = entry - _dist

    return {
        # LAYER 5: TP Unlock Mode — TP1 pay risk (kecilkan exposure cepat),
        # TP2 unlock trail (Exit State Machine sudah aktif race antara TP3
        # fixed price vs trailing exit di check_all_positions), TP3/runner
        # dapat porsi TERBESAR karena itu yang "free ride" — bukan exit
        # cepat di harga tetap, tapi dibiarkan trail selama Exit State
        # Machine bilang masih CAPTURE/belum DEFEND.
        "tp1": {"price": tp1, "size_pct": 0.30, "label": "TP1 (30%, pay risk)"},
        "tp2": {"price": tp2, "size_pct": 0.30, "label": "TP2 (30%, unlock trail)"},
        "tp3": {"price": tp3, "size_pct": 0.40, "label": "TP3/Runner (40%, free ride)"},
    }

def get_adaptive_threshold(market_regime: str, entropy_market: int, recent_win_rate: float, execution_count: int = 0) -> int:
    """P2: Adaptive threshold lowering (75 → 60-65)
    
    [P0 LIFECYCLE FIX #3] Use actual open_positions instead of exec_count
    [P2 PATCH] Blend recent_win_rate (journal-based, caller-provided) dengan
               effective_win_rate (closed-trades-based, dari DB) supaya gak
               cuma ngandelin journal mentah yang bisa kena noise/outlier.
    """
    
    base = 65  # Baseline turun dari 75

    # === P2: EFFECTIVE WR DARI CLOSED TRADES (DB), di-blend sama recent_win_rate ===
    try:
        effective_wr = get_effective_win_rate()
    except Exception:
        effective_wr = recent_win_rate
    blended_wr = (0.5 * recent_win_rate) + (0.5 * effective_wr)
    
    # === INSTRUMENTATION: THRESHOLD_START ===
    logger.info(
        f"THRESHOLD_START "
        f"regime={market_regime} "
        f"entropy={entropy_market} "
        f"wr={recent_win_rate:.2f} "
        f"eff_wr={effective_wr:.2f} "
        f"blended_wr={blended_wr:.2f} "
        f"exec_count={execution_count} "
        f"base={base}"
    )
    
    if market_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        base -= 5  # 60 — trending = agresif
    elif market_regime == "RANGING":
        base += 3  # 68 — ranging = selective
    elif market_regime == "EXPANDING":
        base -= 2  # 63 — expansion = balanced

    # ===== P4.15: REGIME THRESHOLD MODIFIER (bukan score multiplier) =====
    # Threshold modifier = buka/tutup pintu, BUKAN poles score
    _regime_threshold_adj = {
        "RANGING":       0,
        "TRENDING_UP":  -3,   # trend bagus → turunin sedikit barrier
        "TRENDING_DOWN":-3,
        "TRANSITION":   +5,   # pasar bingung → lebih selektif
        "PANIC":       +10,
        "CHAOS":       +15,
        "EXPANDING":    0,
    }
    regime_adj = _regime_threshold_adj.get(market_regime, 0)
    base += regime_adj
    if regime_adj != 0:
        logger.debug(f"P4.15 REGIME_THRESHOLD_ADJ {market_regime}: {regime_adj:+d} → base={base}")
    # ===== END P4.15 =====
    
    if entropy_market < 30:
        base -= 3  # Structure clear
    elif entropy_market > 60:
        base += 5  # Chaos
    
    if blended_wr > 0.65:
        base -= 5  # High WR = confidence
    elif blended_wr > 0.55:
        base -= 2
    elif blended_wr == 0.0:
        base = int(base * 1.3)  # ZERO WR = hukuman keras, threshold +30%
    elif blended_wr < 0.35:
        base += 5  # Low WR = protect
    
    # === P1: INVENTORY — MANAGED POSITIONS ONLY (not DB pending) ===
    with TRADE_MANAGER._lock:
        managed_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
    
    # Use ONLY managed_open for exposure calculation
    # DB pending is orphan historical data, not active risk
    open_positions = managed_open
    
    if open_positions > 20:
        exposure_penalty = min(15, open_positions * 1)  # Lebih soft
        base += exposure_penalty
        logger.info(f"🔴 EXPOSURE_PENALTY: {open_positions} managed open → +{exposure_penalty} to threshold")
    elif open_positions > 10:
        base += 5
        logger.info(f"🟡 MODERATE_EXPOSURE: {open_positions} managed → +5 to threshold")
    else:
        # Low inventory: relax hati-hati (max -5)
        if open_positions < 5:
            base -= 5  # was -10
            logger.info(f"🟢 LOW_INVENTORY: {open_positions} managed → -5 to threshold")
        elif open_positions < 10:
            base -= 3  # was -5
            logger.info(f"🟢 LOW_INVENTORY: {open_positions} managed → -3 to threshold")
    
    final = max(60, min(90, base))  # was max(50, min(95))
    
    # ===== P3: THRESH_TRACE =====
    logger.info(
        f"THRESH_TRACE "
        f"base={base} "
        f"inventory_penalty={open_positions * 2 if open_positions > 3 else 0} "
        f"regime={market_regime} "
        f"entropy_adj={5 if entropy_market > 60 else (-3 if entropy_market < 30 else 0)} "
        f"final={final}"
    )
    # ============================
    
    logger.debug(f"📊 P2: THRESHOLD: base={base} (regime={market_regime}, entropy={entropy_market}, wr={blended_wr:.0%}, open={open_positions}) → final={final}")
    
    return final


# ============================================================
# P4.17 — EXECUTION PERSONALITY (Rolling 30 Trades)
# ============================================================
# Ganti lifetime PnL dengan rolling window agar adaptif.
# ============================================================

def get_execution_personality() -> Tuple[str, int]:
    """
    P4.17: Rolling 30-trade personality.
    Returns (mode: str, threshold_adj: int)
    """
    try:
        with _journal_lock:
            executed = [e for e in _decision_journal if getattr(e, "executed", False)]
            recent = executed[-30:]
            shadows = [e for e in _decision_journal if getattr(e, "shadow", False)][-30:]

        n = len(recent)
        if n < 5:
            return "NORMAL", 0   # insufficient data

        wins = sum(1 for e in recent if getattr(e, "outcome", None) in ("TP_HIT", "PARTIAL_WIN"))
        recent_wr = wins / n * 100

        shadow_n = len(shadows)
        shadow_conversion = (n / (n + shadow_n) * 100) if (n + shadow_n) > 0 else 0

        if n < 3 and shadow_conversion > 15:
            mode, adj = "AGGRESSIVE", -3
        elif recent_wr < 30 and n > 10:
            mode, adj = "DEFENSIVE", +5
        else:
            mode, adj = "NORMAL", 0

        logger.debug(
            f"P4.17 PERSONALITY: mode={mode} adj={adj:+d} "
            f"recent_wr={recent_wr:.1f}% n={n} shadow_conv={shadow_conversion:.1f}%"
        )
        return mode, adj
    except Exception as e:
        logger.debug(f"get_execution_personality error: {e}")
        return "NORMAL", 0

# ============================================================
# END P4.17
# ============================================================


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
    
    def get_with_ts(self, key: str) -> Optional[Tuple[Any, float]]:
        """Return (value, timestamp) or None if key not exists."""
        with self._get_lock(key):
            if key not in self._data:
                return None
            value, ts = self._data[key]
            return value, ts

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

def api_cooldown_remaining() -> float:
    """Return remaining cooldown seconds (0 if none). NON-BLOCKING."""
    with _api_cooldown_lock:
        return max(0.0, _api_cooldown_until - time.time())

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
            trigger_api_cooldown(15)  # P4.53: 25→15
            logger.error(f"🚫 Rate limit hit on {func.__name__}, cooldown activated")
        raise

# ========== P0: PER-ENDPOINT HEALTH (jalan BARENG global cooldown di atas) ==========
# Global cooldown = emergency brake kalau situasi udah parah / gak jelas
# endpoint mana yang bermasalah. Endpoint health = circuit breaker granular
# per endpoint, biar candles kena 429 gak ikut nge-block snapshot/meta yang
# masih sehat. Caller sebaiknya cek DUA-DUANYA (can_call_api() DAN
# can_call_endpoint(name)) — kalau salah satu bilang blocked, ya blocked.
_API_HEALTH: Dict[str, Dict[str, Any]] = {
    "candles": {"blocked_until": 0.0, "fail_count": 0, "success_count": 0},
    "snapshot": {"blocked_until": 0.0, "fail_count": 0, "success_count": 0},
    "meta": {"blocked_until": 0.0, "fail_count": 0, "success_count": 0},
    "l2": {"blocked_until": 0.0, "fail_count": 0, "success_count": 0},
    "trades": {"blocked_until": 0.0, "fail_count": 0, "success_count": 0},
}
_API_HEALTH_LOCK = threading.RLock()

def can_call_endpoint(name: str) -> bool:
    """Cek apakah endpoint spesifik boleh dipanggil (circuit breaker granular)."""
    with _API_HEALTH_LOCK:
        h = _API_HEALTH.get(name)
        if h is None:
            return True
        return time.time() > h["blocked_until"]

def mark_endpoint_success(name: str):
    """Recovery gradual: turunkan fail_count kalau sukses."""
    with _API_HEALTH_LOCK:
        h = _API_HEALTH.setdefault(name, {"blocked_until": 0.0, "fail_count": 0, "success_count": 0})
        if h["fail_count"] > 0:
            h["fail_count"] -= 1
        h["success_count"] += 1

def mark_endpoint_failure(name: str):
    """Exponential backoff per endpoint, bukan global. Cap di 30s."""
    with _API_HEALTH_LOCK:
        h = _API_HEALTH.setdefault(name, {"blocked_until": 0.0, "fail_count": 0, "success_count": 0})
        h["fail_count"] += 1
        cooldown = min(2 ** h["fail_count"], 30)
        h["blocked_until"] = time.time() + cooldown
        logger.warning(f"🔴 Endpoint {name} failed, cooldown {cooldown}s (fail#{h['fail_count']})")

def get_endpoint_health_summary() -> str:
    """Telemetry: status semua endpoint (buat log per cycle)."""
    with _API_HEALTH_LOCK:
        now = time.time()
        lines = []
        for name, h in _API_HEALTH.items():
            blocked = now < h["blocked_until"]
            status = "BLOCKED" if blocked else "OK"
            lines.append(f"{name}={status}(fail={h['fail_count']})")
        return " | ".join(lines)
    
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
    score: float = 0.0           # P4.x: event-level score (for weak-structure penalty)
    mid: Optional[float] = None  # P4.x: optional midpoint price

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

# ===== P4.56: NATIVE MAX LEVERAGE PER COIN =====
# Hyperliquid's meta() response includes "maxLeverage" per asset in
# universe[] — BTC/ETH biasanya 40-50x, mid-cap 10-25x, small-cap 3-10x,
# ditentukan EXCHANGE sendiri (margin tier risk mereka), bukan list manual
# yang gw atau siapapun hardcode. Di-refresh tiap kali snapshot di-refresh.
_max_leverage_cache: Dict[str, int] = {}
_max_leverage_lock = threading.RLock()

def update_max_leverage_cache(meta):
    """Extract maxLeverage per coin dari raw meta() response. Aman dipanggil
    berkali-kali — cuma overwrite dict, gak pernah gagal exception ke caller."""
    try:
        universe = meta[0].get("universe", []) if meta and len(meta) > 0 else []
        with _max_leverage_lock:
            for asset in universe:
                name = asset.get("name")
                lev = asset.get("maxLeverage")
                if name and lev:
                    _max_leverage_cache[name] = int(lev)
    except Exception as e:
        logger.debug(f"update_max_leverage_cache failed (non-fatal): {e}")

def get_max_leverage(coin: str, default: int = 5) -> int:
    """Native max leverage Hyperliquid untuk coin ini. Fallback ke default
    yang konservatif kalau belum ada di cache (misal coin baru listing atau
    cache belum pernah di-populate)."""
    with _max_leverage_lock:
        return _max_leverage_cache.get(coin, default)

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
    journal_accept: bool = True           # FIX: apakah entry ini lolos ke journal
    execute_accept: bool = False          # FIX: apakah lolos threshold & di-execute
    blocked_reason: Optional[str] = None  # FIX: kenapa gak execute (score_below, micro_reject, dll)

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
class EntryZone:
    """L1 Entry Window: Entry zone dengan quality scoring."""
    zone_low: float
    zone_high: float
    optimal_entry: float
    entry_quality: float  # 0-100
    sl_distance_pct: float
    confidence: float
    components: Dict[str, float] = field(default_factory=dict)
    impulse_distance_minutes: float = 0.0

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

# ========== ATTENTION ENGINE V2: STATE ==========
@dataclass
class AttentionState:
    """Attention bukan angka, tapi objek dengan memori."""
    score: float                # 0-1, EMA smoothed
    confidence: float           # 0-1, seberapa reliable data ini
    momentum: float             # rate of change (0-1)
    freshness: float            # 0-1, seberapa baru data
    components: Dict[str, float]  # OI, delta, vol, shock, sector, flow
    ts: float                   # last update

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
    """Data-driven warmup — forward ke _is_warmup_data_driven() setelah state ready.
    FIX A4: versi lama pakai uptime 1800s, salah kalau data OI/delta udah cukup.
    Fallback ke uptime 60s kalau state belum diinit."""
    try:
        return _is_warmup_data_driven()
    except Exception:
        return (time.time() - START_TIME) < 60

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

# ===== FIX #3: delta distribution history per-coin (z-score scoring) =====
# Beda dari _rolling_delta (window=6, dipakai buat delta SHIFT jangka pendek
# di get_delta_shift). Ini window lebih panjang, tujuannya nyimpen distribusi
# delta historis coin ini sendiri, buat tau apa itu "delta tinggi" buat coin
# tsb secara statistik — bukan threshold absolut yang sama buat semua coin.
_delta_distribution: Dict[str, deque] = {}
_delta_distribution_lock = threading.RLock()
_DELTA_DISTRIBUTION_WINDOW = 100
_DELTA_ZSCORE_MIN_SAMPLES = 20

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

# ============================================================
# P4.7 — REJECT TELEMETRY
# ============================================================
_reject_telemetry: Dict[str, Any] = {
    "stage": {},
    "total": 0,
    "last_reset": time.time(),
}
_reject_lock = threading.RLock()
_REJECT_WINDOW = 3600  # 1h rolling

def record_reject(stage: str, reason: str, score: float = None):
    """Catat penolakan dengan stage dan reason (1h rolling)."""
    with _reject_lock:
        now = time.time()
        if now - _reject_telemetry["last_reset"] > _REJECT_WINDOW:
            _reject_telemetry["stage"] = {}
            _reject_telemetry["total"] = 0
            _reject_telemetry["last_reset"] = now
        _reject_telemetry["total"] += 1
        _reject_telemetry["stage"].setdefault(stage, {})
        _reject_telemetry["stage"][stage][reason] = _reject_telemetry["stage"][stage].get(reason, 0) + 1

def get_reject_summary() -> Dict[str, Any]:
    with _reject_lock:
        return {
            "total": _reject_telemetry["total"],
            "stage": {k: dict(v) for k, v in _reject_telemetry["stage"].items()},
            "window": _REJECT_WINDOW,
        }

# ============================================================
# P4.7.5 — GATE YIELD
# ============================================================
_gate_metrics: Dict[str, Dict[str, int]] = {
    "obs":        {"seen": 0, "pass": 0},
    "thesis":     {"seen": 0, "pass": 0},
    "confidence": {"seen": 0, "pass": 0},
    "conviction": {"seen": 0, "pass": 0},
    "micro":      {"seen": 0, "pass": 0},
    "execution":  {"seen": 0, "pass": 0},
}
_gate_lock = threading.RLock()

def record_gate_seen(gate: str):
    with _gate_lock:
        _gate_metrics.setdefault(gate, {"seen": 0, "pass": 0})
        _gate_metrics[gate]["seen"] += 1

def record_gate_pass(gate: str):
    with _gate_lock:
        _gate_metrics.setdefault(gate, {"seen": 0, "pass": 0})
        _gate_metrics[gate]["pass"] += 1

def get_gate_yield() -> Dict[str, Any]:
    with _gate_lock:
        result = {}
        for gate, m in _gate_metrics.items():
            seen = m["seen"]
            passed = m["pass"]
            result[gate] = {
                "seen": seen,
                "pass": passed,
                "yield": round(passed / seen * 100, 1) if seen > 0 else 0.0,
            }
        return result

# ============================================================
# END P4.7 / P4.7.5
# ============================================================

# ============================================================
# P4.19 — REGIME × EXEC MATRIX
# ============================================================
_regime_exec_matrix: Dict[str, Dict] = {
    "RANGING":       {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0},
    "TRENDING_UP":   {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0},
    "TRENDING_DOWN": {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0},
    "TRANSITION":    {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0},
    "PANIC":         {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0},
}
_regime_exec_lock = threading.RLock()

def record_regime_exec(regime: str, stage: str, pnl: float = None, outcome: str = None):
    """Track obs/exec per regime."""
    with _regime_exec_lock:
        if regime not in _regime_exec_matrix:
            _regime_exec_matrix[regime] = {"obs": 0, "exec": 0, "pnl": 0.0, "wins": 0}
        if stage == "OBS":
            _regime_exec_matrix[regime]["obs"] += 1
        elif stage == "EXEC":
            _regime_exec_matrix[regime]["exec"] += 1
            if pnl is not None:
                _regime_exec_matrix[regime]["pnl"] += pnl
            if outcome in ("TP_HIT", "PARTIAL_WIN"):
                _regime_exec_matrix[regime]["wins"] += 1

def get_regime_exec_matrix() -> Dict[str, Dict]:
    with _regime_exec_lock:
        result = {}
        for regime, d in _regime_exec_matrix.items():
            obs, execs, wins, pnl = d["obs"], d["exec"], d["wins"], d["pnl"]
            result[regime] = {
                "obs":     obs,
                "exec":    execs,
                "yield":   round(execs / obs * 100, 1) if obs > 0 else 0.0,
                "wr":      round(wins / execs * 100, 1) if execs > 0 else 0.0,
                "avg_pnl": round(pnl / execs, 2) if execs > 0 else 0.0,
            }
        return result

# ============================================================
# END P4.19
# ============================================================

# ============================================================
# P4.20 — EXEC DISTANCE (Reject Gap Tracking)
# ============================================================
_reject_gaps: List[float] = []
_reject_gaps_lock = threading.RLock()
_REJECT_GAP_WINDOW = 100

def record_reject_gap(gap: float):
    """Record how far rejected signals were from threshold (last 100)."""
    with _reject_gaps_lock:
        _reject_gaps.append(gap)
        if len(_reject_gaps) > _REJECT_GAP_WINDOW:
            _reject_gaps.pop(0)

def get_reject_gap_stats() -> Dict[str, Any]:
    with _reject_gaps_lock:
        if not _reject_gaps:
            return {"avg": 0, "min": 0, "max": 0, "p50": 0, "p90": 0, "n": 0}
        s = sorted(_reject_gaps)
        n = len(s)
        return {
            "avg": round(sum(s) / n, 1),
            "min": round(s[0], 1),
            "max": round(s[-1], 1),
            "p50": round(s[n // 2], 1),
            "p90": round(s[int(n * 0.9)], 1),
            "n":   n,
        }

# ============================================================
# END P4.20
# ============================================================

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

# ============================================================
# V10.4 P0 FIX: ENTRY COOLDOWN (CEGAH DUPLIKAT) - REFINED
# ============================================================
_entry_cooldown: Dict[str, float] = {}
_entry_cooldown_lock = threading.RLock()
_ENTRY_COOLDOWN_SECONDS = 300  # 5 menit

_market_sanity: Dict[str, Any] = {"is_sane": True, "last_check": 0.0, "reason": ""}
_market_sanity_lock = threading.RLock()

_decision_traces: deque = deque(maxlen=TUNABLE["MAX_TRACES"])
_trace_lock = threading.RLock()

# V10: context snapshot — FIX bug #1: global single-slot cache dihapus,
# diganti _context_cache (per-coin dict, lihat get_context_snapshot di bawah)

# V10: snapshot state
_SNAPSHOT_TTL: int = 5
_last_snapshot: Optional[MarketSnapshot] = None
_snapshot_lock = threading.RLock()

_last_funnel_log = 0.0  # V10.4: throttle FUNNEL_SNAPSHOT ke 5 menit

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
        # ===== THRESHOLD DISTRIBUTION =====
_threshold_stats: Dict[int, int] = {}
_threshold_stats_lock = threading.RLock()
# ===== CONFIDENCE HISTOGRAM =====
_conf_histogram: Dict[int, int] = {}
_conf_histogram_window: deque = deque(maxlen=1000)
_conf_histogram_lock = threading.RLock()
    # ===== CONVERSION FUNNEL =====
_conversion_funnel: Dict[str, int] = {
    "obs_pass": 0,
    "thesis_pass": 0,
    "conf_pass": 0,
    "exec_pass": 0,
    "open_count": 0,
    "tp1_hit": 0,
    "tp2_hit": 0,
    "tp3_hit": 0,
}
_conversion_funnel_lock = threading.RLock()

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

def record_funnel_stage(stage: str, n: int = 1):
    with _conversion_funnel_lock:
        _conversion_funnel[stage] = _conversion_funnel.get(stage, 0) + n

def reset_funnel():
    with _conversion_funnel_lock:
        for k in _conversion_funnel:
            _conversion_funnel[k] = 0

def get_funnel_summary() -> str:
    with _conversion_funnel_lock:
        f = _conversion_funnel
        obs = f.get("obs_pass", 0)
        thesis = f.get("thesis_pass", 0)
        conf = f.get("conf_pass", 0)
        exec_pass = f.get("exec_pass", 0)
        
        if obs == 0:
            return "No funnel data"
        
        lines = [
            f"OBS → THESIS: {thesis}/{obs} ({thesis/obs*100:.0f}%)" if obs > 0 else "OBS → THESIS: 0/0",
            f"THESIS → CONF: {conf}/{thesis} ({conf/thesis*100:.0f}%)" if thesis > 0 else "THESIS → CONF: 0/0",
            f"CONF → EXEC: {exec_pass}/{conf} ({exec_pass/conf*100:.0f}%)" if conf > 0 else "CONF → EXEC: 0/0",
            f"EXEC → OPEN: {f.get('open_count', 0)}",
            f"TP1: {f.get('tp1_hit', 0)} | TP2: {f.get('tp2_hit', 0)} | TP3: {f.get('tp3_hit', 0)}",
        ]
        return "\n".join(lines)

# ============================================================
# P4: WARNING AGGREGATION — biar log gak spam pas degraded mode nyala lama
# ============================================================
_WARN_COUNTERS: Dict[str, Dict[str, Any]] = {}
_WARN_COUNTERS_LOCK = threading.RLock()
_WARN_COUNTERS_TTL = 300  # flush ke log tiap 5 menit per kategori

def log_warn_aggregated(category: str, msg: str, increment: int = 1):
    """Log warning dengan counter, bukan spam tiap cycle."""
    with _WARN_COUNTERS_LOCK:
        now = time.time()
        counter = _WARN_COUNTERS.setdefault(category, {"count": 0, "last_log": 0.0, "last_msg": ""})
        counter["count"] += increment
        counter["last_msg"] = msg
        if now - counter["last_log"] > _WARN_COUNTERS_TTL:
            logger.warning(f"[{category}] {msg} (count={counter['count']})")
            counter["last_log"] = now
            counter["count"] = 0

def flush_warn_summary():
    """Log ringkasan semua warning yang terakumulasi, dipanggil di akhir tiap cycle."""
    with _WARN_COUNTERS_LOCK:
        summary = [f"{cat}={data['count']}" for cat, data in _WARN_COUNTERS.items() if data["count"] > 0]
        if summary:
            logger.info(f"📊 WARN SUMMARY: {' | '.join(summary)}")
        _WARN_COUNTERS.clear()


# ============================================================
# PIPELINE COUNTERS
# ============================================================

_exec_pipeline: Dict[str, int] = {}
_exec_pipeline_lock = threading.RLock()
_exec_pipeline_reset_ts: float = time.time()

# ===== P4.55: LAST PIPELINE RESULTS (for /entry without args) =====
# Snapshot of the most recent process_candidates_deep() alert list, so
# /entry can show real THESIS-stage results instead of re-running
# build_candidate_pool() (DISCOVERY-only, not yet thesis-checked).
_last_pipeline_results: List[dict] = []
_last_pipeline_results_lock = threading.RLock()
_last_pipeline_results_ts: float = 0.0

def set_last_pipeline_results(alerts: List[dict]):
    global _last_pipeline_results_ts
    with _last_pipeline_results_lock:
        _last_pipeline_results.clear()
        _last_pipeline_results.extend(alerts)
        _last_pipeline_results_ts = time.time()

def get_last_pipeline_results() -> Tuple[List[dict], float]:
    with _last_pipeline_results_lock:
        return list(_last_pipeline_results), _last_pipeline_results_ts

def inc_pipeline_counter(key: str, n: int = 1):
    with _exec_pipeline_lock:
        _exec_pipeline[key] = _exec_pipeline.get(key, 0) + n

def reset_pipeline_counter():
    global _exec_pipeline_reset_ts
    with _exec_pipeline_lock:
        _exec_pipeline.clear()
        _exec_pipeline_reset_ts = time.time()

# ============================================================
# ENGINE LIFECYCLE — READINESS SCORE + 3 PHASE (BOOT → WARMUP → LIVE)
# ============================================================
# Kenapa ini ada: cold start (baru boot) itu inherent — 229/230 coin belum
# pernah punya candle cache. Tanpa sinyal ini, log keliatan "gelagapan"
# (scan-skip-scan-skip berulang) padahal itu proses normal ngumpulin data.
# Readiness kasih operator sinyal jelas "ini emang lagi warmup" vs "ini
# beneran stuck", dan phase dipakai buat nurunin ekspektasi (cache TTL
# lebih longgar, discovery lebih kecil) selama masa warmup biar gak
# maksa fetch/skip yang sia-sia.
#
# 3 komponen dipilih (bukan 5) karena masing-masing punya sumber data yang
# BENERAN ada di engine ini sekarang — bukan istilah baru tanpa fungsi:
#   - cache_health: rasio candle cache yang fresh dari sample top coin
#   - history_depth: kedalaman OI history BTC (proxy umur data engine)
#   - api_health: rasio endpoint yang gak lagi di-cooldown (dari _API_HEALTH)
_ENGINE_START_TS = time.time()
_READINESS_HISTORY_TARGET = 60  # OI history BTC dianggap "cukup dalam" di 60 titik (~30 menit @30s/refresh)

def compute_cache_health(sample_size: int = 10) -> float:
    """Rasio candle 1h cache yang fresh (<300s) dari sample top coin by OI."""
    try:
        snapshot = CACHE.get("snapshot")
        if not snapshot or not snapshot.oi:
            return 0.0
        top_coins = sorted(snapshot.oi.items(), key=lambda x: x[1], reverse=True)[:sample_size]
        if not top_coins:
            return 0.0
        fresh = 0
        for coin, _ in top_coins:
            key = f"candles_{coin}_1h_100"
            cached = CACHE.get_with_ts(key)
            if cached is not None and (time.time() - cached[1]) < 300:
                fresh += 1
        return fresh / len(top_coins)
    except Exception:
        return 0.0

def compute_history_depth() -> float:
    """Proxy kedalaman data engine dari panjang OI history BTC. 0-1."""
    try:
        with _oi_lock:
            depth = len(_oi_history.get("BTC", []))
        return min(1.0, depth / _READINESS_HISTORY_TARGET)
    except Exception:
        return 0.0

def compute_api_health_score() -> float:
    """Rasio endpoint yang sehat (gak lagi di-cooldown) dari _API_HEALTH."""
    try:
        with _API_HEALTH_LOCK:
            now = time.time()
            total = len(_API_HEALTH)
            if total == 0:
                return 1.0
            healthy = sum(1 for h in _API_HEALTH.values() if now >= h["blocked_until"])
            return healthy / total
    except Exception:
        return 1.0

def compute_engine_readiness() -> Dict[str, Any]:
    """Readiness 0-100 dari 3 komponen. Dipanggil murah (semua sumbernya
    cache/state lokal, gak ada API call baru)."""
    cache_health = compute_cache_health()
    history_depth = compute_history_depth()
    api_health = compute_api_health_score()

    score = (cache_health * 0.45 + history_depth * 0.35 + api_health * 0.20) * 100
    return {
        "readiness": round(score, 1),
        "cache_health": round(cache_health, 2),
        "history_depth": round(history_depth, 2),
        "api_health": round(api_health, 2),
    }

def get_engine_phase() -> Tuple[str, Dict[str, Any]]:
    """3 phase: BOOT (<35) -> WARMUP (35-70) -> LIVE (>=70).
    Age-gated juga: minimal 60s uptime sebelum bisa lewat dari BOOT, biar
    gak lompat fase cuma karena kebetulan sample awal keliatan bagus."""
    r = compute_engine_readiness()
    age = time.time() - _ENGINE_START_TS
    score = r["readiness"]

    if age < 60 or score < 35:
        phase = "BOOT"
    elif score < 70:
        phase = "WARMUP"
    else:
        phase = "LIVE"

    r["phase"] = phase
    r["age_s"] = round(age)
    return phase, r

# Cache TTL grace per phase — dipakai sebagai MAX_CACHE_AGE_THESIS dinamis
# di process_candidates_deep(), gantiin angka statis 300s.
_PHASE_CACHE_GRACE = {"BOOT": 1800, "WARMUP": 600, "LIVE": 300}

def get_cache_grace_for_phase(phase: str) -> int:
    return _PHASE_CACHE_GRACE.get(phase, 300)

# Discovery candidate limit per phase — biar BOOT/WARMUP gak maksa scan
# 12 coin sekaligus pas cache masih kosong (buang waktu ke skip semua).
_PHASE_CANDIDATE_LIMIT = {"BOOT": 4, "WARMUP": 8, "LIVE": 12}

def get_candidate_limit_for_phase(phase: str) -> int:
    return _PHASE_CANDIDATE_LIMIT.get(phase, 12)


# ============================================================
# V2 STRUCTURE ENGINE — SCORING HELPERS
# ============================================================

def pct_score(hist: deque, value: float) -> float:
    """Percentile rank 0-100. Returns 50 if insufficient data."""
    if not hist or len(hist) < 10:
        return 50.0
    arr = sorted(hist)
    n = len(arr)
    idx = bisect_left(arr, value)
    if idx == 0:
        return 0.0
    if idx >= n:
        return 100.0
    lower, upper = arr[idx-1], arr[idx]
    if upper == lower:
        return (idx / n) * 100
    raw = (idx + (value - lower) / (upper - lower)) / n * 100
    return min(100, max(0, raw))

def z_score(hist: deque, value: float) -> float:
    """Z-score clipped to -3..3. Returns 0 if insufficient data."""
    if len(hist) < 10:
        return 0.0
    arr = list(hist)
    mean = np.mean(arr)
    std = np.std(arr)
    if std == 0:
        return 0.0
    return max(-3, min(3, (value - mean) / std))

def combined_score(hist: deque, value: float, pct_weight: float = 0.7) -> float:
    """
    Gabungan percentile + zscore, dinormalisasi ke 0-100.
    pct_weight: bobot percentile (sisanya untuk zscore).
    """
    pct = pct_score(hist, value) / 100.0
    z = (z_score(hist, value) + 3) / 6.0  # -3..3 -> 0..1
    raw = pct * pct_weight + z * (1 - pct_weight)
    return min(100, max(0, raw * 100))

def compute_detector_confidence(hist_len: int, data_freshness: float, event_quality: float) -> float:
    """
    Confidence KHUSUS detector V2 — JANGAN TERTUKAR dengan compute_confidence()
    (fungsi pipeline utama, thesis-level, sudah ada sebelum V2). Ini scoped
    ke satu event/detector saja: history_quality × data_quality × event_quality.
    - hist_len: jumlah sample di rolling history
    - data_freshness: 0-1, seberapa fresh datanya (cache age)
    - event_quality: 0-1, kualitas event itu sendiri
    """
    hist_quality = min(1.0, hist_len / 50)  # 50 samples = full confidence
    data_quality = max(0.3, min(1.0, data_freshness))
    event_quality = max(0.3, min(1.0, event_quality))
    return hist_quality * data_quality * event_quality

def freshness_from_retests(retest_count: int) -> float:
    """Gradual freshness decay per retest."""
    mapping = {0: 1.0, 1: 0.85, 2: 0.70, 3: 0.55, 4: 0.40}
    return mapping.get(retest_count, 0.25)

def price_distance_pct(price1: float, price2: float) -> float:
    """Percentage distance between two prices."""
    if price1 == 0 or price2 == 0:
        return 999.0
    return abs(price1 - price2) / max(price1, price2) * 100


# ============================================================
# P3: FLOW STALL RESCUE — deteksi pipeline mati (scan>0 tapi obs/exec=0
# berturut-turut) dan otomatis masuk mode RESCUE: kurangi jumlah kandidat,
# biar cycle berikutnya lebih ringan dan gampang pulih sendiri tanpa
# perlu restart manual / ubah ENV.
# ============================================================
_ENGINE_MODE: Dict[str, str] = {"mode": "NORMAL"}  # NORMAL | RESCUE
_ENGINE_MODE_LOCK = threading.RLock()
_FLOW_STALL: Dict[str, Any] = {"cycles": 0}
_FLOW_STALL_LOCK = threading.RLock()

def check_flow_stall_and_rescue() -> bool:
    """Cek metrics pipeline cycle terakhir; kalau stall 3x berturut-turut,
    masuk RESCUE mode. Kalau flow udah jalan lagi (obs>0 & exec_pass>0),
    balik ke NORMAL otomatis. Return True kalau baru saja trigger rescue."""
    pipe = get_pipeline_metrics()
    scan = pipe.get("scan_total", 0)
    obs = pipe.get("obs", 0)
    exec_pass = pipe.get("execute_pass", 0)

    triggered = False
    with _FLOW_STALL_LOCK:
        if scan > 0 and obs == 0 and exec_pass == 0:
            _FLOW_STALL["cycles"] += 1
        else:
            _FLOW_STALL["cycles"] = 0

        if _FLOW_STALL["cycles"] >= 3:
            with _ENGINE_MODE_LOCK:
                if _ENGINE_MODE["mode"] != "RESCUE":
                    _ENGINE_MODE["mode"] = "RESCUE"
                    logger.warning("🚨 FLOW STALL: entering RESCUE mode (reduced candidates)")
                    triggered = True
            _FLOW_STALL["cycles"] = 0

    if obs > 0 and exec_pass > 0:
        with _ENGINE_MODE_LOCK:
            if _ENGINE_MODE["mode"] == "RESCUE":
                _ENGINE_MODE["mode"] = "NORMAL"
                logger.info("✅ FLOW RECOVERED: returning to NORMAL mode")

    return triggered


def get_engine_mode() -> str:
    with _ENGINE_MODE_LOCK:
        return _ENGINE_MODE["mode"]


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
        # P4.53 — soft cooldown observability
        scan_total = _exec_pipeline.get("scan_total", 0)
        cache_scan = _exec_pipeline.get("cache_scan", 0)
        live_scan = _exec_pipeline.get("live_scan", 0)
        api_skip = _exec_pipeline.get("api_skip", 0)
        cache_too_old_skip = _exec_pipeline.get("cache_too_old_skip", 0)
        cache_miss = _exec_pipeline.get("cache_miss", 0)
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
            "scan_total": scan_total,
            "cache_scan": cache_scan,
            "live_scan": live_scan,
            "api_skip": api_skip,
            "cache_too_old_skip": cache_too_old_skip,
            "cache_miss": cache_miss,
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

    [P0 PATCH] Ditambah sinyal execute-rate (confidence → executed) sebagai
    layer kedua, supaya kalau funnel thesis/confidence kelihatan sehat tapi
    execute tetap mampet (gap di tahap akhir), kita masih bisa nangkep itu.
    """
    pipe = get_pipeline_metrics()
    thesis = pipe.get("thesis", 0)
    confidence = pipe.get("confidence", 0)
    executed = pipe.get("execute_pass", 0)
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

    # CASE 3: Execution mampet meski confidence ada (gap di tahap akhir funnel)
    if confidence >= 5 and executed == 0:
        relax_exec = 6
        reason_exec = f"exec_dead (conf={confidence})"
    elif confidence > 5 and executed > 0:
        exec_rate = (executed / max(confidence, 1)) * 100
        if exec_rate < 10:
            relax_exec = 4
            reason_exec = f"exec_rate={exec_rate:.0f}%"
        elif exec_rate < 20:
            relax_exec = 2
            reason_exec = f"exec_rate={exec_rate:.0f}%"
        else:
            relax_exec = 0
            reason_exec = None
    else:
        relax_exec = 0
        reason_exec = None

    if relax_exec > relax:
        relax = relax_exec
        reason = f"{reason} + {reason_exec}" if reason != "none" else reason_exec

    # RESET: conf rate sudah sehat DAN execute rate sehat (kedua layer harus sehat biar reset)
    exec_rate_for_reset = (executed / max(confidence, 1)) * 100 if confidence > 10 else 0
    if conf_rate > TUNABLE["RESET_CONF_RATE_THRESHOLD"] and exec_rate_for_reset > 30:
        relax = 0
        reason = "healthy (reset)"

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
                    final_threshold: int, rr: float,
                    shadow_mode: str = "DISCOVERY",   # P4.8
                    block_reason: str = "") -> None:   # P4.8
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
        f"(score={score}, gap={gap}, rr={rr:.2f}, size={shadow_size:.2f}x, mode={shadow_mode})"
    )

    # ===== SAFE JOURNAL BUILDER (register_shadow) =====
    journal_kwargs = dict(
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
        intent=getattr(intent, "value", str(intent)) if intent else "unknown",
        belief=getattr(belief, "value", str(belief)) if belief else "seeking",
        decision_energy=confidence_data.get("decision_energy", 0),
        narrative={
            "decision_type": "SHADOW_DISCOVERY",
            "shadow_mode": shadow_mode,          # P4.8
            "block_reason": block_reason or f"score_{score}_lt_{final_threshold}",  # P4.8
            "gap": gap,
            "threshold": final_threshold,
            "score": score,
            "size_mult": shadow_size,
        },
        journal_accept=True,
        execute_accept=False,
        signal_id=shadow_signal_id,
    )
    
    # ===== HARDENED OPTIONAL FIELDS (register_shadow) =====
    journal_kwargs.update({
        "hidden_liquidity": (
            hl.get("score", 0)
            if isinstance(locals().get("hl"), dict)
            else 0
        ),
        "micro_acceptance": (
            micro_acc.get("score")
            if isinstance(locals().get("micro_acc"), dict)
            else None
        ),
        "failed_risk": (
            failed_risk.get("risk", 1.0)
            if isinstance(locals().get("failed_risk"), dict)
            else 1.0
        ),
        "intent_drift": locals().get("intent_drift", 0.0),
        "surprise": locals().get("surprise", 0.0),
    })
    
    journal_entry = DecisionJournalEntry(**journal_kwargs)
    log_decision_journal(journal_entry)

    with _shadow_stats_lock:
        _shadow_stats["total"] += 1
        _shadow_stats["coins"][coin] = _shadow_stats["coins"].get(coin, 0) + 1


def get_shadow_summary() -> Dict[str, Any]:
    """P4.8 — Shadow breakdown by mode and block_reason."""
    with _journal_lock:
        shadows = [e for e in _decision_journal if getattr(e, "shadow", False)]
        modes = {}
        reasons = {}
        for e in shadows:
            narr = getattr(e, "narrative", {}) or {}
            mode = narr.get("shadow_mode", "DISCOVERY")
            modes[mode] = modes.get(mode, 0) + 1
            reason = narr.get("block_reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        return {"modes": modes, "reasons": reasons, "total": len(shadows)}

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

@contextmanager
def get_db():
    """Context manager untuk DB connection — auto-close di finally, aman dari leak."""
    conn = None
    try:
        conn = db_connect()
        yield conn
    finally:
        if conn:
            conn.close()


# ===== P2: EFFECTIVE WIN RATE DARI CLOSED TRADES =====
_effective_wr_cache = {"value": 0.5, "ts": 0.0}
_effective_wr_lock = threading.RLock()

def get_effective_win_rate(coin: str = None, cache_ttl: float = 30.0) -> float:
    """
    Hitung effective win rate dari trade yang SUDAH CLOSED (evaluated=1) di tabel signals,
    bukan dari journal mentah. break-even (outcome lain di luar TP/SL/PARTIAL) dihitung
    sebagian (40%) supaya gak terlalu menghukum atau terlalu memanjakan.

    Tanpa argumen coin → global effective WR (dipakai get_adaptive_threshold).
    Di-cache singkat (default 30s) karena dipanggil per-signal dan query DB tiap kali itu boros.
    """
    global _effective_wr_cache
    now = time.time()

    if coin is None:
        with _effective_wr_lock:
            if now - _effective_wr_cache["ts"] < cache_ttl:
                return _effective_wr_cache["value"]

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if coin:
                cursor.execute("""
                    SELECT outcome FROM signals
                    WHERE evaluated=1 AND coin=?
                    ORDER BY timestamp DESC
                    LIMIT 50
                """, (coin,))
            else:
                cursor.execute("""
                    SELECT outcome FROM signals
                    WHERE evaluated=1
                    ORDER BY timestamp DESC
                    LIMIT 50
                """)
            rows = cursor.fetchall()

        total = len(rows)
        if total == 0:
            wr = 0.5  # Default netral, belum ada data
        else:
            wins = sum(1 for r in rows if r[0] in ("TP_HIT", "PARTIAL_WIN"))
            losses = sum(1 for r in rows if r[0] in ("SL_HIT", "PARTIAL_LOSS"))
            be = total - wins - losses
            effective_wins = wins + (0.4 * be)
            wr = effective_wins / total

        if coin is None:
            with _effective_wr_lock:
                _effective_wr_cache = {"value": wr, "ts": now}

        return wr

    except Exception as e:
        logger.warning(f"get_effective_win_rate error ({'coin=' + coin if coin else 'global'}): {e}")
        return 0.5


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
            prediction_quality REAL,
            entry_quality INTEGER DEFAULT 0,
            absorption REAL DEFAULT 0,
            mtf_alignment INTEGER DEFAULT 0,
            quality_score REAL DEFAULT 0,
            conviction REAL DEFAULT 0,
            v1_event_types TEXT,
            v2_event_types TEXT,
            v1_count INTEGER DEFAULT 0,
            v2_count INTEGER DEFAULT 0,
            v2_added_events TEXT
        )''')
        # ===== STRUCTURE_COMPARE AUDIT: migrate existing DBs (columns may
        # not exist yet on a pre-existing signals table) =====
        try:
            c.execute("PRAGMA table_info(signals)")
            _sig_cols = [row[1] for row in c.fetchall()]
            for _col, _coltype in [
                ("v1_event_types", "TEXT"), ("v2_event_types", "TEXT"),
                ("v1_count", "INTEGER DEFAULT 0"), ("v2_count", "INTEGER DEFAULT 0"),
                ("v2_added_events", "TEXT"),
            ]:
                if _col not in _sig_cols:
                    c.execute(f"ALTER TABLE signals ADD COLUMN {_col} {_coltype}")
            conn.commit()
        except Exception as e:
            logger.debug(f"signals v1/v2 audit column migration skipped: {e}")

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
            ("signals", "shadow", "INTEGER", "0"),
            ("signals", "execute_accept", "INTEGER", "0"),
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


def migrate_evidence_families_column():
    """Add evidence_families column to signals table if missing."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        
        if "evidence_families" not in existing_cols:
            c.execute("ALTER TABLE signals ADD COLUMN evidence_families INTEGER DEFAULT 0")
            logger.info("✅ Added evidence_families column to signals table")
            conn.commit()
    except Exception as e:
        logger.error(f"migrate_evidence_families_column error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_context_log_coin_column():
    """Bug #1 fix: context_log sebelumnya gak punya kolom coin karena
    get_context_snapshot() dulu global (BTC-only behaviour). Sekarang
    context per-coin, tabel histori juga harus bisa attribute per-coin."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(context_log)")
        existing_cols = [row[1] for row in c.fetchall()]

        if "coin" not in existing_cols:
            c.execute("ALTER TABLE context_log ADD COLUMN coin TEXT DEFAULT 'UNKNOWN'")
            logger.info("✅ Added coin column to context_log table")
            conn.commit()
    except Exception as e:
        logger.error(f"migrate_context_log_coin_column error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_signals_leverage_column():
    """HIGH-LEV patch (leveraged_pnl) ditambahkan belakangan — trade yang
    closed SEBELUM patch ini nyimpen raw pnl, trade SESUDAHNYA nyimpen
    leveraged pnl, keduanya numpuk di kolom pnl yang sama tanpa cara
    membedakan. Kolom leverage ini bukan fix retroaktif (gak bisa, data
    lama gak punya info leverage), tapi minimal trade BARU ke depan bisa
    dibedakan kalau lo butuh filter/normalisasi di /analytics nanti."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]

        if "leverage" not in existing_cols:
            c.execute("ALTER TABLE signals ADD COLUMN leverage REAL DEFAULT NULL")
            logger.info("✅ Added leverage column to signals table")
            conn.commit()
    except Exception as e:
        logger.error(f"migrate_signals_leverage_column error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_score_calibration_columns():
    """P4.3 — Add raw_score, score_adjustment, calibrated_score, calibration_bucket to signals."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            "raw_score": "REAL DEFAULT NULL",
            "score_adjustment": "REAL DEFAULT NULL",
            "calibrated_score": "REAL DEFAULT NULL",
            "calibration_bucket": "TEXT DEFAULT NULL",
        }
        added = []
        for col, typedef in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                added.append(col)
        if added:
            conn.commit()
            logger.info(f"✅ P4.3 migration: added columns {added} to signals")
        else:
            logger.debug("P4.3 migration: all columns already exist")
    except Exception as e:
        logger.error(f"migrate_score_calibration_columns error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_quality_conviction_columns():
    """P4.25-30 migration: add entry_quality, absorption, mtf_alignment, quality_score, conviction."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            "entry_quality": "INTEGER DEFAULT 0",
            "absorption": "REAL DEFAULT 0",
            "mtf_alignment": "INTEGER DEFAULT 0",
            "quality_score": "REAL DEFAULT 0",
            "conviction": "REAL DEFAULT 0",
        }
        added = []
        for col, typedef in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                added.append(col)
        if added:
            conn.commit()
            logger.info(f"✅ P4.25-30 migration: added columns {added} to signals")
        else:
            logger.debug("P4.25-30 migration: all columns already exist")
    except Exception as e:
        logger.error(f"migrate_quality_conviction_columns error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_p450_conviction_mem_columns():
    """
    P4.50: Add conviction/MEM-at-entry/exit-feedback columns to signals.
    NOTE: `conviction` column already exists from P4.25-30 migration but was
    never actually populated by any write path (audit-confirmed). This
    migration does NOT touch that column — update_signal_outcome_v7 below
    now writes into it for the first time. New columns here are the ones
    that genuinely didn't exist: conviction_mode/penalty, MEM-at-entry
    snapshot, and post-exit feedback (early/shape/mature).
    """
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            "conviction_mode": "TEXT DEFAULT NULL",
            "conviction_penalty": "REAL DEFAULT NULL",
            "mem_outcome_boost_at_entry": "REAL DEFAULT NULL",
            "mem_cooldown_mult_at_entry": "REAL DEFAULT NULL",
            "mem_stability_at_entry": "REAL DEFAULT NULL",
            "feedback_early": "INTEGER DEFAULT NULL",
            "feedback_shape": "REAL DEFAULT NULL",
            "feedback_mature": "REAL DEFAULT NULL",
        }
        added = []
        for col, typedef in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                added.append(col)
        if added:
            conn.commit()
            logger.info(f"✅ P4.50 migration: added columns {added} to signals")
        else:
            logger.debug("P4.50 migration: all columns already exist")
    except Exception as e:
        logger.error(f"migrate_p450_conviction_mem_columns error: {e}")
    finally:
        if conn:
            conn.close()
            
def migrate_l4_columns():
    """L4 Alert Quality: Add fpt (First Profit Time) and exit_state columns to signals."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            "fpt": "REAL DEFAULT NULL",  # First Profit Time in seconds
            "exit_state": "TEXT DEFAULT NULL",  # SEEK/CAPTURE/DEFEND/HARVEST at close
        }
        added = []
        for col, typedef in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                added.append(col)
        if added:
            conn.commit()
            logger.info(f"✅ L4 migration: added columns {added} to signals")
        else:
            logger.debug("L4 migration: all columns already exist")
    except Exception as e:
        logger.error(f"migrate_l4_columns error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_alert_snapshot_columns():
    """FIX (alert honesty): Add detect_price/zone/created_ts snapshot columns to
    signals table, so the DETECT vs ZONE vs ENTRY breakdown shown in alerts can
    also be reconstructed later from DB (history, /entry, audits) instead of
    only living in the in-memory alert dict at send time."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        new_cols = {
            "detect_price": "REAL DEFAULT NULL",
            "entry_zone_low": "REAL DEFAULT NULL",
            "entry_zone_high": "REAL DEFAULT NULL",
            "signal_created_ts": "REAL DEFAULT NULL",
        }
        added = []
        for col, typedef in new_cols.items():
            if col not in existing_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {typedef}")
                added.append(col)
        if added:
            conn.commit()
            logger.info(f"✅ alert-snapshot migration: added columns {added} to signals")
        else:
            logger.debug("alert-snapshot migration: all columns already exist")
    except Exception as e:
        logger.error(f"migrate_alert_snapshot_columns error: {e}")
    finally:
        if conn:
            conn.close()


def migrate_entry_quality_column():
    """L1 Entry Window: Add entry_quality column to signals table."""
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        
        if "entry_quality" not in existing_cols:
            c.execute("ALTER TABLE signals ADD COLUMN entry_quality REAL DEFAULT NULL")
            logger.info("✅ Added entry_quality column to signals table")
            conn.commit()
    except Exception as e:
        logger.error(f"migrate_entry_quality_column error: {e}")
    finally:
        if conn:
            conn.close()

# ============================================================
# P4.3 — THESIS CALIBRATION ENGINE
# ============================================================

import math as _math_cal

@dataclass
class CalibrationResult:
    """Hasil kalibrasi untuk satu score."""
    raw_score: float
    effective_score: float
    adjustment: float
    bucket: str
    wr: float
    avg_pnl: float
    edge: float
    sample_size: int
    confidence: float
    bucket_low: int
    bucket_high: int


class ScoreCalibrationEngine:
    """
    Thesis Calibration Engine — Pure DB Read-Through.
    Zero JSON, zero file, zero thread.
    Cache TTL 30s, exponential decay 30d.
    """

    BUCKETS = {
        "0_30":   (0,  30),
        "31_50":  (31, 50),
        "51_70":  (51, 70),
        "71_85":  (71, 85),
        "86_100": (86, 100),
    }
    CACHE_TTL = 30

    def __init__(self):
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._lock = threading.RLock()

    def _get_bucket(self, score: float) -> Optional[Tuple[str, int, int]]:
        for bucket_name, (low, high) in self.BUCKETS.items():
            if low <= score <= high:
                return bucket_name, low, high
        return None

    def _query_bucket_stats(self, bucket: str, low: int, high: int) -> Dict:
        # P4.5: filter ketat — exclude noise outcomes + require exit_time
        # Use `score` (raw signal score) not `final_score` (calibrated) for bucket lookup
        conn = None
        try:
            conn = db_connect()
            c = conn.cursor()
            c.execute("""
                SELECT pnl, outcome, rr, exit_time, timestamp
                FROM signals
                WHERE evaluated = 1
                    AND score >= ? AND score <= ?
                    AND pnl IS NOT NULL
                    AND outcome IS NOT NULL
                    AND outcome NOT IN ('STALE_EXPIRY','TIMEOUT','ORPHAN_RECOVERED','UNKNOWN','BREAK_EVEN','')
                    AND exit_time IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT 200
            """, (low, high))
            rows = c.fetchall()
            conn.close()
            conn = None
            if not rows:
                return {"sample_size": 0, "wr": 0.0, "avg_pnl": 0.0,
                        "be_ratio": 0.0, "edge": 0.0, "avg_rr": 0.0, "confidence": 0.0}
            now = time.time()
            total_weight = weighted_wins = weighted_pnl = weighted_rr = weighted_be = total_losses = 0.0
            for pnl, outcome, rr, exit_time, timestamp in rows:
                age_days = (now - (exit_time or timestamp or now)) / 86400
                weight = _math_cal.exp(-age_days / 30.0)
                total_weight += weight
                is_win = outcome in ("TP_HIT", "PARTIAL_WIN")
                is_loss = outcome in ("SL_HIT", "PARTIAL_LOSS")
                if is_win:   weighted_wins  += weight
                if is_loss:  total_losses   += weight
                if not is_win and not is_loss: weighted_be += weight
                weighted_pnl += pnl * weight
                if rr and rr > 0: weighted_rr += rr * weight
            if total_weight == 0:
                return {"sample_size": len(rows), "wr": 0.0, "avg_pnl": 0.0,
                        "be_ratio": 0.0, "edge": 0.0, "avg_rr": 0.0, "confidence": 0.0}
            wr      = weighted_wins / total_weight
            avg_pnl = weighted_pnl  / total_weight
            be_r    = weighted_be   / total_weight
            avg_rr  = weighted_rr   / total_weight
            loss_rate = 1.0 - wr - be_r
            edge = (wr * avg_rr) - loss_rate if avg_rr > 0 else 0.0
            confidence = min(1.0, len(rows) / 50)
            return {
                "sample_size": len(rows),
                "wr": round(wr * 100, 1),
                "avg_pnl": round(avg_pnl, 2),
                "be_ratio": round(be_r * 100, 1),
                "edge": round(edge, 3),
                "avg_rr": round(avg_rr, 2),
                "confidence": round(confidence, 2),
            }
        except Exception as e:
            logger.error(f"ScoreCalibrationEngine._query_bucket_stats error: {e}")
            return {"sample_size": 0, "wr": 0.0, "avg_pnl": 0.0,
                    "be_ratio": 0.0, "edge": 0.0, "avg_rr": 0.0, "confidence": 0.0}
        finally:
            if conn:
                conn.close()

    def _get_cached_stats(self, bucket: str, low: int, high: int) -> Dict:
        with self._lock:
            now = time.time()
            if bucket in self._cache:
                ts, stats = self._cache[bucket]
                if now - ts < self.CACHE_TTL:
                    return stats
            stats = self._query_bucket_stats(bucket, low, high)
            self._cache[bucket] = (now, stats)
            return stats

    def calibrate(self, raw_score: float) -> CalibrationResult:
        bucket_info = self._get_bucket(raw_score)
        if not bucket_info:
            return CalibrationResult(raw_score=raw_score, effective_score=raw_score,
                                     adjustment=0.0, bucket="UNKNOWN", wr=0.0,
                                     avg_pnl=0.0, edge=0.0, sample_size=0,
                                     confidence=0.0, bucket_low=0, bucket_high=0)
        bucket_name, low, high = bucket_info
        stats = self._get_cached_stats(bucket_name, low, high)
        edge        = stats.get("edge", 0.0)
        avg_pnl     = stats.get("avg_pnl", 0.0)
        confidence  = stats.get("confidence", 0.0)
        sample_size = stats.get("sample_size", 0)
        if sample_size < 5:
            adjustment = 0.0
            confidence = 0.0
        else:
            adjustment = (edge * 12) + (avg_pnl * 2)
            adjustment = max(-15, min(15, adjustment))
            adjustment = round(adjustment * confidence, 1)
        effective_score = round(raw_score + adjustment, 1)
        return CalibrationResult(
            raw_score=raw_score, effective_score=effective_score,
            adjustment=adjustment, bucket=bucket_name,
            wr=stats.get("wr", 0.0), avg_pnl=avg_pnl, edge=edge,
            sample_size=sample_size, confidence=confidence,
            bucket_low=low, bucket_high=high,
        )

    def get_summary(self) -> Dict[str, Dict]:
        result = {}
        for bucket_name, (low, high) in self.BUCKETS.items():
            stats = self._get_cached_stats(bucket_name, low, high)
            result[bucket_name] = {
                "range": f"{low}-{high}",
                "sample_size": stats.get("sample_size", 0),
                "wr": stats.get("wr", 0.0),
                "avg_pnl": stats.get("avg_pnl", 0.0),
                "edge": stats.get("edge", 0.0),
                "be_ratio": stats.get("be_ratio", 0.0),
                "confidence": stats.get("confidence", 0.0),
            }
        return result

    def get_drift_indicator(self) -> Dict[str, Any]:
        total_adj = total_weight = 0.0
        worst = {"bucket": None, "adj": 0.0}
        best  = {"bucket": None, "adj": 0.0}
        for bucket_name, (low, high) in self.BUCKETS.items():
            stats = self._get_cached_stats(bucket_name, low, high)
            if stats.get("sample_size", 0) < 5:
                continue
            edge = stats.get("edge", 0.0)
            avg_pnl = stats.get("avg_pnl", 0.0)
            confidence = stats.get("confidence", 0.0)
            adj = max(-15, min(15, (edge * 12 + avg_pnl * 2) * confidence))
            weight = min(1.0, stats.get("sample_size", 0) / 50)
            total_adj += adj * weight
            total_weight += weight
            if adj < worst["adj"]: worst = {"bucket": bucket_name, "adj": adj}
            if adj > best["adj"]:  best  = {"bucket": bucket_name, "adj": adj}
        overall_drift = total_adj / total_weight if total_weight > 0 else 0.0
        return {
            "worst_bucket": worst["bucket"],
            "worst_adjustment": round(worst["adj"], 1),
            "best_bucket": best["bucket"],
            "best_adjustment": round(best["adj"], 1),
            "overall_drift": round(overall_drift, 1),
            "calibration_active": total_weight > 0,
        }

    def invalidate_cache(self):
        with self._lock:
            self._cache.clear()
        logger.info("ScoreCalibrationEngine cache invalidated")


_calibration_engine: Optional["ScoreCalibrationEngine"] = None

def get_calibration_engine() -> ScoreCalibrationEngine:
    global _calibration_engine
    if _calibration_engine is None:
        _calibration_engine = ScoreCalibrationEngine()
    return _calibration_engine

# ============================================================
# END P4.3
# ============================================================


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


def log_context(ctx: ContextSnapshot, coin: str = "UNKNOWN"):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO context_log
                     (timestamp, shock_score, transition_prob, tension,
                      vol_forecast, breath_bull, breath_bear, event_risk, dominance, regime, coin)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                  (int(ctx.timestamp), ctx.shock_score, ctx.transition_prob, ctx.tension,
                   ctx.vol_forecast, ctx.breath_bull, ctx.breath_bear,
                   ctx.event_risk, ctx.dominance, ctx.regime, coin))
        conn.commit()
    except Exception as e:
        logger.error(f"log_context error: {e}")
    finally:
        if conn:
            conn.close()


def _log_context_db(c, ctx: ContextSnapshot, coin: str = "UNKNOWN"):
    """
    FIX: versi cursor-based buat lewat enqueue_db()/_db_writer_loop().
    log_context() lama nge-spawn THREAD + KONEKSI SQLITE BARU tiap panggilan
    (dipanggil per-coin, bisa 230x/cycle dari get_context_snapshot()) — pas
    banyak coin cache-miss bareng, puluhan connection nulis simultan ke file
    yang sama → "database is locked". Versi ini numpang di writer queue yang
    udah ada (satu koneksi, batched, serial), zero risk lock contention.
    """
    c.execute('''INSERT INTO context_log
                 (timestamp, shock_score, transition_prob, tension,
                  vol_forecast, breath_bull, breath_bear, event_risk, dominance, regime, coin)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
              (int(ctx.timestamp), ctx.shock_score, ctx.transition_prob, ctx.tension,
               ctx.vol_forecast, ctx.breath_bull, ctx.breath_bear,
               ctx.event_risk, ctx.dominance, ctx.regime, coin))


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


def log_ev_mult_performance():
    """
    Log EV_MULT performance using evidence_families from DB.
    """
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT
                CASE
                    WHEN evidence_families >= 3 THEN '3_FAMILIES'
                    WHEN evidence_families >= 2 THEN '2_FAMILIES'
                    WHEN evidence_families >= 1 THEN '1_FAMILY'
                    ELSE '0_FAMILY'
                END as bucket,
                COUNT(*) as count,
                SUM(CASE WHEN outcome IN ('TP_HIT', 'PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
                AVG(pnl) as avg_pnl
            FROM signals
            WHERE evaluated=1 AND evidence_families IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket DESC
        """)
        rows = c.fetchall()
        
        if rows:
            logger.info("EV_MULT_PERFORMANCE:")
            for bucket, count, wins, avg_pnl in rows:
                wr = (wins / count * 100) if count and wins else 0
                logger.info(f"  {bucket}: n={count} WR={wr:.0f}% PnL={avg_pnl:+.2f}%")
        conn.close()
    except Exception as e:
        logger.debug(f"log_ev_mult_performance skipped: {e}")

# ========== DB WRAPPER FUNCTIONS ==========

def save_signal_v7(signal_id, coin, direction, score, entry, sl, tp, rr, reason, data_confidence,
                   hypothesis_thesis="", hypothesis_invalidate="", hypothesis_observe="",
                   execution_mode="BALANCED", intent_type="", decision_energy=0.0,
                   position_size_mult=1.0, filter_score=100.0, intent_confidence=0.0,
                   belief_state="SEEKING", commitment_score=0.0, time_pressure="normal",
                   prediction_quality=50.0, evidence_families=0,
                   raw_score: float = None,           # P4.3
                   score_adjustment: float = None,    # P4.3
                   calibrated_score: float = None,    # P4.3
                   calibration_bucket: str = None,    # P4.3
                   conviction: float = None,                    # P4.50
                   conviction_mode: str = None,                 # P4.50
                   conviction_penalty: float = None,            # P4.50
                   mem_outcome_boost_at_entry: float = None,    # P4.50
                   mem_cooldown_mult_at_entry: float = None,    # P4.50
                   mem_stability_at_entry: float = None,     # P4.50
                   entry_quality: float = None,
                   # ===== FIX (alert honesty): DETECT vs ZONE vs ENTRY snapshot =====
                   detect_price: float = None,
                   entry_zone_low: float = None,
                   entry_zone_high: float = None,
                   signal_created_ts: float = None,
                   # ===== STRUCTURE_COMPARE AUDIT: V1 vs V2 snapshot at signal time =====
                   structure_audit: dict = None):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        # Check which columns exist (P4.3/P4.50 columns may not be migrated yet)
        c.execute("PRAGMA table_info(signals)")
        existing_cols = [row[1] for row in c.fetchall()]
        # Di dalam try block, setelah c.execute("PRAGMA table_info(signals)")
        if "conviction_mode" in existing_cols:
            c.execute('''INSERT INTO signals
                         (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason,
                          timestamp, data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                          execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                          intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                          evidence_families, raw_score, score_adjustment, calibrated_score, calibration_bucket,
                          conviction, conviction_mode, conviction_penalty,
                          mem_outcome_boost_at_entry, mem_cooldown_mult_at_entry, mem_stability_at_entry,
                          entry_quality)  -- ← TAMBAHKAN
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()),
                       data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                       execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                       intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                       evidence_families, raw_score, score_adjustment, calibrated_score, calibration_bucket,
                       conviction, conviction_mode, conviction_penalty,
                       mem_outcome_boost_at_entry, mem_cooldown_mult_at_entry, mem_stability_at_entry,
                       entry_quality))
        elif "raw_score" in existing_cols:
            c.execute('''INSERT INTO signals
                         (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason,
                          timestamp, data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                          execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                          intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                          evidence_families, raw_score, score_adjustment, calibrated_score, calibration_bucket)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()),
                       data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                       execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                       intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                       evidence_families, raw_score, score_adjustment, calibrated_score, calibration_bucket))
        else:
            c.execute('''INSERT INTO signals
                         (signal_id, coin, direction, score, entry_price, sl_price, tp_price, rr, reason,
                          timestamp, data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                          execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                          intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                          evidence_families)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (signal_id, coin, direction, score, entry, sl, tp, rr, reason, int(time.time()),
                       data_confidence, hypothesis_thesis, hypothesis_invalidate, hypothesis_observe,
                       execution_mode, intent_type, decision_energy, position_size_mult, filter_score,
                       intent_confidence, belief_state, commitment_score, time_pressure, prediction_quality,
                       evidence_families))
        conn.commit()

        # ===== FIX (alert honesty): persist DETECT vs ZONE snapshot =====
        # Diupdate terpisah (bukan dijejalin ke INSERT multi-branch di atas)
        # supaya gak resiko geser urutan positional VALUES yang udah rapuh
        # karena 3 fallback schema berbeda. Kalau kolomnya belum ada
        # (migrasi belum jalan), ini no-op aman.
        if "detect_price" in existing_cols and detect_price is not None:
            c.execute(
                """UPDATE signals SET detect_price=?, entry_zone_low=?, entry_zone_high=?,
                   signal_created_ts=? WHERE signal_id=?""",
                (detect_price, entry_zone_low, entry_zone_high, signal_created_ts, signal_id)
            )
            conn.commit()

        # ===== STRUCTURE_COMPARE AUDIT: persist V1 vs V2 snapshot =====
        # Sama seperti detect_price di atas — UPDATE terpisah, no-op aman
        # kalau kolom belum ada (DB lama belum migrasi) atau audit None
        # (STRUCTURE_COMPARE mati / error saat observe_market).
        if "v1_event_types" in existing_cols and structure_audit:
            c.execute(
                """UPDATE signals SET v1_event_types=?, v2_event_types=?,
                   v1_count=?, v2_count=?, v2_added_events=? WHERE signal_id=?""",
                (
                    json.dumps(structure_audit.get("v1_event_types", [])),
                    json.dumps(structure_audit.get("v2_event_types", [])),
                    structure_audit.get("v1_count", 0),
                    structure_audit.get("v2_count", 0),
                    json.dumps(structure_audit.get("v2_added_events", [])),
                    signal_id,
                )
            )
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


def get_unified_metrics() -> Dict[str, Any]:
    """Single source of truth for all metrics — combines pipeline, opportunity,
    analytics, db health, and TradeManager state into one consistent snapshot."""
    pipe = get_pipeline_metrics()
    opp = get_opportunity_metrics()
    analytics = get_analytics()
    health = check_signal_db_health()
    tm_stats = TRADE_MANAGER.get_positions_summary()

    return {
        "cycle": {
            "window": "30s",
            "check": pipe.get("check", 0),
            "obs": pipe.get("obs", 0),
            "thesis": pipe.get("thesis", 0),
            "conf": pipe.get("confidence", 0),
            "exec": pipe.get("execute_pass", 0),
            "dcr": pipe.get("dcr", "N/A"),
            "funnel_issue": pipe.get("funnel_issue", "OK"),
        },
        "session": {
            "window": "1h",
            "scanned": opp.get("scanned", 0),
            "qualified": opp.get("qualified", 0),
            "executed": opp.get("executed", 0),
            "conversion": opp.get("conversion_rate", 0),
        },
        "today": {
            "window": "24h",
            "signals": analytics.get("total", 0),
            "wins": analytics.get("wins", 0),
            "losses": analytics.get("losses", 0),
            "wr": analytics.get("win_rate", 0),
            "avg_rr": analytics.get("avg_rr", 0),
            "pnl": analytics.get("total_pnl", 0),
        },
        "lifetime": {
            "window": "all",
            "journal": len(_decision_journal),
            "managed_open": tm_stats.get("open", 0),
            "managed_total": tm_stats.get("total", 0),
            "orphan": health.get("orphan_count", 0),
        }
    }


def update_signal_outcome_v7(signal_id, outcome, pnl, exit_price, mfe, mae, hypothesis_validated=None,
                              exit_eff=None, data_source=None, regime=None, cache_age=None,
                              feedback_early=None, feedback_shape=None, feedback_mature=None,
                              leverage=None, fpt=None, exit_state=None):
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        # ===== ORPHAN GUARD: auto-recover missing row instead of silently skipping =====
        c.execute("SELECT COUNT(*) FROM signals WHERE signal_id=?", (signal_id,))
        if c.fetchone()[0] == 0:
            logger.warning(f"🟡 ORPHAN: {signal_id} not in signals table — caller should have inserted skeleton first")
            conn.close()
            return
        if hypothesis_validated is not None:
            c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, 
                         mfe=?, mae=?, hypothesis_validated=?,
                         exit_eff=?, source=?, regime=?, cache_age=?,
                         feedback_early=?, feedback_shape=?, feedback_mature=?, leverage=?,
                         fpt=?, exit_state=? WHERE signal_id=?''',
                      (outcome, pnl, exit_price, int(time.time()), mfe, mae,
                       1 if hypothesis_validated else 0,
                       exit_eff, data_source, regime, cache_age,
                       feedback_early, feedback_shape, feedback_mature, leverage,
                       fpt, exit_state, signal_id))
        else:
            c.execute('''UPDATE signals SET evaluated=1, outcome=?, pnl=?, exit_price=?, exit_time=?, 
                         mfe=?, mae=?,
                         exit_eff=?, source=?, regime=?, cache_age=?,
                         feedback_early=?, feedback_shape=?, feedback_mature=?, leverage=?,
                         fpt=?, exit_state=? WHERE signal_id=?''',
                      (outcome, pnl, exit_price, int(time.time()), mfe, mae,
                       exit_eff, data_source, regime, cache_age,
                       feedback_early, feedback_shape, feedback_mature, leverage,
                       fpt, exit_state, signal_id))
        conn.commit()
        
        # ===== P4.24: UPDATE DISCOVERY STATS =====
        is_shadow = signal_id.startswith("SHADOW_")
        update_discovery_stats(signal_id, outcome, is_shadow=is_shadow)
        
        # ===== P4.30: UPDATE EDGE MEMORY =====
        try:
            c.execute("SELECT coin FROM signals WHERE signal_id=?", (signal_id,))
            coin_row = c.fetchone()
            if coin_row:
                coin = coin_row[0]
                if mfe and mfe > 0:
                    update_edge_memory(coin, pnl, mfe)
        except:
            pass
        
        # ===== P4.3: Invalidate calibration cache agar next calibrate() fresh =====
        try:
            get_calibration_engine().invalidate_cache()
            logger.debug(f"P4.3: Calibration cache invalidated after close {signal_id}")
        except Exception as _inv_err:
            logger.debug(f"P4.3: Cache invalidation skipped: {_inv_err}")
    except Exception as e:
        logger.error(f"update_signal_outcome_v7 error: {e}")
    finally:
        if conn:
            conn.close()


def persist_trade_close(signal_id: str, trade_result: Dict, source: str = "TRADE_MANAGER") -> bool:
    """
    Idempotent wrapper for trade close persistence.
    - Checks for duplicate close.
    - Updates DB via update_signal_outcome_v7 (preserves existing logic).
    - Updates in-memory journal.
    - Returns True if persisted, False if already closed.
    """
    try:
        # Check if already closed — and auto-recover orphan if row missing
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT evaluated, exit_time FROM signals WHERE signal_id=?", (signal_id,))
            row = c.fetchone()
            if row and (row[0] == 1 or row[1] is not None):
                logger.info(f"⏭️ SKIP close {signal_id}: already closed (eval={row[0]}, exit_time={row[1]})")
                return False
            if row is None:
                # ===== ORPHAN AUTO-RECOVERY: INSERT skeleton row dengan semua field dari trade_result =====
                logger.warning(f"🟡 ORPHAN DETECTED: {signal_id} missing from signals table — auto-recovering")
                try:
                    coin   = trade_result.get("coin") or (signal_id.split("_")[0] if "_" in signal_id else None)
                    direct = trade_result.get("direction")
                    entry  = trade_result.get("entry")
                    sl     = trade_result.get("sl")
                    score  = trade_result.get("score")
                    regime = trade_result.get("regime")
                    c.execute(
                        '''INSERT OR IGNORE INTO signals
                           (signal_id, coin, direction, entry_price, sl_price,
                            score, regime, source, timestamp, evaluated)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
                        (signal_id, coin, direct, entry, sl,
                         score, regime, "ORPHAN_RECOVERY", int(time.time()))
                    )
                    conn.commit()
                    logger.info(
                        f"✅ ORPHAN_RECOVERY: row inserted {signal_id} "
                        f"coin={coin} dir={direct} entry={entry} sl={sl} score={score}"
                    )
                except Exception as _orp_err:
                    logger.error(f"🔴 ORPHAN_RECOVERY INSERT failed for {signal_id}: {_orp_err}")
                    return False

        # Determine outcome if not provided
        if trade_result.get("outcome"):
            outcome = trade_result["outcome"]
        else:
            outcome = "TP_HIT" if trade_result["pnl"] > 0 else "SL_HIT"

        # ===== HIGH-LEV: gunakan leveraged_pnl untuk DB record =====
        _db_pnl = trade_result.get("leveraged_pnl", trade_result["pnl"])
        _db_leverage = trade_result.get("leverage")

        # Call existing function – preserves all side effects
        update_signal_outcome_v7(
            signal_id=signal_id,
            outcome=outcome,
            pnl=_db_pnl,
            exit_price=trade_result["exit"],
            mfe=trade_result["mfe"],
            mae=trade_result["mae"],
            hypothesis_validated=trade_result.get("hypothesis_validated", None),
            # ===== P4: CORRELATION FIELDS =====
            exit_eff=trade_result.get("exit_eff"),
            data_source=trade_result.get("data_source"),
            regime=trade_result.get("regime"),
            cache_age=trade_result.get("cache_age"),
            # ===== P4.50: EXIT FEEDBACK =====
            feedback_early=trade_result.get("early"),
            feedback_shape=trade_result.get("shape"),
            feedback_mature=trade_result.get("mature"),
            leverage=_db_leverage,
            # ===== L4: ALERT QUALITY =====
            fpt=trade_result.get("fpt"),
            exit_state=trade_result.get("exit_state"),
        )

        # Update journal (in-memory)
        with _journal_lock:
            for entry in _decision_journal:
                if getattr(entry, "signal_id", None) == signal_id:
                    entry.outcome = outcome
                    entry.pnl = trade_result["pnl"]
                    entry.mfe = trade_result["mfe"]
                    entry.mae = trade_result["mae"]
                    entry.closed = True
                    entry.close_reason = trade_result.get("reason", source)
                    entry.duration_minutes = trade_result.get("duration_minutes",
                                                              (trade_result.get("exit_time", time.time()) - entry.timestamp) / 60)
                    break

        logger.info(f"✅ PERSIST_CLOSE {signal_id}: {outcome} pnl={trade_result['pnl']:.2f}% source={source}")
        return True

    except Exception as e:
        logger.error(f"persist_trade_close error {signal_id}: {e}")
        return False


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


def audit_outcomes(limit: int = 1000) -> Dict[str, Any]:
    """
    READ-ONLY: Compare stored outcome vs computed outcome.
    NO AUTO-FIX — just log mismatches.
    """
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT
                    signal_id, coin, direction, entry_price, exit_price,
                    tp_price, sl_price, outcome, pnl, rr
                FROM signals
                WHERE evaluated=1 AND outcome IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            rows = c.fetchall()

        mismatches = []
        total = len(rows)

        for signal_id, coin, direction, entry, exit_price, tp, sl, stored_outcome, pnl, rr in rows:
            if entry is None or exit_price is None or tp is None or sl is None:
                continue

            # Compute outcome independently from raw prices
            if direction == "LONG":
                if exit_price >= tp * 0.995:
                    computed = ("TP_HIT", (tp - entry) / max(entry, 0.01) * 100)
                elif exit_price <= sl * 1.005:
                    computed = ("SL_HIT", (sl - entry) / max(entry, 0.01) * 100)
                else:
                    pnl_calc = (exit_price - entry) / max(entry, 0.01) * 100
                    computed = ("PARTIAL", pnl_calc)
            else:
                if exit_price <= tp * 1.005:
                    computed = ("TP_HIT", (entry - tp) / max(entry, 0.01) * 100)
                elif exit_price >= sl * 0.995:
                    computed = ("SL_HIT", (entry - sl) / max(entry, 0.01) * 100)
                else:
                    pnl_calc = (entry - exit_price) / max(entry, 0.01) * 100
                    computed = ("PARTIAL", pnl_calc)

            if computed[0] != stored_outcome:
                mismatches.append({
                    "signal_id": signal_id,
                    "coin": coin,
                    "stored": stored_outcome,
                    "computed": computed[0],
                    "pnl_stored": pnl,
                    "pnl_computed": computed[1],
                    "price_diff": abs(exit_price - (tp if computed[0] == "TP_HIT" else sl)) / max(exit_price, 0.01) * 100
                })

        mismatch_count = len(mismatches)
        mismatch_pct = (mismatch_count / total * 100) if total > 0 else 0

        by_stored = {}
        by_computed = {}
        for m in mismatches:
            by_stored[m["stored"]] = by_stored.get(m["stored"], 0) + 1
            by_computed[m["computed"]] = by_computed.get(m["computed"], 0) + 1

        logger.info(
            f"OUTCOME_AUDIT total={total} "
            f"mismatch={mismatch_count} ({mismatch_pct:.1f}%) "
            f"by_stored={by_stored} "
            f"by_computed={by_computed}"
        )

        if mismatches:
            for m in mismatches[:10]:
                logger.warning(
                    f"OUTCOME_MISMATCH {m['signal_id']} "
                    f"stored={m['stored']} computed={m['computed']} "
                    f"pnl_stored={m['pnl_stored']:.2f} pnl_computed={m['pnl_computed']:.2f}"
                )

        # Win-loss-BE accounting anomaly check (separate connection, query is independent)
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome IN ('SL_HIT','PARTIAL_LOSS') THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome NOT IN ('TP_HIT','PARTIAL_WIN','SL_HIT','PARTIAL_LOSS') THEN 1 ELSE 0 END) as be
                FROM signals WHERE evaluated=1
            """)
            row = c.fetchone()

        total_closed, wins, losses, be = row
        total_closed = total_closed or 0
        wins = wins or 0
        losses = losses or 0
        be = be or 0
        wr = (wins / total_closed * 100) if total_closed > 0 else 0

        logger.info(
            f"OUTCOME_DIST total={total_closed} "
            f"wins={wins} losses={losses} be={be} "
            f"wr={wr:.1f}%"
        )

        if total_closed > 0 and be > total_closed * 0.5:
            logger.warning(f"🔴 HIGH BE RATIO: {be}/{total_closed} ({be/total_closed*100:.0f}%) — CHECK EXIT PRICE LOGIC")

        return {
            "total": total,
            "mismatch_count": mismatch_count,
            "mismatch_pct": mismatch_pct,
            "by_stored": by_stored,
            "by_computed": by_computed,
            "mismatches": mismatches[:50],
            "distribution": {"wins": wins, "losses": losses, "be": be, "wr": round(wr, 1)}
        }

    except Exception as e:
        logger.error(f"OUTCOME_AUDIT error: {e}")
        return {"error": str(e)}

# ========== HELPERS ==========
def fmt_price(p):
    return f"${p:,.2f}" if p >= 1000 else f"${p:,.4f}"

def classify_entry_status(gap_pct: float) -> str:
    """FIX (alert honesty): klasifikasi status entry berdasarkan seberapa jauh
    current price udah geser dari optimal_entry, biar alert gak bikin user
    salah paham "telat" padahal itu valid zone-based execution.
    gap_pct = |current - optimal_entry| / optimal_entry * 100 (absolute, unsigned)."""
    if gap_pct < 0.3:
        return "🟢 ON_ENTRY"
    elif gap_pct < 1.0:
        return "🟡 ACTIVE"
    else:
        return "⚪ MOVED"

def fmt_age(seconds: float) -> str:
    """FIX (alert honesty): format signal age jadi human-readable (2m14s / 9m)."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m{s:02d}s"

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
                    trigger_api_cooldown(15)  # P4.53: 25→15
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

def get_candles_cached_only(coin: str, timeframe: str, limit: int) -> Optional[List[dict]]:
    """TIER 1 PRIMITIVE — cache/WS read-only, NEVER fetches REST.
    Returns None (not []) if data unavailable, so callers can distinguish
    'no data yet' from 'fetched, genuinely empty'. This is the only candle
    accessor Attention Engine (Tier 1) is allowed to call."""
    if _ws_candle and _ws_candle.is_connected and timeframe in _ws_candle.TIMEFRAMES:
        if _ws_candle.is_seeded(coin, timeframe) and _ws_candle.has_sufficient_history(coin, timeframe):
            ws_candles = _ws_candle.get_candles(coin, timeframe, limit)
            if ws_candles:
                return ws_candles
    key = f"candles_{coin}_{timeframe}_{limit}"
    cached = CACHE.get(key)
    if cached:
        return cached
    return None


def compute_shock_score_cached(coin: str) -> Optional[float]:
    """TIER 1 — cache/WS only, no REST. Returns None if candles unavailable
    (caller must treat None as 'unknown', never as 0.0)."""
    try:
        candles = get_candles_cached_only(coin, "5m", 50)
        if not candles or len(candles) < 20:
            return None
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
        return None


def get_volume_spike_cached(coin: str) -> Optional[float]:
    """TIER 1 — cache/WS only, no REST. Returns None if candles unavailable."""
    cache_key = f"vol_spike_{coin}"
    cached = CACHE.get(cache_key, max_age=20)
    if cached is not None:
        return cached

    candles = get_candles_cached_only(coin, "5m", 30)
    if not candles or len(candles) < 6:
        return None

    price = float(candles[-1]['c'])
    cur = float(candles[-1]['v']) * price
    prev = [float(c['v']) * float(c['c']) for c in candles[-13:-1]]
    avg = sum(prev) / len(prev) if prev else 1.0
    ratio = cur / avg if avg > 0 else 1.0
    CACHE.set(cache_key, ratio)
    return ratio


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

# ====== PART 12 – CONTEXT CACHE (FIXED: per-coin, bukan global single-slot) ======
_context_cache: Dict[str, Tuple[ContextSnapshot, float]] = {}
_context_cache_lock = threading.RLock()

def get_context_ttl(coin: str) -> int:
    """TTL adaptif dari volatilitas coin ini sendiri (ATR%), bukan angka
    tetap. Coin lagi volatile → cache lebih cepat basi (refresh sering),
    coin tenang → cache boleh dipakai lebih lama (hemat compute)."""
    try:
        atr = get_atr_pct(coin, 14, "1h")
        if atr > 3.0:
            return 3
        elif atr > 1.5:
            return 5
        else:
            return 10
    except Exception:
        return 5

def get_context_snapshot(coin: str = "BTC") -> ContextSnapshot:
    """FIX (bug #1): sebelumnya pakai _last_context GLOBAL (single slot) —
    dalam satu cycle scan banyak coin, coin pertama yang minta context
    nge-set cache itu, dan SEMUA coin lain yang minta dalam TTL window
    (10s) ikut dapet context coin pertama itu (cross-coin contamination).
    Sekarang per-coin dict, masing-masing coin punya slot sendiri."""
    with _context_cache_lock:
        now = time.time()
        cached = _context_cache.get(coin)
        if cached:
            ctx, ts = cached
            if now - ts < get_context_ttl(coin):
                return ctx

    shock = compute_shock_score(coin)
    trans = compute_regime_transition(coin)
    tension = compute_market_tension(coin)
    vol_f = compute_vol_forecast(coin)
    breath = compute_market_breath_v10()
    event_adj = get_event_risk_adjustment()
    event_r = event_adj.get("importance", 0)
    # FIX (bug #2 spillover): regime di context juga per-coin sekarang,
    # bukan get_market_regime() yang BTC-only.
    regime = interpret_regime_v10(coin).regime
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
    with _context_cache_lock:
        _context_cache[coin] = (ctx, now)
    enqueue_db(_log_context_db, ctx, coin)
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
    conn = None
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''INSERT INTO intent_memory (timestamp, coin, intent, outcome, pnl)
                     VALUES (?,?,?,?,?)''',
                  (int(time.time()), coin, intent, outcome, pnl))
        conn.commit()
    except Exception as e:
        logger.error(f"update_intent_memory DB error: {e}")
    finally:
        if conn:
            conn.close()

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
    # ===== P4: TIMING START =====
    t0 = time.time()
    
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
    
    # ===== P4: TIMING END =====
    elapsed = time.time() - t0
    if elapsed > 0.5:
        logger.info(f"DIS_TIME {coin} {elapsed:.2f}s")
    
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
        # FIX: neutral values saat warmup, bukan float('inf') yg bisa disrupt scoring
        return 0.0, 0.0, 0.0
    
    now = time.time()
    cutoff = now - seconds_ago
    
    # HANYA data yang <= cutoff (data masa lalu)
    candidates = [(ts, val) for ts, val in history if ts <= cutoff]
    
    if not candidates:
        return 0.0, 0.0, 0.0
    
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
    log_dynamic_coins_summary(
        added=len(new_dynamic),
        removed=len(removed),
        total=len(new_sector)
    )


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

    if not can_call_api() or not can_call_endpoint("meta"):
        stale = CACHE.get("exchange_meta")
        if stale is not None:
            logger.debug("⏳ API/meta on cooldown, using stale exchange meta")
            return stale
        return None

    try:
        meta = info.meta_and_asset_ctxs()
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            mark_endpoint_failure("meta")
        stale = CACHE.get("exchange_meta")
        if stale is not None:
            logger.debug(f"get_exchange_meta failed ({e}), using stale")
            return stale
        logger.error(f"get_exchange_meta failed: {e}")
        return None

    mark_endpoint_success("meta")
    CACHE.set("exchange_meta", meta)
    update_max_leverage_cache(meta)  # P4.56: keep leverage tier fresh from this path too
    return meta


def refresh_snapshot():
    now = time.time()
    ttl = _get_adaptive_snapshot_ttl()
    
    # PAKAI CACHE DULU
    cached = CACHE.get("snapshot", max_age=ttl)
    if cached:
        return cached
    
    # CEK COOLDOWN (global + per-endpoint)
    if not can_call_api() or not can_call_endpoint("snapshot"):
        logger.debug("⏳ API/snapshot on cooldown, using stale snapshot")
        return CACHE.get("snapshot")  # return stale
    
    try:
        meta = info.meta_and_asset_ctxs()
        mark_endpoint_success("snapshot")
        CACHE.set("exchange_meta", meta)  # numpang isi cache bersama get_exchange_meta()
        update_max_leverage_cache(meta)  # P4.56: native leverage tier, zero extra API call
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
        # FIX: dulu hardcode sample BTC/ETH doang. Sekarang ambil 2 coin
        # dengan OI terbesar CYCLE INI (dinamis), jadi sample debug-nya
        # ngikutin kondisi market beneran, bukan asumsi BTC/ETH selalu
        # relevan buat di-tampilin.
        with _oi_lock:
            coins_tracked = len(_oi_history)
            _top2_oi = sorted(oi.items(), key=lambda x: x[1], reverse=True)[:2] if oi else []
            _sample_str = " ".join(
                f"{c}={len(_oi_history.get(c, deque()))}" for c, _ in _top2_oi
            ) or "n/a"
        logger.info(f"OI_HISTORY coins={coins_tracked} {_sample_str}")
        
        for coin, val in funding.items():
            with _funding_lock:
                _funding_cache[coin] = (val, now)
            update_data_integrity_history(coin, 0, val, 0)
        
        return snapshot
        
    except Exception as e:
        if "429" in str(e):
            trigger_api_cooldown(15)  # P4.53: 25→15
            mark_endpoint_failure("snapshot")
            # Return stale snapshot
            stale = CACHE.get("snapshot")
            if stale:
                logger.warning(f"⚠️ Using stale snapshot due to rate limit")
                return stale
        logger.error(f"Snapshot refresh error: {e}")
        return None

def get_snapshot() -> MarketSnapshot:
    snapshot = refresh_snapshot()
    if not snapshot:
        stale = CACHE.get("snapshot")
        snapshot = stale if stale else MarketSnapshot(timestamp=time.time(), mids={}, oi={}, funding={})

    # ===== WS OVERLAY: harga real-time nimpa mids, OI/funding tetap dari REST =====
    # (Hyperliquid WS gak nyediain OI/funding — cuma mids + orderbook — jadi
    # bookkeeping OI history/data integrity di refresh_snapshot() TETAP jalan
    # normal, gak di-skip.)
    if _ws_mid and _ws_mid.is_connected:
        ws_prices = _ws_mid.get_all_prices()
        if ws_prices:
            merged_mids = dict(snapshot.mids)
            merged_mids.update(ws_prices)
            snapshot = replace(snapshot, mids=merged_mids)

    return snapshot
        
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
    
def get_cache_age(coin: str, timeframe: str = "1h", limit: int = 100) -> float:
    """Return age in seconds of cached candles, or 999 if not cached."""
    key = f"candles_{coin}_{timeframe}_{limit}"
    cached = CACHE.get_with_ts(key)
    if cached is None:
        return 999.0
    _, ts = cached
    return time.time() - ts

def should_refresh_live(stage: str, cache_age: float, rank: int) -> bool:
    """
    stage: "OBSERVE", "THESIS", "EXECUTE"
    cache_age: seconds since last cache
    rank: candidate rank (1 = highest)
    """
    if stage == "EXECUTE":
        return True   # selalu verifikasi sebelum eksekusi
    if cache_age > 90:
        return True   # data terlalu tua
    if rank <= 3 and cache_age > 30:
        return True   # prioritas tinggi, refresh proaktif
    return False

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
    
    
# ============================================================
# P2: ADAPTIVE CANDLE TTL — berdasarkan volume rank, bukan static per-timeframe
# Coin volume gede (BTC/ETH) berubah lebih meaningful per menit dan layak
# refresh lebih sering; coin volume kecil aman di-cache lebih lama karena
# datanya toh gak banyak berubah dan gak worth API budget-nya.
# ============================================================
_CANDLE_TTL_RANK: Dict[str, int] = {}
_CANDLE_TTL_LOCK = threading.RLock()
_CANDLE_TTL_BASE = {"5m": 30, "15m": 60, "1h": 120, "4h": 300}  # fallback lama, tetap dipakai sbg basis

def get_candle_ttl(coin: str, timeframe: str = "1h") -> int:
    """TTL adaptif berdasarkan volume/OI rank coin, dikombinasi dengan basis timeframe lama.

    PENTING: fungsi ini TIDAK BOLEH memanggil get_snapshot()/refresh_snapshot().
    refresh_snapshot() -> _get_adaptive_snapshot_ttl() -> get_volatility_regime()
    -> get_atr_pct() -> get_candles() -> get_candle_ttl() lagi = infinite recursion.
    Fallback OI-based di bawah baca cache snapshot LANGSUNG (CACHE.get, read-only,
    tanpa fetch) supaya gak ikut memicu rantai itu."""
    base = _CANDLE_TTL_BASE.get(timeframe, 120)
    with _CANDLE_TTL_LOCK:
        if coin in _CANDLE_TTL_RANK:
            # scale basis timeframe pakai rasio TTL 1h yang udah dihitung dari volume
            return max(base, _CANDLE_TTL_RANK[coin]) if timeframe == "1h" else base

    # Fallback: hitung dari OI size (proxy volume) kalau belum ada data dari meta.
    # Baca cache snapshot langsung (read-only) — JANGAN panggil get_snapshot()/
    # refresh_snapshot() di sini (lihat docstring di atas kenapa).
    snapshot = CACHE.get("snapshot")
    oi_usd = snapshot.oi.get(coin, 0) if snapshot else 0
    if oi_usd > 50:
        ttl = 240
    elif oi_usd > 10:
        ttl = 180
    elif oi_usd > 1:
        ttl = 120
    else:
        ttl = 90

    with _CANDLE_TTL_LOCK:
        _CANDLE_TTL_RANK[coin] = ttl
    return max(base, ttl) if timeframe == "1h" else base


def update_candle_ttl_from_meta(meta):
    """Update TTL per-coin dari dayNtlVlm di exchange_meta. Dipanggil sekali per cycle
    (bukan per-candle-fetch) supaya murah — cukup nempel di state_engine_update_v12()."""
    try:
        if not meta:
            return
        universe = meta[0].get("universe", []) if meta else []
        ctxs = meta[1] if meta else []
        with _CANDLE_TTL_LOCK:
            for asset, ctx in zip(universe, ctxs):
                name = asset.get("name")
                if not name:
                    continue
                vol = float(ctx.get("dayNtlVlm", 0) or 0)
                if vol > 50_000_000:
                    _CANDLE_TTL_RANK[name] = 240
                elif vol > 20_000_000:
                    _CANDLE_TTL_RANK[name] = 180
                elif vol > 5_000_000:
                    _CANDLE_TTL_RANK[name] = 120
                else:
                    _CANDLE_TTL_RANK[name] = 90
    except Exception as e:
        logger.debug(f"update_candle_ttl_from_meta error: {e}")

# ============================================================
# P0: MARKET HALF-LIFE ENGINE
# ============================================================

def compute_market_half_life(coin: str) -> float:
    """
    Market half-life dengan EWMA smoothing.
    Mencegah cache thrashing akibat spike sesaat.
    """
    try:
        ctx = get_context_snapshot(coin)
    except Exception:
        return 0.3  # Default neutral
    
    # ===== RAW ENTROPY =====
    shock_norm = ctx.shock_score / 100.0 if ctx.shock_score <= 100 else min(1.0, ctx.shock_score / 100.0)
    
    try:
        velocity_score, _ = get_velocity_score(coin, "LONG")
        velocity_norm = velocity_score / 100.0
    except Exception:
        velocity_norm = 0.3
    
    try:
        spread = get_spread_compression(coin)
        spread_pressure = 1.0 - spread
    except Exception:
        spread_pressure = 0.3
    
    try:
        oi_accel = get_oi_acceleration(coin, window=5)
        oi_pressure = abs(oi_accel)
    except Exception:
        oi_pressure = 0.3
    
    raw_entropy = (
        0.35 * shock_norm +
        0.25 * velocity_norm +
        0.20 * spread_pressure +
        0.20 * oi_pressure
    )
    raw_entropy = max(0.05, min(1.0, raw_entropy))
    
    # ===== EWMA SMOOTHING =====
    with _entropy_ema_lock:
        prev = _entropy_ema.get(coin, raw_entropy)
        smoothed = _ENTROPY_EMA_ALPHA * raw_entropy + (1 - _ENTROPY_EMA_ALPHA) * prev
        _entropy_ema[coin] = smoothed
    
    return smoothed


def get_dynamic_ttl(coin: str, base_ttl: int = 300) -> int:
    """
    TTL = base_ttl / (1 + entropy * 4)
    
    Entropy 0.0 → 300s (tenang)
    Entropy 0.5 → 100s (normal)
    Entropy 1.0 → 60s  (chaos)
    """
    entropy = compute_market_half_life(coin)
    ttl = base_ttl / (1 + entropy * 4)
    return int(max(15, min(600, ttl)))


def get_cache_confidence(cache_age: float, entropy: float) -> float:
    """
    Confidence = freshness^0.7 × stability^0.3
    
    Freshness dominan (0.7) karena data baru tetap berharga
    meskipun market chaos.
    """
    max_ttl = 600
    freshness = max(0.0, 1.0 - (cache_age / max_ttl))
    stability = max(0.0, 1.0 - entropy)
    confidence = (freshness ** 0.7) * (stability ** 0.3)
    return max(0.0, min(1.0, confidence))


def get_broker_patience(coin: str) -> float:
    """
    Patience = 1 - entropy
    
    Chaos → patience rendah → cepat commit
    Tenang → patience tinggi → banyak observasi
    """
    entropy = compute_market_half_life(coin)
    return max(0.1, 1.0 - entropy)


def get_required_observations(coin: str) -> int:
    """
    Jumlah observasi minimal sebelum fetch decision.
    Patience tinggi → 5 observasi, Patience rendah → 1 observasi
    """
    patience = get_broker_patience(coin)
    obs = int(1 + (patience * 4))
    return max(1, min(5, obs))


def get_chaos_fetch_priority(coin: str, snapshot: MarketSnapshot) -> float:
    """
    Priority score untuk chaos mode:
    - Signal strength
    - Entropy
    """
    try:
        imbalance = compute_imbalance_strength(coin)
        signal_strength = abs(imbalance.get("strength", 0))
    except Exception:
        signal_strength = 0.3
    
    entropy = compute_market_half_life(coin)
    
    # Signal strength + entropy
    priority = signal_strength * 0.5 + entropy * 0.5
    return max(0.0, min(1.0, priority))

# ============================================================
# P0: BACKGROUND REFRESH (DEDUPLIKASI)
# ============================================================

def trigger_background_refresh(coin: str, timeframe: str = "1h", limit: int = 100):
    """Trigger background refresh dengan deduplikasi."""
    key = f"candles_{coin}_{timeframe}_{limit}"
    with _refresh_in_progress_lock:
        now = time.time()
        last_refresh = _refresh_in_progress.get(key, 0)
        if now - last_refresh < _REFRESH_MIN_GAP:
            return
        _refresh_in_progress[key] = now
    
    _EVAL_EXECUTOR.submit(_refresh_candles_async, coin, timeframe, limit, key)


def _refresh_candles_async(coin: str, timeframe: str, limit: int, key: str):
    """Background refresh dengan deduplikasi."""
    try:
        logger.debug(f"🔄 Background refresh: {key}")
        get_candles(coin, timeframe, limit, force=True)
    except Exception as e:
        logger.debug(f"Background refresh failed {key}: {e}")
    finally:
        with _refresh_in_progress_lock:
            _refresh_in_progress.pop(key, None)
            
_CANDLES_IN_PROGRESS: set = set()
_CANDLES_IN_PROGRESS_LOCK = threading.RLock()

def get_candles(coin: str, timeframe: str, limit: int = 80, master: Dict = None, force: bool = False) -> List[dict]:
    if master and coin in master:
        return master[coin]

    # ===== WS PRIORITY =====
    # Cuma buat coin yang di-subscribe (watchlist top-N by OI) DAN udah
    # ke-seed dari REST DAN punya history cukup. Semua coin lain (mayoritas
    # dari ~900+ coin) tetap lewat jalur REST di bawah, gak disentuh.
    if not force and _ws_candle and _ws_candle.is_connected and timeframe in _ws_candle.TIMEFRAMES:
        if _ws_candle.is_seeded(coin, timeframe) and _ws_candle.has_sufficient_history(coin, timeframe):
            if _ws_trades and _ws_trades.is_fresh(coin):
                ws_candles = _ws_candle.get_candles(coin, timeframe, limit)
                if ws_candles:
                    return ws_candles

    key = f"candles_{coin}_{timeframe}_{limit}"

    # ===== RECURSION GUARD =====
    # get_dynamic_ttl()/compute_market_half_life() -> get_context_snapshot()
    # fans out into compute_shock_score / compute_regime_transition /
    # compute_vol_forecast(->get_atr_pct) / compute_market_breath_v10 /
    # interpret_regime_v10 — all of which call get_candles() again for the
    # same coin/timeframe, causing infinite recursion. This guard breaks
    # that cycle: if a fetch for this exact key is already in progress,
    # skip the TTL/entropy detour and return [] instead of recursing.
    guard_key = f"{coin}_{timeframe}_{limit}"
    with _CANDLES_IN_PROGRESS_LOCK:
        if guard_key in _CANDLES_IN_PROGRESS:
            logger.debug(f"⚠️ get_candles recursion guard hit for {guard_key}")
            return []
        _CANDLES_IN_PROGRESS.add(guard_key)

    try:
        # ===== P0: DYNAMIC TTL =====
        base_ttl = get_candle_ttl(coin, timeframe)
        dynamic_ttl = get_dynamic_ttl(coin, base_ttl)

        # ===== P0: CACHE CONFIDENCE =====
        cached = CACHE.get_with_ts(key)
        if cached and not force:
            candles, ts = cached
            cache_age = time.time() - ts
            entropy = compute_market_half_life(coin)
            confidence = get_cache_confidence(cache_age, entropy)

            # DECISION MATRIX
            if confidence > 0.70:
                # Confidence tinggi → pakai cache
                return candles
            elif confidence > 0.45:
                # Confidence medium → pakai cache + background refresh
                trigger_background_refresh(coin, timeframe, limit)
                return candles
            # else: confidence rendah → refresh now

        # ===== LIVE FETCH =====
        if not can_call_api() or not can_call_endpoint("candles"):
            stale = CACHE.get(key)
            if stale:
                logger.debug(f"⏳ Candles cooldown, using stale for {coin}")
                return stale
            return []

        try:
            end_ms = int(time.time() * 1000)
            tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
            interval = tf_ms.get(timeframe, 3600000)
            start_ms = end_ms - limit * interval
            candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms) or []
            mark_endpoint_success("candles")

            # ===== P0: UPDATE CACHE DENGAN TTL BARU =====
            # Simpan tanpa max_age agar confidence logic yang menentukan
            CACHE.set(key, candles)
            return candles

        except Exception as e:
            if "429" in str(e):
                trigger_api_cooldown(15)
                mark_endpoint_failure("candles")
                stale = CACHE.get(key)
                if stale:
                    logger.debug(f"⏳ Rate limit, using stale for {coin}")
                    return stale
                return []
            logger.error(f"get_candles failed for {coin}: {e}")
            return []
    finally:
        with _CANDLES_IN_PROGRESS_LOCK:
            _CANDLES_IN_PROGRESS.discard(guard_key)

    
def get_ob_delta(coin: str) -> float:
    # ===== WS PRIORITY =====
    if _ws_ob and _ws_ob.is_connected and _ws_ob.is_fresh(coin):
        ws_delta = _ws_ob.get_delta(coin)
        return ws_delta

    key = f"ob_{coin}"
    
    # PAKAI CACHE MANAGER
    cached = CACHE.get(key, max_age=5)
    if cached:
        return cached

    # ===== P0: PER-ENDPOINT GUARD (l2) — infra udah ada di _API_HEALTH,
    # sebelumnya gak pernah dipakai. Circuit breaker granular biar l2
    # snapshot yang lagi 429 gak dipaksa terus (exponential backoff), tapi
    # gak ikut nge-block endpoint lain (candles/snapshot/meta). =====
    if not can_call_api() or not can_call_endpoint("l2"):
        return 0

    try:
        l2 = info.l2_snapshot(coin)
        mark_endpoint_success("l2")
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
    except Exception as e:
        if "429" in str(e):
            mark_endpoint_failure("l2")
        return 0

def ensure_delta_seeded(coin: str, min_points: int = 3) -> bool:
    """Lazy-seed: isi _rolling_delta buat coin yang belum punya history
    cukup, TAPI cuma dipanggil buat coin yang udah lolos gate awal (oi_min +
    viability) di v12 — bukan blanket ke semua 230 coin. Otomatis mundur
    kalau endpoint l2 lagi cooldown (via guard di get_ob_delta/can_call_endpoint).
    Return True kalau berhasil nambah data point."""
    with _rolling_delta_lock:
        current_len = len(_rolling_delta.get(coin, []))
    if current_len >= min_points:
        return False
    if not can_call_api() or not can_call_endpoint("l2"):
        return False
    update_rolling_delta(coin)
    return True

def update_rolling_delta(coin: str):
    delta = get_ob_delta(coin)
    with _rolling_delta_lock:
        if coin not in _rolling_delta:
            _rolling_delta[coin] = deque(maxlen=TUNABLE["ROLLING_DELTA_WINDOW"])
        _rolling_delta[coin].append(delta)

    # FIX #3: feed distribution history juga, dari delta yang sama (reuse
    # fetch, gak nambah API call) — window beda tujuan (lihat deklarasi).
    with _delta_distribution_lock:
        if coin not in _delta_distribution:
            _delta_distribution[coin] = deque(maxlen=_DELTA_DISTRIBUTION_WINDOW)
        _delta_distribution[coin].append(delta)

def get_delta_shift(coin: str) -> float:
    with _rolling_delta_lock:
        if coin not in _rolling_delta or len(_rolling_delta[coin]) < 2:
            return 0.0
        recent = list(_rolling_delta[coin])
        return recent[-1] - recent[0]

def get_safe_ob_delta(coin: str) -> Tuple[float, bool]:
    """
    P0 FIX (real): get_ob_delta() return 0.0 SAAT FETCH GAGAL (empty book /
    exception) — bukan cuma nilai netral. Return (delta, is_stale).

    Fallback pas stale: PAKAI LEVEL TERAKHIR YANG VALID (CACHE ob_{coin}
    tanpa max_age constraint), BUKAN get_delta_shift(). get_delta_shift itu
    rate-of-change (recent[-1]-recent[0]) — beda besaran sama level delta
    (-60..+60). Nge-fallback level ke rate-of-change itu salah kaprah,
    seperti fallback "harga" ke "return harian" pas data harga hilang.
    """
    fresh = get_ob_delta(coin)
    if abs(fresh) >= 0.05:
        return fresh, False
    # fresh ~0 → kemungkinan besar fetch gagal (empty book/exception di
    # get_ob_delta), bukan genuinely balanced book (secara statistik hampir
    # mustahil smoothed float mendarat persis di 0).
    last_good = CACHE.get(f"ob_{coin}")  # no max_age = ambil terakhir walau stale
    if last_good is not None:
        return last_good, True
    return fresh, True

def get_delta_zscore(coin: str, current_delta: float) -> Optional[float]:
    """FIX #3: seberapa ekstrem delta SEKARANG dibanding histori delta coin
    INI SENDIRI. None kalau sample belum cukup (caller harus fallback ke
    threshold absolut, jangan dipaksa — cold-start coin baru gak boleh
    auto-reject cuma karena belum punya histori)."""
    with _delta_distribution_lock:
        hist = list(_delta_distribution.get(coin, []))
    if len(hist) < _DELTA_ZSCORE_MIN_SAMPLES:
        return None
    mean = np.mean(hist)
    std = np.std(hist)
    if std < 1e-9:
        return None
    return (current_delta - mean) / std

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


# ===== LAYER 1: PRESSURE SCORE DISCOVERY =====
# Reuse get_oi_roc (oi_accel) dan get_delta_shift (delta_accel) yang sudah
# ada — bukan duplikasi. Cuma 2 komponen baru yang belum punya implementasi:
# taker_imbalance dan price_compression.

def get_taker_imbalance(coin: str, minutes: int = 5) -> float:
    """Rasio taker buy vs sell (BUKAN net delta seperti get_cvd) dalam
    window pendek. Range -1 (all sell) .. +1 (all buy). Beda dari CVD:
    ini proporsi/rasio, bukan net USD — jadi sebanding antar coin
    terlepas dari ukuran volume absolutnya."""
    cache_key = f"taker_imb_{coin}_{minutes}"
    cached = CACHE.get(cache_key, max_age=15)
    if cached is not None:
        return cached
    try:
        trades = info.recent_trades(coin)
        if not trades:
            CACHE.set(cache_key, 0.0)
            return 0.0
        cutoff = int((time.time() - minutes * 60) * 1000)
        buy_usd = 0.0
        sell_usd = 0.0
        for t in trades:
            if t['time'] < cutoff:
                continue
            usd = float(t['px']) * float(t['sz'])
            if t['side'] == 'B':
                buy_usd += usd
            else:
                sell_usd += usd
        total = buy_usd + sell_usd
        if total <= 0:
            CACHE.set(cache_key, 0.0)
            return 0.0
        imbalance = (buy_usd - sell_usd) / total  # -1..+1
        CACHE.set(cache_key, imbalance)
        return imbalance
    except Exception:
        return 0.0


def get_price_compression(coin: str, master: Dict = None) -> float:
    """Seberapa 'mampat' range candle terkini dibanding ATR baseline-nya
    sendiri (volatility squeeze). Range 0 (sangat compressed, candle
    sempit dibanding ATR normal — pra-breakout) .. 1+ (sangat lebar,
    sudah bergerak jauh). Dipakai sebagai sinyal "belum meledak", BUKAN
    "sedang meledak" — itu beda dari vol_spike yang justru nyari yang
    SUDAH bergerak."""
    try:
        candles_recent = get_candles(coin, "5m", 6, master)
        if not candles_recent or len(candles_recent) < 3:
            return 0.5  # neutral, data belum cukup
        ranges = [(float(c['h']) - float(c['l'])) for c in candles_recent]
        avg_recent_range_pct = (sum(ranges) / len(ranges)) / max(float(candles_recent[-1]['c']), 0.01) * 100
        atr_baseline = get_atr_pct(coin, 14, "1h", master)
        if atr_baseline <= 0:
            return 0.5
        # compression rendah = candle MENYEMPIT relatif ATR normal-nya
        compression_ratio = avg_recent_range_pct / atr_baseline
        return max(0.0, min(2.0, compression_ratio))
    except Exception:
        return 0.5


def compute_pressure_score(coin: str) -> float:
    """LAYER 1 — Pressure Score Discovery.

    Pressure = 0.35*delta_accel + 0.25*oi_accel + 0.20*taker_imbalance + 0.20*price_compression

    Tujuan: tangkap coin SEBELUM breakout/SEBELUM OI meledak — bukan
    cuma OI absolute yang udah kejadian. Dipakai sebagai TAMBAHAN sinyal
    di discovery (lihat build_candidate_pool_v11_final), bukan pengganti
    gate yang sudah ada — exact seperti reasoning yang dipakai untuk semua
    upgrade scoring sebelumnya di codebase ini (additive, bukan replace).

    Semua komponen dinormalisasi ke skala yang sebanding sebelum diboboti:
    - delta_accel: get_delta_shift, sudah dalam unit poin-persen (-60..+60
      range delta), dinormalisasi /20 supaya skala mirip komponen lain
    - oi_accel: get_oi_roc(5m), unit persen, dinormalisasi /20
    - taker_imbalance: sudah -1..+1, dipakai langsung
    - price_compression INVERTED jadi "pressure" (compression rendah =
      pressure tinggi, karena itu pra-breakout): pressure_from_compress
      = 1 - min(1, compression_ratio)
    """
    try:
        delta_accel_raw = get_delta_shift(coin)
        delta_accel = max(-1.0, min(1.0, delta_accel_raw / 20.0))

        oi_accel_raw = get_oi_roc(coin, window_minutes=5)
        oi_accel = max(-1.0, min(1.0, oi_accel_raw / 20.0))

        taker_imb = get_taker_imbalance(coin)

        compression = get_price_compression(coin)
        compress_pressure = 1.0 - min(1.0, compression)  # compressed = high pressure

        pressure = (
            0.35 * delta_accel
            + 0.25 * oi_accel
            + 0.20 * taker_imb
            + 0.20 * compress_pressure
        )
        return round(pressure, 4)
    except Exception as e:
        logger.debug(f"compute_pressure_score error {coin}: {e}")
        return 0.0


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


# ============================================================
# L0 DISCOVERY HELPERS (V12)
# ============================================================

def get_oi_acceleration(coin: str, window: int = 5) -> float:
    """OI acceleration = slope dari window titik OI terakhir, dinormalisasi
    jadi %perubahan-per-titik. POSITIF = OI naik (bullish interest), NEGATIF
    = OI turun.

    FIX (root cause "magnitude collapse"): versi lama pakai raw unix epoch
    timestamp (~1.78 milyar) sebagai sumbu-X regresi. Karena titik-X saking
    gedenya dibanding jarak antar-titik (~30-60s), hasil slope-nya punya
    satuan "USD per DETIK" yang otomatis mikroskopis buat pergerakan OI
    human-timescale manapun, lalu masih dibagi /10000 lagi di atasnya.
    Contoh nyata: OI naik solid +2% (100jt->102jt) dalam 5 titik cuma
    ngasilin oi_accel~0.0000015 — collapse total walau sinyalnya kuat.
    get_delta_acceleration() di bawah TIDAK kena bug ini karena udah pakai
    index relatif (0,1,2,...), bukan epoch absolut — jadi fix di sini bikin
    oi_acceleration konsisten sama pola fungsi accel lainnya di file ini.

    Sekarang: pakai index relatif + normalisasi slope terhadap rata-rata OI
    di window itu (jadi %perubahan per titik, scale-independent dari OI
    besar/kecil), baru dikali faktor skala biar masuk range [-1, 1] yang
    proporsional sama kekuatan sinyal asli.
    """
    with _oi_lock:
        hist = list(_oi_history.get(coin, deque()))
    if len(hist) < window:
        return 0.0
    recent = hist[-window:]
    values = [v for _, v in recent]
    avg_val = sum(values) / len(values)
    if avg_val <= 0:
        return 0.0
    x = list(range(len(values)))  # index relatif, BUKAN epoch absolut
    n = len(x)
    sum_x = sum(x)
    sum_y = sum(values)
    sum_xy = sum(x[i] * values[i] for i in range(n))
    sum_xx = sum(x[i] * x[i] for i in range(n))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    slope_pct = slope / avg_val  # %perubahan OI per titik, scale-independent
    return max(-1.0, min(1.0, slope_pct * 20))  # 20 = faktor skala, tune sesuai distribusi real (lihat MAG_DIST log)


def get_oi_consistency(coin: str, window: int = 3) -> float:
    """OI consistency = fraksi titik yang searah dengan trend awal.
    +1.0 = semua naik, -1.0 = semua turun, 0 = campuran."""
    with _oi_lock:
        hist = list(_oi_history.get(coin, deque()))
    if len(hist) < window + 1:
        return 0.0
    recent = hist[-(window + 1):]
    values = [v for _, v in recent]
    directions = []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        directions.append(1 if diff > 0 else (-1 if diff < 0 else 0))
    if not directions:
        return 0.0
    first_dir = next((d for d in directions if d != 0), 0)
    if first_dir == 0:
        return 0.0
    same_count = sum(1 for d in directions if d == first_dir)
    return same_count / len(directions) * first_dir


def get_delta_acceleration(coin: str, window: int = 5) -> float:
    """Delta acceleration = linear slope dari window titik delta terakhir."""
    with _rolling_delta_lock:
        hist = list(_rolling_delta.get(coin, deque()))
    if len(hist) < window:
        return 0.0
    recent = hist[-window:]
    x = list(range(len(recent)))
    y = recent
    n = len(x)
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(x[i] * y[i] for i in range(n))
    sum_xx = sum(x[i] * x[i] for i in range(n))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return max(-1.0, min(1.0, slope / 20))


def get_delta_consistency(coin: str, window: int = 3) -> float:
    """Delta consistency = fraksi step yang searah dengan pergerakan awal."""
    with _rolling_delta_lock:
        hist = list(_rolling_delta.get(coin, deque()))
    if len(hist) < window + 1:
        return 0.0
    recent = hist[-(window + 1):]
    directions = []
    for i in range(1, len(recent)):
        diff = recent[i] - recent[i-1]
        directions.append(1 if diff > 0.5 else (-1 if diff < -0.5 else 0))
    if not directions:
        return 0.0
    first_dir = next((d for d in directions if d != 0), 0)
    if first_dir == 0:
        return 0.0
    same_count = sum(1 for d in directions if d == first_dir)
    return same_count / len(directions) * first_dir


def get_volume_acceleration(coin: str, window: int = 5) -> float:
    """Volume acceleration = log-linear slope dari volume USD terakhir."""
    candles = get_candles(coin, "5m", window + 3)
    if not candles or len(candles) < window:
        return 0.0
    volumes = [float(c['v']) * float(c['c']) for c in candles[-window:]]
    if len(volumes) < 2:
        return 0.0
    log_volumes = [np.log(v + 1) for v in volumes]
    x = list(range(len(log_volumes)))
    n = len(x)
    sum_x = sum(x)
    sum_y = sum(log_volumes)
    sum_xy = sum(x[i] * log_volumes[i] for i in range(n))
    sum_xx = sum(x[i] * x[i] for i in range(n))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return max(0.0, min(1.0, slope * 2))


def get_cvd_acceleration(coin: str, window: int = 5) -> float:
    """CVD acceleration = linear slope dari CVD di berbagai window."""
    cvd_vals = [get_cvd(coin, minutes * 5) for minutes in range(window, 0, -1)]
    if len(cvd_vals) < 2:
        return 0.0
    x = list(range(len(cvd_vals)))
    n = len(x)
    sum_x = sum(x)
    sum_y = sum(cvd_vals)
    sum_xy = sum(x[i] * cvd_vals[i] for i in range(n))
    sum_xx = sum(x[i] * x[i] for i in range(n))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return max(-1.0, min(1.0, slope * 10))


# ============================================================
# L0 DISCOVERY — IMBALANCE STRENGTH ENGINE
# ============================================================

def compute_imbalance_strength(coin: str) -> Dict[str, Any]:
    """Core discovery V12: acceleration × persistence × data_confidence.
    Menggantikan OI-ROC murni dengan multi-signal yang lebih robust."""
    oi_accel = get_oi_acceleration(coin)
    oi_cons = get_oi_consistency(coin)
    delta_accel = get_delta_acceleration(coin)
    delta_cons = get_delta_consistency(coin)
    vol_accel = get_volume_acceleration(coin)
    cvd_accel = get_cvd_acceleration(coin)
    spread_comp = get_spread_compression(coin)

    data_conf, _ = get_data_confidence(coin, time.time())
    data_conf_norm = data_conf / 100.0

    oi_directional = oi_accel * abs(oi_cons)
    delta_directional = delta_accel * abs(delta_cons)

    directional = (
        oi_directional * 0.35 +
        delta_directional * 0.30 +
        cvd_accel * 0.20 +
        vol_accel * 0.15
    )

    persistence = max(0.0, min(1.0,
        abs(oi_cons) * 0.40 +
        abs(delta_cons) * 0.30 +
        0.30  # baseline
    ))

    magnitude = abs(directional) * persistence * data_conf_norm

    return {
        "strength": directional,
        "magnitude": magnitude,
        "persistence": persistence,
        "data_confidence": data_conf_norm,
        "components": {
            "oi_accel": oi_accel,
            "oi_cons": oi_cons,
            "delta_accel": delta_accel,
            "delta_cons": delta_cons,
            "vol_accel": vol_accel,
            "cvd_accel": cvd_accel,
            "spread_comp": spread_comp,
        }
    }


def get_candles_coverage(coin: str, timeframe: str = "1h", limit: int = 100) -> float:
    """Cek cache candles TANPA trigger API fetch — buat keputusan degraded/full mode.
    1.0 = fresh, 0.0 = gak ada cache sama sekali, di antaranya = stale linear decay."""
    key = f"candles_{coin}_{timeframe}_{limit}"
    cached = CACHE.get_with_ts(key)
    if cached is None:
        return 0.0
    _, ts = cached
    age = time.time() - ts
    if age > 300:  # 5 menit dianggap stale
        return max(0.0, 1.0 - (age - 300) / 300)
    return 1.0


def compute_imbalance_strength_degraded(coin: str) -> Dict[str, Any]:
    """P1 Discovery Degraded: OI + delta + CVD PROXY, TANPA panggil get_volume_acceleration
    (yang internally fetch candles via get_candles). Dipakai kalau endpoint candles
    lagi BLOCKED atau cache coverage-nya rendah, biar discovery tetap jalan pakai
    sinyal yang selalu tersedia dari cache lokal (OI history, rolling delta, CVD cache)."""
    oi_accel = get_oi_acceleration(coin)
    oi_cons = get_oi_consistency(coin)
    delta_accel = get_delta_acceleration(coin)
    delta_cons = get_delta_consistency(coin)
    cvd_accel = get_cvd_acceleration(coin)

    # Spread compression proxy: delta konsisten → spread kemungkinan kompres
    spread_proxy = 0.3 + 0.7 * abs(delta_cons)
    spread_comp = max(0.0, min(1.0, spread_proxy))

    # Volume acceleration proxy: OI acceleration sebagai stand-in
    vol_proxy = 0.3 + 0.7 * abs(oi_accel)
    vol_accel = max(0.0, min(1.0, vol_proxy))

    data_conf, _ = get_data_confidence(coin, time.time())
    data_conf_norm = data_conf / 100.0

    oi_directional = oi_accel * abs(oi_cons)
    delta_directional = delta_accel * abs(delta_cons)

    directional = (
        oi_directional * 0.35 +
        delta_directional * 0.30 +
        cvd_accel * 0.20 +
        vol_accel * 0.15
    )

    persistence = max(0.0, min(1.0,
        abs(oi_cons) * 0.40 +
        abs(delta_cons) * 0.30 +
        0.30
    ))

    magnitude = abs(directional) * persistence * data_conf_norm

    return {
        "strength": directional,
        "magnitude": magnitude,
        "persistence": persistence,
        "data_confidence": data_conf_norm,
        "components": {
            "oi_accel": oi_accel,
            "oi_cons": oi_cons,
            "delta_accel": delta_accel,
            "delta_cons": delta_cons,
            "vol_accel": vol_accel,
            "cvd_accel": cvd_accel,
            "spread_comp": spread_comp,
            "mode": "DEGRADED",
        }
    }


def is_imbalance_valid(imbalance: Dict[str, Any], min_magnitude: float = 0.10) -> bool:
    """Valid kalau magnitude cukup, persistence cukup, dan data bisa dipercaya."""
    return (
        imbalance["magnitude"] >= min_magnitude and
        imbalance["persistence"] >= 0.4 and
        imbalance["data_confidence"] >= 0.3
    )


# ============================================================
# DISCOVERY GATE — DATA-DRIVEN MAGNITUDE DISTRIBUTION
# ============================================================
# Gate ngikut distribusi market secara rolling, bukan angka statis
# 0.10 yang di-hardcode. Additive only — is_imbalance_valid() di atas
# TIDAK diubah signature-nya, jadi caller lama (get_best_imbalance_direction,
# dsb) tetap jalan seperti biasa.
# ============================================================

_MAGNITUDE_HISTORY: List[float] = []
_MAGNITUDE_HISTORY_LOCK = threading.RLock()
_MAGNITUDE_HISTORY_MAX = 500
_MAGNITUDE_DIST_CACHE: Dict[str, Any] = {"ts": 0.0, "p50": 0.006, "p70": 0.010, "p90": 0.020, "max": 0.050, "n": 0}
_MAGNITUDE_DIST_CACHE_LOCK = threading.RLock()


def update_magnitude_distribution(magnitude: float):
    """Feed magnitude value ke rolling history (dipanggil per-coin per-scan)."""
    if magnitude is None or magnitude <= 0:
        return
    with _MAGNITUDE_HISTORY_LOCK:
        _MAGNITUDE_HISTORY.append(magnitude)
        if len(_MAGNITUDE_HISTORY) > _MAGNITUDE_HISTORY_MAX:
            del _MAGNITUDE_HISTORY[: len(_MAGNITUDE_HISTORY) - _MAGNITUDE_HISTORY_MAX]


def get_magnitude_distribution() -> Dict[str, float]:
    """Percentile dari rolling history. Cache 60s biar gak recompute tiap panggilan."""
    global _MAGNITUDE_DIST_CACHE
    with _MAGNITUDE_DIST_CACHE_LOCK:
        now = time.time()
        if now - _MAGNITUDE_DIST_CACHE["ts"] < 60:
            return _MAGNITUDE_DIST_CACHE.copy()

    with _MAGNITUDE_HISTORY_LOCK:
        vals = sorted(v for v in _MAGNITUDE_HISTORY if v > 0)

    result = {"p50": 0.006, "p70": 0.010, "p90": 0.020, "max": 0.050, "n": 0, "ts": now}

    if len(vals) >= 20:
        n = len(vals)
        result["p50"] = vals[int(n * 0.50)]
        result["p70"] = vals[int(n * 0.70)]
        result["p90"] = vals[min(n - 1, int(n * 0.90))]
        result["max"] = vals[-1]
        result["n"] = n
        logger.info(
            f"MAG_DIST n={n} p50={result['p50']:.4f} p70={result['p70']:.4f} "
            f"p90={result['p90']:.4f} max={result['max']:.4f}"
        )

    with _MAGNITUDE_DIST_CACHE_LOCK:
        _MAGNITUDE_DIST_CACHE = result

    return result.copy()


def compute_dynamic_magnitude_gate(regime: str = "RANGING") -> float:
    """
    Gate ngikut distribusi market, BUKAN angka absolut.

    FIX: clamp lama [0.03, 0.15] + cold-start fallback 0.10 itu diasumsikan
    dari skala magnitude versi discovery lama. compute_imbalance_strength()
    sekarang ngasilin magnitude ~0.0001-0.001 (perkalian 3 faktor <1: accel x
    persistence x confidence) — 30-300x lebih kecil dari clamp floor 0.03,
    jadi gate SELALU macet di 0.03 apapun kondisi market-nya, gak pernah
    beneran "dinamis". Sekarang clamp-nya relatif ke skala distribusi
    observasi sendiri (p50/max), bukan angka mutlak.

    RANGING:          p70 (sinyal subtle, gate di persentil-70)
    TRENDING_UP/DOWN: antara p70-p90 (sinyal lebih jelas, gate lebih tinggi)
    CHAOS/PANIC:      antara p70-p90 tapi lebih rendah dari trending

    Cold start (n<20): pakai p70 dari cache/default seed apa adanya (bukan
    angka mutlak 0.10 yang gak nyambung skala), biar begitu data ngalir gate
    langsung representatif — bukan nunggu 20 sample buat "unlock" skala yang
    bener.
    """
    dist = get_magnitude_distribution()
    p50 = dist.get("p50", 0.006)
    p70 = dist.get("p70", 0.010)
    p90 = dist.get("p90", 0.020)
    n = dist.get("n", 0)

    if n < 20:
        gate = p70
    elif regime in ("TRENDING_UP", "TRENDING_DOWN"):
        gate = p70 + (p90 - p70) * 0.5
    elif regime in ("CHAOS", "PANIC"):
        gate = p70 + (p90 - p70) * 0.33
    else:  # RANGING / UNKNOWN / default
        gate = p70

    # Clamp RELATIF ke skala distribusi sendiri — floor = separuh median
    # (gak boleh terlalu longgar sampe nerima noise di bawah "normal"),
    # ceiling = max observed cycle ini (gak boleh lebih strict dari puncak
    # distribusi, atau nolak SEMUA coin kayak yang kejadian sebelumnya).
    floor = max(1e-6, p50 * 0.5)
    ceiling = max(floor * 2, dist.get("max", p90 * 2))
    gate = max(floor, min(ceiling, gate))

    logger.info(f"MAG_GATE regime={regime} p50={p50:.4f} p70={p70:.4f} p90={p90:.4f} n={n} gate={gate:.5f}")

    return gate


def get_best_imbalance_direction(imbalance: Dict[str, Any], min_magnitude: Optional[float] = None) -> Optional[str]:
    """LONG kalau strength > 0.15, SHORT kalau strength < -0.15.

    FIX: dulu manggil is_imbalance_valid(imbalance) TANPA gate, jadi diam-diam
    kena default hardcode min_magnitude=0.10 — dobel-gate dengan skala yang
    beda dari compute_dynamic_magnitude_gate() yang dipakai caller di
    build_candidate_pool_v12(). Sekarang terima gate yang sama (atau hitung
    sendiri kalau gak dikasih), biar konsisten satu skala.
    """
    gate = min_magnitude if min_magnitude is not None else compute_dynamic_magnitude_gate()
    if not is_imbalance_valid(imbalance, min_magnitude=gate):
        return None
    if imbalance["strength"] > 0.15:
        return "LONG"
    elif imbalance["strength"] < -0.15:
        return "SHORT"
    return None



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

# ============================================================
# L1 ENTRY WINDOW — IMPULSE DETECTION
# ============================================================

def detect_impulse_candle(coin: str, master: Dict = None) -> Optional[Dict]:
    """
    Deteksi impulse candle terakhir.
    Impulse = candle dengan range > 1.5x ATR atau volume > 2x average.
    
    Return: {"time": timestamp, "range_pct": float, "volume_ratio": float, "idx": int}
    """
    candles = get_candles(coin, "5m", 30, master)
    if not candles or len(candles) < 10:
        return None
    
    atr = get_atr_pct(coin, 14, "1h", master)
    if atr <= 0:
        atr = 0.5
    
    vols = [float(c['v']) * float(c['c']) for c in candles[-10:]]
    avg_vol = sum(vols) / len(vols) if vols else 1
    
    for i in range(len(candles) - 1, max(0, len(candles) - 15), -1):
        c = candles[i]
        candle_range = (float(c['h']) - float(c['l'])) / float(c['c']) * 100
        candle_vol = float(c['v']) * float(c['c'])
        
        if candle_range > atr * 1.5 or candle_vol > avg_vol * 2.0:
            return {
                "time": c.get('t', 0) / 1000,
                "range_pct": candle_range,
                "volume_ratio": candle_vol / avg_vol if avg_vol > 0 else 1.0,
                "idx": i,
                "high": float(c['h']),
                "low": float(c['l']),
                "close": float(c['c'])
            }
    
    return None


def get_distance_from_impulse(coin: str, master: Dict = None) -> float:
    """
    Hitung jarak dari impulse candle terakhir.
    Return: 0-1 score (1 = perfect distance, 0 = terlalu dekat/terlalu jauh)
    """
    impulse = detect_impulse_candle(coin, master)
    if not impulse:
        return 0.5
    
    minutes_since = (time.time() - impulse["time"]) / 60
    
    if 5 <= minutes_since <= 15:
        return 1.0
    elif 3 <= minutes_since < 5:
        return 0.8
    elif 15 < minutes_since <= 25:
        return 0.7
    elif 1 <= minutes_since < 3:
        return 0.5
    elif minutes_since < 1:
        return 0.3
    else:
        return max(0.0, 1.0 - (minutes_since - 25) / 60)


def get_impulse_age_minutes(coin: str, master: Dict) -> float:
    """Get age of last impulse in minutes."""
    impulse = detect_impulse_candle(coin, master)
    if not impulse:
        return 999
    return (time.time() - impulse["time"]) / 60

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

def get_liquidity_efficiency(coin: str) -> float:
    """
    P2: OI/Volume ratio — modal terkonsentrasi vs cuma volume gede (bisa
    wash/noise). Dipakai sebagai VIABILITY GATE, BUKAN pengganti volume
    ranking — additive filter di atas gate yang udah ada (imbalance/OI
    pattern), bukan discovery engine baru.
    Unit: snapshot.oi dalam JUTA USD (lihat komentar line ~6565).
    dayNtlVlm dari get_exchange_meta() dalam USD mentah.
    """
    try:
        oi_usd_m = get_oi_usd(coin)
        if oi_usd_m <= 0:
            return 0.0
        meta = get_exchange_meta()
        if not meta:
            return 0.5  # data gak ada → netral, jangan block coin
        vol_24h = 0.0
        for asset, ctx in zip(meta[0]["universe"], meta[1]):
            if asset["name"] == coin:
                vol_24h = float(ctx.get("dayNtlVlm", 0) or 0)
                break
        if vol_24h <= 0:
            return 0.5
        vol_24h_m = vol_24h / 1_000_000
        ratio = oi_usd_m / vol_24h_m
        return ratio
    except Exception:
        return 0.5

def is_viable_coin(coin: str, min_ratio: float = 0.03) -> bool:
    """P2: viability gate — OI/Volume ratio > min_ratio. Default longgar
    (0.03) biar gak nge-block alt kecil yang legit, cuma nyaring coin
    volume-gede-tapi-OI-kosong (wash trading / noise pump)."""
    return get_liquidity_efficiency(coin) > min_ratio

def get_funding_pct(coin: str) -> float:
    snapshot = get_snapshot()
    if snapshot and coin in snapshot.funding:
        return snapshot.funding[coin]
    return 0.0

# ============================================================
# L1 ENTRY WINDOW — ENTRY ZONE CALCULATOR
# ============================================================

def get_vwap_in_zone(coin: str, low: float, high: float, master: Dict) -> float:
    """Calculate VWAP within price zone from recent candles."""
    candles = get_candles(coin, "5m", 20, master)
    if not candles:
        return (low + high) / 2
    
    total_volume = 0.0
    total_value = 0.0
    
    for c in candles[-10:]:
        price = float(c['c'])
        volume = float(c['v'])
        if low <= price <= high:
            total_volume += volume
            total_value += volume * price
    
    if total_volume > 0:
        return total_value / total_volume
    return (low + high) / 2


def get_spread_compression(coin: str, master: Dict = None) -> float:
    """
    Spread compression = current range / ATR.
    Lower = more compressed = better for entry.

    master: optional (Smart Data Access Layer cache) — kalau dikasih,
    diteruskan ke get_candles/get_atr_pct buat cache-aware fetch.
    Dipanggil tanpa master dari L0 Discovery, dengan master dari L1
    Entry Window.
    """
    candles = get_candles(coin, "5m", 10, master)
    if not candles or len(candles) < 3:
        return 0.5
    
    recent_ranges = [float(c['h']) - float(c['l']) for c in candles[-3:]]
    avg_range = sum(recent_ranges) / len(recent_ranges)
    avg_price = float(candles[-1]['c'])
    range_pct = avg_range / avg_price * 100
    
    atr = get_atr_pct(coin, 14, "1h", master)
    if atr <= 0:
        atr = 1.0
    
    compression = min(1.0, range_pct / atr)
    return 1.0 - compression


def get_velocity_alignment(coin: str, direction: str, master: Dict) -> float:
    """
    Check if price and delta are moving in same direction.
    """
    delta_shift = get_delta_shift(coin)
    
    candles = get_candles(coin, "5m", 6, master)
    if not candles or len(candles) < 2:
        price_change = 0
    else:
        price_now = float(candles[-1]['c'])
        price_5m_ago = float(candles[0]['c'])
        price_change = (price_now - price_5m_ago) / max(price_5m_ago, 0.01) * 100 if price_5m_ago else 0
    
    if direction == "LONG":
        if delta_shift > 0 and price_change > 0:
            return 1.0
        elif delta_shift > 0 or price_change > 0:
            return 0.6
        else:
            return 0.2
    else:
        if delta_shift < 0 and price_change < 0:
            return 1.0
        elif delta_shift < 0 or price_change < 0:
            return 0.6
        else:
            return 0.2


def get_delta_persistence_score(coin: str, direction: str, window: int = 3) -> float:
    """
    Check if delta maintained direction over last N candles.
    Returns 0-1 score.
    """
    with _rolling_delta_lock:
        if coin not in _rolling_delta or len(_rolling_delta[coin]) < window:
            return 0.3
        recent = list(_rolling_delta[coin])[-window:]
    
    if direction == "LONG":
        if all(d > -0.5 for d in recent) and recent[-1] > recent[0]:
            return 1.0
        elif all(d > -0.5 for d in recent):
            return 0.7
        elif recent[-1] > recent[0]:
            return 0.5
        else:
            return 0.3
    else:
        if all(d < 0.5 for d in recent) and recent[-1] < recent[0]:
            return 1.0
        elif all(d < 0.5 for d in recent):
            return 0.7
        elif recent[-1] < recent[0]:
            return 0.5
        else:
            return 0.3


def calculate_entry_zone(
    coin: str,
    direction: str,
    base_price: float,
    event: TradeEvent,
    master: Dict,
    atr_pct: float
) -> Dict:
    """
    L1 ENTRY WINDOW CORE: Buat entry zone, BUKAN limit price fixed.
    
    Components:
    1. Flow stability (delta persistence)
    2. Spread quality (compression vs ATR)
    3. Velocity alignment (price & delta searah)
    4. Distance from impulse
    """
    # ===== 1. FIND SUPPORT/RESISTANCE =====
    candles_1h = get_candles(coin, "1h", 60, master)
    highs, lows = detect_swing_points(candles_1h, lookback=3) if candles_1h else ([], [])
    
    if direction == "LONG":
        supports = [l[1] for l in lows if l[1] < base_price]
        nearest_support = max(supports) if supports else base_price * (1 - atr_pct / 100 * 0.5)
        zone_low = nearest_support
        zone_high = base_price * 1.005
    else:
        resistances = [h[1] for h in highs if h[1] > base_price]
        nearest_resistance = min(resistances) if resistances else base_price * (1 + atr_pct / 100 * 0.5)
        zone_low = base_price * 0.995
        zone_high = nearest_resistance
    
    # ===== 2. FIND OPTIMAL ENTRY =====
    vwap = get_vwap_in_zone(coin, zone_low, zone_high, master)
    optimal_price = vwap
    optimal_price = max(zone_low, min(zone_high, optimal_price))
    
    # ===== 3. COMPUTE ENTRY QUALITY =====
    flow_stability = get_delta_persistence_score(coin, direction, window=3)
    spread_quality = get_spread_compression(coin, master)
    velocity_alignment = get_velocity_alignment(coin, direction, master)
    impulse_distance = get_distance_from_impulse(coin, master)
    
    entry_quality = (
        flow_stability * 0.30 +
        spread_quality * 0.25 +
        velocity_alignment * 0.25 +
        impulse_distance * 0.20
    ) * 100
    
    # ===== 4. SL DISTANCE =====
    sl_distance = abs(optimal_price - zone_low) / max(optimal_price, 0.01) * 100
    sl_distance = max(0.1, min(5.0, sl_distance))
    
    return {
        "zone_low": zone_low,
        "zone_high": zone_high,
        "optimal_entry": optimal_price,
        "entry_quality": round(entry_quality, 2),
        "sl_distance_pct": round(sl_distance, 2),
        "confidence": min(1.0, entry_quality / 80),
        "components": {
            "flow_stability": round(flow_stability, 2),
            "spread_quality": round(spread_quality, 2),
            "velocity_alignment": round(velocity_alignment, 2),
            "impulse_distance": round(impulse_distance, 2),
        },
        "impulse_age_minutes": get_impulse_age_minutes(coin, master)
    }
    
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


# ============================================================
# P4.6 — CONFIDENCE CALIBRATION (Fix Overconfidence)
# ============================================================
# Diagnosis: WR 31-50 = 52%, WR 51-70 = 25% → bot overconfident di range 51-70.
# Solution: penalty di overconfidence zone, dikontrol confidence sumber.
# ============================================================

def calibrate_confidence_v2(
    coin: str,
    raw_confidence: float,
    evidence_families: int,
    entropy_market: int,
) -> float:
    """
    P4.6: Confidence calibration dengan penalty untuk overconfidence.

    Overconfidence zone: 51-70 (WR historis 25%, lebih rendah dari 31-50).
    Heavy zone: >70 (sangat overconfident).
    """
    confidence = raw_confidence

    # Evidence boost: turunkan dari +10 → +5 per family, cap 10
    ev_boost = min(10, evidence_families * 5)
    confidence += ev_boost

    # Overconfidence penalty
    if 51 <= raw_confidence <= 70:
        center = 60.0
        distance = abs(raw_confidence - center)
        penalty = max(0, 15 - distance * 0.5)   # peak = -15 di score 60
        confidence -= penalty
        if penalty > 2:
            logger.debug(f"P4.6 OVERCONFIDENCE_PENALTY {coin}: raw={raw_confidence:.0f} penalty={penalty:.1f}")
    elif raw_confidence > 70:
        penalty = 10 + (raw_confidence - 70) * 0.3
        confidence -= penalty
        if penalty > 2:
            logger.debug(f"P4.6 HIGH_OVERCONF_PENALTY {coin}: raw={raw_confidence:.0f} penalty={penalty:.1f}")

    # Entropy market penalty
    if entropy_market > 80:
        confidence *= 0.80
    elif entropy_market > 60:
        confidence *= 0.90

    return round(max(0.0, min(100.0, confidence)), 2)

# ============================================================
# END P4.6
# ============================================================

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


# ============================================================
# P4.12: SHADOW REASON FIX + P4.19-30: QUALITY & CONVICTION STACK
# ============================================================

# ===== P4.12: SHADOW REASON BREAKDOWN =====
def get_shadow_breakdown() -> Dict[str, int]:
    """Extract shadow reasons from journal narrative."""
    with _journal_lock:
        shadows = [e for e in _decision_journal if getattr(e, "shadow", False)]
        reasons = {}
        for e in shadows:
            narr = getattr(e, "narrative", {}) or {}
            reason = narr.get("block_reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        return reasons


# ===== P4.19: QUALITY SPREAD =====
def spread_score(score: float) -> float:
    """Apply quality spread: compress 0-30, natural 30-70, boost 70+."""
    if score < 30:
        return score * 0.8
    elif score < 50:
        return score
    elif score < 70:
        return score + (score - 50) * 0.4
    else:
        return score + 8


# ===== P4.20: ENTRY QUALITY =====
def compute_entry_quality(mae: float, mfe: float, tp1_hit: bool = False) -> int:
    """Compute entry quality 0-10."""
    quality = 0
    if mae < 0.3:  # small MAE (didn't go far wrong initially)
        quality += 4
    if mfe > 1.0:  # decent MFE (went positive nicely)
        quality += 4
    if tp1_hit:    # hit TP1
        quality += 2
    return min(10, quality)


# ===== P4.21: ABSORPTION =====
def compute_absorption(coin: str, candles_5m: list) -> float:
    """Compute market absorption: volume / range (thousands)."""
    if not candles_5m or len(candles_5m) < 5:
        return 0.0
    try:
        vols = [float(c['v']) * float(c['c']) for c in candles_5m[-5:]]
        ranges = [float(c['h']) - float(c['l']) for c in candles_5m[-5:]]
        avg_vol = sum(vols) / len(vols) if vols else 1
        avg_range = sum(ranges) / len(ranges) if ranges else 0.01
        absorption = avg_vol / (avg_range + 0.001)
        return absorption
    except:
        return 0.0


# ===== P4.22: MTF ALIGNMENT =====
def get_mtf_alignment(coin: str) -> int:
    """Check multi-timeframe alignment. Returns 0-3 (num aligned TFs)."""
    try:
        candles_15 = get_candles(coin, "15m", 30)
        candles_1h = get_candles(coin, "1h", 30)
        candles_4h = get_candles(coin, "4h", 30)
        if not all([candles_15, candles_1h, candles_4h]):
            return 0
        
        def trend(candles):
            closes = [float(c['c']) for c in candles[-21:]]
            if len(closes) < 21:
                return 0
            ema8 = np.mean(closes[-8:])
            ema21 = np.mean(closes[-21:])
            if ema8 > ema21:
                return 1
            elif ema8 < ema21:
                return -1
            else:
                return 0
        
        dirs = [trend(candles_15), trend(candles_1h), trend(candles_4h)]
        dirs = [d for d in dirs if d != 0]
        if not dirs:
            return 0
        from collections import Counter
        return Counter(dirs).most_common(1)[0][1] if dirs else 0
    except:
        return 0


# ===== P4.26: EDGE VELOCITY =====
def get_edge_velocity(coin: str, window: int = 10) -> float:
    """Get recent edge velocity (pnl/mfe) as ±6 signal."""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute('''
            SELECT pnl, mfe FROM signals
            WHERE coin = ? AND evaluated = 1 AND mfe IS NOT NULL AND mfe > 0
            ORDER BY timestamp DESC LIMIT ?
        ''', (coin, window))
        rows = c.fetchall()
        conn.close()
        
        if len(rows) < 3:
            return 0.0
        
        edges = [pnl / mfe for pnl, mfe in rows if mfe > 0]
        avg_edge = np.mean(edges)
        return np.clip(avg_edge * 2, -6, 6)  # scale to ±6
    except:
        return 0.0


# ===== P4.30: EDGE MEMORY =====
_edge_memory: Dict[str, deque] = {}
_edge_memory_lock = threading.RLock()

def update_edge_memory(coin: str, pnl: float, mfe: float):
    """Track recent edges per coin."""
    if mfe <= 0:
        return
    edge = pnl / mfe
    with _edge_memory_lock:
        if coin not in _edge_memory:
            _edge_memory[coin] = deque(maxlen=20)
        _edge_memory[coin].append(edge)


def get_memory_boost(coin: str) -> float:
    """Get conviction boost from recent edge memory ±6."""
    with _edge_memory_lock:
        if coin not in _edge_memory or len(_edge_memory[coin]) < 3:
            return 0.0
        recent = list(_edge_memory[coin])[-3:]
        avg_edge = np.mean(recent)
        if avg_edge > 0.5:
            return 4.0
        elif avg_edge > 0.2:
            return 2.0
        elif avg_edge < -0.2:
            return -6.0
        return 0.0


# ===== P4.24: DISCOVERY CONVERSION =====
_discovery_stats = {
    "shadow_total": 0,
    "shadow_wins": 0,
    "exec_total": 0,
    "exec_wins": 0,
}
_discovery_stats_lock = threading.RLock()

def update_discovery_stats(signal_id: str, outcome: str, is_shadow: bool = False):
    """Track shadow vs exec outcomes."""
    if outcome in ("TP_HIT", "PARTIAL_WIN"):
        win = 1
    elif outcome in ("SL_HIT", "PARTIAL_LOSS"):
        win = 0
    else:
        return  # break_even or unknown
    
    with _discovery_stats_lock:
        if is_shadow:
            _discovery_stats["shadow_total"] += 1
            _discovery_stats["shadow_wins"] += win
        else:
            _discovery_stats["exec_total"] += 1
            _discovery_stats["exec_wins"] += win


def get_discovery_rate() -> float:
    """Get shadow win rate 0-100."""
    with _discovery_stats_lock:
        total = _discovery_stats["shadow_total"]
        if total == 0:
            return 0.0
        return _discovery_stats["shadow_wins"] / total * 100


# ===== P4.25: CONVICTION ENGINE (ACE) =====
def compute_conviction(
    quality: float,
    coin: str,
    entry_quality: int,
    discovery_rate: float,
    recent_exec_wr: float,
    regime: str,
    fatigue: float,
    edge_velocity: float,
    memory_boost: float
) -> float:
    """Compute conviction 0-100+."""
    boost = 0
    
    # Regime bonus
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        boost += 3
    
    # Entry quality bonus
    if entry_quality >= 8:
        boost += 5
    elif entry_quality >= 5:
        boost += 2
    
    # Discovery conversion bonus
    if discovery_rate > 15:
        boost += 4
    
    # Recent execution performance
    if recent_exec_wr > 0.55:
        boost += 3
    
    # Fatigue penalty (inverse)
    fatigue_penalty = int((1.0 - fatigue) * 10)
    
    # Velocity and memory already scaled ±6
    conviction = max(0, quality + boost - fatigue_penalty + edge_velocity + memory_boost)
    return conviction


# ===== P4.23: POSITION SIZE =====
def compute_position_size(
    conviction: float,
    regime: str,
    entry_quality: int,
    personality_size: float
) -> float:
    """Compute position size 0.5-2.0."""
    conv_factor = max(0.5, min(2.0, conviction / 70))
    regime_mult = {"TRENDING_UP": 1.2, "TRENDING_DOWN": 1.2, "RANGING": 0.8}.get(regime, 1.0)
    entry_mult = 1.0 + (entry_quality / 100)  # 1.0 - 1.1
    size = conv_factor * regime_mult * entry_mult * personality_size
    return max(0.5, min(2.0, size))


# ===== P4.27: AUTO PERSONALITY =====
def get_auto_personality(
    discovery_rate: float,
    recent_wr: float,
    total_pnl: float,
    shadow_pressure: float,
    edge_memory_avg: float
) -> Dict[str, Any]:
    """Get adaptive personality based on live metrics."""
    if discovery_rate > 18 and recent_wr > 0.52 and edge_memory_avg > 0.30:
        return {"mode": "HUNTER", "conviction": 5, "size": 1.35}
    elif recent_wr > 0.50 and total_pnl > 0:
        return {"mode": "AGGRESSIVE", "conviction": 2, "size": 1.15}
    elif total_pnl < -5 or shadow_pressure > 80:
        return {"mode": "DEFENSIVE", "conviction": -4, "size": 0.75}
    else:
        return {"mode": "NORMAL", "conviction": 0, "size": 1.00}


def get_shadow_pressure() -> float:
    """Shadow total / (executed + shadow) * 100."""
    with _shadow_stats_lock:
        executed = _shadow_stats.get("total", 0)  # existing shadow_stats tracks all shadows
    
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM signals WHERE outcome IN ('TP_HIT', 'PARTIAL_WIN', 'SL_HIT', 'PARTIAL_LOSS')")
        exec_count = c.fetchone()[0] or 0
        conn.close()
        
        denom = executed + exec_count
        if denom == 0:
            return 0.0
        return executed / denom * 100
    except:
        return 0.0


def get_total_pnl() -> float:
    """Get total PnL from executed trades."""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT SUM(pnl) FROM signals WHERE evaluated=1")
        total = c.fetchone()[0] or 0.0
        conn.close()
        return total
    except:
        return 0.0

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

        # ===== JOURNAL TTL: drop entries older than 30 hari (in-place, thread-safe) =====
        MAX_JOURNAL_AGE_DAYS = 30
        cutoff = time.time() - MAX_JOURNAL_AGE_DAYS * 86400
        stale = sum(1 for e in _decision_journal if e.timestamp <= cutoff)
        if stale:
            _decision_journal[:] = [e for e in _decision_journal if e.timestamp > cutoff]

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

def log_velocity_trace(
    coin: str,
    decision: str,
    score: Optional[float] = None,
    threshold: Optional[float] = None,
    regime: Optional[str] = None,
    size_mult: float = 0.0,
    position_gate: str = "SKIP",
    stage: str = "UNKNOWN",
    cache_age: float = 999.0,
    data_source: str = "UNKNOWN",
    reason: str = "",
):
    """Log per-decision decision-level metrics. score/threshold/regime may
    be None (e.g. for skip/reject events that occur before scoring or
    before regime is computed) — these are rendered as the literal string
    'None' rather than coerced to a default, so downstream parsers
    (cmd_velocity) can tell "not computed yet" (None) apart from
    "computed and it's UNKNOWN" (which never actually happens for regime —
    get_market_regime() only returns TRENDING_UP/TRENDING_DOWN/RANGING).
    Coercing None->"UNKNOWN" here would relabel "telemetry lost context"
    as "regime detector returned unknown", which is a different failure
    and misleads anyone reading the dashboard."""
    try:
        pipe = get_pipeline_metrics()
        score_str = f"{score:.1f}" if score is not None else "None"
        threshold_str = f"{threshold:.1f}" if threshold is not None else "None"
        gap_str = f"{score - threshold:+.1f}" if (score is not None and threshold is not None) else "None"
        regime_str = regime if regime is not None else "None"
        logger.info(
            "VELOCITY_TRACE "
            f"coin={coin} "
            f"decision={decision} "
            f"score={score_str} "
            f"threshold={threshold_str} "
            f"gap={gap_str} "
            f"regime={regime_str} "
            f"size={size_mult:.2f} "
            f"pos={position_gate} "
            f"stage={stage} "
            f"cache_age={cache_age:.0f}s "
            f"source={data_source} "
            f"reason={reason} "
            f"obs={pipe.get('obs', 0)} "
            f"thesis={pipe.get('thesis', 0)} "
            f"conf={pipe.get('confidence', 0)} "
            f"exec={pipe.get('exec', 0)}"
        )
    except Exception as e:
        logger.debug(f"velocity_trace error: {e}")


def emit_velocity_skip(
    coin: str,
    reason: str,
    stage: str = "SKIP",
    score: Optional[float] = None,
    threshold: Optional[float] = None,
    regime: Optional[str] = None,
    cache_age: Optional[float] = None,
    source: Optional[str] = None,
    size: Optional[float] = None,
):
    """
    P4.0/P4.1 — Emit a VELOCITY_TRACE for skip/reject gates WITH whatever
    context is available at that point in the pipeline (score, regime,
    size, data source, cache age), instead of log_skip()'s hardcoded
    zeros/UNKNOWN. regime=None is passed through as-is (not coerced to
    "UNKNOWN") — see log_velocity_trace docstring: None means "telemetry
    didn't have a regime to report at this stage", which is a different
    fact from "regime was computed and is unknown" (the latter never
    happens). Use None for score/threshold when not yet computed at this
    stage — log_velocity_trace renders that as "None" so the /velocity
    aggregator can exclude it rather than treating it as an actual 0.
    """
    log_velocity_trace(
        coin=coin,
        decision="NONE",
        score=score,
        threshold=threshold,
        regime=regime,
        size_mult=size if size is not None else 0.0,
        position_gate="SKIP",
        stage=stage,
        cache_age=cache_age if cache_age is not None else 999.0,
        data_source=source or "UNKNOWN",
        reason=reason,
    )


def log_skip(
    coin: str,
    reason: str,
    stage: str = "SKIP",
    cache_age: float = 999.0,
    data_source: str = "UNKNOWN",
):
    """Log skip/abort events that don't reach decision stage."""
    try:
        pipe = get_pipeline_metrics()
        logger.info(
            "VELOCITY_TRACE "
            f"coin={coin} "
            f"decision=NONE "
            f"score=0.0 "
            f"threshold=0.0 "
            f"gap=0.0 "
            f"regime=UNKNOWN "
            f"size=0.00 "
            f"pos=SKIP "
            f"stage={stage} "
            f"cache_age={cache_age:.0f}s "
            f"source={data_source} "
            f"reason={reason} "
            f"obs={pipe.get('obs', 0)} "
            f"thesis={pipe.get('thesis', 0)} "
            f"conf={pipe.get('confidence', 0)} "
            f"exec={pipe.get('exec', 0)}"
        )
    except Exception as e:
        logger.debug(f"log_skip error: {e}")

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
        tg_send(USER_ID, text, parse_mode='HTML')
        if CHANNEL_ID:
            tg_send(CHANNEL_ID, text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Auto review send error: {e}")

# ============================================================
# PATCH v10.3.3 — AUTO-HEAL ORPHANS
# ============================================================

def auto_heal_orphans():
    """Auto-heal orphan positions every 5 minutes."""
    while RUNTIME.is_running():
        time.sleep(300)
        try:
            health = check_signal_db_health()
            orphan_count = health.get("orphan_count", 0)
            
            if orphan_count > 50:
                logger.warning(f"🔴 AUTO-HEAL: {orphan_count} orphans detected, triggering cleanup...")
                reconcile_open_positions()
        except Exception as e:
            logger.error(f"auto_heal_orphans error: {e}")

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


# ============================================================
# V2 STRUCTURE ENGINE — DETECTORS
# ============================================================
# TradeEvent contract TIDAK diubah (strength/confidence/extra tetap sama
# seperti V1). Semua metric baru dari V2 (score/freshness/persistence/
# quality/age/dll) disimpan di dalam `extra` dict, BUKAN field baru di
# TradeEvent — supaya 11 consumer existing yang baca event.strength tetap
# jalan tanpa perubahan, dan V1/V2 bisa hidup berdampingan tanpa breaking
# change. strength = score (skala sama, 0-100).
#
# Semua detector menghasilkan TradeEvent dengan extra berisi:
# - score: 0-100 (seberapa kuat sinyalnya) — sama dengan .strength
# - confidence: 0-1 (seberapa bisa dipercaya datanya) — juga di top-level .confidence*100
# - freshness: 0-1 (seberapa baru struktur itu)
# - persistence: 0-1 (seberapa lama struktur bertahan)
# - quality: 0-1 (kombinasi percentile + zscore × confidence)
# - age: detik sejak terbentuk

def find_ob_v2(candles, direction, current_price, coin: str) -> Optional[TradeEvent]:
    """
    Order Block V2: score-based, threshold dinamis.
    Components: reaction strength, volume, freshness.
    """
    if not candles or len(candles) < 10:
        return None

    for i in range(len(candles) - 3, 1, -1):
        c, nxt = candles[i], candles[i+1]

        if direction == "LONG" and float(c['c']) < float(c['o']) and float(nxt['c']) > float(nxt['o']) and float(nxt['c']) > float(c['h']):
            ob_low, ob_high = float(c['l']), float(c['h'])

            # 1. Reaction strength: seberapa kuat bounce dari OB
            reaction = _ob_reaction_v2(candles, i, ob_low, ob_high, direction)
            update_metric(_ob_reaction_history, coin, reaction)
            reaction_score = combined_score(get_metric_history(_ob_reaction_history, coin), reaction, pct_weight=0.6)

            # 2. Volume: impulse candle vs average
            vol_impulse = float(c['v'])
            prev_vols = [float(candles[j]['v']) for j in range(max(0, i-5), i)]
            vol_avg = np.mean(prev_vols) if prev_vols else 1.0
            vol_ratio = vol_impulse / vol_avg if vol_avg > 0 else 1.0
            update_metric(_ob_vol_history, coin, vol_ratio)
            vol_score = combined_score(get_metric_history(_ob_vol_history, coin), vol_ratio, pct_weight=0.6)

            # 3. Freshness: berapa kali di-retest
            retest_count = _ob_retest_count_v2(candles, i, ob_low, ob_high, direction)
            freshness = freshness_from_retests(retest_count)

            # 4. Persistence: apakah OB masih valid (belum ditembus)
            persistence = 1.0
            for j in range(i+2, min(i+10, len(candles))):
                if direction == "LONG" and float(candles[j]['c']) < ob_low:
                    persistence = 0.3
                    break
                elif direction == "SHORT" and float(candles[j]['c']) > ob_high:
                    persistence = 0.3
                    break

            # Score: reaction (20%) + volume (35%) + reaction_raw (20%) + freshness (25%)
            score = reaction_score * 0.20 + vol_score * 0.35 + (reaction * 0.20) + (freshness * 100 * 0.25)
            score = min(100, max(0, score))

            confidence = compute_detector_confidence(
                hist_len=len(get_metric_history(_ob_reaction_history, coin)),
                data_freshness=1.0 if candles else 0.8,
                event_quality=0.7 + (score / 333)
            )

            age = (time.time() - (c.get('t', 0) / 1000)) if c.get('t') else 0
            quality = round((score / 100) * confidence, 3)

            event = TradeEvent(
                "OB", ob_low, ob_high, round(score, 1), direction,
                {
                    "idx": i,
                    "reaction": round(reaction, 1),
                    "retest_count": retest_count,
                    "vol_ratio": round(vol_ratio, 2),
                    "score": round(score, 1),
                    "freshness": freshness,
                    "persistence": persistence,
                    "quality": quality,
                    "age": age,
                },
                confidence=round(confidence * 100, 1),
                source_count=1,
            )
            return event

    return None

def _ob_reaction_v2(candles, idx, ob_low, ob_high, direction):
    """Calculate reaction strength: how strong price bounced from OB."""
    if idx + 3 >= len(candles):
        return 50.0

    if direction == "LONG":
        lows = [float(candles[j]['l']) for j in range(idx+1, min(idx+5, len(candles)))]
        if not lows or ob_high == ob_low:
            return 50.0
        min_low = min(lows)
        ob_range = ob_high - ob_low
        if ob_range <= 0:
            return 50.0
        return min(100, max(0, (ob_low - min_low) / ob_range * 100))
    else:
        highs = [float(candles[j]['h']) for j in range(idx+1, min(idx+5, len(candles)))]
        if not highs or ob_high == ob_low:
            return 50.0
        max_high = max(highs)
        ob_range = ob_high - ob_low
        if ob_range <= 0:
            return 50.0
        return min(100, max(0, (max_high - ob_high) / ob_range * 100))

def _ob_retest_count_v2(candles, idx, ob_low, ob_high, direction):
    """Count how many times OB was retested."""
    count = 0
    for j in range(idx+2, min(idx+20, len(candles))):
        if direction == "LONG" and float(candles[j]['l']) <= ob_low * 1.005:
            count += 1
        elif direction == "SHORT" and float(candles[j]['h']) >= ob_high * 0.995:
            count += 1
    return count

# ============================================================

def find_fvg_v2(candles, current_price, coin: str) -> Optional[TradeEvent]:
    """
    Fair Value Gap V2: score-based, threshold dinamis.
    Components: gap size, fill ratio, age.
    """
    if not candles or len(candles) < 10:
        return None

    for i in range(len(candles)-1, 1, -1):
        c1, c3 = candles[i-2], candles[i]
        c1h, c1l, c3h, c3l = float(c1['h']), float(c1['l']), float(c3['h']), float(c3['l'])

        # Bullish FVG: c3l > c1h
        if c3l > c1h:
            gap_low, gap_high = c1h, c3l
            gap_pct = (gap_high - gap_low) / max(gap_low, 0.01) * 100
            if gap_pct < 0.15:
                continue

            update_metric(_gap_history, coin, gap_pct)
            gap_score = combined_score(get_metric_history(_gap_history, coin), gap_pct, pct_weight=0.6)

            filled, fill_pct = _fvg_fill_v2(candles, i, gap_low, gap_high, "LONG")
            update_metric(_fill_history, coin, fill_pct)
            fill_score = combined_score(get_metric_history(_fill_history, coin), (1 - fill_pct) * 100, pct_weight=0.5)

            age = (time.time() - (candles[i].get('t', 0) / 1000)) if candles[i].get('t') else 0

            score = gap_score * 0.4 + fill_score * 0.4 + max(0, (100 - age / 60)) * 0.2
            score = min(100, max(0, score))

            confidence = compute_detector_confidence(
                hist_len=len(get_metric_history(_gap_history, coin)),
                data_freshness=1.0 if candles else 0.8,
                event_quality=0.7 + (score / 400)
            )
            freshness = max(0, 1 - age / 3600)
            persistence = 1.0 if fill_pct < 0.3 else 0.7
            quality = round((score / 100) * confidence, 3)

            event = TradeEvent(
                "FVG", gap_low, gap_high, round(score, 1), "LONG",
                {
                    "idx": i, "gap_pct": round(gap_pct, 2), "fill_ratio": round(fill_pct, 3),
                    "score": round(score, 1), "freshness": freshness,
                    "persistence": persistence, "quality": quality, "age": age,
                },
                confidence=round(confidence * 100, 1),
                source_count=1,
            )
            return event

        # Bearish FVG: c3h < c1l
        if c3h < c1l:
            gap_low, gap_high = c3h, c1l
            gap_pct = (gap_high - gap_low) / max(gap_low, 0.01) * 100
            if gap_pct < 0.15:
                continue

            update_metric(_gap_history, coin, gap_pct)
            gap_score = combined_score(get_metric_history(_gap_history, coin), gap_pct, pct_weight=0.6)

            filled, fill_pct = _fvg_fill_v2(candles, i, gap_low, gap_high, "SHORT")
            update_metric(_fill_history, coin, fill_pct)
            fill_score = combined_score(get_metric_history(_fill_history, coin), (1 - fill_pct) * 100, pct_weight=0.5)

            age = (time.time() - (candles[i].get('t', 0) / 1000)) if candles[i].get('t') else 0

            score = gap_score * 0.4 + fill_score * 0.4 + max(0, (100 - age / 60)) * 0.2
            score = min(100, max(0, score))

            confidence = compute_detector_confidence(
                hist_len=len(get_metric_history(_gap_history, coin)),
                data_freshness=1.0 if candles else 0.8,
                event_quality=0.7 + (score / 400)
            )
            freshness = max(0, 1 - age / 3600)
            persistence = 1.0 if fill_pct < 0.3 else 0.7
            quality = round((score / 100) * confidence, 3)

            event = TradeEvent(
                "FVG", gap_low, gap_high, round(score, 1), "SHORT",
                {
                    "idx": i, "gap_pct": round(gap_pct, 2), "fill_ratio": round(fill_pct, 3),
                    "score": round(score, 1), "freshness": freshness,
                    "persistence": persistence, "quality": quality, "age": age,
                },
                confidence=round(confidence * 100, 1),
                source_count=1,
            )
            return event

    return None

def _fvg_fill_v2(candles, idx, gap_low, gap_high, direction):
    """Calculate FVG fill ratio."""
    filled = 0.0
    for j in range(idx+1, min(idx+20, len(candles))):
        close = float(candles[j]['c'])
        if direction == "LONG":
            if close <= gap_low:
                return True, 1.0
            elif close < gap_high:
                filled = max(filled, (close - gap_low) / (gap_high - gap_low))
        else:  # SHORT
            if close >= gap_high:
                return True, 1.0
            elif close > gap_low:
                filled = max(filled, (gap_high - close) / (gap_high - gap_low))
    return False, filled

# ============================================================

def find_sweep_v2(candles, current_price, vol_spike, coin: str) -> Optional[TradeEvent]:
    """
    Liquidity Sweep V2: score-based, threshold dinamis.
    Components: volume spike percentile + swing detection.
    """
    if not candles or len(candles) < 10:
        return None

    highs, lows = detect_swing_points(candles, lookback=3)
    if not highs and not lows:
        return None

    update_metric(_sweep_vol_history, coin, vol_spike)
    vol_score = combined_score(get_metric_history(_sweep_vol_history, coin), vol_spike, pct_weight=0.5)

    # LONG sweep
    if lows and current_price <= lows[-1][1] * 1.002:
        age = (time.time() - (candles[-1].get('t', 0) / 1000)) if candles[-1].get('t') else 0
        score = vol_score * 0.7 + 30
        score = min(100, max(0, score))

        confidence = compute_detector_confidence(
            hist_len=len(get_metric_history(_sweep_vol_history, coin)),
            data_freshness=1.0 if candles else 0.8,
            event_quality=0.6 + (score / 400)
        )

        # Check if already reclaimed
        reclaimed = False
        for c in candles[-3:]:
            if float(c['c']) > lows[-1][1] * 1.01:
                reclaimed = True
                break

        freshness = max(0, 1 - age / 1800)
        persistence = 0.5
        quality = round((score / 100) * confidence, 3)

        return TradeEvent(
            "LIQUIDITY", lows[-1][1] * 0.999, lows[-1][1] * 1.001, round(score, 1), "LONG",
            {
                "swing_idx": lows[-1][0], "vol_spike": round(vol_spike, 2), "reclaimed": reclaimed,
                "score": round(score, 1), "freshness": freshness,
                "persistence": persistence, "quality": quality, "age": age,
            },
            confidence=round(confidence * 100, 1),
            source_count=1,
        )

    # SHORT sweep
    if highs and current_price >= highs[-1][1] * 0.998:
        age = (time.time() - (candles[-1].get('t', 0) / 1000)) if candles[-1].get('t') else 0
        score = vol_score * 0.7 + 30
        score = min(100, max(0, score))

        confidence = compute_detector_confidence(
            hist_len=len(get_metric_history(_sweep_vol_history, coin)),
            data_freshness=1.0 if candles else 0.8,
            event_quality=0.6 + (score / 400)
        )

        reclaimed = False
        for c in candles[-3:]:
            if float(c['c']) < highs[-1][1] * 0.99:
                reclaimed = True
                break

        freshness = max(0, 1 - age / 1800)
        persistence = 0.5
        quality = round((score / 100) * confidence, 3)

        return TradeEvent(
            "LIQUIDITY", highs[-1][1] * 0.999, highs[-1][1] * 1.001, round(score, 1), "SHORT",
            {
                "swing_idx": highs[-1][0], "vol_spike": round(vol_spike, 2), "reclaimed": reclaimed,
                "score": round(score, 1), "freshness": freshness,
                "persistence": persistence, "quality": quality, "age": age,
            },
            confidence=round(confidence * 100, 1),
            source_count=1,
        )

    return None

# ============================================================

def find_ob_flow_v2(coin: str, current_price: float) -> Optional[TradeEvent]:
    """
    Order Book Flow V2: wall size percentile + delta confirmation.
    Threshold dinamis berbasis percentile wall history coin ini.
    """
    delta_shift = get_delta_shift(coin)
    bid_wall, bid_price = get_bid_wall_level(coin)
    ask_wall, ask_price = get_ask_wall_level(coin)

    # LONG: bid wall + positive delta
    if bid_wall > 0:
        update_metric(_wall_history, coin, bid_wall)
        wall_score = combined_score(get_metric_history(_wall_history, coin), bid_wall, pct_weight=0.5)

        if delta_shift > 0 and current_price <= bid_price * 1.005:
            delta_score = min(100, delta_shift * 5)
            score = wall_score * 0.5 + delta_score * 0.5
            score = min(100, max(0, score))

            confidence = compute_detector_confidence(
                hist_len=len(get_metric_history(_wall_history, coin)),
                data_freshness=1.0,
                event_quality=0.6 + (score / 400)
            )
            quality = round((score / 100) * confidence, 3)

            return TradeEvent(
                "OB_FLOW", bid_price * 0.998, bid_price * 1.002, round(score, 1), "LONG",
                {
                    "wall_usd": round(bid_wall, 0), "delta_shift": round(delta_shift, 1),
                    "score": round(score, 1), "freshness": 1.0,
                    "persistence": 0.8, "quality": quality, "age": 0,
                },
                confidence=round(confidence * 100, 1),
                source_count=1,
            )

    # SHORT: ask wall + negative delta
    if ask_wall > 0:
        update_metric(_wall_history, coin, ask_wall)
        wall_score = combined_score(get_metric_history(_wall_history, coin), ask_wall, pct_weight=0.5)

        if delta_shift < 0 and current_price >= ask_price * 0.995:
            delta_score = min(100, abs(delta_shift) * 5)
            score = wall_score * 0.5 + delta_score * 0.5
            score = min(100, max(0, score))

            confidence = compute_detector_confidence(
                hist_len=len(get_metric_history(_wall_history, coin)),
                data_freshness=1.0,
                event_quality=0.6 + (score / 400)
            )
            quality = round((score / 100) * confidence, 3)

            return TradeEvent(
                "OB_FLOW", ask_price * 0.998, ask_price * 1.002, round(score, 1), "SHORT",
                {
                    "wall_usd": round(ask_wall, 0), "delta_shift": round(delta_shift, 1),
                    "score": round(score, 1), "freshness": 1.0,
                    "persistence": 0.8, "quality": quality, "age": 0,
                },
                confidence=round(confidence * 100, 1),
                source_count=1,
            )

    return None

# ============================================================

def find_fvg_flow_v2(coin: str, current_price: float) -> Optional[TradeEvent]:
    """
    FVG Flow V2: CVD acceleration + delta persistence.
    Threshold dinamis berbasis percentile CVD change history.
    """
    delta_shift = get_delta_shift(coin)
    cvd_30 = get_cvd(coin, 30)
    cvd_60 = get_cvd(coin, 60)
    cvd_change = cvd_30 - cvd_60

    if abs(cvd_change) < 0.1:
        return None

    update_metric(_fvg_flow_cvd_history, coin, cvd_change)
    cvd_score = combined_score(get_metric_history(_fvg_flow_cvd_history, coin), cvd_change, pct_weight=0.5)

    delta_persist = get_delta_persistence_score(coin, "LONG" if delta_shift > 0 else "SHORT", window=3)

    # LONG: CVD positif + delta positif
    if cvd_change > 0 and delta_shift > 0:
        score = cvd_score * 0.5 + delta_persist * 50 * 0.5
        score = min(100, max(0, score))

        confidence = compute_detector_confidence(
            hist_len=len(get_metric_history(_fvg_flow_cvd_history, coin)),
            data_freshness=1.0,
            event_quality=0.6 + (score / 400)
        )
        quality = round((score / 100) * confidence, 3)
        fair_price = current_price * (1 + cvd_change / 100)

        return TradeEvent(
            "FVG_FLOW", current_price, max(current_price, fair_price), round(score, 1), "LONG",
            {
                "cvd_change": round(cvd_change, 3), "delta_shift": round(delta_shift, 1),
                "score": round(score, 1), "freshness": 1.0,
                "persistence": delta_persist, "quality": quality, "age": 0,
            },
            confidence=round(confidence * 100, 1),
            source_count=1,
        )

    # SHORT: CVD negatif + delta negatif
    if cvd_change < 0 and delta_shift < 0:
        score = cvd_score * 0.5 + delta_persist * 50 * 0.5
        score = min(100, max(0, score))

        confidence = compute_detector_confidence(
            hist_len=len(get_metric_history(_fvg_flow_cvd_history, coin)),
            data_freshness=1.0,
            event_quality=0.6 + (score / 400)
        )
        quality = round((score / 100) * confidence, 3)
        fair_price = current_price * (1 + cvd_change / 100)

        return TradeEvent(
            "FVG_FLOW", min(current_price, fair_price), current_price, round(score, 1), "SHORT",
            {
                "cvd_change": round(cvd_change, 3), "delta_shift": round(delta_shift, 1),
                "score": round(score, 1), "freshness": 1.0,
                "persistence": delta_persist, "quality": quality, "age": 0,
            },
            confidence=round(confidence * 100, 1),
            source_count=1,
        )

    return None

# ============================================================

def _vacuum_recovery_speed_v2(coin: str) -> float:
    """Placeholder recovery-speed estimator (0-100). Belum ada raw depth
    time-series tersimpan untuk hitung recovery slope beneran — dibuat
    sebagai fungsi terpisah supaya gampang di-upgrade nanti tanpa nyentuh
    find_vacuum_v2(). Return netral (50) selama data belum tersedia."""
    return 50.0

def find_vacuum_v2(coin: str, current_price: float) -> Optional[TradeEvent]:
    """
    Liquidity Vacuum V2: depth drop percentile + recovery speed.
    Threshold dinamis berbasis percentile depth drop history.
    """
    is_vacuum, severity, near, total, drop_ratio = detect_liquidity_vacuum(coin)
    if not is_vacuum:
        return None

    # Recovery speed: seberapa cepat depth kembali
    recovery_speed = _vacuum_recovery_speed_v2(coin)
    update_metric(_depth_recovery_history, coin, recovery_speed)
    recovery_score = combined_score(get_metric_history(_depth_recovery_history, coin), recovery_speed, pct_weight=0.5)

    update_metric(_depth_history, coin, drop_ratio)
    depth_score = combined_score(get_metric_history(_depth_history, coin), drop_ratio, pct_weight=0.6)

    score = depth_score * 0.4 + recovery_score * 0.4 + min(100, severity * 0.2) * 0.2
    score = min(100, max(0, score))

    confidence = compute_detector_confidence(
        hist_len=len(get_metric_history(_depth_history, coin)),
        data_freshness=1.0,
        event_quality=0.5 + (score / 400)
    )

    atr_pct = get_atr_pct(coin, 14, "1h")
    vacuum_range = atr_pct * 0.5
    low = current_price * (1 - vacuum_range / 100)
    high = current_price * (1 + vacuum_range / 100)
    quality = round((score / 100) * confidence, 3)

    return TradeEvent(
        "VACUUM", low, high, round(score, 1), "BOTH",
        {
            "severity": severity, "depth_drop_pct": round(drop_ratio * 100, 1),
            "score": round(score, 1), "freshness": 1.0,
            "persistence": 0.6, "quality": quality, "age": 0,
        },
        confidence=round(confidence * 100, 1),
        source_count=1,
    )

# ============================================================

def collect_all_events_v2(coin: str, current_price: float, master: Dict) -> List[TradeEvent]:
    """V2 Structure Engine entry point — dipanggil HANYA lewat dispatcher
    collect_all_events() saat STRUCTURE_ENGINE == 'v2'. Jangan panggil
    langsung dari tempat lain supaya observasi V1 vs V2 tetap bersih
    (lihat STRUCTURE_COMPARE untuk audit berdampingan)."""
    candles_1h = get_candles(coin, "1h", 100, master)
    if not candles_1h:
        return []
    vol_spike = get_volume_spike(coin, master)
    events = []

    ob_long = find_ob_v2(candles_1h, "LONG", current_price, coin)
    ob_short = find_ob_v2(candles_1h, "SHORT", current_price, coin)
    if ob_long:
        events.append(ob_long)
    if ob_short:
        events.append(ob_short)

    fvg = find_fvg_v2(candles_1h, current_price, coin)
    if fvg:
        events.append(fvg)

    sweep = find_sweep_v2(candles_1h, current_price, vol_spike, coin)
    if sweep:
        events.append(sweep)

    ob_flow = find_ob_flow_v2(coin, current_price)
    if ob_flow:
        events.append(ob_flow)

    fvg_flow = find_fvg_flow_v2(coin, current_price)
    if fvg_flow:
        events.append(fvg_flow)

    vacuum_area = find_vacuum_v2(coin, current_price)
    if vacuum_area:
        events.append(vacuum_area)

    return events


# ============================================================
# STRUCTURE ENGINE DISPATCHER (V1 / V2)
# ============================================================
# Single gateway — collect_all_events() TETAP jadi satu-satunya entry
# point yang dipanggil pipeline utama. V1 dan V2 TIDAK digabung (event
# V1+V2 tidak pernah masuk Cluster bersamaan) untuk mencegah double
# counting (source_count/cluster strength naik palsu karena dua versi
# algoritma melihat struktur yang sama). Ganti flag ini untuk switch
# engine; gunakan STRUCTURE_COMPARE untuk audit V1 vs V2 lewat log saja,
# tanpa mencampur outputnya ke pipeline.
STRUCTURE_ENGINE = "v1"       # "v1" | "v2"
STRUCTURE_COMPARE = True      # OBSERVATION MODE: log kedua engine (dev/audit only), pipeline tetap pakai STRUCTURE_ENGINE ("v1") — trading TIDAK terpengaruh

def collect_all_events_v1(coin: str, current_price: float, master: Dict) -> List[TradeEvent]:
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

def collect_all_events(coin: str, current_price: float, master: Dict) -> List[TradeEvent]:
    """Single gateway ke Structure Engine. Dispatch berdasarkan
    STRUCTURE_ENGINE flag — TIDAK PERNAH menggabung output V1+V2."""
    if STRUCTURE_ENGINE == "v2":
        return collect_all_events_v2(coin, current_price, master)
    return collect_all_events_v1(coin, current_price, master)


def collect_all_events_with_audit(coin: str, current_price: float, master: Dict) -> Tuple[List[TradeEvent], Optional[Dict]]:
    """Wrapper di atas collect_all_events() yang, kalau STRUCTURE_COMPARE
    aktif, JUGA menjalankan engine yang sedang tidak aktif (read-only, cuma
    untuk observasi) dan mengembalikan ringkasan audit V1-vs-V2 di samping
    event asli yang dipakai pipeline. Event yang dipakai pipeline (elemen
    pertama dari return) SELALU berasal dari collect_all_events() biasa —
    fungsi ini tidak pernah mengubah jalur trading, hanya menambah data
    observasi opsional di elemen kedua.
    """
    audit = None
    if STRUCTURE_COMPARE:
        try:
            v1_events = collect_all_events_v1(coin, current_price, master)
            v2_events = collect_all_events_v2(coin, current_price, master)

            def _fmt(events: List[TradeEvent]) -> str:
                # Pakai .strength (bukan .score) — di titik ini event belum
                # lewat score_event_non_additive() (itu baru jalan di
                # observe_market SETELAH cluster), jadi .score masih
                # default 0.0 untuk event mentah. .strength adalah skor
                # detektor mentah yang sudah diisi tiap find_*().
                return ", ".join(f"{e.type}({e.strength:.0f})" for e in events)

            v1_types = [e.type for e in v1_events]
            v2_types = [e.type for e in v2_events]
            added = sorted(set(v2_types) - set(v1_types))

            logger.info(
                f"STRUCTURE_COMPARE {coin}: V1={len(v1_events)} events [{_fmt(v1_events)}] | "
                f"V2={len(v2_events)} events [{_fmt(v2_events)}] | Added={added or 'none'}"
            )

            audit = {
                "v1_event_types": v1_types,
                "v2_event_types": v2_types,
                "v1_count": len(v1_events),
                "v2_count": len(v2_events),
                "v2_added_events": added,
            }
        except Exception as e:
            logger.debug(f"STRUCTURE_COMPARE error {coin}: {e}")

    events = collect_all_events(coin, current_price, master)
    return events, audit

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

# ============================================================
# REGIME MOMENTUM — Track regime age per coin
# ============================================================
_regime_age_memory: Dict[str, Dict[str, Any]] = {}
_regime_age_lock = threading.RLock()

def get_regime_age(coin: str) -> float:
    """Age (detik) dari regime saat ini untuk coin ini."""
    with _regime_age_lock:
        if coin not in _regime_age_memory:
            return 0.0
        return time.time() - _regime_age_memory[coin]["since"]

def update_regime_age(coin: str, regime: str):
    """Update umur regime — reset kalau regime berubah."""
    with _regime_age_lock:
        if coin not in _regime_age_memory or _regime_age_memory[coin]["regime"] != regime:
            _regime_age_memory[coin] = {"regime": regime, "since": time.time()}


def score_event_non_additive(event: TradeEvent, current_price: float, delta: float,
                             vol_spike: float, oi_roc: float,
                             structure_valid: bool, cvd_accel: bool, momentum: int) -> Tuple[int, List[str]]:
    reasons = []
    evidence_count = 0
    coin = event.extra.get("coin", "BTC")

    # Delta threshold 5 → 4 (slightly relaxed)
    if (event.direction == "LONG" and delta > 4) or (event.direction == "SHORT" and delta < -4):
        evidence_count += 1
        reasons.append("delta")

    # FIX #3: z-score delta sebagai evidence TAMBAHAN, bukan pengganti.
    # delta sudah persentase imbalance orderbook (-60..+60, lihat get_ob_delta),
    # jadi udah sebanding antar-coin secara matematis — TAPI distribusi RIIL-nya
    # beda per-coin (BTC mungkin biasa ±5, coin tipis bisa biasa ±20). Kalau
    # delta sekarang ekstrem dibanding histori coin ITU SENDIRI (bukan
    # dibanding threshold absolut), itu evidence independen. None (histori
    # belum cukup) = skip, JANGAN auto-reject coin baru yang belum punya histori.
    delta_z = get_delta_zscore(coin, delta)
    if delta_z is not None:
        if (event.direction == "LONG" and delta_z > 1.5) or (event.direction == "SHORT" and delta_z < -1.5):
            evidence_count += 1
            reasons.append(f"delta_z{delta_z:+.1f}")

    oi_persist, oi_trend = get_oi_persistence(coin)
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
    dist_pct = abs(mid - current_price) / max(current_price, 0.01) * 100

    # FIX #3: distance dinormalisasi ATR coin ini, bukan persen absolut flat.
    # 0.3% dari harga itu "dekat" buat BTC (ATR rendah) tapi bisa jadi "jauh
    # banget" buat coin micro-cap volatile (ATR tinggi) — dan sebaliknya.
    # atr_ref dipakai sebagai unit jarak: dist_in_atr = berapa kali ATR coin
    # ini jaraknya dari event zone.
    try:
        atr_ref = get_atr_pct(coin, 14, "1h")
    except Exception:
        atr_ref = 1.0
    atr_ref = max(0.1, atr_ref)
    dist_in_atr = dist_pct / atr_ref

    if dist_in_atr < 0.3:
        base += 15
    elif dist_in_atr < 0.6:
        base += 10
    elif dist_in_atr < 1.0:
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

def get_independent_evidence_families(coin: str, direction: str, master: Dict, delta_shift: float = None, delta_stale: bool = None) -> Tuple[bool, bool, bool, List[str]]:
    # ===== P0 FIX: FLOW_INPUT =====
    # delta_shift di sini sebenarnya LEVEL delta (-60..+60), bukan rate-of-
    # change — nama parameter dipertahankan biar gak nabrak call site lain,
    # tapi jangan dibandingin ke get_delta_shift() (itu genuinely beda
    # besaran: rate-of-change dari window). Fallback pas None/stale pakai
    # get_safe_ob_delta(), konsisten sama observe_market().
    delta_stale_here = False
    if delta_shift is None:
        delta_shift, delta_stale_here = get_safe_ob_delta(coin)
    is_stale = delta_stale if delta_stale is not None else delta_stale_here
    cvd_accel = get_cvd_acceleration(coin)
    oi_roc = abs(get_oi_roc(coin))
    funding = get_funding_pct(coin)

    logger.info(
        f"FLOW_INPUT "
        f"coin={coin} "
        f"delta={delta_shift:.1f} "
        f"delta_stale={is_stale} "
        f"cvd_accel={cvd_accel} "
        f"oi_roc={oi_roc:.1f} "
        f"funding={funding:.4f} "
        f"direction={direction}"
    )
    # ===========================
    
    reasons = []
    structure_long, structure_short = get_structure_valid_separate(coin, master)
    momentum = get_composite_momentum(coin, master)

    price_ok = (direction == "LONG" and (structure_long or momentum >= 70)) or (direction == "SHORT" and (structure_short or momentum >= 70))
    if price_ok:
        reasons.append("price")

    # (recalculate flow_ok with delta_shift and cvd_accel already fetched above)
    flow_ok = (direction == "LONG" and (delta_shift > 3 or cvd_accel)) or (direction == "SHORT" and (delta_shift < -3 or cvd_accel))
    if flow_ok:
        reasons.append("flow")

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
    details = []
    if price_roc > 0.2:
        if delta_shift < 0:
            exhaustion += 30
            details.append(f"delta_contra +30")
        if vol_spike < 0.8:
            exhaustion += 20
            details.append(f"vol_low +20 ({vol_spike:.2f})")
        if oi_roc < -2:
            exhaustion += 20
            details.append(f"oi_unwind +20 ({oi_roc:.1f})")
    elif price_roc < -0.2:
        if delta_shift > 0:
            exhaustion += 30
            details.append(f"delta_contra +30")
        if vol_spike < 0.8:
            exhaustion += 20
            details.append(f"vol_low +20 ({vol_spike:.2f})")
        if oi_roc < -2:
            exhaustion += 20
            details.append(f"oi_unwind +20 ({oi_roc:.1f})")

    # ===== P1: EXHAUSTION_DETAIL LOG =====
    logger.info(
        f"EXHAUSTION_DETAIL {coin}: "
        f"price_roc={price_roc:.2f}% "
        f"delta={delta_shift:.1f} "
        f"vol={vol_spike:.2f} "
        f"oi={oi_roc:.1f} "
        f"→ {exhaustion} ({', '.join(details) if details else 'none'})"
    )
    # =====================================

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

def is_cvd_accelerating(coin: str) -> bool:
    """RENAMED dari get_cvd_acceleration (versi bool) — nama lama bentrok
    dengan get_cvd_acceleration(coin, window=5)->float di atas (baris ~7391),
    yang jadi versi AKTIF secara global karena definisi terakhir menang di
    Python. 4 caller lain expect float slope, bukan bool, jadi override ini
    sebelumnya bikin mereka salah baca nilai. Fungsi ini gak dipanggil di
    mana pun sejauh pengecekan, tapi tetap disimpan (di-rename) daripada
    dihapus, kalau-kalau ada pemakaian yang belum kecover grep."""
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

_realized_rr_cache: Dict[str, Any] = {"ts": 0.0, "value": None}
_realized_rr_lock = threading.RLock()

def get_realized_rr_percentile(percentile: float = 0.5, min_samples: int = 15, cache_ttl: float = 120.0) -> Optional[float]:
    """P4.56: ambil percentile dari realized RR (pnl_pct / risk_pct) trade
    yang BENERAN closed dengan profit, dari tabel signals langsung — bukan
    target RR yang dipasang, tapi RR yang sungguh ke-capture. Dipakai
    sebagai basis self-calibrating buat min_rr, jadi threshold ikut naik
    kalau bot ini emang lagi nangkep RR tinggi, dan ikut turun kalau lagi
    susah dapet RR tinggi — bukan angka yang gw atau siapapun tetapkan.
    None kalau sample belum cukup (caller WAJIB fallback, jangan dipaksa).
    """
    cache_key = f"{percentile}_{min_samples}"
    with _realized_rr_lock:
        cached = _realized_rr_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < cache_ttl:
            return cached["value"]

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT pnl, entry_price, sl_price FROM signals
                WHERE evaluated=1 AND outcome IN ('TP_HIT', 'PARTIAL_WIN')
                  AND pnl IS NOT NULL AND entry_price IS NOT NULL AND sl_price IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 100
            """)
            rows = cursor.fetchall()

        realized = []
        for pnl, entry, sl in rows:
            if entry and sl and entry > 0:
                risk_pct = abs(entry - sl) / entry * 100
                if risk_pct > 0 and pnl is not None:
                    realized.append(abs(pnl) / risk_pct)

        if len(realized) < min_samples:
            result = None
        else:
            realized.sort()
            idx = min(len(realized) - 1, int(len(realized) * percentile))
            result = realized[idx]

        with _realized_rr_lock:
            _realized_rr_cache[cache_key] = {"ts": time.time(), "value": result}
        return result
    except Exception as e:
        logger.debug(f"get_realized_rr_percentile failed (non-fatal): {e}")
        return None


def get_dynamic_min_rr(market_regime: str) -> float:
    """P4.56: min_rr self-calibrating dari realized RR distribution bot ini
    sendiri (30th percentile — cukup permisif untuk gak nolak edge yang
    masih valid, tapi tetap di atas trade-trade terburuk). Regime cuma jadi
    PENGALI relatif terhadap baseline data riil, bukan angka tetap. Floor
    absolut tetap ada (RR_FLOOR_ABSOLUTE) untuk cold-start / data kurang —
    bukan cap atas, jadi RR tinggi gak pernah dipotong di layer ini."""
    realized_p30 = get_realized_rr_percentile(percentile=0.30, min_samples=15)
    floor = TUNABLE.get("RR_FLOOR_ABSOLUTE", 1.40)

    if realized_p30 is None:
        # Cold start: belum ada cukup histori closed-win. Pakai floor sebagai
        # base netral — regime multiplier tetap berlaku di bawah supaya
        # behaviour tidak flat selama warmup.
        base = floor
    else:
        base = max(floor, realized_p30)

    # Regime sebagai multiplier relatif (bukan angka absolut baru):
    # market choppy/panic → lebih protective; trending → sedikit longgar.
    regime_mult = {
        "TRENDING_DOWN": 0.9,
        "TRENDING_UP": 0.9,
        "RANGING": 1.0,
        "PANIC": 1.15,
        "CHAOS": 1.3,
    }.get(market_regime, 1.0)

    return max(floor, base * regime_mult)  # floor sebagai lantai, BUKAN plafon

def get_confidence_label(score: int) -> str:
    if score >= 80:
        return "🔥 VERY STRONG"
    if score >= 70:
        return "🟢 STRONG"
    if score >= 60:
        return "🟡 MODERATE"
    return "⚪ WEAK"

def get_nearest_liquidation(coin: str, mark: float, direction: str) -> Optional[float]:
    """P4.56: TIDAK LAGI dipakai sebagai TP target (lihat calculate_sltp_advanced).
    Estimasi liquidation price leveraged trader lain ≠ target profit kita —
    dua konsep berbeda yang sebelumnya tercampur. Dibiarkan ada untuk
    potential future use (misal liquidation-hunt zone sebagai entry event),
    bukan exit target."""
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

def get_next_swing_target(coin: str, direction: str, current_price: float, master: Dict) -> Optional[float]:
    """P4.56: next swing level di ARAH PROFIT (bukan arah SL).
    Untuk LONG, target = swing HIGH berikutnya di atas current_price.
    Untuk SHORT, target = swing LOW berikutnya di bawah current_price.
    Reuse get_nearest_swing dengan direction dibalik, karena fungsi itu
    sendiri sudah directional: 'LONG' cari swing low (buat SL LONG),
    'SHORT' cari swing high (buat SL SHORT) — exact apa yang kita butuh
    sebagai TP arah berlawanan."""
    opposite = "SHORT" if direction == "LONG" else "LONG"
    return get_nearest_swing(coin, opposite, current_price, master)


def get_pattern_rr_weight(coin: str) -> float:
    """P4.56: bobot relatif RR berdasarkan OI pattern coin ini SEKARANG —
    bukan angka tetap, tapi rasio yang mencerminkan karakter pattern itu
    sendiri terhadap baseline (MOMENTUM = 1.0, netral).
    EARLY (baru mulai akumulasi) = ruang gerak masih panjang → bobot >1.
    SPIKE (lonjakan tiba-tiba, rawan reversal cepat) = ruang gerak pendek
    → bobot <1. Pattern lain netral. Dipakai sebagai PENGALI, bukan target
    absolut, jadi tetap mengikuti struktur/statistik di bawahnya."""
    try:
        pattern, _, coverage = get_oi_pattern_v11(coin)
    except Exception:
        return 1.0
    weights = {
        "EARLY": 1.3,
        "MOMENTUM": 1.0,
        "SPIKE": 0.7,
        "LATE": 0.8,
        "NEUTRAL": 1.0,
        "WARMUP": 1.0,
    }
    base = weights.get(pattern, 1.0)
    # Coverage rendah = data pattern belum kuat, tarik mendekati netral
    # (bukan dipaksa 1.0 keras, tapi diredam proporsional ke confidence-nya)
    return 1.0 + (base - 1.0) * max(0.0, min(1.0, coverage))


def get_realized_mfe_rr(coin: str, risk_pct: float, min_samples: int = 3) -> Optional[float]:
    """P4.56: RR target dari distribusi MFE yang BENERAN ke-capture coin ini
    di histori (bukan angka tetap dari gw). MFE median historis coin ini,
    dibagi risk_pct posisi SEKARANG, kasih rasio yang reflect karakter gerak
    riil coin tsb relatif ke SL yang dipasang. None kalau histori belum
    cukup (caller harus fallback ke layer berikutnya, jangan dipaksa)."""
    history = get_recent_outcome_history(coin)
    mfes = [h["mfe"] for h in history if h.get("mfe") is not None and h["mfe"] > 0]
    if len(mfes) < min_samples or risk_pct <= 0:
        return None
    mfes_sorted = sorted(mfes)
    median_mfe = mfes_sorted[len(mfes_sorted) // 2]
    if median_mfe <= 0:
        return None
    return median_mfe / risk_pct


# ============================================================
# L2 LEVERAGE — EXTRACTABILITY (Historical Edge)
# ============================================================

def get_extractability(coin: str, direction: str, limit: int = 30) -> float:
    """
    Extractability = historical MFE / historical MAE untuk setup dengan
    arah yang sama di coin ini.

    Return: 0.5 - 1.5 (1.0 = average, >1.0 = better than average)
    """
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT mfe, mae FROM signals
                WHERE coin = ? AND direction = ? AND evaluated = 1
                  AND mfe IS NOT NULL AND mae IS NOT NULL
                  AND mfe > 0 AND mae < 0
                ORDER BY timestamp DESC
                LIMIT ?
            """, (coin, direction, limit))
            rows = c.fetchall()

        if len(rows) < 5:
            return 1.0  # Insufficient data, neutral

        mfes = [r[0] for r in rows if r[0] > 0]
        maes = [abs(r[1]) for r in rows if r[1] < 0]

        if not mfes or not maes:
            return 1.0

        avg_mfe = sum(mfes) / len(mfes)
        avg_mae = sum(maes) / len(maes)

        if avg_mae == 0:
            return 1.0

        extractability = avg_mfe / avg_mae

        # Clamp ke 0.5 - 1.5
        return max(0.5, min(1.5, extractability))

    except Exception as e:
        logger.debug(f"get_extractability error for {coin}: {e}")
        return 1.0


def get_entry_quality_from_confidence(confidence_data: Dict) -> float:
    """
    Ambil Entry Quality dari confidence_data.
    Fallback ke 50.0 kalau gak ada.
    """
    if not confidence_data:
        return 50.0
    eq = confidence_data.get("entry_quality", 50.0)
    if isinstance(eq, (int, float)):
        return max(0.0, min(100.0, float(eq)))
    return 50.0


def compute_suggested_leverage(coin: str, entry: float, sl: float, position_size_mult: float,
                              conviction: float = 50.0, market_regime: str = "UNKNOWN",
                              confidence_data: Dict = None) -> Dict[str, Any]:
    """High-leverage policy — simulasi smart money dengan leverage agresif,
    TAPI selalu dijepit supaya liquidation distance tetap lebih lebar dari
    SL distance (dengan safety buffer).

    FIX (critical): versi sebelumnya nentuin leverage MURNI dari tier
    exchange cap (native_cap), risk_pct (jarak SL) dihitung tapi gak pernah
    dipakai di kalkulasi akhir. Itu bisa menghasilkan leverage yang bikin
    posisi KE-LIQUIDATE SEBELUM SL SEMPAT TERSENTUH kalau SL-nya lebar
    (misal ETH di kondisi choppy dengan SL 4%, native_cap 40x → suggested
    32x → liquidation di ~3.1%, SL gak pernah kena karena udah liquidated
    duluan). Ini persis cacat matematis yang sama yang ditemukan di sinyal
    bot kompetitor (SL "resmi" lebih jauh dari titik liquidation real di
    leverage yang mereka klaim) — SL jadi cuma angka dekoratif, gak pernah
    benar-benar bisa jadi exit mechanism.

    Tier berdasarkan native_cap exchange (dari Hyperliquid meta):
      BTC/ETH (cap >=40x) → target 80% cap
      Coin mid  (cap 10-39x) → target 75% cap
      Coin kecil (cap <10x)  → target 70% cap

    SAFETY CAP (baru): leverage dijepit lagi supaya liquidation distance
    approx (100/leverage) tetap >= SL distance * LIQ_SAFETY_MULT (default
    1.5x — beri buffer 50% di atas SL, bukan mepet pas-pasan, karena
    estimasi liquidation sederhana ini belum hitung funding/fees/slippage).
    
    ===== P0: CONVICTION/REGIME/VOLATILITY FACTORS =====
    Menambahkan elemen dari conviction, regime, dan volatility (ATR)
    untuk menghasilkan leverage yang lebih agresif pada setup bagus,
    lebih konservatif pada setup rata-rata.
    
    conviction multiplier:
      conviction >= 80 → 1.15 (very confident, bias lever up)
      conviction >= 65 → 1.05 (confident)
      conviction >= 50 → 1.00 (neutral)
      conviction < 50 → 0.85 (low confidence, reduce)
    
    regime multiplier:
      TRENDING_UP/TRENDING_DOWN → 1.10 (directional bias, can lever up)
      RANGING → 1.00 (neutral)
      VOLATILE/PANIC → 0.85 (chaos, reduce leverage)
    
    volatility multiplier (based on ATR pct):
      atr < 0.5% → 1.10 (calm, can lever up)
      atr 0.5-1.0% → 1.00 (neutral)
      atr 1.0-1.5% → 0.90 (elevated volatility)
      atr > 1.5% → 0.75 (high chop, reduce leverage)
    
    position_size_mult dari conviction/regime dipakai sebagai fine-tuning
    ±10% di atas target — bukan penentu utama.
    """
    native_cap = get_max_leverage(coin, default=5)
    risk_pct = abs(entry - sl) / max(entry, 0.01) * 100
    if risk_pct <= 0:
        return {
            "suggested": 1.0,
            "native_cap": native_cap,
            "risk_pct": 0.0,
            "liq_safety_capped": False,
        }

    # ===== 1. ENTRY QUALITY MULTIPLIER (L2) =====
    eq = get_entry_quality_from_confidence(confidence_data)
    # EQ 50 → 1.0, EQ 80 → 1.6, EQ 30 → 0.7
    eq_mult = 1.0 + (eq - 50) * 0.02
    eq_mult = max(0.5, min(1.8, eq_mult))

    # ===== 2. INVALID DISTANCE MULTIPLIER (L2) =====
    # Semakin dekat SL → semakin tinggi leverage (risk lebih kecil)
    invalid_mult = min(2.0, 1.0 + (2.0 - min(4.0, risk_pct)) * 0.3)
    invalid_mult = max(0.8, invalid_mult)

    # ===== 3. EXTRACTABILITY MULTIPLIER (L2) =====
    direction = "LONG" if sl < entry else "SHORT"
    extract_mult = get_extractability(coin, direction)

    # ===== 4. CONVICTION MULTIPLIER =====
    conviction = max(0, min(100, conviction))  # Clamp 0-100
    if conviction >= 80:
        conviction_mult = 1.15
    elif conviction >= 65:
        conviction_mult = 1.05
    elif conviction >= 50:
        conviction_mult = 1.00
    else:
        conviction_mult = 0.85

    # ===== 5. REGIME MULTIPLIER =====
    regime_mult = {
        "TRENDING_UP": 1.20,
        "TRENDING_DOWN": 1.20,
        "TRENDING": 1.20,  # fallback
        "RANGING": 0.85,
        "UNKNOWN": 1.00,
        "VOLATILE": 0.80,
        "PANIC": 0.70,
        "CHAOS": 0.60,
    }.get(market_regime, 1.00)

    # ===== 6. VOLATILITY MULTIPLIER (dari ATR pct) =====
    try:
        atr_pct = get_atr_pct(coin, period=14, timeframe="1h")
    except Exception:
        atr_pct = 1.0  # Default neutral kalau error

    if atr_pct < 0.5:
        vol_mult = 1.10
    elif atr_pct < 1.0:
        vol_mult = 1.00
    elif atr_pct < 1.5:
        vol_mult = 0.90
    else:
        vol_mult = 0.75

    # ===== COMBINED MULTIPLIER =====
    combined_mult = eq_mult * invalid_mult * extract_mult * regime_mult * conviction_mult * vol_mult

    # Tier ratio berdasarkan native_cap (starting point, sebelum safety cap)
    if native_cap >= 40:
        base_ratio = 0.80
    elif native_cap >= 10:
        base_ratio = 0.75
    else:
        base_ratio = 0.70

    size_adj = 0.9 + (min(2.0, max(0.5, position_size_mult)) - 0.5) / 15.0
    tier_suggested = native_cap * base_ratio * size_adj * combined_mult

    # ===== SAFETY CAP: liquidation distance harus >= SL distance * buffer =====
    LIQ_SAFETY_MULT = 1.5  # liquidation distance minimal 1.5x SL distance
    # leverage_max_safe: leverage tertinggi di mana liq_dist (100/lev) masih
    # >= risk_pct * LIQ_SAFETY_MULT
    leverage_max_safe = 100 / (risk_pct * LIQ_SAFETY_MULT)

    raw_suggested = min(tier_suggested, leverage_max_safe)
    suggested = max(1.0, min(native_cap, raw_suggested))

    if should_log("DEVELOPER"):
        logger.debug(
            f"LEV_DEBUG {coin}: "
            f"eq={eq:.0f} eq_mult={eq_mult:.2f} "
            f"risk={risk_pct:.2f}% invalid_mult={invalid_mult:.2f} "
            f"extract={extract_mult:.2f} regime={regime_mult:.2f} "
            f"conv={conviction_mult:.2f} vol={vol_mult:.2f} "
            f"combined={combined_mult:.2f} "
            f"→ lev={suggested:.1f}x (cap={native_cap}x)"
        )

    return {
        "suggested": round(suggested, 1),
        "native_cap": native_cap,
        "risk_pct": round(risk_pct, 2),
        "liq_safety_capped": raw_suggested < tier_suggested,  # info: apakah safety cap yang aktif, bukan tier
        "eq": round(eq, 1),
        "eq_mult": round(eq_mult, 2),
        "invalid_mult": round(invalid_mult, 2),
        "extract_mult": round(extract_mult, 2),
        "regime_mult": round(regime_mult, 2),
        "conv_mult": round(conviction_mult, 2),
        "conviction_mult": round(conviction_mult, 2),
        "vol_mult": round(vol_mult, 2),
        "combined_mult": round(combined_mult, 3),
    }





def compute_tp_rr_boost(coin: str, regime: str, final_score: int) -> float:
    """TP-RR Boost Engine — amplify TP target berdasarkan kualitas sinyal yang
    sudah ada di sistem. Output: rr_multiplier (1.0 = netral, >1.0 = TP lebih jauh).

    Logika: bukan filter baru, bukan threshold baru — murni baca sinyal yang
    sudah dihitung dan amplify TP kalau sinyal-sinyal itu positif sekaligus.

    Sinyal yang dibaca (semua sudah ada di sistem, zero new computation):
      1. OI Pattern (EARLY/MOMENTUM/LATE/SPIKE) — dari get_oi_pattern_v11
      2. Edge memory quality (get_memory_boost) — 3-trade rolling edge ratio
      3. Recent win rate per-coin — dari get_recent_outcome_history
      4. Score tier (sudah dihitung di confidence layer)
      5. Regime (sudah ada di thesis_data)

    Masing-masing sinyal kasih boost kecil (+0.05 sampai +0.20).
    Semua boost dijumlah, di-cap +0.40 max — jadi TP bisa sampai 1.40× lebih
    jauh dari yang dihitung structure/MFE/fallback. Tidak ada sinyal yang
    individually override — butuh konvergensi banyak sinyal positif sekaligus
    untuk dapat boost penuh (mirip conviction system yang sudah ada).
    """
    boost = 0.0

    # ===== SINYAL 1: OI Pattern =====
    # EARLY = akumulasi fresh, ruang gerak panjang → +0.15
    # MOMENTUM = tren berlanjut, tapi sudah jalan → +0.08
    # LATE/SPIKE = depleted, rawan reversal → negatif / netral
    try:
        pattern, strength, coverage = get_oi_pattern_v11(coin)
        oi_boosts = {"EARLY": 0.15, "MOMENTUM": 0.08, "NEUTRAL": 0.03,
                     "WARMUP": 0.0, "LATE": -0.05, "SPIKE": -0.10}
        raw_oi = oi_boosts.get(pattern, 0.0)
        # Scale dengan coverage — kalau data pattern lemah, boost-nya juga dikurangi
        boost += raw_oi * max(0.0, min(1.0, coverage))
    except Exception:
        pass

    # ===== SINYAL 2: Edge memory (3-trade rolling per-coin) =====
    # get_memory_boost() return ±4.0 — normalisasi ke ±0.10
    try:
        mem = get_memory_boost(coin)
        boost += (mem / 4.0) * 0.10
    except Exception:
        pass

    # ===== SINYAL 3: Recent win rate per-coin =====
    # WR > 55% → trade-trade terakhir di coin ini profitable → beri ruang lebih
    try:
        history = get_recent_outcome_history(coin)
        if len(history) >= 5:
            wins = sum(1 for h in history if h.get("pnl", 0) > 0)
            wr = wins / len(history)
            if wr > 0.60:
                boost += 0.12
            elif wr > 0.50:
                boost += 0.06
            elif wr < 0.35:
                boost -= 0.05
    except Exception:
        pass

    # ===== SINYAL 4: Score tier =====
    # Score sudah dihitung di confidence layer — makin tinggi makin layak dapat TP jauh
    if final_score >= 85:
        boost += 0.10
    elif final_score >= 75:
        boost += 0.06
    elif final_score >= 65:
        boost += 0.02

    # ===== SINYAL 5: Regime =====
    # Trending → directional bias kuat, TP lebih jauh valid
    # CHAOS/PANIC → uncertainty tinggi, jangan extend TP
    regime_adj = {
        "TRENDING_UP": 0.08, "TRENDING_DOWN": 0.08,
        "RANGING": 0.0,
        "VOLATILE": -0.05, "PANIC": -0.10, "CHAOS": -0.15,
    }.get(regime, 0.0)
    boost += regime_adj

    # Cap total boost: floor -0.15 (jangan potong TP terlalu agresif),
    # ceiling +0.40 (TP tidak bisa lebih dari 1.40× dari baseline calculation)
    boost = max(-0.15, min(0.40, boost))

    rr_multiplier = 1.0 + boost
    logger.debug(
        f"TP_RR_BOOST {coin}: pattern boost included, score={final_score} "
        f"regime={regime} total_boost={boost:+.3f} → rr_mult={rr_multiplier:.3f}"
    )
    return rr_multiplier


def calculate_sltp_advanced(coin: str, mark: float, direction: str, event: TradeEvent,
                            atr_pct: float, master: Dict,
                            rr_multiplier: float = 1.0) -> Tuple[float, float, float]:
    """P4.56 + TP-RR Boost: TP target structure-first, lalu di-scale oleh
    rr_multiplier dari compute_tp_rr_boost() berdasarkan sinyal kualitas aktif.

    Urutan fallback (tiap layer cuma dipakai kalau layer sebelumnya gak punya data):
      1. Next swing level di arah profit (structural, paling konkret)
      2. Median realized MFE historis coin ini / risk_pct, di-pattern-weight
      3. Self-referential: rasio jarak SL itu sendiri, di-pattern-weight
         (bukan angka 2.0 tetap — basisnya adalah risk yang SUDAH dihitung
         dari struktur SL, jadi tetap nyambung ke kondisi entry sekarang)
    Setelah TP base dihitung dari salah satu layer di atas, rr_multiplier
    diapply sebagai scale TP distance dari entry — bukan geser level struktur,
    tapi extend seberapa jauh kita willing hold di kondisi saat ini.
    """
    if direction == "LONG":
        sl_area = event.price_low * 0.995
        swing_sl = get_nearest_swing(coin, "LONG", mark, master)
        sl_swing = swing_sl * 0.998 if swing_sl else mark * (1 - atr_pct / 100 * 1.2)
        sl = min(sl_area, sl_swing)
    else:
        sl_area = event.price_high * 1.005
        swing_sl = get_nearest_swing(coin, "SHORT", mark, master)
        sl_swing = swing_sl * 1.002 if swing_sl else mark * (1 + atr_pct / 100 * 1.2)
        sl = max(sl_area, sl_swing)

    risk_pct = abs(mark - sl) / max(mark, 0.01) * 100
    pattern_weight = get_pattern_rr_weight(coin)

    # ===== LAYER 1: structure (next swing in profit direction) =====
    swing_target = get_next_swing_target(coin, direction, mark, master)
    tp = None
    if swing_target:
        # buffer kecil biar TP gak nempel pas di level (sering wick-rejected)
        tp = swing_target * 0.998 if direction == "LONG" else swing_target * 1.002
        # validasi: target structural harus tetap di sisi profit & RR masuk akal
        reward_pct_check = abs(tp - mark) / max(mark, 0.01) * 100
        implied_rr_check = reward_pct_check / risk_pct if risk_pct > 0 else 0
        if implied_rr_check < 0.5:  # swing ketemu tapi kelewat dekat, gak worth dipakai
            tp = None

    # ===== LAYER 2: realized MFE distribution (per-coin, dari histori sendiri) =====
    if tp is None:
        mfe_rr = get_realized_mfe_rr(coin, risk_pct)
        if mfe_rr is not None:
            target_rr = mfe_rr * pattern_weight
            tp = mark + (mark - sl) * target_rr if direction == "LONG" else mark - (sl - mark) * target_rr

    # ===== LAYER 3: self-referential fallback (SL distance × pattern weight) =====
    # Dasar rasionya bukan angka ajaib — base_ratio 2.0 di sini cuma starting
    # point netral SEBELUM pattern_weight, supaya EARLY tetap > MOMENTUM tetap
    # > SPIKE secara konsisten walau belum ada histori sama sekali.
    if tp is None:
        base_ratio = 2.0 * pattern_weight
        tp = mark + (mark - sl) * base_ratio if direction == "LONG" else mark - (sl - mark) * base_ratio

    # ===== TP-RR BOOST: scale TP distance dari entry by rr_multiplier =====
    if rr_multiplier != 1.0:
        tp_dist = abs(tp - mark)
        boosted_dist = tp_dist * rr_multiplier
        tp = (mark + boosted_dist) if direction == "LONG" else (mark - boosted_dist)
        logger.debug(
            f"TP_BOOST applied {coin}: tp_dist={tp_dist:.5f} "
            f"→ {boosted_dist:.5f} (×{rr_multiplier:.3f}) tp={tp:.5f}"
        )

    reward = abs(tp - mark) / max(mark, 0.01) * 100
    rr = reward / risk_pct if risk_pct > 0 else 0
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

    # ============================================================
    # P0: BROKER PATIENCE — Observasi vs Fetch Balance
    # ============================================================
    required_obs = get_required_observations(coin)
    
    with _obs_counter_lock:
        _obs_counter[coin] = _obs_counter.get(coin, 0) + 1
        current_obs = _obs_counter[coin]
    
    # Jika belum mencapai required_obs, tetap observasi tapi belum fetch
    if current_obs < required_obs:
        logger.debug(f"🔍 OBSERVING {coin}: {current_obs}/{required_obs} (patience={get_broker_patience(coin):.2f})")
        # Reset counter setelah mencapai threshold? Tidak, biarkan akumulasi.
        return {"status": "OBSERVING", "coin": coin, "progress": f"{current_obs}/{required_obs}"}
    
    # Reset counter setelah mencapai threshold
    with _obs_counter_lock:
        _obs_counter[coin] = 0

    # ===== CONTINUE EXISTING LOGIC =====
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
    # ===== P0: SAFE DELTA (fix beneran, bukan blend level vs rate-of-change) =====
    delta, delta_stale = get_safe_ob_delta(coin)
    if delta_stale:
        logger.debug(f"DELTA_STALE_FALLBACK {coin}: fetch gagal, pakai last_good={delta:.1f}")
    # ===== UPDATE ROLLING DELTA IMMEDIATELY =====
    # Fix: update cache di sini biar OBS dan evidence pakai delta yang sama
    with _rolling_delta_lock:
        if coin not in _rolling_delta:
            _rolling_delta[coin] = deque(maxlen=TUNABLE["ROLLING_DELTA_WINDOW"])
        _rolling_delta[coin].append(delta)
    # =================================================
    cvd_accel = get_cvd_acceleration(coin)
    momentum = get_composite_momentum(coin, master_candles)
    structure_valid_long, structure_valid_short = get_structure_valid_separate(coin, master_candles)
    candles_1h = get_candles(coin, "1h", 60, master_candles)
    market_state = get_market_state_from_structure(candles_1h, mark) if candles_1h else MarketState.UNKNOWN
    # FIX (bug #2): market_regime dulu dari get_all_regimes() — itu cuma BTC
    # 4h EMA9/21, dipaksakan ke SEMUA coin (termasuk coin OI-driven yang
    # gerak independen dari BTC). Sekarang per-coin, dari interpret_regime_v10
    # yang sudah ada (trend strength + stability + transition prob), bukan
    # cuma EMA crossover. regime_int disimpan juga buat downstream yang mau
    # detail lebih (strength/stability/transition), bukan cuma label string.
    regime_int = interpret_regime_v10(coin)
    market_regime = regime_int.regime
    volatility_regime = get_volatility_regime()  # masih market-wide, scope terpisah
    flow_regime = get_flow_regime()               # masih market-wide, scope terpisah

    raw_events, structure_audit = collect_all_events_with_audit(coin, mark, master_candles)
    if not raw_events:
        return {"status": "REJECT", "reason": "no_events", "coin": coin, "mark": mark, "data_confidence": data_confidence, "market_regime": market_regime}

    clustered = cluster_events(raw_events, price_tolerance=0.005)
    oi_roc = get_oi_roc(coin)
    funding_pct = get_funding_pct(coin)
    update_oi_persistence(coin, oi_roc)

    context = get_context_with_confidence(coin, 50.0)

    # ===== CONTEXT ENGINE (Phase B) =====
    # state_vector: NAMA SENGAJA BEDA dari `market_state` (MarketState enum
    # di atas, line ~14230) — dua konsep berbeda, jangan tertukar. Ini
    # continuous 9-dimensi dari MarketStateVector, dipakai contextualize
    # .strength SEBELUM score_event_non_additive (evidence engine, tetap
    # hakim terakhir) baca .strength sebagai base. Kalau
    # CONTEXT_ENGINE_ENABLED=False (default), ini no-op — .strength event
    # tidak berubah, hanya extra["contextualized"]=False ditambahkan untuk
    # observasi.
    state_vector = compute_market_state_vector(coin)
    for ev in raw_events:
        update_detector_distribution(ev.type, ev.strength)
    clustered = contextualize_events_v2(clustered, state_vector)

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
        return {"status": "REJECT", "reason": f"low_score_{best_event.score if best_event else 0}", "coin": coin, "mark": mark, "data_confidence": data_confidence, "best_event": best_event, "market_regime": market_regime}

    # ===== LOG CONTEXTUALIZED SELECTION (observasi) =====
    if CONTEXT_ENGINE_ENABLED and best_event.extra and best_event.extra.get("contextualized") and should_log("DEVELOPER"):
        raw = best_event.extra.get("raw_strength", best_event.strength)
        reasons = best_event.extra.get("context_reasons", [])
        logger.info(
            f"CONTEXT_SELECT {coin}: {best_event.type} "
            f"raw_strength={raw:.0f} -> ctx_strength={best_event.strength:.0f} "
            f"final_score={best_event.score:.0f} ({', '.join(reasons[:3])})"
        )

    # === DETAILED OBSERVATION LOGGING ===
    logger.info(
        f"📊 OBS {coin} | score={best_event.score} data_conf={data_confidence} | "
        f"delta={delta:.1f} oi_roc={oi_roc:.1f} | cluster={best_event.type} strength={best_event.strength}"
    )
    # ===== RECORD FUNNEL: OBS PASS =====
    record_funnel_stage("obs_pass")

    return {
        "status": "PASS",
        "coin": coin, "mark": mark, "best_event": best_event,
        "data_confidence": data_confidence, "ages": ages,
        "atr_pct": atr_pct, "vol_spike": vol_spike, "delta": delta, "delta_stale": delta_stale,
        "cvd_accel": cvd_accel, "momentum": momentum,
        "structure_valid_long": structure_valid_long, "structure_valid_short": structure_valid_short,
        "market_state": market_state, "market_regime": market_regime,
        "state_vector": state_vector,  # MarketStateVector (Phase B Context Engine) — BEDA dari market_state (MarketState enum)
        "regime_int": regime_int,  # FIX #2: detail object (strength/stability/transition_prob), bukan cuma label
        "volatility_regime": volatility_regime, "flow_regime": flow_regime,
        "oi_roc": oi_roc, "funding_pct": funding_pct, "clustered": clustered,
        "master_candles": master_candles, "context": context,
        "pressure_score": compute_pressure_score(coin),  # LAYER 1: dipakai ulang di Layer 2 exploitability
        "structure_audit": structure_audit,  # STRUCTURE_COMPARE: V1 vs V2 snapshot (None kalau compare mode off)
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
        # P4.x ALLOW_WEAK_STRUCTURE: soft-pass dengan score penalty -20
        # Hard reject ganti jadi penalty agar tidak 100% block
        if not TUNABLE.get("ALLOW_WEAK_STRUCTURE", True):
            return {"status": "REJECT", "reason": "structure_invalid_long", "coin": coin}
        else:
            logger.debug(f"WEAK_STRUCTURE {coin}: LONG structure invalid — applying -20 penalty")
            event = replace(event, score=max(0, getattr(event, "score", 0) - 20))
    if event.direction == "SHORT" and not obs["structure_valid_short"]:
        # P4.x ALLOW_WEAK_STRUCTURE: soft-pass dengan score penalty -20
        if not TUNABLE.get("ALLOW_WEAK_STRUCTURE", True):
            return {"status": "REJECT", "reason": "structure_invalid_short", "coin": coin}
        else:
            logger.debug(f"WEAK_STRUCTURE {coin}: SHORT structure invalid — applying -20 penalty")
            event = replace(event, score=max(0, getattr(event, "score", 0) - 20))

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
        "regime_int": obs.get("regime_int"),  # FIX #2: detail object, propagate through
        "pressure_score": obs.get("pressure_score", 0.0),  # LAYER 1: propagate ke thesis_data
        "flow_regime": obs["flow_regime"], "atr_pct": obs["atr_pct"],
        "vol_spike": obs["vol_spike"], "momentum": obs["momentum"],
        "funding_pct": obs["funding_pct"], "oi_roc": obs["oi_roc"],
        "delta": obs.get("delta", 0),  # FIX 3: Add missing delta key for execute_decision
        "cvd_accel": obs.get("cvd_accel", False),
        "clustered": obs["clustered"], "ages": obs["ages"],
        "context": obs.get("context"),
        "bias_4h": bias_4h, "bias_strength": bias_strength, "bias_stability": bias_stability,
        "structure_audit": obs.get("structure_audit"),  # STRUCTURE_COMPARE: propagate V1 vs V2 snapshot
    }
    
# ============================================================
# PART 33 – LAYER 3: COMPUTE CONFIDENCE + HELPER FUNCTIONS
# ============================================================

def compute_exploitability(score: float, momentum: int, event, cvd_accel: bool,
                            structure_valid: bool, pressure_score: float) -> float:
    """LAYER 2 — Exploitability Scoring.

    exploitability = confidence × velocity × capture_prob

    Tujuan: re-ranking, BUKAN filter baru. Setup score 62 yang lebih
    eksploitatif (momentum kuat, zona masih segar, flow konfirmasi) bisa
    menang lawan score 78 yang udah basi/zona padat. Dipakai untuk
    breaking ties di pemilihan event terbaik (best_event), bukan untuk
    reject sinyal — evidence_families dan min_rr filter yang sudah ada
    TETAP jadi gerbang utama, exploitability cuma menentukan PRIORITAS
    di antara kandidat yang sudah lolos.

    Komponen (semua dari data yang sudah ada, tidak ada fungsi/API baru):
    - velocity: momentum (0-100) dinormalisasi ke 0.5-1.5 (1+momentum_norm)
    - fill_quality: 1 - event.fill_ratio (zona FVG/OB makin segar/belum
      terisi = makin eksploitatif, zona padat = sudah banyak dipakai orang)
    - flow_quality: rata-rata dari cvd_accel, structure_valid, dan
      pressure_score (semua bukti arah order flow yang konvergen)
    """
    try:
        velocity = 1.0 + (max(0, min(100, momentum)) / 100.0 - 0.5)  # 0.5..1.5

        fill_ratio = getattr(event, "fill_ratio", 0.0) if event else 0.0
        fill_quality = 1.0 - max(0.0, min(1.0, fill_ratio))  # 0..1, fresh zone = 1

        flow_signals = [
            1.0 if cvd_accel else 0.5,
            1.0 if structure_valid else 0.5,
            0.5 + max(-0.5, min(0.5, pressure_score)),  # pressure_score sudah -1..1 range kasar
        ]
        flow_quality = sum(flow_signals) / len(flow_signals)

        exploit = score * velocity * (0.5 + 0.5 * fill_quality) * flow_quality
        return round(exploit, 2)
    except Exception as e:
        logger.debug(f"compute_exploitability error: {e}")
        return score  # fallback: gak ngerusak ranking kalau gagal


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
        coin, event.direction, thesis_data["master_candles"],
        delta_shift=thesis_data.get("delta"),         # ← INJEKSI dari thesis (sudah safe delta)
        delta_stale=thesis_data.get("delta_stale")    # ← P0: flag stale dari observe_market
    )
    evidence_families = (1 if price_ok else 0) + (1 if flow_ok else 0) + (1 if pos_ok else 0)
    
    # ===== P0: EVIDENCE_TRACE =====
    logger.info(
        "EVIDENCE_TRACE "
        f"coin={coin} "
        f"delta={thesis_data.get('delta', 0):.1f} "
        f"delta_stale={thesis_data.get('delta_stale', False)} "
        f"price={price_ok} "
        f"flow={flow_ok} "
        f"positioning={pos_ok} "
        f"families={evidence_families}"
    )
    # ==============================

    # ===== P0: EVIDENCE_LOG =====
    logger.info(
        f"EVIDENCE_LOG "
        f"coin={coin} "
        f"price={price_ok} "
        f"flow={flow_ok} "
        f"positioning={pos_ok} "
        f"families={evidence_families} "
        f"delta={thesis_data.get('delta', 0):.1f} "
        f"oi={thesis_data.get('oi_roc', 0):.1f} "
        f"funding={thesis_data.get('funding_pct', 0):.4f}"
    )
    # ============================
    
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

    # ===== P4.6: CONFIDENCE CALIBRATION (fix overconfidence) =====
    confidence = calibrate_confidence_v2(
        coin=coin,
        raw_confidence=confidence,
        evidence_families=evidence_families,
        entropy_market=entropy_market,
    )
    # ===== END P4.6 =====

    # ===== TP-RR BOOST: baca sinyal kualitas aktif → scale TP lebih jauh =====
    _tp_rr_mult = compute_tp_rr_boost(
        coin=coin,
        regime=thesis_data.get("market_regime", "UNKNOWN"),
        final_score=final_score,
    )
    sl, tp, rr = calculate_sltp_advanced(coin, thesis_data["mark"], event.direction, event,
                                         thesis_data["atr_pct"], thesis_data["master_candles"],
                                         rr_multiplier=_tp_rr_mult)
    base_rr = get_dynamic_min_rr(thesis_data["market_regime"])
    min_rr = get_entropy_adjusted_min_rr(base_rr, entropy_market)
    entropy_mult = min_rr / base_rr if base_rr > 0 else 1.0
    trace(f"[RR {coin}] rr={rr:.2f} base_rr={base_rr:.2f} entropy={entropy_market} entropy_mult={entropy_mult:.2f} final_min_rr={min_rr:.2f} regime={thesis_data['market_regime']}")
    if rr < min_rr:
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        # NOTE: position_size_mult is computed later in this function
        # (after this check) — passing size=None here, not the undefined
        # variable, to avoid a NameError crash on every low-RR reject.
        emit_velocity_skip(
            coin=coin,
            reason=f"low_rr_{rr:.2f}_min_{min_rr:.2f}",
            stage="CONF",
            score=final_score,
            threshold=None,
            regime=thesis_data.get("market_regime"),
            cache_age=None,
            source=None,
            size=None,
        )
        logger.debug(
            f"❌ CONF FAIL [{coin}] low_rr | rr={rr:.2f} min_rr={min_rr:.2f} | "
            f"regime={thesis_data['market_regime']} entropy={entropy_market}"
        )
        return {"status": "REJECT", "reason": f"low_rr_{rr:.2f}_min_{min_rr:.2f}", "coin": coin, "rr": rr}

    # ===== LAYER 2: EXPLOITABILITY (re-ranking signal, bukan filter baru) =====
    exploitability = compute_exploitability(
        score=final_score,
        momentum=thesis_data["momentum"],
        event=event,
        cvd_accel=thesis_data.get("cvd_accel", False),
        structure_valid=thesis_data.get("structure_valid_long", False) or thesis_data.get("structure_valid_short", False),
        pressure_score=thesis_data.get("pressure_score", 0.0),
    )

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
    # ===== RECORD FUNNEL: CONF PASS =====
    record_funnel_stage("conf_pass")

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
        "thesis_data": thesis_data,
        # FIX: expose tp_rr_mult — _tp_rr_mult adalah local var di fungsi ini,
        # tidak otomatis terlihat di check_entry_alert_v10/_phase1 (NameError).
        # Caller harus baca dari confidence_data["tp_rr_mult"], bukan nama
        # variabel lokal.
        "tp_rr_mult": _tp_rr_mult,
        "exploitability": exploitability,  # LAYER 2: re-ranking signal
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
    
    # ===== P1: EV_MULT_DEBUG =====
    logger.info(
        f"EV_MULT_DEBUG "
        f"families={evidence_families} "
        f"regime={market_regime} "
        f"mult={ev_mult:.2f}"
    )
    # =============================
    
    raw_score = score_long if event.direction == "LONG" else score_short
    contradiction = (score_long > 55 and score_short > 55)
    # ===== P1: ADAPTIVE CONTRA PENALTY =====
    contra_penalty = 0
    if contradiction:
        overlap = min(score_long, score_short)
        contra_penalty = min(12, max(0, (overlap - 55) * 0.5))
        contra_penalty = int(contra_penalty)
        logger.info(
            f"CONTRA_ADAPTIVE "
            f"coin={coin} "
            f"long={score_long} "
            f"short={score_short} "
            f"overlap={overlap} "
            f"penalty={contra_penalty}"
        )
    # ==========================================
    exhaustion_penalty = min(25, exhaustion)          # FIX: min(50,x)→min(25,x)
    quality_penalty = max(0, (100 - data_confidence) * 0.1)  # FIX: 0.2→0.1
    tmp_score = raw_score * ev_mult - contra_penalty - exhaustion_penalty - quality_penalty
    tmp_score = max(0, min(100, int(tmp_score)))

    contributions = {
        "evidence": int(raw_score * (ev_mult - 1)),
        "contra": -contra_penalty if contradiction else 0,
        "exhaust": -exhaustion_penalty,
        "data": -int(quality_penalty)
    }
    
    # ===== P2: SCORE BREAKDOWN LOG =====
    logger.info(
        f"SCORE_BREAKDOWN "
        f"coin={coin} "
        f"event={event.type} "
        f"dir={event.direction} "
        f"raw={raw_score:.1f} "
        f"ev_mult={ev_mult:.2f} "
        f"contra={contra_penalty:.1f} "
        f"exhaust={exhaustion_penalty:.1f} "
        f"quality={quality_penalty:.1f} "
        f"final={tmp_score:.1f}"
    )
    
    # ===== P0: SCORE_AUDIT =====
    logger.info(
        "SCORE_AUDIT "
        f"coin={coin} "
        f"raw={raw_score:.1f} "
        f"families={evidence_families} "
        f"ev_mult={ev_mult:.2f} "
        f"contra={contra_penalty:.1f} "
        f"exhaust={exhaustion_penalty:.1f} "
        f"quality={quality_penalty:.1f} "
        f"final={tmp_score:.1f}"
    )
    # ===========================
    
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
    adjusted_threshold = max(
        52,
        min(72, threshold + threshold_adj)
    )  # Keep in bounds (produksi: P50 63-68, P90 72, MAX 72)
    
    return adjusted_size, adjusted_threshold

# ============================================================
# PART 34 – LAYER 4: EXECUTE DECISION (V10)
# ============================================================
# ============================================================
# PATCH v10.3.3 — EXEC COUNTER (SINGLE SOURCE)
# ============================================================

_EXEC_COUNTER = {"value": 0}
_EXEC_COUNTER_LOCK = threading.RLock()

def record_execute():
    """Single source of truth untuk execution counter."""
    with _EXEC_COUNTER_LOCK:
        _EXEC_COUNTER["value"] += 1
        
        # Sinkronisasi ke kedua sistem
        global _opportunity_stats
        with _opportunity_lock:
            _opportunity_stats["executed"] = _EXEC_COUNTER["value"]
        with _exec_pipeline_lock:
            _exec_pipeline["execute_pass"] = _EXEC_COUNTER["value"]
            _exec_pipeline["exec"] = _EXEC_COUNTER["value"]
        
        logger.info(f"📈 EXEC_COUNTER total={_EXEC_COUNTER['value']}")
        return _EXEC_COUNTER["value"]

def get_exec_count() -> int:
    with _EXEC_COUNTER_LOCK:
        return _EXEC_COUNTER["value"]

# ============================================================
# P0 HELPERS: COOLDOWN + POSITION CHECK
# ============================================================

def is_entry_cooldown_active(coin: str, direction: str) -> bool:
    """Cek apakah coin+direction masih dalam masa cooldown (HANYA BACA)."""
    key = f"{coin}_{direction}"
    with _entry_cooldown_lock:
        last_ts = _entry_cooldown.get(key, 0.0)
        return (time.time() - last_ts) < _ENTRY_COOLDOWN_SECONDS

def mark_entry_cooldown(coin: str, direction: str):
    """Tandai bahwa entry untuk coin+direction ini BERHASIL (hanya dipanggil setelah sukses)."""
    key = f"{coin}_{direction}"
    with _entry_cooldown_lock:
        _entry_cooldown[key] = time.time()
        logger.info(f"⏳ COOLDOWN MARKED: {key} (active for {_ENTRY_COOLDOWN_SECONDS}s)")

def is_position_open(coin: str, direction: str) -> bool:
    """Cek apakah posisi untuk coin+direction ini sudah OPEN di TradeManager."""
    with TRADE_MANAGER._lock:
        for pos in TRADE_MANAGER.positions.values():
            if pos.coin == coin and pos.direction == direction and pos.status == "OPEN":
                return True
    return False

# ========== P1: CAPITAL PRESSURE HELPERS ==========
def _compute_recent_wr_multiplier(coin: str, window: int = 20) -> float:
    """
    Recent win rate multiplier for capital pressure.
    
    WR > 60% → dampen size (too hot, reduce risk)
    WR 40-60% → neutral (1.0)
    WR < 40% → amplify size slightly (cold streak, build back)
    
    Returns: 0.7 to 1.15 multiplier
    """
    try:
        with _journal_lock:
            entries = list(_decision_journal)[-window:] if len(_decision_journal) >= window else list(_decision_journal)
            coin_entries = [e for e in entries if e.coin == coin]
            closed = [e for e in coin_entries if getattr(e, "executed", False) and getattr(e, "outcome", None) is not None]
        
        if len(closed) < 3:
            return 1.0  # Insufficient data, neutral
        
        wins = sum(1 for e in closed if e.outcome in ("TP_HIT", "PARTIAL_WIN"))
        wr = wins / len(closed)
        
        if wr >= 0.65:
            # Hot streak — cool down (70% size)
            return 0.70
        elif wr >= 0.55:
            # Strong but not extreme — slight cool (85% size)
            return 0.85
        elif wr >= 0.40:
            # Healthy zone — neutral
            return 1.0
        elif wr >= 0.25:
            # Cold streak — encourage re-entry (105% size)
            return 1.05
        else:
            # Very cold — rebuild gradually (115% size)
            return 1.15
    except Exception as e:
        logger.debug(f"_compute_recent_wr_multiplier error for {coin}: {e}")
        return 1.0


def _compute_volatility_multiplier(coin: str, atr_pct: float) -> float:
    """
    Volatility risk multiplier for capital pressure.
    
    Idea: High volatility = hard to hit SL without false breakouts
    → Reduce size. Low volatility = easier precise entry → allow larger size.
    
    atr_pct < 0.5%  → amplify (1.10, low chop)
    atr_pct 0.5-1.0% → neutral (1.0)
    atr_pct 1.0-1.5% → reduce (0.90, moderate)
    atr_pct > 1.5%  → reduce more (0.75, high chop)
    
    Returns: 0.75 to 1.10 multiplier
    """
    try:
        if atr_pct < 0.5:
            return 1.10  # Very calm, can size up
        elif atr_pct < 1.0:
            return 1.0  # Neutral
        elif atr_pct < 1.5:
            return 0.90  # Moderate volatility
        else:
            return 0.75  # High chop, reduce risk
    except Exception:
        return 1.0
# ========== END P1 CAPITAL PRESSURE HELPERS ==========


def execute_decision(coin: str, thesis_data: Dict, confidence_data: Dict,
                      event: TradeEvent, intent, intent_legacy,
                      context: ContextSnapshot, breath: Dict[str, float],
                      cache_age: float = 999.0, data_source: str = "UNKNOWN") -> Optional[dict]:
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
    MAX_TOTAL_OPEN = min(
        TUNABLE.get("MAX_OPEN_CAP", 60),
        max(20, int(check_signal_db_health().get(
            "tracked_open_in_manager", 0
        ) * 1.5))
    )
    MAX_COIN_OPEN = 15

    # 1. Total open positions guard (hard cap, beda dari get_exposure_adjusted_threshold
    #    yang soft-scale threshold tapi gak pernah dipanggil di codebase)
    with TRADE_MANAGER._lock:
        total_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
    if total_open >= MAX_TOTAL_OPEN:
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        # NOTE: position_size_mult isn't initialized until after the
        # inventory gates — size=None here, not the undefined variable.
        emit_velocity_skip(
            coin=coin,
            reason=f"inventory_limit_{total_open}/{MAX_TOTAL_OPEN}",
            stage="GATE",
            score=confidence_data.get("final_score", confidence_data.get("confidence")),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=None,
        )
        logger.warning(f"🎒 INVENTORY LIMIT: {total_open}/{MAX_TOTAL_OPEN} open, blocking {coin}")
        update_fatigue_memory(event.type)
        queue_entry_intent({
            "coin": coin,
            "direction": getattr(event, "direction", "?"),
            "score": confidence_data.get("final_score", confidence_data.get("confidence", 0)),
            "threshold": confidence_data.get("final_threshold", 0),
            "blocked": True,
            "block_reason": "inventory_limit"
        })
        return None

    # 2. Per-coin open positions guard
    with TRADE_MANAGER._lock:
        coin_open = sum(1 for p in TRADE_MANAGER.positions.values()
                         if p.coin == coin and p.status == "OPEN")
    if coin_open >= MAX_COIN_OPEN:
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason=f"coin_limit_{coin_open}/{MAX_COIN_OPEN}",
            stage="GATE",
            score=confidence_data.get("final_score", confidence_data.get("confidence")),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=None,
        )
        logger.warning(f"🔴 COIN LIMIT: {coin}={coin_open}/{MAX_COIN_OPEN}, blocking")
        update_fatigue_memory(event.type)
        queue_entry_intent({
            "coin": coin,
            "direction": getattr(event, "direction", "?"),
            "score": confidence_data.get("final_score", confidence_data.get("confidence", 0)),
            "threshold": confidence_data.get("final_threshold", 0),
            "blocked": True,
            "block_reason": "coin_limit"
        })
        return None
    # ===== END INVENTORY CONTROL GATES =====

    # ===== GUARD: INIT position_size_mult SEBELUM EXPOSURE_DIVERSIFIER =====
    position_size_mult = confidence_data.get("position_size_mult", 1.0)
    if not isinstance(position_size_mult, (int, float)) or position_size_mult <= 0:
        position_size_mult = 1.0
        logger.warning(f"⚠️ position_size_mult invalid dari confidence_data, reset 1.0 for {coin}")

    # ===== EXPOSURE DIVERSIFIER (PATCHED v10.3.3) =====
    try:
        with TRADE_MANAGER._lock:
            positions = list(TRADE_MANAGER.positions.values())
            total_positions = len([p for p in positions if p.status == "OPEN"])
            if total_positions > 0:
                btc_positions = [p for p in positions if p.coin == "BTC" and p.status == "OPEN"]
                btc_exposure = len(btc_positions) / total_positions * 100
            
                if btc_exposure > 70 and coin != "BTC":
                    old_mult = position_size_mult
                    position_size_mult *= 0.6
                    logger.info(f"EXPOSURE_DIVERSIFIER {coin}: BTC exposure {btc_exposure:.0f}% > 70% → size {old_mult:.2f}→{position_size_mult:.2f}")
                elif btc_exposure > 50 and coin != "BTC":
                    old_mult = position_size_mult
                    position_size_mult *= 0.8
                    logger.info(f"EXPOSURE_DIVERSIFIER {coin}: BTC exposure {btc_exposure:.0f}% > 50% → size {old_mult:.2f}→{position_size_mult:.2f}")
    except Exception as e:
        logger.warning(f"DIVERSIFIER_SKIP: {e}")

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
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason="strong_contradiction",
            stage="GATE",
            score=confidence_data.get("final_score"),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=position_size_mult,
        )
        logger.debug(f"{coin}: strong contradiction detected (long vs short both >55), skipping")
        update_fatigue_memory(event.type)
        return None
    
    if clarity["decision_quality"] < 0.40:  # Only skip on extreme chaos
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason=f"chaos_{clarity['decision_quality']:.2f}",
            stage="GATE",
            score=confidence_data.get("final_score"),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=position_size_mult,
        )
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

    # position_size_mult sudah diinit sebelum EXPOSURE_DIVERSIFIER di atas
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

    # ===== REGIME MOMENTUM: regime baru → lebih agresif, regime tua → lebih konservatif =====
    _regime_age = get_regime_age(coin)
    _current_regime_v = thesis_data.get("market_regime", "UNKNOWN")
    update_regime_age(coin, _current_regime_v)
    if _regime_age < 600:  # <10 menit — regime baru lahir
        final_threshold = max(50, final_threshold - 2)
        logger.debug(f"🌱 REGIME_YOUNG {coin}: age={_regime_age/60:.1f}m → threshold -2")
    elif _regime_age > 7200:  # >2 jam — regime udah tua
        final_threshold = min(85, final_threshold + 2)
        logger.debug(f"🌳 REGIME_OLD {coin}: age={_regime_age/3600:.1f}h → threshold +2")

    # ===== P4.17: EXECUTION PERSONALITY THRESHOLD ADJUSTMENT =====
    try:
        _personality, _personality_adj = get_execution_personality()
        if _personality_adj != 0:
            final_threshold = max(50, min(95, final_threshold + _personality_adj))
            logger.debug(f"P4.17 PERSONALITY {coin}: mode={_personality} adj={_personality_adj:+d} → threshold={final_threshold}")
    except Exception as _pe:
        logger.debug(f"P4.17 personality error: {_pe}")
    # ===== END P4.17 =====

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
                logger.warning(f"🛑 ZERO WR {coin}: {len(coin_closed)} trades terakhir semuanya loss, threshold dinaikkan + size diperkecil")
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

    # ===== P4.W14: BUCKET-SPECIFIC THRESHOLD OPENING =====
    # Only open for buckets proven positive (31-50)
    _bucket_threshold_adj = {
        "0_30": 0,      # red → don't open
        "31_50": -3,    # green → open a bit
        "51_70": 0,     # red → don't open
        "71_85": 0,     # neutral
        "86_100": 0,    # neutral
    }
    calibration_bucket = confidence_data.get("calibration_bucket")
    if calibration_bucket and calibration_bucket in _bucket_threshold_adj:
        bucket_adj = _bucket_threshold_adj[calibration_bucket]
        if bucket_adj != 0:
            effective_threshold = final_threshold + bucket_adj
            logger.info(f"🔓 BUCKET OPEN {coin}: {calibration_bucket} adj={bucket_adj:+d} → threshold {final_threshold}→{effective_threshold}")
            final_threshold = effective_threshold
    # ===== END P4.W14 =====

    # ===== P0.5: EXEC_GATE INSTRUMENTASI =====
    score = confidence_data.get('final_score', 0)
    gap = score - final_threshold
    record_threshold(final_threshold)

    # === NEW: FINAL REASON + GAP ===
    decision = "EXECUTE" if score >= final_threshold else "REJECT"
    reason_gap = f"score_{score:.0f}_lt_{final_threshold}" if decision == "REJECT" else "score_ok"

    logger.info(
        f"EXEC_GATE "
        f"coin={coin} "
        f"score={score:.1f} "
        f"threshold={final_threshold:.1f} "
        f"gap={gap:+.1f} "
        f"decision={decision} "
        f"reason={reason_gap}"
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

    # ===== P1: CAPITAL PRESSURE (recent_wr + volatility factors) =====
    # Multiply position size by recent_wr_mult and volatility_mult
    # These amplify or dampen sizing based on edge quality and risk environment
    _recent_wr_mult = _compute_recent_wr_multiplier(coin)
    _volatility_mult = _compute_volatility_multiplier(coin, thesis_data.get("atr_pct", 0.5))
    
    _old_size_cp = position_size_mult
    position_size_mult *= _recent_wr_mult * _volatility_mult
    logger.info(
        f"💰 CAPITAL_PRESSURE {coin}: wr_mult={_recent_wr_mult:.3f} "
        f"vol_mult={_volatility_mult:.3f} → "
        f"size {_old_size_cp:.3f}→{position_size_mult:.3f}"
    )
    # ===== END P1 CAPITAL PRESSURE =====

    # ===== P4.33: COIN COOLDOWN PENALTY (soft size reduction post-loss) =====
    _cooldown_penalty = get_coin_cooldown_penalty(coin)
    if _cooldown_penalty < 1.0:
        _old_size = position_size_mult
        position_size_mult *= _cooldown_penalty
        logger.info(f"❄️ COOLDOWN_PENALTY {coin}: size {_old_size:.2f}→{position_size_mult:.2f} (penalty={_cooldown_penalty:.2f})")
    # ===== END P4.33 =====

    # ===== BTC CONCENTRATION CAP =====
    try:
        with TRADE_MANAGER._lock:
            positions = list(TRADE_MANAGER.positions.values())
            total_open = sum(1 for p in positions if p.status == "OPEN")
            btc_open = sum(1 for p in positions if p.coin == "BTC" and p.status == "OPEN")
            
            if total_open > 0:
                btc_share = btc_open / total_open
                logger.info(f"BTC_SHARE: {btc_share:.0%} ({btc_open}/{total_open})")
                
                if btc_share > 0.60 and coin != "BTC":
                    # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
                    emit_velocity_skip(
                        coin=coin,
                        reason=f"btc_concentration_{btc_share:.0%}",
                        stage="GATE",
                        score=confidence_data.get("final_score"),
                        threshold=confidence_data.get("final_threshold"),
                        regime=thesis_data.get("market_regime"),
                        cache_age=cache_age,
                        source=data_source,
                        size=position_size_mult,
                    )
                    logger.warning(f"BTC_CONCENTRATION {btc_share:.0%} > 60% → SKIP {coin}")
                    update_fatigue_memory(event.type)
                    return None
                elif btc_share > 0.40 and coin != "BTC":
                    old_mult = position_size_mult
                    position_size_mult *= 0.5
                    logger.info(f"BTC_CONCENTRATION {btc_share:.0%} > 40% → size {old_mult:.2f}→{position_size_mult:.2f}")
                elif btc_share > 0.25 and coin != "BTC":
                    old_mult = position_size_mult
                    position_size_mult *= 0.7
                    logger.info(f"BTC_CONCENTRATION {btc_share:.0%} > 25% → size {old_mult:.2f}→{position_size_mult:.2f}")
    except Exception as e:
        logger.warning(f"BTC_CONCENTRATION_CHECK failed: {e}")
    
    # ===== CHECK CONVICTION QUALIFICATION (NEW - V10) =====
    if not conviction_data["is_qualified"]:
        reason = f"conviction_{conviction_data['conviction']:.0f}_lt_45"
        record_gate_seen("conviction")
        record_reject("exec", reason, score=confidence_data.get("final_score"))
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason=reason,
            stage="GATE",
            score=confidence_data.get("final_score"),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=position_size_mult,
        )
        record_opportunity_rejected(coin, "conviction_gate")
        inc_pipeline_counter("reject_conviction")
        logger.debug(f"❌ CONVICTION REJECT {coin}: {reason}")
        update_fatigue_memory(event.type)
        return None

    record_gate_pass("conviction")

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

    # ===== L1 ENTRY WINDOW: HITUNG ENTRY ZONE =====
    # FIX (alert honesty): snapshot detect price + creation time SEBELUM mark
    # ditimpa oleh optimal_entry. Tanpa ini, alert OPEN gak bisa bedain
    # "harga saat sinyal terdeteksi" vs "harga optimal-entry hasil zone calc",
    # jadi kalau market udah geser pas alert dikirim, user salah paham "telat".
    detect_price = mark
    signal_created_ts = time.time()
    entry_zone = None
    try:
        entry_zone = calculate_entry_zone(
            coin=coin,
            direction=event.direction,
            base_price=mark,
            event=event,
            master=thesis_data.get("master_candles", {}),
            atr_pct=thesis_data.get("atr_pct", 1.0)
        )
        
        if entry_zone:
            optimal_entry = entry_zone["optimal_entry"]
        
            if event.direction == "LONG":
                sl = entry_zone["zone_low"] * 0.998
            else:
                sl = entry_zone["zone_high"] * 1.002
        
            confidence_data["entry_quality"] = entry_zone["entry_quality"]
            confidence_data["entry_zone"] = entry_zone
            mark = optimal_entry
        
            logger.info(
                f"📐 ENTRY_ZONE {coin}: "
                f"optimal={optimal_entry:.4f} "
                f"zone={entry_zone['zone_low']:.4f}-{entry_zone['zone_high']:.4f} "
                f"EQ={entry_zone['entry_quality']:.1f} "
                f"SL_dist={entry_zone['sl_distance_pct']:.2f}%"
            )
    except Exception as e:
        logger.warning(f"Entry zone calculation failed: {e}, using original entry")
        confidence_data["entry_quality"] = 50.0
        entry_zone = None
                          
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
    record_gate_seen("micro")
    
    # If micro-structure NOT confirmed, reject early (precision filter)
    if not micro_confirmed:
        # P4.x MICRO_SOFT_PASS: score 30-59 → warn + continue (not block)
        soft_threshold = TUNABLE.get("MICRO_SOFT_THRESHOLD", 30)
        if TUNABLE.get("MICRO_SOFT_PASS", True) and micro_score >= soft_threshold:
            logger.info(
                f"🔬 MICRO SOFT-PASS {coin} {event.direction}: "
                f"score={micro_score} (below 60 but >= {soft_threshold}) | {micro_reasons}"
            )
            record_reject("exec", f"micro_soft_pass_{micro_score}", score=confidence_data.get("final_score"))
            # Penalize final_score slightly for weak micro
            confidence_data["final_score"] = max(0, confidence_data.get("final_score", 0) - 5)
            # Fall through — do NOT return None
        else:
            record_reject("exec", f"micro_structure_gate_{micro_score}", score=confidence_data.get("final_score"))
            # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
            emit_velocity_skip(
                coin=coin,
                reason=f"micro_structure_gate_{micro_score}/60",
                stage="GATE",
                score=confidence_data.get("final_score"),
                threshold=confidence_data.get("final_threshold"),
                regime=thesis_data.get("market_regime"),
                cache_age=cache_age,
                source=data_source,
                size=position_size_mult,
            )
            logger.debug(f"🔬 MICRO REJECT {coin} {event.direction}: score={micro_score} < {soft_threshold} | {micro_reasons}")
            record_opportunity_rejected(coin, "micro_structure_gate")
            inc_pipeline_counter("reject_micro_structure")
            update_fatigue_memory(event.type)
            return None
    
    # Micro confirmed: boost confidence slightly (quality signal)
    record_gate_pass("micro")
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

    # ===== P0: ADAPTIVE HARD CAP =====
    # Bukan hard cap statis, tapi adaptif terhadap kondisi pasar (shock/event/regime)
    _hardcap_base = 72
    if context.shock_score > 70 or event_adjust.get('importance', 0) > 70:
        _hardcap = 80  # Stress/event = boleh threshold lebih tinggi
    elif thesis_data.get("market_regime") in ("TRENDING_UP", "TRENDING_DOWN"):
        _hardcap = 75  # Trending = sedikit lebih tinggi
    elif thesis_data.get("market_regime") == "RANGING":
        _hardcap = 68  # Ranging = lebih selektif/rendah
    else:
        _hardcap = _hardcap_base

    if final_threshold > _hardcap:
        logger.info(
            f"THRESH_CAP {coin}: raw={final_threshold} cap={_hardcap} "
            f"regime={thesis_data.get('market_regime')} shock={context.shock_score:.0f} "
            f"event={event_adjust.get('importance', 0)}"
        )
        final_threshold = _hardcap

    # ===== THRESHOLD_EFFECTIVE: LOG AKHIR SEBELUM GATE =====
    logger.info(
        f"THRESHOLD_EFFECTIVE coin={coin} "
        f"value={final_threshold} "
        f"regime={thesis_data.get('market_regime', 'UNKNOWN')} "
        f"entropy={confidence_data.get('entropy_market', 50)} "
        f"shock={context.shock_score:.0f}"
    )
    # ===== P2: THRESH_AUDIT =====
    logger.info(
        f"THRESH_AUDIT "
        f"coin={coin} "
        f"gate_threshold={confidence_data.get('final_threshold', final_threshold)} "
        f"exec_threshold={final_threshold} "
        f"diff={confidence_data.get('final_threshold', final_threshold) - final_threshold}"
    )
    # ============================

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
    
    # ===== P4: THRESH_TRACE_EXEC =====
    logger.info(
        f"THRESH_TRACE_EXEC "
        f"coin={coin} "
        f"final_threshold={final_threshold} "
        f"regime={thesis_data.get('market_regime')} "
        f"shock={context.shock_score:.0f}"
    )
    # =================================
    
    # ============================================================
    # P4.3 — THESIS CALIBRATION (inject sebelum threshold check)
    # ============================================================
    try:
        _raw_score = confidence_data.get("final_score", 0)
        _cal = get_calibration_engine().calibrate(_raw_score)
        confidence_data["raw_score"]              = _raw_score
        confidence_data["effective_score"]        = _cal.effective_score
        confidence_data["score_adjustment"]       = _cal.adjustment
        confidence_data["calibration_bucket"]     = _cal.bucket
        confidence_data["calibration_wr"]         = _cal.wr
        confidence_data["calibration_edge"]       = _cal.edge
        confidence_data["calibration_confidence"] = _cal.confidence
        # Override final_score dengan calibrated value
        confidence_data["final_score"] = _cal.effective_score
        if abs(_cal.adjustment) > 2:
            logger.info(
                f"CALIBRATION {coin}: "
                f"raw={_raw_score:.0f} "
                f"bucket={_cal.bucket} "
                f"wr={_cal.wr:.1f}% "
                f"edge={_cal.edge:.2f} "
                f"adj={_cal.adjustment:+.1f} "
                f"effective={_cal.effective_score:.0f}"
            )
    except Exception as _cal_err:
        logger.warning(f"P4.3 calibration skipped for {coin}: {_cal_err}")
    # ============================================================
    # END P4.3
    # ============================================================

    record_gate_seen("execution")
    if confidence_data["final_score"] < final_threshold:
        record_reject("exec", "score_below_threshold", score=confidence_data["final_score"])
        score = confidence_data["final_score"]
        gap = final_threshold - score
        # P4.20: record reject distance
        try:
            record_reject_gap(gap)
        except Exception:
            pass
    
        logger.warning(
            f"EXEC_SKIP {coin} "
            f"score={score:.0f} < threshold={final_threshold} "
            f"gap={gap:.0f}"
        )
    
        # Shadow registration untuk near-miss (P3: broader gap tolerance)
        if gap <= 15:  # Lebih longgar dari SHADOW_MAX_GAP untuk capture near-pass
            try:
                logger.info(f"SHADOW_NEARPASS coin={coin} score={score:.0f} gap={gap:.0f}")
                register_shadow(
                    coin, event.direction, mark, confidence_data, event,
                    intent, belief, hl, micro_acc, failed_risk,
                    intent_drift, surprise, gap, final_threshold,
                    confidence_data.get("rr", 0.0),
                    shadow_mode="THRESHOLD",
                    block_reason="score_below_threshold",
                )
            except Exception as _se:
                logger.debug(f"register_shadow near-pass error: {_se}")
    
        # LOG KE JOURNAL SEBELUM RETURN
        _narrative = {
            "decision_type": "REJECT",
            "why_not": f"score_{score}_lt_{final_threshold}",
            "threshold": final_threshold,
            "score": score,
            "gap": gap,
        }
        
        # ===== SAFE JOURNAL BUILDER (EXEC_SKIP) =====
        journal_kwargs = dict(
            timestamp=time.time(),
            coin=coin,
            event_type=event.type,
            direction=event.direction,
            score=score,
            mode=execution_mode_str,
            executed=False,
            shadow=True,
            entry=mark,
            sl=confidence_data.get("sl", 0),
            tp=confidence_data.get("tp", 0),
            rr=confidence_data.get("rr", 0.0),
            intent=getattr(intent, "value", str(intent)) if intent else "unknown",
            belief=getattr(belief, "value", str(belief)) if belief else "seeking",
            decision_energy=confidence_data.get("decision_energy", 0),
            narrative=_narrative,
            journal_accept=True,
            execute_accept=False,
            blocked_reason=f"score_{score}_lt_{final_threshold}",
        )
        
        # ===== HARDENED OPTIONAL FIELDS (EXEC_SKIP) =====
        journal_kwargs.update({
            "hidden_liquidity": (
                hl.get("score", 0)
                if isinstance(locals().get("hl"), dict)
                else None
            ),
            "micro_acceptance": (
                micro_acc.get("score")
                if isinstance(locals().get("micro_acc"), dict)
                else None
            ),
            "failed_risk": (
                failed_risk.get("risk", 1.0)
                if isinstance(locals().get("failed_risk"), dict)
                else None
            ),
            "intent_drift": locals().get("intent_drift", 0.0),
            "surprise": locals().get("surprise", 0.0),
        })
        
        journal_entry = DecisionJournalEntry(**journal_kwargs)
        log_decision_journal(journal_entry)
        inc_pipeline_counter("journal")
        inc_pipeline_counter("reject_execute")
        record_opportunity_rejected(coin, "score_below_threshold")
        update_fatigue_memory(event.type)
    
        # ===== VELOCITY TRACE: REJECT =====
        log_velocity_trace(
            coin=coin,
            decision="REJECT",
            score=confidence_data.get("final_score", 0),
            threshold=final_threshold,
            regime=thesis_data.get("market_regime", "UNKNOWN"),
            size_mult=position_size_mult if "position_size_mult" in dir() else 1.0,
            position_gate="PASS" if is_position_open(coin, event.direction) else "CLEAR",
            stage="THESIS",
            cache_age=cache_age,
            data_source=data_source,
        )

        # ===== KRITIS: RETURN NONE, BUKAN LANJUT =====
        return None
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
    # ===== SAFE JOURNAL BUILDER (execute_decision universal) =====
    journal_kwargs = dict(
        timestamp=time.time(),
        coin=coin,
        event_type=event.type,
        direction=event.direction,
        score=confidence_data.get("final_score", 0),
        mode=execution_mode_str,
        executed=(decision_type == "EXECUTE"),
        shadow=(decision_type != "EXECUTE"),
        entry=mark,
        sl=confidence_data.get("sl", 0),
        tp=confidence_data.get("tp", 0),
        rr=confidence_data.get("rr", 0.0),
        intent=getattr(intent, "value", str(intent)) if intent else "unknown",
        belief=getattr(belief, "value", str(belief)) if belief else "seeking",
        decision_energy=confidence_data.get("decision_energy", 0),
        narrative=_narrative,
        journal_accept=True,
        execute_accept=(decision_type == "EXECUTE"),
        blocked_reason=why_not_final if decision_type == "REJECT" else None,
        signal_id=signal_id,
    )
    
    # ===== HARDENED OPTIONAL FIELDS (universal journal) =====
    journal_kwargs.update({
        "hidden_liquidity": (
            hl.get("score", 0)
            if isinstance(locals().get("hl"), dict)
            else None
        ),
        "micro_acceptance": (
            micro_acc.get("score")
            if isinstance(locals().get("micro_acc"), dict)
            else None
        ),
        "failed_risk": (
            failed_risk.get("risk", 1.0)
            if isinstance(locals().get("failed_risk"), dict)
            else None
        ),
        "intent_drift": locals().get("intent_drift", 0.0),
        "surprise": locals().get("surprise", 0.0),
        "outcome": locals().get("outcome"),
        "pnl": locals().get("pnl"),
        "mfe": locals().get("mfe"),
        "mae": locals().get("mae"),
        "closed": locals().get("closed", False),
        "close_reason": locals().get("close_reason"),
        "duration_minutes": locals().get("duration_minutes"),
    })
    
    journal_entry_universal = DecisionJournalEntry(**journal_kwargs)
    log_decision_journal(journal_entry_universal)
    inc_pipeline_counter("journal")

    # ===== P4.7.5: GATE YIELD — execution gate pass/reject =====
    if decision_type == "EXECUTE":
        record_gate_pass("execution")
    # ===== END P4.7.5 =====

    if decision_type == "REJECT":
        if position_size_mult > 0.3:
            position_size_mult = max(0.15, position_size_mult * 0.7)
        else:
            update_fatigue_memory(event.type)
            return None
    # ===== QUEUE ENTRY INTENT (near-pass only) =====
    if confidence_data.get("final_score", 0) < final_threshold:
        if confidence_data.get("final_score", 0) >= final_threshold - 10:
            queue_entry_intent({
                "coin": coin,
                "direction": event.direction,
                "score": confidence_data.get("final_score", 0),
                "threshold": final_threshold,
                "gap": final_threshold - confidence_data.get("final_score", 0),
                "entry": mark,
                "sl": confidence_data.get("sl", 0),
                "tp": confidence_data.get("tp", 0),
                "rr": confidence_data.get("rr", 0),
                "intent": intent.value if hasattr(intent, 'value') else str(intent),
                "belief": belief.value if hasattr(belief, 'value') else str(belief),
                "blocked": True,
                "block_reason": "score_below_threshold",
            })
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
            # ===== P4.49: FIX — field ini kurang, bikin KeyError di caller =====
            # (check_entry_alert_v10_phase1 access result["belief_state"] dkk
            # pakai bracket langsung, gak ada .get() fallback)
            "belief_state": belief.value if hasattr(belief, "value") else str(belief),
            "decision_energy": confidence_data.get("decision_energy", 0.0),
            "positive_evidence": confidence_data.get("evidence_reasons", []),
            "execution_mode": execution_mode_str,
            "execution_mode_v10": exec_mode.value.upper(),
        }
        # Journal already logged universally above
        auto_review()
        # ===== VELOCITY TRACE: SHADOW =====
        log_velocity_trace(
            coin=coin,
            decision="SHADOW",
            score=confidence_data.get("final_score", 0),
            threshold=final_threshold,
            regime=thesis_data.get("market_regime", "UNKNOWN"),
            size_mult=position_size_mult if "position_size_mult" in dir() else 1.0,
            position_gate="PASS" if is_position_open(coin, event.direction) else "CLEAR",
            stage="OBSERVE",
            cache_age=cache_age,
            data_source=data_source,
        )
        return shadow_result

    # ===== RECORD EXECUTION (NEW - V10) =====
    try:
        micro_acc_score = micro_acc.get("score", 50.0) if micro_acc and micro_acc.get("score") is not None else 50.0
        accept_intent(coin=coin, acceptance_score=float(micro_acc_score))
    except Exception as e:
        logger.debug(f"Accept intent error: {e}")

    # ===== P0 GATE 1: COOLDOWN (CEGAH SPAM) =====
    if is_entry_cooldown_active(coin, event.direction):
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason="cooldown_active",
            stage="GATE",
            score=confidence_data.get("final_score"),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=position_size_mult,
        )
        logger.warning(f"🛑 EXEC BLOCKED: {coin} {event.direction} (cooldown active)")
        update_fatigue_memory(event.type)
        record_opportunity_rejected(coin, "duplicate_cooldown")
        inc_pipeline_counter("reject_duplicate")
        return None  # BATAL

    # ===== P0 GATE 2: POSISI SUDAH OPEN (CEGAH DUPLIKAT RESTART/RESTORE) =====
    if is_position_open(coin, event.direction):
        # ===== P4.0: EMIT VELOCITY SKIP WITH CONTEXT =====
        emit_velocity_skip(
            coin=coin,
            reason="position_already_open",
            stage="GATE",
            score=confidence_data.get("final_score"),
            threshold=confidence_data.get("final_threshold"),
            regime=thesis_data.get("market_regime"),
            cache_age=cache_age,
            source=data_source,
            size=position_size_mult,
        )
        logger.warning(f"🛑 EXEC BLOCKED: {coin} {event.direction} (position already open)")
        update_fatigue_memory(event.type)
        record_opportunity_rejected(coin, "duplicate_position_exists")
        inc_pipeline_counter("reject_duplicate")
        return None  # BATAL

    # ===== SINGLE SOURCE OF TRUTH: SUGGESTED LEVERAGE =====
    # FIX: sebelumnya compute_suggested_leverage() dipanggil DUA KALI dengan
    # input berbeda — sekali di sini (untuk notif OPEN) tanpa conviction/
    # market_regime/confidence_data (jatuh ke default netral), dan sekali
    # lagi belakangan (untuk alert dict / compact analysis block) dengan
    # data lengkap. Hasilnya dua leverage berbeda muncul di dua tempat
    # (mis. "Leverage: 7.2x" di OPEN vs "Lev: 9.2x" di compact alert) untuk
    # SATU signal yang sama — padahal leverage harus immutable setelah
    # diputuskan. Sekarang dihitung SEKALI di sini, dengan semua konteks
    # yang tersedia (conviction_data & thesis_data sudah ada di titik ini),
    # lalu dipakai ulang di semua tempat lain — tidak dihitung ulang.
    leverage_info = compute_suggested_leverage(
        coin=coin,
        entry=mark,
        sl=confidence_data["sl"],
        position_size_mult=position_size_mult,
        conviction=conviction_data.get("conviction", 50.0),
        market_regime=thesis_data.get("market_regime", "UNKNOWN"),
        confidence_data=confidence_data,
    )

    # ===== RECORD EXECUTION (SINGLE SOURCE) =====
    record_execute()         
    # ===== RECORD FUNNEL: EXEC PASS & OPEN =====
    record_funnel_stage("exec_pass")
    record_funnel_stage("open_count")

    # P4.19: regime EXEC tracking
    try:
        record_regime_exec(thesis_data.get("market_regime", "UNKNOWN"), "EXEC")
    except Exception:
        pass

    # ===== SAVE =====
    if not PAPER_MODE:
        logger.info(f"DB_WRITE_PENDING signal={signal_id}")
        save_signal_v7(signal_id, coin, event.direction, confidence_data["final_score"], mark,
                      confidence_data["sl"], confidence_data["tp"], confidence_data["rr"], reason,
                      thesis_data["data_confidence"], thesis_obj.statement, thesis_obj.invalidation,
                      thesis_obj.confirmation, execution_mode_str, intent.value,
                      confidence_data["decision_energy"], position_size_mult,
                      filter_score, thesis_data["intent_confidence"], belief.value,
                      commitment_score, confidence_data["time_pressure"].value,
                      confidence_data["prediction_quality_mult"] * 100,
                      evidence_families=confidence_data.get("evidence_families", 0),
                      raw_score=confidence_data.get("raw_score"),
                      score_adjustment=confidence_data.get("score_adjustment"),
                      calibrated_score=confidence_data.get("effective_score"),
                      calibration_bucket=confidence_data.get("calibration_bucket"),
                      # ===== P4.50: CONVICTION + MEM-AT-ENTRY SNAPSHOT =====
                      conviction=conviction_data.get("conviction"),
                      conviction_mode=conviction_data.get("mode"),
                      conviction_penalty=conviction_data.get("total_penalty"),
                      mem_outcome_boost_at_entry=get_recent_outcome_boost(coin),
                      mem_cooldown_mult_at_entry=get_coin_cooldown_penalty(coin),
                      mem_stability_at_entry=compute_mem_stability(coin),
                      entry_quality=confidence_data.get("entry_quality", 0.0),
                      # ===== FIX (alert honesty): DETECT vs ZONE vs ENTRY snapshot =====
                      detect_price=detect_price,
                      entry_zone_low=entry_zone["zone_low"] if entry_zone else None,
                      entry_zone_high=entry_zone["zone_high"] if entry_zone else None,
                      signal_created_ts=signal_created_ts,
                      # ===== STRUCTURE_COMPARE AUDIT: V1 vs V2 snapshot =====
                      structure_audit=thesis_data.get("structure_audit"))
        # ===== TANDAI COOLDOWN HANYA JIKA DB WRITE SUKSES =====
        mark_entry_cooldown(coin, event.direction)

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
                    _lev_info = leverage_info  # single source of truth — dihitung sekali di atas
                    # TP-RR Boost: tampilkan tp3 actual (sudah include rr_multiplier)
                    _notif_targets = calculate_scaled_targets(
                        entry=mark,
                        direction=event.direction,
                        atr_pct=thesis_data.get("atr_pct", 2.0),
                        market_regime=thesis_data.get("market_regime", "UNKNOWN"),
                        rr_multiplier=confidence_data.get("tp_rr_mult", 1.0),
                    )
                    _tp3_price = _notif_targets["tp3"]["price"]
                    _sl_dist = abs(mark - confidence_data['sl'])
                    _tp3_rr = abs(_tp3_price - mark) / max(_sl_dist, 1e-10)

                    # ===== FIX (alert honesty): DETECT vs ZONE vs CURRENT + STATUS + AGE =====
                    # Fetch harga LIVE sekarang (bukan pakai `mark` yang udah jadi
                    # optimal_entry), supaya alert jujur soal seberapa jauh market
                    # udah geser dari saat sinyal ini dibentuk.
                    try:
                        _live_snapshot = get_snapshot()
                        _current_price = _live_snapshot.mids.get(coin, mark) if _live_snapshot else mark
                    except Exception:
                        _current_price = mark

                    _entry_gap_pct = abs(_current_price - mark) / max(mark, 1e-9) * 100
                    _entry_gap_signed = (_current_price - mark) / max(mark, 1e-9) * 100
                    _entry_status = classify_entry_status(_entry_gap_pct)
                    _signal_age = fmt_age(time.time() - signal_created_ts)

                    open_msg = f"🟡 <b>OPEN</b> {coin} [{direction_emoji} {event.direction}]\n"
                    open_msg += f"├─ Detected: {fmt_price(detect_price)}\n"
                    if entry_zone:
                        open_msg += f"├─ Entry Zone: {fmt_price(entry_zone['zone_low'])}–{fmt_price(entry_zone['zone_high'])}\n"
                    open_msg += f"├─ Optimal Entry: {fmt_price(mark)}\n"
                    open_msg += f"├─ Current: {fmt_price(_current_price)}\n"
                    open_msg += f"├─ Status: {_entry_status} ({_entry_gap_signed:+.2f}%)\n"
                    open_msg += f"├─ Age: {_signal_age}\n"
                    open_msg += f"├─ SL: {fmt_price(confidence_data['sl'])}\n"
                    open_msg += f"├─ TP: {fmt_price(_tp3_price)}\n"
                    open_msg += f"├─ Score: {confidence_data['final_score']}\n"
                    open_msg += f"├─ RR: 1:{_tp3_rr:.1f}\n"
                    open_msg += f"├─ Leverage: {_lev_info['suggested']:.1f}x (cap {_lev_info['native_cap']}x)\n"
                    open_msg += f"└─ Signal: {signal_id}"
                    tg_send(USER_ID, open_msg, parse_mode='HTML')
                    logger.info(
                        f"✅ OPEN notif SENT: {coin} {event.direction} signal_id={signal_id} "
                        f"detect={detect_price:.4f} optimal={mark:.4f} current={_current_price:.4f} "
                        f"gap={_entry_gap_signed:+.2f}% age={_signal_age}"
                    )
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
    # FIX (pre-existing bug): _active_candidates entries never had a "score"
    # key populated anywhere — ranking always compared real score against 0
    # for every other coin. Now updates this coin's entry with score +
    # exploitability before ranking, and other coins' entries get updated
    # the same way each time THEY get processed (best-effort, not perfectly
    # synchronous across coins scanned in the same cycle — acceptable since
    # this is informational ranking, not an execution gate).
    with _active_candidates_lock:
        if coin in _active_candidates:
            _active_candidates[coin]["score"] = confidence_data["final_score"]
            _active_candidates[coin]["exploitability"] = confidence_data.get("exploitability", confidence_data["final_score"])

    rank_text = "No rank"
    try:
        active_count = len(_active_candidates)
        if active_count > 0:
            # LAYER 2: ranking pakai exploitability, bukan raw score —
            # setup score lebih rendah tapi lebih eksploitatif (momentum,
            # zona segar, flow konvergen) bisa menang lawan score lebih
            # tinggi yang kurang eksploitatif.
            with _active_candidates_lock:
                active_scores = [(c, data.get("exploitability", data.get("score", 0))) for c, data in _active_candidates.items()]
            
            all_scores = [score for c, score in active_scores if c != coin]
            all_scores.append(confidence_data.get("exploitability", confidence_data["final_score"]))
            all_scores_sorted = sorted(all_scores, reverse=True)
            rank = all_scores_sorted.index(confidence_data.get("exploitability", confidence_data["final_score"])) + 1
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
        market_regime=thesis_data.get("market_regime", "UNKNOWN"),
        rr_multiplier=confidence_data.get("tp_rr_mult", 1.0),
    )

    # FIX: alert['tp']/['rr'] dulu pakai confidence_data["tp"]/["rr"] —
    # single-target SEBELUM TP-RR boost, dan field ini TIDAK PERNAH dipakai
    # eksekusi (TradeManager pakai tp_scaled/tp3 di bawah). Akibatnya semua
    # notif display (compact alert, /entry detail) nunjukin RR yang BEDA
    # dari apa yang benar-benar di-track sebagai exit target posisi.
    # Sekarang disamakan ke tp3 (boosted) — sama persis dengan yang
    # TradeManager pakai, supaya notif gak lagi nunjukin RR yang gak nyambung
    # ke posisi yang sebenarnya jalan.
    _tp3_price_final = targets["tp3"]["price"]
    _sl_dist_final = abs(mark - confidence_data["sl"])
    _tp3_rr_final = abs(_tp3_price_final - mark) / max(_sl_dist_final, 1e-10)

    # ===== P4.56: SUGGESTED LEVERAGE (informational, read-only bot) =====
    # (dihitung sekali di atas — sebelum notif OPEN — reuse di sini, JANGAN
    # dihitung ulang, supaya alert dict & notif OPEN selalu konsisten)

    return {
        "coin": coin,
        "signal_id": signal_id,
        "direction": event.direction,
        "score": confidence_data["final_score"],
        "entry": mark,
        "sl": confidence_data["sl"],
        "tp": _tp3_price_final,
        "tp_single_target": confidence_data["tp"],  # nilai lama, disimpan kalau ada konsumen yang butuh struktural-only
        "tp_scaled": targets,
        "rr": _tp3_rr_final,
        "rr_single_target": confidence_data["rr"],  # nilai lama, sama alasan di atas
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
        "position_size_mult": max(0.1, min(5.0, float(position_size_mult))),  # FIX: final guard clamp
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
        # ===== P4.50: CONVICTION BUDGET — dihitung di atas (compute_conviction_budget)
        # tapi sebelumnya gak pernah masuk return dict, jadi caller gak bisa akses.
        "conviction": conviction_data.get("conviction", 0.0),
        "conviction_mode": conviction_data.get("mode", "UNKNOWN"),
        "conviction_penalty": conviction_data.get("total_penalty", 0.0),
        # ===== P4.50: MEM SNAPSHOT — outcome memory state PADA SAAT entry diambil.
        # Disnapshot di sini (bukan dibaca ulang nanti) supaya alert OPEN dan
        # OpenPosition.mem_* selalu refleksikan kondisi entry, bukan kondisi
        # observasi belakangan yang udah berubah.
        "mem_outcome_boost": get_recent_outcome_boost(coin),
        "mem_cooldown_mult": get_coin_cooldown_penalty(coin),
        "mem_stability": compute_mem_stability(coin),
        # ===== P4.56: ATR snapshot — dipakai add_position() untuk dynamic trailing =====
        "atr_pct": thesis_data.get("atr_pct", 0.0),
        # ===== FIX (alert honesty): DETECT vs ZONE vs ENTRY snapshot =====
        # Disimpan sekali di sini (bukan direcompute di layer notif) supaya
        # alert OPEN, /entry, dan dashboard semua ngeliat angka yang sama
        # persis dengan momen sinyal ini dibuat — bukan hasil recalc belakangan
        # yang bisa beda karena zone/candle udah berubah.
        "detect_price": detect_price,
        "entry_zone_low": entry_zone["zone_low"] if entry_zone else None,
        "entry_zone_high": entry_zone["zone_high"] if entry_zone else None,
        "optimal_entry": mark,
        "signal_created_ts": signal_created_ts,
        # ===== P4.56: SUGGESTED LEVERAGE (informational only, bot read-only) =====
        "leverage_suggested": leverage_info["suggested"],
        "leverage_native_cap": leverage_info["native_cap"],
        "leverage_info": leverage_info,  # ← L2: full breakdown untuk alert
        # ===== FIX: ADD MISSING FIELDS FOR FLOW STATUS =====
        "price_ok": confidence_data.get("price_ok", False),
        "flow_ok": confidence_data.get("flow_ok", False),
        "pos_ok": confidence_data.get("pos_ok", False),
        "delta": thesis_data.get("delta", 0.0),
        "oi_roc": thesis_data.get("oi_roc", 0.0),
        "cvd_accel": thesis_data.get("cvd_accel", False),
        "vol_spike": thesis_data.get("vol_spike", 1.0),
        "momentum": thesis_data.get("momentum", 50),
        "trigger_strength": 0.0,
        "regime_interpretation": None,
        "ob_reaction": None,
        "fvg_quality": None,
        "context_memory": None,
        "confidence_calibrated": confidence_data.get("confidence_calibrated", confidence_data.get("confidence", 50)),
        "calibration_samples": confidence_data.get("calibration_samples", 0),
        # ===== END FIX =====
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
        # NOTE: /entry dan /warroom (caller fungsi ini) selalu fetch fresh candles,
        # jadi cache_age=0 / data_source="LIVE" merepresentasikan kondisi real.
        cache_age = 0.0
        data_source = "LIVE"
        result = execute_decision(
            coin, thesis_data, confidence_data,
            thesis_data["event"], thesis_data["intent"], thesis_data["intent_legacy"],
            context, breath,
            cache_age=cache_age, data_source=data_source
        )

        # ===== LOG TRACE =====
        if result:
            trace = DecisionTrace(
                timestamp=time.time(),
                coin=coin,
                event_type=result.get("area", "UNKNOWN"),
                belief_state=result.get("belief_state", "SEEKING"),  # P4.49: safe fallback
                confidence=result.get("decision_energy", 0.0),
                decision_energy=result.get("decision_energy", 0.0),
                final_decision="EXECUTE",
                reasons=result.get("positive_evidence", []),
                why_not=[result["why_not"]] if result.get("why_not") else [],
                what_changed=f"belief:{result.get('belief_state', 'SEEKING')}|mode:{result.get('execution_mode', 'NORMAL')}|v10_mode:{result.get('execution_mode_v10', 'NORMAL')}|global:{global_intent}",
                context_age=result.get("context_age", 0.0),
                execution_mode=result.get("execution_mode_v10", "NORMAL")
            )
            log_decision_trace(trace)

            # ===== VELOCITY TRACE: EXECUTE =====
            # FIX: pakai field dari `result`/`confidence_data` (in-scope), bukan
            # final_threshold/position_size_mult/event yang sebelumnya gak pernah
            # didefinisikan di fungsi ini (NameError, ke-swallow oleh except di bawah,
            # bikin /entry & /warroom selalu return None walau eksekusi sukses).
            log_velocity_trace(
                coin=coin,
                decision="EXECUTE",
                score=result.get("score", confidence_data.get("final_score", 0)),
                threshold=confidence_data.get("final_threshold", 0),
                regime=thesis_data.get("market_regime", "UNKNOWN"),
                size_mult=result.get("position_size_mult", 1.0),
                position_gate="PASS" if is_position_open(coin, result.get("direction", "")) else "CLEAR",
                stage="EXECUTE",
                cache_age=cache_age,
                data_source=data_source,
            )

        return result

    except Exception as e:
        logger.error(f"Entry error {coin}: {e}")
        return None

# ========== PHASE 1 — ENTRY CHECK UPGRADED ==========

def check_entry_alert_v10_phase1(coin: str, mark: float, master_candles: Dict,
                                 rank: int = 999, cache_age: float = 999.0,
                                 data_source: str = "UNKNOWN") -> Optional[dict]:
    """V10 + Phase 1 upgrades: dengan funnel trace lengkap"""
    try:
        # ===== FAST INVENTORY GATE (P0 FIX) =====
        # Cek inventory SEBELUM observe/thesis/confidence biar gak buang API call
        # buat coin yang pasti di-block di execute_decision() nanti.
        # NOTE: ini gate TAMBAHAN, bukan pengganti gate utama di execute_decision()
        # (yang itu masih jalan karena butuh event.type buat fatigue memory).
        # Pakai cap yang sama persis dengan execute_decision() (satu sumber kebenaran),
        # tapi dihitung murah di sini (tanpa detect_orphan_signals/DB query) supaya
        # fast-path tetap ringan dipanggil per-candidate.
        with TRADE_MANAGER._lock:
            _fast_total_open = sum(1 for p in TRADE_MANAGER.positions.values() if p.status == "OPEN")
        _fast_max_total_open = min(
            TUNABLE.get("MAX_OPEN_CAP", 60),
            max(20, int(_fast_total_open * 1.5))
        )
        if _fast_total_open >= _fast_max_total_open:
            logger.debug(f"INVENTORY FULL ({_fast_total_open}/{_fast_max_total_open}), skip {coin} before fetch")
            record_opportunity_rejected(coin, "inventory_full")
            inc_pipeline_counter("reject_inventory")
            # ===== P4.1: EMIT VELOCITY SKIP WITH CONTEXT (Phase B) =====
            # This fast-path gate fires before observe_market/build_thesis
            # are ever called — there is no regime to report yet, by
            # design (that's the whole point of the fast gate: skip
            # expensive work for a candidate that's guaranteed to be
            # blocked anyway). regime=None here is the honest answer,
            # not a telemetry bug — don't manufacture a value.
            emit_velocity_skip(
                coin=coin,
                reason=f"inventory_full_{_fast_total_open}/{_fast_max_total_open}",
                stage="GATE",
                regime=None,
                cache_age=cache_age,
                source=data_source,
            )
            return None

        # ===== COUNTER: SCAN =====
        record_opportunity_scan(coin)
        inc_pipeline_counter("check")

        # ===== LAYER 0: CONTEXT =====
        try:
            regime = interpret_regime_v10(coin)
            ctx = ensure_context_fields(get_context_snapshot(coin))  # FIX: wrap agar attr access aman
            _context_memory.add(ctx)
            breath = compute_market_breath_v10()
        except Exception as e:
            logger.error(f"Context error {coin}: {e}")
            record_opportunity_rejected(coin, "context_error")
            inc_pipeline_counter("reject_obs")
            # ===== P4.1: EMIT VELOCITY SKIP WITH CONTEXT (Phase B) =====
            # `regime` may or may not have been assigned depending on
            # exactly where the exception fired inside the try block —
            # use locals().get() to avoid an UnboundLocalError, and pass
            # through whatever we get (including None) rather than
            # coercing to "UNKNOWN". If interpret_regime_v10() itself
            # threw, regime genuinely was never computed.
            emit_velocity_skip(
                coin=coin,
                reason="context_error",
                stage="OBS",
                regime=locals().get("regime"),
                cache_age=cache_age,
                source=data_source,
            )
            return None

        # ===== LAYER 1: OBSERVE =====
        record_gate_seen("obs")
        obs = observe_market(coin, mark, master_candles)
        if not obs:
            logger.debug(f"❌ OBS REJECT {coin}: observe_failed")
            record_opportunity_rejected(coin, "observe_failed")
            inc_pipeline_counter("reject_obs")
            record_reject("obs", "observe_failed")
            emit_velocity_skip(
                coin=coin,
                reason="observe_failed",
                stage="OBS",
                regime=None,
                cache_age=cache_age,
                source=data_source,
            )
            return None

        # ===== P0: HANDLE OBSERVING STATUS =====
        if obs.get("status") == "OBSERVING":
            logger.debug(f"🔍 OBSERVING {coin}: {obs.get('progress', '?')}")
            # Tidak reject, hanya skip untuk cycle ini (masih kumpulkan observasi)
            inc_pipeline_counter("obs_observing")
            return None

        if obs.get("status") == "REJECT":
            reason = obs.get("reason", "observe_rejected")
            logger.debug(f"❌ OBS REJECT {coin}: {reason}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_obs")
            record_reject("obs", reason)
            emit_velocity_skip(
                coin=coin,
                reason=reason,
                stage="OBS",
                regime=obs.get("market_regime") if obs else None,
                cache_age=cache_age,
                source=data_source,
            )
            return None
        record_gate_pass("obs")
        inc_pipeline_counter("obs")
        # P4.19: regime OBS tracking
        try:
            record_regime_exec(obs.get("market_regime", "UNKNOWN"), "OBS")
        except Exception:
            pass
        logger.debug(f"✅ OBS PASS {coin}: event={obs['best_event'].type if obs.get('best_event') else 'NONE'}")

        
        # ===== LAYER 2: THESIS [DEBUG] =====
        record_gate_seen("thesis")
        thesis_data = build_thesis(obs)
        if not thesis_data or thesis_data.get("status") == "REJECT":
            reason = thesis_data.get("reason", "thesis_failed") if thesis_data else "thesis_none"
            logger.warning(f"THESIS_REJECT_{coin} reason={reason} obs={obs.get('intent') if obs else 'none'}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_thesis")
            record_reject("thesis", reason)
            # ===== P4.1: EMIT VELOCITY SKIP WITH CONTEXT =====
            # build_thesis()'s REJECT dict only carries status/reason/coin
            # (by design, to stay light) — it does NOT propagate
            # market_regime even though obs (its input, still in scope
            # here) already passed OBS and has it. Pull from obs, not
            # thesis_data, or this would read as regime=None even when a
            # real regime was available.
            emit_velocity_skip(
                coin=coin,
                reason=reason,
                stage="THESIS",
                regime=obs.get("market_regime") if obs else None,
                cache_age=cache_age,
                source=data_source,
            )
            return None
        record_gate_pass("thesis")
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

        record_gate_seen("confidence")
        confidence_data = compute_confidence(thesis_data)
        if not confidence_data or confidence_data.get("status") == "REJECT":
            reason = confidence_data.get("reason", "confidence_failed") if confidence_data else "confidence_none"
            logger.debug(f"❌ CONFIDENCE REJECT {coin}: {reason}")
            record_opportunity_rejected(coin, reason)
            inc_pipeline_counter("reject_conf")
            record_reject("confidence", reason)
            # ===== P4.1: EMIT VELOCITY SKIP WITH CONTEXT =====
            # compute_confidence()'s REJECT dict (e.g. low_rr) only carries
            # status/reason/coin/rr — it does NOT propagate market_regime,
            # even though thesis_data (its input, still in scope here)
            # already passed THESIS and has it. Pull from thesis_data, not
            # confidence_data, or this would read as regime=None even when
            # a real regime was available.
            emit_velocity_skip(
                coin=coin,
                reason=reason,
                stage="CONF",
                regime=thesis_data.get("market_regime") if thesis_data else None,
                cache_age=cache_age,
                source=data_source,
            )
            return None
        record_gate_pass("confidence")
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
            ctx, breath,
            cache_age=cache_age, data_source=data_source
        )
        
        if result:
            # ===== TERMINAL LOG =====
            print(f"🚀 EXECUTED {coin} {result.get('direction', '?')} score={result.get('score', 0)} RR={result.get('rr', 0):.1f}")
            logger.info(f"🚀 EXECUTED {coin}: {result.get('direction', '?')} score={result.get('score', 0)}")
            #trace :
            trace = DecisionTrace(
                timestamp=time.time(),
                coin=coin,
                event_type=result.get("area", "UNKNOWN"),
                belief_state=result.get("belief_state", "SEEKING"),  # P4.49: safe fallback
                confidence=result.get("confidence_calibrated", result.get("decision_energy", 0.0)),
                decision_energy=result.get("decision_energy", 0.0),
                final_decision="EXECUTE",
                reasons=result.get("positive_evidence", []),
                why_not=[result.get("why_not", "")] if result.get("why_not") else [],
                what_changed=f"regime:{regime.regime}|trans:{regime.transition_prob:.0f}%|score:{result.get('score', 0)}",
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
            # ===== P4: CORRELATION FIELDS — propagate cache_age/data_source ke alert =====
            result['cache_age'] = cache_age
            result['data_source'] = data_source

            # ===== DYNAMIC AGGRESSION: OBS memory + hot coin boost (ranking only) =====
            memory_boost = get_obs_memory_boost(coin)
            if memory_boost > 0:
                result["score"] = result.get("score", 0) + memory_boost
                result["memory_boost"] = round(memory_boost, 3)
            hot_boost = get_hot_coin_boost(coin)
            if hot_boost > 0:
                result["score"] = result.get("score", 0) + hot_boost
                result["hot_boost"] = round(hot_boost, 3)
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
        return ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "POL", "LINK", "UNI", "AAVE", "ZEC", "HYPE"]

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
        return ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "POL", "LINK", "UNI", "AAVE", "HYPE", "ZEC"]

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

# ============================================================
# P0: MARKET HALF-LIFE STATE
# ============================================================

_entropy_ema: Dict[str, float] = {}
_entropy_ema_lock = threading.RLock()
_ENTROPY_EMA_ALPHA = 0.2  # Smoothing factor

_refresh_in_progress: Dict[str, float] = {}
_refresh_in_progress_lock = threading.RLock()
_REFRESH_MIN_GAP = 15

_obs_counter: Dict[str, int] = {}
_obs_counter_lock = threading.RLock()

# ===== CHAOS FETCH TRACKING =====
_chaos_fetch_count = 0
_chaos_fetch_lock = threading.RLock()
_CHAOS_FETCH_LIMIT = 3

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
# DYNAMIC AGGRESSION STACK — Market Heat + 6 Pilar
# ============================================================
# Filosofi: Agresif yang cerdas, bukan agresif yang mabuk API
# Semua zero extra API call, semua observable via log/debug
# ============================================================

# ============================================================
# P0: MARKET HEAT — single source of truth untuk semua pilar
# ============================================================
_MARKET_HEAT: Dict[str, Any] = {"score": 0.0, "updated": 0.0, "components": {}}
_MARKET_HEAT_LOCK = threading.RLock()

def update_market_heat():
    """Update market heat dari pipeline metrics + OI/delta acceleration."""
    with _MARKET_HEAT_LOCK:
        pipe = get_pipeline_metrics()
        try:
            oi_accel = get_oi_acceleration("BTC", window=5)
        except Exception:
            oi_accel = 0.0
        try:
            delta_accel = get_delta_acceleration("BTC", window=5)
        except Exception:
            delta_accel = 0.0
        obs = pipe.get('obs', 0)
        scan = pipe.get('scan_total', 1)
        obs_ratio = obs / max(1, scan)
        thesis = pipe.get('thesis', 0)
        thesis_ratio = thesis / max(1, obs) if obs > 0 else 0.0
        api_health = compute_api_health_score()
        heat = (
            abs(oi_accel) * 0.25 +
            abs(delta_accel) * 0.20 +
            obs_ratio * 0.20 +
            thesis_ratio * 0.20 +
            api_health * 0.15
        )
        _MARKET_HEAT["score"] = min(1.0, heat)
        _MARKET_HEAT["updated"] = time.time()
        _MARKET_HEAT["components"] = {
            "oi_accel": round(oi_accel, 3),
            "delta_accel": round(delta_accel, 3),
            "obs_ratio": round(obs_ratio, 3),
            "thesis_ratio": round(thesis_ratio, 3),
            "api_health": round(api_health, 3),
        }

def get_market_heat() -> float:
    with _MARKET_HEAT_LOCK:
        if time.time() - _MARKET_HEAT["updated"] > 30:
            update_market_heat()
        return _MARKET_HEAT["score"]

def get_market_heat_components() -> Dict[str, float]:
    with _MARKET_HEAT_LOCK:
        if time.time() - _MARKET_HEAT["updated"] > 30:
            update_market_heat()
        return _MARKET_HEAT.get("components", {})


# ============================================================
# P1: DISCOVERY BURST — trigger = market bergerak, bukan timer
# ============================================================
_DISCOVERY_BURST: Dict[str, Any] = {"until": 0.0, "boost": 4}
_DISCOVERY_BURST_LOCK = threading.RLock()

def compute_burst_boost() -> int:
    heat = get_market_heat()
    pipe = get_pipeline_metrics()
    obs_ratio = pipe.get('obs', 0) / max(1, pipe.get('scan_total', 1))
    thesis_ratio = pipe.get('thesis', 0) / max(1, pipe.get('obs', 1))
    raw_boost = (heat * 8) + (obs_ratio * 4) + (thesis_ratio * 3)
    return int(max(2, min(10, raw_boost)))

def trigger_burst(duration_seconds: int = 180):
    with _DISCOVERY_BURST_LOCK:
        _DISCOVERY_BURST["until"] = time.time() + duration_seconds
        _DISCOVERY_BURST["boost"] = compute_burst_boost()
        logger.info(f"🔥 BURST: +{_DISCOVERY_BURST['boost']} candidates for {duration_seconds}s (heat={get_market_heat():.2f})")

def get_live_candidate_limit() -> int:
    """Return candidate limit dengan burst window. Cap 18."""
    with _DISCOVERY_BURST_LOCK:
        base = get_candidate_limit_for_phase(get_engine_phase()[0])
        if time.time() < _DISCOVERY_BURST["until"]:
            base += _DISCOVERY_BURST["boost"]
        return min(base, 18)

def check_and_trigger_burst() -> bool:
    heat = get_market_heat()
    pipe = get_pipeline_metrics()
    obs_ratio = pipe.get('obs', 0) / max(1, pipe.get('scan_total', 1))
    thesis_ratio = pipe.get('thesis', 0) / max(1, pipe.get('obs', 1))
    trigger_score = heat * 0.6 + obs_ratio * 0.3 + thesis_ratio * 0.1
    if trigger_score > 0.65:
        trigger_burst(180)
        return True
    return False


# ============================================================
# P2: PROGRESSIVE GATE — regime + persistence, bukan coverage statis
# ============================================================
def get_progressive_gate(coin: str, base_magnitude: float = 0.10) -> float:
    """
    effective_gate = base × (1 - regime_strength×0.15) × (1 - oi_persistence×0.10)
    Range: 70%-100% dari base_magnitude (kondisi sangat jelas -> noisy/default).
    Manggil interpret_regime_v10 (ada candle cost) tapi HANYA untuk coin
    yang sudah lolos is_imbalance_valid(gate=base_magnitude), bukan semua ~230 coin.

    FIX: floor lama 0.07 itu absolut, dari skala magnitude versi lama.
    base_magnitude sekarang datang dari compute_dynamic_magnitude_gate()
    yang udah relatif ke skala observasi (~0.0001-0.001) — floor 0.07 bakal
    OVERRIDE balik ke gate super ketat, nge-mentahin fix di atas. Floor
    sekarang relatif: gak boleh turun di bawah 70% base_magnitude.
    """
    try:
        regime_int = interpret_regime_v10(coin)
        regime_strength = regime_int.strength / 100.0
        oi_persist, _ = get_oi_persistence(coin)
        persistence = 1.0 if oi_persist else 0.0
        effective = base_magnitude * (1 - regime_strength * 0.15) * (1 - persistence * 0.10)
        floor = base_magnitude * 0.7
        return max(floor, min(base_magnitude, effective))
    except Exception:
        return base_magnitude


# ============================================================
# P3: OBS MEMORY — ranking boost, nonlinear decay
# ============================================================
_OBS_MEMORY: Dict[str, Dict[str, Any]] = {}
_OBS_MEMORY_LOCK = threading.RLock()
_OBS_MEMORY_TTL = 900  # 15 menit

def store_obs_memory(coin: str, obs_score: float, event_type: str, direction: str):
    with _OBS_MEMORY_LOCK:
        _OBS_MEMORY[coin] = {
            "score": obs_score,
            "event_type": event_type,
            "direction": direction,
            "ts": time.time(),
        }

def get_obs_memory_boost(coin: str) -> float:
    """boost = sqrt(score) × 0.08 × decay. Max ~8 point."""
    with _OBS_MEMORY_LOCK:
        entry = _OBS_MEMORY.get(coin)
        if not entry:
            return 0.0
        age = time.time() - entry["ts"]
        if age > _OBS_MEMORY_TTL:
            _OBS_MEMORY.pop(coin, None)
            return 0.0
        decay = 1.0 - (age / _OBS_MEMORY_TTL)
        return min(1.0, (entry["score"] ** 0.5) * 0.08 * decay)

def cleanup_obs_memory():
    with _OBS_MEMORY_LOCK:
        now = time.time()
        expired = [c for c, d in list(_OBS_MEMORY.items()) if now - d["ts"] > _OBS_MEMORY_TTL]
        for c in expired:
            _OBS_MEMORY.pop(c, None)


# ============================================================
# P4: HOT COIN — multi-factor, dynamic threshold
# ============================================================
_HOT_COINS: Dict[str, Dict[str, Any]] = {}
_HOT_COINS_LOCK = threading.RLock()
_HOT_COIN_TTL = 1200  # 20 menit

def compute_hot_score(coin: str) -> float:
    try:
        oi_accel = get_oi_acceleration(coin, window=5)
        delta_accel = get_delta_acceleration(coin, window=5)
        velocity = get_velocity_score(coin, "LONG")[0] / 100.0
        return abs(oi_accel) * 0.4 + abs(delta_accel) * 0.3 + velocity * 0.3
    except Exception:
        return 0.0

def get_hot_threshold() -> float:
    """0.65–0.75 berdasarkan market heat."""
    return max(0.65, 0.75 - (get_market_heat() * 0.1))

def update_hot_coin(coin: str):
    hot_score = compute_hot_score(coin)
    if hot_score > get_hot_threshold():
        with _HOT_COINS_LOCK:
            _HOT_COINS[coin] = {
                "score": hot_score,
                "ts": time.time(),
                "boost": min(15.0, hot_score * 20),
            }
        logger.debug(f"🔥 HOT {coin}: score={hot_score:.2f}")

def get_hot_coin_boost(coin: str) -> float:
    with _HOT_COINS_LOCK:
        entry = _HOT_COINS.get(coin)
        if not entry:
            return 0.0
        age = time.time() - entry["ts"]
        if age > _HOT_COIN_TTL:
            _HOT_COINS.pop(coin, None)
            return 0.0
        return entry["boost"] * (1.0 - age / _HOT_COIN_TTL)

def is_hot_coin(coin: str) -> bool:
    with _HOT_COINS_LOCK:
        entry = _HOT_COINS.get(coin)
        if not entry:
            return False
        if time.time() - entry["ts"] > _HOT_COIN_TTL:
            _HOT_COINS.pop(coin, None)
            return False
        return True

def cleanup_hot_coins():
    with _HOT_COINS_LOCK:
        now = time.time()
        expired = [c for c, d in list(_HOT_COINS.items()) if now - d["ts"] > _HOT_COIN_TTL]
        for c in expired:
            _HOT_COINS.pop(c, None)


# ============================================================
# P5: ADAPTIVE INTERVAL — heat-driven
# ============================================================
_LAST_CYCLE_RUNTIME = 5.0
_LAST_CYCLE_RUNTIME_LOCK = threading.RLock()

def record_cycle_runtime(seconds: float):
    """Record actual cycle runtime. FIX: pakai global agar assignment beneran nulis ke modul."""
    global _LAST_CYCLE_RUNTIME
    with _LAST_CYCLE_RUNTIME_LOCK:
        _LAST_CYCLE_RUNTIME = seconds

def get_adaptive_interval() -> int:
    """heat>0.75→15s, heat>0.45→25s, else→40s. Floor = runtime×1.15."""
    heat = get_market_heat()
    with _LAST_CYCLE_RUNTIME_LOCK:
        runtime = _LAST_CYCLE_RUNTIME
    if heat > 0.75:
        target = 15
    elif heat > 0.45:
        target = 25
    else:
        target = 40
    return int(max(target, runtime * 1.15))


# ============================================================
# P6: DISCOVERY FOCUS QUEUE — bot gak amnesia antar cycle
# ============================================================
_DISCOVERY_FOCUS: Dict[str, Dict[str, Any]] = {}
_DISCOVERY_FOCUS_LOCK = threading.RLock()
_DISCOVERY_FOCUS_TTL = 1200  # 20 menit

def add_discovery_focus(coin: str, reason: str, score: float):
    with _DISCOVERY_FOCUS_LOCK:
        _DISCOVERY_FOCUS[coin] = {"reason": reason, "score": score, "ts": time.time()}
    logger.debug(f"🎯 FOCUS {coin}: {reason} (score={score:.0f})")

def get_discovery_focus(limit: int = 5) -> List[str]:
    with _DISCOVERY_FOCUS_LOCK:
        now = time.time()
        expired = [c for c, d in list(_DISCOVERY_FOCUS.items()) if now - d["ts"] > _DISCOVERY_FOCUS_TTL]
        for c in expired:
            _DISCOVERY_FOCUS.pop(c, None)
        sorted_focus = sorted(_DISCOVERY_FOCUS.items(), key=lambda x: x[1]["score"], reverse=True)
        return [c for c, _ in sorted_focus[:limit]]

def cleanup_discovery_focus():
    with _DISCOVERY_FOCUS_LOCK:
        now = time.time()
        expired = [c for c, d in list(_DISCOVERY_FOCUS.items()) if now - d["ts"] > _DISCOVERY_FOCUS_TTL]
        for c in expired:
            _DISCOVERY_FOCUS.pop(c, None)



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
    
# ========== P1: DISCOVERY LAYER HELPERS (Persistence + Acceptance) ==========
def _get_delta_persistence_score(coin: str, window: int = 3) -> float:
    """
    Direction-agnostic persistence score.
    Measures how consistently delta has trended in ONE direction (not require specific direction).
    
    Returns: 0.0 (no persistence) to 1.0 (perfect persistence)
    
    Used in discovery layer where we don't yet know if we'll trade LONG or SHORT.
    """
    try:
        delta_history = list(_rolling_delta.get(coin, deque()))
        if len(delta_history) < window:
            return 0.0
        
        recent = delta_history[-window:]
        
        # Check if delta consistently increasing OR consistently decreasing
        increasing = all(recent[i] <= recent[i+1] for i in range(len(recent)-1))
        decreasing = all(recent[i] >= recent[i+1] for i in range(len(recent)-1))
        
        if increasing or decreasing:
            # Perfect trend persistence
            return 1.0
        
        # Measure partial consistency: how many adjacent pairs have same direction
        same_direction_count = 0
        for i in range(len(recent) - 1):
            if (recent[i+1] - recent[i]) * (recent[i] - (recent[i-1] if i > 0 else 0)) > 0:
                same_direction_count += 1
        
        partial_score = same_direction_count / max(len(recent) - 1, 1)
        return partial_score
    except Exception:
        return 0.0


def _get_acceptance_score(coin: str, master_candles: list = None) -> float:
    """
    Acceptance score: how much did price close within its range?
    
    Idea: If price is under accumulation (smart money buying), close position
    should be in upper half (acceptance). If being distributed, close should be
    in lower half (rejection).
    
    For discovery: we measure acceptance as how close the price is to the top of
    its recent candles — indicating institutional interest without rejection.
    
    Returns: 0.0 (closed at bottom/rejection) to 1.0 (closed at top/acceptance)
    """
    try:
        if not master_candles or len(master_candles) < 2:
            return 0.5  # Default neutral
        
        recent_candle = master_candles[-1]
        high = recent_candle.get("h", 0)
        low = recent_candle.get("l", 0)
        close = recent_candle.get("c", 0)
        
        if high == low:
            return 0.5  # Doji, neutral
        
        # Acceptance position: where close is in the range
        range_span = high - low
        close_position = close - low
        acceptance = close_position / max(range_span, 1e-10)
        
        # Clamp to 0-1
        return max(0.0, min(1.0, acceptance))
    except Exception:
        return 0.5
# ========== END P1 DISCOVERY HELPERS ==========


def apply_flow_diversity_penalty(candidates: List[str], scores: Dict[str, float]) -> Dict[str, float]:
    """Penalty buat candidates dengan flow signature mirip (OI accel, delta, spread
    compression) — cegah pool didominasi coin yang bakal gerak barengan."""
    if len(candidates) < 3:
        return scores

    features = {}
    for coin in candidates:
        try:
            features[coin] = [
                get_oi_acceleration(coin),
                get_delta_shift(coin) / 10,
                get_spread_compression(coin),
            ]
        except Exception:
            features[coin] = [0, 0, 0]

    for i, coin1 in enumerate(candidates):
        if coin1 not in features:
            continue
        for coin2 in candidates[i+1:]:
            if coin2 not in features:
                continue
            f1, f2 = features[coin1], features[coin2]
            dot = sum(a * b for a, b in zip(f1, f2))
            norm1 = math.sqrt(sum(a * a for a in f1)) or 1
            norm2 = math.sqrt(sum(b * b for b in f2)) or 1
            sim = dot / (norm1 * norm2) if norm1 > 0 and norm2 > 0 else 0
            if sim > 0.8:
                if scores.get(coin1, 0) < scores.get(coin2, 0):
                    scores[coin1] = scores.get(coin1, 0) * 0.85
                else:
                    scores[coin2] = scores.get(coin2, 0) * 0.85

    return scores


_LAST_GOOD_CANDIDATES: List[str] = ["BTC", "ETH", "SOL", "ARB", "OP"]
_LAST_GOOD_CANDIDATES_LOCK = threading.RLock()

def rotate_scan_order(candidates: List[str]) -> List[str]:
    """P2: shuffle deterministik berbasis waktu (cycle 5 menit), bukan
    urutan tetap. Kalau budget habis di tengah loop process_candidates_deep,
    coin yang sama gak selalu jadi korban gara-gara selalu di posisi
    belakang list."""
    if not candidates:
        return candidates
    cycle = int(time.time() / 300)
    rng = random.Random(cycle)
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    return shuffled

def update_last_good_candidates(candidates: List[str]):
    """P4: simpan hasil discovery terakhir yang genuinely nge-return
    kandidat (bukan hasil fallback itu sendiri, dicek di caller)."""
    global _LAST_GOOD_CANDIDATES
    if candidates:
        with _LAST_GOOD_CANDIDATES_LOCK:
            _LAST_GOOD_CANDIDATES = candidates[:12]

def get_fallback_candidates(limit: int = 12) -> List[str]:
    """P4: fallback ke discovery result terakhir yang valid, bukan
    hardcoded BTC/ETH/SOL doang — biar alt exposure gak collapse ke
    large-cap tiap kali discovery gagal sesaat."""
    with _LAST_GOOD_CANDIDATES_LOCK:
        return _LAST_GOOD_CANDIDATES[:limit]

# ============================================================
# P0: CONDITIONAL BTC INJECTION (BUKAN PRIVILEGED ADMISSION)
# ============================================================

# Cache untuk discovery scores per cycle (biar gak recompute)
_disco_scores_cache: Dict[str, float] = {}
_disco_scores_cache_lock = threading.RLock()

def cache_discovery_scores(scores: Dict[str, float]):
    """Cache discovery scores for use in BTC injection."""
    with _disco_scores_cache_lock:
        _disco_scores_cache.update(scores)
        # Keep cache bounded
        if len(_disco_scores_cache) > 500:
            _disco_scores_cache.clear()


def clear_discovery_cache():
    """Clear discovery score cache at end of cycle."""
    with _disco_scores_cache_lock:
        _disco_scores_cache.clear()


def inject_context_coin(
    candidates: List[str],
    snapshot: MarketSnapshot,
    max_candidates: int,
    use_strict_gate: bool = True
) -> List[str]:
    """
    Inject BTC as context coin ONLY IF:
    1. BTC not already in candidates
    2. BTC is a valid coin in snapshot
    3. BTC's discovery score is above the current dynamic gate
    
    This replaces the privileged admission (insert(0, "BTC")) 
    with a merit-based conditional injection.
    """
    if "BTC" in candidates:
        return candidates
    
    if not snapshot or "BTC" not in snapshot.mids:
        return candidates
    
    # ===== GATE 1: OI minimum =====
    oi_usd = snapshot.oi.get("BTC", 0)
    if oi_usd < 0.25:  # OI minimum gate ($250k)
        return candidates
    
    # ===== GATE 2: Viability (OI/Volume ratio) =====
    if not is_viable_coin("BTC"):
        return candidates
    
    # ===== GATE 3: Discovery score =====
    try:
        imbalance = compute_imbalance_strength("BTC")
        magnitude = imbalance.get("magnitude", 0)
        persistence = imbalance.get("persistence", 0)
        data_conf = imbalance.get("data_confidence", 0)
        
        # Use dynamic gate based on current market
        dynamic_gate = compute_dynamic_magnitude_gate("RANGING")
        if use_strict_gate:
            min_magnitude = dynamic_gate
        else:
            min_magnitude = max(0.003, dynamic_gate * 0.7)  # Relaxed for context
        
        if magnitude < min_magnitude or persistence < 0.3 or data_conf < 0.3:
            return candidates
        
        # Base score = magnitude * 100 (same formula as V12)
        btc_score = magnitude * 100
        
        # Apply same bonuses as other coins (spread compression, persistence)
        spread_comp = imbalance["components"].get("spread_comp", 0.5)
        if spread_comp > 0.6:
            btc_score += 10
        elif spread_comp > 0.4:
            btc_score += 5
        
        if persistence > 0.7:
            btc_score += 10
        elif persistence > 0.55:
            btc_score += 5
        
        # Apply memory decay (BTC may have been selected many times)
        n = get_coin_selection_count("BTC")
        btc_score *= math.exp(-0.15 * n)
        
    except Exception as e:
        logger.debug(f"BTC discovery score failed (non-fatal): {e}")
        return candidates
    
    # ===== GATE 4: BTC must earn its slot =====
    # Compare against current candidate scores
    candidate_scores = {}
    for coin in candidates:
        # Use cached scores if available
        with _disco_scores_cache_lock:
            if coin in _disco_scores_cache:
                candidate_scores[coin] = _disco_scores_cache[coin]
            else:
                # Fallback: OI ROC proxy
                try:
                    candidate_scores[coin] = get_oi_roc(coin, window_minutes=60) * 5 + 20
                except:
                    candidate_scores[coin] = 0
    
    # BTC must be at least 80% as good as the weakest candidate
    if candidates and candidate_scores:
        min_candidate_score = min(candidate_scores.values())
        if btc_score < min_candidate_score * 0.8:
            logger.debug(
                f"BTC score {btc_score:.0f} < {min_candidate_score * 0.8:.0f} "
                f"(min candidate), not injecting"
            )
            return candidates
        logger.debug(
            f"BTC qualified: score={btc_score:.0f} vs min={min_candidate_score:.0f}"
        )
    
    # BTC earned its slot
    candidates.append("BTC")
    
    # Ensure we don't exceed max_candidates
    if len(candidates) > max_candidates:
        # Sort by score before truncating
        try:
            with _disco_scores_cache_lock:
                all_scores = {c: _disco_scores_cache.get(c, 0) for c in candidates}
                candidates.sort(key=lambda c: all_scores.get(c, 0), reverse=True)
        except:
            pass
        candidates = candidates[:max_candidates]
    
    return candidates

def build_candidate_pool_v11_final(max_candidates: int = 12) -> List[str]:
    """
    Discovery V11 Final: Capital Rotation Detector (Production-Ready)
    """
    try:
        snapshot = get_snapshot()
        if not snapshot or not snapshot.mids:
            return get_fallback_candidates(max_candidates)  # P4

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

            # ===== P1: PERSISTENCE SCORE (delta consistency) =====
            # Measure how consistently delta has trended — indicates conviction
            persistence_score = _get_delta_persistence_score(coin, window=3)
            if persistence_score > 0.6:
                base_score += min(10, persistence_score * 15)  # +0 to +10 bonus
                logger.debug(f"  PERSISTENCE {coin}: {persistence_score:.2f} → +{persistence_score*15:.1f}")
            # ===== END P1 PERSISTENCE =====

            # ===== P1: ACCEPTANCE SCORE (price position in range) =====
            # Measure if price closed in upper half (acceptance) or lower half (rejection)
            try:
                master_candles = get_candles(coin, "1h", 1)
                acceptance_score = _get_acceptance_score(coin, master_candles=master_candles)
            except Exception:
                acceptance_score = 0.5  # Default neutral on error
            
            if acceptance_score > 0.65:
                # Strong acceptance — institutional buyers present
                base_score += min(8, (acceptance_score - 0.5) * 20)  # +0 to +8 bonus
                logger.debug(f"  ACCEPTANCE {coin}: {acceptance_score:.2f} → +{(acceptance_score-0.5)*20:.1f}")
            elif acceptance_score < 0.35:
                # Strong rejection — institutional selling
                base_score -= min(5, (0.5 - acceptance_score) * 15)  # -0 to -5 penalty
                logger.debug(f"  REJECTION {coin}: {acceptance_score:.2f} → -{(0.5-acceptance_score)*15:.1f}")
            # ===== END P1 ACCEPTANCE =====

            # ===== LAYER 1: PRESSURE SCORE (early detection bonus) =====
            # Tujuan: tangkep coin SEBELUM breakout/OI meledak. Additive
            # bonus, bukan gate baru — coin yang gagal pressure tetap bisa
            # masuk lewat jalur scoring yang sudah ada (OI/dislocation/dst).
            pressure_score = compute_pressure_score(coin)
            if pressure_score > 0.3:
                base_score += min(12, pressure_score * 20)  # +0 to +12 bonus
                logger.debug(f"  PRESSURE {coin}: {pressure_score:.2f} → +{pressure_score*20:.1f}")
            elif pressure_score < -0.3:
                base_score -= min(6, abs(pressure_score) * 10)  # small penalty, jangan dominan
                logger.debug(f"  PRESSURE_NEG {coin}: {pressure_score:.2f} → -{abs(pressure_score)*10:.1f}")
            # ===== END LAYER 1 PRESSURE =====

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
            return get_fallback_candidates(max_candidates)  # P4

        # ===== NARRATIVE BOOST =====
        all_coins = list(scores.keys())
        for coin, base_score in list(scores.items()):
            boost = get_narrative_boost_v11_direct(coin, all_coins)
            scores[coin] = base_score * (1 + boost)

        # ===== FLOW DIVERSITY PENALTY =====
        scores = apply_flow_diversity_penalty(all_coins, scores)

        # ===== FINAL SORT =====
        final_sorted = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        candidates = [c for c, _ in final_sorted[:max_candidates]]

        # Cache scores for BTC injection
        cache_discovery_scores(scores)

        # P0: Conditional BTC injection (bukan privileged admission)
        candidates = inject_context_coin(
            candidates=candidates,
            snapshot=snapshot,
            max_candidates=max_candidates,
            use_strict_gate=True
        )

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

        update_last_good_candidates(candidates)  # P4
        return candidates

    except Exception as e:
        logger.error(f"build_candidate_pool_v11_final error: {e}")
        return get_fallback_candidates(max_candidates)  # P4

def get_context_set_scored(snapshot: MarketSnapshot, top_n: int = 3) -> List[Tuple[str, float]]:
    """Sama kayak get_context_set() tapi return (coin, score) — dipake buat
    weighted regime vote, bukan cuma daftar nama coin doang."""
    if not snapshot or not snapshot.mids:
        return [(c, 1.0) for c in get_fallback_candidates(top_n)]

    try:
        meta = get_exchange_meta()
        vol_map: Dict[str, float] = {}
        if meta:
            for asset, ctx in zip(meta[0]["universe"], meta[1]):
                vol_map[asset["name"]] = float(ctx.get("dayNtlVlm", 0) or 0)

        max_oi = max(snapshot.oi.values()) if snapshot.oi else 1.0
        max_vol = max(vol_map.values()) if vol_map else 1.0

        scores: Dict[str, float] = {}
        for coin in snapshot.mids.keys():
            oi = snapshot.oi.get(coin, 0)
            vol = vol_map.get(coin, 0)
            oi_score = oi / max(1e-9, max_oi)
            vol_score = vol / max(1e-9, max_vol)
            scores[coin] = 0.6 * oi_score + 0.4 * vol_score

        if not scores:
            return [(c, 1.0) for c in get_fallback_candidates(top_n)]

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_n]
    except Exception as e:
        logger.debug(f"get_context_set_scored error (non-fatal): {e}")
        return [(c, 1.0) for c in get_fallback_candidates(top_n)]


def get_context_set(snapshot: MarketSnapshot, top_n: int = 3) -> List[str]:
    """
    Context coins buat BACA REGIME market — TERPISAH TOTAL dari candidate
    pool, gak pernah displace/masuk candidates.py punya.

    Dipilih DINAMIS by OI + Volume (weighted, sama filosofi dgn
    get_liquidity_efficiency() di atas) — BUKAN hardcoded BTC/ETH/SOL,
    biar tetep nangkep regime dari alt yang lagi rame modal meskipun
    bukan coin besar. Fallback pun ke get_fallback_candidates() (hasil
    discovery dinamis terakhir), bukan daftar major coin statis.
    """
    return [c for c, _ in get_context_set_scored(snapshot, top_n)]


def get_regime_from_context(context_coins: List[str]) -> str:
    """
    DEPRECATED-ish shim (dipertahankan buat backward-compat): majority
    vote polos. Kalau context = [BTC, ETH, alt], BTC/ETH kerap gerak
    bareng (korelasi tinggi) jadi otomatis menang 2-lawan-1 walau si alt
    lagi decouple dan justru bawa info paling baru (indikasi rotasi
    modal). Pakai get_regime_from_context_weighted() buat itu.
    """
    if not context_coins:
        return "UNKNOWN"
    regimes = []
    for coin in context_coins:
        try:
            regime = interpret_regime_v10(coin)
            regimes.append(regime.regime)
        except Exception:
            continue
    if not regimes:
        return "UNKNOWN"
    from collections import Counter
    return Counter(regimes).most_common(1)[0][0]


def get_regime_from_context_weighted(scored_coins: List[Tuple[str, float]]) -> Dict[str, Any]:
    """
    FIX: regime aggregation yang gak keok sama korelasi BTC/ETH.

    Alih-alih majority vote polos (BTC & ETH sering searah -> otomatis
    menang 2-vs-1), di sini tiap coin dikasih weight = context_score
    (dari OI+Volume, udah dinamis) * DECOUPLE_BONUS. DECOUPLE_BONUS
    lebih tinggi buat coin yang regime-nya BEDA dari mayoritas — karena
    itu justru sinyal independen (rotasi modal), bukan noise yang harus
    ditenggelemkan.

    Return dict biar transparan: consensus regime + breakdown per-coin,
    bukan cuma satu label yang nyembunyiin disagreement.
    """
    if not scored_coins:
        return {"regime": "UNKNOWN", "breakdown": {}, "decoupled": []}

    per_coin_regime: Dict[str, str] = {}
    for coin, _ in scored_coins:
        try:
            per_coin_regime[coin] = interpret_regime_v10(coin).regime
        except Exception:
            continue

    if not per_coin_regime:
        return {"regime": "UNKNOWN", "breakdown": {}, "decoupled": []}

    from collections import Counter
    raw_majority = Counter(per_coin_regime.values()).most_common(1)[0][0]

    weighted_votes: Dict[str, float] = {}
    decoupled: List[str] = []
    for coin, score in scored_coins:
        regime = per_coin_regime.get(coin)
        if not regime:
            continue
        weight = max(0.05, score)
        if regime != raw_majority:
            # Coin ini decouple dari mayoritas -> bobotin lebih (2x),
            # biar gak otomatis ketelan sama pasangan yang korelasinya
            # tinggi (mis. BTC & ETH searah, alt beda sendiri).
            weight *= 2.0
            decoupled.append(coin)
        weighted_votes[regime] = weighted_votes.get(regime, 0.0) + weight

    final_regime = max(weighted_votes.items(), key=lambda x: x[1])[0]

    return {
        "regime": final_regime,
        "breakdown": per_coin_regime,
        "decoupled": decoupled,
    }


def build_candidate_pool_v12(max_candidates: int = 12) -> List[str]:
    """
    L0 DISCOVERY V12 — Imbalance Strength + Persistence.
    Menggantikan OI-pattern statis dengan multi-signal acceleration engine
    yang mendeteksi coin sebelum breakout (bukan setelah OI meledak).
    """
    try:
        snapshot = get_snapshot()
        if not snapshot or not snapshot.mids:
            return get_fallback_candidates(max_candidates)  # P4

        # ===== P1: CEK COVERAGE CANDLES TANPA FETCH → putuskan degraded/full =====
        sample_coins = list(snapshot.mids.keys())[:5]
        candle_coverages = [get_candles_coverage(c) for c in sample_coins]
        avg_coverage = sum(candle_coverages) / len(candle_coverages) if candle_coverages else 0.0
        use_degraded = avg_coverage < 0.3 or not can_call_endpoint("candles") or not can_call_api()

        if use_degraded:
            log_warn_aggregated("discovery", f"Discovery degraded: coverage={avg_coverage:.2f}")

        scores: Dict[str, float] = {}
        imbalance_cache: Dict[str, Dict[str, Any]] = {}

        # ===== DISCOVERY GATE: dynamic base gate dari distribusi rolling =====
        # Dihitung SEKALI sebelum loop (bukan per-coin) supaya gak nambah cost.
        # regime default "RANGING" dipakai di sini karena interpret_regime_v10
        # per-coin mahal (fetch candle) — regime-aware yang lebih presisi
        # tetap jalan di get_progressive_gate() di bawah, untuk coin yang
        # sudah lolos gate awal ini.
        _dynamic_base_gate = compute_dynamic_magnitude_gate("RANGING")

        # ===== P5: GATE TELEMETRY — biar ketauan bottleneck di gate mana =====
        _total_scanned = 0
        _reject_oi_min = 0
        _reject_viability = 0
        _reject_imbalance_base = 0
        _fail_magnitude = 0
        _fail_persistence = 0
        _fail_confidence = 0
        _sum_magnitude = 0.0
        _sum_persistence = 0.0
        _sum_confidence = 0.0
        _sample_fail_count = 0
        _reject_imbalance_progressive = 0
        _reject_no_direction = 0
        # ======================================================================

        for coin in list(snapshot.mids.keys()):
            _total_scanned += 1
            oi_usd = snapshot.oi.get(coin, 0)
            if oi_usd < 0.25:
                _reject_oi_min += 1
                continue

            # ===== P2: VIABILITY GATE (OI/Volume) — additive, bukan pengganti =====
            if not is_viable_coin(coin):
                _reject_viability += 1
                continue

            # ===== LAZY-SEED: isi rolling_delta buat coin yang lolos gate
            # awal tapi belum punya history (mayoritas dari 230 coin, cuma
            # top-20-volume yang ke-cover sebelumnya). Scoped ke sini aja
            # (bukan semua 230), auto-backoff kalau l2 lagi cooldown.
            # Throttle kecil pas beneran fetch — cegah burst 144 request
            # beruntun sebelum circuit breaker sempat merespon 429 pertama.
            if ensure_delta_seeded(coin):
                time.sleep(0.03)

            imbalance = compute_imbalance_strength_degraded(coin) if use_degraded else compute_imbalance_strength(coin)
            imbalance_cache[coin] = imbalance

            # Feed rolling distribution dari SEMUA coin yang di-scan (bukan
            # cuma yang lolos), biar gate makin representatif tiap scan.
            update_magnitude_distribution(imbalance["magnitude"])

            if not is_imbalance_valid(imbalance, min_magnitude=_dynamic_base_gate):
                _reject_imbalance_base += 1
                # ===== P5: per-klausa breakdown (gak mutually exclusive) =====
                if imbalance["magnitude"] < _dynamic_base_gate:
                    _fail_magnitude += 1
                if imbalance["persistence"] < 0.4:
                    _fail_persistence += 1
                if imbalance["data_confidence"] < 0.3:
                    _fail_confidence += 1
                _sum_magnitude += imbalance["magnitude"]
                _sum_persistence += imbalance["persistence"]
                _sum_confidence += imbalance["data_confidence"]
                _sample_fail_count += 1
                # ================================================================
                continue

            # Progressive gate: regime kuat + OI persist → gate bisa turun lebih jauh
            # HANYA dipanggil setelah lolos gate awal (bukan semua ~230 coin)
            _effective_gate = get_progressive_gate(coin, base_magnitude=_dynamic_base_gate)
            if not is_imbalance_valid(imbalance, min_magnitude=_effective_gate):
                _reject_imbalance_progressive += 1
                continue

            # Update hot coin tracking
            update_hot_coin(coin)

            direction = get_best_imbalance_direction(imbalance, min_magnitude=_effective_gate)
            if not direction:
                _reject_no_direction += 1
                continue

            base_score = imbalance["magnitude"] * 100

            # Degraded: penalty konservatif karena sinyal cuma proxy, bukan candle asli
            if use_degraded:
                base_score *= 0.85

            spread_comp = imbalance["components"]["spread_comp"]
            if spread_comp > 0.6:
                base_score += 10
            elif spread_comp > 0.4:
                base_score += 5

            if imbalance["persistence"] > 0.7:
                base_score += 10
            elif imbalance["persistence"] > 0.55:
                base_score += 5

            n = get_coin_selection_count(coin)
            base_score *= math.exp(-0.15 * n)

            scores[coin] = max(0, base_score)

        # ===== P5: GATE_DEBUG_V12 =====
        _avg_mag = _sum_magnitude / _sample_fail_count if _sample_fail_count else 0.0
        _avg_pers = _sum_persistence / _sample_fail_count if _sample_fail_count else 0.0
        _avg_conf = _sum_confidence / _sample_fail_count if _sample_fail_count else 0.0
        logger.info(
            f"GATE_DEBUG_V12 total={_total_scanned} "
            f"oi_min={_reject_oi_min} viability={_reject_viability} "
            f"imbalance_base={_reject_imbalance_base} imbalance_progressive={_reject_imbalance_progressive} "
            f"no_direction={_reject_no_direction} pass={len(scores)} "
            f"gate={_dynamic_base_gate:.4f} "
            f"mode={'DEGRADED' if use_degraded else 'FULL'}"
        )
        if _sample_fail_count:
            logger.info(
                f"GATE_DEBUG_V12_DETAIL fail_magnitude={_fail_magnitude} "
                f"fail_persistence={_fail_persistence} fail_confidence={_fail_confidence} "
                f"avg_magnitude={_avg_mag:.4f}(need>={_dynamic_base_gate:.4f}) "
                f"avg_persistence={_avg_pers:.4f}(need>=0.4) "
                f"avg_confidence={_avg_conf:.4f}(need>=0.3)"
            )
        # ================================

        # ===== RETRY RELAXED: sebelum nyerah ke V11, coba lagi pakai cache
        # yang udah ada (ZERO extra API cost) dengan gate direlaksasi 30%. =====
        if not scores and imbalance_cache:
            # FIX: floor lama pake angka mutlak 1e-6 sebagai safety net kalau
            # _dynamic_base_gate kebetulan 0. Sekarang safety net-nya juga
            # relatif (dari p50 distribusi observasi), bukan angka ajaib —
            # gate relaksasi tetap 70% dari base, TETAP ada fungsi filter,
            # cuma gak collapse ke absolute-zero kalau ada edge case.
            _dist_floor = get_magnitude_distribution().get("p50", 0.006) * 0.05
            _relaxed_gate = max(_dist_floor, _dynamic_base_gate * 0.7)
            logger.warning(
                f"[discovery] pass1 empty (gate={_dynamic_base_gate:.5f}), "
                f"retry relaxed gate={_relaxed_gate:.5f} on {len(imbalance_cache)} cached coins"
            )
            for coin, imbalance in imbalance_cache.items():
                if not is_imbalance_valid(imbalance, min_magnitude=_relaxed_gate):
                    continue
                direction = get_best_imbalance_direction(imbalance, min_magnitude=_relaxed_gate)
                if not direction:
                    continue
                base_score = imbalance["magnitude"] * 100
                if use_degraded:
                    base_score *= 0.85
                spread_comp = imbalance["components"]["spread_comp"]
                if spread_comp > 0.6:
                    base_score += 10
                elif spread_comp > 0.4:
                    base_score += 5
                if imbalance["persistence"] > 0.7:
                    base_score += 10
                elif imbalance["persistence"] > 0.55:
                    base_score += 5
                n = get_coin_selection_count(coin)
                base_score *= math.exp(-0.15 * n)
                scores[coin] = max(0, base_score)
            logger.info(f"[discovery] retry relaxed pass={len(scores)}")

        if not scores:
            log_warn_aggregated("discovery", "Discovery V12: no coins passed imbalance gate, fallback to V11")
            return build_candidate_pool_v11_final(max_candidates)

        sorted_coins = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        candidates = [c for c, _ in sorted_coins[:max_candidates]]

        # Cache scores for BTC injection
        cache_discovery_scores(scores)

        # P0: Conditional BTC injection (bukan privileged admission)
        candidates = inject_context_coin(
            candidates=candidates,
            snapshot=snapshot,
            max_candidates=max_candidates,
            use_strict_gate=True
        )

        with _candidate_history_lock:
            for coin in candidates:
                _candidate_history[coin] = _candidate_history.get(coin, 0) + 1

        logger.info(f"🔍 DISCOVERY V12: {len(candidates)} candidates (mode={'DEGRADED' if use_degraded else 'FULL'}, coverage={avg_coverage:.2f})")
        for coin, score in sorted_coins[:8]:
            imp = imbalance_cache.get(coin) or (compute_imbalance_strength_degraded(coin) if use_degraded else compute_imbalance_strength(coin))
            logger.debug(f"  {coin}: score={score:.0f} str={imp['strength']:+.3f} mag={imp['magnitude']:.3f} pers={imp['persistence']:.2f}")

        # ===== 70/30 SPLIT: focus queue (30%) + fresh discoveries (70%) =====
        focus_limit = max(2, int(max_candidates * 0.3))
        focus_coins = get_discovery_focus(limit=focus_limit)
        result: List[str] = []
        seen: set = set()
        for coin in focus_coins:
            if coin not in seen and coin in snapshot.mids:
                result.append(coin)
                seen.add(coin)
        for coin in candidates:
            if coin not in seen and len(result) < max_candidates:
                result.append(coin)
                seen.add(coin)

        update_last_good_candidates(result)  # P4
        return result

    except Exception as e:
        logger.error(f"build_candidate_pool_v12 error: {e}")
        return build_candidate_pool_v11_final(max_candidates)


def build_candidate_pool(max_candidates: int = 12) -> List[str]:
    """Alias aktif — sekarang pakai V12 (imbalance strength engine)."""
    return build_candidate_pool_v12(max_candidates)

def process_candidates_deep(candidates: List[str], snapshot: MarketSnapshot) -> Tuple[List[dict], int]:
    """Stage B: deep analysis untuk kandidat terpilih."""
    # ===== P2: ROTATE SCAN ORDER =====
    candidates = rotate_scan_order(candidates)
    # ==============================================================================

    # ============================================================
    # P0: MARKET CHAOS DETECTION + PRIORITY QUEUE
    # ============================================================
    btc_entropy = compute_market_half_life("BTC")
    market_chaos = btc_entropy > 0.5
    
    # Reset chaos fetch counter per cycle
    global _chaos_fetch_count
    with _chaos_fetch_lock:
        _chaos_fetch_count = 0
    
    # Hitung priority untuk semua candidate (chaos mode)
    priorities = {}
    if market_chaos:
        for coin in candidates:
            priorities[coin] = get_chaos_fetch_priority(coin, snapshot)
        
        # Sort candidates by priority (highest first)
        candidates = sorted(candidates, key=lambda c: priorities.get(c, 0), reverse=True)
        logger.info(f"⚡ CHAOS MODE: {len(candidates)} candidates sorted by priority")

    results = []
    scan_count = 0
    skip_reasons: Dict[str, int] = {}
    last_snapshot_refresh = time.time()
    SNAPSHOT_REFRESH_INTERVAL = 30

    _phase, _readiness = get_engine_phase()
    _cache_grace = get_cache_grace_for_phase(_phase)

    logger.info(f"""
╔════════════════════════════════════════════╗
║ DEEP_START
║ candidates={len(candidates)}
║ api_used={get_api_used()}/{API_BUDGET_PER_CYCLE}
║ snapshot_mids={len(snapshot.mids) if snapshot else 0}
║ phase={_phase} readiness={_readiness['readiness']}% cache_grace={_cache_grace}s
║ market_chaos={market_chaos} btc_entropy={btc_entropy:.2f}
╚════════════════════════════════════════════╝
""")
    
    for i, coin in enumerate(candidates):
        logger.info(f"┌─ STEP_{i}: coin={coin}")

        # ===== P5: STALE SNAPSHOT REFRESH =====
        if time.time() - last_snapshot_refresh > SNAPSHOT_REFRESH_INTERVAL:
            fresh_snapshot = refresh_snapshot()
            last_snapshot_refresh = time.time()
            if fresh_snapshot:
                snapshot = fresh_snapshot
                logger.debug(f"   🔄 snapshot refreshed (age>{SNAPSHOT_REFRESH_INTERVAL}s)")
            else:
                logger.warning(f"   ⚠️ snapshot refresh failed, continuing with stale snapshot")

        # Check 1: API Budget
        budget_val = get_api_used()
        logger.info(f"   budget={budget_val}/{API_BUDGET_PER_CYCLE}")
        if budget_val >= API_BUDGET_PER_CYCLE:
            wait_time = get_seconds_until_budget_frees()
            logger.warning(f"   BUDGET_HIT waiting={wait_time:.1f}s")
            time.sleep(wait_time)
            if get_api_used() >= API_BUDGET_PER_CYCLE:
                logger.warning(f"   SKIP_{coin}_budget_exhausted")
                skip_reasons["budget_exhausted"] = skip_reasons.get("budget_exhausted", 0) + 1
                continue

        # Check 2: Mark price in snapshot
        mark = snapshot.mids.get(coin, 0) if snapshot else 0
        logger.info(f"   mark={mark}")
        if mark == 0:
            logger.warning(f"   SKIP_{coin}_mark_zero")
            skip_reasons["mark_zero"] = skip_reasons.get("mark_zero", 0) + 1
            continue

        # Check 3: Global API cooldown + PER-ENDPOINT health
        api_ok = can_call_api()
        endpoint_ok = can_call_endpoint("candles")
        cooldown_rem = 0.0 if api_ok else api_cooldown_remaining()
        force_live = api_ok and endpoint_ok
        if not endpoint_ok:
            logger.debug(f"   ENDPOINT_COOLDOWN: candles blocked → CACHE_ONLY for {coin}")
            inc_pipeline_counter("api_skip")
        elif cooldown_rem > 0:
            logger.debug(f"   SOFT_COOLDOWN: {cooldown_rem:.1f}s remaining → CACHE_ONLY for {coin}")
            inc_pipeline_counter("api_skip")

        # Check 5: Get candles dengan cache-aware
        cache_age = get_cache_age(coin, "1h", 100)
        never_cached = cache_age >= 999.0
        rank = i + 1

        # ===== P0: DUA JALUR EKSEKUSI =====
        want_live = force_live and (never_cached or should_refresh_live("THESIS", cache_age, rank))
        want_live = want_live or market_chaos  # Chaos mode override (was: undefined should_force_live)

        if want_live:
            candles_1h = get_candles(coin, "1h", 100, force=True)
            if candles_1h:
                data_source = "LIVE"
                mark_api_call("candles")
            else:
                candles_1h = get_candles(coin, "1h", 100, force=False)
                data_source = "CACHE" if candles_1h else "NONE"
        else:
            candles_1h = get_candles(coin, "1h", 100, force=False)
            data_source = "CACHE" if candles_1h else "NONE"

        candles_len = len(candles_1h) if candles_1h else 0
        logger.info(f"   candles={candles_len} source={data_source} cache_age={cache_age:.0f}s")

        # ===== Observability: catat SETIAP percobaan scan =====
        inc_pipeline_counter("scan_total")
        if data_source == "CACHE":
            inc_pipeline_counter("cache_scan")
        elif data_source == "LIVE":
            inc_pipeline_counter("live_scan")

        if not candles_1h:
            logger.warning(f"   SKIP_{coin}_no_candles")
            inc_pipeline_counter("cache_miss")
            skip_reasons["no_candles"] = skip_reasons.get("no_candles", 0) + 1
            continue

        # === SCAN SUCCESS ===
        scan_count += 1
        logger.info(f"   SCAN_OK")
        
        master_candles = {coin: candles_1h}
        alert = check_entry_alert_v10_phase1(coin, mark, master_candles,
                                             rank=rank, cache_age=cache_age, data_source=data_source)
        
        if alert:
            store_obs_memory(
                coin,
                alert.get('score', 0),
                alert.get('area', ''),
                alert.get('direction', '')
            )
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
    # ===== P5: DEEP_SCAN_AUDIT — skip reason breakdown =====
    if skip_reasons:
        logger.info(f"DEEP_SCAN_AUDIT: scanned={scan_count}/{len(candidates)} skip_reasons={skip_reasons}")
    # ========================================================
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

            # CEK COOLDOWN SEBELUM ENTRY CHECK (NON-BLOCKING)
            cooldown_rem = api_cooldown_remaining()
            if cooldown_rem > 0:
                logger.warning(f"⏳ API cooldown {cooldown_rem:.1f}s, skipping {coin}")
                inc_pipeline_counter("api_skip")
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
        # FIX A2+A3: Register TradeManager DULU.
        # Kalau register gagal → skip send_alert → jangan masuk _active_candidates
        # → putus acceptance loop (XRP accept 5x dalam 10 menit)
        _register_ok = False
        try:
            if alert.get("tp_scaled"):
                _sig_id = alert.get("signal_id")
                if not _sig_id:
                    logger.error(f"🔴 ORPHAN PREVENTION: missing signal_id for {alert['coin']}, skip TradeManager register")
                else:
                    # ===== STEP 1: JOURNAL (observability, non-critical) =====
                    try:
                        # Journal already logged universally in execute_decision
                        pass
                    except Exception as je:
                        logger.exception(f"JOURNAL_FAILED {alert['coin']}: {je}")
                        # Journal gagal → lanjut, jangan batalin posisi
                    
                    # ===== STEP 2: TRADE MANAGER REGISTER (critical) =====
                    try:
                        _regime_val = alert.get("regime_interpretation")
                        if isinstance(_regime_val, dict):
                            _regime_val = _regime_val.get("regime", "UNKNOWN")
                        elif _regime_val is None:
                            _regime_val = "UNKNOWN"
                        else:
                            _regime_val = getattr(_regime_val, "regime", "UNKNOWN")
                        # ===== REGISTER_TRACE: staleness audit (observational only, entry math unchanged) =====
                        try:
                            _reg_snap = get_snapshot()
                            _reg_live = _reg_snap.mids.get(alert["coin"]) if _reg_snap else None
                            if _reg_live is not None:
                                _reg_gap = (_reg_live - alert["entry"]) / max(alert["entry"], 1e-9) * 100
                                logger.info(
                                    f"REGISTER_TRACE coin={alert['coin']} signal={_sig_id} "
                                    f"mark_entry={alert['entry']:.4f} live_at_register={_reg_live:.4f} "
                                    f"gap={_reg_gap:+.2f}%"
                                )
                        except Exception as _reg_trace_err:
                            logger.debug(f"REGISTER_TRACE failed for {alert.get('coin')}: {_reg_trace_err}")
                        # ===== END REGISTER_TRACE =====
                        TRADE_MANAGER.add_position(
                            signal_id=_sig_id,
                            coin=alert["coin"],
                            direction=alert["direction"],
                            entry=alert["entry"],
                            sl=alert["sl"],
                            tp_targets=alert["tp_scaled"],
                            entry_time=time.time(),
                            # ===== P4: CORRELATION FIELDS =====
                            score=alert.get("score", 0),
                            size=alert.get("position_size_mult", 1.0),
                            regime=_regime_val,
                            source=alert.get("data_source", "UNKNOWN"),
                            cache_age=alert.get("cache_age", 0.0),
                            # ===== P4.50: CONVICTION + MEM SNAPSHOT =====
                            conviction=alert.get("conviction", 0.0),
                            conviction_mode=alert.get("conviction_mode", "UNKNOWN"),
                            conviction_penalty=alert.get("conviction_penalty", 0.0),
                            mem_outcome_boost=alert.get("mem_outcome_boost", 0.0),
                            mem_cooldown_mult=alert.get("mem_cooldown_mult", 1.0),
                            mem_stability=alert.get("mem_stability"),
                            mem_edge=alert.get("mem_outcome_boost", 0.0),
                            entry_atr_pct=alert.get("atr_pct", 0.0),
                            # ===== HIGH-LEV: pass leverage ke posisi (fix: V10 path was missing this) =====
                            leverage=alert.get("leverage_suggested", 1.0),
                            entry_quality=alert.get("entry_quality", 0.0),  # ← TAMBAHKAN  
                        )
                        _register_ok = True
                        logger.info(f"POSITION_REGISTERED signal={_sig_id} coin={alert['coin']} managed={len(TRADE_MANAGER.positions)}")
                        logger.info(f"✅ TradeManager registered: {alert['coin']} {_sig_id}")
                    except Exception as re:
                        logger.exception(f"REGISTER_FAILED {alert['coin']}: {re}")
                        # Register gagal → BATAL, jangan lanjut ke DB
                        _register_ok = False
                    
                    # ===== STEP 3: DB PERSIST (non-critical, tapi harus konsisten) =====
                    if _register_ok:
                        try:
                            if not PAPER_MODE:
                                # Signal sudah di-persist oleh save_signal_v7 di execute_decision
                                # Ini just verification + logging
                                logger.info(f"DB_PERSIST_CHECK signal={_sig_id}")
                        except Exception as dbe:
                            logger.exception(f"DB_PERSIST_FAILED {alert['coin']}: {dbe}")
                            # DB gagal → posisi di TradeManager, perlu rollback
                            try:
                                if _sig_id in TRADE_MANAGER.positions:
                                    del TRADE_MANAGER.positions[_sig_id]
                                    logger.warning(f"ROLLBACK: removed {_sig_id} from TradeManager")
                                _register_ok = False
                            except:
                                pass
            else:
                _register_ok = True  # discovery/shadow alerts (no tp_scaled) tetap lanjut
        except Exception as e:
            logger.error(f"🔴 TradeManager register FAILED for {alert['coin']}: {e} — skip candidate update")
            # FIX A3: rollback _active_candidates supaya acceptance loop putus
            with _active_candidates_lock:
                _active_candidates.pop(alert["coin"], None)

        if not _register_ok:
            logger.warning(f"SKIP send_alert + candidate update: register gagal untuk {alert['coin']}")
            continue

        send_alert_v10(alert)
    # ===== P1 FIX: CHECK ALL OPEN POSITIONS PERIODICALLY =====
    try:
        snapshot = get_snapshot()
        closed_trades = TRADE_MANAGER.check_all_positions(snapshot)
    
        for trade in closed_trades:
            try:
                # ===== STEP 4A: persist_trade_close (idempotent, DB + journal) =====
                persist_trade_close(trade["signal_id"], trade, source="TRADE_MANAGER_P1")

                # ===== TERMINAL LOG =====
                _p1_lev = trade.get("leverage", 1.0) or 1.0
                _p1_lpnl = trade.get("leveraged_pnl", trade["pnl"] * _p1_lev)
                print(f"📊 CLOSE {trade['coin']} {trade['direction']} | {trade['reason']} | PnL: {_p1_lpnl:+.2f}% (x{_p1_lev:.1f}) | raw: {trade['pnl']:+.2f}%")
                logger.info(f"✅ P1: Trade closed {trade['coin']} | {trade['reason']} | PnL: {_p1_lpnl:+.2f}% (x{_p1_lev:.1f}) raw={trade['pnl']:+.2f}%")
                # Send Telegram alert
                if USER_ID and not PAPER_MODE:
                    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
                    direction_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
                    # ===== P3.1: EXIT EFFICIENCY =====
                    eff = trade.get("exit_eff")
                    eff_label = get_exit_eff_label(eff)
                    eff_line = f"ExitEff: {eff:.0f}% {eff_label}" if eff is not None else "ExitEff: ⚪ N/A"
                    # ===== P4.46: PASSIVE LEARNING CONTEXT =====
                    learning_close = build_learning_context_close(trade["coin"], trade["pnl"], eff)
                    learn_close_line = f"├─ Learn: {' • '.join(learning_close)}\n" if learning_close else ""
                    # ===== END P4.46 =====
                    # ===== P4.50: FEEDBACK BLOCK (burn-in observability) =====
                    fb_early = trade.get("early", 0)
                    fb_shape = trade.get("shape")
                    fb_mature = trade.get("mature")
                    fb_shape_str = f"{fb_shape:.2f}" if fb_shape is not None else "N/A"
                    fb_mature_str = f"{fb_mature:.2f}" if fb_mature is not None else "N/A"
                    feedback_line = (
                        f"├─ 🧠 FEEDBACK: early={fb_early:+d} | shape={fb_shape_str} | mature={fb_mature_str}\n"
                    )
                    # ===== END P4.50 =====
                    _lev_used = trade.get("leverage", 1.0) or 1.0
                    _lev_pnl  = trade.get("leveraged_pnl", trade["pnl"] * _lev_used)
                    msg = f"{emoji} <b>CLOSE</b> {trade['coin']} [{direction_emoji} {trade['direction']}]\n"
                    msg += f"├─ Reason: {trade['reason']}\n"
                    msg += f"├─ Entry: {fmt_price(trade['entry'])} → Exit: {fmt_price(trade['exit'])}\n"
                    msg += f"├─ PnL: {_lev_pnl:+.2f}% (x{_lev_used:.1f}) | raw: {trade['pnl']:+.2f}%\n"
                    msg += f"├─ MFE: {trade['mfe']:+.2f}% | MAE: {trade['mae']:+.2f}%\n"
                    msg += f"├─ {eff_line}\n"
                    msg += feedback_line
                    msg += learn_close_line
                    msg += f"└─ Time: {trade['duration_minutes']:.0f}m | TP Levels: {trade['tp_levels_captured']}/3"
            except Exception as e:
                logger.error(f"P1: Error processing closed trade {trade.get('signal_id', 'unknown')}: {e}")
    except Exception as e:
        logger.warning(f"P1: Position check error: {e}")

def state_engine_update_v11():
    """State Engine V11: 2-stage architecture.
    Stage A (cheap discovery, no candles) -> Stage B (deep analysis, API budget).
    Menggantikan scan top-N-volume statis dari V10 dengan 3-bucket candidate pool
    (alpha + OI flow + narrative) plus per-endpoint API budget supaya gak kena 429."""
    reset_funnel()
    reset_pipeline_counter()  # FIX A1: reset tiap cycle biar Thesis gak bisa > Observed

    global _last_funnel_log
    if time.time() - _last_funnel_log > 300:
        funnel_summary = get_funnel_summary()
        logger.info(f"FUNNEL_SNAPSHOT {funnel_summary}")
        _last_funnel_log = time.time()

    context = get_context_snapshot("BTC")
    snap = refresh_snapshot()
    compute_market_breath_v10()

    # Sanitize sector/narrative maps dari live snapshot (throttled 5 menit)
    if snap:
        sanitize_maps_from_snapshot(snap)

    with _oi_lock:
        oi_hist_coins = len(_oi_history)
        oi_hist_btc = len(_oi_history.get("BTC", []))
    log_oi_summary()

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

    # P4.55: snapshot results for /entry (no-arg) to reuse without recompute
    set_last_pipeline_results(alerts)

    pipe = get_pipeline_metrics()
    log_funnel_compact()
    log_engine_summary_compact()

    # ===== FLUSH CYCLE LOGS =====
    flush_cycle_logs()

    # ===== SEND ALERTS + REGISTER (sama seperti V10) =====
    for alert in alerts:
        # FIX: register dulu (sama seperti V10), alert belakangan
        _register_ok = False
        try:
            if alert.get("tp_scaled"):
                _sig_id = alert.get("signal_id")
                if not _sig_id:
                    logger.error(f"🔴 ORPHAN PREVENTION: missing signal_id for {alert['coin']}, skip TradeManager register")
                else:
                    _regime_val = alert.get("regime_interpretation")
                    if isinstance(_regime_val, dict):
                        _regime_val = _regime_val.get("regime", "UNKNOWN")
                    elif _regime_val is None:
                        _regime_val = "UNKNOWN"
                    else:
                        _regime_val = getattr(_regime_val, "regime", "UNKNOWN")
                    # ===== REGISTER_TRACE: staleness audit (observational only, entry math unchanged) =====
                    try:
                        _reg_snap = get_snapshot()
                        _reg_live = _reg_snap.mids.get(alert["coin"]) if _reg_snap else None
                        if _reg_live is not None:
                            _reg_gap = (_reg_live - alert["entry"]) / max(alert["entry"], 1e-9) * 100
                            logger.info(
                                f"REGISTER_TRACE coin={alert['coin']} signal={_sig_id} "
                                f"mark_entry={alert['entry']:.4f} live_at_register={_reg_live:.4f} "
                                f"gap={_reg_gap:+.2f}%"
                            )
                    except Exception as _reg_trace_err:
                        logger.debug(f"REGISTER_TRACE failed for {alert.get('coin')}: {_reg_trace_err}")
                    # ===== END REGISTER_TRACE =====
                    TRADE_MANAGER.add_position(
                        signal_id=_sig_id,
                        coin=alert["coin"],
                        direction=alert["direction"],
                        entry=alert["entry"],
                        sl=alert["sl"],
                        tp_targets=alert["tp_scaled"],
                        entry_time=time.time(),
                        # ===== P4: CORRELATION FIELDS =====
                        score=alert.get("score", 0),
                        size=alert.get("position_size_mult", 1.0),
                        regime=_regime_val,
                        source=alert.get("data_source", "UNKNOWN"),
                        cache_age=alert.get("cache_age", 0.0),
                        # ===== P4.50: CONVICTION + MEM SNAPSHOT =====
                        conviction=alert.get("conviction", 0.0),
                        conviction_mode=alert.get("conviction_mode", "UNKNOWN"),
                        conviction_penalty=alert.get("conviction_penalty", 0.0),
                        mem_outcome_boost=alert.get("mem_outcome_boost", 0.0),
                        mem_cooldown_mult=alert.get("mem_cooldown_mult", 1.0),
                        mem_stability=alert.get("mem_stability"),
                        mem_edge=alert.get("mem_outcome_boost", 0.0),
                        entry_atr_pct=alert.get("atr_pct", 0.0),
                        # ===== HIGH-LEV: pass suggested leverage ke posisi =====
                        leverage=alert.get("leverage_suggested", 1.0),
                        entry_quality=alert.get("entry_quality", 0.0), 
                    )
                    _register_ok = True
                    logger.info(f"✅ V11 TradeManager registered: {alert['coin']} {_sig_id}")
            else:
                _register_ok = True  # discovery/shadow alerts tetap lanjut
        except Exception as e:
            logger.error(f"🔴 V11 register FAILED for {alert['coin']}: {e}")
            # Rollback _active_candidates supaya acceptance loop putus
            with _active_candidates_lock:
                _active_candidates.pop(alert["coin"], None)

        if not _register_ok:
            logger.warning(f"V11: SKIP send_alert: register gagal untuk {alert['coin']}")
            continue

        send_alert_v10(alert)
    # ===== CHECK OPEN POSITIONS (sama seperti V10) =====
    try:
        snap_for_check = get_snapshot()
        closed_trades = TRADE_MANAGER.check_all_positions(snap_for_check)
        for trade in closed_trades:
            try:
                # ===== STEP 4B: persist_trade_close (idempotent, DB + journal) =====
                persist_trade_close(trade["signal_id"], trade, source="TRADE_MANAGER_V11")

                # ===== TERMINAL LOG =====
                _t_lev = trade.get("leverage", 1.0) or 1.0
                _t_lpnl = trade.get("leveraged_pnl", trade["pnl"] * _t_lev)
                print(f"🍗 CLOSE {trade['coin']} {trade['direction']} | {trade['reason']} | PnL: {_t_lpnl:+.2f}% (x{_t_lev:.1f}) | raw: {trade['pnl']:+.2f}%")
                logger.info(f"✅ V11: Trade closed {trade['coin']} | {trade['reason']} | PnL: {_t_lpnl:+.2f}% (x{_t_lev:.1f}) raw={trade['pnl']:+.2f}%")
                
                if USER_ID and not PAPER_MODE:
                    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
                    direction_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
                    # ===== P3.1: EXIT EFFICIENCY =====
                    eff = trade.get("exit_eff")
                    eff_label = get_exit_eff_label(eff)
                    eff_line = f"ExitEff: {eff:.0f}% {eff_label}" if eff is not None else "ExitEff: ⚪ N/A"
                    # ===== P4.46: PASSIVE LEARNING CONTEXT =====
                    learning_close = build_learning_context_close(trade["coin"], trade["pnl"], eff)
                    learn_close_line = f"├─ Learn: {' • '.join(learning_close)}\n" if learning_close else ""
                    # ===== END P4.46 =====
                    # ===== P4.50: FEEDBACK BLOCK (burn-in observability) =====
                    fb_early = trade.get("early", 0)
                    fb_shape = trade.get("shape")
                    fb_mature = trade.get("mature")
                    fb_shape_str = f"{fb_shape:.2f}" if fb_shape is not None else "N/A"
                    fb_mature_str = f"{fb_mature:.2f}" if fb_mature is not None else "N/A"
                    feedback_line = (
                        f"├─ 🧠 FEEDBACK: early={fb_early:+d} | shape={fb_shape_str} | mature={fb_mature_str}\n"
                    )
                    # ===== END P4.50 =====
                    msg = f"{emoji} <b>CLOSE</b> {trade['coin']} [{direction_emoji} {trade['direction']}]\n"
                    msg += f"├─ Reason: {trade['reason']}\n"
                    msg += f"├─ Entry: {fmt_price(trade['entry'])} → Exit: {fmt_price(trade['exit'])}\n"
                    msg += f"├─ PnL: {_t_lpnl:+.2f}% (x{_t_lev:.1f}) | raw: {trade['pnl']:+.2f}%\n"
                    msg += f"├─ MFE: {trade['mfe']:+.2f}% | MAE: {trade['mae']:+.2f}%\n"
                    msg += f"├─ {eff_line}\n"
                    msg += feedback_line
                    msg += learn_close_line
                    msg += f"└─ Time: {trade['duration_minutes']:.0f}m | TP Levels: {trade['tp_levels_captured']}/3"
                    try:
                        tg_send(USER_ID, msg, parse_mode='HTML')
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
            audit_inventory()
            # === P2: TM_STATUS LOG ===
            try:
                log_trade_manager_summary()
            except Exception as tm_err:
                logger.debug(f"TM_STATUS error: {tm_err}")

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
            
            # === EXIT ENGINE HEALTH ===
            try:
                exit_health = TRADE_MANAGER.get_exit_health()
                if exit_health["is_stalled"]:
                    logger.warning(
                        f"EXIT_ENGINE_STALLED: "
                        f"closed_last_hour={exit_health['closed_last_hour']} "
                        f"stale_gt_6h={exit_health['stale_gt_6h']} "
                        f"stale_gt_24h={exit_health['stale_gt_24h']}"
                    )
            except Exception as eh_err:
                logger.debug(f"Exit health check error: {eh_err}")
            
            logger.info(f"State engine V11 cycle done, next in {interval}s")
        except Exception:
            logger.exception("STATE_ENGINE_CRASH — cycle skipped, thread alive")
        RUNTIME.wait(interval)

def state_engine_update_v12():
    """State Engine V12: Discovery v12 (imbalance strength) + Entry Window."""
    # ===== P0: Clear discovery cache setiap cycle =====
    clear_discovery_cache()
    # ===== DYNAMIC AGGRESSION: update heat + trigger burst + cleanup memory =====
    update_market_heat()
    check_and_trigger_burst()
    cleanup_discovery_focus()
    cleanup_hot_coins()
    cleanup_obs_memory()

    cycle_start = time.time()

    reset_funnel()
    reset_pipeline_counter()

    context = get_context_snapshot("BTC")
    snap = refresh_snapshot()
    compute_market_breath_v10()

    if snap:
        sanitize_maps_from_snapshot(snap)
        # P2: update adaptive candle TTL dari exchange_meta, sekali per cycle (murah)
        meta = get_exchange_meta()
        if meta:
            update_candle_ttl_from_meta(meta)

        # ===== CONTEXT REGIME (info/observability aja) =====
        # get_context_set_scored() milih coin dinamis by OI+Volume (bukan
        # hardcode BTC/ETH/SOL). Regime dibaca pakai versi WEIGHTED yang
        # gak keok korelasi BTC/ETH — coin yang decouple dari mayoritas
        # dikasih bobot lebih (sinyal rotasi modal), bukan ketelan vote.
        # Hasilnya CUMA dipake buat baca regime — TIDAK filter/displace
        # candidate pool.
        try:
            _ctx_scored = get_context_set_scored(snap, top_n=3)
            _ctx_result = get_regime_from_context_weighted(_ctx_scored)
            _ctx_coins = [c for c, _ in _ctx_scored]
            _decoupled_note = f" decoupled={_ctx_result['decoupled']}" if _ctx_result["decoupled"] else ""
            logger.info(
                f"📊 REGIME from context {_ctx_coins}: {_ctx_result['regime']} "
                f"breakdown={_ctx_result['breakdown']}{_decoupled_note}"
            )
        except Exception as e:
            logger.debug(f"context regime read error (non-fatal): {e}")

    with _oi_lock:
        oi_hist_btc = len(_oi_history.get("BTC", []))
    log_oi_summary()
    logger.debug(f"OI HIST BTC={oi_hist_btc}")

    with _api_window_lock:
        _api_window.clear()

    # Reaction engine (sama seperti V11)
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

    # ===== P3: CEK FLOW STALL (pakai metrics dari cycle SEBELUMNYA, sebelum di-reset) =====
    # Catatan: reset_pipeline_counter() sudah kepanggil di awal function ini, jadi
    # check_flow_stall_and_rescue() di titik ini akan lihat metrics kosong pada cycle
    # pertama setelah reset — itu bukan bug, cuma berarti P3 efektif mulai mendeteksi
    # stall dari cycle KEDUA setelah reset counter jalan. Ini konsisten dengan pola
    # existing reset_pipeline_counter() yang juga dipanggil di awal V11.
    check_flow_stall_and_rescue()
    mode = get_engine_mode()

    # ===== LIFECYCLE: PHASE + READINESS (log eksplisit tiap cycle) =====
    phase, readiness = get_engine_phase()
    logger.info(
        f"🧬 PHASE={phase} readiness={readiness['readiness']}% "
        f"(cache={readiness['cache_health']*100:.0f}% history={readiness['history_depth']*100:.0f}% "
        f"api={readiness['api_health']*100:.0f}%) age={readiness['age_s']}s"
    )

    # ===== STAGE A: WATCHLIST (Attention-based, phase-aware fallback) =====
    snapshot = get_snapshot()

    if mode == "RESCUE":
        candidates = build_candidate_pool_v12(max_candidates=4)
        logger.info("🔄 RESCUE MODE: discovery dikurangi ke 4 kandidat")
    elif snapshot:
        watchlist = _attention_engine_v2.get_watchlist(snapshot, max_watch=80)
        avg_conf = np.mean([_attention_engine_v2.get_confidence(c) for c in watchlist]) if watchlist else 0.0

        phase_limit = get_candidate_limit_for_phase(phase)
        if avg_conf < 0.5:
            booster = build_candidate_pool_v12(max_candidates=phase_limit)
            for coin in booster:
                if coin not in watchlist:
                    watchlist.append(coin)
            logger.info(f"Attention confidence rendah ({avg_conf:.2f}), tambah {len(booster)} booster dari Discovery (phase={phase})")
        elif phase != "LIVE":
            logger.info(f"🧬 {phase}: watchlist attention aktif, {len(watchlist)} coin (readiness={readiness['readiness']}%)")

        candidates = watchlist
    else:
        phase_limit = get_candidate_limit_for_phase(phase)
        candidates = build_candidate_pool_v12(max_candidates=phase_limit)
        if phase != "LIVE":
            logger.info(f"🧬 {phase}: discovery dibatasi ke {phase_limit} kandidat (readiness={readiness['readiness']}%)")

    if not candidates:
        logger.warning("V12: No candidates, using fallback")
        candidates = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "LINK", "UNI", "AAVE"]

    # ===== STAGE B: DEEP ANALYSIS (max 12, RESCUE → 4) =====
    if mode == "RESCUE":
        candidates_for_deep = candidates[:4]
        logger.info("🔄 RESCUE MODE: deep analysis dikurangi ke 4 kandidat")
    else:
        candidates_for_deep = candidates[:12]

    alerts, scan_count = process_candidates_deep(candidates_for_deep, snapshot)

    set_last_pipeline_results(alerts)

    # Record cycle runtime untuk adaptive interval
    record_cycle_runtime(time.time() - cycle_start)

    log_funnel_compact()
    log_engine_summary_compact()
    flush_cycle_logs()

    # P4: flush warning aggregation + P0: log endpoint health telemetry
    flush_warn_summary()
    logger.info(f"📊 API HEALTH: {get_endpoint_health_summary()}")

    # Add high-score alerts ke focus queue (bot gak amnesia antar cycle)
    for alert in alerts:
        if alert.get('score', 0) > 60:
            add_discovery_focus(
                alert['coin'],
                f"{alert.get('area', 'event')} score={alert.get('score', 0):.0f}",
                alert.get('score', 0)
            )

    # Send alerts + register ke TradeManager
    # ===== LIFECYCLE GATE: BOOT phase = observe only, gak execute entry =====
    # Kenapa: di BOOT, cache/history masih terlalu tipis buat dipercaya
    # sebagai basis entry (readiness<35 atau age<60s). Observasi/scan/thesis
    # tetap jalan penuh (data tetap kekumpul), cuma eksekusi posisi ditahan
    # sampai minimal masuk WARMUP. Ini BUKAN pause total — cuma nahan bagian
    # paling berisiko (buka posisi pakai data yang belum matang).
    if phase == "BOOT" and alerts:
        blocked_coins = [a["coin"] for a in alerts if a.get("tp_scaled")]
        if blocked_coins:
            log_warn_aggregated("boot_gate", f"BOOT phase: entry ditahan untuk {blocked_coins} (readiness={readiness['readiness']}%)")
        alerts = [a for a in alerts if not a.get("tp_scaled")]

    for alert in alerts:
        _register_ok = False
        try:
            if alert.get("tp_scaled"):
                _sig_id = alert.get("signal_id")
                if not _sig_id:
                    logger.error(f"ORPHAN PREVENTION: missing signal_id for {alert['coin']}")
                else:
                    _regime_val = alert.get("regime_interpretation")
                    if isinstance(_regime_val, dict):
                        _regime_val = _regime_val.get("regime", "UNKNOWN")
                    elif _regime_val is None:
                        _regime_val = "UNKNOWN"
                    else:
                        _regime_val = getattr(_regime_val, "regime", "UNKNOWN")

                    # ===== REGISTER_TRACE: staleness audit (observational only, entry math unchanged) =====
                    try:
                        _reg_snap = get_snapshot()
                        _reg_live = _reg_snap.mids.get(alert["coin"]) if _reg_snap else None
                        if _reg_live is not None:
                            _reg_gap = (_reg_live - alert["entry"]) / max(alert["entry"], 1e-9) * 100
                            logger.info(
                                f"REGISTER_TRACE coin={alert['coin']} signal={_sig_id} "
                                f"mark_entry={alert['entry']:.4f} live_at_register={_reg_live:.4f} "
                                f"gap={_reg_gap:+.2f}%"
                            )
                    except Exception as _reg_trace_err:
                        logger.debug(f"REGISTER_TRACE failed for {alert.get('coin')}: {_reg_trace_err}")
                    # ===== END REGISTER_TRACE =====

                    TRADE_MANAGER.add_position(
                        signal_id=_sig_id,
                        coin=alert["coin"],
                        direction=alert["direction"],
                        entry=alert["entry"],
                        sl=alert["sl"],
                        tp_targets=alert["tp_scaled"],
                        entry_time=time.time(),
                        score=alert.get("score", 0),
                        size=alert.get("position_size_mult", 1.0),
                        regime=_regime_val,
                        source=alert.get("data_source", "UNKNOWN"),
                        cache_age=alert.get("cache_age", 0.0),
                        conviction=alert.get("conviction", 0.0),
                        conviction_mode=alert.get("conviction_mode", "UNKNOWN"),
                        conviction_penalty=alert.get("conviction_penalty", 0.0),
                        mem_outcome_boost=alert.get("mem_outcome_boost", 0.0),
                        mem_cooldown_mult=alert.get("mem_cooldown_mult", 1.0),
                        mem_stability=alert.get("mem_stability"),
                        mem_edge=alert.get("mem_outcome_boost", 0.0),
                        entry_atr_pct=alert.get("atr_pct", 0.0),
                        leverage=alert.get("leverage_suggested", 1.0),
                        entry_quality=alert.get("entry_quality", 0.0),
                    )
                    _register_ok = True
                    logger.info(f"✅ V12 TradeManager registered: {alert['coin']} {_sig_id}")
            else:
                _register_ok = True
        except Exception as e:
            logger.error(f"V12 register FAILED for {alert['coin']}: {e}")
            with _active_candidates_lock:
                _active_candidates.pop(alert["coin"], None)

        if not _register_ok:
            logger.warning(f"V12: SKIP send_alert: register gagal untuk {alert['coin']}")
            continue

        send_alert_v10(alert)

    # Check open positions
    try:
        snap_for_check = get_snapshot()
        closed_trades = TRADE_MANAGER.check_all_positions(snap_for_check)
        for trade in closed_trades:
            try:
                persist_trade_close(trade["signal_id"], trade, source="TRADE_MANAGER_V12")
                _t_lev = trade.get("leverage", 1.0) or 1.0
                _t_lpnl = trade.get("leveraged_pnl", trade["pnl"] * _t_lev)
                logger.info(f"✅ V12: Trade closed {trade['coin']} | {trade['reason']} | PnL: {_t_lpnl:+.2f}%")
                if USER_ID and not PAPER_MODE:
                    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
                    direction_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
                    eff = trade.get("exit_eff")
                    eff_label = get_exit_eff_label(eff)
                    eff_line = f"ExitEff: {eff:.0f}% {eff_label}" if eff is not None else "ExitEff: ⚪ N/A"
                    msg = f"{emoji} <b>CLOSE</b> {trade['coin']} [{direction_emoji} {trade['direction']}]\n"
                    msg += f"├─ Reason: {trade['reason']}\n"
                    msg += f"├─ Entry: {fmt_price(trade['entry'])} → Exit: {fmt_price(trade['exit'])}\n"
                    msg += f"├─ PnL: {_t_lpnl:+.2f}% (x{_t_lev:.1f}) | raw: {trade['pnl']:+.2f}%\n"
                    msg += f"├─ MFE: {trade['mfe']:+.2f}% | MAE: {trade['mae']:+.2f}%\n"
                    msg += f"├─ {eff_line}\n"
                    msg += f"└─ Time: {trade['duration_minutes']:.0f}m | TP Levels: {trade['tp_levels_captured']}/3"
                    tg_send(USER_ID, msg, parse_mode='HTML')
            except Exception as e:
                logger.error(f"V12: Error processing closed trade {trade.get('signal_id', 'unknown')}: {e}")
    except Exception as e:
        logger.warning(f"V12: Position check error: {e}")


def scheduled_state_engine_v12():
    """State Engine V12 scheduler."""
    while RUNTIME.is_running():
        interval = get_adaptive_interval()  # heat-driven, bukan fix
        try:
            if not RUNTIME.is_alert_enabled():
                RUNTIME.wait(60)
                continue

            audit_trade_state()
            audit_inventory()
            try:
                log_trade_manager_summary()
            except Exception as tm_err:
                logger.debug(f"TM_STATUS error: {tm_err}")

            cleaned = emergency_lifecycle_cleanup()
            if cleaned > 0:
                logger.warning(f"🔄 Cleaned {cleaned} stale trades")

            state_engine_update_v12()

            # Interval sudah ditentukan di awal (get_adaptive_interval()),
            # tidak perlu volatility override lagi (heat udah mencakup itu)
            try:
                exit_health = TRADE_MANAGER.get_exit_health()
                if exit_health["is_stalled"]:
                    logger.warning(f"EXIT_ENGINE_STALLED: {exit_health}")
            except Exception as eh_err:
                logger.debug(f"Exit health check error: {eh_err}")

            logger.info(f"State engine V12 cycle done, next in {interval}s (heat={get_market_heat():.2f})")
        except Exception:
            logger.exception("STATE_ENGINE_CRASH — cycle skipped, thread alive")
        RUNTIME.wait(interval)


def scheduled_trigger_engine_v7():
    """Trigger engine — update delta/OI/volume every 3 seconds."""
    while RUNTIME.is_running():
        trigger_engine_update_v7()
        RUNTIME.wait(TUNABLE.get("TRIGGER_ENGINE_INTERVAL_ACTIVE", 3))


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
        cleanup_obs_memory()
        cleanup_hot_coins()
        cleanup_discovery_focus()
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

# ============================================================
# P5: CACHE WARMER — background thread yang prefetch candle top coin
# (by OI) supaya pas discovery/thesis jalan, cache-nya udah anget dan
# gak perlu nunggu live fetch di jalur kritis. Cache-only, non-blocking,
# selalu respect can_call_api()/can_call_endpoint("candles").
# ============================================================
def cache_warmer_loop():
    while RUNTIME.is_running():
        try:
            if not can_call_api() or not can_call_endpoint("candles"):
                time.sleep(30)
                continue

            snapshot = get_snapshot()
            if not snapshot or not snapshot.oi:
                time.sleep(60)
                continue

            top_coins = sorted(snapshot.oi.items(), key=lambda x: x[1], reverse=True)[:10]
            top_coins = [c for c, _ in top_coins if c in snapshot.mids]

            if not top_coins:
                time.sleep(60)
                continue

            for coin in top_coins:
                key = f"candles_{coin}_1h_100"
                cached = CACHE.get_with_ts(key)
                is_stale = cached is None or (time.time() - cached[1] > 300)
                if is_stale:
                    _EVAL_EXECUTOR.submit(get_candles, coin, "1h", 100, None, False)
                time.sleep(0.05)  # jangan hammer executor/API

            time.sleep(120)  # tiap 2 menit

        except Exception as e:
            logger.debug(f"Cache warmer error: {e}")
            time.sleep(60)


def fetch_candles_master(coins: List[str], timeframe: str, limit: int = 80) -> Dict[str, List[dict]]:
    def fetch_one(coin):
        # FIX: cek API budget + endpoint health sebelum hit exchange
        if not can_call_api() or not can_call_endpoint("candles"):
            logger.warning(f"fetch_candles_master: candles cooldown, skip {coin}")
            return coin, []
        for attempt in range(3):
            try:
                end_ms = int(time.time() * 1000)
                tf_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
                interval = tf_ms.get(timeframe, 3600000)
                start_ms = end_ms - limit * interval
                candles = info.candles_snapshot(coin, timeframe, start_ms, end_ms)
                mark_api_call("candles")  # FIX: track di sliding window budget
                mark_endpoint_success("candles")
                return coin, (candles if candles else [])
            except Exception as e:
                if "429" in str(e) or "rate limit" in str(e).lower():
                    trigger_api_cooldown(8)  # P4.53: 15→8
                    mark_endpoint_failure("candles")
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

# ===== ASYNC TELEGRAM SENDER =====
# Semua bot.send_message di-route lewat queue ini supaya
# scan thread tidak blocking kalau Telegram slow/timeout.
_telegram_queue: Queue = Queue(maxsize=200)

def _tg_sender_loop():
    """Background thread: drain _telegram_queue dan kirim ke Telegram."""
    while RUNTIME.is_running():
        try:
            item = _telegram_queue.get(timeout=1)
            if item is None:
                continue
            chat_id, text, kwargs = item
            try:
                bot.send_message(chat_id, text, **kwargs)
            except Exception as e:
                logger.error(f"Telegram send failed (chat={chat_id}): {e}")
        except Empty:
            continue
        except Exception as e:
            logger.error(f"_tg_sender_loop error: {e}")

def tg_send(chat_id: int, text: str, **kwargs):
    """Non-blocking Telegram send. Enqueue ke _telegram_queue."""
    if not kwargs.get("parse_mode"):
        kwargs["parse_mode"] = "HTML"
    kwargs.setdefault("timeout", 30)
    try:
        _telegram_queue.put_nowait((chat_id, text, kwargs))
    except Exception:
        # Queue penuh → fallback blocking (rare)
        try:
            bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            logger.error(f"tg_send fallback failed: {e}")

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

    # ===== FIX (entry alert honesty): ZONE-BASED ENTRY (market order, harga bisa udah move) =====
    _zone_low = alert.get("entry_zone_low")
    _zone_high = alert.get("entry_zone_high")
    if _zone_low and _zone_high:
        try:
            _live_snap = get_snapshot()
            _cur = _live_snap.mids.get(alert["coin"], alert["entry"]) if _live_snap else alert["entry"]
        except Exception:
            _cur = alert["entry"]
        _zone_ok = "✅" if _zone_low <= _cur <= _zone_high else "❌"
        entry_str = f"{fmt_price(_zone_low)}-{fmt_price(_zone_high)}{_zone_ok}"
    else:
        entry_str = entry

    # ===== L2: LEVERAGE COMPONENTS (EQ + suggested leverage) =====
    lev_info = alert.get("leverage_info", {}) or {}
    lev_suggested = lev_info.get("suggested", alert.get("leverage_suggested", 1.0))
    eq = lev_info.get("eq", alert.get("entry_quality", 50.0))

    if eq >= 80:
        eq_emoji = "🚀"
    elif eq >= 60:
        eq_emoji = "⚡"
    elif eq >= 40:
        eq_emoji = "⚖️"
    else:
        eq_emoji = "🐢"

    # ===== FIX: GET FLOW STATUS FIELDS =====
    price_ok = alert.get("price_ok", False)
    flow_ok = alert.get("flow_ok", False)
    pos_ok = alert.get("pos_ok", False)
    
    # Flow status indicator
    flow_status = "✅" if flow_ok else ("🔄" if price_ok else "❌")
    price_status = "✅" if price_ok else "❌"
    pos_status = "✅" if pos_ok else "❌"
    # ===== END FIX =====
    
    # Top 2 reasons
    reasons = ", ".join(alert.get("positive_evidence", [])[:2])
    neg_list = alert.get("negative_evidence", "").split(",") if alert.get("negative_evidence") else []
    neg = ", ".join([x.strip() for x in neg_list[:2] if x.strip()]) if neg_list else "none"
    
    # Rank info
    rank_text = alert.get("rank", "No rank")
    
    # Decision stability (inverted entropy)
    decision_stability = 100 - alert.get("entropy_decision", 0)

    # ===== P4.46: PASSIVE LEARNING CONTEXT =====
    learning = build_learning_context_open(alert["coin"])
    learn_line = f"├─ Learn: {' • '.join(learning)}\n" if learning else ""
    # ===== END P4.46 =====

    # ===== P4.50: CONVICTION + MEM BLOCK (burn-in observability) =====
    conv_score = alert.get("conviction", 0.0)
    conv_penalty = alert.get("conviction_penalty", 0.0)
    conv_mode = alert.get("conviction_mode", "UNKNOWN")
    conv_penalty_str = f"-{conv_penalty:.0f}" if conv_penalty != 0 else "0"
    conv_line = (
        f"├─ 🧠 CONV: score={conv_score:.0f} | penalty={conv_penalty_str} | "
        f"mode={conv_mode} | size={size:.2f}\n"
    )

    mem_edge = alert.get("mem_outcome_boost", 0.0)
    mem_cooldown = alert.get("mem_cooldown_mult", 1.0)
    mem_stab = alert.get("mem_stability")
    mem_stab_str = f"{mem_stab:.1f}" if mem_stab is not None else "N/A"
    # FIX: f"{0.0:+.0f}" -> "+0" yang gak bermakna (menyiratkan "sedikit
    # positif" padahal literally nol). Tampilkan angka polos kalau nilainya
    # nol, tanda +/- cuma muncul kalau memang ada boost/penalty nyata.
    mem_edge_str = f"{mem_edge:+.0f}" if mem_edge != 0 else "0"
    mem_line = (
        f"├─ 🧬 MEM: outcome={mem_edge_str} | cooldown_mult={mem_cooldown:.2f} | "
        f"edge={mem_edge_str} | stability={mem_stab_str}\n"
    )
    # ===== END P4.50 =====

    compact = (
        f"{direction_emoji} <b>{alert['coin']} {alert['direction']}</b>\n"
        f"├─ Score: {score} {label} | RR: 1:{rr:.1f}\n"
        f"├─ EQ: {eq:.0f} {eq_emoji} | Size: {size:.2f}x | Lev: {lev_suggested:.1f}x\n"
        f"├─ Flow: {flow_status} | Price: {price_status} | Pos: {pos_status}\n"
        f"├─ Entry {entry_str} | SL {sl} | TP {tp}\n"
        f"├─ Why: +{reasons} | –{neg}\n"
        f"├─ Rank: {rank_text} | Stability: {decision_stability}%\n"
        f"{conv_line}"
        f"{mem_line}"
        f"{learn_line}"
        f"└─ /entry {alert['coin']}"
    )
    return compact


# ============================================================
# P4.50 — GLOBAL CONTEXT (bukan BTC hardcoded)
# ============================================================
_global_context_coin = "BTC"  # fallback
_global_context_lock = threading.RLock()


def get_global_context() -> ContextSnapshot:
    """Get context for the most relevant coin (top OI or BTC)."""
    global _global_context_coin
    
    # Try to find top OI coin
    snapshot = get_snapshot()
    if snapshot and snapshot.oi:
        # Filter non-BTC top coins
        candidates = [c for c in snapshot.oi.keys() if c in snapshot.mids]
        if candidates:
            # Sort by OI descending, but prefer BTC if it's healthy
            if "BTC" in snapshot.oi and snapshot.oi["BTC"] > 0:
                _global_context_coin = "BTC"
            else:
                _global_context_coin = max(candidates, key=lambda c: snapshot.oi.get(c, 0))
    
    return get_context_snapshot(_global_context_coin)


def get_mode_emoji(mode):
    """Get emoji for execution mode."""
    mode_emoji_map = {
        "NORMAL": "🟢",
        "PREPARE": "🟡",
        "CAUTIOUS": "🟡",
        "AGGRESSIVE": "🟢",
        "DEFENSIVE": "🔴",
    }
    return mode_emoji_map.get(mode.value if hasattr(mode, 'value') else mode, "⚪")


def fmt_price(price: float) -> str:
    """Format price nicely."""
    if price > 1:
        return f"{price:.2f}"
    elif price > 0.01:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"


def get_rejection_reason_counts(window_minutes: int = 30) -> Dict[str, int]:
    """Get rejection reason counts from last N minutes."""
    try:
        conn = db_connect()
        c = conn.cursor()
        cutoff = int(time.time()) - window_minutes * 60
        c.execute('''SELECT rejection_reason, COUNT(*) as cnt
                    FROM journal WHERE timestamp > ? AND rejection_reason IS NOT NULL
                    GROUP BY rejection_reason
                    ORDER BY cnt DESC''', (cutoff,))
        rows = c.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows} if rows else {}
    except:
        return {}


# ============================================================
# P4.50 — COMPACT ALERTS
# ============================================================

def send_alert_v10_compact(alert: dict):
    """Compact OPEN alert — 6 lines, includes SL."""
    if not RUNTIME.is_alert_enabled():
        return
    
    arrow = "🟢" if alert["direction"] == "LONG" else "🔴"
    score = alert["score"]
    score_emoji = "🟢" if score >= 70 else "🟡" if score >= 55 else "⚪"
    rr = alert.get("rr", 0)
    
    # Evidence (positive)
    ev = ", ".join(alert.get("positive_evidence", [])[:2]) or "none"
    
    # Calculate SL/TP distances
    entry = alert["entry"]
    sl = alert["sl"]
    tp = alert["tp"]
    sl_pct = abs(sl - entry) / max(entry, 0.01) * 100
    tp_pct = abs(tp - entry) / max(entry, 0.01) * 100
    lev_suggested = alert.get("leverage_suggested")
    lev_cap = alert.get("leverage_native_cap")
    lev_line = f"Lev {lev_suggested:.1f}x (cap {lev_cap}x)\n" if lev_suggested else ""
    
    text = f"""{arrow} <b>OPEN {alert['coin']} {alert['direction']}</b>
━━━━━━━━━━

Score {score} {score_emoji}
RR 1:{rr:.1f}
{lev_line}
Entry {fmt_price(entry)}
SL {fmt_price(sl)} ({sl_pct:.2f}%)
TP {fmt_price(tp)} ({tp_pct:.2f}%)

Why
+{ev}
"""
    tg_send(USER_ID, text, parse_mode='HTML')
    if CHANNEL_ID:
        tg_send(CHANNEL_ID, text, parse_mode='HTML')


def send_close_alert_compact(trade: dict):
    """Compact CLOSE alert — includes TP level, ExitEff, First Profit Time."""
    emoji = "🟢" if trade["pnl"] > 0 else "🔴"
    dir_emoji = "🔼" if trade["direction"] == "LONG" else "🔽"
    
    # Exit efficiency label
    eff = trade.get("exit_eff")
    eff_label = "🚀" if eff and eff >= 70 else "⚖️" if eff and eff >= 40 else "🐢"
    eff_line = f"{eff_label} {eff:.0f}%" if eff is not None else "N/A"
    
    # TP level captured
    tp_captured = trade.get("tp_levels_captured", 0)
    tp_line = f"{tp_captured}/3" if tp_captured > 0 else "0/3"

    # ===== L4: FIRST PROFIT TIME =====
    fpt = trade.get("fpt")
    if fpt is not None:
        fpt_str = f"{fpt:.0f}s" if fpt < 60 else f"{fpt/60:.1f}m"
    else:
        fpt_str = "N/A"
    
    reason_map = {
        "tp3_hit": "✅ TP3",
        "trailing_sl": "⚠️ Trail SL",
        "timeout_tp2": "⌚ Timeout",
        "sl_hit": "🛑 SL",
        "stale_expiry": "⏰ Stale",
    }
    reason_display = reason_map.get(trade["reason"], trade["reason"])
    
    text = f"""{emoji} <b>CLOSE {trade['coin']}</b> [{dir_emoji}]
━━━━━━━━━━

{trade['pnl']:+.2f}%

Exit {eff_line}
TP {tp_line}
1st Profit {fpt_str}

{reason_display}
{trade['duration_minutes']:.0f}m
"""
    tg_send(USER_ID, text, parse_mode='HTML')
    if CHANNEL_ID:
        tg_send(CHANNEL_ID, text, parse_mode='HTML')

def send_alert_v10(alert: dict):
    if not RUNTIME.is_alert_enabled():
        return

    is_real_position = bool(alert.get("tp_scaled"))

    value, label = compute_alert_value(alert)
    if value < TUNABLE["ALERT_VALUE_MIN"]:
        if is_real_position:
            logger.warning(f"⚠️ COMPACT ALERT SKIPPED (value={value:.0f} < {TUNABLE['ALERT_VALUE_MIN']}) untuk POSISI REAL {alert['coin']}")
        else:
            logger.debug(f"Alert value too low ({value:.0f}), skip {alert['coin']}")
        return

    alert["value_label"] = label
    alert["value_score"] = value
    
    level = _get_alert_level(alert)

    coin = alert["coin"]
    now = time.time()

    with _alert_history_lock:
        if coin not in _alert_history:
            _alert_history[coin] = deque(maxlen=5)
        while _alert_history[coin] and now - _alert_history[coin][0] > TUNABLE["ALERT_HISTORY_WINDOW"]:
            _alert_history[coin].popleft()

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
            if is_real_position:
                logger.warning(f"⚠️ COMPACT ALERT SKIPPED (cooldown) untuk POSISI REAL {coin}")
            else:
                logger.warning(f"COOLDOWN_SKIP {coin} remaining_secs={cooldown - (now - _last_alert[coin]):.0f}")
            return
        _last_alert[coin] = now

    with _alert_history_lock:
        _alert_history[coin].append(now)

    if level == 0:
        if is_real_position:
            logger.warning(f"⚠️ COMPACT ALERT SKIPPED (level=0/silent) untuk POSISI REAL {coin}")
        else:
            logger.debug(f"📦 Alert {coin} level 0 (silent)")
        return

    regime = alert.get('regime_interpretation')
    ob_reaction = alert.get('ob_reaction')
    fvg_quality = alert.get('fvg_quality')
    context_memory = alert.get('context_memory')
    cal_conf = alert.get('confidence_calibrated', alert.get('score', 50))
    cal_samples = alert.get('calibration_samples', 0)

    compact = _build_compact_alert(alert)

    if level == 1:
        tg_send(USER_ID, compact)
        if CHANNEL_ID:
            tg_send(CHANNEL_ID, compact)
        return

    tg_send(USER_ID, compact)
    if CHANNEL_ID:
        tg_send(CHANNEL_ID, compact)

    # ===== BUILD FULL TEXT (FIXED: NO EMOJI AT START OF STRING) =====
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
    ebar_d = "█"*int(e_data/10)+"░"*(10-int(e_data/10)) if e_data else "░░░░░░░░░░"
    ebar_m = "█"*int(e_market/10)+"░"*(10-int(e_market/10)) if e_market else "░░░░░░░░░░"
    ebar_dec = "█"*int(e_decision/10)+"░"*(10-int(e_decision/10)) if e_decision else "░░░░░░░░░░"

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

    regime_text = ""
    if regime:
        regime_text = (
            f"📈 *Regime*: {regime.regime}\n"
            f"├─ Strength: {regime.strength:.0f}% | Stability: {regime.stability:.0f}%\n"
            f"├─ Confidence: {regime.confidence:.0f}% | Age: {regime.age_minutes:.0f}m\n"
            f"└─ Transition: {regime.transition_prob:.0f}% {regime.transition_direction}"
        )

    ob_text = ""
    if ob_reaction and ob_reaction.touch_count > 0:
        ob_text = (
            f"📊 *OB Reaction*\n"
            f"├─ Touches: {ob_reaction.touch_count}\n"
            f"├─ Max reaction: {ob_reaction.max_reaction_strength:.0f}%\n"
            f"├─ Followthrough: {ob_reaction.followthrough:.0f}%\n"
            f"└─ Confidence: {ob_reaction.confidence:.0f}%"
        )

    fvg_text = ""
    if fvg_quality and fvg_quality.quality_score > 0:
        fvg_text = (
            f"📊 *FVG Quality*\n"
            f"├─ Size: {fvg_quality.size:.0f}%\n"
            f"├─ Fill: {fvg_quality.fill_ratio:.0%} (speed: {fvg_quality.fill_speed:.0f}%)\n"
            f"├─ Reaction: {fvg_quality.reaction:.0f}%\n"
            f"└─ Quality: {fvg_quality.quality_score:.0f}"
        )

    ctx_text = ""
    if context_memory and context_memory.snapshots:
        ctx_text = (
            f"🧠 *Context Memory*\n"
            f"├─ Regimes: {' → '.join(context_memory.get_regime_sequence()[-5:])}\n"
            f"├─ Shock trend: {context_memory.get_trend('shock_score')}\n"
            f"├─ Volatility trend: {context_memory.get_volatility_trend()}\n"
            f"└─ Transitioning: {'Yes' if context_memory.is_transitioning() else 'No'}"
        )

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

    # ===== L1: ENTRY QUALITY EMOJI =====
    eq = alert.get("entry_quality", 0)
    if eq >= 80:
        eq_emoji = "🚀"
    elif eq >= 60:
        eq_emoji = "⚡"
    elif eq >= 40:
        eq_emoji = "⚖️"
    else:
        eq_emoji = "🐢"

    sl_pct = abs(alert['entry'] - alert['sl']) / max(alert['entry'], 0.01) * 100
    tp_pct = abs(alert['tp'] - alert['entry']) / max(alert['entry'], 0.01) * 100

    # ===== FIX (entry alert honesty): ZONE-BASED ENTRY DISPLAY =====
    # Sebelumnya alert['entry'] nunjukin harga optimal saat sinyal dibentuk
    # (mark), yang udah stale by the time notif ini nyampe user — bikin
    # bingung karena beda sama harga eksekusi aktual di OPEN notif.
    # Sekarang: tampilkan Entry Zone (bukan titik tunggal), + status
    # apakah current price masih valid di dalam zone tsb.
    _zone_low = alert.get("entry_zone_low")
    _zone_high = alert.get("entry_zone_high")
    try:
        _live_snap = get_snapshot()
        _entry_current = _live_snap.mids.get(coin, alert['entry']) if _live_snap else alert['entry']
    except Exception:
        _entry_current = alert['entry']

    if _zone_low and _zone_high:
        _zone_valid = _zone_low <= _entry_current <= _zone_high
        _zone_mark = "✅" if _zone_valid else "❌"
        entry_line = (
            f"├─ Entry: {fmt_price(_zone_low)} - {fmt_price(_zone_high)} {_zone_mark}\n"
            f"├─ Current: {fmt_price(_entry_current)}\n"
        )
    else:
        entry_line = f"├─ Entry: {fmt_price(_entry_current)}\n"
    
    text = f"""{arrow} {mode_emoji} *V10 ALERT* • {coin} {intent_emoji}
━━━━━━━━━━━━━━━━━━━━━━
{label} | {mode_color_v10} Mode: {mode_emoji_v10} {mode_v10}
Context age: {context_age:.1f}s {context_warn}
{event_info} {reaction_info}

🧠 *Belief*: {belief_emoji} {alert.get('belief_state', 'SEEKING').upper()} | ⏱️ Pressure: {pressure_emoji} {alert.get('time_pressure', 'normal').upper()}
📊 Intent Success: {success_emoji} {intent_success:.0f}%

📊 *Setup Quality*
├─ Score: {alert['score']} | {alert['label']}
├─ EQ: {alert.get('entry_quality', 0):.1f} {eq_emoji}
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
{entry_line}├─ SL: {fmt_price(alert['sl'])} ({sl_pct:.2f}%)
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
    tg_send(USER_ID, text)
    if CHANNEL_ID:
        tg_send(CHANNEL_ID, text)
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
    """Orientasi — 6 lines, hapus semua noise."""
    ctx = get_global_context()
    mode, _ = get_execution_mode_v10(ctx, get_current_reaction(), 0.5, get_event_risk_adjustment())
    pipe = get_pipeline_metrics()
    
    status = "🔴 OFFLINE" if not RUNTIME.is_running() else "🟢 ONLINE"
    mode_emoji = get_mode_emoji(mode)
    
    text = f"""🚀 <b>HL BOT V10</b>
━━━━━━━━━━

{status}
{mode_emoji} Mode: {mode.value if hasattr(mode, 'value') else mode}
📊 Regime: {ctx.regime}

📊 Runtime
Obs: {pipe.get('obs', 0)}
Exec: {pipe.get('execute_pass', 0)}

🎯 Next:
→ /status
→ /entry BTC
→ /analytics
"""
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['help'])
def cmd_help(m):
    text = """📖 <b>HL BOT V10</b>
━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>Daily</b>
/status    → What to do now
/dashboard → Engine health & performance
/entry     → Best setups (or /entry BTC)

🔧 <b>Advanced</b>
/debug BTC → Deep dive
/audit     → System health

📊 <b>Reference</b>
/analytics → Performance summary

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
    """Control tower — sekarang bot ngapain."""
    ctx = get_global_context()
    breath = compute_market_breath_v10()
    mode, _ = get_execution_mode_v10(ctx, get_current_reaction(), 0.5, get_event_risk_adjustment())
    pipe = get_pipeline_metrics()
    exec_rate = pipe.get('execute_pass', 0) / max(1, pipe.get('check', 1)) * 100
    
    # Determine action
    if ctx.shock_score > 60:
        action = "👀 WAIT"
        reason = f"Stress {ctx.shock_score:.0f}%"
    elif ctx.transition_prob > 60:
        action = "🔧 PREPARE"
        reason = f"Transition {ctx.transition_prob:.0f}%"
    elif ctx.shock_score < 30 and breath.get('bull', 0) < 0.3:
        action = "🧊 HOLD"
        reason = "Low breath"
    elif pipe.get('execute_pass', 0) == 0 and pipe.get('check', 0) > 20:
        action = "🔍 SCANNING"
        reason = "Looking for setup"
    else:
        action = "👀 WAIT"
        reason = ctx.regime
    
    text = f"""🧠 <b>CONTROL TOWER</b>
━━━━━━━━━━

State:
🟢 ONLINE
{mode.value if hasattr(mode, 'value') else mode}

Market:
Stress {ctx.shock_score:.0f}%
Breath {breath.get('bull', 0)*100:.0f}%

Engine:
Obs {pipe.get('obs', 0)}
Exec {pipe.get('execute_pass', 0)} ({exec_rate:.1f}%)

Action:
{action}
{reason}
"""
    bot.reply_to(m, text, parse_mode='HTML')

# ===== P4.W13: STALE AUDIT (Observability Only) =====
def get_stale_audit() -> Dict[str, Any]:
    """Audit stale positions (observability only)."""
    try:
        now = time.time()
        with TRADE_MANAGER._lock:
            open_positions = [p for p in TRADE_MANAGER.positions.values() if p.status == "OPEN"]
            ages = [now - p.entry_time for p in open_positions]
        
        n = len(ages)
        if n == 0:
            return {"n": 0, "avg_age": 0, "gt_24h": 0, "gt_48h": 0, "risk": "LOW"}
        
        gt_24h = sum(1 for a in ages if a > 24*3600)
        gt_48h = sum(1 for a in ages if a > 48*3600)
        avg_age = sum(ages) / n / 3600
        
        if gt_48h > 0:
            risk = "🔴 HIGH"
        elif gt_24h > 5:
            risk = "🟡 MEDIUM"
        else:
            risk = "🟢 LOW"
        
        return {
            "n": n,
            "avg_age": round(avg_age, 1),
            "gt_24h": gt_24h,
            "gt_48h": gt_48h,
            "risk": risk,
        }
    except Exception as e:
        logger.error(f"get_stale_audit error: {e}")
        return {"error": str(e)}

@bot.message_handler(commands=['dashboard'])
def cmd_dashboard(m):
    """Health ringkas — 3 sections only."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    # Today stats
    today = get_analytics()
    pipe = get_pipeline_metrics()
    cal = get_calibration_engine().get_drift_indicator() if hasattr(get_calibration_engine(), 'get_drift_indicator') else {"calibration_active": False}
    
    # Flow
    obs = pipe.get('obs', 0)
    conf = pipe.get('confidence', 0)
    exec_count = pipe.get('execute_pass', 0)
    
    obs_rate = (obs / max(1, pipe.get('check', 1))) * 100
    conf_rate = (conf / max(1, obs)) * 100 if obs > 0 else 0
    exec_rate = (exec_count / max(1, conf)) * 100 if conf > 0 else 0
    
    # Quality — jujur (Recent vs Lifetime)
    recent_wr = get_recent_win_rate(20)
    lifetime_wr = today.get('win_rate', 0)
    
    # Issue detection
    issues = []
    if exec_count == 0 and obs > 20:
        issues.append("⚠️ Exec blocked")
    if today.get('total_pnl', 0) < -10:
        issues.append("⚠️ Negative PnL")
    if recent_wr < 30 and today.get('total', 0) > 10:
        issues.append("⚠️ Low WR")
    if obs_rate < 10 and pipe.get('check', 0) > 50:
        issues.append("⚠️ Too selective")
    
    issue_line = "\n".join(issues) if issues else "✅ No issues"
    
    # P4.53 — data source ratio
    scan_total = pipe.get('scan_total', 0)
    cache_scan = pipe.get('cache_scan', 0)
    live_scan = pipe.get('live_scan', 0)
    cache_pct = (cache_scan / max(1, scan_total)) * 100 if scan_total > 0 else 0
    live_pct = 100 - cache_pct if scan_total > 0 else 0

    text = f"""🧠 <b>DASHBOARD</b>
━━━━━━━━━━

📊 Today
Signals {today.get('total', 0)}
WR {today.get('win_rate', 0):.0f}%
PnL {today.get('total_pnl', 0):+.1f}%

📊 Flow
Obs {obs_rate:.0f}%
Conf {conf_rate:.0f}%
Exec {exec_rate:.0f}%

📊 Data Source
Total {scan_total}
Live {live_scan} ({live_pct:.0f}%)
Cache {cache_scan} ({cache_pct:.0f}%)

📊 Quality
Recent: {recent_wr:.0f}%
Lifetime: {lifetime_wr:.0f}%
Calibration {'🟢' if cal.get('calibration_active', False) else '⏳'}

{issue_line}
"""
    bot.reply_to(m, text, parse_mode='HTML')

def get_recent_win_rate(n: int = 50) -> float:
    """P4.3 — Win rate dari N trade terakhir."""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT outcome FROM signals
            WHERE evaluated = 1 AND outcome IS NOT NULL
            ORDER BY exit_time DESC LIMIT ?
        """, (n,))
        rows = c.fetchall()
        conn.close()
        if not rows:
            return 0.0
        wins = sum(1 for r in rows if r[0] in ("TP_HIT", "PARTIAL_WIN"))
        return wins / len(rows) * 100
    except:
        return 0.0

def get_lifetime_win_rate() -> float:
    """P4.3 — Overall lifetime win rate."""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END)
            FROM signals WHERE evaluated = 1 AND outcome IS NOT NULL
        """)
        row = c.fetchone()
        conn.close()
        if not row or not row[0]:
            return 0.0
        return row[1] / row[0] * 100
    except:
        return 0.0


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


@bot.message_handler(commands=['audit'])
def cmd_audit(m):
    """P4.4a backend (audit_outcome_authority/batch_audit_outcomes) sudah ada
    sejak lama tapi gak pernah punya command handler — /audit selalu silent
    fail karena Telegram gak punya handler buat command ini sama sekali."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    parts = m.text.split()
    limit = 200
    if len(parts) >= 2:
        try:
            limit = max(10, min(1000, int(parts[1])))
        except ValueError:
            pass

    bot.reply_to(m, f"🔍 Auditing last {limit} evaluated signals...")

    try:
        result = batch_audit_outcomes(limit=limit)
    except Exception as e:
        bot.reply_to(m, f"🔴 Audit gagal: {e}")
        return

    if result.get("error"):
        bot.reply_to(m, f"🔴 Audit error: {result['error']}")
        return

    total = result["total"]
    mismatches = result["mismatches"]
    mismatch_pct = result["mismatch_pct"]

    if total == 0:
        bot.reply_to(m, "📭 Belum ada signal evaluated untuk diaudit.")
        return

    text = (
        "🔍 <b>OUTCOME AUDIT</b>\n"
        "━━━━━━━━━━\n\n"
        f"Total diperiksa: {total}\n"
        f"Mismatch: {mismatches} ({mismatch_pct:.1f}%)\n\n"
    )

    if mismatch_pct < 5:
        text += "✅ Outcome authority sehat — stored vs computed konsisten.\n"
    elif mismatch_pct < 20:
        text += "🟡 Ada mismatch, masih dalam batas wajar.\n"
    else:
        text += "🔴 Mismatch tinggi — kemungkinan TP target di DB (single-target lama) gak sinkron dengan TP3-boosted yang sebenarnya dieksekusi (lihat catatan P4.56). Bukan berarti eksekusi salah, tapi audit ini belum tau cara baca tp_scaled.\n"

    # Tampilkan beberapa contoh mismatch buat investigasi cepat
    sample_mismatches = [r for r in result["results"] if not r["match"]][:5]
    if sample_mismatches:
        text += "\n<b>Contoh mismatch:</b>\n"
        for r in sample_mismatches:
            text += (
                f"├─ {r['signal_id']}: stored={r['stored']} → computed={r['computed']} "
                f"(pnl stored={r['stored_pnl']:.2f}%, computed={r['computed_pnl']:.2f}%)\n"
            )

    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['ws'])
def cmd_ws(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    connected = bool(_ws_manager and _ws_manager.is_connected)
    status = "🟢 CONNECTED" if connected else "🔴 DISCONNECTED"
    mode = "WS" if connected else "REST (fallback)"
    mid_n = len(_ws_mid.get_all_prices()) if _ws_mid else 0
    ob_n = len(_ws_ob._subscribed_coins) if _ws_ob else 0
    trades_n = len(_ws_trades._subscribed_coins) if _ws_trades else 0

    candle_lines = []
    if _ws_candle and _ws_ob:
        for coin in sorted(_ws_ob._subscribed_coins):
            live_tfs = [tf for tf in ("5m", "1h") if _ws_candle.has_sufficient_history(coin, tf)]
            if live_tfs:
                candle_lines.append(f"  {coin}: {'/'.join(live_tfs)}")
    candle_status = "\n".join(candle_lines) if candle_lines else "  (belum ada history cukup — nunggu candle close pertama)"

    text = (
        f"🌐 <b>WEBSOCKET</b>\n"
        f"━━━━━━━━━━\n"
        f"Status: {status}\n"
        f"Mode: {mode}\n"
        f"Mid: {'✅' if _ws_mid and _ws_mid.is_connected else '❌'} ({mid_n} coins)\n"
        f"OB: {'✅' if _ws_ob and _ws_ob.is_connected else '❌'} ({ob_n} subscribed)\n"
        f"Trades: {'✅' if _ws_trades and _ws_trades.is_connected else '❌'} ({trades_n} subscribed)\n"
        f"Live Candles (5m/1h, watchlist only):\n{candle_status}\n"
        f"Semua coin di luar watchlist tetap REST.\n"
    )
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['cachehealth'])
def cmd_cachehealth(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    try:
        lines = []
        with open(os.path.join(LOG_DIR, "engine.log"), "r") as f:
            for line in f:
                if "VELOCITY_TRACE" in line:
                    lines.append(line)
        lines = lines[-500:]
    except Exception as e:
        bot.reply_to(m, f"Error reading log: {e}")
        return

    if not lines:
        bot.reply_to(m, "No VELOCITY_TRACE data yet.")
        return

    cache_ages = []
    sources = {}
    stages = {}
    reasons = {}
    exec_cache_ages = []
    thesis_cache_ages = []
    obs_cache_ages = []

    for line in lines:
        parts = line.split()
        try:
            source = next((p for p in parts if p.startswith("source=")), "source=UNKNOWN").split("=")[1]
            stage = next((p for p in parts if p.startswith("stage=")), "stage=UNKNOWN").split("=")[1]
            reason = next((p for p in parts if p.startswith("reason=")), "reason=").split("=", 1)[1] if "reason=" in line else ""
            ca_raw = next((p for p in parts if p.startswith("cache_age=")), "cache_age=999s").split("=")[1].rstrip("s")
            cache_age = float(ca_raw)

            cache_ages.append(cache_age)
            sources[source] = sources.get(source, 0) + 1
            stages[stage] = stages.get(stage, 0) + 1
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
            if stage == "EXECUTE":
                exec_cache_ages.append(cache_age)
            elif stage == "THESIS":
                thesis_cache_ages.append(cache_age)
            elif stage in ("OBSERVE", "OBS"):
                obs_cache_ages.append(cache_age)
        except Exception:
            continue

    if not cache_ages:
        bot.reply_to(m, "No valid VELOCITY_TRACE entries.")
        return

    import statistics
    avg_age = statistics.mean(cache_ages)
    avg_exec = statistics.mean(exec_cache_ages) if exec_cache_ages else 0
    avg_thesis = statistics.mean(thesis_cache_ages) if thesis_cache_ages else 0
    avg_obs = statistics.mean(obs_cache_ages) if obs_cache_ages else 0

    total = sum(sources.values())
    hit_rate = (sources.get("CACHE", 0) / total * 100) if total > 0 else 0
    recovered_skip = sum(v for k, v in reasons.items() if "cooldown" in k or "api" in k)

    gate_skip_count = stages.get("GATE", 0) + stages.get("SKIP", 0)

    text = (
        f"📊 <b>CACHE HEALTH</b> (last {len(cache_ages)} events)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 <b>Source</b>\n"
        f"├─ CACHE: {sources.get('CACHE', 0)} ({hit_rate:.0f}%)\n"
        f"├─ LIVE: {sources.get('LIVE', 0)}\n"
        f"└─ UNKNOWN: {sources.get('UNKNOWN', 0)}\n\n"
        f"⏱️ <b>Cache Age by Stage</b>\n"
        f"├─ OBS: {avg_obs:.0f}s\n"
        f"├─ THESIS: {avg_thesis:.0f}s\n"
        f"└─ EXEC: {avg_exec:.0f}s\n\n"
        f"📋 <b>Stages</b>\n"
        f"├─ EXECUTE: {stages.get('EXECUTE', 0)}\n"
        f"├─ REJECT: {stages.get('REJECT', 0)}\n"
        f"├─ SHADOW: {stages.get('SHADOW', 0)}\n"
        f"└─ GATE/SKIP: {gate_skip_count}\n\n"
        f"🔄 <b>Recovery</b>\n"
        f"├─ Avg Cache Age: {avg_age:.0f}s\n"
        f"├─ Hit Rate: {hit_rate:.0f}%\n"
        f"└─ Recovered Skip: {recovered_skip}\n\n"
        f"💡 <b>Interpretasi</b>\n"
        f"{'✅ EXEC cache under 30s — healthy' if avg_exec < 30 else f'🟡 EXEC cache {avg_exec:.0f}s — pertimbangin tuning'}\n"
        f"{'✅ Thesis cache under 180s — ok' if avg_thesis < 180 else f'🟡 Thesis cache {avg_thesis:.0f}s — ok for structure'}\n"
        f"{'✅ Hit rate above 60% — cache working' if hit_rate > 60 else f'🟡 Hit rate {hit_rate:.0f}% — room for improvement'}\n"
        f"{'✅ UNKNOWN=0 — source lengkap' if sources.get('UNKNOWN', 0) == 0 else '🔴 UNKNOWN above 0 — cek metadata injection'}"
    )

    # ===== P4.54: REAL-TIME PIPELINE SOURCE RATIO (this cycle) =====
    pipe = get_pipeline_metrics()
    p_scan_total = pipe.get('scan_total', 0)
    p_live = pipe.get('live_scan', 0)
    p_cache = pipe.get('cache_scan', 0)
    p_miss = pipe.get('cache_miss', 0)
    p_api_skip = pipe.get('api_skip', 0)
    p_too_old = pipe.get('cache_too_old_skip', 0)
    p_denom = max(1, p_scan_total + p_miss)
    live_pct = p_live / p_denom * 100
    cache_pct = p_cache / p_denom * 100
    miss_pct = p_miss / p_denom * 100

    text += (
        f"\n\n📊 <b>PIPELINE (this run, P4.54)</b>\n"
        f"├─ LIVE: {p_live} ({live_pct:.0f}%)\n"
        f"├─ CACHE: {p_cache} ({cache_pct:.0f}%)\n"
        f"├─ NO DATA: {p_miss} ({miss_pct:.0f}%)\n"
        f"├─ Cache too old (skipped): {p_too_old}\n"
        f"└─ Cooldown-forced cache: {p_api_skip}\n"
        f"{'🔴 NO DATA above 20% — check connectivity/cache TTL' if miss_pct > 20 else '✅ NO DATA under 20%'}"
    )

    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['velocity'])
def cmd_velocity(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    try:
        lines = []
        with open(os.path.join(LOG_DIR, "engine.log"), "r") as f:
            for line in f:
                if "VELOCITY_TRACE" in line:
                    lines.append(line)
        lines = lines[-100:]
    except Exception as e:
        bot.reply_to(m, f"Error reading log: {e}")
        return

    if not lines:
        bot.reply_to(m, "No VELOCITY_TRACE data yet.")
        return

    scores = []
    thresholds = []
    gaps = []
    decisions = {"EXECUTE": 0, "REJECT": 0, "SHADOW": 0}
    regimes = {}
    size_mult = []
    pos_gates = {"PASS": 0, "CLEAR": 0}
    source_counts = {}
    stage_ages: dict = {}  # stage -> list of cache_age floats
    reasons: dict = {}

    skipped_no_score = 0
    regime_no_context = 0
    for line in lines:
        parts = line.split()
        try:
            # P4.0: score/threshold/gap can be the literal string "None"
            # (emitted by emit_velocity_skip for gates that fire before
            # scoring happens). Only float-parse and append when present —
            # this is what actually fixes "Median Score = 0", since
            # previously these lines either crashed float("None") and got
            # silently dropped by the except below, or (pre-P4.0) never
            # carried real context at all.
            score_raw = next(p for p in parts if p.startswith("score=")).split("=")[1]
            threshold_raw = next(p for p in parts if p.startswith("threshold=")).split("=")[1]
            gap_raw = next(p for p in parts if p.startswith("gap=")).split("=")[1]
            decision = next(p for p in parts if p.startswith("decision=")).split("=")[1]
            regime = next(p for p in parts if p.startswith("regime=")).split("=")[1]
            size = float(next(p for p in parts if p.startswith("size=")).split("=")[1])
            pos = next(p for p in parts if p.startswith("pos=")).split("=")[1]
            stage = next((p for p in parts if p.startswith("stage=")), "stage=UNKNOWN").split("=")[1]
            ca_raw = next((p for p in parts if p.startswith("cache_age=")), "cache_age=999s").split("=")[1].rstrip("s")
            cache_age_val = float(ca_raw)
            source = next((p for p in parts if p.startswith("source=")), "source=UNKNOWN").split("=")[1]
            reason = next((p for p in parts if p.startswith("reason=")), "reason=").split("=", 1)[1] if "reason=" in line else ""

            has_score = score_raw != "None"
            has_threshold = threshold_raw != "None"
            if has_score:
                scores.append(float(score_raw))
            if has_threshold:
                thresholds.append(float(threshold_raw))
            if gap_raw != "None":
                gaps.append(float(gap_raw))
            if not has_score:
                skipped_no_score += 1

            decisions[decision] = decisions.get(decision, 0) + 1
            # P4.1: regime can be the literal string "None" — telemetry had
            # no regime to report at this stage (gate fired before
            # observe_market/build_thesis ever ran). Track it separately
            # from real regimes so it shows as its own N/A line instead of
            # polluting the top-3 regime ranking with a fake "regime".
            if regime == "None":
                regime_no_context += 1
            else:
                regimes[regime] = regimes.get(regime, 0) + 1
            size_mult.append(size)
            pos_gates[pos] = pos_gates.get(pos, 0) + 1
            source_counts[source] = source_counts.get(source, 0) + 1
            stage_ages.setdefault(stage, []).append(cache_age_val)
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        except Exception:
            continue

    if not scores:
        bot.reply_to(m, "No valid VELOCITY_TRACE entries found.")
        return

    import statistics
    median_score = statistics.median(scores)
    median_threshold = statistics.median(thresholds) if thresholds else 0.0
    median_gap = statistics.median(gaps) if gaps else 0.0
    avg_size = sum(size_mult) / len(size_mult) if size_mult else 0.0

    top_regimes = sorted(regimes.items(), key=lambda x: x[1], reverse=True)[:3]
    regime_lines = "\n".join(f"├─ {r}: {c}" for r, c in top_regimes)
    if regime_no_context:
        regime_lines += f"\n└─ N/A (no context yet): {regime_no_context}"

    source_lines = "\n".join(f"├─ {s}: {c}" for s, c in sorted(source_counts.items()))
    stage_age_lines = "\n".join(
        f"├─ {s}: avg {sum(ages)/len(ages):.0f}s"
        for s, ages in sorted(stage_ages.items()) if ages
    )

    top_reasons = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:3]
    reason_lines = "\n".join(f"├─ {r}: {c}" for r, c in top_reasons) if top_reasons else "└─ (none)"

    score_note = "🔴 Score floor? Median under 68 — mungkin stuck." if median_score < 68 else "✅ Score sehat."
    gap_note = "🟡 Threshold terlalu tinggi? Median gap under -3." if median_gap < -3 else "✅ Threshold balanced."
    gate_note = "🟢 Position gate aktif (PASS tinggi)" if pos_gates.get("PASS", 0) > pos_gates.get("CLEAR", 0) * 0.3 else "✅ Position gate bersih."

    text = (
        f"📊 <b>VELOCITY</b> (last {len(lines)} entries, {len(scores)} scored)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📈 <b>Score</b>\n"
        f"├─ Median: {median_score:.1f}\n"
        f"├─ Median Threshold: {median_threshold:.1f}\n"
        f"└─ Median Gap: {median_gap:+.1f}\n\n"
        f"🎯 <b>Decisions</b>\n"
        f"├─ EXECUTE: {decisions['EXECUTE']}\n"
        f"├─ REJECT: {decisions['REJECT']}\n"
        f"└─ SHADOW: {decisions['SHADOW']}\n\n"
        f"📊 <b>Regime</b>\n"
        f"{regime_lines}\n\n"
        f"📦 <b>Position Gate</b>\n"
        f"├─ CLEAR: {pos_gates.get('CLEAR', 0)}\n"
        f"└─ PASS: {pos_gates.get('PASS', 0)}\n\n"
        f"📐 <b>Size</b>\n"
        f"└─ Avg: {avg_size:.2f}x\n\n"
        f"🗄 <b>Data Source</b>\n"
        f"{source_lines}\n\n"
        f"⏱ <b>Cache Age by Stage</b>\n"
        f"{stage_age_lines}\n\n"
        f"📋 <b>Top Reasons</b>\n"
        f"{reason_lines}\n\n"
        f"💡 <b>Interpretasi</b>\n"
        f"{score_note}\n{gap_note}\n{gate_note}"
    )
    bot.reply_to(m, text, parse_mode='HTML')

# ===== P4.W11: FAST_FAIL DEEP DIVE =====
def safe_avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0


def get_fast_fail_breakdown(limit: int = 100) -> Dict[str, Any]:
    """Breakdown FAST_FAIL trades by score, regime, rr, duration, reason."""
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT 
                score, regime, rr, 
                (exit_time - timestamp) / 60.0 as duration_minutes,
                outcome, reason
            FROM signals 
            WHERE evaluated=1 
              AND exit_time IS NOT NULL
              AND timestamp IS NOT NULL
              AND (exit_time - timestamp) < 15*60
              AND mfe < 0.3
              AND pnl < 0
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            return {"n": 0}
        
        # Indices: 0=score, 1=regime, 2=rr, 3=duration, 4=outcome, 5=reason
        scores = [r[0] for r in rows if r[0] is not None]
        regimes = [r[1] for r in rows if r[1]]
        rrs = [r[2] for r in rows if r[2] is not None]
        durations = [r[3] for r in rows if r[3] is not None]
        outcomes = [r[4] for r in rows if r[4]]
        reasons = [r[5] for r in rows if r[5]]  # entry reason (fallback)
        
        from collections import Counter
        regime_counter = Counter(regimes)
        reason_counter = Counter(reasons)
        outcome_counter = Counter(outcomes)
        
        return {
            "n": len(rows),
            "avg_score": safe_avg(scores),
            "avg_rr": safe_avg(rrs),
            "avg_duration": safe_avg(durations),
            "top_regimes": regime_counter.most_common(3),
            "top_reasons": reason_counter.most_common(3),
            "top_outcomes": outcome_counter.most_common(3),
        }
    except Exception as e:
        logger.error(f"get_fast_fail_breakdown error: {e}")
        return {"n": 0, "error": str(e)}



# ===== P4.W12: EDGE DECOMPOSITION =====
def get_edge_decomposition() -> Dict[str, Dict]:
    """
    Decompose PnL into EntryEdge + ExitEdge per bucket.
    EntryEdge = how good was entry timing (MFE - MAE)
    ExitEdge = how good was exit timing (actual - MFE)
    """
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("""
            SELECT
                CASE
                    WHEN score BETWEEN 0 AND 30 THEN '0-30'
                    WHEN score BETWEEN 31 AND 50 THEN '31-50'
                    WHEN score BETWEEN 51 AND 70 THEN '51-70'
                    WHEN score >= 71 THEN '71+'
                END as bucket,
                COUNT(*) as n,
                AVG(mfe - abs(mae)) as entry_edge,
                AVG(pnl - (mfe - abs(mae))) as exit_edge,
                AVG(pnl) as total_pnl,
                SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins
            FROM signals
            WHERE evaluated=1 AND mfe IS NOT NULL AND mae IS NOT NULL AND pnl IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
        """)
        rows = c.fetchall()
        conn.close()
        
        result = {}
        for bucket, n, entry_edge, exit_edge, total_pnl, wins in rows:
            if n > 0:
                wr = wins / n * 100
                result[bucket] = {
                    "n": n,
                    "entry_edge": round(entry_edge or 0, 2),
                    "exit_edge": round(exit_edge or 0, 2),
                    "total_pnl": round(total_pnl or 0, 2),
                    "wr": round(wr, 1),
                }
        return result
    except Exception as e:
        logger.error(f"get_edge_decomposition error: {e}")
        return {}


@bot.message_handler(commands=['analytics'])
def cmd_analytics(m):
    """Evaluasi — ringkas, insight-driven."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    conn = db_connect()
    c = conn.cursor()
    
    # Overall
    c.execute('''SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
                AVG(rr) as avg_rr,
                SUM(pnl) as total_pnl
                FROM signals WHERE evaluated=1''')
    row = c.fetchone()
    total = row[0] or 0
    wins = row[1] or 0
    avg_rr = row[2] or 0.0
    total_pnl = row[3] or 0.0
    wr = (wins / total * 100) if total > 0 else 0

    # FIX: HIGH-LEV patch (leveraged_pnl di signals.pnl) ditambahkan belakangan.
    # Trade lama (sebelum patch) nyimpen raw pnl, trade baru nyimpen leveraged
    # pnl — keduanya numpuk di kolom yang sama. leverage IS NULL = data lama
    # (sebelum kolom ini ada), leverage IS NOT NULL = data baru (post-patch,
    # pnl-nya sudah leveraged). Pisahkan biar gak salah baca rata-rata gabungan.
    c.execute('''SELECT
                COUNT(*), SUM(pnl), AVG(pnl)
                FROM signals WHERE evaluated=1 AND leverage IS NOT NULL''')
    lev_row = c.fetchone()
    lev_n = lev_row[0] or 0
    lev_total_pnl = lev_row[1] or 0.0

    c.execute('''SELECT COUNT(*), SUM(pnl)
                FROM signals WHERE evaluated=1 AND leverage IS NULL''')
    old_row = c.fetchone()
    old_n = old_row[0] or 0
    old_total_pnl = old_row[1] or 0.0
    
    # Best/Worst buckets
    c.execute('''SELECT
                    CASE
                        WHEN score BETWEEN 0 AND 30 THEN '0-30'
                        WHEN score BETWEEN 31 AND 50 THEN '31-50'
                        WHEN score BETWEEN 51 AND 70 THEN '51-70'
                        WHEN score >= 71 THEN '71+'
                    END as bucket,
                    COUNT(*) as n,
                    AVG(pnl) as avg_pnl,
                    SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins
                FROM signals WHERE evaluated=1
                GROUP BY bucket
                ORDER BY bucket''')
    rows = c.fetchall()
    conn.close()
    
    best_bucket = "—"
    best_pnl = 0.0
    worst_bucket = "—"
    worst_pnl = 0.0
    
    for bucket, n, avg_pnl, bucket_wins in rows:
        if n > 0:
            if avg_pnl and avg_pnl > best_pnl:
                best_pnl = avg_pnl
                best_bucket = bucket
            if avg_pnl and avg_pnl < worst_pnl:
                worst_pnl = avg_pnl
                worst_bucket = bucket
    
    # Insights
    insights = []
    if best_pnl > 0.5:
        insights.append(f"✅ Best: {best_bucket} → +{best_pnl:.2f}%")
    else:
        insights.append("⚠️ No strong bucket")
    
    if worst_pnl < -0.5:
        insights.append(f"⚠️ Worst: {worst_bucket} → {worst_pnl:.2f}%")
    
    # Entry vs Exit health (proxy)
    if wr > 50 and avg_rr > 2.0:
        insights.append("✅ Healthy entry & exit")
    elif wr > 50 and avg_rr < 1.5:
        insights.append("⚠️ Entry ok, exit weak")
    elif wr < 40 and avg_rr > 2.0:
        insights.append("⚠️ Entry weak, exit ok")
    
    text = f"""📈 <b>PERFORMANCE</b>
━━━━━━━━━━

📊 Result
WR {wr:.0f}%
PnL {total_pnl:+.1f}%
Avg RR {avg_rr:.2f}
n={total}

📊 Data Split (HIGH-LEV patch)
Leveraged: {lev_n} trades, {lev_total_pnl:+.1f}%
Pre-patch (raw): {old_n} trades, {old_total_pnl:+.1f}%
{'⚠️ Total PnL di atas masih campur raw+leveraged — pertimbangkan /resethistory kalau mau angka bersih full-leveraged.' if old_n > 0 and lev_n > 0 else ''}
📊 Best
{best_bucket} → +{best_pnl:.2f}%

📊 Worst
{worst_bucket} → {worst_pnl:.2f}%

💡 Insight
{chr(10).join(insights)}
"""
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['analytics_deep'])
def cmd_analytics_deep(m):
    """Full analytics — for diagnosis (not default)."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    # ===== MOVE ALL OLD cmd_analytics LOGIC HERE =====
    # This includes full detailed analysis - keeping for reference
    # (placeholder for now - retain existing comprehensive analysis)
    bot.reply_to(m, "📊 /analytics_deep reserved for future full diagnosis mode")


@bot.message_handler(commands=['health'])
def cmd_health(m):
    """Kenapa gak entry? — ringkas."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    pipe = get_pipeline_metrics()
    obs = pipe.get('obs', 0)
    conf = pipe.get('confidence', 0)
    exec_count = pipe.get('execute_pass', 0)
    
    # Block reasons (top 3 from journal)
    rejections = get_rejection_reason_counts(window_minutes=30)
    top_blocks = sorted(rejections.items(), key=lambda x: x[1], reverse=True)[:3]
    
    # Pipeline health
    obs_rate = (obs / max(1, pipe.get('check', 1))) * 100
    conf_rate = (conf / max(1, obs)) * 100 if obs > 0 else 0
    exec_rate = (exec_count / max(1, conf)) * 100 if conf > 0 else 0
    
    # Status
    if obs_rate < 10:
        status = "🔵 Ultra Selective"
    elif exec_rate == 0:
        status = "🟡 Execution Blocked"
    elif conf_rate < 30:
        status = "🟡 Thesis → Confidence Drop"
    else:
        status = "🟢 Healthy"
    
    block_lines = "\n".join([f"├─ {r}: {c}" for r, c in top_blocks]) if top_blocks else "├─ (none)"

    # ===== EXEC SKIP DETAIL: ambil top reasons dari record_reject("exec", ...) =====
    exec_skip_detail = ""
    try:
        _exec_skips = {}
        _exec_scores = {}
        _cutoff = time.time() - 1800  # 30 menit terakhir
        with _journal_lock:
            for _e in _decision_journal:
                if getattr(_e, "timestamp", 0) < _cutoff:
                    continue
                if getattr(_e, "executed", True):
                    continue
                _narr = getattr(_e, "narrative", {}) or {}
                _r = (_narr.get("why_not") or _narr.get("reason") or "unknown")[:35]
                _s = getattr(_e, "score", None) or _narr.get("score", 0)
                _t = _narr.get("threshold", 0)
                _exec_skips[_r] = _exec_skips.get(_r, 0) + 1
                if _s and _t:
                    _exec_scores[_r] = (_s, _t)
        if _exec_skips:
            top_skips = sorted(_exec_skips.items(), key=lambda x: x[1], reverse=True)[:3]
            lines = []
            for _r, _c in top_skips:
                _sc = _exec_scores.get(_r)
                if _sc:
                    lines.append(f"├─ {_r}: {_c}x (s={_sc[0]:.0f}/t={_sc[1]:.0f})")
                else:
                    lines.append(f"├─ {_r}: {_c}x")
            exec_skip_detail = "\n📋 Exec Skip\n" + "\n".join(lines) + "\n"
    except Exception:
        pass

    text = f"""🩺 <b>ENGINE HEALTH</b>
━━━━━━━━━━

📊 Pipeline
Obs {obs}
Conf {conf}
Exec {exec_count}

📊 Rate
Obs {obs_rate:.0f}%
Conf {conf_rate:.0f}%
Exec {exec_rate:.0f}%

🚫 Block
{block_lines}
{exec_skip_detail}
Status
{status}
"""
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
├─ Near TP (above 75%): {buckets['near_tp']}
├─ Mid TP (25-75%): {buckets['mid_tp']}
└─ Early TP (under 25%): {buckets['early_tp']}

🔴 <b>Loss Zone</b> ({loss_total})
├─ Near SL (above 75%): {buckets['near_sl']}
├─ Mid SL (25-75%): {buckets['mid_sl']}
└─ Early SL (under 25%): {buckets['early_sl']}

⚪ Undefined (no price): {buckets['undefined']}

📐 <b>Cohort Stats</b>
├─ Avg RR: {avg_rr_open:.2f}
├─ Avg Drift: {avg_drift:+.2f}%
└─ Exposure (Σ size_mult): {exposure_total:.2f}

⏱️ <b>Age Distribution</b>
├─ under 1h: {age_buckets['<1h']}
├─ 1-4h: {age_buckets['1-4h']}
├─ 4-12h: {age_buckets['4-12h']}
├─ 12-24h: {age_buckets['12-24h']}
└─ above 24h: {age_buckets['>24h']}

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


# ===== P4.W15: EXEC DISCOVERY =====
@bot.message_handler(commands=['execdiscovery'])
def cmd_execdiscovery(m):
    """Simulate what would happen if threshold was lowered."""
    if m.from_user.id != USER_ID:
        return
    
    try:
        # Get candidates that were rejected
        with _journal_lock:
            rejected = [e for e in _decision_journal if not e.executed and not e.shadow]
            recent = rejected[-50:]
        
        if not recent:
            bot.reply_to(m, "No rejected candidates found.")
            return
        
        # Simulate threshold lowering
        thresholds = [0, -3, -5, -8, -10]
        results = {}
        
        for adj in thresholds:
            executed = 0
            for e in recent:
                if e.score >= (e.narrative.get("threshold", 70) + adj):
                    executed += 1
            results[adj] = executed
        
        # Build response
        text = f"🚀 <b>EXEC DISCOVERY</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        text += f"Eligible (recent rejected): {len(recent)}\n\n"
        text += f"📊 <b>Simulation: Threshold -X</b>\n"
        for adj, exec_count in sorted(results.items()):
            pct = exec_count/len(recent)*100 if len(recent) > 0 else 0
            text += f"├─ -{abs(adj)}: {exec_count} ({pct:.0f}%)\n"
        
        text += f"\n💡 <b>Recommendation</b>\n"
        if results.get(-5, 0) > 0 and results.get(-10, 0) > len(recent) * 0.5:
            text += "└─ 🟢 Threshold -5 bisa dibuka (eksekusi mulai muncul)"
        elif results.get(-10, 0) > 0:
            text += "└─ 🟡 Perlu -10 untuk muncul (quality issue)"
        else:
            text += "└─ 🔴 Reject legitimate (jangan buka threshold)"
        
        bot.reply_to(m, text, parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

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
    
    # WITH ARGUMENT: deep detail for specific coin
    if len(parts) >= 2:
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
            _lev_info = compute_suggested_leverage(coin, alert['entry'], alert['sl'], alert.get('position_size_mult', 1.0))
            # ===== FIX (entry alert honesty): ZONE-BASED ENTRY DISPLAY =====
            _zone_low = alert.get("entry_zone_low")
            _zone_high = alert.get("entry_zone_high")
            if _zone_low and _zone_high:
                _zone_valid = _zone_low <= mark <= _zone_high
                _zone_mark = "✅" if _zone_valid else "❌"
                entry_line = (
                    f"├─ Entry: {fmt_price(_zone_low)} - {fmt_price(_zone_high)} {_zone_mark}\n"
                    f"├─ Current: {fmt_price(mark)}\n"
                )
            else:
                entry_line = f"├─ Entry: {fmt_price(mark)}\n"
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
{entry_line}├─ SL: {fmt_price(alert['sl'])} ({abs(alert['entry']-alert['sl'])/max(alert['entry'],0.01)*100:.2f}%)
├─ TP: {fmt_price(alert['tp'])} ({abs(alert['tp']-alert['entry'])/max(alert['entry'],0.01)*100:.2f}%)
├─ RR: 1:{alert['rr']:.1f}
└─ Leverage: {_lev_info['suggested']:.1f}x (cap {_lev_info['native_cap']}x)

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
        return
    
    # WITHOUT ARGUMENT: show top setups from real pipeline (THESIS-stage)
    try:
        results, results_ts = get_last_pipeline_results()
        results_age = time.time() - results_ts if results_ts > 0 else 999999

        # Pipeline results too stale (>10min) or empty → fallback to discovery
        if results and results_age < 600:
            results_sorted = sorted(results, key=lambda x: x.get('score', 0), reverse=True)
            top = results_sorted[:5]

            text = f"🎯 <b>TOP SETUPS</b> (pipeline, {results_age:.0f}s ago)\n━━━━━━━━━━━━━━━━━━━━━━\n"
            for alert in top:
                coin = alert.get('coin', '?')
                direction = alert.get('direction', '?')
                score = alert.get('score', 0)
                rr = alert.get('rr', 0)
                entry = alert.get('entry', 0)
                tp = alert.get('tp', 0)
                dir_emoji = "🟢" if direction == "LONG" else "🔴"
                text += (
                    f"{dir_emoji} <b>{coin}</b> {direction} | Score: {score}\n"
                    f"├─ Entry: {fmt_price(entry)} | TP: {fmt_price(tp)} | RR: 1:{rr:.1f}\n"
                    f"└─ /entry {coin} for detail\n"
                )
            text += "\n💡 /entry BTC → deep detail"
            bot.reply_to(m, text, parse_mode='HTML')
            return

        # Fallback: pipeline hasn't run recently (e.g. just started) — old discovery path
        snapshot = get_snapshot()
        if not snapshot:
            bot.reply_to(m, "❌ No market data yet")
            return

        candidates = build_candidate_pool(max_candidates=5)
        if not candidates:
            bot.reply_to(m, "🔍 No setups detected yet")
            return

        text = "🎯 <b>TOP SETUPS</b> (discovery, pipeline not run yet)\n━━━━━━━━━━━━━━━━━━━━━━\n"

        for coin in candidates[:5]:
            mark = snapshot.mids.get(coin, 0)
            if mark == 0:
                continue

            # Quick check: use engine's cached result if available
            try:
                master = {coin: get_candles(coin, "1h", 100)}
                alert = check_entry_alert_v10(coin, mark, master)
                if alert:
                    text += f"├─ <b>{coin}</b>: {fmt_price(mark)} | {alert['direction']} | Score: {alert.get('score', 0)}\n"
                else:
                    text += f"├─ {coin}: {fmt_price(mark)}\n"
            except:
                text += f"├─ {coin}: {fmt_price(mark)}\n"

        text += "\n💡 /entry BTC → deep detail"
        bot.reply_to(m, text, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")

@bot.message_handler(commands=['warroom'])
def cmd_warroom(m):
    """Redirect to /entry for backward compatibility"""
    parts = m.text.split()
    
    if len(parts) < 2:
        bot.reply_to(
            m,
            "🧠 <b>WARROOM</b> → now /entry\n\n"
            "Usage:\n"
            "  /entry BTC  → deep detail\n"
            "  /entry      → top setups"
        )
        return
    
    # Redirect to /entry with same arguments
    cmd_entry(m)

# ===== P4.57: RESET HISTORY (destructive, confirm-gated) =====
_pending_reset_confirm: Dict[int, float] = {}
_pending_reset_lock = threading.RLock()
_RESET_CONFIRM_WINDOW_SECS = 60

_RESET_TABLES = [
    "signals", "journal", "counterfactual", "shadow_decisions",
    "hypothesis_validation", "prediction_quality", "belief_state_log",
    "decision_traces", "context_log", "intent_memory", "reaction_log",
]

def backup_db_before_reset() -> Optional[str]:
    """P4.57: copy file DB apa adanya sebelum destructive reset, supaya ada
    safety net kalau ternyata salah pencet / berubah pikiran. Return path
    backup, atau None kalau gagal (caller harus treat sebagai abort)."""
    try:
        if not os.path.exists(DB_PATH):
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{DB_PATH}.backup_{ts}"
        shutil.copy2(DB_PATH, backup_path)
        return backup_path
    except Exception as e:
        logger.error(f"backup_db_before_reset failed: {e}")
        return None

def execute_full_history_reset() -> Dict[str, Any]:
    """P4.57: DROP semua tabel history lalu recreate schema dari fungsi init
    yang sudah ada (bukan re-tulis schema manual — biar selalu sinkron
    dengan kolom hasil migrasi terbaru). Juga bersihkan in-memory cache
    yang derive dari data lama (effective WR, realized RR percentile),
    biar gak ada sisa angka lama nyangkut walau cuma sebentar (TTL cache)."""
    result = {"backup": None, "dropped": [], "error": None}
    try:
        result["backup"] = backup_db_before_reset()

        with get_db() as conn:
            cursor = conn.cursor()
            for table in _RESET_TABLES:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                result["dropped"].append(table)
            conn.commit()

        # Recreate schema fresh — exact sequence yang sama seperti bootstrap()
        init_db()
        ensure_signals_schema()
        migrate_evidence_families_column()
        migrate_context_log_coin_column()
        migrate_signals_leverage_column()
        migrate_score_calibration_columns()
        migrate_quality_conviction_columns()
        migrate_p450_conviction_mem_columns()
        migrate_l4_columns()
        migrate_alert_snapshot_columns()
        detect_signal_score_column()

        # Clear in-memory state yang derive dari history lama
        with _recent_outcome_lock:
            _recent_outcome_memory.clear()
        with _coin_cooldown_lock:
            _coin_cooldown.clear()
        with _effective_wr_lock:
            _effective_wr_cache["value"] = 0.5
            _effective_wr_cache["ts"] = 0.0
        with _realized_rr_lock:
            _realized_rr_cache.clear()
        with _exec_pipeline_lock:
            _exec_pipeline.clear()

        logger.warning(f"🗑️ FULL HISTORY RESET completed. Backup: {result['backup']}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"execute_full_history_reset failed: {e}")
    return result

@bot.message_handler(commands=['resethistory'])
def cmd_resethistory(m):
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return

    parts = m.text.split()
    now = time.time()

    if len(parts) >= 2 and parts[1].upper() == "CONFIRM":
        with _pending_reset_lock:
            requested_at = _pending_reset_confirm.get(m.from_user.id)
            _pending_reset_confirm.pop(m.from_user.id, None)

        if not requested_at or now - requested_at > _RESET_CONFIRM_WINDOW_SECS:
            bot.reply_to(
                m,
                "⏰ Konfirmasi expired atau belum minta reset.\n"
                "Kirim /resethistory dulu (tanpa CONFIRM) untuk mulai."
            )
            return

        bot.reply_to(m, "🗑️ Resetting... (backup dulu, jangan kirim apa-apa)")
        result = execute_full_history_reset()

        if result["error"]:
            bot.reply_to(m, f"🔴 Reset GAGAL: {result['error']}\nDB asli TIDAK diubah kalau drop belum sempat commit — cek log.")
            return

        backup_note = f"Backup: {result['backup']}" if result['backup'] else "⚠️ Backup gagal dibuat (DB lama mungkin belum ada / kosong)"
        text = (
            "✅ <b>HISTORY RESET COMPLETE</b>\n"
            "━━━━━━━━━━\n\n"
            f"Tabel direset: {len(result['dropped'])}\n"
            f"{backup_note}\n\n"
            "Win rate, RR percentile, MEM stability, cooldown — semua mulai dari 0.\n"
            "Posisi yang LAGI OPEN di TradeManager TIDAK kehapus (cuma DB history).\n\n"
            "💡 Kalau ternyata salah pencet, backup file di atas bisa di-restore manual\n"
            "(copy balik ke signals.db, lalu restart bot)."
        )
        bot.reply_to(m, text, parse_mode='HTML')
        return

    # First call: warn + ask for explicit confirm
    with _pending_reset_lock:
        _pending_reset_confirm[m.from_user.id] = now

    bot.reply_to(
        m,
        "⚠️ <b>FULL HISTORY RESET</b>\n"
        "━━━━━━━━━━\n\n"
        "Ini akan MENGHAPUS SEMUA tabel history (signals, journal, dan "
        "semua log observability) — win rate, RR distribution, MEM stability, "
        "coin cooldown, semua balik ke 0.\n\n"
        "Posisi yang lagi OPEN sekarang TIDAK ikut terhapus.\n\n"
        f"Backup otomatis dibuat sebelum drop (gak destructive permanen kalau salah).\n\n"
        f"Kirim <code>/resethistory CONFIRM</code> dalam {_RESET_CONFIRM_WINDOW_SECS}s "
        "untuk lanjutkan, atau abaikan untuk batal.",
        parse_mode='HTML'
    )

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

    # ===== P4.12: SHADOW REASON BREAKDOWN =====
    try:
        shadow_breakdown = get_shadow_breakdown()
        text += "\n👻 <b>Shadow Reasons (P4.12)</b>\n"
        if shadow_breakdown:
            for reason, count in sorted(shadow_breakdown.items(), key=lambda x: x[1], reverse=True)[:5]:
                text += f"├─ {reason}: {count}\n"
        else:
            text += "├─ (no shadows yet)\n"
    except Exception as _err:
        logger.debug(f"Shadow breakdown error: {_err}")

    # ===== P4.24: DISCOVERY CONVERSION =====
    try:
        disc_rate = get_discovery_rate()
        text += f"\n📈 <b>Discovery Conversion</b>\n├─ Shadow Win Rate: {disc_rate:.1f}%\n"
    except Exception as _err:
        logger.debug(f"Discovery rate error: {_err}")
    
    # === P1 FIX: EXEC_BLOCK / INVENTORY HEALTH (pakai check_signal_db_health yang udah ada) ===
        if sig_health:
            db_pending = sig_health.get("pending_eval", 0)
            tracked_open = sig_health.get("tracked_open_in_manager", 0)
            orphan_count = sig_health.get("orphan_count", 0)
            managed_ratio = sig_health.get("managed_ratio_pct", 0)
            gate_status = "🔴 BLOCKED" if tracked_open >= 120 else "🟢 CLEAR"
            text += f"""
🚫 <b>EXEC_BLOCK / INVENTORY</b>
├─ Managed (TradeManager): {tracked_open}
├─ DB Pending (evaluated=0): {db_pending}
├─ Orphan (stale above 6h, untracked): {orphan_count}
├─ Managed Ratio: {managed_ratio}%
└─ Gate Status: {gate_status}
"""
    except Exception as e:
        logger.error(f"cmd_health signal_db_health error: {e}")
    
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
# ENTRY INTENT QUEUE — PINDAHKAN KE SINI (ATAS cmd_funnel)
# ============================================================

_entry_queue: deque = deque(maxlen=200)
_entry_queue_lock = threading.RLock()

def queue_entry_intent(entry_data: Dict):
    """Queue entry intent only if near-pass (within 10 points of threshold)."""
    with _entry_queue_lock:
        score = entry_data.get("score", 0)
        threshold = entry_data.get("threshold", 100)
        
        if score >= threshold - 10:
            entry_data["queued_at"] = time.time()
            entry_data["gap"] = threshold - score
            _entry_queue.append(entry_data)
            
            cutoff = time.time() - 3600
            while _entry_queue and _entry_queue[0].get("queued_at", 0) < cutoff:
                _entry_queue.popleft()
            
            logger.info(f"📋 ENTRY_QUEUE {entry_data.get('coin')}: score={score}, threshold={threshold}, gap={threshold-score}")

def get_entry_queue_status() -> List[Dict]:
    """Get current entry queue status."""
    with _entry_queue_lock:
        return list(_entry_queue)[-20:]


# ============================================================
# BOT COMMAND: FUNNEL
# ============================================================

@bot.message_handler(commands=['funnel'])
def cmd_funnel(m):
    """Show conversion funnel + threshold distribution + confidence histogram + entry queue."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    funnel_text = get_funnel_summary()
    threshold_text = get_threshold_summary()
    confidence_text = get_confidence_summary()
    queue = get_entry_queue_status()  # ← SEKARANG AMAN!
    
    text = f"""
📡 <b>CONVERSION FUNNEL</b>
━━━━━━━━━━━━━━━━━━━━━━
{funnel_text}

📊 <b>THRESHOLD DISTRIBUTION</b>
{threshold_text}

📊 <b>CONFIDENCE HISTOGRAM</b>
{confidence_text}

📋 <b>ENTRY QUEUE</b> (near-pass)
"""
    if queue:
        for q in queue[-8:]:
            text += f"├─ {q['coin']} {q['direction']}: score={q['score']} threshold={q['threshold']} gap={q['gap']:.0f}\n"
        if len(queue) > 8:
            text += f"└─ ... and {len(queue) - 8} more\n"
    else:
        text += "├─ (empty)\n"
    
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['queue'])
def cmd_queue(m):
    """Show entry queue (near-miss setups blocked by inventory/threshold)."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    with _entry_queue_lock:
        queue = list(_entry_queue)[-20:]
    
    if not queue:
        bot.reply_to(m, "📋 ENTRY QUEUE: (empty)")
        return
    
    text = "📋 <b>ENTRY QUEUE</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for q in queue:
        blocked = q.get("block_reason", "unknown")
        if "inventory" in blocked.lower() or "full" in blocked.lower():
            emoji = "🚫"
        elif "threshold" in blocked.lower() or "score" in blocked.lower():
            emoji = "📊"
        else:
            emoji = "⏳"
        
        text += f"{emoji} {q['coin']} {q['direction']}: score={q['score']} threshold={q['threshold']} gap={q['gap']:.0f}\n"
        text += f"   └─ blocked: {blocked}\n"
    
    bot.reply_to(m, text, parse_mode='HTML')
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
        
        set_log_layer("DEVELOPER")
        bot.reply_to(m, "🔊 <b>DEBUG MODE</b>\n🎚️ Log layer: DEVELOPER\n⏱️ Auto-reset to RUNTIME in 5 min", parse_mode='HTML')
        
        def reset():
            time.sleep(300)
            set_log_layer("RUNTIME")
        threading.Thread(target=reset, daemon=True).start()

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

@bot.message_handler(commands=['loglayer'])
def cmd_loglayer(m):
    """Switch log layer."""
    if m.from_user.id != USER_ID:
        bot.reply_to(m, "⛔ Admin only")
        return
    
    parts = m.text.split()
    if len(parts) < 2:
        current = [k for k, v in LOG_LAYER.items() if v == _active_log_layer]
        text = f"📊 Current log layer: {current[0] if current else 'RUNTIME'}\n\n"
        text += f"Available layers:\n"
        text += f"  OPERATOR → /status, /dashboard (minimal)\n"
        text += f"  RUNTIME → + ENGINE SUMMARY, FUNNEL (default)\n"
        text += f"  DEVELOPER → + VELOCITY_TRACE, STRUCT\n"
        text += f"  RESEARCH → + DIS_DEBUG, dynamic coins\n"
        text += f"  EXPERIMENT → + TM_SAMPLE, OI_HISTORY\n\n"
        text += f"Usage: /loglayer DEVELOPER"
        bot.reply_to(m, text, parse_mode='HTML')
        return
    
    layer = parts[1].upper()
    if layer not in LOG_LAYER:
        bot.reply_to(m, f"❌ Unknown layer: {layer}\nAvailable: OPERATOR, RUNTIME, DEVELOPER, RESEARCH, EXPERIMENT")
        return
    
    set_log_layer(layer)
    bot.reply_to(m, f"📊 Log layer set to: <b>{layer}</b>\n\n"
                   f"OPERATOR → /status, /dashboard\n"
                   f"RUNTIME → + ENGINE SUMMARY, FUNNEL\n"
                   f"DEVELOPER → + VELOCITY_TRACE, STRUCT\n"
                   f"RESEARCH → + DIS_DEBUG, dynamic coins\n"
                   f"EXPERIMENT → + TM_SAMPLE, OI_HISTORY", parse_mode='HTML')

@bot.message_handler(commands=['staleaudit'])
def cmd_staleaudit(m):
    if m.from_user.id != USER_ID:
        return
    snapshot = get_snapshot()
    if not snapshot:
        bot.reply_to(m, "❌ No snapshot")
        return

    now = time.time()
    lines = ["📊 STALE AUDIT (age above 24h, still OPEN)\n━━━━━━━━━━━━━━━━━━━━━━"]
    with TRADE_MANAGER._lock:
        for sid, pos in TRADE_MANAGER.positions.items():
            if pos.status != "OPEN":
                continue
            age = now - pos.entry_time
            if age < 24*3600:
                continue
            price = snapshot.mids.get(pos.coin, pos.entry)
            if pos.direction == "LONG":
                if price >= pos.tp3.price:
                    expected = f"TP (would win +{((price-pos.entry)/pos.entry*100):.2f}%)"
                elif price <= pos.sl:
                    expected = f"SL (would lose -{((pos.entry-price)/pos.entry*100):.2f}%)"
                else:
                    expected = f"OPEN ({((price-pos.entry)/pos.entry*100):.2f}%)"
            else:
                if price <= pos.tp3.price:
                    expected = f"TP (would win +{((pos.entry-price)/pos.entry*100):.2f}%)"
                elif price >= pos.sl:
                    expected = f"SL (would lose -{((price-pos.entry)/pos.entry*100):.2f}%)"
                else:
                    expected = f"OPEN ({((pos.entry-price)/pos.entry*100):.2f}%)"
            lines.append(f"{sid} {pos.coin} age={age/3600:.1f}h | price={price:.2f} | {expected}")

    total_stale = len(lines) - 1
    if total_stale == 0:
        bot.reply_to(m, "✅ No stale trades (above 24h) found.")
        return

    profit = sum(1 for l in lines[1:] if "TP" in l)
    loss = sum(1 for l in lines[1:] if "SL" in l)
    open_still = total_stale - profit - loss
    lines.append(f"\n📌 Summary: total={total_stale}, profit_if_close={profit}, loss_if_close={loss}, still_open={open_still}")
    bot.reply_to(m, "\n".join(lines[:25]) + (f"\n... and {len(lines)-25} more" if len(lines)>25 else ""), parse_mode='HTML')


@bot.message_handler(commands=['restoreaudit'])
def cmd_restoreaudit(m):
    if m.from_user.id != USER_ID:
        return
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT signal_id, coin, direction, entry_price, sl_price, timestamp FROM signals WHERE evaluated=0")
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.reply_to(m, "✅ No pending signals in DB (evaluated=0).")
            return

        snapshot = get_snapshot()
        lines = ["📊 RESTORE AUDIT (pending signals)\n━━━━━━━━━━━━━━━━━━━━━━"]
        managed_ids = set(TRADE_MANAGER.positions.keys())
        already_managed = 0
        will_restore = 0
        for signal_id, coin, direction, entry, sl, ts in rows:
            if signal_id in managed_ids:
                already_managed += 1
                continue
            if snapshot and coin in snapshot.mids:
                price = snapshot.mids[coin]
                risk = abs(entry - sl)
                if direction == "LONG":
                    tp_est = entry + 2 * risk
                    if price >= tp_est:
                        expected = f"would be TP (price {price:.2f})"
                    elif price <= sl:
                        expected = f"would be SL (price {price:.2f})"
                    else:
                        expected = f"still open (price {price:.2f})"
                else:
                    tp_est = entry - 2 * risk
                    if price <= tp_est:
                        expected = f"would be TP (price {price:.2f})"
                    elif price >= sl:
                        expected = f"would be SL (price {price:.2f})"
                    else:
                        expected = f"still open (price {price:.2f})"
                will_restore += 1
                lines.append(f"{signal_id} {coin} {direction} entry={entry:.4f} | {expected}")
            else:
                lines.append(f"{signal_id} {coin} {direction} → ⚠️ COIN NOT FOUND")
                will_restore += 1

        lines.append(f"\n📌 Summary: pending={len(rows)}, already_managed={already_managed}, will_restore={will_restore}")
        bot.reply_to(m, "\n".join(lines[:25]) + (f"\n... and {len(lines)-25} more" if len(lines)>25 else ""), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")


@bot.message_handler(commands=['score'])
def cmd_score(m):
    if m.from_user.id != USER_ID:
        return
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT 
            CASE 
                WHEN score BETWEEN 0 AND 30 THEN '0-30'
                WHEN score BETWEEN 31 AND 50 THEN '31-50'
                WHEN score BETWEEN 51 AND 70 THEN '51-70'
                WHEN score BETWEEN 71 AND 85 THEN '71-85'
                WHEN score >= 86 THEN '86+'
            END as bucket,
            COUNT(*) as total,
            SUM(CASE WHEN outcome IN ('TP_HIT','PARTIAL_WIN') THEN 1 ELSE 0 END) as wins,
            AVG(pnl) as avg_pnl,
            AVG(mfe) as avg_mfe,
            AVG(mae) as avg_mae
        FROM signals
        WHERE evaluated=1 AND outcome IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
    """)
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.reply_to(m, "Belum ada closed trade yang valid.")
        return
    text = "📊 SCORE PERFORMANCE\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for bucket, total, wins, avg_pnl, avg_mfe, avg_mae in rows:
        wr = (wins / total * 100) if total > 0 else 0
        text += f"{bucket}: n={total} WR={wr:.0f}% PnL={avg_pnl:.2f}% MFE={avg_mfe:.2f}% MAE={avg_mae:.2f}%\n"
    bot.reply_to(m, text, parse_mode='HTML')


# ============================================================
# BOT COMMAND: /marketdna — Market State Vector (Phase B/C)
# ============================================================

@bot.message_handler(commands=['marketdna'])
def cmd_marketdna(m):
    """Show Market DNA — continuous state vector. Observation/audit tool;
    CONTEXT_ENGINE_ENABLED flag (separate) controls whether this actually
    affects live decisions."""
    if m.from_user.id != USER_ID:
        return

    target_coin = "BTC"
    parts = m.text.split()
    if len(parts) > 1:
        target_coin = parts[1].upper()

    state = compute_market_state_vector(target_coin)

    def bar(value, width=20):
        filled = int((value / 100) * width)
        return "█" * filled + "░" * (width - filled)

    engine_status = "🟢 ON (affects live scoring)" if CONTEXT_ENGINE_ENABLED else "⚪ OFF (observation only)"

    text = f"""🧬 <b>MARKET DNA — {target_coin}</b>
━━━━━━━━━━━━━━━━━━━━━━

📌 <b>State</b>: {state.regime_label} (conf: {state.confidence:.0f}%)
⚙️ <b>Context Engine</b>: {engine_status}

📊 <b>Components</b>
├─ Momentum      {bar(state.momentum)} {state.momentum:.0f}%
├─ Compression   {bar(state.compression)} {state.compression:.0f}%
├─ Shock         {bar(state.shock)} {state.shock:.0f}%
├─ Entropy       {bar(state.entropy)} {state.entropy:.0f}%
├─ Participation {bar(state.participation)} {state.participation:.0f}%
├─ Tension       {bar(state.tension)} {state.tension:.0f}%
├─ Volatility    {bar(state.volatility)} {state.volatility:.0f}%
├─ Liquidity     {bar(state.liquidity)} {state.liquidity:.0f}%
└─ Directionality {'↗' if state.directionality > 0 else '↘'} {state.directionality:+.0f}

💡 <b>Interpretasi</b>
{'✅ Strong trend' if state.momentum > 60 else
'⚡ Compression building' if state.compression > 60 else
'🌀 Chaotic' if state.entropy > 60 else
'⚖️ Neutral'}

⏰ {get_wib()}
"""
    bot.reply_to(m, text, parse_mode='HTML')


# ============================================================
# BOT COMMAND: /detectorstats — Distribution Observation
# ============================================================

@bot.message_handler(commands=['detectorstats'])
def cmd_detectorstats(m):
    """Show detector distribution stats — OBSERVATION ONLY, tidak
    mempengaruhi keputusan trading sama sekali."""
    if m.from_user.id != USER_ID:
        return

    text = "📊 <b>DETECTOR DISTRIBUTION</b> (last 300, OBSERVATION ONLY)\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "❌ <b>NOT used for decision making</b>\n"
    text += "✅ <b>Only for observation/analytics</b>\n\n"

    detector_types = ["OB", "FVG", "LIQUIDITY", "VACUUM", "OB_FLOW", "FVG_FLOW"]

    for dt in detector_types:
        stats = get_detector_stats(dt)
        if stats.get("n", 0) < 5:
            text += f"⚪ {dt}: insufficient data\n"
            continue

        text += (
            f"📈 <b>{dt}</b>\n"
            f"├─ n={stats['n']}\n"
            f"├─ P50: {stats['p50']}  P70: {stats['p70']}\n"
            f"├─ P90: {stats['p90']}  Max: {stats['max']}\n"
            f"└─ Mean: {stats['mean']}\n\n"
        )

    bot.reply_to(m, text, parse_mode='HTML')


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
            # ===== P4: OUTCOME_TRACE / CORRELATION FIELDS =====
            "exit_eff": "REAL DEFAULT NULL",
            "source": "TEXT DEFAULT NULL",
            "regime": "TEXT DEFAULT NULL",
            "cache_age": "REAL DEFAULT NULL",
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

# ============================================================
# RECONCILE — SYNC DB PENDING WITH TRADE_MANAGER
# ============================================================

def reconcile_open_positions():
    """Sync DB pending with TradeManager positions.
    Fix orphan ghost positions blocking inventory.
    """
    try:
        conn = db_connect()
        cursor = conn.cursor()
        
        # Get all pending signals from DB
        cursor.execute("""
            SELECT signal_id, coin, direction, entry_price, sl_price, timestamp
            FROM signals WHERE evaluated = 0
        """)
        db_rows = cursor.fetchall()
        conn.close()
        
        db_pending = len(db_rows)
        managed_ids = set(TRADE_MANAGER.positions.keys())
        
        # Find orphans: in DB but NOT in TradeManager
        orphans = []
        for signal_id, coin, direction, entry, sl, ts in db_rows:
            if signal_id not in managed_ids:
                orphans.append((signal_id, coin, direction, entry, sl, ts))
        
        if not orphans:
            logger.info(f"✅ RECONCILE: no orphans found (db={db_pending}, managed={len(managed_ids)})")
            return
        
        logger.warning(f"🔴 RECONCILE: {len(orphans)} orphans found — archiving...")
        
        # Archive orphans (mark as recovered)
        conn = db_connect()
        cursor = conn.cursor()
        archived = 0
        for signal_id, coin, direction, entry, sl, ts in orphans:
            # Check age > 1 hour → safe to archive
            age_hours = (time.time() - ts) / 3600 if ts else 999
            if age_hours > 1:
                cursor.execute("""
                    UPDATE signals 
                    SET evaluated = 1, 
                        outcome = 'ORPHAN_RECOVERED',
                        exit_time = CAST(strftime('%s', 'now') AS INTEGER)
                    WHERE signal_id = ?
                """, (signal_id,))
                archived += 1
                logger.debug(f"  Archived orphan: {signal_id} {coin} (age={age_hours:.1f}h)")
            else:
                # Still fresh → try to restore
                logger.debug(f"  Fresh orphan: {signal_id} {coin} (age={age_hours:.1f}h) — restoring...")
                try:
                    atr_pct = get_atr_pct(coin, 14, "1h") or 2.0
                    regime = get_market_regime()
                    _boost_mult = compute_tp_rr_boost(coin=coin, regime=regime, final_score=0)
                    targets = calculate_scaled_targets(entry, direction, atr_pct, regime, rr_multiplier=_boost_mult)
                    
                    _restore_lev = compute_suggested_leverage(coin, entry, sl, 1.0)["suggested"]
                    TRADE_MANAGER.add_position(
                        signal_id=signal_id,
                        coin=coin,
                        direction=direction,
                        entry=entry,
                        sl=sl,
                        tp_targets=targets,
                        entry_time=ts if ts else time.time(),
                        entry_atr_pct=atr_pct,
                        leverage=_restore_lev,
                    )
                    archived += 1  # counted as restored
                except Exception as restore_err:
                    logger.error(f"  Failed to restore {signal_id}: {restore_err}")
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ RECONCILE DONE: archived/restored {archived} orphans")
        
    except Exception as e:
        logger.error(f"reconcile_open_positions error: {e}")

def restore_orphans(limit: int = 300) -> Dict[str, int]:
    """
    Restore orphan positions from DB into TradeManager.
    AMAN: tidak pernah close/delete, hanya register ulang ke manager.
    """
    try:
        conn = db_connect()
        cursor = conn.cursor()
        
        # Ambil semua pending signals
        cursor.execute("""
            SELECT signal_id, coin, direction, entry_price, sl_price, timestamp
            FROM signals 
            WHERE evaluated = 0
            ORDER BY timestamp ASC
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            logger.info("RESTORE_ORPHANS: no pending signals found")
            return {"found": 0, "restored": 0, "failed": 0}
        
        restored = 0
        failed = 0
        skipped = 0
        
        with TRADE_MANAGER._lock:
            existing_ids = set(TRADE_MANAGER.positions.keys())
        
        for signal_id, coin, direction, entry, sl, ts in rows:
            # Skip jika sudah di-manage
            if signal_id in existing_ids:
                skipped += 1
                continue
            
            try:
                # Rekonstruksi data yang hilang
                atr_pct = get_atr_pct(coin, 14, "1h") or 2.0
                regime = get_market_regime()
                _boost_mult = compute_tp_rr_boost(coin=coin, regime=regime, final_score=0)
                targets = calculate_scaled_targets(entry, direction, atr_pct, regime, rr_multiplier=_boost_mult)
                
                _restore_lev = compute_suggested_leverage(coin, entry, sl, 1.0)["suggested"]
                TRADE_MANAGER.add_position(
                    signal_id=signal_id,
                    coin=coin,
                    direction=direction,
                    entry=entry,
                    sl=sl,
                    tp_targets=targets,
                    entry_time=ts if ts else time.time(),
                    entry_atr_pct=atr_pct,
                    leverage=_restore_lev,
                )
                restored += 1
                logger.info(f"RESTORE_ORPHAN: {signal_id} {coin} {direction} restored lev={_restore_lev:.1f}x")
                
            except Exception as e:
                failed += 1
                logger.error(f"RESTORE_FAILED {signal_id}: {e}")
        
        logger.warning(
            f"ORPHAN_RESTORE SUMMARY: found={len(rows)} "
            f"restored={restored} skipped={skipped} failed={failed}"
        )
        
        return {"found": len(rows), "restored": restored, "failed": failed, "skipped": skipped}
        
    except Exception as e:
        logger.error(f"restore_orphans error: {e}")
        return {"found": 0, "restored": 0, "failed": 0, "skipped": 0}

def preload_candles_startup(coins: List[str], timeframe: str = "1h", limit: int = 100) -> int:
    """P4 (preload): fetch candle buat sekumpulan coin dan LANGSUNG isi CACHE
    (key sama persis dengan yang dipakai get_candles: candles_{coin}_{tf}_{limit}),
    beda dari fetch_candles_master() yang cuma return dict in-memory tanpa
    nyentuh CACHE. Dipanggil sekali pas bootstrap, sebelum engine thread mulai,
    biar cycle scan pertama gak nemu cache kosong total untuk top coin.
    Return: jumlah coin yang berhasil di-preload."""
    results = fetch_candles_master(coins, timeframe, limit)
    key_suffix = f"{timeframe}_{limit}"
    count = 0
    for coin, candles in results.items():
        if candles:
            CACHE.set(f"candles_{coin}_{key_suffix}", candles)
            count += 1
    return count


# ============================================================
# ATTENTION ENGINE V2 — Behavioral Attention Engine
# ============================================================

class AttentionEngineV2:
    def __init__(self):
        self._states: Dict[str, AttentionState] = {}
        self._lock = threading.RLock()
        self._ema_alpha = 0.3          # smoothing
        self._decay_threshold = 0.02   # kalau perubahan < 2%, stagnan → decay

    def update(self, coin: str, snapshot: MarketSnapshot) -> AttentionState:
        """Compute raw components + update state with EMA and decay."""
        if not snapshot or coin not in snapshot.mids:
            return self._get_stale_state(coin)

        # === RAW COMPONENTS (0-1) ===
        raw = self._compute_raw_components(coin, snapshot)

        # === SMOOTHING + DECAY ===
        with self._lock:
            old = self._states.get(coin)
            if old is None:
                # first time
                new_score = raw["score"]
                new_momentum = 0.0
                new_freshness = 1.0
            else:
                # EMA
                new_score = self._ema_alpha * raw["score"] + (1 - self._ema_alpha) * old.score
                # momentum = perubahan
                new_momentum = (new_score - old.score) / max(old.score, 0.01)
                # freshness = 1 jika berubah, turun kalau stagnan
                if abs(new_score - old.score) < self._decay_threshold:
                    new_freshness = old.freshness * 0.95
                else:
                    new_freshness = min(1.0, old.freshness + 0.1)

                # Decay tambahan kalau momentum negatif dan freshness rendah
                if new_momentum < -0.02 and new_freshness < 0.3:
                    new_score *= 0.98  # turun perlahan

            state = AttentionState(
                score=max(0.0, min(1.0, new_score)),
                confidence=self._compute_confidence(coin, snapshot, raw),
                momentum=new_momentum,
                freshness=new_freshness,
                components=raw["components"],
                ts=time.time()
            )
            self._states[coin] = state
            return state

    def _compute_raw_components(self, coin: str, snapshot: MarketSnapshot) -> Dict:
        """Return dict with 'score', 'components', and 'coverage' (0-1 = ratio
        of components that had real data vs unknown). TIER 1 — cache/WS only,
        never calls REST-capable helpers."""
        price = snapshot.mids.get(coin, 0)
        oi_usd = snapshot.oi.get(coin, 0)
        funding = snapshot.funding.get(coin, 0)

        oi_growth = get_oi_roc(coin, window_minutes=5)
        delta_shift = get_delta_shift(coin)
        vol_spike = get_volume_spike_cached(coin)      # None = unknown
        shock = compute_shock_score_cached(coin)        # None = unknown

        expected = 5  # oi, delta, volume, shock, oi_abs
        available = 3  # oi, delta, oi_abs are always cheap/available from snapshot+history

        # Normalisasi
        oi_score = min(1.0, max(0, oi_growth / 10))
        delta_score = min(1.0, abs(delta_shift) / 20)

        if vol_spike is None:
            vol_score = 0.0  # neutral — unknown, not "no spike"
        else:
            vol_score = min(1.0, (vol_spike - 0.5) / 1.5) if vol_spike > 0.5 else 0.0
            available += 1

        if shock is None:
            shock_score = 0.0  # neutral — unknown, not "no shock"
        else:
            shock_score = min(1.0, shock / 100)
            available += 1

        max_oi = max(snapshot.oi.values()) if snapshot.oi else 1
        oi_abs_score = min(1.0, oi_usd / max_oi) if max_oi > 0 else 0

        # Sektor strength (dinamis)
        sector = get_coin_sector(coin)
        sector_strength = 0.0
        if sector:
            top_sector, top_score = get_top_narrative()
            if sector == top_sector:
                sector_strength = min(1.0, top_score / 100)  # top_score sudah dalam persen

        raw_score = (
            0.30 * oi_score +
            0.25 * delta_score +
            0.20 * vol_score +
            0.15 * shock_score +
            0.10 * oi_abs_score
        ) * (1 + 0.15 * sector_strength)  # bonus dinamis

        return {
            "score": min(1.0, raw_score),
            "coverage": available / expected,
            "components": {
                "oi": oi_score,
                "delta": delta_score,
                "volume": vol_score,
                "shock": shock_score,
                "oi_abs": oi_abs_score,
                "sector_strength": sector_strength
            }
        }

    def _compute_confidence(self, coin: str, snapshot: MarketSnapshot, raw: Dict) -> float:
        """Confidence = base data availability (OI/delta/OI-abs, always cheap)
        scaled by coverage of the optional expensive-but-cached components
        (volume spike, shock score). Missing optional data lowers confidence
        proportionally — it does NOT compound per-component (independent
        unknowns shouldn't be punished multiplicatively for calm markets
        where cache just hasn't built up yet)."""
        has_oi = snapshot.oi.get(coin, 0) > 0.25
        with _rolling_delta_lock:
            has_delta = len(_rolling_delta.get(coin, deque())) > 3
        base_confidence = (has_oi + has_delta) / 2

        coverage = raw.get("coverage", 0.6)  # 3/5 baseline if not provided
        confidence = base_confidence * (0.7 + coverage * 0.3)
        return min(1.0, confidence)

    def _get_stale_state(self, coin: str) -> AttentionState:
        """Return existing state with decay if coin not in snapshot."""
        with self._lock:
            old = self._states.get(coin)
            if old is None:
                return AttentionState(0.0, 0.0, 0.0, 0.0, {}, time.time())
            # decay
            new_score = old.score * 0.95
            return AttentionState(
                score=new_score,
                confidence=old.confidence * 0.9,
                momentum=old.momentum * 0.9,
                freshness=old.freshness * 0.9,
                components=old.components,
                ts=time.time()
            )

    def get_watchlist(self, snapshot: MarketSnapshot, max_watch: int = 80) -> List[str]:
        """Select coins based on distribution threshold."""
        if not snapshot:
            return []

        # Update semua coin di snapshot
        for coin in snapshot.mids:
            if snapshot.oi.get(coin, 0) > 0.25:
                self.update(coin, snapshot)

        with self._lock:
            scores = [(c, s.score) for c, s in self._states.items() if s.confidence > 0.3]
            if not scores:
                return []

            sorted_scores = sorted(scores, key=lambda x: x[1], reverse=True)
            all_values = [s for _, s in sorted_scores]
            if len(all_values) < 5:
                threshold = 0.0
            else:
                # threshold = P75 atau mean+std, mana yang lebih rendah (agar tidak terlalu ketat)
                p75 = np.percentile(all_values, 75)
                mean = np.mean(all_values)
                std = np.std(all_values)
                threshold = min(p75, mean + std)
                threshold = max(0.05, min(0.5, threshold))

            selected = [c for c, s in sorted_scores if s >= threshold]
            if len(selected) > max_watch:
                selected = selected[:max_watch]

            # Logging
            if should_log("DEVELOPER"):
                logger.debug(f"AttentionEngine V2: {len(selected)} selected (th={threshold:.3f}, total={len(scores)})")
                for c, s in sorted_scores[:8]:
                    comp = self._states[c].components
                    logger.debug(f"  {c}: {s:.3f} | OI:{comp['oi']:.2f} Δ:{comp['delta']:.2f} Vol:{comp['volume']:.2f}")

            return selected

    def get_confidence(self, coin: str) -> float:
        """Return confidence for a coin."""
        with self._lock:
            state = self._states.get(coin)
            return state.confidence if state else 0.0

# Global instance
_attention_engine_v2 = AttentionEngineV2()


def bootstrap():
    """Proper startup order - RUN ONCE BEFORE ENGINE"""
    logger.info("🤖 BOOTSTRAP STARTING...🚀")
    
    # ===== STEP 1: DATABASE =====
    logger.info("  ├─ Step 1/6: Database init...")
    init_db()
    ensure_signals_schema()
    migrate_evidence_families_column()
    migrate_context_log_coin_column()
    migrate_signals_leverage_column()
    migrate_score_calibration_columns()
    migrate_quality_conviction_columns()
    migrate_p450_conviction_mem_columns()
    migrate_entry_quality_column()
    migrate_l4_columns()
    migrate_alert_snapshot_columns()
    detect_signal_score_column()
    
    # ===== STEP 1.5: WEBSOCKET INIT (non-fatal, fallback ke REST) =====
    logger.info("  ├─ Step 1.5/6: WebSocket init...")
    try:
        init_websocket()
    except Exception as e:
        logger.error(f"  └─ WS init failed, bot lanjut via REST only: {e}")
    
    # ===== STEP 2: RESTORE OPEN TRADES =====
    logger.info("  ├─ Step 2/6: Restoring open trades from DB...")
    restore_open_trades()
    
    # ===== STEP 2.5: RECONCILE (NEW!) =====
    logger.info("  ├─ Step 2.5/6: Reconciling DB with TradeManager...")
    reconcile_open_positions()
    
    # ===== STEP 2.7: RESTORE ORPHANS (NEW) =====
    logger.info("  ├─ Step 2.7/6: Restoring orphans to TradeManager...")
    orphan_result = restore_orphans(limit=300)
    logger.info(f"  └─ Orphan restore: {orphan_result}")
    
    # ===== STEP 2.6: MIGRATE JOURNAL =====
    logger.info("  ├─ Step 2.6/6: Migrating journal entries...")
    migrate_journal_entries()
    
    # ===== STEP 3: AUDIT =====
    logger.info("  ├─ Step 3/6: Auditing trade state post-restore...")
    audit_result = audit_trade_state()
    
    # === NEW: RESTORE SUMMARY LOG ===
    managed_count = len(TRADE_MANAGER.positions)
    logger.info(f"RESTORE_SUMMARY managed={managed_count} db_pending={audit_result['db_open']} orphan={audit_result['orphan_count']}")
    
    # ===== STEP 4: MARKET DATA =====
    logger.info("  ├─ Step 4/6: Fetching market data...")
    snapshot = refresh_snapshot()
    if snapshot:
        sanitize_maps_from_snapshot(snapshot)

    # ===== STEP 4.2: WS WATCHLIST SUBSCRIBE (fix: snapshot sekarang ready) =====
    if snapshot:
        try:
            ws_subscribe_watchlist(snapshot, top_n=8, candle_timeframes=("5m", "1h"))
        except Exception as e:
            logger.error(f"  └─ WS watchlist subscribe failed (non-fatal): {e}")

    # ===== STEP 4.5: PROCESS RESTORED ORPHANS (NEW) =====
    # restore_orphans() di Step 2.7 cuma register ke TRADE_MANAGER, belum
    # dicek TP/SL-nya. Tanpa ini, posisi yg di-restore nunggu nganggur
    # sampai cycle engine pertama jalan (bisa beberapa menit kalau snapshot
    # warmup lama). Proses langsung begitu snapshot ready.
    if orphan_result.get('restored', 0) > 0:
        logger.info(f"  ├─ Processing {orphan_result['restored']} restored positions...")
        if snapshot:
            try:
                closed = TRADE_MANAGER.check_all_positions(snapshot)
                logger.info(f"  └─ Processed restored positions, {len(closed)} closed immediately")
            except Exception as e:
                logger.error(f"  └─ check_all_positions on restored orphans failed: {e}")
        else:
            logger.warning("  └─ No snapshot yet, restored positions will be checked next engine cycle")
    
    # ===== STEP 5: WARMUP HISTORIES =====
    # FIX: 5x refresh_snapshot() cuma dikasih jeda 0.5s bikin API kena
    # cooldown/rate-limit SEBELUM step 5.5 (preload candles) sempat jalan,
    # jadi candle preload banyak yang ke-skip percuma. Naikin jeda + skip
    # refresh kalau snapshot masih fresh dari cache.
    logger.info("  ├─ Step 5/6: Warming up histories...")
    for i in range(5):
        cached_snap = CACHE.get("snapshot", max_age=10)
        if not cached_snap:
            refresh_snapshot()
        logger.debug(f"     Warmup {i+1}/5")
        time.sleep(1.5)

    # FIX: Kalau API lagi cooldown abis warmup, tunggu bentar biar preload
    # candles di step 5.5 gak langsung ke-skip semua.
    if not can_call_api():
        wait_s = api_cooldown_remaining()
        if wait_s > 0:
            logger.info(f"  ├─ API on cooldown, waiting {wait_s:.1f}s before candle preload...")
            time.sleep(min(wait_s + 0.5, 20))

    # ===== STEP 5.5: PRELOAD CANDLES (P4) =====
    # Isi cache candle 1h buat top coin by OI SEBELUM engine thread mulai,
    # biar cycle scan pertama gak nemu 229/230 coin dengan cache kosong
    # total (yang sebelumnya bikin hampir semua candidate ke-skip
    # "cache_too_old" di deep scan pertama).
    logger.info("  ├─ Step 5.5/6: Preloading candles for top coins...")
    try:
        warm_snapshot = CACHE.get("snapshot")
        if warm_snapshot and warm_snapshot.oi:
            top_by_oi = sorted(warm_snapshot.oi.items(), key=lambda x: x[1], reverse=True)[:15]
            preload_coins = [c for c, _ in top_by_oi if c in warm_snapshot.mids]
            if "BTC" not in preload_coins and "BTC" in warm_snapshot.mids:
                preload_coins.insert(0, "BTC")
            if "ETH" not in preload_coins and "ETH" in warm_snapshot.mids:
                preload_coins.insert(1, "ETH")
            preloaded = preload_candles_startup(preload_coins, "1h", 100)
            logger.info(f"  └─ Preloaded candles for {preloaded}/{len(preload_coins)} coins")
        else:
            logger.warning("  └─ No snapshot yet, skip candle preload (will warm up via cache_warmer thread)")
    except Exception as e:
        logger.error(f"  └─ Candle preload failed (non-fatal): {e}")
    
    # === POST RECON LOG ===
    logger.info(f"POST_RECON managed={len(TRADE_MANAGER.positions)}")
    
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
                _boost_mult = compute_tp_rr_boost(coin=coin, regime=regime, final_score=0)
                targets = calculate_scaled_targets(entry, direction, atr_pct, regime, rr_multiplier=_boost_mult)
                
                _restore_lev = compute_suggested_leverage(coin, entry, sl, 1.0)["suggested"]
                TRADE_MANAGER.add_position(
                    signal_id=signal_id,
                    coin=coin,
                    direction=direction,
                    entry=entry,
                    sl=sl,
                    tp_targets=targets,
                    entry_time=float(ts) if ts else time.time(),
                    entry_atr_pct=atr_pct,
                    leverage=_restore_lev,
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
        threading.Thread(target=scheduled_state_engine_v12, daemon=True),
        threading.Thread(target=scheduled_trigger_engine_v7, daemon=True),
        threading.Thread(target=scheduled_shadow_evaluation_v7, daemon=True),
        threading.Thread(target=scheduled_cleanup_v7, daemon=True),
        threading.Thread(target=monitor_pending_setups_v6, daemon=True),
        threading.Thread(target=cleanup_memory_v10, daemon=True, name="mem_cleanup"),
        threading.Thread(target=_db_writer_loop, daemon=True, name="db_writer"),
        threading.Thread(target=log_snapshot_metrics, daemon=True, name="metrics_logger"),
        threading.Thread(target=summary_loop, daemon=True, name="summary"),
        threading.Thread(target=_tg_sender_loop, daemon=True, name="tg_sender"),  # FIX: async telegram
        threading.Thread(target=auto_heal_orphans, daemon=True, name="auto_heal"),  # PATCH v10.3.3
        threading.Thread(target=cache_warmer_loop, daemon=True, name="cache_warmer"),  # P5
    ]
    for t in threads:
        t.start()
    
    # ===== START POLLING (SATU WHILE LOOP AJA) =====
    poll_failures = 0
    while RUNTIME.is_running():
        try:
            logger.info(f"Starting bot polling V10... (failures so far: {poll_failures})")
            bot.polling(non_stop=True, timeout=30, long_polling_timeout=30)
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
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT coin FROM journal WHERE timestamp > ? GROUP BY coin ORDER BY COUNT(*) DESC LIMIT ?", 
                      (int(time.time()) - 86400 * 3, limit))
            rows = c.fetchall()
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

def _is_warmup_data_driven() -> bool:
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

