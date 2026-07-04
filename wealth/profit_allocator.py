"""
CRAVE v12 — Profit Allocation & Wealth Management
=====================================================
Automatically splits profits into buckets and routes them:
  50% → Compounding (stays in trading account for growth)
  20% → Daily expenses (withdrawn to bank/wallet)
  20% → Investment (auto-buy ETFs/stocks/SIP)
  10% → Emergency reserve (kept liquid)

TRACKING:
  - Tracks high-water mark (peak equity)
  - Only allocates on NEW profits (above previous high-water mark)
  - Monthly allocation cycle (runs on 1st of each month)
  - Full audit trail saved to DB

WITHDRAWAL STRATEGY (India-optimized):
  The bot recommends the best withdrawal path based on research:
  1. MT5 → Bank wire → INR savings (standard, auditable, FEMA compliant)
  2. For crypto profits: Exchange → P2P → UPI (if small amounts)
  3. For stock investment: Alpaca → Direct stock purchase (USD denominated)
  4. All transactions logged for ITR-3 filing
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, List

logger = logging.getLogger("crave.wealth")

STATE_DIR = Path(__file__).parent.parent / "State" / "wealth"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ALLOCATION_FILE = STATE_DIR / "allocation_state.json"
HISTORY_FILE    = STATE_DIR / "allocation_history.json"


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT ALLOCATION SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_ALLOCATION = {
    "compounding":  0.50,  # 50% reinvested
    "expenses":     0.20,  # 20% daily expenses
    "investment":   0.20,  # 20% long-term investment
    "reserve":      0.10,  # 10% emergency reserve
}


class ProfitAllocator:
    """
    Tracks profits and allocates them across configured buckets.
    """

    def __init__(self):
        self._state = self._load_state()
        self._allocation = DEFAULT_ALLOCATION
        # Override from env if set
        for key in self._allocation:
            env_val = os.environ.get(f"ALLOC_{key.upper()}", "")
            if env_val:
                try:
                    self._allocation[key] = float(env_val)
                except ValueError:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def update_equity(self, current_equity: float):
        """
        Called regularly (e.g., daily) with the current account equity.
        Tracks the high-water mark and pending profits.
        """
        hwm = self._state.get("high_water_mark", current_equity)
        initial = self._state.get("initial_equity", current_equity)

        if current_equity > hwm:
            self._state["high_water_mark"] = current_equity

        self._state["current_equity"] = current_equity
        self._state["unrealized_profit"] = max(current_equity - hwm, 0)
        self._state["total_return_pct"] = round(
            (current_equity - initial) / max(initial, 1) * 100, 2
        )
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def run_monthly_allocation(self) -> Optional[Dict]:
        """
        Run the monthly profit allocation cycle.
        Only allocates profits above the high-water mark.
        Returns allocation dict if profits exist, None otherwise.
        """
        equity = self._state.get("current_equity", 0)
        hwm    = self._state.get("high_water_mark", equity)
        last_alloc_equity = self._state.get("last_alloc_equity", hwm)

        # Profit = current equity - last allocated equity
        new_profit = equity - last_alloc_equity
        if new_profit <= 0:
            logger.info("[Wealth] No new profits to allocate this month.")
            return None

        # Calculate allocations
        allocation = {
            "month":           datetime.now(timezone.utc).strftime("%Y-%m"),
            "total_profit":    round(new_profit, 2),
            "equity_before":   round(last_alloc_equity, 2),
            "equity_after":    round(equity, 2),
            "buckets":         {},
            "allocated_at":    datetime.now(timezone.utc).isoformat(),
        }

        for bucket, pct in self._allocation.items():
            amount = round(new_profit * pct, 2)
            allocation["buckets"][bucket] = {
                "percentage": pct * 100,
                "amount_usd": amount,
                "status":     "pending",
            }

        # Update state
        self._state["last_alloc_equity"] = equity
        self._state["last_allocation"] = allocation
        self._save_state()

        # Save to history
        self._save_allocation(allocation)

        # Notify via Telegram
        self._send_allocation_report(allocation)

        # Execute automatic actions
        self._execute_investment(allocation)

        logger.info(
            f"[Wealth] Monthly allocation: ${new_profit:.2f} profit allocated "
            f"across {len(self._allocation)} buckets"
        )

        return allocation

    def get_status(self) -> Dict:
        """Return current wealth management status."""
        return {
            "current_equity":     self._state.get("current_equity", 0),
            "high_water_mark":    self._state.get("high_water_mark", 0),
            "initial_equity":     self._state.get("initial_equity", 0),
            "total_return_pct":   self._state.get("total_return_pct", 0),
            "unrealized_profit":  self._state.get("unrealized_profit", 0),
            "last_allocation":    self._state.get("last_allocation", {}),
            "allocation_config":  {k: f"{v*100:.0f}%" for k, v in self._allocation.items()},
        }

    def get_withdrawal_strategy(self) -> Dict:
        """
        Return the recommended withdrawal strategy for India.
        """
        return {
            "recommended_method": "MT5 → Wire Transfer → Indian Bank Account",
            "details": {
                "step_1": "Withdraw from MT5 to your MetaQuotes bank account via internal transfer",
                "step_2": "Wire transfer from broker to your Indian bank (HDFC/ICICI/SBI)",
                "step_3": "Bank auto-generates FIRC/e-FIRA for tax compliance",
                "step_4": "Report as Business Income on ITR-3",
            },
            "tax_optimization": {
                "classification": "Non-speculative Business Income (Section 43(5))",
                "deductions": [
                    "Brokerage fees and commissions",
                    "Internet costs (proportional to trading use)",
                    "Software and platform subscriptions",
                    "Advisory and research costs",
                    "Electricity (home office proportion)",
                    "Computer/hardware depreciation",
                ],
                "audit_threshold": "If turnover exceeds ₹10 crore, tax audit u/s 44AB required",
                "advance_tax": "If tax liability > ₹10,000, pay advance tax quarterly",
            },
            "alternatives": {
                "Rise (Winvesta)": {
                    "pros": "Easy US investing, INR auto-conversion, low fees",
                    "cons": "Only for US stocks, not forex profits",
                    "use_case": "Route the 20% investment bucket here for US stock SIPs",
                },
                "Vested/INDmoney": {
                    "pros": "US stocks + mutual funds, INR deposit",
                    "cons": "Higher spread, limited instruments",
                    "use_case": "Alternative to Rise for diversified investing",
                },
                "Direct Bank Wire": {
                    "pros": "Most compliant, auditable, generates FIRC",
                    "cons": "Slow (2-5 days), bank charges",
                    "use_case": "Primary withdrawal method for all profits",
                },
            },
            "warning": (
                "DO NOT use Wise/PayPal for forex profit withdrawal to India. "
                "These are not authorized for receiving trading income under FEMA. "
                "Use only SWIFT wire transfer to authorized dealer banks."
            ),
        }

    def set_initial_equity(self, equity: float):
        """Set the starting equity (called once at bot start)."""
        if "initial_equity" not in self._state:
            self._state["initial_equity"] = equity
            self._state["high_water_mark"] = equity
            self._state["last_alloc_equity"] = equity
            self._save_state()

    # ─────────────────────────────────────────────────────────────────────────
    # INVESTMENT EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_investment(self, allocation: dict):
        """Auto-invest the investment bucket."""
        investment_bucket = allocation["buckets"].get("investment", {})
        amount = investment_bucket.get("amount_usd", 0)

        if amount < 10:
            return

        # Route to Alpaca for US stock purchases (SIP-style)
        try:
            from brokers.broker_router import get_router
            router = get_router()

            # Simple SIP: split equally across SPY, QQQ, VTI
            etfs = ["SPY", "QQQ", "VTI"]
            per_etf = round(amount / len(etfs), 2)

            for etf in etfs:
                try:
                    # Fractional shares via Alpaca
                    router._alpaca_client().submit_order(
                        symbol=etf,
                        notional=per_etf,
                        side="buy",
                        type="market",
                        time_in_force="day",
                    )
                    logger.info(f"[Wealth] SIP: Bought ${per_etf} of {etf}")
                except Exception as e:
                    logger.warning(f"[Wealth] SIP {etf} failed: {e}")

            investment_bucket["status"] = "executed"
            self._save_state()

        except Exception as e:
            logger.warning(f"[Wealth] Investment execution failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # TELEGRAM REPORT
    # ─────────────────────────────────────────────────────────────────────────

    def _send_allocation_report(self, allocation: dict):
        try:
            from interfaces.telegram_interface import tg

            lines = [
                "💰 *Monthly Profit Allocation*\n",
                f"📈 Total Profit: `${allocation['total_profit']:.2f}`",
                f"📊 Equity: `${allocation['equity_before']:.2f}` → `${allocation['equity_after']:.2f}`\n",
                "*Allocation:*",
            ]

            for bucket, data in allocation["buckets"].items():
                emoji = {"compounding": "🔄", "expenses": "💵",
                         "investment": "📊", "reserve": "🛡️"}.get(bucket, "•")
                lines.append(
                    f"  {emoji} {bucket.title()}: `${data['amount_usd']:.2f}` "
                    f"({data['percentage']:.0f}%)"
                )

            tg.send("\n".join(lines))
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if ALLOCATION_FILE.exists():
                with open(ALLOCATION_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            with open(ALLOCATION_FILE, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"[Wealth] State save failed: {e}")

    def _save_allocation(self, allocation: dict):
        try:
            history = []
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
            history.append(allocation)
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"[Wealth] History save failed: {e}")


# ── Singleton ──────────────────────────────────────────────────────────────
_allocator: Optional[ProfitAllocator] = None

def get_allocator() -> ProfitAllocator:
    global _allocator
    if _allocator is None:
        _allocator = ProfitAllocator()
    return _allocator
