# production/engine.py
# ============================================================
# PRODUCTION ENGINE — SATU FUNGSI UTAMA
# ============================================================

import time
import threading
import logging
from typing import Optional, List

from .config import ProductionConfig, EntryMode, OpenMode, OrderType
from .models import (
    Decision,
    Wallet,
    Order,
    OrderSide,
    ExecutionResult,
    ExchangePosition,
    PositionSnapshot,
)
from .provider import Provider
from providers.paper import PaperProvider

logger = logging.getLogger("ProductionEngine")


class MoneyManager:
    """
    Money Manager — lahir di Layer 5.
    
    Tugas: menghitung ukuran posisi berdasarkan wallet dan risk setting.
    BUKAN memutuskan boleh entry atau enggak — itu urusan Brain.
    """
    
    def __init__(self, config: ProductionConfig):
        self._config = config
        self._risk_per_trade = config.RISK_PER_TRADE_PCT / 100.0
        self._max_exposure = config.MAX_EXPOSURE_PCT / 100.0
        self._max_total_exposure = config.MAX_TOTAL_EXPOSURE_PCT / 100.0
    
    def calculate_position_size(self, decision: Decision, wallet: Wallet) -> dict:
        """
        Hitung ukuran posisi berdasarkan wallet dan risk setting.
        
        Returns:
            {
                "size_usdc": float,    # ukuran dalam USDC
                "size_coin": float,    # ukuran dalam coin
                "risk_usdc": float,    # risk dalam USDC (SL distance)
            }
        """
        if wallet.usdc_balance <= 0:
            return {"size_usdc": 0.0, "size_coin": 0.0, "risk_usdc": 0.0}
        
        # 1. Hitung risk dalam USDC
        risk_usdc = wallet.usdc_balance * self._risk_per_trade
        
        # 2. Hitung SL distance dalam persen
        sl_distance_pct = abs(decision.entry - decision.sl) / decision.entry
        
        if sl_distance_pct <= 0:
            return {"size_usdc": 0.0, "size_coin": 0.0, "risk_usdc": 0.0}
        
        # 3. Size USDC = risk / SL distance
        size_usdc = risk_usdc / sl_distance_pct
        
        # 4. Cap max exposure per trade
        max_size_usdc = wallet.usdc_balance * self._max_exposure
        size_usdc = min(size_usdc, max_size_usdc)
        
        # 5. Cap max total exposure (TODO: track dari posisi terbuka)
        
        # 6. Size in coin units
        size_coin = size_usdc / decision.entry
        
        return {
            "size_usdc": round(size_usdc, 2),
            "size_coin": round(size_coin, 6),
            "risk_usdc": round(risk_usdc, 2),
        }


class Translator:
    """
    Translator — dari Engine OpenPosition ke Decision dan Exchange Payload.
    """
    
    @staticmethod
    def position_to_decision(position) -> Decision:
        """Engine OpenPosition → Production Decision."""
        # position adalah OpenPosition dari main.py
        return Decision(
            signal_id=position.signal_id,
            coin=position.coin,
            direction=position.direction,
            entry=position.entry,
            sl=position.sl,
            tp=position.tp3.price,  # Primary target
            leverage=position.leverage,
            truth_mode=position.execution_mode == "FIXED",
        )
    
    @staticmethod
    def decision_to_order(decision: Decision, size_coin: float, order_type: OrderType = OrderType.LIMIT) -> Order:
        """Decision → Order untuk exchange."""
        side = OrderSide.BUY if decision.direction == "LONG" else OrderSide.SELL
        
        return Order(
            order_id="",
            coin=decision.coin,
            side=side,
            order_type=order_type,
            price=decision.entry,
            size=size_coin,
            size_usdc=size_coin * decision.entry,
        )


class ProductionEngine:
    """
    Production Engine — Layer 5.
    
    Satu fungsi utama: execute(decision)
    """
    
    def __init__(self, config: ProductionConfig = None):
        self._config = config or ProductionConfig()
        self._provider: Optional[Provider] = None
        self._money_manager = MoneyManager(self._config)
        self._translator = Translator()
        self._initialized = False
        self._lock = threading.RLock()
    
    def initialize(self, provider: Optional[Provider] = None) -> bool:
        """Initialize production engine with a provider."""
        with self._lock:
            self._active_mode = self._config.OPEN_MODE.value

            if provider:
                self._provider = provider
            elif self._config.PROVIDER == "hyperliquid":
                try:
                    from production.providers.hyperliquid import HyperliquidProvider
                    mode = self._config.OPEN_MODE.value
                    self._provider = HyperliquidProvider(mode=mode)
                except ImportError as _hl_err:
                    logger.warning(f"HyperliquidProvider not available, using PaperProvider — {_hl_err}")
                    import traceback
                    logger.error(f"HYPERLIQUID_IMPORT_TRACEBACK:\n{traceback.format_exc()}")
                    self._provider = PaperProvider()
                    self._active_mode = "paper (fallback: import error)"
            else:
                self._provider = PaperProvider()

            if self._provider:
                self._initialized = self._provider.connect()

            # === AUTO-FALLBACK: TESTNET dikonfigurasi tapi belum "siap" ===
            # (gagal connect, atau connect sukses tapi wallet-nya kosong/belum
            # di-fund) -> otomatis turun ke PaperProvider, TANPA nge-block bot.
            # Cuma berlaku buat TESTNET; LIVE sengaja TIDAK di-auto-fallback,
            # karena kalau LIVE gagal itu harus keliatan jelas, bukan didiemin.
            if self._config.OPEN_MODE == OpenMode.TESTNET:
                needs_fallback = False
                reason = ""

                if not self._initialized:
                    needs_fallback = True
                    reason = "gagal connect ke testnet"
                else:
                    wallet = self._provider.get_wallet()
                    if not wallet or wallet.usdc_balance <= 0:
                        needs_fallback = True
                        reason = f"testnet wallet belum ada saldo (balance={wallet.usdc_balance if wallet else 0})"

                if needs_fallback:
                    logger.warning(f"⚠️ Testnet belum ready ({reason}) — auto-fallback ke PaperProvider.")
                    self._provider = PaperProvider()
                    self._initialized = self._provider.connect()
                    self._active_mode = "paper (fallback: testnet not ready)"
                else:
                    self._active_mode = "testnet"

            logger.info(
                f"ProductionEngine initialized: {self._config.PROVIDER} "
                f"(configured={self._config.OPEN_MODE.value}, active={self._active_mode})"
            )
            return self._initialized
    
    @property
    def active_mode(self) -> str:
        """Mode yang BENERAN aktif sekarang (bisa beda dari OPEN_MODE kalau kena fallback)."""
        return getattr(self, "_active_mode", self._config.OPEN_MODE.value)
    
    @property
    def is_ready(self) -> bool:
        return self._initialized and self._provider and self._provider.is_connected()

    
    def execute(self, decision: Decision) -> ExecutionResult:
        """
        Satu fungsi utama.
        
        Decision dari Brain → Production → Exchange.
        """
        if not self.is_ready:
            return ExecutionResult(
                success=False,
                error="production_engine_not_ready"
            )
        
        logger.info(f"💸 PRODUCTION EXECUTE: {decision.signal_id} {decision.coin} {decision.direction}")
        
        # === STEP 1: Cek wallet & margin ===
        wallet = self._provider.get_wallet()
        if not wallet:
            return ExecutionResult(
                success=False,
                error="failed_to_get_wallet"
            )
        
        # ===== RUNTIME FALLBACK (TESTNET only) =====
        # initialize() cuma ngecek balance SEKALI pas startup. Kalau testnet
        # wallet kebetulan ada saldo waktu boot tapi abis/berubah 0 di
        # tengah jalan (dipakai posisi lain, testnet reset, dll), execute()
        # sebelumnya langsung gagal keras "insufficient_balance" walau
        # config-nya TESTNET (yang seharusnya boleh fallback ke Paper).
        # Sekarang re-check di sini juga, tiap kali mau eksekusi — bukan
        # cuma sekali di boot. LIVE tetap TIDAK di-auto-fallback (harus
        # keliatan jelas kalau gagal, sesuai desain awal).
        if wallet.usdc_balance <= 0 and self._config.OPEN_MODE == OpenMode.TESTNET:
            logger.warning(
                f"⚠️ Testnet wallet balance={wallet.usdc_balance:.2f} saat execute() — "
                f"runtime fallback ke PaperProvider untuk {decision.signal_id}"
            )
            self._provider = PaperProvider()
            self._provider.connect()
            self._active_mode = "paper (fallback: testnet balance depleted at runtime)"
            wallet = self._provider.get_wallet()
        
        if wallet.usdc_balance <= 0:
            return ExecutionResult(
                success=False,
                error="insufficient_balance"
            )
        
        logger.info(f"💼 Wallet: {wallet.usdc_balance:.2f} USDC")
        
        # === STEP 2: Hitung size ===
        size_info = self._money_manager.calculate_position_size(decision, wallet)
        
        if size_info["size_usdc"] <= 0:
            return ExecutionResult(
                success=False,
                error="invalid_position_size"
            )
        
        logger.info(f"🧮 Size: {size_info['size_coin']:.6f} coin ({size_info['size_usdc']:.2f} USDC)")
        
        # === STEP 3: Entry Alert (publikasi thesis) ===
        entry_mode = decision.entry_mode or self._config.ENTRY_MODE
        
        if entry_mode != EntryMode.NONE:
            self._publish_entry(decision, size_info, entry_mode)
        
        # === STEP 4: Open Position (eksekusi nyata) ===
        open_mode = decision.open_mode or self._config.OPEN_MODE
        
        if open_mode == OpenMode.NONE:
            return ExecutionResult(
                success=True,
                error="open_mode_none",
                filled_price=decision.entry,
                filled_size=size_info["size_coin"],
            )
        
        # === STEP 5: Place entry order ===
        order = self._translator.decision_to_order(
            decision,
            size_info["size_coin"],
            self._config.DEFAULT_ORDER_TYPE
        )
        
        result = self._provider.place_order(order)
        
        if not result.success:
            logger.error(f"❌ Entry order failed: {result.error}")
            return result
        
        logger.info(f"✅ Entry order placed: {result.order_id} @ {result.filled_price}")
        
        # === STEP 6: Place SL order ===
        sl_result = self._provider.place_stop_loss(
            coin=decision.coin,
            price=decision.sl,
            size=size_info["size_coin"],
        )
        
        if sl_result.success:
            logger.info(f"✅ SL placed: {sl_result.order_id} @ {decision.sl}")
        else:
            logger.warning(f"⚠️ SL failed: {sl_result.error}")
        
        # === STEP 7: Place TP order ===
        tp_result = self._provider.place_take_profit(
            coin=decision.coin,
            price=decision.tp,
            size=size_info["size_coin"],
        )
        
        if tp_result.success:
            logger.info(f"✅ TP placed: {tp_result.order_id} @ {decision.tp}")
        else:
            logger.warning(f"⚠️ TP failed: {tp_result.error}")
        
        # === STEP 8: Build result ===
        return ExecutionResult(
            success=True,
            order_id=result.order_id,
            filled_price=result.filled_price,
            filled_size=result.filled_size,
            sl_order_id=sl_result.order_id if sl_result.success else None,
            tp_order_id=tp_result.order_id if tp_result.success else None,
        )
    
    def _publish_entry(self, decision: Decision, size_info: dict, mode: EntryMode):
        """Publish entry alert ke Telegram."""
        # TODO: Implement
        # Reuse existing send_alert_v10 logic
        logger.info(f"📢 ENTRY ALERT ({mode.value}): {decision.coin} {decision.direction}")
    
    # === WRAPPER METHODS ===
    
    def get_wallet(self) -> Optional[Wallet]:
        if not self.is_ready:
            return None
        return self._provider.get_wallet()
    
    def get_positions(self) -> List[ExchangePosition]:
        if not self.is_ready:
            return []
        return self._provider.get_positions()
    
    def close_position(self, coin: str, side: str) -> ExecutionResult:
        if not self.is_ready:
            return ExecutionResult(success=False, error="engine_not_ready")
        return self._provider.close_position(coin, side)
    
    def sync(self) -> PositionSnapshot:
        if not self.is_ready:
            return PositionSnapshot(positions=[], wallet=None)
        return self._provider.sync()
