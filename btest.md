# CRAVE Hybrid Strategy Backtest Report (Verified)

> **WARNING - SUPERSEDES ALL PREVIOUS REPORTS**
> This report was generated using the fully reconciled `verify_hybrid_backtest.py` harness, which strictly mirrors the live `HybridStrategyAgent` (15m timeframe, MTF confluence, partial-booking exits, and exact confidence gates).

> **Note**: Every test is compared against a Random Baseline (coin-flip direction with identical gates and partial-booking exits) to isolate the true edge from the exit model's structural skew.

## SI=F

### 1. Random Baseline (60 days)
```text
=== XAGUSD (Silver) — Random Baseline (Coin-flip direction) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 182 | Win/Loss: 112/70 | WR: 61.5%
Expectancy: +0.252R | Return: +135.62% | MaxDD: -21.87% | PF: 1.65
Grade breakdown: {'A+': {'trades': 76, 'win_rate': '61.8%'}, 'A': {'trades': 76, 'win_rate': '56.6%'}, 'B+': {'trades': 30, 'win_rate': '73.3%'}}
Skipped (why signals were rejected): {'unknown_trend': 1340, 'mtf_conflict': 666, 'bias_conflict': 107, 'confidence': 121, 'grade': 0, 'kill_zone': 1496}
```

### 2. Real Strategy (60 days)
```text
=== XAGUSD (Silver) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 235 | Win/Loss: 141/94 | WR: 60.0%
Expectancy: +0.287R | Return: +254.72% | MaxDD: -30.24% | PF: 1.72
Grade breakdown: {'A+': {'trades': 110, 'win_rate': '60.0%'}, 'A': {'trades': 77, 'win_rate': '49.4%'}, 'B+': {'trades': 48, 'win_rate': '77.1%'}}
Skipped (why signals were rejected): {'unknown_trend': 1340, 'mtf_conflict': 391, 'bias_conflict': 189, 'confidence': 261, 'grade': 0, 'kill_zone': 1496}
```

### 3. Walk-Forward Validation (60 days, 3 folds)
```text
=== Walk-forward: SI=F ===
  Fold 1 (0-20d ago): 84 trades | WR 61.9% | Exp +0.085R | Return +13.13%
  Fold 2 (20-40d ago): 141 trades | WR 64.5% | Exp +0.346R | Return +152.93%
  Fold 3 (40-60d ago): 235 trades | WR 60.0% | Exp +0.287R | Return +254.72%
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.239, 'expectancy_std': 0.112, 'verdict': 'STABLE — edge holds up across folds'}
```

## GC=F

### 1. Random Baseline (60 days)
```text
=== XAUUSD (Gold) — Random Baseline (Coin-flip direction) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 208 | Win/Loss: 110/98 | WR: 52.9%
Expectancy: +0.171R | Return: +88.82% | MaxDD: -27.77% | PF: 1.36
Grade breakdown: {'A+': {'trades': 29, 'win_rate': '75.9%'}, 'A': {'trades': 149, 'win_rate': '49.0%'}, 'B+': {'trades': 30, 'win_rate': '50.0%'}}
Skipped (why signals were rejected): {'unknown_trend': 2552, 'mtf_conflict': 296, 'bias_conflict': 34, 'confidence': 0, 'grade': 0, 'kill_zone': 822}
```

### 2. Real Strategy (60 days)
```text
=== XAUUSD (Gold) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 389 | Win/Loss: 199/190 | WR: 51.2%
Expectancy: +0.124R | Return: +128.38% | MaxDD: -53.03% | PF: 1.25
Grade breakdown: {'A+': {'trades': 53, 'win_rate': '77.4%'}, 'A': {'trades': 277, 'win_rate': '46.9%'}, 'B+': {'trades': 59, 'win_rate': '47.5%'}}
Skipped (why signals were rejected): {'unknown_trend': 2552, 'mtf_conflict': 96, 'bias_conflict': 53, 'confidence': 0, 'grade': 0, 'kill_zone': 822}
```

### 3. Walk-Forward Validation (60 days, 3 folds)
```text
=== Walk-forward: GC=F ===
  Fold 1 (0-20d ago): 140 trades | WR 57.9% | Exp +0.328R | Return +136.64%
  Fold 2 (20-40d ago): 251 trades | WR 50.2% | Exp +0.172R | Return +115.07%
  Fold 3 (40-60d ago): 389 trades | WR 51.2% | Exp +0.124R | Return +128.38%
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.208, 'expectancy_std': 0.087, 'verdict': 'STABLE — edge holds up across folds'}
```

## EURUSD=X

### 1. Random Baseline (60 days)
```text
=== EURUSD (Forex) — Random Baseline (Coin-flip direction) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 69 | Win/Loss: 40/29 | WR: 58.0%
Expectancy: +0.129R | Return: +17.06% | MaxDD: -12.62% | PF: 1.31
Grade breakdown: {'A+': {'trades': 7, 'win_rate': '71.4%'}, 'A': {'trades': 62, 'win_rate': '56.5%'}}
Skipped (why signals were rejected): {'unknown_trend': 2046, 'mtf_conflict': 555, 'bias_conflict': 145, 'confidence': 213, 'grade': 0, 'kill_zone': 1148}
```

### 2. Real Strategy (60 days)
```text
=== EURUSD (Forex) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 130 | Win/Loss: 74/56 | WR: 56.9%
Expectancy: +0.030R | Return: +4.51% | MaxDD: -25.78% | PF: 1.07
Grade breakdown: {'A+': {'trades': 8, 'win_rate': '62.5%'}, 'A': {'trades': 122, 'win_rate': '56.6%'}}
Skipped (why signals were rejected): {'unknown_trend': 2046, 'mtf_conflict': 168, 'bias_conflict': 255, 'confidence': 429, 'grade': 0, 'kill_zone': 1148}
```

### 3. Walk-Forward Validation (60 days, 3 folds)
```text
=== Walk-forward: EURUSD=X ===
  Fold 1 (0-20d ago): 55 trades | WR 63.6% | Exp +0.283R | Return +34.13%
  Fold 2 (20-40d ago): 97 trades | WR 58.8% | Exp +0.077R | Return +13.04%
  Fold 3 (40-60d ago): 130 trades | WR 56.9% | Exp +0.030R | Return +4.51%
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.13, 'expectancy_std': 0.11, 'verdict': 'STABLE — edge holds up across folds'}
```

## BTC-USD

### 1. Random Baseline (60 days)
```text
=== BTCUSD (BTC) — Random Baseline (Coin-flip direction) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 365 | Win/Loss: 236/129 | WR: 64.7%
Expectancy: +0.410R | Return: +1638.64% | MaxDD: -23.10% | PF: 2.16
Grade breakdown: {'A+': {'trades': 189, 'win_rate': '67.2%'}, 'A': {'trades': 143, 'win_rate': '58.7%'}, 'B+': {'trades': 33, 'win_rate': '75.8%'}}
Skipped (why signals were rejected): {'unknown_trend': 1371, 'mtf_conflict': 1146, 'bias_conflict': 111, 'confidence': 191, 'grade': 0, 'kill_zone': 2355}
```

### 2. Real Strategy (60 days)
```text
=== BTCUSD (BTC) — HybridStrategyAgent (SMC+OrderFlow, mirrors live trading_loop.py) ===
Period: 60d @ 15m | Gates: {'kill_zone': True, 'mtf_confluence': True, 'approx_daily_bias': True}
Signals: 570 | Win/Loss: 375/195 | WR: 65.8%
Expectancy: +0.455R | Return: +14259.37% | MaxDD: -36.51% | PF: 2.33
Grade breakdown: {'A+': {'trades': 334, 'win_rate': '67.4%'}, 'A': {'trades': 165, 'win_rate': '63.0%'}, 'B+': {'trades': 71, 'win_rate': '64.8%'}}
Skipped (why signals were rejected): {'unknown_trend': 1371, 'mtf_conflict': 720, 'bias_conflict': 179, 'confidence': 344, 'grade': 0, 'kill_zone': 2356}
```

### 3. Walk-Forward Validation (60 days, 3 folds)
```text
=== Walk-forward: BTC-USD ===
  Fold 1 (0-20d ago): 193 trades | WR 69.4% | Exp +0.735R | Return +1461.91%
  Fold 2 (20-40d ago): 374 trades | WR 70.9% | Exp +0.695R | Return +15163.67%
  Fold 3 (40-60d ago): 570 trades | WR 65.8% | Exp +0.455R | Return +14259.37%
Stability: {'folds_valid': 3, 'folds_profitable': 3, 'pct_folds_profitable': 100.0, 'expectancy_mean': 0.628, 'expectancy_std': 0.124, 'verdict': 'STABLE — edge holds up across folds'}
```

