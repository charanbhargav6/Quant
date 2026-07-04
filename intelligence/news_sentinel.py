"""
CRAVE v12 — News Sentinel (Hybrid Multi-Source Scraper)
==========================================================
Pulls financial news from 4 free sources simultaneously:
  1. NewsAPI.org        — structured financial news (free 100 req/day)
  2. GDELT GKG API     — global event database, completely free, no key
  3. ForexFactory RSS  — economic calendar red/orange/yellow events
  4. GNews API         — Google News aggregator (free 100 req/day)
  5. RSS fallback      — Reuters, FT, Bloomberg RSS (no key needed)

OUTPUT per news item:
  {
    "headline":   str,
    "source":     str,
    "published":  ISO timestamp,
    "url":        str,
    "sentiment":  "bullish" | "bearish" | "neutral",
    "assets":     ["XAUUSD", "EURUSD", ...],
    "impact":     "high" | "medium" | "low",
    "event_type": "economic" | "geopolitical" | "earnings" | "fed" | "breaking"
  }

CALENDAR EVENTS (ForexFactory):
  {
    "event":    "NFP",
    "currency": "USD",
    "impact":   "high",       # red folder
    "time_utc": ISO timestamp,
    "actual":   "...",        # filled after release
    "forecast": "...",
    "previous": "...",
  }
"""

import os
import json
import logging
import time
import threading
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from pathlib import Path

logger = logging.getLogger("crave.news_sentinel")

# ─────────────────────────────────────────────────────────────────────────────
# ASSET KEYWORD MAP — which assets does a headline affect?
# ─────────────────────────────────────────────────────────────────────────────

ASSET_KEYWORDS = {
    "XAUUSD=X": ["gold", "xau", "bullion", "precious metal", "haven", "safe haven"],
    "EURUSD=X": ["euro", "eur", "ecb", "european central bank", "eurozone", "lagarde"],
    "GBPUSD=X": ["pound", "sterling", "gbp", "boe", "bank of england", "bailey"],
    "USDJPY=X": ["yen", "jpy", "boj", "bank of japan", "ueda", "japan"],
    "AUDUSD=X": ["aussie", "aud", "rba", "australia", "china gdp", "iron ore"],
    "BTCUSDT":  ["bitcoin", "btc", "crypto", "cryptocurrency", "coinbase", "etf"],
    "ETHUSDT":  ["ethereum", "eth", "defi", "smart contract"],
    "SPY":      ["s&p", "sp500", "nasdaq", "dow", "fed", "fomc", "rate", "cpi", "nfp",
                 "inflation", "recession", "gdp", "earnings"],
    "USD":      ["fed", "federal reserve", "powell", "fomc", "dollar", "dxy",
                 "rate hike", "rate cut", "nfp", "cpi", "pce", "inflation", "gdp"],
}

SENTIMENT_BULLISH = [
    "rate cut", "dovish", "stimulus", "bullish", "rally", "surge", "beat",
    "strong", "growth", "positive", "recovery", "breakout", "buy", "upgrade",
    "higher", "gain", "support", "safe haven demand", "risk on",
]
SENTIMENT_BEARISH = [
    "rate hike", "hawkish", "recession", "bearish", "crash", "plunge", "miss",
    "weak", "slowdown", "negative", "selloff", "downgrade", "lower", "loss",
    "risk off", "safe haven flight", "war", "conflict", "sanction", "default",
]

# ─────────────────────────────────────────────────────────────────────────────
# RSS FEED SOURCES (no API key required)
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "Reuters Markets":    "https://feeds.reuters.com/reuters/businessNews",
    "Reuters Economy":    "https://feeds.reuters.com/reuters/UKdomesticNews",
    "Investing.com Gold": "https://www.investing.com/rss/news_25.rss",
    "Investing.com Forex":"https://www.investing.com/rss/news_1.rss",
    "FXStreet":           "https://www.fxstreet.com/rss/news",
    "MarketWatch":        "https://feeds.marketwatch.com/marketwatch/topstories/",
    "Yahoo Finance":      "https://finance.yahoo.com/rss/",
    "Kitco Gold":         "https://www.kitco.com/rss/news/kitco-news.rss",
}

# ForexFactory event calendar (parses HTML — returns structured events)
FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ─────────────────────────────────────────────────────────────────────────────
# MAIN CLASS
# ─────────────────────────────────────────────────────────────────────────────

class NewsSentinel:
    """
    Hybrid multi-source financial news scraper.
    Runs in background, caches results, exposes clean API to trading loop.
    """

    REFRESH_INTERVAL = 300   # 5 minutes between full refreshes
    CACHE_TTL        = 3600  # Headlines older than 1h are dropped

    def __init__(self):
        self._newsapi_key = os.environ.get("NEWSAPI_KEY", "")
        self._gnews_key   = os.environ.get("GNEWS_KEY", "")
        self._cache: List[Dict] = []
        self._calendar: List[Dict] = []
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        """Start background refresh loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="NewsSentinel"
        )
        self._thread.start()
        logger.info("[NewsSentinel] Started — refreshing every 5 minutes")

    def stop(self):
        self._running = False

    def get_recent_news(self, max_age_mins: int = 60) -> List[Dict]:
        """Return news from last N minutes, sorted newest first."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_mins)
        with self._lock:
            return [
                n for n in self._cache
                if self._parse_ts(n.get("published", "")) > cutoff
            ]

    def get_asset_sentiment(self, crave_symbol: str,
                             max_age_mins: int = 60) -> Dict:
        """
        Get aggregated sentiment for a specific asset.
        Returns: {"sentiment": "bullish"|"bearish"|"neutral", "score": float,
                  "headlines": [...], "impact": "high"|"medium"|"low"}
        """
        news = self.get_recent_news(max_age_mins)
        relevant = [n for n in news if crave_symbol in n.get("assets", [])]

        if not relevant:
            return {"sentiment": "neutral", "score": 0.0,
                    "headlines": [], "impact": "low"}

        scores = []
        for item in relevant:
            s = item.get("sentiment", "neutral")
            w = 2.0 if item.get("impact") == "high" else 1.0
            if s == "bullish":
                scores.append(+w)
            elif s == "bearish":
                scores.append(-w)
            else:
                scores.append(0.0)

        avg = sum(scores) / len(scores) if scores else 0.0
        max_impact = "high" if any(
            n.get("impact") == "high" for n in relevant
        ) else "medium" if any(
            n.get("impact") == "medium" for n in relevant
        ) else "low"

        if avg > 0.5:
            sentiment = "bullish"
        elif avg < -0.5:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        return {
            "sentiment": sentiment,
            "score":     round(avg, 2),
            "headlines": [n["headline"] for n in relevant[:5]],
            "impact":    max_impact,
            "count":     len(relevant),
        }

    def get_upcoming_events(self, hours_ahead: int = 4) -> List[Dict]:
        """Return red/orange folder events in the next N hours."""
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours_ahead)
        with self._lock:
            return [
                e for e in self._calendar
                if e.get("impact") in ("high", "medium")
                and now <= self._parse_ts(e.get("time_utc", "")) <= horizon
            ]

    def get_red_folder_events(self, hours_ahead: int = 1) -> List[Dict]:
        """Return ONLY high-impact (red folder) events in the next N hours."""
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours_ahead)
        with self._lock:
            return [
                e for e in self._calendar
                if e.get("impact") == "high"
                and now <= self._parse_ts(e.get("time_utc", "")) <= horizon
            ]

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND REFRESH
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_loop(self):
        # Initial fetch immediately
        self._do_refresh()
        while self._running:
            time.sleep(self.REFRESH_INTERVAL)
            self._do_refresh()

    def _do_refresh(self):
        try:
            all_news = []
            all_news.extend(self._fetch_rss())
            all_news.extend(self._fetch_newsapi())
            all_news.extend(self._fetch_gdelt())
            all_news.extend(self._fetch_gnews())

            # Deduplicate by headline similarity
            seen_headlines = set()
            unique = []
            for item in all_news:
                key = item["headline"][:60].lower()
                if key not in seen_headlines:
                    seen_headlines.add(key)
                    unique.append(item)

            # Sort newest first
            unique.sort(key=lambda x: x.get("published", ""), reverse=True)

            calendar = self._fetch_forex_factory()

            with self._lock:
                self._cache    = unique
                self._calendar = calendar

            logger.info(
                f"[NewsSentinel] Refreshed: {len(unique)} headlines, "
                f"{len(calendar)} calendar events"
            )
            self._last_refresh = time.time()

        except Exception as e:
            logger.error(f"[NewsSentinel] Refresh error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE 1: RSS FEEDS (no key needed)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_rss(self) -> List[Dict]:
        results = []
        for source_name, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:10]:
                    headline = entry.get("title", "")
                    published = entry.get("published", "") or entry.get("updated", "")
                    link = entry.get("link", "")
                    summary = entry.get("summary", "")
                    full_text = f"{headline} {summary}".lower()

                    item = {
                        "headline":   headline,
                        "source":     source_name,
                        "published":  self._normalize_ts(published),
                        "url":        link,
                        "sentiment":  self._classify_sentiment(full_text),
                        "assets":     self._classify_assets(full_text),
                        "impact":     self._classify_impact(full_text),
                        "event_type": self._classify_event_type(full_text),
                    }
                    results.append(item)
            except Exception as e:
                logger.debug(f"[NewsSentinel] RSS {source_name} failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE 2: NewsAPI.org (100 req/day free)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_newsapi(self) -> List[Dict]:
        if not self._newsapi_key:
            return []
        results = []
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q":        "forex OR gold OR bitcoin OR fed OR inflation",
                "language": "en",
                "sortBy":   "publishedAt",
                "pageSize": 20,
                "apiKey":   self._newsapi_key,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                for art in resp.json().get("articles", []):
                    full_text = f"{art.get('title','')} {art.get('description','')}".lower()
                    results.append({
                        "headline":   art.get("title", ""),
                        "source":     f"NewsAPI/{art.get('source',{}).get('name','')}",
                        "published":  art.get("publishedAt", ""),
                        "url":        art.get("url", ""),
                        "sentiment":  self._classify_sentiment(full_text),
                        "assets":     self._classify_assets(full_text),
                        "impact":     self._classify_impact(full_text),
                        "event_type": self._classify_event_type(full_text),
                    })
        except Exception as e:
            logger.debug(f"[NewsSentinel] NewsAPI failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE 3: GDELT GKG (completely free, no key)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_gdelt(self) -> List[Dict]:
        results = []
        try:
            # GDELT DOC API — search last 24h for financial themes
            url = "https://api.gdeltproject.org/api/v2/doc/doc"
            params = {
                "query":      "fed OR gold OR bitcoin OR forex OR inflation",
                "mode":       "artlist",
                "maxrecords": 20,
                "format":     "json",
                "timespan":   "2h",
                "sort":       "datedesc",
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for art in data.get("articles", []):
                    full_text = f"{art.get('title', '')} {art.get('url', '')}".lower()
                    results.append({
                        "headline":   art.get("title", ""),
                        "source":     f"GDELT/{art.get('domain', '')}",
                        "published":  art.get("seendate", ""),
                        "url":        art.get("url", ""),
                        "sentiment":  self._classify_sentiment(full_text),
                        "assets":     self._classify_assets(full_text),
                        "impact":     self._classify_impact(full_text),
                        "event_type": self._classify_event_type(full_text),
                    })
        except Exception as e:
            logger.debug(f"[NewsSentinel] GDELT failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE 4: GNews API (100 req/day free)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_gnews(self) -> List[Dict]:
        if not self._gnews_key:
            return []
        results = []
        try:
            url = "https://gnews.io/api/v4/search"
            params = {
                "q":        "forex gold bitcoin federal reserve inflation",
                "lang":     "en",
                "sortby":   "publishedAt",
                "max":      10,
                "token":    self._gnews_key,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                for art in resp.json().get("articles", []):
                    full_text = f"{art.get('title','')} {art.get('description','')}".lower()
                    results.append({
                        "headline":   art.get("title", ""),
                        "source":     f"GNews/{art.get('source',{}).get('name','')}",
                        "published":  art.get("publishedAt", ""),
                        "url":        art.get("url", ""),
                        "sentiment":  self._classify_sentiment(full_text),
                        "assets":     self._classify_assets(full_text),
                        "impact":     self._classify_impact(full_text),
                        "event_type": self._classify_event_type(full_text),
                    })
        except Exception as e:
            logger.debug(f"[NewsSentinel] GNews failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SOURCE 5: ForexFactory Economic Calendar (free JSON feed)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_forex_factory(self) -> List[Dict]:
        results = []
        try:
            resp = requests.get(FOREXFACTORY_URL, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                for event in resp.json():
                    impact_raw = event.get("impact", "").lower()
                    if impact_raw == "high":
                        impact = "high"
                    elif impact_raw == "medium":
                        impact = "medium"
                    else:
                        impact = "low"

                    # Parse event time (ForexFactory gives local NY time)
                    time_str = event.get("date", "") + " " + event.get("time", "")
                    results.append({
                        "event":    event.get("title", ""),
                        "currency": event.get("country", ""),
                        "impact":   impact,
                        "time_utc": self._ff_time_to_utc(time_str),
                        "forecast": event.get("forecast", ""),
                        "previous": event.get("previous", ""),
                        "actual":   event.get("actual", ""),
                    })
        except Exception as e:
            logger.debug(f"[NewsSentinel] ForexFactory calendar failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # CLASSIFIERS
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_sentiment(self, text: str) -> str:
        bull_score = sum(1 for kw in SENTIMENT_BULLISH if kw in text)
        bear_score = sum(1 for kw in SENTIMENT_BEARISH if kw in text)
        if bull_score > bear_score:
            return "bullish"
        elif bear_score > bull_score:
            return "bearish"
        return "neutral"

    def _classify_assets(self, text: str) -> List[str]:
        matched = []
        for asset, keywords in ASSET_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                matched.append(asset)
        return matched if matched else ["SPY"]  # default to market-wide

    def _classify_impact(self, text: str) -> str:
        high_kw = ["fed", "fomc", "nfp", "cpi", "gdp", "rate decision",
                   "war", "crisis", "crash", "emergency", "black swan"]
        med_kw  = ["pmi", "retail sales", "unemployment", "trade", "earnings"]
        if any(kw in text for kw in high_kw):
            return "high"
        if any(kw in text for kw in med_kw):
            return "medium"
        return "low"

    def _classify_event_type(self, text: str) -> str:
        if any(kw in text for kw in ["fed", "fomc", "rate", "powell", "central bank"]):
            return "fed"
        if any(kw in text for kw in ["nfp", "cpi", "gdp", "pmi", "retail"]):
            return "economic"
        if any(kw in text for kw in ["war", "sanction", "election", "geopolit"]):
            return "geopolitical"
        if any(kw in text for kw in ["earnings", "revenue", "profit", "eps"]):
            return "earnings"
        return "breaking"

    # ─────────────────────────────────────────────────────────────────────────
    # UTILITIES
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize_ts(self, ts_str: str) -> str:
        """Convert various timestamp formats to ISO UTC string."""
        if not ts_str:
            return datetime.now(timezone.utc).isoformat()
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(ts_str)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return ts_str

    def _parse_ts(self, ts_str: str) -> datetime:
        """Parse ISO timestamp string to timezone-aware datetime."""
        if not ts_str:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    def _ff_time_to_utc(self, time_str: str) -> str:
        """Convert ForexFactory NY time to UTC ISO string."""
        try:
            # ForexFactory times are US Eastern (UTC-5 or UTC-4 DST)
            dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %I:%M%p")
            # Approximate: assume EST (UTC-5) — good enough for planning
            dt_utc = dt + timedelta(hours=5)
            return dt_utc.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()


# ── Singleton ──────────────────────────────────────────────────────────────
_sentinel: Optional[NewsSentinel] = None

def get_sentinel() -> NewsSentinel:
    global _sentinel
    if _sentinel is None:
        _sentinel = NewsSentinel()
    return _sentinel
