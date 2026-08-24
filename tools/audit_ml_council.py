from pathlib import Path
import json
import os
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

print("=== ENVIRONMENT / MODELS ===")
for key in ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "NEWS_API_KEY", "GNEWS_API_KEY"]:
    print(f"{key}={'set' if os.environ.get(key) else 'unset'}")
for path in [ROOT / "ml/models/regime_classifier.pkl", ROOT / "ml/models/deep_regime_lstm.pth"]:
    print(f"{path.relative_to(ROOT)}={'present' if path.exists() else 'missing'}")

print("=== COUNCIL FALLBACK SAFETY ===")
from intelligence.agent_council import DirectorAgent
signal = {"direction": "buy", "symbol": "XAUUSD=X", "entry": 100.0}
verdict = DirectorAgent().verdict(signal, "No external model response")
print(json.dumps({k: verdict.get(k) for k in ["decision", "sl_multiplier", "tp_multiplier", "reasoning"]}, sort_keys=True))

print("=== ML REGIME STATUS / REAL DATA PREDICTIONS ===")
from ml.regime_classifier import regime_model
print(json.dumps(regime_model.get_status(), default=str, sort_keys=True))
for symbol in ["EURUSD", "GBPUSD", "XAUUSD"]:
    path = ROOT / "data" / f"{symbol}_M15.parquet"
    df = pd.read_parquet(path).tail(500).reset_index(drop=True)
    try:
        print(symbol, regime_model.predict(symbol + "=X", df), "rows", len(df))
    except Exception as exc:
        print(symbol, "ERROR", repr(exc))

print("=== ORDERFLOW PROXY ON REAL DATA ===")
from engines.orderflow_strategy import analyze_orderflow
for symbol in ["EURUSD", "GBPUSD", "XAUUSD"]:
    path = ROOT / "data" / f"{symbol}_M15.parquet"
    df = pd.read_parquet(path).tail(100).reset_index(drop=True)
    try:
        result = analyze_orderflow(df, len(df) - 1)
        print(symbol, json.dumps({k: result.get(k) for k in ["grade", "score", "direction", "delta_pct", "reason"]}, default=str, sort_keys=True))
    except Exception as exc:
        print(symbol, "ERROR", repr(exc))
