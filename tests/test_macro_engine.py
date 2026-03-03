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
    TruflationClient,
    RSSFeedParser,
    FeedEntry,
    SentimentExtractor,
    SentimentResult,
    MacroEngine,
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


class TestMacroEngineAggregation:
    """Tests for MacroEngine signal aggregation."""

    def test_cpi_bias_from_tips_breakeven(self):
        """TIPS breakeven above Cleveland Fed nowcast -> positive CPI bias."""
        engine = MacroEngine.__new__(MacroEngine)
        # Cleveland nowcast: 2.8%, TIPS breakeven: 3.1%
        # Bias should be positive (market expects higher inflation)
        bias = engine._tips_breakeven_bias(breakeven=3.1, nowcast=2.8)
        assert bias > 0
        assert bias < 0.5  # should be moderate

    def test_cpi_bias_from_tips_below_nowcast(self):
        """TIPS breakeven below nowcast -> negative bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._tips_breakeven_bias(breakeven=2.5, nowcast=2.8)
        assert bias < 0

    def test_cpi_bias_tips_equal_nowcast(self):
        """TIPS == nowcast -> zero bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._tips_breakeven_bias(breakeven=2.8, nowcast=2.8)
        assert bias == pytest.approx(0.0, abs=0.01)

    def test_truflation_bias(self):
        """Truflation above nowcast -> positive bias."""
        engine = MacroEngine.__new__(MacroEngine)
        bias = engine._truflation_bias(truflation=3.0, nowcast=2.8)
        assert bias > 0

    def test_aggregate_confidence_scales_with_sources(self):
        """Confidence is higher when more sources agree."""
        engine = MacroEngine.__new__(MacroEngine)
        # All positive biases -> higher confidence
        conf = engine._aggregate_confidence(
            biases=[0.05, 0.03, 0.02],
            source_count=3,
            total_sources=6,
        )
        assert conf > 0.3

    def test_aggregate_confidence_low_when_sources_disagree(self):
        """Mixed positive/negative biases -> lower confidence."""
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
        """No sources -> zero confidence."""
        engine = MacroEngine.__new__(MacroEngine)
        conf = engine._aggregate_confidence(biases=[], source_count=0, total_sources=6)
        assert conf == 0.0

    def test_sigma_adjustment(self):
        """High confidence should tighten sigma, low confidence widen it."""
        engine = MacroEngine.__new__(MacroEngine)
        # High confidence -> tighter sigma (multiplier < 1)
        tight = engine.compute_sigma_multiplier(confidence=0.8)
        assert tight < 1.0
        # Low confidence -> no tightening
        wide = engine.compute_sigma_multiplier(confidence=0.2)
        assert wide >= 1.0
