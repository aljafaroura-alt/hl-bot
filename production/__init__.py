# production/__init__.py
# ============================================================
# PRODUCTION LAYER — Public API
# ============================================================

from .config import ProductionConfig, EntryMode, OpenMode
from .models import (
    Decision,
    Wallet,
    Order,
    OrderSide,
    OrderType,
    OrderStatus,
    ExecutionResult,
    ExchangePosition,
    PositionSnapshot,
)
from .engine import ProductionEngine
from .provider import Provider
from providers.paper import PaperProvider

# NOTE: HyperliquidProvider SENGAJA gak di-import eager di sini.
# hyperliquid/__init__.py butuh production.models & production.provider saat
# loading, jadi kalau production eager-import HyperliquidProvider balik →
# circular import ("partially initialized module"). ProductionEngine sendiri
# udah import HyperliquidProvider secara lazy (di dalam initialize()).
# Kalau butuh akses langsung: `from production.providers.hyperliquid import HyperliquidProvider`.


def __getattr__(name):
    if name == "HyperliquidProvider":
        from production.providers.hyperliquid import HyperliquidProvider
        return HyperliquidProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ProductionConfig", "EntryMode", "OpenMode",
    "Decision", "Wallet", "Order", "OrderSide", "OrderType", "OrderStatus",
    "ExecutionResult", "ExchangePosition", "PositionSnapshot",
    "ProductionEngine",
    "Provider", "PaperProvider", "HyperliquidProvider",
]
