"""
CRAVE Strategy Adapters
========================
WHY THIS FILE EXISTS
---------------------
Until now, `HybridVerifyBacktestAgent` hardcoded exactly one signal engine
(HybridStrategyAgent, which internally blends SMC/Structure + OrderFlow into
a single confidence/grade number) plus a separate hardcoded branch for Gold
(gold_strategy.py). That meant:

  1. You could never independently backtest "SMC/Structure alone" vs
     "OrderFlow alone" on the same instrument — Hybrid's blending logic
     decides internally which one wins, and reports one merged number. You
     cannot tell from a Hybrid backtest report whether Gold's poor result
     comes from Structure being wrong, OrderFlow being wrong, or both.
  2. There was no way to assign a DIFFERENT strategy to a different account
     (the multi-account plan) because there was only ever one strategy
     class in play at a time, wired in by hand.
  3. gold_strategy.py's trend-pullback logic — despite being fully generic
     (every threshold is ATR/RSI/ADX/EMA-ratio based, nothing gold-specific;
     confirmed by reading attach_gold_indicators() and analyze_gold() line
     by line) — was artificially locked to Gold only, via a ticker check in
     the harness, instead of being available as a strategy for ANY instrument.

RESEARCH BASIS (deep-solution-research pass)
----------------------------------------------
- The Strategy design pattern (pluggable `generate_signals(data)`-style
  interface, swappable without touching the execution engine) is the
  standard architecture in both production trading platforms and
  educational references — e.g. NautilusTrader's Strategy/StrategyConfig
  split and "same strategy code for backtest and live" principle, and a
  Medium walkthrough of a production-style platform explicitly describing
  strategies as "modular plugins" behind a standard interface so "one
  engineer can add a new strategy while another refactors the execution
  layer without conflict."
- SMC and ICT are NOT independent alpha sources — multiple independent
  trading-education sources agree SMC is a simplified, community-evolved
  subset of ICT's original teachings (Order Blocks, FVGs, liquidity sweeps,
  BOS/CHoCH are literally the same mechanics under different names). Building
  them as two separate strategies would mostly duplicate one engine under
  two labels. This file treats "Structure" (SMC/ICT combined) as ONE family.
- Genuine footprint/order-flow analysis is explicitly documented as needing
  real executed-trade Level 2/tick data — multiple sources describe it as
  "data-intensive," requiring "high-quality tick-level data," and most
  effective on centralized markets (futures, crypto exchanges) with real
  volume, vs. being unreliable on feeds without it. This reinforces the
  existing caveat that MT5 FX/CFD "volume" (commonly tick-count, not real
  executed volume) puts the OrderFlow adapter on much shakier ground for
  forex than for crypto — worth remembering when assigning this strategy
  to accounts.
- Ensemble/multi-strategy literature (BuildAlpha, IRJET alpha-combination
  paper, institutional multi-strategy risk-budgeting practice) converges on:
  ensembling only helps when component strategies are GENUINELY DIVERSE in
  what they measure (volume vs. structure vs. trend), and blending a poor
  strategy into a good one does not rescue the poor one — you have to fix
  or discard it. This is the argument for validating each adapter STANDALONE
  before ever re-combining them, rather than trusting Hybrid's internal
  blend to average out a bad component.
- Curve-fitting risk is well-documented as severe in retail systematic
  trading (Bailey & López de Prado's work on backtest overfitting;
  Quantopian's own finding that in-sample Sharpe had ~zero predictive power
  for 888 live-deployed strategies). This is the reason each adapter below
  gets validated through the SAME walk-forward + random-baseline + cost
  model already built, not a fresh one-off backtest per strategy.

THE ADAPTER CONTRACT
---------------------
Every adapter implements:
    prepare(df) -> dict         # one-time per (symbol, window) setup
    analyze(ctx, i) -> dict     # per-candle signal, using data up to index i only

`analyze()` must return {"direction": "buy"|"sell"|None, "confidence": 0-100,
"grade": "A+"|"A"|"B+"|"B"|None}. Returning direction=None means "no signal
this candle" and is handled identically to the old "Unknown trend" skip path.

This is intentionally the SAME shape strategy_agent.py's analyze_market_context
already effectively returns — adapters exist to give that shape a formal,
swappable interface, not to invent a new one.
"""

from typing import Optional
import pandas as pd


class StrategyAdapter:
    """Base contract. See module docstring."""
    name: str = "base"

    def prepare(self, df: pd.DataFrame) -> dict:
        """df already has ['time','open','high','low','close','volume','atr'].
        Return an extra-context dict to merge into the harness's ctx — e.g.
        catalogs, or {'df': df_with_more_indicator_columns} to replace df.
        Must NOT drop rows or reorder the index — the harness indexes into
        df by integer position elsewhere and assumes alignment is preserved.
        """
        return {}

    def analyze(self, ctx: dict, i: int) -> dict:
        raise NotImplementedError


class HybridAdapter(StrategyAdapter):
    """Wraps engines.hybrid_strategy.HybridStrategyAgent — the current
    default, SMC/Structure + OrderFlow blended into one confidence/grade.
    Kept as the default for non-gold symbols so existing call sites
    (run_new_btest.py) keep behaving exactly as before unless they
    explicitly opt into a different adapter."""
    name = "hybrid_smc_orderflow_blend"

    def __init__(self):
        from engines.hybrid_strategy import HybridStrategyAgent
        self._engine = HybridStrategyAgent()

    def prepare(self, df: pd.DataFrame) -> dict:
        fvg = self._engine._build_fvg_catalog(df)
        ob = self._engine._build_ob_catalog(df)
        structure = self._engine._build_structure(df)
        return {"df": df, "_hybrid_fvg": fvg, "_hybrid_ob": ob, "_hybrid_struct": structure}

    def analyze(self, ctx: dict, i: int) -> dict:
        df = ctx["df"]
        context = self._engine.analyze_market_context(
            ctx["ticker"], df, i, ctx["_hybrid_fvg"], ctx["_hybrid_ob"], ctx["_hybrid_struct"]
        )
        if "error" in context:
            return {"direction": None, "confidence": 0, "grade": None}
        macro_trend = context.get("Macro_Trend", "Unknown")
        if macro_trend == "Unknown":
            return {"direction": None, "confidence": 0, "grade": None}
        direction = "buy" if macro_trend == "Bullish" else "sell"
        score = context.get("Structure_Score", "C")
        grade = next((g for g in ("A+", "A", "B+") if g in score), None)
        return {"direction": direction, "confidence": context.get("Confidence_Pct", 0), "grade": grade}


class StructureAdapter(StrategyAdapter):
    """SMC/ICT structure ALONE — no OrderFlow blending. Wraps the plain
    core.strategy_agent.StrategyAgent (FVG, Order Block, liquidity sweep,
    BOS/CHoCH, premium/discount, RSI divergence, volume-delta confluence
    score). Answers: does structure-based pattern reading have edge on its
    own, independent of order flow?"""
    name = "structure_smc_ict"

    def __init__(self):
        from core.strategy_agent import StrategyAgent
        self._engine = StrategyAgent()

    def prepare(self, df: pd.DataFrame) -> dict:
        fvg = self._engine._build_fvg_catalog(df)
        ob = self._engine._build_ob_catalog(df)
        structure = self._engine._build_structure(df)
        return {"df": df, "_struct_fvg": fvg, "_struct_ob": ob, "_struct_struct": structure}

    def analyze(self, ctx: dict, i: int) -> dict:
        df = ctx["df"]
        context = self._engine.analyze_market_context(
            ctx["ticker"], df, i, ctx["_struct_fvg"], ctx["_struct_ob"], ctx["_struct_struct"]
        )
        if "error" in context:
            return {"direction": None, "confidence": 0, "grade": None}
        macro_trend = context.get("Macro_Trend", "Unknown")
        if macro_trend == "Unknown":
            return {"direction": None, "confidence": 0, "grade": None}
        direction = "buy" if macro_trend == "Bullish" else "sell"
        score = context.get("Structure_Score", "C")
        grade = next((g for g in ("A+", "A", "B+") if g in score), None)
        return {"direction": direction, "confidence": context.get("Confidence_Pct", 0), "grade": grade}


class OrderFlowAdapter(StrategyAdapter):
    """OrderFlow ALONE — no Structure blending. Wraps
    engines.orderflow_strategy.analyze_orderflow directly, bypassing
    Hybrid's internal qualification rules entirely.

    IMPORTANT — mirrors hybrid_strategy.py's own bar exactly, not an
    arbitrary choice: the live Hybrid logic only ever lets OrderFlow qualify
    ALONE at grade A+/A (`take_of = of_grade in ("A+", "A")`), with
    confidence 80/60 respectively. This adapter reproduces that same bar so
    a standalone OrderFlow backtest is judged the same way live trading
    would actually accept an OrderFlow-only signal.

    CAVEAT (see module docstring): analyze_orderflow scores off the `volume`
    column. For MT5-fed FX/CFD symbols this is commonly tick-count, not real
    executed volume — treat results on those symbols with real skepticism
    versus crypto, where exchange volume is real.
    """
    name = "orderflow_footprint_proxy"

    def __init__(self, vp_bins: int = 50, delta_threshold: int = 20):
        self.vp_bins = vp_bins
        self.delta_threshold = delta_threshold

    def prepare(self, df: pd.DataFrame) -> dict:
        return {"df": df}

    def analyze(self, ctx: dict, i: int) -> dict:
        from engines.orderflow_strategy import analyze_orderflow
        df = ctx["df"]
        of = analyze_orderflow(df, i, lookback=50, bins=self.vp_bins,
                                delta_threshold=self.delta_threshold)
        grade = of.get("grade")
        direction_raw = of.get("direction", "Neutral")
        if grade not in ("A+", "A") or direction_raw not in ("Bullish", "Bearish"):
            return {"direction": None, "confidence": 0, "grade": None}
        direction = "buy" if direction_raw == "Bullish" else "sell"
        confidence = 80 if grade == "A+" else 60  # matches hybrid_strategy.py's OF-alone mapping
        return {"direction": direction, "confidence": confidence, "grade": grade}


class TrendPriceActionAdapter(StrategyAdapter):
    """Trend/pullback price-action ALONE. Wraps engines.gold_strategy's
    analyze_gold()/attach_gold_indicators() — despite the module name, this
    logic is entirely generic (EMA50/200 trend filter, ADX strength filter,
    EMA21 pullback detection, RSI/Keltner/Stochastic confirmation — every
    threshold is a ratio or an indicator level, nothing gold-price-specific).
    It was previously locked to Gold only by a ticker check in the harness;
    this adapter makes it available to any instrument.
    """
    name = "trend_price_action"

    def prepare(self, df: pd.DataFrame) -> dict:
        from engines.gold_strategy import attach_gold_indicators
        df2 = attach_gold_indicators(df)  # adds columns only, preserves row alignment
        return {"df": df2}

    def analyze(self, ctx: dict, i: int) -> dict:
        from engines.gold_strategy import analyze_gold
        df = ctx["df"]
        sig = analyze_gold(df, i)
        if sig.get("direction") is None:
            return {"direction": None, "confidence": 0, "grade": None}
        return {"direction": sig["direction"], "confidence": sig.get("confidence", 0),
                "grade": sig.get("grade")}


STRATEGY_ADAPTERS = {
    "hybrid": HybridAdapter,
    "structure": StructureAdapter,
    "orderflow": OrderFlowAdapter,
    "trend_price_action": TrendPriceActionAdapter,
}
