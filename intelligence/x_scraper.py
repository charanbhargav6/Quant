"""
CRAVE v12 — X/Twitter & Influential Account Monitor
=====================================================
Monitors high-signal accounts that move markets when they post.

MONITORED ACCOUNTS (market-moving):
  - Donald Trump (@realDonaldTrump) — tariffs, trade wars, crypto
  - Federal Reserve (@federalreserve) — rate policy
  - Jerome Powell (speech transcripts)
  - Elon Musk (@elonmusk) — crypto (DOGE, BTC)
  - Michael Saylor (@saylor) — BTC
  - CoinDesk (@CoinDesk) — crypto breaking news
  - Neel Kashkari, Raphael Bostic, etc. — Fed officials

STRATEGY:
  - No X API required — uses Nitter public instances + RSS feeds
  - Nitter is a free, open-source Twitter frontend with RSS support
  - Multiple Nitter instances tried in rotation (failover)
  - Also checks official press release RSS feeds

OUTPUT:
  {
    "account": "@realDonaldTrump",
    "text":    "...",
    "timestamp": ISO,
    "sentiment": "bullish"|"bearish"|"neutral",
    "assets": ["BTCUSDT", "EURUSD=X", ...],
    "urgency": "immediate"|"watch"|"low",
    "signal_strength": float  # 0-1
  }
"""

import os
import logging
import time
import threading
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger("crave.x_scraper")

# ─────────────────────────────────────────────────────────────────────────────
# NITTER INSTANCES (public, free, rotate on failure)
# ─────────────────────────────────────────────────────────────────────────────

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.cz",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]

# ─────────────────────────────────────────────────────────────────────────────
# MONITORED ACCOUNTS & THEIR ASSET RELEVANCE
# ─────────────────────────────────────────────────────────────────────────────

MONITORED_ACCOUNTS = {
    # Politics / macro
    "realDonaldTrump": {
        "name": "Donald Trump",
        "assets": ["BTCUSDT", "EURUSD=X", "XAUUSD=X", "SPY"],
        "keywords": {
            "tariff":     {"assets": ["EURUSD=X", "USDJPY=X"], "sentiment_bias": "bearish_usd"},
            "china":      {"assets": ["AUDUSD=X"], "sentiment_bias": "bearish"},
            "bitcoin":    {"assets": ["BTCUSDT", "ETHUSDT"], "sentiment_bias": "bullish"},
            "crypto":     {"assets": ["BTCUSDT", "ETHUSDT"], "sentiment_bias": "bullish"},
            "deal":       {"assets": ["EURUSD=X", "SPY"], "sentiment_bias": "bullish"},
            "war":        {"assets": ["XAUUSD=X"], "sentiment_bias": "bullish_gold"},
            "sanction":   {"assets": ["XAUUSD=X", "EURUSD=X"], "sentiment_bias": "bearish"},
            "trade":      {"assets": ["EURUSD=X", "USDJPY=X"], "sentiment_bias": "mixed"},
        },
    },
    "elonmusk": {
        "name": "Elon Musk",
        "assets": ["BTCUSDT", "ETHUSDT"],
        "keywords": {
            "doge":    {"assets": ["BTCUSDT"], "sentiment_bias": "bullish"},
            "bitcoin": {"assets": ["BTCUSDT"], "sentiment_bias": "bullish"},
            "btc":     {"assets": ["BTCUSDT"], "sentiment_bias": "bullish"},
            "crypto":  {"assets": ["BTCUSDT", "ETHUSDT"], "sentiment_bias": "bullish"},
        },
    },
    "saylor": {
        "name": "Michael Saylor",
        "assets": ["BTCUSDT"],
        "keywords": {
            "bitcoin": {"assets": ["BTCUSDT"], "sentiment_bias": "bullish"},
            "buy":     {"assets": ["BTCUSDT"], "sentiment_bias": "bullish"},
            "usd":     {"assets": ["BTCUSDT"], "sentiment_bias": "bearish_usd"},
        },
    },
    "federalreserve": {
        "name": "Federal Reserve",
        "assets": ["EURUSD=X", "XAUUSD=X", "BTCUSDT", "SPY"],
        "keywords": {
            "rate":    {"assets": ["EURUSD=X", "XAUUSD=X"], "sentiment_bias": "hawkish"},
            "inflation": {"assets": ["XAUUSD=X"], "sentiment_bias": "bullish_gold"},
            "taper":   {"assets": ["SPY"], "sentiment_bias": "bearish"},
        },
    },
}

# Official press release / speech RSS (no API needed)
OFFICIAL_RSS = {
    "Fed Speeches":      "https://www.federalreserve.gov/feeds/speeches.xml",
    "Fed Press Release": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB":               "https://www.ecb.europa.eu/rss/pr.html",
    "BOE":               "https://www.bankofengland.co.uk/rss/news",
}


class XScraper:
    """
    Monitor influential accounts via Nitter RSS + official feeds.
    Produces market-moving signal events.
    """

    REFRESH_INTERVAL = 120   # 2 minutes — X moves fast
    MAX_POST_AGE_H   = 6     # Only surface posts from last 6 hours

    def __init__(self):
        self._cache: List[Dict] = []
        self._seen_ids: set = set()
        self._lock = threading.Lock()
        self._running = False
        self._nitter_idx = 0

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        t = threading.Thread(
            target=self._refresh_loop, daemon=True, name="XScraper"
        )
        t.start()
        logger.info("[XScraper] Started — monitoring influential accounts")

    def stop(self):
        self._running = False

    def get_recent_signals(self, max_age_mins: int = 30) -> List[Dict]:
        """Return market-moving posts from last N minutes."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_mins)
        with self._lock:
            return [
                s for s in self._cache
                if self._parse_ts(s.get("timestamp", "")) > cutoff
                and s.get("urgency") in ("immediate", "watch")
            ]

    def get_asset_signals(self, crave_symbol: str,
                           max_age_mins: int = 60) -> List[Dict]:
        """Return signals relevant to a specific asset."""
        recent = self.get_recent_signals(max_age_mins)
        return [s for s in recent if crave_symbol in s.get("assets", [])]

    # ─────────────────────────────────────────────────────────────────────────
    # BACKGROUND LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_loop(self):
        self._do_refresh()
        while self._running:
            time.sleep(self.REFRESH_INTERVAL)
            self._do_refresh()

    def _do_refresh(self):
        try:
            new_signals = []
            new_signals.extend(self._fetch_nitter_rss())
            new_signals.extend(self._fetch_official_rss())

            with self._lock:
                for sig in new_signals:
                    uid = sig.get("id", sig.get("text", "")[:40])
                    if uid not in self._seen_ids:
                        self._seen_ids.add(uid)
                        self._cache.append(sig)

                # Prune old signals
                cutoff = datetime.now(timezone.utc) - timedelta(
                    hours=self.MAX_POST_AGE_H
                )
                self._cache = [
                    s for s in self._cache
                    if self._parse_ts(s.get("timestamp", "")) > cutoff
                ]

            if new_signals:
                logger.info(f"[XScraper] {len(new_signals)} new signals fetched")

        except Exception as e:
            logger.error(f"[XScraper] Refresh error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # NITTER RSS (rotating instances)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_nitter_rss(self) -> List[Dict]:
        results = []
        for username, config in MONITORED_ACCOUNTS.items():
            for attempt in range(len(NITTER_INSTANCES)):
                instance = NITTER_INSTANCES[self._nitter_idx % len(NITTER_INSTANCES)]
                url = f"{instance}/{username}/rss"
                try:
                    feed = feedparser.parse(url)
                    if not feed.entries:
                        self._nitter_idx += 1
                        continue

                    for entry in feed.entries[:5]:
                        text = entry.get("title", "") or entry.get("summary", "")
                        pub  = entry.get("published", "")
                        link = entry.get("link", "")
                        uid  = entry.get("id", link)

                        sig = self._analyze_post(
                            username, config, text, pub, link, uid
                        )
                        if sig:
                            results.append(sig)
                    break  # success

                except Exception:
                    self._nitter_idx += 1
                    continue

        return results

    # ─────────────────────────────────────────────────────────────────────────
    # OFFICIAL INSTITUTION RSS
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_official_rss(self) -> List[Dict]:
        results = []
        for source_name, url in OFFICIAL_RSS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    text = f"{entry.get('title','')} {entry.get('summary','')}"
                    pub  = entry.get("published", "")
                    link = entry.get("link", "")

                    text_lower = text.lower()
                    # Only include if it mentions rate/inflation/monetary
                    if not any(kw in text_lower for kw in [
                        "rate", "inflation", "monetary", "policy",
                        "interest", "quantitative", "taper"
                    ]):
                        continue

                    results.append({
                        "account":         source_name,
                        "text":            text[:300],
                        "timestamp":       self._normalize_ts(pub),
                        "url":             link,
                        "sentiment":       self._basic_sentiment(text_lower),
                        "assets":          ["EURUSD=X", "XAUUSD=X", "BTCUSDT", "SPY"],
                        "urgency":         "watch",
                        "signal_strength": 0.6,
                        "id":              link,
                    })
            except Exception as e:
                logger.debug(f"[XScraper] {source_name} RSS failed: {e}")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def _analyze_post(self, username: str, config: dict, text: str,
                       pub: str, link: str, uid: str) -> Optional[Dict]:
        """Analyze a post from an influential account and produce a signal."""
        text_lower = text.lower()
        matched_assets = set(config.get("assets", []))
        matched_keywords = []
        sentiment_votes = []

        for kw, kw_config in config.get("keywords", {}).items():
            if kw in text_lower:
                matched_keywords.append(kw)
                matched_assets.update(kw_config.get("assets", []))
                bias = kw_config.get("sentiment_bias", "")
                if "bullish" in bias:
                    sentiment_votes.append(1)
                elif "bearish" in bias:
                    sentiment_votes.append(-1)
                elif "hawkish" in bias:
                    sentiment_votes.append(-0.5)

        if not matched_keywords:
            # Post doesn't match any tracked keyword — low urgency
            return None

        avg_vote = sum(sentiment_votes) / len(sentiment_votes) if sentiment_votes else 0
        sentiment = "bullish" if avg_vote > 0.2 else "bearish" if avg_vote < -0.2 else "neutral"
        signal_strength = min(len(matched_keywords) * 0.25, 1.0)
        urgency = "immediate" if signal_strength >= 0.5 else "watch"

        return {
            "account":         f"@{username}",
            "name":            config.get("name", username),
            "text":            text[:300],
            "timestamp":       self._normalize_ts(pub),
            "url":             link,
            "sentiment":       sentiment,
            "assets":          list(matched_assets),
            "urgency":         urgency,
            "signal_strength": round(signal_strength, 2),
            "keywords_hit":    matched_keywords,
            "id":              uid,
        }

    def _basic_sentiment(self, text: str) -> str:
        bullish = ["cut", "dovish", "stimulus", "support", "positive"]
        bearish = ["hike", "hawkish", "tighten", "concern", "risk"]
        b = sum(1 for kw in bullish if kw in text)
        s = sum(1 for kw in bearish if kw in text)
        return "bullish" if b > s else "bearish" if s > b else "neutral"

    def _normalize_ts(self, ts: str) -> str:
        if not ts:
            return datetime.now(timezone.utc).isoformat()
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(ts).astimezone(timezone.utc).isoformat()
        except Exception:
            return ts

    def _parse_ts(self, ts: str) -> datetime:
        if not ts:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


# ── Singleton ──────────────────────────────────────────────────────────────
_scraper: Optional[XScraper] = None

def get_x_scraper() -> XScraper:
    global _scraper
    if _scraper is None:
        _scraper = XScraper()
    return _scraper
