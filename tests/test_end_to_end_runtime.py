import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_database_initialization_applies_account_migration(tmp_path):
    from core.database_manager import DatabaseManager

    db = DatabaseManager(str(tmp_path / "fresh.db"))
    columns = {row["name"] for row in db.query("PRAGMA table_info(accounts)")}
    assert {"broker", "profile", "process_status", "pid", "port", "last_error"}.issubset(columns)


def test_broker_router_exposes_legacy_factory_alias():
    from brokers.broker_router import get_broker_router, get_router

    assert get_broker_router() is get_router()


def test_order_book_is_not_requested_for_non_binance_exchange():
    from core.data_agent import DataAgent

    agent = DataAgent.__new__(DataAgent)
    agent.binance = MagicMock()
    result = agent.get_order_book_imbalance("EURUSD=X", exchange="mt5")
    assert result["available"] is False
    agent.binance.fetch_order_book.assert_not_called()


def test_account_add_rejects_unsafe_profile_and_unconfirmed_live(monkeypatch):
    import core.account_endpoints_patch as patch
    from flask import Flask

    app = Flask(__name__)
    db = MagicMock()
    patch.register_account_routes(app, lambda: db, MagicMock())
    client = app.test_client()

    unsafe = client.post("/api/accounts/add", json={
        "profile": "../escape", "broker": "mt5", "login": 123,
        "password": "pw", "server": "Demo", "trading_mode": "paper",
    })
    assert unsafe.get_json()["status"] == "error"

    unconfirmed = client.post("/api/accounts/add", json={
        "profile": "demo", "broker": "mt5", "login": 123,
        "password": "pw", "server": "Demo", "trading_mode": "live",
    })
    assert unconfirmed.get_json()["status"] == "error"
    assert "confirm_live" in unconfirmed.get_json()["reason"]


def test_account_add_persists_only_after_verified_result(monkeypatch, tmp_path):
    import core.account_endpoints_patch as patch
    from flask import Flask

    app = Flask(__name__)
    db = MagicMock()
    db.query_one.return_value = None
    db.add_account.return_value = True
    db.query_one.side_effect = [None, {"id": 7}]
    db.execute.return_value = None

    monkeypatch.setattr(patch, "verify_and_connect", lambda *a, **k: {
        "ok": True, "balance": 3000.0, "equity": 2999.5,
        "currency": "USD", "server": "Demo-Server", "reason": None,
        "requires_oauth": False, "login_url": None,
    })
    monkeypatch.setattr(patch, "_write_profile_env", lambda *a, **k: None)
    monkeypatch.setattr(patch, "encrypt_secret", lambda value: "encrypted")

    patch.register_account_routes(app, lambda: db, MagicMock())
    result = app.test_client().post("/api/accounts/add", json={
        "type": "demo", "broker": "mt5", "profile": "safe_demo",
        "login": 123, "password": "pw", "server": "Demo-Server",
    })
    payload = result.get_json()
    assert payload["status"] == "success"
    assert payload["account"]["balance"] == 3000.0
    db.add_account.assert_called_once()
    assert db.add_account.call_args.args[4] == 3000.0


def test_account_add_does_not_persist_failed_verification(monkeypatch):
    import core.account_endpoints_patch as patch
    from flask import Flask

    app = Flask(__name__)
    db = MagicMock()
    monkeypatch.setattr(patch, "verify_and_connect", lambda *a, **k: {
        "ok": False, "balance": None, "equity": None,
        "currency": None, "server": None, "reason": "bad credentials",
        "requires_oauth": False, "login_url": None,
    })
    patch.register_account_routes(app, lambda: db, MagicMock())
    result = app.test_client().post("/api/accounts/add", json={
        "type": "demo", "broker": "mt5", "profile": "bad_demo",
        "login": 123, "password": "pw", "server": "Demo-Server",
    })
    assert result.get_json()["status"] == "error"
    db.add_account.assert_not_called()


def test_runtime_verifier_handles_fresh_database(tmp_path, monkeypatch):
    # This verifies the verifier's selection logic without touching a real broker.
    from tools import verify_runtime

    class FakeDB:
        def query(self, sql, params=()):
            if "PRAGMA table_info" in sql:
                return [{"name": n} for n in ["id", "account_type", "login", "password", "server", "status", "strategies_enabled"]]
            return []

    monkeypatch.setattr(verify_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(verify_runtime, "importlib", verify_runtime.importlib)
    monkeypatch.setattr("core.database_manager.get_db", lambda: FakeDB())
    snapshot = verify_runtime.db_snapshot()
    assert snapshot["accounts"] == []
