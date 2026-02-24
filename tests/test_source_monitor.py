"""Tests for source-monitor optimizations: time-decay sigma, data freshness,
cross-market consistency, retry logic, and edge thresholds."""

import math
import datetime
import pytest
from unittest.mock import MagicMock
from probability import album_data_sigma, boxoffice_data_sigma, _reset_calibration
from probability import nws_sigma_for_hour, nws_probability


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
