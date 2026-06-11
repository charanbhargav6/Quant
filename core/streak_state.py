"""
CRAVE v10.0 — Streak State Tracker & Circuit Breaker
======================================================
Tracks daily P&L and consecutive losing days.
Persists to JSON so state survives bot restarts.
Synced to GitHub so all three nodes see the same state.

RULES ENCODED:
  Daily hard stop:     -4% on the day → close everything, no new entries
  Circuit breaker:     2 consecutive losing days → 24h cooldown
  Deep circuit breaker: 3 consecutive losing days → 48h + Telegram alert
  Re-entry:            Half-size on first day back after cooldown
  Scaling up:          Anti-martingale — risk increases after wins

USAGE:
  from core.streak_state import streak
  
  # Check before taking a trade
  allowed, reason = streak.can_trade()
  if not allowed:
      logger.warning(reason)
      return
  
  # Get current risk level
  risk_pct = streak.get_current_risk_pct("A+")
  
  # Update after trade closes
  streak.record_trade_result(r_multiple=2.0)
  
  # Update daily P&L (call this continuously during trading)
  streak.update_daily_pnl(current_equity, start_equity)
  
  # Call at end of each day (21:00 UTC)
  streak.close_day()
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, Optional

logger = logging.getLogger("crave.streak")


class StreakStateTracker:

    def __init__(self, state_file: Optional[str] = None):
        from config.config import STATE_FILE, RISK
        self.state_file = Path(state_file or STATE_FILE)
        self.risk_cfg   = RISK

        # Load persisted state or create fresh
        self._state = self._load()
        logger.info(
            f"[Streak] Loaded state: "
            f"streak={self._state['consecutive_wins']}W / "
            f"{self._state['consecutive_loss_days']}L-days | "
            f"CB={'ACTIVE' if self._state['circuit_breaker_active'] else 'off'}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # STATE STRUCTURE
    # ─────────────────────────────────────────────────────────────────────────

    def _default_state(self) -> dict:
        return {
            # Trade-level streak
            "consecutive_wins":         0,
            "consecutive_losses":       0,   # individual trades (not days)

            # Day-level streak
            "consecutive_loss_days":    0,
            "today_pnl_pct":            0.0,
            "today_trades":             0,
            "session_date":             self._session_date(),

            # Circuit breaker
            "circuit_breaker_active":   False,
            "circuit_breaker_until":    None,   # ISO datetime string
            "half_size_day":            False,  # true on re-entry day

            # Daily baseline (set at session open)
            "daily_start_equity":       None,
            "daily_date_set":           None,

            # History (last 7 days)
            "day_history":              [],     # list of {date, pnl, result}

            # Manual overrides
            "manually_paused":          False,
            "pause_reason":             "",
        }

    def _session_date(self) -> str:
        """
        NY session day boundary = 21:00 UTC.
        A new session starts at 21:00 UTC (NY close), not calendar midnight.
        This prevents the daily loss counter from resetting mid-US-session
        for traders in UTC+5:30 (India).
        """
        now = datetime.now(timezone.utc)
        if now.hour >= 21:
            return (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    # ─────────────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    saved = json.load(f)
                # Merge with defaults to handle new keys added in updates
                state = self._default_state()
                state.update(saved)
                return state
            except Exception as e:
                logger.warning(f"[Streak] Could not load state: {e}. Using fresh state.")
        return self._default_state()

    def _save(self):
        """Persist state to JSON file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"[Streak] Failed to save state: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION RESET
    # ─────────────────────────────────────────────────────────────────────────

    def _check_session_rollover(self):
        """
        Check if we've crossed into a new NY session day.
        If yes, finalize yesterday and reset daily counters.
        """
        current_session = self._session_date()
        if self._state["session_date"] != current_session:
            logger.info(
                f"[Streak] Session rollover: "
                f"{self._state['session_date']} → {current_session}"
            )
            # Archive the closing day
            self._archive_day(self._state["session_date"],
                               self._state["today_pnl_pct"])
            # Reset daily counters
            self._state["today_pnl_pct"]    = 0.0
            self._state["today_trades"]     = 0
            self._state["session_date"]     = current_session
            self._state["daily_start_equity"] = None
            self._state["daily_date_set"]   = current_session
            self._state["half_size_day"]    = False

            # Check if circuit breaker has expired
            if self._state["circuit_breaker_active"]:
                cb_until = self._state.get("circuit_breaker_until")
                if cb_until:
                    until_dt = datetime.fromisoformat(cb_until)
                    if datetime.now(timezone.utc) >= until_dt:
                        self._state["circuit_breaker_active"] = False
                        self._state["circuit_breaker_until"]  = None
                        self._state["half_size_day"]          = True
                        logger.info("[Streak] Circuit breaker expired. Half-size day activated.")

            self._save()

    def _archive_day(self, date: str, pnl: float):
        """Add day to history and update consecutive loss days."""
        result = "W" if pnl >= 0 else "L"
        history = self._state.get("day_history", [])
        history.append({"date": date, "pnl": round(pnl, 4), "result": result})
        # Keep last 30 days
        self._state["day_history"] = history[-30:]

        if result == "L":
            self._state["consecutive_loss_days"] += 1
            self._maybe_trigger_circuit_breaker()
        else:
            # Winning day resets consecutive loss days
            self._state["consecutive_loss_days"] = 0

        # Save day to database
        try:
            from core.database_manager import db
            db.save_day_stats(
                date=date,
                pnl_pct=pnl,
                consecutive_losses=self._state["consecutive_loss_days"],
                circuit_breaker_fired=self._state.get("circuit_breaker_active", False),
                risk_level=self.get_streak_state(),
                trades_today=self._state.get("today_trades", 0),
            )
        except Exception as e:
            logger.warning(f"[Streak] Could not save day stats to DB: {e}")

    def _maybe_trigger_circuit_breaker(self):
        """Trigger circuit breaker after configured consecutive losing days."""
        loss_days = self._state["consecutive_loss_days"]
        cfg       = self.risk_cfg

        if loss_days >= cfg["circuit_breaker_losing_days"]:
            cooldown_h = cfg["circuit_breaker_cooldown_h"]
            # 3+ days = double cooldown
            if loss_days >= 3:
                cooldown_h *= 2

            until = datetime.now(timezone.utc) + timedelta(hours=cooldown_h)
            self._state["circuit_breaker_active"] = True
            self._state["circuit_breaker_until"]  = until.isoformat()

            msg = (
                f"⚠️ CIRCUIT BREAKER TRIGGERED\n"
                f"Consecutive losing days: {loss_days}\n"
                f"Cooldown: {cooldown_h}h\n"
                f"Resumes: {until.strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"Half-size mode on re-entry."
            )
            logger.critical(msg)
            self._notify(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master check — call this before every potential trade entry.
        Returns (allowed: bool, reason: str).
        """
        self._check_session_rollover()

        # Manual pause
        if self._state["manually_paused"]:
            return False, f"Manually paused: {self._state.get('pause_reason', '')}"

        # Circuit breaker
        if self._state["circuit_breaker_active"]:
            until = self._state.get("circuit_breaker_until", "unknown")
            return False, f"Circuit breaker active until {until}"

        # Daily hard stop — -4% on the day
        max_daily = self.risk_cfg["max_daily_loss_pct"]
        if self._state["today_pnl_pct"] <= -max_daily:
            return False, (
                f"Daily loss limit hit: {self._state['today_pnl_pct']:.2f}% "
                f"(limit: -{max_daily}%)"
            )

        return True, "OK"

    def update_daily_pnl(self, current_equity: float,
                          start_equity: Optional[float] = None):
        """
        Update today's P&L percentage.
        Call this continuously during trading with latest equity.
        start_equity: if None, uses the equity at session start.
        """
        self._check_session_rollover()

        # Set daily baseline if not set
        if self._state["daily_start_equity"] is None:
            self._state["daily_start_equity"] = start_equity or current_equity
            self._save()
            return

        base = self._state["daily_start_equity"]
        if base and base > 0:
            pnl = (current_equity - base) / base * 100
            self._state["today_pnl_pct"] = round(pnl, 4)

            # Trigger hard stop
            max_daily = self.risk_cfg["max_daily_loss_pct"]
            if pnl <= -max_daily and not self._state.get("_daily_stop_fired"):
                self._state["_daily_stop_fired"] = True
                msg = (
                    f"🚨 DAILY LOSS LIMIT HIT: {pnl:.2f}%\n"
                    f"Hard stop for today. No new entries.\n"
                    f"All positions being evaluated."
                )
                logger.critical(msg)
                self._notify(msg)

            self._save()

    def record_trade_result(self, r_multiple: float):
        """
        Call after every trade closes.
        Updates trade-level consecutive wins/losses.
        """
        self._check_session_rollover()
        self._state["today_trades"] += 1

        if r_multiple > 0:
            self._state["consecutive_wins"]   += 1
            self._state["consecutive_losses"]  = 0
        else:
            self._state["consecutive_losses"] += 1
            self._state["consecutive_wins"]    = 0

        self._save()

    def get_streak_state(self) -> str:
        """
        Returns the current streak state key for the risk scale table.
        Maps to RISK['scale_table'] keys.
        """
        wins   = self._state["consecutive_wins"]
        losses = self._state["consecutive_losses"]
        l_days = self._state["consecutive_loss_days"]

        if l_days >= 3 or losses >= 3:
            return "3+_losses"
        if l_days >= 2 or losses >= 2:
            return "2_losses"
        if wins >= 5:
            return "5+_wins"
        if wins >= 3:
            return "3-4_wins"
        if wins >= 1:
            return "1-2_wins"
        return "neutral"

    def get_current_risk_pct(self, grade: str = "A+") -> float:
        """
        Returns effective risk % for this grade at the current streak state.
        This is what gets passed to RiskAgent.size_position().
        """
        from config.config import get_risk_for_grade_and_streak
        streak = self.get_streak_state()

        # Half-size on re-entry day after circuit breaker
        result = get_risk_for_grade_and_streak(grade, streak)
        if self._state.get("half_size_day"):
            result *= 0.5

        return round(result, 4)

    def close_day(self):
        """
        Call at 21:00 UTC (NY close) to finalise the day.
        Archives today's P&L and updates consecutive loss days.
        """
        self._archive_day(
            date=self._state["session_date"],
            pnl=self._state["today_pnl_pct"],
        )
        # Force rollover: set to yesterday so next _check_session_rollover()
        # sees a genuine date change rather than archiving a blank date.
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        self._state["session_date"] = yesterday
        self._save()
        logger.info(
            f"[Streak] Day closed. PnL={self._state['today_pnl_pct']:.2f}% | "
            f"ConsecLossDays={self._state['consecutive_loss_days']}"
        )

    def manual_pause(self, reason: str = ""):
        """Pause all new entries manually (Telegram /pause command)."""
        self._state["manually_paused"] = True
        self._state["pause_reason"]    = reason
        self._save()
        logger.info(f"[Streak] Manually paused. Reason: {reason}")

    def manual_resume(self):
        """Resume after manual pause (Telegram /resume command)."""
        self._state["manually_paused"] = False
        self._state["pause_reason"]    = ""
        self._save()
        logger.info("[Streak] Manually resumed.")

    def get_status(self) -> dict:
        """Full status dict for /status Telegram command."""
        self._check_session_rollover()
        return {
            "session_date":          self._state["session_date"],
            "today_pnl_pct":         f"{self._state['today_pnl_pct']:.2f}%",
            "today_trades":          self._state["today_trades"],
            "streak_state":          self.get_streak_state(),
            "consecutive_wins":      self._state["consecutive_wins"],
            "consecutive_losses":    self._state["consecutive_losses"],
            "consecutive_loss_days": self._state["consecutive_loss_days"],
            "circuit_breaker":       self._state["circuit_breaker_active"],
            "circuit_breaker_until": self._state.get("circuit_breaker_until"),
            "half_size_day":         self._state["half_size_day"],
            "manually_paused":       self._state["manually_paused"],
            "can_trade":             self.can_trade()[0],
            "current_risk_A+":       f"{self.get_current_risk_pct('A+'):.2f}%",
            "current_risk_B":        f"{self.get_current_risk_pct('B'):.2f}%",
        }

    def get_status_message(self) -> str:
        """Formatted status for Telegram."""
        s = self.get_status()
        cb_line = ""
        if s["circuit_breaker"]:
            cb_line = f"\n🔴 CB ACTIVE until {s['circuit_breaker_until']}"
        elif s["half_size_day"]:
            cb_line = "\n🟡 Half-size re-entry day"

        return (
            f"📊 STREAK STATUS\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Session   : {s['session_date']}\n"
            f"Day P&L   : {s['today_pnl_pct']}\n"
            f"Trades    : {s['today_trades']}\n"
            f"Streak    : {s['consecutive_wins']}W / {s['consecutive_losses']}L\n"
            f"Loss Days : {s['consecutive_loss_days']}\n"
            f"Risk (A+) : {s['current_risk_A+']}\n"
            f"Risk (B)  : {s['current_risk_B']}\n"
            f"Can Trade : {'✅' if s['can_trade'] else '❌'}"
            f"{cb_line}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _notify(self, msg: str):
        """Send Telegram alert. Fails silently if Telegram not ready."""
        try:
            from interfaces.telegram_interface import tg
            tg.send(msg)
        except Exception:
            pass   # Telegram may not be initialised yet — that's fine


# ── Singleton ─────────────────────────────────────────────────────────────────
streak = StreakStateTracker()
