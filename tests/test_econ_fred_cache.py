"""Tests for FRED per-scan cache in macro_engine.py.

Verifies that FREDClient caches results within a scan cycle to eliminate
duplicate API calls. The economics bot previously called fetch_all() multiple
times per scan, wasting FRED rate limit budget.
"""

import pytest
from unittest.mock import patch, MagicMock

from macro_engine import FREDClient


class TestFREDPerScanCache:
    """Tests for FREDClient per-scan caching behavior."""

    @patch("macro_engine.retry_request")
    def test_fetch_series_cached_within_scan(self, mock_request):
        """Second fetch_series call for same key returns cached value, no API call."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        result1 = client.fetch_series("tips_breakeven_10y")
        result2 = client.fetch_series("tips_breakeven_10y")
        assert result1 == result2
        # Should only make one API call
        assert mock_request.call_count == 1

    @patch("macro_engine.retry_request")
    def test_fetch_all_cached_within_scan(self, mock_request):
        """Second fetch_all call returns cached result, no API calls."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        result1 = client.fetch_all()
        call_count_after_first = mock_request.call_count
        result2 = client.fetch_all()
        # Should not make additional calls
        assert mock_request.call_count == call_count_after_first
        assert result1 == result2

    @patch("macro_engine.retry_request")
    def test_clear_cache_enables_refetch(self, mock_request):
        """After clear_cache(), fetch_all makes fresh API calls."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        client.fetch_all()
        first_count = mock_request.call_count

        client.clear_cache()
        client.fetch_all()
        # Should have made new API calls
        assert mock_request.call_count > first_count

    def test_clear_cache_resets_state(self):
        """clear_cache() resets both individual and batch caches."""
        client = FREDClient()
        client._scan_cache = {"tips_breakeven_10y": 2.35}
        client._scan_cache_all = {"tips_breakeven_10y": 2.35}
        client.clear_cache()
        assert client._scan_cache == {}
        assert client._scan_cache_all is None

    @patch("macro_engine.retry_request")
    def test_fetch_series_caches_none_on_error(self, mock_request):
        """Failed fetch caches None to avoid retrying same broken series."""
        mock_request.side_effect = Exception("timeout")
        client = FREDClient()
        result1 = client.fetch_series("tips_breakeven_10y")
        assert result1 is None
        # Second call should return cached None, not re-attempt
        mock_request.side_effect = None
        mock_request.return_value = MagicMock(
            json=MagicMock(return_value={"observations": [{"value": "2.35"}]})
        )
        result2 = client.fetch_series("tips_breakeven_10y")
        assert result2 is None  # Still cached as None
        # Only one call was made (the failed one)
        assert mock_request.call_count == 1

    @patch("macro_engine.retry_request")
    def test_different_series_not_cached(self, mock_request):
        """Different series keys should each make their own API call."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "observations": [{"date": "2026-03-01", "value": "2.35"}]
        }
        mock_request.return_value = mock_resp

        client = FREDClient()
        client.fetch_series("tips_breakeven_10y")
        client.fetch_series("tips_breakeven_5y")
        # Should make two separate API calls
        assert mock_request.call_count == 2
