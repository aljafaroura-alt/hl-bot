# production/providers/paper.py
# ============================================================
# PAPER PROVIDER — Simulasi, No Real Orders
# ============================================================

import time
from typing import Optional, List, Dict

from ..provider import Provider
from ..models import (
    Wallet,
    Order,
    OrderStatus,
    ExecutionResult,
    ExchangePosition,
    PositionSnapshot,
)


class PaperProvider(Provider):
    """Paper trading provider — simulasi, no real orders."""
    
    def __init__(self):
        self._connected = True
        self._balance = 10000.0  # $10k paper money
        self._positions: List[ExchangePosition] = []
        self._orders: Dict[str, Order] = {}
        self._order_counter = 0
    
    def connect(self) -> bool:
        self._connected = True
        return True
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_wallet(self) -> Optional[Wallet]:
        return Wallet(
            address="PAPER",
            usdc_balance=self._balance,
            total_margin=self._balance,
            used_margin=0.0,
            free_margin=self._balance,
            equity=self._balance,
        )
    
    def place_order(self, order: Order) -> ExecutionResult:
        self._order_counter += 1
        order_id = f"paper_{self._order_counter}"
        order.order_id = order_id
        order.status = OrderStatus.FILLED
        order.filled_price = order.price
        order.filled_size = order.size
        
        self._orders[order_id] = order
        
        # Simulate position
        if not order.reduce_only:
            pos = ExchangePosition(
                coin=order.coin,
                side="long" if order.side.value == "buy" else "short",
                size=order.size,
                entry_price=order.price,
                mark_price=order.price,
                liquidation_price=order.price * 0.8 if order.side.value == "buy" else order.price * 1.2,
                unrealized_pnl=0.0,
                margin_used=order.size_usdc,
            )
            self._positions.append(pos)
        
        return ExecutionResult(
            success=True,
            order_id=order_id,
            filled_price=order.price,
            filled_size=order.size,
        )
    
    def cancel_order(self, order_id: str) -> ExecutionResult:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return ExecutionResult(success=True, order_id=order_id)
        return ExecutionResult(success=False, error="order_not_found")
    
    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)
    
    def place_stop_loss(self, coin: str, price: float, size: float) -> ExecutionResult:
        return ExecutionResult(success=True, order_id=f"sl_{int(time.time())}")
    
    def place_take_profit(self, coin: str, price: float, size: float) -> ExecutionResult:
        return ExecutionResult(success=True, order_id=f"tp_{int(time.time())}")
    
    def get_positions(self) -> List[ExchangePosition]:
        return self._positions
    
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        self._positions = [p for p in self._positions if not (p.coin == coin and p.side == side)]
        return ExecutionResult(success=True)
    
    def sync(self) -> PositionSnapshot:
        wallet = self.get_wallet()
        return PositionSnapshot(
            positions=self._positions,
            wallet=wallet,
                                                               )
