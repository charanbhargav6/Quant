from backtesting.verify_hybrid_backtest import HybridVerifyBacktestAgent
from strategy_adapters import StructureAdapter, OrderFlowAdapter

agent = HybridVerifyBacktestAgent()

with open("docs/adapter_test.md", "w", encoding="utf-8") as f:
    f.write("Testing StructureAdapter on XAUUSD (Gold)...\n")
    r = agent.run_backtest("XAUUSD", days=45, enforce_kill_zones=True, adapter=StructureAdapter())
    f.write(agent.format_report(r) + "\n\n")

    f.write("Testing StructureAdapter on EURUSD=X (Forex)...\n")
    r2 = agent.run_backtest("EURUSD=X", days=45, enforce_kill_zones=True, adapter=StructureAdapter())
    f.write(agent.format_report(r2) + "\n\n")

    f.write("Testing OrderFlowAdapter on BTC-USD (Crypto)...\n")
    r3 = agent.run_backtest("BTC-USD", days=45, enforce_kill_zones=True, adapter=OrderFlowAdapter())
    f.write(agent.format_report(r3) + "\n\n")
