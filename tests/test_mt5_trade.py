import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

from brokers.mt5_agent import get_mt5
import MetaTrader5 as mt5

agent = get_mt5()
ok = agent.connect()
print(f"Connected: {ok}")

# Get current EURUSD price
tick = mt5.symbol_info_tick("EURUSD")
print(f"EURUSD Ask: {tick.ask}  Bid: {tick.bid}")

# Place a tiny test BUY with 30 pip SL and 60 pip TP
entry = tick.ask
sl = round(entry - 0.0030, 5)   # 30 pips below
tp = round(entry + 0.0060, 5)   # 60 pips above

print(f"Placing TEST order: BUY 0.01 EURUSD @ {entry} SL={sl} TP={tp}")

result = agent.place_order(
    crave_symbol="EURUSD=X",
    direction="buy",
    lot_size=0.01,
    sl=sl,
    tp=tp,
    comment="CRAVE TEST"
)
print(f"Result: {result}")

if result.get("status") == "filled":
    ticket = result["ticket"]
    fill = result["fill_price"]
    print(f"SUCCESS! Ticket: {ticket} | Fill price: {fill}")
    print("Check your MT5 terminal - you should see the trade!")
else:
    print(f"FAILED: {result.get('reason')}")
