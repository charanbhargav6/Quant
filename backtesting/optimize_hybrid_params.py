"""
Hybrid Strategy Parameter Optimizer — Pre-compute signals across bins & deltas, sweep params.
"""
import sys, os, json, logging
sys.path.insert(0, os.getcwd())
os.environ.setdefault("CRAVE_SKIP_INIT", "1")
logging.basicConfig(level=logging.WARNING)

import numpy as np
import pandas as pd
from backtesting.backtest_agent import BacktestAgent, resolve_symbol, _wilder_atr
from engines.hybrid_strategy import HybridStrategyAgent

agent = BacktestAgent()
agent.strategy = HybridStrategyAgent()

R_BY_OUTCOME = {
    "tp2": 2.0, "tp2_via_tp1": 2.0,
    "tp1_partial": 0.5, "tp1_then_be": 0.0,
    "sl": -1.0,
}

def precompute_signals(ticker, days, vp_bins_list, delta_thresholds):
    """Run Hybrid analyze_market_context for every candle for each combination of bins/delta."""
    t = resolve_symbol(ticker)
    interval = "1h"
    warmup_extra = 60
    # To avoid yfinance API failure, if 180 days is requested, we try 180 days
    try:
        df = agent.fetch_data_yfinance(ticker, days, interval, warmup_extra)
    except Exception as e:
        sys.stdout.write(f"  [X] Failed fetching data: {e}\n")
        return None, None
        
    if df is None or len(df) < 250:
        return None, None
    
    df['atr'] = _wilder_atr(df, 14)
    df = agent._attach_indicators(df)
    
    fvg = agent.strategy._build_fvg_catalog(df)
    ob  = agent.strategy._build_ob_catalog(df)
    struct = agent.strategy._build_structure(df)
    
    test_cutoff = df['time'].iloc[-1] - pd.Timedelta(days=days)
    test_idx = df[df['time'] >= test_cutoff].index[0]
    sig_start = max(test_idx, 200)
    
    signals_dict = {}
    lookahead = 20
    
    sys.stdout.write(f"  Pre-computing signals ({sig_start} -> {len(df)-lookahead}) for {len(vp_bins_list)*len(delta_thresholds)} combinations...\n")
    sys.stdout.flush()
    
    for bins in vp_bins_list:
        for delta in delta_thresholds:
            signals = []
            for i in range(sig_start, len(df) - lookahead):
                ctx = agent.strategy.analyze_market_context(
                    t, df, i-1, fvg, ob, struct, vp_bins=bins, delta_threshold=delta
                )
                if "error" in ctx:
                    continue
                
                conf = ctx.get("Confidence_Pct", 0)
                score = ctx.get("Structure_Score", "C")
                trend = ctx.get("Macro_Trend", "Unknown")
                
                if trend == "Unknown":
                    continue
                
                grade = None
                for g in ("A+", "A", "B+"):
                    if g in score:
                        grade = g
                        break
                if not grade:
                    continue
                
                direction = "buy" if trend == "Bullish" else "sell"
                entry = df.iloc[i-1]['close']
                atr = df.iloc[i-1]['atr']
                if pd.isna(atr) or atr == 0:
                    continue
                
                future = df.iloc[i:i+lookahead]
                
                signals.append({
                    "i": i, "conf": conf, "grade": grade, "direction": direction,
                    "entry": entry, "atr": atr, "future": future,
                })
            signals_dict[(bins, delta)] = signals
            sys.stdout.write(f"    - (bins={bins}, delta={delta}%): found {len(signals)} raw signals\n")
            sys.stdout.flush()
            
    return signals_dict, df


def evaluate_params(signals, sl_mult, rr):
    """Score a set of params against pre-computed signals."""
    if not signals: return None
    
    r_multiples = []
    total = wins = 0
    risk = 0.01
    
    for s in signals:
        # Hybrid requires A or A+ grade, the precomputation already outputs this
        if s["grade"] not in ["A+", "A"]:
            continue
            
        total += 1
        entry = s["entry"]
        atr = s["atr"]
        direction = s["direction"]
        future = s["future"]
        
        if direction == "buy":
            sl = entry - atr*sl_mult
            tp1 = entry + atr*sl_mult
            tp2 = entry + atr*sl_mult*rr
        else:
            sl = entry + atr*sl_mult
            tp1 = entry - atr*sl_mult
            tp2 = entry - atr*sl_mult*rr
        
        outcome = None
        r_result = 0.0
        active_sl = sl
        tp1_hit = False
        
        for _, row in future.iterrows():
            if direction == "buy":
                if row['low'] <= active_sl:
                    outcome = "tp1_then_be" if tp1_hit else "sl"
                    r_result = R_BY_OUTCOME[outcome]; break
                if not tp1_hit and row['high'] >= tp1:
                    tp1_hit = True; active_sl = entry
                    outcome = "tp1_partial"; r_result = 0.5
                if row['high'] >= tp2:
                    outcome = "tp2_via_tp1" if tp1_hit else "tp2"
                    r_result = R_BY_OUTCOME[outcome]; break
            else:
                if row['high'] >= active_sl:
                    outcome = "tp1_then_be" if tp1_hit else "sl"
                    r_result = R_BY_OUTCOME[outcome]; break
                if not tp1_hit and row['low'] <= tp1:
                    tp1_hit = True; active_sl = entry
                    outcome = "tp1_partial"; r_result = 0.5
                if row['low'] <= tp2:
                    outcome = "tp2_via_tp1" if tp1_hit else "tp2"
                    r_result = R_BY_OUTCOME[outcome]; break
        
        if outcome is None:
            continue
        
        r_multiples.append(r_result)
        if r_result > 0: wins += 1
    
    if total < 2 or not r_multiples:
        return None
    
    r_arr = np.array(r_multiples)
    eq = r_arr * risk
    ret = ((1 + eq).prod() - 1) * 100
    wr = wins / total * 100
    gp = r_arr[r_arr > 0].sum()
    gl = abs(r_arr[r_arr < 0].sum())
    pf = gp / gl if gl > 0 else 999
    
    curve = np.cumprod(1 + eq) * 10000
    peak = np.maximum.accumulate(curve)
    dd = ((peak - curve) / peak * 100).max()
    
    score = ret * (wr / 100) / max(dd, 0.5) if ret > 0 else ret * 0.01
    
    return {
        "trades": total, "return_pct": round(ret, 2), "win_rate": round(wr, 1),
        "expectancy": round(r_arr.mean(), 3), "pf": round(pf, 2),
        "max_dd": round(dd, 2), "score": round(score, 2),
        "sl_mult": sl_mult, "rr": rr,
    }

INSTRUMENTS = {
    "BTC-USD":   {"days": 180, "vp_bins": [30, 50], "delta": [15, 20], "sl_mult": [1.0, 1.5], "rr": [1.5, 2.0]},
    "EURUSD=X":  {"days": 180, "vp_bins": [30, 50], "delta": [15, 20], "sl_mult": [1.0, 1.5], "rr": [1.5, 2.0]},
    "GC=F":      {"days": 180, "vp_bins": [30, 50], "delta": [15, 20], "sl_mult": [1.0, 1.5], "rr": [1.5, 2.0]},
}

all_results = {}

for ticker, cfg in INSTRUMENTS.items():
    sys.stdout.write(f"\n{'='*60}\n  {ticker} ({cfg['days']}d)\n{'='*60}\n")
    sys.stdout.flush()
    
    signals_dict, df = precompute_signals(ticker, cfg['days'], cfg['vp_bins'], cfg['delta'])
    if not signals_dict:
        sys.stdout.write(f"  [X] Failed to precompute signals\n"); sys.stdout.flush()
        continue
    
    best = None
    best_score = -999
    tested = 0
    
    for bins in cfg['vp_bins']:
        for delta in cfg['delta']:
            signals = signals_dict[(bins, delta)]
            if not signals: continue
            
            for sm in cfg["sl_mult"]:
                for rr in cfg["rr"]:
                    r = evaluate_params(signals, sm, rr)
                    tested += 1
                    if r and r["score"] > best_score and r["return_pct"] > 0:
                        best_score = r["score"]
                        r["vp_bins"] = bins
                        r["delta"] = delta
                        best = r

    if best:
        sys.stdout.write(
            f"  [OK] BEST: vp_bins={best['vp_bins']} delta={best['delta']}% "
            f"sl={best['sl_mult']} rr={best['rr']}\n"
            f"     Trades={best['trades']} WR={best['win_rate']}% "
            f"Return={best['return_pct']}% PF={best['pf']} DD={best['max_dd']}%\n"
        )
        all_results[ticker] = best
    else:
        sys.stdout.write(f"  [X] No profitable config ({tested} tested)\n")
    sys.stdout.flush()

sys.stdout.write(f"\n\n{'='*95}\n  HYBRID OPTIMIZATION SUMMARY\n{'='*95}\n")
sys.stdout.write(f"{'Ticker':<12} {'Bins':>5} {'Delta%':>6} {'SL':>5} {'RR':>5} "
                 f"{'Trades':>7} {'WR%':>6} {'Return':>8} {'PF':>6} {'DD':>6}\n")
sys.stdout.write("-" * 95 + "\n")

for ticker, b in all_results.items():
    sys.stdout.write(f"{ticker:<12} {b['vp_bins']:>5} {b['delta']:>6} {b['sl_mult']:>5} {b['rr']:>5} "
                     f"{b['trades']:>7} {b['win_rate']:>5}% "
                     f"{b['return_pct']:>7}% {b['pf']:>5} {b['max_dd']:>5}%\n")

with open("backtesting/hybrid_optimization_results.json", "w") as f:
    json.dump(all_results, f, indent=2)

sys.stdout.write(f"\n[OK] Saved to backtesting/hybrid_optimization_results.json\n")
sys.stdout.flush()
