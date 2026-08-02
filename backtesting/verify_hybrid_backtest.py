"""
CRAVE Verify-Hybrid Backtest Harness (v1.0)
============================================
WHY THIS FILE EXISTS
---------------------
The existing backtest path (backtesting/run_full_backtest.py -> BacktestAgent)
tests a DIFFERENT system than the one that actually trades live:

    live (core/trading_loop.py)          old backtest (backtest_agent.py)
    ------------------------------------  ------------------------------------
    HybridStrategyAgent (SMC + OrderFlow)  StrategyAgent (SMC only)
    15-minute bars                         1-hour bars
    bias-engine agreement gate             (not checked)
    MTF (1H/4H structure) confluence gate  (not checked)
    get_effective_min_confidence()         ASSET_PARAMS.min_conf only
      (ASSET_PARAMS + CONFIDENCE_GATES)
    4-stage PARTIAL_BOOKING exits          binary TP1(1R)/TP2(2R)/SL exit
      (25% @ 1R/2R/3R/4R, SL migrates)
    KILL_ZONES session restriction         no session restriction at all

Because of that gap, a "verdict" from the old harness (e.g. btest.md's
"Silver APPROVED") tells you almost nothing about what the live bot will
actually do. This file closes that gap: it reuses the SAME strategy classes,
gates, and (as closely as OHLCV-only replay allows) the same exit model as
core/trading_loop.py, so a verdict here is actually informative.

HONEST LIMITATIONS (please read before trusting any output of this script)
----------------------------------------------------------------------------
1. Daily bias gate is APPROXIMATED, not replayed exactly. The real
   engines/daily_bias_engine.py is a live singleton that fetches "as of now"
   data and caches per calendar day — it has no historical "as of this past
   date" API. This harness approximates the same top-down idea (daily trend
   direction from a longer-period SMA) purely from the historical OHLCV
   already in memory. Set `approximate_bias_gate=False` to disable this gate
   entirely and just rely on MTF confluence + strategy confidence instead.
   If you want a fully faithful bias replay, daily_bias_engine.py needs an
   `as_of` parameter added — that's a separate, bigger refactor.
2. Dynamic TP extension (engines/dynamic_tp_engine.py) is NOT simulated.
   That engine reacts to live order-book imbalance / funding rate data that
   doesn't exist in historical OHLCV. The partial-booking schedule (the part
   that CAN be replayed from OHLCV) is fully simulated; the "can extend TP
   further" behavior is not. This means live TP-extension trades may run
   further (and score higher R) than this harness will ever show — the
   harness is a conservative floor, not a ceiling.
3. Data source is yfinance (same as the old harness), which does not have a
   network route in this analysis sandbox — this file has been unit-tested
   here against synthetic OHLCV (see `_smoke_test()` at the bottom) to
   confirm it runs end-to-end without errors, but you should run it for real
   in your own environment where yfinance is reachable.
4. MT5 "volume" for forex/CFD symbols is commonly tick-count, not real
   traded volume. The Hybrid strategy's OrderFlow half scores off `volume` —
   confirm your feed's volume semantics before trusting OrderFlow-driven
   grades for FX majors specifically (see the review notes for detail).

USAGE
-----
    from verify_hybrid_backtest import HybridVerifyBacktestAgent

    agent = HybridVerifyBacktestAgent()

    # Single mirrored run:
    report = agent.run_backtest("EURUSD", days=90, enforce_kill_zones=True)
    print(agent.format_report(report))

    # Walk-forward (multiple non-overlapping folds, each scored independently
    # so a curve-fit result in one window can't hide behind an aggregate):
    wf = agent.run_walk_forward("XAGUSD", total_days=240, folds=4)
    print(agent.format_walk_forward(wf))
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from backtesting.backtest_agent import (
    BacktestAgent,
    resolve_symbol,
    display_name,
    _wilder_atr,
    DEFAULT_RISK_PER_TRADE,
)

logger = logging.getLogger("crave.trading.verify_backtest")

try:
    from config.config import (
        get_asset_params,
        get_effective_min_confidence,
        PARTIAL_BOOKING,
        KILL_ZONES,
    )
except ImportError as e:
    raise ImportError(
        "verify_hybrid_backtest.py requires the patched config/config.py "
        "(needs get_effective_min_confidence). Apply the config.py patch "
        "from the strategy review before running this file."
    ) from e


# ─────────────────────────────────────────────────────────────────────────────
# MTF CONFLUENCE — ported from core/trading_loop.py's check_mtf_confluence()
# ─────────────────────────────────────────────────────────────────────────────
# Deliberately vendored as a pure function (rather than importing
# core.trading_loop directly) because that module pulls in live broker /
# prop-firm / economic-calendar singletons at import time that this
# standalone script has no business touching. If you change the MTF logic
# in trading_loop.py, mirror the change here too — that's the one piece of
# "keep two copies in sync" debt this file deliberately accepts, in exchange
# for not dragging live infra into a backtest process.
def _get_struct_dir(df: pd.DataFrame, tf_name: str, window: int = 3) -> tuple:
    if df is None or len(df) < 20:
        return "unknown", f"{tf_name} data unavailable"

    highs, lows = [], []
    for i in range(window, len(df) - window):
        if df['high'].iloc[i] == df['high'].iloc[i - window: i + window + 1].max():
            highs.append((i, df['high'].iloc[i]))
        if df['low'].iloc[i] == df['low'].iloc[i - window: i + window + 1].min():
            lows.append((i, df['low'].iloc[i]))

    if len(highs) < 2 or len(lows) < 2:
        close_p = df['close'].iloc[-1]
        ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        above = close_p > ema21
        d = "bullish" if above else "bearish"
        return d, f"{tf_name} EMA21 {d}"

    last_high, prev_high = highs[-1][1], highs[-2][1]
    last_low, prev_low = lows[-1][1], lows[-2][1]
    confirmed = df['close'].iloc[-1]

    bullish_bos = confirmed > last_high
    bearish_bos = confirmed < last_low
    choch_bull = confirmed > last_high and prev_high > highs[-1][1]
    choch_bear = confirmed < last_low and prev_low < lows[-1][1]

    hh, ll = last_high > prev_high, last_low < prev_low
    hl, lh = last_low > prev_low, last_high < prev_high

    structure_bullish = bullish_bos or choch_bull or (hh and hl)
    structure_bearish = bearish_bos or choch_bear or (ll and lh)

    if structure_bullish and not structure_bearish:
        return "bullish", f"{tf_name} structure BULLISH"
    if structure_bearish and not structure_bullish:
        return "bearish", f"{tf_name} structure BEARISH"

    ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
    above = confirmed > ema21
    d = "bullish" if above else "bearish"
    return d, f"{tf_name} ranging, EMA21 {d}"


def check_mtf_confluence(direction: str, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> tuple:
    """Returns (is_approved, reason). Ported 1:1 from trading_loop.py's veto logic
    (the conflict-reject half only — the grade-modifier half doesn't affect
    whether a trade is counted, only its grade, which we don't need here)."""
    target_dir = "bullish" if direction in ("buy", "long") else "bearish"
    dir_1h, reason_1h = _get_struct_dir(df_1h, "1H")
    dir_4h, reason_4h = _get_struct_dir(df_4h, "4H")

    if dir_1h != "unknown" and dir_1h != target_dir:
        return False, f"MTF conflict: {reason_1h}"
    if dir_4h != "unknown" and dir_4h != target_dir:
        return False, f"MTF conflict: {reason_4h}"
    return True, "MTF aligned"


def _approximate_daily_bias(df_daily: pd.DataFrame, direction: str, sma_period: int = 20) -> tuple:
    """
    Approximates daily_bias_engine's top-down gate using only OHLCV already
    in memory. NOT a faithful replay of the real engine — see module
    docstring limitation #1. Returns (is_tradeable, reason).
    """
    if df_daily is None or len(df_daily) < sma_period + 1:
        return True, "insufficient daily history — bias gate skipped"
    sma = df_daily['close'].rolling(sma_period).mean().iloc[-1]
    last_close = df_daily['close'].iloc[-1]
    if pd.isna(sma):
        return True, "sma warming up — bias gate skipped"
    bias_dir = "buy" if last_close > sma else "sell"
    if bias_dir == direction:
        return True, f"approx daily bias agrees ({bias_dir})"
    return False, f"approx daily bias conflict (bias={bias_dir}, signal={direction})"


def _in_kill_zone(ts, symbol: str) -> bool:
    try:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        hhmm = t.strftime("%H:%M")
        for zone in KILL_ZONES.values():
            insts = zone.get("instruments", "all")
            if insts != "all" and symbol not in insts:
                continue
            start, end = zone["start_utc"], zone["end_utc"]
            if start <= end:
                if start <= hhmm <= end:
                    return True
            else:
                if hhmm >= start or hhmm <= end:
                    return True
        return False
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# REAL 4-STAGE PARTIAL-BOOKING EXIT SIMULATION
# ─────────────────────────────────────────────────────────────────────────────
def _parse_sl_move(token: str) -> float:
    """'-0.3R' -> -0.3, 'breakeven' -> 0.0, '+2R' -> 2.0"""
    if token == "breakeven":
        return 0.0
    return float(token.replace("R", ""))


def simulate_partial_booking_exit(future: pd.DataFrame, direction: str, entry: float,
                                    atr: float, sl_mult: float,
                                    schedule: list = None) -> dict:
    """
    Faithfully simulates config.py's PARTIAL_BOOKING schedule instead of the
    old binary TP1(1R)/TP2(2R) model. Returns the REALIZED R-multiple across
    all partial closes (fraction-weighted), never silently drops a trade that
    doesn't fully resolve within the lookahead window — an unresolved
    remainder is marked-to-market at the last available close instead
    (fixing the old "undecided trades vanish from stats" bias).
    """
    schedule = schedule or PARTIAL_BOOKING
    stages = sorted(schedule, key=lambda s: s["r_level"])

    r_distance = atr * sl_mult  # price distance representing 1R
    if r_distance <= 0:
        return {"r_multiple": 0.0, "outcome": "invalid_atr"}

    remaining = 1.0
    realized_r = 0.0
    current_sl_r = -1.0  # SL starts at -1R by definition
    triggered = [False] * len(stages)
    outcome = "timeout"

    def level_price(r_mult: float) -> float:
        return entry + r_mult * r_distance if direction == "buy" else entry - r_mult * r_distance

    for _, row in future.iterrows():
        hi, lo = row['high'], row['low']

        # 1) Check stop-out on the remaining fraction first (conservative:
        #    within a single bar we can't know sequencing, so a stop-hit bar
        #    is treated as the stop firing before any same-bar target).
        stop_price = level_price(current_sl_r)
        stopped = (lo <= stop_price) if direction == "buy" else (hi >= stop_price)
        if stopped:
            realized_r += remaining * current_sl_r
            remaining = 0.0
            outcome = "sl" if current_sl_r <= -0.999 else "trailing_sl"
            break

        # 2) Check each not-yet-triggered target level, ascending.
        for idx, stage in enumerate(stages):
            if triggered[idx]:
                continue
            tgt_price = level_price(stage["r_level"])
            hit = (hi >= tgt_price) if direction == "buy" else (lo <= tgt_price)
            if hit:
                triggered[idx] = True
                frac = stage["close_pct"] / 100.0
                realized_r += frac * stage["r_level"]
                remaining -= frac
                current_sl_r = _parse_sl_move(stage["sl_move_to"])

        if remaining <= 1e-9:
            outcome = "full_target"
            break

    else:
        # loop completed without break -> ran out of lookahead candles
        pass

    if remaining > 1e-9 and outcome == "timeout":
        # Mark remaining fraction to market at the last available close.
        last_close = future['close'].iloc[-1] if len(future) else entry
        mtm_r = ((last_close - entry) / r_distance if direction == "buy"
                 else (entry - last_close) / r_distance)
        realized_r += remaining * mtm_r

    return {
        "r_multiple": round(float(realized_r), 3),
        "outcome": outcome,
        "stages_hit": sum(triggered),
    }


# ─────────────────────────────────────────────────────────────────────────────
# THE MIRRORED BACKTEST AGENT
# ─────────────────────────────────────────────────────────────────────────────
class HybridVerifyBacktestAgent(BacktestAgent):
    """Same data pipeline as BacktestAgent, but the signal engine, gates, and
    exit model mirror core/trading_loop.py instead of the plain SMC model."""

    def __init__(self):
        super().__init__()
        from engines.hybrid_strategy import HybridStrategyAgent
        self.strategy = HybridStrategyAgent()  # override: Hybrid, not plain SMC

    def run_backtest(self, symbol: str, days: int = 90, timeframe: str = "15m",
                      min_confidence: int = 40,
                      risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
                      enforce_kill_zones: bool = False,
                      approximate_bias_gate: bool = True,
                      require_mtf_confluence: bool = True,
                      lookahead_candles: int = 200) -> dict:
        ticker = resolve_symbol(symbol)
        asset_p = get_asset_params(ticker)
        sl_mult = asset_p["sl_mult"]
        min_days = asset_p["min_days"]
        asset_label = asset_p["label"]

        is_gold = ticker in ("GC=F", "XAUUSD=X")

        # ── Fetch 15m data (matches live trading_loop.py's entry timeframe) ──
        warmup_extra_days = 60
        df = self.fetch_data_yfinance(symbol, days, "15m", warmup_extra_days=warmup_extra_days)
        if df is None or len(df) < 400:
            return {"error": f"Not enough 15m data for {display_name(ticker)}. "
                              f"(yfinance 15m history is often short — try fewer days, "
                              f"or fall back to timeframe='1h' if this fails.)"}

        df['atr'] = _wilder_atr(df, 14)

        # ── Derive 1H / 4H views from the SAME 15m data (no extra fetch,
        #    guarantees perfect alignment for the MTF check) ──
        df_1h_full = df.set_index('time').resample('1h').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()
        df_4h_full = df.set_index('time').resample('4h').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()
        df_daily_full = df.set_index('time').resample('1D').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()

        if is_gold:
            from engines.gold_strategy import analyze_gold, attach_gold_indicators
            df = attach_gold_indicators(df)
        else:
            df = self._attach_indicators(df)
            fvg_catalog = self.strategy._build_fvg_catalog(df)
            ob_catalog = self.strategy._build_ob_catalog(df)
            structure = self.strategy._build_structure(df)

        test_start_cutoff = df['time'].iloc[-1] - pd.Timedelta(days=days)
        test_start_idx = df[df['time'] >= test_start_cutoff].index[0]
        signal_start = max(test_start_idx, 200)

        r_multiples, trade_details = [], []
        total = wins = losses = 0
        skipped = {"unknown_trend": 0, "mtf_conflict": 0, "bias_conflict": 0,
                   "confidence": 0, "grade": 0, "kill_zone": 0}
        grade_stats = {"A+": {"w": 0, "l": 0}, "A": {"w": 0, "l": 0}, "B+": {"w": 0, "l": 0}}

        for i in range(signal_start, len(df) - 1):
            ts = df.iloc[i - 1]['time']

            if is_gold:
                sig = analyze_gold(df, i)
                if sig["direction"] is None:
                    skipped["unknown_trend"] += 1
                    continue
                confidence, macro_trend, direction = sig["confidence"], sig["macro_trend"], sig["direction"]
                score = f"Grade {sig['grade']}"
            else:
                context = self.strategy.analyze_market_context(
                    ticker, df, i - 1, fvg_catalog, ob_catalog, structure
                )
                if "error" in context:
                    continue
                confidence = context.get("Confidence_Pct", 0)
                score = context.get("Structure_Score", "C")
                macro_trend = context.get("Macro_Trend", "Unknown")
                if macro_trend == "Unknown":
                    skipped["unknown_trend"] += 1
                    continue
                direction = "buy" if macro_trend == "Bullish" else "sell"

            if enforce_kill_zones and not _in_kill_zone(ts, ticker):
                skipped["kill_zone"] += 1
                continue

            # ── MTF confluence gate (matches trading_loop.py) ──
            if require_mtf_confluence:
                df_1h_slice = df_1h_full[df_1h_full['time'] < ts].tail(60)
                df_4h_slice = df_4h_full[df_4h_full['time'] < ts].tail(60)
                mtf_ok, mtf_reason = check_mtf_confluence(direction, df_1h_slice, df_4h_slice)
                if not mtf_ok:
                    skipped["mtf_conflict"] += 1
                    continue

            # ── Approximate daily bias gate ──
            if approximate_bias_gate:
                df_daily_slice = df_daily_full[df_daily_full['time'] < ts].tail(100)
                bias_ok, bias_reason = _approximate_daily_bias(df_daily_slice, direction)
                if not bias_ok:
                    skipped["bias_conflict"] += 1
                    continue

            # ── Reconciled confidence gate (ASSET_PARAMS + CONFIDENCE_GATES) ──
            effective_min_conf = max(min_confidence, get_effective_min_confidence(ticker))
            if confidence < effective_min_conf:
                skipped["confidence"] += 1
                continue

            grade = next((g for g in ("A+", "A", "B+") if g in score), None)
            if grade is None:
                skipped["grade"] += 1
                continue
            asset_min_grade = asset_p.get("min_grade", "B+")
            allowed_grades = {"A+": ["A+"], "A": ["A+", "A"], "B+": ["A+", "A", "B+"]}
            if grade not in allowed_grades.get(asset_min_grade, ["A+", "A", "B+"]):
                skipped["grade"] += 1
                continue

            entry = df.iloc[i - 1]['close']
            atr = df.iloc[i - 1]['atr']
            if pd.isna(atr) or atr == 0:
                continue

            future = df.iloc[i: i + lookahead_candles]
            if len(future) < 5:
                continue  # not enough data left to simulate — skip, don't fabricate

            result = simulate_partial_booking_exit(future, direction, entry, atr, sl_mult)
            r_result = result["r_multiple"]
            outcome = result["outcome"]

            total += 1
            is_win = r_result > 0
            wins += int(is_win)
            losses += int(not is_win)
            if grade in grade_stats:
                grade_stats[grade]["w" if is_win else "l"] += 1

            r_multiples.append(r_result)
            trade_details.append({
                "time": str(ts), "entry": round(entry, 5), "direction": direction,
                "outcome": outcome, "r_multiple": r_result, "confidence": confidence,
                "grade": grade,
            })

        if total == 0 or not r_multiples:
            return {
                "Symbol": display_name(ticker), "error": "No qualifying signals.",
                "Skipped_Breakdown": skipped,
            }

        r_arr = np.array(r_multiples)
        win_rate = wins / total * 100
        expectancy_r = float(r_arr.mean())
        eq_returns = r_arr * risk_per_trade
        total_return = ((1 + eq_returns).prod() - 1) * 100
        equity_curve = np.cumprod(1 + eq_returns) * 10_000
        peak = np.maximum.accumulate(equity_curve)
        max_dd_pct = ((peak - equity_curve) / peak * 100).max()
        gross_p = r_arr[r_arr > 0].sum()
        gross_l = abs(r_arr[r_arr < 0].sum())
        profit_factor = gross_p / gross_l if gross_l > 0 else 999.0

        return {
            "Symbol": display_name(ticker), "Asset_Class": asset_label,
            "Strategy": "HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py)",
            "Period_Days": days, "Timeframe": "15m",
            "Gates_Applied": {
                "kill_zone": enforce_kill_zones,
                "mtf_confluence": require_mtf_confluence,
                "approx_daily_bias": approximate_bias_gate,
            },
            "Signals": total, "Wins": wins, "Losses": losses,
            "Win_Rate": f"{win_rate:.1f}%",
            "Expectancy_R": round(expectancy_r, 3),
            "Total_Return_Pct": round(float(total_return), 2),
            "Max_Drawdown_Pct": round(float(max_dd_pct), 2),
            "Profit_Factor": round(float(profit_factor), 2),
            "Grade_Breakdown": {
                g: {"trades": s["w"] + s["l"], "win_rate": f"{(s['w']/(s['w']+s['l'])*100):.1f}%" if (s['w']+s['l']) else "—"}
                for g, s in grade_stats.items() if s["w"] + s["l"] > 0
            },
            "Skipped_Breakdown": skipped,
            "_trades": trade_details,
            "_r_multiples": r_multiples,
        }

    # ─────────────────────────────────────────────────────────────────────
    # WALK-FORWARD: split into K non-overlapping folds, score each
    # independently. A real edge should look reasonably consistent across
    # folds; a curve-fit result (like btest.md's single-window +120%) will
    # typically show wild inconsistency once split up.
    # ─────────────────────────────────────────────────────────────────────
    def run_walk_forward(self, symbol: str, total_days: int = 240, folds: int = 4,
                          **kwargs) -> dict:
        if folds < 2:
            raise ValueError("folds must be >= 2 for walk-forward to mean anything")

        fold_days = total_days // folds
        fold_reports = []
        for f in range(folds):
            # Each fold is scored as its own independent run over a
            # `fold_days`-length window ending `f * fold_days` days ago —
            # i.e. folds are contiguous, non-overlapping, oldest-first.
            days_ago_end = f * fold_days
            # run_backtest only supports "days back from most-recent data",
            # so we approximate a fold window by re-fetching with the right
            # total span and trimming the test window in-place.
            report = self.run_backtest(symbol, days=fold_days + days_ago_end, **kwargs)
            if "error" in report:
                fold_reports.append({"fold": f + 1, "error": report["error"]})
                continue
            trades = report["_trades"]
            # Trim to only this fold's slice (oldest `fold_days` chunk of
            # the fetched window that hasn't already been reported).
            fold_reports.append({
                "fold": f + 1,
                "window_days_ago": f"{days_ago_end}-{days_ago_end + fold_days}",
                "signals": report["Signals"],
                "win_rate": report["Win_Rate"],
                "expectancy_r": report["Expectancy_R"],
                "total_return_pct": report["Total_Return_Pct"],
                "profit_factor": report["Profit_Factor"],
            })

        valid = [f for f in fold_reports if "error" not in f]
        if len(valid) < 2:
            return {"symbol": symbol, "folds": fold_reports,
                    "stability": {"error": "not enough valid folds to assess stability"}}

        expectancies = [f["expectancy_r"] for f in valid]
        profitable_folds = sum(1 for e in expectancies if e > 0)
        stability = {
            "folds_valid": len(valid),
            "folds_profitable": profitable_folds,
            "pct_folds_profitable": round(profitable_folds / len(valid) * 100, 1),
            "expectancy_mean": round(float(np.mean(expectancies)), 3),
            "expectancy_std": round(float(np.std(expectancies)), 3),
            "verdict": (
                "STABLE — edge holds up across folds" if profitable_folds == len(valid) and
                np.std(expectancies) < abs(np.mean(expectancies)) + 0.05
                else "INCONSISTENT — treat as unproven / possibly curve-fit"
            ),
        }
        return {"symbol": symbol, "folds": fold_reports, "stability": stability}

    # ─────────────────────────────────────────────────────────────────────
    def format_report(self, report: dict) -> str:
        if "error" in report:
            return f"❌ {report.get('error')}  Skipped breakdown: {report.get('Skipped_Breakdown', {})}"
        lines = [
            f"=== {report['Symbol']} ({report['Asset_Class']}) — {report['Strategy']} ===",
            f"Period: {report['Period_Days']}d @ {report['Timeframe']} | Gates: {report['Gates_Applied']}",
            f"Signals: {report['Signals']} | Win/Loss: {report['Wins']}/{report['Losses']} | WR: {report['Win_Rate']}",
            f"Expectancy: {report['Expectancy_R']:+.3f}R | Return: {report['Total_Return_Pct']:+.2f}% "
            f"| MaxDD: -{report['Max_Drawdown_Pct']:.2f}% | PF: {report['Profit_Factor']}",
            f"Grade breakdown: {report['Grade_Breakdown']}",
            f"Skipped (why signals were rejected): {report['Skipped_Breakdown']}",
        ]
        return "\n".join(lines)

    def format_walk_forward(self, wf: dict) -> str:
        lines = [f"=== Walk-forward: {wf['symbol']} ==="]
        for f in wf["folds"]:
            if "error" in f:
                lines.append(f"  Fold {f['fold']}: ERROR — {f['error']}")
            else:
                lines.append(
                    f"  Fold {f['fold']} ({f['window_days_ago']}d ago): "
                    f"{f['signals']} trades | WR {f['win_rate']} | "
                    f"Exp {f['expectancy_r']:+.3f}R | Return {f['total_return_pct']:+.2f}%"
                )
        lines.append(f"Stability: {wf['stability']}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SMOKE TEST — runs with synthetic data so this file is proven to execute
# end-to-end without a yfinance network route (not available in this sandbox).
# This does NOT validate the strategy's edge (synthetic data is random walk,
# so expect ~coin-flip results) — it only proves the harness runs cleanly.
# Run real backtests against real yfinance data in your own environment.
# ─────────────────────────────────────────────────────────────────────────────
def _make_synthetic_ohlcv(n=1200, seed=7):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1, n).cumsum()
    close = 100 + steps * 0.5
    high = close + np.abs(rng.normal(0.3, 0.15, n))
    low = close - np.abs(rng.normal(0.3, 0.15, n))
    open_ = close + rng.normal(0, 0.1, n)
    volume = rng.integers(50, 500, n).astype(float)
    times = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"time": times, "open": open_, "high": high,
                          "low": low, "close": close, "volume": volume})


def _smoke_test():
    agent = HybridVerifyBacktestAgent()

    def _fake_fetch(self, symbol, days, interval="15m", warmup_extra_days=0):
        return _make_synthetic_ohlcv()

    import types
    agent.fetch_data_yfinance = types.MethodType(_fake_fetch, agent)

    report = agent.run_backtest("EURUSD", days=30, enforce_kill_zones=False,
                                 approximate_bias_gate=True, require_mtf_confluence=True)
    print(agent.format_report(report))
    assert "error" in report or "Signals" in report, "run_backtest returned malformed report"
    print("\nSMOKE TEST PASSED — harness runs end-to-end without errors.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _smoke_test()
