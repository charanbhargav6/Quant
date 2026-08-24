import os
from pathlib import Path

import pandas as pd

from intelligence.agent_council import DirectorAgent, TradingCouncil
from intelligence.order_flow import check_delta_confirmation
from intelligence.strategy_evolver import StrategyEvolver


def test_director_fails_closed_without_model_response(monkeypatch):
    agent = DirectorAgent()
    monkeypatch.setattr(agent, "_call_llm", lambda *args, **kwargs: None)
    result = agent.verdict({"direction": "buy", "symbol": "XAUUSD=X", "entry": 1.0}, "")
    assert result["decision"] == "reject"


def test_open_trade_review_holds_without_model_response(monkeypatch):
    council = TradingCouncil()
    monkeypatch.setattr(council.director, "_call_llm", lambda *args, **kwargs: None)
    result = council.evaluate_open_trade(
        {"symbol": "XAUUSD=X", "direction": "buy", "entry": 1.0,
         "sl": 0.9, "live_price": 1.01, "pnl_r": 0.1}, {}
    )
    assert result["action"] == "hold"


def test_missing_volume_waits_instead_of_entering():
    df = pd.DataFrame({
        "open": [1.0] * 10,
        "high": [1.1] * 10,
        "low": [0.9] * 10,
        "close": [1.0] * 10,
    })
    result = check_delta_confirmation(df, [0.95, 1.05], "buy")
    assert result["signal"] == "WAIT"
    assert result["confirmed"] is False


def test_evolver_does_not_stage_heuristic_proposal_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_HEURISTIC_EVOLUTION", raising=False)
    evolver = StrategyEvolver()
    result = evolver._test_proposals([], {
        "win_rate": 20, "avg_r": -1, "max_drawdown_r": 10,
    })
    assert result is None
