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

INCIDENT (post-deployment): the gate initially checked validated["node"]
as the strategy identifier. "node" is actually the machine hostname
classification (laptop/phone/aws, from config.NODES) -- unrelated to
which strategy generated a signal. Since no live signal's "node" value
ever matched a STRATEGY_DEFS id, is_live_ready() always returned False,
silently blocking 100% of live trades. Root cause underneath that: the
live signal generator (core/trading_loop.py's _analyse_and_execute) was
ALSO never wired to tag signals with a real strategy_id in the first
place -- it was, and largely still is, calling
engines.hybrid_strategy.HybridStrategyAgent directly, disconnected from
strategy_adapters.py and this registry entirely. Two bugs compounded:
wrong field read, AND the field it should have read didn't exist yet.
See core/trading_loop.py for the fix (BTC/ETH/SOL now route through
OrderFlowAdapter and tag strategy_id="orderflow_btc"; everything else
still runs the legacy engines, now honestly registered below as NOT
live-ready rather than being invisible to this registry).

ENFORCE_LIVE_READY_GATE: kill switch. Defaults to OFF while the above fix
rolls out and gets verified against real trading again. Flip to "true" in
.env once you're confident the strategy_id tagging is correct and you
want live_ready to actually gate execution again. Leaving this off does
NOT disable the strategies_enabled per-account toggle -- only the
walk-forward-validation gate.
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
    {
        "id":   "legacy_hybrid_live",
        "name": "Legacy Hybrid (SMC+OrderFlow blend) — live default engine",
        "description": (
            "The ORIGINAL engines.hybrid_strategy.HybridStrategyAgent, still the "
            "actual live signal generator for every symbol except gold and "
            "BTC/ETH/SOL as of this fix. Never isolated-validated the way the "
            "adapters above were -- btest.md's own blended-Hybrid numbers on "
            "Silver (40.6% WR, +0.242R) were barely above that instrument's "
            "coin-flip random baseline (+0.262R), and its walk-forward verdict "
            "was 'INCONSISTENT / possibly curve-fit'. Registered here honestly "
            "so the gate has something real to check, not because it has "
            "passed validation -- it has not."
        ),
        "instruments": ["most non-gold, non-BTC/ETH/SOL symbols"],
        "backtest_period": "See btest.md — blended Hybrid results, not isolated",
        "static_backtest": {"win_rate": None, "profit_factor": None, "expectancy_r": None, "max_dd_pct": None, "trades": None, "rr": 2.0},
        "live_ready": False,
    },
    {
        "id":   "legacy_gold_pullback_live",
        "name": "Legacy Gold Pullback — live default engine for XAUUSD",
        "description": (
            "engines.gold_strategy's dedicated pullback logic, routed for gold "
            "specifically inside core/trading_loop.py._analyse_and_execute. "
            "Never run through strategy_adapters.py or walk-forward validated "
            "in isolation. Registered here for the same reason as "
            "legacy_hybrid_live -- honesty, not endorsement."
        ),
        "instruments": ["XAUUSD=X"],
        "backtest_period": "Not yet walk-forward validated",
        "static_backtest": {"win_rate": None, "profit_factor": None, "expectancy_r": None, "max_dd_pct": None, "trades": None, "rr": 2.0},
        "live_ready": False,
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
