"""
Indian Market Optimization - 15m Timeframe
Finds the best Volume Profile Bins and Delta thresholds for NIFTY over a 60-day 15m sample.
"""
import sys, os, json, itertools, math
sys.path.insert(0, os.getcwd())
os.environ.setdefault("CRAVE_SKIP_INIT", "1")

from backtesting.backtest_agent import BacktestAgent
from engines.hybrid_strategy import HybridStrategyAgent

def run_grid():
    instruments = ["^NSEI", "^NSEBANK"]
    vp_bins_range = [30, 40, 50, 60]
    delta_range = [10, 15, 20]
    sl_range = [1.0, 1.5]
    rr_range = [1.5, 2.0]
    
    # Generate grid
    configs = list(itertools.product(vp_bins_range, delta_range))
    print(f"Total Volumetric Combinations per asset: {len(configs)}")
    
    agent = BacktestAgent()
    agent.strategy = HybridStrategyAgent()
    
    results = {}
    
    for symbol in instruments:
        print(f"\n" + "="*60)
        print(f"  {symbol} (60d @ 15m)")
        print("="*60)
        
        best_rar = -999
        best_cfg = None
        best_metrics = None
        
        for bins, delta in configs:
            original_analyze = agent.strategy.analyze_market_context
            
            def patched_analyze(s, d, i, fvg, ob, struct, macro="", **kwargs):
                kwargs["vp_bins"] = bins
                kwargs["delta_threshold"] = delta
                return original_analyze(s, d, i, fvg, ob, struct, macro, **kwargs)
                
            agent.strategy.analyze_market_context = patched_analyze
            
            # Run backtest - relies on internal data fetch
            report = agent.run_backtest(symbol, days=60, min_confidence=50, timeframe="15m")
            agent.strategy.analyze_market_context = original_analyze
            
            trades = report.get("total_trades", 0)
            wr = report.get("win_rate_pct", 0)
            ret = report.get("total_return_pct", 0)
            pf = report.get("profit_factor", 0)
            dd = report.get("max_drawdown_pct", 0)
            
            # Risk Adjusted Return (RAR)
            # If WR < 35% or trades < 5, penalize heavily
            if trades < 5 or wr < 35.0:
                rar = -999
            else:
                rar = ret / (dd + 1.0)
                
            print(f"    - (bins={bins}, delta={delta}%): Trades={trades}, WR={wr:.1f}%, Ret={ret:.2f}%, PF={pf:.2f}")
            
            if rar > best_rar:
                best_rar = rar
                best_cfg = (bins, delta)
                best_metrics = (trades, wr, ret, pf, dd)
                
        if best_cfg:
            b_bins, b_delta = best_cfg
            t, w, r, p, d = best_metrics
            print(f"  [OK] BEST: vp_bins={b_bins} delta={b_delta}%")
            print(f"       Trades={t} WR={w:.1f}% Return={r:.2f}% PF={p:.2f} DD={d:.2f}%")
            results[symbol] = {
                "vp_bins": b_bins,
                "delta": b_delta,
                "trades": t,
                "win_rate": w,
                "return": r,
                "profit_factor": p,
                "drawdown": d
            }
            
    with open("backtesting/india_15m_results.json", "w") as f:
        json.dump(results, f, indent=4)
    print("\n[OK] Saved to backtesting/india_15m_results.json")

if __name__ == "__main__":
    run_grid()
