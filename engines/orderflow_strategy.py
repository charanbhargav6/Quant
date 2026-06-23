import pandas as pd
import numpy as np
import logging
from typing import Tuple

logger = logging.getLogger("crave.orderflow")

def calculate_volume_profile(df: pd.DataFrame, bins: int = 50) -> dict:
    if len(df) == 0 or 'volume' not in df.columns or df['volume'].sum() == 0:
        return {"poc": None, "vah": None, "val": None, "error": "No volume data"}

    min_price = df['low'].min()
    max_price = df['high'].max()
    
    if min_price == max_price:
        return {"poc": min_price, "vah": min_price, "val": min_price}

    # Use pd.cut to bin the typical prices (H+L+C)/3 weighted by volume
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    volume = df['volume']
    
    # Fast histogram using numpy
    hist, bin_edges = np.histogram(typical_price, bins=bins, weights=volume, range=(min_price, max_price))
    volume_profile = hist

    # Find Point of Control (POC)
    poc_idx = np.argmax(volume_profile)
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx+1]) / 2

    # Calculate Value Area (70% of total volume)
    total_volume = np.sum(volume_profile)
    target_volume = total_volume * 0.70
    
    val_idx = poc_idx
    vah_idx = poc_idx
    current_volume = volume_profile[poc_idx]

    while current_volume < target_volume:
        can_expand_up   = vah_idx < bins - 1
        can_expand_down = val_idx > 0

        if not can_expand_up and not can_expand_down:
            break   # Both boundaries exhausted — stop instead of using -1 sentinel

        vol_above = volume_profile[vah_idx + 1] if can_expand_up   else -1
        vol_below = volume_profile[val_idx - 1] if can_expand_down else -1

        if vol_above >= vol_below and can_expand_up:
            vah_idx += 1
            current_volume += vol_above
        elif can_expand_down:
            val_idx -= 1
            current_volume += vol_below
        else:
            break

    val_price = bin_edges[val_idx]
    vah_price = bin_edges[vah_idx + 1]

    return {
        "poc": round(poc_price, 5),
        "vah": round(vah_price, 5),
        "val": round(val_price, 5),
        "total_volume": total_volume
    }


def analyze_orderflow(df: pd.DataFrame, current_idx: int, lookback: int = 50, bins: int = 50, delta_threshold: int = 20) -> dict:
    """
    Analyzes Volume Profile and Volume Delta for a given point in time.
    Returns a score and grade based on how price reacts to the Value Area.
    """
    if df is None or len(df) < lookback:
        return {"grade": "C", "score": 0, "reason": "Insufficient data"}

    start_idx = max(0, current_idx - lookback + 1)
    window = df.iloc[start_idx : current_idx + 1].copy()
    
    profile = calculate_volume_profile(window, bins=bins)
    if profile.get("poc") is None:
        return {"grade": "C", "score": 0, "reason": "No volume data"}

    poc = profile["poc"]
    vah = profile["vah"]
    val = profile["val"]
    
    current_candle = window.iloc[-1]
    current_price = current_candle["close"]
    
    # Volume Delta approximation for the last 3 candles
    recent = window.tail(3).copy()
    recent['bull_vol'] = np.where(recent['close'] >= recent['open'], recent['volume'], 0)
    recent['bear_vol'] = np.where(recent['close'] < recent['open'],  recent['volume'], 0)
    
    bull_vol = recent['bull_vol'].sum()
    bear_vol = recent['bear_vol'].sum()
    total_vol = bull_vol + bear_vol
    
    if total_vol == 0:
        delta_pct = 0
    else:
        # Range from -100 to +100
        delta_pct = ((bull_vol - bear_vol) / total_vol) * 100

    score = 0
    breakdown = []
    direction = "Neutral"

    # SCORING LOGIC
    # 1. Price vs Value Area
    # If price is at VAL (Value Area Low) -> Good for Buys
    if current_price <= val * 1.002 and current_price >= val * 0.998:
        score += 40
        breakdown.append("Price at Value Area Low (Support)")
        direction = "Bullish"
    # If price is at VAH (Value Area High) -> Good for Sells
    elif current_price >= vah * 0.998 and current_price <= vah * 1.002:
        score += 40
        breakdown.append("Price at Value Area High (Resistance)")
        direction = "Bearish"
    # If price is near POC -> Messy chop zone, avoid
    elif current_price >= poc * 0.998 and current_price <= poc * 1.002:
        score -= 20
        breakdown.append("Price at POC (Chop Zone)")
        direction = "Neutral"
    else:
        # Price is outside value area or in no-man's land
        score += 10
        breakdown.append("Price outside key volume nodes")

    # 2. Volume Delta Confirmation
    if direction == "Bullish" and delta_pct > delta_threshold:
        score += 40
        breakdown.append(f"Strong Bullish Delta Confirmation (+{delta_pct:.1f}%)")
    elif direction == "Bearish" and delta_pct < -delta_threshold:
        score += 40
        breakdown.append(f"Strong Bearish Delta Confirmation ({delta_pct:.1f}%)")
    elif direction != "Neutral":
        score += 10
        breakdown.append(f"Weak/No Delta Confirmation ({delta_pct:.1f}%)")

    # Grades
    if score >= 80:
        grade = "A+"
    elif score >= 50:
        grade = "A"
    elif score >= 30:
        grade = "B"
    else:
        grade = "C"

    return {
        "grade": grade,
        "score": score,
        "direction": direction,
        "breakdown": breakdown,
        "poc": poc,
        "vah": vah,
        "val": val,
        "delta_pct": delta_pct
    }
