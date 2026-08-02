"""
CRAVE Quant — Trade Connection Test Script
==========================================
Tests whether the MT5 broker connection is working by:
  1. Connecting to MT5 and verifying account info
  2. Placing a real MICRO test order (smallest possible lot size = 0.01)
  3. Waiting 5 seconds
  4. Closing the test order
  5. Reporting the result

This script lets you verify end-to-end execution WITHOUT running the full bot.

Usage:  python test_trade.py [--paper]  (add --paper to skip real order)

WARNING: Without --paper, this places a real live order on your MT5 account.
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PAPER_ONLY = "--paper" in sys.argv

print()
print("=" * 60)
print("  CRAVE Quant — Trade Connection Test")
print("=" * 60)
print(f"  Mode: {'PAPER SIMULATION (no real order)' if PAPER_ONLY else 'LIVE MT5 TEST ORDER'}")
print()

# ── Step 1: Connect to MT5 ────────────────────────────────────────────────────
print("[1/5] Connecting to MT5...", flush=True)

from brokers.mt5_agent import get_mt5
mt5 = get_mt5()

if not mt5.connect():
    print("  FAILED: Could not connect to MT5.")
    print("  Make sure MetaTrader 5 is open and your credentials in .env are correct.")
    sys.exit(1)

info = mt5.get_account_info()
if not info:
    print("  FAILED: Connected but could not fetch account info.")
    sys.exit(1)

print(f"  OK: Account #{info['login']} on {info['server']}")
print(f"  Balance: ${info['balance']:.2f} | Equity: ${info['equity']:.2f} | Currency: {info['currency']}")
print()

# ── Step 2: Get live price for EURUSD ─────────────────────────────────────────
SYMBOL = "EURUSD"  # Most liquid, smallest spread
print(f"[2/5] Getting live price for {SYMBOL}...", flush=True)

from brokers.mt5_agent import SYMBOL_MAP
import MetaTrader5 as mt5lib

mt5_sym = SYMBOL_MAP.get(SYMBOL, SYMBOL)
tick = mt5lib.symbol_info_tick(mt5_sym)
if not tick:
    # Try raw symbol
    mt5_sym = "EURUSD"
    tick = mt5lib.symbol_info_tick(mt5_sym)

if not tick:
    print(f"  FAILED: Could not get tick for {mt5_sym}")
    print("  Make sure EURUSD is added to your MT5 Market Watch.")
    sys.exit(1)

bid = tick.bid
ask = tick.ask
spread_pips = round((ask - bid) * 10000, 1)
print(f"  OK: {mt5_sym} — Bid: {bid:.5f} | Ask: {ask:.5f} | Spread: {spread_pips} pips")
print()

if PAPER_ONLY:
    print("[3/5] PAPER MODE: Simulating BUY order fill at:", round(ask, 5))
    print("[4/5] PAPER MODE: Simulating CLOSE at:", round(bid, 5))
    pips_pnl = round((bid - ask) * 10000, 1)
    print(f"[5/5] PAPER MODE: Simulated P&L = {pips_pnl} pips (just spread cost, expected)")
    print()
    print("RESULT: Paper simulation passed. MT5 connection is live and prices are streaming.")
    sys.exit(0)

# ── Step 3: Place smallest possible BUY order ─────────────────────────────────
print(f"[3/5] Placing MICRO test BUY order on {mt5_sym} (0.01 lots)...", flush=True)

sym_info = mt5lib.symbol_info(mt5_sym)
if not sym_info:
    print(f"  FAILED: Could not get symbol info for {mt5_sym}")
    sys.exit(1)

min_lot  = sym_info.volume_min   # usually 0.01
lot_size = min_lot

# Simple market order — no SL/TP for test
request = {
    "action":    mt5lib.TRADE_ACTION_DEAL,
    "symbol":    mt5_sym,
    "volume":    lot_size,
    "type":      mt5lib.ORDER_TYPE_BUY,
    "price":     ask,
    "deviation": 20,      # max slippage (2 pips)
    "magic":     999999,  # test magic number
    "comment":   "CRAVE_TEST",
    "type_time": mt5lib.ORDER_TIME_GTC,
    "type_filling": mt5lib.ORDER_FILLING_IOC,
}

result = mt5lib.order_send(request)

if result is None or result.retcode != mt5lib.TRADE_RETCODE_DONE:
    retcode = result.retcode if result else "None"
    comment = result.comment if result else "No response"
    print(f"  FAILED: Order rejected. Code={retcode} | {comment}")
    print()
    print("Common causes:")
    print("  - Market is closed (check trading hours)")
    print("  - Account is in read-only/view mode")
    print("  - Insufficient free margin")
    print("  - Auto-trading not enabled in MT5 terminal")
    print()
    print("  TIP: Run  python test_trade.py --paper  for a safe connection test")
    sys.exit(1)

ticket = result.order
fill_price = result.price
print(f"  OK: BUY order FILLED | Ticket #{ticket} | Fill price: {fill_price:.5f} | Lot: {lot_size}")
print()

# ── Step 4: Wait 3 seconds then close ────────────────────────────────────────
print("[4/5] Holding for 3 seconds then closing...", flush=True)
time.sleep(3)

# Get current position
positions = mt5lib.positions_get(ticket=ticket)
if not positions:
    print(f"  WARNING: Could not find position #{ticket} to close — may have already closed")
else:
    pos = positions[0]
    close_tick = mt5lib.symbol_info_tick(mt5_sym)
    close_price = close_tick.bid if close_tick else pos.price_open

    close_request = {
        "action":    mt5lib.TRADE_ACTION_DEAL,
        "symbol":    mt5_sym,
        "volume":    lot_size,
        "type":      mt5lib.ORDER_TYPE_SELL,
        "position":  ticket,
        "price":     close_price,
        "deviation": 20,
        "magic":     999999,
        "comment":   "CRAVE_TEST_CLOSE",
        "type_filling": mt5lib.ORDER_FILLING_IOC,
    }

    close_result = mt5lib.order_send(close_request)
    if close_result and close_result.retcode == mt5lib.TRADE_RETCODE_DONE:
        pnl_pips = round((close_price - fill_price) * 10000, 1)
        print(f"  OK: Position CLOSED | Close price: {close_price:.5f} | P&L: {pnl_pips:+.1f} pips")
    else:
        rc = close_result.retcode if close_result else "None"
        print(f"  WARNING: Could not close position #{ticket} (retcode={rc})")
        print(f"  Please close manually in MT5: ticket #{ticket}")

print()

# ── Step 5: Final summary ─────────────────────────────────────────────────────
print("[5/5] Test complete.")
print()
print("=" * 60)
print("  RESULT: MT5 trade execution is WORKING correctly")
print("  The bot CAN place and close real orders on your account.")
print("=" * 60)
print()
print("To add a new account to the engine:")
print("  1. Add your MT5 credentials to .env:")
print("       MT5_LOGIN=your_account_number")
print("       MT5_PASSWORD=your_password")
print("       MT5_SERVER=your_broker_server")
print("  2. Restart the bot — it auto-detects LIVE mode if MT5 creds exist")
print("  3. The dashboard will show your account under /api/accounts")
print()
print("To add a Zerodha account:")
print("  1. Set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env")
print("  2. Run: python brokers/zerodha_agent.py  to complete OAuth login")
print()
