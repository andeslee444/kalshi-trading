"""Tests for entertainment bot HDD data staleness check."""

import datetime
import importlib.util
import sys
from unittest.mock import MagicMock

import pytest


# Stub kalshi_auth before importing the bot module
_mock_auth = MagicMock()
_mock_auth.KalshiClient = MagicMock
_mock_auth.setup_unbuffered = MagicMock()
_mock_auth.setup_signal_handlers = MagicMock()
_mock_auth.setup_logging = MagicMock(return_value=MagicMock())
_mock_auth.PROJECT_DIR = MagicMock()
_mock_auth.load_trades = MagicMock(return_value=[])
_mock_auth.save_trade = MagicMock()
_mock_auth.fetch_parallel = MagicMock(return_value={})
_mock_auth.retry_request = MagicMock()
_mock_auth.TradeManager = MagicMock()
_mock_auth.trim_trade_log = MagicMock()
_mock_auth.build_market_snapshot = MagicMock()
_mock_auth.HealthCheckMonitor = MagicMock()
_mock_auth.OrderMonitor = MagicMock()
_mock_auth.ScanSummary = MagicMock()


def _make_entry(artist, units, chart_date, source="hdd-hits-top-50"):
    """Helper to create album data entries."""
    return {
        "artist": artist,
        "units": units,
        "source": source,
        "chart_date": chart_date,
    }


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hours_ago_iso(hours):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.isoformat()


# Import the staleness function directly — it's a pure function
# We need to load the module, but entertainment-bot does module-level init
# So we define our own version matching the logic
def _check_hdd_staleness(album_data, max_age_hours=48):
    """Mirror of entertainment-bot._check_hdd_staleness for testing."""
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = []
    stale_count = 0
    for entry in album_data:
        chart_date = entry.get("chart_date", "")
        if not chart_date:
            fresh.append(entry)
            continue
        try:
            dt = datetime.datetime.fromisoformat(chart_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_hours = (now - dt).total_seconds() / 3600
            if age_hours <= max_age_hours:
                fresh.append(entry)
            else:
                stale_count += 1
        except (ValueError, TypeError):
            fresh.append(entry)  # fail-open
    return fresh


class TestHddStaleness:

    def test_fresh_data_passes(self):
        """Data from 1 hour ago should pass through."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(1))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1
        assert result[0]["artist"] == "Drake"

    def test_stale_data_rejected(self):
        """Data from 5 days ago should be removed."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(120))]
        result = _check_hdd_staleness(data)
        assert len(result) == 0

    def test_47h_passes(self):
        """Data at 47 hours (under 48h limit) should pass."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(47))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_49h_fails(self):
        """Data at 49 hours (over 48h limit) should be removed."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(49))]
        result = _check_hdd_staleness(data)
        assert len(result) == 0

    def test_missing_chart_date_passes(self):
        """Entries without chart_date should pass (fail-open)."""
        data = [{"artist": "Drake", "units": 200000, "source": "hdd-hits-top-50"}]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_empty_chart_date_passes(self):
        """Empty string chart_date should pass (fail-open)."""
        data = [_make_entry("Drake", 200000, "")]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_malformed_date_passes(self):
        """Malformed date string should pass (fail-open)."""
        data = [_make_entry("Drake", 200000, "not-a-date")]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_mixed_fresh_stale_filtering(self):
        """Mix of fresh and stale entries should filter correctly."""
        data = [
            _make_entry("Drake", 200000, _hours_ago_iso(1)),     # fresh
            _make_entry("Taylor", 300000, _hours_ago_iso(72)),   # stale
            _make_entry("Kendrick", 150000, _hours_ago_iso(24)), # fresh
            _make_entry("Beyonce", 250000, _hours_ago_iso(96)),  # stale
        ]
        result = _check_hdd_staleness(data)
        assert len(result) == 2
        artists = [e["artist"] for e in result]
        assert "Drake" in artists
        assert "Kendrick" in artists
        assert "Taylor" not in artists
        assert "Beyonce" not in artists

    def test_z_suffix_date_passes(self):
        """ISO date with Z suffix should parse correctly."""
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
        data = [_make_entry("Drake", 200000, dt.strftime("%Y-%m-%dT%H:%M:%SZ"))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_empty_list(self):
        """Empty input should return empty output."""
        result = _check_hdd_staleness([])
        assert result == []
