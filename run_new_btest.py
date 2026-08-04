import sys
import logging
from backtesting.verify_hybrid_backtest import HybridVerifyBacktestAgent
from config.config import get_tradeable_symbols

logging.basicConfig(level=logging.WARNING)

def run_all():
    # To keep the runtime reasonable, we'll run the core representatives of each asset class,
    # plus the user's focus (Silver). The user can easily edit this list to run all of them.
    core_symbols = [
        'SI=F',        # Silver (The original btest.md focus)
        'GC=F',        # Gold
        'EURUSD=X',    # Forex Major
        'BTC-USD',     # Crypto (using yahoo ticker format)
    ]
    
    agent = HybridVerifyBacktestAgent()
    
    print(f"Starting verified backtest run for: {core_symbols}", flush=True)
    
    with open("btest.md", "w", encoding="utf-8") as f:
        f.write("# CRAVE Hybrid Strategy Backtest Report (Verified)\n\n")
        f.write("> **WARNING - SUPERSEDES ALL PREVIOUS REPORTS**\n")
        f.write("> This report was generated using the fully reconciled `verify_hybrid_backtest.py` harness, which strictly mirrors the live `HybridStrategyAgent` (15m timeframe, MTF confluence, partial-booking exits, and exact confidence gates).\n\n")
        f.write("> **Note**: Every test is compared against a Random Baseline (coin-flip direction with identical gates and partial-booking exits) to isolate the true edge from the exit model's structural skew.\n\n")
        
        for sym in core_symbols:
            try:
                print(f"Testing {sym}...", flush=True)
                
                f.write(f"## {sym}\n\n")
                
                # 1. Random Baseline
                print(f"  -> Random Baseline (45 days)...", flush=True)
                base_report = agent.run_random_baseline(sym, days=45, enforce_kill_zones=True)
                f.write("### 1. Random Baseline (45 days)\n```text\n")
                f.write(agent.format_report(base_report))
                f.write("\n```\n\n")
                
                # 2. Real Strategy
                print(f"  -> Real Strategy (45 days)...", flush=True)
                real_report = agent.run_backtest(sym, days=45, enforce_kill_zones=True)
                f.write("### 2. Real Strategy (45 days)\n```text\n")
                f.write(agent.format_report(real_report))
                f.write("\n```\n\n")
                
                # 3. Walk Forward
                print(f"  -> Walk Forward (45 days, 3 folds)...", flush=True)
                wf = agent.run_walk_forward(sym, total_days=45, folds=3, enforce_kill_zones=True)
                f.write("### 3. Walk-Forward Validation (45 days, 3 folds)\n```text\n")
                f.write(agent.format_walk_forward(wf))
                f.write("\n```\n\n")
                f.flush()
                
            except Exception as e:
                print(f"Error testing {sym}: {e}", flush=True)
                f.write(f"**Error testing {sym}:** {e}\n\n")
                
    print("btest.md has been regenerated.", flush=True)

if __name__ == "__main__":
    run_all()
