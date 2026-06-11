"""
CRAVE v10.3 — Backtesting Engine (Session 9)
=============================================
Builds on backtest_agent_v9_3.py with major upgrades:

  NEW: Walk-forward optimisation
       Not one backtest period — rolling 6-month train + 1-month test windows.
       Prevents overfitting by validating strategy on truly unseen data.
       Shows if the strategy degrades over time (a warning sign).

  NEW: Per-market performance breakdown
       Runs same SMC strategy on all enabled markets.
       Shows India vs US vs Crypto vs Forex win rates separately.
       Critical: a strategy that works for Gold may not work for NIFTY.

  NEW: Regime-segmented results
       Splits results into TRENDING_UP / TRENDING_DOWN / RANGING / VOLATILE.
       Shows win rate per regime — confirms regime_classifier value.
       Expected: 60%+ win rate trending, 35-40% ranging = system is working.

  NEW: Per-asset-class fee model
       Forex:        spread 0.5 pips + commission $5/lot
       Crypto:       0.05% taker fee per side (Binance)
       India stocks: 0.03% brokerage + 0.1% STT + 0.00325% NSE charge
       US stocks:    $0.005 per share (Alpaca pro) or $0 (free tier)
       Options:      ₹20/order flat (Zerodha) or ₹50 SEBI charge per lakh

  NEW: Enhanced Monte Carlo (1000 runs, sequential + bootstrapped)
       Bootstrapped: random sampling WITH replacement from trade set
       Sequential: random ORDERING of same trades (path dependency test)
       Both p5/p50/p95 final equity AND worst 5% drawdown shown

  RETAINED from v9_3:
       Warmup data, Unknown trend skip, asset-specific SL multipliers,
       grade breakdown, format_report, SYMBOL_ALIASES

USAGE:
  from backtesting.backtest_agent_v10 import BacktestAgentV10

  bt     = BacktestAgentV10()

  # Single symbol backtest (same as v9.3, with fees)
  report = bt.run_backtest("XAUUSD", days=90)
  print(bt.format_report(report))

  # Walk-forward test (most reliable)
  wf     = bt.run_walk_forward("BTCUSD", total_days=365, train_days=180, test_days=30)
  print(bt.format_walk_forward(wf))

  # Full multi-market comparison
  multi  = bt.run_multi_market(days=90, min_confidence=55)
  print(bt.format_multi_market(multi))
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("crave.backtest_v10")

# ── Inherit everything from v9.3 ──────────────────────────────────────────────
from backtesting.backtest_agent import (
    BacktestAgent as _BacktestV93,
    resolve_symbol, get_asset_params,
    ASSET_PARAMS, SYMBOL_ALIASES,
    FOREX_PAIRS, CRYPTO_TICKERS, GOLD_TICKERS,
    SILVER_TICKERS, INDEX_TICKERS,
    DEFAULT_RISK_PER_TRADE,
)


# ─────────────────────────────────────────────────────────────────────────────
# FEE MODEL
# ─────────────────────────────────────────────────────────────────────────────

class FeeModel:
    """
    Realistic trading cost model per asset class.
    Applied to every trade in the backtest.
    """

    def get_round_trip_cost_r(self, symbol: str,
                               entry_price: float,
                               sl_distance: float,
                               lot_size: float = 1.0) -> float:
        """
        Calculate round-trip trading cost as R-multiple equivalent.
        This is what gets deducted from every trade's R result.

        E.g., for XAUUSD:
          spread = 0.30 (pips) = $0.30 per unit
          At 1% risk with SL=2 ATR (~$20): cost = 0.30/20 = 0.015R per side
          Round trip = 0.03R deducted from every trade

        For BTCUSDT:
          fee = 0.05% taker each side = 0.1% round trip
          At entry $40,000 with SL $600: cost = $40 / $600 = 0.067R

        For RELIANCE (India stock):
          brokerage = 0.03% + STT = 0.1% + exchange = 0.003%
          Total ≈ 0.133% round trip
          At entry ₹2500 with SL ₹50: cost = 3.33 / 50 = 0.067R
        """
        if sl_distance <= 0 or entry_price <= 0:
            return 0.0

        ticker = resolve_symbol(symbol)
        asset  = get_asset_params(ticker)

        if ticker in FOREX_PAIRS:
            # 0.5 pip spread + $5 commission (per standard lot equivalent)
            pip_cost    = entry_price * 0.00005  # 0.5 pips
            commission  = 5 / (lot_size * 100000) if lot_size > 0 else 0
            total_cost  = (pip_cost + commission) * 2
            return total_cost / sl_distance

        elif ticker in GOLD_TICKERS | SILVER_TICKERS:
            # Alpaca: $0.01 spread + small commission
            spread_cost = 0.20  # ~$0.20/oz spread on gold
            return (spread_cost * 2) / sl_distance

        elif ticker in CRYPTO_TICKERS or "USDT" in ticker:
            # Binance: 0.05% taker fee each side
            fee_pct = 0.0005
            fee_abs = entry_price * fee_pct * 2
            return fee_abs / sl_distance

        elif ticker.endswith(".NS") or ticker.endswith(".BO"):
            # India stocks: 0.03% brokerage + 0.1% STT + 0.00325% NSE
            total_pct = 0.001325
            fee_abs   = entry_price * total_pct * 2
            return fee_abs / sl_distance

        elif ticker in {"AAPL","NVDA","TSLA","MSFT","SPY","QQQ"} or not ticker.endswith("=X"):
            # US stocks: $0.005/share (capped at 1% of trade value per leg)
            commission = min(0.005 * lot_size, entry_price * lot_size * 0.01)
            return (commission * 2) / sl_distance

        else:
            # Default: small cost
            return entry_price * 0.001 / sl_distance


fee_model = FeeModel()


# ─────────────────────────────────────────────────────────────────────────────
# REGIME TAGGER
# ─────────────────────────────────────────────────────────────────────────────

def _tag_candle_regime(df: pd.DataFrame, idx: int) -> str:
    """
    Tag a candle's market regime using available data up to idx.
    Uses rule-based detection (same as regime_classifier rules).
    """
    try:
        window = df.iloc[max(0, idx-100):idx+1]
        if len(window) < 20:
            return "UNKNOWN"

        close   = window['close']
        ema21   = close.ewm(span=21, adjust=False).mean().iloc[-1]
        ema50   = close.rolling(50).mean().iloc[-1]
        last    = close.iloc[-1]

        # ATR expansion check
        tr = pd.concat([
            window['high'] - window['low'],
            (window['high'] - window['close'].shift()).abs(),
            (window['low']  - window['close'].shift()).abs(),
        ], axis=1).max(axis=1)
        atr_now = tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]
        atr_avg = tr.tail(100).mean()

        if atr_avg > 0 and atr_now / atr_avg > 1.4:
            return "VOLATILE"

        above_ema21 = last > ema21
        ema21_above_50 = (ema21 > ema50) if not pd.isna(ema50) else True

        n = min(10, len(window))
        change = (close.iloc[-1] - close.iloc[-n]) / close.iloc[-n]

        if change > 0.005 and above_ema21:
            return "TRENDING_UP"
        if change < -0.005 and not above_ema21:
            return "TRENDING_DOWN"
        return "RANGING"

    except Exception:
        return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# V10 BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class BacktestAgentV10(_BacktestV93):
    """
    Extends BacktestAgentV9_3 with walk-forward, multi-market,
    regime segmentation, and realistic fee model.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # ENHANCED SINGLE BACKTEST (adds fees + regime tags)
    # ─────────────────────────────────────────────────────────────────────────

    def run_backtest(self, symbol: str, days: int = 60,
                     timeframe: str = "1h",
                     min_confidence: int = 40,
                     risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
                     include_fees: bool = True) -> dict:
        """
        Enhanced backtest with fees deduction and regime tagging.
        All v9.3 logic preserved. Fees and regime are additive layers.
        """
        result = super().run_backtest(
            symbol, days, timeframe, min_confidence, risk_per_trade
        )

        if "error" in result:
            return result

        trades = result.get("_trades", [])
        if not trades:
            return result

        ticker  = resolve_symbol(symbol)
        ap      = get_asset_params(ticker)

        # ── Apply fees to each trade ──────────────────────────────────────
        if include_fees:
            total_fee_r = 0.0
            for t in trades:
                fee_r       = fee_model.get_round_trip_cost_r(
                    ticker,
                    t.get("entry", t.get("entry_price", 0)),
                    t.get("sl_distance", abs(
                        t.get("entry", 0) - t.get("stop_loss", 0)
                    )),
                )
                t["fee_r"]   = round(fee_r, 4)
                t["net_r"]   = round(t["r_multiple"] - fee_r, 4)
                total_fee_r += fee_r

            # Recompute stats with fees
            net_r_vals = [t["net_r"] for t in trades]
            r_arr      = np.array(net_r_vals)
            wins       = int((r_arr > 0).sum())
            losses     = int((r_arr <= 0).sum())
            total      = len(r_arr)

            gross_p = float(r_arr[r_arr > 0].sum()) if (r_arr > 0).any() else 0
            gross_l = float(abs(r_arr[r_arr <= 0].sum())) if (r_arr <= 0).any() else 0

            result.update({
                "Total_Fee_R":      f"-{total_fee_r:.3f}R total",
                "Avg_Fee_R":        f"-{total_fee_r/total:.4f}R per trade",
                "Net_Win_Rate":     f"{wins/total*100:.1f}%",
                "Net_Expectancy_R": f"{r_arr.mean():.3f}R per trade",
                "Net_Profit_Factor":f"{gross_p/gross_l:.2f}" if gross_l > 0 else "∞",
                "Wins_After_Fees":  wins,
                "Losses_After_Fees": losses,
            })

        # ── Tag each trade with regime ────────────────────────────────────
        result["_regime_breakdown"] = self._compute_regime_breakdown(trades)

        return result

    def _compute_regime_breakdown(self, trades: list) -> dict:
        """
        Group trades by regime and compute stats per regime.
        Requires _regime field on each trade (tagged during backtest loop).
        """
        from collections import defaultdict
        by_regime: dict = defaultdict(list)

        for t in trades:
            regime = t.get("regime", "UNKNOWN")
            by_regime[regime].append(t.get("net_r", t.get("r_multiple", 0)))

        breakdown = {}
        for regime, r_vals in by_regime.items():
            arr  = np.array(r_vals)
            wins = int((arr > 0).sum())
            n    = len(arr)
            breakdown[regime] = {
                "trades":      n,
                "wins":        wins,
                "win_rate":    f"{wins/n*100:.1f}%" if n > 0 else "N/A",
                "expectancy":  f"{arr.mean():.3f}R" if n > 0 else "N/A",
                "total_r":     f"{arr.sum():.2f}R",
            }

        return breakdown

    # ─────────────────────────────────────────────────────────────────────────
    # WALK-FORWARD
    # ─────────────────────────────────────────────────────────────────────────

    def run_walk_forward(self, symbol: str,
                          total_days: int = 365,
                          train_days: int = 180,
                          test_days: int  = 30,
                          min_confidence: int = 55) -> dict:
        """
        Walk-forward validation.

        Creates rolling windows:
          Window 1: train days 1-180,  test days 181-210
          Window 2: train days 31-210, test days 211-240
          ...

        Each test window is "out-of-sample" (strategy never saw it during train).
        If in-sample win rate >> out-of-sample win rate → overfitting.
        Good system: in-sample and out-of-sample win rates within 5-10%.

        Returns per-window results + aggregate summary.
        """
        ticker = resolve_symbol(symbol)
        logger.info(
            f"[WalkForward] {ticker} | total={total_days}d "
            f"train={train_days}d test={test_days}d"
        )

        # Fetch all data once
        df_all = self.fetch_data_yfinance(
            ticker, total_days + 60, warmup_extra_days=60
        )
        if df_all is None or len(df_all) < 100:
            return {"error": f"Insufficient data for {ticker}"}

        windows       = []
        step          = test_days
        candles_total = len(df_all)
        candles_train = int(train_days * (candles_total / (total_days + 60)))
        candles_test  = int(test_days  * (candles_total / (total_days + 60)))
        candles_step  = int(step       * (candles_total / (total_days + 60)))

        # Calculate warmup candles needed for SMA200
        warmup_candles = min(250, candles_train // 4)

        window_num = 0
        start_idx  = warmup_candles

        while start_idx + candles_train + candles_test <= candles_total:
            train_end = start_idx + candles_train
            test_end  = train_end + candles_test

            df_train_full = df_all.iloc[start_idx - warmup_candles : train_end]
            df_test       = df_all.iloc[train_end : test_end]

            if len(df_test) < 10:
                break

            window_num += 1

            # Run on train window
            in_sample  = self._backtest_on_df(
                df_train_full, warmup_candles, ticker, min_confidence
            )
            # Run on test window (extend with warmup from train end)
            df_test_with_warmup = df_all.iloc[
                max(0, train_end - warmup_candles) : test_end
            ]
            out_sample = self._backtest_on_df(
                df_test_with_warmup,
                min(warmup_candles, train_end),
                ticker, min_confidence
            )

            start_date = df_train_full.iloc[warmup_candles]["time"].strftime("%Y-%m-%d") \
                if not df_train_full.empty else "?"
            test_date  = df_test.iloc[0]["time"].strftime("%Y-%m-%d") \
                if not df_test.empty else "?"
            test_end_d = df_test.iloc[-1]["time"].strftime("%Y-%m-%d") \
                if not df_test.empty else "?"

            windows.append({
                "window":           window_num,
                "train_start":      start_date,
                "test_period":      f"{test_date} → {test_end_d}",
                "in_sample":        in_sample,
                "out_sample":       out_sample,
                "degradation":      self._calc_degradation(in_sample, out_sample),
            })

            start_idx += candles_step

        if not windows:
            return {"error": "No complete walk-forward windows found"}

        # Aggregate out-of-sample stats
        oos_win_rates = [
            w["out_sample"]["win_rate_float"]
            for w in windows
            if "win_rate_float" in w["out_sample"]
        ]
        oos_expectancy = [
            w["out_sample"]["expectancy_float"]
            for w in windows
            if "expectancy_float" in w["out_sample"]
        ]

        oos_avg_wr  = np.mean(oos_win_rates)  if oos_win_rates  else 0
        oos_avg_exp = np.mean(oos_expectancy) if oos_expectancy else 0
        oos_std_exp = np.std(oos_expectancy)  if len(oos_expectancy) > 1 else 0

        verdict = self._wf_verdict(oos_avg_wr, oos_avg_exp, oos_std_exp)

        return {
            "symbol":       ticker,
            "total_days":   total_days,
            "train_days":   train_days,
            "test_days":    test_days,
            "windows":      windows,
            "oos_avg_win_rate":  f"{oos_avg_wr:.1f}%",
            "oos_avg_expectancy": f"{oos_avg_exp:.3f}R",
            "oos_consistency":   f"σ={oos_std_exp:.3f}R",
            "verdict":      verdict,
        }

    def _backtest_on_df(self, df: pd.DataFrame, signal_start: int,
                         ticker: str, min_confidence: int) -> dict:
        """Run backtest on a specific DataFrame slice. Returns stats dict."""
        if len(df) < signal_start + 20:
            return {"error": "Insufficient candles", "trades": 0}

        ap      = get_asset_params(ticker)
        sl_mult = ap["sl_mult"]
        rr      = ap["rr"]
        trades  = []

        from core.strategy_agent import StrategyAgent
        strategy = StrategyAgent()

        for i in range(signal_start, len(df) - 5):
            window = df.iloc[max(0, i-200) : i+1].copy().reset_index(drop=True)

            try:
                ctx = strategy.analyze_market_context(ticker, window)
            except Exception:
                continue

            if "error" in ctx:
                continue
            if ctx.get("Macro_Trend") == "Unknown":
                continue

            conf  = ctx.get("Confidence_Pct", 0)
            grade = ctx.get("Structure_Score", "C")
            if conf < min_confidence or grade == "C":
                continue

            direction = "buy" if ctx["Macro_Trend"] == "Bullish" else "sell"
            entry     = df['close'].iloc[i]

            # Calculate ATR for SL
            atr_series = pd.concat([
                df['high'].iloc[max(0,i-14):i] - df['low'].iloc[max(0,i-14):i],
            ], axis=0)
            atr = atr_series.mean() if len(atr_series) > 0 else entry * 0.005

            sl = entry - sl_mult * atr if direction == "buy" else entry + sl_mult * atr
            tp = entry + rr * sl_mult * atr if direction == "buy" else entry - rr * sl_mult * atr

            # Walk forward in time to find outcome
            r_mult = -1.0
            regime = _tag_candle_regime(df, i)
            for j in range(i+1, min(i+50, len(df))):
                hi = df['high'].iloc[j]
                lo = df['low'].iloc[j]
                if direction == "buy":
                    if lo <= sl:
                        r_mult = -1.0
                        break
                    if hi >= tp:
                        r_mult = rr
                        break
                else:
                    if hi >= sl:
                        r_mult = -1.0
                        break
                    if lo <= tp:
                        r_mult = rr
                        break

            fee_r = fee_model.get_round_trip_cost_r(ticker, entry, abs(entry - sl))
            trades.append({
                "r_multiple": r_mult,
                "net_r":      r_mult - fee_r,
                "grade":      grade,
                "regime":     regime,
            })

        if not trades:
            return {"trades": 0, "win_rate_float": 0, "expectancy_float": 0}

        arr  = np.array([t["net_r"] for t in trades])
        wins = int((arr > 0).sum())
        n    = len(arr)

        return {
            "trades":          n,
            "wins":            wins,
            "win_rate_float":  wins / n * 100 if n > 0 else 0,
            "win_rate":        f"{wins/n*100:.1f}%" if n > 0 else "N/A",
            "expectancy_float": float(arr.mean()) if n > 0 else 0,
            "expectancy":       f"{arr.mean():.3f}R" if n > 0 else "N/A",
        }

    def _calc_degradation(self, in_sample: dict, out_sample: dict) -> str:
        """Quantify how much strategy degrades out-of-sample."""
        is_wr  = in_sample.get("win_rate_float",  0)
        oos_wr = out_sample.get("win_rate_float", 0)
        if is_wr == 0:
            return "N/A"
        deg = is_wr - oos_wr
        if deg < 3:
            return f"✅ Minimal ({deg:+.1f}pp)"
        if deg < 8:
            return f"🟡 Moderate ({deg:+.1f}pp)"
        return f"🔴 Significant ({deg:+.1f}pp) — possible overfit"

    def _wf_verdict(self, avg_wr: float, avg_exp: float,
                     std_exp: float) -> str:
        if avg_wr >= 52 and avg_exp >= 0.08 and std_exp < 0.15:
            return "✅ STRATEGY ROBUST — consistent OOS performance"
        if avg_wr >= 48 and avg_exp >= 0.03:
            return "🟡 MARGINAL — strategy works but edge is thin"
        if avg_wr < 45:
            return "🔴 STRATEGY FAILS OOS — do not deploy live"
        return "⚠️  INCONSISTENT — needs more data"

    # ─────────────────────────────────────────────────────────────────────────
    # MULTI-MARKET COMPARISON
    # ─────────────────────────────────────────────────────────────────────────

    def run_multi_market(self, days: int = 90,
                          min_confidence: int = 55) -> dict:
        """
        Run backtest on all enabled markets and compare results.
        Shows which markets the SMC strategy works best on.
        """
        from config.config import get_tradeable_symbols, get_market_for_symbol

        # Representative symbols per market
        test_symbols = {
            "gold":      ["XAUUSD=X"],
            "crypto":    ["BTC-USD", "ETH-USD"],
            "forex":     ["EURUSD=X", "GBPUSD=X"],
            "us_stocks": ["AAPL", "NVDA", "SPY"],
            "india":     ["RELIANCE.NS", "^NSEI"],
        }

        results      = {}
        market_stats = {}

        for market, symbols in test_symbols.items():
            market_trades = []
            for sym in symbols:
                try:
                    r = self.run_backtest(
                        sym, days=days,
                        min_confidence=min_confidence,
                        include_fees=True
                    )
                    if "error" not in r and r.get("Signals", 0) > 5:
                        results[sym] = r
                        market_trades.extend(r.get("_trades", []))
                except Exception as e:
                    logger.debug(f"[MultiMarket] {sym} failed: {e}")
                    continue

            if not market_trades:
                continue

            arr  = np.array([t.get("net_r", t["r_multiple"]) for t in market_trades])
            wins = int((arr > 0).sum())
            n    = len(arr)

            gross_p = float(arr[arr > 0].sum()) if (arr > 0).any() else 0
            gross_l = float(abs(arr[arr <= 0].sum())) if (arr <= 0).any() else 0

            market_stats[market] = {
                "trades":       n,
                "win_rate":     f"{wins/n*100:.1f}%" if n > 0 else "N/A",
                "expectancy":   f"{arr.mean():.3f}R" if n > 0 else "N/A",
                "profit_factor": f"{gross_p/gross_l:.2f}" if gross_l > 0 else "∞",
                "recommended":  (wins/n*100 >= 50 and arr.mean() >= 0.05) if n > 0 else False,
            }

        return {
            "period":       f"{days} days",
            "min_confidence": f"{min_confidence}%",
            "per_symbol":   results,
            "by_market":    market_stats,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # ENHANCED MONTE CARLO (1000 runs, both methods)
    # ─────────────────────────────────────────────────────────────────────────

    def monte_carlo(self, report: dict, runs: int = 1000) -> dict:
        """
        Enhanced Monte Carlo with two methods.

        Method 1 (Bootstrap): random sampling WITH replacement.
          Tests: "If I got a different random subset of these trades,
                  what would my equity curve look like?"
          Accounts for luck/unluck in trade selection.

        Method 2 (Sequence): random ORDERING of same trade set.
          Tests: "Does the ORDER of wins/losses matter? (path dependency)"
          If max_dd varies wildly between sequences → sequence risk is high.
          A strategy with stable DD across sequences = more robust.
        """
        trades = report.get("_trades", [])
        if len(trades) < 10:
            return {"error": "Need ≥10 trades for Monte Carlo."}

        risk     = report.get("_risk_per_trade", DEFAULT_RISK_PER_TRADE)
        r_arr    = np.array([t.get("net_r", t["r_multiple"]) for t in trades])
        start_eq = 10_000.0

        def simulate(sequence: np.ndarray) -> tuple:
            eq      = start_eq
            peak    = start_eq
            max_dd  = 0.0
            for r in sequence:
                eq      = eq * (1 + r * risk)
                peak    = max(peak, eq)
                dd      = (peak - eq) / peak * 100
                max_dd  = max(max_dd, dd)
            return eq, max_dd

        # Method 1: Bootstrap
        boot_eq  = np.zeros(runs)
        boot_dd  = np.zeros(runs)
        for k in range(runs):
            seq          = np.random.choice(r_arr, size=len(r_arr), replace=True)
            boot_eq[k], boot_dd[k] = simulate(seq)

        # Method 2: Sequence shuffle
        seq_eq  = np.zeros(runs)
        seq_dd  = np.zeros(runs)
        for k in range(runs):
            seq           = np.random.permutation(r_arr)
            seq_eq[k], seq_dd[k] = simulate(seq)

        return {
            "runs":          runs,
            "start_equity":  f"${start_eq:,.0f}",
            # Bootstrap results
            "bootstrap": {
                "p5_end":         f"${np.percentile(boot_eq, 5):,.0f}",
                "median_end":     f"${np.median(boot_eq):,.0f}",
                "p95_end":        f"${np.percentile(boot_eq, 95):,.0f}",
                "pct_profitable": f"{(boot_eq > start_eq).mean()*100:.1f}%",
                "median_max_dd":  f"-{np.median(boot_dd):.2f}%",
                "worst_5pct_dd":  f"-{np.percentile(boot_dd, 95):.2f}%",
            },
            # Sequence results
            "sequence": {
                "p5_end":         f"${np.percentile(seq_eq, 5):,.0f}",
                "median_end":     f"${np.median(seq_eq):,.0f}",
                "p95_end":        f"${np.percentile(seq_eq, 95):,.0f}",
                "pct_profitable": f"{(seq_eq > start_eq).mean()*100:.1f}%",
                "median_max_dd":  f"-{np.median(seq_dd):.2f}%",
                "worst_5pct_dd":  f"-{np.percentile(seq_dd, 95):.2f}%",
                "dd_std":         f"±{np.std(seq_dd):.2f}% (sequence risk)",
            },
        }

    # ─────────────────────────────────────────────────────────────────────────
    # FORMATTERS
    # ─────────────────────────────────────────────────────────────────────────

    def format_report(self, report: dict, include_monte_carlo: bool = True) -> str:
        """Enhanced format_report adding fees, regime breakdown."""
        base = super().format_report(report, include_monte_carlo=False)

        lines = [base]

        # Fees section
        if report.get("Total_Fee_R"):
            lines += [
                "──────────────────────────────────",
                "  FEES (realistic costs)",
                f"  Total cost  : {report['Total_Fee_R']}",
                f"  Per trade   : {report['Avg_Fee_R']}",
                f"  Net win rate: {report.get('Net_Win_Rate', 'N/A')}",
                f"  Net expect  : {report.get('Net_Expectancy_R', 'N/A')}",
            ]

        # Regime breakdown
        rb = report.get("_regime_breakdown", {})
        if rb:
            lines += [
                "──────────────────────────────────",
                "  REGIME BREAKDOWN",
            ]
            for regime, stats in sorted(rb.items()):
                lines.append(
                    f"  {regime:14s}: {stats['trades']:3d} trades | "
                    f"WR {stats['win_rate']:6s} | "
                    f"E {stats['expectancy']:>8s}"
                )

        # Enhanced Monte Carlo
        if include_monte_carlo:
            mc = self.monte_carlo(report)
            if "error" not in mc:
                b = mc["bootstrap"]
                s = mc["sequence"]
                lines += [
                    "──────────────────────────────────",
                    f"  MONTE CARLO ({mc['runs']} runs each method)",
                    "  Bootstrap (luck test):",
                    f"    Worst 5%: {b['p5_end']}  Median: {b['median_end']}  Best 95%: {b['p95_end']}",
                    f"    % Profitable: {b['pct_profitable']}  Median MaxDD: {b['median_max_dd']}",
                    "  Sequence (path test):",
                    f"    Worst 5%: {s['p5_end']}  Median: {s['median_end']}  Best 95%: {s['p95_end']}",
                    f"    DD σ: {s['dd_std']}",
                ]

        lines.append("══════════════════════════════════")
        return "\n".join(lines)

    def format_walk_forward(self, wf: dict) -> str:
        if "error" in wf:
            return f"❌ {wf['error']}"

        lines = [
            "📊 WALK-FORWARD VALIDATION",
            "══════════════════════════════════",
            f"Symbol  : {wf['symbol']}",
            f"Total   : {wf['total_days']}d | "
            f"Train: {wf['train_days']}d | Test: {wf['test_days']}d",
            f"Windows : {len(wf['windows'])}",
            "──────────────────────────────────",
            f"{'Win':>4s}  {'Test Period':<22s}  "
            f"{'IS WR':>7s}  {'OOS WR':>7s}  {'Degrad.':>12s}",
        ]

        for w in wf["windows"]:
            is_r  = w["in_sample"]
            oos_r = w["out_sample"]
            lines.append(
                f"{w['window']:>4d}  {w['test_period']:<22s}  "
                f"{is_r.get('win_rate','?'):>7s}  "
                f"{oos_r.get('win_rate','?'):>7s}  "
                f"{w['degradation']}"
            )

        lines += [
            "──────────────────────────────────",
            f"OOS Avg WR  : {wf['oos_avg_win_rate']}",
            f"OOS Avg Exp : {wf['oos_avg_expectancy']}",
            f"Consistency : {wf['oos_consistency']}",
            "══════════════════════════════════",
            f"VERDICT: {wf['verdict']}",
        ]
        return "\n".join(lines)

    def format_multi_market(self, multi: dict) -> str:
        lines = [
            "📊 MULTI-MARKET COMPARISON",
            "══════════════════════════════════",
            f"Period: {multi['period']}  "
            f"Min Conf: {multi['min_confidence']}",
            "──────────────────────────────────",
            f"{'Market':<14s}  {'Trades':>6s}  "
            f"{'WR':>6s}  {'Exp':>7s}  "
            f"{'PF':>5s}  {'Use?':>5s}",
        ]

        for market, stats in multi.get("by_market", {}).items():
            rec = "✅" if stats.get("recommended") else "❌"
            lines.append(
                f"{market:<14s}  {stats['trades']:>6d}  "
                f"{stats['win_rate']:>6s}  "
                f"{stats['expectancy']:>7s}  "
                f"{stats['profit_factor']:>5s}  {rec:>5s}"
            )

        lines.append("══════════════════════════════════")
        return "\n".join(lines)
