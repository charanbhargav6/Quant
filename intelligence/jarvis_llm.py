"""
CRAVE v10.4 — Jarvis LLM Intelligence (Zone 2)
================================================
Two LLM-powered features that give the bot semantic awareness.

─────────────────────────────────────────────
FEATURE 1: SENTIMENT-WEIGHTED BIAS (Narrative Filter)
─────────────────────────────────────────────
The bot reads numbers. Jarvis reads the narrative.

A 1H chart can show a perfect BUY setup — OB hit, CHoCH confirmed,
volume expanding. But if the Fed just released hawkish minutes and
every headline says "rate hike imminent," entering that BUY is
walking into institutional supply.

FLOW:
  1. Scrape top 10 headlines from free news APIs
  2. Send to Gemini with structured prompt
  3. Get: HAWKISH / DOVISH / BLACK_SWAN / NEUTRAL per asset class
  4. Apply override to signal:
       HAWKISH + BUY crypto/gold → HALF_SIZE or NO_TRADE
       BLACK_SWAN (any) → NO_TRADE for 4 hours
       DOVISH + BUY → confirm (bias reinforced)
       NEUTRAL → no change

─────────────────────────────────────────────
FEATURE 2: AUTOMATED TRADE POST-MORTEM
─────────────────────────────────────────────
After every trade closes, Jarvis writes a 3-paragraph journal entry
comparing plan vs execution and identifying patterns.

Over 100+ trades it builds a pattern library:
  "You win 80% of A+ trades on Tuesday morning London open"
  "You lose 70% of B trades on Friday afternoon"
  "Your best setups are XAUUSD CHoCH + OB confluence"
  "Recommendation: Disable B-grade trades on Friday"

SETUP:
  1. Get free Gemini API key: aistudio.google.com/apikey
  2. Add to .env: GEMINI_API_KEY=your_key
  3. Uses gemini-1.5-flash (free tier, 15 req/min, 1M tokens/day)

USAGE:
  from intelligence.jarvis_llm import get_jarvis

  jarvis = get_jarvis()

  # Before trading — get sentiment override
  override = jarvis.get_sentiment_override("XAUUSD", "buy")
  if override["action"] == "NO_TRADE":
      return  # Jarvis vetoed the trade

  # After trade closes — write post-mortem
  jarvis.write_trade_postmortem(closed_trade_dict)
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
try:
    from src.core.paths import CRAVE_ROOT
except ImportError:
    from pathlib import Path
    import os
    CRAVE_ROOT = os.environ.get('CRAVE_ROOT', str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger("crave.jarvis")

# Cache sentiment so we don't spam the API on every signal
# Sentiment is refreshed every 2 hours (markets move slowly at narrative level)
_SENTIMENT_CACHE_MINS = 120
_POSTMORTEM_DIR = Path(__file__).parent.parent.parent.parent / "State" / "postmortems"
_POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)


class JarvisLLM:

    def __init__(self):
        self._api_key = os.environ.get("GEMINI_API_KEY", "")
        
        self._model = "gemini-flash-latest"
        try:
            hw_path = Path(CRAVE_ROOT) / "config" / "hardware.json"
            if hw_path.exists():
                with open(hw_path, "r", encoding="utf-8") as f:
                    hw = json.load(f)
                    self._model = hw.get("api_routing", {}).get("models", {}).get("gemini", "gemini-flash-latest")
        except Exception:
            pass
            
        self._client  = None
        self._sentiment_cache: dict = {}      # asset_class → {sentiment, ts}
        self._postmortem_lock = threading.Lock()
        self._connect()

    def _connect(self):
        if not self._api_key:
            logger.info(
                "[Jarvis] GEMINI_API_KEY not set. "
                "Sentiment filter and post-mortems disabled. "
                "Get free key at aistudio.google.com/apikey"
            )
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
            logger.info(f"[Jarvis] Connected to Gemini ({self._model}) ✅")
        except ImportError:
            logger.warning(
                "[Jarvis] google-genai not installed. "
                "Run: pip install google-genai"
            )
        except Exception as e:
            logger.warning(f"[Jarvis] Gemini connection failed: {e}")

    def is_ready(self) -> bool:
        return self._client is not None

    # ─────────────────────────────────────────────────────────────────────────
    # SENTIMENT FILTER
    # ─────────────────────────────────────────────────────────────────────────

    def get_sentiment_override(self, symbol: str,
                                direction: str) -> dict:
        """
        Get Jarvis sentiment verdict for this symbol+direction combination.
        Returns override action and reasoning.

        Returns:
          action:    "PROCEED" / "HALF_SIZE" / "NO_TRADE"
          sentiment: "BULLISH" / "BEARISH" / "HAWKISH" / "DOVISH" /
                     "BLACK_SWAN" / "NEUTRAL"
          reason:    human-readable explanation
          source:    "llm" / "cache" / "disabled" / "error"
        """
        if not self.is_ready():
            return {
                "action":    "PROCEED",
                "sentiment": "NEUTRAL",
                "reason":    "Jarvis not configured — proceeding without filter",
                "source":    "disabled",
            }

        from config.config import get_asset_class
        asset_class = get_asset_class(symbol)

        # Check cache (avoid spamming API on every 5-min signal scan)
        cached = self._sentiment_cache.get(asset_class)
        if cached:
            age = (datetime.now(timezone.utc) - cached["ts"]).seconds / 60
            if age < _SENTIMENT_CACHE_MINS:
                return self._apply_sentiment_logic(
                    cached["sentiment"], direction, symbol,
                    source="cache",
                    headlines_used=cached.get("headlines_used", 0),
                )

        # Fetch fresh sentiment
        headlines = self._scrape_headlines(asset_class, symbol)
        sentiment = self._classify_sentiment(headlines, asset_class, symbol)

        # Cache it
        self._sentiment_cache[asset_class] = {
            "sentiment":     sentiment,
            "ts":            datetime.now(timezone.utc),
            "headlines_used": len(headlines),
        }

        return self._apply_sentiment_logic(
            sentiment, direction, symbol,
            source="llm",
            headlines_used=len(headlines),
        )

    def _scrape_headlines(self, asset_class: str, symbol: str) -> list:
        """
        Fetch top headlines from free news APIs.
        Uses multiple sources for resilience.
        """
        headlines = []

        # Source 1: NewsAPI.org (free tier: 100 req/day)
        news_key = os.environ.get("NEWS_API_KEY", "")
        if news_key:
            try:
                import requests
                # Map asset class to search terms
                query_map = {
                    "crypto":       "bitcoin cryptocurrency crypto market",
                    "forex":        "federal reserve currency forex interest rate",
                    "gold":         "gold price inflation dollar",
                    "stocks":       "stock market S&P 500 earnings",
                    "stocks_india": "nifty sensex RBI india market",
                    "indices":      "stock market economy federal reserve",
                }
                q = query_map.get(asset_class, "financial markets economy")

                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q":        q,
                        "sortBy":   "publishedAt",
                        "pageSize": 10,
                        "language": "en",
                        "apiKey":   news_key,
                    },
                    timeout=8,
                )
                if resp.status_code == 200:
                    articles = resp.json().get("articles", [])
                    headlines.extend([
                        a["title"] for a in articles if a.get("title")
                    ])
            except Exception as e:
                logger.debug(f"[Jarvis] NewsAPI failed: {e}")

        # Source 2: GNews API (free tier: 100 req/day)
        gnews_key = os.environ.get("GNEWS_API_KEY", "")
        if gnews_key and len(headlines) < 5:
            try:
                import requests
                resp = requests.get(
                    "https://gnews.io/api/v4/top-headlines",
                    params={
                        "topic":    "business",
                        "lang":     "en",
                        "max":      10,
                        "apikey":   gnews_key,
                    },
                    timeout=8,
                )
                if resp.status_code == 200:
                    articles = resp.json().get("articles", [])
                    headlines.extend([
                        a["title"] for a in articles if a.get("title")
                    ])
            except Exception as e:
                logger.debug(f"[Jarvis] GNews failed: {e}")

        # Source 3: RSS fallback (no API key needed)
        if len(headlines) < 5:
            try:
                import requests
                import xml.etree.ElementTree as ET

                feeds = {
                    "crypto": "https://cointelegraph.com/rss",
                    "forex":  "https://www.forexfactory.com/rss?ff=cal",
                    "gold":   "https://www.kitco.com/rss/news.xml",
                    "default":"https://feeds.reuters.com/reuters/businessNews",
                }
                url  = feeds.get(asset_class, feeds["default"])
                resp = requests.get(url, timeout=6,
                                     headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for item in root.iter("item"):
                        title = item.find("title")
                        if title is not None and title.text:
                            headlines.append(title.text.strip())
                        if len(headlines) >= 10:
                            break
            except Exception as e:
                logger.debug(f"[Jarvis] RSS fallback failed: {e}")

        logger.info(
            f"[Jarvis] Scraped {len(headlines)} headlines "
            f"for {asset_class}/{symbol}"
        )
        return headlines[:10]   # Cap at 10

    def _classify_sentiment(self, headlines: list,
                              asset_class: str,
                              symbol: str) -> str:
        """
        Send headlines to Gemini and get a structured sentiment label.
        Returns one of: BULLISH / BEARISH / HAWKISH / DOVISH /
                        BLACK_SWAN / NEUTRAL
        """
        if not headlines:
            return "NEUTRAL"

        headlines_text = "\n".join(f"- {h}" for h in headlines[:10])

        prompt = f"""You are a professional macro analyst for an algorithmic trading system.
Analyze these recent financial news headlines for {asset_class.upper()} assets (symbol: {symbol}).

HEADLINES:
{headlines_text}

Classify the overall market narrative into EXACTLY ONE of these labels:
- BULLISH: News suggests prices will rise (risk-on, positive data, dovish central bank)
- BEARISH: News suggests prices will fall (risk-off, negative data, recession fears)
- HAWKISH: Central bank language signals rate hikes / tightening (bad for risk assets)
- DOVISH: Central bank signals cuts / easing (good for risk assets)
- BLACK_SWAN: Extreme unexpected event (war, default, sudden political crisis)
- NEUTRAL: No clear directional bias in the headlines

Respond with ONLY a JSON object, nothing else:
{{"sentiment": "LABEL", "confidence": 0-100, "key_driver": "one sentence max"}}"""

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt
            )
            text     = response.text.strip()

            # Parse JSON response
            # Strip markdown code blocks if present
            if "```" in text:
                text = text.split("```")[1].replace("json", "").strip()

            data        = json.loads(text)
            sentiment   = data.get("sentiment", "NEUTRAL").upper()
            confidence  = data.get("confidence", 50)
            key_driver  = data.get("key_driver", "")

            # Validate
            valid = {"BULLISH", "BEARISH", "HAWKISH", "DOVISH",
                     "BLACK_SWAN", "NEUTRAL"}
            if sentiment not in valid:
                sentiment = "NEUTRAL"

            logger.info(
                f"[Jarvis] Sentiment: {sentiment} "
                f"(confidence={confidence}%) | {key_driver}"
            )
            return sentiment

        except Exception as e:
            logger.warning(f"[Jarvis] Sentiment classification failed: {e}")
            return "NEUTRAL"

    def _apply_sentiment_logic(self, sentiment: str,
                                direction: str,
                                symbol: str,
                                source: str = "llm",
                                headlines_used: int = 0) -> dict:
        """
        Map sentiment + trade direction to an action.

        LOGIC TABLE:
          BLACK_SWAN (any direction)     → NO_TRADE (4h freeze)
          HAWKISH + BUY crypto/gold      → HALF_SIZE (fight the narrative)
          HAWKISH + SELL crypto/gold     → PROCEED (aligned with narrative)
          BEARISH + BUY (any)            → HALF_SIZE
          BEARISH + SELL                 → PROCEED
          BULLISH + BUY                  → PROCEED
          BULLISH + SELL                 → HALF_SIZE
          DOVISH + BUY                   → PROCEED (bonus confirmation)
          NEUTRAL                        → PROCEED
        """
        from config.config import get_asset_class
        asset = get_asset_class(symbol)
        is_buy = direction.lower() in ("buy", "long")

        # BLACK SWAN — always veto
        if sentiment == "BLACK_SWAN":
            # Set a 4-hour freeze
            self._freeze_until = datetime.now(timezone.utc) + timedelta(hours=4)
            return {
                "action":    "NO_TRADE",
                "sentiment": sentiment,
                "reason":    (
                    "⚠️ BLACK SWAN detected in headlines. "
                    "All new entries frozen for 4 hours."
                ),
                "source":    source,
                "headlines": headlines_used,
            }

        # Check if freeze is still active (from a previous BLACK_SWAN)
        if hasattr(self, "_freeze_until"):
            if datetime.now(timezone.utc) < self._freeze_until:
                return {
                    "action":    "NO_TRADE",
                    "sentiment": "BLACK_SWAN",
                    "reason":    "Post-BLACK_SWAN freeze still active.",
                    "source":    "cache",
                }
            else:
                del self._freeze_until

        # Risk assets (crypto, gold) are sensitive to hawkish/dovish
        if asset in ("crypto", "gold", "silver"):
            if sentiment == "HAWKISH" and is_buy:
                return {
                    "action":    "HALF_SIZE",
                    "sentiment": sentiment,
                    "reason":    (
                        f"HAWKISH sentiment conflicts with {direction.upper()} on "
                        f"{symbol}. Reducing size — fighting the narrative."
                    ),
                    "source":    source,
                }
            if sentiment == "DOVISH" and is_buy:
                return {
                    "action":    "PROCEED",
                    "sentiment": sentiment,
                    "reason":    f"DOVISH confirms {direction.upper()} on {symbol}.",
                    "source":    source,
                }

        # General bearish/bullish
        if sentiment == "BEARISH" and is_buy:
            return {
                "action":    "HALF_SIZE",
                "sentiment": sentiment,
                "reason":    (
                    f"BEARISH narrative. {direction.upper()} on {symbol} "
                    f"goes against macro flow — half size."
                ),
                "source":    source,
            }
        if sentiment == "BULLISH" and not is_buy:
            return {
                "action":    "HALF_SIZE",
                "sentiment": sentiment,
                "reason":    (
                    f"BULLISH narrative. {direction.upper()} on {symbol} "
                    f"goes against macro flow — half size."
                ),
                "source":    source,
            }

        # Aligned or neutral — proceed normally
        return {
            "action":    "PROCEED",
            "sentiment": sentiment,
            "reason":    f"{sentiment} sentiment — no override needed.",
            "source":    source,
            "headlines": headlines_used,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # TRADE POST-MORTEM
    # ─────────────────────────────────────────────────────────────────────────

    def write_trade_postmortem(self, closed_trade: dict,
                                run_async: bool = True):
        """
        Write an automated 3-paragraph post-mortem after a trade closes.
        Runs in a background thread by default so it doesn't block trading.

        Output: Markdown file in State/postmortems/{trade_id}.md
                Telegram summary (truncated to 400 chars)
                Weekly pattern analysis if 10+ trades accumulated
        """
        if not self.is_ready():
            return

        if run_async:
            t = threading.Thread(
                target=self._write_postmortem_sync,
                args=(closed_trade,),
                daemon=True,
                name="JarvisPostmortem",
            )
            t.start()
        else:
            self._write_postmortem_sync(closed_trade)

    def _write_postmortem_sync(self, trade: dict):
        """Synchronous post-mortem writing — called from background thread."""
        with self._postmortem_lock:
            try:
                trade_id  = trade.get("trade_id", "UNKNOWN")
                symbol    = trade.get("symbol", "?")
                direction = trade.get("direction", "?")
                entry     = trade.get("entry_price", 0)
                exit_p    = trade.get("exit_price", 0)
                sl        = trade.get("stop_loss", 0) or trade.get("current_sl", 0)
                tp1       = trade.get("tp1_price", 0)
                tp2       = trade.get("current_tp", 0) or trade.get("original_tp2", 0)
                r         = trade.get("r_multiple", 0)
                outcome   = trade.get("outcome", "?")
                grade     = trade.get("grade", "?")
                hold_h    = trade.get("hold_duration_h", 0)
                open_t    = trade.get("open_time", "?")
                close_t   = trade.get("close_time", "?")

                # Get pattern data for context
                pattern_context = self._get_pattern_context(symbol)

                prompt = f"""You are a professional SMC (Smart Money Concepts) trading analyst 
and performance coach writing a trade journal entry.

TRADE DATA:
  Trade ID:    {trade_id}
  Symbol:      {symbol}
  Direction:   {direction.upper()}
  Grade:       {grade}
  Entry:       {entry}
  Stop Loss:   {sl}
  TP1 Target:  {tp1}
  TP2 Target:  {tp2}
  Exit Price:  {exit_p}
  Outcome:     {outcome}
  R Multiple:  {r:+.2f}R
  Hold Time:   {hold_h:.1f} hours
  Opened:      {open_t}
  Closed:      {close_t}

RECENT PATTERN DATA:
{pattern_context}

Write a professional 3-paragraph trade post-mortem in Markdown format:

**Paragraph 1 — EXECUTION REVIEW** (2-3 sentences):
Compare the plan (SL/TP levels, grade) to the actual execution. 
Was the entry at the right level? Did price respect the OB/FVG?

**Paragraph 2 — WHAT THE MARKET DID** (2-3 sentences):
Describe what actually happened after entry. Did price sweep liquidity 
before going to TP? Was the SL correctly placed? What SMC pattern played out?

**Paragraph 3 — LESSON & RECOMMENDATION** (2-3 sentences):
Extract one actionable lesson from this specific trade.
If pattern data shows a recurring issue (e.g., losing on Fridays), mention it.
Give one concrete recommendation to improve future trades.

Be specific, professional, and data-driven. Avoid generic platitudes.
Use the actual price levels and R-multiple in your analysis."""

                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt
                )
                analysis = response.text.strip()

                # Build full markdown document
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                markdown  = f"""# Trade Post-Mortem — {trade_id}
**Symbol:** {symbol} | **Direction:** {direction.upper()} | **Grade:** {grade}
**Result:** {r:+.2f}R ({outcome}) | **Hold:** {hold_h:.1f}h
**Generated:** {timestamp}

---

{analysis}

---

## Raw Trade Data
| Field | Value |
|-------|-------|
| Entry | {entry} |
| Exit  | {exit_p} |
| SL    | {sl} |
| TP1   | {tp1} |
| TP2   | {tp2} |
| Opened | {open_t} |
| Closed | {close_t} |
"""
                # Save to file
                filepath = _POSTMORTEM_DIR / f"{trade_id}.md"
                filepath.write_text(markdown)
                logger.info(f"[Jarvis] Post-mortem saved: {filepath}")

                # Send summary to Telegram (first 400 chars)
                try:
                    from interfaces.telegram_interface import tg
                    summary_lines = [
                        l for l in analysis.split("\n")
                        if l.strip() and not l.startswith("#")
                    ]
                    summary = " ".join(summary_lines)[:380]
                    result_emoji = "✅" if r > 0 else "❌"
                    tg.send(
                        f"{result_emoji} <b>JARVIS POST-MORTEM: "
                        f"{symbol} {r:+.2f}R</b>\n\n"
                        f"{summary}...\n\n"
                        f"<i>Full analysis: State/postmortems/{trade_id}.md</i>"
                    )
                except Exception:
                    pass

                # Run pattern analysis every 10 trades
                self._maybe_run_pattern_analysis()

            except Exception as e:
                logger.error(f"[Jarvis] Post-mortem failed: {e}")

    def _get_pattern_context(self, symbol: str) -> str:
        """Build a summary of recent trade patterns for context."""
        try:
            from core.database_manager import db
            trades = db.get_recent_trades(limit=50)
            if not trades:
                return "No pattern data yet."

            # Day of week win rates
            from collections import defaultdict
            day_results: dict = defaultdict(list)
            for t in trades:
                try:
                    ct  = datetime.fromisoformat(t.get("close_time", ""))
                    day = ct.strftime("%A")
                    day_results[day].append(float(t.get("r_multiple", 0)))
                except Exception:
                    continue

            lines = []
            for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
                rs = day_results.get(day, [])
                if len(rs) >= 3:
                    wr  = sum(1 for r in rs if r > 0) / len(rs) * 100
                    avg = sum(rs) / len(rs)
                    lines.append(
                        f"  {day}: {len(rs)} trades, "
                        f"WR={wr:.0f}%, avg={avg:+.2f}R"
                    )

            return "Day-of-week pattern:\n" + "\n".join(lines) if lines else "Insufficient data."
        except Exception:
            return "Pattern analysis unavailable."

    def _maybe_run_pattern_analysis(self):
        """Every 10th trade: run full pattern analysis and send to Telegram."""
        try:
            files = list(_POSTMORTEM_DIR.glob("*.md"))
            if len(files) % 10 != 0 or len(files) == 0:
                return

            from core.database_manager import db
            trades = db.get_recent_trades(limit=100)
            if len(trades) < 10:
                return

            # Build pattern summary to send to Gemini
            from collections import defaultdict
            day_stats:     dict = defaultdict(list)
            session_stats: dict = defaultdict(list)
            grade_stats:   dict = defaultdict(list)
            symbol_stats:  dict = defaultdict(list)

            for t in trades:
                r = float(t.get("r_multiple", 0) or 0)
                try:
                    ct  = datetime.fromisoformat(t.get("close_time", ""))
                    day = ct.strftime("%A")
                    day_stats[day].append(r)
                except Exception:
                    pass
                session_stats[t.get("session", "?")].append(r)
                grade_stats[t.get("grade", "?")].append(r)
                symbol_stats[t.get("symbol", "?")].append(r)

            def summarise(d: dict) -> str:
                lines = []
                for k, rs in sorted(d.items()):
                    if not rs:
                        continue
                    wr  = sum(1 for r in rs if r > 0) / len(rs) * 100
                    avg = sum(rs) / len(rs)
                    lines.append(f"  {k}: n={len(rs)}, WR={wr:.0f}%, E={avg:+.2f}R")
                return "\n".join(lines)

            prompt = f"""You are a quantitative performance analyst reviewing 
an algorithmic trading bot's recent {len(trades)} trades.

DAY-OF-WEEK STATS:
{summarise(day_stats)}

SESSION STATS:
{summarise(session_stats)}

GRADE STATS:
{summarise(grade_stats)}

SYMBOL STATS:
{summarise(symbol_stats)}

Identify the top 3 actionable improvements. Format as bullet points.
Focus on: which days/sessions/grades to disable, which symbols to focus on.
Be specific with thresholds (e.g., "Disable B-grade trades if WR < 45%").
Max 200 words."""

            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt
            )
            analysis = response.text.strip()

            try:
                from interfaces.telegram_interface import tg
                tg.send(
                    f"📊 <b>JARVIS PATTERN ANALYSIS ({len(trades)} trades)</b>\n\n"
                    f"{analysis}"
                )
            except Exception:
                pass

            logger.info("[Jarvis] Pattern analysis complete and sent.")

        except Exception as e:
            logger.debug(f"[Jarvis] Pattern analysis failed: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
_jarvis: Optional[JarvisLLM] = None

def get_jarvis() -> JarvisLLM:
    global _jarvis
    if _jarvis is None:
        _jarvis = JarvisLLM()
    return _jarvis
