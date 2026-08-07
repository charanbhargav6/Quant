import sys, os
sys.path.insert(0, os.path.abspath('D:/Desktop/engine'))
from backtesting.backtest_agent import BacktestAgent
bt = BacktestAgent()
for sym in ['BTCUSD', 'ETHUSD', 'SOLUSD']:
    print(f'\nRunning {sym}...')
    res = bt.run_backtest(sym, days=30, min_confidence=50, risk_per_trade=0.02)
    if 'error' not in res:
        print(f'{sym}: {res["Signals"]} trades | WR: {res["Win_Rate"]} | Return: {res["Total_Return"]} | Exp: {res["Expectancy_R"]}')
    else:
        print(f'{sym} failed: {res["error"]}')
