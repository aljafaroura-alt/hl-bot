# hyperliquid/positions.py  (production/providers/hyperliquid/positions.py)
# ============================================================
# HYPERLIQUID POSITIONS
# ============================================================

import logging
from typing import List
from production.models import ExchangePosition, ExecutionResult, PositionSnapshot

logger = logging.getLogger("HyperliquidPositions")


class HyperliquidPositions:
    """Position management for Hyperliquid."""

    def __init__(self, api):
        self._api = api

    def get_all(self) -> List[ExchangePosition]:
        """Get all open positions dari user_state()['assetPositions']."""
        try:
            state = self._api.user_state()
            positions = []
            for ap in state.get("assetPositions", []):
                pos = ap.get("position", {})
                szi = float(pos.get("szi", 0.0))
                if szi == 0.0:
                    continue  # skip, ga ada posisi beneran

                leverage_info = pos.get("leverage", {})
                positions.append(ExchangePosition(
                    coin=pos.get("coin", ""),
                    side="long" if szi > 0 else "short",
                    size=abs(szi),
                    entry_price=float(pos.get("entryPx", 0.0)),
                    mark_price=float(pos.get("positionValue", 0.0)) / abs(szi) if szi else 0.0,
                    liquidation_price=float(pos.get("liquidationPx") or 0.0),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0.0)),
                    margin_used=float(pos.get("marginUsed", 0.0)),
                    leverage=float(leverage_info.get("value", 1.0)),
                ))
            return positions

        except Exception as e:
            logger.error(f"❌ get_positions failed: {e}")
            return []

    def close(self, coin: str, side: str) -> ExecutionResult:
        """Close a position pakai market order (SDK helper market_close)."""
        try:
            resp = self._api.close_position_market(coin)
            if resp.get("status") != "ok":
                return ExecutionResult(success=False, error=str(resp))

            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" in statuses[0]:
                return ExecutionResult(success=False, error=statuses[0]["error"])

            return ExecutionResult(success=True)

        except Exception as e:
            logger.error(f"❌ close_position failed {coin}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def sync(self) -> PositionSnapshot:
        """Full sync — dipanggil dari HyperliquidProvider.sync()."""
        from production.models import Wallet  # local import, hindari cycle di top-level

        positions = self.get_all()
        state = self._api.user_state()
        margin_summary = state.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 0.0))
        total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))

        wallet = Wallet(
            address=self._api.address or "0x0",
            usdc_balance=account_value,
            total_margin=account_value,
            used_margin=total_margin_used,
            free_margin=float(state.get("withdrawable", account_value - total_margin_used)),
            equity=account_value,
        )
        return PositionSnapshot(positions=positions, wallet=wallet)
        

