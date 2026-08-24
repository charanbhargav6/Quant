"""
CRAVE v13.1 — Multi-Agent Hedge Fund Intelligence (Debate Architecture)
======================================================================
3-round debate engine with multi-LLM support:
  Groq (ultra-fast, free) → Gemini (smart) → Ollama (offline backup)

Round 1 (The Pitch): Quant & Sentiment build the bullish/bearish case.
Round 2 (Cross-Exam): Devil's Advocate & Risk attack the theses.
Round 3 (Verdict): Director reads the full transcript and rules,
                   dynamically adjusting SL/TP multipliers.
"""

import os
import json
import logging
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("crave.council")


class BaseAgent:
    """Base class for all council agents with multi-LLM routing."""

    name: str = "Base"
    role: str = "Generic"

    def __init__(self):
        self._gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self._groq_key = os.environ.get("GROQ_API_KEY", "")
        self._openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._openai_compat_key = os.environ.get("COUNCIL_OPENAI_API_KEY", "")
        self._openai_compat_base = os.environ.get("COUNCIL_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self._openai_compat_model = os.environ.get("COUNCIL_OPENAI_MODEL", "gpt-5-mini")
        try:
            from config.config import LLM_COUNCIL
            self.config = LLM_COUNCIL
        except ImportError:
            self.config = {}

    def _call_llm(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Try LLMs in order: assigned model → Groq → Gemini → Ollama."""
        model_choice = self.config.get(self.name, "groq").lower()

        # Build ordered fallback chain
        chain = []
        if model_choice not in chain:
            chain.append(model_choice)
        for fallback in ["groq", "gemini", "openrouter", "openai_compat", "ollama"]:
            if fallback not in chain:
                chain.append(fallback)

        for provider in chain:
            try:
                if provider == "groq":
                    res = self._call_groq(prompt, system_prompt)
                elif provider == "gemini":
                    res = self._call_gemini(prompt, system_prompt)
                elif provider == "ollama":
                    res = self._call_ollama(prompt, system_prompt)
                elif provider == "openrouter":
                    res = self._call_openrouter(prompt, system_prompt)
                elif provider == "openai_compat":
                    res = self._call_openai_compat(prompt, system_prompt)
                else:
                    res = None

                if res and len(res.strip()) > 5:
                    return res
                logger.debug(f"[Council] {self.name}: {provider} returned empty, trying next")
            except Exception as e:
                logger.debug(f"[Council] {self.name}: {provider} error: {e}")

        logger.warning(f"[Council] {self.name}: ALL LLMs failed, using rule-based fallback")
        return None

    # ── Groq (ultra-fast, free tier) ─────────────────────────────────────
    def _call_groq(self, prompt: str, system_prompt: str) -> Optional[str]:
        if not self._groq_key:
            return None
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._groq_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.2,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            else:
                logger.debug(f"[Groq] HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.debug(f"[Groq] Error: {e}")
        return None

    # ── Gemini (Google AI Studio, free tier) ─────────────────────────────
    def _call_gemini(self, prompt: str, system_prompt: str) -> Optional[str]:
        if not self._gemini_key:
            return None
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/gemini-flash-latest:generateContent"
                f"?key={self._gemini_key}"
            )
            full_prompt = f"SYSTEM: {system_prompt}\n\nUSER: {prompt}"
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {"maxOutputTokens": 400, "temperature": 0.2},
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            else:
                logger.debug(f"[Gemini] HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.debug(f"[Gemini] Error: {e}")
        return None

    # ── OpenAI-compatible provider (user-supplied key) ───────────────────
    def _call_openai_compat(self, prompt: str, system_prompt: str) -> Optional[str]:
        if not self._openai_compat_key:
            return None
        try:
            resp = requests.post(
                f"{self._openai_compat_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openai_compat_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._openai_compat_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.2,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"].get("content", "")
            logger.debug(f"[OpenAI-compatible] HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.debug(f"[OpenAI-compatible] Error: {e}")
        return None

    # ── Ollama (local, offline backup) ───────────────────────────────────
    def _call_ollama(self, prompt: str, system_prompt: str) -> Optional[str]:
        url = self.config.get("ollama_url", "http://localhost:11434/api/generate")
        model = self.config.get("default_ollama_model", "qwen2.5:14b")
        full_prompt = f"SYSTEM: {system_prompt}\n\nUSER: {prompt}"
        try:
            resp = requests.post(
                url,
                json={
                    "model": model,
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 400},
                },
                timeout=120,
            )
            if resp.status_code == 200:
                return resp.json().get("response", "")
        except Exception as e:
            logger.debug(f"[Ollama] Error: {e}")
        return None

    # ── OpenRouter (backup, many models) ─────────────────────────────────
    def _call_openrouter(self, prompt: str, system_prompt: str) -> Optional[str]:
        if not self._openrouter_key:
            return None
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._openrouter_key}",
                    "HTTP-Referer": "https://crave-trading.local",
                },
                json={
                    "model": "meta-llama/llama-3.3-70b-instruct:free",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.2,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            else:
                logger.debug(f"[OpenRouter] HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.debug(f"[OpenRouter] Error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 1: THE PITCH
# ─────────────────────────────────────────────────────────────────────────────

class QuantAgent(BaseAgent):
    name = "Quant"
    role = "Data Scientist"

    def pitch(self, signal: dict, context: dict) -> str:
        prompt = (
            f"TRADE: {signal['direction'].upper()} {signal['symbol']} "
            f"at {signal.get('entry', 'market')}\n"
            f"CONTEXT: RSI={context.get('rsi', 'N/A')}, "
            f"ADX={context.get('adx', 'N/A')}, "
            f"Regime={context.get('regime', 'N/A')}, "
            f"Daily Bias={context.get('daily_bias', 'N/A')}\n\n"
            "Build a concise, purely TECHNICAL argument FOR this trade. "
            "Reference the indicator values. Max 3 sentences."
        )
        return self._call_llm(
            prompt,
            "You are a quantitative analyst at a hedge fund. "
            "Build a bulletproof technical case for this trade using the data provided."
        ) or "Fallback: Technicals align with regime and momentum."


class SentimentAgent(BaseAgent):
    name = "Sentiment"
    role = "News/Sentiment Analyst"

    def pitch(self, signal: dict, context: dict) -> str:
        prompt = (
            f"TRADE: {signal['direction'].upper()} {signal['symbol']}\n"
            f"Market Regime: {context.get('regime', 'N/A')}\n\n"
            "Build a concise macro/sentiment argument FOR this trade. "
            "Consider current economic climate, central bank policy, "
            "and market sentiment. Max 3 sentences."
        )
        return self._call_llm(
            prompt,
            "You are a macro-economic analyst at a hedge fund. "
            "Pitch this trade using fundamental and sentiment logic."
        ) or "Fallback: Macro conditions are supportive of this direction."


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 2: CROSS-EXAMINATION
# ─────────────────────────────────────────────────────────────────────────────

class DevilsAdvocateAgent(BaseAgent):
    name = "DevilsAdvocate"
    role = "Contrarian Lawyer"

    def cross_examine(self, signal: dict, pitches: dict) -> str:
        prompt = (
            f"TRADE PROPOSED: {signal['direction'].upper()} {signal['symbol']}\n\n"
            f"BULL CASE (Quant):\n{pitches.get('Quant', 'No pitch')}\n\n"
            f"BULL CASE (Sentiment):\n{pitches.get('Sentiment', 'No pitch')}\n\n"
            "You are the OPPOSING LAWYER. Your job is to DESTROY these arguments. "
            "Point out: fakeout risks, liquidity traps, divergences they missed, "
            "why the timing is wrong. Be ruthless. Max 4 sentences."
        )
        return self._call_llm(
            prompt,
            "You are the Devil's Advocate at a hedge fund trading desk. "
            "You find flaws in every trade thesis. Be skeptical and precise."
        ) or "Fallback: The setup is vulnerable to a fakeout and liquidity grab."


class RiskAgent(BaseAgent):
    name = "Risk"
    role = "Risk Manager"

    def cross_examine(self, signal: dict, context: dict, pitches: dict) -> str:
        prompt = (
            f"TRADE: {signal['direction'].upper()} {signal['symbol']}\n"
            f"Current Drawdown: {context.get('drawdown_pct', 0)}%\n"
            f"ATR Ratio: {context.get('atr_ratio', 'N/A')}\n\n"
            f"PITCHES:\n{json.dumps(pitches, indent=2)}\n\n"
            "Evaluate the RISK of this trade. Consider: "
            "position sizing, correlation with existing positions, "
            "drawdown limits, and volatility. Max 3 sentences."
        )
        return self._call_llm(
            prompt,
            "You are the Chief Risk Officer. Your #1 priority is capital preservation. "
            "Flag any danger you see."
        ) or "Fallback: Risk is within limits, but SL should be strictly adhered to."


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 3: THE VERDICT
# ─────────────────────────────────────────────────────────────────────────────

class DirectorAgent(BaseAgent):
    name = "Director"
    role = "Portfolio Manager"

    def verdict(self, signal: dict, transcript: str) -> dict:
        prompt = (
            f"TRADE PROPOSED: {signal['direction'].upper()} {signal['symbol']}\n\n"
            f"FULL DEBATE TRANSCRIPT:\n{transcript}\n\n"
            "You are the PORTFOLIO MANAGER. Read every argument above.\n"
            "Make your final decision. Output ONLY valid JSON:\n"
            '{"decision": "execute" or "reject", '
            '"sl_multiplier": 1.0 to 2.0, '
            '"tp_multiplier": 1.5 to 3.0, '
            '"reasoning": "1 sentence explanation"}\n\n'
            "RULES:\n"
            "- If the Devil's Advocate raised valid concerns, WIDEN the SL (higher multiplier)\n"
            "- If Risk flagged high drawdown, REJECT\n"
            "- If both pitches are strong and critiques are weak, EXECUTE with tight SL"
        )

        res = self._call_llm(
            prompt,
            "You are the Portfolio Manager / final judge. "
            "Output ONLY valid JSON. No markdown, no explanation outside JSON."
        )

        if res:
            try:
                # Strip markdown code fences
                cleaned = res.strip()
                if "```json" in cleaned:
                    cleaned = cleaned.split("```json")[1].split("```")[0].strip()
                elif "```" in cleaned:
                    cleaned = cleaned.split("```")[1].split("```")[0].strip()

                # Find the JSON object
                start = cleaned.find("{")
                end = cleaned.rfind("}") + 1
                if start >= 0 and end > start:
                    cleaned = cleaned[start:end]

                data = json.loads(cleaned)
                return {
                    "decision": str(data.get("decision", "reject")).lower().strip(),
                    "sl_multiplier": max(0.5, min(3.0, float(data.get("sl_multiplier", 1.5)))),
                    "tp_multiplier": max(1.0, min(5.0, float(data.get("tp_multiplier", 2.0)))),
                    "reasoning": str(data.get("reasoning", "Parsed from LLM")),
                    "raw": res,
                }
            except Exception as e:
                logger.error(f"[Director] JSON parse error: {e} — Raw: {res[:200]}")

        # Fail closed: an unavailable or malformed judge response must never
        # approve a trade. Deterministic risk and execution code remains the
        # source of truth; the council is an advisory gate.
        return {
            "decision": "reject",
            "sl_multiplier": 1.5,
            "tp_multiplier": 2.0,
            "reasoning": "LLM unavailable or invalid verdict; fail-closed rejection",
            "raw": None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# COUNCIL ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

class TradingCouncil:
    """Orchestrates the 3-round debate and produces a final verdict."""

    def __init__(self):
        self.quant = QuantAgent()
        self.sentiment = SentimentAgent()
        self.devil = DevilsAdvocateAgent()
        self.risk = RiskAgent()
        self.director = DirectorAgent()
        self._last_council = None

    def deliberate(self, signal: dict, context: dict) -> dict:
        t0 = time.time()
        sym = signal.get("symbol", "?")
        direction = signal.get("direction", "?")
        logger.info(
            f"\n[Council] 🏛️ CONVENING DEBATE: {sym} {direction.upper()}"
        )

        # ── Round 1: The Pitch ───────────────────────────────────────────
        t1 = time.time()
        logger.info("[Council] ━━━ Round 1: The Pitch ━━━")
        pitches = {
            "Quant": self.quant.pitch(signal, context),
            "Sentiment": self.sentiment.pitch(signal, context),
        }
        r1_time = time.time() - t1
        logger.info(f"[Council] Quant says: {pitches['Quant'][:120]}...")
        logger.info(f"[Council] Sentiment says: {pitches['Sentiment'][:120]}...")
        logger.info(f"[Council] Round 1 completed in {r1_time:.1f}s")

        # ── Round 2: Cross-Examination ───────────────────────────────────
        t2 = time.time()
        logger.info("[Council] ━━━ Round 2: Cross-Examination ━━━")
        critiques = {
            "DevilsAdvocate": self.devil.cross_examine(signal, pitches),
            "Risk": self.risk.cross_examine(signal, context, pitches),
        }
        r2_time = time.time() - t2
        logger.info(f"[Council] Devil's Advocate: {critiques['DevilsAdvocate'][:120]}...")
        logger.info(f"[Council] Risk Manager: {critiques['Risk'][:120]}...")
        logger.info(f"[Council] Round 2 completed in {r2_time:.1f}s")

        # ── Round 3: The Verdict ─────────────────────────────────────────
        t3 = time.time()
        logger.info("[Council] ━━━ Round 3: The Verdict ━━━")
        transcript = (
            f"--- PITCHES (Arguments FOR the trade) ---\n"
            f"Quant: {pitches['Quant']}\n"
            f"Sentiment: {pitches['Sentiment']}\n\n"
            f"--- CRITIQUES (Arguments AGAINST the trade) ---\n"
            f"Devil's Advocate: {critiques['DevilsAdvocate']}\n"
            f"Risk Manager: {critiques['Risk']}"
        )
        verdict = self.director.verdict(signal, transcript)
        r3_time = time.time() - t3
        total_time = time.time() - t0

        decision_emoji = "✅" if verdict["decision"] == "execute" else "❌"
        logger.info(
            f"[Council] {decision_emoji} VERDICT: {verdict['decision'].upper()} "
            f"| SLx{verdict['sl_multiplier']:.1f} TPx{verdict['tp_multiplier']:.1f} "
            f"| {verdict['reasoning']}"
        )
        logger.info(
            f"[Council] ⏱️ Debate took {total_time:.1f}s "
            f"(R1={r1_time:.1f}s R2={r2_time:.1f}s R3={r3_time:.1f}s)"
        )

        votes = [
            {"agent": "Quant", "vote": "approve", "reasoning": pitches["Quant"]},
            {"agent": "Sentiment", "vote": "approve", "reasoning": pitches["Sentiment"]},
            {"agent": "Devil's Advocate", "vote": "reject" if "reject" in critiques["DevilsAdvocate"].lower() else "approve", "reasoning": critiques["DevilsAdvocate"]},
            {"agent": "Risk Manager", "vote": "reject" if "reject" in critiques["Risk"].lower() else "approve", "reasoning": critiques["Risk"]},
            {"agent": "Director", "vote": "approve" if verdict["decision"] == "execute" else "reject", "reasoning": verdict["reasoning"]},
        ]
        result = {
            "decision": verdict["decision"],
            "approvals": sum(v["vote"] == "approve" for v in votes),
            "votes": votes,
            "reasoning": verdict["reasoning"],
            "sl_multiplier": verdict["sl_multiplier"],
            "tp_multiplier": verdict["tp_multiplier"],
            "transcript": transcript,
            "debate_time_s": round(total_time, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._last_council = result
        return result

    def get_last_deliberation(self) -> Optional[dict]:
        return self._last_council

    def evaluate_open_trade(self, trade: dict, context: dict) -> dict:
        """Post-trade check for active positions."""
        # Simple LLM prompt for the Director to evaluate an open trade
        prompt = (
            f"Review this active trade: {trade['symbol']} {trade['direction']}. "
            f"Entry: {trade['entry']}, SL: {trade['sl']}, Live: {trade['live_price']}. "
            f"Current PNL: {trade['pnl_r']:.2f}R. "
            f"Any major news or market shift requiring us to CLOSE or EXTEND_TP? "
            f"Reply with JSON: {{\"action\": \"hold\"|\"close\"|\"extend_tp\", \"reasoning\": \"...\", \"tp_multiplier\": 1.5}}"
        )
        try:
            raw = self.director._call_llm(
                prompt,
                "You are a conservative portfolio risk monitor. Output only JSON. "
                "Never invent news or market data; if uncertain, choose hold."
            )
            if not raw:
                return {"action": "hold", "reasoning": "No model response; holding"}
            cleaned = raw.strip()
            if "```" in cleaned:
                cleaned = cleaned.split("```", 2)[1].replace("json", "", 1).strip()
            start, end = cleaned.find("{"), cleaned.rfind("}") + 1
            data = json.loads(cleaned[start:end]) if start >= 0 and end > start else {}
            action = str(data.get("action", "hold")).lower().strip()
            if action not in {"hold", "close", "extend_tp"}:
                action = "hold"
            result = {
                "action": action,
                "reasoning": str(data.get("reasoning", "Parsed council review")),
            }
            if action == "extend_tp":
                result["tp_multiplier"] = max(1.5, min(3.0, float(data.get("tp_multiplier", 1.5))))
            return result
        except Exception as e:
            logger.debug(f"[Council] Open trade check failed: {e}")
            return {"action": "hold", "reasoning": "Invalid or unavailable review; holding"}

    def get_telegram_summary(self, result: dict) -> str:
        emoji = "✅" if result["decision"] == "execute" else "❌"
        lines = [
            f"{emoji} *Council Debate Verdict*\n",
            f"*Decision:* {result['decision'].upper()}",
            f"*Reason:* {result['reasoning']}",
            f"*Adjustments:* SLx{result['sl_multiplier']:.1f}, TPx{result['tp_multiplier']:.1f}",
            f"*Debate Time:* {result.get('debate_time_s', '?')}s",
        ]
        return "\n".join(lines)


# ── Singleton ──────────────────────────────────────────────────────────────
_council: Optional[TradingCouncil] = None


def get_council() -> TradingCouncil:
    global _council
    if _council is None:
        _council = TradingCouncil()
    return _council
