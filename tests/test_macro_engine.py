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
