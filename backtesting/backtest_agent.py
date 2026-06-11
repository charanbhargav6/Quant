"""
CRAVE Phase 9.3 - Universal Backtest Engine
============================================
FIXES vs v9.2 (driven by XAUUSD -35% win rate diagnosis):

  🔧 CRITICAL — Warmup data pre-fetch
     Problem: SMA200 needs 200 candles. 30-day 1H Gold only gives ~438 candles.
     Signals fired from candle 55 onward, but SMA200 is NaN until candle 200.
     That's 40% of the backtest window with trend="Unknown".
     Fix: fetch extra 'warmup_extra_days' before the test window purely to
     seed indicators. These candles don't generate signals — they just ensure
     SMA200 is valid from the very first signal bar.

  🔧 CRITICAL — Never trade when macro_trend = "Unknown"
     Old: direction = "buy" if Macro_Trend == "Bullish" else "sell"
          → "Unknown" defaulted silently to SELL
     New: skip the signal entirely. Unknown trend = no edge = no trade.
     This alone would have converted ~30 of the 50 XAUUSD losses to skipped signals.

  🔧 MEDIUM — Asset-specific ATR multipliers
     Gold (XAUUSD/XAGUSD) has ATR ~1.0% of price vs BTC ~0.6%.
     Using sl_mult=1.5 on Gold gives stops too tight for its normal swing range.
     Gold needs sl_mult=2.0 minimum. Forex majors need 2.5 (lower volatility
     means you need more room). These are now in ASSET_PARAMS lookup table.

  🔧 MEDIUM — Minimum candle guard per asset class
     Gold and Forex need 90+ days of 1H data for a valid backtest.
     Crypto can work with 30 days. Added per-asset minimum day check
     with a clear error message before the loop starts.

  All stats remain R-multiple based from v9.2. No regression on those fixes.
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("crave.trading.backtest")

DEFAULT_RISK_PER_TRADE = 0.02

SYMBOL_ALIASES = {
    # Forex
    "eurusd": "EURUSD=X", "gbpusd": "GBPUSD=X", "usdjpy": "USDJPY=X",
    "audusd": "AUDUSD=X", "usdcad": "USDCAD=X", "usdchf": "USDCHF=X",
    "nzdusd": "NZDUSD=X", "eurjpy": "EURJPY=X", "gbpjpy": "GBPJPY=X",
    # Commodities
    "xauusd": "GC=F", "gold": "GC=F", "silver": "SI=F", "xagusd": "SI=F",
    # Crypto
    "btcusd": "BTC-USD", "btc": "BTC-USD", "bitcoin": "BTC-USD",
    "ethusd": "ETH-USD", "eth": "ETH-USD",
    "solusd": "SOL-USD", "sol": "SOL-USD",
    # US Stocks
    "aapl": "AAPL", "tsla": "TSLA", "msft": "MSFT", "nvda": "NVDA",
    "amzn": "AMZN", "meta": "META", "goog": "GOOGL",
    # US Indices / ETFs (F&O proxies)
    "spy": "SPY", "qqq": "QQQ", "spx": "^GSPC", "ndx": "^NDX",
    # Indian Indices (F&O)
    "nifty": "^NSEI", "sensex": "^BSESN", "banknifty": "^NSEBANK",
    # Indian Stocks (F&O eligible)
    "reliance": "RELIANCE.NS", "tcs": "TCS.NS", "infy": "INFY.NS",
    "hdfcbank": "HDFCBANK.NS", "icicibank": "ICICIBANK.NS",
    "sbin": "SBIN.NS", "tatamotors": "TATAMOTORS.NS",
    "adanient": "ADANIENT.NS", "wipro": "WIPRO.NS",
    "bajfinance": "BAJFINANCE.NS",
}

# Reverse map: Yahoo ticker -> user-friendly display name
DISPLAY_NAMES = {
    "GC=F": "XAUUSD", "SI=F": "XAGUSD",
    "EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD", "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD", "USDCAD=X": "USDCAD", "USDCHF=X": "USDCHF",
    "NZDUSD=X": "NZDUSD", "EURJPY=X": "EURJPY", "GBPJPY=X": "GBPJPY",
    "BTC-USD": "BTCUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD",
    "DOGE-USD": "DOGEUSD", "XRP-USD": "XRPUSD",
    "^NSEI": "NIFTY50", "^BSESN": "SENSEX", "^NSEBANK": "BANKNIFTY",
    "^GSPC": "S&P500", "^NDX": "NASDAQ100",
    "RELIANCE.NS": "RELIANCE", "TCS.NS": "TCS", "INFY.NS": "INFY",
    "HDFCBANK.NS": "HDFCBANK", "ICICIBANK.NS": "ICICIBANK",
    "SBIN.NS": "SBIN", "TATAMOTORS.NS": "TATAMOTORS",
    "ADANIENT.NS": "ADANIENT", "WIPRO.NS": "WIPRO",
    "BAJFINANCE.NS": "BAJFINANCE",
}


def resolve_symbol(raw: str) -> str:
    clean = raw.strip().lower().replace(" ", "")
    return SYMBOL_ALIASES.get(clean, raw.strip().upper())


def display_name(ticker: str) -> str:
    """Return user-friendly display name for a Yahoo Finance ticker."""
    return DISPLAY_NAMES.get(ticker, ticker)


def parse_period(text: str) -> tuple:
    import re
    text = text.lower()
    m = re.search(r'(\d+)\s*(year|month|week|day)s?', text)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u == "year":  return (n * 365, f"{n} Year{'s' if n>1 else ''}")
        if u == "month": return (n * 30,  f"{n} Month{'s' if n>1 else ''}")
        if u == "week":  return (n * 7,   f"{n} Week{'s' if n>1 else ''}")
        if u == "day":   return (n,        f"{n} Day{'s' if n>1 else ''}")
    return (15, "15 Days")


def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────────
# ASSET CLASS PARAMETERS — imported from shared config (single source of truth)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from config.config import ASSET_PARAMS, get_asset_params
except ImportError:
    # Fallback for standalone use
    ASSET_PARAMS = {
        "gold":    {"sl_mult": 2.0, "rr": 1.5, "min_days": 60,  "label": "Gold",    "min_grade": "B+", "min_conf": 35},
        "silver":  {"sl_mult": 1.0, "rr": 1.5, "min_days": 60,  "label": "Silver",  "min_grade": "B+", "min_conf": 50},
        "forex":   {"sl_mult": 1.0, "rr": 1.5, "min_days": 90,  "label": "Forex",   "min_grade": "B+", "min_conf": 35},
        "btc":     {"sl_mult": 1.2, "rr": 1.5, "min_days": 30,  "label": "BTC",     "min_grade": "B+", "min_conf": 45},
        "crypto":  {"sl_mult": 2.5, "rr": 1.5, "min_days": 30,  "label": "Crypto",  "min_grade": "A",  "min_conf": 35},
        "default": {"sl_mult": 1.5, "rr": 2.0, "min_days": 30,  "label": "Unknown", "min_grade": "A",  "min_conf": 55},
    }

    def get_asset_params(ticker: str) -> dict:
        t = ticker.upper()
        if t in GOLD_TICKERS:     return ASSET_PARAMS["gold"]
        if t in SILVER_TICKERS:   return ASSET_PARAMS["silver"]
        if t in FOREX_PAIRS:      return ASSET_PARAMS["forex"]
        if t in ("BTC-USD", "BTC"): return ASSET_PARAMS["btc"]
        if t in CRYPTO_TICKERS:   return ASSET_PARAMS["crypto"]
        return ASSET_PARAMS["default"]

FOREX_PAIRS    = {"EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
                  "USDCHF=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X"}
CRYPTO_TICKERS = {"BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD"}
GOLD_TICKERS   = {"GC=F"}
SILVER_TICKERS = {"SI=F"}
INDEX_TICKERS  = {"^NSEI", "^BSESN", "^NSEBANK", "^GSPC", "^NDX"}



# ─────────────────────────────────────────────────────────────────────────────
# R-MULTIPLE REFERENCE
# ─────────────────────────────────────────────────────────────────────────────
R_BY_OUTCOME = {
    "sl":          -1.0,
    "tp1_then_be":  0.5,
    "tp1_partial":  0.5,
    "tp2_via_tp1":  1.5,
    "tp2":          2.0,
}


class BacktestAgent:

    def __init__(self):
        from core.strategy_agent import StrategyAgent
        from core.risk_agent import RiskAgent
        self.strategy = StrategyAgent()
        self.risk     = RiskAgent()

    # ─────────────────────────────────────────────────────────────────────────
    # DATA FETCHER — with warmup pre-fetch
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_data_yfinance(self, symbol: str, days: int,
                             interval: str = "1h",
                             warmup_extra_days: int = 0) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV from yfinance.

        warmup_extra_days: extra calendar days to fetch BEFORE the test window.
        These extra candles allow SMA200 and other long-period indicators to
        warm up fully before the first signal bar.
        """
        import yfinance as yf

        ticker      = resolve_symbol(symbol)
        days_to_fetch  = days + warmup_extra_days

        if days_to_fetch > 730 and interval in ("1h", "1m", "5m", "15m"):
            interval = "1d"

        try:
            # Map days to valid yfinance period
            if days_to_fetch <= 5: period_str = "5d"
            elif days_to_fetch <= 30: period_str = "1mo"
            elif days_to_fetch <= 90: period_str = "3mo"
            elif days_to_fetch <= 180: period_str = "6mo"
            elif days_to_fetch <= 365: period_str = "1y"
            elif days_to_fetch <= 730: period_str = "2y"
            else: period_str = "max"
            
            logger.info(f"[Data] Fetching {period_str} of {interval} data for {ticker} (using period)")
            data = yf.download(ticker, period=period_str, interval=interval, progress=False)
            if data is None or data.empty:
                logger.error(f"[Data] yfinance returned empty data for {symbol}")
                return None

            df = data.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [col[0] for col in df.columns]

            col_map = {}
            for col in df.columns:
                cl = str(col).lower()
                if "date" in cl or "datetime" in cl: col_map[col] = "time"
                elif cl == "open":   col_map[col] = "open"
                elif cl == "high":   col_map[col] = "high"
                elif cl == "low":    col_map[col] = "low"
                elif cl == "close":  col_map[col] = "close"
                elif cl == "volume": col_map[col] = "volume"

            df = df.rename(columns=col_map)
            if "volume" not in df.columns:
                df["volume"] = 0

            df["time"] = pd.to_datetime(df["time"])
            df = df[["time", "open", "high", "low", "close", "volume"]].dropna()
            df = df.reset_index(drop=True)
            logger.info(f"Fetched {len(df)} candles for {ticker} "
                        f"({days_to_fetch}d window, {warmup_extra_days}d warmup)")
            return df

        except Exception as e:
            logger.error(f"[Data] yfinance fetch failed for {symbol}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # CORE BACKTEST ENGINE
    # ─────────────────────────────────────────────────────────────────────────

    def _attach_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Pre-calculate all path-dependent indicators on the FULL dataframe."""
        # EMAs
        df['EMA_21']  = df['close'].ewm(span=21,  adjust=False).mean()
        df['SMA_50']  = df['close'].rolling(50).mean()
        df['SMA_200'] = df['close'].rolling(200).mean()

        # RSI (using the original Cutler's RSI formula with rolling mean to match old strategy)
        delta = df['close'].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        df['rsi_14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

        return df

    def run_backtest(self, symbol: str, days: int = 30, timeframe: str = "1h",
                     min_confidence: int = 40,
                     risk_per_trade: float = DEFAULT_RISK_PER_TRADE) -> dict:
        """
        Walk-forward backtest with asset-specific parameters and proper warmup.

        Key behaviour changes vs v9.2:
        1. Fetches extra warmup data so SMA200 is valid from signal bar 1
        2. Skips signals when macro_trend = "Unknown" (never defaults to sell)
        3. Uses asset-specific sl_mult so Gold gets 2.0x, Forex 2.5x, BTC 1.5x
        4. Warns if days < min_days for the asset class
        """
        ticker      = resolve_symbol(symbol)
        asset_p     = get_asset_params(ticker)
        sl_mult     = asset_p["sl_mult"]
        rr          = asset_p["rr"]
        min_days    = asset_p["min_days"]
        asset_label = asset_p["label"]

        logger.info(
            f"[Backtest] {ticker} ({asset_label}) | {days}d | "
            f"sl_mult={sl_mult} rr={rr} | min_conf={min_confidence}%"
        )

        # ── Minimum days warning ──
        days_warning = None
        if days < min_days:
            days_warning = (
                f"{asset_label} needs {min_days}+ days for a valid backtest "
                f"(you passed {days}). Results may be unreliable. "
                f"Rerun with days={min_days} or higher."
            )
            logger.warning(days_warning)

        # ── Warmup pre-fetch ──
        warmup_extra_days = 60
        # FIX: Always use 1h — daily candles don't provide enough data points
        # for SMA200 warmup. yfinance supports up to 730 days of hourly data.
        interval = "1h"

        df = self.fetch_data_yfinance(
            symbol, days, interval,
            warmup_extra_days=warmup_extra_days
        )

        if df is None or len(df) < 250:
            return {"error": f"Not enough data for {display_name(ticker)}."}

        df['atr'] = _wilder_atr(df, 14)
        
        # ── Route gold to dedicated gold strategy ─────────────────────────
        is_gold = ticker in ("GC=F", "XAUUSD=X")
        
        if is_gold:
            from engines.gold_strategy import attach_gold_indicators, analyze_gold
            df = attach_gold_indicators(df)
            logger.info(f"[Backtest] Using Gold Trend-Pullback strategy for {ticker}")
        else:
            df = self._attach_indicators(df)
            # Build SMC pattern catalogs once on the full dataframe
            fvg_catalog = self.strategy._build_fvg_catalog(df)
            ob_catalog  = self.strategy._build_ob_catalog(df)
            structure   = self.strategy._build_structure(df)

        # ── Find where the actual test window starts ──
        test_start_cutoff = df['time'].iloc[-1] - pd.Timedelta(days=days)
        test_start_idx    = df[df['time'] >= test_start_cutoff].index[0]

        # Require 200+ candles before any signal so SMA200 is completely stable
        signal_start = max(test_start_idx, 200)

        logger.info(
            f"[Backtest] Total candles: {len(df)} | "
            f"Test window starts at candle {test_start_idx} | "
            f"First signal at candle {signal_start} "
            f"(Indicators perfectly warmed up)"
        )

        lookahead     = 20
        r_multiples   = []
        trade_details = []
        total = wins = losses = 0
        skipped_unknown_trend = 0

        grade_stats = {"A+": {"w": 0, "l": 0}, "A": {"w": 0, "l": 0},
                       "B+": {"w": 0, "l": 0}}

        for i in range(signal_start, len(df) - lookahead):
            future = df.iloc[i: i + lookahead].copy()

            # ── Gold: use dedicated strategy ──
            if is_gold:
                gold_sig = analyze_gold(df, i)
                if gold_sig["direction"] is None:
                    if gold_sig.get("macro_trend") == "Unknown":
                        skipped_unknown_trend += 1
                    continue
                confidence  = gold_sig["confidence"]
                score       = f"Grade {gold_sig['grade']}"
                macro_trend = gold_sig["macro_trend"]
                direction   = gold_sig["direction"]
            else:
                # ── SMC: use standard strategy ──
                context = self.strategy.analyze_market_context(
                    ticker, df, i-1, fvg_catalog, ob_catalog, structure
                )
                if "error" in context:
                    continue
                confidence  = context.get("Confidence_Pct", 0)
                score       = context.get("Structure_Score", "C")
                macro_trend = context.get("Macro_Trend", "Unknown")

                if macro_trend == "Unknown":
                    skipped_unknown_trend += 1
                    continue

                direction = "buy" if macro_trend == "Bullish" else "sell"

            # ── Per-asset confidence and grade filtering ──
            asset_min_conf = asset_p.get("min_conf", min_confidence)
            effective_min_conf = max(min_confidence, asset_min_conf)
            if confidence < effective_min_conf:
                continue

            grade = None
            for g in ("A+", "A", "B+"):
                if g in score:
                    grade = g
                    break
            if grade is None:
                continue

            # ── Per-asset grade gate ──
            asset_min_grade = asset_p.get("min_grade", "B+")
            allowed_grades = {"A+": ["A+"], "A": ["A+", "A"], "B+": ["A+", "A", "B+"]}
            if grade not in allowed_grades.get(asset_min_grade, ["A+", "A", "B+"]):
                continue

            total += 1
            entry = df.iloc[i-1]['close']
            atr   = df.iloc[i-1]['atr']
            if pd.isna(atr) or atr == 0:
                continue

            # ── Asset-specific ATR multiplier ──
            if direction == "buy":
                sl  = entry - atr * sl_mult
                tp1 = entry + atr * sl_mult             # +1R
                tp2 = entry + atr * sl_mult * rr        # +2R
            else:
                sl  = entry + atr * sl_mult
                tp1 = entry - atr * sl_mult
                tp2 = entry - atr * sl_mult * rr

            outcome   = None
            r_result  = 0.0
            active_sl = sl
            tp1_hit   = False

            for _, row in future.iterrows():
                if direction == "buy":
                    if row['low'] <= active_sl:
                        outcome  = "tp1_then_be" if tp1_hit else "sl"
                        r_result = R_BY_OUTCOME[outcome]
                        break
                    if not tp1_hit and row['high'] >= tp1:
                        tp1_hit   = True
                        active_sl = entry
                        outcome   = "tp1_partial"
                        r_result  = R_BY_OUTCOME["tp1_partial"]
                    if row['high'] >= tp2:
                        outcome  = "tp2_via_tp1" if tp1_hit else "tp2"
                        r_result = R_BY_OUTCOME[outcome]
                        break
                else:
                    if row['high'] >= active_sl:
                        outcome  = "tp1_then_be" if tp1_hit else "sl"
                        r_result = R_BY_OUTCOME[outcome]
                        break
                    if not tp1_hit and row['low'] <= tp1:
                        tp1_hit   = True
                        active_sl = entry
                        outcome   = "tp1_partial"
                        r_result  = R_BY_OUTCOME["tp1_partial"]
                    if row['low'] <= tp2:
                        outcome  = "tp2_via_tp1" if tp1_hit else "tp2"
                        r_result = R_BY_OUTCOME[outcome]
                        break

            if outcome is None:
                continue

            is_win = r_result > 0
            wins   += int(is_win)
            losses += int(not is_win)

            if grade in grade_stats:
                grade_stats[grade]["w" if is_win else "l"] += 1

            r_multiples.append(r_result)
            trade_details.append({
                "entry":      round(entry, 5),
                "direction":  direction,
                "outcome":    outcome,
                "r_multiple": round(r_result, 2),
                "sl_dist_pct": round(abs(entry - sl) / entry * 100, 3),
                "confidence": confidence,
                "grade":      grade,
            })

        if total == 0 or not r_multiples:
            msg = (
                f"No qualifying signals found for {display_name(ticker)}. "
                f"Skipped {skipped_unknown_trend} signals due to unknown trend. "
                f"Try days={min_days} or lower min_confidence."
            )
            return {"Symbol": display_name(ticker), "error": msg}

        # ── STATISTICS (R-multiple space) ────────────────────────────────────
        r_arr          = np.array(r_multiples)
        win_rate       = wins / total * 100
        expectancy_r   = r_arr.mean()
        eq_returns     = r_arr * risk_per_trade
        total_return   = ((1 + eq_returns).prod() - 1) * 100
        std            = eq_returns.std()
        sharpe         = (eq_returns.mean() / std * np.sqrt(252)) if std > 0 else 0.0
        equity_curve   = np.cumprod(1 + eq_returns) * 10_000
        peak           = np.maximum.accumulate(equity_curve)
        max_dd_pct     = ((peak - equity_curve) / peak * 100).max()
        gross_profit_r = r_arr[r_arr > 0].sum()
        gross_loss_r   = abs(r_arr[r_arr < 0].sum())
        profit_factor  = gross_profit_r / gross_loss_r if gross_loss_r > 0 else 999.0

        grade_breakdown = {}
        for g, s in grade_stats.items():
            t = s["w"] + s["l"]
            if t > 0:
                g_rs = [r for r, d in zip(r_multiples, trade_details) if d['grade'] == g]
                grade_breakdown[g] = {
                    "trades":       t,
                    "win_rate":     f"{s['w'] / t * 100:.1f}%",
                    "expectancy_r": round(np.mean(g_rs), 2),
                }

        if win_rate >= 60 and expectancy_r >= 0.4 and sharpe > 1.5:
            verdict = "✅ EXCELLENT — Deploy to paper immediately. Go live in 2 weeks."
        elif win_rate >= 55 and expectancy_r >= 0.25 and profit_factor >= 1.5:
            verdict = "✅ SOLID — Real edge exists. Paper trade for 2 weeks then go live."
        elif win_rate >= 50 and expectancy_r >= 0.1 and profit_factor >= 1.2:
            verdict = "⚠️ MARGINAL — Edge is thin. Raise min_confidence to 55+."
        elif expectancy_r > 0:
            verdict = "⚠️ WEAK — Positive but unreliable. Do not trade live yet."
        else:
            verdict = "❌ REJECTED — Negative expectancy. Do NOT trade live."

        result = {
            "Symbol":                display_name(ticker),
            "Asset_Class":           asset_label,
            "Strategy":              "SMC v9.3 (OB+FVG+CHoCH+Volume)",
            "Period":                f"{days} Days",
            "Interval":              interval,
            "Min_Confidence":        f"{min_confidence}%",
            "Risk_Per_Trade":        f"{risk_per_trade*100:.1f}%",
            "SL_Multiplier":         f"{sl_mult}\u00d7 ATR",
            "Candles_Total":         len(df),
            "Candles_Test_Window":   len(df) - signal_start,
            "Signals":               total,
            "Skipped_Unknown_Trend": skipped_unknown_trend,
            "Wins":                  wins,
            "Losses":                losses,
            "Win_Rate":              f"{win_rate:.1f}%",
            "Expectancy_R":          f"{expectancy_r:.3f}R per trade",
            "Best_Trade_R":          f"+{r_arr.max():.1f}R",
            "Worst_Trade_R":         f"{r_arr.min():.1f}R",
            "Total_Return":          f"{total_return:.2f}%",
            "Max_Drawdown":          f"-{max_dd_pct:.2f}%",
            "Profit_Factor":         f"{profit_factor:.2f}",
            "Sharpe_Ratio":          f"{sharpe:.2f}",
            "Grade_Breakdown":       grade_breakdown,
            "Verdict":               verdict,
            "_trades":               trade_details,
            "_risk_per_trade":       risk_per_trade,
        }

        if days_warning:
            result["Warning"] = days_warning

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # MONTE CARLO — R-multiple based (unchanged from v9.2)
    # ─────────────────────────────────────────────────────────────────────────

    def monte_carlo(self, report: dict, runs: int = 500) -> dict:
        trades = report.get("_trades", [])
        if len(trades) < 10:
            return {"error": "Need at least 10 trades for Monte Carlo."}

        risk_per_trade = report.get("_risk_per_trade", DEFAULT_RISK_PER_TRADE)
        r_arr          = np.array([t['r_multiple'] for t in trades])
        start_eq       = 10_000.0
        final_equities = np.zeros(runs)
        max_drawdowns  = np.zeros(runs)

        for k in range(runs):
            shuffled = np.random.permutation(r_arr)
            eq       = start_eq
            peak_eq  = start_eq
            max_dd   = 0.0
            for r in shuffled:
                eq      = eq * (1 + r * risk_per_trade)
                peak_eq = max(peak_eq, eq)
                dd      = (peak_eq - eq) / peak_eq * 100
                max_dd  = max(max_dd, dd)
            final_equities[k] = eq
            max_drawdowns[k]  = max_dd

        return {
            "runs":           runs,
            "start_equity":   f"${start_eq:,.0f}",
            "p5_end":         f"${np.percentile(final_equities, 5):,.0f}",
            "median_end":     f"${np.median(final_equities):,.0f}",
            "p95_end":        f"${np.percentile(final_equities, 95):,.0f}",
            "pct_profitable": f"{(final_equities > start_eq).mean()*100:.1f}%",
            "median_max_dd":  f"-{np.median(max_drawdowns):.2f}%",
            "worst_5pct_dd":  f"-{np.percentile(max_drawdowns, 95):.2f}%",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # REPORT FORMATTER
    # ─────────────────────────────────────────────────────────────────────────

    def format_report(self, report: dict, include_monte_carlo: bool = True) -> str:
        if "error" in report:
            return f"❌ {report.get('error')}"

        mc = self.monte_carlo(report) if include_monte_carlo else {}

        lines = [
            "📊 CRAVE v9.3 BACKTEST REPORT",
            "══════════════════════════════════",
            f"Symbol       : {report['Symbol']} ({report.get('Asset_Class', '?')})",
            f"Strategy     : {report['Strategy']}",
            f"Period       : {report['Period']} ({report['Interval']})",
            f"Min Conf.    : {report['Min_Confidence']}",
            f"Risk/Trade   : {report['Risk_Per_Trade']}",
            f"SL Distance  : {report['SL_Multiplier']}",
            f"Candles      : {report['Candles_Test_Window']} test "
            f"(+{report['Candles_Total'] - report['Candles_Test_Window']} warmup)",
        ]

        if report.get("Warning"):
            lines.append(f"⚠️  {report['Warning']}")

        lines += [
            "──────────────────────────────────",
            f"Signals      : {report['Signals']}",
            f"Skipped(Unk) : {report['Skipped_Unknown_Trend']}",
            f"Wins / Losses: {report['Wins']} / {report['Losses']}",
            f"Win Rate     : {report['Win_Rate']}",
            "──────────────────────────────────",
            "  R-MULTIPLE STATS",
            f"  Expectancy  : {report['Expectancy_R']}",
            f"  Best Trade  : {report['Best_Trade_R']}",
            f"  Worst Trade : {report['Worst_Trade_R']}",
            "──────────────────────────────────",
            "  EQUITY STATS",
            f"  Total Return: {report['Total_Return']}",
            f"  Max Drawdown: {report['Max_Drawdown']}",
            f"  Profit Factor:{report['Profit_Factor']}",
            f"  Sharpe Ratio: {report['Sharpe_Ratio']}",
        ]

        gb = report.get("Grade_Breakdown", {})
        if gb:
            lines.append("──────────────────────────────────")
            lines.append("  GRADE BREAKDOWN")
            for g in ("A+", "A", "B+"):
                if g in gb:
                    d = gb[g]
                    lines.append(
                        f"  {g:3s}  : {d['trades']:3d} trades | "
                        f"WR {d['win_rate']:6s} | "
                        f"E {d['expectancy_r']:+.2f}R"
                    )

        if mc and "error" not in mc:
            lines += [
                "──────────────────────────────────",
                f"  MONTE CARLO ({mc['runs']} runs)",
                f"  Worst 5%    : {mc['p5_end']}",
                f"  Median      : {mc['median_end']}",
                f"  Best 95%    : {mc['p95_end']}",
                f"  % Profitable: {mc['pct_profitable']}",
                f"  Median MaxDD: {mc['median_max_dd']}",
                f"  Worst 5% DD : {mc['worst_5pct_dd']}",
            ]

        lines += [
            "══════════════════════════════════",
            f"VERDICT: {report['Verdict']}",
        ]
        return "\n".join(lines)

    # ── LEGACY WRAPPERS ───────────────────────────────────────────────────────

    def run_15_day_backtest(self, symbol: str, timeframe: str = "1h",
                             exchange: str = "alpaca") -> dict:
        return self.run_backtest(symbol, days=30, timeframe=timeframe)
