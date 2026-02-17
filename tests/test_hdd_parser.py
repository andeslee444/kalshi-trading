"""Tests for chart parsing and sales extraction in hdd-scraper.py."""

import importlib
import types
import sys
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------

def _load_hdd_scraper():
    # Save the real kalshi_auth entry (if any) so we can restore it after
    # loading hdd-scraper.py and avoid polluting other test modules.
    orig_auth = sys.modules.get("kalshi_auth")

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: None
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = Path("/tmp/fake_project")
    fake_auth.load_trades = lambda *a, **kw: []
    fake_auth.save_trade = lambda *a, **kw: None
    sys.modules["kalshi_auth"] = fake_auth

    # Create directories the module expects at import time
    Path("/tmp/fake_project/data/kalshi-source-snapshots/hdd").mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "hdd_scraper",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "hdd-scraper.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore so later test modules get the real kalshi_auth.
    if orig_auth is not None:
        sys.modules["kalshi_auth"] = orig_auth
    else:
        del sys.modules["kalshi_auth"]

    return mod


_mod = _load_hdd_scraper()
parse_chart_data = _mod.parse_chart_data
clean_number = _mod.clean_number
extract_sales_from_text = _mod.extract_sales_from_text


# ===================================================================
# clean_number tests
# ===================================================================

class TestCleanNumber:

    def test_comma_separated_integer(self):
        assert clean_number("290,861") == 290861

    def test_larger_comma_separated(self):
        assert clean_number("175,345") == 175345

    def test_plain_integer(self):
        assert clean_number("12345") == 12345

    def test_empty_string_returns_zero(self):
        assert clean_number("") == 0

    def test_non_numeric_returns_zero(self):
        assert clean_number("abc") == 0

    def test_whitespace_only_returns_zero(self):
        assert clean_number("   ") == 0

    def test_number_with_spaces(self):
        assert clean_number(" 1,234 ") == 1234

    def test_float_string(self):
        """clean_number tries int() first, then falls back to int(float())."""
        assert clean_number("123.9") == 123


# ===================================================================
# extract_sales_from_text tests
# ===================================================================

class TestExtractSalesFromText:

    def test_simple_k_units(self):
        """'Artist sold 150K units' should extract 150000."""
        result = extract_sales_from_text("Artist sold 150K units")
        assert 150000 in result

    def test_projected_at(self):
        """'projected at 200K' should extract 200000."""
        result = extract_sales_from_text("projected at 200K")
        assert 200000 in result

    def test_empty_text_returns_empty_list(self):
        assert extract_sales_from_text("") == []

    def test_none_text_returns_empty_list(self):
        assert extract_sales_from_text(None) == []

    def test_no_sales_numbers(self):
        result = extract_sales_from_text("The weather is nice today.")
        assert result == []

    def test_building_toward(self):
        result = extract_sales_from_text("Album is building toward 300K")
        assert 300000 in result

    def test_opening_with(self):
        result = extract_sales_from_text("Opening with 250K units")
        assert 250000 in result

    def test_first_week_number(self):
        result = extract_sales_from_text("first-week number: 290K")
        assert 290000 in result

    def test_comma_separated_units(self):
        result = extract_sales_from_text("Artist moved 175,000 units this week")
        assert 175000 in result

    def test_numbers_outside_reasonable_range_ignored(self):
        """Numbers below 5000 or above 5,000,000 should be filtered out."""
        # "2K units" -> 2000 which is below the 5000 floor
        result = extract_sales_from_text("Artist sold 2K units")
        assert 2000 not in result

    def test_very_large_number_ignored(self):
        """A number above 5,000,000 should be filtered out."""
        result = extract_sales_from_text("Total sales of 10000K units")
        # 10000K -> 10,000,000, which exceeds the 5M cap
        for n in result:
            assert n <= 5_000_000


# ===================================================================
# parse_chart_data tests
# ===================================================================

class TestParseChartData:

    def test_empty_string_returns_empty_list(self):
        assert parse_chart_data("") == []

    def test_none_returns_empty_list(self):
        assert parse_chart_data(None) == []

    def test_parses_single_entry(self):
        """A well-formed tab-separated chart line should parse correctly."""
        line = "1\t1\tDrake | For All The Dogs\tRepublic\t290,861\t175,345\t50,000\t65,000"
        entries = parse_chart_data(line)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["artist"] == "Drake"
        assert entry["album"] == "For All The Dogs"
        assert entry["label"] == "Republic"
        assert entry["rank"] == 1
        assert entry["activity"] == 290861
        assert entry["albums"] == 175345

    def test_parses_multiple_entries(self):
        lines = (
            "1\t1\tArtist A | Album A\tLabel A\t100,000\t50,000\n"
            "2\t2\tArtist B | Album B\tLabel B\t80,000\t40,000\n"
        )
        entries = parse_chart_data(lines)
        assert len(entries) == 2
        assert entries[0]["artist"] == "Artist A"
        assert entries[1]["artist"] == "Artist B"

    def test_skips_malformed_lines(self):
        """Lines with fewer than 4 tab-separated fields are skipped."""
        data = "short\tline\n1\t1\tReal | Entry\tLabel\t100,000"
        entries = parse_chart_data(data)
        assert len(entries) == 1
        assert entries[0]["artist"] == "Real"

    def test_handles_dashes_for_last_week(self):
        line = "--\t5\tNew Artist | Debut\tIndie\t30,000\t20,000"
        entries = parse_chart_data(line)
        assert len(entries) == 1
        assert entries[0]["last_week"] is None
        assert entries[0]["rank"] == 5
