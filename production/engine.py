# production/engine.py
# ============================================================
# PRODUCTION ENGINE — SATU FUNGSI UTAMA
# ============================================================

import time
import threading
import logging
from typing import Optional, List, Dict

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
        # Rasio floor margin minimum per slot, dikali fair_share (balance /
        # max_slots) DAN size_mult tier Brain — jadi floor tetap proporsional
        # ke conviction tier (E/size_mult=0.0 -> floor $0, tetap skip; D/0.5
        # -> floor separuh fair_share; S/8.0 -> floor jauh lebih besar, nanti
        # dijepit MAX_EXPOSURE_PCT). Default 1.0 artinya floor = alokasi
        # "seolah remaining_slots == max_slots" (skenario paling adil), biar
        # margin gak jatuh recehan pas slot lagi padat + free_margin ngumpul
        # rendah. Configurable lewat config.MIN_MARGIN_FLOOR_RATIO.
        self._min_margin_floor_ratio = getattr(config, "MIN_MARGIN_FLOOR_RATIO", 1.0)
    
    def calculate_position_size(self, decision: Decision, wallet: Wallet) -> dict:
        """
        Hitung ukuran posisi berdasarkan wallet dan risk setting.

        DUA MODE:
        1. SLOT-BASED (dipakai kalau decision.open_slots_used DAN
           decision.max_slots keduanya diisi Brain): margin per trade =
           free_margin / sisa_slot, dikali decision.size_mult (default 1.0
           kalau Brain gak ngirim). Adaptif — free_margin naik/turun sesuai
           margin yang lagi kepake di posisi lain, dan sisa_slot mengecil
           tiap posisi baru kebuka, jadi margin per-trade otomatis
           menyesuaikan kapasitas yang beneran ada, BUKAN fixed 1% dari
           balance tanpa peduli berapa banyak slot lain yang lagi dipakai.
           size_mult di atas itu ngatur CAPITAL CONCENTRATION per-trade:
           setup confidence tinggi (tier S) dapet size_mult jauh lebih
           besar dari setup biasa (tier D) — modal terkonsentrasi ke setup
           terbaik, bukan dibagi rata ke semua slot.

           Kenapa "sisa slot" (max_slots - open_slots_used), bukan
           max_slots langsung: kalau selalu bagi ke kapasitas PENUH dari
           awal, margin per-trade jadi kekecilan pas slot yang kepake
           dikit (padahal free_margin available banyak) — sisa slot bikin
           margin per-trade membesar otomatis waktu kapasitas belum penuh,
           dan mengecil waktu inventory makin padat, konsisten sama
           filosofi Brain punya inventory gate.

        2. RISK-BASED (fallback lama): dipakai kalau Brain belum ngirim
           open_slots_used/max_slots (None) — size = (balance * risk%) /
           SL_distance. Dipertahankan sebagai fallback biar backward
           compatible.

        Returns:
            {
                "size_usdc": float,    # ukuran dalam USDC
                "size_coin": float,    # ukuran dalam coin
                "risk_usdc": float,    # risk dalam USDC (SL distance)
            }
        """
        if wallet.usdc_balance <= 0:
            return {"size_usdc": 0.0, "size_coin": 0.0, "risk_usdc": 0.0}

        sl_distance_pct = abs(decision.entry - decision.sl) / decision.entry
        if sl_distance_pct <= 0:
            return {"size_usdc": 0.0, "size_coin": 0.0, "risk_usdc": 0.0}

        has_slot_info = decision.open_slots_used is not None and decision.max_slots is not None

        if has_slot_info:
            # === MODE SLOT-BASED ===
            remaining_slots = max(1, decision.max_slots - decision.open_slots_used)
            # free_margin bisa negatif kalau ada anomali data — jangan sampai
            # size jadi negatif, clamp ke 0 minimal.
            free_margin = max(0.0, wallet.free_margin)
            margin_per_slot = free_margin / remaining_slots

            # === CAPITAL ALLOCATION: CONFIDENCE-TIER SIZE MULTIPLIER ===
            # size_mult dari Brain (confidence tier x behavior state) ngatur
            # SEBERAPA BESAR trade INI SPESIFIK relatif ke slot lain — bukan
            # ngubah berapa banyak slot yang tersedia (itu urusan max_slots).
            # None/tidak diisi → 1.0 (semua slot dapet porsi sama, behaviour
            # lama, backward compatible).
            size_mult = decision.size_mult if decision.size_mult is not None else 1.0
            size_mult = max(0.0, size_mult)  # jangan sampai negatif

            logger.info(
                f"🎒 SLOT-BASED SIZING: free_margin=${free_margin:.2f} "
                f"slots={decision.open_slots_used}/{decision.max_slots} "
                f"remaining={remaining_slots} -> margin_per_slot=${margin_per_slot:.2f} "
                f"x size_mult={size_mult:.2f}"
            )

            size_usdc = margin_per_slot * size_mult

            # === MINIMUM MARGIN FLOOR (tier-aware, align sama Brain) ===
            # Floor HARUS ikut size_mult tier dari Brain (main.py), bukan
            # angka flat — kalau flat, floor bakal maksa tier E (SHADOW,
            # size_mult=0.0, sengaja "jangan entry beneran") ikut naik ke
            # floor yang sama kayak tier lain, ngerusak filosofi Brain.
            # Jadi floor = fair_share x size_mult x ratio: tetep proporsional
            # ke conviction tier (S dapet floor gede, D dapet floor kecil,
            # E dapet floor $0 karena size_mult-nya emang 0), tapi gak jatuh
            # ke recehan kayak $8 pas remaining_slots kecil + free_margin
            # ngumpul rendah. Tetep dicap ke free_margin yang beneran ada.
            fair_share = wallet.usdc_balance / decision.max_slots
            min_margin_floor = fair_share * size_mult * self._min_margin_floor_ratio
            # NOTE: floor SENGAJA gak di-clamp ke free_margin di sini lagi.
            # Dulu ada `min(min_margin_floor, free_margin)` yang bikin floor
            # collapse balik jadi recehan justru pas paling dibutuhkan --
            # waktu banyak slot lain kebuka bareng dan free_margin mepet,
            # padahal itu skenario PERSIS yang floor ini dibuat buat handle
            # (biar tier confidence tinggi tetap dapet margin layak, bukan
            # sisa-sisa slot lain). Batas atas yang beneran perlu (jangan
            # sampai over-allocate dari wallet) sudah dijaga dua lapis di
            # bawah: MAX_EXPOSURE_PCT (per-trade) dan MAX_TOTAL_EXPOSURE_PCT
            # (agregat semua slot) -- keduanya baca wallet.used_margin yang
            # real, jadi lebih akurat daripada asal min() ke free_margin.
            if size_usdc < min_margin_floor:
                logger.info(
                    f"📐 MARGIN FLOOR applied: {decision.coin} size_usdc "
                    f"${size_usdc:.2f} < floor ${min_margin_floor:.2f} "
                    f"(fair_share=${fair_share:.2f} x size_mult={size_mult:.2f} "
                    f"x ratio={self._min_margin_floor_ratio:.2f}) -> dinaikkan ke floor"
                )
                size_usdc = min_margin_floor

            risk_usdc = size_usdc * sl_distance_pct  # risk implisit dari size ini, bukan basis hitung
        else:
            # === MODE RISK-BASED (fallback lama) ===
            risk_usdc = wallet.usdc_balance * self._risk_per_trade
            size_usdc = risk_usdc / sl_distance_pct

        # Cap max exposure per trade tetap berlaku di SEMUA mode — safety
        # net biar 1 slot gak bisa dialokasikan lebih dari batas absolut,
        # meskipun hasil hitungan slot-based/risk-based di atas gede, DAN
        # meskipun size_mult (tier S misalnya 8.0x) mencoba menggandakan
        # margin_per_slot lebih jauh. Ini garis pertahanan terakhir supaya
        # tier confidence tertinggi pun tidak bisa melewati eksposur
        # maksimum per-posisi yang sudah ditetapkan.
        max_size_usdc = wallet.usdc_balance * self._max_exposure
        size_usdc = min(size_usdc, max_size_usdc)

        # === CAP TOTAL EXPOSURE (agregat semua slot terbuka) ===
        # MAX_EXPOSURE_PCT di atas cuma jaga 1 TRADE ini doang — gak ada
        # yang jaga TOTAL semua posisi terbuka bareng biar gak lewat
        # MAX_TOTAL_EXPOSURE_PCT dari wallet (misal 10 slot semua kebuka
        # tier tinggi bareng, per-trade masing2 aman di bawah cap, tapi
        # GABUNGANNYA bisa jauh ngelewatin batas aman). Sebelumnya
        # config.MAX_TOTAL_EXPOSURE_PCT ke-set tapi gak pernah dipakai
        # (dead value) — sekarang beneran dijepit di sini.
        #
        # wallet.used_margin = margin YANG UDAH terpakai di posisi lain
        # (real margin, hasil notional/leverage exchange). size_usdc di
        # sini masih notional (belum dibagi leverage) — jadi buat
        # dibandingkan apple-to-apple ke used_margin, konversi ke margin
        # dulu pakai decision.leverage (fallback 1.0x kalau kosong).
        lev_for_margin = decision.leverage if decision.leverage and decision.leverage > 0 else 1.0
        margin_needed = size_usdc / lev_for_margin
        max_total_margin = wallet.usdc_balance * self._max_total_exposure
        remaining_total_budget = max(0.0, max_total_margin - max(0.0, wallet.used_margin))

        if margin_needed > remaining_total_budget:
            old_size_usdc = size_usdc
            size_usdc = remaining_total_budget * lev_for_margin
            logger.info(
                f"🧢 TOTAL EXPOSURE CAP applied: {decision.coin} margin_needed="
                f"${margin_needed:.2f} > remaining_budget=${remaining_total_budget:.2f} "
                f"(used_margin=${wallet.used_margin:.2f} / max_total=${max_total_margin:.2f}) "
                f"-> size_usdc ${old_size_usdc:.2f} dipotong jadi ${size_usdc:.2f}"
            )
            if size_usdc <= 0:
                logger.warning(
                    f"🚫 {decision.coin} entry DIBATALKAN — total exposure wallet "
                    f"udah kena MAX_TOTAL_EXPOSURE_PCT (${max_total_margin:.2f}), "
                    f"gak ada budget margin tersisa buat slot baru."
                )

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
            # getattr aman: kalau Brain (main.py, di luar wewenang drive
            # layer buat diubah) belum nambahin atribut ini di OpenPosition,
            # tetap None -> MoneyManager fallback ke risk-based sizing lama.
            open_slots_used=getattr(position, "open_slots_used", None),
            max_slots=getattr(position, "max_slots", None),
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
        # Jejak notional (size_usdc, leverage) per signal_id yang berhasil
        # dieksekusi -> dipakai on_position_closed() buat ngitung $ PnL pas
        # Brain nutup posisi itu, tanpa perlu ubah Provider abstract interface.
        self._open_notional: Dict[str, dict] = {}
    
    def initialize(self, provider: Optional[Provider] = None) -> bool:
        """Initialize production engine with a provider."""
        with self._lock:
            self._active_mode = self._config.OPEN_MODE.value

            if provider:
                self._provider = provider
            elif self._config.OPEN_MODE == OpenMode.PAPER:
                # ===== FIX: OPEN_MODE.PAPER harus SELALU pakai PaperProvider =====
                # Sebelumnya, kalau PROVIDER == "hyperliquid" (default), kondisi
                # di bawah (elif self._config.PROVIDER == "hyperliquid") akan
                # tetap menang duluan dan bikin HyperliquidProvider(mode="paper")
                # dikonstruksi — yang tetap connect ke Hyperliquid API ASLI
                # (testnet/mainnet), bukan simulasi. Akibatnya get_wallet()
                # balikin usdc_balance dari wallet exchange beneran (yang belum
                # di-fund), bukan $10k paper money — jadi tiap eksekusi gagal
                # "insufficient_balance" walau config-nya PAPER. OPEN_MODE.PAPER
                # sekarang jadi override tertinggi: apapun PROVIDER-nya, kalau
                # mode-nya PAPER, provider yang dipakai PaperProvider titik.
                self._provider = PaperProvider()
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
        
        logger.info(
            f"💸 PRODUCTION EXECUTE: {decision.signal_id} {decision.coin} {decision.direction} "
            f"| provider={type(self._provider).__name__} active_mode={self.active_mode}"
        )
        
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

        # === STEP 4.4: Cek posisi existing SEBELUM entry (WAJIB, sebelum set_leverage) ===
        # BUG FIX (leverage-numpuk): Hyperliquid "leverage is only checked
        # when opening a position" — kalau coin ini SUDAH punya posisi
        # terbuka di exchange (leftover dari test manual, trade sebelumnya
        # yang belum clean-close, atau bug lain), set_leverage() di bawah
        # akan tetap return status="ok" (dia berhasil UBAH SETTING untuk
        # entry berikutnya), TAPI posisi yang sudah exist TIDAK ikut
        # berubah leverage-nya. Order baru non-reduce-only akan NETTING
        # (nambah) ke posisi lama itu, dan yang berlaku di exchange adalah
        # leverage POSISI LAMA (misal 20x), bukan leverage baru yang Brain
        # hitung (misal 6.5x -> 6x). Efeknya persis laporan user: Telegram
        # bilang "Lev 6.5x" tapi DEX nunjukin 20x — bukan salah pembulatan
        # (floor 6.5 gak mungkin jadi 20), tapi entry numpuk ke posisi lama
        # yang leverage-nya beda sama sekali.
        # Fix: cek FRESH ke exchange (bukan state internal manapun) dulu.
        # Kalau ternyata sudah ada posisi utk coin ini, BATALKAN entry
        # (bukan tetap lanjut numpuk diam-diam) — lebih aman kehilangan 1
        # entry baru daripada silently mencampur 2 leverage berbeda dalam
        # 1 posisi netted, yang bikin risk/PnL Brain gak match exchange.
        # UPDATE: bot sekarang BOLEH nambah (pyramiding) ke posisi yang udah
        # ada, asal SAME DIRECTION dan leverage exchange aktual COMPATIBLE
        # (floor sama) dengan leverage baru yang Brain hitung. Beda arah atau
        # leverage beneran mismatch tetap DIBATALKAN — itu tetap bahaya
        # (netting ke leverage lama yang beda / posisi kebalik). Kalau cocok,
        # skip re-set_leverage() (exchange udah pas) dan lanjut entry nambah
        # size ke posisi existing.
        existing_position = None
        get_positions_fn = getattr(self._provider, "get_positions", None)
        if get_positions_fn:
            try:
                for p in get_positions_fn():
                    if p.coin == decision.coin:
                        existing_position = p
                        break
            except Exception as e:
                logger.error(
                    f"❌ get_positions exception {decision.coin}: {e} — "
                    f"entry DIBATALKAN (gak bisa pastikan aman dari numpuk leverage)"
                )
                return ExecutionResult(
                    success=False,
                    error=f"get_positions_exception: {e}",
                )

        skip_set_leverage = False
        if existing_position is not None:
            same_direction = str(existing_position.side).upper() == str(decision.direction).upper()
            lev_compatible = int(existing_position.leverage) == int(decision.leverage)
            if not same_direction:
                logger.error(
                    f"🚨 ENTRY DIBATALKAN: {decision.coin} SUDAH ADA posisi "
                    f"{existing_position.side.upper()} terbuka di exchange, tapi "
                    f"decision baru ini {decision.direction.upper()} — arah beda, entry "
                    f"numpuk bisa kebalik posisi. Tutup posisi lama dulu."
                )
                return ExecutionResult(
                    success=False,
                    error=f"position_already_open_{decision.coin}_opposite_direction",
                )
            if not lev_compatible:
                logger.error(
                    f"🚨 ENTRY DIBATALKAN: {decision.coin} SUDAH ADA posisi terbuka "
                    f"dengan leverage exchange {existing_position.leverage:.1f}x, beda "
                    f"sama leverage baru {decision.leverage:.1f}x yang Brain hitung — "
                    f"numpuk bakal ikut leverage lama itu. Tutup/bersihkan posisi lama "
                    f"dulu sebelum entry baru untuk coin ini."
                )
                return ExecutionResult(
                    success=False,
                    error=f"position_already_open_{decision.coin}_leverage_mismatch",
                )
            skip_set_leverage = True
            logger.info(
                f"➕ ADD TO POSITION: {decision.coin} udah ada posisi "
                f"{existing_position.side.upper()} {existing_position.leverage:.1f}x — "
                f"entry baru ini akan NAMBAH size ke posisi yang sama (leverage cocok)."
            )
        
        # === STEP 4.5: Set leverage (WAJIB SUKSES sebelum entry) ===
        # Leverage exchange WAJIB bilangan bulat, dan harus di-set SEBELUM
        # entry order supaya posisi kebuka pakai leverage yang Brain hitung
        # (misal 7.9x -> 8x), bukan default lama yang nempel di exchange.
        #
        # BUG FIX: sebelumnya set_leverage() cuma dibungkus try/except tanpa
        # ngecek return value-nya. set_leverage() TIDAK raise exception saat
        # gagal — dia return False (misal API reject, rate limit, dsb) dan
        # cuma nge-log warning. Kode lama nganggep "no exception" = "aman",
        # terus tetap lanjut entry order walau leverage GAGAL ke-set.
        # Akibatnya posisi kebuka pakai leverage LAMA yang masih nempel di
        # exchange (misal 10x dari trade sebelumnya) — bukan leverage yang
        # Brain hitung (misal 7.9x). Ini bikin DUA hal rusak sekaligus:
        #   1. Notif Telegram nunjukin "Lev 7.9x" (dari decision.leverage)
        #      padahal exchange beneran pakai 10x — user liat "kebulet
        #      kegedean" padahal sebenarnya angka itu gak related sama
        #      sekali ke leverage exchange asli.
        #   2. PnL % di alert CLOSE dihitung Brain pakai leverage internal
        #      yang salah (final_pnl * 7.9) padahal PnL exchange sebenarnya
        #      pakai 10x — hasilnya leveraged_pnl di notif MELESET jauh dari
        #      PnL asli di exchange (mismatch yang user laporkan: notif
        #      +7.33% padahal exchange masih posisi terbuka dgn pnl beda).
        # Sekarang: leverage WAJIB berhasil di-set sebelum lanjut. Kalau
        # gagal, entry DIBATALKAN (bukan lanjut dengan leverage yang gak
        # diketahui Brain) — lebih aman kehilangan 1 entry daripada buka
        # posisi dengan leverage/PnL yang gak match sama sekali.
        set_lev_fn = getattr(self._provider, "set_leverage", None)
        if set_lev_fn and not skip_set_leverage:
            try:
                lev_ok = set_lev_fn(decision.coin, decision.leverage)
            except Exception as e:
                logger.error(f"❌ set_leverage exception {decision.coin}: {e} — entry DIBATALKAN")
                return ExecutionResult(
                    success=False,
                    error=f"set_leverage_exception: {e}",
                )
            if not lev_ok:
                logger.error(
                    f"❌ set_leverage GAGAL {decision.coin}={decision.leverage}x — entry DIBATALKAN "
                    f"(mencegah posisi kebuka dengan leverage exchange yang gak diketahui Brain)"
                )
                return ExecutionResult(
                    success=False,
                    error=f"set_leverage_failed_{decision.coin}_{decision.leverage}x",
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
        
        # Catat notional posisi ini, dipakai nanti pas closed (on_position_closed)
        # dan pas trailing SL update (update_stop_loss).
        self._open_notional[decision.signal_id] = {
            "coin": decision.coin,
            "direction": decision.direction,
            "size_usdc": size_info["size_usdc"],
            "size_coin": size_info["size_coin"],
            "leverage": decision.leverage or 1.0,
            "sl_order_id": None,
            "tp_order_id": None,
            "tp_price": decision.tp,
        }

        # === GUARD: SL/TP cuma dipasang kalau entry BENERAN fill ===
        # Kalau order masih resting (belum fill, filled_size=0 — misal
        # limit order lama, atau market order kena slippage tolerance
        # exceeded), JANGAN coba pasang SL/TP: belum ada posisi di exchange,
        # bakal error "no_open_position_for_X" yang bikin bingung.
        if not result.filled_size or result.filled_size <= 0:
            logger.warning(
                f"⚠️ Entry {decision.coin} belum fill (order resting, filled_size=0) — "
                f"SL/TP DITUNDA, belum ada posisi beneran di exchange."
            )
            return ExecutionResult(
                success=True,
                order_id=result.order_id,
                filled_price=0.0,
                filled_size=0.0,
            )
        
        # === STEP 6: Place SL order ===
        sl_result = self._provider.place_stop_loss(
            coin=decision.coin,
            price=decision.sl,
            size=size_info["size_coin"],
        )
        
        if sl_result.success:
            logger.info(f"✅ SL placed: {sl_result.order_id} @ {decision.sl}")
            self._open_notional[decision.signal_id]["sl_order_id"] = sl_result.order_id
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
            self._open_notional[decision.signal_id]["tp_order_id"] = tp_result.order_id
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
    
    def update_stop_loss(self, signal_id: str, new_sl_price: float) -> ExecutionResult:
        """
        Dipanggil dari Brain (main.py) tiap kali trailing SL geser SL posisi
        yang lagi terbuka.

        PENTING — kenapa ini refresh SL *dan* TP, bukan SL doang: provider
        (HyperliquidOrders.update_stop_loss) sweep SEMUA open order buat
        coin ini dulu (bukan cancel 1 order_id yang ditrack), biar SL basi
        yang gagal ke-cancel di tick sebelumnya ga numpuk dan ga beresiko
        ke-trigger duluan. Efek sampingnya, TP yang masih nempel ikut
        kesapu. Makanya di sini TP WAJIB dipasang ulang juga di harga yang
        sama (info["tp_price"]), tiap kali abis refresh SL — kalau engga,
        TP jadi orphan permanen sampai posisi ditutup lewat jalur lain.
        """
        if not self.is_ready:
            return ExecutionResult(success=False, error="engine_not_ready")

        info = self._open_notional.get(signal_id)
        if not info:
            return ExecutionResult(success=False, error=f"no_tracked_position_for_{signal_id}")

        tp_price = info.get("tp_price")
        if tp_price is None:
            # Posisi lama dari sebelum fix ini (tp_price belum pernah ditrack)
            # — jangan sweep-cancel TP tanpa tau harga buat masang ulang.
            # Fallback ke update SL lama (cancel 1 order_id doang) supaya
            # minimal SL tetap ke-update, TP dibiarkan apa adanya.
            logger.warning(
                f"⚠️ tp_price gak ketracking utk {signal_id} ({info['coin']}) — "
                f"skip sweep-all, cuma update SL order_id lama (best-effort)."
            )
            try:
                result = self._provider.update_stop_loss(
                    coin=info["coin"],
                    old_order_id=info.get("sl_order_id"),
                    new_price=new_sl_price,
                    size=info["size_coin"],
                )
                if result.success:
                    info["sl_order_id"] = result.order_id
                return result
            except Exception as e:
                logger.error(f"update_stop_loss exception {signal_id}: {e}")
                return ExecutionResult(success=False, error=str(e))

        try:
            refresh_fn = getattr(self._provider, "refresh_sl_and_tp", None)
            if not refresh_fn:
                # Provider lama/paper tanpa dukungan sweep — fallback aman.
                result = self._provider.update_stop_loss(
                    coin=info["coin"],
                    old_order_id=info.get("sl_order_id"),
                    new_price=new_sl_price,
                    size=info["size_coin"],
                )
                if result.success:
                    info["sl_order_id"] = result.order_id
                return result

            refreshed = refresh_fn(
                coin=info["coin"],
                sl_price=new_sl_price,
                tp_price=tp_price,
                size=info["size_coin"],
            )
            sl_result = refreshed["sl"]
            tp_result = refreshed["tp"]

            if sl_result.success:
                info["sl_order_id"] = sl_result.order_id
                logger.info(
                    f"🔄 SL UPDATED: {info['coin']} signal={signal_id} "
                    f"new_sl={new_sl_price:.6f} order={sl_result.order_id}"
                )
            else:
                logger.warning(f"⚠️ SL update failed: {signal_id} error={sl_result.error}")

            if tp_result.success:
                info["tp_order_id"] = tp_result.order_id
                logger.info(
                    f"🔄 TP RE-ARMED: {info['coin']} signal={signal_id} "
                    f"tp={tp_price:.6f} order={tp_result.order_id}"
                )
            else:
                logger.error(
                    f"🚨 TP RE-ARM GAGAL: {info['coin']} signal={signal_id} "
                    f"error={tp_result.error} — TP ORPHAN, cek manual!"
                )

            # Kontrak return tetap ExecutionResult tunggal (dipakai caller lama
            # di main.py) — pakai hasil SL sebagai status utama, tapi kalau SL
            # sukses dan TP gagal, tetap surface sebagai kegagalan supaya
            # Brain/alert notice-nya jalan, bukan diam-diam ke-treat sukses.
            if sl_result.success and not tp_result.success:
                return ExecutionResult(
                    success=False,
                    order_id=sl_result.order_id,
                    error=f"sl_ok_but_tp_rearm_failed: {tp_result.error}",
                )
            return sl_result

        except Exception as e:
            logger.error(f"update_stop_loss exception {signal_id}: {e}")
            return ExecutionResult(success=False, error=str(e))
    
    def is_position_actually_open(self, signal_id: str) -> Optional[bool]:
        """
        Verifikasi FRESH ke exchange: apakah posisi signal_id ini beneran
        masih terbuka SEKARANG, bukan asumsi dari state internal manapun
        (Brain punya pos.status, Engine punya _open_notional — keduanya
        bisa desync dari exchange kalau ada order yang gagal sync).

        Dipakai buat cross-check SEBELUM mengirim notifikasi status posisi
        (misal "CLOSED, profitable") — kalau Brain nutup posisi berdasarkan
        trailing_sl versi internalnya sendiri, method ini bisa konfirmasi
        dulu apakah exchange beneran udah gak punya posisi itu.

        Return:
          True  -> posisi masih ada / masih terbuka di exchange
          False -> posisi udah gak ada di exchange (size 0 / gak ketemu)
          None  -> gak bisa diverifikasi (signal_id gak ditrack, provider
                   gak support, atau fetch ke exchange gagal) — caller
                   TIDAK boleh treat None sebagai "aman"/"closed", ini
                   artinya statusnya genuinely gak diketahui.
        """
        if not self.is_ready:
            return None

        info = self._open_notional.get(signal_id)
        if not info:
            return None  # gak pernah lewat Production Engine (shadow/discovery alert)

        is_open_fn = getattr(self._provider, "is_position_open", None)
        if not is_open_fn:
            return None  # provider (misal Paper) gak support verifikasi fresh

        try:
            return is_open_fn(info["coin"])
        except Exception as e:
            logger.error(f"is_position_actually_open exception {signal_id} ({info['coin']}): {e}")
            return None

    def close_position_for_signal(self, signal_id: str, reason: str = "") -> Optional[ExecutionResult]:
        """
        Dipanggil dari Brain (main.py) SEBELUM on_position_closed(), tiap kali
        TradeManager mutusin nutup posisi (sl_hit / tp3_hit / trailing_sl /
        timeout / dll) TERLEPAS dari apakah itu udah ke-trigger duluan di
        exchange lewat native SL/TP order.

        Kenapa ini perlu: SL/TP order yang dipasang execute() itu cuma proteksi
        harga murni di exchange. Sebagian besar jalur close di Brain (misal
        timeout_tp2, trailing_sl, atau override manual) BUKAN hasil dari SL/TP
        order itu ke-trigger — itu keputusan internal Brain berdasarkan
        state/waktu. Exchange gak tau soal itu, jadi posisi asli akan tetap
        nyangkut terbuka kalau kita gak eksplisit kirim close order di sini.

        Best-effort & aman dipanggil di semua mode:
          - Paper: provider gak punya close_position() → no-op (paper close
            udah dihandle on_position_closed via apply_realized_pnl).
          - Live/Testnet (HyperliquidProvider): kirim market close beneran.

        Return None kalau di-skip (paper / posisi gak ketemu), atau
        ExecutionResult dari provider kalau beneran dieksekusi.
        """
        info = self._open_notional.get(signal_id)
        if not info:
            return None  # posisi ini gak pernah lewat Production Engine (shadow/discovery alert)

        close_fn = getattr(self._provider, "close_position", None) if self._provider else None
        if not close_fn:
            return None  # provider (misal Paper) gak support close_position asli

        side = "long" if info["direction"] == "LONG" else "short"
        try:
            result = close_fn(info["coin"], side)
            if result.success:
                logger.info(
                    f"🔴 EXCHANGE POSITION CLOSED: {info['coin']} {info['direction']} "
                    f"signal={signal_id} reason={reason or 'n/a'}"
                )
            else:
                logger.error(
                    f"🚨 EXCHANGE CLOSE FAILED: {info['coin']} {info['direction']} "
                    f"signal={signal_id} reason={reason or 'n/a'} error={result.error} "
                    f"— POSISI KEMUNGKINAN MASIH TERBUKA DI EXCHANGE, CEK MANUAL!"
                )
            return result
        except Exception as e:
            logger.error(
                f"🚨 close_position_for_signal EXCEPTION {signal_id} ({info['coin']}): {e} "
                f"— POSISI KEMUNGKINAN MASIH TERBUKA DI EXCHANGE, CEK MANUAL!"
            )
            return ExecutionResult(success=False, error=str(e))

    def on_position_closed(self, signal_id: str, pnl_pct: float) -> None:
        """
        Dipanggil dari Brain (main.py) tiap kali TradeManager nutup posisi
        (trailing_sl / tp_hit / timeout / dll), TERLEPAS dari sukses/gagalnya.
        Best-effort, non-blocking — kalau provider ga punya kemampuan ini
        (misal HyperliquidProvider, karena wallet exchange asli udah
        ke-update otomatis lewat order close beneran), ini jadi no-op.

        PENTING: kalau posisinya real (bukan paper), panggil
        close_position_for_signal() DULU sebelum ini — method ini cuma urus
        PnL bookkeeping, bukan nutup posisi beneran di exchange.
        """
        info = self._open_notional.pop(signal_id, None)
        if not info:
            return  # posisi ini gak pernah lewat Production Engine (shadow/discovery alert)

        apply_fn = getattr(self._provider, "apply_realized_pnl", None) if self._provider else None
        if not apply_fn:
            return

        pnl_usdc = info["size_usdc"] * (pnl_pct / 100.0) * info["leverage"]
        side = "long" if info["direction"] == "LONG" else "short"
        try:
            new_balance = apply_fn(info["coin"], side, pnl_usdc)
            logger.info(
                f"💰 PAPER PNL APPLIED: {info['coin']} {info['direction']} signal={signal_id} "
                f"pnl=${pnl_usdc:+.2f} -> balance=${new_balance:.2f}"
            )
        except Exception as e:
            logger.error(f"Failed to apply realized pnl for {signal_id}: {e}")
    
    def sync(self) -> PositionSnapshot:
        if not self.is_ready:
            return PositionSnapshot(positions=[], wallet=None)
        return self._provider.sync()
