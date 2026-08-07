import sys
sys.path.insert(0, "/home/ubuntu/engine")
from config.config import get_instrument
print(f"AXISBANK config: {get_instrument('AXISBANK')}")
from data.market_data_router import get_data_router
r = get_data_router()
print("Fetching AXISBANK via router...")
df = r.get_ohlcv('AXISBANK', '1h', 50, force_fresh=True)
print(f"Result: {len(df) if df is not None else None}")
