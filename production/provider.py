# production/provider.py
# ============================================================
# PROVIDER ABSTRACTION
# ============================================================

from abc import ABC, abstractmethod
from typing import Optional, List

from .models import (
    Wallet,
    Order,
    ExecutionResult,
    ExchangePosition,
    PositionSnapshot,
)


class Provider(ABC):
    """Abstract base class untuk exchange provider."""
    
    @abstractmethod
    def connect(self) -> bool:
        """Connect ke exchange."""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check koneksi."""
        pass
    
    # === WALLET ===
    @abstractmethod
    def get_wallet(self) -> Optional[Wallet]:
        """Get wallet state."""
        pass
    
    # === ORDERS ===
    @abstractmethod
    def place_order(self, order: Order) -> ExecutionResult:
        """Place an order."""
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> ExecutionResult:
        """Cancel an order."""
        pass
    
    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order status."""
        pass
    
    # === SL / TP ===
    @abstractmethod
    def place_stop_loss(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a stop loss order."""
        pass
    
    @abstractmethod
    def place_take_profit(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a take profit order."""
        pass
    
    @abstractmethod
    def update_stop_loss(self, coin: str, old_order_id: Optional[str], new_price: float, size: float) -> ExecutionResult:
        """
        Geser SL ke harga baru (trailing). Implementasi: cancel order lama
        (kalau old_order_id ada & masih valid), lalu pasang SL baru di
        new_price. old_order_id boleh None (misal SL awal gagal ke-pasang).
        """
        pass
    
    # === POSITIONS ===
    @abstractmethod
    def get_positions(self) -> List[ExchangePosition]:
        """Get all open positions."""
        pass
    
    @abstractmethod
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        """Close a position."""
        pass
    
    @abstractmethod
    def sync(self) -> PositionSnapshot:
        """Full sync with exchange."""
        pass
