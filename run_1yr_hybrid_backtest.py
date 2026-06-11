import sys
import os
import pandas as pd
sys.path.insert(0, r'D:\Desktop\engine')

from backtesting.backtest_agent_v10 import BacktestAgentV10
from engines.hybrid_strategy import HybridStrategyAgent
from backtesting import backtest_agent

# Override gold check so HybridStrategy processes it
backtest_agent.GOLD_TICKERS = set()

agent = BacktestAgentV10()
agent.strategy = HybridStrategyAgent()

symbols = ['GC=F', 'BTC-USD', 'EURUSD=X', '^NSEI']

print("Running 1-Year (365 days) Hybrid Backtest (SMC + Order Flow)...")
print("This may take 1-2 minutes per instrument.")

for sym in symbols:
    print(f"\n{'='*50}\nTesting {sym}\n{'='*50}")
    try:
        # Run 365 days, require 40 min confidence (which gets overridden by A/A+ grades in Hybrid)
        report = agent.run_backtest(sym, days=365, min_confidence=40, include_fees=True)
        # We can format the report and include Monte Carlo simulation
        print(agent.format_report(report, include_monte_carlo=True))
    except Exception as e:
        print(f"Failed {sym}: {e}")

