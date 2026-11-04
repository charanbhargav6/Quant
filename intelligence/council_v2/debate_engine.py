"""
CRAVE Council v2 — Adversarial Debate Engine
=============================================
Orchestrates a 3-round structured debate between 6 specialist agents.

Round 1 — Independent Analysis  (all 6 agents, no cross-talk)
Round 2 — Cross-Examination     (DevilsAdvocate + RiskManager challenge Round 1)
Round 3 — Rebuttals             (challenged agents respond briefly)
Verdict  — Director synthesises the full transcript into a recommendation

The debate is ADVISORY ONLY.  The result dict contains:
  recommendation: "proceed" | "hold" | "avoid"
  confidence:     0-100
  reasoning:      human-readable explanation
  full_transcript: complete per-round outputs for dashboard display
  providers_used: which LLM served each agent
  debate_time_s:  wall-clock seconds for the full debate
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from .agents import build_council, Director

log = logging.getLogger("crave.council_v2.debate")


class DebateEngine:

    def __init__(self):
        self._agents = build_council()
        self._director: Director = self._agents["Director"]
        self._last_result: Optional[dict] = None

    # ── Public interface ───────────────────────────────────────────────────────

    def run(self, context: dict) -> dict:
        """
        Execute a full 3-round debate and return the final result.
        context must contain at least: { "signal": {...} }
        Optional keys: session, daily_bias, regime, drawdown_pct, atr_ratio, adx, rsi, news_events
        """
        t0 = time.time()
        signal = context.get("signal", {})
        symbol = signal.get("symbol", "UNKNOWN")
        direction = signal.get("direction", "?")

        log.info(f"[Debate] Starting debate: {symbol} {direction.upper()}")

        # ── Round 1: Independent Analysis ─────────────────────────────────────
        log.info("[Debate] Round 1 — Independent analysis")
        t1 = time.time()
        round1 = self._run_round1(context)
        r1_time = time.time() - t1
        log.info(f"[Debate] Round 1 complete in {r1_time:.1f}s — "
                 f"{sum(1 for v in round1.values() if v.get('provider','none') != 'none')}/5 agents responded")

        # ── Round 2: Cross-Examination ─────────────────────────────────────────
        log.info("[Debate] Round 2 — Cross-examination")
        t2 = time.time()
        round2 = self._run_round2(round1, context)
        r2_time = time.time() - t2
        log.info(f"[Debate] Round 2 complete in {r2_time:.1f}s")

        # ── Round 3: Rebuttals ─────────────────────────────────────────────────
        log.info("[Debate] Round 3 — Rebuttals")
        t3 = time.time()
        round3 = self._run_round3(round1, round2, context)
        r3_time = time.time() - t3

        # ── Director's Verdict ─────────────────────────────────────────────────
        log.info("[Debate] Director — synthesising verdict")
        transcript = {"round1": round1, "round2": round2, "round3": round3}
        verdict = self._director.synthesise(transcript, context)

        total_time = time.time() - t0

        # Approval counting (align stances with direction)
        buy_dir = direction.lower() in ("buy", "long")
        approvals = sum(
            1 for a in round1.values()
            if (buy_dir and a.get("stance") == "bullish")
            or (not buy_dir and a.get("stance") == "bearish")
        )

        result = {
            "symbol":          symbol,
            "direction":       direction,
            "recommendation":  verdict["recommendation"],
            "confidence":      verdict["confidence"],
            "reasoning":       verdict["reasoning"],
            "key_factors":     verdict.get("key_factors", []),
            "dissent":         verdict.get("dissent", ""),
            "watch_levels":    verdict.get("watch_levels", {}),
            "approvals":       approvals,
            "total_agents":    len(round1),
            "transcript":      transcript,
            "providers_used":  {
                name: data.get("provider", "none")
                for name, data in {**round1, **round2, **{"Director": verdict}}.items()
            },
            "timing": {
                "round1_s":    round(r1_time, 1),
                "round2_s":    round(r2_time, 1),
                "round3_s":    round(r3_time, 1),
                "total_s":     round(total_time, 1),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "advisory_only": True,  # always — Council never executes trades
        }

        self._last_result = result

        rec_emoji = {"proceed": "✅", "hold": "⏸️", "avoid": "🚫"}.get(
            verdict["recommendation"], "❓")
        log.info(
            f"[Debate] {rec_emoji} VERDICT: {verdict['recommendation'].upper()} "
            f"| {verdict['confidence']}% confident | {total_time:.1f}s"
        )
        return result

    def get_last_result(self) -> Optional[dict]:
        return self._last_result

    def quick_market_assessment(self, topic: str, context: dict = None) -> dict:
        """
        Lightweight 1-round research query (no trade signal needed).
        Uses QuantAnalyst + SentimentAnalyst + DevilsAdvocate only.
        Returns a summary dict.
        """
        context = context or {}
        context.setdefault("signal", {"symbol": topic, "direction": "research"})

        agents_to_use = ["QuantAnalyst", "SentimentAnalyst", "DevilsAdvocate"]
        results = {}
        for name in agents_to_use:
            agent = self._agents.get(name)
            if agent:
                results[name] = agent.analyze(context)

        # Director summarises
        transcript = {"round1": results, "round2": {}, "round3": {}}
        verdict = self._director.synthesise(transcript, context)

        return {
            "topic":          topic,
            "recommendation": verdict["recommendation"],
            "confidence":     verdict["confidence"],
            "reasoning":      verdict["reasoning"],
            "agent_views":    {k: v.get("stance") for k, v in results.items()},
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "advisory_only":  True,
        }

    # ── Private round runners ──────────────────────────────────────────────────

    def _run_round1(self, context: dict) -> dict:
        """Run all analysts simultaneously via thread pool."""
        analyst_names = ["QuantAnalyst", "SentimentAnalyst",
                         "MacroResearcher", "DevilsAdvocate", "RiskManager"]
        results = {}

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {
                ex.submit(self._agents[name].analyze, context): name
                for name in analyst_names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=45)
                except Exception as e:
                    log.error(f"[Debate] Round 1 agent {name} crashed: {e}")
                    results[name] = {
                        "agent": name, "analysis": f"[crashed: {e}]",
                        "stance": "neutral", "confidence": 0,
                        "key_points": [], "provider": "none",
                    }
        return results

    def _run_round2(self, round1: dict, context: dict) -> dict:
        """DevilsAdvocate and RiskManager cross-examine Round 1."""
        results = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {
                ex.submit(self._agents["DevilsAdvocate"].cross_examine, round1, context):
                    "DevilsAdvocate",
                ex.submit(self._agents["RiskManager"].cross_examine, round1, context):
                    "RiskManager",
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=35)
                except Exception as e:
                    log.error(f"[Debate] Round 2 agent {name} crashed: {e}")
                    results[name] = {
                        "agent": name, "challenge": f"[crashed: {e}]",
                        "targets": [], "severity": "low", "provider": "none",
                    }
        return results

    def _run_round3(self, round1: dict, round2: dict, context: dict) -> dict:
        """
        Agents that were directly challenged write a brief rebuttal.
        Max 2 rebuttals to keep debate time bounded.
        """
        # Collect targeted agents
        challenged = set()
        for challenge_data in round2.values():
            challenged.update(challenge_data.get("targets", []))

        rebuttals = {}
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {}
            for name in list(challenged)[:3]:  # cap at 3 rebuttals
                agent = self._agents.get(name)
                r1_data = round1.get(name, {})
                r2_challenges = [
                    d.get("challenge", "")
                    for d in round2.values()
                    if name in d.get("targets", [])
                ]
                if agent and hasattr(agent, 'rebut') and r2_challenges:
                    combined_challenge = " | ".join(r2_challenges)
                    futures[ex.submit(agent.rebut, combined_challenge, context)] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result(timeout=25)
                    if result:
                        rebuttals[name] = result
                except Exception as e:
                    log.debug(f"[Debate] Round 3 rebuttal {name}: {e}")

        return rebuttals


# ── Singleton ──────────────────────────────────────────────────────────────────
_engine: Optional[DebateEngine] = None


def get_debate_engine() -> DebateEngine:
    global _engine
    if _engine is None:
        _engine = DebateEngine()
    return _engine

