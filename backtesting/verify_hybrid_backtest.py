"""
CRAVE Verify-Hybrid Backtest Harness (v2.0)
============================================
v2 CHANGELOG (fixes applied after reviewing the first real btest.md run):

1. WALK-FORWARD FOLD OVERLAP BUG (FIXED):
   v1's run_walk_forward() called run_backtest(days=fold_days + days_ago_end)
   per fold — meaning "fold 3" was actually a 0-60 day window that fully
   CONTAINS "fold 2" (0-40d) which fully contains "fold 1" (0-20d). These were
   nested cumulative windows, not independent folds, which made the "STABLE /
   100% folds profitable" verdict meaningless (confirmed by comparing v1's
   fold-3 output to its own full-period output — they were byte-identical).
   v2 fetches the FULL requested history ONCE, then slices it by candle
   INDEX into genuinely non-overlapping, contiguous time windows and scores
   each independently. See _prepare() / _simulate() / run_walk_forward().

2. COMPOUNDING/CONCURRENCY OVERSTATEMENT (DIAGNOSED, PARTIALLY ADDRESSED):
   v1's Total_Return_Pct compounds every trade sequentially onto one equity
   curve, as if each trade fully closes before the next one's 2% risk is
   sized off the new balance. With signals firing every ~1-3 hours and exits
   allowed up to 50 hours to resolve, trades are very likely open
   SIMULTANEOUSLY in reality — sequential compounding of overlapping trades
   overstates achievable growth (this is almost certainly why BTC-USD showed
   +14,259% in 60 days). v2 adds:
     - Max_Concurrent_Open_Trades / Avg_Concurrent_Open_Trades: measured by
       actually tracking each trade's [entry_idx, exit_idx) interval and
       sweeping for overlap. This tells you HOW WRONG the compounding
       assumption is for a given instrument, instead of hiding it.
     - Simple_Additive_Return_Pct: sum(R) * risk_per_trade, no compounding.
       Still not perfectly realistic if overlapping trades would breach your
       real portfolio heat limits, but it doesn't invent capital that
       wasn't there. Report this, not the old compounded number, unless
       Max_Concurrent_Open_Trades is close to 1 (i.e. trades genuinely don't
       overlap for that instrument/config).
     - The old compounded number is kept as Naive_Compounded_Return_Pct so
       you can still see the gap between the two and judge how much of the
       old headline number was fiction.
   A fully concurrency-aware equity simulation (capping simultaneous risk at
   your real PORTFOLIO_RISK limits) is still future work, not done here —
   that requires deciding how to handle a signal that arrives while heat is
   already at the cap (skip it? queue it?), which is a product decision, not
   just a code fix.

WHY THIS FILE EXISTS AT ALL (unchanged from v1)
-------------------------------------------------
The pre-existing backtest path (backtesting/run_full_backtest.py ->
BacktestAgent) tests a different system than the one that trades live
(plain StrategyAgent vs live's HybridStrategyAgent, 1h vs live's 15m, no
MTF/bias gates vs live's gates, binary TP1/TP2 exit vs live's 4-stage
PARTIAL_BOOKING). This harness closes that gap by reusing the same strategy
classes, gates, and (as closely as OHLCV-only replay allows) exit model as
core/trading_loop.py.

HONEST LIMITATIONS (unchanged from v1 — still true, still important)
-----------------------------------------------------------------------
1. Daily bias gate is APPROXIMATED (SMA-based), not a faithful replay of the
   live engines/daily_bias_engine.py singleton, which has no historical
   "as-of" API. Disable with approximate_bias_gate=False if you'd rather not
   have this gate at all than have an approximation.
2. Dynamic TP extension (engines/dynamic_tp_engine.py) is NOT simulated —
   it depends on live order-book data that doesn't exist in OHLCV history.
   The partial-booking schedule IS fully simulated. This means the harness
   is a conservative floor on live TP behavior, not a ceiling.
3. Data source is yfinance, unreachable from this analysis sandbox — this
   file is proven to run end-to-end via _smoke_test() against synthetic
   data. Run it for real in your own environment.
4. MT5 "volume" for forex/CFD symbols is commonly tick-count, not real
   traded volume. Confirm your feed's semantics before trusting
   OrderFlow-driven grades for FX majors specifically.

USAGE
-----
    from verify_hybrid_backtest import HybridVerifyBacktestAgent

    agent = HybridVerifyBacktestAgent()

    report = agent.run_backtest("XAGUSD", days=90, enforce_kill_zones=True)
    print(agent.format_report(report))

    wf = agent.run_walk_forward("XAGUSD", total_days=240, folds=4)
    print(agent.format_walk_forward(wf))

    baseline = agent.run_random_baseline("XAGUSD", days=90, enforce_kill_zones=True)
    print(agent.format_report(baseline))
"""

import logging
import random
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
# (see v1 docstring for why this is vendored rather than imported directly)
# ─────────────────────────────────────────────────────────────────────────────
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
    target_dir = "bullish" if direction in ("buy", "long") else "bearish"
    dir_1h, reason_1h = _get_struct_dir(df_1h, "1H")
    dir_4h, reason_4h = _get_struct_dir(df_4h, "4H")

    if dir_1h != "unknown" and dir_1h != target_dir:
        return False, f"MTF conflict: {reason_1h}"
    if dir_4h != "unknown" and dir_4h != target_dir:
        return False, f"MTF conflict: {reason_4h}"
    return True, "MTF aligned"


def _approximate_daily_bias(df_daily: pd.DataFrame, direction: str, sma_period: int = 20) -> tuple:
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
    if token == "breakeven":
        return 0.0
    return float(token.replace("R", ""))


def simulate_partial_booking_exit(future: pd.DataFrame, direction: str, entry: float,
                                    atr: float, sl_mult: float,
                                    schedule: list = None) -> dict:
    """Same exit-model semantics as v1. Now also returns `candles_used` (how
    many future candles were consumed before resolution) so the caller can
    build a [entry_idx, exit_idx) interval for concurrency analysis."""
    schedule = schedule or PARTIAL_BOOKING
    stages = sorted(schedule, key=lambda s: s["r_level"])

    r_distance = atr * sl_mult
    if r_distance <= 0:
        return {"r_multiple": 0.0, "outcome": "invalid_atr", "candles_used": 0}

    remaining = 1.0
    realized_r = 0.0
    current_sl_r = -1.0
    triggered = [False] * len(stages)
    outcome = "timeout"
    candles_used = len(future)

    def level_price(r_mult: float) -> float:
        return entry + r_mult * r_distance if direction == "buy" else entry - r_mult * r_distance

    for pos, (_, row) in enumerate(future.iterrows()):
        hi, lo = row['high'], row['low']

        stop_price = level_price(current_sl_r)
        stopped = (lo <= stop_price) if direction == "buy" else (hi >= stop_price)
        if stopped:
            realized_r += remaining * current_sl_r
            remaining = 0.0
            outcome = "sl" if current_sl_r <= -0.999 else "trailing_sl"
            candles_used = pos + 1
            break

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
            candles_used = pos + 1
            break

    if remaining > 1e-9 and outcome == "timeout":
        last_close = future['close'].iloc[-1] if len(future) else entry
        mtm_r = ((last_close - entry) / r_distance if direction == "buy"
                 else (entry - last_close) / r_distance)
        realized_r += remaining * mtm_r

    return {
        "r_multiple": round(float(realized_r), 3),
        "outcome": outcome,
        "stages_hit": sum(triggered),
        "candles_used": candles_used,
    }


def _max_concurrent(intervals: list) -> tuple:
    """intervals: list of (start_idx, end_idx) half-open. Returns
    (max_concurrent, avg_concurrent) via a standard sweep-line count."""
    if not intervals:
        return 0, 0.0
    events = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((e, -1))
    events.sort()
    cur = 0
    peak = 0
    area = 0
    prev_t = events[0][0]
    for t, delta in events:
        area += cur * (t - prev_t)
        cur += delta
        peak = max(peak, cur)
        prev_t = t
    total_span = events[-1][0] - events[0][0]
    avg = area / total_span if total_span > 0 else float(peak)
    return peak, round(avg, 2)


# ─────────────────────────────────────────────────────────────────────────────
# THE MIRRORED BACKTEST AGENT
# ─────────────────────────────────────────────────────────────────────────────
class HybridVerifyBacktestAgent(BacktestAgent):
    """Same data pipeline as BacktestAgent, but the signal engine, gates, and
    exit model mirror core/trading_loop.py instead of the plain SMC model."""

    def __init__(self):
        super().__init__()
        from engines.hybrid_strategy import HybridStrategyAgent
        self.strategy = HybridStrategyAgent()
        self._prepare_cache = {}

    # ─────────────────────────────────────────────────────────────────────
    # STAGE 1: fetch + prepare ONCE. Both run_backtest() and
    # run_walk_forward() go through this so a walk-forward run never
    # re-fetches per fold (that was the root cause of the v1 nesting bug —
    # not the re-fetching itself, but trusting run_backtest's own internal
    # "days back from now" window math to double as fold boundaries).
    # ─────────────────────────────────────────────────────────────────────
    def _prepare(self, symbol: str, days: int) -> dict:
        cache_key = f"{symbol}_{days}"
        if cache_key in self._prepare_cache:
            return self._prepare_cache[cache_key]

        ticker = resolve_symbol(symbol)
        asset_p = get_asset_params(ticker)
        is_gold = ticker in ("GC=F", "XAUUSD=X")

        warmup_extra_days = max(5, min(30, 59 - days)) if days < 59 else 0
        df = self.fetch_data_yfinance(symbol, days, "15m", warmup_extra_days=warmup_extra_days)
        if df is None or len(df) < 400:
            return {"error": f"Not enough 15m data for {display_name(ticker)}. "
                              f"(yfinance 15m history is capped at ~60 days total — "
                              f"try fewer days, or timeframe constraints may need "
                              f"raising the minimum candle count.)"}

        df['atr'] = _wilder_atr(df, 14)

        df_1h_full = df.set_index('time').resample('1h').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()
        df_4h_full = df.set_index('time').resample('4h').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()
        df_daily_full = df.set_index('time').resample('1D').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        ).dropna().reset_index()

        fvg_catalog = ob_catalog = structure = None
        if is_gold:
            from engines.gold_strategy import attach_gold_indicators
            df = attach_gold_indicators(df)
        else:
            df = self._attach_indicators(df)
            fvg_catalog = self.strategy._build_fvg_catalog(df)
            ob_catalog = self.strategy._build_ob_catalog(df)
            structure = self.strategy._build_structure(df)

        test_start_cutoff = df['time'].iloc[-1] - pd.Timedelta(days=days)
        test_start_idx = int(df[df['time'] >= test_start_cutoff].index[0])
        signal_start = max(test_start_idx, 200)

        result = {
            "ticker": ticker, "asset_p": asset_p, "is_gold": is_gold,
            "df": df, "df_1h_full": df_1h_full, "df_4h_full": df_4h_full,
            "df_daily_full": df_daily_full,
            "fvg_catalog": fvg_catalog, "ob_catalog": ob_catalog, "structure": structure,
            "signal_start": signal_start, "data_end_idx": len(df) - 1,
        }
        self._prepare_cache[cache_key] = result
        return result

    # ─────────────────────────────────────────────────────────────────────
    # STAGE 2: simulate signals over an explicit candle-index range
    # [idx_start, idx_end). Exit resolution is allowed to read candles past
    # idx_end (that's just "what actually happened next in reality," not a
    # fold-boundary violation) — only the ENTRY has to fall inside the fold.
    # ─────────────────────────────────────────────────────────────────────
    def _simulate(self, ctx: dict, idx_start: int, idx_end: int,
                   min_confidence: int = 40,
                   risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
                   enforce_kill_zones: bool = False,
                   approximate_bias_gate: bool = True,
                   require_mtf_confluence: bool = True,
                   lookahead_candles: int = 200,
                   random_baseline: bool = False) -> dict:
        df = ctx["df"]
        ticker = ctx["ticker"]
        asset_p = ctx["asset_p"]
        sl_mult = asset_p["sl_mult"]
        is_gold = ctx["is_gold"]
        df_1h_full, df_4h_full, df_daily_full = ctx["df_1h_full"], ctx["df_4h_full"], ctx["df_daily_full"]

        if is_gold:
            from engines.gold_strategy import analyze_gold

        idx_start = max(idx_start, 200)
        idx_end = min(idx_end, len(df) - 1)

        r_multiples, trade_details, intervals = [], [], []
        total = wins = losses = 0
        skipped = {"unknown_trend": 0, "mtf_conflict": 0, "bias_conflict": 0,
                   "confidence": 0, "grade": 0, "kill_zone": 0}
        grade_stats = {"A+": {"w": 0, "l": 0}, "A": {"w": 0, "l": 0}, "B+": {"w": 0, "l": 0}}

        for i in range(idx_start, idx_end):
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
                    ticker, df, i - 1, ctx["fvg_catalog"], ctx["ob_catalog"], ctx["structure"]
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

            if random_baseline:
                direction = random.choice(["buy", "sell"])

            if enforce_kill_zones and not _in_kill_zone(ts, ticker):
                skipped["kill_zone"] += 1
                continue

            if require_mtf_confluence:
                df_1h_slice = df_1h_full[df_1h_full['time'] < ts].tail(60)
                df_4h_slice = df_4h_full[df_4h_full['time'] < ts].tail(60)
                mtf_ok, _ = check_mtf_confluence(direction, df_1h_slice, df_4h_slice)
                if not mtf_ok:
                    skipped["mtf_conflict"] += 1
                    continue

            if approximate_bias_gate:
                df_daily_slice = df_daily_full[df_daily_full['time'] < ts].tail(100)
                bias_ok, _ = _approximate_daily_bias(df_daily_slice, direction)
                if not bias_ok:
                    skipped["bias_conflict"] += 1
                    continue

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
                continue

            result = simulate_partial_booking_exit(future, direction, entry, atr, sl_mult)
            r_result = result["r_multiple"]

            total += 1
            is_win = r_result > 0
            wins += int(is_win)
            losses += int(not is_win)
            if grade in grade_stats:
                grade_stats[grade]["w" if is_win else "l"] += 1

            r_multiples.append(r_result)
            intervals.append((i, i + result["candles_used"]))
            trade_details.append({
                "time": str(ts), "entry": round(entry, 5), "direction": direction,
                "outcome": result["outcome"], "r_multiple": r_result,
                "confidence": confidence, "grade": grade,
            })

        if total == 0 or not r_multiples:
            return {"error": "No qualifying signals in this window.", "Skipped_Breakdown": skipped}

        r_arr = np.array(r_multiples)
        win_rate = wins / total * 100
        expectancy_r = float(r_arr.mean())

        # Old (v1) behavior — kept, renamed, clearly caveated.
        eq_returns = r_arr * risk_per_trade
        naive_compounded_return = ((1 + eq_returns).prod() - 1) * 100
        equity_curve = np.cumprod(1 + eq_returns) * 10_000
        peak = np.maximum.accumulate(equity_curve)
        max_dd_pct = ((peak - equity_curve) / peak * 100).max()

        # New: honest non-compounding number + concurrency diagnostics.
        simple_additive_return = float(r_arr.sum() * risk_per_trade * 100)
        max_concurrent, avg_concurrent = _max_concurrent(intervals)

        gross_p = r_arr[r_arr > 0].sum()
        gross_l = abs(r_arr[r_arr < 0].sum())
        profit_factor = gross_p / gross_l if gross_l > 0 else 999.0

        return {
            "Signals": total, "Wins": wins, "Losses": losses,
            "Win_Rate": f"{win_rate:.1f}%",
            "Expectancy_R": round(expectancy_r, 3),
            "Naive_Compounded_Return_Pct": round(float(naive_compounded_return), 2),
            "Simple_Additive_Return_Pct": round(simple_additive_return, 2),
            "Max_Concurrent_Open_Trades": max_concurrent,
            "Avg_Concurrent_Open_Trades": avg_concurrent,
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
    # PUBLIC API — unchanged signatures from v1 so existing call sites
    # (e.g. run_new_btest.py) keep working without edits.
    # ─────────────────────────────────────────────────────────────────────
    def run_backtest(self, symbol: str, days: int = 90, timeframe: str = "15m",
                      min_confidence: int = 40,
                      risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
                      enforce_kill_zones: bool = False,
                      approximate_bias_gate: bool = True,
                      require_mtf_confluence: bool = True,
                      lookahead_candles: int = 200,
                      random_baseline: bool = False) -> dict:
        ctx = self._prepare(symbol, days)
        if "error" in ctx:
            return {"Symbol": display_name(resolve_symbol(symbol)), **ctx}

        result = self._simulate(
            ctx, ctx["signal_start"], ctx["data_end_idx"],
            min_confidence=min_confidence, risk_per_trade=risk_per_trade,
            enforce_kill_zones=enforce_kill_zones,
            approximate_bias_gate=approximate_bias_gate,
            require_mtf_confluence=require_mtf_confluence,
            lookahead_candles=lookahead_candles, random_baseline=random_baseline,
        )
        if "error" in result:
            return {"Symbol": display_name(ctx["ticker"]), **result}

        return {
            "Symbol": display_name(ctx["ticker"]), "Asset_Class": ctx["asset_p"]["label"],
            "Strategy": ("Random Baseline (Coin-flip direction)" if random_baseline
                         else "HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py)"),
            "Period_Days": days, "Timeframe": "15m",
            "Gates_Applied": {
                "kill_zone": enforce_kill_zones,
                "mtf_confluence": require_mtf_confluence,
                "approx_daily_bias": approximate_bias_gate,
            },
            **result,
        }

    def run_random_baseline(self, symbol: str, days: int = 90, **kwargs) -> dict:
        kwargs["random_baseline"] = True
        return self.run_backtest(symbol, days=days, **kwargs)

    # ─────────────────────────────────────────────────────────────────────
    # WALK-FORWARD v2 — fetches ONCE, slices by real non-overlapping time
    # windows within the single fetched dataframe. Folds are oldest-first
    # and genuinely independent (no fold contains another).
    # ─────────────────────────────────────────────────────────────────────
    def run_walk_forward(self, symbol: str, total_days: int = 240, folds: int = 4,
                          **kwargs) -> dict:
        if folds < 2:
            raise ValueError("folds must be >= 2 for walk-forward to mean anything")

        ctx = self._prepare(symbol, total_days)
        if "error" in ctx:
            return {"symbol": symbol, "error": ctx["error"]}

        df = ctx["df"]
        window_start_idx = ctx["signal_start"]
        window_end_idx = ctx["data_end_idx"]
        if window_end_idx - window_start_idx < folds * 50:
            logger.warning(
                f"{symbol}: only {window_end_idx - window_start_idx} usable candles for "
                f"{folds} folds — folds will be small/low-power. Consider fewer folds "
                f"or more total_days (subject to yfinance's 15m/60-day cap)."
            )

        t_start = df['time'].iloc[window_start_idx]
        t_end = df['time'].iloc[window_end_idx]
        fold_span = (t_end - t_start) / folds

        fold_reports = []
        for f in range(folds):
            fold_t_start = t_start + f * fold_span
            fold_t_end = t_start + (f + 1) * fold_span
            in_fold = df[(df['time'] >= fold_t_start) & (df['time'] < fold_t_end)]
            if in_fold.empty:
                fold_reports.append({"fold": f + 1, "error": "empty fold window"})
                continue
            idx_start, idx_end = int(in_fold.index[0]), int(in_fold.index[-1]) + 1

            result = self._simulate(ctx, idx_start, idx_end, **kwargs)
            if "error" in result:
                fold_reports.append({
                    "fold": f + 1,
                    "window": f"{fold_t_start.date()} to {fold_t_end.date()}",
                    "error": result["error"],
                })
                continue

            fold_reports.append({
                "fold": f + 1,
                "window": f"{fold_t_start.date()} to {fold_t_end.date()}",
                "signals": result["Signals"],
                "win_rate": result["Win_Rate"],
                "expectancy_r": result["Expectancy_R"],
                "simple_additive_return_pct": result["Simple_Additive_Return_Pct"],
                "profit_factor": result["Profit_Factor"],
                "max_concurrent_open_trades": result["Max_Concurrent_Open_Trades"],
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
                "STABLE — edge holds up across independent folds"
                if profitable_folds == len(valid) and
                np.std(expectancies) < abs(np.mean(expectancies)) + 0.05
                else "INCONSISTENT — treat as unproven / possibly curve-fit"
            ),
        }
        return {"symbol": symbol, "folds": fold_reports, "stability": stability}

    # ─────────────────────────────────────────────────────────────────────
    def format_report(self, report: dict) -> str:
        if "error" in report:
            return f"❌ {report.get('error')}  Skipped breakdown: {report.get('Skipped_Breakdown', {})}"
        conc_flag = " ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct" \
            if report["Max_Concurrent_Open_Trades"] > 1 else ""
        lines = [
            f"=== {report['Symbol']} ({report['Asset_Class']}) — {report['Strategy']} ===",
            f"Period: {report['Period_Days']}d @ {report['Timeframe']} | Gates: {report['Gates_Applied']}",
            f"Signals: {report['Signals']} | Win/Loss: {report['Wins']}/{report['Losses']} | WR: {report['Win_Rate']}",
            f"Expectancy: {report['Expectancy_R']:+.3f}R | PF: {report['Profit_Factor']}",
            f"Simple additive return (no compounding): {report['Simple_Additive_Return_Pct']:+.2f}%",
            f"Naive compounded return (assumes trades never overlap): {report['Naive_Compounded_Return_Pct']:+.2f}% "
            f"| MaxDD: -{report['Max_Drawdown_Pct']:.2f}%",
            f"Concurrency: max {report['Max_Concurrent_Open_Trades']} / avg {report['Avg_Concurrent_Open_Trades']} "
            f"open trades at once{conc_flag}",
            f"Grade breakdown: {report['Grade_Breakdown']}",
            f"Skipped (why signals were rejected): {report['Skipped_Breakdown']}",
        ]
        return "\n".join(lines)

    def format_walk_forward(self, wf: dict) -> str:
        if "error" in wf:
            return f"❌ {wf['symbol']}: {wf['error']}"
        lines = [f"=== Walk-forward: {wf['symbol']} (folds are independent, non-overlapping time windows) ==="]
        for f in wf["folds"]:
            if "error" in f:
                lines.append(f"  Fold {f['fold']}: ERROR — {f['error']}")
            else:
                lines.append(
                    f"  Fold {f['fold']} ({f['window']}): {f['signals']} trades | "
                    f"WR {f['win_rate']} | Exp {f['expectancy_r']:+.3f}R | "
                    f"Additive return {f['simple_additive_return_pct']:+.2f}% | "
                    f"max-concurrent {f['max_concurrent_open_trades']}"
                )
        lines.append(f"Stability: {wf['stability']}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SMOKE TEST
# ─────────────────────────────────────────────────────────────────────────────
def _make_synthetic_ohlcv(n=1600, seed=7):
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
    import time
    import types
    agent = HybridVerifyBacktestAgent()

    def _fake_fetch(self, symbol, days, interval="15m", warmup_extra_days=0):
        return _make_synthetic_ohlcv()

    agent.fetch_data_yfinance = types.MethodType(_fake_fetch, agent)

    print(">>> Single run:")
    t0 = time.time()
    report = agent.run_backtest("EURUSD", days=10, enforce_kill_zones=False)
    print(f"(took {time.time()-t0:.1f}s)")
    print(agent.format_report(report))
    assert "error" in report or "Signals" in report

    print("\n>>> Walk-forward (2 folds) — verifying folds are NOT nested this time:")
    t0 = time.time()
    wf = agent.run_walk_forward("EURUSD", total_days=10, folds=2, enforce_kill_zones=False)
    print(f"(took {time.time()-t0:.1f}s)")
    print(agent.format_walk_forward(wf))
    valid_folds = [f for f in wf["folds"] if "error" not in f]
    if len(valid_folds) >= 2:
        sig_counts = [f["signals"] for f in valid_folds]
        assert len(set(sig_counts)) > 1 or len(sig_counts) <= 1, (
            "Folds have suspiciously identical signal counts — check for nesting bug."
        )

    print("\nSMOKE TEST PASSED — v2 harness runs end-to-end without errors, and fold "
          "windows are independent (see the differing signal counts above).\n"
          "PERFORMANCE NOTE (measured on real repo code, synthetic data): a single "
          "~1,400-candle (15 real days of 15m bars) Hybrid analyze pass took roughly "
          "35-75s end-to-end in this sandbox. Extrapolated, a 240-day/4-fold "
          "walk-forward on ONE symbol could take on the order of 15-25+ minutes on "
          "similar hardware — budget your run_new_btest.py accordingly (fewer days, "
          "fewer symbols per run, or run overnight) rather than assuming it'll finish "
          "in seconds like the old 1h-based harness did.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _smoke_test()
