"""
CRAVE v10.3 — Market Data Router (Session 8)
=============================================
Single entry point for ALL market data regardless of source.
Every module calls this instead of DataAgent/yfinance directly.

ROUTING PRIORITY PER ASSET CLASS:
  Crypto:        WebSocket cache → Binance REST → CCXT fallback
  US Stocks:     Alpaca WebSocket → Alpaca REST → yfinance fallback
  India Stocks:  Zerodha KiteTicker → Kite REST → yfinance (.NS) fallback
  Forex/Gold:    Alpaca WebSocket → Alpaca REST → yfinance (=X) fallback
  Options:       Zerodha → NSE API fallback
  Any:           Database OHLCV cache (avoids repeat API calls)

OHLCV CACHE POLICY:
  All fetched data is written to database OHLCV cache.
  Next request for same symbol+timeframe within cache_ttl_mins:
    → served from cache, zero API calls
  cache_ttl_mins:
    1m  data: 2 minutes
    5m  data: 5 minutes
    1h  data: 30 minutes
    1d  data: 6 hours

USAGE:
  from data.market_data_router import get_router

  router = get_router()
  df    = router.get_ohlcv("BTCUSDT",    "1h",  limit=250)
  df    = router.get_ohlcv("AAPL",       "1h",  limit=250)
  df    = router.get_ohlcv("RELIANCE",   "15m", limit=200)
  df    = router.get_ohlcv("EURUSD=X",   "1h",  limit=250)
  price = router.get_live_price("BTCUSDT")
  chain = router.get_option_chain("NIFTY")
  fii   = router.get_fii_dii_data()
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import pandas as pd

logger = logging.getLogger("crave.data_router")


# ─────────────────────────────────────────────────────────────────────────────
# CACHE TTL POLICY (minutes)
# ─────────────────────────────────────────────────────────────────────────────

CACHE_TTL = {
    "1m":  2,
    "3m":  3,
    "5m":  5,
    "15m": 15,
    "30m": 30,
    "1h":  30,
    "4h":  120,
    "1d":  360,
    "1wk": 720,
    "day": 360,
}


class MarketDataRouter:

    def __init__(self):
        self._cache_timestamps: dict = {}   # (symbol, tf) → last_fetch UTC

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN OHLCV ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, timeframe: str = "1h",
                   limit: int = 250,
                   force_fresh: bool = False) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data for any symbol from the best available source.

        Steps:
          1. Check database cache (if within TTL and not force_fresh)
          2. Try WebSocket cache (live symbols only)
          3. Try primary source for this exchange
          4. Try fallback source
          5. Cache result to database

        Returns None only if all sources fail.
        """
        from config.config import get_instrument, get_asset_class

        inst     = get_instrument(symbol)
        exchange = inst.get("exchange", "yfinance")
        asset    = get_asset_class(symbol)

        # ── Step 1: Database cache ─────────────────────────────────────────
        if not force_fresh and self._is_cache_fresh(symbol, timeframe):
            try:
                from core.database_manager import db
                cached = db.get_cached_ohlcv(symbol, timeframe, limit=limit)
                if cached is not None and len(cached) >= min(20, limit // 4):
                    logger.debug(f"[DataRouter] Cache hit: {symbol} {timeframe}")
                    return cached
            except Exception:
                pass

        # ── Step 2: WebSocket (live instruments) ──────────────────────────
        ws_df = self._try_websocket(symbol, timeframe, limit)
        if ws_df is not None and len(ws_df) >= 20:
            self._write_cache(symbol, timeframe, ws_df)
            return ws_df

        # ── Step 3: Primary source ────────────────────────────────────────
        df = None
        if exchange == "binance":
            df = self._fetch_binance(symbol, timeframe, limit)
        elif exchange == "zerodha":
            df = self._fetch_zerodha(symbol, timeframe, limit, inst)
        elif exchange == "alpaca":
            df = self._fetch_alpaca(symbol, timeframe, limit, asset)
        elif exchange == "yfinance":
            df = self._fetch_yfinance(symbol, timeframe, limit)

        # ── Step 4: Fallback ──────────────────────────────────────────────
        if df is None or len(df) < 10:
            df = self._fetch_fallback(symbol, timeframe, limit, exchange, asset)

        # ── Step 5: Cache result ──────────────────────────────────────────
        if df is not None and len(df) >= 10:
            self._write_cache(symbol, timeframe, df)
            logger.debug(
                f"[DataRouter] Fetched {len(df)} candles: "
                f"{symbol} {timeframe} from {exchange}"
            )

        return df

    def get_live_price(self, symbol: str) -> Optional[float]:
        """
        Get the most current price for a symbol.
        Tries WebSocket first for zero-latency, falls back to OHLCV close.
        """
        # Try WebSocket cache
        try:
            from interfaces.websocket_manager import get_ws
            price = get_ws().get_live_price(symbol)
            if price and price > 0:
                return price
        except Exception:
            pass

        # Fall back to last close from 1m OHLCV
        df = self.get_ohlcv(symbol, "1m", limit=2)
        if df is not None and not df.empty:
            return float(df['close'].iloc[-1])

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE-SPECIFIC FETCHERS
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_binance(self, symbol: str, timeframe: str,
                        limit: int) -> Optional[pd.DataFrame]:
        try:
            from core.data_agent import get_data_agent
            return get_data_agent().get_ohlcv(
                symbol, exchange="binance", timeframe=timeframe, limit=limit
            )
        except Exception as e:
            logger.debug(f"[DataRouter] Binance failed {symbol}: {e}")
            return None

    def _fetch_alpaca(self, symbol: str, timeframe: str,
                       limit: int, asset: str) -> Optional[pd.DataFrame]:
        try:
            from core.data_agent import get_data_agent
            # Let DataAgent handle timeframe conversion internally
            return get_data_agent().get_ohlcv(
                symbol, exchange="alpaca", timeframe=timeframe, limit=limit
            )
        except Exception as e:
            logger.debug(f"[DataRouter] Alpaca failed {symbol}: {e}")
            return None

    def _fetch_zerodha(self, symbol: str, timeframe: str,
                        limit: int, inst: dict) -> Optional[pd.DataFrame]:
        """Fetch from Zerodha Kite Connect."""
        try:
            from brokers.zerodha_agent import get_zerodha
            zr = get_zerodha()
            if not zr.is_authenticated():
                return None

            ts = inst.get("tradingsymbol", symbol)
            kx = inst.get("kite_exchange", "NSE")

            # Map timeframe to Kite interval
            tf_map = {
                "1m": "minute", "3m": "3minute", "5m": "5minute",
                "15m": "15minute", "30m": "30minute",
                "1h": "60minute", "1d": "day",
            }
            interval = tf_map.get(timeframe, "15minute")
            days     = max(10, limit // 26 + 5)

            return zr.get_ohlcv(ts, kx, interval=interval, days=days)
        except Exception as e:
            logger.debug(f"[DataRouter] Zerodha failed {symbol}: {e}")
            return None

    def _fetch_yfinance(self, symbol: str, timeframe: str,
                         limit: int) -> Optional[pd.DataFrame]:
        """yfinance fetch — works for backtest and paper trading."""
        try:
            import yfinance as yf
            import logging as yf_logging
            yf_logging.getLogger('yfinance').setLevel(yf_logging.CRITICAL)

            tf_map = {
                "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                "1h": "1h", "4h": "4h", "1d": "1d", "1wk": "1wk",
            }
            interval = tf_map.get(timeframe, "1h")

            # Calculate days needed
            mins_per_candle = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                               "1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 60)
            total_mins = limit * mins_per_candle * 1.4
            days       = max(7, int(total_mins / (24 * 60)) + 5)
            days       = min(days, 729)

            end   = datetime.now()
            start = end - timedelta(days=days)
            df    = yf.download(symbol, start=start, end=end,
                                 interval=interval, progress=False)
            if df is None or df.empty:
                return None

            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            col_map = {}
            for col in df.columns:
                cl = str(col).lower()
                if "date" in cl: col_map[col] = "time"
                elif cl == "open":   col_map[col] = "open"
                elif cl == "high":   col_map[col] = "high"
                elif cl == "low":    col_map[col] = "low"
                elif cl == "close":  col_map[col] = "close"
                elif cl == "volume": col_map[col] = "volume"

            df = df.rename(columns=col_map)
            if "volume" not in df.columns:
                df["volume"] = 0
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df[["time","open","high","low","close","volume"]].dropna()
            return df.tail(limit).reset_index(drop=True)

        except Exception as e:
            logger.debug(f"[DataRouter] yfinance failed {symbol}: {e}")
            return None

    def _fetch_fallback(self, symbol: str, timeframe: str,
                         limit: int, exchange: str,
                         asset: str) -> Optional[pd.DataFrame]:
        """
        Try yfinance as fallback when primary source failed.
        For forex =X pairs, the yfinance ticker is the same symbol.
        For Binance/NSE, maps to yfinance equivalents.
        """
        fallback_sym = self._get_fallback_symbol(symbol, asset)
        if not fallback_sym:
            return None

        # Skip if primary was already yfinance with same symbol
        if fallback_sym == symbol and exchange == "yfinance":
            return None

        logger.debug(
            f"[DataRouter] Trying fallback: {symbol} → {fallback_sym} (yfinance)"
        )
        return self._fetch_yfinance(fallback_sym, timeframe, limit)

    def _get_fallback_symbol(self, symbol: str, asset: str) -> Optional[str]:
        """Map primary symbol to yfinance fallback."""
        fallbacks = {
            "BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "SOLUSDT": "SOL-USD",
            "XAUUSD=X": "GC=F",   "XAGUSD=X": "SI=F",
            "RELIANCE":  "RELIANCE.NS", "TCS": "TCS.NS",
            "HDFCBANK":  "HDFCBANK.NS", "INFY": "INFY.NS",
            "NIFTY_FUT": "^NSEI",  "BANKNIFTY_FUT": "^NSEBANK",
        }
        fb = fallbacks.get(symbol.upper())
        if fb:
            return fb

        # Forex =X symbols (EURUSD=X, AUDUSD=X, etc.) are valid yfinance tickers
        if symbol.endswith("=X"):
            return symbol

        return None

    def _try_websocket(self, symbol: str, timeframe: str,
                        limit: int) -> Optional[pd.DataFrame]:
        """Try WebSocket cache for live data."""
        try:
            from interfaces.websocket_manager import get_ws
            return get_ws().get_live_ohlcv(symbol, timeframe, limit=limit)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # CACHE MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _is_cache_fresh(self, symbol: str, timeframe: str) -> bool:
        """Check if cached data is within TTL."""
        key       = (symbol, timeframe)
        last_ts   = self._cache_timestamps.get(key)
        if not last_ts:
            return False
        ttl_mins = CACHE_TTL.get(timeframe, 30)
        age_mins = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        return age_mins < ttl_mins

    def _write_cache(self, symbol: str, timeframe: str,
                      df: pd.DataFrame):
        """Write to DB cache and update timestamp."""
        try:
            from core.database_manager import db
            db.cache_ohlcv(symbol, timeframe, df)
            self._cache_timestamps[(symbol, timeframe)] = datetime.now(timezone.utc)
        except Exception as e:
            logger.debug(f"[DataRouter] Cache write failed {symbol}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # INDIA-SPECIFIC DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_option_chain(self, symbol: str = "NIFTY") -> Optional[dict]:
        """
        Fetch NSE option chain data.
        Primary: Zerodha instruments API
        Fallback: NSE public API (no auth required)
        """
        # Try Zerodha first (authenticated, more reliable)
        try:
            from brokers.zerodha_agent import get_zerodha
            zr = get_zerodha()
            if zr.is_authenticated():
                # Use PCR endpoint which includes basic chain data
                pcr = zr.get_pcr(symbol)
                if pcr.get("available"):
                    return {
                        "source": "zerodha",
                        "pcr":    pcr,
                        "symbol": symbol,
                    }
        except Exception:
            pass

        # Fallback: NSE public API
        try:
            import requests
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            headers = {"User-Agent":  "Mozilla/5.0",
                       "Accept":      "application/json",
                       "Referer":     "https://www.nseindia.com"}
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                return {"source": "nse_api", "data": r.json(), "symbol": symbol}
        except Exception as e:
            logger.debug(f"[DataRouter] Option chain failed {symbol}: {e}")

        return None

    def get_fii_dii_data(self) -> dict:
        """Get FII/DII institutional flow data for India."""
        try:
            from brokers.zerodha_agent import get_zerodha
            return get_zerodha().get_fii_dii_data()
        except Exception as e:
            logger.debug(f"[DataRouter] FII/DII failed: {e}")
            return {"available": False}

    def get_pcr(self, symbol: str = "NIFTY") -> dict:
        """Get Put-Call Ratio for NSE index/stock."""
        try:
            from brokers.zerodha_agent import get_zerodha
            return get_zerodha().get_pcr(symbol)
        except Exception:
            return {"available": False}

    # ─────────────────────────────────────────────────────────────────────────
    # STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        cached_count = len(self._cache_timestamps)
        fresh_count  = sum(
            1 for (sym, tf) in self._cache_timestamps
            if self._is_cache_fresh(sym, tf)
        )
        return {
            "cached_symbols": cached_count,
            "fresh_entries":  fresh_count,
            "stale_entries":  cached_count - fresh_count,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
_router: Optional[MarketDataRouter] = None

def get_data_router() -> MarketDataRouter:
    global _router
    if _router is None:
        _router = MarketDataRouter()
    return _router
