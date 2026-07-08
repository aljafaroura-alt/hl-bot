# production/config.py
# ============================================================
# PRODUCTION CONFIG
# ============================================================

import os
from enum import Enum
from typing import Optional


class EntryMode(Enum):
    """Mode publikasi entry alert."""
    NONE = "none"          # Gak publish alert
    PRIVATE = "private"    # Publish ke owner aja
    PUBLIC = "public"      # Publish ke channel


class OpenMode(Enum):
    """Mode eksekusi order."""
    NONE = "none"          # Gak eksekusi
    PAPER = "paper"        # Paper trading (simulasi)
    TESTNET = "testnet"    # Hyperliquid testnet
    LIVE = "live"          # Uang asli


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProductionConfig:
    """Global production config."""
    
    # === EXECUTION MODE ===
    ENTRY_MODE: EntryMode = EntryMode.PUBLIC
    OPEN_MODE: OpenMode = OpenMode.TESTNET  # Testnet dulu, belum LIVE
    
    # === MONEY MANAGEMENT ===
    RISK_PER_TRADE_PCT: float = 1.0          # 1% dari wallet per trade
    # Sebelumnya 20% — kepotong duluan sebelum MIN_MARGIN_FLOOR_RATIO di
    # engine.py sempat kerja penuh (tier B/C size_mult 1.5x-3.0x butuh
    # ~15-30% fair_share buat gak recehan, 20% cuma pas-pasan buat tier C
    # dan motong tier B). Naik ke 30% biar tier B/C dapet notional penuh
    # sesuai floor, sementara tier A/S (size_mult 5.0x/8.0x) tetap kena
    # cap ini sebagai safety net — itu emang tujuannya, biar 1 trade
    # confidence super tinggi pun gak bisa makan >30% wallet sekaligus.
    MAX_EXPOSURE_PCT: float = 30.0           # Maks 30% wallet di satu posisi
    # NAIK dari 60 -> 75 (2026-07): audit nunjukkin margin per-slot kekecilan
    # ($5-26 padahal equity ~$900) waktu banyak slot terbuka bareng (8/10),
    # karena remaining_total_budget abis duluan sebelum semua slot kebagian
    # margin layak. 75% masih nyisain 25% buffer (bukan all-in), TAPI ini
    # ngurangin margin of safety agregat — kalau semua posisi bareng kena
    # gerak berlawanan, drawdown total bisa lebih dalam dari sebelumnya.
    # MAX_EXPOSURE_PCT (per-trade, 30%) SENGAJA TIDAK diubah — itu safety net
    # individual biar 1 trade confidence tinggi gak bisa makan porsi wallet
    # kelewat besar sendirian, terpisah dari masalah agregat di atas.
    MAX_TOTAL_EXPOSURE_PCT: float = 75.0     # Maks 75% wallet total (dari 60%)
    # Ditulis eksplisit (sebelumnya cuma default getattr tersembunyi di
    # engine.py). 1.0 = floor sama besar dengan fair_share*size_mult penuh
    # (tanpa didiskon). Naikkan (>1.0) kalau masih ngerasa slot tier
    # bawah/menengah terlalu kecil setelah MAX_TOTAL_EXPOSURE_PCT dinaikkan;
    # turunkan (<1.0) kalau ternyata margin of safety kurang setelah live test.
    MIN_MARGIN_FLOOR_RATIO: float = 1.0
    
    # === ORDER DEFAULTS ===
    # MARKET, bukan LIMIT — filosofi Brain itu "deteksi sinyal -> entry SEKARANG
    # di harga pasar", bukan taruh order nunggu di orderbook. Limit order yang
    # gak fill instan bikin SL/TP gagal dipasang (posisi belum ada di exchange
    # walau Brain udah nganggep "OPEN" di internal record).
    DEFAULT_ORDER_TYPE: OrderType = OrderType.MARKET
    DEFAULT_SLIPPAGE: float = 0.05          # 5% (dipakai market_open sebagai slippage tolerance)
    
    # === PROVIDER ===
    PROVIDER: str = "hyperliquid"
    
    # === HYPERLIQUID TESTNET ===
    # Dead fields — api.py baca langsung dari env var HL_TESTNET_WALLET_ADDRESS /
    # HL_TESTNET_PRIVATE_KEY, bukan dari sini. Dibiarin buat referensi aja.
    TESTNET_WALLET_ADDRESS: Optional[str] = os.environ.get("HL_TESTNET_WALLET_ADDRESS")
    TESTNET_PRIVATE_KEY: Optional[str] = os.environ.get("HL_TESTNET_PRIVATE_KEY")
    
    # === HYPERLIQUID LIVE ===
    LIVE_WALLET_ADDRESS: Optional[str] = os.environ.get("HL_WALLET_ADDRESS")
    LIVE_PRIVATE_KEY: Optional[str] = os.environ.get("HL_PRIVATE_KEY")
