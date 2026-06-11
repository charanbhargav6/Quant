import sys
import os
sys.path.insert(0, r'D:\Desktop\engine')

from backtesting.backtest_agent_v10 import BacktestAgentV10

agent = BacktestAgentV10()

print("Running 30-day backtest on key instruments...")

symbols = ['GC=F', 'BTC-USD', '^NSEI']

for sym in symbols:
    print(f"\n--- Running backtest for {sym} ---")
    try:
        report = agent.run_backtest(sym, days=30, min_confidence=40, include_fees=True)
        print(agent.format_report(report, include_monte_carlo=False))
    except Exception as e:
        print(f"Failed {sym}: {e}")

