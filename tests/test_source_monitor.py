"""Tests for source-monitor optimizations: time-decay sigma, data freshness,
cross-market consistency, retry logic, and edge thresholds."""

import math
import datetime
import pytest
from unittest.mock import MagicMock
from probability import album_data_sigma, boxoffice_data_sigma, _reset_calibration


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
