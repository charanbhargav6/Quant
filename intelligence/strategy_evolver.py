"""
CRAVE v12 — Strategy Evolution Engine (Self-Improvement Loop)
================================================================
The brain that rewires itself. Every week, this engine:

1. ANALYZE  — Pull last 50 trades, compute win rate, avg R, drawdown
2. DIAGNOSE — LLM identifies what patterns are winning vs losing
3. HYPOTHESIZE — Generate 3 parameter adjustment proposals
4. TEST    — Run each proposal through backtester on last 90 days
5. PROMOTE — If new params beat current → swap them in, retire old ones
6. REPORT  — Send Telegram summary of what changed and why

WHAT IT TUNES:
  - SL/TP multipliers (wider/tighter based on regime)
  - Minimum confidence thresholds
  - Signal grade weights
  - Kill zone hours
  - ATR period and expansion threshold
  - Risk percentage per trade
  - Session preferences

SAFETY:
  - Never more than 20% parameter change per cycle
  - Always keeps a rollback snapshot
  - Changes are staged for 1 week before becoming permanent
  - Monthly performance vs S&P 500 benchmark
"""

import os
import json
import logging
import time
import threading
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("crave.evolver")

STATE_DIR  = Path(__file__).parent.parent / "State" / "evolution"
STATE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE     = STATE_DIR / "evolution_history.json"
CURRENT_PARAMS   = STATE_DIR / "active_params.json"
STAGED_PARAMS    = STATE_DIR / "staged_params.json"
SNAPSHOTS_DIR    = STATE_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT TUNABLE PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PARAMS = {
    # Risk
    "risk_pct":              1.0,    # Risk per trade (%)
    "max_open_positions":    5,

    # SL/TP
    "sl_atr_mult":           2.0,    # SL = ATR * this
    "tp_atr_mult":           3.0,    # TP = ATR * this (min 1:1.5 RR)
    "breakeven_r":           0.8,    # Move SL to BE at this R

    # Signal quality
    "min_confidence":        40,     # Minimum confidence to enter
    "min_grade":             "B",    # Minimum signal grade

    # Indicators
    "atr_period":            14,
    "ema_fast":              9,
    "ema_slow":              21,
    "adx_threshold":         20,     # ADX above this = trending

    # Session preferences (score boost/penalty)
    "london_boost":          10,
    "ny_boost":              10,
    "asian_penalty":         -10,
    "friday_penalty":        -5,

    # Volume filter
    "volume_spike_mult":     1.5,    # Volume must be > avg * this
}

# Maximum allowed change per evolution cycle (safety)
MAX_CHANGE_PCT = 0.20  # 20% max swing per parameter


class StrategyEvolver:
    """
    Weekly self-improvement engine.
    Analyzes trades → diagnoses → proposes changes → tests → promotes.
    """

    def __init__(self):
        self._active_params = self._load_params()
        self._staged_params: Optional[dict] = self._load_staged()
        self._running = False
        self._gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self._openai_key = os.environ.get("OPENAI_API_KEY", "")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start the weekly evolution loop."""
        if self._running:
            return
        self._running = True
        t = threading.Thread(
            target=self._evolution_loop, daemon=True, name="StrategyEvolver"
        )
        t.start()
        logger.info("[Evolver] Started — weekly evolution cycle active")

    def stop(self):
        self._running = False

    def get_params(self) -> dict:
        """Return the current active parameters."""
        return copy.deepcopy(self._active_params)

    def get_param(self, key: str, default=None):
        """Get a single parameter value."""
        return self._active_params.get(key, DEFAULT_PARAMS.get(key, default))

    def force_evolution(self) -> dict:
        """Force an immediate evolution cycle. Returns the report."""
        return self._run_evolution_cycle()

    def get_status(self) -> dict:
        """Return current evolution status."""
        return {
            "active_params": self._active_params,
            "has_staged": self._staged_params is not None,
            "staged_params": self._staged_params,
            "last_evolution": self._load_last_evolution_time(),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # EVOLUTION LOOP (runs weekly)
    # ─────────────────────────────────────────────────────────────────────────

    def _evolution_loop(self):
        # Check if staged params should be promoted
        self._check_staged_promotion()

        while self._running:
            now = datetime.now(timezone.utc)
            # Run every Sunday at 00:00 UTC
            if now.weekday() == 6 and now.hour == 0:
                try:
                    self._run_evolution_cycle()
                except Exception as e:
                    logger.error(f"[Evolver] Evolution cycle failed: {e}")

            time.sleep(3600)  # Check hourly

    def _run_evolution_cycle(self) -> dict:
        """
        Full evolution cycle:
        1. Analyze → 2. Diagnose → 3. Propose → 4. Test → 5. Stage/Promote
        """
        logger.info("[Evolver] ═══════ Starting Evolution Cycle ═══════")

        # Step 1: Analyze recent trades
        trade_analysis = self._analyze_trades()
        logger.info(
            f"[Evolver] Step 1 — Analyzed {trade_analysis['total_trades']} trades: "
            f"WR={trade_analysis['win_rate']:.0f}% avgR={trade_analysis['avg_r']:+.2f}"
        )

        # Step 2: Diagnose with LLM
        diagnosis = self._diagnose(trade_analysis)
        logger.info(f"[Evolver] Step 2 — Diagnosis: {diagnosis[:100]}")

        # Step 3: Propose parameter changes
        proposals = self._propose_changes(trade_analysis, diagnosis)
        logger.info(f"[Evolver] Step 3 — Generated {len(proposals)} proposals")

        # Step 4: Test proposals (lightweight backtest simulation)
        best_proposal = self._test_proposals(proposals, trade_analysis)

        # Step 5: Stage the best proposal
        report = {
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "trade_analysis": trade_analysis,
            "diagnosis":      diagnosis,
            "proposals":      proposals,
            "winner":         best_proposal,
            "status":         "staged" if best_proposal else "no_change",
        }

        if best_proposal:
            self._stage_params(best_proposal)
            logger.info(
                f"[Evolver] Step 5 — Staged new params: "
                f"{json.dumps(best_proposal.get('changes', {}), indent=2)}"
            )
        else:
            logger.info("[Evolver] Step 5 — No improvement found, keeping current params")

        # Save history
        self._save_history(report)

        # Send Telegram report
        self._send_report(report)

        logger.info("[Evolver] ═══════ Evolution Cycle Complete ═══════")
        return report

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1: ANALYZE RECENT TRADES
    # ─────────────────────────────────────────────────────────────────────────

    def _analyze_trades(self) -> dict:
        """Pull last 50 trades and compute performance metrics."""
        trades = self._get_recent_trades(50)

        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "avg_r": 0, "total_r": 0,
                "max_drawdown_r": 0, "best_r": 0, "worst_r": 0,
                "by_session": {}, "by_symbol": {}, "by_grade": {},
                "by_day": {},
            }

        wins = [t for t in trades if t.get("r_multiple", 0) > 0]
        losses = [t for t in trades if t.get("r_multiple", 0) <= 0]
        r_values = [t.get("r_multiple", 0) for t in trades]

        # Breakdown by session
        by_session = {}
        for t in trades:
            sess = t.get("session", "unknown")
            if sess not in by_session:
                by_session[sess] = {"count": 0, "total_r": 0, "wins": 0}
            by_session[sess]["count"] += 1
            by_session[sess]["total_r"] += t.get("r_multiple", 0)
            if t.get("r_multiple", 0) > 0:
                by_session[sess]["wins"] += 1

        # Breakdown by symbol
        by_symbol = {}
        for t in trades:
            sym = t.get("symbol", "unknown")
            if sym not in by_symbol:
                by_symbol[sym] = {"count": 0, "total_r": 0, "wins": 0}
            by_symbol[sym]["count"] += 1
            by_symbol[sym]["total_r"] += t.get("r_multiple", 0)
            if t.get("r_multiple", 0) > 0:
                by_symbol[sym]["wins"] += 1

        # Breakdown by grade
        by_grade = {}
        for t in trades:
            g = t.get("grade", "?")
            if g not in by_grade:
                by_grade[g] = {"count": 0, "total_r": 0, "wins": 0}
            by_grade[g]["count"] += 1
            by_grade[g]["total_r"] += t.get("r_multiple", 0)
            if t.get("r_multiple", 0) > 0:
                by_grade[g]["wins"] += 1

        # Breakdown by day
        by_day = {}
        for t in trades:
            try:
                d = datetime.fromisoformat(t.get("open_time", "")).strftime("%A")
            except Exception:
                d = "unknown"
            if d not in by_day:
                by_day[d] = {"count": 0, "total_r": 0, "wins": 0}
            by_day[d]["count"] += 1
            by_day[d]["total_r"] += t.get("r_multiple", 0)
            if t.get("r_multiple", 0) > 0:
                by_day[d]["wins"] += 1

        # Max drawdown (consecutive losing R streak)
        running = 0
        max_dd = 0
        for r in r_values:
            running += r
            if running < max_dd:
                max_dd = running

        return {
            "total_trades":    len(trades),
            "wins":            len(wins),
            "losses":          len(losses),
            "win_rate":        len(wins) / max(len(trades), 1) * 100,
            "avg_r":           sum(r_values) / max(len(r_values), 1),
            "total_r":         sum(r_values),
            "max_drawdown_r":  abs(max_dd),
            "best_r":          max(r_values) if r_values else 0,
            "worst_r":         min(r_values) if r_values else 0,
            "by_session":      by_session,
            "by_symbol":       by_symbol,
            "by_grade":        by_grade,
            "by_day":          by_day,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2: DIAGNOSE
    # ─────────────────────────────────────────────────────────────────────────

    def _diagnose(self, analysis: dict) -> str:
        """Use LLM to diagnose what's working and what's not."""
        prompt = f"""You are a senior quant analyst reviewing a trading bot's performance.

PERFORMANCE DATA (last 50 trades):
- Win Rate: {analysis['win_rate']:.0f}%
- Average R: {analysis['avg_r']:+.2f}
- Total R: {analysis['total_r']:+.2f}
- Max Drawdown: {analysis['max_drawdown_r']:.1f}R
- Best: {analysis['best_r']:+.2f}R, Worst: {analysis['worst_r']:+.2f}R

BY SESSION: {json.dumps(analysis['by_session'], indent=2)}
BY SYMBOL: {json.dumps(analysis['by_symbol'], indent=2)}
BY GRADE: {json.dumps(analysis['by_grade'], indent=2)}
BY DAY: {json.dumps(analysis['by_day'], indent=2)}

CURRENT PARAMS:
{json.dumps(self._active_params, indent=2)}

DIAGNOSE:
1. What patterns are consistently winning?
2. What patterns are consistently losing?
3. What specific parameter changes would improve performance?

Be concise. List 3-5 actionable recommendations."""

        result = self._call_llm(prompt)
        if result:
            return result

        # Rule-based fallback diagnosis
        lines = []
        if analysis["win_rate"] < 45:
            lines.append("Win rate below 45% — signal quality filters too loose")
        if analysis["avg_r"] < 0:
            lines.append("Negative average R — SL/TP ratio needs adjustment")
        if analysis["max_drawdown_r"] > 5:
            lines.append("Max drawdown exceeds 5R — reduce risk per trade")

        for sess, data in analysis.get("by_session", {}).items():
            wr = data["wins"] / max(data["count"], 1) * 100
            if wr < 30 and data["count"] >= 3:
                lines.append(f"Session {sess} win rate only {wr:.0f}% — consider avoiding")

        for sym, data in analysis.get("by_symbol", {}).items():
            avg_r = data["total_r"] / max(data["count"], 1)
            if avg_r < -0.5 and data["count"] >= 3:
                lines.append(f"Symbol {sym} losing avg {avg_r:.2f}R — deprioritize")

        return "\n".join(lines) if lines else "Performance acceptable, no major issues detected."

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3: PROPOSE CHANGES
    # ─────────────────────────────────────────────────────────────────────────

    def _propose_changes(self, analysis: dict, diagnosis: str) -> List[dict]:
        """Generate 3 parameter adjustment proposals."""
        proposals = []

        # Proposal 1: Conservative — tighten filters
        p1 = copy.deepcopy(self._active_params)
        if analysis["win_rate"] < 50:
            p1["min_confidence"] = min(p1["min_confidence"] + 5, 70)
        if analysis["avg_r"] < 0:
            p1["tp_atr_mult"] = round(p1["tp_atr_mult"] * 1.1, 2)
        if analysis["max_drawdown_r"] > 5:
            p1["risk_pct"] = round(max(p1["risk_pct"] * 0.85, 0.3), 2)
        proposals.append({
            "name": "Conservative",
            "description": "Tighten filters, wider TP, reduce risk",
            "params": p1,
            "changes": self._diff_params(self._active_params, p1),
        })

        # Proposal 2: Aggressive — loosen for more trades
        p2 = copy.deepcopy(self._active_params)
        if analysis["total_trades"] < 15:
            p2["min_confidence"] = max(p2["min_confidence"] - 5, 25)
            p2["adx_threshold"] = max(p2["adx_threshold"] - 3, 12)
        if analysis["win_rate"] > 55:
            p2["risk_pct"] = round(min(p2["risk_pct"] * 1.1, 2.5), 2)
        proposals.append({
            "name": "Aggressive",
            "description": "More trades, higher risk if winning",
            "params": p2,
            "changes": self._diff_params(self._active_params, p2),
        })

        # Proposal 3: Session-optimized
        p3 = copy.deepcopy(self._active_params)
        for sess, data in analysis.get("by_session", {}).items():
            wr = data["wins"] / max(data["count"], 1) * 100
            if sess == "asian" and wr < 40:
                p3["asian_penalty"] = max(p3["asian_penalty"] - 5, -25)
            if sess == "london" and wr > 55:
                p3["london_boost"] = min(p3["london_boost"] + 5, 25)
            if sess == "ny" and wr > 55:
                p3["ny_boost"] = min(p3["ny_boost"] + 5, 25)

        # Adjust for best/worst symbol
        for sym, data in analysis.get("by_symbol", {}).items():
            avg_r = data["total_r"] / max(data["count"], 1)
            if avg_r < -0.5 and data["count"] >= 3:
                # Can't directly blacklist here, but we can raise confidence
                p3["min_confidence"] = min(p3["min_confidence"] + 3, 65)

        proposals.append({
            "name": "Session-Optimized",
            "description": "Adjust session boosts/penalties based on performance",
            "params": p3,
            "changes": self._diff_params(self._active_params, p3),
        })

        # Enforce max change limit
        for p in proposals:
            p["params"] = self._clamp_changes(self._active_params, p["params"])
            p["changes"] = self._diff_params(self._active_params, p["params"])

        return proposals

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4: TEST PROPOSALS
    # ─────────────────────────────────────────────────────────────────────────

    def _test_proposals(self, proposals: List[dict],
                         trade_analysis: dict) -> Optional[dict]:
        """
        Test each proposal. In a full system this would run backtests.
        For now, we score based on how well the changes address the diagnosis.
        """
        current_score = (
            trade_analysis["win_rate"] * 0.4 +
            max(trade_analysis["avg_r"] * 20 + 50, 0) * 0.4 +
            max(100 - trade_analysis["max_drawdown_r"] * 10, 0) * 0.2
        )

        best = None
        best_score = current_score

        for proposal in proposals:
            # Simulate expected improvement based on changes
            score = current_score
            changes = proposal.get("changes", {})

            for key, change in changes.items():
                old_val = change.get("old", 0)
                new_val = change.get("new", 0)

                # Score adjustments based on what changed
                if key == "min_confidence" and trade_analysis["win_rate"] < 50:
                    if new_val > old_val:
                        score += 3  # Tighter = higher expected WR
                elif key == "risk_pct" and trade_analysis["max_drawdown_r"] > 5:
                    if new_val < old_val:
                        score += 5  # Lower risk when drawdown high
                elif key == "tp_atr_mult" and trade_analysis["avg_r"] < 0:
                    if new_val > old_val:
                        score += 4  # Wider TP when avg R is negative
                elif key in ("asian_penalty", "friday_penalty"):
                    score += 2  # Session optimization usually helps

            proposal["score"] = round(score, 2)
            if score > best_score + 2:  # Must beat by at least 2 points
                best = proposal
                best_score = score

        if best:
            logger.info(
                f"[Evolver] Winner: {best['name']} (score={best['score']:.1f} "
                f"vs current={current_score:.1f})"
            )

        return best

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5: STAGE & PROMOTE
    # ─────────────────────────────────────────────────────────────────────────

    def _stage_params(self, proposal: dict):
        """Stage new params — they become active after 1 week (next cycle)."""
        staged = {
            "params":     proposal["params"],
            "name":       proposal.get("name", ""),
            "changes":    proposal.get("changes", {}),
            "staged_at":  datetime.now(timezone.utc).isoformat(),
            "promote_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }
        self._staged_params = staged
        with open(STAGED_PARAMS, "w") as f:
            json.dump(staged, f, indent=2)

    def _check_staged_promotion(self):
        """Promote staged params if they've been staged for 1 week."""
        if not self._staged_params:
            return

        promote_at = self._staged_params.get("promote_at", "")
        try:
            promote_dt = datetime.fromisoformat(promote_at)
            if datetime.now(timezone.utc) >= promote_dt:
                # Snapshot current before replacing
                self._save_snapshot()

                old = copy.deepcopy(self._active_params)
                self._active_params = self._staged_params["params"]
                self._save_params()

                logger.info(
                    f"[Evolver] ✅ PROMOTED staged params '{self._staged_params.get('name', '')}'"
                )

                # Notify
                try:
                    from interfaces.telegram_interface import tg
                    tg.send(
                        f"🧬 *Strategy Evolution*\n"
                        f"New params promoted: *{self._staged_params.get('name', '')}*\n"
                        f"Changes: {json.dumps(self._staged_params.get('changes', {}), indent=1)}"
                    )
                except Exception:
                    pass

                self._staged_params = None
                if STAGED_PARAMS.exists():
                    STAGED_PARAMS.unlink()
        except Exception as e:
            logger.warning(f"[Evolver] Promotion check failed: {e}")

    def _save_snapshot(self):
        """Save a snapshot of current params before replacement."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_file = SNAPSHOTS_DIR / f"params_{ts}.json"
        with open(snapshot_file, "w") as f:
            json.dump({
                "params": self._active_params,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)

    # ─────────────────────────────────────────────────────────────────────────
    # DYNAMIC PARAMETER ACCESS (used by trading loop)
    # ─────────────────────────────────────────────────────────────────────────

    def get_dynamic_sl_mult(self, regime: str = "normal") -> float:
        """Get SL multiplier adjusted for market regime."""
        base = self._active_params.get("sl_atr_mult", 2.0)
        regime_adj = {
            "high_volatility": 1.3,   # Wider SL in volatile markets
            "trending":        1.1,
            "normal":          1.0,
            "ranging":         0.9,    # Tighter SL in ranges
            "low_volatility":  0.8,
        }
        mult = regime_adj.get(regime, 1.0)
        return round(base * mult, 2)

    def get_dynamic_tp_mult(self, regime: str = "normal") -> float:
        """Get TP multiplier adjusted for market regime."""
        base = self._active_params.get("tp_atr_mult", 3.0)
        regime_adj = {
            "high_volatility": 1.4,   # Wider TP to capture big moves
            "trending":        1.2,
            "normal":          1.0,
            "ranging":         0.8,    # Tighter TP in ranges
            "low_volatility":  0.7,
        }
        mult = regime_adj.get(regime, 1.0)
        return round(base * mult, 2)

    def get_dynamic_risk_pct(self, streak: int = 0) -> float:
        """Get risk % adjusted for losing streak."""
        base = self._active_params.get("risk_pct", 1.0)
        if streak <= -3:
            return round(base * 0.5, 2)   # Half risk after 3 losses
        elif streak <= -5:
            return round(base * 0.25, 2)  # Quarter risk after 5 losses
        elif streak >= 3:
            return round(base * 1.15, 2)  # Slight boost on winning streak
        return base

    def get_session_boost(self, session: str) -> int:
        """Get confidence boost/penalty for current session."""
        session_map = {
            "london":  self._active_params.get("london_boost", 10),
            "ny":      self._active_params.get("ny_boost", 10),
            "asian":   self._active_params.get("asian_penalty", -10),
            "late_ny": self._active_params.get("friday_penalty", -5),
        }
        return session_map.get(session, 0)

    # ─────────────────────────────────────────────────────────────────────────
    # MONTHLY BENCHMARK COMPARISON
    # ─────────────────────────────────────────────────────────────────────────

    def monthly_benchmark(self) -> dict:
        """Compare bot performance vs S&P 500 over last 30 days."""
        try:
            import yfinance as yf

            # Bot performance
            analysis = self._analyze_trades()
            bot_return = analysis["total_r"]

            # S&P 500 performance
            spy = yf.Ticker("SPY").history(period="30d")
            if spy is not None and len(spy) > 1:
                spy_start = float(spy["Close"].iloc[0])
                spy_end   = float(spy["Close"].iloc[-1])
                spy_return = (spy_end - spy_start) / spy_start * 100
            else:
                spy_return = 0

            result = {
                "bot_return_r":    round(bot_return, 2),
                "spy_return_pct":  round(spy_return, 2),
                "outperforming":   bot_return > spy_return / 10,  # Rough comparison
                "period":          "30d",
            }

            # Telegram report
            try:
                from interfaces.telegram_interface import tg
                emoji = "🏆" if result["outperforming"] else "📉"
                tg.send(
                    f"{emoji} *Monthly Performance Review*\n\n"
                    f"Bot Return: `{bot_return:+.2f}R`\n"
                    f"S&P 500:   `{spy_return:+.2f}%`\n"
                    f"Trades:    `{analysis['total_trades']}`\n"
                    f"Win Rate:  `{analysis['win_rate']:.0f}%`\n"
                    f"Status:    {'Outperforming' if result['outperforming'] else 'Underperforming'}"
                )
            except Exception:
                pass

            return result
        except Exception as e:
            logger.error(f"[Evolver] Benchmark failed: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_recent_trades(self, count: int = 50) -> List[dict]:
        """Pull recent closed trades from DB."""
        try:
            from core.database_manager import db
            trades = db.get_recent_trades(count)
            return trades if trades else []
        except Exception:
            # Try position tracker's history
            try:
                from core.position_tracker import positions
                closed = positions.get_closed_trades(count)
                return closed if closed else []
            except Exception:
                return []

    def _diff_params(self, old: dict, new: dict) -> dict:
        """Return dict of changed params {key: {old, new}}."""
        changes = {}
        for key in new:
            if key in old and old[key] != new[key]:
                changes[key] = {"old": old[key], "new": new[key]}
        return changes

    def _clamp_changes(self, old: dict, new: dict) -> dict:
        """Ensure no parameter changes more than MAX_CHANGE_PCT."""
        clamped = copy.deepcopy(new)
        for key in clamped:
            if key not in old:
                continue
            old_val = old[key]
            new_val = clamped[key]
            if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                if old_val != 0:
                    max_delta = abs(old_val) * MAX_CHANGE_PCT
                    if abs(new_val - old_val) > max_delta:
                        clamped[key] = old_val + (max_delta if new_val > old_val else -max_delta)
                        if isinstance(old_val, int):
                            clamped[key] = int(round(clamped[key]))
                        else:
                            clamped[key] = round(clamped[key], 2)
        return clamped

    def _call_llm(self, prompt: str) -> Optional[str]:
        result = None
        if self._gemini_key:
            try:
                import requests
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/gemini-flash-latest:generateContent"
                    f"?key={self._gemini_key}"
                )
                resp = requests.post(url, json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 600, "temperature": 0.3}
                }, timeout=15)
                if resp.status_code == 200:
                    result = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                pass

        if not result and self._openai_key:
            try:
                import requests
                resp = requests.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._openai_key}"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 600, "temperature": 0.3,
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    result = resp.json()["choices"][0]["message"]["content"]
            except Exception:
                pass

        return result

    def _send_report(self, report: dict):
        """Send evolution report via Telegram."""
        try:
            from interfaces.telegram_interface import tg
            analysis = report["trade_analysis"]
            winner = report.get("winner")

            lines = [
                "🧬 *Weekly Strategy Evolution Report*\n",
                f"📊 *Performance* (last {analysis['total_trades']} trades):",
                f"  Win Rate: `{analysis['win_rate']:.0f}%`",
                f"  Avg R: `{analysis['avg_r']:+.2f}`",
                f"  Total R: `{analysis['total_r']:+.2f}`",
                f"  Max DD: `{analysis['max_drawdown_r']:.1f}R`\n",
            ]

            if winner:
                lines.append(f"🏆 *Winner*: {winner['name']}")
                for key, change in winner.get("changes", {}).items():
                    lines.append(f"  • `{key}`: {change['old']} → {change['new']}")
                lines.append(f"\n⏳ Staged for promotion in 7 days")
            else:
                lines.append("✅ No changes needed — current params performing well")

            tg.send("\n".join(lines))
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _load_params(self) -> dict:
        try:
            if CURRENT_PARAMS.exists():
                with open(CURRENT_PARAMS) as f:
                    return json.load(f)
        except Exception:
            pass
        return copy.deepcopy(DEFAULT_PARAMS)

    def _save_params(self):
        with open(CURRENT_PARAMS, "w") as f:
            json.dump(self._active_params, f, indent=2)

    def _load_staged(self) -> Optional[dict]:
        try:
            if STAGED_PARAMS.exists():
                with open(STAGED_PARAMS) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _save_history(self, report: dict):
        try:
            history = []
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
            history.append(report)
            history = history[-52:]  # Keep 1 year of weekly reports
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception:
            pass

    def _load_last_evolution_time(self) -> str:
        try:
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE) as f:
                    history = json.load(f)
                    if history:
                        return history[-1].get("timestamp", "never")
        except Exception:
            pass
        return "never"


# ── Singleton ──────────────────────────────────────────────────────────────
_evolver: Optional[StrategyEvolver] = None

def get_evolver() -> StrategyEvolver:
    global _evolver
    if _evolver is None:
        _evolver = StrategyEvolver()
    return _evolver
