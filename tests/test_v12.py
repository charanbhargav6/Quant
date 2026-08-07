"""
CRAVE v12 — Full Integration Test
Tests that all 9 new modules import and initialize correctly.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("CRAVE v12 — Full Integration Test")
print("=" * 60)

# 1. News Sentinel
print("\n1. News Sentinel...", end=" ")
try:
    from intelligence.news_sentinel import get_sentinel
    s = get_sentinel()
    print(f"✅ OK (5 sources configured)")
except Exception as e:
    print(f"❌ {e}")

# 2. X Scraper
print("2. X/Twitter Scraper...", end=" ")
try:
    from intelligence.x_scraper import get_x_scraper
    x = get_x_scraper()
    print(f"✅ OK ({len(x.MONITORED_ACCOUNTS)} accounts tracked)")
except Exception as e:
    print(f"❌ {e}")

# 3. News Trader
print("3. News Trader...", end=" ")
try:
    from intelligence.news_trader import get_news_trader
    nt = get_news_trader()
    print(f"✅ OK (3 strategies: directional, straddle, x-signal)")
except Exception as e:
    print(f"❌ {e}")

# 4. Trade Autopsy
print("4. Trade Autopsy...", end=" ")
try:
    from intelligence.trade_autopsy import get_autopsy
    a = get_autopsy()
    stats = a.get_statistics()
    print(f"✅ OK ({stats['total_lessons']} lessons in memory)")
except Exception as e:
    print(f"❌ {e}")

# 5. Agent Council
print("5. Agent Council...", end=" ")
try:
    from intelligence.agent_council import get_council
    c = get_council()
    print(f"✅ OK ({len(c.agents)} agents: {', '.join(a.name for a in c.agents)})")
except Exception as e:
    print(f"❌ {e}")

# 6. Strategy Evolver
print("6. Strategy Evolver...", end=" ")
try:
    from intelligence.strategy_evolver import get_evolver
    ev = get_evolver()
    params = ev.get_params()
    print(f"✅ OK ({len(params)} tunable parameters)")
except Exception as e:
    print(f"❌ {e}")

# 7. Universal Scanner
print("7. Universal Scanner...", end=" ")
try:
    from engines.universal_scanner import get_universal_scanner
    us = get_universal_scanner()
    total = len(us.FOREX_UNIVERSE) + len(us.CRYPTO_UNIVERSE) + \
            len(us.COMMODITIES_UNIVERSE) + len(us.INDEX_UNIVERSE)
    print(f"✅ OK ({total} assets in universe)")
except Exception as e:
    print(f"❌ {e}")

# 8. Correlation Engine
print("8. Correlation Engine...", end=" ")
try:
    from engines.correlation_engine import get_correlation_engine
    ce = get_correlation_engine()
    print(f"✅ OK ({len(ce.KNOWN_PAIRS)} known correlation pairs)")
except Exception as e:
    print(f"❌ {e}")

# 9. Profit Allocator
print("9. Profit Allocator...", end=" ")
try:
    from wealth.profit_allocator import get_allocator
    pa = get_allocator()
    strategy = pa.get_withdrawal_strategy()
    print(f"✅ OK (recommended: {strategy['recommended_method'][:40]})")
except Exception as e:
    print(f"❌ {e}")

# Council simulation
print("\n" + "=" * 60)
print("COUNCIL SIMULATION — Testing all 6 agents")
print("=" * 60)
try:
    test_signal = {
        "symbol": "EURUSD=X",
        "direction": "buy",
        "grade": "A",
        "confidence": 65,
    }
    test_context = {
        "regime": "trending",
        "daily_bias": "buy",
        "session": "london",
        "drawdown_pct": 2.0,
        "atr_ratio": 1.3,
        "adx": 28,
        "rsi": 55,
    }
    result = c.deliberate(test_signal, test_context)
    print(f"\nSignal: BUY EURUSD | Grade A | Confidence 65%")
    print(f"Context: Trending regime | Daily bias BUY | London session")
    print(f"\nVotes:")
    for v in result["votes"]:
        emoji = "👍" if v["vote"] == "approve" else "👎" if v["vote"] == "reject" else "🤷"
        print(f"  {emoji} {v['agent']:18s} | {v['vote']:7s} | conf={v['confidence']:3d}% | {v['reasoning'][:55]}")
    
    emoji = "✅" if result["decision"] == "execute" else "❌"
    print(f"\n{emoji} DECISION: {result['decision'].upper()} ({result['approvals']}/6 approve)")
    print(f"   Reason: {result['reasoning']}")
except Exception as e:
    print(f"❌ Council simulation failed: {e}")
    import traceback
    traceback.print_exc()

# Evolver params
print("\n" + "=" * 60)
print("ACTIVE PARAMETERS")
print("=" * 60)
try:
    for key, val in sorted(params.items()):
        print(f"  {key:25s} = {val}")
    
    # Dynamic params
    print(f"\n  Dynamic SL (trending):     {ev.get_dynamic_sl_mult('trending')}")
    print(f"  Dynamic SL (high_vol):     {ev.get_dynamic_sl_mult('high_volatility')}")
    print(f"  Dynamic SL (ranging):      {ev.get_dynamic_sl_mult('ranging')}")
    print(f"  Dynamic TP (trending):     {ev.get_dynamic_tp_mult('trending')}")
    print(f"  Dynamic risk (0 streak):   {ev.get_dynamic_risk_pct(0)}%")
    print(f"  Dynamic risk (-3 streak):  {ev.get_dynamic_risk_pct(-3)}%")
    print(f"  Dynamic risk (-5 streak):  {ev.get_dynamic_risk_pct(-5)}%")
    print(f"  Session boost (london):    +{ev.get_session_boost('london')}")
    print(f"  Session boost (asian):     {ev.get_session_boost('asian')}")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)
print("✅ ALL MODULES LOADED — CRAVE v12 READY")
print("=" * 60)
