"""
Indian Market Backtest Script — 1 Year Hybrid Strategy Execution on NIFTY/BANKNIFTY
"""
import sys, os, json, logging
sys.path.insert(0, os.getcwd())
os.environ.setdefault("CRAVE_SKIP_INIT", "1")
logging.basicConfig(level=logging.WARNING)

from backtesting.backtest_agent import BacktestAgent
from engines.hybrid_strategy import HybridStrategyAgent

def main():
    agent = BacktestAgent()
    agent.strategy = HybridStrategyAgent()
    
    # Configuration based on global optimization
    days = 60
    interval = "15m"
    min_conf = 55
    vp_bins = 50
    delta_threshold = 15
    sl_mult = 1.5
    rr = 1.5
    
    instruments = ["^NSEI", "^NSEBANK"]
    
    print("=" * 80)
    print(" INDIAN MARKET HYBRID STRATEGY BACKTEST (1 YEAR)")
    print("=" * 80)
    print(f" Parameters: Bins={vp_bins}, Delta={delta_threshold}%, SL={sl_mult}x, RR={rr}x")
    
    for symbol in instruments:
        print(f"\n[+] Backtesting {symbol} over {days} days...")
        # Since we use kwargs in hybrid_strategy, we can't easily pass them through 
        # the standard agent.run_backtest without modifying run_backtest.
        # Instead, we'll monkey-patch the hybrid strategy defaults for this run:
        
        original_analyze = agent.strategy.analyze_market_context
        
        def patched_analyze(s, df, i, fvg, ob, struct, macro="", **kwargs):
            kwargs["vp_bins"] = vp_bins
            kwargs["delta_threshold"] = delta_threshold
            return original_analyze(s, df, i, fvg, ob, struct, macro, **kwargs)
            
        agent.strategy.analyze_market_context = patched_analyze
        
        try:
            report = agent.run_backtest(symbol, days=days, min_confidence=min_conf, timeframe=interval)
            print(agent.format_report(report))
        except Exception as e:
            print(f" [!] Backtest failed for {symbol}: {e}")
            
        # restore
        agent.strategy.analyze_market_context = original_analyze

if __name__ == "__main__":
    main()
