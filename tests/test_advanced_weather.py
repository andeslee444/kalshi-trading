"""Tests for advanced weather probability model: skew-normal, hour-aware sigma,
adaptive ensemble weighting, and ensemble disagreement scoring.
"""

import math
import pytest

from probability import (
    _norm_cdf,
    _norm_pdf,
    _skew_normal_cdf,
    weather_probability,
    weather_sigma,
    weather_sigma_hourly,
    compute_adaptive_ensemble_weights,
    ensemble_disagreement_score,
    ensemble_weather_probability_v2,
    _reset_calibration,
)


# ===================================================================
# Skew-normal CDF tests
# ===================================================================

class TestSkewNormalCdf:

    def test_zero_skew_equals_normal(self):
        """skew_normal_cdf(0, alpha=0) equals norm_cdf(0) = 0.5."""
        assert abs(_skew_normal_cdf(0, alpha=0) - 0.5) < 1e-6

    def test_zero_skew_matches_normal_at_various_x(self):
        """With alpha=0, skew-normal should match standard normal CDF."""
        for x in [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]:
            assert abs(_skew_normal_cdf(x, alpha=0) - _norm_cdf(x)) < 1e-5, \
                f"Failed at x={x}: skew_normal={_skew_normal_cdf(x, alpha=0)}, norm={_norm_cdf(x)}"

    def test_positive_skew_shifts_mass_right(self):
        """skew_normal_cdf(x, alpha=3) < norm_cdf(x) for positive x.
        Positive alpha means more mass in the right tail, so CDF is lower
        (less mass accumulated to the left of x)."""
        for x in [0.5, 1.0, 1.5, 2.0]:
            sn = _skew_normal_cdf(x, alpha=3)
            n = _norm_cdf(x)
            assert sn < n, \
                f"Positive skew should lower CDF at positive x={x}: sn={sn}, n={n}"

    def test_negative_skew_shifts_mass_left(self):
        """skew_normal_cdf(x, alpha=-3) > norm_cdf(x) for positive x.
        Negative alpha means more mass in the left tail, so CDF is higher
        (more mass accumulated to the left of x)."""
        for x in [0.5, 1.0, 1.5, 2.0]:
            sn = _skew_normal_cdf(x, alpha=-3)
            n = _norm_cdf(x)
            assert sn > n, \
                f"Negative skew should raise CDF at positive x={x}: sn={sn}, n={n}"

    def test_skew_normal_bounded_0_1(self):
        """CDF values should be in [0, 1]."""
        for alpha in [-5, -2, 0, 2, 5]:
            for x in [-5, -2, 0, 2, 5]:
                val = _skew_normal_cdf(x, alpha=alpha)
                assert 0 <= val <= 1, f"Out of bounds: sn_cdf({x}, alpha={alpha}) = {val}"

    def test_skew_normal_monotonic(self):
        """CDF should be monotonically increasing in x (within numerical tolerance)."""
        for alpha in [-3, 0, 3]:
            vals = [_skew_normal_cdf(x, alpha=alpha) for x in [-3, -2, -1, 0, 1, 2, 3]]
            for i in range(len(vals) - 1):
                # Allow 1e-7 tolerance for quadrature numerical precision at extremes
                assert vals[i] <= vals[i + 1] + 1e-7, \
                    f"Not monotonic at alpha={alpha}: {vals}"


# ===================================================================
# Weather probability with skew parameter tests
# ===================================================================

class TestWeatherProbabilitySkew:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_positive_skew_higher_above_threshold(self):
        """With positive skew, prob of being above threshold should be higher
        than symmetric model (warm bias -> more mass in upper tail)."""
        prob_symmetric = weather_probability(90, 86, "T", 0)
        prob_skew = weather_probability(90, 86, "T", 0, skew=2.0)
        assert prob_skew > prob_symmetric, \
            f"Positive skew should give higher above-threshold prob: skew={prob_skew}, sym={prob_symmetric}"

    def test_zero_skew_backward_compatible(self):
        """skew=0 (default) should not change existing behavior."""
        prob_default = weather_probability(90, 86, "T", 0)
        prob_zero_skew = weather_probability(90, 86, "T", 0, skew=0.0)
        assert abs(prob_default - prob_zero_skew) < 1e-10

    def test_negative_skew_lower_above_threshold(self):
        """With negative skew (cold bias), model overestimates -> lower prob."""
        prob_symmetric = weather_probability(90, 86, "T", 0)
        prob_skew = weather_probability(90, 86, "T", 0, skew=-2.0)
        assert prob_skew < prob_symmetric


# ===================================================================
# Hour-of-day-aware sigma tests
# ===================================================================

class TestWeatherSigmaHourly:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_morning_more_uncertain_than_afternoon(self):
        """weather_sigma_hourly(days_out=0, hour=6) > weather_sigma_hourly(days_out=0, hour=14)."""
        morning = weather_sigma_hourly(days_out=0, hour_of_day=6)
        afternoon = weather_sigma_hourly(days_out=0, hour_of_day=14)
        assert morning > afternoon, \
            f"Morning sigma ({morning}) should be > afternoon sigma ({afternoon})"

    def test_late_afternoon_most_certain(self):
        """weather_sigma_hourly(days_out=0, hour=17) < weather_sigma_hourly(days_out=0, hour=10)."""
        late_afternoon = weather_sigma_hourly(days_out=0, hour_of_day=17)
        mid_morning = weather_sigma_hourly(days_out=0, hour_of_day=10)
        assert late_afternoon < mid_morning, \
            f"Late afternoon sigma ({late_afternoon}) should be < mid-morning sigma ({mid_morning})"

    def test_overnight_extra_uncertainty(self):
        """Before 6am, sigma should be higher than at 6am (overnight extra uncertainty)."""
        overnight = weather_sigma_hourly(days_out=0, hour_of_day=3)
        morning = weather_sigma_hourly(days_out=0, hour_of_day=6)
        assert overnight > morning, \
            f"Overnight sigma ({overnight}) should be > morning sigma ({morning})"

    def test_falls_back_to_weather_sigma_when_hour_none(self):
        """When hour is None, should fall back to weather_sigma (backward compatibility)."""
        hourly = weather_sigma_hourly(days_out=3, hour_of_day=None)
        base = weather_sigma(days_out=3)
        assert abs(hourly - base) < 1e-10, \
            f"Hour=None should match base sigma: hourly={hourly}, base={base}"

    def test_days_out_gt_0_ignores_hour(self):
        """For days_out > 0, hour_of_day should be ignored (full daily sigma)."""
        sigma_morning = weather_sigma_hourly(days_out=3, hour_of_day=6)
        sigma_afternoon = weather_sigma_hourly(days_out=3, hour_of_day=17)
        assert abs(sigma_morning - sigma_afternoon) < 1e-10, \
            f"days_out > 0 should ignore hour: morning={sigma_morning}, afternoon={sigma_afternoon}"

    def test_afternoon_reduction_roughly_40_to_60_percent(self):
        """Afternoon sigma should be roughly 40-60% less than morning for day-0."""
        morning = weather_sigma_hourly(days_out=0, hour_of_day=6)
        afternoon = weather_sigma_hourly(days_out=0, hour_of_day=14)
        reduction = 1.0 - (afternoon / morning)
        assert 0.25 <= reduction <= 0.70, \
            f"Afternoon reduction ({reduction*100:.0f}%) should be 25-70% vs morning"


# ===================================================================
# Adaptive ensemble weights tests
# ===================================================================

class TestAdaptiveEnsembleWeights:

    def test_inverse_brier_weighting(self):
        """Adaptive weights should use inverse-Brier weighting."""
        verification_data = {
            "gfs": {"brier_predictions": [(0.8, 1)] * 25 + [(0.2, 0)] * 25},  # good model
            "ecmwf": {"brier_predictions": [(0.6, 1)] * 25 + [(0.4, 0)] * 25},  # mediocre model
            "icon": {"brier_predictions": [(0.5, 1)] * 25 + [(0.5, 0)] * 25},  # worst model
        }
        weights = compute_adaptive_ensemble_weights(verification_data)
        assert weights is not None
        # GFS should have highest weight (lowest Brier)
        assert weights["gfs"] > weights["ecmwf"]
        assert weights["ecmwf"] > weights["icon"]
        # Weights should sum to ~1
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_fallback_when_insufficient_data(self):
        """Falls back to default_weights if insufficient verification data (< 20 samples)."""
        verification_data = {
            "gfs": {"brier_predictions": [(0.8, 1)] * 5},  # too few samples
            "ecmwf": {"brier_predictions": [(0.6, 1)] * 5},
        }
        defaults = {"gfs": 0.5, "ecmwf": 0.3, "icon": 0.2}
        weights = compute_adaptive_ensemble_weights(verification_data, default_weights=defaults)
        assert weights == defaults

    def test_fallback_when_no_data(self):
        """Falls back to default_weights when verification data is empty."""
        defaults = {"gfs": 0.5, "ecmwf": 0.3, "icon": 0.2}
        weights = compute_adaptive_ensemble_weights({}, default_weights=defaults)
        assert weights == defaults

    def test_fallback_when_none_data(self):
        """Falls back to default_weights when verification data is None."""
        defaults = {"gfs": 0.5, "ecmwf": 0.3, "icon": 0.2}
        weights = compute_adaptive_ensemble_weights(None, default_weights=defaults)
        assert weights == defaults


# ===================================================================
# Ensemble disagreement score tests
# ===================================================================

class TestEnsembleDisagreement:

    def test_perfect_agreement_returns_zero(self):
        """When all models agree, disagreement should be 0."""
        model_probs = {"gfs": 0.75, "ecmwf": 0.75, "icon": 0.75}
        score = ensemble_disagreement_score(model_probs)
        assert abs(score) < 1e-10, f"Perfect agreement should give 0, got {score}"

    def test_high_disagreement(self):
        """When models disagree significantly, score should be high."""
        model_probs = {"gfs": 0.90, "ecmwf": 0.10, "icon": 0.50}
        score = ensemble_disagreement_score(model_probs)
        assert score > 0.3, f"High disagreement should give > 0.3, got {score}"

    def test_bounded_0_1(self):
        """Disagreement score should be in [0, 1]."""
        test_cases = [
            {"gfs": 0.99, "ecmwf": 0.01, "icon": 0.50},
            {"gfs": 0.50, "ecmwf": 0.50, "icon": 0.50},
            {"gfs": 0.75, "ecmwf": 0.70, "icon": 0.80},
        ]
        for probs in test_cases:
            score = ensemble_disagreement_score(probs)
            assert 0 <= score <= 1, f"Score out of bounds: {score} for {probs}"

    def test_moderate_disagreement(self):
        """Moderate disagreement should be between 0 and 0.3."""
        model_probs = {"gfs": 0.70, "ecmwf": 0.65, "icon": 0.75}
        score = ensemble_disagreement_score(model_probs)
        assert 0 < score < 0.3, f"Moderate disagreement should be in (0, 0.3), got {score}"

    def test_single_model_returns_zero(self):
        """With only one model, no disagreement possible."""
        score = ensemble_disagreement_score({"gfs": 0.75})
        assert abs(score) < 1e-10


# ===================================================================
# Ensemble weather probability v2 tests
# ===================================================================

class TestEnsembleV2:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_basic_probability_returns_float(self):
        """Basic call should return a float probability."""
        forecasts = {"gfs": 90.0, "ecmwf": 89.0, "icon": 91.0}
        prob = ensemble_weather_probability_v2(forecasts, 86, "T", 0)
        assert isinstance(prob, float)
        assert 0 < prob < 1

    def test_return_details_gives_dict(self):
        """With return_details=True, should return (prob, details)."""
        forecasts = {"gfs": 90.0, "ecmwf": 89.0, "icon": 91.0}
        result = ensemble_weather_probability_v2(forecasts, 86, "T", 0, return_details=True)
        assert isinstance(result, tuple)
        assert len(result) == 2
        prob, details = result
        assert isinstance(prob, float)
        assert isinstance(details, dict)
        assert "disagreement_score" in details
        assert "weights_used" in details
        assert "per_model_probs" in details

    def test_backward_compatible_without_new_args(self):
        """Without new args, should produce similar results to v1."""
        forecasts = {"gfs": 90.0, "ecmwf": 89.0, "icon": 91.0}
        from probability import ensemble_weather_probability
        prob_v1 = ensemble_weather_probability(forecasts, 86, "T", 0)
        prob_v2 = ensemble_weather_probability_v2(forecasts, 86, "T", 0)
        # Should be close (same underlying model, same default weights)
        assert abs(prob_v1 - prob_v2) < 0.05, \
            f"V2 should be close to V1: v1={prob_v1}, v2={prob_v2}"

    def test_hour_of_day_affects_day0(self):
        """Hour of day should change day-0 probability."""
        forecasts = {"gfs": 88.0, "ecmwf": 87.0, "icon": 89.0}
        prob_morning = ensemble_weather_probability_v2(forecasts, 86, "T", 0, hour_of_day=6)
        prob_afternoon = ensemble_weather_probability_v2(forecasts, 86, "T", 0, hour_of_day=17)
        # Afternoon should give more extreme prob (tighter sigma)
        # Both > 0.5, so afternoon should be higher
        assert prob_afternoon > prob_morning, \
            f"Afternoon prob ({prob_afternoon}) should be > morning ({prob_morning}) due to tighter sigma"
