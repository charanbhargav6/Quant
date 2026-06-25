"""Test the rewritten instrument scanner against multiple symbols."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')

from engines.instrument_scanner import scanner

test_symbols = ['XAUUSD=X', 'EURUSD=X', 'USDJPY=X', 'GBPUSD=X', 'BTCUSDT']

print("=" * 70)
print("INSTRUMENT SCANNER v11.1 - VERIFICATION TEST")
print("=" * 70)

for sym in test_symbols:
    try:
        result = scanner._score_instrument(sym)
        print(f"\n{'-' * 50}")
        print(f"  {sym}")
        print(f"  Score: {result['score']}/13  |  Tradeable: {result['tradeable']}")
        print(f"  Reason: {result['reason']}")
        print(f"  Breakdown:")
        for k, v in result['breakdown'].items():
            print(f"    {k:12s}: {v}")
    except Exception as e:
        print(f"\n  {sym}: CRASHED: {e}")
        import traceback
        traceback.print_exc()

print(f"\n{'=' * 70}")
print("TEST COMPLETE")
print(f"{'=' * 70}")
