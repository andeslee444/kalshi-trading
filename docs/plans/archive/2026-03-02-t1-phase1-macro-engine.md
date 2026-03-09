# T1-Phase 1: Macro/Geopolitics Engine — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an autonomous macro sentiment engine that produces quantified CPI/GDP/Jobs bias adjustments from curated external data sources, directly improving the economics bot's trade decisions.

**Architecture:** New module `src/kalshi/macro_engine.py` fetches data from FRED API (TIPS breakevens, UMich expectations, Atlanta Fed GDPNow), Truflation real-time CPI, and curated blog/RSS feeds (Kobeissi, Reuters). DeepSeek LLM extracts sentiment from blog text. All sources produce signals that aggregate into a `MacroSignal` with CPI bias and confidence. The economics bot consumes this signal to adjust its nowcast before probability computation.

**Tech Stack:** Python 3, `feedparser` (new dependency), `requests` (existing), DeepSeek API (existing key), FRED API (free, no auth), BeautifulSoup (existing).

**Branch:** `track1/alpha-generation`

---

### Task 1: Create Branch and Add feedparser Dependency

**Files:**
- Modify: `requirements.txt`

**Step 1: Create the feature branch**

```bash
git checkout -b track1/alpha-generation main
```

**Step 2: Add feedparser to requirements.txt**

Add `feedparser>=6.0` to `requirements.txt`.

**Step 3: Install the dependency**

Run: `pip install feedparser>=6.0`

**Step 4: Commit**

```bash
git add requirements.txt
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "chore: add feedparser dependency for macro engine RSS parsing"
```

---

### Task 2: FRED API Client — Tests

**Files:**
- Create: `tests/test_macro_engine.py`
- Create: `src/kalshi/macro_engine.py` (minimal stub)

**Step 1: Write the test file with FRED API tests**

```python
"""Tests for macro_engine.py — FRED API, Truflation, sentiment aggregation."""

import json
import math
import time
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Import the module directly (no side effects at import time)
from macro_engine import (
    FREDClient,
    MacroSignal,
)


class TestFREDClient:
    """Tests for FRED API data fetching."""

    def _make_fred_response(self, value, date="2026-03-01"):
        """Build a mock FRED API JSON response."""
        return {
            "observations": [
                {"date": date, "value": str(value)}
            ]
        }

    def test_parse_tips_breakeven(self):
        """Parse 10Y TIPS breakeven rate from FRED response."""
        client = FREDClient()
        resp = self._make_fred_response(2.35)
        result = client._parse_observation(resp)
        assert result == pytest.approx(2.35, abs=0.01)

    def test_parse_empty_observations(self):
        """Empty observations list returns None."""
        client = FREDClient()
        result = client._parse_observation({"observations": []})
        assert result is None

    def test_parse_dot_value_returns_none(self):
        """FRED uses '.' for missing data — should return None."""
        client = FREDClient()
        resp = self._make_fred_response(".")
        result = client._parse_observation(resp)
        assert result is None

    def test_parse_umich_expectations(self):
        """Parse UMich consumer inflation expectations."""
        client = FREDClient()
        resp = self._make_fred_response(3.1)
        result = client._parse_observation(resp)
        assert result == pytest.approx(3.1, abs=0.01)

    def test_parse_gdpnow(self):
        """Parse Atlanta Fed GDPNow estimate."""
        client = FREDClient()
        resp = self._make_fred_response(-1.5)
        result = client._parse_observation(resp)
        assert result == pytest.approx(-1.5, abs=0.01)
```

**Step 2: Write the minimal stub to make imports work**

Create `src/kalshi/macro_engine.py`:

```python
"""Macro/geopolitics sentiment engine for CPI/GDP/Jobs bias adjustments.

Fetches data from FRED API, Truflation, and curated blog/RSS feeds.
Produces MacroSignal with quantified bias adjustments for economics bot.
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict

import requests

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
```

**Step 3: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All 5 tests PASS

**Step 4: Commit**

```bash
git add tests/test_macro_engine.py src/kalshi/macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add FRED API client stub and tests for macro engine"
```

---

### Task 3: FRED API Client — Fetch Implementation

**Files:**
- Modify: `src/kalshi/macro_engine.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write fetch test with mocked HTTP**

Add to `tests/test_macro_engine.py`:

```python
class TestFREDClientFetch:
    """Test actual fetch methods with mocked HTTP."""

    @patch("macro_engine.retry_request")
    def test_fetch_tips_breakeven(self, mock_request):
        """fetch_series returns parsed value from FRED."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        result = client.fetch_series("tips_breakeven_10y")
        assert result == pytest.approx(2.35, abs=0.01)
        mock_request.assert_called_once()

    @patch("macro_engine.retry_request")
    def test_fetch_handles_http_error(self, mock_request):
        """fetch_series returns None on HTTP error."""
        mock_request.side_effect = Exception("Connection failed")
        client = FREDClient()
        result = client.fetch_series("tips_breakeven_10y")
        assert result is None

    @patch("macro_engine.retry_request")
    def test_fetch_all_returns_dict(self, mock_request):
        """fetch_all returns dict of available series."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        result = client.fetch_all()
        assert isinstance(result, dict)
        assert len(result) > 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_macro_engine.py::TestFREDClientFetch -v`
Expected: FAIL (fetch_series not defined)

**Step 3: Implement fetch methods**

Add to `FREDClient` class in `macro_engine.py`:

```python
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
```

Also add the import at the top of `macro_engine.py`:

```python
from kalshi_auth import retry_request, _atomic_write_json, PROJECT_DIR
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: implement FRED API fetch with retry and caching"
```

---

### Task 4: Truflation Client

**Files:**
- Modify: `src/kalshi/macro_engine.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write Truflation tests**

Add to `tests/test_macro_engine.py`:

```python
from macro_engine import TruflationClient


class TestTruflationClient:
    """Tests for Truflation real-time CPI data."""

    def test_parse_cpi_response(self):
        """Parse Truflation CPI value from API response."""
        client = TruflationClient()
        data = {"currentCpiYoY": 2.75}
        result = client._parse_cpi(data)
        assert result == pytest.approx(2.75, abs=0.01)

    def test_parse_handles_missing_field(self):
        """Missing field returns None."""
        client = TruflationClient()
        result = client._parse_cpi({})
        assert result is None

    def test_parse_rejects_extreme_values(self):
        """Values outside 0-20% are rejected."""
        client = TruflationClient()
        assert client._parse_cpi({"currentCpiYoY": -5.0}) is None
        assert client._parse_cpi({"currentCpiYoY": 25.0}) is None

    @patch("macro_engine.retry_request")
    def test_fetch_returns_value(self, mock_request):
        """fetch() returns CPI value from API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"currentCpiYoY": 2.80}
        mock_request.return_value = mock_resp

        client = TruflationClient()
        result = client.fetch()
        assert result == pytest.approx(2.80, abs=0.01)

    @patch("macro_engine.retry_request")
    def test_fetch_handles_error(self, mock_request):
        """fetch() returns None on error."""
        mock_request.side_effect = Exception("timeout")
        client = TruflationClient()
        assert client.fetch() is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_macro_engine.py::TestTruflationClient -v`
Expected: FAIL (TruflationClient not defined)

**Step 3: Implement TruflationClient**

Add to `macro_engine.py`:

```python
class TruflationClient:
    """Fetch real-time CPI from Truflation public API."""

    # Truflation provides a public API for current CPI YoY
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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Truflation real-time CPI client"
```

---

### Task 5: RSS Feed Parser (Kobeissi / Reuters)

**Files:**
- Modify: `src/kalshi/macro_engine.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write RSS parser tests**

Add to `tests/test_macro_engine.py`:

```python
from macro_engine import RSSFeedParser, FeedEntry


class TestRSSFeedParser:
    """Tests for RSS/blog feed parsing."""

    SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
    <channel>
        <title>Test Feed</title>
        <item>
            <title>CPI rises to 2.9% on tariff pressure</title>
            <link>https://example.com/article1</link>
            <pubDate>Mon, 01 Mar 2026 12:00:00 GMT</pubDate>
            <description>Consumer prices rose more than expected...</description>
        </item>
        <item>
            <title>Market update: stocks flat</title>
            <link>https://example.com/article2</link>
            <pubDate>Sun, 28 Feb 2026 12:00:00 GMT</pubDate>
            <description>Markets were mostly unchanged...</description>
        </item>
    </channel>
    </rss>"""

    def test_parse_rss_entries(self):
        """Parse entries from RSS XML."""
        parser = RSSFeedParser()
        entries = parser.parse_feed_xml(self.SAMPLE_RSS_XML)
        assert len(entries) == 2
        assert entries[0].title == "CPI rises to 2.9% on tariff pressure"
        assert entries[0].url == "https://example.com/article1"

    def test_filter_relevant_entries(self):
        """Only entries with CPI/inflation/tariff keywords pass filter."""
        parser = RSSFeedParser()
        entries = parser.parse_feed_xml(self.SAMPLE_RSS_XML)
        relevant = parser.filter_relevant(entries)
        assert len(relevant) == 1
        assert "CPI" in relevant[0].title

    def test_empty_feed(self):
        """Empty RSS feed returns empty list."""
        parser = RSSFeedParser()
        entries = parser.parse_feed_xml("<rss><channel></channel></rss>")
        assert entries == []

    def test_filter_keywords(self):
        """Various macro keywords trigger relevance."""
        parser = RSSFeedParser()
        for keyword in ["inflation", "CPI", "tariff", "Fed rate", "jobs report"]:
            entry = FeedEntry(title=f"Article about {keyword}", url="", summary="", published="")
            assert parser._is_relevant(entry), f"'{keyword}' should be relevant"

    def test_non_relevant_filtered(self):
        """Non-macro articles are filtered out."""
        parser = RSSFeedParser()
        entry = FeedEntry(title="Movie review: great film", url="", summary="", published="")
        assert not parser._is_relevant(entry)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_macro_engine.py::TestRSSFeedParser -v`
Expected: FAIL (RSSFeedParser not defined)

**Step 3: Implement RSSFeedParser**

Add to `macro_engine.py`:

```python
import re
import feedparser


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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add RSS feed parser with macro keyword filtering"
```

---

### Task 6: DeepSeek Sentiment Extraction

**Files:**
- Modify: `src/kalshi/macro_engine.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write sentiment extraction tests**

Add to `tests/test_macro_engine.py`:

```python
from macro_engine import SentimentExtractor, SentimentResult


class TestSentimentExtractor:
    """Tests for DeepSeek LLM sentiment extraction."""

    def test_parse_valid_json_response(self):
        """Parse a valid JSON response from DeepSeek."""
        extractor = SentimentExtractor(api_key="test")
        raw = '{"direction": "higher", "magnitude": 3, "confidence": 0.7, "factors": ["tariffs", "shelter"]}'
        result = extractor._parse_response(raw)
        assert result is not None
        assert result.direction == "higher"
        assert result.magnitude == 3
        assert result.confidence == pytest.approx(0.7, abs=0.01)
        assert "tariffs" in result.factors

    def test_parse_response_with_markdown(self):
        """Parse JSON wrapped in markdown code block."""
        extractor = SentimentExtractor(api_key="test")
        raw = '```json\n{"direction": "lower", "magnitude": 2, "confidence": 0.5, "factors": ["recession"]}\n```'
        result = extractor._parse_response(raw)
        assert result is not None
        assert result.direction == "lower"

    def test_parse_invalid_json_returns_none(self):
        """Invalid JSON returns None."""
        extractor = SentimentExtractor(api_key="test")
        assert extractor._parse_response("This is not JSON") is None

    def test_direction_to_score(self):
        """Convert direction + magnitude to -1..+1 score."""
        extractor = SentimentExtractor(api_key="test")
        # higher with magnitude 3 (out of 5) = +0.6
        assert extractor._direction_to_score("higher", 3) == pytest.approx(0.6, abs=0.01)
        # lower with magnitude 4 = -0.8
        assert extractor._direction_to_score("lower", 4) == pytest.approx(-0.8, abs=0.01)
        # neutral = 0
        assert extractor._direction_to_score("neutral", 3) == pytest.approx(0.0, abs=0.01)

    def test_score_clamped_to_range(self):
        """Score is clamped to [-1, +1]."""
        extractor = SentimentExtractor(api_key="test")
        assert extractor._direction_to_score("higher", 6) == pytest.approx(1.0, abs=0.01)
        assert extractor._direction_to_score("lower", 6) == pytest.approx(-1.0, abs=0.01)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_macro_engine.py::TestSentimentExtractor -v`
Expected: FAIL (SentimentExtractor not defined)

**Step 3: Implement SentimentExtractor**

Add to `macro_engine.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add DeepSeek sentiment extraction for macro articles"
```

---

### Task 7: MacroEngine Aggregation

**Files:**
- Modify: `src/kalshi/macro_engine.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write aggregation tests**

Add to `tests/test_macro_engine.py`:

```python
from macro_engine import MacroEngine


class TestMacroEngineAggregation:
    """Tests for MacroEngine signal aggregation."""

    def test_cpi_bias_from_tips_breakeven(self):
        """TIPS breakeven above Cleveland Fed nowcast → positive CPI bias."""
        engine = MacroEngine.__new__(MacroEngine)
        # Cleveland nowcast: 2.8%, TIPS breakeven: 3.1%
        # Bias should be positive (market expects higher inflation)
        bias = engine._tips_breakeven_bias(breakeven=3.1, nowcast=2.8)
        assert bias > 0
        assert bias < 0.5  # should be moderate

    def test_cpi_bias_from_tips_below_nowcast(self):
        """TIPS breakeven below nowcast → negative bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._tips_breakeven_bias(breakeven=2.5, nowcast=2.8)
        assert bias < 0

    def test_cpi_bias_tips_equal_nowcast(self):
        """TIPS == nowcast → zero bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._tips_breakeven_bias(breakeven=2.8, nowcast=2.8)
        assert bias == pytest.approx(0.0, abs=0.01)

    def test_truflation_bias(self):
        """Truflation above nowcast → positive bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._truflation_bias(truflation=3.0, nowcast=2.8)
        assert bias > 0

    def test_aggregate_confidence_scales_with_sources(self):
        """Confidence is higher when more sources agree."""
        engine = MacroEngine.__new__(MacroEngine)
        # All positive biases → higher confidence
        conf = engine._aggregate_confidence(
            biases=[0.05, 0.03, 0.02],
            source_count=3,
            total_sources=6,
        )
        assert conf > 0.3

    def test_aggregate_confidence_low_when_sources_disagree(self):
        """Mixed positive/negative biases → lower confidence."""
        engine = MacroEngine.__new__(MacroEngine)
        conf = engine._aggregate_confidence(
            biases=[0.05, -0.03, 0.02],
            source_count=3,
            total_sources=6,
        )
        # Should be lower than all-agreeing case
        conf_agree = engine._aggregate_confidence(
            biases=[0.05, 0.03, 0.02],
            source_count=3,
            total_sources=6,
        )
        assert conf < conf_agree

    def test_aggregate_confidence_zero_when_no_sources(self):
        """No sources → zero confidence."""
        engine = MacroEngine.__new__(MacroEngine)
        conf = engine._aggregate_confidence(biases=[], source_count=0, total_sources=6)
        assert conf == 0.0

    def test_sigma_adjustment(self):
        """High confidence should tighten sigma, low confidence widen it."""
        engine = MacroEngine.__new__(MacroEngine)
        base_sigma = 0.10
        # High confidence → tighter sigma (multiplier < 1)
        tight = engine.compute_sigma_multiplier(confidence=0.8)
        assert tight < 1.0
        # Low confidence → no tightening
        wide = engine.compute_sigma_multiplier(confidence=0.2)
        assert wide >= 1.0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_macro_engine.py::TestMacroEngineAggregation -v`
Expected: FAIL

**Step 3: Implement MacroEngine aggregation**

Add to `macro_engine.py`:

```python
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

        Higher confidence → tighter sigma (up to 30% reduction).
        Low confidence → no change (multiplier = 1.0).

        Returns: multiplier in [0.7, 1.0]
        """
        if confidence <= 0.3:
            return 1.0
        # Linear interpolation: conf 0.3 → 1.0, conf 1.0 → 0.7
        return 1.0 - 0.3 * ((confidence - 0.3) / 0.7)

    def compute_signal(self, cleveland_nowcast: Optional[float] = None) -> MacroSignal:
        """Fetch all sources and compute aggregated macro signal.

        Args:
            cleveland_nowcast: Current Cleveland Fed nowcast value (e.g. 2.8%).
                Used as anchor for relative bias computation.

        Returns:
            MacroSignal with cpi_bias, confidence, and individual source values.
        """
        import datetime

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
            for entry in relevant[:3]:  # cap per feed
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
            # Sentiment score → CPI bias: +1 score → +0.05pp bias
            bias = avg_score * 0.05
            biases.append(bias)

        # 5. Aggregate
        if biases:
            signal.cpi_bias = sum(biases) / len(biases)
            # Clamp bias to [-0.15, +0.15] pp
            signal.cpi_bias = max(-0.15, min(0.15, signal.cpi_bias))
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
            if time.time() - data.get("cached_at", 0) > MACRO_CACHE_TTL:
                return None
            s = data.get("signal", {})
            return MacroSignal(**{k: v for k, v in s.items()
                                  if k in MacroSignal.__dataclass_fields__})
        except Exception:
            return None
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: implement MacroEngine aggregation with multi-source bias computation"
```

---

### Task 8: Economics Bot Integration

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Modify: `tests/test_macro_engine.py`

**Step 1: Write integration test**

Add to `tests/test_macro_engine.py`:

```python
class TestEconomicsBotIntegration:
    """Test the macro-adjusted nowcast computation."""

    def test_adjusted_nowcast_with_positive_bias(self):
        """Positive CPI bias increases the nowcast."""
        nowcast = 2.8
        signal = MacroSignal(cpi_bias=0.05, confidence=0.6)
        adjusted = nowcast + signal.cpi_bias
        assert adjusted == pytest.approx(2.85, abs=0.01)

    def test_adjusted_nowcast_with_negative_bias(self):
        """Negative CPI bias decreases the nowcast."""
        nowcast = 2.8
        signal = MacroSignal(cpi_bias=-0.03, confidence=0.5)
        adjusted = nowcast + signal.cpi_bias
        assert adjusted == pytest.approx(2.77, abs=0.01)

    def test_sigma_tighter_with_high_confidence(self):
        """High confidence signal should tighten sigma."""
        engine = MacroEngine.__new__(MacroEngine)
        base_sigma = 0.10
        mult = engine.compute_sigma_multiplier(confidence=0.8)
        adjusted_sigma = base_sigma * mult
        assert adjusted_sigma < base_sigma
        assert adjusted_sigma > base_sigma * 0.5  # not more than 50% reduction

    def test_sigma_unchanged_with_low_confidence(self):
        """Low confidence signal should not change sigma."""
        engine = MacroEngine.__new__(MacroEngine)
        mult = engine.compute_sigma_multiplier(confidence=0.1)
        assert mult == pytest.approx(1.0, abs=0.01)

    def test_zero_bias_zero_confidence(self):
        """No macro data → zero bias, zero confidence, unchanged nowcast."""
        signal = MacroSignal()
        assert signal.cpi_bias == 0.0
        assert signal.confidence == 0.0
```

**Step 2: Run test to verify it passes**

Run: `pytest tests/test_macro_engine.py::TestEconomicsBotIntegration -v`
Expected: PASS (uses existing MacroSignal and MacroEngine)

**Step 3: Integrate macro engine into economics-bot.py**

In `economics-bot.py`, add the macro engine import and integration in `scan_and_trade()`. The key changes:

1. Add import at top: `from macro_engine import MacroEngine`
2. Initialize engine alongside other clients
3. In `scan_and_trade()`, after fetching nowcast, call `macro.compute_signal(nowcast_cpi)`
4. Adjust the nowcast value and sigma before computing probabilities

The integration point in `scan_and_trade()` is after line 578 (`if nowcast:`):

```python
# Macro adjustment (if available)
macro_signal = None
try:
    macro_signal = macro.compute_signal(cleveland_nowcast=nowcast.get("cpi_yoy"))
    if macro_signal and macro_signal.confidence > 0.2:
        # Adjust CPI nowcast
        if "cpi_yoy" in nowcast:
            nowcast["cpi_yoy"] += macro_signal.cpi_bias
            log.info(f"  Macro-adjusted CPI nowcast: {nowcast['cpi_yoy']:.3f}% "
                     f"(bias={macro_signal.cpi_bias:+.3f}%, conf={macro_signal.confidence:.2f})")
except Exception as e:
    log.warning(f"  Macro engine error (non-fatal): {e}")
```

And in the sigma computation section, adjust sigma with macro confidence:

```python
if macro_signal and macro_signal.confidence > 0.3:
    sigma *= macro.compute_sigma_multiplier(macro_signal.confidence)
```

**Step 4: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/economics-bot.py src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate macro engine into economics bot for CPI nowcast adjustment"
```

---

### Task 9: Config and Documentation

**Files:**
- Modify: `config/bots-config.json`
- Modify: `CLAUDE.md`

**Step 1: Add macro engine config to bots-config.json**

Add a `"macro"` section to `config/bots-config.json`:

```json
"macro": {
    "enabled": true,
    "cacheTtlHours": 4,
    "rssFeedUrls": [],
    "fredApiKey": "",
    "maxSentimentArticles": 5,
    "biasClamppPp": 0.15,
    "sigmaTighteningMax": 0.30
}
```

**Step 2: Update CLAUDE.md**

Add `macro_engine.py` to the Shared Modules section:

```
**`src/kalshi/macro_engine.py`** — Macro/geopolitics sentiment engine (~300 lines). Fetches from FRED API (TIPS breakevens, UMich expectations, GDPNow), Truflation real-time CPI, and curated blog/RSS feeds. DeepSeek LLM extracts sentiment. Produces `MacroSignal` with CPI bias and confidence for economics bot nowcast adjustment.
```

**Step 3: Commit**

```bash
git add config/bots-config.json CLAUDE.md
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "docs: add macro engine config and documentation"
```

---

### Task 10: Final Verification

**Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS (including existing tests unchanged)

**Step 2: Verify economics bot can start with `--once` in demo mode**

Run: `KALSHI_MODE=demo python3 src/kalshi/economics-bot.py --once 2>&1 | head -30`
Expected: Bot starts, macro engine fetches (may fail on some sources — that's OK), scan completes.

**Step 3: Verify macro_engine imports cleanly**

Run: `python3 -c "from macro_engine import MacroEngine, MacroSignal; print('OK')"`
(Run from `src/kalshi/` directory or with PYTHONPATH set)

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Branch + feedparser dep | requirements.txt | — |
| 2 | FRED client tests + stub | macro_engine.py, test_macro_engine.py | 5 |
| 3 | FRED client fetch implementation | macro_engine.py, test_macro_engine.py | +3 |
| 4 | Truflation client | macro_engine.py, test_macro_engine.py | +5 |
| 5 | RSS feed parser | macro_engine.py, test_macro_engine.py | +5 |
| 6 | DeepSeek sentiment extraction | macro_engine.py, test_macro_engine.py | +5 |
| 7 | MacroEngine aggregation | macro_engine.py, test_macro_engine.py | +8 |
| 8 | Economics bot integration | economics-bot.py, test_macro_engine.py | +5 |
| 9 | Config + docs | bots-config.json, CLAUDE.md | — |
| 10 | Final verification | — | Full suite |

**Total new tests:** ~36
**New files:** `src/kalshi/macro_engine.py`, `tests/test_macro_engine.py`
**Modified files:** `economics-bot.py`, `requirements.txt`, `bots-config.json`, `CLAUDE.md`
