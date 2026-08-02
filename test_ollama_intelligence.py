"""
OLLAMA (qwen2.5:14b) Intelligence Test Suite
=============================================
Tests the local AI's ability to:
  1. Basic connectivity
  2. Monitor open trades and flag geopolitical risk
  3. Dynamically adjust TP/SL based on geopolitical context
  4. Analyze a broken strategy and suggest a fix
  5. Create a new strategy from plain-English description
  6. Evaluate code quality of an existing strategy file
"""
import requests, json

OLLAMA_MODEL = "qwen2.5:14b"
OLLAMA_URL = "http://localhost:11434/api/chat"
BANNER = "=" * 60

def call_ollama(prompt: str, system: str = "") -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "options": {"temperature": 0.7}
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
        if resp.status_code != 200:
            return f"ERROR {resp.status_code}: {resp.text}"
        return resp.json()["message"]["content"].strip()
    except requests.exceptions.ConnectionError:
        return "ERROR: Ollama is not running on localhost:11434"
    except Exception as e:
        return f"ERROR: {str(e)}"

def run_test(num, title, prompt, system=""):
    print(f"\n{BANNER}")
    print(f"TEST {num}: {title}")
    print(BANNER)
    result = call_ollama(prompt, system)
    print(result)
    return result

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
JARVIS_SYSTEM = """You are Jarvis, the AI trading assistant for CRAVE Quant Engine.
You have deep expertise in algorithmic trading, SMC/ICT concepts, risk management, 
and financial markets. You monitor live positions and can recommend:
- Closing trades based on geopolitical events
- Extending or tightening TP/SL levels
- Identifying broken strategies from code review
- Writing new strategies from plain-English descriptions
Always be concise, professional, and data-driven."""

# ── TESTS ──────────────────────────────────────────────────────────────────────
run_test(1, "Basic Connectivity",
    "Say hello and briefly state your capabilities as a trading AI assistant.",
    JARVIS_SYSTEM)

open_position = {
    "symbol": "XAUUSD",
    "direction": "BUY",
    "entry": 2310.50,
    "current_price": 2335.20,
    "tp": 2370.00,
    "sl": 2285.00,
    "pnl_r": "+1.0R",
    "open_since": "6 hours"
}
geo_event = "BREAKING NEWS: US announces new military strikes in Middle East. Oil prices surging 4%. Safe-haven demand elevated but USD strengthening simultaneously."

run_test(2, "Geopolitical Trade Monitoring",
    f"""We have an open position:
{json.dumps(open_position, indent=2)}

Breaking geopolitical event:
{geo_event}

Analyze the situation and recommend: HOLD / CLOSE / EXTEND TP / TIGHTEN SL.
Give specific price levels for any adjustments. Explain your reasoning in 3-4 sentences.""",
    JARVIS_SYSTEM)

run_test(3, "TP Extension on Favorable Geopolitics",
    f"""Same position as before:
{json.dumps(open_position, indent=2)}

New context: The Federal Reserve unexpectedly paused rate hikes AND geopolitical tensions in Middle East 
escalated further. Gold historically surges 2-3% in these dual scenarios.

Should we extend the TP from 2370 to capture more upside? 
Suggest a new TP level with rationale. Also advise on whether to trail the SL to protect gains.""",
    JARVIS_SYSTEM)

broken_strategy_code = """
def generate_signal(df):
    # EMA crossover strategy
    df['ema_fast'] = df['close'].ewm(span=11).mean()
    df['ema_slow'] = df['close'].ewm(span=15).mean()
    
    # BUY when fast crosses above slow
    if df['ema_fast'].iloc[-1] > df['ema_slow'].iloc[-1]:
        return 'BUY'
    
    # BUY when fast crosses below slow (WRONG!)
    if df['ema_fast'].iloc[-1] < df['ema_slow'].iloc[-1]:
        return 'BUY'
    
    return None
"""

run_test(4, "Strategy Code Review — Detect Bugs",
    f"""Review this trading strategy code and identify all bugs, logic errors, and improvements:

```python
{broken_strategy_code}
```

List: (a) Critical bugs, (b) Logic errors, (c) Missing risk controls, (d) Corrected version of the code.""",
    JARVIS_SYSTEM)

run_test(5, "Create New Strategy from Description",
    """Create a complete Python strategy function based on this description:

'I want a strategy that trades Gold (XAUUSD) during the London session open (7:00-9:00 UTC). 
It should only trade when the price is above the daily VWAP, RSI is between 40-60 (not overbought/oversold), 
and the last candle's volume is 1.5x the 20-period average volume. 
Buy signal when price bounces off VWAP with a bullish engulfing pattern. 
SL at recent swing low, TP at 2R.'

Write the complete Python function with OHLCV DataFrame input. Include all indicators inline.""",
    JARVIS_SYSTEM)

backtest_stats = {
    "strategy": "EMA 11/15 Crossover",
    "period": "Jan 2024 - Jul 2026",
    "total_trades": 214,
    "win_rate": 57.2,
    "profit_factor": 1.48,
    "max_drawdown_pct": 6.1,
    "expectancy_r": 0.23,
    "worst_month": "March 2024 (-4.2%)",
    "best_month": "November 2024 (+8.7%)",
    "sharpe_ratio": 1.12
}

run_test(6, "Strategy Performance Deep Analysis",
    f"""Analyze these backtest statistics and give a comprehensive verdict:

{json.dumps(backtest_stats, indent=2)}

Answer: 
1. Is this strategy live-ready? (Yes/No/Conditional)
2. What are the 3 biggest risks?
3. What market regime does it fail in?
4. What one parameter change would most improve the Sharpe ratio?
5. At what point would you pull the plug on this strategy in live trading?""",
    JARVIS_SYSTEM)

print(f"\n{BANNER}")
print(f"ALL INTELLIGENCE TESTS COMPLETE")
print(f"Model used: {OLLAMA_MODEL}")
print(BANNER)
