"""Tests for parse_ticker() and compute_probability() in weather-bot.py."""

import json
import sys
import pytest
from pathlib import Path

from probability import _reset_calibration
from conftest import make_fake_auth, load_bot_module


# ---------------------------------------------------------------------------
# Import helper -- weather-bot.py has a hyphen in its name and performs
# heavy side-effects at import time (reads config, instantiates client).
# We stub those out so we can test the pure functions.
# ---------------------------------------------------------------------------

_fake_project = Path("/tmp/fake_project")
(_fake_project / "config").mkdir(parents=True, exist_ok=True)
(_fake_project / "config" / "kalshi-config.json").write_text(json.dumps({
    "cities": {},
    "mode": "demo",
    "maxTradeAmount": 1,
    "edgeThreshold": 0.10,
    "scanIntervalMinutes": 60,
    "maxDailyTrades": 10,
    "maxDailyLoss": 10,
}))
(_fake_project / "data").mkdir(parents=True, exist_ok=True)

_fake_auth = make_fake_auth(PROJECT_DIR=_fake_project)
_mod = load_bot_module("weather-bot.py", _fake_auth)
parse_ticker = _mod.parse_ticker
compute_probability = _mod.compute_probability


# ===================================================================
# parse_ticker tests
# ===================================================================

class TestParseTicker:

    def test_valid_threshold_ticker(self):
        result = parse_ticker("KXHIGHMIA-26FEB16-T86")
        assert result is not None
        assert result["city"] == "MIA"
        assert result["date"] == "2026-02-16"
        assert result["direction"] == "T"
        assert result["threshold"] == 86.0

    def test_bracket_ticker(self):
        result = parse_ticker("KXHIGHMIA-26FEB16-B85.5")
        assert result is not None
        assert result["direction"] == "B"
        assert result["threshold"] == 85.5

    def test_invalid_ticker_returns_none(self):
        assert parse_ticker("INVALID-TICKER") is None

    def test_completely_empty_string(self):
        assert parse_ticker("") is None

    def test_wrong_prefix(self):
        assert parse_ticker("KXLOWMIA-26FEB16-T86") is None

    def test_different_city(self):
        result = parse_ticker("KXHIGHCHI-26JAN10-T32")
        assert result is not None
        assert result["city"] == "CHI"
        assert result["date"] == "2026-01-10"
        assert result["threshold"] == 32.0

    def test_integer_threshold(self):
        result = parse_ticker("KXHIGHNYC-26MAR05-T50")
        assert result is not None
        assert result["threshold"] == 50.0

    def test_invalid_month(self):
        """A three-letter month code that isn't in the MONTHS map."""
        assert parse_ticker("KXHIGHMIA-26XYZ16-T86") is None


# ===================================================================
# compute_probability tests
# ===================================================================

class TestComputeProbability:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    # --- direction = "T" (above threshold) ---

    def test_forecast_way_above_threshold(self):
        """Forecast 10 degrees above threshold -> high probability."""
        prob = compute_probability(96, 86, "T")
        assert prob > 0.9

    def test_forecast_way_below_threshold(self):
        """Forecast 10 degrees below threshold -> very low probability."""
        prob = compute_probability(76, 86, "T")
        assert prob < 0.1

    def test_forecast_near_threshold(self):
        """Forecast within 1 degree of threshold -> mid probability (~0.45)."""
        prob = compute_probability(86, 86, "T")
        assert 0.3 <= prob <= 0.6

    def test_slightly_above_threshold(self):
        """Forecast 2 degrees above -> moderately high probability."""
        prob = compute_probability(88, 86, "T")
        assert 0.5 < prob < 0.9

    def test_slightly_below_threshold(self):
        """Forecast 2 degrees below -> moderately low probability."""
        prob = compute_probability(84, 86, "T")
        assert 0.1 < prob < 0.5

    # --- direction = "B" (bracket) ---

    def test_bracket_forecast_at_center(self):
        """Forecast right at bracket center -> peak bracket probability.
        CDF model: 1F bracket at sigma=2.5F gives ~0.16 (PDF peak * width)."""
        # Bracket center for threshold 85 is 85.5
        prob = compute_probability(85.5, 85, "B")
        assert 0.10 < prob < 0.25

    def test_bracket_forecast_far_from_center(self):
        """Forecast 10 degrees from bracket center -> very low probability."""
        prob = compute_probability(95, 85, "B")
        assert prob < 0.10

    def test_bracket_moderate_distance(self):
        """Forecast 2 degrees from center -> ~0.20."""
        prob = compute_probability(87.5, 85, "B")
        assert 0.10 <= prob <= 0.25

    def test_bracket_large_distance(self):
        """Forecast 4 degrees from center -> ~0.06."""
        prob = compute_probability(89.5, 85, "B")
        assert prob <= 0.10


# ===================================================================
# ensemble spread sigma multiplier tests
# ===================================================================

class TestEnsembleSpreadSigma:
    """Test ensemble spread tracking for adaptive weather sigma."""

    def setup_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def teardown_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def test_tight_ensemble_no_sigma_increase(self):
        """When models agree (spread < 2°F), no sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        # Models within 1°F of each other
        mult = ensemble_spread_sigma_multiplier(spread_f=1.0)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_moderate_spread_mild_increase(self):
        """When models diverge moderately (3-5°F), mild sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=4.0)
        assert 1.1 < mult < 1.5

    def test_large_spread_significant_increase(self):
        """When models diverge significantly (>8°F), large sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=10.0)
        assert mult > 1.5

    def test_zero_spread_no_increase(self):
        """Zero spread should give multiplier of 1.0."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=0.0)
        assert mult == pytest.approx(1.0)

    def test_multiplier_capped(self):
        """Multiplier should be capped at reasonable maximum (e.g., 2.5)."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=20.0)
        assert mult <= 2.5
