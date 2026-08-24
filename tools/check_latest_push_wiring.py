"""Static/runtime wiring checks; never sends orders or probes broker credentials."""
from importlib import import_module
from pathlib import Path
import ast
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
results = []

def check(name, ok, detail):
    results.append({"check": name, "ok": bool(ok), "detail": detail})

for module in [
    "config.config", "core.database_manager", "core.account_connector",
    "core.account_endpoints_patch", "core.trading_loop", "brokers.broker_router",
    "brokers.binance_agent", "core.data_agent", "intelligence.agent_council",
    "core.strategy_registry", "add_xm_account",
]:
    try:
        import_module(module)
        check(f"import:{module}", True, "imported")
    except Exception as exc:
        check(f"import:{module}", False, f"{type(exc).__name__}: {exc}")

try:
    from brokers.broker_router import get_router, get_broker_router
    check("router alias", get_router is not None and get_broker_router() is get_router(), "legacy alias resolves same singleton")
except Exception as exc:
    check("router alias", False, repr(exc))

try:
    from core.strategy_registry import STRATEGY_DEFS
    ids = [d.get("id") for d in STRATEGY_DEFS]
    check("strategy IDs unique", len(ids) == len(set(ids)), json.dumps(ids))
    check("paper-only metadata", all("paper_only" in d for d in STRATEGY_DEFS if not d.get("live_ready")), "all non-live-ready definitions are explicit")
except Exception as exc:
    check("strategy registry", False, repr(exc))

try:
    source = (ROOT / "core" / "trading_loop.py").read_text(encoding="utf-8")
    start = source.index("    def _live_execute")
    block = source[start:start + 1200]
    body = block.split('"""', 2)[-1]
    check("central live router", "get_router" in body and "is_paper=False" in body and "ExecutionAgent" not in body, "live wrapper uses centralized router")
except Exception as exc:
    check("central live router", False, repr(exc))

try:
    source = (ROOT / "add_xm_account.py").read_text(encoding="utf-8")
    check("XM helper verified", "verify_and_connect" in source and 'status="verified"' in source, "verification precedes persistence")
    check("XM helper DB contract", "capital=float(result[\"balance\"])" in source and "strategies=strategies_json" in source, "matches DatabaseManager.add_account")
    check("XM helper no forced delete", "DELETE FROM accounts" not in source, "does not delete unrelated accounts")
except Exception as exc:
    check("XM helper", False, repr(exc))

try:
    from core.data_agent import _binance_market_symbol
    check("Binance symbol mapping", _binance_market_symbol("BTCUSDT") == "BTC/USDT:USDT" and _binance_market_symbol("BTC-USD") == "BTC/USDT:USDT", "USD-M symbols normalized")
except Exception as exc:
    check("Binance symbol mapping", False, repr(exc))

failed = [r for r in results if not r["ok"]]
print(json.dumps({"ok": not failed, "checks": results}, indent=2))
raise SystemExit(1 if failed else 0)
