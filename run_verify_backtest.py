import sys
import os
import pandas as pd
sys.path.insert(0, r'D:\Desktop\engine')
sys.stdout.reconfigure(encoding='utf-8')

from backtesting.backtest_agent_v10 import BacktestAgentV10
from engines.hybrid_strategy import HybridStrategyAgent
from backtesting import backtest_agent

# Override gold check so HybridStrategy processes it
backtest_agent.GOLD_TICKERS = set()

agent = BacktestAgentV10()
agent.strategy = HybridStrategyAgent()

symbols = ['GC=F', 'EURUSD=X', 'BTC-USD']

print("Running 90-Day Verify Backtest (SMC + Order Flow)...")

for sym in symbols:
    print(f"\n{'='*50}\nTesting {sym}\n{'='*50}")
    try:
        report = agent.run_backtest(sym, days=90, min_confidence=35, include_fees=True)
        print(agent.format_report(report, include_monte_carlo=False))
    except Exception as e:
        print(f"Failed {sym}: {e}")
