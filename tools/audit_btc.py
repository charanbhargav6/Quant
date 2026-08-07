import pandas as pd
from backtesting.verify_hybrid_backtest import HybridVerifyBacktestAgent
from config.config import get_asset_params

agent = HybridVerifyBacktestAgent()

params = get_asset_params('BTC-USD')
print(f"BTC sl_mult is: {params['sl_mult']}")
print(f"BTC min_conf is: {params.get('min_conf', 'not set')}")

report = agent.run_backtest('BTC-USD', days=60, enforce_kill_zones=True)

if "error" in report:
    print(report["error"])
else:
    trades = report.get("_trades", [])
    print(f"Total Signals Fired: {report.get('Signals', 0)}")
    print(f"Skipped breakdown: {report.get('Skipped_Breakdown', {})}")
    
    # Audit confidence
    confidences = [t["confidence"] for t in trades]
    if confidences:
        print(f"Avg Confidence: {sum(confidences)/len(confidences):.2f}")
        print(f"Min Conf: {min(confidences)}, Max Conf: {max(confidences)}")
    
    # Audit time distribution
    if trades:
        df_trades = pd.DataFrame(trades)
        df_trades['time'] = pd.to_datetime(df_trades['time'])
        df_trades['date'] = df_trades['time'].dt.date
        signals_per_day = df_trades.groupby('date').size()
        print(f"Avg signals/day: {signals_per_day.mean():.2f}")
        print(f"Max signals in one day: {signals_per_day.max()}")
        print(f"Days with 0 signals: {60 - len(signals_per_day)}")
