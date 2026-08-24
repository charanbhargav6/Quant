"""Read-only end-to-end runtime verifier.

Usage:
  python tools/verify_runtime.py              # static/config/database checks
  python tools/verify_runtime.py --probe      # additionally probes configured
                                               # broker/data sessions without orders

The probe deliberately never calls order_send, create_order, or any close method.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def present(name: str) -> str:
    return "set" if os.environ.get(name) else "unset"


def check_dependencies() -> dict[str, str]:
    names = [
        "MetaTrader5", "ccxt", "kiteconnect", "cryptography",
        "xgboost", "sklearn", "torch", "google.genai", "openai",
    ]
    return {name: ("installed" if importlib.util.find_spec(name) else "missing") for name in names}


def db_snapshot() -> dict:
    try:
        from core.database_manager import get_db
        db = get_db()
        columns = [row["name"] for row in db.query("PRAGMA table_info(accounts)")]
        optional = [
            "broker", "profile", "process_status", "pid", "port",
            "last_health_check", "restart_count", "last_error",
        ]
        available_optional = [name for name in optional if name in columns]
        select_cols = ["id", "account_type", "login", "server", "status", "strategies_enabled"] + available_optional
        rows = db.query(f"SELECT {', '.join(select_cols)} FROM accounts ORDER BY id")
        safe = []
        for row in rows:
            safe.append({
                k: (str(v)[:6] + "…" if k == "login" and v else v)
                for k, v in row.items()
            })
        return {"columns": columns, "accounts": safe}
    except Exception as exc:
        return {"error": f"database check failed: {type(exc).__name__}: {exc}"}


def strategy_snapshot() -> dict:
    try:
        from core.strategy_registry import STRATEGY_DEFS
        return {
            meta["id"]: {
                "live_ready": bool(meta.get("live_ready", False)),
                "paper_only": bool(meta.get("paper_only", False)),
                "instruments": meta.get("instruments", []),
            }
            for meta in STRATEGY_DEFS
        }
    except Exception as exc:
        return {"error": f"strategy registry check failed: {type(exc).__name__}: {exc}"}


def probe_mt5() -> dict:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"available": False, "reason": "MetaTrader5 package missing"}
    login = os.environ.get("MT5_LOGIN")
    server = os.environ.get("MT5_SERVER")
    if not login or not server:
        return {"available": False, "reason": "MT5_LOGIN or MT5_SERVER unset"}
    try:
        if not mt5.initialize():
            return {"available": False, "reason": f"initialize failed: {mt5.last_error()}"}
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            return {"available": False, "reason": f"account_info failed: {mt5.last_error()}"}
        identity_ok = int(info.login) == int(login) and str(info.server).lower() == str(server).lower()
        symbols = {}
        for symbol in ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "ETHUSD"]:
            item = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
            symbols[symbol] = {
                "exists": item is not None,
                "visible": bool(item.visible) if item else False,
                "tick_available": tick is not None,
            }
        result = {
            "available": True,
            "identity_ok": identity_ok,
            "reported_login": int(info.login),
            "reported_server": str(info.server),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "currency": str(info.currency),
            "trade_allowed": bool(getattr(info, "trade_allowed", False)),
            "symbols": symbols,
        }
        mt5.shutdown()
        return result
    except Exception as exc:
        try:
            mt5.shutdown()
        except Exception:
            pass
        return {"available": False, "reason": f"probe failed: {type(exc).__name__}: {exc}"}


def probe_binance() -> dict:
    try:
        import requests
        response = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=8,
        )
        public = {
            "available": response.status_code == 200,
            "status_code": response.status_code,
            "btc_price_present": bool(response.json().get("price")) if response.status_code == 200 else False,
        }
    except Exception as exc:
        public = {"available": False, "reason": f"public probe failed: {type(exc).__name__}: {exc}"}
    public["authenticated_keys_present"] = bool(os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET"))
    return public


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="run non-trading broker/data probes")
    args = parser.parse_args()

    print("=== RUNTIME CONFIG ===")
    for key in [
        "TRADING_MODE", "BROKER_TYPE", "MT5_LOGIN", "MT5_SERVER",
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "ZERODHA_API_KEY",
        "ZERODHA_ACCESS_TOKEN", "ACCOUNT_ENCRYPTION_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "OPENROUTER_API_KEY", "COUNCIL_OPENAI_API_KEY",
    ]:
        print(f"{key}={present(key)}")
    print(f"root={ROOT}")
    print(f"checked_at={datetime.now(timezone.utc).isoformat()}")

    print("=== DEPENDENCIES ===")
    print(json.dumps(check_dependencies(), sort_keys=True))
    print("=== ACCOUNT DATABASE ===")
    print(json.dumps(db_snapshot(), default=str, sort_keys=True))
    print("=== STRATEGY REGISTRY ===")
    print(json.dumps(strategy_snapshot(), default=str, sort_keys=True))

    if args.probe:
        print("=== READ-ONLY MT5 PROBE ===")
        print(json.dumps(probe_mt5(), default=str, sort_keys=True))
        print("=== READ-ONLY BINANCE PUBLIC PROBE ===")
        print(json.dumps(probe_binance(), default=str, sort_keys=True))
    else:
        print("=== BROKER PROBES ===")
        print("not run; pass --probe to perform read-only checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
