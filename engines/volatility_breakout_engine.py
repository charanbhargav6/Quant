"""
Opt-in volatility-breakout strategy.

This is a research candidate, not a profitability claim. It is intentionally
separate from the existing mean-reversion engine because a close outside a
Bollinger band with trend strength is a continuation hypothesis, not a fade.
The trading loop should activate it only in paper mode until instrument-specific
walk-forward validation passes.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("crave.volatility_breakout")


class VolatilityBreakoutEngine:
    """Bollinger expansion + ADX + directional candle confirmation."""

    strategy_id = "volatility_breakout_xau"

    def __init__(self):
        self.bb_period = 20
        self.bb_std = 2.0
        self.atr_period = 14
        self.adx_period = 14
        self.min_adx = 25.0
        self.min_body_atr = 0.50
        self.stop_atr = 1.20
        self.tp1_r = 1.0
        self.tp2_r = 2.0
        self.risk_pct = 0.005

    def analyze(self, symbol: str, df: pd.DataFrame) -> dict:
        """Return a trading-loop-compatible signal or a reason for no signal."""
        required = {"open", "high", "low", "close"}
        if df is None or len(df) < 220 or not required.issubset(df.columns):
            return {"signal": None, "reason": "Insufficient OHLC data for breakout"}

        data = df.copy().reset_index(drop=True)
        close = pd.to_numeric(data["close"], errors="coerce")
        open_ = pd.to_numeric(data["open"], errors="coerce")
        high = pd.to_numeric(data["high"], errors="coerce")
        low = pd.to_numeric(data["low"], errors="coerce")
        if any(series.isna().iloc[-1] for series in (close, open_, high, low)):
            return {"signal": None, "reason": "Latest OHLC row is invalid"}

        mid = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / self.atr_period, adjust=False).mean()

        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        dm_mask = plus_dm > minus_dm
        plus_dm = plus_dm.where(dm_mask, 0.0)
        minus_dm = minus_dm.where(~dm_mask, 0.0)
        plus_di = 100 * plus_dm.ewm(alpha=1 / self.adx_period, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1 / self.adx_period, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / self.adx_period, adjust=False).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

        i = len(data) - 1
        vals = [close.iloc[i], open_.iloc[i], high.iloc[i], low.iloc[i],
                upper.iloc[i], lower.iloc[i], upper.iloc[i - 1], lower.iloc[i - 1],
                atr.iloc[i], adx.iloc[i], rsi.iloc[i]]
        if not all(np.isfinite(float(v)) for v in vals):
            return {"signal": None, "reason": "Indicators are not warmed or finite"}

        current_close = float(close.iloc[i])
        current_open = float(open_.iloc[i])
        current_atr = float(atr.iloc[i])
        current_adx = float(adx.iloc[i])
        current_rsi = float(rsi.iloc[i])
        body = abs(current_close - current_open)
        previous_upper = float(upper.iloc[i - 1])
        previous_lower = float(lower.iloc[i - 1])
        current_upper = float(upper.iloc[i])
        current_lower = float(lower.iloc[i])

        long_break = (
            float(close.iloc[i - 1]) <= previous_upper
            and current_close > current_upper
            and current_close > current_open
            and current_rsi >= 55
        )
        short_break = (
            float(close.iloc[i - 1]) >= previous_lower
            and current_close < current_lower
            and current_close < current_open
            and current_rsi <= 45
        )

        if current_adx < self.min_adx:
            return {"signal": None, "reason": f"ADX {current_adx:.1f} below {self.min_adx:.1f}"}
        if body < self.min_body_atr * current_atr:
            return {"signal": None, "reason": "Breakout candle body is too small relative to ATR"}
        if not long_break and not short_break:
            return {"signal": None, "reason": "No confirmed first-close Bollinger breakout"}

        direction = "buy" if long_break else "sell"
        risk_dist = max(self.stop_atr * current_atr, (current_upper - current_lower) * 0.25)
        entry = current_close
        if direction == "buy":
            stop = entry - risk_dist
            tp1 = entry + self.tp1_r * risk_dist
            tp2 = entry + self.tp2_r * risk_dist
        else:
            stop = entry + risk_dist
            tp1 = entry - self.tp1_r * risk_dist
            tp2 = entry - self.tp2_r * risk_dist

        confidence = min(95, int(55 + min(25, max(0, current_adx - self.min_adx))
                          + min(15, max(0, body / current_atr * 5))))
        return {
            "signal": direction,
            "symbol": symbol,
            "strategy": self.strategy_id,
            "entry": round(entry, 5),
            "stop_loss": round(stop, 5),
            "take_profit_1": round(tp1, 5),
            "take_profit_2": round(tp2, 5),
            "rr_ratio": self.tp2_r,
            "confidence": confidence,
            "atr": round(current_atr, 5),
            "risk_pct": self.risk_pct,
            "regime": "VOLATILE_TREND",
            "indicators": {
                "adx": round(current_adx, 2),
                "rsi": round(current_rsi, 2),
                "bb_upper": round(current_upper, 5),
                "bb_lower": round(current_lower, 5),
                "body_atr": round(body / current_atr, 3),
            },
            "reason": (
                f"{direction.upper()} first-close Bollinger breakout; "
                f"ADX={current_adx:.1f}, RSI={current_rsi:.1f}, "
                f"body={body / current_atr:.2f} ATR"
            ),
        }


_instance: Optional[VolatilityBreakoutEngine] = None


def get_volatility_breakout_engine() -> VolatilityBreakoutEngine:
    global _instance
    if _instance is None:
        _instance = VolatilityBreakoutEngine()
    return _instance
