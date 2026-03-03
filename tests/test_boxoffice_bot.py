"""Tests for box office scraping functions in source-monitor.py.

Tests the HTML parsing functions that extract weekend domestic gross
revenue data from The Numbers and Box Office Mojo websites.
"""

import json
import re
import pytest
from unittest.mock import MagicMock, patch
import importlib
import importlib.util
import sys
from pathlib import Path


# --- Module loading (same pattern as test_source_monitor.py) ---

def _load_source_monitor():
    """Load source-monitor.py with stubbed dependencies."""
    stub_names = ["kalshi_auth", "probability", "capital_allocator",
                  "hdd_parser", "macro_engine", "correlation_engine", "ticker_utils"]

    # Save originals
    originals = {name: sys.modules.get(name) for name in stub_names}

    # Stub kalshi_auth before import
    mock_auth = MagicMock()
    mock_client = MagicMock()
    mock_auth.KalshiClient.return_value = mock_client
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    mock_auth.TradeManager = MagicMock()
    mock_auth.HealthCheckMonitor = MagicMock()
    mock_auth.OrderMonitor = MagicMock()
    mock_auth.ScanSummary = MagicMock()
    mock_auth.build_market_snapshot = MagicMock(return_value={})
    mock_auth.CITY_TIMEZONES = {}
    mock_auth._local_today = MagicMock()
    mock_auth.round_half_up = round
    mock_auth.trim_trade_log = MagicMock()
    mock_auth.check_kill_switch = MagicMock()
    mock_auth.retry_request = MagicMock()
    mock_auth.load_trades.return_value = []
    mock_auth.fetch_parallel = MagicMock(return_value={})
    mock_auth.is_market_liquid = MagicMock(return_value=True)
    mock_auth.compute_limit_price = MagicMock(return_value=50)
    mock_auth.kalshi_fee_cents = MagicMock(return_value=1)

    # Stub other imports
    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["probability"] = MagicMock()
    sys.modules["capital_allocator"] = MagicMock()
    sys.modules["hdd_parser"] = MagicMock()
    sys.modules["macro_engine"] = MagicMock()
    sys.modules["correlation_engine"] = MagicMock()
    sys.modules["ticker_utils"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "source_monitor",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "source-monitor.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore originals
    for name in stub_names:
        if originals[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = originals[name]

    return mod


# --- Fixtures: realistic HTML snippets ---

THE_NUMBERS_HTML = """
<table>
<tr><td>1</td><td><a href="/movie/a-minecraft-movie">A Minecraft Movie</a></td>
<td class="money">$163,830,000</td><td class="money">$163,830,000</td></tr>
<tr><td>2</td><td><a href="/movie/thunderbolts">Thunderbolts*</a></td>
<td class="money">$75,200,000</td><td class="money">$75,200,000</td></tr>
<tr><td>3</td><td><a href="/movie/snow-white-2025">Snow White</a></td>
<td class="money">$12,400,000</td><td class="money">$88,600,000</td></tr>
</table>
"""

MOJO_HTML = """
<table class="mojo-body-table">
<tr><td class="mojo-field-type-rank">1</td>
<td><a href="/release/rl1234">A Minecraft Movie</a></td>
<td class="money">$163.8M</td></tr>
<tr><td class="mojo-field-type-rank">2</td>
<td><a href="/release/rl5678">Thunderbolts*</a></td>
<td class="money">$75.2M</td></tr>
</table>
"""

THE_NUMBERS_EMPTY = "<table><tr><td>No data available</td></tr></table>"


class TestParseTheNumbersHtml:
    """Test parsing weekend box office data from The Numbers HTML."""

    def test_extracts_top_movies(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_HTML)
        assert len(result) >= 2
        assert result[0]["title"] == "A Minecraft Movie"
        assert result[0]["gross"] == 163_830_000

    def test_extracts_second_movie(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_HTML)
        thunderbolts = [m for m in result if "Thunderbolts" in m["title"]]
        assert len(thunderbolts) == 1
        assert thunderbolts[0]["gross"] == 75_200_000

    def test_empty_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_EMPTY)
        assert result == []

    def test_malformed_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html("<div>not a table</div>")
        assert result == []


class TestParseMojoHtml:
    """Test parsing weekend box office data from Box Office Mojo HTML."""

    def test_extracts_top_movies(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html(MOJO_HTML)
        assert len(result) >= 1
        assert result[0]["title"] == "A Minecraft Movie"
        assert abs(result[0]["gross"] - 163_800_000) < 100_000  # $163.8M

    def test_handles_million_suffix(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html(MOJO_HTML)
        t = [m for m in result if "Thunderbolts" in m["title"]]
        assert len(t) == 1
        assert abs(t[0]["gross"] - 75_200_000) < 100_000

    def test_empty_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html("<table></table>")
        assert result == []


class TestFetchBoxOfficeData:
    """Test the top-level fetch function with HTTP mocking."""

    def test_the_numbers_success(self):
        mod = _load_source_monitor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = THE_NUMBERS_HTML
        with patch.object(mod, "retry_request", return_value=mock_resp):
            result = mod.fetch_boxoffice_data()
        assert len(result) >= 2
        assert result[0]["source"] == "the_numbers"

    def test_fallback_to_mojo_on_the_numbers_failure(self):
        mod = _load_source_monitor()
        mock_fail = MagicMock()
        mock_fail.status_code = 503
        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.text = MOJO_HTML
        with patch.object(mod, "retry_request", side_effect=[mock_fail, mock_success]):
            result = mod.fetch_boxoffice_data()
        assert len(result) >= 1
        assert result[0]["source"] == "mojo"

    def test_both_fail_returns_empty(self):
        mod = _load_source_monitor()
        mock_fail = MagicMock()
        mock_fail.status_code = 503
        with patch.object(mod, "retry_request", return_value=mock_fail):
            result = mod.fetch_boxoffice_data()
        assert result == []

    def test_deduplicates_across_sources(self):
        mod = _load_source_monitor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = THE_NUMBERS_HTML
        with patch.object(mod, "retry_request", return_value=mock_resp):
            result = mod.fetch_boxoffice_data()
        titles = [m["title"] for m in result]
        assert len(titles) == len(set(titles))


class TestBoxOfficeScanIntegration:
    """Test that box office data flows through the full scan pipeline."""

    def test_scan_calls_fetch_and_match(self):
        """Verify scan_boxoffice() calls fetch_boxoffice_data then match_boxoffice_to_markets."""
        mod = _load_source_monitor()
        mock_data = [{"title": "Test Movie", "gross": 50_000_000, "source": "the_numbers"}]
        with patch.object(mod, "fetch_boxoffice_data", return_value=mock_data) as mock_fetch, \
             patch.object(mod, "match_boxoffice_to_markets") as mock_match:
            mod.scan_boxoffice()
            mock_fetch.assert_called_once()
            mock_match.assert_called_once_with(mock_data, prefetched_markets=None, ss=None)

    def test_scan_skips_on_empty_data(self):
        """Verify scan_boxoffice() doesn't call match when no data."""
        mod = _load_source_monitor()
        with patch.object(mod, "fetch_boxoffice_data", return_value=[]) as mock_fetch, \
             patch.object(mod, "match_boxoffice_to_markets") as mock_match:
            mod.scan_boxoffice()
            mock_fetch.assert_called_once()
            mock_match.assert_not_called()

    def test_scan_handles_fetch_exception(self):
        """Verify scan_boxoffice() handles fetch errors gracefully."""
        mod = _load_source_monitor()
        with patch.object(mod, "fetch_boxoffice_data", side_effect=Exception("network error")):
            # Should not raise
            mod.scan_boxoffice()


class TestHddAutoReEnable:
    """Test that HDD scanning auto-re-enables after Sanity recovers."""

    def test_hdd_re_enabled_after_health_check_passes(self):
        """If HDD was disabled due to errors but Sanity is now healthy, re-enable."""
        mod = _load_source_monitor()
        # Simulate HDD being error-disabled
        with patch.object(mod, "check_sanity_health", return_value=True):
            result = mod.should_retry_hdd()
            assert result is True

    def test_hdd_stays_disabled_if_still_unhealthy(self):
        """If Sanity is still down, keep HDD disabled."""
        mod = _load_source_monitor()
        with patch.object(mod, "check_sanity_health", return_value=False):
            result = mod.should_retry_hdd()
            assert result is False
