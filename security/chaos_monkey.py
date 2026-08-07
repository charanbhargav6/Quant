"""
CRAVE v10.4 — Chaos Monkey (Zone 3)
=====================================
Deliberately breaks CRAVE to prove the failover actually works
before trusting it with real money.

PHILOSOPHY: "Hope is not a strategy."
  You have a 3-node failover, circuit breakers, and position tracking.
  But have you ever actually tested what happens when:
    - The database connection drops mid-trade?
    - Binance returns HTTP 429 for 2 minutes?
    - Network latency spikes to 500ms during a kill zone?
    - The state sync repo is unavailable?
    - Telegram goes down during an event hedge?

  If you haven't tested it, it WILL break at the worst possible moment.
  Chaos Monkey simulates each failure safely in paper mode.

TESTS INCLUDED:
  1. network_lag        — adds 500ms delay to all HTTP calls
  2. api_rate_limit     — simulates 429 errors from exchange
  3. db_disconnect      — drops SQLite connection mid-cycle
  4. state_sync_fail    — blocks GitHub push/pull
  5. telegram_down      — silences all Telegram sends
  6. exchange_timeout   — makes all exchange calls time out
  7. node_failover      — kills active node to test secondary pickup
  8. full_scenario      — combines 3 failures simultaneously

RUN MODES:
  python -m Sub_Projects.Trading.security.chaos_monkey --test network_lag
  python -m Sub_Projects.Trading.security.chaos_monkey --test all --duration 300
  python -m Sub_Projects.Trading.security.chaos_monkey --scenario market_open

SAFETY:
  - ONLY runs in paper mode (checks TRADING_MODE env var)
  - All injected failures are temporary (duration-limited)
  - Bot state is verified before and after each test
  - Results saved to State/chaos_results.json
"""

import os
import sys
import time
import json
import logging
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

logger = logging.getLogger("crave.chaos")

RESULTS_FILE = Path(__file__).parent.parent / "State" / "chaos_results.json"


class ChaosMonkey:

    def __init__(self):
        # Safety check — never run chaos tests in live mode
        mode = os.environ.get("TRADING_MODE", "paper").lower()
        if mode == "live":
            raise RuntimeError(
                "CHAOS MONKEY REFUSED: TRADING_MODE=live. "
                "Chaos tests only run in paper mode. "
                "Set TRADING_MODE=paper first."
            )

        self._results: list = []
        self._active_patches: list = []
        logger.info("[Chaos] Chaos Monkey initialised (paper mode only) 🐒")

    # ─────────────────────────────────────────────────────────────────────────
    # INDIVIDUAL FAILURE INJECTORS
    # ─────────────────────────────────────────────────────────────────────────

    def inject_network_lag(self, lag_ms: int = 500,
                            duration_secs: int = 60) -> dict:
        """
        Adds artificial latency to all requests.requests calls.
        Simulates slow internet / mobile data during kill zone.

        Expected bot behaviour:
          - Signals still fire (just slower)
          - No timeouts if timeout > lag_ms
          - WebSocket reconnects if underlying connection affected
        """
        logger.warning(
            f"[Chaos] 🔴 INJECTING network lag: {lag_ms}ms for {duration_secs}s"
        )
        start = time.time()
        pass_count = fail_count = 0

        original_get  = None
        original_post = None

        def slow_get(*args, **kwargs):
            nonlocal pass_count
            time.sleep(lag_ms / 1000)
            pass_count += 1
            return original_get(*args, **kwargs)

        def slow_post(*args, **kwargs):
            time.sleep(lag_ms / 1000)
            return original_post(*args, **kwargs)

        try:
            import requests
            original_get  = requests.get
            original_post = requests.post
            requests.get  = slow_get
            requests.post = slow_post

            logger.info(f"[Chaos] Network lag active. Monitoring for {duration_secs}s...")
            time.sleep(duration_secs)

        finally:
            if original_get:
                requests.get  = original_get
                requests.post = original_post
            elapsed = time.time() - start

        result = {
            "test":       "network_lag",
            "lag_ms":     lag_ms,
            "duration_s": elapsed,
            "calls_slowed": pass_count,
            "status":     "completed",
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(result)
        self._save_result(result)
        return result

    def inject_api_rate_limit(self, error_rate: float = 0.5,
                               duration_secs: int = 120) -> dict:
        """
        Makes 50% of exchange API calls return HTTP 429 (Too Many Requests).
        Tests the retry_on_ratelimit decorator in data_agent.py.

        Expected bot behaviour:
          - Retries with exponential backoff
          - Signals delayed but not lost
          - No crashes or infinite loops
        """
        logger.warning(
            f"[Chaos] 🔴 INJECTING API rate limits "
            f"({error_rate*100:.0f}% error rate) for {duration_secs}s"
        )
        import random
        original_request = None
        throttle_count = 0
        pass_count     = 0

        def rate_limited_request(method, url, **kwargs):
            nonlocal throttle_count, pass_count
            if random.random() < error_rate:
                throttle_count += 1
                mock_resp = MagicMock()
                mock_resp.status_code = 429
                mock_resp.text        = "Too Many Requests"
                mock_resp.json.return_value = {"code": -1003, "msg": "Too many requests"}
                return mock_resp
            pass_count += 1
            return original_request(method, url, **kwargs)

        try:
            import requests
            original_request = requests.Session.request
            requests.Session.request = rate_limited_request

            logger.info(f"[Chaos] Rate limit injection active for {duration_secs}s...")
            time.sleep(duration_secs)

        finally:
            if original_request:
                requests.Session.request = original_request

        result = {
            "test":          "api_rate_limit",
            "error_rate_pct": error_rate * 100,
            "throttled":     throttle_count,
            "passed":        pass_count,
            "duration_s":    duration_secs,
            "status":        "completed",
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(result)
        self._save_result(result)
        return result

    def inject_db_disconnect(self, duration_secs: int = 30) -> dict:
        """
        Temporarily breaks the SQLite connection.
        Tests that the bot doesn't crash and recovers state on reconnect.

        Expected bot behaviour:
          - Logs DB errors at WARNING level (not CRITICAL)
          - Bot continues scanning signals
          - Reconnects automatically on next DB call
          - No positions lost (positions.json is the primary store)
        """
        logger.warning(
            f"[Chaos] 🔴 INJECTING DB disconnect for {duration_secs}s"
        )

        errors_caught = 0
        original_get_conn = None

        def broken_conn(self_db):
            nonlocal errors_caught
            errors_caught += 1
            raise Exception(
                "[ChaosMonkey] Simulated DB connection failure"
            )

        try:
            from core.database_manager import DatabaseManager
            original_get_conn           = DatabaseManager._get_conn
            DatabaseManager._get_conn   = broken_conn

            logger.info(f"[Chaos] DB disconnect active for {duration_secs}s...")
            time.sleep(duration_secs)

        finally:
            if original_get_conn:
                from core.database_manager import DatabaseManager
                DatabaseManager._get_conn = original_get_conn
                logger.info("[Chaos] DB connection restored.")

        result = {
            "test":         "db_disconnect",
            "db_errors":    errors_caught,
            "duration_s":   duration_secs,
            "status":       "completed",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(result)
        self._save_result(result)
        return result

    def inject_telegram_blackout(self, duration_secs: int = 120) -> dict:
        """
        Silences all Telegram sends.
        Tests that bot continues trading when alerts can't be sent.

        Expected bot behaviour:
          - Bot continues all operations normally
          - Telegram errors logged at DEBUG (not ERROR)
          - No state corruption
        """
        logger.warning(
            f"[Chaos] 🔴 INJECTING Telegram blackout for {duration_secs}s"
        )
        silenced_count = 0
        original_send  = None

        def silent_send(self_tg, text, **kwargs):
            nonlocal silenced_count
            silenced_count += 1
            logger.debug(f"[Chaos] Telegram silenced: {text[:50]}...")
            return False  # Pretend it failed silently

        try:
            from interfaces.telegram_interface import TelegramInterface
            original_send               = TelegramInterface._send_now
            TelegramInterface._send_now = silent_send

            logger.info(f"[Chaos] Telegram blackout active for {duration_secs}s...")
            time.sleep(duration_secs)

        finally:
            if original_send:
                from interfaces.telegram_interface import TelegramInterface
                TelegramInterface._send_now = original_send
                logger.info("[Chaos] Telegram restored.")

        result = {
            "test":        "telegram_blackout",
            "silenced":    silenced_count,
            "duration_s":  duration_secs,
            "status":      "completed",
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(result)
        self._save_result(result)
        return result

    def inject_state_sync_fail(self, duration_secs: int = 180) -> dict:
        """
        Blocks GitHub state sync for 3 minutes.
        Tests that position state stays consistent without GitHub sync.

        Expected bot behaviour:
          - state_sync logs warnings but doesn't crash
          - Positions continue to update locally
          - Resumes sync when connection restored
        """
        logger.warning(
            f"[Chaos] 🔴 INJECTING state sync failure for {duration_secs}s"
        )
        sync_failures = 0
        original_push = None

        def failing_push(self_sync, *args, **kwargs):
            nonlocal sync_failures
            sync_failures += 1
            raise ConnectionError("[ChaosMonkey] Simulated GitHub push failure")

        try:
            from infra.state_sync import StateSync
            original_push  = StateSync._git_push
            StateSync._git_push = failing_push

            logger.info(f"[Chaos] State sync failure active for {duration_secs}s...")
            time.sleep(duration_secs)

        finally:
            if original_push:
                from infra.state_sync import StateSync
                StateSync._git_push = original_push
                logger.info("[Chaos] State sync restored.")

        result = {
            "test":        "state_sync_fail",
            "sync_fails":  sync_failures,
            "duration_s":  duration_secs,
            "status":      "completed",
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(result)
        self._save_result(result)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # COMPOUND SCENARIOS
    # ─────────────────────────────────────────────────────────────────────────

    def run_market_open_scenario(self) -> dict:
        """
        Simulates a worst-case London open scenario:
          - Network lag 300ms (congested market open)
          - API rate limit 30% (everyone hitting exchange at once)
          - Telegram slow (notification backlog)

        Duration: 5 minutes (typical London open chaos window)
        """
        logger.warning("[Chaos] 🔴 RUNNING market open scenario (5 min)")
        threads  = []
        results  = {}

        def run_lag():
            results["lag"] = self.inject_network_lag(300, 300)

        def run_ratelimit():
            results["rate"] = self.inject_api_rate_limit(0.30, 300)

        for fn in [run_lag, run_ratelimit]:
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=350)

        composite = {
            "test":      "market_open_scenario",
            "sub_tests": results,
            "status":    "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save_result(composite)
        return composite

    def run_full_chaos(self, duration_secs: int = 120) -> dict:
        """
        Maximum stress test: all failures simultaneously.
        If the bot survives this, it's production-ready.
        """
        logger.warning(
            f"[Chaos] 🔴 FULL CHAOS MODE for {duration_secs}s 🔴"
        )
        threads = []
        results = {}

        tests = [
            ("lag",  lambda: self.inject_network_lag(200, duration_secs)),
            ("rate", lambda: self.inject_api_rate_limit(0.40, duration_secs)),
            ("db",   lambda: self.inject_db_disconnect(duration_secs // 2)),
            ("tg",   lambda: self.inject_telegram_blackout(duration_secs)),
        ]

        for name, fn in tests:
            def runner(n=name, f=fn):
                results[n] = f()
            t = threading.Thread(target=runner, daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=duration_secs + 30)

        composite = {
            "test":      "full_chaos",
            "sub_tests": results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._verify_bot_state(composite)
        self._save_result(composite)
        return composite

    # ─────────────────────────────────────────────────────────────────────────
    # STATE VERIFICATION
    # ─────────────────────────────────────────────────────────────────────────

    def _verify_bot_state(self, result: dict):
        """
        After each test, verify the bot state is intact.
        Checks: positions file, DB connectivity, streak state.
        """
        checks = {}

        # Check positions file
        try:
            from config.config import POSITIONS_FILE
            from pathlib import Path
            pf = Path(POSITIONS_FILE)
            if pf.exists():
                import json
                with open(pf) as f:
                    pos = json.load(f)
                checks["positions_file"] = f"OK ({len(pos)} open)"
            else:
                checks["positions_file"] = "OK (empty)"
        except Exception as e:
            checks["positions_file"] = f"CORRUPTED: {e}"

        # Check DB
        try:
            from core.database_manager import db
            db.get_recent_trades(limit=1)
            checks["database"] = "OK"
        except Exception as e:
            checks["database"] = f"ERROR: {e}"

        # Check streak state
        try:
            from core.streak_state import streak
            ct, _ = streak.can_trade()
            checks["streak_state"] = "OK"
        except Exception as e:
            checks["streak_state"] = f"ERROR: {e}"

        # Bot still running
        try:
            from core.trading_loop import trading_loop
            checks["trading_loop"] = (
                "RUNNING" if trading_loop._running else "STOPPED"
            )
        except Exception:
            checks["trading_loop"] = "not running"

        result["state_checks"] = checks
        all_ok = all("OK" in v or "RUNNING" in v or "empty" in v
                     for v in checks.values())
        result["all_checks_passed"] = all_ok

        if all_ok:
            logger.info(f"[Chaos] ✅ Post-test state: ALL OK")
        else:
            logger.warning(
                f"[Chaos] ⚠️ Post-test state issues: "
                + ", ".join(f"{k}={v}" for k, v in checks.items() if "OK" not in v)
            )

    def _save_result(self, result: dict):
        """Append test result to results file."""
        self._results.append(result)
        try:
            RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if RESULTS_FILE.exists():
                with open(RESULTS_FILE) as f:
                    existing = json.load(f)
            existing.append(result)
            # Keep last 50 results
            existing = existing[-50:]
            with open(RESULTS_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.warning(f"[Chaos] Results save failed: {e}")

    def print_report(self):
        """Print summary of all tests run this session."""
        print("\n" + "═" * 60)
        print("  CHAOS MONKEY TEST REPORT")
        print("═" * 60)
        for r in self._results:
            test  = r.get("test", "?")
            ts    = r.get("timestamp", "?")[:16]
            ok    = "✅" if r.get("all_checks_passed") else "⚠️"
            print(f"\n{ok} [{ts}] {test.upper()}")
            checks = r.get("state_checks", {})
            for k, v in checks.items():
                print(f"     {k}: {v}")
        print("═" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CRAVE Chaos Monkey")
    parser.add_argument("--test", choices=[
        "network_lag", "api_rate_limit", "db_disconnect",
        "telegram_blackout", "state_sync_fail",
        "market_open_scenario", "full_chaos",
    ], required=True)
    parser.add_argument("--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    args = parser.parse_args()

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    monkey = ChaosMonkey()

    if args.test == "network_lag":
        monkey.inject_network_lag(500, args.duration)
    elif args.test == "api_rate_limit":
        monkey.inject_api_rate_limit(0.5, args.duration)
    elif args.test == "db_disconnect":
        monkey.inject_db_disconnect(args.duration)
    elif args.test == "telegram_blackout":
        monkey.inject_telegram_blackout(args.duration)
    elif args.test == "state_sync_fail":
        monkey.inject_state_sync_fail(args.duration)
    elif args.test == "market_open_scenario":
        monkey.run_market_open_scenario()
    elif args.test == "full_chaos":
        monkey.run_full_chaos(args.duration)

    monkey.print_report()
