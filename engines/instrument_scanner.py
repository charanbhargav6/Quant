"""
CRAVE v11.1 - Instrument Scanner
==================================
Runs daily at 06:45 UTC after DailyBiasEngine completes.
Ranks all instruments and selects the top 1-2 to trade today.

SCORING CRITERIA (max 13 points):
  ATR expansion         +3  current ATR > 20d avg ATR by 20%+ (market is moving)
  Liquidity nearby      +2  equal highs/lows or FVG within 1× ATR (magnet levels)
  Bias strength         +3  from DailyBiasEngine (1/2/3 points)
  Clean structure       +1  no conflicting signals, clear BOS/CHoCH
  Support/Resistance    +4  price at key S/R with confluence
  Funding neutral       -2  (crypto only) extreme funding = crowded trade

TRADEABLE TODAY:
  Score >= 6 AND bias != NO_TRADE → tradeable (SMC)
  Score >= 4 (MR trades when ranging)
  Score < 4 → skip today

IF NOTHING SCORES >= 4:
  No trades today. Zero is better than a forced bad trade.

USAGE:
  from engines.instrument_scanner import scanner

  # Run daily ranking
  ranking = scanner.run_daily_scan()

  # Check if specific instrument is tradeable
  ok, reason = scanner.is_tradeable("XAUUSD=X")
  
  # Get top instrument for today
  top = scanner.get_top_instrument()
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger("crave.scanner")


class InstrumentScanner:

    MIN_SCORE_TO_TRADE    = 6   # Standard SMC threshold
    MIN_SCORE_MR_TRADE    = 4   # Mean Reversion threshold (lower — compensated by higher win rate)

    def __init__(self):
        self._today_ranking: list = []
        self._last_scan_date: Optional[str] = None

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN SCAN
    # ─────────────────────────────────────────────────────────────────────────

    def run_daily_scan(self, force: bool = False) -> list:
        """
        Score and rank all tradeable instruments.
        Returns sorted list of {symbol, score, tradeable, reason}.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not force and self._last_scan_date == today and self._today_ranking:
            return self._today_ranking

        logger.info("[Scanner] Running daily instrument scan...")

        from config.config import get_tradeable_symbols
        from concurrent.futures import ThreadPoolExecutor, as_completed
        symbols = get_tradeable_symbols()
        results = []

        # PRIORITY 4: Parallel scoring — each symbol is independent.
        # ThreadPoolExecutor(4) reduces wall time from ~16s to ~4s.
        # Errors are caught per-symbol; one failure doesn't block others.
        def _score_safe(symbol: str) -> dict:
            try:
                return self._score_instrument(symbol)
            except Exception as e:
                logger.error(f"[Scanner] Scoring failed for {symbol}: {e}")
                return {
                    "symbol":    symbol,
                    "score":     0,
                    "tradeable": False,
                    "reason":    f"Error: {e}",
                    "breakdown": {},
                }

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_score_safe, s): s for s in symbols}
            for future in as_completed(futures):
                results.append(future.result())

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)

        self._today_ranking  = results
        self._last_scan_date = today

        # Log and notify
        self._log_and_notify(results)

        return results

    def _score_instrument(self, symbol: str) -> dict:
        """Score a single instrument. Returns score dict."""
        from config.config import get_instrument
        inst_cfg  = get_instrument(symbol)
        score     = 0
        breakdown = {}

        # ── 1. ATR Expansion (+3) ──────────────────────────────────────────
        df = self._get_ohlcv(symbol, "1h", limit=500)
        atr_score = 0
        if df is not None and len(df) >= 50:
            # Calculate full ATR series using Wilder's smoothing
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low']  - df['close'].shift()).abs(),
            ], axis=1).max(axis=1)
            atr_series = tr.ewm(alpha=1.0/14, adjust=False).mean()

            current_atr = float(atr_series.iloc[-1])
            # 20-day average on 1H = last 480 candles (or all available)
            lookback = min(480, len(atr_series) - 1)
            avg_atr = float(atr_series.iloc[-lookback:].mean())

            if avg_atr > 0 and not pd.isna(current_atr) and not pd.isna(avg_atr):
                expansion = current_atr / avg_atr
                if expansion >= 1.4:
                    atr_score = 3
                    breakdown["ATR"] = f"+3 (strongly expanding {expansion:.2f}x)"
                elif expansion >= 1.2:
                    atr_score = 2
                    breakdown["ATR"] = f"+2 (expanding {expansion:.2f}x)"
                elif expansion >= 1.0:
                    atr_score = 1
                    breakdown["ATR"] = f"+1 (normal {expansion:.2f}x)"
                else:
                    breakdown["ATR"] = f"+0 (contracting {expansion:.2f}x - avoid)"
            else:
                breakdown["ATR"] = "+0 (insufficient data)"
            score += atr_score
        else:
            breakdown["ATR"] = "+0 (no data)"

        # ── 2. Liquidity Proximity (+2) ────────────────────────────────────
        liq_score = 0
        if df is not None and len(df) >= 20:
            liq_score = self._check_liquidity_proximity(df, symbol)
            score    += liq_score
            breakdown["Liquidity"] = f"+{liq_score}"

        # ── 3. Bias Strength (+3) ──────────────────────────────────────────
        bias_score = 0
        direction = None
        try:
            from engines.daily_bias_engine import bias_engine
            bias = bias_engine.get_bias(symbol)
            if bias:
                bias_val = bias.get("bias", "NO_TRADE")
                if bias_val == "BUY": direction = "long"
                elif bias_val == "SELL": direction = "short"

                strength = bias.get("strength", 0)
                if bias_val != "NO_TRADE":
                    bias_score = min(strength, 3)
                    breakdown["Bias"] = f"+{bias_score} ({bias_val} str={strength})"
                else:
                    breakdown["Bias"] = "+0 (NO_TRADE)"
        except Exception as e:
            breakdown["Bias"] = f"+0 (error: {e})"
        score += bias_score

        # ── 4. Clean structure (+1) ────────────────────────────────────────
        structure_score = 0
        if df is not None and len(df) >= 30:
            # Check that last 3 candles are not high-wick dojis (indecision)
            last3       = df.tail(3)
            bodies      = (last3['close'] - last3['open']).abs()
            ranges      = last3['high'] - last3['low']
            body_ratio  = (bodies / ranges.replace(0, np.nan)).mean()
            if not pd.isna(body_ratio) and body_ratio > 0.4:
                structure_score = 1
                breakdown["Structure"] = "+1 (clean candles)"
            else:
                breakdown["Structure"] = "+0 (doji/indecision)"
        score += structure_score

        # ── 5. Support & Resistance (+4) ───────────────────────────────────
        sr_score = 0
        if df is not None and len(df) >= 50:
            sr_score = self._check_support_resistance(df, symbol, direction)
            score   += sr_score
            breakdown["S/R"] = f"+{sr_score}"

        # ── 6. Funding rate penalty (crypto, -2) ──────────────────────────
        funding_penalty = 0
        if inst_cfg.get("funding_check"):
            try:
                from core.data_agent import get_data_agent
                da   = get_data_agent()
                rate = da.get_funding_rate(symbol)
                if rate.get("available"):
                    r = abs(rate.get("funding_rate_pct", 0))
                    if r > 0.05:
                        funding_penalty = -2
                        breakdown["Funding"] = f"-2 (extreme: {r:.3f}%)"
                    else:
                        breakdown["Funding"] = "+0 (neutral)"
            except Exception:
                pass
        score += funding_penalty

        # ── Final decision ────────────────────────────────────────────────
        # Two tiers:
        #   SMC trades:  score >= 6 AND bias present (directional trend needed)
        #   MR  trades:  score >= 4 (ranging sessions — no direction required)
        smc_tradeable = score >= self.MIN_SCORE_TO_TRADE and bias_score > 0
        mr_tradeable  = score >= self.MIN_SCORE_MR_TRADE and not smc_tradeable
        tradeable     = smc_tradeable or mr_tradeable

        # ── IMP-01 / IMP-02 Filters ───────────────────────────────────────
        filter_reason = None
        if tradeable:
            vix_ok, vix_reason = self._check_vix_regime(symbol)
            if not vix_ok:
                tradeable = False
                filter_reason = vix_reason
                
            if tradeable and direction:
                dxy_ok, dxy_reason = self._check_dxy_gate(symbol, direction)
                if not dxy_ok:
                    tradeable = False
                    filter_reason = dxy_reason

        if not tradeable:
            if filter_reason:
                reason = filter_reason
            elif bias_score == 0 and score < self.MIN_SCORE_MR_TRADE:
                reason = "No clear daily bias"
            elif score < self.MIN_SCORE_TO_TRADE:
                reason = f"Score {score} < minimum {self.MIN_SCORE_TO_TRADE}"
            else:
                reason = "Below threshold"
        else:
            reason = f"Score {score}/13 - tradeable"

        return {
            "symbol":    symbol,
            "score":     score,
            "tradeable": tradeable,
            "reason":    reason,
            "breakdown": breakdown,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # SUPPORT & RESISTANCE CHECK (+4 max)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_support_resistance(self, df: pd.DataFrame,
                                   symbol: str,
                                   direction: Optional[str] = None) -> int:
        """
        Detect key Support and Resistance levels using pivot points,
        round numbers, and multi-touch validation.
        
        Returns 0-4 points based on S/R confluence:
          +4 = price at a multi-touch S/R level with direction alignment
          +3 = price at a strong S/R level (3+ touches)
          +2 = price near a moderate S/R level (2 touches)
          +1 = price near a round number or single pivot
          +0 = no significant S/R nearby
        """
        current = float(df['close'].iloc[-1])
        atr     = self._wilder_atr(df, 14)
        if atr == 0:
            return 0

        # Proximity: within 0.5× ATR of the level
        proximity = atr * 0.5
        sr_points = 0

        # ── 1. Pivot-based S/R (swing highs/lows with touch counting) ────
        levels = self._find_sr_levels(df, window=10)
        
        best_level_score = 0
        best_level_info  = None
        
        for level_price, touch_count, level_type in levels:
            dist = abs(current - level_price)
            if dist > proximity:
                continue
            
            # Direction alignment bonus
            direction_aligned = False
            if direction == "long" and level_type == "support":
                direction_aligned = True
            elif direction == "short" and level_type == "resistance":
                direction_aligned = True
            
            level_score = 0
            if touch_count >= 3 and direction_aligned:
                level_score = 4  # Multi-touch + direction aligned = maximum
            elif touch_count >= 3:
                level_score = 3  # Strong S/R (3+ touches)
            elif touch_count >= 2:
                level_score = 2  # Moderate S/R (2 touches)
            else:
                level_score = 1  # Single pivot
            
            if level_score > best_level_score:
                best_level_score = level_score
                best_level_info  = (level_price, touch_count, level_type)
        
        sr_points = best_level_score

        # ── 2. Round number proximity bonus (+1) ──────────────────────────
        # Gold: round to $10, Forex: round to 0.0100, Crypto: round to $1000/$100
        round_bonus = self._check_round_number(current, symbol, atr)
        if round_bonus and sr_points < 4:
            sr_points = min(sr_points + 1, 4)

        return sr_points

    def _find_sr_levels(self, df: pd.DataFrame, window: int = 10) -> list:
        """
        Find S/R levels by detecting swing highs/lows and counting touches.
        
        Returns list of (price, touch_count, type) tuples.
        type = 'support' or 'resistance'
        """
        highs = df['high'].values
        lows  = df['low'].values
        closes = df['close'].values
        current = closes[-1]
        atr = self._wilder_atr(df, 14)
        if atr == 0:
            return []
        
        # Tolerance for "touching" a level: 0.3% of price or 0.2× ATR
        touch_tol = min(current * 0.003, atr * 0.2)
        
        levels = []  # (price, type)
        
        # Find swing highs (resistance candidates)
        for i in range(window, len(df) - window):
            if highs[i] == max(highs[i-window:i+window+1]):
                levels.append((float(highs[i]), "resistance"))
        
        # Find swing lows (support candidates)
        for i in range(window, len(df) - window):
            if lows[i] == min(lows[i-window:i+window+1]):
                levels.append((float(lows[i]), "support"))
        
        # Cluster nearby levels (within touch_tol)
        clustered = []
        used = set()
        for i, (price_i, type_i) in enumerate(levels):
            if i in used:
                continue
            cluster_prices = [price_i]
            cluster_type   = type_i
            used.add(i)
            for j, (price_j, type_j) in enumerate(levels):
                if j in used:
                    continue
                if abs(price_i - price_j) <= touch_tol:
                    cluster_prices.append(price_j)
                    used.add(j)
            
            avg_price   = sum(cluster_prices) / len(cluster_prices)
            touch_count = len(cluster_prices)
            clustered.append((avg_price, touch_count, cluster_type))
        
        # Sort by touch count descending
        clustered.sort(key=lambda x: x[1], reverse=True)
        return clustered[:10]  # Top 10 levels

    def _check_round_number(self, price: float, symbol: str,
                             atr: float) -> bool:
        """Check if price is near a psychologically significant round number."""
        # Determine round number interval based on asset
        sym_upper = symbol.upper()
        if "XAU" in sym_upper or "GOLD" in sym_upper or "GC" in sym_upper:
            interval = 10.0    # Gold: $10 rounds (e.g., 3300, 3310)
        elif any(c in sym_upper for c in ("BTC",)):
            interval = 1000.0  # BTC: $1000 rounds
        elif any(c in sym_upper for c in ("ETH",)):
            interval = 100.0   # ETH: $100 rounds
        elif any(c in sym_upper for c in ("SOL",)):
            interval = 10.0    # SOL: $10 rounds
        elif any(c in sym_upper for c in ("USD", "EUR", "GBP", "AUD", "JPY")):
            interval = 0.0100  # Forex: 100 pip rounds
            if "JPY" in sym_upper:
                interval = 1.0  # JPY pairs: 1.0 rounds
        elif any(c in sym_upper for c in ("NIFTY", "BANKNIFTY")):
            interval = 100.0   # Indian indices
        elif any(c in sym_upper for c in ("RELIANCE", "TCS", "HDFC", "INFY")):
            interval = 50.0    # Indian stocks
        else:
            interval = atr * 5  # Fallback: 5× ATR as "big round"

        if interval == 0:
            return False

        nearest_round = round(price / interval) * interval
        dist = abs(price - nearest_round)
        
        # "Near" = within 0.3× ATR of the round number
        return dist <= atr * 0.3

    # ─────────────────────────────────────────────────────────────────────────
    # LIQUIDITY PROXIMITY CHECK (+2 max, was +3 — tightened)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_liquidity_proximity(self, df: pd.DataFrame,
                                    symbol: str) -> int:
        """
        Check if price is near a significant liquidity level.
        Equal highs/lows or unmitigated FVGs = price magnet = high follow-through.
        
        Tightened vs v10: proximity threshold reduced from 2× ATR to 1× ATR,
        FVG window reduced from 20 to 10 candles for freshness.
        """
        current = df['close'].iloc[-1]
        atr     = self._wilder_atr(df, 14)
        if atr == 0:
            return 0

        proximity_threshold = atr * 1.0   # Tightened from 2× to 1× ATR

        # Equal highs (liquidity pool above)
        recent_highs = df['high'].tail(30).values  # Reduced from 50 to 30
        for i in range(len(recent_highs)):
            for j in range(i + 1, len(recent_highs)):
                if abs(recent_highs[i] - recent_highs[j]) / recent_highs[i] <= 0.001:  # Tighter: 0.1% vs 0.2%
                    level = (recent_highs[i] + recent_highs[j]) / 2
                    if abs(current - level) <= proximity_threshold:
                        return 2   # Near equal highs

        # Equal lows (liquidity pool below)
        recent_lows = df['low'].tail(30).values
        for i in range(len(recent_lows)):
            for j in range(i + 1, len(recent_lows)):
                if abs(recent_lows[i] - recent_lows[j]) / recent_lows[i] <= 0.001:
                    level = (recent_lows[i] + recent_lows[j]) / 2
                    if abs(current - level) <= proximity_threshold:
                        return 2

        # Unmitigated FVG nearby (last 10 candles only — must be fresh)
        for i in range(max(0, len(df)-10), len(df)-2):
            c1_high = df['high'].iloc[i]
            c3_low  = df['low'].iloc[i + 2]
            c3_high = df['high'].iloc[i + 2]
            c1_low  = df['low'].iloc[i]

            if c3_low > c1_high:   # Bullish FVG
                mid = (c1_high + c3_low) / 2
                if abs(current - mid) <= proximity_threshold:
                    return 1

            if c3_high < c1_low:   # Bearish FVG
                mid = (c3_high + c1_low) / 2
                if abs(current - mid) <= proximity_threshold:
                    return 1

        return 0

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC QUERIES
    # ─────────────────────────────────────────────────────────────────────────

    def is_tradeable(self, symbol: str) -> tuple:
        """Returns (tradeable: bool, reason: str)."""
        ranking = self.run_daily_scan()
        for item in ranking:
            if item["symbol"] == symbol:
                return item["tradeable"], item["reason"]
        return False, f"{symbol} not in instrument list"

    def get_top_instrument(self) -> Optional[dict]:
        """Get the highest-scoring tradeable instrument today."""
        ranking = self.run_daily_scan()
        for item in ranking:
            if item["tradeable"]:
                return item
        return None

    def get_tradeable_today(self) -> list:
        """Get all tradeable instruments today, ranked by score."""
        ranking = self.run_daily_scan()
        return [item for item in ranking if item["tradeable"]]

    # ── IMP-01: VIX Regime Filter ────────────────────────────────────────────
    def _check_vix_regime(self, instrument: str) -> tuple:
        if instrument != "SPY":
            return True, ""
        try:
            from core.data_agent import get_data_agent
            df = get_data_agent().get_ohlcv('^VIX', exchange='yfinance', timeframe='1d', limit=5)
            if df is not None and not df.empty:
                vix = df['close'].iloc[-1]
                if vix > 25:
                    msg = f"SPY blocked: VIX={vix:.1f} > 25 (fear regime)"
                    logger.info(f"[Scanner] {msg}")
                    return False, msg
        except Exception as e:
            logger.debug(f"[Scanner] VIX fetch failed: {e}")
            return False, "VIX fetch failed (safety trigger)"
        # FIX: was missing — VIX <= 25 means OK to trade
        return True, ""

    # ── IMP-02: DXY Inverse Gate ─────────────────────────────────────────────
    def _check_dxy_gate(self, instrument: str, direction: str) -> tuple:
        if instrument not in ("XAUUSD=X", "XAUUSD", "GC=F"):
            return True, ""
        try:
            from core.data_agent import get_data_agent
            df = get_data_agent().get_ohlcv('DX-Y.NYB', exchange='yfinance', timeframe='1d', limit=10)
            if df is not None and len(df) >= 5:
                current_dxy = df['close'].iloc[-1]
                prev_dxy = df['close'].iloc[-5]
                dxy_mom = (current_dxy - prev_dxy) / prev_dxy

                if direction == "long" and dxy_mom > 0.003:
                    msg = f"Gold LONG blocked: DXY rising {dxy_mom*100:.2f}% (5d)"
                    logger.info(f"[Scanner] {msg}")
                    return False, msg
                if direction == "short" and dxy_mom < -0.003:
                    msg = f"Gold SHORT blocked: DXY falling {dxy_mom*100:.2f}% (5d)"
                    logger.info(f"[Scanner] {msg}")
                    return False, msg
        except Exception as e:
            logger.debug(f"[Scanner] DXY fetch failed: {e}")
            return False, "DXY fetch failed (safety trigger)"
        # FIX: was missing — DXY momentum neutral or favourable = OK
        return True, ""

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _wilder_atr(self, df: pd.DataFrame, period: int) -> float:
        if len(df) < period + 1:
            return 0.0
        tr  = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low']  - df['close'].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/period, adjust=False).mean()
        val = atr.iloc[-1]
        return float(val) if not pd.isna(val) else 0.0

    def _get_ohlcv(self, symbol: str, timeframe: str,
                    limit: int) -> Optional[pd.DataFrame]:
        """Fetch OHLCV via MarketDataRouter — single source of truth."""
        try:
            from data.market_data_router import get_data_router
            df = get_data_router().get_ohlcv(symbol, timeframe, limit=limit)
            if df is not None and len(df) >= 10:
                return df
        except Exception as e:
            logger.error(f"[Scanner] OHLCV fetch failed {symbol}: {e}")
        return None

    def _log_and_notify(self, results: list):
        """Log ranking and send Telegram summary."""
        logger.info("[Scanner] Daily ranking:")
        for item in results:
            status = "✅" if item["tradeable"] else "❌"
            logger.info(
                f"  {status} {item['symbol']:15s} "
                f"score={item['score']:4.1f} {item['reason']}"
            )

        try:
            from interfaces.telegram_interface import tg
            tradeable = [i for i in results if i["tradeable"]]
            lines     = [
                "🎯 <b>INSTRUMENT SCAN</b>",
                f"Tradeable today: {len(tradeable)}",
                "━━━━━━━━━━━━━━━",
            ]
            for item in results[:6]:   # show top 6
                status = "✅" if item["tradeable"] else "❌"
                short  = item["symbol"].replace("=X", "").replace("-USD", "")
                lines.append(f"{status} {short}: {item['score']}/13 - {item['reason']}")

            if not tradeable:
                lines.append("\n⚠️ No instruments qualify today. No trades.")

            # Removed Telegram alert per user request, just log it.
            # tg.send("\n".join(lines))
        except Exception as e:
            logger.debug(f"[Scanner] Telegram notify failed: {e}")

    def get_summary_message(self) -> str:
        """Formatted ranking for Telegram /scan command."""
        ranking = self.run_daily_scan()
        if not ranking:
            return "📊 No scan results. Run /scan to refresh."
        lines = [f"🎯 <b>INSTRUMENT RANKING - "
                 f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}</b>",
                 "━━━━━━━━━━━━━━━"]
        for item in ranking:
            status = "✅" if item["tradeable"] else "❌"
            short  = item["symbol"].replace("=X","").replace("-USD","")
            lines.append(f"{status} {short}: {item['score']}/13")
        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────
scanner = InstrumentScanner()


def _get_ema_lean(symbol: str) -> str:
    """
    Helper used by daily_bias_engine._combine_biases() as tiebreaker
    when both weekly and daily bias are neutral/unknown.
    Returns 'bullish', 'bearish', or 'neutral'.
    """
    try:
        df = scanner._get_ohlcv(symbol, "1h", 50)
        if df is None or len(df) < 22:
            return "neutral"
        ema21 = df["close"].ewm(span=21, adjust=False).mean()
        slope = ema21.iloc[-1] - ema21.iloc[-5]   # 5-bar slope
        close = float(df["close"].iloc[-1])
        ema_val = float(ema21.iloc[-1])
        if close > ema_val and slope > 0:
            return "bullish"
        if close < ema_val and slope < 0:
            return "bearish"
        return "neutral"
    except Exception:
        return "neutral"
