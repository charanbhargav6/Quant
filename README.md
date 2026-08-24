# Trading Engine v11.0

> **Smart Money Concept (SMC) Trading System** — Multi-market, multi-broker autonomous trading bot with Indian market integration.

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Trading Engine v11.0                     │
├─────────────────────────────────────────────────────────────┤
│  run_bot.py → Entry Point (Paper/Live/Backtest/Status)      │
├─────────┬──────────┬──────────┬──────────┬─────────────────┤
│  core/  │ engines/ │ brokers/ │   ml/    │  interfaces/    │
│ Trading │ Strategy │ Binance  │ Regime   │  Telegram       │
│  Loop   │  Bias    │ Alpaca   │ Features │  WebSocket      │
│ Paper   │  TP/SL   │ Zerodha  │ Backtest │  Supabase       │
│ Risk    │  Scanner │          │          │                 │
├─────────┴──────────┴──────────┴──────────┴─────────────────┤
│  data/ (Market Data Router → yfinance / Kite / Binance)     │
│  options/ (Greeks Monitor → IV Rank → Options Engine)       │
│  risk/ (Portfolio Heat → Correlation → Circuit Breakers)    │
├─────────────────────────────────────────────────────────────┤
│  Supabase (Always-on dashboard backend)                     │
│  SQLite (Local trade history, OHLCV cache)                  │
└─────────────────────────────────────────────────────────────┘
```

## 🇮🇳 Indian Market Strategy (Maximum Returns / Minimum Loss)

| Strategy | When | Why |
|---|---|---|
| **NIFTY/BANKNIFTY Futures** | Open Drive (9:30-11 IST) + Close Drive (2-3:30 IST) | Highest liquidity, cleanest SMC patterns |
| **FII/DII Flow Following** | Pre-market analysis (6:30 UTC daily) | Trade WITH institutional money, not against it |
| **India VIX Regime** | VIX < 13: Directional trades, VIX > 20: Sell options, VIX > 28: Halt | Volatility regime is the #1 loss reducer |
| **Weekly Expiry** | Wed (BANKNIFTY) + Thu (NIFTY) | Option selling on expiry has theta decay edge |
| **PCR Tracking** | Continuous | Put-Call Ratio confirms market sentiment |
| **Circuit Breakers** | Auto-enforced at 5/10/20% | NSE-mandated, prevents catastrophic loss |
| **Delivery Volume** | Swing trades only | >50% delivery = institutional accumulation |
| **Pre-Open Auction** | 9:15-9:30 IST | FII/DII order flow visible before market open |

### 14 Indian Stocks Enabled
RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, SBIN, BHARTIARTL, ITC, BAJFINANCE, TATAMOTORS, MARUTI, WIPRO, AXISBANK, KOTAKBANK + NIFTY/BANKNIFTY Futures

## 🚀 Quick Start

```bash
# 1. Clone/navigate to project
cd D:\Desktop\engine

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your API keys only when needed

# 4. Run paper mode (safe default)
python run_bot.py --paper

# 5. Other modes
python run_bot.py --status       # Check state
python run_bot.py --backtest     # Backtest a symbol
python run_bot.py --readiness    # Check paper/live readiness
python run_bot.py --setup        # Setup wizard
python run_bot.py --live         # Live trading; requires explicit credentials and gates

# Opt-in paper-only Gold breakout research candidate
# ENABLE_VOLATILITY_BREAKOUT=true in .env, then keep TRADING_MODE=paper
```

## 📊 Supported Markets

| Market | Broker | Status | Instruments |
|--------|--------|--------|------------|
| 🪙 Crypto | Binance | ✅ Enabled | BTC, ETH, SOL |
| 💱 Forex | MT5 | ✅ Enabled | EUR/USD, GBP/USD, USD/JPY, AUD/USD |
| 🥇 Gold/Silver | MT5 | ✅ Enabled | XAU/USD, XAG/USD; optional paper-only XAUUSD volatility breakout |
| 🇮🇳 India | Zerodha | ✅ Enabled | 14 stocks + NIFTY/BANKNIFTY F&O |
| 🇺🇸 US Stocks | Alpaca | ⬜ Disabled | AAPL, NVDA, TSLA, MSFT, SPY, QQQ |
| 📈 Options | Zerodha | ✅ Enabled | NIFTY/BANKNIFTY options |

## 🤖 Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Streak state, circuit breaker, risk level |
| `/positions` | All open positions with entry/SL/TP |
| `/bias` | Today's daily bias per instrument |
| `/portfolio` | Full portfolio heat by market |
| `/india` | Indian market status + FII/DII + PCR |
| `/vix` | India VIX level and regime |
| `/options` | Options positions and greeks |
| `/iv NIFTY` | IV Rank for a symbol |
| `/paper` | Paper trading status |
| `/readiness` | Full readiness gate check |
| `/ml` | ML model status |
| `/ws` | WebSocket connection status |
| `/help` | Show all commands |

## 🔧 Project Structure

```
engine/
├── run_bot.py          # Main entry point
├── config/             # Central configuration
├── core/               # Trading loop, position tracker, paper trading, risk
├── engines/            # Bias, TP/SL, hedge, scanner, compounding
├── brokers/            # Binance, Alpaca, Zerodha integrations
├── data/               # Market data router, NSE bhavcopy
├── intelligence/       # Jarvis LLM, order flow analysis
├── ml/                 # Regime classifier, feature engineering
├── options/            # Options engine, Greeks monitor
├── risk/               # Portfolio risk engine
├── interfaces/         # Telegram, WebSocket
├── dashboard/          # Supabase always-on pusher
├── backtesting/        # Backtest agents, optimization
├── infra/              # AWS, node orchestrator, state sync
├── security/           # API sentinel, chaos monkey
├── content/            # Trade recap generator
├── Database/           # SQLite storage
├── State/              # JSON state files
└── Logs/               # Application logs
```

## 📡 Supabase Backend (Always-On)

The Supabase dashboard pusher runs continuously, syncing:
- Live positions and P&L
- Paper trading equity curve
- Trade journal entries
- Portfolio heat map
- Node health status

Configure `SUPABASE_URL`, `SUPABASE_KEY` in `.env`.

## ⚠️ Risk Management

- **Paper trading first** — 30+ trades with 50%+ win rate required before live
- **Prop firm compliance** — FundingPips/FTMO rules enforced
- **Circuit breakers** — Auto-halt after 2 consecutive losing days
- **Max daily loss** — 4% hard limit
- **Max drawdown** — 10% account-level protection
- **Correlation limits** — Max 2% exposure to correlated pairs
- **India VIX filter** — Halts new entries when VIX > 28

## 📋 License

Proprietary — All rights reserved.
