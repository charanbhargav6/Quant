"""
CRAVE — Phase 4: Walk-Forward Validation of Adapter Strategies
====================================================================
WHY THIS SCRIPT EXISTS:
  quant_server.py's STRATEGY_DEFS lists four strategies, all correctly
  marked live_ready: False. This script is what actually earns that flag —
  or confirms it should stay False — by running each one through the same
  walk-forward rigor btest.md's Hybrid results went through, instead of
  editing the flag by hand (which is exactly how the old fabricated
  "61.4% WR, live_ready: True" Silver entry happened in the first place).

TWO LABELING ISSUES THIS SCRIPT FIXES, FOUND DURING REVIEW:
  1. orderflow_btc's backtest_period claims "3-fold WFO" — false. The
     underlying numbers (59.0% WR, PF 3.87) came from run_adapter_test.py,
     a SINGLE 45-day run, never walk-forward folded. This script actually
     folds it.
  2. structure_silver's numbers (40.6% WR, +0.242R) are the BLENDED
     Hybrid strategy's result from btest.md — not an isolated
     StructureAdapter run. StructureAdapter has only ever been tested
     alone on Gold and EURUSD (adapter_test.md), never Silver. This
     script runs the actual isolated test that was missing.

CANNOT BE RUN IN THE ANALYSIS SANDBOX:
  Data source is yfinance, unreachable from that environment (see the
  HONEST LIMITATIONS note at the top of backtesting/verify_hybrid_backtest.py).
  The walk-forward MECHANICS were verified there against synthetic data —
  correct fold isolation, adapter dispatch, stability verdict — but the
  actual numbers below can only come from running this for real, with
  network access to yfinance.

USAGE:
  python run_phase4_walk_forward.py
  Writes results to docs/wfo_phase4_results.md AND prints a suggested
  STRATEGY_DEFS patch — it does NOT edit quant_server.py automatically.
  Review the stability verdict before flipping any live_ready flag by hand;
  a script that auto-approves its own validation defeats the point of
  having a gate at all.
"""

import sys
from datetime import datetime, timezone

from backtesting.verify_hybrid_backtest import HybridVerifyBacktestAgent
from strategy_adapters import StructureAdapter, OrderFlowAdapter, TrendPriceActionAdapter

# (strategy_id, symbol, adapter, folds, total_days) — symbol is each
# strategy's PRIMARY instrument from STRATEGY_DEFS["instruments"][0].
# folds/total_days kept modest by default because yfinance's 15m history
# is capped at ~60 days total — raise total_days only if your data source
# has more history than that.
RUNS = [
    ("orderflow_btc",    "BTC-USD",   OrderFlowAdapter(),           3, 45),
    ("trend_pa_gold",    "GC=F",      TrendPriceActionAdapter(),    3, 45),
    ("trend_pa_forex",   "EURUSD=X",  TrendPriceActionAdapter(),    3, 45),
    ("structure_silver", "SI=F",      StructureAdapter(),           3, 45),
]


def main():
    agent = HybridVerifyBacktestAgent()
    results = []

    for strategy_id, symbol, adapter, folds, days in RUNS:
        print(f"\n{'='*70}\nRunning walk-forward: {strategy_id} on {symbol} "
              f"({adapter.__class__.__name__}, {folds} folds, {days}d)\n{'='*70}")
        wf = agent.run_walk_forward(symbol, total_days=days, folds=folds,
                                     adapter=adapter, enforce_kill_zones=True)
        print(agent.format_walk_forward(wf))
        results.append((strategy_id, symbol, adapter.__class__.__name__, wf))

    _write_report(results)
    _print_strategy_defs_patch(results)


def _write_report(results):
    lines = [
        "# Phase 4 — Walk-Forward Validation Results",
        f"\nGenerated {datetime.now(timezone.utc).isoformat()} by run_phase4_walk_forward.py\n",
        "Each strategy tested in isolation (its own adapter class, not the "
        "blended Hybrid) — correcting two labeling issues found during "
        "review: orderflow_btc was previously a single-period run "
        "mislabeled as WFO, and structure_silver's numbers were actually "
        "the blended Hybrid result, not an isolated Structure-adapter test.\n",
    ]
    for strategy_id, symbol, adapter_name, wf in results:
        lines.append(f"## {strategy_id} — {symbol} ({adapter_name})\n")
        if "error" in wf:
            lines.append(f"ERROR: {wf['error']}\n")
            continue
        stability = wf.get("stability", {})
        lines.append(f"```\n{HybridVerifyBacktestAgent().format_walk_forward(wf)}\n```\n")
        verdict = stability.get("verdict", "unknown")
        lines.append(f"**Verdict: {verdict}**\n")

    from pathlib import Path
    out = Path(__file__).parent / "docs" / "wfo_phase4_results.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n\nWritten to {out}")


def _print_strategy_defs_patch(results):
    print(f"\n{'='*70}\nSuggested STRATEGY_DEFS updates (review before applying by hand)\n{'='*70}")
    for strategy_id, symbol, adapter_name, wf in results:
        stability = wf.get("stability", {})
        verdict = stability.get("verdict", "")
        earns_live = verdict.startswith("STABLE")
        print(f"\n{strategy_id}:")
        print(f"  live_ready: {earns_live}   ({'PASSED' if earns_live else 'DID NOT PASS'} walk-forward)")
        if "error" not in wf:
            print(f"  expectancy_mean: {stability.get('expectancy_mean')}R "
                  f"(std {stability.get('expectancy_std')}) across "
                  f"{stability.get('folds_valid')} folds, "
                  f"{stability.get('pct_folds_profitable')}% profitable")


if __name__ == "__main__":
    main()
