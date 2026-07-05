# hyperliquid/api.py  (production/providers/hyperliquid/api.py)
# ============================================================
# HYPERLIQUID API — Wrapper around hyperliquid-python-sdk
# ============================================================

import os
import logging
from typing import Optional, Dict, Any

import eth_account
from eth_account.signers.local import LocalAccount

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

logger = logging.getLogger("HyperliquidAPI")


class HyperliquidAPI:
    """
    Low-level wrapper around hyperliquid-python-sdk.

    mode:
      "testnet" -> pakai env HL_TESTNET_WALLET_ADDRESS / HL_TESTNET_PRIVATE_KEY
      "live"    -> pakai env HL_WALLET_ADDRESS / HL_PRIVATE_KEY
      "paper"   -> API ga dipanggil sama sekali (harusnya udah ke-handle di
                   engine.py, PaperProvider dipake, bukan HyperliquidProvider)
    """

    def __init__(self, mode: str = "testnet"):
        self._mode = mode
        self._base_url = self._get_base_url()

        self._private_key = self._get_private_key()
        self._wallet_address = self._get_wallet_address()

        self._account: Optional[LocalAccount] = None
        self._info: Optional[Info] = None
        self._exchange: Optional[Exchange] = None
        self._connected = False

    def _get_base_url(self) -> str:
        if self._mode == "live":
            return constants.MAINNET_API_URL
        return constants.TESTNET_API_URL

    def _get_private_key(self) -> Optional[str]:
        if self._mode == "live":
            return os.environ.get("HL_PRIVATE_KEY")
        return os.environ.get("HL_TESTNET_PRIVATE_KEY")

    def _get_wallet_address(self) -> Optional[str]:
        if self._mode == "live":
            return os.environ.get("HL_WALLET_ADDRESS")
        return os.environ.get("HL_TESTNET_WALLET_ADDRESS")

    def connect(self) -> bool:
        """Setup Info (public) + Exchange (private, signed) clients."""
        try:
            # Info bisa jalan tanpa private key (cuma butuh buat public data),
            # tapi kalau mau baca wallet/posisi punya address tertentu, address
            # tetap wajib ada.
            self._info = Info(self._base_url, skip_ws=True)

            if not self._private_key:
                logger.warning(
                    f"HL_{'PRIVATE_KEY' if self._mode == 'live' else 'TESTNET_PRIVATE_KEY'} "
                    f"belum di-set — Exchange (order placement) gak bisa dipakai, "
                    f"cuma Info (read-only) yang aktif."
                )
                self._connected = True  # read-only tetap dianggap "connect"
                return True

            self._account = eth_account.Account.from_key(self._private_key)

            # Kalau wallet_address ga di-set eksplisit, pakai address dari private key
            address = self._wallet_address or self._account.address

            self._exchange = Exchange(
                self._account,
                self._base_url,
                account_address=address,
            )
            self._wallet_address = address

            logger.info(f"✅ HyperliquidAPI connected ({self._mode}) address={address}")
            self._connected = True
            return True

        except Exception as e:
            logger.error(f"❌ HyperliquidAPI connect failed: {e}")
            self._connected = False
            return False

    def is_connected(self) -> bool:
        return self._connected

    @property
    def address(self) -> Optional[str]:
        return self._wallet_address

    # === PUBLIC (Info) ===
    def user_state(self) -> Dict[str, Any]:
        if not self._info or not self._wallet_address:
            return {}
        return self._info.user_state(self._wallet_address)

    def spot_user_state(self) -> Dict[str, Any]:
        """
        Saldo Spot — WAJIB dicek buat akun mode 'Unified' atau 'Portfolio
        Margin' (default buat wallet baru di Hyperliquid). Di mode itu,
        clearinghouseState (perps) SENGAJA selalu return accountValue=0,
        karena saldo USDC yang "unified" itu nyimpen di spot clearinghouse
        state, bukan di perps state. Pakai raw HTTP request (bukan method
        SDK) biar ga gantung ke nama method yang bisa beda-beda antar versi
        hyperliquid-python-sdk.
        """
        if not self._wallet_address:
            return {}
        try:
            import requests
            resp = requests.post(
                f"{self._base_url}/info",
                json={"type": "spotClearinghouseState", "user": self._wallet_address},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"❌ spot_user_state failed: {e}")
            return {}

    def open_orders(self) -> list:
        if not self._info or not self._wallet_address:
            return []
        return self._info.open_orders(self._wallet_address)

    def all_mids(self) -> Dict[str, float]:
        if not self._info:
            return {}
        return self._info.all_mids()

    # === PRIVATE (Exchange, signed) ===
    def place_limit_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        reduce_only: bool = False,
        tif: str = "Gtc",
    ) -> Dict[str, Any]:
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        order_type = {"limit": {"tif": tif}}
        return self._exchange.order(coin, is_buy, size, price, order_type, reduce_only=reduce_only)

    def place_trigger_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        tpsl: str,  # "sl" atau "tp"
        reduce_only: bool = True,
    ) -> Dict[str, Any]:
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": tpsl,
            }
        }
        # Trigger order butuh limit_px juga (dipakai kalau isMarket=False);
        # untuk market trigger, SDK tetap minta angka, pakai trigger_price.
        return self._exchange.order(
            coin, is_buy, size, trigger_price, order_type, reduce_only=reduce_only
        )

    def cancel_order(self, coin: str, order_id: int) -> Dict[str, Any]:
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        return self._exchange.cancel(coin, order_id)

    def close_position_market(self, coin: str) -> Dict[str, Any]:
        """Tutup posisi coin tertentu pakai market order (SDK helper)."""
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        return self._exchange.market_close(coin)
