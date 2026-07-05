# hyperliquid/orders.py  (production/providers/hyperliquid/orders.py)
# ============================================================
# HYPERLIQUID ORDERS
# ============================================================

import logging
from production.models import Order, OrderSide, OrderStatus, ExecutionResult

logger = logging.getLogger("HyperliquidOrders")


class HyperliquidOrders:
    """Order management for Hyperliquid."""

    def __init__(self, api):
        self._api = api

    def place(self, order: Order) -> ExecutionResult:
        """Place an entry order (limit)."""
        try:
            is_buy = order.side == OrderSide.BUY
            resp = self._api.place_limit_order(
                coin=order.coin,
                is_buy=is_buy,
                size=order.size,
                price=order.price,
                reduce_only=order.reduce_only,
            )
            return self._parse_order_response(resp, order.price, order.size)

        except Exception as e:
            logger.error(f"❌ place_order failed {order.coin}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def cancel(self, order_id: str) -> ExecutionResult:
        """Cancel an order. order_id format: 'COIN:oid' (lihat _encode_order_id)."""
        try:
            coin, oid = self._decode_order_id(order_id)
            resp = self._api.cancel_order(coin, oid)
            ok = resp.get("status") == "ok"
            return ExecutionResult(success=ok, order_id=order_id, error=None if ok else str(resp))
        except Exception as e:
            logger.error(f"❌ cancel_order failed {order_id}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def get(self, order_id: str) -> Order:
        """Get order status dari open_orders(). None kalau udah gak ada (filled/cancelled)."""
        try:
            coin, oid = self._decode_order_id(order_id)
            for o in self._api.open_orders():
                if o.get("oid") == oid:
                    return Order(
                        order_id=order_id,
                        coin=o.get("coin", coin),
                        side=OrderSide.BUY if o.get("side") == "B" else OrderSide.SELL,
                        order_type=None,
                        price=float(o.get("limitPx", 0.0)),
                        size=float(o.get("sz", 0.0)),
                        size_usdc=float(o.get("sz", 0.0)) * float(o.get("limitPx", 0.0)),
                        status=OrderStatus.OPEN,
                    )
            return None
        except Exception as e:
            logger.error(f"❌ get_order failed {order_id}: {e}")
            return None

    def place_stop_loss(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a stop loss trigger order (market, reduce-only)."""
        try:
            is_buy_close = self._get_close_side(coin)
            if is_buy_close is None:
                return ExecutionResult(success=False, error=f"no_open_position_for_{coin}")
            resp = self._api.place_trigger_order(
                coin=coin,
                is_buy=is_buy_close,
                size=size,
                trigger_price=price,
                tpsl="sl",
                reduce_only=True,
            )
            return self._parse_order_response(resp, price, size)
        except Exception as e:
            logger.error(f"❌ place_stop_loss failed {coin}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def place_take_profit(self, coin: str, price: float, size: float) -> ExecutionResult:
        """Place a take profit trigger order (market, reduce-only)."""
        try:
            is_buy_close = self._get_close_side(coin)
            if is_buy_close is None:
                return ExecutionResult(success=False, error=f"no_open_position_for_{coin}")
            resp = self._api.place_trigger_order(
                coin=coin,
                is_buy=is_buy_close,
                size=size,
                trigger_price=price,
                tpsl="tp",
                reduce_only=True,
            )
            return self._parse_order_response(resp, price, size)
        except Exception as e:
            logger.error(f"❌ place_take_profit failed {coin}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def _get_close_side(self, coin: str):
        """
        SL/TP harus CLOSE posisi yang lagi terbuka:
          posisi LONG (szi > 0)  -> ditutup dengan SELL -> is_buy=False
          posisi SHORT (szi < 0) -> ditutup dengan BUY  -> is_buy=True
        Return None kalau posisi coin ini gak ketemu/ga ada size.
        """
        state = self._api.user_state()
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                szi = float(pos.get("szi", 0.0))
                if szi == 0.0:
                    return None
                return szi < 0  # True (buy) kalau posisi short
        return None

    # === HELPERS ===
    def _parse_order_response(self, resp: dict, price: float, size: float) -> ExecutionResult:
        if resp.get("status") != "ok":
            return ExecutionResult(success=False, error=str(resp))

        try:
            statuses = resp["response"]["data"]["statuses"]
            first = statuses[0]
            if "error" in first:
                return ExecutionResult(success=False, error=first["error"])

            if "filled" in first:
                filled = first["filled"]
                oid = filled.get("oid")
                return ExecutionResult(
                    success=True,
                    order_id=self._encode_order_id(filled.get("coin", ""), oid),
                    filled_price=float(filled.get("avgPx", price)),
                    filled_size=float(filled.get("totalSz", size)),
                )

            if "resting" in first:
                resting = first["resting"]
                oid = resting.get("oid")
                return ExecutionResult(
                    success=True,
                    order_id=self._encode_order_id(resting.get("coin", ""), oid),
                    filled_price=0.0,
                    filled_size=0.0,
                )

            return ExecutionResult(success=False, error=f"unrecognized_status: {first}")

        except (KeyError, IndexError) as e:
            return ExecutionResult(success=False, error=f"parse_error: {e} raw={resp}")

    @staticmethod
    def _encode_order_id(coin: str, oid) -> str:
        return f"{coin}:{oid}"

    @staticmethod
    def _decode_order_id(order_id: str):
        coin, oid = order_id.split(":", 1)
        return coin, int(oid)
        
