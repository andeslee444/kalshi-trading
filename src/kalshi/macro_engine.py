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
