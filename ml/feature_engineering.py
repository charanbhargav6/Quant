"""
CRAVE v10.0 — ML Feature Engineering
=======================================
Extracts and stores features at every signal — whether traded or not.
Builds the training dataset automatically during paper trading.

After 500+ trades, these features train:
  1. RegimeClassifier   — is the market trending/ranging/volatile?
  2. SetupQualityModel  — what's the real probability this setup wins?
  3. TPExtensionModel   — will price reach the next volume node?

FEATURES CAPTURED:
  Price features:
    - ATR as % of price (volatility)
    - Distance to nearest swing high/low (structure proximity)
    - Position in premium/discount range (0-1 scale)
    - EMA stack alignment (21/50/200)
    - Distance from PoC (volume profile)

  Signal features:
    - FVG hit (bool)
    - OB hit (bool)
    - Liquidity sweep detected (bool)
    - RSI divergence type
    - Volume delta signal
    - Structure event (BOS/CHoCH)
    - Confluence score (0-100)

  Context features:
    - Session (London/NY/Asian encoded as 0/1/2)
    - Day of week (Monday=0)
    - Hour of day (UTC)
    - Funding rate (crypto only)
    - ATR expansion ratio (current/20d avg)

  Outcome (filled in later when trade closes):
    - r_multiple (continuous: -1.0 to +3.0+)
    - outcome_class (0=loss, 1=tp1_be, 2=tp2_win, 3=runner)
"""

import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.ml.features")


def extract_features(symbol: str,
                      df_1h: pd.DataFrame,
                      context: dict,
                      session_name: str = "unknown") -> dict:
    """
    Extract all features for a single signal observation.
    Returns a flat dict suitable for ML training.

    This is called by trading_loop._log_signal() on every signal.
    The features are stored in the ml_features table.
    Outcome is filled in later when the trade resolves.

    Parameters:
      symbol:       Instrument symbol
      df_1h:        1H OHLCV DataFrame at signal time
      context:      Output from strategy_agent.analyze_market_context()
      session_name: 'london' / 'ny' / 'asian'
    """
    features = {}
    now = datetime.now(timezone.utc)
    raw_signal_time = context.get("signal_time") if isinstance(context, dict) else None
    try:
        if raw_signal_time:
            parsed = pd.Timestamp(raw_signal_time).to_pydatetime()
            now = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except Exception:
        logger.debug("[ML] Invalid signal_time; using current UTC time")

    # ── Time features ─────────────────────────────────────────────────────
    features["utc_hour"]    = now.hour
    features["day_of_week"] = now.weekday()   # 0=Mon, 4=Fri
    features["session"]     = {"london": 0, "ny": 1, "asian": 2}.get(
        session_name.lower(), -1
    )

    # ── Price / volatility features ───────────────────────────────────────
    try:
        close    = df_1h['close'].iloc[-1]
        features["close_price"] = float(close)

        # ATR as % of price
        atr = _calc_atr(df_1h, 14)
        features["atr_pct"] = round(atr / close * 100, 4) if close > 0 else 0

        # ATR expansion ratio (current ATR / 20-day avg ATR)
        if len(df_1h) >= 480:   # 20 days × 24 hours
            atr_20d = _calc_atr(df_1h.tail(480), 14)
            features["atr_expansion"] = round(atr / atr_20d, 3) if atr_20d > 0 else 1.0
        else:
            features["atr_expansion"] = 1.0

        # EMA stack
        ema21  = df_1h['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        ema50  = df_1h['close'].rolling(50).mean().iloc[-1]
        ema200 = df_1h['close'].rolling(200).mean().iloc[-1]

        features["above_ema21"]  = int(close > ema21)
        features["above_ema50"]  = int(close > ema50) if not pd.isna(ema50) else 0
        features["above_ema200"] = int(close > ema200) if not pd.isna(ema200) else 0
        features["ema21_above_ema50"] = (
            int(ema21 > ema50) if not pd.isna(ema50) else 0
        )

        # Float EMA values — required by regime_classifier._label_regime()
        # which reads row.get("ema21_close", row.get("ema21", 0)).
        # Without these, _label_regime() receives 0 for all three and labels
        # every non-volatile trade as RANGING regardless of market structure.
        features["ema21"]  = float(ema21)
        features["ema50"]  = float(ema50)  if not pd.isna(ema50)  else 0.0
        features["ema200"] = float(ema200) if not pd.isna(ema200) else 0.0

        # Distance to nearest swing high/low (in ATR units)
        swing_dist = _swing_proximity(df_1h, close, atr)
        features["dist_to_swing_high_atr"] = swing_dist["high"]
        features["dist_to_swing_low_atr"]  = swing_dist["low"]

    except Exception as e:
        logger.debug(f"[ML] Price feature extraction failed: {e}")

    # ── Premium/Discount features ─────────────────────────────────────────
    try:
        pd_zone = context.get("Premium_Discount", {})
        if pd_zone.get("equilibrium") and pd_zone.get("leg_high") and pd_zone.get("leg_low"):
            eq      = pd_zone["equilibrium"]
            leg_hi  = pd_zone["leg_high"]
            leg_lo  = pd_zone["leg_low"]
            leg_rng = leg_hi - leg_lo
            price   = context.get("Current_Price", 0)

            # 0.0 = at leg low (deep discount), 1.0 = at leg high (deep premium)
            if leg_rng > 0:
                pd_position = (price - leg_lo) / leg_rng
                features["pd_position"] = round(pd_position, 3)
            else:
                features["pd_position"] = 0.5
        else:
            features["pd_position"] = 0.5
    except Exception:
        features["pd_position"] = 0.5

    # ── Signal quality features ───────────────────────────────────────────
    features["confidence"]       = context.get("Confidence_Pct", 0)
    features["fvg_hit"]          = int(any(
        f.get("price_inside") for f in context.get("Recent_FVGs", [])
    ))
    features["ob_hit"]           = int(bool(context.get("Order_Blocks")))
    features["sweep_detected"]   = int(
        context.get("Liquidity_Sweep", {}).get("sweep_detected", False)
    )

    # RSI divergence type (encoded)
    rsi_div = context.get("RSI_Divergence", {}).get("divergence", "none")
    features["rsi_divergence_type"] = {
        "none":                          0,
        "Regular Bullish (Buy Pressure Building)":  1,
        "Regular Bearish (Sell Pressure Building)": 2,
        "Hidden Bullish (Trend Continuation Up)":   3,
        "Hidden Bearish (Trend Continuation Down)": 4,
    }.get(rsi_div, 0)

    # Volume signal (encoded)
    vol_signal = context.get("Volume_Delta", {}).get("signal", "Balanced")
    features["volume_signal"] = {
        "Bullish Pressure": 1,
        "Balanced":         0,
        "Bearish Pressure": -1,
        "Flat":             0,
        "No volume data":   0,
    }.get(vol_signal, 0)

    features["volume_expanding"] = int(
        context.get("Volume_Delta", {}).get("volume_expanding", False)
    )

    # Market structure event (encoded)
    struct_event = context.get("Market_Structure", {}).get("event", "")
    features["structure_event"] = _encode_structure(struct_event)

    # ── Macro trend (encoded) ──────────────────────────────────────────────
    macro = context.get("Macro_Trend", "Unknown")
    features["macro_trend"] = {"Bullish": 1, "Bearish": -1}.get(macro, 0)

    # ── Asset class (encoded) ─────────────────────────────────────────────
    from config.config import get_asset_class
    asset = get_asset_class(symbol)
    features["asset_class"] = {
        "crypto": 0, "gold": 1, "silver": 2,
        "forex": 3, "stocks": 4, "indices": 5,
    }.get(asset, -1)

    # ── Crypto-specific: funding rate ─────────────────────────────────────
    features["funding_rate"] = 0.0   # default
    try:
        from config.config import get_instrument
        if get_instrument(symbol).get("funding_check"):
            from core.data_agent import DataAgent
            rate = DataAgent().get_funding_rate(symbol)
            if rate.get("available"):
                features["funding_rate"] = float(
                    rate.get("funding_rate_pct", 0)
                )
    except Exception:
        pass

    return features


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float((df['high'] - df['low']).mean()) if len(df) > 0 else 0.001
    tr  = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    val = tr.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
    return float(val) if not pd.isna(val) else 0.001


def _swing_proximity(df: pd.DataFrame,
                      current: float,
                      atr: float) -> dict:
    """Return distance to nearest swing high/low in ATR units."""
    window = 5
    highs, lows = [], []

    for i in range(window, len(df) - window):
        if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
            highs.append(df['high'].iloc[i])
        if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
            lows.append(df['low'].iloc[i])

    # Distance in ATR units (smaller = closer = more significant)
    if highs and atr > 0:
        nearest_high = min(highs, key=lambda h: abs(h - current))
        dist_high    = round(abs(current - nearest_high) / atr, 2)
    else:
        dist_high = 10.0   # far away

    if lows and atr > 0:
        nearest_low = min(lows, key=lambda l: abs(l - current))
        dist_low    = round(abs(current - nearest_low) / atr, 2)
    else:
        dist_low = 10.0

    return {"high": dist_high, "low": dist_low}


def _encode_structure(event: str) -> int:
    """Encode market structure event as integer."""
    e = event.lower()
    if "bos bullish"    in e: return  2
    if "choch bullish"  in e: return  1
    if "ranging"        in e: return  0
    if "choch bearish"  in e: return -1
    if "bos bearish"    in e: return -2
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# TRAINING DATA EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def get_training_dataframe(min_rows: int = 100) -> Optional[pd.DataFrame]:
    """
    Export ML training data from database as a clean DataFrame.
    Only includes rows where outcome is known (trade has closed).

    Returns None if insufficient data.
    Called by regime_classifier.py and setup_quality_model.py when training.
    """
    try:
        from core.database_manager import db
        rows = db.get_ml_training_data(min_rows=min_rows)

        if rows is None:
            return None

        records = []
        for row in rows:
            try:
                features = json.loads(row["features"])
                features["r_multiple"]    = row["r_multiple"]
                features["outcome"]       = row["outcome"]
                # Outcome class: 0=loss, 1=small win (tp1_be), 2=full win (tp2), 3=runner
                r = row["r_multiple"]
                features["outcome_class"] = (
                    0 if r <= 0 else
                    1 if r <= 0.6 else
                    2 if r <= 2.0 else
                    3
                )
                records.append(features)
            except Exception:
                continue

        if len(records) < min_rows:
            return None

        df = pd.DataFrame(records)
        logger.info(
            f"[ML] Training data ready: {len(df)} rows, "
            f"{df.columns.tolist()}"
        )
        return df

    except Exception as e:
        logger.error(f"[ML] Training data export failed: {e}")
        return None
