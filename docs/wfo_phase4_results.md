# Phase 4 — Walk-Forward Validation Results

Generated 2026-08-24T04:12:13.912138+00:00 by run_phase4_walk_forward.py

Each strategy tested in isolation (its own adapter class, not the blended Hybrid) — correcting two labeling issues found during review: orderflow_btc was previously a single-period run mislabeled as WFO, and structure_silver's numbers were actually the blended Hybrid result, not an isolated Structure-adapter test.

## orderflow_btc — BTC-USD (OrderFlowAdapter)

```
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 90 trades | WR 65.6% | Exp +0.635R | Additive return +114.35% | max-concurrent 23
  Fold 2 (2026-07-25 to 2026-08-09): 77 trades | WR 57.1% | Exp +0.492R | Additive return +75.80% | max-concurrent 21
  Fold 3 (2026-08-09 to 2026-08-24): 59 trades | WR 27.1% | Exp +0.125R | Additive return +14.75% | max-concurrent 13
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.417, 'expectancy_std': 0.215, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## trend_pa_gold — GC=F (TrendPriceActionAdapter)

```
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 107 trades | WR 33.6% | Exp +0.204R | Additive return +43.56% | max-concurrent 16
  Fold 2 (2026-07-25 to 2026-08-09): 100 trades | WR 43.0% | Exp +0.209R | Additive return +41.80% | max-concurrent 20
  Fold 3 (2026-08-09 to 2026-08-24): 89 trades | WR 20.2% | Exp -0.077R | Additive return -13.65% | max-concurrent 14
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.112, 'expectancy_std': 0.134, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex — EURUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 106 trades | WR 37.7% | Exp +0.182R | Additive return +38.52% | max-concurrent 16
  Fold 2 (2026-07-25 to 2026-08-09): 101 trades | WR 28.7% | Exp +0.080R | Additive return +16.22% | max-concurrent 13
  Fold 3 (2026-08-09 to 2026-08-24): 88 trades | WR 42.0% | Exp +0.311R | Additive return +54.81% | max-concurrent 10
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.191, 'expectancy_std': 0.095, 'verdict': 'STABLE — edge holds up across independent folds'}
```

**Verdict: STABLE — edge holds up across independent folds**

## structure_silver — SI=F (StructureAdapter)

```
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 42 trades | WR 38.1% | Exp +0.121R | Additive return +10.20% | max-concurrent 14
  Fold 2 (2026-07-25 to 2026-08-09): 56 trades | WR 28.6% | Exp -0.131R | Additive return -14.70% | max-concurrent 12
  Fold 3 (2026-08-09 to 2026-08-24): 35 trades | WR 28.6% | Exp -0.231R | Additive return -16.20% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 1, 'pct_folds_profitable': 33.3, 'expectancy_mean': -0.08, 'expectancy_std': 0.148, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex_gbp — GBPUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: GBPUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 100 trades | WR 32.0% | Exp +0.256R | Additive return +51.20% | max-concurrent 14
  Fold 2 (2026-07-25 to 2026-08-09): 128 trades | WR 32.8% | Exp -0.072R | Additive return -18.34% | max-concurrent 16
  Fold 3 (2026-08-09 to 2026-08-24): 123 trades | WR 30.1% | Exp -0.144R | Additive return -35.51% | max-concurrent 11
Stability: {'folds_valid': 3, 'folds_profitable': 1, 'pct_folds_profitable': 33.3, 'expectancy_mean': 0.013, 'expectancy_std': 0.174, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex_jpy — USDJPY=X (TrendPriceActionAdapter)

```
=== Walk-forward: USDJPY=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 167 trades | WR 27.5% | Exp -0.054R | Additive return -18.16% | max-concurrent 20
  Fold 2 (2026-07-25 to 2026-08-09): 89 trades | WR 31.5% | Exp +0.063R | Additive return +11.23% | max-concurrent 15
  Fold 3 (2026-08-09 to 2026-08-24): 56 trades | WR 23.2% | Exp -0.294R | Additive return -32.88% | max-concurrent 19
Stability: {'folds_valid': 3, 'folds_profitable': 1, 'pct_folds_profitable': 33.3, 'expectancy_mean': -0.095, 'expectancy_std': 0.149, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**

## trend_pa_forex_aud — AUDUSD=X (TrendPriceActionAdapter)

```
=== Walk-forward: AUDUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-07-10 to 2026-07-25): 109 trades | WR 34.9% | Exp +0.169R | Additive return +36.93% | max-concurrent 15
  Fold 2 (2026-07-25 to 2026-08-09): 102 trades | WR 38.2% | Exp +0.381R | Additive return +77.79% | max-concurrent 19
  Fold 3 (2026-08-09 to 2026-08-24): 88 trades | WR 14.8% | Exp -0.451R | Additive return -79.44% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.033, 'expectancy_std': 0.353, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

**Verdict: INCONSISTENT — treat as unproven / possibly curve-fit**
