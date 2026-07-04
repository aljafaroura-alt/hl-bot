# test_production.py
# ============================================================
# TEST PRODUCTION ENGINE
# ============================================================

from production import (
    ProductionEngine,
    ProductionConfig,
    Decision,
    EntryMode,
    OpenMode,
)

def main():
    # === CONFIG ===
    config = ProductionConfig()
    config.ENTRY_MODE = EntryMode.PUBLIC
    config.OPEN_MODE = OpenMode.PAPER
    config.RISK_PER_TRADE_PCT = 1.0
    
    # === ENGINE ===
    engine = ProductionEngine(config)
    engine.initialize()
    
    print(f"✅ Engine ready: {engine.is_ready}")
    
    # === DECISION ===
    decision = Decision(
        signal_id="TEST_123456",
        coin="GRAM",
        direction="LONG",
        entry=1.80,
        sl=1.67,
        tp=2.19,
        leverage=2.9,
        truth_mode=True,
    )
    
    # === EXECUTE ===
    result = engine.execute(decision)
    
    print(f"\n📊 RESULT:")
    print(f"   Success: {result.success}")
    print(f"   Order ID: {result.order_id}")
    print(f"   Filled: {result.filled_size} @ {result.filled_price}")
    print(f"   SL: {result.sl_order_id}")
    print(f"   TP: {result.tp_order_id}")
    print(f"   Error: {result.error}")

if __name__ == "__main__":
    main()
