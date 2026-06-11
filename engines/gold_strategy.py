"""
CRAVE Gold-Specific Strategy — Trend Pullback + Keltner Breakout
================================================================
Why SMC fails for Gold:
  - Gold's FVGs get swept by macro-driven spikes (rate decisions, DXY moves)
  - Order Blocks are less reliable due to institutional hedging flows
  - Gold whipsaws around SMC zones more than crypto/forex

What works for Gold:
  1. Strong trend-following (Gold trends heavily when it moves)
  2. Pullback entries to moving average support (EMA 21)
  3. Keltner Channel breakouts for momentum confirmation
  4. Session awareness (London/NY = best gold moves)
  5. RSI divergence for avoiding exhausted trends

Strategy: "Gold Trend Pullback"
  TREND:     EMA50 > EMA200 = bullish (and vice versa)
  PULLBACK:  Price touches EMA21 zone (within 1 ATR)
  CONFIRM:   RSI between 40-70 (buy) or 30-60 (sell) — not exhausted
  BREAKOUT:  Candle closes above/below Keltner midline after pullback
  FILTER:    ADX > 20 (trending, not ranging)

  SL:  2.0x ATR from entry
  TP1: 2.0x ATR (1:1 R) — partial close, move SL to breakeven
  TP2: 4.0x ATR (2:1 R) — full close
"""

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger("crave.trading.gold")


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength."""
    high, low, close = df['high'], df['low'], df['close']
    
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    
    # When +DM > -DM, keep +DM, zero -DM (and vice versa)
    mask = plus_dm > minus_dm
    plus_dm  = plus_dm.where(mask, 0)
    minus_dm = minus_dm.where(~mask, 0)
    
    atr = _wilder_atr(df, period)
    
    plus_di  = 100 * _ema(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * _ema(minus_dm, period) / atr.replace(0, np.nan)
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _ema(dx, period)
    
    return adx


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _keltner_channel(df: pd.DataFrame, ema_period: int = 20,
                      atr_mult: float = 2.0, atr_period: int = 14):
    """Returns (mid, upper, lower) Keltner Channel bands."""
    mid   = _ema(df['close'], ema_period)
    atr   = _wilder_atr(df, atr_period)
    upper = mid + atr_mult * atr
    lower = mid - atr_mult * atr
    return mid, upper, lower


def _stochastic_rsi(rsi_series: pd.Series, k_period: int = 14,
                     d_period: int = 3) -> tuple:
    """Stochastic RSI — more sensitive than RSI for pullback timing."""
    min_rsi = rsi_series.rolling(k_period).min()
    max_rsi = rsi_series.rolling(k_period).max()
    stoch_k = 100 * (rsi_series - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan)
    stoch_d = stoch_k.rolling(d_period).mean()
    return stoch_k, stoch_d


# ── Attach all gold indicators to DataFrame ──────────────────────────────────

def attach_gold_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach all gold-specific indicators. Call once on full DataFrame."""
    df = df.copy()
    
    # Trend
    df['ema_9']   = _ema(df['close'], 9)
    df['ema_21']  = _ema(df['close'], 21)
    df['ema_50']  = _ema(df['close'], 50)
    df['ema_200'] = _ema(df['close'], 200)
    
    # Volatility
    df['atr_14']  = _wilder_atr(df, 14)
    
    # Momentum
    df['rsi_14']  = _rsi(df['close'], 14)
    df['adx_14']  = _adx(df, 14)
    
    # Keltner Channel
    df['kc_mid'], df['kc_upper'], df['kc_lower'] = _keltner_channel(df)
    
    # Stochastic RSI
    df['stoch_k'], df['stoch_d'] = _stochastic_rsi(df['rsi_14'])
    
    # Price position relative to EMA 21
    df['dist_to_ema21'] = (df['close'] - df['ema_21']) / df['atr_14'].replace(0, np.nan)
    
    # Candle body strength (strong close = trend continuation)
    df['body_pct'] = (df['close'] - df['open']).abs() / (df['high'] - df['low']).replace(0, np.nan)
    
    # Volume relative to 20-period average
    if 'volume' in df.columns and df['volume'].sum() > 0:
        df['vol_ratio'] = df['volume'] / df['volume'].rolling(20).mean().replace(0, np.nan)
    else:
        df['vol_ratio'] = 1.0
    
    return df


# ── Signal Detection ─────────────────────────────────────────────────────────

def analyze_gold(df: pd.DataFrame, i: int) -> dict:
    """
    Analyze gold at candle index i. Returns a signal dict compatible with
    the backtest/live trading framework.
    
    Returns dict with keys:
      - direction: "buy" / "sell" / None
      - confidence: 0-100
      - grade: "A+" / "A" / "B+" / "B" / None
      - reason: human-readable explanation
      - macro_trend: "Bullish" / "Bearish" / "Unknown"
    """
    if i < 200 or i >= len(df):
        return {"direction": None, "reason": "Insufficient warmup"}
    
    c = df.iloc[i]
    prev = df.iloc[i-1]
    
    # Extract indicators
    close    = c['close']
    ema_9    = c['ema_9']
    ema_21   = c['ema_21']
    ema_50   = c['ema_50']
    ema_200  = c['ema_200']
    atr      = c['atr_14']
    rsi      = c['rsi_14']
    adx      = c['adx_14']
    kc_mid   = c['kc_mid']
    kc_upper = c['kc_upper']
    kc_lower = c['kc_lower']
    stoch_k  = c['stoch_k']
    stoch_d  = c['stoch_d']
    dist_ema = c['dist_to_ema21']
    body_pct = c['body_pct']
    vol_r    = c['vol_ratio']
    
    # Check for NaN
    if any(pd.isna(x) for x in [ema_50, ema_200, atr, rsi, adx]):
        return {"direction": None, "reason": "NaN indicators"}
    
    if atr == 0:
        return {"direction": None, "reason": "Zero ATR"}
    
    # ── 1. TREND DETERMINATION ────────────────────────────────────────────
    bullish_trend = ema_50 > ema_200 and close > ema_200
    bearish_trend = ema_50 < ema_200 and close < ema_200
    
    if not bullish_trend and not bearish_trend:
        return {
            "direction": None,
            "macro_trend": "Unknown",
            "reason": "No clear trend (EMA 50/200 not aligned)"
        }
    
    macro_trend = "Bullish" if bullish_trend else "Bearish"
    
    # ── 2. ADX TREND STRENGTH FILTER ──────────────────────────────────────
    # ADX < 18 = ranging market, avoid
    if adx < 18:
        return {
            "direction": None,
            "macro_trend": macro_trend,
            "reason": f"Weak trend (ADX={adx:.1f} < 18)"
        }
    
    # ── 3. PULLBACK DETECTION ─────────────────────────────────────────────
    # Price must have recently pulled back to EMA 21 zone
    # "Recently" = within last 5 candles, distance < 1.5 ATR from EMA 21
    
    pullback_detected = False
    pullback_depth = 0
    
    lookback = min(5, i)
    for j in range(i - lookback, i + 1):
        row = df.iloc[j]
        dist = abs(row['close'] - row['ema_21']) / atr
        if dist < 1.5:
            pullback_detected = True
            pullback_depth = max(pullback_depth, dist)
    
    # ── 4. SIGNAL GENERATION ──────────────────────────────────────────────
    
    direction = None
    confidence = 0
    reasons = []
    
    if bullish_trend:
        # BUY conditions:
        #   a) Price pulled back to EMA 21 and is now bouncing up
        #   b) Close is above EMA 21 (bounce confirmed)
        #   c) RSI is 40-70 (not overbought, has room to run)
        #   d) Candle closed with strength (body > 40% of range)
        
        bounce_up = close > ema_21 and prev['close'] <= prev['ema_21'] * 1.002
        near_ema  = abs(dist_ema) < 1.5 and close > ema_21
        ema_cross = ema_9 > ema_21 and df.iloc[i-1]['ema_9'] <= df.iloc[i-1]['ema_21']
        kc_breakout = close > kc_mid and prev['close'] <= prev['kc_mid']
        
        if pullback_detected and (bounce_up or near_ema or ema_cross or kc_breakout):
            if 35 <= rsi <= 75:
                direction = "buy"
                
                # Confidence scoring
                confidence = 30  # base
                
                if adx > 25:
                    confidence += 10
                    reasons.append(f"Strong trend (ADX={adx:.0f})")
                if adx > 35:
                    confidence += 5
                
                if bounce_up:
                    confidence += 15
                    reasons.append("EMA21 bounce confirmed")
                elif near_ema:
                    confidence += 10
                    reasons.append("Near EMA21 support")
                
                if ema_cross:
                    confidence += 10
                    reasons.append("EMA 9/21 bullish cross")
                
                if kc_breakout:
                    confidence += 10
                    reasons.append("Keltner midline breakout")
                
                if body_pct > 0.6:
                    confidence += 5
                    reasons.append("Strong candle body")
                
                if vol_r > 1.2:
                    confidence += 5
                    reasons.append("Above-average volume")
                
                if not pd.isna(stoch_k) and stoch_k > stoch_d and stoch_k < 80:
                    confidence += 5
                    reasons.append("Stoch RSI bullish")
                
                if close > kc_upper:
                    confidence += 5
                    reasons.append("Above Keltner upper")
    
    elif bearish_trend:
        # SELL conditions (mirror of buy)
        bounce_down = close < ema_21 and prev['close'] >= prev['ema_21'] * 0.998
        near_ema    = abs(dist_ema) < 1.5 and close < ema_21
        ema_cross   = ema_9 < ema_21 and df.iloc[i-1]['ema_9'] >= df.iloc[i-1]['ema_21']
        kc_breakout = close < kc_mid and prev['close'] >= prev['kc_mid']
        
        if pullback_detected and (bounce_down or near_ema or ema_cross or kc_breakout):
            if 25 <= rsi <= 65:
                direction = "sell"
                
                confidence = 30
                
                if adx > 25:
                    confidence += 10
                    reasons.append(f"Strong trend (ADX={adx:.0f})")
                if adx > 35:
                    confidence += 5
                
                if bounce_down:
                    confidence += 15
                    reasons.append("EMA21 rejection confirmed")
                elif near_ema:
                    confidence += 10
                    reasons.append("Near EMA21 resistance")
                
                if ema_cross:
                    confidence += 10
                    reasons.append("EMA 9/21 bearish cross")
                
                if kc_breakout:
                    confidence += 10
                    reasons.append("Keltner midline breakdown")
                
                if body_pct > 0.6:
                    confidence += 5
                    reasons.append("Strong candle body")
                
                if vol_r > 1.2:
                    confidence += 5
                    reasons.append("Above-average volume")
                
                if not pd.isna(stoch_k) and stoch_k < stoch_d and stoch_k > 20:
                    confidence += 5
                    reasons.append("Stoch RSI bearish")
                
                if close < kc_lower:
                    confidence += 5
                    reasons.append("Below Keltner lower")
    
    if direction is None:
        return {
            "direction": None,
            "macro_trend": macro_trend,
            "reason": "No pullback signal"
        }
    
    # ── 5. GRADE ASSIGNMENT ───────────────────────────────────────────────
    if confidence >= 70:
        grade = "A+"
    elif confidence >= 55:
        grade = "A"
    elif confidence >= 40:
        grade = "B+"
    else:
        grade = "B"
    
    return {
        "direction": direction,
        "macro_trend": macro_trend,
        "confidence": min(confidence, 100),
        "grade": grade,
        "reason": " | ".join(reasons),
        "atr": atr,
        "entry": close,
    }


# ── Convenience: Full analysis matching strategy_agent interface ──────────

def analyze_gold_context(symbol: str, df: pd.DataFrame, i: int) -> dict:
    """
    Drop-in replacement for strategy_agent.analyze_market_context() but
    specialized for gold. Returns the same dict format.
    """
    signal = analyze_gold(df, i)
    
    if signal["direction"] is None:
        return {
            "Macro_Trend": signal.get("macro_trend", "Unknown"),
            "Confidence_Pct": 0,
            "Structure_Score": "C",
            "Current_Price": df.iloc[i]['close'] if i < len(df) else 0,
            "Direction": "neutral",
            "Reason": signal.get("reason", "No signal"),
        }
    
    return {
        "Macro_Trend": signal["macro_trend"],
        "Confidence_Pct": signal["confidence"],
        "Structure_Score": f"Grade {signal['grade']}",
        "Current_Price": signal["entry"],
        "Direction": signal["direction"],
        "Reason": signal["reason"],
        "ATR": signal["atr"],
    }
