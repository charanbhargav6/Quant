import sys
import os
import pandas as pd
sys.path.insert(0, r'D:\Desktop\engine')

from backtesting.backtest_agent_v10 import BacktestAgentV10
from engines.hybrid_strategy import HybridStrategyAgent

agent = BacktestAgentV10()
agent.strategy = HybridStrategyAgent()

print("Running 60-day Hybrid Backtest for ^NSEI...")

report = agent.run_backtest('^NSEI', days=60, min_confidence=40, include_fees=True)
print(agent.format_report(report, include_monte_carlo=True))
