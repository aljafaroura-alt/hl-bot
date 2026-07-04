# production/models.py
# ============================================================
# PRODUCTION MODELS
# ============================================================

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
import time

from .config import OrderSide, OrderType, OrderStatus, EntryMode, OpenMode


@dataclass
class Decision:
    """
    Decision dari Layer 4 (Brain) ke Layer 5 (Production).
    
    Ini satu-satunya kontrak antara Brain dan Production.
    Production gak perlu tau apapun tentang score, confidence, regime, flow, dll.
    """
    # === IDENTITY ===
    signal_id: str                           # "BTC_LONG_1234567890"
    coin: str                                # "GRAM", "DOGE", "BTC", etc
    
    # === THESIS ===
    direction: str                           # "LONG" or "SHORT"
    entry: float                             # harga eksekusi
    sl: float                                # stop loss
    tp: float                                # take profit (primary)
    leverage: float                          # leverage yang dipakai
    
    # === EXECUTION MODE (bisa override global config) ===
    entry_mode: Optional[EntryMode] = None   # override global
    open_mode: Optional[OpenMode] = None     # override global
    
    # === TRUTH / AUDIT ===
    truth_mode: bool = False                 # True = FIXED, False = ADAPTIVE
    
    # === METADATA ===
    timestamp: float = field(default_factory=time.time)


@dataclass
class Wallet:
    """Wallet state dari exchange."""
    address: str
    usdc_balance: float
    total_margin: float
    used_margin: float
    free_margin: float
    equity: float
    unrealized_pnl: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Order:
    """Order representation."""
    order_id: str
    coin: str
    side: OrderSide
    order_type: OrderType
    price: float
    size: float                          # in coin units
    size_usdc: float                     # in USDC
    filled_size: float = 0.0
    filled_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp: float = field(default_factory=time.time)
    reduce_only: bool = False
    error: Optional[str] = None


@dataclass
class ExchangePosition:
    """Position as seen by the exchange."""
    coin: str
    side: str                             # "long" or "short"
    size: float                           # in coin units
    entry_price: float
    mark_price: float
    liquidation_price: float
    unrealized_pnl: float
    margin_used: float
    leverage: float = 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExecutionResult:
    """Result of an order execution."""
    success: bool
    order_id: Optional[str] = None
    filled_price: float = 0.0
    filled_size: float = 0.0
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    
    # Untuk open position lengkap
    position: Optional[ExchangePosition] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None


@dataclass
class PositionSnapshot:
    """Full position state snapshot."""
    positions: List[ExchangePosition]
    wallet: Wallet
    timestamp: float = field(default_factory=time.time)
