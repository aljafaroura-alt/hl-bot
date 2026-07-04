# production/providers/hyperliquid/wallet.py
# ============================================================
# HYPERLIQUID WALLET — Skeleton
# ============================================================

from ...models import Wallet


class HyperliquidWallet:
    """Wallet state from Hyperliquid."""
    
    def __init__(self, api):
        self._api = api
    
    def get(self) -> Wallet:
        """Get current wallet state."""
        # TODO: Implement
        # 1. Call user_state
        # 2. Parse USDC balance
        # 3. Calculate free/used margin
        
        return Wallet(
            address="0x...",
            usdc_balance=1000.0,
            total_margin=1000.0,
            used_margin=0.0,
            free_margin=1000.0,
            equity=1000.0,
        )
