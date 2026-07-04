# hyperliquid/orders.py
# ============================================================
# HYPERLIQUID ORDERS — Skeleton
# ============================================================

from production.models import Order, ExecutionResult


class HyperliquidOrders:
    """Order management for Hyperliquid."""
    
    def __init__(self, api):
        self._api = api
    
    def place(self, order: Order) -> ExecutionResult:
        """Place an order."""
        # TODO: Implement
        return ExecutionResult(
            success=False,
            error="not_implemented",
        )
    
    def cancel(self, order_id: str) -> ExecutionResult:
        """Cancel an order."""
        return ExecutionResult(
            success=False,
            error="not_implemented",
        )
    
    def get(self, order_id: str) -> Order:
        """Get order status."""
        return None
    
    def place_stop_loss(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a stop loss order."""
        return ExecutionResult(
            success=False,
            error="not_implemented",
        )
    
    def place_take_profit(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a take profit order."""
        return ExecutionResult(
            success=False,
            error="not_implemented",
        )
      
