# CRAVE Hybrid Strategy Backtest Report (Verified)

> **WARNING - SUPERSEDES ALL PREVIOUS REPORTS**
> This report was generated using the fully reconciled `verify_hybrid_backtest.py` harness, which strictly mirrors the live `HybridStrategyAgent` (15m timeframe, MTF confluence, partial-booking exits, and exact confidence gates).

> **Note**: Every test is compared against a Random Baseline (coin-flip direction with identical gates and partial-booking exits) to isolate the true edge from the exit model's structural skew.

## SI=F

### 1. Random Baseline (45 days)
```text
=== XAGUSD (Silver) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 119 | Win/Loss: 80/39 | WR: 67.2%
Expectancy: +0.324R | PF: 1.99
Simple additive return (no compounding): +77.15%
Naive compounded return (assumes trades never overlap): +108.28% | MaxDD: -16.10%
Concurrency: max 9 / avg 1.04 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 47, 'win_rate': '68.1%'}, 'A': {'trades': 57, 'win_rate': '64.9%'}, 'B+': {'trades': 15, 'win_rate': '73.3%'}}
Skipped (why signals were rejected): {'unknown_trend': 973, 'mtf_conflict': 454, 'bias_conflict': 93, 'confidence': 80, 'grade': 0, 'kill_zone': 1036}
```

### 2. Real Strategy (45 days)
```text
=== XAGUSD (Silver) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 141 | Win/Loss: 91/50 | WR: 64.5%
Expectancy: +0.346R | PF: 1.98
Simple additive return (no compounding): +97.65%
Naive compounded return (assumes trades never overlap): +152.93% | MaxDD: -30.24%
Concurrency: max 14 / avg 1.61 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 66, 'win_rate': '63.6%'}, 'A': {'trades': 48, 'win_rate': '56.2%'}, 'B+': {'trades': 27, 'win_rate': '81.5%'}}
Skipped (why signals were rejected): {'unknown_trend': 973, 'mtf_conflict': 309, 'bias_conflict': 161, 'confidence': 135, 'grade': 0, 'kill_zone': 1036}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: SI=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 36 trades | WR 61.1% | Exp +0.658R | Additive return +47.35% | max-concurrent 14
  Fold 2 (2026-07-06 to 2026-07-20): 61 trades | WR 72.1% | Exp +0.371R | Additive return +45.25% | max-concurrent 11
  Fold 3 (2026-07-20 to 2026-08-03): 44 trades | WR 56.8% | Exp +0.057R | Additive return +5.05% | max-concurrent 12
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.362, 'expectancy_std': 0.245, 'verdict': 'STABLE — edge holds up across independent folds'}
```

## GC=F

### 1. Random Baseline (45 days)
```text
=== XAUUSD (Gold) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 118 | Win/Loss: 63/55 | WR: 53.4%
Expectancy: +0.266R | PF: 1.57
Simple additive return (no compounding): +62.87%
Naive compounded return (assumes trades never overlap): +78.81% | MaxDD: -23.93%
Concurrency: max 12 / avg 1.61 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 18, 'win_rate': '66.7%'}, 'A': {'trades': 78, 'win_rate': '52.6%'}, 'B+': {'trades': 22, 'win_rate': '45.5%'}}
Skipped (why signals were rejected): {'unknown_trend': 1796, 'mtf_conflict': 217, 'bias_conflict': 26, 'confidence': 0, 'grade': 0, 'kill_zone': 599}
```

### 2. Real Strategy (45 days)
```text
=== XAUUSD (Gold) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 253 | Win/Loss: 126/127 | WR: 49.8%
Expectancy: +0.162R | PF: 1.32
Simple additive return (no compounding): +82.13%
Naive compounded return (assumes trades never overlap): +106.55% | MaxDD: -50.69%
Concurrency: max 23 / avg 3.72 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 35, 'win_rate': '71.4%'}, 'A': {'trades': 178, 'win_rate': '46.6%'}, 'B+': {'trades': 40, 'win_rate': '45.0%'}}
Skipped (why signals were rejected): {'unknown_trend': 1796, 'mtf_conflict': 65, 'bias_conflict': 43, 'confidence': 0, 'grade': 0, 'kill_zone': 599}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: GC=F (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-21 to 2026-07-06): 77 trades | WR 48.1% | Exp +0.257R | Additive return +39.58% | max-concurrent 23
  Fold 2 (2026-07-06 to 2026-07-20): 102 trades | WR 41.2% | Exp +0.011R | Additive return +2.25% | max-concurrent 16
  Fold 3 (2026-07-20 to 2026-08-03): 74 trades | WR 63.5% | Exp +0.272R | Additive return +40.30% | max-concurrent 19
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.18, 'expectancy_std': 0.12, 'verdict': 'STABLE — edge holds up across independent folds'}
```

## EURUSD=X

### 1. Random Baseline (45 days)
```text
=== EURUSD (Forex) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 49 | Win/Loss: 31/18 | WR: 63.3%
Expectancy: +0.039R | PF: 1.11
Simple additive return (no compounding): +3.80%
Naive compounded return (assumes trades never overlap): +2.83% | MaxDD: -14.32%
Concurrency: max 5 / avg 0.21 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 3, 'win_rate': '66.7%'}, 'A': {'trades': 46, 'win_rate': '63.0%'}}
Skipped (why signals were rejected): {'unknown_trend': 1429, 'mtf_conflict': 406, 'bias_conflict': 74, 'confidence': 153, 'grade': 0, 'kill_zone': 822}
```

### 2. Real Strategy (45 days)
```text
=== EURUSD (Forex) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 102 | Win/Loss: 57/45 | WR: 55.9%
Expectancy: +0.024R | PF: 1.05
Simple additive return (no compounding): +4.90%
Naive compounded return (assumes trades never overlap): +2.17% | MaxDD: -20.11%
Concurrency: max 8 / avg 0.42 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 8, 'win_rate': '50.0%'}, 'A': {'trades': 94, 'win_rate': '56.4%'}}
Skipped (why signals were rejected): {'unknown_trend': 1429, 'mtf_conflict': 125, 'bias_conflict': 146, 'confidence': 309, 'grade': 0, 'kill_zone': 822}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: EURUSD=X (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-19 to 2026-07-04): 19 trades | WR 47.4% | Exp +0.043R | Additive return +1.65% | max-concurrent 4
  Fold 2 (2026-07-04 to 2026-07-19): 49 trades | WR 61.2% | Exp +0.147R | Additive return +14.40% | max-concurrent 8
  Fold 3 (2026-07-19 to 2026-08-03): 34 trades | WR 52.9% | Exp -0.164R | Additive return -11.15% | max-concurrent 7
Stability: {'folds_valid': 3, 'folds_profitable': 2, 'pct_folds_profitable': 66.7, 'expectancy_mean': 0.009, 'expectancy_std': 0.129, 'verdict': 'INCONSISTENT — treat as unproven / possibly curve-fit'}
```

## BTC-USD

### 1. Random Baseline (45 days)
```text
=== BTCUSD (BTC) — Random Baseline (Coin-flip direction) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 304 | Win/Loss: 200/104 | WR: 65.8%
Expectancy: +0.511R | PF: 2.49
Simple additive return (no compounding): +310.45%
Naive compounded return (assumes trades never overlap): +1868.96% | MaxDD: -26.39%
Concurrency: max 23 / avg 1.32 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 160, 'win_rate': '68.1%'}, 'A': {'trades': 118, 'win_rate': '62.7%'}, 'B+': {'trades': 26, 'win_rate': '65.4%'}}
Skipped (why signals were rejected): {'unknown_trend': 1055, 'mtf_conflict': 898, 'bias_conflict': 109, 'confidence': 96, 'grade': 0, 'kill_zone': 1858}
```

### 2. Real Strategy (45 days)
```text
=== BTCUSD (BTC) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 45d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 433 | Win/Loss: 293/140 | WR: 67.7%
Expectancy: +0.566R | PF: 2.75
Simple additive return (no compounding): +490.25%
Naive compounded return (assumes trades never overlap): +11162.28% | MaxDD: -34.51%
Concurrency: max 38 / avg 1.97 open trades at once ⚠️ OVERLAPPING TRADES — compounded return is unreliable, trust Simple_Additive_Return_Pct
Grade breakdown: {'A+': {'trades': 243, 'win_rate': '72.4%'}, 'A': {'trades': 146, 'win_rate': '60.3%'}, 'B+': {'trades': 44, 'win_rate': '65.9%'}}
Skipped (why signals were rejected): {'unknown_trend': 1055, 'mtf_conflict': 604, 'bias_conflict': 179, 'confidence': 191, 'grade': 0, 'kill_zone': 1858}
```

### 3. Walk-Forward Validation (45 days, 3 folds)
```text
=== Walk-forward: BTC-USD (folds are independent, non-overlapping time windows) ===
  Fold 1 (2026-06-19 to 2026-07-04): 148 trades | WR 64.2% | Exp +0.529R | Additive return +156.50% | max-concurrent 31
  Fold 2 (2026-07-04 to 2026-07-19): 177 trades | WR 65.5% | Exp +0.408R | Additive return +144.60% | max-concurrent 24
  Fold 3 (2026-07-19 to 2026-08-03): 108 trades | WR 75.9% | Exp +0.876R | Additive return +189.15% | max-concurrent 20
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.604, 'expectancy_std': 0.198, 'verdict': 'STABLE — edge holds up across independent folds'}
```

