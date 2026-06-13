import sys
sys.path.insert(0, "/home/ubuntu/engine")
from data.market_data_router import get_data_router
r = get_data_router()
df_1h = r.get_ohlcv("AUDUSD=X", "1h", 250)
df_4h = r.get_ohlcv("AUDUSD=X", "4h", 60)
print(f"1h: {len(df_1h) if df_1h is not None else None}")
print(f"4h: {len(df_4h) if df_4h is not None else None}")
