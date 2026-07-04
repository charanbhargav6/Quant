"""
CRAVE v12 — Cross-Asset Correlation Engine
=============================================
Prevents hidden overexposure by checking if new trades are correlated
with existing open positions.

PROBLEM:
  EURUSD LONG + GBPUSD LONG = nearly the same trade (92% correlated)
  = 2x the risk you think you're taking.

  XAUUSD LONG + EURUSD LONG when DXY is falling = same dollar bet.

SOLUTION:
  Before every new trade, compute the rolling 50-period correlation
  between the new symbol and every open position. If any correlation
  exceeds the threshold, either:
  a) Block the trade entirely (same direction)
  b) Reduce position size by 50% (moderate correlation)
  c) Allow it as a hedge (opposite direction, high correlation)
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger("crave.correlation")

# ─────────────────────────────────────────────────────────────────────────────
# KNOWN CORRELATION CLUSTERS (pre-computed, used as fast lookup)
# These are long-term average correlations — used when data isn't available
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_PAIRS = {
    # Format: (symbol_a, symbol_b) → average correlation
    ("EURUSD=X", "GBPUSD=X"):  +0.92,
    ("EURUSD=X", "AUDUSD=X"):  +0.75,
    ("EURUSD=X", "NZDUSD=X"):  +0.82,
    ("EURUSD=X", "USDCHF=X"):  -0.95,
    ("EURUSD=X", "USDJPY=X"):  -0.40,
    ("GBPUSD=X", "AUDUSD=X"):  +0.68,
    ("GBPUSD=X", "USDCHF=X"):  -0.88,
    ("AUDUSD=X", "NZDUSD=X"):  +0.90,
    ("USDJPY=X", "USDCHF=X"):  +0.55,
    ("XAUUSD=X", "EURUSD=X"):  +0.60,   # Both anti-USD
    ("XAUUSD=X", "USDCHF=X"):  -0.55,
    ("XAUUSD=X", "USDJPY=X"):  -0.30,
    ("GC=F", "EURUSD=X"):      +0.60,
    ("BTC-USD", "ETH-USD"):    +0.88,
    ("BTC-USD", "SOL-USD"):    +0.80,
    ("ETH-USD", "SOL-USD"):    +0.82,
    ("BTCUSDT", "ETHUSDT"):    +0.88,
}

# ─────────────────────────────────────────────────────────────────────────────
# THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

BLOCK_THRESHOLD    = 0.85   # Correlation > 85% + same direction → block trade
REDUCE_THRESHOLD   = 0.60   # Correlation > 60% → halve position size
HEDGE_MIN_CORR     = 0.70   # Correlation > 70% + opposite direction → allow as hedge


class CorrelationEngine:
    """
    Cross-asset correlation guard.
    Checks if a proposed trade is too correlated with existing positions.
    """

    def __init__(self):
        self._corr_cache: Dict[Tuple[str, str], float] = {}
        self._last_calc: Dict[Tuple[str, str], datetime] = {}

    def check_trade(self, new_symbol: str, new_direction: str,
                     open_positions: List[Dict]) -> Dict:
        """
        Check if a new trade should be allowed, reduced, or blocked.

        Args:
            new_symbol:    The symbol we want to trade (e.g. "EURUSD=X")
            new_direction: "buy" or "sell"
            open_positions: List of current open position dicts

        Returns:
            {
                "action":   "allow" | "reduce" | "block" | "hedge",
                "reason":   str,
                "size_mult": float (1.0 = full, 0.5 = half, 0.0 = block),
                "conflicts": [{symbol, direction, correlation}...]
            }
        """
        if not open_positions:
            return {
                "action": "allow", "reason": "No open positions",
                "size_mult": 1.0, "conflicts": []
            }

        conflicts = []
        worst_action = "allow"
        worst_mult = 1.0

        for pos in open_positions:
            pos_symbol    = pos.get("symbol", "")
            pos_direction = pos.get("direction", "")

            if pos_symbol == new_symbol:
                # Same symbol — check if same direction (doubling down)
                if pos_direction == new_direction:
                    conflicts.append({
                        "symbol": pos_symbol,
                        "direction": pos_direction,
                        "correlation": 1.0,
                    })
                    worst_action = "block"
                    worst_mult = 0.0
                    continue
                else:
                    # Opposite direction on same symbol = hedging
                    continue

            corr = self._get_correlation(new_symbol, pos_symbol)

            # Determine effective correlation considering direction
            same_dir = (new_direction == pos_direction)
            effective_corr = corr if same_dir else -corr

            if abs(corr) < REDUCE_THRESHOLD:
                continue  # Low correlation — no concern

            conflict = {
                "symbol":      pos_symbol,
                "direction":   pos_direction,
                "correlation": round(corr, 3),
            }

            if effective_corr > BLOCK_THRESHOLD:
                # High positive effective correlation = same bet = BLOCK
                conflict["action"] = "block"
                conflicts.append(conflict)
                worst_action = "block"
                worst_mult = 0.0
            elif effective_corr > REDUCE_THRESHOLD:
                # Moderate correlation = reduce size
                conflict["action"] = "reduce"
                conflicts.append(conflict)
                if worst_action != "block":
                    worst_action = "reduce"
                    worst_mult = 0.5
            elif effective_corr < -HEDGE_MIN_CORR:
                # Negative effective correlation = hedge = ALLOW
                conflict["action"] = "hedge"
                conflicts.append(conflict)

        reason = "OK"
        if worst_action == "block":
            syms = ", ".join(c["symbol"] for c in conflicts if c.get("action") == "block")
            reason = f"Blocked: too correlated with {syms}"
        elif worst_action == "reduce":
            syms = ", ".join(c["symbol"] for c in conflicts if c.get("action") == "reduce")
            reason = f"Reduced size: correlated with {syms}"

        return {
            "action":    worst_action,
            "reason":    reason,
            "size_mult": worst_mult,
            "conflicts": conflicts,
        }

    def get_correlation_matrix(self, symbols: List[str]) -> pd.DataFrame:
        """
        Compute full correlation matrix for a set of symbols.
        Uses 50-period 1H returns.
        """
        returns = {}
        for sym in symbols:
            try:
                import yfinance as yf
                df = yf.Ticker(sym).history(period="30d", interval="1h")
                if df is not None and len(df) > 20:
                    r = df["Close"].pct_change().dropna().values[-50:]
                    returns[sym] = r[:min(len(r), 50)]
            except Exception:
                continue

        if len(returns) < 2:
            return pd.DataFrame()

        # Align lengths
        min_len = min(len(v) for v in returns.values())
        aligned = {k: v[-min_len:] for k, v in returns.items()}

        df = pd.DataFrame(aligned)
        return df.corr()

    # ─────────────────────────────────────────────────────────────────────────
    # CORRELATION CALCULATION
    # ─────────────────────────────────────────────────────────────────────────

    def _get_correlation(self, sym_a: str, sym_b: str) -> float:
        """Get correlation between two symbols (cached)."""

        key = tuple(sorted([sym_a, sym_b]))

        # Check cache (valid for 4 hours)
        if key in self._corr_cache:
            last = self._last_calc.get(key)
            if last and (datetime.now(timezone.utc) - last).total_seconds() < 14400:
                return self._corr_cache[key]

        # Try live computation
        corr = self._compute_correlation(sym_a, sym_b)
        if corr is not None:
            self._corr_cache[key] = corr
            self._last_calc[key] = datetime.now(timezone.utc)
            return corr

        # Fall back to known pairs table
        if (sym_a, sym_b) in KNOWN_PAIRS:
            return KNOWN_PAIRS[(sym_a, sym_b)]
        if (sym_b, sym_a) in KNOWN_PAIRS:
            return KNOWN_PAIRS[(sym_b, sym_a)]

        return 0.0  # Unknown pair — assume uncorrelated

    def _compute_correlation(self, sym_a: str, sym_b: str) -> Optional[float]:
        """
        Compute rolling 50-period return correlation from 1H data.
        """
        try:
            import yfinance as yf

            df_a = yf.Ticker(sym_a).history(period="10d", interval="1h")
            df_b = yf.Ticker(sym_b).history(period="10d", interval="1h")

            if df_a is None or df_b is None:
                return None
            if len(df_a) < 30 or len(df_b) < 30:
                return None

            ret_a = df_a["Close"].pct_change().dropna().values[-50:]
            ret_b = df_b["Close"].pct_change().dropna().values[-50:]

            min_len = min(len(ret_a), len(ret_b))
            if min_len < 20:
                return None

            ret_a = ret_a[-min_len:]
            ret_b = ret_b[-min_len:]

            corr = float(np.corrcoef(ret_a, ret_b)[0, 1])
            return corr if not np.isnan(corr) else 0.0

        except Exception as e:
            logger.debug(f"[Correlation] Compute failed for {sym_a}/{sym_b}: {e}")
            return None


# ── Singleton ──────────────────────────────────────────────────────────────
_engine: Optional[CorrelationEngine] = None

def get_correlation_engine() -> CorrelationEngine:
    global _engine
    if _engine is None:
        _engine = CorrelationEngine()
    return _engine
