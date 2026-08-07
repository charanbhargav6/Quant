import sys
sys.path.insert(0, "/home/ubuntu/engine")
from core.trading_loop import _get_ohlcv_with_ws_fallback

df1 = _get_ohlcv_with_ws_fallback('BTCUSDT', '15m', 500)
df2 = _get_ohlcv_with_ws_fallback('BTCUSDT', '1h', 250)
df3 = _get_ohlcv_with_ws_fallback('BTCUSDT', '4h', 60)
print(f'BTC: 15m={len(df1) if df1 is not None else None}, 1h={len(df2) if df2 is not None else None}, 4h={len(df3) if df3 is not None else None}')
