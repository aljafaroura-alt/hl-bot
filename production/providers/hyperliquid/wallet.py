# hyperliquid/wallet.py  (production/providers/hyperliquid/wallet.py)
# ============================================================
# HYPERLIQUID WALLET
# ============================================================

import logging
from production.models import Wallet

logger = logging.getLogger("HyperliquidWallet")


class HyperliquidWallet:
    """Wallet state from Hyperliquid."""

    def __init__(self, api):
        self._api = api

    def get(self) -> Wallet:
        """Get current wallet state dari user_state()."""
        try:
            state = self._api.user_state()
            if not state:
                return Wallet(
                    address=self._api.address or "0x0",
                    usdc_balance=0.0,
                    total_margin=0.0,
                    used_margin=0.0,
                    free_margin=0.0,
                    equity=0.0,
                )

            margin_summary = state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0.0))
            total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))

            # withdrawable = margin bebas yang bisa dipakai buat posisi baru
            free_margin = float(state.get("withdrawable", account_value - total_margin_used))

            return Wallet(
                address=self._api.address or "0x0",
                usdc_balance=account_value,
                total_margin=account_value,
                used_margin=total_margin_used,
                free_margin=free_margin,
                equity=account_value,
            )

        except Exception as e:
            logger.error(f"❌ Failed to get wallet state: {e}")
            return Wallet(
                address=self._api.address or "0x0",
                usdc_balance=0.0,
                total_margin=0.0,
                used_margin=0.0,
                free_margin=0.0,
                equity=0.0,
            )
            

