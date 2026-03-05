"""Tests for weather bot bug fixes: negative edge guard, tightened sigma, relaxed liquidity.

Covers:
- weather_sigma returns 1.5 for days_out=0 (not 2.0) with no calibration
- weather_sigma returns ~2.21 for days_out=2 (1.5 + 0.5*sqrt(2)) with no calibration
- MIN_LIQUIDITY_VOLUME is 10 (reduced from 50)
- is_market_liquid with volume=10 passes (verifying the reduced threshold)
- weather_probability produces tighter probabilities with the new sigma
"""

import math
import pytest
from probability import (
    weather_sigma,
    weather_probability,
    is_market_liquid,
    MIN_LIQUIDITY_VOLUME,
    _reset_calibration,
)


class TestTightenedSigma:
    """Verify sigma intercept is 1.5 (not 2.0) for tighter calibration."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_sigma_day0_returns_1_5(self):
        """weather_sigma(0) should return 1.5 with no calibration (was 2.0)."""
        sigma = weather_sigma(0)
        assert sigma == pytest.approx(1.5, abs=0.01)

    def test_sigma_day2_returns_2_21(self):
        """weather_sigma(2) should return 1.5 + 0.5*sqrt(2) ~= 2.207 (was ~2.71)."""
        expected = 1.5 + 0.5 * math.sqrt(2)
        sigma = weather_sigma(2)
        assert sigma == pytest.approx(expected, abs=0.01)

    def test_sigma_day1_returns_2_0(self):
        """weather_sigma(1) should return 1.5 + 0.5*sqrt(1) = 2.0."""
        sigma = weather_sigma(1)
        assert sigma == pytest.approx(2.0, abs=0.01)

    def test_tighter_sigma_higher_probability_far_above(self):
        """With tighter sigma (1.5 vs 2.0), forecast far above threshold
        should produce HIGHER probability (more confident)."""
        # Forecast 5F above threshold at day 0
        prob = weather_probability(91, 86, "T", days_out=0)
        # With sigma=1.5, z = (86-91)/1.5 = -3.33, P(>86) should be very high
        assert prob > 0.95


class TestRelaxedLiquidity:
    """Verify MIN_LIQUIDITY_VOLUME is reduced from 50 to 10."""

    def test_min_liquidity_volume_is_10(self):
        """Module-level MIN_LIQUIDITY_VOLUME should be 10 (was 50)."""
        assert MIN_LIQUIDITY_VOLUME == 10

    def test_market_with_volume_10_passes(self):
        """A market with volume=10 should pass the default liquidity filter."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 10}
        assert is_market_liquid(market) is True

    def test_market_with_volume_5_passes_with_override(self):
        """A market with volume=5 should pass when min_volume=5 is passed."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 5}
        assert is_market_liquid(market, min_volume=5) is True

    def test_market_with_volume_9_fails_default(self):
        """A market with volume=9 should fail the default liquidity filter (threshold=10)."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 9}
        assert is_market_liquid(market) is False

    def test_wide_spread_still_rejected(self):
        """Even with relaxed volume, wide spread markets should still fail."""
        market = {"yes_bid": 30, "yes_ask": 60, "volume": 100}
        assert is_market_liquid(market) is False
