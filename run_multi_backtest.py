import sys
import os
sys.path.insert(0, r'D:\Desktop\engine')

from backtesting.backtest_agent_v10 import BacktestAgentV10

agent = BacktestAgentV10()
print('Running multi-market backtest (60 days, min_conf=50)...')
multi = agent.run_multi_market(days=60, min_confidence=50)
print(agent.format_multi_market(multi))
