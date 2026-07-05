"""
CRAVE v12 — Multi-Agent Trading Council
==========================================
Instead of one bot making all decisions, a council of 6 specialized
AI agents votes on every trade. A trade only executes if the majority
approves (4/6 minimum).

AGENTS:
  1. Director Agent    — "What's the macro theme? Is this the right market?"
  2. Quant Agent       — "Do the numbers support this? SMC, ATR, momentum?"
  3. Sentiment Agent   — "What are news/X/Twitter saying about this asset?"
  4. Risk Agent        — "Can we afford this trade? Drawdown, correlation?"
  5. Devil's Advocate  — "Why could this trade FAIL? What am I missing?"
  6. Execution Agent   — "Is timing right? Spread OK? Session optimal?"

FLOW:
  Signal generated → Each agent evaluates independently →
  Votes tallied → 4/6 required → Execute or Reject

Each agent uses LLM (Gemini/OpenAI) for reasoning with structured
JSON output. If LLM is unavailable, rule-based fallback kicks in.
"""

import os
import json
import logging
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("crave.council")


# ─────────────────────────────────────────────────────────────────────────────
# AGENT BASE
# ─────────────────────────────────────────────────────────────────────────────

class BaseAgent:
    """Base class for all council agents."""

    name: str = "Base"
    role: str = "Generic"

    def __init__(self):
        self._gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self._openai_key = os.environ.get("OPENAI_API_KEY", "")

    def evaluate(self, signal: dict, context: dict) -> dict:
        """
        Evaluate a trade signal.
        Returns: {"vote": "approve"|"reject"|"abstain",
                  "confidence": 0-100,
                  "reasoning": str}
        """
        raise NotImplementedError

    def _call_llm(self, prompt: str) -> Optional[str]:
        """Call LLM with fallback chain: Gemini → OpenAI."""
        result = self._call_gemini(prompt)
        if not result:
            result = self._call_openai(prompt)
        return result

    def _call_gemini(self, prompt: str) -> Optional[str]:
        if not self._gemini_key:
            return None
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/gemini-2.0-flash:generateContent"
                f"?key={self._gemini_key}"
            )
            resp = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 400, "temperature": 0.2}
            }, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            pass
        return None

    def _call_openai(self, prompt: str) -> Optional[str]:
        if not self._openai_key:
            return None
        try:
            resp = requests.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._openai_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400, "temperature": 0.2,
                },
                timeout=12,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
        return None


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1: DIRECTOR
# ─────────────────────────────────────────────────────────────────────────────

class DirectorAgent(BaseAgent):
    """
    Macro strategist. Asks: "Is this the right market environment for
    this trade? What's the macro theme?"
    """
    name = "Director"
    role = "Macro Strategist"

    def evaluate(self, signal: dict, context: dict) -> dict:
        symbol    = signal.get("symbol", "")
        direction = signal.get("direction", "")
        grade     = signal.get("grade", "")
        regime    = context.get("regime", "unknown")
        bias      = context.get("daily_bias", "neutral")
        session   = context.get("session", "")

        prompt = f"""You are the DIRECTOR of a trading council. Your job is to evaluate
the MACRO environment for a trade. You are conservative and strategic.

TRADE: {direction.upper()} {symbol}
Signal Grade: {grade} | Market Regime: {regime} | Daily Bias: {bias}
Session: {session}

Answer in EXACTLY this format (no other text):
VOTE: approve OR reject
CONFIDENCE: 0-100
REASON: one sentence why"""

        llm_response = self._call_llm(prompt)
        if llm_response:
            return self._parse_response(llm_response)

        # Rule-based fallback
        vote = "approve"
        confidence = 60
        reasons = []

        # Reject if trading against daily bias
        if bias and bias.lower() != "neutral":
            if (bias.lower() == "sell" and direction == "buy") or \
               (bias.lower() == "buy" and direction == "sell"):
                vote = "reject"
                confidence = 75
                reasons.append(f"Direction {direction} conflicts with daily bias {bias}")

        # Boost if regime aligns
        if regime and "trending" in regime.lower():
            confidence = min(confidence + 15, 100)
            reasons.append(f"Trending regime supports directional trade")

        # Penalize off-session
        if session in ("asian", "late_ny"):
            confidence = max(confidence - 15, 0)
            reasons.append(f"Off-session ({session}) reduces conviction")

        return {
            "vote": vote,
            "confidence": confidence,
            "reasoning": "; ".join(reasons) if reasons else "Macro conditions acceptable",
        }

    def _parse_response(self, text: str) -> dict:
        lines = text.strip().split("\n")
        vote = "abstain"
        confidence = 50
        reason = ""
        for line in lines:
            line_up = line.upper().strip()
            if line_up.startswith("VOTE:"):
                v = line.split(":", 1)[1].strip().lower()
                vote = "approve" if "approve" in v else "reject" if "reject" in v else "abstain"
            elif line_up.startswith("CONFIDENCE:"):
                try:
                    confidence = int("".join(c for c in line.split(":", 1)[1] if c.isdigit())[:3])
                except (ValueError, IndexError):
                    confidence = 50
            elif line_up.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        return {"vote": vote, "confidence": confidence, "reasoning": reason}


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 2: QUANT
# ─────────────────────────────────────────────────────────────────────────────

class QuantAgent(BaseAgent):
    """
    Technical analyst. Asks: "Do the numbers support this trade?
    Structure, indicators, price action?"
    """
    name = "Quant"
    role = "Technical Analyst"

    def evaluate(self, signal: dict, context: dict) -> dict:
        grade      = signal.get("grade", "C")
        confidence = signal.get("confidence", 0)
        atr_ratio  = context.get("atr_ratio", 1.0)
        adx        = context.get("adx", 0)
        rsi        = context.get("rsi", 50)

        # Rule-based (fast, no LLM needed for quant)
        vote = "approve"
        score = 0
        reasons = []

        # Grade gate
        grade_scores = {"A+": 30, "A": 25, "B+": 20, "B": 15, "C": 0}
        grade_val = grade_scores.get(grade, 0)
        score += grade_val
        if grade_val == 0:
            reasons.append(f"Grade {grade} is below minimum")

        # Signal confidence
        if confidence >= 60:
            score += 25
            reasons.append(f"High confidence {confidence}%")
        elif confidence >= 40:
            score += 15
            reasons.append(f"Moderate confidence {confidence}%")
        else:
            reasons.append(f"Low confidence {confidence}%")

        # ATR expansion
        if atr_ratio > 1.2:
            score += 15
            reasons.append("ATR expanding (volatility rising)")
        elif atr_ratio < 0.8:
            score -= 10
            reasons.append("ATR contracting (avoid)")

        # ADX trend strength
        if adx > 25:
            score += 15
            reasons.append(f"Strong trend (ADX={adx:.0f})")
        elif adx < 15:
            score -= 5
            reasons.append(f"Weak/no trend (ADX={adx:.0f})")

        # RSI extremes
        if rsi > 80 and signal.get("direction") == "buy":
            score -= 15
            reasons.append(f"Overbought RSI={rsi:.0f}")
        elif rsi < 20 and signal.get("direction") == "sell":
            score -= 15
            reasons.append(f"Oversold RSI={rsi:.0f}")

        if score < 30:
            vote = "reject"
        elif score < 50:
            vote = "abstain"

        return {
            "vote": vote,
            "confidence": min(score, 100),
            "reasoning": "; ".join(reasons),
        }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 3: SENTIMENT
# ─────────────────────────────────────────────────────────────────────────────

class SentimentAgent(BaseAgent):
    """
    News & social media analyst. Asks: "What is the market narrative?
    Does sentiment align with this trade?"
    """
    name = "Sentiment"
    role = "News & Social Analyst"

    def evaluate(self, signal: dict, context: dict) -> dict:
        symbol    = signal.get("symbol", "")
        direction = signal.get("direction", "")

        vote = "approve"
        confidence = 50
        reasons = []

        # Check news sentiment
        try:
            from intelligence.news_sentinel import get_sentinel
            sent = get_sentinel().get_asset_sentiment(symbol, max_age_mins=60)
            news_sentiment = sent.get("sentiment", "neutral")
            news_score = sent.get("score", 0)
            headline_count = sent.get("count", 0)

            if headline_count > 0:
                reasons.append(f"{headline_count} relevant headlines ({news_sentiment})")

                # Check alignment
                if (news_sentiment == "bullish" and direction == "buy") or \
                   (news_sentiment == "bearish" and direction == "sell"):
                    confidence += 20
                    reasons.append("News sentiment ALIGNS with direction")
                elif (news_sentiment == "bearish" and direction == "buy") or \
                     (news_sentiment == "bullish" and direction == "sell"):
                    confidence -= 20
                    vote = "reject"
                    reasons.append("News sentiment CONFLICTS with direction")
        except Exception:
            reasons.append("News data unavailable")

        # Check X/Twitter signals
        try:
            from intelligence.x_scraper import get_x_scraper
            x_signals = get_x_scraper().get_asset_signals(symbol, max_age_mins=30)
            if x_signals:
                for sig in x_signals[:2]:
                    reasons.append(f"X signal: {sig['account']} ({sig['sentiment']})")
                    if sig["sentiment"] == "bullish" and direction == "buy":
                        confidence += 10
                    elif sig["sentiment"] == "bearish" and direction == "sell":
                        confidence += 10
                    elif sig["urgency"] == "immediate":
                        confidence -= 15
                        vote = "reject"
        except Exception:
            pass

        # Check upcoming red folder events
        try:
            from intelligence.news_sentinel import get_sentinel
            red_events = get_sentinel().get_red_folder_events(hours_ahead=1)
            if red_events:
                reasons.append(f"⚠️ {len(red_events)} red-folder event(s) in next hour")
                confidence -= 15
        except Exception:
            pass

        confidence = max(0, min(100, confidence))
        if confidence < 30:
            vote = "reject"

        return {
            "vote": vote,
            "confidence": confidence,
            "reasoning": "; ".join(reasons) if reasons else "No sentiment data",
        }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 4: RISK MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class RiskAgent(BaseAgent):
    """
    Risk controller. Asks: "Can we afford this trade? What's the
    current drawdown? Are we over-correlated?"
    """
    name = "Risk"
    role = "Risk Controller"

    def evaluate(self, signal: dict, context: dict) -> dict:
        symbol    = signal.get("symbol", "")
        direction = signal.get("direction", "")

        vote = "approve"
        confidence = 70
        reasons = []

        # Check position count
        try:
            from core.position_tracker import positions
            open_count = positions.count()
            if open_count >= 5:
                vote = "reject"
                confidence = 90
                reasons.append(f"Too many open positions ({open_count}/5 max)")
            elif open_count >= 3:
                confidence -= 10
                reasons.append(f"{open_count} positions open (cautious)")
        except Exception:
            pass

        # Check correlation with open positions
        try:
            from engines.correlation_engine import get_correlation_engine
            from core.position_tracker import positions
            open_pos = list(positions.get_all().values())
            if open_pos:
                corr_check = get_correlation_engine().check_trade(
                    symbol, direction, open_pos
                )
                if corr_check["action"] == "block":
                    vote = "reject"
                    confidence = 95
                    reasons.append(f"Correlation block: {corr_check['reason']}")
                elif corr_check["action"] == "reduce":
                    confidence -= 15
                    reasons.append(f"Moderate correlation: {corr_check['reason']}")
        except Exception:
            pass

        # Check streak / circuit breaker
        try:
            from core.streak_state import streak
            can_trade, streak_reason = streak.can_trade()
            if not can_trade:
                vote = "reject"
                confidence = 99
                reasons.append(f"Circuit breaker: {streak_reason}")
        except Exception:
            pass

        # Check drawdown
        try:
            drawdown_pct = context.get("drawdown_pct", 0)
            if drawdown_pct > 8:
                vote = "reject"
                confidence = 90
                reasons.append(f"Drawdown {drawdown_pct:.1f}% exceeds 8% limit")
            elif drawdown_pct > 5:
                confidence -= 15
                reasons.append(f"Drawdown elevated at {drawdown_pct:.1f}%")
        except Exception:
            pass

        return {
            "vote": vote,
            "confidence": min(confidence, 100),
            "reasoning": "; ".join(reasons) if reasons else "Risk parameters OK",
        }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 5: DEVIL'S ADVOCATE
# ─────────────────────────────────────────────────────────────────────────────

class DevilsAdvocateAgent(BaseAgent):
    """
    Contrarian. Actively tries to find reasons NOT to take the trade.
    If the devil can't find a reason, the trade is strong.
    """
    name = "Devil's Advocate"
    role = "Contrarian Analyst"

    def evaluate(self, signal: dict, context: dict) -> dict:
        symbol    = signal.get("symbol", "")
        direction = signal.get("direction", "")
        grade     = signal.get("grade", "")
        session   = context.get("session", "")
        regime    = context.get("regime", "unknown")

        prompt = f"""You are the DEVIL'S ADVOCATE on a trading council.
Your job is to find every reason this trade could FAIL.
Be harsh, skeptical, and look for hidden risks.

TRADE: {direction.upper()} {symbol}
Grade: {grade} | Regime: {regime} | Session: {session}

List the top 3 risks, then decide:
VOTE: approve (if risks are manageable) OR reject (if risks are too high)
CONFIDENCE: 0-100
REASON: one sentence summary"""

        llm_response = self._call_llm(prompt)
        if llm_response:
            return self._parse_response(llm_response)

        # Rule-based devil's advocate
        risks = []
        risk_score = 0

        # Friday afternoon risk
        now = datetime.now(timezone.utc)
        if now.weekday() == 4 and now.hour >= 15:
            risks.append("Friday close: weekend gap risk")
            risk_score += 20

        # Off-session risk
        if session in ("asian", "late_ny"):
            risks.append(f"Low liquidity {session} session")
            risk_score += 15

        # Ranging market + directional trade
        if "ranging" in regime.lower():
            risks.append("Ranging market may whipsaw directional trades")
            risk_score += 15

        # Low grade
        if grade in ("B", "C"):
            risks.append(f"Signal grade {grade} is below institutional quality")
            risk_score += 20

        # Check lessons from autopsy
        try:
            from intelligence.trade_autopsy import get_autopsy
            adj = get_autopsy().get_confidence_adjustment(symbol, direction, grade, session)
            if adj < -10:
                risks.append(f"Historical pattern shows {abs(adj):.0f}% penalty for similar trades")
                risk_score += 15
        except Exception:
            pass

        vote = "reject" if risk_score >= 40 else "approve"
        confidence = min(risk_score + 30, 100)

        return {
            "vote": vote,
            "confidence": confidence,
            "reasoning": "; ".join(risks) if risks else "No major risks identified — trade is clean",
        }

    def _parse_response(self, text: str) -> dict:
        lines = text.strip().split("\n")
        vote = "abstain"
        confidence = 50
        reason = ""
        for line in lines:
            line_up = line.upper().strip()
            if line_up.startswith("VOTE:"):
                v = line.split(":", 1)[1].strip().lower()
                vote = "approve" if "approve" in v else "reject" if "reject" in v else "abstain"
            elif line_up.startswith("CONFIDENCE:"):
                try:
                    confidence = int("".join(c for c in line.split(":", 1)[1] if c.isdigit())[:3])
                except (ValueError, IndexError):
                    confidence = 50
            elif line_up.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        return {"vote": vote, "confidence": confidence, "reasoning": reason}


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 6: EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionAgent(BaseAgent):
    """
    Execution specialist. Checks timing, spread, and optimal entry.
    """
    name = "Execution"
    role = "Execution Specialist"

    def evaluate(self, signal: dict, context: dict) -> dict:
        symbol  = signal.get("symbol", "")
        session = context.get("session", "")

        vote = "approve"
        confidence = 65
        reasons = []

        # Check spread
        try:
            from brokers.mt5_agent import get_mt5
            import MetaTrader5 as mt5
            agent = get_mt5()
            if agent.ensure_connected():
                mt5_sym = agent._map_symbol(symbol)
                info = mt5.symbol_info(mt5_sym)
                if info:
                    spread_pips = info.spread * info.point
                    avg_spread = {"EURUSD": 0.0002, "GBPUSD": 0.0003,
                                  "XAUUSD": 0.25, "USDJPY": 0.02}.get(mt5_sym, 0.0005)
                    if spread_pips > avg_spread * 3:
                        vote = "reject"
                        confidence = 80
                        reasons.append(f"Spread too wide: {spread_pips} (3x normal)")
                    elif spread_pips > avg_spread * 2:
                        confidence -= 15
                        reasons.append(f"Spread elevated: {spread_pips}")
                    else:
                        reasons.append(f"Spread OK: {spread_pips}")
        except Exception:
            reasons.append("Spread check unavailable")

        # Session timing
        optimal_sessions = {
            "EURUSD=X": ["london", "ny"],
            "GBPUSD=X": ["london", "ny"],
            "XAUUSD=X": ["london", "ny"],
            "USDJPY=X": ["london", "ny", "asian"],
            "BTCUSDT":  ["london", "ny", "asian", "late_ny"],
        }
        good_sessions = optimal_sessions.get(symbol, ["london", "ny"])
        if session in good_sessions:
            confidence += 10
            reasons.append(f"Optimal session for {symbol}")
        else:
            confidence -= 10
            reasons.append(f"Suboptimal session ({session}) for {symbol}")

        # Market open check
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:  # Saturday/Sunday
            vote = "reject"
            confidence = 100
            reasons.append("Market closed (weekend)")

        return {
            "vote": vote,
            "confidence": min(confidence, 100),
            "reasoning": "; ".join(reasons),
        }


# ─────────────────────────────────────────────────────────────────────────────
# THE COUNCIL
# ─────────────────────────────────────────────────────────────────────────────

class TradingCouncil:
    """
    Orchestrates all 6 agents. Collects votes, makes final decision.
    Minimum 4/6 approval required to execute a trade.
    """

    MIN_APPROVALS = 4   # Need 4 out of 6 agents to approve

    def __init__(self):
        self.agents = [
            DirectorAgent(),
            QuantAgent(),
            SentimentAgent(),
            RiskAgent(),
            DevilsAdvocateAgent(),
            ExecutionAgent(),
        ]
        self._last_council: Optional[dict] = None

    def deliberate(self, signal: dict, context: dict) -> dict:
        """
        Run the full council deliberation on a trade signal.

        Returns:
            {
                "decision":   "execute" | "reject",
                "approvals":  int,
                "rejections": int,
                "abstentions": int,
                "votes":      [{agent, vote, confidence, reasoning}...],
                "reasoning":  str (summary),
            }
        """
        votes = []
        approvals = 0
        rejections = 0
        abstentions = 0

        for agent in self.agents:
            try:
                result = agent.evaluate(signal, context)
                vote_entry = {
                    "agent":      agent.name,
                    "role":       agent.role,
                    "vote":       result.get("vote", "abstain"),
                    "confidence": result.get("confidence", 50),
                    "reasoning":  result.get("reasoning", ""),
                }
                votes.append(vote_entry)

                if result["vote"] == "approve":
                    approvals += 1
                elif result["vote"] == "reject":
                    rejections += 1
                else:
                    abstentions += 1

            except Exception as e:
                logger.warning(f"[Council] {agent.name} agent failed: {e}")
                votes.append({
                    "agent": agent.name, "role": agent.role,
                    "vote": "abstain", "confidence": 0,
                    "reasoning": f"Agent error: {e}",
                })
                abstentions += 1

        # Risk Agent has VETO power — if Risk rejects, trade is blocked
        risk_vote = next((v for v in votes if v["agent"] == "Risk"), None)
        risk_veto = risk_vote and risk_vote["vote"] == "reject" and risk_vote["confidence"] >= 80

        # Execution Agent has VETO power for market closures (weekends, holidays)
        exec_vote = next((v for v in votes if v["agent"] == "Execution"), None)
        exec_veto = exec_vote and exec_vote["vote"] == "reject" and exec_vote["confidence"] >= 95

        decision = "execute"
        if risk_veto:
            decision = "reject"
            summary = f"VETOED by Risk Agent: {risk_vote['reasoning']}"
        elif exec_veto:
            decision = "reject"
            summary = f"VETOED by Execution Agent: {exec_vote['reasoning']}"
        elif approvals >= self.MIN_APPROVALS:
            decision = "execute"
            summary = f"APPROVED {approvals}/6 — executing trade"
        else:
            decision = "reject"
            rejectors = [v["agent"] for v in votes if v["vote"] == "reject"]
            summary = f"REJECTED {approvals}/6 (need {self.MIN_APPROVALS}) — blocked by: {', '.join(rejectors)}"

        result = {
            "decision":    decision,
            "approvals":   approvals,
            "rejections":  rejections,
            "abstentions": abstentions,
            "votes":       votes,
            "reasoning":   summary,
            "risk_veto":   risk_veto,
            "exec_veto":   exec_veto,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

        self._last_council = result

        # Log
        sym = signal.get("symbol", "?")
        direction = signal.get("direction", "?")
        emoji = "✅" if decision == "execute" else "❌"
        logger.info(
            f"[Council] {emoji} {sym} {direction.upper()} — "
            f"{approvals} approve, {rejections} reject, {abstentions} abstain → {decision.upper()}"
        )

        # Log individual votes
        for v in votes:
            vote_emoji = "👍" if v["vote"] == "approve" else "👎" if v["vote"] == "reject" else "🤷"
            logger.info(
                f"  {vote_emoji} {v['agent']:18s} | {v['vote']:7s} | "
                f"conf={v['confidence']:3d}% | {v['reasoning'][:60]}"
            )

        return result

    def get_last_deliberation(self) -> Optional[dict]:
        return self._last_council

    def get_telegram_summary(self, result: dict) -> str:
        """Format council result for Telegram."""
        decision = result["decision"]
        emoji = "✅" if decision == "execute" else "❌"
        lines = [f"{emoji} *Trading Council Decision*\n"]

        for v in result["votes"]:
            ve = "👍" if v["vote"] == "approve" else "👎" if v["vote"] == "reject" else "🤷"
            lines.append(
                f"{ve} *{v['agent']}* ({v['confidence']}%): {v['reasoning'][:50]}"
            )

        lines.append(f"\n*Result*: {result['reasoning']}")
        return "\n".join(lines)


# ── Singleton ──────────────────────────────────────────────────────────────
_council: Optional[TradingCouncil] = None

def get_council() -> TradingCouncil:
    global _council
    if _council is None:
        _council = TradingCouncil()
    return _council
