import os

from brokers.broker_router import BrokerRouter
from core.paper_trading import PaperTradingEngine


def test_market_fill_has_directional_slippage():
    engine = PaperTradingEngine()
    buy = engine.simulate_fill(
        {"symbol": "EURUSD=X", "direction": "buy", "order_type": "market"},
        1.10000,
    )
    sell = engine.simulate_fill(
        {"symbol": "EURUSD=X", "direction": "sell", "order_type": "market"},
        1.10000,
    )
    assert buy["status"] if "status" in buy else "fill_price" in buy
    assert buy["fill_price"] > 1.10000
    assert sell["fill_price"] < 1.10000


def test_limit_fill_uses_direction_before_branch():
    engine = PaperTradingEngine()
    pending = engine.simulate_fill(
        {
            "symbol": "EURUSD=X",
            "direction": "buy",
            "order_type": "limit",
            "limit_price": 1.09000,
        },
        1.10000,
    )
    assert pending["status"] == "pending"

    filled = engine.simulate_fill(
        {
            "symbol": "EURUSD=X",
            "direction": "buy",
            "order_type": "limit",
            "limit_price": 1.11000,
        },
        1.10000,
    )
    assert filled["status"] == "filled"
    assert filled["fill_price"] == 1.11000


def test_paper_readiness_does_not_require_exchange_keys(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ZERODHA_API_KEY", raising=False)
    monkeypatch.delenv("MT5_LOGIN", raising=False)

    engine = PaperTradingEngine()
    engine._state = engine._default_state()
    ok, report = engine.check_readiness()

    assert not ok
    assert "Not required in paper mode" in report


def test_router_accepts_paper_emergency_close():
    result = BrokerRouter().execute(
        {"symbol": "EURUSD=X", "direction": "close", "trade_id": "missing"},
        1.10000,
        is_paper=True,
    )
    assert result["status"] == "failed"
    assert "not found" in result["reason"]
