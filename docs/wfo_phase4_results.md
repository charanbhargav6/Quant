# Phase 4 — Walk-Forward Validation Results

Generated 2026-08-14T09:11:16.382088+00:00 by run_phase4_walk_forward.py

Each strategy tested in isolation (its own adapter class, not the blended Hybrid) — correcting two labeling issues found during review: orderflow_btc was previously a single-period run mislabeled as WFO, and structure_silver's numbers were actually the blended Hybrid result, not an isolated Structure-adapter test.

## orderflow_btc — BTC-USD (OrderFlowAdapter)

```
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-30 to 2026-07-15): 95 trades | WR 65.3% | Exp +0.962R | Additive return +182.80% | max-concurrent 24
  Fold 2 (2026-07-15 to 2026-07-30): 66 trades | WR 68.2% | Exp +0.799R | Additive return +105.45% | max-concurrent 23
  Fold 3 (2026-07-30 to 2026-08-14): 74 trades | WR 54.1% | Exp +0.453R | Additive return +67.10% | max-concurrent 21
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.738, 'expectancy_std': 0.212, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## trend_pa_gold — GC=F (TrendPriceActionAdapter)

```
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-30 to 2026-07-15): 84 trades | WR 21.4% | Exp -0.272R | Additive return -45.73% | max-concurrent 16
  Fold 2 (2026-07-15 to 2026-07-30): 86 trades | WR 36.0% | Exp +0.196R | Additive return +33.78% | max-concurrent 20
  Fold 3 (2026-07-30 to 2026-08-14): 132 trades | WR 29.5% | Exp +0.022R | Additive return +5.76% | max-concurrent 16
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': -0.018, 'expectancy_std': 0.193, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex — EURUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-30 to 2026-07-15): 87 trades | WR 12.6% | Exp -0.348R | Additive return -60.61% | max-concurrent 13
  Fold 2 (2026-07-15 to 2026-07-30): 100 trades | WR 39.0% | Exp +0.210R | Additive return +41.95% | max-concurrent 16
  Fold 3 (2026-07-30 to 2026-08-14): 109 trades | WR 33.0% | Exp +0.177R | Additive return +38.61% | max-concurrent 13
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.013, 'expectancy_std': 0.256, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## structure_silver — SI=F (StructureAdapter)

```
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-30 to 2026-07-15): 35 trades | WR 57.1% | Exp +0.144R | Additive return +10.05% | max-concurrent 10
  Fold 2 (2026-07-15 to 2026-07-30): 37 trades | WR 24.3% | Exp -0.091R | Additive return -6.70% | max-concurrent 14
  Fold 3 (2026-07-30 to 2026-08-14): 54 trades | WR 31.5% | Exp -0.031R | Additive return -3.30% | max-concurrent 11
Stability: {'folds_valid': 3, 'folds_profitable': 1, 'pct_folds_profitable': 33.3, 'expectancy_mean': 0.007, 'expectancy_std': 0.1, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**
