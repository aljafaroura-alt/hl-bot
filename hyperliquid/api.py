# hyperliquid/api.py
# ============================================================
# HYPERLIQUID API — Skeleton
# ============================================================

import time
from typing import Optional, Dict, Any


class HyperliquidAPI:
    """Low-level Hyperliquid API."""
    
    BASE_URL_MAINNET = "https://api.hyperliquid.xyz"
    BASE_URL_TESTNET = "https://api.hyperliquid-testnet.xyz"
    
    def __init__(self, mode: str = "testnet"):
        self._mode = mode
        self._base_url = self._get_base_url()
        self._session = None
        self._rate_limit = 0.1
    
    def _get_base_url(self) -> str:
        if self._mode == "testnet":
            return self.BASE_URL_TESTNET
        return self.BASE_URL_MAINNET
    
    # === PUBLIC ===
    def info(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Public info endpoint."""
        # TODO: Implement
        return {}
    
    def all_mids(self) -> Dict[str, float]:
        """Get all mids."""
        # TODO: Implement
        return {}
    
    # === PRIVATE (authenticated) ===
    def exchange(self, endpoint: str, payload: Dict) -> Dict[str, Any]:
        """Private exchange endpoint."""
        # TODO: Implement with wallet auth
        return {}
    
    def place_order(self, payload: Dict) -> Dict:
        """Place an order."""
        return self.exchange("placeOrder", payload)
    
    def cancel_order(self, payload: Dict) -> Dict:
        """Cancel an order."""
        return self.exchange("cancelOrder", payload)
    
    def modify_order(self, payload: Dict) -> Dict:
        """Modify an order."""
        return self.exchange("modifyOrder", payload)

