import sys
sys.path.insert(0, "/home/ubuntu/engine")
from data.market_data_router import get_data_router
r = get_data_router()
df = r._fetch_yfinance("AUDUSD=X", "1h", 250)
print(f"yfinance 1h: {len(df) if df is not None else None}")
