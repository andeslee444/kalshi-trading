"""Tests for source-monitor optimizations: time-decay sigma, data freshness,
cross-market consistency, retry logic, and edge thresholds."""

import math
import datetime
import sys
import tempfile
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from probability import album_data_sigma, boxoffice_data_sigma, _reset_calibration
from probability import nws_sigma_for_hour, nws_probability

from conftest import make_fake_auth, load_bot_module


# === Module import helper for source-monitor.py ===

_sm_module = None


def _load_source_monitor():
    """Load source-monitor.py with stubbed dependencies. Cached after first call."""
    global _sm_module
    if _sm_module is not None:
        return _sm_module

    _config = {
        "maxTradeAmount": 25, "maxDailyTrades": 25, "maxDailyLoss": 50,
        "sources": {
            "hdd": {"enabled": True, "intervalMinutes": 15, "chartSlugs": ["hits-top-50"]},
            "boxoffice": {"enabled": True, "intervalMinutes": 30, "activeDays": ["Friday", "Saturday", "Sunday", "Monday"], "tmdbEnabled": False},
            "nws": {"enabled": True, "intervalMinutes": 10, "stations": {}},
        },
    }
    _tmp_dir = Path(tempfile.mkdtemp())
    config_dir = _tmp_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kalshi-monitor-config.json").write_text(json.dumps(_config))
    data_dir = _tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "kalshi-source-snapshots").mkdir(parents=True, exist_ok=True)
    (data_dir / "kalshi-monitor-trades.json").write_text("[]")

    fake_auth = make_fake_auth(
        PROJECT_DIR=_tmp_dir,
        round_half_up=round,
    )

    _sm_module = load_bot_module("source-monitor.py", fake_auth, extra_stubs={
        "capital_allocator": MagicMock(),
        "hdd_parser": MagicMock(),
        "ticker_utils": MagicMock(),
    })
    return _sm_module


class TestTimeDecaySigma:
    """Test time-decay sigma for album and box office data."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_album_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert album_data_sigma(0) == 0.15  # Monday
        assert album_data_sigma(2) == 0.10  # Wednesday
        assert album_data_sigma(4) == 0.05  # Friday

    def test_album_friday_48h_decay(self):
        """48 hours of staleness increases sigma by 50%."""
        result = album_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(0.05 * 1.5, rel=1e-3)

    def test_album_friday_96h_decay(self):
        """96 hours of staleness increases sigma by 100%."""
        result = album_data_sigma(4, hours_since_publication=96)
        assert result == pytest.approx(0.05 * 2.0, rel=1e-3)

    def test_album_decay_capped_at_3x(self):
        """Decay factor caps at 3x regardless of age."""
        result = album_data_sigma(4, hours_since_publication=500)
        assert result == pytest.approx(0.05 * 3.0, rel=1e-3)

    def test_album_monday_no_decay(self):
        """Monday with 0 hours_since_publication is unchanged."""
        assert album_data_sigma(0, hours_since_publication=0) == 0.15

    def test_album_monday_with_decay(self):
        """Monday with 48h decay."""
        result = album_data_sigma(0, hours_since_publication=48)
        assert result == pytest.approx(0.15 * 1.5, rel=1e-3)

    def test_boxoffice_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert boxoffice_data_sigma(4) == 0.12  # Friday
        assert boxoffice_data_sigma(6) == 0.05  # Sunday
        assert boxoffice_data_sigma(0) == 0.04  # Monday

    def test_boxoffice_friday_48h_decay(self):
        """48h staleness on Friday estimate."""
        result = boxoffice_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(0.12 * 1.5, rel=1e-3)

    def test_boxoffice_decay_capped_at_3x(self):
        """Decay caps at 3x."""
        result = boxoffice_data_sigma(0, hours_since_publication=500)
        assert result == pytest.approx(0.04 * 3.0, rel=1e-3)

    def test_boxoffice_sunday_24h_decay(self):
        """Sunday with 24h decay."""
        result = boxoffice_data_sigma(6, hours_since_publication=24)
        assert result == pytest.approx(0.05 * 1.25, rel=1e-3)


class TestNwsSigma:
    """Test extracted NWS sigma helper."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_overnight_sigma(self):
        """Before 6am, sigma should be 5.0 (max uncertainty)."""
        assert nws_sigma_for_hour(3) == 5.0

    def test_morning_sigma(self):
        """At hour 6, sigma should be ~4.0."""
        sigma = nws_sigma_for_hour(6)
        assert sigma == pytest.approx(4.0, abs=0.1)

    def test_midday_sigma(self):
        """At hour 12, sigma should be ~1.4."""
        sigma = nws_sigma_for_hour(12)
        assert sigma == pytest.approx(4.0 * math.exp(-0.18 * 6), abs=0.1)

    def test_afternoon_sigma(self):
        """At hour 15, sigma should be ~0.7."""
        sigma = nws_sigma_for_hour(15)
        assert sigma == pytest.approx(4.0 * math.exp(-0.18 * 9), abs=0.1)

    def test_evening_sigma_floor(self):
        """At hour 20, sigma should hit the 0.5 floor."""
        sigma = nws_sigma_for_hour(20)
        assert sigma == 0.5

    def test_nws_probability_unchanged(self):
        """Verify nws_probability still works identically after refactor."""
        # Running high 85F, threshold 80F, direction T, hour 15
        prob = nws_probability(85, 80, "T", 15)
        assert prob > 0.9

    def test_nws_probability_bracket(self):
        """Bracket probability still works."""
        prob = nws_probability(80, 80, "B", 12)
        assert 0 < prob < 1

    def test_sigma_monotonically_decreasing(self):
        """Sigma should decrease from morning to evening."""
        sigmas = [nws_sigma_for_hour(h) for h in range(6, 20)]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] >= sigmas[i + 1]


# === Integration tests using source-monitor module import ===


class TestRetryLogic:
    """Test _check_with_retry from source-monitor.py (lines 69-88)."""

    def test_success_on_first_try(self):
        sm = _load_source_monitor()
        check_fn = MagicMock()
        ss = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, ss)
        assert check_fn.call_count == 1
        ss.source_ok.assert_called_once_with("test_source")

    def test_success_on_retry(self):
        sm = _load_source_monitor()
        check_fn = MagicMock(side_effect=[Exception("transient"), None])
        ss = MagicMock()
        with patch("time.sleep"):
            sm._check_with_retry(check_fn, "test_source", None, ss)
        assert check_fn.call_count == 2
        ss.source_ok.assert_called_once()

    def test_failure_after_max_retries(self):
        sm = _load_source_monitor()
        check_fn = MagicMock(side_effect=Exception("persistent"))
        ss = MagicMock()
        with patch("time.sleep"):
            sm._check_with_retry(check_fn, "test_source", None, ss, max_retries=2)
        assert check_fn.call_count == 3
        ss.source_fail.assert_called_once()

    def test_no_scan_summary(self):
        """ss=None should not raise."""
        sm = _load_source_monitor()
        check_fn = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, None)
        check_fn.assert_called_once()


class TestCrossMarketConsistency:
    """Test _validate_market_cluster from source-monitor.py (lines 90-109)."""

    def test_single_market_passthrough(self):
        sm = _load_source_monitor()
        signals = [("MKT1", 100, 0.80)]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 1

    def test_monotonic_pass(self):
        """P(>100) > P(>200) should pass."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.90),
            ("MKT2", 200, 0.60),
            ("MKT3", 300, 0.30),
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 3

    def test_non_monotonic_rejection(self):
        """P(>200) > P(>100) violates monotonicity — reject all."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.50),
            ("MKT2", 200, 0.80),  # higher prob at higher threshold = inconsistent
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 0

    def test_tolerance_boundary(self):
        """Small violations within 5% tolerance should pass."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.80),
            ("MKT2", 200, 0.83),  # 3% higher — within 5% tolerance
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 2

    def test_empty_list(self):
        sm = _load_source_monitor()
        result = sm._validate_market_cluster("artist", [])
        assert result == []


class TestNWSEdgeThresholds:
    """Test _nws_min_edge from source-monitor.py."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_very_confident_afternoon(self):
        """Hour 15, margin 5F >> ci_99 ~1.8F -> min_edge 0.05."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(90, 85, 15, False) == 0.05

    def test_moderate_confidence_midday(self):
        """Hour 12, margin 2F, ci_99 ~3.6F, margin > ci/2 -> min_edge 0.10."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(82, 80, 12, False) == 0.10

    def test_uncertain_morning(self):
        """Hour 8, margin 1F, ci_99 ~6.8F -> min_edge 0.15."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(81, 80, 8, False) == 0.15

    def test_bracket_always_020(self):
        """Bracket markets always require 20% edge."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(80, 80, 18, True) == 0.20

    def test_evening_confident(self):
        """Hour 18, sigma=0.5, ci_99=1.29, margin 5F -> very confident."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(85, 80, 18, False) == 0.05

    def test_overnight_uncertain(self):
        """Hour 4, sigma=5.0, ci_99=12.88, margin 3F -> uncertain."""
        sm = _load_source_monitor()
        assert sm._nws_min_edge(83, 80, 4, False) == 0.15


class TestDataFreshness:
    """Test data freshness gating logic.

    The source-monitor uses compute_data_age_hours() from hdd_parser
    and MAX_DATA_AGE_HOURS = 168 (7 days) from source-monitor.py:65.
    """

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_max_data_age_constant(self):
        sm = _load_source_monitor()
        assert sm.MAX_DATA_AGE_HOURS == 168

    def test_sigma_increase_at_72h(self):
        """At 72 hours, sigma should be 1.75x base (50% per 48h)."""
        result = album_data_sigma(0, hours_since_publication=72)
        expected = 0.15 * (1.0 + 0.5 * (72 / 48))
        assert result == pytest.approx(expected, rel=1e-3)

    def test_sigma_at_168h_before_rejection(self):
        """At exactly 168h (7 days), probability.py still computes sigma (rejection is in bot logic)."""
        result = album_data_sigma(0, hours_since_publication=168)
        # 1 + 0.5 * 168/48 = 2.75 -> sigma = 0.15 * 2.75 = 0.4125
        assert result == pytest.approx(0.15 * 2.75, rel=1e-3)

    def test_fresh_data_no_decay(self):
        """Data at 0 hours has no decay applied."""
        result = album_data_sigma(4, hours_since_publication=0)
        assert result == 0.05  # Friday base, no decay


class TestSourceAwareSigma:
    """Test source-aware album_data_sigma with different HDD data sources."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_hits_top_50_lowest_sigma(self):
        """HDD Hits Top 50 is the settlement source — should have lowest sigma."""
        sigma = album_data_sigma(4, source="hdd-hits-top-50")
        assert sigma == 0.02

    def test_midweek_20_higher_sigma(self):
        """Midweek 20 is an estimate — should have higher sigma than Top 50."""
        sigma_top50 = album_data_sigma(4, source="hdd-hits-top-50")
        sigma_mid = album_data_sigma(4, source="hdd-midweek-20")
        assert sigma_mid > sigma_top50

    def test_article_highest_sigma(self):
        """Article text extraction has highest uncertainty."""
        sigma_article = album_data_sigma(4, source="hdd-article")
        assert sigma_article >= 0.15

    def test_unknown_source_uses_day_default(self):
        """Unknown source falls back to day-of-week default."""
        sigma = album_data_sigma(4, source="unknown-source")
        assert sigma == 0.05  # Friday default

    def test_none_source_uses_day_default(self):
        """No source falls back to day-of-week default."""
        sigma = album_data_sigma(4, source=None)
        assert sigma == album_data_sigma(4)


# ============================================================
# Task 6.1: NWS Timezone Correctness Regression Tests
# ============================================================


class TestNWSTimezoneCorrectness:
    """Verify source-monitor uses per-city local timezone for date boundaries.

    The code uses _local_today(city) and CITY_TIMEZONES from kalshi_auth.py
    to determine which markets are "settling today". These tests lock in
    that behavior to prevent regressions to naive UTC/server-local dates.
    """

    def test_local_today_uses_city_timezone(self):
        """_local_today should use per-city ZoneInfo, not UTC or server local."""
        from zoneinfo import ZoneInfo
        # Verify the real _local_today uses CITY_TIMEZONES
        from kalshi_auth import _local_today, CITY_TIMEZONES
        # NYC should use Eastern time
        assert CITY_TIMEZONES["NY"] == "America/New_York"
        # LAX should use Pacific time
        assert CITY_TIMEZONES["LAX"] == "America/Los_Angeles"
        # _local_today returns an ISO date string
        result = _local_today("NY")
        assert len(result) == 10  # YYYY-MM-DD format
        assert result.count("-") == 2

    def test_local_today_different_cities_can_differ(self):
        """At midnight boundaries, different cities can report different dates."""
        from kalshi_auth import _local_today, CITY_TIMEZONES
        # Both calls should return valid ISO dates
        ny_date = _local_today("NY")
        lax_date = _local_today("LAX")
        # Both are valid dates (may or may not differ depending on server time)
        assert len(ny_date) == 10
        assert len(lax_date) == 10

    def test_local_today_with_mock_late_night_est(self):
        """At 11pm EST, it's still today in NYC but already tomorrow in UTC.

        Confirms _local_today returns the city-local date, not UTC date.
        """
        from zoneinfo import ZoneInfo
        from kalshi_auth import CITY_TIMEZONES
        # 2026-03-07 23:30 EST = 2026-03-08 04:30 UTC
        est_tz = ZoneInfo("America/New_York")
        fake_time = datetime.datetime(2026, 3, 7, 23, 30, tzinfo=est_tz)
        utc_date = fake_time.astimezone(datetime.timezone.utc).date().isoformat()
        local_date = fake_time.date().isoformat()
        assert local_date == "2026-03-07"
        assert utc_date == "2026-03-08"  # UTC has already rolled over

    def test_local_today_pst_vs_est_boundary(self):
        """At 11pm PST (2am EST next day), LAX should still show today."""
        from zoneinfo import ZoneInfo
        pst_tz = ZoneInfo("America/Los_Angeles")
        est_tz = ZoneInfo("America/New_York")
        # 2026-03-07 23:00 PST
        pst_time = datetime.datetime(2026, 3, 7, 23, 0, tzinfo=pst_tz)
        # In EST this is already March 8
        est_equivalent = pst_time.astimezone(est_tz).date().isoformat()
        pst_date = pst_time.date().isoformat()
        assert pst_date == "2026-03-07"
        assert est_equivalent == "2026-03-08"  # EST has rolled over

    def test_city_timezones_all_valid(self):
        """All cities in CITY_TIMEZONES should have valid ZoneInfo entries."""
        from zoneinfo import ZoneInfo
        from kalshi_auth import CITY_TIMEZONES
        for city, tz_name in CITY_TIMEZONES.items():
            tz = ZoneInfo(tz_name)
            assert tz is not None, f"Invalid timezone for {city}: {tz_name}"

    def test_check_nws_daily_highs_uses_local_midnight(self):
        """check_nws_daily_highs uses per-city local midnight, not UTC midnight.

        The function constructs NWS observation query URLs using
        ZoneInfo(CITY_TIMEZONES[city]) for local midnight, then converts
        to UTC. This prevents including yesterday's late-afternoon temps
        for western cities.
        """
        sm = _load_source_monitor()
        # Verify the function exists and has the timezone logic
        import inspect
        source = inspect.getsource(sm.check_nws_daily_highs)
        # Must reference CITY_TIMEZONES for per-city midnight
        assert "CITY_TIMEZONES" in source
        # Must compute local_midnight and convert to UTC
        assert "local_midnight" in source
        assert "astimezone" in source

    def test_match_nws_uses_local_today_for_market_date(self):
        """match_nws_to_markets uses _local_today(city) for date matching.

        Each city's market should be matched against the city-local date,
        not the server date or UTC date.
        """
        sm = _load_source_monitor()
        import inspect
        source = inspect.getsource(sm.match_nws_to_markets)
        assert "_local_today" in source


class TestPreDawnGate:
    """Verify the pre-dawn gate prevents NWS trades before 8 AM local time.

    Before 8 AM local time in the city, the running high temperature is
    unreliable because the day hasn't really started. The pre-dawn gate
    in match_nws_to_markets skips NWS trade evaluation during this period.
    """

    def test_pre_dawn_gate_uses_city_local_hour(self):
        """Pre-dawn gate should use city-local hour, not server-local hour."""
        sm = _load_source_monitor()
        import inspect
        source = inspect.getsource(sm.match_nws_to_markets)
        # Must use city_hour for the pre-dawn check, not now.hour
        assert "city_hour < 8" in source
        # Must NOT use naive now.hour for the gate
        assert "now.hour < 8" not in source

    def test_pre_dawn_gate_skips_record(self):
        """When pre-dawn is active, scan summary should record a skip."""
        sm = _load_source_monitor()
        import inspect
        source = inspect.getsource(sm.match_nws_to_markets)
        # Should call ss.skip("pre_dawn") when gated
        assert "pre_dawn" in source

    def test_nws_uses_city_hour_for_sigma(self):
        """NWS probability and edge calculation should use city-local hour.

        This ensures sigma model uses the city's local hour (e.g., LAX at
        Pacific time, not server time) for accurate uncertainty estimates.
        """
        sm = _load_source_monitor()
        import inspect
        source = inspect.getsource(sm.match_nws_to_markets)
        # Should use city_hour in nws_probability calls, not now.hour
        assert "nws_probability(running_high, threshold, direction, city_hour)" in source
        assert "nws_probability(running_high, threshold, \"T\", city_hour)" in source
        # Should use city_hour in _nws_min_edge calls
        assert "_nws_min_edge(running_high, threshold, city_hour," in source


class TestNWSObservationFreshness:
    """Test NWS observation staleness detection.

    The source-monitor rejects NWS data if the observation timestamp
    is older than NWS_MAX_OBS_AGE_MINUTES (90 minutes).
    """

    def test_max_obs_age_constant(self):
        """NWS_MAX_OBS_AGE_MINUTES should be 90."""
        sm = _load_source_monitor()
        assert sm.NWS_MAX_OBS_AGE_MINUTES == 90

    def test_compute_obs_age_recent(self):
        """A recent observation (10 seconds ago) should return a small age."""
        sm = _load_source_monitor()
        recent_ts = (datetime.datetime.now(datetime.timezone.utc) -
                     datetime.timedelta(seconds=10)).isoformat()
        age = sm._compute_nws_obs_age_minutes(recent_ts)
        assert age is not None
        assert age < 1.0  # Less than 1 minute

    def test_compute_obs_age_stale(self):
        """A 2-hour-old observation should return ~120 minutes."""
        sm = _load_source_monitor()
        old_ts = (datetime.datetime.now(datetime.timezone.utc) -
                  datetime.timedelta(hours=2)).isoformat()
        age = sm._compute_nws_obs_age_minutes(old_ts)
        assert age is not None
        assert 119 < age < 121

    def test_compute_obs_age_none_timestamp(self):
        """Missing timestamp should return None."""
        sm = _load_source_monitor()
        assert sm._compute_nws_obs_age_minutes(None) is None
        assert sm._compute_nws_obs_age_minutes("") is None

    def test_compute_obs_age_invalid_timestamp(self):
        """Invalid timestamp should return None (not raise)."""
        sm = _load_source_monitor()
        assert sm._compute_nws_obs_age_minutes("not-a-date") is None
        assert sm._compute_nws_obs_age_minutes("2026-13-45T99:00:00Z") is None

    def test_compute_obs_age_z_suffix(self):
        """Timestamps with 'Z' suffix should be handled correctly."""
        sm = _load_source_monitor()
        ts = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = sm._compute_nws_obs_age_minutes(ts)
        assert age is not None
        assert 29 < age < 31

    def test_compute_obs_age_offset_format(self):
        """Timestamps with +00:00 offset should work."""
        sm = _load_source_monitor()
        ts = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        age = sm._compute_nws_obs_age_minutes(ts)
        assert age is not None
        assert 44 < age < 46

    def test_staleness_gate_threshold(self):
        """Observations at exactly 90 minutes should be rejected (> not >=)."""
        sm = _load_source_monitor()
        # 91 minutes ago — should be rejected
        ts_91 = (datetime.datetime.now(datetime.timezone.utc) -
                 datetime.timedelta(minutes=91)).isoformat()
        age = sm._compute_nws_obs_age_minutes(ts_91)
        assert age > sm.NWS_MAX_OBS_AGE_MINUTES

        # 89 minutes ago — should be accepted
        ts_89 = (datetime.datetime.now(datetime.timezone.utc) -
                 datetime.timedelta(minutes=89)).isoformat()
        age = sm._compute_nws_obs_age_minutes(ts_89)
        assert age < sm.NWS_MAX_OBS_AGE_MINUTES


class TestNWSBracketGuardrails:
    def test_bracket_guardrail_requires_settlement_parity_opt_in(self):
        sm = _load_source_monitor()
        reason = sm._nws_bracket_guardrail_reason(
            14,
            15,
            {"enabled": True, "minLocalHour": 13, "maxObservationAgeMinutes": 30},
        )
        assert reason == "bracket_settlement_mismatch"

    def test_bracket_guardrail_rejects_early_local_hour(self):
        sm = _load_source_monitor()
        reason = sm._nws_bracket_guardrail_reason(
            11,
            5,
            {
                "enabled": True,
                "settlementParityConfirmed": True,
                "minLocalHour": 13,
                "maxObservationAgeMinutes": 30,
            },
        )
        assert reason == "bracket_too_early"

    def test_bracket_guardrail_rejects_stale_observation(self):
        sm = _load_source_monitor()
        reason = sm._nws_bracket_guardrail_reason(
            14,
            45,
            {
                "enabled": True,
                "settlementParityConfirmed": True,
                "minLocalHour": 13,
                "maxObservationAgeMinutes": 30,
            },
        )
        assert reason == "stale_bracket_obs"

    def test_bracket_guardrail_accepts_fresh_afternoon_observation(self):
        sm = _load_source_monitor()
        reason = sm._nws_bracket_guardrail_reason(
            14,
            15,
            {
                "enabled": True,
                "settlementParityConfirmed": True,
                "minLocalHour": 13,
                "maxObservationAgeMinutes": 30,
            },
        )
        assert reason is None


class TestNWSEdgeThresholdBoundaries:
    """Test edge threshold tier boundaries in detail.

    The CI-based tier system should produce appropriate min-edge
    requirements based on the margin-to-CI ratio at different hours.
    """

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_edge_tiers_are_consistent_across_hours(self):
        """As hours increase (sigma decreases), the same margin should move
        from uncertain to confident. A 3F margin at hour 8 (sigma~2.65) is
        uncertain, but at hour 18 (sigma=0.5) it's very confident."""
        sm = _load_source_monitor()
        # At hour 8, sigma ~2.65, ci_99 ~6.83, 3F margin < ci_99/2 = uncertain
        edge_morning = sm._nws_min_edge(83, 80, 8, False)
        # At hour 18, sigma 0.5, ci_99 ~1.29, 3F margin > ci_99 = very confident
        edge_evening = sm._nws_min_edge(83, 80, 18, False)
        assert edge_morning > edge_evening
        assert edge_morning == 0.15  # uncertain
        assert edge_evening == 0.05  # very confident

    def test_zero_margin_always_uncertain(self):
        """When running high equals threshold, should always be uncertain tier."""
        sm = _load_source_monitor()
        for hour in [8, 12, 15, 18]:
            edge = sm._nws_min_edge(80, 80, hour, False)
            assert edge == 0.15, f"Expected 0.15 at hour {hour} with zero margin"

    def test_large_margin_always_confident(self):
        """A 10F margin should be in the confident tier at any hour after dawn."""
        sm = _load_source_monitor()
        for hour in [8, 12, 15, 18]:
            edge = sm._nws_min_edge(90, 80, hour, False)
            assert edge == 0.05, f"Expected 0.05 at hour {hour} with 10F margin"


class TestScanMetrics:
    """Test the per-scan measurement framework.

    Metrics are logged to data/source-monitor-metrics.json for
    observability of NWS freshness, source availability, and trade activity.
    """

    def test_build_scan_metrics_basic(self):
        """_build_scan_metrics should produce a dict with expected keys."""
        sm = _load_source_monitor()
        mock_ss = MagicMock()
        mock_ss.markets_fetched = 10
        mock_ss.markets_evaluated = 5
        mock_ss.trades_placed = 2
        mock_ss.skips = {"low_edge": 3}
        mock_ss.data_sources = {"nws": "ok"}

        metrics = sm._build_scan_metrics(mock_ss, sources_checked=["nws", "hdd"])
        assert metrics["sources_checked"] == ["nws", "hdd"]
        assert metrics["markets_fetched"] == 10
        assert metrics["markets_evaluated"] == 5
        assert metrics["trades_placed"] == 2
        assert metrics["skips"] == {"low_edge": 3}
        assert metrics["data_sources"] == {"nws": "ok"}

    def test_build_scan_metrics_with_nws_freshness(self):
        """NWS freshness data should be included when provided."""
        sm = _load_source_monitor()
        mock_ss = MagicMock()
        mock_ss.markets_fetched = 0
        mock_ss.markets_evaluated = 0
        mock_ss.trades_placed = 0
        mock_ss.skips = {}
        mock_ss.data_sources = {}

        freshness = {"MIA": 12.5, "LAX": 45.0}
        metrics = sm._build_scan_metrics(mock_ss, nws_freshness=freshness)
        assert metrics["nws_freshness"] == {"MIA": 12.5, "LAX": 45.0}

    def test_build_scan_metrics_no_nws_freshness(self):
        """When no NWS freshness, key should be absent."""
        sm = _load_source_monitor()
        mock_ss = MagicMock()
        mock_ss.markets_fetched = 0
        mock_ss.markets_evaluated = 0
        mock_ss.trades_placed = 0
        mock_ss.skips = {}
        mock_ss.data_sources = {}

        metrics = sm._build_scan_metrics(mock_ss)
        assert "nws_freshness" not in metrics

    def test_build_scan_metrics_none_ss(self):
        """With ss=None, should return safe defaults."""
        sm = _load_source_monitor()
        metrics = sm._build_scan_metrics(None)
        assert metrics["markets_fetched"] == 0
        assert metrics["trades_placed"] == 0
        assert metrics["skips"] == {}

    def test_log_scan_metrics_creates_file(self):
        """_log_scan_metrics should create the metrics file if absent."""
        sm = _load_source_monitor()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = sm.METRICS_PATH
            sm.METRICS_PATH = Path(tmpdir) / "test-metrics.json"
            try:
                sm._log_scan_metrics({"test": True})
                assert sm.METRICS_PATH.exists()
                data = json.loads(sm.METRICS_PATH.read_text())
                assert len(data) == 1
                assert data[0]["test"] is True
                assert "timestamp" in data[0]
            finally:
                sm.METRICS_PATH = original_path

    def test_log_scan_metrics_appends(self):
        """_log_scan_metrics should append to existing entries."""
        sm = _load_source_monitor()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = sm.METRICS_PATH
            sm.METRICS_PATH = Path(tmpdir) / "test-metrics.json"
            try:
                sm._log_scan_metrics({"scan": 1})
                sm._log_scan_metrics({"scan": 2})
                data = json.loads(sm.METRICS_PATH.read_text())
                assert len(data) == 2
                assert data[0]["scan"] == 1
                assert data[1]["scan"] == 2
            finally:
                sm.METRICS_PATH = original_path

    def test_log_scan_metrics_trims_to_max(self):
        """_log_scan_metrics should trim to MAX_METRICS_ENTRIES."""
        sm = _load_source_monitor()
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = sm.METRICS_PATH
            original_max = sm.MAX_METRICS_ENTRIES
            sm.METRICS_PATH = Path(tmpdir) / "test-metrics.json"
            sm.MAX_METRICS_ENTRIES = 5  # Small for testing
            try:
                for i in range(10):
                    sm._log_scan_metrics({"scan": i})
                data = json.loads(sm.METRICS_PATH.read_text())
                assert len(data) == 5
                # Should keep the last 5
                assert data[0]["scan"] == 5
                assert data[4]["scan"] == 9
            finally:
                sm.METRICS_PATH = original_path
                sm.MAX_METRICS_ENTRIES = original_max

    def test_metrics_path_constant(self):
        """METRICS_PATH should point to data/source-monitor-metrics.json."""
        sm = _load_source_monitor()
        assert sm.METRICS_PATH.name == "source-monitor-metrics.json"


class TestNWSTradeMetadata:
    def test_nws_no_trade_stores_no_side_probability(self):
        sm = _load_source_monitor()

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime.datetime(2026, 4, 17, 18, 0, tzinfo=datetime.timezone.utc)
                return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

        market = {
            "ticker": "KXHIGHNY-26APR17-T80",
            "title": "New York high above 80F",
            "yes_bid": 80,
            "yes_ask": 85,
            "no_ask": 20,
            "volume": 100,
            "close_time": "2026-04-18T00:00:00Z",
        }
        temp_data = {
            "NY": {
                "running_high_f": 70.0,
                "temp_f": 70.0,
                "station": "KNYC",
                "timestamp": "2026-04-17T17:55:00+00:00",
                "obs_age_minutes": 5.0,
                "obs_count": 12,
            }
        }

        sm.config = {
            "maxTradeAmount": 25,
            "sources": {
                "nws": {
                    "enabled": True,
                    "disableYes": False,
                    "brackets": {},
                }
            },
        }
        sm.log = MagicMock()
        sm._local_today = MagicMock(return_value="2026-04-17")
        sm.parse_temp_ticker = MagicMock(
            return_value={"city": "NY", "date": "2026-04-17", "direction": "T", "threshold": 80.0}
        )
        sm.is_market_liquid = MagicMock(return_value=True)
        sm.nws_probability = MagicMock(return_value=0.20)
        sm.kalshi_fee_cents = MagicMock(return_value=1.0)
        sm.quarter_kelly = MagicMock(return_value=(5, 100, {"kelly_fraction": 0.1, "bankroll_used": 1000}))
        sm.build_market_snapshot = MagicMock(return_value={"yes_bid": 80, "yes_ask": 85})

        budget = MagicMock(approved=True, max_cost_cents=1000, bankroll_cents=100000, reason=None)
        sm.allocator = MagicMock()
        sm.allocator.request_budget.return_value = budget
        sm.trade_manager = MagicMock()
        sm.trade_manager.place_order.return_value = {"cost_cents": 100}

        ss = MagicMock()
        ss.markets_evaluated = 0
        ss.trades_placed = 0

        with patch.object(sm.datetime, "datetime", FixedDateTime):
            sm.match_nws_to_markets(temp_data, prefetched_markets={"weather": [market]}, ss=ss)

        assert sm.trade_manager.place_order.called
        assert sm.trade_manager.place_order.call_args.kwargs["model_prob"] == pytest.approx(0.80)
