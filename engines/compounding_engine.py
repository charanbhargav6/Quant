"""
CRAVE v10.5 — Micro-Position Compounding Engine
=================================================
Starts from any account size (even $100).
Auto-scales position size as equity grows.
Shows milestone progress and projected growth.

HOW IT WORKS:
  Instead of fixed 2% risk, risk is TIERED by account size:
  
  Tier 1  $100–$500    → risk 0.5% per trade  (e.g. $0.50–$2.50 per trade)
  Tier 2  $500–$2000   → risk 0.75%            ($3.75–$15)
  Tier 3  $2000–$5000  → risk 1.0%             ($20–$50)
  Tier 4  $5000–$20000 → risk 1.5%             ($75–$300)
  Tier 5  $20000+      → risk 2.0%             ($400+)
  
  Each trade's dollar risk is small but COMPOUNDS.
  10% monthly on $100 = $10 gain → $110 next month → $121...

BROKER MINIMUMS (what you actually need to start):
  Binance Futures: $10 USDT min notional (0.001 BTC @ ~$95k = $95 notional)
  Binance Spot:    $5 min order
  Zerodha Kite:    ₹1 min (no effective minimum)
  Alpaca (Forex):  $1000 min account recommended
  XM:              $5 min deposit, 0.01 lot = $1000 notional on EURUSD (1:1000 leverage)

BEST INSTRUMENTS FOR SMALL ACCOUNTS (ranked by min entry cost):
  1. Binance BTCUSDT (0.001 BTC = ~$95)   — best liquidity, 24/7
  2. Binance ETHUSDT (0.01 ETH = ~$18)    — great for $100+ accounts
  3. Binance SOLUSDT (0.1 SOL = ~$14)     — lowest entry on Binance
  4. XM EURUSD (0.01 lot = 1000 units)    — forex with leverage

COMPOUNDING MATH:
  Monthly return target: 7% (conservative for 5-15% range)
  
  Start $100 → after 12 months: $225
  Start $500 → after 12 months: $1,125
  Start $1000 → after 12 months: $2,252
  Start $10000 → after 12 months: $22,522
"""

import logging
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crave.compound")

CRAVE_ROOT = Path(os.environ.get("CRAVE_ROOT", Path(__file__).resolve().parents[1]))

# ── Risk tiers (account_size → risk_pct) ─────────────────────────────────────
RISK_TIERS = [
    (100,    0.005),   # $0–$100     → 0.5%
    (500,    0.005),   # $100–$500   → 0.5%
    (2000,   0.0075),  # $500–$2000  → 0.75%
    (5000,   0.010),   # $2000–$5000 → 1.0%
    (20000,  0.015),   # $5k–$20k    → 1.5%
    (float("inf"), 0.020),  # $20k+   → 2.0%
]

# ── Broker min notional / min qty per instrument ──────────────────────────────
BROKER_MINIMUMS = {
    # Binance (all values in USDT notional)
    "BTCUSDT":  {"min_qty": 0.001, "min_notional": 5.0,  "qty_step": 0.001, "broker": "binance"},
    "ETHUSDT":  {"min_qty": 0.01,  "min_notional": 5.0,  "qty_step": 0.01,  "broker": "binance"},
    "SOLUSDT":  {"min_qty": 0.1,   "min_notional": 5.0,  "qty_step": 0.1,   "broker": "binance"},
    # Alpaca Forex (in units)
    "EURUSD=X": {"min_qty": 1.0,   "min_notional": 1.0,  "qty_step": 1.0,   "broker": "alpaca"},
    "GBPUSD=X": {"min_qty": 1.0,   "min_notional": 1.0,  "qty_step": 1.0,   "broker": "alpaca"},
    "USDJPY=X": {"min_qty": 1.0,   "min_notional": 1.0,  "qty_step": 1.0,   "broker": "alpaca"},
    "AUDUSD=X": {"min_qty": 1.0,   "min_notional": 1.0,  "qty_step": 1.0,   "broker": "alpaca"},
    # Gold
    "XAUUSD=X": {"min_qty": 0.01,  "min_notional": 20.0, "qty_step": 0.01,  "broker": "alpaca"},
}

# ── Milestones ────────────────────────────────────────────────────────────────
MILESTONES = [100, 200, 500, 1000, 2000, 5000, 10000, 25000, 50000, 100000]


class CompoundingEngine:

    def __init__(self):
        self._state_file = CRAVE_ROOT / "data" / "compound_state.json"
        self._state      = self._load_state()

    # ─────────────────────────────────────────────────────────────────────────
    # RISK TIER — scales automatically with equity
    # ─────────────────────────────────────────────────────────────────────────

    def get_risk_pct(self, equity: float) -> float:
        """Return the correct risk fraction for this equity level."""
        for threshold, pct in RISK_TIERS:
            if equity <= threshold:
                return pct
        return 0.020

    def get_dollar_risk(self, equity: float) -> float:
        """Dollar amount risked per trade at current equity."""
        return round(equity * self.get_risk_pct(equity), 2)

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION SIZING — with broker minimum enforcement
    # ─────────────────────────────────────────────────────────────────────────

    def size_position(self, symbol: str, equity: float,
                      entry_price: float, stop_loss_price: float) -> dict:
        """
        Calculate position size for any account size.
        Enforces broker minimums and rounds to correct step.
        
        Returns dict with:
          qty:           actual quantity to trade
          dollar_risk:   dollars at risk
          notional:      total position value in USD
          tradeable:     bool — can we trade with this account size?
          reason:        why not tradeable (if applicable)
          risk_pct:      fraction of equity risked
        """
        price_risk = abs(entry_price - stop_loss_price)
        if price_risk == 0:
            return {"tradeable": False, "reason": "SL = entry price", "qty": 0}

        risk_pct    = self.get_risk_pct(equity)
        dollar_risk = equity * risk_pct
        raw_qty     = dollar_risk / price_risk

        # Get broker constraints
        mins = BROKER_MINIMUMS.get(symbol, {
            "min_qty": 0.001, "min_notional": 1.0, "qty_step": 0.001, "broker": "unknown"
        })

        # Round down to step size
        step = mins["qty_step"]
        qty  = max(mins["min_qty"], (raw_qty // step) * step)

        notional = qty * entry_price

        # Check minimum notional
        if notional < mins["min_notional"]:
            # Can we hit minimum notional with this equity?
            min_notional_needed = mins["min_notional"]
            needed_equity_for_min = (min_notional_needed * price_risk / entry_price) / risk_pct
            return {
                "tradeable":   False,
                "reason":      (
                    f"Account too small: ${equity:.0f} equity → ${dollar_risk:.2f} risk → "
                    f"{raw_qty:.6f} {symbol} → ${notional:.2f} notional. "
                    f"Broker minimum: ${mins['min_notional']}. "
                    f"Need ~${needed_equity_for_min:.0f} equity for this instrument."
                ),
                "qty":               0,
                "dollar_risk":       round(dollar_risk, 2),
                "min_needed_equity": round(needed_equity_for_min, 0),
                "risk_pct":          risk_pct,
            }

        return {
            "tradeable":   True,
            "qty":         round(qty, 8),
            "dollar_risk": round(dollar_risk, 2),
            "notional":    round(notional, 2),
            "risk_pct":    risk_pct,
            "risk_tier":   self._get_tier_label(equity),
            "reason":      "",
        }

    def _get_tier_label(self, equity: float) -> str:
        if equity < 500:   return "Tier 1 (micro)"
        if equity < 2000:  return "Tier 2 (small)"
        if equity < 5000:  return "Tier 3 (standard)"
        if equity < 20000: return "Tier 4 (growth)"
        return "Tier 5 (professional)"

    # ─────────────────────────────────────────────────────────────────────────
    # COMPOUNDING TRACKER
    # ─────────────────────────────────────────────────────────────────────────

    def record_trade(self, r_multiple: float, risk_pct: float,
                     symbol: str = "", grade: str = ""):
        """Record a completed trade and update compounding state."""
        # risk_pct is already a percentage value (e.g., 1.0 means 1%).
        # paper_trading.py uses: equity_change_pct = r_multiple * risk_pct (no ×100).
        # The previous ×100 multiplier inflated a 2R/1% win to 200% equity gain.
        equity_change_pct = r_multiple * risk_pct
        old_equity  = self._state["equity"]
        new_equity  = old_equity * (1 + equity_change_pct / 100)

        self._state["equity"]       = round(new_equity, 4)
        self._state["total_trades"] += 1
        if r_multiple > 0:
            self._state["wins"] += 1
        else:
            self._state["losses"] += 1

        self._state["equity_curve"].append({
            "equity": round(new_equity, 4),
            "ts":     datetime.now(timezone.utc).isoformat(),
            "r":      round(r_multiple, 3),
            "symbol": symbol,
            "grade":  grade,
        })
        if len(self._state["equity_curve"]) > 1000:
            self._state["equity_curve"] = self._state["equity_curve"][-1000:]

        # Peak tracking
        if new_equity > self._state["peak_equity"]:
            self._state["peak_equity"] = new_equity

        # Max drawdown
        dd = (self._state["peak_equity"] - new_equity) / self._state["peak_equity"] * 100
        if dd > self._state["max_drawdown_pct"]:
            self._state["max_drawdown_pct"] = round(dd, 2)

        # Milestone check
        milestone_hit = self._check_milestone(old_equity, new_equity)

        self._save_state()

        return {
            "old_equity":    round(old_equity, 4),
            "new_equity":    round(new_equity, 4),
            "change_pct":    round(equity_change_pct, 2),
            "milestone_hit": milestone_hit,
            "new_risk_tier": self._get_tier_label(new_equity),
            "next_milestone": self._next_milestone(new_equity),
        }

    def _check_milestone(self, old_eq: float, new_eq: float) -> Optional[float]:
        """Check if we crossed a milestone. Returns milestone value or None."""
        for m in MILESTONES:
            if old_eq < m <= new_eq:
                logger.info(f"[Compound] 🎯 MILESTONE HIT: ${m:,.0f}!")
                try:
                    from interfaces.telegram_interface import tg
                    tg.send(
                        f"🎯 <b>MILESTONE REACHED: ${m:,.0f}!</b>\n"
                        f"Started at: ${self._state['starting_equity']:,.2f}\n"
                        f"Current:    ${new_eq:,.2f}\n"
                        f"Growth:     {(new_eq/self._state['starting_equity']-1)*100:.1f}%\n"
                        f"Trades:     {self._state['total_trades']}\n"
                        f"Win rate:   {self.win_rate:.1f}%\n"
                        f"New risk tier: {self._get_tier_label(new_eq)}"
                    )
                except Exception:
                    pass
                return m
        return None

    def _next_milestone(self, equity: float) -> Optional[float]:
        for m in MILESTONES:
            if m > equity:
                return m
        return None

    @property
    def win_rate(self) -> float:
        t = self._state["total_trades"]
        return (self._state["wins"] / t * 100) if t > 0 else 0.0

    def get_status(self) -> dict:
        eq     = self._state["equity"]
        start  = self._state["starting_equity"]
        growth = (eq - start) / start * 100 if start > 0 else 0
        return {
            "equity":           round(eq, 2),
            "starting_equity":  start,
            "growth_pct":       round(growth, 2),
            "peak_equity":      round(self._state["peak_equity"], 2),
            "max_drawdown_pct": self._state["max_drawdown_pct"],
            "total_trades":     self._state["total_trades"],
            "wins":             self._state["wins"],
            "losses":           self._state["losses"],
            "win_rate":         round(self.win_rate, 1),
            "risk_pct":         round(self.get_risk_pct(eq) * 100, 2),
            "dollar_risk":      self.get_dollar_risk(eq),
            "risk_tier":        self._get_tier_label(eq),
            "next_milestone":   self._next_milestone(eq),
        }

    def project_growth(self, months: int = 12,
                       monthly_return_pct: float = 7.0) -> list:
        """Project equity growth with compounding."""
        eq = self._state["equity"]
        curve = []
        for m in range(months + 1):
            projected = eq * ((1 + monthly_return_pct/100) ** m)
            curve.append({
                "month":   m,
                "equity":  round(projected, 2),
                "risk_pct": round(self.get_risk_pct(projected) * 100, 2),
                "dollar_risk": round(self.get_dollar_risk(projected), 2),
                "tier":    self._get_tier_label(projected),
            })
        return curve

    # ─────────────────────────────────────────────────────────────────────────
    # STATE PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text())
        except Exception:
            pass
        return self._default_state()

    def _default_state(self) -> dict:
        try:
            from config.config import PAPER_TRADING
            start = float(PAPER_TRADING.get("starting_equity", 10000))
        except Exception:
            start = float(os.environ.get("ACCOUNT_SIZE", 10000))
        return {
            "equity":          start,
            "starting_equity": start,
            "peak_equity":     start,
            "total_trades":    0,
            "wins": 0, "losses": 0,
            "max_drawdown_pct": 0.0,
            "equity_curve":    [],
        }

    def _save_state(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.debug(f"[Compound] Save failed: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: Optional[CompoundingEngine] = None

def get_compound_engine() -> CompoundingEngine:
    global _instance
    if _instance is None:
        _instance = CompoundingEngine()
    return _instance


# ── Convenience: what can I trade with X dollars? ────────────────────────────
def what_can_i_trade(equity: float) -> str:
    """
    Returns a human-readable summary of what instruments
    are tradeable at a given equity level.
    """
    eng = CompoundingEngine()
    lines = [f"💰 Account: ${equity:,.2f} | Risk tier: {eng._get_tier_label(equity)}"]
    lines.append(f"📊 Risk per trade: ${eng.get_dollar_risk(equity):.2f} ({eng.get_risk_pct(equity)*100:.1f}%)")
    lines.append("─" * 40)

    # Test each instrument with a sample SL of 1% of price
    sample_entries = {
        "BTCUSDT":  95000, "ETHUSDT": 1800, "SOLUSDT": 145,
        "EURUSD=X": 1.085, "GBPUSD=X": 1.265,
        "XAUUSD=X": 3300,
    }
    tradeable = []
    blocked   = []

    for sym, price in sample_entries.items():
        sl = price * 0.985  # 1.5% SL
        result = eng.size_position(sym, equity, price, sl)
        if result["tradeable"]:
            tradeable.append(
                f"  ✅ {sym:<12} qty={result['qty']} | "
                f"risk=${result['dollar_risk']} | "
                f"notional=${result['notional']:,.0f}"
            )
        else:
            needed = result.get("min_needed_equity", 0)
            blocked.append(f"  ❌ {sym:<12} need ~${needed:.0f}")

    if tradeable:
        lines.append("TRADEABLE NOW:")
        lines.extend(tradeable)
    if blocked:
        lines.append("NOT YET (account too small):")
        lines.extend(blocked)

    proj = eng.project_growth(12, 7.0)
    lines.append("\n📈 PROJECTED GROWTH at 7%/month:")
    for p in [proj[1], proj[3], proj[6], proj[12]]:
        lines.append(
            f"  Month {p['month']:2d}: ${p['equity']:>10,.2f} "
            f"| risk/trade: ${p['dollar_risk']:.2f}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    for amount in [100, 500, 1000, 5000, 10000]:
        print(what_can_i_trade(amount))
        print()
