# hyperliquid/__init__.py

from typing import Optional, List

from .api import HyperliquidAPI
from .wallet import HyperliquidWallet
from .orders import HyperliquidOrders
from .positions import HyperliquidPositions
from .parsers import HyperliquidParser
from production.provider import Provider
from production.models import Wallet, Order, ExecutionResult, ExchangePosition, PositionSnapshot


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
        """
        Connect beneran ke Hyperliquid (Info + Exchange client via SDK).
        Sebelumnya ini cuma set self._connected = True tanpa pernah manggil
        self._api.connect() — akibatnya HyperliquidAPI._info/._exchange tetap
        None selamanya walau is_connected() bilang True, dan get_wallet()
        akan selalu balikin kosong/0 (user_state() no-op kalau _info None).
        Itu bikin ProductionEngine.initialize() auto-fallback diam-diam ke
        PaperProvider (karena wallet.usdc_balance <= 0), padahal alasannya
        bukan "testnet belum di-fund" tapi karena connect() ini gak pernah
        beneran jalan.
        """
        self._connected = self._api.connect()
        return self._connected
    
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
    
    def update_stop_loss(self, coin: str, old_order_id, new_price: float, size: float) -> ExecutionResult:
        return self._orders.update_stop_loss(coin, old_order_id, new_price, size)
    
    def get_positions(self) -> List[ExchangePosition]:
        return self._positions.get_all()
    
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        return self._positions.close(coin, side)
    
    def sync(self) -> PositionSnapshot:
        return self._positions.sync()
