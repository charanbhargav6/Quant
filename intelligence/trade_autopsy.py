"""
CRAVE v12 — Trade Autopsy Engine (Self-Learning)
===================================================
After every trade closes (win OR loss), analyze what happened and
extract actionable lessons. These lessons compound over time into
a "memory" that makes the bot increasingly sharper.

FLOW:
  Trade closes → Autopsy runs → Lesson extracted → Saved to DB
  Next trade evaluation → Load similar lessons → Apply filters/boosts

EXAMPLE AUTOPSY:
  LOSS on EURUSD LONG at 1.0950 (-1.2R):
  ├── Entry signal: BOS + FVG at OB | Grade A | Confidence 62%
  ├── Market context: DXY rising, NFP in 2 hours, hawkish sentiment
  ├── What went wrong: Traded against macro bias (DXY rising = USD strong)
  ├── Lesson: "Don't go LONG EURUSD when DXY 5D trend is UP"
  └── Action: Tag future EURUSD LONG signals with -10 confidence penalty
              when DXY momentum is positive.

OVER 100+ TRADES:
  "You win 80% of A+ trades on Tuesday morning London open"
  "You lose 70% of B trades on Friday afternoon"
  "XAUUSD CHoCH+OB has 75% win rate, but only during NY session"
  "Recommendation: Disable B-grade on Fridays, boost XAUUSD CHoCH weight"
"""

import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger("crave.autopsy")

AUTOPSY_DIR  = Path(__file__).parent.parent / "State" / "autopsies"
LESSONS_FILE = Path(__file__).parent.parent / "State" / "lessons.json"
AUTOPSY_DIR.mkdir(parents=True, exist_ok=True)


class TradeAutopsy:
    """
    Post-trade analysis engine. Learns from every trade outcome.
    """

    def __init__(self):
        self._lessons: List[Dict] = self._load_lessons()
        self._api_key = os.environ.get("GEMINI_API_KEY", "")
        self._openai_key = os.environ.get("OPENAI_API_KEY", "")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_trade(self, closed_trade: dict):
        """
        Run full autopsy on a closed trade.
        Called automatically after every trade close.
        """
        try:
            trade_id   = closed_trade.get("trade_id", "unknown")
            symbol     = closed_trade.get("symbol", "")
            direction  = closed_trade.get("direction", "")
            r_multiple = closed_trade.get("r_multiple", 0)
            outcome    = closed_trade.get("outcome", "")
            entry_time = closed_trade.get("open_time", "")
            close_time = closed_trade.get("close_time", "")
            entry_price= closed_trade.get("entry_price", 0)
            exit_price = closed_trade.get("exit_price", 0)

            # Gather context
            context = self._gather_context(closed_trade)

            # Run LLM analysis
            llm_analysis = self._run_llm_autopsy(closed_trade, context)

            # Extract structured lessons
            lessons = self._extract_lessons(closed_trade, context, llm_analysis)

            # Save autopsy
            autopsy = {
                "trade_id":    trade_id,
                "symbol":      symbol,
                "direction":   direction,
                "r_multiple":  r_multiple,
                "outcome":     outcome,
                "entry_time":  entry_time,
                "close_time":  close_time,
                "context":     context,
                "llm_analysis": llm_analysis,
                "lessons":     lessons,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }

            self._save_autopsy(autopsy)

            # Add lessons to memory
            for lesson in lessons:
                self._add_lesson(lesson)

            logger.info(
                f"[Autopsy] {symbol} {direction} {outcome} ({r_multiple:+.2f}R) → "
                f"{len(lessons)} lessons extracted"
            )

        except Exception as e:
            logger.error(f"[Autopsy] Analysis failed: {e}")

    def get_lessons_for_trade(self, symbol: str, direction: str,
                               grade: str = "", session: str = "") -> List[Dict]:
        """
        Retrieve relevant lessons for a proposed trade.
        Used by the trading loop to adjust confidence/risk.
        """
        relevant = []
        for lesson in self._lessons:
            # Match by symbol or universal
            lesson_sym = lesson.get("applies_to", {}).get("symbol", "")
            if lesson_sym and lesson_sym != symbol and lesson_sym != "*":
                continue

            # Match by direction
            lesson_dir = lesson.get("applies_to", {}).get("direction", "")
            if lesson_dir and lesson_dir != direction and lesson_dir != "*":
                continue

            # Match by session
            lesson_sess = lesson.get("applies_to", {}).get("session", "")
            if lesson_sess and lesson_sess != session and lesson_sess != "*":
                continue

            relevant.append(lesson)

        return relevant

    def get_confidence_adjustment(self, symbol: str, direction: str,
                                    grade: str = "", session: str = "") -> float:
        """
        Calculate net confidence adjustment from all relevant lessons.
        Returns: float (e.g., -10 means reduce confidence by 10%)
        """
        lessons = self.get_lessons_for_trade(symbol, direction, grade, session)

        total_adjustment = 0.0
        for lesson in lessons:
            adj = lesson.get("confidence_adjustment", 0)
            weight = lesson.get("weight", 1.0)
            total_adjustment += adj * weight

        return round(total_adjustment, 1)

    def get_statistics(self) -> Dict:
        """Return learning statistics."""
        if not self._lessons:
            return {"total_lessons": 0, "net_adjustment": 0}

        return {
            "total_lessons": len(self._lessons),
            "positive_lessons": sum(1 for l in self._lessons if l.get("confidence_adjustment", 0) > 0),
            "negative_lessons": sum(1 for l in self._lessons if l.get("confidence_adjustment", 0) < 0),
            "symbols_covered": len(set(l.get("applies_to", {}).get("symbol", "*") for l in self._lessons)),
            "top_lessons": self._lessons[:5],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXT GATHERING
    # ─────────────────────────────────────────────────────────────────────────

    def _gather_context(self, trade: dict) -> dict:
        """Gather market context at the time of the trade."""
        symbol = trade.get("symbol", "")
        context = {
            "day_of_week": "",
            "session": "",
            "news_sentiment": "neutral",
            "dxy_trend": "unknown",
            "regime": "unknown",
            "grade": trade.get("grade", ""),
            "confidence": trade.get("confidence", 0),
        }

        try:
            open_time = datetime.fromisoformat(trade.get("open_time", ""))
            context["day_of_week"] = open_time.strftime("%A")
            hour = open_time.hour
            if 7 <= hour < 12:
                context["session"] = "london"
            elif 12 <= hour < 17:
                context["session"] = "ny"
            elif 0 <= hour < 7:
                context["session"] = "asian"
            else:
                context["session"] = "late_ny"
        except Exception:
            pass

        # Get news context at that time
        try:
            from intelligence.news_sentinel import get_sentinel
            sent = get_sentinel().get_asset_sentiment(symbol, max_age_mins=120)
            context["news_sentiment"] = sent.get("sentiment", "neutral")
        except Exception:
            pass

        return context

    # ─────────────────────────────────────────────────────────────────────────
    # LLM ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def _run_llm_autopsy(self, trade: dict, context: dict) -> str:
        """Use LLM to write a detailed post-mortem analysis."""
        prompt = f"""You are a senior trader analyzing a closed trade. Be brutally honest.

TRADE DATA:
- Symbol: {trade.get('symbol')}
- Direction: {trade.get('direction')}
- Entry: {trade.get('entry_price')} → Exit: {trade.get('exit_price')}
- R-Multiple: {trade.get('r_multiple'):+.2f}R
- Outcome: {trade.get('outcome')}
- Signal Grade: {context.get('grade')}
- Confidence: {context.get('confidence')}%
- Day: {context.get('day_of_week')} | Session: {context.get('session')}
- News Sentiment: {context.get('news_sentiment')}

ANALYZE:
1. What went RIGHT in this trade?
2. What went WRONG?
3. Was the entry timing good or bad?
4. Was the direction aligned with the macro trend?
5. What specific lesson should be saved to avoid repeating this mistake (or to repeat this success)?

Reply in 3-5 concise sentences. Focus on the actionable lesson."""

        # Try Gemini first, then OpenAI
        analysis = self._call_gemini(prompt)
        if not analysis:
            analysis = self._call_openai(prompt)
        if not analysis:
            # Basic rule-based analysis
            analysis = self._rule_based_analysis(trade, context)

        return analysis

    def _call_gemini(self, prompt: str) -> Optional[str]:
        if not self._api_key:
            return None
        try:
            import requests
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/gemini-1.5-flash:generateContent"
                f"?key={self._api_key}"
            )
            resp = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0.3}
            }, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.debug(f"[Autopsy] Gemini call failed: {e}")
        return None

    def _call_openai(self, prompt: str) -> Optional[str]:
        if not self._openai_key:
            return None
        try:
            import requests
            resp = requests.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._openai_key}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.debug(f"[Autopsy] OpenAI call failed: {e}")
        return None

    def _rule_based_analysis(self, trade: dict, context: dict) -> str:
        """Fallback analysis when no LLM is available."""
        r = trade.get("r_multiple", 0)
        sym = trade.get("symbol", "")
        direction = trade.get("direction", "")
        session = context.get("session", "")
        sentiment = context.get("news_sentiment", "neutral")

        parts = []
        if r > 1.5:
            parts.append(f"Strong winner ({r:+.2f}R). Entry timing was good.")
        elif r > 0:
            parts.append(f"Small win ({r:+.2f}R). Could improve TP targeting.")
        elif r > -0.5:
            parts.append(f"Breakeven/small loss ({r:+.2f}R). SL may be too tight.")
        else:
            parts.append(f"Significant loss ({r:+.2f}R). Review entry conditions.")

        if sentiment == "bearish" and direction == "buy":
            parts.append("Entry went against news sentiment (bearish).")
        elif sentiment == "bullish" and direction == "sell":
            parts.append("Entry went against news sentiment (bullish).")

        if session == "asian" and r < 0:
            parts.append("Asian session losses are common — low volume.")

        return " ".join(parts)

    # ─────────────────────────────────────────────────────────────────────────
    # LESSON EXTRACTION
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_lessons(self, trade: dict, context: dict,
                          analysis: str) -> List[Dict]:
        """Extract structured lessons from a trade autopsy."""
        lessons = []
        r = trade.get("r_multiple", 0)
        sym = trade.get("symbol", "")
        direction = trade.get("direction", "")
        session = context.get("session", "")
        grade = context.get("grade", "")
        day = context.get("day_of_week", "")
        sentiment = context.get("news_sentiment", "neutral")

        # Pattern: Trade against news sentiment
        if (sentiment == "bearish" and direction == "buy" and r < 0) or \
           (sentiment == "bullish" and direction == "sell" and r < 0):
            lessons.append({
                "type": "anti_sentiment",
                "description": f"Lost trading against {sentiment} sentiment on {sym}",
                "applies_to": {"symbol": sym, "direction": direction, "session": "*"},
                "confidence_adjustment": -8,
                "weight": 1.0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        # Pattern: Session-specific performance
        if r < -0.5 and session == "asian":
            lessons.append({
                "type": "bad_session",
                "description": f"Loss during Asian session on {sym} ({r:+.2f}R)",
                "applies_to": {"symbol": sym, "direction": "*", "session": "asian"},
                "confidence_adjustment": -5,
                "weight": 0.8,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        # Pattern: Day-specific performance
        if r < -0.5 and day == "Friday":
            lessons.append({
                "type": "friday_loss",
                "description": f"Friday loss on {sym} ({r:+.2f}R) — weekend risk",
                "applies_to": {"symbol": sym, "direction": "*", "session": "*"},
                "confidence_adjustment": -5,
                "weight": 0.7,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        # Pattern: Grade performance
        if r > 1.5 and grade:
            lessons.append({
                "type": "winning_grade",
                "description": f"Strong win with grade {grade} on {sym} ({r:+.2f}R)",
                "applies_to": {"symbol": sym, "direction": direction, "session": session},
                "confidence_adjustment": +5,
                "weight": 0.8,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        # Pattern: Consistent winner
        if r > 2.0:
            lessons.append({
                "type": "big_win",
                "description": f"Big win on {sym} {direction} ({r:+.2f}R) during {session}",
                "applies_to": {"symbol": sym, "direction": direction, "session": session},
                "confidence_adjustment": +8,
                "weight": 1.0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        return lessons

    # ─────────────────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────

    def _add_lesson(self, lesson: dict):
        """Add a lesson and save to disk."""
        self._lessons.append(lesson)
        # Keep only last 200 lessons (sliding window)
        if len(self._lessons) > 200:
            self._lessons = self._lessons[-200:]
        self._save_lessons()

    def _save_autopsy(self, autopsy: dict):
        """Save individual trade autopsy."""
        fname = AUTOPSY_DIR / f"{autopsy['trade_id']}.json"
        try:
            with open(fname, "w") as f:
                json.dump(autopsy, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"[Autopsy] Save failed: {e}")

    def _save_lessons(self):
        try:
            with open(LESSONS_FILE, "w") as f:
                json.dump(self._lessons, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"[Autopsy] Lesson save failed: {e}")

    def _load_lessons(self) -> List[Dict]:
        try:
            if LESSONS_FILE.exists():
                with open(LESSONS_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return []


# ── Singleton ──────────────────────────────────────────────────────────────
_autopsy: Optional[TradeAutopsy] = None

def get_autopsy() -> TradeAutopsy:
    global _autopsy
    if _autopsy is None:
        _autopsy = TradeAutopsy()
    return _autopsy
