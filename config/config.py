"""
Trading Engine v11.0 — Central Configuration (Standalone)
==========================================================
Extracted from CRAVE → Standalone Trading Engine
  ✅ All audit Part 4 additions (ML, MARKETS, PORTFOLIO_RISK, OPTIONS, INDIA)
  ✅ US Stock instruments (Session 5A)
  ✅ Indian market instruments (Session 5B) — ENABLED with 14 stocks
  ✅ Zerodha integration with auto-refresh
  ✅ India VIX regime detection
  ✅ Expiry day strategy support
  ✅ Fixed ML_DIR path bug
  ✅ Supabase dashboard backend
"""

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────

ENGINE_ROOT  = Path(__file__).parent.parent
ROOT_DIR     = ENGINE_ROOT  # Alias for backward compat
STATE_DIR    = ENGINE_ROOT / "State"
DATABASE_DIR = ENGINE_ROOT / "Database"
LOGS_DIR     = ENGINE_ROOT / "Logs"
CONFIG_DIR   = ENGINE_ROOT / "config"
SETUP_DIR    = ENGINE_ROOT / "setup"
ML_DIR       = ENGINE_ROOT / "ml"  # Fixed: was broken nested path

for _d in [STATE_DIR, DATABASE_DIR, LOGS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

STATE_FILE     = STATE_DIR / "crave_state.json"
POSITIONS_FILE = STATE_DIR / "crave_positions.json"
BIAS_FILE      = STATE_DIR / "crave_bias.json"
JOURNAL_FILE   = STATE_DIR / "crave_journal.json"
DB_PATH        = DATABASE_DIR / "crave.db"

# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT VARIABLES — full documented list
# ─────────────────────────────────────────────────────────────────────────────
# Set these in .env file. Never hardcode.
#
# EXISTING (Sessions 1-4):
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   BINANCE_API_KEY, BINANCE_API_SECRET
#   ALPACA_API_KEY, ALPACA_SECRET_KEY
#   CRAVE_STATE_REPO, GITHUB_TOKEN
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_KEY_NAME
#
# NEW (Session 5):
#   ZERODHA_API_KEY       — from kite.zerodha.com/apps
#   ZERODHA_ACCESS_TOKEN  — refreshed daily at 03:30 UTC via zerodha_agent.py
#   ZERODHA_REDIRECT_URL  — your app's redirect URL for OAuth flow
#   ALPACA_PAPER_URL      — https://paper-api.alpaca.markets (stocks paper)
#   POLYGON_API_KEY       — optional: better US market data than Alpaca
#   TRADING_MODE          — paper / live (default: paper)
#   USE_DEEP_REGIME       — True = LSTM Engine, False = XGBoost/Rules (default: False)
#
# NEW (Session 10):
#   PROP_FIRM             — Name of the prop firm (e.g. ftmo, the5ers). Default: ftmo
#   ACCOUNT_SIZE          — Account size. Must match PAPER_TRADING starting_equity
#                           in paper mode. Default: 10000
# ─────────────────────────────────────────────────────────────────────────────

PROP_FIRM = os.environ.get("PROP_FIRM", "fundingpips").lower()
try:
    ACCOUNT_SIZE = float(os.environ.get("ACCOUNT_SIZE", "1000"))
except ValueError:
    ACCOUNT_SIZE = 1000.0

USE_DEEP_REGIME = os.environ.get("USE_DEEP_REGIME", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# NODE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

NODES = {
    "laptop": {
        "hostname_patterns": ["DESKTOP", "LAPTOP", "WSL", "CHARAN", "PC"],
        "can_run": [
            "full_bot", "backtest", "ml_training",
            "paper_trading", "position_monitor", "signal_detection",
            "daily_bias", "instrument_scan", "websocket",
        ],
        "db_path": str(DB_PATH),
        "is_primary": True,
    },
    "phone": {
        "hostname_patterns": ["localhost", "android", "termux"],
        "can_run": [
            "full_bot", "lite_bot", "position_monitor", "signal_detection",
            "daily_bias", "instrument_scan", "websocket",
            "telegram_interface", "state_sync", "heartbeat", "paper_trading"
        ],
        "db_path": "/data/data/com.termux/files/home/CRAVE/Database/crave.db",
        "thermal_limit_celsius": 42,
        "thermal_warn_celsius":  38,
        "is_primary": False,
    },
    "aws": {
        "hostname_patterns": ["ip-", "ec2", "ubuntu"],
        "can_run": [
            "full_bot", "position_monitor", "signal_detection",
            "daily_bias", "instrument_scan", "websocket",
        ],
        "db_path": "/home/ubuntu/CRAVE/Database/crave.db",
        "is_primary": False,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# INSTRUMENTS
# ─────────────────────────────────────────────────────────────────────────────
# Fields:
#   label:           human-readable name
#   asset_class:     crypto / gold / silver / forex / stocks / stocks_india /
#                    index_futures / options
#   sl_mult:         ATR multiplier for stop loss
#   rr:              risk:reward target
#   min_days:        minimum backtest window days
#   sessions:        which kill zone(s) this instrument trades in
#   exchange:        binance / alpaca / zerodha / yfinance
#   type:            spot / futures / forex / equity / fo
#   lot_size_type:   "units" (forex/crypto) or "shares" (stocks)
#   pip_size:        minimum price movement
#   currencies:      list of currencies for red-folder check
#   funding_check:   True for perpetual futures
#   backtest_only:   True = excluded from live trading
#   market:          crypto / forex / us_stocks / india / gold
#   enabled:         False = temporarily disabled (easy on/off switch)
#   gap_risk:        True = needs overnight gap protection
#   earnings_blackout: True = block entries near earnings

INSTRUMENTS = {

    # ═════════════════════════════════════════════════════════════════════════
    # EXISTING INSTRUMENTS (unchanged from v10.0)
    # ═════════════════════════════════════════════════════════════════════════

    # ── Gold & Silver ──────────────────────────────────────────────────────
    "XAUUSD=X": {
        "label": "Gold", "asset_class": "gold", "market": "gold",
        "sl_mult": 2.0, "rr": 2.0, "min_days": 60,
        "sessions": ["london", "ny"], "exchange": "mt5", "type": "spot",
        "lot_size_type": "units", "currencies": ["XAU", "USD"],
        "pip_size": 0.01, "enabled": True,
    },
    "XAGUSD=X": {
        "label": "Silver", "asset_class": "silver", "market": "gold",
        "sl_mult": 2.0, "rr": 2.0, "min_days": 60,
        "sessions": ["london", "ny"], "exchange": "mt5", "type": "spot",
        "lot_size_type": "units", "currencies": ["XAG", "USD"],
        "pip_size": 0.001, "enabled": False,
    },

    # ── Crypto (Binance Futures) ───────────────────────────────────────────
    "BTCUSDT": {
        "label": "Bitcoin", "asset_class": "crypto", "market": "crypto",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 30,
        "sessions": ["london", "ny", "asian"], "exchange": "binance", "type": "futures",
        "lot_size_type": "units", "currencies": ["BTC"],
        "pip_size": 0.1, "funding_check": True, "enabled": True,
    },
    "ETHUSDT": {
        "label": "Ethereum", "asset_class": "crypto", "market": "crypto",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 30,
        "sessions": ["london", "ny", "asian"], "exchange": "binance", "type": "futures",
        "lot_size_type": "units", "currencies": ["ETH"],
        "pip_size": 0.01, "funding_check": True, "enabled": True,
    },
    "SOLUSDT": {
        "label": "Solana", "asset_class": "crypto", "market": "crypto",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 30,
        "sessions": ["london", "ny", "asian"], "exchange": "binance", "type": "futures",
        "lot_size_type": "units", "currencies": ["SOL"],
        "pip_size": 0.001, "funding_check": True, "enabled": True,
    },

    # ── Forex Majors ──────────────────────────────────────────────────────
    "EURUSD=X": {
        "label": "Euro/Dollar", "asset_class": "forex", "market": "forex",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 90,
        "sessions": ["london", "ny"], "exchange": "mt5", "type": "forex",
        "lot_size_type": "units", "currencies": ["EUR", "USD"],
        "pip_size": 0.0001, "enabled": True,
    },
    "GBPUSD=X": {
        "label": "Pound/Dollar", "asset_class": "forex", "market": "forex",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 90,
        "sessions": ["london", "ny"], "exchange": "mt5", "type": "forex",
        "lot_size_type": "units", "currencies": ["GBP", "USD"],
        "pip_size": 0.0001, "enabled": True,
    },
    "USDJPY=X": {
        "label": "Dollar/Yen", "asset_class": "forex", "market": "forex",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 90,
        "sessions": ["london", "ny", "asian"], "exchange": "mt5", "type": "forex",
        "lot_size_type": "units", "currencies": ["USD", "JPY"],
        "pip_size": 0.01, "enabled": True,
    },
    "AUDUSD=X": {
        "label": "Aussie/Dollar", "asset_class": "forex", "market": "forex",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 90,
        "sessions": ["london", "ny", "asian"], "exchange": "mt5", "type": "forex",
        "lot_size_type": "units", "currencies": ["AUD", "USD"],
        "pip_size": 0.0001, "enabled": True,
    },

    # ── yfinance backtest-only ─────────────────────────────────────────────
    "BTC-USD": {
        "label": "Bitcoin (yfinance)", "asset_class": "crypto", "market": "crypto",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 30,
        "sessions": ["london", "ny", "asian"], "exchange": "yfinance", "type": "spot",
        "lot_size_type": "units", "currencies": ["BTC"],
        "pip_size": 0.1, "backtest_only": True, "enabled": True,
    },
    "ETH-USD": {
        "label": "Ethereum (yfinance)", "asset_class": "crypto", "market": "crypto",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 30,
        "sessions": ["london", "ny", "asian"], "exchange": "yfinance", "type": "spot",
        "lot_size_type": "units", "currencies": ["ETH"],
        "pip_size": 0.01, "backtest_only": True, "enabled": True,
    },

    # ═════════════════════════════════════════════════════════════════════════
    # SESSION 5A — US STOCKS (Alpaca)
    # Disabled by default. Enable in MARKETS["us_stocks"]["enabled"] = True
    # and set ALPACA_API_KEY in .env.
    # ═════════════════════════════════════════════════════════════════════════

    "AAPL": {
        "label": "Apple", "asset_class": "stocks", "market": "us_stocks",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": True, "gap_risk": True,
        "sector": "technology", "enabled": False,
    },
    "NVDA": {
        "label": "Nvidia", "asset_class": "stocks", "market": "us_stocks",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": True, "gap_risk": True,
        "sector": "technology", "enabled": False,
    },
    "TSLA": {
        "label": "Tesla", "asset_class": "stocks", "market": "us_stocks",
        "sl_mult": 2.0, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": True, "gap_risk": True,
        "sector": "automotive", "enabled": False,
    },
    "MSFT": {
        "label": "Microsoft", "asset_class": "stocks", "market": "us_stocks",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": True, "gap_risk": True,
        "sector": "technology", "enabled": False,
    },
    "SPY": {
        "label": "S&P 500 ETF", "asset_class": "indices", "market": "us_stocks",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "etf",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": False, "gap_risk": True,
        "sector": "index", "enabled": False,
    },
    "QQQ": {
        "label": "Nasdaq 100 ETF", "asset_class": "indices", "market": "us_stocks",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["us_open_drive", "us_power_hour"],
        "exchange": "alpaca", "type": "etf",
        "lot_size_type": "shares", "min_shares": 1,
        "currencies": ["USD"], "pip_size": 0.01,
        "market_hours_utc": {"open": "13:30", "close": "20:00"},
        "earnings_blackout": False, "gap_risk": True,
        "sector": "index", "enabled": False,
    },

    # ═════════════════════════════════════════════════════════════════════════
    # SESSION 5B — INDIAN STOCKS (Zerodha Kite Connect)
    # Disabled by default. Enable MARKETS["india"]["enabled"] = True
    # and set ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN in .env.
    # Token must be refreshed daily at 03:30 UTC via zerodha_agent.daily_login()
    # ═════════════════════════════════════════════════════════════════════════

    "RELIANCE": {
        "label": "Reliance Industries", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "RELIANCE",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "energy", "enabled": True,
    },
    "TCS": {
        "label": "Tata Consultancy Services", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "TCS",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "technology", "enabled": True,
    },
    "HDFCBANK": {
        "label": "HDFC Bank", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "HDFCBANK",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "banking", "enabled": True,
    },
    "INFY": {
        "label": "Infosys", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "INFY",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "technology", "enabled": True,
    },

    # ── NEW: Additional high-liquidity Indian stocks ───────────────────────
    "ICICIBANK": {
        "label": "ICICI Bank", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "ICICIBANK",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "banking", "enabled": True,
    },
    "SBIN": {
        "label": "State Bank of India", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "SBIN",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "banking", "enabled": True,
    },
    "BHARTIARTL": {
        "label": "Bharti Airtel", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "BHARTIARTL",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "telecom", "enabled": True,
    },
    "ITC": {
        "label": "ITC Limited", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "ITC",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "fmcg", "enabled": True,
    },
    "BAJFINANCE": {
        "label": "Bajaj Finance", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 2.0, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "BAJFINANCE",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "finance", "enabled": True,
    },
    "TATAMOTORS": {
        "label": "Tata Motors", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 2.0, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "TATAMOTORS",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "automotive", "enabled": True,
    },
    "MARUTI": {
        "label": "Maruti Suzuki", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "MARUTI",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "automotive", "enabled": True,
    },
    "WIPRO": {
        "label": "Wipro", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "WIPRO",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "technology", "enabled": True,
    },
    "AXISBANK": {
        "label": "Axis Bank", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "AXISBANK",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "banking", "enabled": True,
    },
    "KOTAKBANK": {
        "label": "Kotak Mahindra Bank", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "equity",
        "lot_size_type": "shares", "min_shares": 1,
        "kite_exchange": "NSE", "tradingsymbol": "KOTAKBANK",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "circuit_breaker_pct": 10.0,
        "earnings_blackout": True, "gap_risk": False,
        "sector": "banking", "enabled": True,
    },

    "NIFTY50": {
        "label": "Nifty 50 Index", "asset_class": "indices", "market": "india",
        "sl_mult": 1.5, "rr": 1.5, "min_days": 60,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "index",
        "lot_size_type": "shares",
        "kite_exchange": "NSE", "tradingsymbol": "NIFTY 50",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "enabled": True, "backtest_only": True,
    },
    # NIFTY Futures (F&O)
    "NIFTY_FUT": {
        "label": "Nifty Futures", "asset_class": "index_futures", "market": "india",
        "sl_mult": 1.5, "rr": 1.5, "min_days": 30,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "fo",
        "lot_size_type": "lots", "lot_size": 50,
        "kite_exchange": "NFO", "tradingsymbol": "NIFTY",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "fo_expiry_day": "Thursday",
        "margin_required": True, "enabled": True,
    },
    "BANKNIFTY_FUT": {
        "label": "Bank Nifty Futures", "asset_class": "index_futures", "market": "india",
        "sl_mult": 1.5, "rr": 1.5, "min_days": 30,
        "sessions": ["india_open_drive", "india_close_drive"],
        "exchange": "zerodha", "type": "fo",
        "lot_size_type": "lots", "lot_size": 15,
        "kite_exchange": "NFO", "tradingsymbol": "BANKNIFTY",
        "currencies": ["INR"], "pip_size": 0.05,
        "market_hours_utc": {"open": "04:00", "close": "10:00"},
        "fo_expiry_day": "Wednesday",
        "margin_required": True, "enabled": True,
    },

    # ── yfinance India tickers (backtest only) ─────────────────────────────
    "^NSEI": {
        "label": "Nifty 50 (yfinance)", "asset_class": "indices", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "index",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
    "^BSESN": {
        "label": "Sensex (yfinance)", "asset_class": "indices", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "index",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
    "RELIANCE.NS": {
        "label": "Reliance (yfinance)", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "equity",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
    "TCS.NS": {
        "label": "TCS (yfinance)", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "equity",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
    "ICICIBANK.NS": {
        "label": "ICICI Bank (yfinance)", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "equity",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
    "SBIN.NS": {
        "label": "SBI (yfinance)", "asset_class": "stocks_india", "market": "india",
        "sl_mult": 1.5, "rr": 2.0, "min_days": 60,
        "sessions": ["india_open_drive"], "exchange": "yfinance", "type": "equity",
        "lot_size_type": "shares", "currencies": ["INR"],
        "pip_size": 0.05, "backtest_only": True, "enabled": True,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# RISK MANAGEMENT (unchanged from v10.0)
# ─────────────────────────────────────────────────────────────────────────────

RISK = {
    "base_risk_pct": 1.0,
    "scale_table": {
        "3+_losses": 0.25, "2_losses": 0.50,
        "neutral":   1.00, "1-2_wins": 1.50,
        "3-4_wins":  2.00, "5+_wins":  2.50,
    },
    "grade_multipliers": {
        "A+": 1.00, "A": 0.75, "B+": 0.50, "B": 0.25,
    },
    "max_daily_loss_pct":          4.0,
    "max_account_drawdown_pct":    10.0,
    "circuit_breaker_losing_days": 2,
    "circuit_breaker_cooldown_h":  24,
    "min_rr_ratio":                1.5,
    "max_correlated_exposure_pct": 2.0,
    "correlation_threshold":       0.70,
    "event_hedge_reduce_pct":      50.0,
    "event_window_mins":           90,
    "weekend_partial_close_pct":   30.0,
    "weekend_close_time_utc":      "20:00",
    "stale_trade_hours":           48,
    "stale_sl_compress_pct":       25,
}

# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE GATES
# ─────────────────────────────────────────────────────────────────────────────

CONFIDENCE_GATES = {
    "BTCUSDT": 0.45,
    "ETHUSDT": 0.45,
    "EURUSD=X": 0.45,
    "XAUUSD=X": 0.40,
    "NIFTY50": 0.55,
    "SPY": 0.55,
    "default": 0.40,
}

# ─────────────────────────────────────────────────────────────────────────────
# PARTIAL BOOKING SCHEDULE (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

PARTIAL_BOOKING = [
    # v11.2 FIX: Old "breakeven at 1R" was the #1 cause of 12% win rate.
    # Price hits 1R (tiny move with tight SL), SL moves to entry,
    # any normal pullback closes at 0R → most trades are breakeven.
    # NEW: Protect 70% at 1R (-0.3R), full breakeven only at 2R.
    {"r_level": 1.0, "close_pct": 25, "sl_move_to": "-0.3R"},
    {"r_level": 2.0, "close_pct": 25, "sl_move_to": "breakeven"},
    {"r_level": 3.0, "close_pct": 25, "sl_move_to": "+1R"},
    {"r_level": 4.0, "close_pct": 25, "sl_move_to": "+2R"},
]

# ─────────────────────────────────────────────────────────────────────────────
# KILL ZONES — extended for US and India markets
# ─────────────────────────────────────────────────────────────────────────────

KILL_ZONES = {
    # ── Original (crypto / forex / gold) ──────────────────────────────────
    "london": {
        "start_utc": "06:00", "end_utc": "11:00",
        "instruments": "all", "priority": 1,
    },
    "ny": {
        "start_utc": "11:30", "end_utc": "16:00",
        "instruments": "all", "priority": 1,
    },
    "asian": {
        "start_utc": "22:00", "end_utc": "03:00",
        "instruments": ["USDJPY=X", "BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "priority": 2,
    },
    "london_close": {
        "start_utc": "14:00", "end_utc": "17:00",
        "instruments": ["XAUUSD=X", "EURUSD=X", "GBPUSD=X"],
        "priority": 2,
    },

    # ── Session 5A: US Stocks ──────────────────────────────────────────────
    # Only trade open_drive and power_hour for stocks.
    # Lunch (15:00-19:00 UTC) = low volume chop — SMC signals fail here.
    "us_open_drive": {
        "start_utc": "13:30", "end_utc": "15:30",
        "instruments": ["AAPL", "NVDA", "TSLA", "MSFT", "SPY", "QQQ"],
        "priority": 1,
        "market": "us_stocks",
    },
    "us_power_hour": {
        "start_utc": "19:00", "end_utc": "20:00",
        "instruments": ["AAPL", "NVDA", "TSLA", "MSFT", "SPY", "QQQ"],
        "priority": 2,
        "market": "us_stocks",
    },

    # ── Session 5B: Indian Stocks ──────────────────────────────────────────
    # IST = UTC + 5:30
    # Best SMC setups on NSE: open drive and close drive only.
    # Midday (05:30-08:30 UTC = 11:00-14:00 IST) = low volume, skip.
    "india_open_drive": {
        "start_utc": "03:00", "end_utc": "06:30",  # 09:30-11:00 IST (+ widened)
        "instruments": ["RELIANCE", "TCS", "HDFCBANK", "INFY",
                         "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
                         "BAJFINANCE", "TATAMOTORS", "MARUTI",
                         "WIPRO", "AXISBANK", "KOTAKBANK",
                         "NIFTY_FUT", "BANKNIFTY_FUT"],
        "priority": 1,
        "market": "india",
    },
    "india_close_drive": {
        "start_utc": "07:30", "end_utc": "11:00",  # 14:00-15:30 IST (+ widened)
        "instruments": ["RELIANCE", "TCS", "HDFCBANK", "INFY",
                         "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
                         "BAJFINANCE", "TATAMOTORS", "MARUTI",
                         "WIPRO", "AXISBANK", "KOTAKBANK",
                         "NIFTY_FUT", "BANKNIFTY_FUT"],
        "priority": 1,
        "market": "india",
    },
    # Pre-open auction session — FII/DII order flow visible here
    "india_pre_open": {
        "start_utc": "03:45", "end_utc": "04:00",  # 09:15-09:30 IST
        "instruments": ["NIFTY_FUT", "BANKNIFTY_FUT"],
        "priority": 2,
        "market": "india",
        "analysis_only": True,  # Don't trade, just gather data
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# MARKETS — per-market enable/disable + heat limits
# ─────────────────────────────────────────────────────────────────────────────

MARKETS = {
    "crypto":    {"enabled": True,  "broker": "binance",  "max_heat_pct": 3.0},
    "forex":     {"enabled": True,  "broker": "alpaca",   "max_heat_pct": 2.0},
    "gold":      {"enabled": True,  "broker": "alpaca",   "max_heat_pct": 2.0},
    "us_stocks": {"enabled": False, "broker": "alpaca",   "max_heat_pct": 2.0},
    "india":     {"enabled": True,  "broker": "zerodha",  "max_heat_pct": 2.5},
    "options":   {"enabled": True,  "broker": "zerodha",  "max_heat_pct": 1.5},
}

# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO RISK (Session 7)
# ─────────────────────────────────────────────────────────────────────────────

PORTFOLIO_RISK = {
    "max_total_heat_pct":        6.0,
    "max_single_market_pct":     3.0,
    "max_currency_exposure_pct": 40.0,
    "max_vega_exposure_pct":     5.0,
    "emergency_close_at_pct":    6.5,
}

# ─────────────────────────────────────────────────────────────────────────────
# ML TRAINING — single source of truth (audit M8)
# ─────────────────────────────────────────────────────────────────────────────

ML = {
    "min_training_rows":        100,
    "production_quality_rows":  500,
    "retrain_interval_trades":  100,
    "feature_count":            26,
}

# ─────────────────────────────────────────────────────────────────────────────
# OPTIONS (Session 6)
# ─────────────────────────────────────────────────────────────────────────────

OPTIONS = {
    "min_dte": 21, "max_dte": 45,
    "min_iv_rank_to_sell": 50, "max_iv_rank_to_buy": 30,
    "max_single_option_risk": 1.0,
    "greeks": {
        "max_delta_drift": 0.15,
        "max_theta_decay_pct": 2.0,
        "max_vega_pct": 5.0,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# INDIA (Session 5B)
# ─────────────────────────────────────────────────────────────────────────────

INDIA = {
    "broker":               "zerodha",
    "timezone":             "Asia/Kolkata",
    "market_open_utc":      "04:00",
    "market_close_utc":     "10:00",
    "pre_open_utc":         "03:45",
    "token_refresh_utc":    "03:30",
    "fo_expiry_day":        "Thursday",
    "banknifty_expiry_day": "Wednesday",
    "circuit_breaker_pcts": [5, 10, 20],
    "t1_settlement":        True,
    "fii_dii_enabled":      True,
    "pcr_enabled":          True,
    "overnight_close_check_utc": "09:45",
    # India VIX regime detection — key for max returns / min loss
    "vix_enabled":          True,
    "vix_symbol":           "^INDIAVIX",
    "vix_high_threshold":   20.0,   # Above 20 = sell options, reduce size
    "vix_low_threshold":    13.0,   # Below 13 = directional trades ok
    "vix_extreme_threshold": 28.0,  # Above 28 = halt new entries
    # Expiry day strategy — weekly NIFTY/BANKNIFTY options
    "expiry_day_enabled":   True,
    "expiry_straddle_entry_utc":   "04:30",  # 10:00 IST
    "expiry_straddle_exit_utc":    "09:30",  # 15:00 IST
    "expiry_max_loss_pct":         1.0,      # Max 1% account loss on expiry trades
    # Delivery volume analysis for swing trades
    "delivery_volume_enabled":     True,
    "delivery_volume_threshold":   50.0,     # >50% delivery = institutional interest
}

# ─────────────────────────────────────────────────────────────────────────────
# US STOCKS (Session 5A)
# ─────────────────────────────────────────────────────────────────────────────

US_STOCKS = {
    "broker":               "alpaca",
    "market_open_utc":      "13:30",
    "market_close_utc":     "20:00",
    "premarket_open_utc":   "09:00",
    "after_hours_close_utc": "21:00",
    "overnight_close_check_utc": "19:45",
    "t2_settlement":        True,
    "max_position_value_pct": 20.0,
    "earnings_blackout_days_before": 2,
    "earnings_blackout_days_after":  1,
}

# ─────────────────────────────────────────────────────────────────────────────
# EVENT SPIKE THRESHOLDS — asset-class aware (audit M11)
# ─────────────────────────────────────────────────────────────────────────────

EVENT_SPIKE_THRESHOLDS = {
    "forex":        0.0015,
    "gold":         0.0020,
    "silver":       0.0025,
    "crypto":       0.0050,
    "stocks":       0.0020,
    "stocks_india": 0.0020,
    "indices":      0.0030,
    "default":      0.0020,
}

# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC TP, TELEGRAM, PAPER TRADING, STATE SYNC, AWS, LOGGING
# (unchanged from v10.0 — reproduced for completeness)
# ─────────────────────────────────────────────────────────────────────────────

DYNAMIC_TP = {
    "check_interval_mins":      15,
    "min_conditions_to_extend": 2,
    "order_book_imbalance_pct": 70,
    "volume_node_min_gap":      0.003,
    "liquidity_void_threshold": 0.5,
    "funding_rate_danger":      0.05,
    "max_extensions":           None,
    "tp_can_decrease":          False,
}

TELEGRAM = {
    "token_env_var":      "TELEGRAM_BOT_TOKEN",
    "chat_id_env_var":    "TELEGRAM_CHAT_ID",
    "daily_summary_utc":  "21:00",
    "weekly_summary_day": "sunday",
    "weekly_summary_utc": "20:00",
    "alert_on": [
        "trade_open", "trade_close", "tp1_hit", "tp_extended",
        "sl_hit", "circuit_breaker", "thermal_handoff",
        "node_failover", "daily_loss_limit", "drawdown_warning",
        "event_hedge", "weekend_partial_close", "earnings_blackout",
        "circuit_breaker_nse", "zerodha_token_refresh",
    ],
}

TELEGRAM_COMMANDS = {
    "/start":      "Show welcome + status",
    "/status":     "Streak state, circuit breaker, risk level",
    "/positions":  "All open positions with entry/SL/TP",
    "/close":      "Close a position: /close XAUUSD",
    "/pause":      "Pause new entries",
    "/resume":     "Resume after pause",
    "/half_size":  "Toggle half-size mode",
    "/bias":       "Today's daily bias per instrument",
    "/levels":     "Key levels: /levels XAUUSD",
    "/tp_check":   "Force TP extension check",
    "/node":       "Node status, temps, uptime",
    "/switch":     "Switch node: /switch phone",
    "/aws_start":  "Start AWS instance",
    "/aws_stop":   "Stop AWS instance",
    "/temp":       "Phone CPU temperature",
    "/stats":      "Win rate, expectancy, streak",
    "/journal":    "Last 10 closed trades",
    "/paper":      "Paper trading status + readiness",
    "/readiness":  "Run full readiness gate check",
    "/live":       "Request live trading mode",
    "/ml":         "ML model status",
    "/ws":         "WebSocket connection status",
    "/markets":    "All market status (open/closed/disabled)",
    "/india":      "Indian market status + FII/DII + PCR",
    "/earnings":   "Upcoming earnings blackouts: /earnings AAPL",
    "/portfolio":  "Full portfolio heat by market",
    "/help":       "Show all commands",
}

PAPER_TRADING = {
    "enabled":               True,
    "starting_equity":       ACCOUNT_SIZE,
    "min_trades_for_live":   30,
    "min_win_rate":          50.0,
    "max_wr_deviation_pct":  5.0,
    "max_dd_deviation_pct":  2.0,
    "simulate_slippage":     True,
    "simulate_spread":       True,
    "readiness_gate_required": True,
}

STATE_SYNC = {
    "enabled":             True,
    "repo_env_var":        "CRAVE_STATE_REPO",
    "token_env_var":       "GITHUB_TOKEN",
    "branch":              "state",
    "sync_interval_secs":  60,
    "pull_interval_secs":  30,
    "files_to_sync": [
        "State/crave_state.json",
        "State/crave_positions.json",
        "State/crave_bias.json",
    ],
    "files_to_sync_slow": ["State/crave_journal.json"],
    "slow_sync_interval_secs": 300,
}

AWS = {
    "region":           "ap-south-1",
    "instance_type":    "t3.small",
    "ami_id":           "ami-0f58b397bc5c1f2e8",
    "key_name_env":     "AWS_KEY_NAME",
    "security_group":   "crave-sg",
    "auto_stop_when_primary_resumes": True,
    "heartbeat_timeout_secs": 120,
}

LOGGING = {
    "level":          "INFO",
    "log_dir":        str(LOGS_DIR),
    "max_size_mb":    10,
    "backup_count":   5,
    "log_to_file":    True,
    "log_to_console": True,
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS — updated for Session 5
# ─────────────────────────────────────────────────────────────────────────────

def get_instrument(symbol: str) -> dict:
    return INSTRUMENTS.get(symbol, {})

def get_asset_class(symbol: str) -> str:
    return INSTRUMENTS.get(symbol, {}).get("asset_class", "default")

def get_sl_mult(symbol: str) -> float:
    return INSTRUMENTS.get(symbol, {}).get("sl_mult", 1.5)

def get_tradeable_symbols() -> list:
    """Return enabled symbols that are not backtest-only."""
    result = []
    for symbol, cfg in INSTRUMENTS.items():
        if cfg.get("backtest_only", False):
            continue
        if not cfg.get("enabled", True):
            continue
        # Check if the market is enabled
        market = cfg.get("market", "")
        if market and not MARKETS.get(market, {}).get("enabled", True):
            continue
        result.append(symbol)
    return result

def get_market_for_symbol(symbol: str) -> str:
    """Get market name for a symbol (crypto/forex/gold/us_stocks/india)."""
    return INSTRUMENTS.get(symbol, {}).get("market", "unknown")

def is_market_enabled(market: str) -> bool:
    """Check if a market is currently enabled."""
    return MARKETS.get(market, {}).get("enabled", False)

def get_symbols_for_market(market: str) -> list:
    """Get all enabled tradeable symbols for a specific market."""
    return [
        s for s, cfg in INSTRUMENTS.items()
        if cfg.get("market") == market
        and not cfg.get("backtest_only", False)
        and cfg.get("enabled", True)
    ]

def is_shares_based(symbol: str) -> bool:
    """True if position is sized in shares (stocks), False for units (forex/crypto)."""
    return INSTRUMENTS.get(symbol, {}).get("lot_size_type") == "shares"

def get_lot_size(symbol: str) -> int:
    """For F&O instruments, return the lot size. Returns 1 for all others."""
    return INSTRUMENTS.get(symbol, {}).get("lot_size", 1)

def get_risk_for_grade_and_streak(grade: str, streak_state: str) -> float:
    base      = RISK["base_risk_pct"]
    scale     = RISK["scale_table"].get(streak_state, 1.0)
    grade_mul = RISK["grade_multipliers"].get(grade, 0.25)
    cap       = RISK["scale_table"]["5+_wins"] * base
    return min(base * scale * grade_mul, cap)

def get_spike_threshold(symbol: str) -> float:
    """Event spike detection threshold for this instrument."""
    asset = get_asset_class(symbol)
    return EVENT_SPIKE_THRESHOLDS.get(asset, EVENT_SPIKE_THRESHOLDS["default"])


# ─────────────────────────────────────────────────────────────────────────────
# ASSET CLASS PARAMETERS — Shared between backtest and live trading
# Sweep-optimized on 90d hourly data (2026-05-04).
# ─────────────────────────────────────────────────────────────────────────────

ASSET_PARAMS = {
    # sl_mult: ATR multiplier for stop loss distance
    # rr:      reward-to-risk ratio (TP2 = sl_mult * rr * ATR)
    # min_grade: minimum allowed signal grade
    # min_conf:  minimum confidence % threshold
    #
    # v11.2 LIVE-TUNED: Wider SL + higher RR to prevent breakeven trap.
    # Old 1.0x ATR SL = market noise level → guaranteed stop hunt.
    # New 1.5-2.0x ATR SL = room to breathe + 2.0 RR = better payoff.
    #
    "gold":    {"sl_mult": 2.0, "rr": 2.0, "min_days": 60,  "label": "Gold",    "min_grade": "B+", "min_conf": 40},
    "silver":  {"sl_mult": 1.5, "rr": 2.0, "min_days": 60,  "label": "Silver",  "min_grade": "B+", "min_conf": 40},
    "forex":   {"sl_mult": 1.5, "rr": 2.0, "min_days": 90,  "label": "Forex",   "min_grade": "B+", "min_conf": 35},
    "btc":     {"sl_mult": 1.8, "rr": 2.0, "min_days": 30,  "label": "BTC",     "min_grade": "B+", "min_conf": 40},
    "crypto":  {"sl_mult": 1.8, "rr": 2.0, "min_days": 30,  "label": "Crypto",  "min_grade": "B+", "min_conf": 40},
    "india":   {"sl_mult": 1.5, "rr": 2.0, "min_days": 30,  "label": "India",   "min_grade": "B+", "min_conf": 40},
    "default": {"sl_mult": 1.5, "rr": 2.0, "min_days": 30,  "label": "Unknown", "min_grade": "B+", "min_conf": 40},
}

_FOREX_PAIRS_SET    = {"EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
                       "USDCHF=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X"}
_CRYPTO_TICKERS_SET = {"BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD",
                       "BTCUSDT", "ETHUSDT", "SOLUSDT"}
_GOLD_TICKERS_SET   = {"GC=F", "XAUUSD=X"}
_SILVER_TICKERS_SET = {"SI=F", "XAGUSD=X"}


def get_asset_params(ticker: str) -> dict:
    """Return backtest-proven ATR multipliers, grade gates, and confidence thresholds."""
    t = ticker.upper()
    if t in _GOLD_TICKERS_SET:     return ASSET_PARAMS["gold"]
    if t in _SILVER_TICKERS_SET:   return ASSET_PARAMS["silver"]
    if t in _FOREX_PAIRS_SET:      return ASSET_PARAMS["forex"]
    if t in ("BTC-USD", "BTC", "BTCUSDT"): return ASSET_PARAMS["btc"]
    if t in _CRYPTO_TICKERS_SET:   return ASSET_PARAMS["crypto"]
    if get_market_for_symbol(t) == "india": return ASSET_PARAMS["india"]
    return ASSET_PARAMS["default"]
