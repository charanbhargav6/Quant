"""
CRAVE v10.1 — Paper Trading Engine & Readiness Gate
=====================================================
FIXES vs v10.0 (audit-driven):

  🔧 FIX 5 — Lazy singleton: no more crash-on-import
             Old: paper_engine = PaperTradingEngine()  ← runs on import,
             crashes entire bot if Config fails.
             New: get_paper_engine() factory function, instance created
             only when first called. All imports use get_paper_engine().

  🔧 FIX M5 — Sharpe now uses actual risk_pct per trade, not fixed 1%
             Old: eq_returns = [r * 0.01 for r in r_vals]
             New: stores (r_multiple, risk_pct) tuples, so a B trade at
             0.25% risk and an A+ trade at 2% risk contribute correctly
             to Sharpe. Readiness gate was passing/failing incorrectly.

  🔧 FIX M4 — Slippage logic consolidated here (single source of truth)
             simulate_fill() is asset-class-aware:
               forex: 2 pips | gold: 3 pips | crypto: 0.05% | default: 0.02%
             trading_loop._paper_execute() now calls this instead of its
             own pip_size * 2 formula.
"""

import json
import logging
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("crave.paper")


class PaperTradingEngine:

    def __init__(self):
        from config.config import PAPER_TRADING, STATE_DIR
        self._cfg        = PAPER_TRADING
        self._state_file = STATE_DIR / "crave_paper_state.json"
        self._state      = self._load_state()
        logger.info(
            f"[Paper] Engine loaded. "
            f"Equity=${self._state['equity']:,.2f} | "
            f"Trades={self._state['total_trades']}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # STATE
    # ─────────────────────────────────────────────────────────────────────────

    def _default_state(self) -> dict:
        from config.config import PAPER_TRADING
        return {
            "equity":           float(PAPER_TRADING.get("starting_equity", 10000)),
            "peak_equity":      float(PAPER_TRADING.get("starting_equity", 10000)),
            "starting_equity":  float(PAPER_TRADING.get("starting_equity", 10000)),
            "total_trades":     0,
            "wins":             0,
            "losses":           0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            # FIX M5: store tuples {r, risk} not bare floats
            # Each entry: {"r": float, "risk": float}
            "r_entries":        [],
            # Keep plain r_multiples list for backward compat
            "r_multiples":      [],
            "equity_curve":     [],
            "last_updated":     datetime.now(timezone.utc).isoformat(),
        }

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                with open(self._state_file) as f:
                    saved = json.load(f)
                state = self._default_state()
                state.update(saved)
                # Migrate old states that had r_multiples but not r_entries
                if not state.get("r_entries") and state.get("r_multiples"):
                    state["r_entries"] = [
                        {"r": r, "risk": 1.0}
                        for r in state["r_multiples"]
                    ]
                return state
            except Exception as e:
                logger.warning(f"[Paper] State load failed: {e}. Fresh start.")
        return self._default_state()

    def _save_state(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"[Paper] State save failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # FILL SIMULATION (FIX M4 — single source of truth for slippage)
    # ─────────────────────────────────────────────────────────────────────────

    def simulate_fill(self, validated: dict, live_price: float) -> dict:
        """
        Asset-class-aware slippage simulation.
        This is the ONLY place in the codebase that calculates paper slippage.
        trading_loop._paper_execute() delegates here entirely.

        v10.4: If order_type == "limit", simulate a perfect fill at the
        limit price with zero slippage (institutional-grade OB entry).

        Slippage model (for market orders):
          Forex majors:  2 pips    (tight spread, high liquidity)
          Gold/Silver:   3 pips    (moderate spread)
          Crypto:        0.05%     (wider spread, volatile)
          Stocks:        0.02%     (moderate)
          Default:       0.02%
        """
        # Limit orders: perfect fill at the limit price, zero slippage
        order_type = validated.get("order_type", "market")
        limit_px   = validated.get("limit_price")
        if order_type == "limit" and limit_px:
            return {
                "fill_price":      round(limit_px, 5),
                "slippage":        0.0,
                "spread_cost_pct": 0.0,
                "simulated":       True,
                "order_type":      "limit",
            }

        from config.config import get_instrument, get_asset_class
        symbol    = validated["symbol"]
        direction = validated["direction"]
        asset     = get_asset_class(symbol)
        inst      = get_instrument(symbol)
        pip_size  = inst.get("pip_size", 0.0001)

        if asset == "forex":
            slippage = pip_size * 2
        elif asset in ("gold", "silver"):
            slippage = pip_size * 3
        elif asset == "crypto":
            slippage = live_price * 0.0005
        elif asset == "stocks":
            slippage = live_price * 0.0002
        else:
            slippage = live_price * 0.0002

        if direction in ("buy", "long"):
            fill_price = round(live_price + slippage, 5)
        else:
            fill_price = round(live_price - slippage, 5)

        spread_cost_pct = (pip_size * 0.5) / live_price * 100

        return {
            "fill_price":      fill_price,
            "slippage":        round(slippage, 5),
            "spread_cost_pct": round(spread_cost_pct, 6),
            "simulated":       True,
            "order_type":      "market",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # TRADE RESULT — FIX M5: stores (r, risk) pair
    # ─────────────────────────────────────────────────────────────────────────

    def record_trade_result(self, r_multiple: float, risk_pct: float = 1.0,
                             symbol: str = "", grade: str = ""):
        """
        Update paper equity after a trade closes.
        v10.5: Also updates compounding engine for milestone tracking + tier upgrades.
        """
        equity_change_pct = r_multiple * risk_pct
        old_equity        = self._state["equity"]
        new_equity        = old_equity * (1 + equity_change_pct / 100)

        self._state["equity"]        = round(new_equity, 2)
        self._state["total_trades"] += 1

        if r_multiple > 0:
            self._state["wins"] += 1
        else:
            self._state["losses"] += 1

        if new_equity > self._state["peak_equity"]:
            self._state["peak_equity"] = new_equity

        dd = (self._state["peak_equity"] - new_equity) / self._state["peak_equity"] * 100
        if dd > self._state["max_drawdown_pct"]:
            self._state["max_drawdown_pct"] = round(dd, 3)

        # FIX M5: store (r, risk) pair, not bare float
        self._state["r_entries"].append({
            "r":    round(r_multiple, 3),
            "risk": round(risk_pct, 4),
        })
        self._state["r_multiples"].append(round(r_multiple, 3))
        self._state["equity_curve"].append(round(new_equity, 2))

        # Keep last 500
        if len(self._state["r_entries"]) > 500:
            self._state["r_entries"]    = self._state["r_entries"][-500:]
            self._state["r_multiples"]  = self._state["r_multiples"][-500:]
            self._state["equity_curve"] = self._state["equity_curve"][-500:]

        start = self._state["starting_equity"]
        self._state["total_return_pct"] = round(
            (new_equity - start) / start * 100, 3
        )
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

        # ── v10.5: Feed compound engine for milestone + tier tracking ─────
        try:
            from engines.compounding_engine import get_compound_engine
            comp_result = get_compound_engine().record_trade(
                r_multiple, risk_pct, symbol=symbol, grade=grade
            )
            if comp_result.get("milestone_hit"):
                logger.info(
                    f"[Paper] 🎯 MILESTONE HIT: ${comp_result['milestone_hit']:,.0f}!"
                )
            if comp_result.get("new_risk_tier") != self._get_current_tier(old_equity):
                logger.info(
                    f"[Paper] 📈 TIER UPGRADE → {comp_result['new_risk_tier']} "
                    f"(equity ${new_equity:,.2f})"
                )
        except Exception as e:
            logger.debug(f"[Paper] Compound engine update failed (non-fatal): {e}")

        logger.info(
            f"[Paper] Trade: {r_multiple:+.2f}R @ {risk_pct:.2f}% risk | "
            f"Equity ${old_equity:,.2f} → ${new_equity:,.2f} | "
            f"Total {self._state['total_return_pct']:+.2f}%"
        )

    def _get_current_tier(self, equity: float) -> str:
        try:
            from engines.compounding_engine import CompoundingEngine
            return CompoundingEngine()._get_tier_label(equity)
        except Exception:
            return ""

    # ─────────────────────────────────────────────────────────────────────────
    # STATS — FIX M5: Sharpe uses actual equity returns
    # ─────────────────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        return self._state["equity"]

    def get_stats(self) -> dict:
        total    = self._state["total_trades"]
        wins     = self._state["wins"]
        r_vals   = self._state["r_multiples"]
        r_entries = self._state.get("r_entries", [])

        if total == 0:
            return {"total_trades": 0,
                    "message": "No paper trades yet."}

        win_rate      = wins / total * 100
        expectancy_r  = float(np.mean(r_vals))
        gross_profit  = sum(r for r in r_vals if r > 0)
        gross_loss    = abs(sum(r for r in r_vals if r < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0

        # FIX M5: equity returns use actual risk_pct per trade
        if r_entries:
            eq_returns = [
                entry["r"] * entry["risk"] / 100
                for entry in r_entries
            ]
        else:
            # Fallback: assume 1% risk for old records
            eq_returns = [r * 0.01 for r in r_vals]

        std    = np.std(eq_returns) if len(eq_returns) > 1 else 0
        sharpe = (np.mean(eq_returns) / std * np.sqrt(252)) if std > 0 else 0.0

        return {
            "total_trades":       total,
            "wins":               wins,
            "losses":             self._state["losses"],
            "win_rate":           f"{win_rate:.1f}%",
            "win_rate_float":     win_rate,
            "expectancy_r":       f"{expectancy_r:.3f}R",
            "expectancy_float":   expectancy_r,
            "profit_factor":      f"{profit_factor:.2f}",
            "profit_factor_float": profit_factor,
            "sharpe_ratio":       f"{sharpe:.2f}",
            "sharpe_float":       sharpe,
            "total_return":       f"{self._state['total_return_pct']:+.2f}%",
            "max_drawdown":       f"-{self._state['max_drawdown_pct']:.2f}%",
            "max_dd_float":       self._state["max_drawdown_pct"],
            "starting_equity":    f"${self._state['starting_equity']:,.2f}",
            "current_equity":     f"${self._state['equity']:,.2f}",
            "best_trade":         f"+{max(r_vals):.1f}R" if r_vals else "N/A",
            "worst_trade":        f"{min(r_vals):.1f}R" if r_vals else "N/A",
            "last_updated":       self._state["last_updated"],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # READINESS GATE — unchanged logic, uses corrected stats
    # ─────────────────────────────────────────────────────────────────────────

    def check_readiness(self, backtest_win_rate: float = None,
                         backtest_max_dd: float = None) -> Tuple[bool, str]:
        from config.config import PAPER_TRADING
        cfg   = PAPER_TRADING
        stats = self.get_stats()
        checks = []
        passed = failed = 0

        def chk(name, ok, detail):
            nonlocal passed, failed
            checks.append((name, ok, detail))
            passed += int(ok is True)
            failed += int(ok is False)

        total    = stats.get("total_trades", 0)
        min_t    = cfg.get("min_trades_for_live", 30)
        actual_wr = stats.get("win_rate_float", 0)
        actual_dd = stats.get("max_dd_float", 0)
        sharpe    = stats.get("sharpe_float", 0)

        chk("Min paper trades",   total >= min_t,   f"{total}/{min_t}")
        chk("Win rate >= 50%",    actual_wr >= 50.0, f"{actual_wr:.1f}%")

        if backtest_win_rate is not None:
            dev = abs(actual_wr - backtest_win_rate)
            chk("WR within 5% of backtest", dev <= 5.0,
                f"Paper={actual_wr:.1f}% BT={backtest_win_rate:.1f}%")
        else:
            checks.append(("WR vs backtest", None, "Skipped — no BT reference"))

        if backtest_max_dd is not None:
            dd_dev = abs(actual_dd - backtest_max_dd)
            chk("MaxDD within 2% of backtest", dd_dev <= 2.0,
                f"Paper={actual_dd:.2f}% BT={backtest_max_dd:.2f}%")
        else:
            chk("Max drawdown < 10%", actual_dd < 10.0, f"{actual_dd:.2f}%")

        chk("Sharpe ratio >= 0.8", sharpe >= 0.8, f"{sharpe:.2f}")

        try:
            from core.streak_state import streak
            cb = streak._state.get("circuit_breaker_active", False)
            ld = streak._state.get("consecutive_loss_days", 0)
            chk("No active circuit breaker", not cb and ld < 2,
                f"CB={'ACTIVE' if cb else 'off'} LossDays={ld}")
        except Exception:
            checks.append(("Circuit breaker", None, "Could not check"))

        import os
        has_keys = bool(os.environ.get("BINANCE_API_KEY") or
                        os.environ.get("ALPACA_API_KEY"))
        chk("Exchange API keys set", has_keys,
            "✅ Found" if has_keys else "❌ Missing — add to .env")

        all_passed = failed == 0 and all(c[1] is not False for c in checks)

        lines = [
            "╔══════════════════════════════════════════╗",
            "║    CRAVE v10.1 — READINESS GATE REPORT   ║",
            "╚══════════════════════════════════════════╝",
            "",
            f"Paper Performance (Sharpe uses real risk_pct per trade):",
            f"  Trades    : {total}",
            f"  Win Rate  : {stats.get('win_rate')}",
            f"  Expectancy: {stats.get('expectancy_r')}",
            f"  Sharpe    : {stats.get('sharpe_ratio')}",
            f"  Max DD    : {stats.get('max_drawdown')}",
            f"  Total Rtn : {stats.get('total_return')}",
            "",
            "Gate Checks:",
        ]

        for name, ok, detail in checks:
            sym = "✅" if ok is True else "❌" if ok is False else "⚠️"
            lines.append(f"  {sym} {name}: {detail}")

        lines += ["", f"Passed: {passed} | Failed: {failed}", ""]

        if all_passed:
            lines += [
                "══════════════════════════════════════════",
                "✅ READINESS GATE: PASSED",
                "   1. Add real API keys to .env",
                "   2. Set TRADING_MODE=live in .env",
                "   3. python run_bot.py --live",
                "══════════════════════════════════════════",
            ]
        else:
            lines += [
                "══════════════════════════════════════════",
                "❌ READINESS GATE: FAILED",
                f"   {failed} check(s) not met. Fix ❌ items above.",
                "══════════════════════════════════════════",
            ]

        return all_passed, "\n".join(lines)

    def get_status_message(self) -> str:
        from config.config import PAPER_TRADING
        stats     = self.get_stats()
        min_t     = PAPER_TRADING.get("min_trades_for_live", 30)
        total     = stats.get("total_trades", 0)
        remaining = max(0, min_t - total)
        ready     = (
            "✅ Ready! Run: python run_bot.py --readiness"
            if remaining == 0
            else f"⏳ {remaining} more trades needed"
        )
        if total == 0:
            return (
                "📄 <b>PAPER TRADING</b>\n"
                "No trades yet.\n"
                "Bot fires signals during kill zones (07:00 + 12:30 UTC)."
            )
        return (
            f"📄 <b>PAPER TRADING STATUS</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Equity    : {stats.get('current_equity')}\n"
            f"Return    : {stats.get('total_return')}\n"
            f"Trades    : {total} ({remaining} to min)\n"
            f"Win Rate  : {stats.get('win_rate')}\n"
            f"Expectancy: {stats.get('expectancy_r')}\n"
            f"Sharpe    : {stats.get('sharpe_ratio')}\n"
            f"Max DD    : {stats.get('max_drawdown')}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{ready}"
        )

    def reset(self):
        self._state = self._default_state()
        self._save_state()
        logger.info("[Paper] State reset.")


# ─────────────────────────────────────────────────────────────────────────────
# FIX 5 — Lazy singleton (no crash-on-import)
# ─────────────────────────────────────────────────────────────────────────────
# Old code ran PaperTradingEngine() at module level.
# Any file importing paper_trading would crash if Config wasn't ready.
# Now: get_paper_engine() is called on first use, not on import.
#
# Usage everywhere:
#   from core.paper_trading import get_paper_engine
#   pe = get_paper_engine()
#   pe.record_trade_result(r, risk)

_paper_engine_instance: Optional[PaperTradingEngine] = None

def get_paper_engine() -> PaperTradingEngine:
    global _paper_engine_instance
    if _paper_engine_instance is None:
        _paper_engine_instance = PaperTradingEngine()
    return _paper_engine_instance


# Backward compat alias — use get_paper_engine() in new code
paper_engine = get_paper_engine
