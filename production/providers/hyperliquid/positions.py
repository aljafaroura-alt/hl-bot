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
        """
        Close a position pakai market order (SDK helper market_close).

        PENTING soal resp == None: SDK hyperliquid-python-sdk return None
        (bukan raise exception, bukan dict) dari market_close() kalau pas
        dipanggil ternyata posisi coin ini UDAH GA ADA di exchange — biasanya
        karena salah satu trigger order (SL/TP) yang masih nempel di exchange
        udah ke-trigger duluan dan nutup posisi itu sebelum Brain sempat
        kirim close eksplisit ini. Ini BUKAN kegagalan close — posisinya
        emang udah tertutup, yang notabene itu tujuan akhir method ini.
        Kalau resp==None di-treat sebagai error (resp.get(...) langsung),
        exception 'NoneType' object has no attribute 'get' bakal nutupin
        fakta bahwa posisi sebenarnya sudah aman tertutup, dan alert ke user
        jadi 'GAGAL CLOSE — cek manual' padahal exchange-nya udah beres.
        Kasus ini return success=True, already_closed=True.
        """
        try:
            resp = self._api.close_position_market(coin)

            if resp is None:
                logger.info(
                    f"ℹ️ close_position_market({coin}) return None — posisi "
                    f"kemungkinan udah ke-close duluan di exchange (SL/TP lama "
                    f"ke-trigger). Treat sebagai already_closed, bukan error."
                )
                return ExecutionResult(success=True, already_closed=True)

            if resp.get("status") != "ok":
                return ExecutionResult(success=False, error=str(resp))

            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" in statuses[0]:
                err_msg = str(statuses[0]["error"])
                # SDK/exchange balikin pesan error spesifik ini kalau posisi
                # yang mau ditutup ternyata udah size=0 — sinyal yang sama
                # dengan resp is None di atas, cuma lewat jalur response
                # ber-status "ok" tapi statuses[0] berisi error.
                if "no position" in err_msg.lower() or "position not found" in err_msg.lower():
                    logger.info(
                        f"ℹ️ close_position_market({coin}) bilang posisi udah "
                        f"gak ada — treat sebagai already_closed: {err_msg}"
                    )
                    return ExecutionResult(success=True, already_closed=True)
                return ExecutionResult(success=False, error=err_msg)

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
        

