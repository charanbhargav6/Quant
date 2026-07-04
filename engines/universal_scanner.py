"""
CRAVE v12 — Universal Asset Scanner
======================================
Instead of trading 12 fixed assets, dynamically scan the entire market
and rank every tradeable instrument by opportunity quality.

HOW IT WORKS:
  Every 4 hours:
  1. Pull price data for ~80+ assets (forex, crypto, commodities, indices)
  2. Score each on 6 factors: ATR expansion, ADX trend strength, volume,
     news sentiment, S/R proximity, and momentum
  3. Rank all assets by composite score
  4. Feed the top 8 scored assets into the trading loop

  This is how real hedge funds work — they don't pre-decide what to trade,
  they find where the opportunity IS.
"""

import os
import logging
import time
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger("crave.universal_scanner")

# ─────────────────────────────────────────────────────────────────────────────
# FULL ASSET UNIVERSE
# ─────────────────────────────────────────────────────────────────────────────

FOREX_UNIVERSE = [
    # Majors
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
    "USDCHF=X", "NZDUSD=X",
    # Crosses
    "EURJPY=X", "GBPJPY=X", "EURGBP=X", "EURAUD=X", "AUDNZD=X",
    "AUDJPY=X", "CADJPY=X", "CHFJPY=X", "NZDJPY=X",
    "EURNZD=X", "GBPAUD=X", "GBPNZD=X", "GBPCAD=X",
    "EURCAD=X", "AUDCAD=X", "NZDCAD=X", "NZDCHF=X",
]

CRYPTO_UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
    "ADA-USD", "AVAX-USD", "LINK-USD", "DOT-USD", "MATIC-USD",
    "ATOM-USD", "NEAR-USD", "FTM-USD", "OP-USD", "ARB-USD",
]

COMMODITIES_UNIVERSE = [
    "GC=F",    # Gold
    "SI=F",    # Silver
    "CL=F",    # Crude Oil WTI
    "NG=F",    # Natural Gas
    "HG=F",    # Copper
    "PL=F",    # Platinum
]

INDEX_UNIVERSE = [
    "^GSPC",   # S&P 500
    "^IXIC",   # NASDAQ
    "^DJI",    # Dow Jones
    "^RUT",    # Russell 2000
    "^FTSE",   # FTSE 100
    "^GDAXI",  # DAX
    "^N225",   # Nikkei 225
]

# Which symbols can be traded on MT5 (rest are scan-only for intelligence)
MT5_TRADEABLE = {
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
    "USDCHF=X", "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "EURGBP=X",
    "GC=F", "XAUUSD=X",
}

# ─────────────────────────────────────────────────────────────────────────────
# SCORING WEIGHTS
# ─────────────────────────────────────────────────────────────────────────────

WEIGHTS = {
    "atr_expansion":  0.25,  # Higher ATR = more movement = more opportunity
    "adx_strength":   0.20,  # ADX > 25 = trending, better for SMC
    "volume_spike":   0.15,  # Volume > 1.5x avg = institutional interest
    "momentum":       0.15,  # Price above/below EMA21 (trend alignment)
    "news_sentiment": 0.15,  # News alignment with technical setup
    "sr_proximity":   0.10,  # Near a S/R level = clear invalidation point
}


class UniversalScanner:
    """
    Scans the entire tradeable universe and ranks assets by
    opportunity quality. Feeds the top N to the trading loop.
    """

    SCAN_INTERVAL_SECS = 14400   # 4 hours
    TOP_N = 8                     # Feed top 8 assets to trading loop

    def __init__(self):
        self._rankings: List[Dict] = []
        self._active_universe: List[str] = []
        self._lock = threading.Lock()
        self._running = False
        self._last_scan = 0.0
        self._thread: Optional[threading.Thread] = None

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="UniversalScanner"
        )
        self._thread.start()
        logger.info("[UniversalScanner] Started — scanning universe every 4 hours")

    def stop(self):
        self._running = False

    def get_active_universe(self) -> List[str]:
        """Return current top-ranked tradeable symbols."""
        with self._lock:
            if self._active_universe:
                return list(self._active_universe)
        # Fallback: return the original fixed assets
        return ["XAUUSD=X", "EURUSD=X", "GBPUSD=X", "BTCUSDT"]

    def get_full_rankings(self) -> List[Dict]:
        """Return the full ranked list with scores."""
        with self._lock:
            return list(self._rankings)

    def force_scan(self):
        """Force an immediate full scan."""
        self._do_full_scan()

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_loop(self):
        self._do_full_scan()
        while self._running:
            time.sleep(self.SCAN_INTERVAL_SECS)
            self._do_full_scan()

    def _do_full_scan(self):
        """Scan all assets, score them, rank them, select top N."""
        try:
            all_symbols = (
                FOREX_UNIVERSE + CRYPTO_UNIVERSE +
                COMMODITIES_UNIVERSE + INDEX_UNIVERSE
            )

            scored = []
            for symbol in all_symbols:
                try:
                    score = self._score_asset(symbol)
                    if score is not None:
                        scored.append(score)
                except Exception as e:
                    logger.debug(f"[UniversalScanner] {symbol} scoring failed: {e}")

            # Sort by composite score descending
            scored.sort(key=lambda x: x["composite_score"], reverse=True)

            # Filter: only include MT5-tradeable symbols in active universe
            tradeable = [
                s for s in scored
                if s["symbol"] in MT5_TRADEABLE or s.get("exchange") == "binance"
            ]

            top_n = tradeable[:self.TOP_N]
            active = [s["symbol"] for s in top_n]

            with self._lock:
                self._rankings = scored
                self._active_universe = active

            # Log the hot assets
            top_str = " | ".join(
                f"{s['symbol']}({s['composite_score']:.1f})" for s in top_n[:5]
            )
            logger.info(
                f"[UniversalScanner] Full scan complete: "
                f"{len(scored)} assets scored, top 5: {top_str}"
            )

            # Notify via Telegram
            try:
                from interfaces.telegram_interface import tg
                lines = ["🔭 *Universe Scan Complete*\n"]
                for i, s in enumerate(top_n[:8], 1):
                    lines.append(
                        f"{i}. `{s['symbol']}` — Score: {s['composite_score']:.1f} "
                        f"| ATR: {s['atr_score']:.0f} ADX: {s['adx_score']:.0f} "
                        f"Mom: {s['momentum_score']:.0f}"
                    )
                tg.send("\n".join(lines))
            except Exception:
                pass

            self._last_scan = time.time()

        except Exception as e:
            logger.error(f"[UniversalScanner] Full scan error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # ASSET SCORING
    # ─────────────────────────────────────────────────────────────────────────

    def _score_asset(self, symbol: str) -> Optional[Dict]:
        """
        Score a single asset on 6 factors, return composite score 0-100.
        """
        df = self._fetch_data(symbol, period="30d", interval="1h")
        if df is None or len(df) < 50:
            return None

        close = df["close"].values
        high  = df["high"].values
        low   = df["low"].values
        volume = df["volume"].values if "volume" in df.columns else np.ones(len(close))

        # 1. ATR Expansion (0-100)
        atr_14 = self._calc_atr(high, low, close, 14)
        atr_50 = self._calc_atr(high, low, close, 50)
        atr_ratio = atr_14 / max(atr_50, 1e-10)
        atr_score = min(atr_ratio * 50, 100)  # 2x expansion = 100

        # 2. ADX Strength (0-100)
        adx = self._calc_adx(high, low, close, 14)
        adx_score = min(adx * 2, 100)  # ADX 50+ = max score

        # 3. Volume Spike (0-100)
        if np.sum(volume) > 0:
            vol_avg = np.mean(volume[-50:])
            vol_recent = np.mean(volume[-5:])
            vol_ratio = vol_recent / max(vol_avg, 1)
            vol_score = min(vol_ratio * 40, 100)
        else:
            vol_score = 50  # no volume data = neutral

        # 4. Momentum — price relative to EMA21 (0-100)
        ema21 = self._ema(close, 21)
        price = close[-1]
        ema_val = ema21[-1]
        pct_from_ema = abs(price - ema_val) / max(ema_val, 1e-10) * 100
        momentum_score = min(pct_from_ema * 20, 100)  # 5% from EMA = max

        # 5. News Sentiment Alignment (0-100)
        news_score = self._get_news_score(symbol)

        # 6. S/R Proximity (0-100)
        sr_score = self._calc_sr_proximity(close, high, low)

        # Composite
        composite = (
            atr_score    * WEIGHTS["atr_expansion"] +
            adx_score    * WEIGHTS["adx_strength"] +
            vol_score    * WEIGHTS["volume_spike"] +
            momentum_score * WEIGHTS["momentum"] +
            news_score   * WEIGHTS["news_sentiment"] +
            sr_score     * WEIGHTS["sr_proximity"]
        )

        # Determine which exchange handles this
        exchange = "yfinance"
        if symbol in MT5_TRADEABLE:
            exchange = "mt5"
        elif symbol in [s for s in CRYPTO_UNIVERSE]:
            exchange = "binance"

        return {
            "symbol":          symbol,
            "composite_score": round(composite, 2),
            "atr_score":       round(atr_score, 1),
            "adx_score":       round(adx_score, 1),
            "volume_score":    round(vol_score, 1),
            "momentum_score":  round(momentum_score, 1),
            "news_score":      round(news_score, 1),
            "sr_score":        round(sr_score, 1),
            "exchange":        exchange,
            "price":           round(float(close[-1]), 5),
            "atr_14":          round(float(atr_14), 6),
            "adx":             round(float(adx), 1),
            "scanned_at":      datetime.now(timezone.utc).isoformat(),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # DATA FETCHING
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_data(self, symbol: str, period: str = "30d",
                     interval: str = "1h") -> Optional[pd.DataFrame]:
        """Fetch OHLCV data via yfinance (works for forex, crypto, commodities, indices)."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            if df is not None and len(df) > 0:
                df.columns = [c.lower() for c in df.columns]
                return df
        except Exception as e:
            logger.debug(f"[UniversalScanner] Data fetch failed for {symbol}: {e}")
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # INDICATORS
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_atr(self, high, low, close, period: int = 14) -> float:
        """Average True Range."""
        if len(close) < period + 1:
            return 0.0
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:]  - close[:-1])
            )
        )
        return float(np.mean(tr[-period:]))

    def _calc_adx(self, high, low, close, period: int = 14) -> float:
        """Simplified ADX calculation."""
        if len(close) < period * 2:
            return 0.0

        plus_dm = np.maximum(high[1:] - high[:-1], 0)
        minus_dm = np.maximum(low[:-1] - low[1:], 0)

        # Zero out where the other is larger
        mask = plus_dm > minus_dm
        plus_dm[~mask] = 0
        minus_dm[mask] = 0

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:]  - close[:-1])
            )
        )

        atr = np.mean(tr[-period:])
        if atr <= 0:
            return 0.0

        plus_di  = np.mean(plus_dm[-period:]) / atr * 100
        minus_di = np.mean(minus_dm[-period:]) / atr * 100

        di_sum = plus_di + minus_di
        if di_sum <= 0:
            return 0.0

        dx = abs(plus_di - minus_di) / di_sum * 100
        return float(dx)

    def _ema(self, data, period: int) -> np.ndarray:
        """Exponential Moving Average."""
        result = np.zeros_like(data, dtype=float)
        result[0] = data[0]
        k = 2.0 / (period + 1)
        for i in range(1, len(data)):
            result[i] = data[i] * k + result[i - 1] * (1 - k)
        return result

    def _calc_sr_proximity(self, close, high, low) -> float:
        """
        Score 0-100 based on how close price is to a support/resistance level.
        Uses fractal pivot points from recent data.
        """
        if len(close) < 30:
            return 50.0

        # Find local highs and lows as S/R levels
        window = 10
        levels = []
        for i in range(window, len(high) - window):
            if high[i] == max(high[i - window:i + window + 1]):
                levels.append(high[i])
            if low[i] == min(low[i - window:i + window + 1]):
                levels.append(low[i])

        if not levels:
            return 50.0

        price = close[-1]
        min_dist = min(abs(price - lvl) / max(price, 1e-10) for lvl in levels)

        # Closer to S/R = higher score (within 0.5% = max)
        if min_dist < 0.002:
            return 100.0
        elif min_dist < 0.005:
            return 75.0
        elif min_dist < 0.01:
            return 50.0
        elif min_dist < 0.02:
            return 25.0
        return 10.0

    def _get_news_score(self, symbol: str) -> float:
        """Get news sentiment score for this asset (0-100)."""
        try:
            from intelligence.news_sentinel import get_sentinel
            sentinel = get_sentinel()
            result = sentinel.get_asset_sentiment(symbol, max_age_mins=120)
            score = result.get("score", 0)
            # Convert -2..+2 range to 0-100
            return min(max((score + 2) * 25, 0), 100)
        except Exception:
            return 50.0  # neutral


# ── Singleton ──────────────────────────────────────────────────────────────
_scanner: Optional[UniversalScanner] = None

def get_universal_scanner() -> UniversalScanner:
    global _scanner
    if _scanner is None:
        _scanner = UniversalScanner()
    return _scanner
