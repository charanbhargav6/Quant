import sys
sys.path.insert(0, "/home/ubuntu/engine")
from data.market_data_router import get_data_router
r = get_data_router()
print("Without force_fresh...")
df_15m = r.get_ohlcv("AUDUSD=X", "15m", 500)
print(f"AUDUSD=X 15m: {len(df_15m) if df_15m is not None else None}")
df_btc_15m = r.get_ohlcv("BTCUSDT", "15m", 500)
print(f"BTCUSDT 15m: {len(df_btc_15m) if df_btc_15m is not None else None}")
