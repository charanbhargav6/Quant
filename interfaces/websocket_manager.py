"""
CRAVE v10.0 — WebSocket Manager
=================================
Replaces REST API polling with persistent WebSocket streams.

WHY THIS MATTERS:
  Old way (polling): call get_ohlcv() every 5 min → hits rate limits,
  adds 200-500ms latency per request, misses price action between calls.

  New way (WebSocket): one persistent connection per exchange receives
  ALL subscribed symbol updates in real-time. Price cache updated
  continuously. Signal loop reads from cache — zero API calls needed.

ARCHITECTURE:
  One WebSocket thread per exchange (Binance, Alpaca).
  Each thread maintains a reconnecting connection.
  Price updates land in _live_prices dict (symbol → latest candle).
  All other modules read from this cache via get_live_price().

BINANCE STREAMS:
  {symbol}@kline_1m   → 1-minute candle updates
  {symbol}@bookTicker → best bid/ask (for spread monitoring)

ALPACA STREAMS:
  bars.{symbol}       → real-time bar updates
  quotes.{symbol}     → bid/ask quotes

USAGE:
  from interfaces.websocket_manager import ws

  ws.start()                          # start all streams
  price = ws.get_live_price("BTCUSDT")  # latest price
  df    = ws.get_live_ohlcv("BTCUSDT", "1m", limit=50)  # recent candles
  ws.subscribe("ETHUSDT")             # add a symbol
  ws.stop()                           # graceful shutdown
"""

import json
import logging
import threading
import time
import queue
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Deque

import pandas as pd

logger = logging.getLogger("crave.websocket")


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE STORE
# ─────────────────────────────────────────────────────────────────────────────

class CandleStore:
    """
    Rolling buffer of candles per symbol per timeframe.
    Thread-safe. Converts to DataFrame on demand.
    """

    def __init__(self, max_candles: int = 500):
        self._max     = max_candles
        self._candles: Dict[str, Dict[str, Deque]] = {}
        self._lock    = threading.Lock()

    def update(self, symbol: str, timeframe: str, candle: dict):
        """Add or update a candle. If same timestamp exists, replace it."""
        key = f"{symbol}:{timeframe}"
        with self._lock:
            if key not in self._candles:
                self._candles[key] = deque(maxlen=self._max)

            buf = self._candles[key]

            # Replace last candle if same timestamp (candle still forming)
            if buf and buf[-1]["time"] == candle["time"]:
                buf[-1] = candle
            else:
                buf.append(candle)

    def get_df(self, symbol: str, timeframe: str,
               limit: int = 100) -> Optional[pd.DataFrame]:
        """Return recent candles as DataFrame."""
        key = f"{symbol}:{timeframe}"
        with self._lock:
            buf = self._candles.get(key)
            if not buf or len(buf) < 3:
                return None
            data = list(buf)[-limit:]

        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df.sort_values("time").reset_index(drop=True)

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get most recent close price for any timeframe."""
        for tf in ("1m", "5m", "1h"):
            key = f"{symbol}:{tf}"
            with self._lock:
                buf = self._candles.get(key)
                if buf:
                    return buf[-1].get("close")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# BASE WEBSOCKET CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class BaseWSClient:
    """
    Base class for exchange-specific WebSocket clients.
    Handles: connection, reconnection with backoff, message parsing.
    """

    RECONNECT_DELAY_SECS = 5
    MAX_RECONNECT_SECS   = 60

    def __init__(self, name: str, candle_store: CandleStore):
        self._name         = name
        self._store        = candle_store
        self._running      = False
        self._connected    = False
        self._thread:      Optional[threading.Thread] = None
        self._reconnect_delay = self.RECONNECT_DELAY_SECS

    def start(self, symbols: list):
        """Start WebSocket connection in background thread."""
        self._symbols = symbols
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_with_reconnect,
            daemon=True,
            name=f"CRAVE_WS_{self._name}"
        )
        self._thread.start()
        logger.info(f"[WS:{self._name}] Started for {len(symbols)} symbols.")

    def stop(self):
        self._running    = False
        self._connected  = False
        logger.info(f"[WS:{self._name}] Stopped.")

    def _run_with_reconnect(self):
        """Main loop: connect, run, reconnect on failure."""
        while self._running:
            try:
                logger.info(f"[WS:{self._name}] Connecting...")
                self._connect_and_run()
                self._reconnect_delay = self.RECONNECT_DELAY_SECS  # reset on clean exit
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"[WS:{self._name}] Disconnected: {e}. "
                        f"Reconnecting in {self._reconnect_delay}s..."
                    )
                    self._connected = False
                    self._notify_disconnect()
                    time.sleep(self._reconnect_delay)
                    # Exponential backoff, cap at 60s
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self.MAX_RECONNECT_SECS
                    )

    def _connect_and_run(self):
        """Override in subclass."""
        raise NotImplementedError

    def _notify_disconnect(self):
        """Alert on unexpected disconnect."""
        try:
            from interfaces.telegram_interface import tg
            tg.send(
                f"⚠️ WebSocket {self._name} disconnected. Reconnecting..."
            )
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        return self._connected


# ─────────────────────────────────────────────────────────────────────────────
# BINANCE WEBSOCKET CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class BinanceWSClient(BaseWSClient):
    """
    Binance WebSocket client.
    Subscribes to: kline (1m candles) + bookTicker (bid/ask spread).
    Uses Binance combined stream endpoint for efficiency.
    """

    WS_BASE = "wss://fstream.binance.com/stream"   # Futures
    # WS_BASE = "wss://stream.binance.com:9443/stream"  # Spot

    def _connect_and_run(self):
        try:
            import websocket
        except ImportError:
            logger.warning(
                "[WS:Binance] websocket-client not installed. "
                "Run: pip install websocket-client"
            )
            time.sleep(60)
            return

        # Build combined stream URL
        # e.g., /stream?streams=btcusdt@kline_1m/ethusdt@kline_1m
        streams = []
        for symbol in self._symbols:
            s = symbol.lower()
            streams.append(f"{s}@kline_1m")
            streams.append(f"{s}@bookTicker")

        if not streams:
            time.sleep(30)
            return

        url = f"{self.WS_BASE}?streams={'/'.join(streams)}"
        logger.info(f"[WS:Binance] Connecting to {len(self._symbols)} streams...")

        ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        self._connected = True
        self._reconnect_delay = self.RECONNECT_DELAY_SECS
        logger.info(f"[WS:Binance] Connected ✅")

    def _on_message(self, ws, raw: str):
        try:
            msg    = json.loads(raw)
            data   = msg.get("data", msg)
            stream = msg.get("stream", "")

            # ── Kline (candle) update ──────────────────────────────────────
            if "@kline" in stream:
                k      = data.get("k", {})
                symbol = data.get("s", "UNKNOWN")
                candle = {
                    "time":   datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc).isoformat(),
                    "open":   float(k["o"]),
                    "high":   float(k["h"]),
                    "low":    float(k["l"]),
                    "close":  float(k["c"]),
                    "volume": float(k["v"]),
                    "closed": k.get("x", False),
                }
                self._store.update(symbol, "1m", candle)

            # ── BookTicker (best bid/ask) ──────────────────────────────────
            elif "@bookTicker" in stream:
                symbol = data.get("s", "UNKNOWN")
                bid    = float(data.get("b", 0))
                ask    = float(data.get("a", 0))
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    # Store as a synthetic tick (not a full candle)
                    self._store.update(symbol, "tick", {
                        "time":  datetime.now(timezone.utc).isoformat(),
                        "open":  mid, "high": ask,
                        "low":   bid, "close": mid,
                        "volume": 0,
                    })

        except Exception as e:
            logger.debug(f"[WS:Binance] Message parse error: {e}")

    def _on_error(self, ws, error):
        logger.warning(f"[WS:Binance] Error: {error}")

    def _on_close(self, ws, code, msg):
        self._connected = False
        logger.info(f"[WS:Binance] Closed (code={code})")
        if self._running:
            raise ConnectionError(f"Binance WS closed: {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# ALPACA WEBSOCKET CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class AlpacaWSClient(BaseWSClient):
    """
    Alpaca WebSocket client for stocks and forex.
    Uses Alpaca's streaming data v2 API.
    Authenticates with API key from environment.
    """

    WS_URL = "wss://stream.data.alpaca.markets/v2/sip"

    def _connect_and_run(self):
        import os
        key    = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")

        if not key or not secret:
            logger.info("[WS:Alpaca] No API keys — skipping Alpaca stream.")
            time.sleep(300)
            return

        try:
            import websocket
        except ImportError:
            logger.warning("[WS:Alpaca] websocket-client not installed.")
            time.sleep(60)
            return

        self._ws_key    = key
        self._ws_secret = secret

        ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        """Authenticate and subscribe on connection."""
        import os
        # Step 1: Auth
        ws.send(json.dumps({
            "action": "auth",
            "key":    os.environ.get("ALPACA_API_KEY", ""),
            "secret": os.environ.get("ALPACA_SECRET_KEY", ""),
        }))
        self._ws_ref = ws

    def _on_message(self, ws, raw: str):
        try:
            messages = json.loads(raw)
            if not isinstance(messages, list):
                messages = [messages]

            for msg in messages:
                msg_type = msg.get("T", "")

                # Auth success → subscribe
                if msg_type == "success" and msg.get("msg") == "authenticated":
                    self._connected = True
                    logger.info("[WS:Alpaca] Authenticated ✅")
                    # Subscribe to bars
                    ws.send(json.dumps({
                        "action": "subscribe",
                        "bars":   self._symbols,
                    }))

                # Bar update
                elif msg_type == "b":
                    symbol = msg.get("S", "UNKNOWN")
                    candle = {
                        "time":   msg.get("t", ""),
                        "open":   float(msg.get("o", 0)),
                        "high":   float(msg.get("h", 0)),
                        "low":    float(msg.get("l", 0)),
                        "close":  float(msg.get("c", 0)),
                        "volume": float(msg.get("v", 0)),
                    }
                    self._store.update(symbol, "1m", candle)

                # Quote update (for spread monitoring)
                elif msg_type == "q":
                    symbol = msg.get("S", "UNKNOWN")
                    bid    = float(msg.get("bp", 0))
                    ask    = float(msg.get("ap", 0))
                    if bid > 0 and ask > 0:
                        self._store.update(symbol, "tick", {
                            "time":  msg.get("t", ""),
                            "open":  bid, "high": ask,
                            "low":   bid, "close": (bid+ask)/2,
                            "volume": 0,
                        })

        except Exception as e:
            logger.debug(f"[WS:Alpaca] Parse error: {e}")

    def _on_error(self, ws, error):
        logger.warning(f"[WS:Alpaca] Error: {error}")

    def _on_close(self, ws, code, msg):
        self._connected = False
        logger.info(f"[WS:Alpaca] Closed")
        if self._running:
            raise ConnectionError("Alpaca WS closed")


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET MANAGER (orchestrates all clients)
# ─────────────────────────────────────────────────────────────────────────────

class WebSocketManager:
    """
    Manages all WebSocket connections.
    Routes symbol subscriptions to the correct exchange client.
    Provides unified interface for live prices.
    """

    def __init__(self):
        self._store   = CandleStore(max_candles=500)
        self._clients: Dict[str, BaseWSClient] = {}
        self._running = False

        # Symbol → exchange routing
        self._binance_symbols: list = []
        self._alpaca_symbols:  list = []
        self._classify_symbols()

    def _classify_symbols(self):
        """Route each instrument to the correct exchange client."""
        from config.config import get_tradeable_symbols, get_instrument

        for symbol in get_tradeable_symbols():
            inst     = get_instrument(symbol)
            exchange = inst.get("exchange", "alpaca")

            if exchange == "binance":
                self._binance_symbols.append(symbol)
            elif exchange == "alpaca":
                self._alpaca_symbols.append(symbol)
            # yfinance symbols are backtest-only, no live stream needed

        logger.info(
            f"[WS] Binance symbols: {self._binance_symbols}\n"
            f"     Alpaca symbols:  {self._alpaca_symbols}"
        )

    def start(self):
        """Start all WebSocket connections."""
        self._running = True

        # Binance stream
        if self._binance_symbols:
            binance_client = BinanceWSClient("Binance", self._store)
            binance_client.start(self._binance_symbols)
            self._clients["binance"] = binance_client

        # Alpaca stream
        if self._alpaca_symbols:
            alpaca_client = AlpacaWSClient("Alpaca", self._store)
            alpaca_client.start(self._alpaca_symbols)
            self._clients["alpaca"] = alpaca_client

        logger.info(
            f"[WS] Manager started. "
            f"{len(self._clients)} exchange client(s) running."
        )

    def stop(self):
        """Stop all connections gracefully."""
        self._running = False
        for name, client in self._clients.items():
            client.stop()
            logger.info(f"[WS] {name} client stopped.")

    def subscribe(self, symbol: str):
        """Dynamically add a symbol to streams (after start)."""
        from config.config import get_instrument
        exchange = get_instrument(symbol).get("exchange", "alpaca")

        if exchange == "binance" and symbol not in self._binance_symbols:
            self._binance_symbols.append(symbol)
            # For now, restart required to pick up new subscriptions
            # Full dynamic subscription requires sending subscribe message
            logger.info(f"[WS] {symbol} queued for Binance stream.")

        elif exchange == "alpaca" and symbol not in self._alpaca_symbols:
            self._alpaca_symbols.append(symbol)
            logger.info(f"[WS] {symbol} queued for Alpaca stream.")

    # ─────────────────────────────────────────────────────────────────────────
    # DATA ACCESS
    # ─────────────────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str) -> Optional[float]:
        """
        Get the latest live price from WebSocket cache.
        Falls back to REST API if WebSocket data is stale.
        """
        price = self._store.get_latest_price(symbol)
        if price:
            return price

        # Fallback to REST if WS not ready
        try:
            from core.data_agent import DataAgent
            da  = DataAgent()
            df  = da.get_ohlcv(symbol, timeframe="1m", limit=2)
            if df is not None and not df.empty:
                return float(df['close'].iloc[-1])
        except Exception:
            pass

        return None

    def get_live_ohlcv(self, symbol: str, timeframe: str = "1m",
                        limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get recent OHLCV from WebSocket cache.
        Falls back to database cache or REST API.
        """
        # Try WS cache first
        df = self._store.get_df(symbol, timeframe, limit=limit)
        if df is not None and len(df) >= 10:
            return df

        # Fall back to database cache
        try:
            from core.database_manager import db
            cached = db.get_cached_ohlcv(symbol, timeframe, limit=limit)
            if cached is not None and len(cached) >= 10:
                return cached
        except Exception:
            pass

        # Fall back to REST API
        try:
            from core.data_agent import DataAgent
            return DataAgent().get_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception:
            return None

    def get_status(self) -> dict:
        """Status of all WebSocket connections."""
        return {
            name: {
                "connected": client.is_connected,
                "symbols":   (self._binance_symbols
                              if name == "binance"
                              else self._alpaca_symbols),
            }
            for name, client in self._clients.items()
        }

    def get_status_message(self) -> str:
        """Formatted status for Telegram."""
        lines = ["📡 <b>WEBSOCKET STATUS</b>", "━━━━━━━━━━━━━━━"]
        for name, info in self.get_status().items():
            ok     = "✅" if info["connected"] else "❌"
            syms   = ", ".join(info["symbols"][:3])
            n_syms = len(info["symbols"])
            lines.append(f"{ok} {name.capitalize()}: {n_syms} symbols ({syms}...)")

        if not self._clients:
            lines.append("No active streams (API keys not configured)")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# FIX 5 — Lazy singleton (module-level instantiation crashes on import
# if websocket-client missing or Config not ready)
# ─────────────────────────────────────────────────────────────────────────────

_ws_instance: Optional["WebSocketManager"] = None

def get_ws() -> "WebSocketManager":
    global _ws_instance
    if _ws_instance is None:
        _ws_instance = WebSocketManager()
    return _ws_instance


# Backward compat alias
ws = get_ws
