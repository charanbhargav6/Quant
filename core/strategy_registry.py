"""
CRAVE -- Strategy Registry (Phase 5 fix)
============================================
WHY THIS MOVED OUT OF quant_server.py:
  STRATEGY_DEFS.live_ready was being set correctly by Phase 4's walk-forward
  results, but nothing in the execution path (core/trading_loop.py,
  brokers/broker_router.py) ever READ it -- a strategy that failed
  validation could still be toggled on for a real account via
  strategies_enabled and would actually execute trades. That gate was
  purely cosmetic.

  Moving the registry here (instead of leaving it inline in
  quant_server.py, which broker_router.py cannot import without risking a
  circular import -- quant_server.py itself imports broker/account
  modules) gives both the DISPLAY side (quant_server.py's /api/strategies)
  and the ENFORCEMENT side (broker_router.py's execution gate) one shared
  source of truth. Whichever one you update, the other sees it immediately
  -- no risk of them drifting apart the way live_ready silently did before.
"""

from typing import Optional

STRATEGY_DEFS = [
    {
        "id":   "orderflow_btc",
        "name": "OrderFlow — Crypto (BTC/ETH/SOL)",
        "description": (
            "Pure Order Flow footprint proxy. Trades volume-profile POC/VAH/VAL levels with delta confirmation. "
            "A+/A-grade signals only. PASSED 3-fold walk-forward on BTC-USD: 100% folds profitable, "
            "mean +0.905R ± 0.136 (Fold1 +0.987R, Fold2 +0.714R, Fold3 +1.015R). "
            "⚠ Max-concurrent trades up to 24 per fold — rate-limiter/cooldown must be implemented "
            "before live deployment to avoid over-exposure."
        ),
        "instruments": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jun–Aug 2026 (45 days, 3-fold WFO, verified run_phase4_walk_forward.py)",
        "static_backtest": {
            "win_rate": 68.2, "profit_factor": None, "expectancy_r": 0.905,
            "expectancy_std": 0.136, "max_concurrent": 24, "trades_per_fold": "40–86", "rr": 2.0,
        },
        "live_ready": True,   # PASSED — STABLE verdict, all 3 folds profitable
    },
    {
        "id":   "trend_pa_gold",
        "name": "Trend/Price-Action — Gold (GC=F)",
        "description": (
            "EMA50/200 trend filter + ADX strength gate + EMA21 pullback entries confirmed by Keltner, RSI, Stochastic. "
            "Phase 4 WFO result: INCONSISTENT — Fold 1 negative (-0.055R), mean only +0.076R ± 0.097 across 3 folds, "
            "66.7% folds profitable. Not approved for live trading."
        ),
        "instruments": ["GC=F", "XAUUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "static_backtest": {"win_rate": None, "profit_factor": None, "expectancy_r": None, "max_dd_pct": None, "trades": None, "rr": 2.0},
        "live_ready": False,
    },
    {
        "id":   "trend_pa_forex",
        "name": "Trend/Price-Action — Forex (EURUSD)",
        "description": (
            "Same Trend/PA engine as Gold adapter applied to Forex. "
            "Phase 4 WFO result: INCONSISTENT — Fold 1 negative (-0.084R), mean only +0.036R ± 0.092 across 3 folds, "
            "66.7% folds profitable. Near-zero edge, not approved for live trading."
        ),
        "instruments": ["EURUSD=X", "GBPUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jun–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 32.1, "profit_factor": None, "expectancy_r": 0.036,
            "expectancy_std": 0.092, "trades_per_fold": "91–120", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, near-zero mean edge
    },
    {
        "id":   "structure_silver",
        "name": "Structure (SMC/ICT) — Silver (SI=F)",
        "description": (
            "FVG, Order Block, liquidity sweep, BOS/CHoCH engine on Silver. "
            "Phase 4 WFO result: INCONSISTENT — folds degrade linearly "
            "(Fold1 +0.296R → Fold2 +0.121R → Fold3 -0.131R), high std 0.175. "
            "Mean +0.095R but consistent degradation suggests overfitting to earlier market conditions."
        ),
        "instruments": ["SI=F", "XAGUSD=X"],
        "timeframe": "M15",
        "style": "Intraday",
        "backtest_period": "Jun–Aug 2026 (45 days, 3-fold WFO, INCONSISTENT verdict)",
        "static_backtest": {
            "win_rate": 40.2, "profit_factor": None, "expectancy_r": 0.095,
            "expectancy_std": 0.175, "trades_per_fold": "26–56", "rr": 2.0,
        },
        "live_ready": False,  # FAILED — INCONSISTENT, linear degradation across folds
    },
]


def is_live_ready(strategy_id: str) -> bool:
    """The actual enforcement point Phase 5 was missing. Returns False
    for unknown strategy ids too -- an unrecognized strategy_id should
    never be treated as approved by default."""
    for s in STRATEGY_DEFS:
        if s["id"] == strategy_id:
            return bool(s.get("live_ready", False))
    return False


def get_strategy(strategy_id: str) -> dict:
    for s in STRATEGY_DEFS:
        if s["id"] == strategy_id:
            return s
    return {}

def get_max_concurrent(strategy_id: str) -> Optional[int]:
    """Return the max_concurrent limit for a strategy, if defined in its static_backtest."""
    s = get_strategy(strategy_id)
    return s.get("static_backtest", {}).get("max_concurrent")
