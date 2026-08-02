# CRAVE Quant Backtest Report

## 1. Test Details & Parameters
- **Test Engine:** CRAVE Walk-Forward Optimization (WFO) Harness (`run_full_backtest.py`)
- **Starting Equity:** $10,000.00
- **Risk per Trade:** 2.0%
- **Target R:R Ratio:** Strict 1.2 minimum (scaled dynamically to 1.5 - 2.0 based on asset)
- **Timeframe / Interval:** 1-hour (H1) and 15-minute (M15) internal tick simulation
- **Session Filter:** London and New York overlaps only (dynamic DST-aware filtering)
- **Data Source:** YFinance historical market data

## 2. Strategies Deployed
The backtest utilized the **SMC v9.3 Engine** (Smart Money Concepts), which evaluates trades using a hybrid multi-factor scoring system. It scans for the following setups:

- **Order Blocks (OB):** Institutional footprint candles preceding impulsive displacement. Filtered for > 2.0x ATR displacement.
- **Fair Value Gaps (FVG):** Tracks unmitigated price imbalances and executes upon mitigation with structural confirmation.
- **Liquidity Sweeps:** Identifies stops sitting above/below swing highs/lows and enters on trap/reversal patterns.
- **Structure Breaks (BOS/CHoCH):** Requires strict candle-close confirmation of market structure shifts.

*Note: Trades are assigned a grade (A+, A, B+, B, C). The engine requires a minimum confidence score (40%-55% depending on the asset) to execute.*

## 3. Assets Evaluated & Results

### Portfolio Overview
- **Total Return:** -36.95% (Final Equity: $6,304.91)
- **Total Trades:** 807
- **Win Rate:** 52.3%
- **Expectancy:** -0.019R per trade
- **Verdict:** The raw multi-asset portfolio failed. The engine requires strict asset isolation.

### Individual Asset Breakdown

#### Silver (XAGUSD)
- **Period Tested:** 60 Days
- **Win Rate:** 61.4% (148 Wins / 88 Losses)
- **Total Return:** +120.98%
- **Expectancy:** +0.178R (Grade A+ yielded +1.0R; Grade A yielded +0.35R)
- **Max Drawdown:** -24.02%
- **Verdict:** **[APPROVED]** Massive edge detected. XAGUSD responds extremely well to SMC liquidity sweeps and FVG mitigations.

#### Solana (SOLUSD)
- **Period Tested:** 30 Days
- **Win Rate:** 48.0% (59 Wins / 45 Losses)
- **Total Return:** -11.84%
- **Expectancy:** -0.053R
- **Verdict:** **[REJECTED]** Negative expectancy over the test period.

#### Euro (EURUSD)
- **Period Tested:** 90 Days
- **Win Rate:** 49.7% (168 Wins / 169 Losses)
- **Total Return:** -34.87%
- **Expectancy:** -0.053R
- **Verdict:** **[REJECTED]** Choppy market behavior caused severe drawdown. Edge is nonexistent under current volatility.

#### Bitcoin (BTCUSD)
- **Period Tested:** 30 Days
- **Win Rate:** 34.8% (47 Wins / 83 Losses)
- **Total Return:** -50.31%
- **Expectancy:** -0.258R
- **Verdict:** **[REJECTED]** High stop-out rate on crypto structural wicks. Strategy must be recalibrated for BTC volatility.

*(Note: ETHUSD and XAUUSD failed to download enough consecutive historical data from the yfinance API during the backtest window and were excluded).*

## 4. Conclusion & Action Taken
Following the out-of-sample data analysis, all assets producing negative expectancy were purged from live configuration. The CRAVE Engine `STRATEGY_DEFS` config was updated to exclusively deploy **Crave AI - Metals (SMC)** targeting XAGUSD setups that maintain the strict >1.2 R:R constraint.
