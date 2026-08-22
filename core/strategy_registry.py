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
  trend_pa_forex      → EURUSD=X, GBPUSD=X,         (WFO: INCONSISTENT ⚠)
                        USDJPY=X, AUDUSD=X

ENFORCE_LIVE_READY_GATE: kill switch. Defaults to OFF while non-crypto
strategies are still unvalidated. Flip to "true" in .env once walk-forward
validation passes for the strategy you want to gate. While OFF, only the
per-account strategies_enabled toggle is enforced.
"""
import os

ENFORCE_LIVE_READY_GATE = os.environ.get("ENFORCE_LIVE_READY_GATE", "false").lower() == "true"

STRATEGY_DEFS = [
    {
        "id":   "orderflow_btc",
        "name": "OrderFlow — Crypto (BTC/ETH/SOL)",
        "description": (
            "Pure Order Flow footprint proxy. Trades volume-profile POC/VAH/VAL levels with delta confirmation. "
            "A+/A-grade signals only. PASSED 3-fold walk-forward on BTC-USD: 100% folds profitable, "
            "mean +0.649R ± 0.350 (Fold1 +0.614R, Fold2 +1.095R, Fold3 +0.239R). "
            "⚠ Max-concurrent trades up to 29 per fold — rate-limiter/cooldown must be implemented "
            "before live deployment to avoid over-exposure."
        ),
        "instruments": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, verified)",
        "static_backtest": {
            "win_rate": 61.1, "profit_factor": None, "expectancy_r": 0.649,
            "expectancy_std": 0.350, "max_concurrent": 29, "trades_per_fold": "47–95", "rr": 2.0,
        },
        "live_ready": True,   # PASSED — STABLE verdict, all 3 folds profitable

    },
    {
        "id":   "trend_pa_gold",
        "name": "Trend/Price-Action — Gold (GC=F)",
        "description": (
            "EMA50/200 trend filter + ADX strength gate + EMA21 pullback entries confirmed by Keltner, RSI, Stochastic. "
            "Phase 4 WFO result: INCONSISTENT — Fold 3 negative (-0.038R), mean +0.065R ± 0.073 across 3 folds, "
            "66.7% folds profitable. Not approved for live trading."
        ),
        "instruments": ["GC=F", "XAUUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 32.3, "profit_factor": None, "expectancy_r": 0.065,
            "expectancy_std": 0.073, "trades_per_fold": "93–117", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT verdict

    },
    {
        "id":   "trend_pa_forex",
        "name": "Trend/Price-Action — Forex (EURUSD)",
        "description": (
            "Same Trend/PA engine as Gold adapter applied to Forex. "
            "Phase 4 WFO result: STABLE — 100% of folds profitable. "
            "Mean edge +0.098R ± 0.079. Approved for live trading."
        ),
        "instruments": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, STABLE verdict)",
        "static_backtest": {
            "win_rate": 31.7, "profit_factor": None, "expectancy_r": 0.098,
            "expectancy_std": 0.079, "trades_per_fold": "101–125", "rr": 2.0,
        },
        "live_ready": True,  # PASSED — STABLE, 100% folds profitable

    },
    {
        "id":   "structure_silver",
        "name": "Structure (SMC/ICT) — Silver (SI=F)",
        "description": (
            "FVG, Order Block, liquidity sweep, BOS/CHoCH engine on Silver. "
            "Phase 4 WFO result: INCONSISTENT — folds degenerate quickly "
            "(Fold1 +0.184R → Fold2 -0.128R → Fold3 -0.231R). "
            "Mean -0.058R edge. Strategy is actively losing money in current regime."
        ),
        "instruments": ["SI=F", "XAGUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jul–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 34.2, "profit_factor": None, "expectancy_r": -0.058,
            "expectancy_std": 0.176, "trades_per_fold": "37–54", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, negative mean expectancy
    },
]


def is_live_ready(strategy_id: str) -> bool:
    """The actual enforcement point Phase 5 was missing. Returns False
    for unknown strategy ids too -- an unrecognized strategy_id should
    never be treated as approved by default.

    Respects ENFORCE_LIVE_READY_GATE: while the gate is disabled (see
    module docstring -- INCIDENT), this always returns True so execution
    falls back to the pre-Phase-5 behavior (only strategies_enabled is
    checked, not walk-forward status) rather than silently blocking
    everything again if a caller forgets to check the flag itself.
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
