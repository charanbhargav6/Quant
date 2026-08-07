import sys
sys.path.insert(0, "/home/ubuntu/engine")
from data.market_data_router import get_data_router

r = get_data_router()
print("Fetching AUDUSD=X 15m 500 bars...")
df_15m = r.get_ohlcv("AUDUSD=X", "15m", 500, force_fresh=True)
print(f"15m: {len(df_15m) if df_15m is not None else None}")

print("Fetching BTCUSDT 15m 500 bars...")
df_btc_15m = r.get_ohlcv("BTCUSDT", "15m", 500, force_fresh=True)
print(f"BTC 15m: {len(df_btc_15m) if df_btc_15m is not None else None}")
