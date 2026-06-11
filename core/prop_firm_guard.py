"""
CRAVE v10.5 — Prop Firm Guard
================================
Universal rules engine for ALL prop firm platforms.
Enforces firm-specific rules BEFORE every trade to prevent account failure.

SUPPORTED FIRMS (2026 rules):
  • FTMO          — 10% max loss, 5% daily loss, 10% target, 4 min days
  • FundedNext    — 10% max loss, 5% daily loss, 8-10% target
  • The5ers       — 6% max loss, 4% daily loss, 8% target (Hyper plan)
  • Apex          — 6% max loss, no daily limit (futures focused)
  • TopStep       — 6% max loss, no daily limit (futures)
  • XM Live       — No challenge rules, just broker margin rules
  • Binance       — No challenge rules, just position limits
  • Zerodha/Kite  — SEBI margin rules

KEY INSIGHT (2026 research):
  The #1 reason traders fail prop challenges is NOT bad entry decisions —
  it's violating daily loss limits from overleveraging ONE bad session.
  
  CRAVE's standard 2% risk/trade = safe for most firms.
  But a 3-loss streak = 6% daily loss = FTMO daily limit breached.
  
  This guard adds a FIRM-SPECIFIC risk scaler that reduces position size
  as you approach each firm's limits, creating a "buffer zone" that
  prevents limit breaches while keeping you active in the market.

USAGE:
  from core.prop_firm_guard import PropFirmGuard

  guard = PropFirmGuard(firm="ftmo", account_size=100000)
  guard.update_equity(current_equity)
  
  result = guard.check_trade(risk_pct=0.02)
  if not result["allowed"]:
      print(result["reason"])
  else:
      actual_risk = result["scaled_risk_pct"]  # may be reduced
"""

import logging
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("crave.prop_firm_guard")

# ── Firm rules database (verified April 2026) ─────────────────────────────
FIRM_RULES = {
    "ftmo": {
        "name":            "FTMO",
        "max_loss_pct":    0.10,   # 10% of starting balance (static — key advantage)
        "daily_loss_pct":  0.05,   # 5% of starting balance
        "profit_target":   0.10,   # 10% Phase 1, 5% Phase 2
        "min_trading_days": 4,
        "news_restriction": True,  # no trading 2min before/after high-impact news
        "drawdown_type":   "static",  # calculated from STARTING balance, not peak
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Static drawdown = biggest advantage. Build cushion aggressively.",
    },
    "fundednext": {
        "name":            "FundedNext",
        "max_loss_pct":    0.10,
        "daily_loss_pct":  0.05,
        "profit_target":   0.08,
        "min_trading_days": 5,
        "news_restriction": False,  # Stellar plan allows news trading
        "drawdown_type":   "static",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Best profit split (95%) on Stellar plan. Lower fees than FTMO.",
    },
    "the5ers": {
        "name":            "The5ers Hyper",
        "max_loss_pct":    0.06,   # Tighter — 6% max
        "daily_loss_pct":  0.04,   # 4% daily
        "profit_target":   0.08,
        "min_trading_days": 0,     # No minimum
        "news_restriction": False,
        "drawdown_type":   "trailing",  # Trailing from peak — harder
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Trailing drawdown — be careful after big wins. Instant funding.",
    },
    "apex": {
        "name":            "Apex Trader",
        "max_loss_pct":    0.06,
        "daily_loss_pct":  None,   # No daily limit
        "profit_target":   None,   # Flexible
        "min_trading_days": 0,
        "news_restriction": False,
        "drawdown_type":   "trailing",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Futures only. No daily limit is a big advantage.",
    },
    "topstep": {
        "name":            "TopStep",
        "max_loss_pct":    0.06,
        "daily_loss_pct":  None,
        "profit_target":   None,
        "min_trading_days": 0,
        "news_restriction": False,
        "drawdown_type":   "trailing",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Futures. EOD drawdown calc — more breathing room intraday.",
    },
    "xm": {
        "name":            "XM Live",
        "max_loss_pct":    None,   # No challenge — just your own capital
        "daily_loss_pct":  None,
        "profit_target":   None,
        "min_trading_days": None,
        "news_restriction": False,
        "drawdown_type":   "none",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Live broker. CRAVE standard risk rules apply.",
    },
    "binance": {
        "name":            "Binance",
        "max_loss_pct":    None,
        "daily_loss_pct":  None,
        "profit_target":   None,
        "min_trading_days": None,
        "news_restriction": False,
        "drawdown_type":   "none",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Live crypto. CRAVE standard risk rules apply.",
    },
    "zerodha": {
        "name":            "Zerodha/Kite",
        "max_loss_pct":    None,
        "daily_loss_pct":  None,
        "profit_target":   None,
        "min_trading_days": None,
        "news_restriction": False,
        "drawdown_type":   "none",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "India broker. SEBI intraday margin rules apply.",
    },
    "fundingpips": {
        "name":            "Funding Pips (2-Step Pro)",
        "max_loss_pct":    0.06,   # 6% max overall drawdown
        "daily_loss_pct":  0.03,   # 3% daily loss limit — STRICT
        "profit_target":   0.08,   # 8% Phase 1, 5% Phase 2
        "min_trading_days": 3,
        "news_restriction": False,  # Funding Pips allows news trading
        "drawdown_type":   "static",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "2-Step Pro: Tightest limits (3% daily, 6% max). Static drawdown. 80% profit split.",
    },
    "fundingpips_1step": {
        "name":            "Funding Pips (1-Step)",
        "max_loss_pct":    0.06,   # 6% max
        "daily_loss_pct":  0.03,   # 3% daily
        "profit_target":   0.10,   # 10% target
        "min_trading_days": 3,
        "news_restriction": False,
        "drawdown_type":   "static",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "1-Step: Single phase, 10% target, 3% daily, 6% max. 80% split.",
    },
    "fundingpips_standard": {
        "name":            "Funding Pips (2-Step Standard)",
        "max_loss_pct":    0.10,   # 10% max
        "daily_loss_pct":  0.05,   # 5% daily
        "profit_target":   0.08,   # 8% Phase 1
        "min_trading_days": 3,
        "news_restriction": False,
        "drawdown_type":   "static",
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "2-Step Standard: More relaxed (5% daily, 10% max). 80% split.",
    },
    "fundingpips_zero": {
        "name":            "Funding Pips (Zero)",
        "max_loss_pct":    0.05,   # 5% max — tightest overall
        "daily_loss_pct":  0.03,   # 3% daily
        "profit_target":   0.08,   # 8% target
        "min_trading_days": 3,
        "news_restriction": False,
        "drawdown_type":   "trailing",  # TRAILING from peak equity — hardest mode
        "max_trades_per_day": None,
        "max_position_lots": None,
        "note": "Zero: Trailing drawdown from equity peak. 3% daily, 5% max. 90% split.",
    },
}


class PropFirmGuard:
    """
    Enforces prop firm rules before every trade.
    Scales risk down as limits approach — never hard-blocks until limit is hit.
    """

    def __init__(self, firm: str = "ftmo", account_size: float = 10000.0):
        self.firm_key      = firm.lower()
        self.rules         = FIRM_RULES.get(self.firm_key, FIRM_RULES["ftmo"])
        self.account_size  = account_size       # starting/initial balance
        self.current_equity = account_size
        self.peak_equity   = account_size

        # Session tracking
        self._session_day: Optional[object] = None
        self._session_start_equity: float = account_size
        self._trades_today: int = 0
        self._trading_days: set = set()

        # State
        self._paused_until: Optional[datetime] = None
        self._pause_reason: str = ""

        logger.info(
            f"[PropFirmGuard] Initialized: {self.rules['name']} | "
            f"Account: ${account_size:,.0f} | "
            f"Max loss: {(self.rules['max_loss_pct'] or 0)*100:.0f}% | "
            f"Daily limit: {(self.rules['daily_loss_pct'] or 0)*100:.0f}%"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # EQUITY UPDATE (call every tick/bar)
    # ─────────────────────────────────────────────────────────────────────────

    def update_equity(self, current_equity: float):
        """Update current equity. Call after every position change."""
        self.current_equity = current_equity

        if self.rules["drawdown_type"] == "trailing":
            if current_equity > self.peak_equity:
                self.peak_equity = current_equity

        # Session day tracking (NY close = 21:00 UTC)
        now_utc = datetime.now(timezone.utc)
        session_day = (now_utc + timedelta(hours=3)).date()  # NY-aligned
        if self._session_day != session_day:
            self._session_day = session_day
            self._session_start_equity = current_equity
            self._trades_today = 0
            logger.info(
                f"[PropFirmGuard] New session: {session_day} | "
                f"Session start equity: ${current_equity:,.2f}"
            )

        # Track trading day for min_days requirement
        self._trading_days.add(session_day)

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN TRADE CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def check_trade(self, risk_pct: float = 0.02,
                    symbol: str = "",
                    during_news: bool = False) -> dict:
        """
        Check if a trade is allowed under firm rules.
        Returns dict with: allowed, scaled_risk_pct, reason, warnings, status
        """
        result = {
            "allowed":         True,
            "scaled_risk_pct": risk_pct,
            "reason":          "",
            "warnings":        [],
            "status":          {},
            "firm":            self.rules["name"],
        }

        # ── Paused check ──────────────────────────────────────────────────
        if self._paused_until:
            if datetime.now(timezone.utc) < self._paused_until:
                result["allowed"] = False
                result["reason"]  = f"Trading paused until {self._paused_until.strftime('%H:%M UTC')}: {self._pause_reason}"
                return result
            else:
                self._paused_until = None
                self._pause_reason = ""

        # ── News restriction check ────────────────────────────────────────
        if self.rules["news_restriction"] and during_news:
            result["allowed"] = False
            result["reason"]  = f"{self.rules['name']} prohibits trading during high-impact news events."
            return result

        # ── Compute drawdown levels ────────────────────────────────────────
        if self.rules["drawdown_type"] == "static":
            # Loss from STARTING balance
            total_loss_pct = (self.account_size - self.current_equity) / self.account_size
            drawdown_ref   = self.account_size
        elif self.rules["drawdown_type"] == "trailing":
            # Loss from PEAK equity
            total_loss_pct = (self.peak_equity - self.current_equity) / self.peak_equity
            drawdown_ref   = self.peak_equity
        else:
            total_loss_pct = 0.0
            drawdown_ref   = self.account_size

        # Daily loss
        daily_loss_pct = (
            (self._session_start_equity - self.current_equity) / self._session_start_equity
            if self._session_start_equity > 0 else 0
        )

        # ── Hard limits check ─────────────────────────────────────────────
        max_loss = self.rules["max_loss_pct"]
        if max_loss and total_loss_pct >= max_loss:
            result["allowed"] = False
            result["reason"]  = (
                f"ACCOUNT BREACHED: {total_loss_pct*100:.2f}% total loss exceeds "
                f"{self.rules['name']} limit of {max_loss*100:.0f}%."
            )
            self._pause_until_session_end()
            return result

        daily_limit = self.rules["daily_loss_pct"]
        if daily_limit and daily_loss_pct >= daily_limit:
            result["allowed"] = False
            result["reason"]  = (
                f"DAILY LIMIT HIT: {daily_loss_pct*100:.2f}% today exceeds "
                f"{self.rules['name']} daily limit of {daily_limit*100:.0f}%."
            )
            self._pause_until_session_end()
            return result

        # ── Risk scaling (buffer zone) ────────────────────────────────────
        # As we approach limits, scale down risk to create safety buffer.
        # BUFFER ZONES:
        #   > 70% of limit used → reduce risk by 50%
        #   > 85% of limit used → reduce risk by 75%
        #   > 95% of limit used → block trading (too close)

        scaled_risk = risk_pct

        if max_loss and max_loss > 0:
            loss_usage = total_loss_pct / max_loss
            if loss_usage >= 0.95:
                result["allowed"] = False
                result["reason"]  = (
                    f"Too close to max loss limit ({total_loss_pct*100:.1f}% / {max_loss*100:.0f}%). "
                    f"Protecting account — no new trades."
                )
                return result
            elif loss_usage >= 0.85:
                scaled_risk *= 0.25
                result["warnings"].append(
                    f"⚠️ {loss_usage*100:.0f}% of max loss used — risk reduced to 25%"
                )
            elif loss_usage >= 0.70:
                scaled_risk *= 0.50
                result["warnings"].append(
                    f"⚠️ {loss_usage*100:.0f}% of max loss used — risk halved"
                )

        if daily_limit and daily_limit > 0:
            daily_usage = daily_loss_pct / daily_limit
            if daily_usage >= 0.90:
                result["allowed"] = False
                result["reason"]  = (
                    f"Too close to daily limit ({daily_loss_pct*100:.2f}% / {daily_limit*100:.0f}%). "
                    f"Protecting account for rest of session."
                )
                return result
            elif daily_usage >= 0.75:
                scaled_risk = min(scaled_risk, risk_pct * 0.33)
                result["warnings"].append(
                    f"⚠️ {daily_usage*100:.0f}% of daily limit used — risk reduced to 33%"
                )
            elif daily_usage >= 0.50:
                scaled_risk = min(scaled_risk, risk_pct * 0.66)
                result["warnings"].append(
                    f"⚠️ {daily_usage*100:.0f}% of daily limit used — risk reduced to 66%"
                )

        result["scaled_risk_pct"] = round(scaled_risk, 5)
        result["status"] = self._build_status(
            total_loss_pct, daily_loss_pct, max_loss, daily_limit
        )

        # ── Profit target progress ────────────────────────────────────────
        profit_pct = (self.current_equity - self.account_size) / self.account_size
        target     = self.rules.get("profit_target")
        if target and profit_pct >= target:
            result["warnings"].append(
                f"🎯 PROFIT TARGET REACHED: {profit_pct*100:.2f}% / {target*100:.0f}%. "
                f"Consider requesting payout now."
            )

        return result

    def _build_status(self, total_loss, daily_loss, max_loss, daily_limit) -> dict:
        profit_pct = (self.current_equity - self.account_size) / self.account_size
        target = self.rules.get("profit_target") or 0
        return {
            "equity":          self.current_equity,
            "account_size":    self.account_size,
            "profit_pct":      round(profit_pct * 100, 2),
            "profit_target":   round(target * 100, 1) if target else None,
            "target_remaining": round((target - profit_pct) * 100, 2) if target else None,
            "total_loss_pct":  round(total_loss * 100, 2),
            "max_loss_limit":  round((max_loss or 0) * 100, 1),
            "daily_loss_pct":  round(daily_loss * 100, 2),
            "daily_loss_limit": round((daily_limit or 0) * 100, 1),
            "trading_days":    len(self._trading_days),
            "min_days_needed": self.rules.get("min_trading_days") or 0,
            "drawdown_type":   self.rules["drawdown_type"],
        }

    def _pause_until_session_end(self):
        """Pause trading until next NY session (21:00 UTC)."""
        now = datetime.now(timezone.utc)
        session_end = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now.hour >= 21:
            session_end += timedelta(days=1)
        self._paused_until = session_end

    def get_dashboard_summary(self) -> str:
        """Short summary for Telegram / dashboard."""
        profit_pct = (self.current_equity - self.account_size) / self.account_size
        max_loss   = self.rules.get("max_loss_pct") or 0
        daily_lim  = self.rules.get("daily_loss_pct") or 0
        target     = self.rules.get("profit_target") or 0

        lines = [
            f"🏦 <b>{self.rules['name']}</b>",
            f"Balance: ${self.current_equity:,.2f}",
            f"P&L: {profit_pct*100:+.2f}% {'✅' if profit_pct > 0 else '🔴'}",
        ]
        if target:
            prog = min(profit_pct / target, 1.0) * 100 if target > 0 else 0
            lines.append(f"Target: {profit_pct*100:.1f}% / {target*100:.0f}% ({prog:.0f}%)")
        if max_loss:
            used = (self.account_size - self.current_equity) / self.account_size
            lines.append(f"Max loss buffer: {(max_loss-used)*100:.1f}% remaining")
        lines.append(f"Trading days: {len(self._trading_days)}")
        return "\n".join(lines)

    @staticmethod
    def list_firms() -> list:
        return [
            {"key": k, "name": v["name"], "note": v["note"]}
            for k, v in FIRM_RULES.items()
        ]
