# CRAVE Hybrid Strategy Backtest Report (Verified)

> **WARNING - SUPERSEDES ALL PREVIOUS REPORTS**
> This report was generated using the fully reconciled `verify_hybrid_backtest.py` harness, which strictly mirrors the live `HybridStrategyAgent` (15m timeframe, MTF confluence, partial-booking exits, and exact confidence gates).

> **Note**: Every test is compared against a Random Baseline (coin-flip direction with identical gates and partial-booking exits) to isolate the true edge from the exit model's structural skew.

## SI=F

### 1. Random Baseline (45 days)
```text
=== XAGUSD (Silver) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 121 | Win/Loss: 48/73 | WR: 39.7%
Expectancy (net of est. costs): +0.262R | gross (no costs): +0.362R | est. cost/trade: 0.100R | PF: 1.68
Simple additive return (no compounding): +63.45%
Naive compounded return (assumes trades never overlap): +81.23% | MaxDD: -20.66%
Concurrency: max 11 / avg 1.1 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 48, 'win_rate': '33.3%'}, 'A': {'trades': 58, 'win_rate': '41.4%'}, 'B+': {'trades': 15, 'win_rate': '53.3%'}}
Skipped (why signals were rejected): {'unknown_trend': 1007, 'mtf_conflict': 461, 'bias_conflict': 96, 'confidence': 79, 'grade': 0, 'kill_zone': 1045}
```

### 2. Real Strategy (45 days)
```text
=== XAGUSD (Silver) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 143 | Win/Loss: 58/85 | WR: 40.6%
Expectancy (net of est. costs): +0.242R | gross (no costs): +0.342R | est. cost/trade: 0.100R | PF: 1.6
Simple additive return (no compounding): +69.15%
Naive compounded return (assumes trades never overlap): +90.49% | MaxDD: -38.43%
Concurrency: max 14 / avg 1.56 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 66, 'win_rate': '40.9%'}, 'A': {'trades': 50, 'win_rate': '32.0%'}, 'B+': {'trades': 27, 'win_rate': '55.6%'}}
Skipped (why signals were rejected): {'unknown_trend': 1007, 'mtf_conflict': 325, 'bias_conflict': 159, 'confidence': 130, 'grade': 0, 'kill_zone': 1045}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 36 trades | WR 41.7% | Exp +0.558R | Additive return +40.15% | max-concurrent 14
  Fold 2 (2026-07-06 to 2026-07-20): 61 trades | WR 47.5% | Exp +0.271R | Additive return +33.05% | max-concurrent 11
  Fold 3 (2026-07-20 to 2026-08-04): 46 trades | WR 30.4% | Exp -0.044R | Additive return -4.05% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.262, 'expectancy_std': 0.246, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

## GC=F

### 1. Random Baseline (45 days)
```text
=== XAUUSD (Gold) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 122 | Win/Loss: 51/71 | WR: 41.8%
Expectancy (net of est. costs): +0.327R | gross (no costs): +0.387R | est. cost/trade: 0.060R | PF: 1.68
Simple additive return (no compounding): +79.83%
Naive compounded return (assumes trades never overlap): +110.54% | MaxDD: -21.77%
Concurrency: max 14 / avg 1.91 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 21, 'win_rate': '57.1%'}, 'A': {'trades': 80, 'win_rate': '42.5%'}, 'B+': {'trades': 21, 'win_rate': '23.8%'}}
Skipped (why signals were rejected): {'unknown_trend': 1833, 'mtf_conflict': 219, 'bias_conflict': 27, 'confidence': 0, 'grade': 0, 'kill_zone': 609}
```

### 2. Real Strategy (45 days)
```text
=== XAUUSD (Gold) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 253 | Win/Loss: 87/166 | WR: 34.4%
Expectancy (net of est. costs): +0.102R | gross (no costs): +0.162R | est. cost/trade: 0.060R | PF: 1.19
Simple additive return (no compounding): +51.77%
Naive compounded return (assumes trades never overlap): +52.56% | MaxDD: -54.27%
Concurrency: max 23 / avg 3.72 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 35, 'win_rate': '54.3%'}, 'A': {'trades': 178, 'win_rate': '30.3%'}, 'B+': {'trades': 40, 'win_rate': '35.0%'}}
Skipped (why signals were rejected): {'unknown_trend': 1833, 'mtf_conflict': 65, 'bias_conflict': 50, 'confidence': 0, 'grade': 0, 'kill_zone': 609}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 79 trades | WR 36.7% | Exp +0.165R | Additive return +26.10% | max-concurrent 23
  Fold 2 (2026-07-06 to 2026-07-20): 100 trades | WR 27.0% | Exp -0.029R | Additive return -5.75% | max-concurrent 16
  Fold 3 (2026-07-20 to 2026-08-04): 74 trades | WR 41.9% | Exp +0.212R | Additive return +31.42% | max-concurrent 19
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.116, 'expectancy_std': 0.104, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

## EURUSD=X

### 1. Random Baseline (45 days)
```text
=== EURUSD (Forex) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 56 | Win/Loss: 17/39 | WR: 30.4%
Expectancy (net of est. costs): -0.006R | gross (no costs): +0.034R | est. cost/trade: 0.040R | PF: 0.99
Simple additive return (no compounding): -0.68%
Naive compounded return (assumes trades never overlap): -2.01% | MaxDD: -16.21%
Concurrency: max 5 / avg 0.29 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 5, 'win_rate': '0.0%'}, 'A': {'trades': 51, 'win_rate': '33.3%'}}
Skipped (why signals were rejected): {'unknown_trend': 1444, 'mtf_conflict': 397, 'bias_conflict': 78, 'confidence': 150, 'grade': 0, 'kill_zone': 842}
```

### 2. Real Strategy (45 days)
```text
=== EURUSD (Forex) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 102 | Win/Loss: 29/73 | WR: 28.4%
Expectancy (net of est. costs): -0.016R | gross (no costs): +0.024R | est. cost/trade: 0.040R | PF: 0.97
Simple additive return (no compounding): -3.26%
Naive compounded return (assumes trades never overlap): -5.84% | MaxDD: -22.20%
Concurrency: max 8 / avg 0.43 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 8, 'win_rate': '12.5%'}, 'A': {'trades': 94, 'win_rate': '29.8%'}}
Skipped (why signals were rejected): {'unknown_trend': 1444, 'mtf_conflict': 124, 'bias_conflict': 146, 'confidence': 309, 'grade': 0, 'kill_zone': 842}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 19 trades | WR 31.6% | Exp +0.057R | Additive return +2.18% | max-concurrent 4
  Fold 2 (2026-07-06 to 2026-07-20): 57 trades | WR 28.1% | Exp +0.104R | Additive return +11.89% | max-concurrent 8
  Fold 3 (2026-07-20 to 2026-08-04): 26 trades | WR 26.9% | Exp -0.333R | Additive return -17.33% | max-concurrent 5
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': -0.057, 'expectancy_std': 0.196, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

## BTC-USD

### 1. Random Baseline (45 days)
```text
=== BTCUSD (BTC) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 268 | Win/Loss: 138/130 | WR: 51.5%
Expectancy (net of est. costs): +0.489R | gross (no costs): +0.539R | est. cost/trade: 0.050R | PF: 2.22
Simple additive return (no compounding): +262.35%
Naive compounded return (assumes trades never overlap): +1125.70% | MaxDD: -31.20%
Concurrency: max 22 / avg 1.08 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 143, 'win_rate': '57.3%'}, 'A': {'trades': 125, 'win_rate': '44.8%'}}
Skipped (why signals were rejected): {'unknown_trend': 1068, 'mtf_conflict': 882, 'bias_conflict': 130, 'confidence': 129, 'grade': 0, 'kill_zone': 1841}
```

### 2. Real Strategy (45 days)
```text
=== BTCUSD (BTC) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 406 | Win/Loss: 219/187 | WR: 53.9%
Expectancy (net of est. costs): +0.566R | gross (no costs): +0.616R | est. cost/trade: 0.050R | PF: 2.69
Simple additive return (no compounding): +459.49%
Naive compounded return (assumes trades never overlap): +8247.46% | MaxDD: -36.06%
Concurrency: max 37 / avg 1.74 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 251, 'win_rate': '57.8%'}, 'A': {'trades': 155, 'win_rate': '47.7%'}}
Skipped (why signals were rejected): {'unknown_trend': 1068, 'mtf_conflict': 583, 'bias_conflict': 179, 'confidence': 241, 'grade': 0, 'kill_zone': 1841}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 141 trades | WR 44.0% | Exp +0.550R | Additive return +155.05% | max-concurrent 37
  Fold 2 (2026-07-06 to 2026-07-21): 156 trades | WR 54.5% | Exp +0.334R | Additive return +104.15% | max-concurrent 24
  Fold 3 (2026-07-21 to 2026-08-05): 109 trades | WR 66.1% | Exp +0.919R | Additive return +200.29% | max-concurrent 19
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.601, 'expectancy_std': 0.242, 'verdict': 'STABLE — edge holds up across independent folds'}
```

