from pathlib import Path

import pandas as pd

from engines.volatility_breakout_engine import VolatilityBreakoutEngine


ROOT = Path(__file__).resolve().parents[1]


def test_breakout_engine_accepts_real_xau_data():
    df = pd.read_parquet(ROOT / "data" / "XAUUSD_M15.parquet")
    result = VolatilityBreakoutEngine().analyze("XAUUSD=X", df.tail(500))
    assert "signal" in result
    assert "reason" in result
    if result["signal"]:
        assert result["signal"] in {"buy", "sell"}
        assert result["stop_loss"] > 0
        assert result["take_profit_1"] > 0
        assert result["take_profit_2"] > 0
        assert result["rr_ratio"] >= 1.5


def test_breakout_engine_can_find_and_validate_historical_candidates():
    df = pd.read_parquet(ROOT / "data" / "XAUUSD_M15.parquet")
    engine = VolatilityBreakoutEngine()
    signals = []
    for end in range(max(220, len(df) - 2000), len(df) + 1):
        result = engine.analyze("XAUUSD=X", df.iloc[:end])
        if result.get("signal"):
            signals.append(result)
    assert signals, "Expected at least one historical breakout candidate"
    assert all(s["strategy"] == "volatility_breakout_xau" for s in signals)
