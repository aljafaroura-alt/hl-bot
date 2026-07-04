# production/providers/hyperliquid/__init__.py

from .api import HyperliquidAPI
from .wallet import HyperliquidWallet
from .orders import HyperliquidOrders
from .positions import HyperliquidPositions
from .parser import HyperliquidParser
from ..provider import Provider
from ...models import Wallet, Order, ExecutionResult, ExchangePosition, PositionSnapshot


class HyperliquidProvider(Provider):
    """Hyperliquid provider implementation."""
    
    def __init__(self, mode: str = "testnet"):
        self._mode = mode
        self._connected = False
        self._api = HyperliquidAPI(mode)
        self._wallet = HyperliquidWallet(self._api)
        self._orders = HyperliquidOrders(self._api)
        self._positions = HyperliquidPositions(self._api)
        self._parser = HyperliquidParser()
    
    def connect(self) -> bool:
        # TODO: Implement actual connection
        self._connected = True
        return True
    
    def is_connected(self) -> bool:
        return self._connected
    
    def get_wallet(self) -> Optional[Wallet]:
        return self._wallet.get()
    
    def place_order(self, order: Order) -> ExecutionResult:
        return self._orders.place(order)
    
    def cancel_order(self, order_id: str) -> ExecutionResult:
        return self._orders.cancel(order_id)
    
    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)
    
    def place_stop_loss(self, coin: str, price: float, size: float) -> ExecutionResult:
        return self._orders.place_stop_loss(coin, price, size)
    
    def place_take_profit(self, coin: str, price: float, size: float) -> ExecutionResult:
        return self._orders.place_take_profit(coin, price, size)
    
    def get_positions(self) -> List[ExchangePosition]:
        return self._positions.get_all()
    
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        return self._positions.close(coin, side)
    
    def sync(self) -> PositionSnapshot:
        return self._positions.sync()
