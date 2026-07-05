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
        """
        Get current wallet state. Gabungin 2 sumber:
        - spotClearinghouseState -> saldo USDC beneran (buat akun mode
          Unified/Portfolio Margin, INI yang jadi sumber balance asli)
        - clearinghouseState (perps) -> totalMarginUsed, buat tau berapa
          yang lagi ke-lock di posisi terbuka
        """
        try:
            perp_state = self._api.user_state() or {}
            spot_state = self._api.spot_user_state() or {}

            # === DEBUG: dump raw response, buat diagnosa saldo ===
            import json
            logger.warning("========== RAW USER STATE (perp) ==========")
            logger.warning(f"address_queried={self._api.address}")
            logger.warning(json.dumps(perp_state, indent=2, default=str))
            logger.warning("========== RAW USER STATE (spot) ==========")
            logger.warning(json.dumps(spot_state, indent=2, default=str))
            logger.warning("====================================")

            # Cari saldo USDC di spot balances
            spot_usdc = 0.0
            for bal in spot_state.get("balances", []):
                if bal.get("coin") == "USDC":
                    spot_usdc = float(bal.get("total", 0.0))
                    break

            margin_summary = perp_state.get("marginSummary", {})
            perp_account_value = float(margin_summary.get("accountValue", 0.0))
            total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))

            # Mode Unified: saldo asli ada di spot. Kalau spot 0 tapi perp
            # ada isinya (akun mode Manual/Standard, bukan Unified), pakai
            # perp accountValue sebagai fallback.
            usdc_balance = spot_usdc if spot_usdc > 0 else perp_account_value
            equity = usdc_balance  # margin yang lagi dipakai tetap "milik" akun, jadi equity = total saldo
            free_margin = usdc_balance - total_margin_used

            return Wallet(
                address=self._api.address or "0x0",
                usdc_balance=usdc_balance,
                total_margin=equity,
                used_margin=total_margin_used,
                free_margin=free_margin,
                equity=equity,
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
