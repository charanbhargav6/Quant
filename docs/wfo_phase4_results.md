# Phase 4 — Walk-Forward Validation Results

Generated 2026-08-08T05:29:39.903402+00:00 by run_phase4_walk_forward.py

Each strategy tested in isolation (its own adapter class, not the blended Hybrid) — correcting two labeling issues found during review: orderflow_btc was previously a single-period run mislabeled as WFO, and structure_silver's numbers were actually the blended Hybrid result, not an isolated Structure-adapter test.

## orderflow_btc — BTC-USD (OrderFlowAdapter)

```
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-24 to 2026-07-09): 69 trades | WR 60.9% | Exp +0.987R | Additive return +136.15% | max-concurrent 24
  Fold 2 (2026-07-09 to 2026-07-24): 86 trades | WR 68.6% | Exp +0.714R | Additive return +122.75% | max-concurrent 23
  Fold 3 (2026-07-24 to 2026-08-08): 40 trades | WR 75.0% | Exp +1.015R | Additive return +81.20% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.905, 'expectancy_std': 0.136, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## trend_pa_gold — GC=F (TrendPriceActionAdapter)

```
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-23 to 2026-07-08): 71 trades | WR 26.8% | Exp -0.055R | Additive return -7.87% | max-concurrent 20
  Fold 2 (2026-07-08 to 2026-07-23): 116 trades | WR 31.0% | Exp +0.106R | Additive return +24.48% | max-concurrent 16
  Fold 3 (2026-07-23 to 2026-08-07): 101 trades | WR 39.6% | Exp +0.177R | Additive return +35.68% | max-concurrent 20
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.076, 'expectancy_std': 0.097, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex — EURUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-23 to 2026-07-08): 91 trades | WR 27.5% | Exp -0.084R | Additive return -15.33% | max-concurrent 17
  Fold 2 (2026-07-08 to 2026-07-23): 105 trades | WR 36.2% | Exp +0.141R | Additive return +29.60% | max-concurrent 16
  Fold 3 (2026-07-23 to 2026-08-07): 120 trades | WR 32.5% | Exp +0.051R | Additive return +12.35% | max-concurrent 13
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.036, 'expectancy_std': 0.092, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## structure_silver — SI=F (StructureAdapter)

```
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-23 to 2026-07-08): 26 trades | WR 53.8% | Exp +0.296R | Additive return +15.40% | max-concurrent 8
  Fold 2 (2026-07-08 to 2026-07-23): 42 trades | WR 38.1% | Exp +0.121R | Additive return +10.20% | max-concurrent 14
  Fold 3 (2026-07-23 to 2026-08-07): 56 trades | WR 28.6% | Exp -0.131R | Additive return -14.70% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.095, 'expectancy_std': 0.175, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**
