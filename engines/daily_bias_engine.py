"""
CRAVE v10.0 - Daily Bias Engine
=================================
Runs once at 06:30 UTC (London pre-market).
Determines directional bias for each instrument for the day.

OUTPUT PER INSTRUMENT:
  bias:               BUY / SELL / NO_TRADE
  strength:           1 (weak) / 2 (moderate) / 3 (strong)
  reason:             Why this bias was set
  daily_invalidation: Price level that kills the bias (hard stop on thesis)
  key_levels:         D1/W1 OBs and FVGs to watch today

BIAS LOGIC (top-down):
  1. Weekly: Is price above/below weekly midpoint?
             Are equal highs (sell-side liquidity) or equal lows (buy-side) nearby?
  2. Daily:  BOS/CHoCH on D1. Daily FVG. Daily OB proximity. EMA21 direction.
  3. Alignment: If weekly and daily agree → strength 3
                If one is unknown/mixed → strength 2 or 1
                If they conflict → NO_TRADE

NO_TRADE DECLARATIONS:
  - Weekly and daily bias conflict
  - Red-folder event within 4 hours for this currency pair
  - ATR is contracting (ranging - SMC strategy underperforms)
  - Previous day closed as a doji on D1 (indecision)
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("crave.bias")


class DailyBiasEngine:

    def __init__(self):
        self._today_bias: dict = {}   # symbol → bias dict, reset each day
        self._last_run_date: Optional[str] = None

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────────

    def run_daily_analysis(self, force: bool = False) -> dict:
        """
        Run bias analysis for all tradeable instruments.
        Caches result for the day - won't re-run unless forced.

        Returns dict of symbol → bias_dict.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not force and self._last_run_date == today and self._today_bias:
            logger.debug("[Bias] Using cached bias for today.")
            return self._today_bias

        logger.info("[Bias] Running daily analysis for all instruments...")

        from config.config import get_tradeable_symbols
        symbols  = get_tradeable_symbols()
        results  = {}

        for symbol in symbols:
            try:
                bias = self.analyse_instrument(symbol)
                if bias:
                    results[symbol] = bias
                    # Persist to database
                    self._save_bias(symbol, bias, today)
            except Exception as e:
                logger.error(f"[Bias] Analysis failed for {symbol}: {e}")

        self._today_bias    = results
        self._last_run_date = today

        # Send Telegram summary
        self._send_bias_summary(results)

        logger.info(
            f"[Bias] Complete. "
            f"BUY={sum(1 for b in results.values() if b['bias']=='BUY')} | "
            f"SELL={sum(1 for b in results.values() if b['bias']=='SELL')} | "
            f"NO_TRADE={sum(1 for b in results.values() if b['bias']=='NO_TRADE')}"
        )

        return results

    def get_bias(self, symbol: str) -> Optional[dict]:
        """Get today's bias for a specific instrument."""
        # Check in-memory cache first
        if symbol in self._today_bias:
            return self._today_bias[symbol]

        # Fall back to database
        try:
            from core.database_manager import db
            return db.get_today_bias(symbol)
        except Exception:
            return None

    def is_tradeable_today(self, symbol: str, direction: str) -> bool:
        """
        Quick check: is this direction allowed today based on bias?
        direction: "buy" / "sell"
        """
        bias = self.get_bias(symbol)
        if not bias:
            return False   # No bias set = no trade

        b = bias.get("bias", "NO_TRADE")
        if b == "NO_TRADE":
            return False
        if b == "BUY" and direction.lower() in ("buy", "long"):
            return True
        if b == "SELL" and direction.lower() in ("sell", "short"):
            return True
        return False   # Direction conflicts with bias

    # ─────────────────────────────────────────────────────────────────────────
    # INSTRUMENT ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def analyse_instrument(self, symbol: str) -> Optional[dict]:
        """Full top-down analysis for one instrument."""
        logger.info(f"[Bias] Analysing {symbol}...")

        # Fetch D1 and W1 data
        df_daily  = self._get_ohlcv(symbol, "1d", limit=100)
        df_weekly = self._get_ohlcv(symbol, "1wk", limit=52)

        if df_daily is None or len(df_daily) < 20:
            logger.warning(f"[Bias] Insufficient daily data for {symbol}")
            return None

        # ── Weekly analysis ────────────────────────────────────────────────
        weekly_bias = self._analyse_weekly(df_weekly) if (
            df_weekly is not None and len(df_weekly) >= 4
        ) else {"direction": "unknown", "reason": "insufficient weekly data"}

        # ── Daily analysis ─────────────────────────────────────────────────
        daily_bias = self._analyse_daily(df_daily)

        # ── Zone 1: Liquidity Void Scanner ────────────────────────────────
        # Check for unfilled FVGs > 7 days old that act as price magnets.
        # If trade direction points toward a void -> add strength bonus.
        void_bonus = 0
        try:
            from intelligence.order_flow import (
                scan_liquidity_voids, get_void_bias_bonus
            )
            voids = scan_liquidity_voids(df_daily, min_age_days=7)
            if voids:
                bias_direction = daily_bias.get("direction", "unknown")
                void_result = get_void_bias_bonus(
                    voids, bias_direction,
                    float(df_daily["close"].iloc[-1])
                )
                void_bonus = void_result.get("bonus", 0)
                if void_bonus > 0:
                    logger.info(
                        f"[Bias] {symbol}: Liquidity void bonus +{void_bonus} - "
                        f"{void_result.get('reason','')}"
                    )
        except Exception as e:
            logger.debug(f"[Bias] Void scanner error (non-fatal): {e}")

        # ── Combine ────────────────────────────────────────────────────────
        final_bias, strength, reason = self._combine_biases(
            weekly_bias, daily_bias, symbol
        )
        # Apply void bonus to strength
        if void_bonus > 0:
            strength = min(3, strength + void_bonus)

        # ── Key levels ────────────────────────────────────────────────────
        key_levels        = self._find_key_levels(df_daily)
        invalidation_level = self._find_invalidation(
            df_daily, final_bias
        )

        # ── Calendar check ─────────────────────────────────────────────────
        calendar_block = self._check_calendar(symbol)
        if calendar_block:
            final_bias = "NO_TRADE"
            strength   = 0
            reason     = f"Red folder event within 4h: {calendar_block}"

        result = {
            "bias":               final_bias,
            "strength":           strength,
            "reason":             reason,
            "daily_invalidation": invalidation_level,
            "key_levels":         key_levels,
            "weekly_bias":        weekly_bias.get("direction", "unknown"),
            "daily_bias":         daily_bias.get("direction", "unknown"),
            "analysed_at":        datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            f"[Bias] {symbol}: {final_bias} (strength {strength}/3) - {reason}"
        )
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # WEEKLY ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def _analyse_weekly(self, df: pd.DataFrame) -> dict:
        """Determine weekly directional bias."""
        if len(df) < 4:
            return {"direction": "unknown", "reason": "insufficient data"}

        current = df['close'].iloc[-1]

        # Weekly range midpoint (ICT premium/discount)
        recent_high = df['high'].tail(12).max()   # ~3 months
        recent_low  = df['low'].tail(12).min()
        midpoint    = (recent_high + recent_low) / 2

        # Price position relative to weekly midpoint
        in_discount = current < midpoint
        in_premium  = current > midpoint

        # Weekly EMA trend
        ema20_weekly = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
        above_ema20  = current > ema20_weekly

        # Equal highs/lows (liquidity pools)
        # Equal highs: two or more weekly highs within 0.2% of each other
        last_highs = df['high'].tail(8).values
        equal_highs = self._find_equal_levels(last_highs, tolerance=0.002)
        last_lows  = df['low'].tail(8).values
        equal_lows  = self._find_equal_levels(last_lows,  tolerance=0.002)

        # Weekly BOS
        prev_high = df['high'].iloc[-3:-1].max()
        prev_low  = df['low'].iloc[-3:-1].min()
        broke_high = df['close'].iloc[-1] > prev_high
        broke_low  = df['close'].iloc[-1] < prev_low

        # Determine direction
        bullish_signals = sum([
            in_discount,
            above_ema20,
            broke_high,
            bool(equal_lows),   # lows swept = potential reversal up
        ])
        bearish_signals = sum([
            in_premium,
            not above_ema20,
            broke_low,
            bool(equal_highs),  # highs swept = potential reversal down
        ])

        if bullish_signals >= 3:
            direction = "bullish"
            reason    = f"Price in discount, above W-EMA20, {bullish_signals}/4 bullish signals"
        elif bearish_signals >= 3:
            direction = "bearish"
            reason    = f"Price in premium, below W-EMA20, {bearish_signals}/4 bearish signals"
        else:
            direction = "neutral"
            reason    = f"Mixed signals ({bullish_signals}B/{bearish_signals}S)"

        return {
            "direction":     direction,
            "reason":        reason,
            "equal_highs":   [round(h, 5) for h in equal_highs],
            "equal_lows":    [round(l, 5) for l in equal_lows],
            "midpoint":      round(midpoint, 5),
            "in_discount":   in_discount,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # DAILY ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def _analyse_daily(self, df: pd.DataFrame) -> dict:
        """Determine daily directional bias."""
        if len(df) < 20:
            return {"direction": "unknown", "reason": "insufficient data"}

        current = df['close'].iloc[-1]
        prev    = df.iloc[-2]   # yesterday's candle

        # EMA stack
        ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        ema50 = df['close'].rolling(50).mean().iloc[-1]
        above_ema21 = current > ema21
        ema21_above_ema50 = ema21 > ema50 if not pd.isna(ema50) else None

        # Daily BOS (simplified: close beyond recent swing)
        window   = 5
        highs    = []
        lows     = []
        for i in range(window, len(df) - window):
            if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
                highs.append(df['high'].iloc[i])
            if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
                lows.append(df['low'].iloc[i])

        broke_above = (highs and current > highs[-1]) if highs else False
        broke_below = (lows  and current < lows[-1])  if lows  else False

        # Daily FVG check (last 3 daily candles)
        fvg_bullish = (len(df) >= 3 and
                       df['low'].iloc[-1] > df['high'].iloc[-3])
        fvg_bearish = (len(df) >= 3 and
                       df['high'].iloc[-1] < df['low'].iloc[-3])

        # ATR contraction (ranging detection)
        atr_14 = self._calc_atr(df, 14)
        atr_50 = self._calc_atr(df, 50)
        atr_contracting = (atr_14 < atr_50 * 0.7) if atr_50 > 0 else False

        # Previous daily candle quality
        prev_body = abs(prev['close'] - prev['open'])
        prev_range = prev['high'] - prev['low']
        is_doji = (prev_body < prev_range * 0.2) if prev_range > 0 else False

        # Score
        bullish_signals = sum([
            above_ema21,
            ema21_above_ema50 if ema21_above_ema50 is not None else False,
            broke_above,
            fvg_bullish,
        ])
        bearish_signals = sum([
            not above_ema21,
            not ema21_above_ema50 if ema21_above_ema50 is not None else False,
            broke_below,
            fvg_bearish,
        ])

        if atr_contracting:
            return {
                "direction": "neutral",
                "reason":    "ATR contracting - ranging market, avoid",
                "atr_contracting": True,
            }

        if is_doji:
            return {
                "direction": "neutral",
                "reason":    "Daily doji - indecision, wait for confirmation",
                "is_doji":   True,
            }

        if bullish_signals >= 3:
            direction = "bullish"
            reason    = f"D1 bullish: {bullish_signals}/4 signals (EMA stack, structure)"
        elif bearish_signals >= 3:
            direction = "bearish"
            reason    = f"D1 bearish: {bearish_signals}/4 signals (EMA stack, structure)"
        else:
            direction = "neutral"
            reason    = f"D1 mixed: {bullish_signals}B/{bearish_signals}S"

        return {
            "direction":        direction,
            "reason":           reason,
            "ema21":            round(ema21, 5),
            "atr_contracting":  atr_contracting,
            "broke_structure":  broke_above or broke_below,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # BIAS COMBINATION
    # ─────────────────────────────────────────────────────────────────────────

    def _combine_biases(self, weekly: dict, daily: dict,
                         symbol: str) -> tuple:
        """
        Combine weekly and daily bias into a final verdict.
        Returns (bias, strength, reason).
        """
        w_dir = weekly.get("direction", "unknown")
        d_dir = daily.get("direction",  "unknown")

        # Both agree strongly
        if w_dir == "bullish" and d_dir == "bullish":
            return "BUY", 3, f"Weekly + Daily both bullish"
        if w_dir == "bearish" and d_dir == "bearish":
            return "SELL", 3, f"Weekly + Daily both bearish"

        # One strong, one neutral
        if w_dir == "bullish" and d_dir == "neutral":
            return "BUY", 2, "Weekly bullish, daily neutral - lean long"
        if w_dir == "neutral" and d_dir == "bullish":
            return "BUY", 2, "Daily bullish, weekly neutral - lean long"
        if w_dir == "bearish" and d_dir == "neutral":
            return "SELL", 2, "Weekly bearish, daily neutral - lean short"
        if w_dir == "neutral" and d_dir == "bearish":
            return "SELL", 2, "Daily bearish, weekly neutral - lean short"

        # Both neutral or unknown — FIX B2: use EMA21 direction as tiebreaker
        # instead of hard NO_TRADE. Gives strength=1 (lowest confidence).
        # This allows mean reversion trades in ranging sessions.
        if w_dir in ("neutral", "unknown") and d_dir in ("neutral", "unknown"):
            # Fall back to simple EMA21 slope to pick a lean direction
            try:
                from engines.instrument_scanner import _get_ema_lean
                lean = _get_ema_lean(symbol)
                if lean == "bullish":
                    return "BUY",  1, "Neutral session - EMA21 lean long (MR mode)"
                elif lean == "bearish":
                    return "SELL", 1, "Neutral session - EMA21 lean short (MR mode)"
            except Exception:
                pass
            return "BUY", 1, "No clear bias - allowing low-confidence MR setups"

        # Conflict: weekly says one thing, daily says opposite
        if (w_dir == "bullish" and d_dir == "bearish") or \
           (w_dir == "bearish" and d_dir == "bullish"):
            return "NO_TRADE", 0, (
                f"Weekly/Daily CONFLICT ({w_dir}/{d_dir}) - "
                f"No trade until alignment"
            )

        return "NO_TRADE", 0, f"Unclear bias (W:{w_dir}, D:{d_dir})"

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _find_key_levels(self, df: pd.DataFrame) -> list:
        """Find significant D1 swing highs/lows to watch today."""
        levels = []
        window = 5
        for i in range(window, len(df) - window):
            high = df['high'].iloc[i]
            low  = df['low'].iloc[i]
            if high == df['high'].iloc[i-window:i+window+1].max():
                levels.append({"type": "resistance", "price": round(high, 5)})
            if low == df['low'].iloc[i-window:i+window+1].min():
                levels.append({"type": "support",    "price": round(low, 5)})

        # Return last 6 levels (most recent)
        recent = sorted(levels, key=lambda x: x["price"])[-6:]
        return [l["price"] for l in recent]

    def _find_invalidation(self, df: pd.DataFrame,
                             bias: str) -> Optional[float]:
        """
        The price level that would invalidate today's bias.
        For BUY bias: the most recent D1 swing low (break = bearish CHoCH)
        For SELL bias: the most recent D1 swing high
        """
        window = 5
        if bias == "BUY":
            lows = []
            for i in range(window, len(df) - window):
                if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
                    lows.append(df['low'].iloc[i])
            return round(lows[-1], 5) if lows else None
        elif bias == "SELL":
            highs = []
            for i in range(window, len(df) - window):
                if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
                    highs.append(df['high'].iloc[i])
            return round(highs[-1], 5) if highs else None
        return None

    def _find_equal_levels(self, prices: np.ndarray,
                            tolerance: float = 0.002) -> list:
        """Find clusters of prices within tolerance% of each other."""
        equals = []
        for i in range(len(prices)):
            for j in range(i + 1, len(prices)):
                if abs(prices[i] - prices[j]) / prices[i] <= tolerance:
                    equals.append(round((prices[i] + prices[j]) / 2, 5))
        return equals

    def _calc_atr(self, df: pd.DataFrame, period: int) -> float:
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
        """Fetch OHLCV via MarketDataRouter — single source of truth.
        
        The router handles Alpaca/Binance primary sources and falls back
        to yfinance with correct symbol mapping (XAUUSD=X → GC=F, etc.).
        No direct yfinance calls here — prevents 404 errors.
        """
        try:
            from data.market_data_router import get_data_router
            df = get_data_router().get_ohlcv(symbol, timeframe, limit=limit)
            if df is not None and len(df) >= 10:
                return df
        except Exception as e:
            logger.error(f"[Bias] OHLCV fetch failed for {symbol} {timeframe}: {e}")
        return None

    def _check_calendar(self, symbol: str) -> Optional[str]:
        """Check if there's a red-folder event in the next 4 hours."""
        try:
            from config.config import get_instrument
            inst      = get_instrument(symbol)
            currencies = inst.get("currencies", ["USD"])

            from core.data_agent import get_data_agent
            da    = get_data_agent()
            result = da.check_red_folder(currencies=currencies, window_mins=240)
            if result.get("is_danger"):
                return result.get("event_name", "unknown event")
        except Exception as e:
            logger.debug(f"[Bias] Calendar check failed for {symbol}: {e}")
        return None

    def _save_bias(self, symbol: str, bias: dict, date: str):
        """Persist bias to database."""
        try:
            from core.database_manager import db
            db.save_daily_bias(
                date=date,
                symbol=symbol,
                bias=bias["bias"],
                strength=bias["strength"],
                reason=bias["reason"],
                invalidation_level=bias.get("daily_invalidation") or 0.0,
                key_levels=bias.get("key_levels", []),
            )
        except Exception as e:
            logger.warning(f"[Bias] DB save failed for {symbol}: {e}")

    def _send_bias_summary(self, results: dict):
        """Send daily bias summary to Telegram."""
        try:
            from interfaces.telegram_interface import tg
            lines = [
                f"📅 <b>DAILY BIAS - "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}</b>",
                "━━━━━━━━━━━━━━━"
            ]
            for symbol, bias in results.items():
                b     = bias.get("bias", "?")
                s     = bias.get("strength", 0)
                emoji = "🟢" if b == "BUY" else "🔴" if b == "SELL" else "⬜"
                stars = "⭐" * s
                short_sym = symbol.replace("=X", "").replace("-USD", "")
                lines.append(f"{emoji} {short_sym}: {b} {stars}")
            tg.send("\n".join(lines))
        except Exception as e:
            logger.debug(f"[Bias] Telegram summary failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PRIORITY 2: Lazy singleton — no crash-on-import if Config not ready
# Use get_bias_engine() everywhere instead of bare bias_engine
# ─────────────────────────────────────────────────────────────────────────────
_bias_engine_instance = None

def get_bias_engine() -> "DailyBiasEngine":
    global _bias_engine_instance
    if _bias_engine_instance is None:
        _bias_engine_instance = DailyBiasEngine()
    return _bias_engine_instance

# Backward-compat: existing imports of `bias_engine` still work via the callable
bias_engine = get_bias_engine()

