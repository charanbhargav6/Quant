# Phase 4 — Walk-Forward Validation Results

Generated 2026-08-22T08:12:55.238780+00:00 by run_phase4_walk_forward.py

Each strategy tested in isolation (its own adapter class, not the blended Hybrid) — correcting two labeling issues found during review: orderflow_btc was previously a single-period run mislabeled as WFO, and structure_silver's numbers were actually the blended Hybrid result, not an isolated Structure-adapter test.

## orderflow_btc — BTC-USD (OrderFlowAdapter)

```
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-08 to 2026-07-23): 95 trades | WR 64.2% | Exp +0.614R | Additive return +116.60% | max-concurrent 29
  Fold 2 (2026-07-23 to 2026-08-07): 47 trades | WR 80.9% | Exp +1.095R | Additive return +102.95% | max-concurrent 17
  Fold 3 (2026-08-07 to 2026-08-22): 86 trades | WR 38.4% | Exp +0.239R | Additive return +41.13% | max-concurrent 21
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.649, 'expectancy_std': 0.35, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## trend_pa_gold — GC=F (TrendPriceActionAdapter)

```
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-07 to 2026-07-22): 117 trades | WR 32.5% | Exp +0.108R | Additive return +25.21% | max-concurrent 16
  Fold 2 (2026-07-22 to 2026-08-06): 93 trades | WR 37.6% | Exp +0.124R | Additive return +23.09% | max-concurrent 20
  Fold 3 (2026-08-06 to 2026-08-21): 104 trades | WR 26.9% | Exp -0.038R | Additive return -8.00% | max-concurrent 14
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.065, 'expectancy_std': 0.073, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex — EURUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-07 to 2026-07-22): 125 trades | WR 30.4% | Exp +0.050R | Additive return +12.60% | max-concurrent 16
  Fold 2 (2026-07-22 to 2026-08-06): 113 trades | WR 28.3% | Exp +0.035R | Additive return +7.81% | max-concurrent 11
  Fold 3 (2026-08-06 to 2026-08-21): 101 trades | WR 36.6% | Exp +0.209R | Additive return +42.12% | max-concurrent 13
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.098, 'expectancy_std': 0.079, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## structure_silver — SI=F (StructureAdapter)

```
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-07 to 2026-07-22): 53 trades | WR 45.3% | Exp +0.184R | Additive return +19.55% | max-concurrent 14
  Fold 2 (2026-07-22 to 2026-08-06): 54 trades | WR 27.8% | Exp -0.128R | Additive return -13.80% | max-concurrent 12
  Fold 3 (2026-08-06 to 2026-08-21): 37 trades | WR 29.7% | Exp -0.231R | Additive return -17.10% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 1, 'pct_folds_profitable': 33.3, 'expectancy_mean': -0.058, 'expectancy_std': 0.176, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**
