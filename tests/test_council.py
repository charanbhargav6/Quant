"""Full end-to-end test of the Council debate with real LLM calls."""
import sys, os, time, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv('.env')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# Verify keys loaded
print("=" * 60)
print("PRE-FLIGHT CHECK")
print("=" * 60)
print(f"GEMINI_API_KEY: {'SET (' + os.environ.get('GEMINI_API_KEY', '')[:10] + '...)' if os.environ.get('GEMINI_API_KEY') else 'MISSING'}")
print(f"GROQ_API_KEY:   {'SET (' + os.environ.get('GROQ_API_KEY', '')[:10] + '...)' if os.environ.get('GROQ_API_KEY') else 'MISSING'}")
print(f"OPENROUTER_KEY: {'SET (' + os.environ.get('OPENROUTER_API_KEY', '')[:10] + '...)' if os.environ.get('OPENROUTER_API_KEY') else 'MISSING'}")

from intelligence.agent_council import get_council

signal = {
    "symbol": "XAUUSD",
    "direction": "buy",
    "entry": 2650.50,
    "grade": "A",
    "stop_loss": 2640.00,
    "take_profit": 2670.00,
}
context = {
    "rsi": 38.2,
    "adx": 32,
    "regime": "TRENDING_UP",
    "daily_bias": "BUY",
    "drawdown_pct": 1.8,
    "atr_ratio": 1.2,
    "session": "London",
}

print("\n" + "=" * 60)
print("INITIATING HEDGE FUND COUNCIL DEBATE")
print("=" * 60)

t0 = time.time()
council = get_council()
result = council.deliberate(signal, context)
total = time.time() - t0

print("\n" + "=" * 60)
print("FINAL RESULT")
print("=" * 60)
print(f"Decision:      {result['decision'].upper()}")
print(f"Reasoning:     {result['reasoning']}")
print(f"SL Multiplier: {result['sl_multiplier']}")
print(f"TP Multiplier: {result['tp_multiplier']}")
print(f"Debate Time:   {total:.1f}s")
print("=" * 60)

# Print Telegram message
print("\n--- TELEGRAM MESSAGE ---")
print(council.get_telegram_summary(result))
