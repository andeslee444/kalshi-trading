"""Macro/geopolitics sentiment engine for CPI/GDP/Jobs bias adjustments.

Fetches data from FRED API, Truflation, and curated blog/RSS feeds.
Produces MacroSignal with quantified bias adjustments for economics bot.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict

import feedparser
import requests

from kalshi_auth import retry_request, _atomic_write_json, PROJECT_DIR

_log = logging.getLogger("macro_engine")


@dataclass
class MacroSignal:
    """Aggregated macro signal with CPI bias and confidence."""
    cpi_bias: float = 0.0          # +/- adjustment to CPI nowcast (pp)
    confidence: float = 0.0         # 0-1, how confident we are in the bias
    tips_breakeven: Optional[float] = None
    umich_expectations: Optional[float] = None
    gdpnow: Optional[float] = None
    truflation_cpi: Optional[float] = None
    sentiment_score: float = 0.0    # -1 to +1 from blog/RSS
    sources_available: int = 0
    sources_total: int = 6
    timestamp: str = ""


class FREDClient:
    """Fetch economic data from FRED API (free, no auth required)."""

    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    # FRED series IDs
    SERIES = {
        "tips_breakeven_10y": "T10YIE",       # 10-Year Breakeven Inflation Rate
        "tips_breakeven_5y": "T5YIE",         # 5-Year Breakeven Inflation Rate
        "umich_expectations": "MICH",          # UMich Inflation Expectations
        "gdpnow": "GDPNOW",                   # Atlanta Fed GDPNow
    }

    def __init__(self, api_key: str = ""):
        # FRED allows limited requests without a key, but key is recommended
        # For now, use the free tier (no key needed for basic series)
        self.api_key = api_key

    def _parse_observation(self, data: dict) -> Optional[float]:
        """Parse the latest observation value from a FRED API response."""
        observations = data.get("observations", [])
        if not observations:
            return None
        latest = observations[-1]
        value_str = latest.get("value", ".")
        if value_str == "." or not value_str:
            return None
        try:
            return float(value_str)
        except (ValueError, TypeError):
            return None

    def fetch_series(self, series_key: str) -> Optional[float]:
        """Fetch the latest value for a FRED series.

        Args:
            series_key: Key from SERIES dict (e.g. "tips_breakeven_10y").

        Returns:
            Latest value as float, or None on error.
        """
        series_id = self.SERIES.get(series_key)
        if not series_id:
            _log.warning("Unknown FRED series key: %s", series_key)
            return None

        params = {
            "series_id": series_id,
            "sort_order": "desc",
            "limit": "1",
            "file_type": "json",
        }
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            resp = retry_request("GET", self.BASE_URL, params=params, timeout=15)
            data = resp.json()
            value = self._parse_observation(data)
            if value is not None:
                _log.info("  FRED %s (%s): %.3f", series_key, series_id, value)
            return value
        except Exception as e:
            _log.error("  FRED fetch failed for %s: %s", series_key, e)
            return None

    def fetch_all(self) -> Dict[str, float]:
        """Fetch all configured FRED series. Returns {key: value} dict."""
        results = {}
        for key in self.SERIES:
            value = self.fetch_series(key)
            if value is not None:
                results[key] = value
        return results


class TruflationClient:
    """Fetch real-time CPI from Truflation public API."""

    BASE_URL = "https://truflation.com/api/data"

    def _parse_cpi(self, data: dict) -> Optional[float]:
        """Parse CPI YoY from Truflation response."""
        value = data.get("currentCpiYoY")
        if value is None:
            return None
        try:
            v = float(value)
            if 0.0 < v < 20.0:
                return v
            return None
        except (ValueError, TypeError):
            return None

    def fetch(self) -> Optional[float]:
        """Fetch current real-time CPI estimate.

        Returns CPI YoY as percentage (e.g. 2.75), or None on error.
        """
        try:
            resp = retry_request("GET", self.BASE_URL, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            value = self._parse_cpi(data)
            if value is not None:
                _log.info("  Truflation CPI: %.2f%%", value)
            return value
        except Exception as e:
            _log.error("  Truflation fetch failed: %s", e)
            return None


@dataclass
class FeedEntry:
    """A single RSS/blog feed entry."""
    title: str
    url: str
    summary: str
    published: str


# Keywords that indicate macro/inflation relevance
_MACRO_KEYWORDS = re.compile(
    r'(?:cpi|inflation|tariff|trade.?war|fed\s+rate|fomc|interest.?rate|'
    r'jobs?\s+report|nonfarm|employment|gdp|recession|price.?index|'
    r'consumer.?price|import.?price|pce|shelter|rent|wage|labor.?market|'
    r'treasury|bond.?yield|breakeven|deficit|fiscal|monetary.?policy)',
    re.IGNORECASE,
)


class RSSFeedParser:
    """Parse RSS/Atom feeds and filter for macro-relevant articles."""

    def parse_feed_xml(self, xml_text: str) -> List[FeedEntry]:
        """Parse RSS/Atom XML into FeedEntry list."""
        feed = feedparser.parse(xml_text)
        entries = []
        for entry in feed.entries:
            entries.append(FeedEntry(
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                summary=entry.get("summary", entry.get("description", "")),
                published=entry.get("published", ""),
            ))
        return entries

    def _is_relevant(self, entry: FeedEntry) -> bool:
        """Check if a feed entry is relevant to macro/inflation analysis."""
        text = f"{entry.title} {entry.summary}".lower()
        return bool(_MACRO_KEYWORDS.search(text))

    def filter_relevant(self, entries: List[FeedEntry]) -> List[FeedEntry]:
        """Filter entries to only macro-relevant ones."""
        return [e for e in entries if self._is_relevant(e)]

    def fetch_feed(self, url: str) -> List[FeedEntry]:
        """Fetch and parse an RSS/Atom feed URL.

        Returns list of FeedEntry, empty on error.
        """
        try:
            resp = retry_request("GET", url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
            return self.parse_feed_xml(resp.text)
        except Exception as e:
            _log.error("  RSS fetch failed for %s: %s", url, e)
            return []


@dataclass
class SentimentResult:
    """Result of LLM sentiment extraction from a single article."""
    direction: str       # "higher", "lower", "neutral"
    magnitude: int       # 1-5 (strength of signal)
    confidence: float    # 0-1 (LLM's self-assessed confidence)
    factors: List[str]   # Key factors driving the outlook
    score: float = 0.0   # Computed: direction * magnitude/5, in [-1, +1]


class SentimentExtractor:
    """Extract CPI/inflation sentiment from article text using DeepSeek LLM.

    Follows the same pattern as beatrelease-scanner.py for LLM calls.
    """

    DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

    PROMPT_TEMPLATE = """Analyze this article for its implications on US CPI inflation.

Output a JSON object with:
- "direction": "higher", "lower", or "neutral" (expected CPI direction)
- "magnitude": 1-5 (1=barely, 5=strongly)
- "confidence": 0.0-1.0 (your confidence in this assessment)
- "factors": list of 1-3 key factors (e.g. "tariffs", "shelter costs", "energy prices")

Only output the JSON object, no other text.

Article:
---
{article_text}
"""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def _parse_response(self, raw: str) -> Optional[SentimentResult]:
        """Parse DeepSeek response into SentimentResult."""
        # Strip markdown code blocks
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        try:
            data = json.loads(text)
            direction = data.get("direction", "neutral")
            magnitude = max(1, min(5, int(data.get("magnitude", 3))))
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
            factors = data.get("factors", [])
            if not isinstance(factors, list):
                factors = [str(factors)]
            score = self._direction_to_score(direction, magnitude)
            return SentimentResult(
                direction=direction,
                magnitude=magnitude,
                confidence=confidence,
                factors=factors[:5],
                score=score,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def _direction_to_score(self, direction: str, magnitude: int) -> float:
        """Convert direction + magnitude to [-1, +1] score."""
        if direction == "neutral":
            return 0.0
        raw = magnitude / 5.0
        raw = max(0.0, min(1.0, raw))
        return raw if direction == "higher" else -raw

    def extract(self, article_text: str) -> Optional[SentimentResult]:
        """Send article to DeepSeek and extract sentiment.

        Returns SentimentResult, or None on error.
        """
        if not self.api_key:
            return None

        prompt = self.PROMPT_TEMPLATE.format(article_text=article_text[:30000])

        try:
            resp = retry_request("POST", self.DEEPSEEK_URL, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }, json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 500,
            }, timeout=60)

            content = resp.json()["choices"][0]["message"]["content"].strip()
            return self._parse_response(content)
        except Exception as e:
            _log.error("  DeepSeek sentiment extraction failed: %s", e)
            return None


# Default RSS feed URLs
DEFAULT_RSS_FEEDS = [
    "https://www.reuters.com/arc/outboundfeeds/v3/all/",  # Reuters headlines
]
KOBEISSI_URL = "https://thekobeissiletter.com/blog"

# Cache settings
MACRO_CACHE_PATH = PROJECT_DIR / "data" / "macro-cache.json"
MACRO_CACHE_TTL = 4 * 3600  # 4 hours


class MacroEngine:
    """Autonomous macro sentiment engine.

    Fetches from FRED, Truflation, RSS feeds, and Kobeissi blog.
    Produces a MacroSignal with CPI bias adjustments for the economics bot.

    Usage:
        engine = MacroEngine()
        signal = engine.compute_signal(cleveland_nowcast=2.8)
        adjusted_nowcast = 2.8 + signal.cpi_bias
        sigma_mult = engine.compute_sigma_multiplier(signal.confidence)
        adjusted_sigma = base_sigma * sigma_mult
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._cache_ttl = self._config.get("cacheTtlHours", 4) * 3600
        self._bias_clamp = self._config.get("biasClampPp", 0.15)
        self._sigma_tightening_max = self._config.get("sigmaTighteningMax", 0.30)
        self._max_sentiment_articles = self._config.get("maxSentimentArticles", 5)
        self._fred = FREDClient(api_key=self._config.get("fred_api_key", ""))
        self._truflation = TruflationClient()
        self._rss = RSSFeedParser()
        self._deepseek_key = self._load_deepseek_key()
        self._sentiment = SentimentExtractor(api_key=self._deepseek_key)

    def _load_deepseek_key(self) -> str:
        """Load DeepSeek API key (same path as beatrelease scanner)."""
        key_path = PROJECT_DIR / "config" / "keys" / "deepseek.txt"
        try:
            key = key_path.read_text().strip()
            if key and key != "PASTE_YOUR_DEEPSEEK_API_KEY_HERE":
                return key
        except (FileNotFoundError, OSError):
            pass
        import os
        return os.environ.get("DEEPSEEK_API_KEY", "")

    def _tips_breakeven_bias(self, breakeven: float, nowcast: float) -> float:
        """Compute CPI bias from TIPS breakeven vs Cleveland Fed nowcast.

        Returns bias in percentage points. Scaled by 0.3 because TIPS
        breakevens reflect long-term expectations, not next-month CPI.
        """
        if breakeven is None or nowcast is None:
            return 0.0
        return (breakeven - nowcast) * 0.3

    def _truflation_bias(self, truflation: float, nowcast: float) -> float:
        """Compute CPI bias from Truflation real-time CPI vs nowcast.

        Returns bias in percentage points. Truflation tracks the same
        basket, so the scaling factor is higher (0.5).
        """
        if truflation is None or nowcast is None:
            return 0.0
        return (truflation - nowcast) * 0.5

    def _aggregate_confidence(self, biases: List[float], source_count: int,
                               total_sources: int) -> float:
        """Compute confidence from bias agreement and source coverage.

        Higher when:
          - More sources available (coverage)
          - Sources agree on direction (agreement)
        """
        if not biases or source_count == 0:
            return 0.0

        # Coverage: fraction of sources that returned data
        coverage = source_count / max(1, total_sources)

        # Agreement: what fraction of biases agree on sign?
        positive = sum(1 for b in biases if b > 0.005)
        negative = sum(1 for b in biases if b < -0.005)
        total = len(biases)
        agreement = max(positive, negative) / total if total > 0 else 0.0

        # Combine: sqrt(coverage * agreement) gives a 0-1 score
        import math
        return min(1.0, math.sqrt(coverage * agreement))

    def compute_sigma_multiplier(self, confidence: float) -> float:
        """Compute sigma adjustment multiplier based on macro confidence.

        Higher confidence -> tighter sigma (up to sigmaTighteningMax reduction).
        Low confidence -> no change (multiplier = 1.0).

        Returns: multiplier in [1.0 - sigmaTighteningMax, 1.0]
        """
        if confidence <= 0.3:
            return 1.0
        # Linear interpolation: conf 0.3 -> 1.0, conf 1.0 -> (1.0 - sigmaTighteningMax)
        return 1.0 - self._sigma_tightening_max * ((confidence - 0.3) / 0.7)

    def compute_signal(self, cleveland_nowcast: Optional[float] = None) -> MacroSignal:
        """Fetch all sources and compute aggregated macro signal.

        Args:
            cleveland_nowcast: Current Cleveland Fed nowcast value (e.g. 2.8%).
                Used as anchor for relative bias computation.

        Returns:
            MacroSignal with cpi_bias, confidence, and individual source values.
        """
        import datetime

        if not self._enabled:
            return MacroSignal(timestamp=datetime.datetime.now().isoformat())

        signal = MacroSignal(timestamp=datetime.datetime.now().isoformat())
        biases = []

        # 1. FRED data
        fred_data = self._fred.fetch_all()
        if "tips_breakeven_10y" in fred_data:
            signal.tips_breakeven = fred_data["tips_breakeven_10y"]
            signal.sources_available += 1
            if cleveland_nowcast is not None:
                bias = self._tips_breakeven_bias(signal.tips_breakeven, cleveland_nowcast)
                biases.append(bias)

        if "umich_expectations" in fred_data:
            signal.umich_expectations = fred_data["umich_expectations"]
            signal.sources_available += 1
            if cleveland_nowcast is not None:
                bias = (signal.umich_expectations - cleveland_nowcast) * 0.2
                biases.append(bias)

        if "gdpnow" in fred_data:
            signal.gdpnow = fred_data["gdpnow"]
            signal.sources_available += 1

        # 2. Truflation
        truflation = self._truflation.fetch()
        if truflation is not None:
            signal.truflation_cpi = truflation
            signal.sources_available += 1
            if cleveland_nowcast is not None:
                bias = self._truflation_bias(truflation, cleveland_nowcast)
                biases.append(bias)

        # 3. RSS sentiment
        all_sentiments = []
        rss_urls = self._config.get("rss_feeds", DEFAULT_RSS_FEEDS)
        for url in rss_urls:
            entries = self._rss.fetch_feed(url)
            relevant = self._rss.filter_relevant(entries)
            for entry in relevant[:self._max_sentiment_articles]:  # cap per feed
                sentiment = self._sentiment.extract(
                    f"{entry.title}\n\n{entry.summary}"
                )
                if sentiment:
                    all_sentiments.append(sentiment)

        # 4. Kobeissi blog (HTML scrape, not RSS)
        kobeissi_sentiments = self._fetch_kobeissi_sentiment()
        all_sentiments.extend(kobeissi_sentiments)

        if all_sentiments:
            signal.sources_available += 1
            avg_score = sum(s.score for s in all_sentiments) / len(all_sentiments)
            avg_conf = sum(s.confidence for s in all_sentiments) / len(all_sentiments)
            signal.sentiment_score = avg_score
            # Sentiment score -> CPI bias: +1 score -> +0.05pp bias
            bias = avg_score * 0.05
            biases.append(bias)

        # 5. Aggregate
        if biases:
            signal.cpi_bias = sum(biases) / len(biases)
            # Clamp bias to [-biasClampPp, +biasClampPp] pp
            signal.cpi_bias = max(-self._bias_clamp, min(self._bias_clamp, signal.cpi_bias))
        signal.confidence = self._aggregate_confidence(
            biases, signal.sources_available, signal.sources_total
        )

        _log.info("  Macro signal: bias=%.3f%%, confidence=%.2f, sources=%d/%d",
                   signal.cpi_bias, signal.confidence,
                   signal.sources_available, signal.sources_total)

        # Cache the signal
        self._save_cache(signal)

        return signal

    def _fetch_kobeissi_sentiment(self) -> List[SentimentResult]:
        """Fetch and analyze Kobeissi Letter blog posts."""
        results = []
        try:
            from bs4 import BeautifulSoup
            resp = retry_request("GET", KOBEISSI_URL, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(resp.text, "html.parser")
            # Extract article links and text
            for article in soup.find_all("article")[:3]:
                text = article.get_text(separator="\n", strip=True)
                if len(text) < 50:
                    continue
                # Quick relevance check
                if not _MACRO_KEYWORDS.search(text):
                    continue
                sentiment = self._sentiment.extract(text)
                if sentiment:
                    results.append(sentiment)
        except Exception as e:
            _log.warning("  Kobeissi fetch/parse failed: %s", e)
        return results

    def _save_cache(self, signal: MacroSignal):
        """Cache the signal to disk."""
        try:
            _atomic_write_json(MACRO_CACHE_PATH, {
                "cached_at": time.time(),
                "signal": asdict(signal),
            })
        except Exception as e:
            _log.warning("  Failed to cache macro signal: %s", e)

    def load_cached_signal(self) -> Optional[MacroSignal]:
        """Load cached signal if fresh (within TTL)."""
        try:
            if not MACRO_CACHE_PATH.exists():
                return None
            data = json.loads(MACRO_CACHE_PATH.read_text())
            if time.time() - data.get("cached_at", 0) > self._cache_ttl:
                return None
            s = data.get("signal", {})
            return MacroSignal(**{k: v for k, v in s.items()
                                  if k in MacroSignal.__dataclass_fields__})
        except Exception:
            return None
