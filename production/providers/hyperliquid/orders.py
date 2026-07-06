# hyperliquid/orders.py  (production/providers/hyperliquid/orders.py)
# ============================================================
# HYPERLIQUID ORDERS
# ============================================================

import logging
from typing import Dict
from production.models import Order, OrderSide, OrderStatus, ExecutionResult
from production.config import OrderType

logger = logging.getLogger("HyperliquidOrders")


class HyperliquidOrders:
    """Order management for Hyperliquid."""

    def __init__(self, api):
        self._api = api

    def place(self, order: Order) -> ExecutionResult:
        """Place an entry order — MARKET (fill instan) atau LIMIT (nangkring), sesuai order.order_type."""
        try:
            is_buy = order.side == OrderSide.BUY

            if order.order_type == OrderType.MARKET:
                resp = self._api.place_market_order(
                    coin=order.coin,
                    is_buy=is_buy,
                    size=order.size,
                    reduce_only=order.reduce_only,
                )
            else:
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

    def update_stop_loss(self, coin: str, old_order_id, new_price: float, size: float) -> ExecutionResult:
        """
        Trailing SL beneran: SWEEP semua open order lama buat coin ini
        (bukan cuma old_order_id yang ditrack di state internal Brain),
        baru pasang SL baru.

        Kenapa sweep-all, bukan cancel 1 order_id doang: kalau 1 tick
        trailing gagal cancel (diam-diam), order_id yang ditrack di state
        Brain jadi stale — trailing_sl tick berikutnya cuma tau order_id
        yang PALING BARU, sisa SL basi sebelumnya ga pernah ke-cancel dan
        numpuk terus di exchange sampai salah satunya beneran ke-trigger.

        PENTING: sweep-all ini ikut nyapu TP yang masih nempel di coin
        yang sama. Makanya method ini TIDAK dipanggil sendirian dari
        trailing tick — caller (engine.py) WAJIB re-arm TP juga sesudahnya,
        lihat Engine.update_stop_loss / _refresh_sl_tp.
        """
        try:
            sweep_resp = self._api.cancel_all_orders_for_coin(coin)
            if sweep_resp.get("status") == "err":
                # Fetch open_orders sendiri gagal total (misal network) — tetap
                # lanjut coba pasang SL baru (best-effort), tapi ini kasus
                # paling beresiko numpuk order basi, jadi di-log keras.
                logger.error(
                    f"❌ sweep cancel gagal total {coin} sebelum SL baru: "
                    f"{sweep_resp.get('error')} — resiko order basi numpuk!"
                )
            elif sweep_resp.get("failed"):
                logger.warning(
                    f"⚠️ sweep cancel {coin}: {len(sweep_resp.get('failed'))} order "
                    f"gagal di-cancel — cek manual, mungkin masih ada order basi."
                )

            return self.place_stop_loss(coin, new_price, size)

        except Exception as e:
            logger.error(f"❌ update_stop_loss failed {coin}: {e}")
            return ExecutionResult(success=False, error=str(e))

    def refresh_sl_and_tp(self, coin: str, sl_price: float, tp_price: float, size: float) -> Dict[str, ExecutionResult]:
        """
        Trailing tick lengkap: sweep SEMUA order lama (SL + TP) buat coin
        ini sekali aja, lalu pasang ULANG SL dan TP bareng.

        Ini pengganti pola lama "update_stop_loss() doang tiap tick" yang
        efek sampingnya nyapu TP tanpa masang ulang -> TP jadi orphan
        selamanya sampai posisi ditutup manual/timeout. Selalu pakai method
        ini (bukan update_stop_loss + place_take_profit terpisah) supaya
        cuma ada SATU sweep per tick, bukan dua kali cancel-all yang bisa
        saling nyenggol race condition di exchange.
        """
        result = {"sl": None, "tp": None, "sweep_failed": []}
        try:
            sweep_resp = self._api.cancel_all_orders_for_coin(coin)
            if sweep_resp.get("status") == "err":
                logger.error(
                    f"❌ sweep cancel gagal total {coin} sebelum refresh SL/TP: "
                    f"{sweep_resp.get('error')} — resiko order basi numpuk!"
                )
            elif sweep_resp.get("failed"):
                result["sweep_failed"] = sweep_resp["failed"]
                logger.warning(
                    f"⚠️ sweep cancel {coin}: {len(sweep_resp['failed'])} order "
                    f"gagal di-cancel — cek manual, mungkin masih ada order basi."
                )

            result["sl"] = self.place_stop_loss(coin, sl_price, size)
            if not result["sl"].success:
                logger.warning(f"⚠️ refresh SL gagal {coin}: {result['sl'].error}")

            result["tp"] = self.place_take_profit(coin, tp_price, size)
            if not result["tp"].success:
                logger.warning(f"⚠️ refresh TP gagal {coin}: {result['tp'].error}")

            return result
        except Exception as e:
            logger.error(f"❌ refresh_sl_and_tp failed {coin}: {e}")
            result["sl"] = result["sl"] or ExecutionResult(success=False, error=str(e))
            result["tp"] = result["tp"] or ExecutionResult(success=False, error=str(e))
            return result

    def _get_close_side(self, coin: str, retries: int = 4, delay: float = 0.4):
        """
        SL/TP harus CLOSE posisi yang lagi terbuka:
          posisi LONG (szi > 0)  -> ditutup dengan SELL -> is_buy=False
          posisi SHORT (szi < 0) -> ditutup dengan BUY  -> is_buy=True
        Return None kalau posisi coin ini beneran gak ketemu/ga ada size
        setelah semua percobaan retry habis.

        BUG FIX: dipanggil detik itu juga setelah entry market order fill
        (lihat Engine.execute() STEP 6). user_state() itu HTTP request
        TERPISAH ke exchange, dan ada race window kecil tapi nyata antara
        entry order settle di L1 vs assetPositions snapshot ke-update di
        endpoint user_state(). Sebelumnya method ini cuma fetch SEKALI —
        kalau snapshot itu masih nunjukin posisi lama (szi=0) di window
        itu, return None langsung, dan place_stop_loss/place_take_profit
        GAGAL TOTAL dengan 'no_open_position_for_X' — TANPA RETRY. Posisi
        kebuka tapi SL DAN TP dua-duanya gagal terpasang, tanpa proteksi
        apapun, dan tanpa 'invalid price' error yang keliatan di log (gagal
        di titik ini, sebelum sempat kirim trigger order ke exchange).

        Retry masuk akal di sini karena kita SUDAH TAHU posisi seharusnya
        ada (dipanggil cuma dari place_stop_loss/place_take_profit setelah
        entry fill) — bukan spekulatif nunggu tanpa alasan.
        """
        import time
        for attempt in range(retries):
            state = self._api.user_state()
            for ap in state.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin") == coin:
                    szi = float(pos.get("szi", 0.0))
                    if szi != 0.0:
                        return szi < 0  # True (buy) kalau posisi short
                    break  # coin ketemu tapi szi=0 -> lanjut retry, mungkin masih propagasi

            if attempt < retries - 1:
                logger.warning(
                    f"⏳ _get_close_side({coin}): posisi belum kelihatan di "
                    f"user_state() (attempt {attempt + 1}/{retries}), retry dalam {delay}s..."
                )
                time.sleep(delay)

        logger.error(
            f"❌ _get_close_side({coin}): posisi TETAP gak ketemu setelah "
            f"{retries} percobaan ({retries * delay:.1f}s total) — kemungkinan "
            f"entry beneran gagal atau di-reject exchange, bukan cuma lag propagasi."
        )
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
