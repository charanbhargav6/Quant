"""
CRAVE Council v2 — Specialist Agents
======================================
Six independent analysts with distinct mandates.
Each agent receives only the data relevant to its role.
No agent sees another's output during Round 1 (independent analysis).
"""

import json
import logging
import re
from typing import Optional
from .provider_registry import get_registry, ProviderError

log = logging.getLogger("crave.council_v2.agents")

# ── Preferred provider per role ────────────────────────────────────────────────
# Assign different providers to different roles to maximise diversity.
# If a preferred provider is unavailable, the registry falls back automatically.
ROLE_PROVIDERS = {
    "QuantAnalyst":      "groq",        # fast, great at structured reasoning
    "SentimentAnalyst":  "openrouter",  # Gemini: good at holistic market feel
    "MacroResearcher":   "openrouter",  # Gemini: good at geopolitical / macro
    "DevilsAdvocate":    "groq",        # fast, adversarial challenges
    "RiskManager":       "groq",        # fast, risk arithmetic
    "Director":          "openrouter",  # synthesiser — benefits from larger context
}


def _safe_json(text: str) -> dict:
    """Extract first JSON object from LLM response (handles markdown code fences)."""
    # Strip markdown fences
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end])
        except json.JSONDecodeError:
            pass
    return {}


class BaseSpecialist:
    name: str = "Base"
    mandate: str = "Generic"

    # Shared registry — all agents share the same pool but pick different providers
    _registry = None

    @classmethod
    def _reg(cls):
        if cls._registry is None:
            cls._registry = get_registry()
        return cls._registry

    def _call(self, system: str, user: str, max_tokens: int = 600) -> tuple[str, str]:
        """Returns (response_text, provider_used). Raises ProviderError if all fail."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        prefer = ROLE_PROVIDERS.get(self.name, None)
        return self._reg().chat(messages, max_tokens=max_tokens, prefer=prefer)

    def analyze(self, context: dict) -> dict:
        """
        Produce independent Round 1 analysis.
        Returns: { "analysis": str, "stance": "bullish|bearish|neutral|caution",
                   "confidence": int(0-100), "key_points": [str], "provider": str }
        """
        raise NotImplementedError

    def cross_examine(self, original_analyses: dict, context: dict) -> dict:
        """
        Round 2 — receive all Round 1 analyses and produce a targeted challenge.
        Returns: { "challenge": str, "targets": [agent_name], "severity": "low|medium|high" }
        Only DevilsAdvocate and RiskManager participate in this round.
        """
        return {"challenge": "N/A", "targets": [], "severity": "low", "provider": "skip"}

    def rebut(self, challenge: str, context: dict) -> Optional[str]:
        """
        Round 3A — respond to a challenge targeting this agent.
        Returns rebuttal string or None if not challenged.
        """
        return None


# ── Agent 1: Quant Analyst ─────────────────────────────────────────────────────

class QuantAnalyst(BaseSpecialist):
    name = "QuantAnalyst"
    mandate = "Technical price action, indicators, pattern recognition"

    SYSTEM = (
        "You are a systematic quantitative analyst at a hedge fund. "
        "You evaluate trade signals based strictly on price action, technical indicators "
        "and statistical edge. You are skeptical of narrative-driven trades. "
        "Be precise, cite specific levels, and always express confidence as a number. "
        "Reply ONLY with valid JSON — no markdown, no prose outside the JSON."
    )

    def analyze(self, context: dict) -> dict:
        signal = context.get("signal", {})
        market = context.get("market", {})
        user = (
            f"Evaluate this trade signal from a purely technical standpoint.\n\n"
            f"Signal: {json.dumps(signal, indent=2)}\n"
            f"Market context: {json.dumps(market, indent=2)}\n\n"
            f"Reply with JSON:\n"
            f'{{"analysis": "...", "stance": "bullish|bearish|neutral|caution", '
            f'"confidence": 0-100, "key_points": ["...", "..."], '
            f'"key_levels": {{"support": 0.0, "resistance": 0.0}}}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user)
            data = _safe_json(raw)
            return {
                "agent":      self.name,
                "analysis":   data.get("analysis", raw[:300]),
                "stance":     data.get("stance", "neutral"),
                "confidence": int(data.get("confidence", 50)),
                "key_points": data.get("key_points", []),
                "key_levels": data.get("key_levels", {}),
                "provider":   provider,
                "raw":        raw,
            }
        except ProviderError as e:
            return {"agent": self.name, "analysis": f"[unavailable: {e}]",
                    "stance": "neutral", "confidence": 0, "key_points": [], "provider": "none"}


# ── Agent 2: Sentiment Analyst ─────────────────────────────────────────────────

class SentimentAnalyst(BaseSpecialist):
    name = "SentimentAnalyst"
    mandate = "Market sentiment, news catalysts, fear/greed, positioning"

    SYSTEM = (
        "You are a market sentiment analyst. You evaluate crowd psychology, news flow, "
        "options positioning and social sentiment to identify contrarian opportunities "
        "or momentum confirmation. You never fabricate specific news stories. "
        "If you lack real-time data, state that explicitly and reason from general principles. "
        "Reply ONLY with valid JSON."
    )

    def analyze(self, context: dict) -> dict:
        signal  = context.get("signal", {})
        session = context.get("session", "london")
        bias    = context.get("daily_bias", "neutral")
        regime  = context.get("regime", "unknown")
        news    = context.get("news_events", [])

        user = (
            f"Evaluate market sentiment for this potential trade.\n\n"
            f"Symbol: {signal.get('symbol', 'Unknown')}\n"
            f"Direction: {signal.get('direction', 'Unknown')}\n"
            f"Session: {session} | Daily bias: {bias} | Regime: {regime}\n"
            f"Upcoming news events: {json.dumps(news[:5]) if news else 'None provided'}\n\n"
            f"Reply with JSON:\n"
            f'{{"analysis": "...", "stance": "bullish|bearish|neutral|caution", '
            f'"confidence": 0-100, "sentiment_score": -100 to 100, '
            f'"key_points": ["...", "..."], "news_risk": "low|medium|high"}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user)
            data = _safe_json(raw)
            return {
                "agent":           self.name,
                "analysis":        data.get("analysis", raw[:300]),
                "stance":          data.get("stance", "neutral"),
                "confidence":      int(data.get("confidence", 50)),
                "sentiment_score": int(data.get("sentiment_score", 0)),
                "key_points":      data.get("key_points", []),
                "news_risk":       data.get("news_risk", "unknown"),
                "provider":        provider,
                "raw":             raw,
            }
        except ProviderError as e:
            return {"agent": self.name, "analysis": f"[unavailable: {e}]",
                    "stance": "neutral", "confidence": 0, "key_points": [], "provider": "none"}


# ── Agent 3: Macro Researcher ──────────────────────────────────────────────────

class MacroResearcher(BaseSpecialist):
    name = "MacroResearcher"
    mandate = "Macro environment, correlations, intermarket analysis, economic calendar"

    SYSTEM = (
        "You are a macro researcher at a global macro fund. "
        "You evaluate how central bank policy, economic cycles, intermarket correlations "
        "and geopolitical risk affect individual instruments. "
        "You are rigorous and do not invent economic data. "
        "Reply ONLY with valid JSON."
    )

    def analyze(self, context: dict) -> dict:
        signal  = context.get("signal", {})
        symbol  = signal.get("symbol", "Unknown")
        regime  = context.get("regime", "unknown")
        atr_ratio = context.get("atr_ratio", 1.0)

        user = (
            f"Assess the macro environment for a potential {signal.get('direction','?')} trade on {symbol}.\n\n"
            f"Market regime: {regime}\n"
            f"ATR ratio (current/average): {atr_ratio:.2f}\n"
            f"Signal details: {json.dumps(signal, indent=2)}\n\n"
            f"Consider: USD correlation, risk-on/risk-off environment, "
            f"relevant central bank stance, and any known macro headwinds or tailwinds.\n\n"
            f"Reply with JSON:\n"
            f'{{"analysis": "...", "stance": "bullish|bearish|neutral|caution", '
            f'"confidence": 0-100, "macro_tailwind": true|false, '
            f'"key_points": ["...", "..."], "correlation_risk": "low|medium|high"}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user)
            data = _safe_json(raw)
            return {
                "agent":            self.name,
                "analysis":         data.get("analysis", raw[:300]),
                "stance":           data.get("stance", "neutral"),
                "confidence":       int(data.get("confidence", 50)),
                "macro_tailwind":   bool(data.get("macro_tailwind", False)),
                "key_points":       data.get("key_points", []),
                "correlation_risk": data.get("correlation_risk", "unknown"),
                "provider":         provider,
                "raw":              raw,
            }
        except ProviderError as e:
            return {"agent": self.name, "analysis": f"[unavailable: {e}]",
                    "stance": "neutral", "confidence": 0, "key_points": [], "provider": "none"}


# ── Agent 4: Devil's Advocate ──────────────────────────────────────────────────

class DevilsAdvocate(BaseSpecialist):
    name = "DevilsAdvocate"
    mandate = "Find flaws, challenge assumptions, identify what can go wrong"

    SYSTEM = (
        "You are the Devil's Advocate on a trading council. "
        "Your ONLY job is to identify everything that could go wrong with a proposed trade. "
        "Do not look for reasons to approve. Find the weakest points in the bull case. "
        "Be specific, cite exact concerns, and assign a probability (1-100%) to each failure mode. "
        "You are the last line of defence against overconfidence. "
        "Reply ONLY with valid JSON."
    )

    def analyze(self, context: dict) -> dict:
        signal = context.get("signal", {})
        user = (
            f"The council is considering this trade. Find every reason it could fail.\n\n"
            f"Signal: {json.dumps(signal, indent=2)}\n\n"
            f"Reply with JSON:\n"
            f'{{"challenge": "...", "failure_modes": ['
            f'{{"mode": "...", "probability": 0-100, "severity": "low|medium|high"}}], '
            f'"confidence_in_rejection": 0-100, '
            f'"key_points": ["...", "..."]}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user, max_tokens=700)
            data = _safe_json(raw)
            return {
                "agent":                   self.name,
                "analysis":                data.get("challenge", raw[:300]),
                "stance":                  "caution",
                "confidence":              int(data.get("confidence_in_rejection", 50)),
                "failure_modes":           data.get("failure_modes", []),
                "key_points":              data.get("key_points", []),
                "provider":                provider,
                "raw":                     raw,
            }
        except ProviderError as e:
            return {"agent": self.name, "analysis": f"[unavailable: {e}]",
                    "stance": "caution", "confidence": 0, "key_points": [], "provider": "none"}

    def cross_examine(self, original_analyses: dict, context: dict) -> dict:
        """Round 2 — read all Round 1 stances and challenge the most optimistic ones."""
        signal = context.get("signal", {})
        analyses_summary = "\n".join(
            f"- {name}: {data.get('stance','?')} ({data.get('confidence',0)}% confident)\n"
            f"  Key points: {'; '.join(data.get('key_points', [])[:2])}"
            for name, data in original_analyses.items()
            if name != self.name
        )
        user = (
            f"You have read the initial analyses from the council.\n\n"
            f"{analyses_summary}\n\n"
            f"Trade being considered: {signal.get('symbol','?')} {signal.get('direction','?')}\n\n"
            f"Now write a targeted cross-examination. Which specific claims are weakest? "
            f"Which agents are being overconfident?\n\n"
            f"Reply with JSON:\n"
            f'{{"challenge": "...", "targets": ["AgentName"], '
            f'"severity": "low|medium|high", "contradiction": "..."}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user, max_tokens=600)
            data = _safe_json(raw)
            return {
                "agent":        self.name,
                "challenge":    data.get("challenge", raw[:300]),
                "targets":      data.get("targets", []),
                "severity":     data.get("severity", "medium"),
                "contradiction":data.get("contradiction", ""),
                "provider":     provider,
            }
        except ProviderError as e:
            return {"agent": self.name, "challenge": f"[unavailable: {e}]",
                    "targets": [], "severity": "low", "provider": "none"}


# ── Agent 5: Risk Manager ──────────────────────────────────────────────────────

class RiskManager(BaseSpecialist):
    name = "RiskManager"
    mandate = "Position sizing, drawdown, portfolio risk, capital preservation"

    SYSTEM = (
        "You are the Risk Manager at a systematic trading fund. "
        "You evaluate every potential trade through the lens of capital preservation. "
        "You assess R:R ratios, current drawdown levels, position sizing, and portfolio "
        "concentration. You never approve a trade that violates risk rules. "
        "Reply ONLY with valid JSON."
    )

    def analyze(self, context: dict) -> dict:
        signal      = context.get("signal", {})
        drawdown    = context.get("drawdown_pct", 0.0)
        adx         = context.get("adx", 0)
        rsi         = context.get("rsi", 50)

        entry  = signal.get("limit_price") or signal.get("entry_price", 0)
        sl     = signal.get("stop_loss", 0)
        tp1    = signal.get("take_profit_1", 0)
        tp2    = signal.get("take_profit_2", 0)

        sl_dist  = abs(entry - sl) if entry and sl else 0
        rr_ratio = abs(tp2 - entry) / sl_dist if sl_dist > 0 and tp2 else 0

        user = (
            f"Evaluate the risk profile of this proposed trade.\n\n"
            f"Symbol: {signal.get('symbol','?')} | Direction: {signal.get('direction','?')}\n"
            f"Entry: {entry} | SL: {sl} | TP1: {tp1} | TP2: {tp2}\n"
            f"R:R ratio to TP2: {rr_ratio:.2f}\n"
            f"Current account drawdown: {drawdown:.1f}%\n"
            f"ADX (trend strength): {adx} | RSI: {rsi}\n\n"
            f"Assess: Is the R:R acceptable? Is the position appropriate given current drawdown? "
            f"Are there any risk red flags?\n\n"
            f"Reply with JSON:\n"
            f'{{"analysis": "...", "stance": "bullish|bearish|neutral|caution", '
            f'"confidence": 0-100, "rr_acceptable": true|false, '
            f'"risk_flags": ["..."], "max_loss_acceptable": true|false, '
            f'"key_points": ["...", "..."]}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user, max_tokens=600)
            data = _safe_json(raw)
            return {
                "agent":               self.name,
                "analysis":            data.get("analysis", raw[:300]),
                "stance":              data.get("stance", "caution"),
                "confidence":          int(data.get("confidence", 50)),
                "rr_acceptable":       bool(data.get("rr_acceptable", False)),
                "risk_flags":          data.get("risk_flags", []),
                "max_loss_acceptable": bool(data.get("max_loss_acceptable", False)),
                "key_points":          data.get("key_points", []),
                "computed_rr":         round(rr_ratio, 2),
                "drawdown_pct":        drawdown,
                "provider":            provider,
                "raw":                 raw,
            }
        except ProviderError as e:
            return {"agent": self.name, "analysis": f"[unavailable: {e}]",
                    "stance": "caution", "confidence": 0, "key_points": [], "provider": "none"}

    def cross_examine(self, original_analyses: dict, context: dict) -> dict:
        """Round 2 — flag risk issues the other analysts may have dismissed."""
        drawdown = context.get("drawdown_pct", 0.0)
        signal   = context.get("signal", {})
        analyses_summary = "\n".join(
            f"- {name}: {data.get('stance','?')} ({data.get('confidence',0)}%)"
            for name, data in original_analyses.items()
            if name != self.name
        )
        user = (
            f"The council has produced these preliminary stances:\n{analyses_summary}\n\n"
            f"Current drawdown: {drawdown:.1f}%\n"
            f"Signal: {signal.get('symbol','?')} {signal.get('direction','?')}\n\n"
            f"Are the other analysts underweighting risk? "
            f"Point out any risk factors they've missed or minimised.\n\n"
            f"Reply with JSON:\n"
            f'{{"challenge": "...", "targets": ["AgentName"], '
            f'"severity": "low|medium|high", "risk_items_missed": ["..."]}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user, max_tokens=500)
            data = _safe_json(raw)
            return {
                "agent":              self.name,
                "challenge":          data.get("challenge", raw[:300]),
                "targets":            data.get("targets", []),
                "severity":           data.get("severity", "medium"),
                "risk_items_missed":  data.get("risk_items_missed", []),
                "provider":           provider,
            }
        except ProviderError as e:
            return {"agent": self.name, "challenge": f"[unavailable: {e}]",
                    "targets": [], "severity": "low", "provider": "none"}


# ── Agent 6: Director ──────────────────────────────────────────────────────────

class Director(BaseSpecialist):
    name = "Director"
    mandate = "Synthesise all evidence, produce final advisory recommendation"

    SYSTEM = (
        "You are the Director of a quant trading council. "
        "You have read the full debate: initial analyses, cross-examinations, and rebuttals. "
        "Your job is to produce a clear, evidence-based advisory recommendation. "
        "You DO NOT control trade execution — this is advisory only. "
        "Be objective, weigh the evidence fairly, and do not simply side with the majority. "
        "If the evidence is split, say so explicitly. "
        "Reply ONLY with valid JSON."
    )

    def synthesise(self, transcript: dict, context: dict) -> dict:
        """
        Read the full transcript and produce the final recommendation.
        transcript: { "round1": {agent: analysis}, "round2": {agent: challenge},
                      "round3": {agent: rebuttal} }
        Returns: { "recommendation": "proceed|hold|avoid",
                   "confidence": int, "reasoning": str,
                   "key_factors": [str], "dissent": str, "provider": str }
        """
        signal = context.get("signal", {})

        # Summarise transcript compactly for the director prompt
        r1 = "\n".join(
            f"[{a}] Stance: {d.get('stance','?')} ({d.get('confidence',0)}%) — "
            f"{d.get('analysis','')[:200]}"
            for a, d in transcript.get("round1", {}).items()
        )
        r2 = "\n".join(
            f"[{a} challenges] {d.get('challenge','')[:200]}"
            for a, d in transcript.get("round2", {}).items()
            if d.get("challenge", "N/A") != "N/A"
        )
        r3 = "\n".join(
            f"[{a} rebuts] {t[:150]}"
            for a, t in transcript.get("round3", {}).items()
            if t
        )

        approve_count = sum(
            1 for d in transcript.get("round1", {}).values()
            if d.get("stance") in ("bullish", "bearish")
            and "direction" in context.get("signal", {})
            and (
                (d.get("stance") == "bullish" and context["signal"]["direction"] in ("buy","long"))
                or (d.get("stance") == "bearish" and context["signal"]["direction"] in ("sell","short"))
            )
        )

        user = (
            f"=== FULL COUNCIL DEBATE ===\n\n"
            f"Trade: {signal.get('symbol','?')} {signal.get('direction','?').upper()}\n"
            f"Entry: {signal.get('limit_price') or signal.get('entry_price','?')} | "
            f"SL: {signal.get('stop_loss','?')} | "
            f"TP2: {signal.get('take_profit_2','?')}\n\n"
            f"--- ROUND 1: Independent Analyses ---\n{r1}\n\n"
            f"--- ROUND 2: Cross-Examinations ---\n{r2 or 'None'}\n\n"
            f"--- ROUND 3: Rebuttals ---\n{r3 or 'None'}\n\n"
            f"Agents in favour: {approve_count}/{len(transcript.get('round1',{}))}\n\n"
            f"Produce your final ADVISORY recommendation.\n"
            f"Remember: this is advisory only — the deterministic risk engine makes the final call.\n\n"
            f"Reply with JSON:\n"
            f'{{"recommendation": "proceed|hold|avoid", "confidence": 0-100, '
            f'"reasoning": "...", "key_factors": ["...", "..."], '
            f'"dissent": "...", "watch_levels": {{"support": 0.0, "resistance": 0.0}}}}'
        )
        try:
            raw, provider = self._call(self.SYSTEM, user, max_tokens=900)
            data = _safe_json(raw)
            return {
                "agent":          self.name,
                "recommendation": data.get("recommendation", "hold"),
                "confidence":     int(data.get("confidence", 50)),
                "reasoning":      data.get("reasoning", raw[:400]),
                "key_factors":    data.get("key_factors", []),
                "dissent":        data.get("dissent", ""),
                "watch_levels":   data.get("watch_levels", {}),
                "provider":       provider,
                "raw":            raw,
            }
        except ProviderError as e:
            return {
                "agent": self.name, "recommendation": "hold",
                "confidence": 0, "reasoning": f"Director unavailable: {e}",
                "key_factors": [], "dissent": "", "provider": "none",
            }


# ── Factory ───────────────────────────────────────────────────────────────────

def build_council() -> dict:
    """Return all 6 agents as a dict keyed by name."""
    return {
        "QuantAnalyst":    QuantAnalyst(),
        "SentimentAnalyst": SentimentAnalyst(),
        "MacroResearcher": MacroResearcher(),
        "DevilsAdvocate":  DevilsAdvocate(),
        "RiskManager":     RiskManager(),
        "Director":        Director(),
    }

