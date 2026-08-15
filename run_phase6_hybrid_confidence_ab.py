"""
CRAVE — Phase 6a: Hybrid Confidence-Gate Fix — A/B Walk-Forward Validation
============================================================================
WHY THIS SCRIPT EXISTS:
  commit e6eace2 changed engines/hybrid_strategy.py's SMC-alone confidence
  math: B+-tier signals (raw 35-49 points) used to always lose 5 points for
  lack of Order Flow confirmation, which pushed almost the entire B+ band
  below CONFIDENCE_GATES (40-45%) even though this class's own docstring
  says "SMC B+ or above -> tradeable". The fix drops that penalty for B+
  specifically (A/A+ tier is unchanged — it has enough headroom above every
  gate to absorb it without changing the pass/fail outcome).

  run_phase4_walk_forward.py's re-run after that fix (docs/wfo_phase4_
  results.md, regenerated 2026-08-14) confirmed orderflow_btc stayed STABLE
  and nothing else regressed — but that script tests StructureAdapter /
  OrderFlowAdapter / TrendPriceActionAdapter IN ISOLATION and never
  instantiates HybridAdapter, i.e. never calls engines/hybrid_strategy.py's
  analyze_market_context() at all. It could not have exercised this fix.
  This script is what actually validates it.

METHOD — A CONTROLLED A/B, NOT TWO INDEPENDENT RUNS:
  Comparing "before" and "after" walk-forward runs taken weeks apart would
  confound the fix's effect with ordinary market-regime drift between the
  two windows. Instead this script runs the SAME fold windows, on the SAME
  underlying price data, through the SAME deployed HybridAdapter, and only
  reconstructs what the OLD confidence number would have been — separating
  the fix's effect from any data/regime confound. See
  _OldPenaltyHybridAdapter below for exactly how, and why no second call
  into the SMC/OrderFlow engines is needed to do this correctly.

WHAT "PASS" LOOKS LIKE:
  Not just "more trades" — the newly-unblocked B+ trades need to hold a
  non-negative expectancy for this to be a real win rather than reclaiming
  a low-confidence tier that was correctly being filtered out. Read the
  per-fold delta_expectancy_r column, not just delta_signals.

CANNOT BE RUN IN THE ANALYSIS SANDBOX: same yfinance network limitation as
  run_phase4_walk_forward.py — see that script's header for details.

USAGE:
  python run_phase6_hybrid_confidence_ab.py
  Writes docs/wfo_phase6_confidence_ab_results.md. Does not touch
  CONFIDENCE_GATES, hybrid_strategy.py, or any live config — read the
  report and decide by hand, same as Phase 4.
"""

import sys
from datetime import datetime, timezone

import numpy as np

from backtesting.verify_hybrid_backtest import HybridVerifyBacktestAgent
from strategy_adapters import HybridAdapter
from config.config import get_effective_min_confidence


class _OldPenaltyHybridAdapter(HybridAdapter):
    """Reconstructs the PRE-FIX confidence number for A/B comparison,
    without a second, independently-variable call into the SMC/OrderFlow
    engines.

    Before e6eace2, the SMC-alone branch (OF not confirming) always applied
    `Confidence_Pct = max(0, smc_conf - 5)`, for every qualifying grade
    including B+. After e6eace2, that branch keeps `Confidence_Pct = smc_conf`
    unchanged specifically when grade == "B+"; A/A+ tier is untouched by the
    fix (already `max(0, smc_conf - 5)` before and after).

    That means: for a B+-tier, OF-unconfirmed signal, the CURRENT deployed
    code's Confidence_Pct output IS smc_conf directly — so the pre-fix value
    is recoverable exactly as `max(0, current_output - 5)`. No need to touch
    engines/hybrid_strategy.py, re-run the SMC/OF math, or risk the two
    variants seeing different signals due to any nondeterminism in that
    calculation — both adapters below are built from one identical
    analyze_market_context() call per candle.
    """
    name = "hybrid_smc_orderflow_blend_PRE_FIX_penalty"

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
        confidence = context.get("Confidence_Pct", 0)

        of_breakdown = context.get("Confidence_Breakdown", {}).get("OrderFlow", "")
        smc_alone = of_breakdown.startswith("Not confirmed")
        if smc_alone and grade == "B+":
            # Current/deployed code applies no penalty here; reconstruct
            # what the pre-fix always-minus-5 rule would have produced.
            confidence = max(0, confidence - 5)
        return {"direction": direction, "confidence": confidence, "grade": grade}


# (symbol, folds, total_days) — only instruments that actually route through
# the blended Hybrid strategy live (config.py "exchange": "mt5", not one of
# the OrderFlowAdapter crypto symbols). Kept at 3 folds / 45 days to match
# Phase 4's convention and yfinance's ~60-day 15m history cap.
RUNS = [
    ("EURUSD=X", 3, 45),
    ("GBPUSD=X", 3, 45),
    ("USDJPY=X", 3, 45),
    ("GC=F",     3, 45),
    ("SI=F",     3, 45),
]


def main():
    agent = HybridVerifyBacktestAgent()
    results = []

    for symbol, folds, days in RUNS:
        min_conf = get_effective_min_confidence(symbol)
        print(f"\n{'='*70}\n{symbol}: A/B walk-forward "
              f"({folds} folds, {days}d, live gate={min_conf:.0f}%)\n{'='*70}")

        print(f"-- post_fix (current deployed code) --")
        wf_post = agent.run_walk_forward(
            symbol, total_days=days, folds=folds,
            adapter=HybridAdapter(), enforce_kill_zones=True,
            min_confidence=min_conf,
        )
        print(agent.format_walk_forward(wf_post))

        print(f"\n-- pre_fix (reconstructed old B+ penalty) --")
        wf_pre = agent.run_walk_forward(
            symbol, total_days=days, folds=folds,
            adapter=_OldPenaltyHybridAdapter(), enforce_kill_zones=True,
            min_confidence=min_conf,
        )
        print(agent.format_walk_forward(wf_pre))

        results.append((symbol, min_conf, wf_pre, wf_post))

    _write_report(results)
    _print_summary(results)


def _fold_delta_rows(wf_pre: dict, wf_post: dict) -> list:
    """Pairs up folds by index (both runs use identical fold boundaries
    since both share the same underlying _prepare() data) and computes the
    delta each fix-relevant metric moved."""
    rows = []
    pre_folds = {f["fold"]: f for f in wf_pre.get("folds", []) if "error" not in f}
    post_folds = {f["fold"]: f for f in wf_post.get("folds", []) if "error" not in f}
    for fold_n in sorted(set(pre_folds) & set(post_folds)):
        p, q = pre_folds[fold_n], post_folds[fold_n]
        rows.append({
            "fold": fold_n,
            "window": q["window"],
            "pre_signals": p["signals"], "post_signals": q["signals"],
            "delta_signals": q["signals"] - p["signals"],
            "pre_wr": p["win_rate"], "post_wr": q["win_rate"],
            "pre_exp_r": p["expectancy_r"], "post_exp_r": q["expectancy_r"],
            "delta_exp_r": round(q["expectancy_r"] - p["expectancy_r"], 3),
        })
    return rows


def _write_report(results):
    lines = [
        "# Phase 6a — Hybrid Confidence-Gate Fix: A/B Walk-Forward Validation",
        f"\nGenerated {datetime.now(timezone.utc).isoformat()} by "
        "run_phase6_hybrid_confidence_ab.py\n",
        "Validates commit e6eace2's B+ confidence-penalty fix directly "
        "through engines/hybrid_strategy.py's blended HybridAdapter — the "
        "path Phase 4's isolated-adapter re-run never touched. Both "
        "variants below run on identical fold windows and identical "
        "underlying price data; only the confidence number attached to "
        "B+-tier, OF-unconfirmed signals differs between them.\n",
    ]
    for symbol, min_conf, wf_pre, wf_post in results:
        lines.append(f"## {symbol} (live gate: {min_conf:.0f}%)\n")
        if "error" in wf_pre or "error" in wf_post:
            lines.append(f"ERROR — pre: {wf_pre.get('error')}, "
                          f"post: {wf_post.get('error')}\n")
            continue

        rows = _fold_delta_rows(wf_pre, wf_post)
        lines.append("| Fold | Window | Signals (pre→post) | ΔSignals | "
                      "WR (pre→post) | Expectancy R (pre→post) | ΔExpectancy R |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['fold']} | {r['window']} | "
                f"{r['pre_signals']}→{r['post_signals']} | "
                f"{'+' if r['delta_signals'] >= 0 else ''}{r['delta_signals']} | "
                f"{r['pre_wr']}→{r['post_wr']} | "
                f"{r['pre_exp_r']:+.3f}→{r['post_exp_r']:+.3f} | "
                f"{'+' if r['delta_exp_r'] >= 0 else ''}{r['delta_exp_r']} |"
            )

        pre_stab, post_stab = wf_pre.get("stability", {}), wf_post.get("stability", {})
        lines.append(f"\n**pre_fix verdict:** {pre_stab.get('verdict', 'n/a')} "
                      f"(mean expectancy {pre_stab.get('expectancy_mean', 'n/a')}R)")
        lines.append(f"\n**post_fix verdict:** {post_stab.get('verdict', 'n/a')} "
                      f"(mean expectancy {post_stab.get('expectancy_mean', 'n/a')}R)\n")

        total_delta_signals = sum(r["delta_signals"] for r in rows)
        avg_delta_exp = round(float(np.mean([r["delta_exp_r"] for r in rows])), 3) if rows else 0.0
        verdict = _judge(total_delta_signals, avg_delta_exp)
        lines.append(f"**Net effect of fix: {total_delta_signals:+d} signals across "
                      f"all folds, avg Δexpectancy {avg_delta_exp:+.3f}R/fold — {verdict}**\n")

    from pathlib import Path
    out = Path(__file__).parent / "docs" / "wfo_phase6_confidence_ab_results.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n\nWritten to {out}")


def _judge(total_delta_signals: int, avg_delta_exp: float) -> str:
    if total_delta_signals <= 0:
        return "NO NEW TRADES UNLOCKED — check gate math / grade distribution for this symbol"
    if avg_delta_exp >= -0.02:
        return "PASS — reclaimed B+ trades hold expectancy, safe to ship for this symbol"
    return ("REVIEW — reclaimed B+ trades measurably drag expectancy down; "
            "consider a partial fix (e.g. raise the B+ floor slightly) "
            "rather than the full penalty removal for this symbol")


def _print_summary(results):
    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    for symbol, min_conf, wf_pre, wf_post in results:
        if "error" in wf_pre or "error" in wf_post:
            print(f"{symbol}: ERROR")
            continue
        rows = _fold_delta_rows(wf_pre, wf_post)
        total_delta_signals = sum(r["delta_signals"] for r in rows)
        avg_delta_exp = round(float(np.mean([r["delta_exp_r"] for r in rows])), 3) if rows else 0.0
        print(f"{symbol}: {total_delta_signals:+d} signals, "
              f"avg Δexpectancy {avg_delta_exp:+.3f}R/fold — "
              f"{_judge(total_delta_signals, avg_delta_exp)}")


if __name__ == "__main__":
    sys.exit(main())
