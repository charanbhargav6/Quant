"""
CRAVE -- Strategy Registry (Phase 6 — Full Adapter Routing)
============================================
All MT5 instruments now route through named strategy adapters that are
registered and trackable here. HybridStrategyAgent is removed from the
live execution path entirely.

Adapter→Asset mapping (core/trading_loop.py):
  orderflow_btc       → BTCUSDT, ETHUSDT, SOLUSDT  (validated ✅)
  trend_pa_gold       → XAUUSD=X                    (WFO: INCONSISTENT ⚠)
  structure_silver    → XAGUSD=X                    (WFO: INCONSISTENT ⚠)
  trend_pa_forex      → EURUSD=X                    (WFO: STABLE ✅)
  trend_pa_forex_gbp  → GBPUSD=X                    (WFO: INCONSISTENT ⚠)
  trend_pa_forex_jpy  → USDJPY=X                    (WFO: INCONSISTENT ⚠)
  trend_pa_forex_aud  → AUDUSD=X                    (WFO: INCONSISTENT ⚠)
  mean_reversion_ranging → staged for WFO; not live-ready
  volatility_breakout_xau → staged for WFO; paper-only

ENFORCE_LIVE_READY_GATE: kill switch. Defaults to ON so strategies that
are unvalidated or failed cannot execute live. Set it to "false" only for an
explicit paper/staging run. The per-account strategies_enabled toggle can
further narrow access but cannot override a failed validation when the gate is on.
"""
import os

ENFORCE_LIVE_READY_GATE = os.environ.get("ENFORCE_LIVE_READY_GATE", "true").lower() == "true"

STRATEGY_DEFS = [
    {
        "id":   "orderflow_btc",
        "name": "OrderFlow — Crypto (BTC/ETH/SOL)",
        "description": (
            "Pure Order Flow footprint proxy. Trades volume-profile POC/VAH/VAL levels with delta confirmation. "
            "A+/A-grade signals only. PASSED 3-fold walk-forward on BTC-USD: 100% folds profitable, "
            "mean +0.417R ± 0.215 (Fold1 +0.635R, Fold2 +0.492R, Fold3 +0.125R). "
            "⚠ Max-concurrent trades up to 23 per fold — rate-limiter/cooldown must be implemented "
            "before live deployment to avoid over-exposure."
        ),
        "instruments": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, verified)",
        "static_backtest": {
            "win_rate": 49.9, "profit_factor": None, "expectancy_r": 0.417,
            "expectancy_std": 0.215, "max_concurrent": 23, "trades_per_fold": "59–90", "rr": 2.0,
        },
        "live_ready": True,   # PASSED — STABLE verdict, all 3 folds profitable


    },
    {
        "id":   "trend_pa_gold",
        "name": "Trend/Price-Action — Gold (GC=F)",
        "description": (
            "EMA50/200 trend filter + ADX strength gate + EMA21 pullback entries confirmed by Keltner, RSI, Stochastic. "
            "Phase 4 WFO result: INCONSISTENT — Fold 3 negative (-0.077R), mean +0.112R ± 0.134 across 3 folds, "
            "66.7% folds profitable. Not approved for live trading."
        ),
        "instruments": ["GC=F", "XAUUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 32.2, "profit_factor": None, "expectancy_r": 0.112,
            "expectancy_std": 0.134, "trades_per_fold": "89–107", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT verdict

    },
    {
        "id":   "trend_pa_forex",
        "name": "Trend/Price-Action — Forex (EURUSD)",
        "description": (
            "Same Trend/PA engine as Gold adapter applied to Forex. "
            "Phase 4 WFO result: STABLE — 100% of folds profitable. "
            "Mean edge +0.191R ± 0.095. Approved for live trading."
        ),
        "instruments": ["EURUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, STABLE verdict)",
        "static_backtest": {
            "win_rate": 36.1, "profit_factor": None, "expectancy_r": 0.191,
            "expectancy_std": 0.095, "trades_per_fold": "88–106", "rr": 2.0,
        },
        "live_ready": True,  # PASSED — STABLE, 100% folds profitable

    },
    {
        "id":   "trend_pa_forex_gbp",
        "name": "Trend/Price-Action — Forex (GBPUSD)",
        "description": (
            "Same Trend/PA engine as trend_pa_forex, tested separately on GBPUSD "
            "(never assume one pair's edge transfers to another — see docs/"
            "wfo_phase4_results.md). Phase 4 WFO result: INCONSISTENT — only "
            "1 of 3 folds profitable (+0.256R → -0.072R → -0.144R). "
            "Mean +0.013R ± 0.174, essentially zero edge. Not approved for live trading."
        ),
        "instruments": ["GBPUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 31.6, "profit_factor": None, "expectancy_r": 0.013,
            "expectancy_std": 0.174, "trades_per_fold": "100–128", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, near-zero mean edge
    },
    {
        "id":   "trend_pa_forex_jpy",
        "name": "Trend/Price-Action — Forex (USDJPY)",
        "description": (
            "Same Trend/PA engine as trend_pa_forex, tested separately on USDJPY. "
            "Phase 4 WFO result: INCONSISTENT — only 1 of 3 folds profitable "
            "(-0.054R → +0.063R → -0.294R). Mean -0.095R ± 0.149, net-negative edge. "
            "Not approved for live trading."
        ),
        "instruments": ["USDJPY=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 27.4, "profit_factor": None, "expectancy_r": -0.095,
            "expectancy_std": 0.149, "trades_per_fold": "56–167", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, negative mean expectancy
    },
    {
        "id":   "trend_pa_forex_aud",
        "name": "Trend/Price-Action — Forex (AUDUSD)",
        "description": (
            "Same Trend/PA engine as trend_pa_forex, tested separately on AUDUSD. "
            "Phase 4 WFO result: INCONSISTENT — 2 of 3 folds profitable but high "
            "variance (+0.169R → +0.381R → -0.451R). Mean +0.033R ± 0.353 — the "
            "single worst-fold swing of any tested pair. Not approved for live trading."
        ),
        "instruments": ["AUDUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 29.3, "profit_factor": None, "expectancy_r": 0.033,
            "expectancy_std": 0.353, "trades_per_fold": "88–109", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, worst fold-to-fold variance tested
    },
    {
        "id":   "trend_pa_forex_untested",
        "name": "Trend/Price-Action — Forex (any other pair)",
        "description": (
            "Catch-all tag for any forex pair besides EURUSD/GBPUSD/USDJPY/AUDUSD "
            "that reaches the live Trend/PA routing branch. Never walk-forward "
            "tested at all — registered explicitly as not live_ready rather than "
            "silently reusing trend_pa_forex's passing id for an untested instrument."
        ),
        "instruments": [],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Not tested",
        "static_backtest": {"win_rate": None, "profit_factor": None, "expectancy_r": None,
                             "expectancy_std": None, "trades_per_fold": None, "rr": None},
        "live_ready": False,  # Never tested — do not enable without WFO first
    },
    {
        "id":   "mean_reversion_ranging",
        "name": "Mean Reversion — Ranging Regime",
        "description": (
            "Bollinger-band extreme plus RSI and volume-exhaustion setup in a "
            "RANGING regime. Staged as a complementary strategy, but it has "
            "not yet passed instrument-specific walk-forward validation."
        ),
        "instruments": [],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Not tested in the current WFO report",
        "static_backtest": {"win_rate": None, "profit_factor": None,
                             "expectancy_r": None, "expectancy_std": None,
                             "trades_per_fold": None, "rr": 1.5},
        "live_ready": False,
        "paper_only": True,
    },
    {
        "id":   "volatility_breakout_xau",
        "name": "Volatility Breakout — Gold",
        "description": (
            "First-close Bollinger expansion with ADX, RSI, and candle-body "
            "confirmation. Paper-only research candidate pending longer WFO."
        ),
        "instruments": ["XAUUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "2025-01-02 to 2026-07-31 (candidate folds)",
        "static_backtest": {"win_rate": None, "profit_factor": None,
                             "expectancy_r": None, "expectancy_std": None,
                             "trades_per_fold": None, "rr": 2.0},
        "live_ready": False,
        "paper_only": True,
    },
    {
        "id":   "structure_silver",
        "name": "Structure (SMC/ICT) — Silver (SI=F)",
        "description": (
            "FVG, Order Block, liquidity sweep, BOS/CHoCH engine on Silver. "
            "Phase 4 WFO result: INCONSISTENT — folds degenerate quickly "
            "(Fold1 +0.121R → Fold2 -0.131R → Fold3 -0.231R). "
            "Mean -0.080R edge. Strategy is actively losing money in current regime."
        ),
        "instruments": ["SI=F", "XAGUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 31.7, "profit_factor": None, "expectancy_r": -0.080,
            "expectancy_std": 0.148, "trades_per_fold": "35–56", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, negative mean expectancy
    },
]


def is_live_ready(strategy_id: str) -> bool:
    """The actual enforcement point Phase 5 was missing. Returns False
    for unknown strategy ids too -- an unrecognized strategy_id should
    never be treated as approved by default.

    Respects ENFORCE_LIVE_READY_GATE: when explicitly disabled, this returns
    True for backward-compatible paper or staging runs. Live deployments
    should leave the default enabled so unknown or failed strategies are
    rejected rather than silently treated as approved.
    """
    if not ENFORCE_LIVE_READY_GATE:
        return True
    for s in STRATEGY_DEFS:
        if s["id"] == strategy_id:
            return bool(s.get("live_ready", False))
    return False


def get_strategy(strategy_id: str) -> dict:
    for s in STRATEGY_DEFS:
        if s["id"] == strategy_id:
            return s
    return {}
