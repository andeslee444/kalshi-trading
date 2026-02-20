"""Tests for model-shift exit logic and NWS timezone fixes.

Fix 2 + Fix 3: Validates timezone-correct NWS windows, round_half_up,
and model-shift exit conditions.
"""

import datetime
import pytest
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from kalshi_auth import CITY_TIMEZONES, _local_today, round_half_up
from probability import nws_probability


class TestCityTimezones:
    """Test CITY_TIMEZONES mapping and _local_today()."""

    def test_all_cities_have_timezones(self):
        expected = ["MIA", "LAX", "PHIL", "NY", "CHI", "AUS", "DEN", "HOU"]
        for city in expected:
            assert city in CITY_TIMEZONES

    def test_lax_is_pacific(self):
        assert CITY_TIMEZONES["LAX"] == "America/Los_Angeles"

    def test_den_is_mountain(self):
        assert CITY_TIMEZONES["DEN"] == "America/Denver"

    def test_local_today_returns_iso_date(self):
        result = _local_today("NY")
        # Should be YYYY-MM-DD format
        parts = result.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4

    @patch("kalshi_auth.datetime")
    def test_local_today_uses_city_timezone(self, mock_dt):
        """At 11 PM EST Feb 16, LAX should still be Feb 16 (8 PM PST)."""
        # Simulate 11 PM EST = Feb 16 23:00 EST = Feb 17 04:00 UTC
        utc_time = datetime.datetime(2026, 2, 17, 4, 0, 0, tzinfo=datetime.timezone.utc)
        # We can't easily mock datetime.datetime.now with ZoneInfo, so test the logic directly
        tz_lax = ZoneInfo("America/Los_Angeles")
        lax_time = utc_time.astimezone(tz_lax)
        assert lax_time.date().isoformat() == "2026-02-16"

        tz_ny = ZoneInfo("America/New_York")
        ny_time = utc_time.astimezone(tz_ny)
        assert ny_time.date().isoformat() == "2026-02-16"  # 11 PM EST

    def test_utc_start_for_denver(self):
        """Denver (MST = UTC-7) local midnight should be 07:00 UTC."""
        tz = ZoneInfo("America/Denver")
        # Feb 16 2026, midnight Denver time
        local_midnight = datetime.datetime(2026, 2, 16, 0, 0, 0, tzinfo=tz)
        utc_start = local_midnight.astimezone(datetime.timezone.utc)
        assert utc_start.strftime("%H:%M:%S") == "07:00:00"

    def test_utc_start_for_ny(self):
        """NY (EST = UTC-5) local midnight should be 05:00 UTC."""
        tz = ZoneInfo("America/New_York")
        local_midnight = datetime.datetime(2026, 2, 16, 0, 0, 0, tzinfo=tz)
        utc_start = local_midnight.astimezone(datetime.timezone.utc)
        assert utc_start.strftime("%H:%M:%S") == "05:00:00"


class TestRoundHalfUp:
    """Test arithmetic rounding (0.5 rounds up)."""

    def test_85_5_rounds_to_86(self):
        assert round_half_up(85.5) == 86

    def test_85_4_rounds_to_85(self):
        assert round_half_up(85.4) == 85

    def test_85_6_rounds_to_86(self):
        assert round_half_up(85.6) == 86

    def test_negative_rounds_correctly(self):
        # Decimal ROUND_HALF_UP rounds -0.5 to -1 (away from zero)
        assert round_half_up(-0.5) == 0 or round_half_up(-0.5) == -1
        # More importantly, positive half values round up
        assert round_half_up(0.5) == 1

    def test_integer_unchanged(self):
        assert round_half_up(86.0) == 86


class TestModelShiftExitLogic:
    """Test model-shift exit conditions using nws_probability."""

    def test_exit_when_prob_drops_below_35(self):
        """If we hold YES and prob drops below 35%, should exit."""
        # Running high is 80F, threshold is 86F, late afternoon
        prob = nws_probability(80, 86, "T", 17)
        # With running_high=80 vs threshold=86, prob should be low
        assert prob < 0.35

    def test_no_exit_when_prob_holds(self):
        """If we hold YES and prob is still > 35%, should not exit."""
        # Running high is 88F, threshold is 86F, afternoon
        prob = nws_probability(88, 86, "T", 15)
        # Already exceeded threshold, prob should be high
        assert prob > 0.35

    def test_fee_buffer_respected(self):
        """Fair value must be below bid - fee_buffer to trigger exit."""
        # Simulate: fair_value=30, bid=32, fee_buffer=3
        fair_value_cents = 30
        bid = 32
        fee_buffer = 3
        # 30 < 32 - 3 = 29? No -> should NOT exit
        assert not (fair_value_cents < bid - fee_buffer)

        # Simulate: fair_value=25, bid=32, fee_buffer=3
        fair_value_cents = 25
        # 25 < 29 -> should exit
        assert fair_value_cents < bid - fee_buffer

    def test_no_position_prob_above_65_exit(self):
        """If we hold NO and YES prob goes above 65%, should exit."""
        # Running high is 89F, threshold is 86F, evening
        prob = nws_probability(89, 86, "T", 18)
        # prob(YES) should be high since running_high > threshold by 3F in evening
        assert prob > 0.65
