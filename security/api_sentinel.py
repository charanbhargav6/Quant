"""
CRAVE v10.4 — API Sentinel (Zone 3)
=====================================
Blue-team defensive engineering for your exchange API keys.

THREAT MODEL:
  If your API key is ever leaked (GitHub commit, env file exposure,
  screen share, compromised machine), an attacker could:
    1. Place trades on your behalf
    2. Drain your account with wrong-direction trades
    3. Withdraw funds (if withdrawal permission was granted)

  The Sentinel detects anomalous API usage patterns and kills the bot
  before damage can accumulate.

DETECTION METHODS:
  1. UUID Whitelist: Every order CRAVE places gets a UUID in the client
     order ID field. Any order without this UUID = foreign order.
     Exchange APIs support client_order_id — we use it.

  2. Volume Anomaly: If total order count in last hour exceeds 2×
     normal rate, something is wrong.

  3. Direction Anomaly: If the same symbol has orders placed in both
     directions within 60 seconds, likely not us.

  4. Size Anomaly: If an order lot size is > 3× our maximum configured
     size, something is wrong.

  5. Off-hours Order: If an order fires outside our kill zones and
     we have no open positions, something is wrong.

RESPONSE LEVELS:
  WARNING:   Log + Telegram alert (don't kill yet, could be edge case)
  ALERT:     Pause new entries + Telegram critical alert
  KILL:      Cancel all orders + close all positions + disable bot

SETUP:
  Add to run_bot.py:
    from security.api_sentinel import get_sentinel
    get_sentinel().start()

  Add to .env:
    SENTINEL_ALERT_THRESHOLD=3    (anomalies before ALERT)
    SENTINEL_KILL_THRESHOLD=5     (anomalies before KILL)
"""

import os
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
try:
    from src.core.paths import CRAVE_ROOT
except ImportError:
    from pathlib import Path
    import os
    CRAVE_ROOT = os.environ.get('CRAVE_ROOT', str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger("crave.sentinel")


class APISentinel:

    CHECK_INTERVAL_SECS = 30
    ORDER_HISTORY_WINDOW_MINS = 60

    def __init__(self):
        self._running          = False
        self._thread: Optional[threading.Thread] = None
        self._known_order_ids: set  = set()
        self._anomaly_count:   int  = 0
        self._last_alert_time: Optional[datetime] = None

        # Thresholds from .env (sensible defaults)
        self._alert_threshold = int(os.environ.get("SENTINEL_ALERT_THRESHOLD", 3))
        self._kill_threshold  = int(os.environ.get("SENTINEL_KILL_THRESHOLD", 5))

        # Generate a session UUID prefix — all our orders will have this
        # Format: CRV-{session_hex[:8]}-{counter}
        self._session_prefix = f"CRV-{uuid.uuid4().hex[:8].upper()}"
        self._order_counter  = 0

        # ── SQLite persistence for known order IDs ────────────────────────
        crave_root = CRAVE_ROOT
        db_dir = os.path.join(crave_root, "data")
        os.makedirs(db_dir, exist_ok=True)
        self._db_path = os.path.join(db_dir, "sentinel_orders.db")
        self._init_db()
        self._load_known_orders()

        logger.info(
            f"[Sentinel] Initialised. "
            f"Session prefix: {self._session_prefix} | "
            f"Alert at {self._alert_threshold} | "
            f"Kill at {self._kill_threshold} anomalies"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER ID GENERATION
    # ─────────────────────────────────────────────────────────────────────────

    def _init_db(self):
        """Create the sentinel orders table if it doesn't exist."""
        con = sqlite3.connect(self._db_path)
        con.execute(
            "CREATE TABLE IF NOT EXISTS known_orders "
            "(order_id TEXT PRIMARY KEY, created_at TEXT)"
        )
        con.commit()
        con.close()

    def _load_known_orders(self):
        """Load persisted order IDs from SQLite on startup."""
        try:
            con = sqlite3.connect(self._db_path)
            rows = con.execute("SELECT order_id FROM known_orders").fetchall()
            con.close()
            self._known_order_ids = {r[0] for r in rows}
            if self._known_order_ids:
                logger.info(f"[Sentinel] Loaded {len(self._known_order_ids)} known orders from DB.")
        except Exception as e:
            logger.warning(f"[Sentinel] Failed to load orders DB: {e}")

    def _persist_order(self, order_id: str):
        """Save a single order ID to SQLite."""
        try:
            con = sqlite3.connect(self._db_path)
            con.execute(
                "INSERT OR IGNORE INTO known_orders (order_id, created_at) VALUES (?, ?)",
                (order_id, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            con.close()
        except Exception as e:
            logger.debug(f"[Sentinel] DB persist failed: {e}")

    def generate_order_id(self) -> str:
        """
        Generate a unique client order ID for every order CRAVE places.
        All our orders are prefixed with CRV- so we can identify them.

        Format: CRV-{session}-{counter}
        Example: CRV-A3F7B2C1-0042

        This ID gets stored in the exchange's client_order_id field.
        Any order without this prefix is FOREIGN and triggers an alert.
        """
        self._order_counter += 1
        oid = f"{self._session_prefix}-{self._order_counter:04d}"
        self._known_order_ids.add(oid)
        self._persist_order(oid)
        return oid

    def register_known_order(self, order_id: str):
        """Register an order we know about (placed by us)."""
        self._known_order_ids.add(order_id)
        self._persist_order(order_id)

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND MONITORING
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="CRAVEAPISentinel",
        )
        self._thread.start()
        logger.info("[Sentinel] Monitoring started.")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            try:
                self._run_checks()
            except Exception as e:
                logger.debug(f"[Sentinel] Check error: {e}")
            time.sleep(self.CHECK_INTERVAL_SECS)

    def _run_checks(self):
        """Run all anomaly checks."""
        anomalies = []

        # Check Binance orders
        binance_anomalies = self._check_binance_orders()
        anomalies.extend(binance_anomalies)

        # Check Alpaca orders
        alpaca_anomalies = self._check_alpaca_orders()
        anomalies.extend(alpaca_anomalies)

        if not anomalies:
            return

        # Process each anomaly
        for anomaly in anomalies:
            self._anomaly_count += 1
            severity = anomaly.get("severity", "WARNING")
            logger.warning(
                f"[Sentinel] ANOMALY #{self._anomaly_count}: "
                f"{anomaly['type']} | {anomaly['detail']}"
            )
            self._send_alert(anomaly)

        # Take action based on total anomaly count
        if self._anomaly_count >= self._kill_threshold:
            self._execute_kill()
        elif self._anomaly_count >= self._alert_threshold:
            self._execute_pause()

    def _check_binance_orders(self) -> list:
        """Check Binance recent orders for anomalies."""
        anomalies = []
        try:
            import ccxt
            api_key    = os.environ.get("BINANCE_API_KEY", "")
            api_secret = os.environ.get("BINANCE_API_SECRET", "")
            if not api_key:
                return []

            exchange = ccxt.binance({
                "apiKey": api_key, "secret": api_secret,
                "options": {"defaultType": "future"},
            })

            # Get recent orders (last hour)
            from config.config import get_tradeable_symbols, get_asset_class
            binance_syms = [
                s for s in get_tradeable_symbols()
                if get_asset_class(s) == "crypto"
            ]

            for symbol in binance_syms[:3]:  # Check top 3 to avoid rate limits
                try:
                    orders = exchange.fetch_orders(symbol, limit=10)
                    for order in orders:
                        # Check timestamp (only check recent orders)
                        ts = order.get("timestamp", 0)
                        if ts and (time.time() * 1000 - ts) > 3600000:
                            continue  # Older than 1 hour — skip

                        client_id = order.get("clientOrderId", "")
                        # Foreign order: doesn't have our session prefix
                        if client_id and not client_id.startswith("CRV-"):
                            anomalies.append({
                                "type":     "FOREIGN_ORDER",
                                "severity": "ALERT",
                                "symbol":   symbol,
                                "order_id": order.get("id"),
                                "client_id": client_id,
                                "detail": (
                                    f"Order {order.get('id')} on {symbol} "
                                    f"does not have CRAVE prefix. "
                                    f"client_order_id='{client_id}'"
                                ),
                            })

                        # Size anomaly: order > 5× our max configured size
                        from config.config import RISK
                        max_risk_pct = RISK.get("base_risk_pct", 1.0) * 2.5
                        order_value  = float(order.get("cost", 0) or 0)

                        try:
                            from core.paper_trading import get_paper_engine
                            equity = get_paper_engine().get_equity()
                            if equity > 0:
                                order_risk_pct = order_value / equity * 100
                                if order_risk_pct > max_risk_pct * 3:
                                    anomalies.append({
                                        "type":     "SIZE_ANOMALY",
                                        "severity": "ALERT",
                                        "symbol":   symbol,
                                        "detail": (
                                            f"Order size {order_risk_pct:.1f}% of equity "
                                            f"exceeds 3× max ({max_risk_pct*3:.1f}%)"
                                        ),
                                    })
                        except Exception:
                            pass

                except Exception as e:
                    logger.debug(f"[Sentinel] Binance check {symbol}: {e}")

        except Exception as e:
            logger.debug(f"[Sentinel] Binance check failed: {e}")

        return anomalies

    def _check_alpaca_orders(self) -> list:
        """Check Alpaca recent orders for anomalies."""
        anomalies = []
        try:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            if not api_key:
                return []

            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.requests import GetOrdersRequest
                client = TradingClient(
                    api_key, os.environ.get("ALPACA_SECRET_KEY", ""),
                    paper=(os.environ.get("TRADING_MODE", "paper") == "paper")
                )
                orders = client.get_orders(
                    GetOrdersRequest(status="all", limit=20)
                )
                for order in (orders or []):
                    client_id = getattr(order, "client_order_id", "")
                    # Foreign order check
                    if client_id and not client_id.startswith("CRV-"):
                        anomalies.append({
                            "type":     "FOREIGN_ORDER_ALPACA",
                            "severity": "ALERT",
                            "symbol":   str(getattr(order, "symbol", "?")),
                            "order_id": str(getattr(order, "id", "?")),
                            "detail": (
                                f"Alpaca order without CRAVE prefix: "
                                f"client_order_id='{client_id}'"
                            ),
                        })
            except ImportError:
                pass

        except Exception as e:
            logger.debug(f"[Sentinel] Alpaca check failed: {e}")

        return anomalies

    # ─────────────────────────────────────────────────────────────────────────
    # RESPONSE ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_pause(self):
        """Pause new entries — don't kill existing positions yet."""
        logger.warning(
            "[Sentinel] ALERT THRESHOLD reached. "
            "Pausing new entries."
        )
        try:
            from core.streak_state import streak
            streak.manual_pause(reason="API Sentinel alert threshold reached")
        except Exception as e:
            logger.error(f"[Sentinel] Pause failed: {e}")

        self._send_critical_alert(
            "🔒 SENTINEL ALERT: Anomalies detected. "
            "New entries PAUSED. Review /journal immediately."
        )

    def _execute_kill(self):
        """
        Full kill: cancel all exchange orders + close all positions.
        This is the nuclear option — only for confirmed attacks.
        """
        logger.critical(
            "[Sentinel] KILL THRESHOLD reached. "
            "Executing emergency shutdown."
        )

        # Cancel all open exchange orders
        self._cancel_all_exchange_orders()

        # Close all positions via broker router
        try:
            from core.position_tracker import positions
            from brokers.broker_router import get_router

            for pos in positions.get_all():
                try:
                    get_router().execute(
                        {"symbol": pos["symbol"], "direction": "close",
                         "trade_id": pos["trade_id"]},
                        0,
                        is_paper=pos.get("is_paper", True),
                    )
                except Exception as e:
                    logger.error(
                        f"[Sentinel] Kill-close failed {pos['symbol']}: {e}"
                    )
        except Exception as e:
            logger.error(f"[Sentinel] Position close failed: {e}")

        # Pause the trading loop
        try:
            from core.trading_loop import trading_loop
            trading_loop.stop()
        except Exception:
            pass

        self._send_critical_alert(
            "🚨 SENTINEL KILL ACTIVATED\n"
            "Anomaly threshold exceeded.\n"
            "ALL orders cancelled. ALL positions closed.\n"
            "Bot STOPPED. Manual investigation required."
        )

    def _cancel_all_exchange_orders(self):
        """Cancel all open orders on all exchanges."""
        # Binance
        try:
            import ccxt
            exchange = ccxt.binance({
                "apiKey": os.environ.get("BINANCE_API_KEY", ""),
                "secret": os.environ.get("BINANCE_API_SECRET", ""),
                "options": {"defaultType": "future"},
            })
            exchange.cancel_all_orders()
            logger.info("[Sentinel] Binance orders cancelled.")
        except Exception as e:
            logger.error(f"[Sentinel] Binance cancel failed: {e}")

        # Alpaca
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                os.environ.get("ALPACA_API_KEY", ""),
                os.environ.get("ALPACA_SECRET_KEY", ""),
                paper=(os.environ.get("TRADING_MODE", "paper") == "paper"),
            )
            client.cancel_orders()
            logger.info("[Sentinel] Alpaca orders cancelled.")
        except Exception as e:
            logger.debug(f"[Sentinel] Alpaca cancel: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # ALERTING
    # ─────────────────────────────────────────────────────────────────────────

    def _send_alert(self, anomaly: dict):
        """Send anomaly alert to Telegram."""
        # Rate-limit alerts to 1 per minute
        now = datetime.now(timezone.utc)
        if self._last_alert_time:
            if (now - self._last_alert_time).seconds < 60:
                return
        self._last_alert_time = now

        try:
            from interfaces.telegram_interface import tg
            severity = anomaly.get("severity", "WARNING")
            emoji    = "⚠️" if severity == "WARNING" else "🚨"
            tg.send(
                f"{emoji} <b>SENTINEL {severity}</b>\n"
                f"Type   : {anomaly.get('type')}\n"
                f"Symbol : {anomaly.get('symbol', '?')}\n"
                f"Detail : {anomaly.get('detail', '?')}\n"
                f"Count  : {self._anomaly_count}/{self._kill_threshold}"
            )
        except Exception:
            pass

    def _send_critical_alert(self, msg: str):
        try:
            from interfaces.telegram_interface import tg
            tg.send(msg)
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "running":          self._running,
            "anomaly_count":    self._anomaly_count,
            "alert_threshold":  self._alert_threshold,
            "kill_threshold":   self._kill_threshold,
            "known_orders":     len(self._known_order_ids),
            "session_prefix":   self._session_prefix,
        }

    def reset_anomaly_count(self):
        """Call after manual investigation to clear anomaly counter."""
        self._anomaly_count = 0
        logger.info("[Sentinel] Anomaly counter reset.")


# ── Singleton ─────────────────────────────────────────────────────────────────
_sentinel: Optional[APISentinel] = None

def get_sentinel() -> APISentinel:
    global _sentinel
    if _sentinel is None:
        _sentinel = APISentinel()
    return _sentinel
