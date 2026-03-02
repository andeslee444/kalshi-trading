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
