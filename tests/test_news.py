import sys, os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

from intelligence.news_sentinel import get_sentinel

s = get_sentinel()
print("Fetching news from 5 sources...")
s._do_refresh()

news = s.get_recent_news(120)
print(f"\nHeadlines found: {len(news)}")
for n in news[:8]:
    sent = n["sentiment"]
    headline = n["headline"][:80]
    assets = ", ".join(n.get("assets", [])[:3])
    print(f"  [{sent:8s}] {headline}")
    print(f"             → Assets: {assets} | Impact: {n['impact']}")

cal = s._calendar
red = [e for e in cal if e.get("impact") == "high"]
print(f"\nCalendar: {len(cal)} events total, {len(red)} RED FOLDER")
for e in red[:5]:
    print(f"  🔴 {e['event']} ({e['currency']}) @ {e['time_utc'][:16]}")

# Test asset-specific sentiment
print("\n--- Asset Sentiment ---")
for sym in ["XAUUSD=X", "EURUSD=X", "BTCUSDT"]:
    sent = s.get_asset_sentiment(sym, max_age_mins=120)
    print(f"  {sym}: {sent['sentiment']} (score={sent['score']:.2f}, {sent['count']} headlines)")
