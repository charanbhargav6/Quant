import sys
sys.path.insert(0, "/home/ubuntu/engine")
from core.trading_loop import _get_ohlcv_with_ws_fallback
print("Testing _get_ohlcv_with_ws_fallback for ICICIBANK...")
res = _get_ohlcv_with_ws_fallback("ICICIBANK", "1h", 250)
print(f"Result: {len(res) if res is not None else None}")
