"""Tests for HDD chart parsing and sales extraction."""

import logging
import pytest
from unittest.mock import MagicMock, patch
from hdd_parser import parse_chart_data, clean_number, extract_sales_from_text, get_album_sales, compute_data_age_hours, parse_album_threshold


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

    def test_albums_is_second_number(self):
        """Albums column should be the second number in chart data (after Activity)."""
        line = "1\t1\tArtist | Album\tLabel\t290,861\t175,345\t50,000"
        entries = parse_chart_data(line)
        assert len(entries) == 1
        assert entries[0]["activity"] == 290861
        assert entries[0]["albums"] == 175345

    def test_single_number_no_albums_key(self):
        """When only one number present, albums key doesn't exist (only activity)."""
        line = "1\t1\tArtist | Album\tLabel\t290,861"
        entries = parse_chart_data(line)
        assert len(entries) == 1
        assert entries[0]["activity"] == 290861
        assert "albums" not in entries[0]


# ===================================================================
# compute_data_age_hours tests
# ===================================================================

class TestComputeDataAgeHours:

    def test_empty_string_returns_zero(self):
        assert compute_data_age_hours("") == 0

    def test_none_returns_zero(self):
        assert compute_data_age_hours(None) == 0

    def test_unparseable_returns_zero(self):
        assert compute_data_age_hours("not-a-date") == 0

    def test_recent_date_returns_positive(self):
        """A date from 1 hour ago should return ~1."""
        import datetime
        one_hour_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
        age = compute_data_age_hours(one_hour_ago)
        assert 0.9 < age < 1.5

    def test_unparseable_date_logs_warning(self, caplog):
        """Unparseable dates should log a warning and return 0."""
        with caplog.at_level(logging.WARNING, logger="hdd_parser"):
            result = compute_data_age_hours("garbage-date-string")
        assert result == 0
        assert "Unparseable chart_date" in caplog.text


# ===================================================================
# parse_album_threshold tests
# ===================================================================

class TestParseAlbumThreshold:

    def test_k_units_pattern(self):
        """'200K copies' should parse to 200000."""
        assert parse_album_threshold("Will Artist sell 200K copies?") == 200000

    def test_comma_separated_units(self):
        """'150,000 units' should parse to 150000."""
        assert parse_album_threshold("Will it sell more than 150,000 units?") == 150000

    def test_more_than_pattern(self):
        """'more than 200' should parse (with *1000 normalization)."""
        assert parse_album_threshold("Sell more than 200") == 200000

    def test_over_pattern(self):
        """'over 200' should parse (with *1000 normalization)."""
        assert parse_album_threshold("Will sales go over 200") == 200000

    def test_ticker_fallback(self):
        """Ticker T200 should parse to 200000."""
        assert parse_album_threshold("Some unparseable title", "KXALBUMSALES-ART-T200") == 200000

    def test_ticker_large_number(self):
        """Ticker T150000 should stay at 150000 (no *1000)."""
        assert parse_album_threshold("No match", "KXALBUMSALES-T150000") == 150000

    def test_returns_none_on_no_match(self):
        assert parse_album_threshold("No numbers here", "NOTHRESHOLD") is None

    def test_thousand_word_pattern(self):
        """'200 thousand' should parse to 200000."""
        assert parse_album_threshold("Will sales reach 200 thousand") == 200000


# ===================================================================
# HDD Sanity CMS health check tests
# ===================================================================

class TestHddHealthCheck:
    """Test HDD Sanity CMS health check functions."""

    def test_check_sanity_health_success(self):
        """Health check returns True when Sanity responds."""
        from hdd_parser import check_sanity_health
        response = MagicMock(status_code=200, json=lambda: {"result": []}, content=b'{"result":[]}')
        with patch("requests.get", return_value=response):
            assert check_sanity_health("8aky18h3") is True
        response.close.assert_called_once()

    def test_check_sanity_health_failure(self):
        """Health check returns False on timeout/error."""
        from hdd_parser import check_sanity_health
        with patch("requests.get", side_effect=Exception("timeout")):
            assert check_sanity_health("8aky18h3") is False

    def test_check_sanity_health_bad_status(self):
        """Health check returns False on non-200 response."""
        from hdd_parser import check_sanity_health
        response = MagicMock(status_code=500, content=b"error")
        with patch("requests.get", return_value=response):
            assert check_sanity_health("8aky18h3") is False
        response.close.assert_called_once()

    def test_sanity_query_closes_response(self):
        from hdd_parser import sanity_query

        response = MagicMock()
        response.status_code = 200
        response.content = b'{"result":[]}'
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": []}

        with patch("requests.get", return_value=response):
            assert sanity_query('*[_type=="chart"][0]{_id}') == []

        response.close.assert_called_once()
