# production/providers/paper.py
# ============================================================
# PAPER PROVIDER — Simulasi, No Real Orders
# ============================================================

import time
from typing import Optional, List, Dict

from production.provider import Provider
from production.models import (
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
        self._balance = 10000.0       # cash bebas (belum dipakai buat margin)
        self._locked_margin = 0.0     # total margin yang lagi "ketahan" di posisi terbuka
        self._realized_pnl_total = 0.0  # akumulasi PnL realisasi sepanjang sesi ini
        self._starting_balance = 10000.0
        self._positions: List[ExchangePosition] = []
        self._orders: Dict[str, Order] = {}
        self._order_counter = 0
    
    def connect(self) -> bool:
        self._connected = True
        return True
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_wallet(self) -> Optional[Wallet]:
        equity = self._balance + self._locked_margin  # cash bebas + margin ketahan di posisi
        return Wallet(
            address="PAPER",
            usdc_balance=self._balance,
            total_margin=equity,
            used_margin=self._locked_margin,
            free_margin=self._balance,
            equity=equity,
        )
    
    def place_order(self, order: Order) -> ExecutionResult:
        self._order_counter += 1
        order_id = f"paper_{self._order_counter}"
        order.order_id = order_id
        order.status = OrderStatus.FILLED
        order.filled_price = order.price
        order.filled_size = order.size
        
        self._orders[order_id] = order
        
        # Simulate position — kunci margin-nya dari cash bebas
        if not order.reduce_only:
            self._balance -= order.size_usdc
            self._locked_margin += order.size_usdc
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
    
    def update_stop_loss(self, coin: str, old_order_id: Optional[str], new_price: float, size: float) -> ExecutionResult:
        if old_order_id and old_order_id in self._orders:
            self._orders[old_order_id].status = OrderStatus.CANCELLED
        new_id = f"sl_{int(time.time()*1000)}"
        return ExecutionResult(success=True, order_id=new_id)
    
    def get_positions(self) -> List[ExchangePosition]:
        return self._positions
    
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        self._positions = [p for p in self._positions if not (p.coin == coin and p.side == side)]
        return ExecutionResult(success=True)
    
    def apply_realized_pnl(self, coin: str, side: str, pnl_usdc: float) -> float:
        """
        Dipanggil dari ProductionEngine.on_position_closed() pas Brain (main.py)
        nutup posisi. Release margin yang ke-lock + tambah/kurang realized PnL
        ke cash balance. Return balance baru (buat logging).
        """
        released_margin = 0.0
        remaining = []
        found = False
        for p in self._positions:
            if not found and p.coin == coin and p.side == side:
                released_margin = p.margin_used
                found = True
                continue
            remaining.append(p)
        self._positions = remaining

        self._locked_margin -= released_margin
        self._balance += released_margin + pnl_usdc
        self._realized_pnl_total += pnl_usdc
        return self._balance
    
    def sync(self) -> PositionSnapshot:
        wallet = self.get_wallet()
        return PositionSnapshot(
            positions=self._positions,
            wallet=wallet,
        )
