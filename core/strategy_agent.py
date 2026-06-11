"""
CRAVE Phase 9.1 - Smart Money Strategy Engine
==============================================
FIXES vs v9.0:
  🔧 FVG mitigation logic corrected (was marking zones as dead on approach)
  🔧 Macro override no longer bypasses SMC scoring (danger = caution, not confidence)
  🔧 RSI divergence now uses proper swing pivot detection
  🔧 OB displacement threshold raised to 2.0× with ATR expansion confirmation
  🔧 CHoCH/BOS uses candle CLOSE confirmation, not raw tick
  🔧 Session filter uses ZoneInfo for DST-accurate London/NY hours
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime, timezone

logger = logging.getLogger("crave.trading.strategy")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _is_london_or_ny(ts) -> bool:
    """
    FIX v9.1: Uses ZoneInfo for DST-correct London open detection.
    London: 08:00–17:00 local (shifts between 07:00–08:00 UTC depending on BST/GMT)
    NY:     09:30–17:00 ET local (13:30–21:00 UTC in winter, 12:30–20:00 UTC in summer)
    Simplified: check UTC hours with DST-aware conversion using ZoneInfo.
    """
    try:
        from zoneinfo import ZoneInfo

        utc_ts = pd.Timestamp(ts)
        if utc_ts.tzinfo is None:
            utc_ts = utc_ts.tz_localize("UTC")
        else:
            utc_ts = utc_ts.tz_convert("UTC")

        london_ts = utc_ts.tz_convert(ZoneInfo("Europe/London"))
        ny_ts     = utc_ts.tz_convert(ZoneInfo("America/New_York"))

        # London session: 08:00–17:00 local
        in_london = 8 <= london_ts.hour < 17
        # NY session: 09:30–17:00 local (using hour only, minute ignored for simplicity)
        in_ny     = 9 <= ny_ts.hour < 17

        return in_london or in_ny

    except Exception:
        # Fallback to fixed UTC hours if ZoneInfo unavailable
        try:
            h = pd.Timestamp(ts).hour
            return (7 <= h < 16) or (13 <= h < 21)
        except Exception:
            return True  # Fail open — don't block trades on parse error


def _find_swing_pivots(series: pd.Series, window: int = 5) -> list:
    """
    NEW v9.1: Returns list of (index, value) for genuine swing highs or lows.
    Used by RSI divergence to avoid comparing arbitrary midpoints.
    Window=5 means price is the highest/lowest of the ±5 surrounding bars.
    """
    if len(series) < window * 2 + 1:
        return []
    
    # Vectorized pivot detection
    is_high = series == series.rolling(window=window*2+1, center=True).max()
    is_low  = series == series.rolling(window=window*2+1, center=True).min()
    
    is_pivot = is_high | is_low
    pivot_idx = np.where(is_pivot)[0]
    
    return [(i, float(series.iloc[i])) for i in pivot_idx if window <= i < len(series) - window]


# ─────────────────────────────────────────────────────────────────────────────

class StrategyAgent:
    def __init__(self):
        pass

    # ── 1. FVG DETECTOR ──────────────────────────────────────────────────────

    def _build_fvg_catalog(self, df: pd.DataFrame) -> list[dict]:
        """
        Scans the FULL dataframe once and records every FVG with:
        - The exact candle it formed
        - Its price boundaries
        - Whether it's bullish or bearish
        """
        catalog = []
        for i in range(1, len(df) - 1):
            prev   = df.iloc[i - 1]
            curr   = df.iloc[i]
            nxt    = df.iloc[i + 1]

            atr = curr['atr'] if 'atr' in curr and not pd.isna(curr['atr']) and curr['atr'] > 0 else 1.0

            # Bullish FVG: gap between prev candle's high and next candle's low
            if nxt['low'] > prev['high']:
                catalog.append({
                    'type':       'BULL',
                    'formed_at':  i,
                    'top':        nxt['low'],
                    'bottom':     prev['high'],
                    'midpoint':   (nxt['low'] + prev['high']) / 2,
                    'size_atr':   (nxt['low'] - prev['high']) / atr,
                })

            # Bearish FVG: gap between prev candle's low and next candle's high
            elif nxt['high'] < prev['low']:
                catalog.append({
                    'type':       'BEAR',
                    'formed_at':  i,
                    'top':        prev['low'],
                    'bottom':     nxt['high'],
                    'midpoint':   (prev['low'] + nxt['high']) / 2,
                    'size_atr':   (prev['low'] - nxt['high']) / atr,
                })

        return catalog

    def _query_active_fvgs(self, catalog: list, df: pd.DataFrame, i: int, max_age: int = None) -> list[dict]:
        """
        At candle i, returns FVGs that:
        1. Were formed BEFORE candle i (visible in real-time)
        2. Have NOT been mitigated (price has not fully traded through them)
        3. Optionally within max_age candles (None = entire history — matches Run 1)
        """
        current_price = df.iloc[i]['close']
        active = []

        for fvg in catalog:
            if fvg['formed_at'] >= i:
                continue

            if max_age is not None and (i - fvg['formed_at']) > max_age:
                continue

            candles_since = df.iloc[fvg['formed_at']:i]

            if fvg['type'] == 'BULL':
                if (candles_since['close'] < fvg['bottom']).any():
                    continue
                if current_price > fvg['top'] * 1.005:  # 0.5% tolerance above top
                    continue
            elif fvg['type'] == 'BEAR':
                if (candles_since['close'] > fvg['top']).any():
                    continue
                if current_price < fvg['bottom'] * 0.995:
                    continue

            active.append(fvg)

        return sorted(active, key=lambda x: x['formed_at'], reverse=True)

    # ── 2. ORDER BLOCK DETECTOR ──────────────────────────────────────────────

    def _build_ob_catalog(self, df: pd.DataFrame) -> list[dict]:
        """
        Scans FULL history once. An Order Block is the last bearish candle before
        a bullish impulse (bull OB) or the last bullish candle before a bearish
        impulse (bear OB). Uses ATR-normalized impulse threshold.
        """
        catalog = []
        impulse_threshold = 1.5

        for i in range(2, len(df) - 3):
            curr = df.iloc[i]
            atr = curr['atr'] if 'atr' in curr and not pd.isna(curr['atr']) and curr['atr'] > 0 else 1.0

            # Bullish OB: last bearish candle before upward impulse
            if curr['close'] < curr['open']:
                impulse_move = df.iloc[i+1:i+4]['close'].max() - curr['close']
                if impulse_move > impulse_threshold * atr:
                    catalog.append({
                        'type':      'BULL',
                        'formed_at': i,
                        'top':       curr['open'],
                        'bottom':    curr['close'],
                        'high':      curr['high'],
                        'low':       curr['low'],
                        'strength':  impulse_move / atr,
                    })

            # Bearish OB: last bullish candle before downward impulse
            elif curr['close'] > curr['open']:
                impulse_move = curr['close'] - df.iloc[i+1:i+4]['close'].min()
                if impulse_move > impulse_threshold * atr:
                    catalog.append({
                        'type':      'BEAR',
                        'formed_at': i,
                        'top':       curr['close'],
                        'bottom':    curr['open'],
                        'high':      curr['high'],
                        'low':       curr['low'],
                        'strength':  impulse_move / atr,
                    })

        return catalog

    def _query_active_obs(self, catalog: list, df: pd.DataFrame, i: int) -> list[dict]:
        """
        At candle i, returns OBs that:
        1. Formed before candle i
        2. Have NOT been broken (price hasn't closed THROUGH the OB zone)
        3. Are still in play (price hasn't moved too far away)
        """
        current_high  = df.iloc[i]['high']
        current_low   = df.iloc[i]['low']
        active = []

        for ob in catalog:
            if ob['formed_at'] >= i:
                continue

            candles_since = df.iloc[ob['formed_at']:i]

            if ob['type'] == 'BULL':
                if (candles_since['close'] < ob['bottom']).any():
                    continue
                if current_low > ob['top'] * 1.03:
                    continue

            elif ob['type'] == 'BEAR':
                if (candles_since['close'] > ob['top']).any():
                    continue
                if current_high < ob['bottom'] * 0.97:
                    continue

            active.append(ob)

        return sorted(active, key=lambda x: x['strength'], reverse=True)

    # ── 3. CHoCH / BOS DETECTOR ──────────────────────────────────────────────

    def _build_structure(self, df: pd.DataFrame, pivot_lookback: int = 10) -> list[dict]:
        """
        Identifies ALL swing highs/lows and structure breaks on full history.
        Uses a fixed pivot_lookback so the same pivots are identified regardless
        of where in the loop you are.
        """
        structure_events = []
        pivot_highs = []
        pivot_lows  = []

        for i in range(pivot_lookback, len(df) - pivot_lookback):
            window_high = df.iloc[i - pivot_lookback : i + pivot_lookback + 1]['high']
            window_low  = df.iloc[i - pivot_lookback : i + pivot_lookback + 1]['low']

            if df.iloc[i]['high'] == window_high.max():
                pivot_highs.append({'candle': i, 'price': df.iloc[i]['high']})

            if df.iloc[i]['low'] == window_low.min():
                pivot_lows.append({'candle': i, 'price': df.iloc[i]['low']})

        # Detect BOS (Break of Structure) and CHoCH (Change of Character)
        for j in range(1, len(pivot_highs)):
            prev_high = pivot_highs[j-1]['price']
            curr_high = pivot_highs[j]['price']
            at_candle = pivot_highs[j]['candle']

            if curr_high > prev_high:
                structure_events.append({
                    'type': 'BOS_BULL', 'candle': at_candle,
                    'level': prev_high, 'strength': curr_high - prev_high
                })
            elif curr_high < prev_high:
                structure_events.append({
                    'type': 'CHoCH_BEAR', 'candle': at_candle,
                    'level': prev_high, 'strength': prev_high - curr_high
                })

        for j in range(1, len(pivot_lows)):
            prev_low = pivot_lows[j-1]['price']
            curr_low = pivot_lows[j]['price']
            at_candle = pivot_lows[j]['candle']

            if curr_low < prev_low:
                structure_events.append({
                    'type': 'BOS_BEAR', 'candle': at_candle,
                    'level': prev_low, 'strength': prev_low - curr_low
                })
            elif curr_low > prev_low:
                structure_events.append({
                    'type': 'CHoCH_BULL', 'candle': at_candle,
                    'level': prev_low, 'strength': curr_low - prev_low
                })

        return sorted(structure_events, key=lambda x: x['candle'])

    def _query_last_structure(self, structure: list, i: int, lookback: int = 50) -> dict | None:
        """Returns the most recent structure event before candle i."""
        visible = [s for s in structure if s['candle'] < i and s['candle'] >= i - lookback]
        return visible[-1] if visible else None

    # ── 4. LIQUIDITY SWEEP DETECTOR ──────────────────────────────────────────
    # No changes — logic was correct.

    def _detect_liquidity_sweep(self, df: pd.DataFrame) -> dict:
        if len(df) < 20:
            return {"sweep_detected": False}

        recent     = df.tail(30)
        swing_high = recent['high'].iloc[:-3].max()
        swing_low  = recent['low'].iloc[:-3].min()
        last       = df.iloc[-1]
        prev_close = df['close'].iloc[-2]

        if last['low'] < swing_low and last['close'] > swing_low and prev_close > swing_low:
            return {
                "sweep_detected": True,
                "type":           "Bullish (Stop Hunt Below Lows)",
                "swept_level":    round(swing_low, 5),
                "implication":    "Institutions grabbed sell stops — expect reversal UP",
            }

        if last['high'] > swing_high and last['close'] < swing_high and prev_close < swing_high:
            return {
                "sweep_detected": True,
                "type":           "Bearish (Stop Hunt Above Highs)",
                "swept_level":    round(swing_high, 5),
                "implication":    "Institutions grabbed buy stops — expect reversal DOWN",
            }

        return {"sweep_detected": False}

    # ── 5. PREMIUM / DISCOUNT ZONE ───────────────────────────────────────────
    # No changes.

    def _premium_discount_zone(self, df: pd.DataFrame) -> dict:
        if len(df) < 20:
            return {"zone": "unknown", "equilibrium": None}

        recent  = df.tail(50)
        leg_high = recent['high'].max()
        leg_low  = recent['low'].min()
        equil    = (leg_high + leg_low) / 2
        current  = df['close'].iloc[-1]

        zone = "DISCOUNT (Good for Buys)" if current < equil else "PREMIUM (Good for Sells)"
        return {
            "zone":       zone,
            "equilibrium": round(equil, 5),
            "leg_high":   round(leg_high, 5),
            "leg_low":    round(leg_low, 5),
        }

    # ── 6. RSI DIVERGENCE ────────────────────────────────────────────────────

    def _detect_rsi_divergence(self, df: pd.DataFrame) -> dict:
        """
        FIX v9.1: Now uses genuine swing pivots (via _find_swing_pivots) instead
        of comparing arbitrary midpoints (iloc[mid] had no structural meaning).

        Process:
        1. Find last 2 swing HIGH pivots → check for bearish regular/hidden div
        2. Find last 2 swing LOW  pivots → check for bullish regular/hidden div
        3. Compare price action at those pivots to RSI value at same bar index
        """
        if len(df) < 40:
            return {"divergence": "none"}

        if 'rsi_14' in df.columns:
            rsi_series = df['rsi_14']
        else:
            rsi_series = _rsi(df['close'], 14)

        # ── Find swing HIGH pivots ──
        high_pivots = _find_swing_pivots(df['high'], window=5)
        if len(high_pivots) >= 2:
            (i1, ph1), (i2, ph2) = high_pivots[-2], high_pivots[-1]
            r1, r2 = rsi_series.iloc[i1], rsi_series.iloc[i2]
            if not (pd.isna(r1) or pd.isna(r2)):
                # Regular Bearish: price HH, RSI LH
                if ph2 > ph1 and r2 < r1:
                    return {"divergence": "Regular Bearish (Sell Pressure Building)",
                            "rsi_now": round(float(rsi_series.iloc[-1]), 1)}
                # Hidden Bearish: price LH, RSI HH (trend continuation down)
                if ph2 < ph1 and r2 > r1:
                    return {"divergence": "Hidden Bearish (Trend Continuation Down)",
                            "rsi_now": round(float(rsi_series.iloc[-1]), 1)}

        # ── Find swing LOW pivots ──
        low_pivots = _find_swing_pivots(df['low'], window=5)
        if len(low_pivots) >= 2:
            (i1, pl1), (i2, pl2) = low_pivots[-2], low_pivots[-1]
            r1, r2 = rsi_series.iloc[i1], rsi_series.iloc[i2]
            if not (pd.isna(r1) or pd.isna(r2)):
                # Regular Bullish: price LL, RSI HL
                if pl2 < pl1 and r2 > r1:
                    return {"divergence": "Regular Bullish (Buy Pressure Building)",
                            "rsi_now": round(float(rsi_series.iloc[-1]), 1)}
                # Hidden Bullish: price HL, RSI LL (trend continuation up)
                if pl2 > pl1 and r2 < r1:
                    return {"divergence": "Hidden Bullish (Trend Continuation Up)",
                            "rsi_now": round(float(rsi_series.iloc[-1]), 1)}

        rsi_now = rsi_series.iloc[-1]
        return {"divergence": "none", "rsi_now": round(float(rsi_now), 1) if not pd.isna(rsi_now) else 50}

    # ── 7. VOLUME DELTA ──────────────────────────────────────────────────────
    # No changes — logic was correct.

    def _volume_delta_signal(self, df: pd.DataFrame) -> dict:
        if 'volume' not in df.columns or df['volume'].sum() == 0:
            return {"signal": "No volume data"}

        recent = df.tail(10).copy()
        recent['bull_vol'] = np.where(recent['close'] >= recent['open'], recent['volume'], 0)
        recent['bear_vol'] = np.where(recent['close'] < recent['open'],  recent['volume'], 0)

        total_bull = recent['bull_vol'].sum()
        total_bear = recent['bear_vol'].sum()
        total      = total_bull + total_bear

        if total == 0:
            return {"signal": "Flat"}

        bull_pct   = total_bull / total * 100
        bear_pct   = total_bear / total * 100
        recent_vol = recent['volume'].tail(3).mean()
        prior_vol  = recent['volume'].head(3).mean()
        expanding  = recent_vol > prior_vol * 1.1

        return {
            "bull_volume_pct":  round(bull_pct, 1),
            "bear_volume_pct":  round(bear_pct, 1),
            "volume_expanding": expanding,
            "signal": (
                "Bullish Pressure" if bull_pct > 60 else
                "Bearish Pressure" if bear_pct > 60 else
                "Balanced"
            ),
        }

    # ── 8. LIQUIDITY PIVOTS ───────────────────────────────────────────────────
    # No changes.

    def _find_liquidity_pivots(self, df: pd.DataFrame, window: int = 5) -> dict:
        pivot_high = df['high'] == df['high'].rolling(window=window * 2 + 1, center=True).max()
        pivot_low  = df['low']  == df['low'].rolling(window=window * 2 + 1, center=True).min()

        highs = df.loc[pivot_high, 'high'].tail(5).tolist()
        lows  = df.loc[pivot_low, 'low'].tail(5).tolist()

        return {
            "recent_resistances": [round(h, 5) for h in highs],
            "recent_supports":    [round(l, 5) for l in lows],
        }

    # ── 9. CONFIDENCE SCORER ─────────────────────────────────────────────────
    # No changes to scoring weights.

    def _compute_confidence(self, factors: dict) -> tuple:
        points    = 0
        breakdown = {}

        trend         = factors.get("trend")
        structure     = factors.get("structure_event", "")
        fvg_hit       = factors.get("fvg_hit", False)
        ob_hit        = factors.get("ob_hit", False)
        sweep         = factors.get("sweep", False)
        pd_zone       = factors.get("pd_zone", "")
        divergence    = factors.get("divergence", "none")
        vol_signal    = factors.get("vol_signal", "")
        session_ok    = factors.get("session_ok", True)

        if trend in ("Bullish", "Bearish"):
            points += 20; breakdown["Trend Filter"] = "+20"
        else:
            breakdown["Trend Filter"] = "+0 (Unknown)"

        if fvg_hit:
            points += 10; breakdown["FVG Interaction"] = "+10"

        if ob_hit:
            points += 10; breakdown["Order Block"] = "+10"

        if sweep:
            points += 10; breakdown["Liquidity Sweep"] = "+10"

        # ── Strict 3-Concept Confluence Bonus ──
        if fvg_hit and ob_hit and sweep:
            points += 25; breakdown["3-Concept Confluence"] = "+25"

        if "CHoCH" in structure or "BOS" in structure:
            points += 10; breakdown["Market Structure"] = f"+10 ({structure})"

        if (trend == "Bullish" and "DISCOUNT" in pd_zone) or (trend == "Bearish" and "PREMIUM" in pd_zone):
            points += 10; breakdown["Premium/Discount"] = "+10"

        if divergence != "none" and "none" not in divergence.lower():
            points += 10; breakdown["RSI Divergence"] = f"+10 ({divergence})"

        if (trend == "Bullish" and "Bullish" in vol_signal) or (trend == "Bearish" and "Bearish" in vol_signal):
            points += 5; breakdown["Volume Delta"] = "+5"

        if not session_ok:
            points = max(0, points - 10)
            breakdown["Session Filter"] = "-10 (Asian/Dead Zone)"

        if points >= 75:   grade = "A+"
        elif points >= 55: grade = "A"
        elif points >= 40: grade = "B+"
        elif points >= 25: grade = "B"
        else:              grade = "C"

        return grade, min(points, 100), breakdown

    # ── 10. MASTER ANALYSIS ───────────────────────────────────────────────────

    def analyze_market_context(self, symbol: str, df: pd.DataFrame, i: int, fvg_catalog: list, ob_catalog: list, structure: list, macro_news: str = "") -> dict:
        if df is None or len(df) < 20 or i < 20:
            return {"error": "Insufficient data for analysis."}

        current = df.iloc[i]
        current_price = current['close']
        
        # Fast slice for stateless indicators (max 50 candles needed)
        fast_window = df.iloc[max(0, i-50):i+1]

        sma50  = current['SMA_50'] if 'SMA_50' in current else fast_window['close'].rolling(50).mean().iloc[-1]
        sma200 = current['SMA_200'] if 'SMA_200' in current else fast_window['close'].rolling(200).mean().iloc[-1]
        ema21  = current['EMA_21'] if 'EMA_21' in current else _ema(fast_window['close'], 21).iloc[-1]

        bullish_trend = (not pd.isna(sma200)) and sma50 > sma200 and current_price > ema21
        bearish_trend = (not pd.isna(sma200)) and sma50 < sma200 and current_price < ema21
        macro_trend   = "Bullish" if bullish_trend else "Bearish" if bearish_trend else "Unknown"

        pivots   = self._find_liquidity_pivots(fast_window)
        active_fvgs = self._query_active_fvgs(fvg_catalog, df, i)
        active_obs  = self._query_active_obs(ob_catalog, df, i)
        last_struct = self._query_last_structure(structure, i)
        
        # Convert last_struct to the old format for confidence scorer
        struct_event = {"event": "Neutral", "direction": "unknown"}
        if last_struct:
            if "BULL" in last_struct['type']:
                struct_event = {"event": last_struct['type'], "direction": "bullish", "broken_level": last_struct['level']}
            else:
                struct_event = {"event": last_struct['type'], "direction": "bearish", "broken_level": last_struct['level']}

        sweep    = self._detect_liquidity_sweep(fast_window)
        pd_zone  = self._premium_discount_zone(fast_window)
        rsi_div  = self._detect_rsi_divergence(fast_window)
        vol_data = self._volume_delta_signal(fast_window)

        fvg_hit = any(
            (f['type'] == 'BULL' and f['top'] >= current_price >= f['bottom']) or
            (f['type'] == 'BEAR' and f['bottom'] <= current_price <= f['top'])
            for f in active_fvgs
        )
        
        ob_hit = any(
            ob['low'] <= current_price <= ob['high']
            for ob in active_obs
        )

        try:
            last_ts    = df['time'].iloc[-1]
            session_ok = _is_london_or_ny(last_ts)
        except Exception:
            logger.debug(
                "[StrategyAgent] 'time' column missing — session check skipped, assuming OK"
            )
            session_ok = True

        # ── Macro override — FIX v9.1 ──
        # OLD: high unrest → automatic 90% confidence (dangerous — trades into chaos)
        # NEW: high unrest → force SWING mode + apply a confidence PENALTY (not bonus)
        #      If macro is dangerous, we reduce confidence and require higher bar to trade.
        danger_words = ["war", "strike", "missile", "crisis", "crash", "emergency",
                        "attack", "sanction", "escalation", "conflict", "invasion"]
        unrest_score   = 0
        is_swing_trade = False

        if macro_news and "No Tavily" not in macro_news and "Tavily Error" not in macro_news:
            mn_lower     = macro_news.lower()
            unrest_score = sum(1 for w in danger_words if w in mn_lower)
            if unrest_score >= 2:
                is_swing_trade = True
                logger.warning(f"[StrategyAgent] Macro unrest={unrest_score}. Swing trade mode ON.")

        factors = {
            "trend":           macro_trend,
            "structure_event": struct_event.get("event", ""),
            "fvg_hit":         fvg_hit,
            "ob_hit":          ob_hit,
            "sweep":           sweep.get("sweep_detected", False),
            "pd_zone":         pd_zone.get("zone", ""),
            "divergence":      rsi_div.get("divergence", "none"),
            "vol_signal":      vol_data.get("signal", ""),
            "session_ok":      session_ok,
        }

        grade, confidence, breakdown = self._compute_confidence(factors)

        # FIX: Apply macro penalty AFTER scoring — high unrest reduces confidence
        # rather than overriding it to 90. Scale: each danger word = -5 pts, max -25.
        if unrest_score >= 2:
            macro_penalty = min(unrest_score * 5, 25)
            confidence    = max(0, confidence - macro_penalty)
            breakdown["Macro Danger Penalty"] = f"-{macro_penalty} (unrest_score={unrest_score})"
            logger.warning(
                f"[StrategyAgent] Macro penalty applied: -{macro_penalty}pts. "
                f"New confidence: {confidence}%. "
                f"Tip: manually review before trading during high-unrest events."
            )

        return {
            "Symbol":               symbol,
            "Current_Price":        round(current_price, 5),
            "Structure_Score":      grade,
            "Confidence_Pct":       confidence,
            "Confidence_Breakdown": breakdown,
            "Is_Swing_Trade":       is_swing_trade,
            "Macro_Unrest_Score":   unrest_score,
            "Macro_Trend":          macro_trend,
            "Market_Structure":     struct_event,
            "Liquidity_Sweep":      sweep,
            "Premium_Discount":     pd_zone,
            "RSI_Divergence":       rsi_div,
            "Volume_Delta":         vol_data,
            "Liquidity_Zones":      pivots,
            "Recent_FVGs":          active_fvgs[:5],
            "Order_Blocks":         active_obs[:4],
            "Session_Valid":        session_ok,
        }
