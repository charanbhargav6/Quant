import sys
sys.path.insert(0, "/home/ubuntu/engine")
from data.market_data_router import get_data_router
r = get_data_router()
ws_df = r._try_websocket("AUDUSD=X", "1h", 250)
print(f"ws_df: {len(ws_df) if ws_df is not None else None}")

ws_df_btc = r._try_websocket("BTCUSDT", "1h", 250)
print(f"ws_df_btc: {len(ws_df_btc) if ws_df_btc is not None else None}")
