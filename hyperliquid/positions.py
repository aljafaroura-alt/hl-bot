# hyperliquid/positions.py
# ============================================================
# HYPERLIQUID POSITIONS — Skeleton
# ============================================================

from typing import List
from production.models import ExchangePosition, ExecutionResult, PositionSnapshot, Wallet


class HyperliquidPositions:
    """Position management for Hyperliquid."""
    
    def __init__(self, api):
        self._api = api
    
    def get_all(self) -> List[ExchangePosition]:
        """Get all open positions."""
        # TODO: Implement
        return []
    
    def close(self, coin: str, side: str) -> ExecutionResult:
        """Close a position."""
        return ExecutionResult(
            success=False,
            error="not_implemented",
        )
    
    def sync(self) -> PositionSnapshot:
        """Full sync."""
        # TODO: Implement
        wallet = Wallet(
            address="0x...",
            usdc_balance=1000.0,
            total_margin=1000.0,
            used_margin=0.0,
            free_margin=1000.0,
            equity=1000.0,
        )
        return PositionSnapshot(positions=[], wallet=wallet)
