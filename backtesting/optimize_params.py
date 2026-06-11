"""
Fast Parameter Optimizer — Pre-compute signals once, sweep params on results.
"""
import sys, os, json, logging
sys.path.insert(0, os.getcwd())
os.environ.setdefault("CRAVE_SKIP_INIT", "1")
logging.basicConfig(level=logging.WARNING)

import numpy as np
import pandas as pd
from backtesting.backtest_agent import BacktestAgent, resolve_symbol, _wilder_atr

agent = BacktestAgent()

R_BY_OUTCOME = {
    "tp2": 2.0, "tp2_via_tp1": 2.0,
    "tp1_partial": 0.5, "tp1_then_be": 0.0,
    "sl": -1.0,
}


def precompute_signals(ticker, days):
    """Run analyze_market_context for every candle ONCE. Cache all signals."""
    t = resolve_symbol(ticker)
    warmup_extra = 60
    interval = "1d" if days > 60 else "1h"
    df = agent.fetch_data_yfinance(ticker, days, interval, warmup_extra)
    
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
    
    signals = []
    lookahead = 20
    
    sys.stdout.write(f"  Pre-computing signals ({sig_start} -> {len(df)-lookahead})...\n")
    sys.stdout.flush()
    
    for i in range(sig_start, len(df) - lookahead):
        ctx = agent.strategy.analyze_market_context(t, df, i-1, fvg, ob, struct)
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
            "i": i,
            "conf": conf,
            "grade": grade,
            "direction": direction,
            "entry": entry,
            "atr": atr,
            "future": future,
        })
    
    sys.stdout.write(f"  Found {len(signals)} raw signals\n")
    sys.stdout.flush()
    return signals, df


def evaluate_params(signals, sl_mult, rr, min_grade, min_conf):
    """Score a set of params against pre-computed signals."""
    allowed = {"A+": ["A+"], "A": ["A+", "A"], "B+": ["A+", "A", "B+"]}
    
    r_multiples = []
    total = wins = 0
    risk = 0.01
    
    for s in signals:
        if s["conf"] < min_conf:
            continue
        if s["grade"] not in allowed.get(min_grade, []):
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
    
    if total < 3 or not r_multiples:
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
    
    # Score: return * win_rate_factor / drawdown
    score = ret * (wr / 100) / max(dd, 0.5) if ret > 0 else ret * 0.01
    
    return {
        "trades": total, "return_pct": round(ret, 2), "win_rate": round(wr, 1),
        "expectancy": round(r_arr.mean(), 3), "pf": round(pf, 2),
        "max_dd": round(dd, 2), "score": round(score, 2),
        "sl_mult": sl_mult, "rr": rr, "min_grade": min_grade, "min_conf": min_conf,
    }


# ── Instruments to optimize ──────────────────────────────────────────────
INSTRUMENTS = {
    "BTC-USD":   {"days": 30,  "sl_mult": [1.0, 1.2, 1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "ETH-USD":   {"days": 30,  "sl_mult": [1.0, 1.2, 1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "SOL-USD":   {"days": 30,  "sl_mult": [1.0, 1.2, 1.5, 2.0, 2.5], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "GC=F":      {"days": 90,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "XAGUSD=X":  {"days": 60,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "EURUSD=X":  {"days": 90,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "GBPUSD=X":  {"days": 90,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "USDJPY=X":  {"days": 90,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
    "AUDUSD=X":  {"days": 90,  "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "rr": [1.5, 2.0, 2.5, 3.0], "min_grade": ["B+", "A"], "min_conf": [35, 40, 45, 50]},
}

all_results = {}

for ticker, cfg in INSTRUMENTS.items():
    sys.stdout.write(f"\n{'='*60}\n  {ticker} ({cfg['days']}d)\n{'='*60}\n")
    sys.stdout.flush()
    
    signals, df = precompute_signals(ticker, cfg['days'])
    if signals is None or len(signals) == 0:
        sys.stdout.write(f"  [X] No signals\n"); sys.stdout.flush()
        continue
    
    best = None
    best_score = -999
    tested = 0
    
    for sm in cfg["sl_mult"]:
        for rr in cfg["rr"]:
            for mg in cfg["min_grade"]:
                for mc in cfg["min_conf"]:
                    r = evaluate_params(signals, sm, rr, mg, mc)
                    tested += 1
                    if r and r["score"] > best_score and r["return_pct"] > 0:
                        best_score = r["score"]
                        best = r
    
    # Also show top-3 alternatives
    top3 = []
    for sm in cfg["sl_mult"]:
        for rr in cfg["rr"]:
            for mg in cfg["min_grade"]:
                for mc in cfg["min_conf"]:
                    r = evaluate_params(signals, sm, rr, mg, mc)
                    if r and r["return_pct"] > 0:
                        top3.append(r)
    
    top3.sort(key=lambda x: x["score"], reverse=True)
    
    if best:
        sys.stdout.write(
            f"  [OK] BEST: sl={best['sl_mult']} rr={best['rr']} "
            f"grade>={best['min_grade']} conf>={best['min_conf']}%\n"
            f"     Trades={best['trades']} WR={best['win_rate']}% "
            f"Return={best['return_pct']}% PF={best['pf']} DD={best['max_dd']}%\n"
        )
        if len(top3) > 1:
            sys.stdout.write(f"  Runner-up:\n")
            for alt in top3[1:3]:
                sys.stdout.write(
                    f"     sl={alt['sl_mult']} rr={alt['rr']} "
                    f"grade>={alt['min_grade']} conf>={alt['min_conf']}% -> "
                    f"Ret={alt['return_pct']}% WR={alt['win_rate']}% "
                    f"Trades={alt['trades']}\n"
                )
        all_results[ticker] = best
    else:
        sys.stdout.write(f"  [X] No profitable config ({tested} tested)\n")
    sys.stdout.flush()


# ── SUMMARY ──────────────────────────────────────────────────────────────
sys.stdout.write(f"\n\n{'='*80}\n  OPTIMIZATION SUMMARY\n{'='*80}\n")
sys.stdout.write(f"{'Ticker':<12} {'SL':>5} {'RR':>5} {'Grade':>6} {'Conf':>5} "
                 f"{'Trades':>7} {'WR%':>6} {'Return':>8} {'PF':>6} {'DD':>6}\n")
sys.stdout.write("-" * 80 + "\n")

for ticker, b in all_results.items():
    sys.stdout.write(f"{ticker:<12} {b['sl_mult']:>5} {b['rr']:>5} {b['min_grade']:>6} "
                     f"{b['min_conf']:>5} {b['trades']:>7} {b['win_rate']:>5}% "
                     f"{b['return_pct']:>7}% {b['pf']:>5} {b['max_dd']:>5}%\n")

with open("Sub_Projects/Trading/optimization_results.json", "w") as f:
    json.dump(all_results, f, indent=2)

sys.stdout.write(f"\n[OK] Saved to optimization_results.json\n")
sys.stdout.flush()
