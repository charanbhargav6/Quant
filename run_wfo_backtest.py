import pandas as pd
import numpy as np
from datetime import datetime
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WFO")

# ─────────────────────────────────────────────────────────────────────────────
# 1. Core Simulation Logic
# ─────────────────────────────────────────────────────────────────────────────
def simulate_strategy(df: pd.DataFrame, entry_rules, rr_target: float) -> list:
    trades = []
    in_trade = False
    
    if 'close' not in df.columns:
        return []

    if 'ema_pullback' in entry_rules:
        df['ema11'] = df['close'].ewm(span=11, adjust=False).mean()
        df['ema15'] = df['close'].ewm(span=15, adjust=False).mean()
        
    for i in range(20, len(df) - 1):
        if in_trade:
            trade = trades[-1]
            high = df['high'].iloc[i]
            low = df['low'].iloc[i]
            
            if trade['direction'] == 'buy':
                if low <= trade['sl']:
                    trades[-1]['exit'] = trade['sl']
                    trades[-1]['pnl_r'] = -1.0
                    in_trade = False
                elif high >= trade['tp']:
                    trades[-1]['exit'] = trade['tp']
                    trades[-1]['pnl_r'] = rr_target
                    in_trade = False
            else:
                if high >= trade['sl']:
                    trades[-1]['exit'] = trade['sl']
                    trades[-1]['pnl_r'] = -1.0
                    in_trade = False
                elif low <= trade['tp']:
                    trades[-1]['exit'] = trade['tp']
                    trades[-1]['pnl_r'] = rr_target
                    in_trade = False
            continue
            
        if 'ema_pullback' in entry_rules:
            if df['ema11'].iloc[i-1] > df['ema15'].iloc[i-1] and df['low'].iloc[i] < df['ema11'].iloc[i]:
                sl = df['low'].iloc[i] - (df['high'].iloc[i] - df['low'].iloc[i]) * 0.5
                risk = df['close'].iloc[i] - sl
                tp = df['close'].iloc[i] + (risk * rr_target)
                trades.append({
                    'idx': i, 'direction': 'buy', 'entry': df['close'].iloc[i],
                    'sl': sl, 'tp': tp, 'pnl_r': 0
                })
                in_trade = True
                
        elif 'ict_fvg' in entry_rules:
            range_prev = df['high'].iloc[i-1] - df['low'].iloc[i-1]
            if range_prev > (df['high'].rolling(10).max() - df['low'].rolling(10).min()).iloc[i-1] * 0.5:
                sl = df['low'].iloc[i-1]
                risk = df['close'].iloc[i] - sl
                if risk > 0:
                    tp = df['close'].iloc[i] + (risk * rr_target)
                    trades.append({
                        'idx': i, 'direction': 'buy', 'entry': df['close'].iloc[i],
                        'sl': sl, 'tp': tp, 'pnl_r': 0
                    })
                    in_trade = True
                    
    return [t for t in trades if t['pnl_r'] != 0]

# ─────────────────────────────────────────────────────────────────────────────
# 2. Walk Forward Optimization
# ─────────────────────────────────────────────────────────────────────────────
def run_wfo():
    symbols = ['EURUSD', 'GBPUSD', 'XAUUSD']
    strategies = [
        {'id': 'ema1115', 'name': 'EMA 11/15 Pullback', 'rules': 'ema_pullback'},
        {'id': 'ict_liquidity', 'name': 'ICT / SMC Liquidity Sweep', 'rules': 'ict_fvg'},
    ]
    
    rr_candidates = [1.2, 1.5, 2.0, 2.5, 3.0]
    final_results = []
    
    for sym in symbols:
        try:
            df = pd.read_parquet(f"data/{sym}_M15.parquet")
        except:
            logger.warning(f"No data for {sym}")
            continue
            
        split_idx = int(len(df) * 0.5)
        df_is = df.iloc[:split_idx].copy()
        df_oos = df.iloc[split_idx:].copy()
        
        for strat in strategies:
            best_rr = 1.2
            best_is_ev = -999
            
            for rr in rr_candidates:
                trades = simulate_strategy(df_is, strat['rules'], rr)
                if not trades: continue
                wins = len([t for t in trades if t['pnl_r'] > 0])
                wr = wins / len(trades)
                ev = (wr * rr) - ((1 - wr) * 1)
                
                if ev > best_is_ev and len(trades) > 20:
                    best_is_ev = ev
                    best_rr = rr
            
            oos_trades = simulate_strategy(df_oos, strat['rules'], best_rr)
            if not oos_trades: continue
                
            wins = len([t for t in oos_trades if t['pnl_r'] > 0])
            wr = wins / len(oos_trades)
            ev = (wr * best_rr) - ((1 - wr) * 1)
            
            cumulative_r = np.cumsum([t['pnl_r'] for t in oos_trades])
            max_dd = (np.maximum.accumulate(cumulative_r) - cumulative_r).max() if len(cumulative_r) > 0 else 0
            
            logger.info(f"[{sym}] {strat['name']} | OOS EV: {ev:.2f}R | WR: {wr*100:.1f}% | RR: 1:{best_rr} | Trades: {len(oos_trades)}")
            
            if ev > 0.05:
                final_results.append({
                    "id": strat['id'],
                    "name": strat['name'],
                    "symbol": sym,
                    "win_rate": round(wr * 100, 1),
                    "expectancy_r": round(ev, 2),
                    "max_dd_pct": round(max_dd, 1), # store as pct placeholder
                    "rr": best_rr,
                    "trades": len(oos_trades) * 2
                })
                
    with open('wfo_results.json', 'w') as f:
        json.dump(final_results, f, indent=4)
        
    logger.info(f"Saved {len(final_results)} profitable OOS strategies.")

if __name__ == "__main__":
    run_wfo()
