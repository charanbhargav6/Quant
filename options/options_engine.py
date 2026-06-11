"""
CRAVE v10.3 — Options Engine (Session 6)
==========================================
Strategy selection, entry logic, and lifecycle management for
NSE F&O options (NIFTY/BANKNIFTY weekly + monthly).

STRATEGY SELECTION LOGIC:
  Step 1 — Regime check (from regime_classifier)
    TRENDING   → directional strategies (long call/put, debit spread)
    RANGING    → premium selling (iron condor, short strangle)
    VOLATILE   → avoid selling, only defined-risk buys

  Step 2 — IV Rank check
    IV_rank > 50 → sell premium (iron condor, covered call, short strangle)
    IV_rank < 30 → buy premium (long call/put, debit spread)
    IV_rank 30-50 → neutral → lean on regime signal

  Step 3 — SMC signal alignment
    If directional SMC signal exists → align option strategy to it
    If no signal → non-directional premium strategy only

  Step 4 — DTE gate (always: 21-45 DTE)
    Never buy <7 DTE (theta crush)
    Never sell >60 DTE (too much capital locked)

  Step 5 — PCR confirmation (for index options)
    PCR > 1.2 + SELL signal → high conviction short
    PCR < 0.8 + BUY signal  → high conviction long

NSE-SPECIFIC RULES:
  NIFTY weekly expiry:     every Thursday
  BANKNIFTY weekly expiry: every Wednesday
  Monthly expiry:          last Thursday of month
  Never hold through expiry — close 2 days before
  Lot sizes fixed: NIFTY=50, BANKNIFTY=15

STRATEGIES SUPPORTED:
  long_call       → buy ATM call, defined risk, unlimited upside
  long_put        → buy ATM put, defined risk, unlimited downside
  bull_call_spread→ buy ATM call + sell OTM call, lower cost
  bear_put_spread → buy ATM put  + sell OTM put, lower cost
  iron_condor     → sell OTM call + put, buy further OTM, range-bound
  short_strangle  → sell OTM call + OTM put, high IV premium harvest
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("crave.options")


# ─────────────────────────────────────────────────────────────────────────────
# EXPIRY CALENDAR
# ─────────────────────────────────────────────────────────────────────────────

class ExpiryCalendar:
    """NSE F&O expiry date calculator."""

    def get_next_expiry(self, symbol: str,
                         expiry_type: str = "weekly") -> Optional[datetime]:
        """
        Get next expiry date for NIFTY or BANKNIFTY.
        expiry_type: 'weekly' or 'monthly'
        """
        from config.config import INDIA
        now = datetime.now(timezone.utc)

        if symbol.upper() in ("NIFTY", "NIFTY_FUT", "NIFTY50"):
            weekly_day  = 3   # Thursday = 3
            monthly_day = 3
        elif symbol.upper() in ("BANKNIFTY", "BANKNIFTY_FUT"):
            weekly_day  = 2   # Wednesday = 2
            monthly_day = 2
        else:
            weekly_day  = 3
            monthly_day = 3

        if expiry_type == "weekly":
            return self._next_weekday(now, weekly_day)
        else:
            return self._last_weekday_of_month(now, monthly_day)

    def get_dte(self, expiry: datetime) -> int:
        """Days to expiry from now."""
        now = datetime.now(timezone.utc)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return max(0, (expiry - now).days)

    def is_near_expiry(self, expiry: datetime,
                        danger_days: int = 2) -> bool:
        """True if expiry is within danger_days — close position."""
        return self.get_dte(expiry) <= danger_days

    def _next_weekday(self, from_dt: datetime, weekday: int) -> datetime:
        """Next occurrence of a weekday (0=Mon … 6=Sun)."""
        days_ahead = weekday - from_dt.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return from_dt + timedelta(days=days_ahead)

    def _last_weekday_of_month(self, from_dt: datetime,
                                weekday: int) -> datetime:
        """Last occurrence of weekday in current or next month."""
        import calendar
        # Find last day of current month
        last_day = calendar.monthrange(from_dt.year, from_dt.month)[1]
        last_date = from_dt.replace(day=last_day)
        days_back = (last_date.weekday() - weekday) % 7
        last_occurrence = last_date - timedelta(days=days_back)
        if last_occurrence.date() < from_dt.date():
            # Advance to next month
            if from_dt.month == 12:
                next_month = from_dt.replace(year=from_dt.year+1, month=1, day=1)
            else:
                next_month = from_dt.replace(month=from_dt.month+1, day=1)
            return self._last_weekday_of_month(next_month, weekday)
        return last_occurrence


expiry_calendar = ExpiryCalendar()


# ─────────────────────────────────────────────────────────────────────────────
# IV CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

class IVCalculator:
    """
    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) × 100
    0-100 scale.
    > 50 → sell premium (IV rich, expect mean reversion)
    < 30 → buy premium (IV cheap, good time to own options)
    """

    def __init__(self):
        self._iv_history: dict = {}   # symbol → list of (date, iv)

    def get_iv_rank(self, symbol: str,
                     current_iv: Optional[float] = None) -> dict:
        """
        Calculate IV Rank for a symbol.
        If current_iv not provided, estimates from option chain data.
        Returns dict with iv_rank, signal, and recommendation.
        """
        if current_iv is None:
            current_iv = self._estimate_iv(symbol)

        if current_iv is None:
            return {"available": False, "reason": "Cannot estimate IV"}

        history = self._get_iv_history(symbol)
        if not history or len(history) < 20:
            return {
                "available":    False,
                "current_iv":   current_iv,
                "reason":       "Insufficient IV history (need 20+ days)",
            }

        high_52w = max(history)
        low_52w  = min(history)
        rng      = high_52w - low_52w

        if rng <= 0:
            return {"available": False, "reason": "IV range is zero"}

        iv_rank = round((current_iv - low_52w) / rng * 100, 1)

        from config.config import OPTIONS
        sell_threshold = OPTIONS.get("min_iv_rank_to_sell", 50)
        buy_threshold  = OPTIONS.get("max_iv_rank_to_buy",  30)

        if iv_rank >= sell_threshold:
            signal     = "SELL_PREMIUM"
            strategies = ["iron_condor", "short_strangle", "covered_call"]
            reason     = f"IV rich ({iv_rank:.0f}%) — sell theta"
        elif iv_rank <= buy_threshold:
            signal     = "BUY_PREMIUM"
            strategies = ["long_call", "long_put", "debit_spread"]
            reason     = f"IV cheap ({iv_rank:.0f}%) — buy cheap options"
        else:
            signal     = "NEUTRAL"
            strategies = ["debit_spread"]
            reason     = f"IV neutral ({iv_rank:.0f}%) — defined risk only"

        return {
            "available":    True,
            "iv_rank":      iv_rank,
            "current_iv":   round(current_iv, 2),
            "high_52w":     round(high_52w, 2),
            "low_52w":      round(low_52w, 2),
            "signal":       signal,
            "strategies":   strategies,
            "reason":       reason,
        }

    def _estimate_iv(self, symbol: str) -> Optional[float]:
        """
        Estimate current IV from NSE option chain.
        Uses ATM straddle price as proxy when full IV data unavailable.
        """
        try:
            from brokers.zerodha_agent import get_zerodha
            zr    = get_zerodha()
            chain = self._get_option_chain(symbol)
            if not chain:
                return None

            # ATM strike = closest strike to current price
            spot = chain.get("underlyingValue", 0)
            if not spot:
                return None

            strikes = [d["strikePrice"] for d in chain.get("data", [])
                       if d.get("strikePrice")]
            if not strikes:
                return None

            atm_strike = min(strikes, key=lambda s: abs(s - spot))

            # Find ATM call and put IV
            for row in chain.get("data", []):
                if row.get("strikePrice") == atm_strike:
                    ce_iv = row.get("CE", {}).get("impliedVolatility", 0)
                    pe_iv = row.get("PE", {}).get("impliedVolatility", 0)
                    if ce_iv and pe_iv:
                        return (ce_iv + pe_iv) / 2
        except Exception as e:
            logger.debug(f"[IVCalc] IV estimation failed {symbol}: {e}")
        return None

    def _get_option_chain(self, symbol: str) -> Optional[dict]:
        """Fetch option chain from NSE."""
        try:
            import requests
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            headers = {"User-Agent": "Mozilla/5.0",
                       "Accept": "application/json",
                       "Referer": "https://www.nseindia.com"}
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                return r.json().get("records", {})
        except Exception:
            pass
        return None

    def _get_iv_history(self, symbol: str) -> list:
        """Return list of historical IV values for 52-week rank calc."""
        history = self._iv_history.get(symbol, [])
        # Stub: in production, populate from daily IV snapshots in DB
        # For now return empty — IV rank will be unavailable until data builds
        return history

    def record_daily_iv(self, symbol: str, iv: float):
        """Call once per day to build IV history for rank calculation."""
        if symbol not in self._iv_history:
            self._iv_history[symbol] = []
        self._iv_history[symbol].append(iv)
        # Keep 252 trading days (1 year)
        self._iv_history[symbol] = self._iv_history[symbol][-252:]


iv_calculator = IVCalculator()


# ─────────────────────────────────────────────────────────────────────────────
# OPTION STRIKE SELECTOR
# ─────────────────────────────────────────────────────────────────────────────

class StrikeSelector:
    """
    Selects appropriate strikes for each strategy type.
    Uses ATM ± 1 strike for directional, ATM ± 2 for premium selling.
    """

    def get_strikes(self, strategy: str, spot: float,
                     symbol: str) -> Optional[dict]:
        """
        Returns strike configuration for a given strategy.
        All strikes rounded to nearest valid NSE strike interval.
        """
        interval = self._get_strike_interval(symbol, spot)

        atm      = self._round_to_interval(spot, interval)
        otm_1    = interval          # 1 strike OTM
        otm_2    = interval * 2      # 2 strikes OTM

        if strategy == "long_call":
            return {"buy_call": atm}

        elif strategy == "long_put":
            return {"buy_put": atm}

        elif strategy == "bull_call_spread":
            return {
                "buy_call":  atm,
                "sell_call": atm + otm_1,
                "max_loss":  None,    # filled by premium calculator
                "max_gain":  None,
            }

        elif strategy == "bear_put_spread":
            return {
                "buy_put":  atm,
                "sell_put": atm - otm_1,
            }

        elif strategy == "iron_condor":
            return {
                "sell_call":   atm + otm_1,
                "buy_call":    atm + otm_2,
                "sell_put":    atm - otm_1,
                "buy_put":     atm - otm_2,
                "max_profit":  None,   # net premium received
                "max_loss":    None,   # width - premium
            }

        elif strategy == "short_strangle":
            return {
                "sell_call": atm + otm_1,
                "sell_put":  atm - otm_1,
            }

        return None

    def _get_strike_interval(self, symbol: str, spot: float) -> float:
        """NSE standard strike intervals."""
        s = symbol.upper()
        if "NIFTY" in s and "BANK" not in s:
            return 50.0
        if "BANKNIFTY" in s:
            return 100.0
        # Single stocks: interval depends on price range
        if spot < 250:    return 2.5
        if spot < 500:    return 5.0
        if spot < 1000:   return 10.0
        if spot < 2500:   return 20.0
        return 50.0

    def _round_to_interval(self, price: float, interval: float) -> float:
        return round(round(price / interval) * interval, 2)


strike_selector = StrikeSelector()


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONS ENGINE (main class)
# ─────────────────────────────────────────────────────────────────────────────

class OptionsEngine:
    """
    Master options engine.
    Called by trading_loop when MARKETS["options"]["enabled"] = True.
    """

    def __init__(self):
        self._open_positions: list = []   # list of open option position dicts

    # ─────────────────────────────────────────────────────────────────────────
    # STRATEGY SELECTION
    # ─────────────────────────────────────────────────────────────────────────

    def select_strategy(self, symbol: str,
                         df: pd.DataFrame,
                         smc_direction: Optional[str] = None) -> Optional[dict]:
        """
        Full strategy selection pipeline.
        Returns strategy dict or None if no entry warranted.

        strategy dict keys:
          name, symbol, underlying, direction, strikes,
          expiry, dte, lot_size, max_risk_pct, iv_rank, reason
        """
        from config.config import OPTIONS

        # ── Step 1: DTE check — find valid expiry ─────────────────────────
        expiry = self._find_valid_expiry(symbol)
        if expiry is None:
            logger.debug(f"[Options] {symbol}: no valid expiry in 21-45 DTE range")
            return None

        dte = expiry_calendar.get_dte(expiry)

        # ── Step 2: Regime ────────────────────────────────────────────────
        regime = "UNKNOWN"
        try:
            from ml.regime_classifier import regime_model
            regime = regime_model.predict(symbol, df)
        except Exception:
            pass

        # In VOLATILE regime: skip premium selling entirely
        if regime == "VOLATILE" and smc_direction is None:
            logger.debug(f"[Options] {symbol}: volatile + no direction — skip")
            return None

        # ── Step 3: IV Rank ───────────────────────────────────────────────
        underlying = self._get_underlying(symbol)
        spot       = df['close'].iloc[-1] if not df.empty else 0
        iv_data    = iv_calculator.get_iv_rank(underlying)

        # ── Step 4: PCR confirmation ──────────────────────────────────────
        pcr_data = {}
        try:
            from brokers.zerodha_agent import get_zerodha
            pcr_data = get_zerodha().get_pcr(underlying)
        except Exception:
            pass

        # ── Step 5: Strategy decision ─────────────────────────────────────
        strategy_name = self._decide_strategy(
            regime, iv_data, smc_direction, pcr_data
        )
        if not strategy_name:
            return None

        # ── Step 6: Strike selection ──────────────────────────────────────
        strikes = strike_selector.get_strikes(strategy_name, spot, underlying)
        if not strikes:
            return None

        # ── Step 7: Risk sizing ───────────────────────────────────────────
        from config.config import OPTIONS as OPT_CFG
        max_risk_pct = OPT_CFG.get("max_single_option_risk", 1.0)

        lot_size = self._get_lot_size(underlying)

        result = {
            "name":          strategy_name,
            "symbol":        symbol,
            "underlying":    underlying,
            "spot":          spot,
            "direction":     smc_direction or "neutral",
            "strikes":       strikes,
            "expiry":        expiry.isoformat(),
            "dte":           dte,
            "lot_size":      lot_size,
            "max_risk_pct":  max_risk_pct,
            "iv_rank":       iv_data.get("iv_rank"),
            "iv_signal":     iv_data.get("signal", "NEUTRAL"),
            "regime":        regime,
            "pcr":           pcr_data.get("pcr"),
            "reason": (
                f"{strategy_name} | "
                f"IV:{iv_data.get('iv_rank', '?')} | "
                f"Regime:{regime} | "
                f"DTE:{dte} | "
                f"{iv_data.get('reason', '')}"
            ),
        }

        logger.info(
            f"[Options] Strategy selected: {strategy_name} on {symbol} | "
            f"{result['reason']}"
        )
        return result

    def _decide_strategy(self, regime: str, iv_data: dict,
                          smc_direction: Optional[str],
                          pcr_data: dict) -> Optional[str]:
        """
        Core decision matrix.
        Returns strategy name or None.
        """
        iv_signal = iv_data.get("signal", "NEUTRAL") if iv_data.get("available") else "NEUTRAL"
        pcr       = pcr_data.get("pcr", 1.0) if pcr_data.get("available") else 1.0

        # VOLATILE + directional SMC → debit spread (defined risk buy)
        if regime == "VOLATILE" and smc_direction:
            if smc_direction in ("buy", "long"):
                return "bull_call_spread"
            return "bear_put_spread"

        # RANGING + high IV → iron condor (best range-bound premium seller)
        if regime == "RANGING" and iv_signal == "SELL_PREMIUM":
            return "iron_condor"

        # RANGING + low IV → skip (no edge)
        if regime == "RANGING" and iv_signal == "BUY_PREMIUM":
            return None

        # TRENDING + SMC signal + low IV → naked directional
        if regime in ("TRENDING_UP", "TRENDING_DOWN") and iv_signal == "BUY_PREMIUM":
            if smc_direction in ("buy", "long") and regime == "TRENDING_UP":
                return "long_call"
            if smc_direction in ("sell", "short") and regime == "TRENDING_DOWN":
                return "long_put"
            # SMC vs regime conflict → defined risk
            if smc_direction in ("buy", "long"):
                return "bull_call_spread"
            return "bear_put_spread"

        # TRENDING + high IV → sell premium with trend
        if regime in ("TRENDING_UP", "TRENDING_DOWN") and iv_signal == "SELL_PREMIUM":
            # Use short strangle (collect premium both sides, let trend do work)
            return "short_strangle"

        # High IV + no direction → iron condor
        if iv_signal == "SELL_PREMIUM" and not smc_direction:
            return "iron_condor"

        # No clear edge
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # EXPIRY MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _find_valid_expiry(self, symbol: str) -> Optional[datetime]:
        """
        Find next expiry within 21-45 DTE window.
        Tries weekly first, then monthly.
        """
        from config.config import OPTIONS
        min_dte = OPTIONS.get("min_dte", 21)
        max_dte = OPTIONS.get("max_dte", 45)

        underlying = self._get_underlying(symbol)

        for expiry_type in ("weekly", "monthly"):
            expiry = expiry_calendar.get_next_expiry(underlying, expiry_type)
            if expiry is None:
                continue
            dte = expiry_calendar.get_dte(expiry)
            if min_dte <= dte <= max_dte:
                return expiry

        return None

    def check_expiry_danger(self) -> list:
        """
        Check all open option positions for near-expiry risk.
        Returns list of positions needing attention.
        """
        danger = []
        for pos in self._open_positions:
            expiry_str = pos.get("expiry")
            if not expiry_str:
                continue
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry_calendar.is_near_expiry(expiry, danger_days=2):
                    danger.append(pos)
                    logger.warning(
                        f"[Options] EXPIRY DANGER: {pos['symbol']} "
                        f"expires {expiry_str[:10]} "
                        f"(DTE={expiry_calendar.get_dte(expiry)})"
                    )
            except Exception:
                continue
        return danger

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_underlying(self, symbol: str) -> str:
        """Map any F&O symbol to its underlying index/stock."""
        s = symbol.upper()
        if "NIFTY" in s and "BANK" not in s:
            return "NIFTY"
        if "BANKNIFTY" in s:
            return "BANKNIFTY"
        return s.replace("_FUT", "").replace("_CE", "").replace("_PE", "")

    def _get_lot_size(self, underlying: str) -> int:
        from config.config import get_lot_size
        lot = get_lot_size(underlying)
        return lot if lot > 1 else 50   # NIFTY default

    def get_status_message(self) -> str:
        open_n = len(self._open_positions)
        return (
            f"⚙️ <b>OPTIONS ENGINE</b>\n"
            f"Open positions: {open_n}\n"
            f"IV data: {'✅' if iv_calculator._iv_history else '⏳ building'}"
        )


# ── Singleton ─────────────────────────────────────────────────────────────────
_options_engine: Optional[OptionsEngine] = None

def get_options_engine() -> OptionsEngine:
    global _options_engine
    if _options_engine is None:
        _options_engine = OptionsEngine()
    return _options_engine
