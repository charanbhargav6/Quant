"""CRAVE backtest pipeline: MT5 data -> causal backtest -> Monte Carlo -> WFO -> sensitivity."""
PHASES = ["data", "backtest", "monte_carlo", "wfo", "sensitivity"]
PHASE_LABELS = {"data": "Phase 0 · MT5 data", "backtest": "Phase 1 · Backtest",
                "monte_carlo": "Phase 2 · Monte Carlo", "wfo": "Phase 3 · Walk-forward",
                "sensitivity": "Phase 4 · Sensitivity"}
