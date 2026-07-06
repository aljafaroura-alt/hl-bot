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
        self._meta_cache = None  # cache hasil info.meta(), buat szDecimals per coin

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

    # === PRECISION HELPERS ===
    # Hyperliquid strict soal presisi angka: price maks 5 significant figures,
    # size harus sesuai szDecimals per coin (beda2 tiap aset), leverage harus
    # bilangan bulat. Kirim angka mentah dari hasil pembagian float Python
    # (misal 0.15767218393561092) bakal ditolak SDK ("float_to_wire causes
    # rounding") daripada diam-diam dibulatin salah.
    def _get_meta(self) -> Dict[str, Any]:
        if self._meta_cache is None:
            try:
                self._meta_cache = self._info.meta()
            except Exception as e:
                logger.error(f"❌ fetch meta failed: {e}")
                self._meta_cache = {"universe": []}
        return self._meta_cache

    def _get_sz_decimals(self, coin: str) -> int:
        meta = self._get_meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == coin:
                return int(asset.get("szDecimals", 3))
        return 3  # default aman kalau coin gak ketemu di meta

    def _round_price(self, coin: str, price: float) -> float:
        """
        Aturan presisi price Hyperliquid itu DUA syarat sekaligus, bukan
        cuma satu:
          1. Maks 5 significant figures.
          2. Maks (6 - szDecimals) angka di belakang koma (buat perps).
        Kedua syarat WAJIB dipenuhi bareng — dipakai yang PALING KETAT
        (paling sedikit desimalnya) di antara keduanya.

        BUG SEBELUMNYA: cuma nerapin syarat #1. Buat coin murah kayak PUMP
        (harga ~$0.00167, szDecimals kecil misal 0-1), SL/TP yang dihitung
        Brain bisa punya 7 digit di belakang koma (0.0016507) yang masih
        "5 significant figures" secara hitungan sig-fig (1,6,5,0,7), tapi
        MELEBIHI batas (6 - szDecimals) desimal exchange. Order ditolak
        'Order has invalid price' — SL dan TP GAGAL terpasang sama sekali,
        posisi kebuka TANPA proteksi apapun sampai next trailing tick
        (kalau ada) atau close manual.
        """
        if price == 0:
            return 0.0
        import math

        # Syarat 1: maks 5 significant figures
        magnitude = math.floor(math.log10(abs(price))) + 1
        decimals_sigfig = max(0, 5 - magnitude)

        # Syarat 2: maks (6 - szDecimals) angka desimal
        sz_decimals = self._get_sz_decimals(coin)
        decimals_szrule = max(0, 6 - sz_decimals)

        # Pakai yang paling ketat (paling sedikit desimal) dari keduanya,
        # supaya DUA syarat itu sama-sama gak dilanggar.
        decimals = min(decimals_sigfig, decimals_szrule)
        return round(price, decimals)

    def _round_size(self, coin: str, size: float) -> float:
        """Bulatin KE BAWAH (floor) sesuai szDecimals coin itu — biar gak
        pernah over-order dari margin yang udah dihitung MoneyManager."""
        import math
        decimals = self._get_sz_decimals(coin)
        factor = 10 ** decimals
        return math.floor(size * factor) / factor

    def set_leverage(self, coin: str, leverage: float, is_cross: bool = True) -> bool:
        """
        Leverage Hyperliquid WAJIB bilangan bulat. Dipanggil sebelum
        entry order, supaya posisi kebuka pakai leverage yang Brain hitung,
        bukan default lama yang nempel di exchange.

        FLOOR, bukan round(): compute_suggested_leverage() di Brain udah
        ngitung leverage yang aman berdasarkan LIQ_SAFETY_MULT (liquidation
        distance >= SL distance * 1.5x). Kalau angka pecahannya (misal 7.9x)
        dibulatkan NAIK ke 8x, buffer keamanan liquidation itu jadi lebih
        tipis dari yang di-approve Brain. floor() SELALU bulat ke bawah,
        jadi leverage exchange gak pernah melebihi apa yang udah dihitung
        aman — konsisten juga dengan _round_size() yang floor demi alasan
        yang sama (jangan pernah over dari yang dihitung MoneyManager).
        """
        if not self._exchange:
            return False
        try:
            import math
            lev_int = max(1, math.floor(leverage))
            logger.warning(
                f"🔍 set_leverage CALL: coin={coin} raw_leverage_in={leverage} "
                f"-> lev_int={lev_int} is_cross={is_cross}"
            )
            resp = self._exchange.update_leverage(lev_int, coin, is_cross)
            logger.warning(f"🔍 set_leverage RESPONSE raw: {resp}")
            ok = resp.get("status") == "ok"
            if ok:
                logger.warning(f"⚙️ Leverage set: {coin} = {lev_int}x ({'cross' if is_cross else 'isolated'})")
            else:
                logger.warning(f"⚠️ set_leverage {coin}={lev_int}x gagal: {resp}")
            return ok
        except Exception as e:
            logger.error(f"❌ set_leverage failed {coin}: {e}")
            return False

    def place_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        reduce_only: bool = False,
        slippage: float = 0.05,
    ) -> Dict[str, Any]:
        """
        Market order beneran (bukan limit yang cuma nangkring). Pakai
        market_open() dari SDK — itu aggressive marketable limit order
        dengan slippage tolerance (default 5%), yang emang jadi cara resmi
        Hyperliquid buat market order. Fill instan sesuai filosofi Brain:
        "deteksi sinyal -> entry sekarang di harga pasar", bukan nunggu di
        orderbook.
        """
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        size = self._round_size(coin, size)
        return self._exchange.market_open(coin, is_buy, size, None, slippage)

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
        size = self._round_size(coin, size)
        price = self._round_price(coin, price)
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
        size = self._round_size(coin, size)
        trigger_price = self._round_price(coin, trigger_price)
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

    def cancel_all_orders_for_coin(self, coin: str) -> Dict[str, Any]:
        """
        Sapu bersih SEMUA open order (SL, TP, sisa apapun) buat 1 coin,
        bukan cuma 1 order_id yang ditrack di state internal Brain.

        Kenapa ini perlu: kalau proses cancel SL lama gagal diam-diam
        (network blip, race dengan trigger exchange, dsb), order lama itu
        numpuk terus di exchange — trailing tick berikutnya bakal nge-cancel
        order_id yang ditrack doang, sisa order basi ga pernah ke-bersihin.
        Salah satu order basi itu bisa beneran ke-trigger duluan (SL basi
        nyentuh harga) dan nutup posisi diam-diam sebelum Brain sadar.

        Best-effort per order: satu order gagal di-cancel gak ngeblok yang
        lain. Return summary, bukan exception, biar caller (orders.py) bisa
        cek 'ok'/'failed' list dan mutusin lanjut atau engga.
        """
        if not self._exchange or not self._info or not self._wallet_address:
            return {"status": "err", "error": "exchange_not_ready", "cancelled": [], "failed": []}

        cancelled = []
        failed = []
        try:
            open_orders = self._info.open_orders(self._wallet_address)
        except Exception as e:
            logger.error(f"❌ cancel_all_orders_for_coin: gagal fetch open_orders: {e}")
            return {"status": "err", "error": str(e), "cancelled": [], "failed": []}

        coin_orders = [o for o in open_orders if o.get("coin") == coin]

        for o in coin_orders:
            oid = o.get("oid")
            try:
                resp = self._exchange.cancel(coin, oid)
                if resp.get("status") == "ok":
                    cancelled.append(oid)
                else:
                    failed.append({"oid": oid, "error": resp})
                    logger.warning(f"⚠️ cancel_all_orders_for_coin: gagal cancel oid={oid} {coin}: {resp}")
            except Exception as e:
                failed.append({"oid": oid, "error": str(e)})
                logger.warning(f"⚠️ cancel_all_orders_for_coin: exception cancel oid={oid} {coin}: {e}")

        status = "ok" if not failed else ("partial" if cancelled else "err")
        if failed:
            logger.warning(
                f"⚠️ cancel_all_orders_for_coin {coin}: {len(cancelled)} ok, "
                f"{len(failed)} gagal — order basi mungkin masih nyangkut."
            )
        return {"status": status, "cancelled": cancelled, "failed": failed}

    def close_position_market(self, coin: str) -> Dict[str, Any]:
        """Tutup posisi coin tertentu pakai market order (SDK helper)."""
        if not self._exchange:
            return {"status": "err", "error": "exchange_not_ready"}
        return self._exchange.market_close(coin)
