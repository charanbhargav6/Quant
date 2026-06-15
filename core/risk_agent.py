"""
CRAVE Phase 9.1 - Capital Protection Engine
============================================
FIXES vs v9.0:
  🔧 Daily loss limit now resets at NY session close (21:00 UTC), not calendar midnight
     → Critical for traders in UTC+5:30 (Mumbai) trading US markets
  🔧 validate_trade_signal now stores ATR in the returned packet
     → Required so ExecutionAgent can use ATR-based trailing SL (not % trail)
  No other changes — v9.0 risk logic was otherwise correct.
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime, date, timezone, timedelta

logger = logging.getLogger("crave.trading.risk")


class RiskAgent:
    def __init__(self, telegram_agent=None):
        self.telegram               = telegram_agent
        self.max_risk_per_trade     = 0.02    # fallback — overridden by compound engine
        self.max_account_drawdown   = 0.05    # 5% trailing DD kill switch
        self.daily_loss_limit       = 0.02    # 2% max loss per session day
        self.min_rr_ratio           = 1.5     # Minimum acceptable R:R
        self.max_consecutive_losses = 3       # Kill switch after 3 L's in a row

        # ── v10.5: Compounding engine ─────────────────────────────────────
        # Risk % scales automatically with account size:
        #   <$500   → 0.5%  (micro — small $ risk, protects tiny accounts)
        #   <$2000  → 0.75% (small)
        #   <$5000  → 1.0%  (standard)
        #   <$20000 → 1.5%  (growth)
        #   $20000+ → 2.0%  (professional)
        try:
            from engines.compounding_engine import get_compound_engine
            self._compound = get_compound_engine()
            logger.info("[RiskAgent] Compounding engine loaded.")
        except Exception as e:
            self._compound = None
            logger.debug(f"[RiskAgent] Compound engine unavailable (non-fatal): {e}")

        # ── State tracking ──
        self.equity_peak          = None
        self.daily_start_equity   = None
        self._session_day_start   = None   # FIX: tracks NY session day, not calendar day
        self.trade_log            = []
        self.consecutive_losses   = 0
        self.open_positions       = []

    # ─────────────────────────────────────────────────────────────────────────
    # FIX: SESSION-AWARE DAILY RESET
    # ─────────────────────────────────────────────────────────────────────────

    def _get_ny_session_day(self) -> datetime:
        """
        FIX v9.1: Returns the current NY trading session "day" as a date.
        A new session day starts at 21:00 UTC (NY close), not at midnight local time.

        WHY THIS MATTERS:
        A Mumbai-based trader (UTC+5:30) has midnight at 18:30 UTC — right in
        the middle of the US session. Without this fix, a 1.5% loss before midnight
        + another 1.5% loss after midnight never triggers the 2% daily limit
        because the counter resets at midnight. That's a 3% loss in one effective
        trading session, which violates the intended protection.

        Using NY close (21:00 UTC) as the session boundary means the daily reset
        happens when markets are genuinely closed, regardless of local timezone.
        """
        now_utc = datetime.now(timezone.utc)
        # If current UTC hour is >= 21, we're in a new session day (use tomorrow's date)
        # If current UTC hour is < 21, we're still in today's session
        if now_utc.hour >= 21:
            # Session starts at 21:00 UTC — next calendar day in UTC terms
            session_day = now_utc.date() + timedelta(days=1)
        else:
            session_day = now_utc.date()
        return session_day

    # ─────────────────────────────────────────────────────────────────────────
    # DRAWDOWN & DAILY LOSS CHECKS
    # ─────────────────────────────────────────────────────────────────────────

    def check_drawdown_limit(self, current_equity: float) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        Checks BOTH trailing drawdown AND daily loss limit.
        """
        # FIX: Use NY session day instead of calendar day
        session_day = self._get_ny_session_day()
        if self._session_day_start != session_day:
            self.daily_start_equity   = current_equity
            self._session_day_start   = session_day
            logger.info(f"[RiskAgent] New session day {session_day}. "
                        f"Daily baseline set to ${current_equity:,.2f}")

        # ── Trailing drawdown ──
        if self.equity_peak is None or current_equity > self.equity_peak:
            self.equity_peak = current_equity

        dd_pct = (self.equity_peak - current_equity) / self.equity_peak
        if dd_pct >= self.max_account_drawdown:
            msg = (f"🚨 TRAILING DRAWDOWN BREACHED: {dd_pct*100:.2f}% "
                   f"from peak ${self.equity_peak:,.2f}")
            logger.critical(msg)
            self._notify(msg)
            return False, f"Trailing drawdown {dd_pct*100:.1f}% exceeds 5% limit."

        # ── Daily loss limit ──
        if self.daily_start_equity and self.daily_start_equity > 0:
            daily_loss_pct = (self.daily_start_equity - current_equity) / self.daily_start_equity
            if daily_loss_pct >= self.daily_loss_limit:
                msg = (f"🚨 DAILY LOSS LIMIT HIT: -{daily_loss_pct*100:.2f}% "
                       f"this session. Trading paused until next session (21:00 UTC).")
                logger.critical(msg)
                self._notify(msg)
                return False, f"Daily loss limit {daily_loss_pct*100:.1f}% hit."

        # ── Consecutive loss kill switch ──
        recent = [t['result'] for t in self.trade_log[-self.max_consecutive_losses:]]
        if (len(recent) == self.max_consecutive_losses
                and all(r == 'L' for r in recent)):
            msg = (f"⚠️ {self.max_consecutive_losses} consecutive losses. "
                   f"Cooling off — no new trades this session.")
            logger.warning(msg)
            self._notify(msg)
            return False, f"{self.max_consecutive_losses} consecutive losses. Cooldown active."

        return True, "OK"

    # ─────────────────────────────────────────────────────────────────────────
    # ATR (Wilder's Smoothed) — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return (df['high'] - df['low']).mean() if len(df) > 0 else 0.001

        high_low   = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close  = (df['low']  - df['close'].shift()).abs()
        tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr        = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        val        = atr.iloc[-1]
        if not pd.isna(val):
            return val
        # Instrument-relative fallback: 0.5% of the last close price.
        # A flat 0.001 causes SL ≈ entry for high-priced assets (e.g. Gold at
        # $2000), which makes size_position() return 0 and silently skip the trade.
        last_close = df['close'].iloc[-1] if len(df) > 0 else 0
        return last_close * 0.005 if last_close > 0 else 0.001

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION SIZING — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def size_position(self, current_equity: float, entry_price: float,
                      stop_loss_price: float, use_kelly: bool = False,
                      win_rate: float = 0.55) -> float:
        price_risk = abs(entry_price - stop_loss_price)
        if price_risk == 0:
            logger.warning(
                f"[RiskAgent] size_position: price_risk=0 "
                f"(entry={entry_price}, sl={stop_loss_price}) — sizing skipped"
            )
            return 0.0

        # ── v10.5: Use compound engine tier-based risk ────────────────────
        if self._compound:
            risk_fraction = self._compound.get_risk_pct(current_equity)
        elif use_kelly and len(self.trade_log) >= 20:
            wins  = [t for t in self.trade_log if t['result'] == 'W']
            loss_ = [t for t in self.trade_log if t['result'] == 'L']
            if wins and loss_:
                avg_win  = np.mean([t['r_multiple'] for t in wins])
                avg_loss = abs(np.mean([t['r_multiple'] for t in loss_]))
                w        = len(wins) / len(self.trade_log)
                rr       = avg_win / avg_loss if avg_loss > 0 else 1.5
                kelly_f  = w - (1 - w) / rr
                risk_fraction = min(max(kelly_f * 0.5, 0.005), self.max_risk_per_trade)
                logger.info(f"[RiskAgent] Kelly fraction={kelly_f:.3f}, using half={risk_fraction:.3f}")
            else:
                risk_fraction = self.max_risk_per_trade
        else:
            risk_fraction = self.max_risk_per_trade

        risk_amount   = current_equity * risk_fraction
        position_size = risk_amount / price_risk
        return round(position_size, 4)

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN VALIDATION GATE
    # ─────────────────────────────────────────────────────────────────────────

    def validate_trade_signal(self, current_equity: float, signal: dict,
                               df: pd.DataFrame, confidence_pct: int = 50) -> dict:
        """
        Master trade validation.

        v10.6: Now uses ASSET_PARAMS for sl_mult and rr to match backtest exactly.
        Before: hardcoded sl_mult=2.0, rr=2.0 for everything.
        After:  Gold=2.0/2.0, Forex=2.5/3.0, BTC=1.5/2.0, Stocks=1.8/2.5
        This is critical for paper/live results to match backtest performance.
        """
        allowed, reason = self.check_drawdown_limit(current_equity)
        if not allowed:
            return {"approved": False, "reason": reason}

        if confidence_pct < 40:
            return {
                "approved": False,
                "reason":   f"Setup confidence {confidence_pct}% below 40% threshold.",
            }

        direction   = signal.get("action", "").lower()
        entry_price = float(signal.get("price", df['close'].iloc[-1]))
        is_swing    = signal.get("is_swing_trade", False)
        symbol      = signal.get("symbol", "UNKNOWN")

        atr = self.calculate_atr(df)

        # v10.6: Use asset-specific SL/TP from ASSET_PARAMS (matches backtest)
        try:
            from config.config import get_asset_params
            asset_p = get_asset_params(symbol)
            sl_mult = asset_p["sl_mult"]
            rr      = asset_p["rr"]
        except Exception:
            sl_mult = 2.0
            rr      = 2.0

        # Override for swing trades only
        if is_swing:
            sl_mult = 3.5
            rr      = 3.0

        if direction in ("buy", "long"):
            sl_price  = entry_price - (atr * sl_mult)
            tp2_price = entry_price + (atr * sl_mult * rr)
            tp1_price = entry_price + (atr * sl_mult * 1.0)

        elif direction in ("sell", "short"):
            sl_price  = entry_price + (atr * sl_mult)
            tp2_price = entry_price - (atr * sl_mult * rr)
            tp1_price = entry_price - (atr * sl_mult * 1.0)

        else:
            return {"approved": False, "reason": f"Invalid direction: '{direction}'"}

        actual_rr = abs(tp2_price - entry_price) / abs(entry_price - sl_price)
        if actual_rr < self.min_rr_ratio:
            return {
                "approved": False,
                "reason":   f"RR {actual_rr:.2f}:1 below minimum {self.min_rr_ratio}:1.",
            }

        lot_size        = self.size_position(current_equity, entry_price, sl_price)
        # Use the actual risk_fraction computed above (Kelly / tier / default),
        # not max_risk_per_trade which ignores Kelly and tier scaling.
        price_risk      = abs(entry_price - sl_price)
        capital_risked  = round(lot_size * price_risk, 2)
        sl_distance_pct = round(abs(entry_price - sl_price) / entry_price * 100, 3)

        return {
            "approved":         True,
            "symbol":           symbol,
            "direction":        direction,
            "entry":            round(entry_price, 5),
            "stop_loss":        round(sl_price, 5),
            "take_profit_1":    round(tp1_price, 5),
            "take_profit_2":    round(tp2_price, 5),
            "take_profit":      round(tp2_price, 5),
            "lot_size":         lot_size,
            "rr_ratio":         round(actual_rr, 2),
            "sl_distance_pct":  sl_distance_pct,
            "capital_risked":   capital_risked,
            "atr":              round(atr, 5),
            # FIX: Expose ATR explicitly for ExecutionAgent's trailing SL
            "atr_value":        atr,
            # Also expose SL multiplier so the trail distance is consistent
            "sl_multiplier":    sl_mult,
            "confidence_pct":   confidence_pct,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # TRADE RESULT LOGGING — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def log_trade_result(self, result: str, r_multiple: float):
        self.trade_log.append({
            "result":     result,
            "r_multiple": r_multiple,
            "ts":         datetime.utcnow(),
        })

        if result == "L":
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        recent = self.trade_log[-50:]
        if len(recent) >= 10:
            expectancy = np.mean([t['r_multiple'] for t in recent])
            win_rate   = sum(1 for t in recent if t['result'] == 'W') / len(recent)
            logger.info(
                f"[RiskAgent] Expectancy(50)={expectancy:.2f}R | WR={win_rate*100:.1f}%"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # STATS DASHBOARD — unchanged from v9.0
    # ─────────────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        if not self.trade_log:
            return {"trades": 0, "message": "No trades logged yet."}

        recent = self.trade_log[-50:] if len(self.trade_log) >= 50 else self.trade_log
        total  = len(recent)
        wins   = sum(1 for t in recent if t['result'] == 'W')
        r_vals = [t['r_multiple'] for t in recent]

        win_rate      = wins / total * 100
        expectancy    = np.mean(r_vals)
        profit_factor = (
            sum(r for r in r_vals if r > 0) /
            abs(sum(r for r in r_vals if r < 0))
            if any(r < 0 for r in r_vals) else 999
        )

        return {
            "total_trades":       total,
            "win_rate":           f"{win_rate:.1f}%",
            "expectancy_per_R":   f"{expectancy:.2f}R",
            "profit_factor":      f"{profit_factor:.2f}",
            "consecutive_losses": self.consecutive_losses,
            "equity_peak":        self.equity_peak,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _notify(self, msg: str):
        if self.telegram:
            try:
                self.telegram.send_message_sync(msg)
            except Exception as e:
                logger.error(f"Telegram notify failed: {e}")
