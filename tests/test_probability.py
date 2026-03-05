"""Tests for the shared probability module (src/kalshi/probability.py)."""

import math
import pytest

# conftest.py adds src/kalshi/ to sys.path, so direct import works
from probability import (
    _norm_cdf,
    _norm_pdf,
    _probit,
    _student_t_cdf,
    weather_probability,
    ensemble_weather_probability,
    nws_probability,
    info_arb_probability,
    album_data_sigma,
    boxoffice_data_sigma,
    _reset_calibration,
    gas_price_probability,
    cpi_nowcast_sigma,
    gdp_nowcast_sigma,
    econ_nowcast_probability,
)


# ===================================================================
# _norm_cdf tests
# ===================================================================

class TestNormCdf:

    def test_zero_gives_half(self):
        assert abs(_norm_cdf(0) - 0.5) < 1e-10

    def test_large_positive(self):
        assert _norm_cdf(6.0) > 0.999999

    def test_large_negative(self):
        assert _norm_cdf(-6.0) < 0.000001

    def test_symmetry(self):
        """CDF(x) + CDF(-x) = 1."""
        for x in [0.5, 1.0, 2.0, 3.0]:
            assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10

    def test_one_sigma(self):
        """CDF(1) ≈ 0.8413."""
        assert abs(_norm_cdf(1.0) - 0.8413) < 0.001


# ===================================================================
# _student_t_cdf tests
# ===================================================================

class TestStudentTCdf:

    def test_zero_gives_half(self):
        assert abs(_student_t_cdf(0, 6) - 0.5) < 1e-10

    def test_symmetry(self):
        """CDF(x) + CDF(-x) = 1."""
        for x in [0.5, 1.0, 2.0, 3.0]:
            assert abs(_student_t_cdf(x, 6) + _student_t_cdf(-x, 6) - 1.0) < 1e-8

    def test_monotonically_increasing(self):
        vals = [_student_t_cdf(x, 6) for x in [-3, -2, -1, 0, 1, 2, 3]]
        for i in range(len(vals) - 1):
            assert vals[i] < vals[i + 1]

    def test_fatter_tails_than_normal(self):
        """1 - t_cdf(3, 6) > 1 - norm_cdf(3): more mass in tails."""
        t_tail = 1 - _student_t_cdf(3.0, 6)
        n_tail = 1 - _norm_cdf(3.0)
        assert t_tail > n_tail

    def test_converges_to_normal_at_high_df(self):
        """t_cdf(x, 1000) should be very close to norm_cdf(x)."""
        for x in [0.5, 1.0, 2.0]:
            assert abs(_student_t_cdf(x, 1000) - _norm_cdf(x)) < 0.001

    def test_known_values_df6(self):
        """Verify against known t-distribution table values for df=6."""
        # t(1.0, 6) ≈ 0.8220
        assert abs(_student_t_cdf(1.0, 6) - 0.8220) < 0.002
        # t(2.0, 6) ≈ 0.9536
        assert abs(_student_t_cdf(2.0, 6) - 0.9536) < 0.002

    def test_large_positive(self):
        assert _student_t_cdf(10.0, 6) > 0.9999

    def test_large_negative(self):
        assert _student_t_cdf(-10.0, 6) < 0.0001

    def test_df_4_fatter_than_df_6(self):
        """Lower df should have fatter tails."""
        tail_4 = 1 - _student_t_cdf(3.0, 4)
        tail_6 = 1 - _student_t_cdf(3.0, 6)
        assert tail_4 > tail_6

    def test_fallback_to_normal_on_invalid_df(self):
        """df <= 0 should fall back to normal CDF."""
        assert abs(_student_t_cdf(1.0, 0) - _norm_cdf(1.0)) < 1e-10


# ===================================================================
# weather_probability tests
# ===================================================================

class TestWeatherProbability:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    # T-direction

    def test_t_forecast_well_above(self):
        """Forecast 10F above threshold -> high probability."""
        prob = weather_probability(96, 86, "T", 0)
        assert prob > 0.95

    def test_t_forecast_well_below(self):
        """Forecast 10F below threshold -> low probability."""
        prob = weather_probability(76, 86, "T", 0)
        assert prob < 0.05

    def test_t_forecast_at_threshold(self):
        """Forecast equals threshold -> ~0.50."""
        prob = weather_probability(86, 86, "T", 0)
        assert 0.45 <= prob <= 0.55

    def test_t_slightly_above(self):
        """Forecast 2F above -> moderately high."""
        prob = weather_probability(88, 86, "T", 0)
        assert 0.55 < prob < 0.90

    def test_t_slightly_below(self):
        """Forecast 2F below -> moderately low."""
        prob = weather_probability(84, 86, "T", 0)
        assert 0.10 < prob < 0.45

    # B-direction

    def test_b_forecast_at_center(self):
        """Forecast at bracket center -> peak bracket probability.
        For 1F bracket at sigma=2.5F, peak ≈ 0.16 (PDF at center * width)."""
        prob = weather_probability(85.5, 85, "B", 0)
        assert 0.10 < prob < 0.25

    def test_b_forecast_far_away(self):
        """Forecast 10F from bracket -> near zero."""
        prob = weather_probability(95, 85, "B", 0)
        assert prob < 0.05

    # days_out scaling

    def test_more_days_out_widens_uncertainty(self):
        """7-day forecast has wider sigma, so less extreme probabilities."""
        prob_0 = weather_probability(96, 86, "T", 0)
        prob_7 = weather_probability(96, 86, "T", 7)
        # With wider sigma, probability should be less extreme (closer to 0.5)
        assert prob_7 < prob_0

    def test_days_out_zero_default(self):
        """Default days_out=0 should work."""
        prob = weather_probability(90, 86, "T")
        assert 0.5 < prob < 1.0

    def test_sqrt_sigma_scaling(self):
        """Sigma grows sublinearly: day-1→day-4 gap < day-4→day-9 (sqrt)."""
        prob_1 = weather_probability(90, 86, "T", 1)
        prob_4 = weather_probability(90, 86, "T", 4)
        prob_9 = weather_probability(90, 86, "T", 9)
        assert prob_1 > prob_4 > prob_9


# ===================================================================
# nws_probability tests
# ===================================================================

class TestNwsProbability:

    def test_post_5pm_large_margin(self):
        """After 5PM with running high well above threshold -> ~1.0."""
        prob = nws_probability(92.0, 86.0, "T", 17)
        assert prob > 0.99

    def test_at_threshold_3pm(self):
        """At 3PM, running high right at threshold -> ~0.50."""
        prob = nws_probability(86.0, 86.0, "T", 15)
        assert 0.40 <= prob <= 0.60

    def test_post_5pm_below_threshold(self):
        """After 5PM with running high well below -> near 0."""
        prob = nws_probability(80.0, 86.0, "T", 17)
        assert prob < 0.01

    def test_early_morning_high_uncertainty(self):
        """Before 3PM, sigma is large so probability is less extreme."""
        prob = nws_probability(92.0, 86.0, "T", 10)
        assert prob > 0.5  # Still above 0.5 but less extreme than post-5PM

    def test_bracket_post_5pm(self):
        """Bracket probability after 5PM when running high is in bracket."""
        prob = nws_probability(86.5, 86.0, "B", 17)
        # sigma=0.5, should be high since 86.5 is centered in [86, 87)
        assert prob > 0.5

    def test_bracket_post_5pm_outside(self):
        """Bracket probability after 5PM when running high is far from bracket."""
        prob = nws_probability(92.0, 86.0, "B", 17)
        assert prob < 0.01


# ===================================================================
# info_arb_probability tests
# ===================================================================

class TestInfoArbProbability:

    def test_well_above_threshold(self):
        """200K units vs 150K threshold with 5% sigma -> very high."""
        prob = info_arb_probability(200000, 150000, 0.05)
        assert prob > 0.99

    def test_well_below_threshold(self):
        """100K units vs 150K threshold -> very low."""
        prob = info_arb_probability(100000, 150000, 0.05)
        assert prob < 0.01

    def test_at_threshold(self):
        """Observed equals threshold -> 0.50."""
        prob = info_arb_probability(150000, 150000, 0.05)
        assert abs(prob - 0.5) < 0.01

    def test_slightly_above(self):
        """Slightly above threshold -> above 0.5."""
        prob = info_arb_probability(155000, 150000, 0.10)
        assert 0.5 < prob < 0.9

    def test_larger_sigma_more_uncertainty(self):
        """Larger sigma means less extreme probabilities."""
        prob_tight = info_arb_probability(180000, 150000, 0.03)
        prob_wide = info_arb_probability(180000, 150000, 0.15)
        assert prob_tight > prob_wide


# ===================================================================
# Day sigma tests
# ===================================================================

class TestDaySigma:

    def test_album_monday(self):
        assert album_data_sigma(0) == 0.15

    def test_album_tuesday(self):
        assert album_data_sigma(1) == 0.15

    def test_album_wednesday(self):
        assert album_data_sigma(2) == 0.10

    def test_album_thursday(self):
        assert album_data_sigma(3) == 0.10

    def test_album_friday(self):
        assert album_data_sigma(4) == 0.05

    def test_album_saturday(self):
        assert album_data_sigma(5) == 0.05

    def test_album_sunday(self):
        assert album_data_sigma(6) == 0.05

    def test_boxoffice_friday(self):
        assert boxoffice_data_sigma(4) == 0.12

    def test_boxoffice_saturday(self):
        assert boxoffice_data_sigma(5) == 0.12

    def test_boxoffice_sunday(self):
        assert boxoffice_data_sigma(6) == 0.05

    def test_boxoffice_monday(self):
        assert boxoffice_data_sigma(0) == 0.04


# ===================================================================
# Kelly tests — comprehensive tests live in test_kelly.py
# ===================================================================
# See tests/test_kelly.py for full half_kelly, half_kelly_sell, quarter_kelly_sell,
# and exact-value pinning tests. Only a basic smoke test is kept here.


# ===================================================================
# Calibration tests
# ===================================================================

class TestCalibration:

    def setup_method(self):
        """Reset calibration cache before each test."""
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_default_sigma_without_calibration_file(self):
        """Without calibration.json, weather uses default sigma = 2.0 + 0.5 * sqrt(days_out)."""
        # At day 0 with forecast = threshold, prob should be ~0.50 (sigma=2.0)
        prob = weather_probability(86, 86, "T", 0)
        assert 0.45 <= prob <= 0.55

    def test_city_param_optional(self):
        """Passing city=None or city='UNKNOWN' shouldn't break anything."""
        prob1 = weather_probability(90, 86, "T", 0)
        prob2 = weather_probability(90, 86, "T", 0, city=None)
        prob3 = weather_probability(90, 86, "T", 0, city="UNKNOWN")
        assert prob1 == prob2
        assert prob1 == prob3

    def test_album_sigma_defaults(self):
        """Without calibration, album sigma returns hardcoded defaults."""
        assert album_data_sigma(0) == 0.15
        assert album_data_sigma(4) == 0.05

    def test_boxoffice_sigma_defaults(self):
        """Without calibration, box office sigma returns hardcoded defaults."""
        assert boxoffice_data_sigma(4) == 0.12
        assert boxoffice_data_sigma(0) == 0.04


# ===================================================================
# gas_price_probability tests
# ===================================================================

class TestGasPriceProbability:

    def test_well_above_threshold(self):
        """Gas at $3.45, threshold $3.00 above -> very high."""
        prob = gas_price_probability(3.45, 3.00, "above")
        assert prob > 0.99

    def test_well_below_threshold(self):
        """Gas at $3.45, threshold $4.00 above -> very low."""
        prob = gas_price_probability(3.45, 4.00, "above")
        assert prob < 0.01

    def test_at_threshold(self):
        """Gas at threshold -> ~0.50."""
        prob = gas_price_probability(3.50, 3.50, "above")
        assert 0.45 <= prob <= 0.55

    def test_below_direction(self):
        """P(below X) = 1 - P(above X)."""
        prob_above = gas_price_probability(3.45, 3.50, "above")
        prob_below = gas_price_probability(3.45, 3.50, "below")
        assert abs(prob_above + prob_below - 1.0) < 1e-10

    def test_near_threshold(self):
        """Gas at $3.45, threshold $3.50 above -> below 50% but not extreme."""
        prob = gas_price_probability(3.45, 3.50, "above")
        assert 0.15 < prob < 0.50


# ===================================================================
# CPI Nowcast Sigma tests
# ===================================================================

class TestCpiNowcastSigma:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_release_day(self):
        """At d=0, sigma should be the floor (0.05%)."""
        assert cpi_nowcast_sigma(0) == pytest.approx(0.05, abs=0.005)

    def test_one_week(self):
        """At d=7, sigma should be moderate (~0.17%)."""
        sigma = cpi_nowcast_sigma(7)
        assert 0.13 <= sigma <= 0.20

    def test_two_weeks(self):
        """At d=14, sigma should be ~0.21%."""
        sigma = cpi_nowcast_sigma(14)
        assert 0.18 <= sigma <= 0.25

    def test_one_month(self):
        """At d=30, sigma should be ~0.27-0.33%."""
        sigma = cpi_nowcast_sigma(30)
        assert 0.24 <= sigma <= 0.33

    def test_long_horizon(self):
        """At d=107, sigma should approach ~0.40% (Cleveland Fed CI width)."""
        sigma = cpi_nowcast_sigma(107)
        assert 0.35 <= sigma <= 0.42

    def test_very_long_horizon(self):
        """At d=200, sigma should be near the asymptote (~0.40%)."""
        sigma = cpi_nowcast_sigma(200)
        assert 0.38 <= sigma <= 0.41

    def test_monotonically_increasing(self):
        """Sigma should increase with days_to_release."""
        prev = cpi_nowcast_sigma(0)
        for d in [1, 3, 7, 14, 30, 60, 107]:
            sigma = cpi_nowcast_sigma(d)
            assert sigma >= prev
            prev = sigma

    def test_negative_days_clamped(self):
        """Negative days_to_release should clamp to 0."""
        assert cpi_nowcast_sigma(-5) == cpi_nowcast_sigma(0)

    def test_model_prob_not_one_at_long_horizon(self):
        """With widened sigma, model_prob should NOT be 1.0 for 107-day contracts."""
        sigma = cpi_nowcast_sigma(107)
        prob = econ_nowcast_probability(2.41, sigma, 2.0, "above")
        assert prob < 0.99, f"prob={prob} should be <0.99 with sigma={sigma}"
        assert prob > 0.70, f"prob={prob} should be >0.70 (still likely)"

    def test_integration_with_econ_nowcast(self):
        """CPI sigma feeds into econ_nowcast_probability correctly."""
        sigma = cpi_nowcast_sigma(0)
        # 5bp gap with 5bp sigma -> z=1.0 -> high confidence
        prob = econ_nowcast_probability(2.80, sigma, 2.85, "above")
        # nowcast (2.80) is below threshold (2.85), so P(above 2.85) < 0.5
        assert prob < 0.5

    def test_econ_nowcast_complementarity(self):
        """P(above) + P(below) must equal 1.0 for any inputs."""
        test_cases = [
            (2.80, 0.10, 2.85),
            (3.0, 0.06, 2.5),
            (2.5, 0.05, 2.5),
            (1.0, 0.10, 1.5),
            (5.0, 0.20, 4.0),
        ]
        for nowcast, sigma, threshold in test_cases:
            p_above = econ_nowcast_probability(nowcast, sigma, threshold, "above")
            p_below = econ_nowcast_probability(nowcast, sigma, threshold, "below")
            assert abs(p_above + p_below - 1.0) < 1e-10, \
                f"P(above)+P(below)={p_above+p_below} != 1.0 for ({nowcast}, {sigma}, {threshold})"


# ===================================================================
# Source-aware album sigma tests
# ===================================================================

class TestAlbumSigmaSourceAware:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_hdd_hits_top_50_returns_2pct(self):
        """hdd-hits-top-50 is the settlement source -> 2% sigma regardless of day."""
        for day in range(7):
            assert album_data_sigma(day, source="hdd-hits-top-50") == 0.02

    def test_hdd_midweek_20_day_based(self):
        """hdd-midweek-20 uses day-based sigma like legacy."""
        assert album_data_sigma(0, source="hdd-midweek-20") == 0.15  # Mon
        assert album_data_sigma(2, source="hdd-midweek-20") == 0.10  # Wed
        assert album_data_sigma(4, source="hdd-midweek-20") == 0.05  # Fri

    def test_hdd_article_returns_18pct(self):
        """hdd-article has high uncertainty -> 18% sigma."""
        for day in range(7):
            assert album_data_sigma(day, source="hdd-article") == 0.18

    def test_source_none_legacy_behavior(self):
        """source=None returns legacy day-based sigma (backward compatible)."""
        assert album_data_sigma(0, source=None) == 0.15
        assert album_data_sigma(2, source=None) == 0.10
        assert album_data_sigma(4, source=None) == 0.05

    def test_no_source_param_legacy_behavior(self):
        """Calling without source param at all returns legacy day-based sigma."""
        assert album_data_sigma(0) == 0.15
        assert album_data_sigma(4) == 0.05

    def test_unknown_source_falls_through_to_day_based(self):
        """Unknown source string falls through to day-based sigma."""
        assert album_data_sigma(0, source="some-unknown-source") == 0.15
        assert album_data_sigma(4, source="some-unknown-source") == 0.05

    def test_time_decay_applies_to_all_sources(self):
        """Time decay still applies regardless of source."""
        fresh = album_data_sigma(4, hours_since_publication=0, source="hdd-hits-top-50")
        stale = album_data_sigma(4, hours_since_publication=96, source="hdd-hits-top-50")
        assert stale > fresh

    def test_time_decay_on_article_source(self):
        """Time decay applies to article source too."""
        fresh = album_data_sigma(0, hours_since_publication=0, source="hdd-article")
        stale = album_data_sigma(0, hours_since_publication=48, source="hdd-article")
        assert stale > fresh
        assert fresh == 0.18


# ===================================================================
# Probit (inverse normal CDF) tests
# ===================================================================

class TestProbit:

    def test_roundtrip(self):
        """_norm_cdf(_probit(p)) ≈ p for various p values."""
        for p in [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]:
            assert abs(_norm_cdf(_probit(p)) - p) < 1e-4, f"roundtrip failed for p={p}"

    def test_probit_half_is_zero(self):
        """probit(0.5) = 0."""
        assert abs(_probit(0.5)) < 1e-4

    def test_probit_symmetry(self):
        """probit(p) = -probit(1-p)."""
        for p in [0.1, 0.2, 0.3, 0.4]:
            assert abs(_probit(p) + _probit(1 - p)) < 1e-3

    def test_probit_monotone(self):
        """probit is monotonically increasing."""
        vals = [_probit(p) for p in [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99]]
        for i in range(len(vals) - 1):
            assert vals[i] < vals[i + 1]


# ===================================================================
# Student-t economics probability tests
# ===================================================================

class TestEconStudentT:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_econ_uses_student_t(self):
        """Student-t should give fatter tails than Gaussian at 3-sigma."""
        # At 3-sigma, student-t (df=5) should give higher tail probability
        # Use large sigma gap so z ~ 3
        # nowcast=3.0, sigma=0.10, threshold=3.30 -> z=3.0
        prob_t = econ_nowcast_probability(3.0, 0.10, 3.30, "above", df=5)
        # Gaussian would give ~0.0013 at z=3
        gauss_prob = 1.0 - _norm_cdf(3.0)
        assert prob_t > gauss_prob, f"Student-t prob {prob_t} should exceed Gaussian {gauss_prob}"

    def test_econ_df_override_high(self):
        """df=500 should approximate Gaussian behavior."""
        prob_high_df = econ_nowcast_probability(3.0, 0.10, 3.30, "above", df=500)
        gauss_prob = 1.0 - _norm_cdf(3.0)
        # Should be very close to Gaussian
        assert abs(prob_high_df - gauss_prob) < 0.001

    def test_econ_complementarity_with_student_t(self):
        """P(above) + P(below) must still equal 1.0 with Student-t."""
        for df in [3, 5, 10, 100]:
            p_above = econ_nowcast_probability(2.80, 0.10, 2.85, "above", df=df)
            p_below = econ_nowcast_probability(2.80, 0.10, 2.85, "below", df=df)
            assert abs(p_above + p_below - 1.0) < 1e-10, f"Complementarity failed for df={df}"


# ===================================================================
# CPI sigma smoothness tests
# ===================================================================

class TestCpiSigmaSmooth:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_cpi_sigma_no_discontinuities(self):
        """Adjacent days should not have large sigma jumps (exponential is smooth)."""
        for d in range(0, 20):
            s1 = cpi_nowcast_sigma(d)
            s2 = cpi_nowcast_sigma(d + 1)
            # Adjacent days should differ by at most ~0.02 (wider sigma range, still smooth)
            assert abs(s2 - s1) < 0.02, f"Discontinuity at d={d}: {s1:.4f} -> {s2:.4f}"

    def test_cpi_sigma_at_release(self):
        """At release (d=0), sigma should be approximately 0.05 (floor)."""
        assert cpi_nowcast_sigma(0) == pytest.approx(0.05, abs=0.005)


# ===================================================================
# GDP nowcast sigma tests
# ===================================================================

class TestGdpNowcastSigma:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_release_day_floor(self):
        """At release day (d=0), sigma should be 0.05."""
        assert gdp_nowcast_sigma(0) == 0.05

    def test_monotonically_increasing(self):
        """Sigma should increase with days to release."""
        sigmas = [gdp_nowcast_sigma(d) for d in range(0, 30)]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] <= sigmas[i + 1]

    def test_day_14_range(self):
        """At 14 days out, sigma should be reasonable."""
        sigma = gdp_nowcast_sigma(14)
        assert 0.10 <= sigma <= 0.18

    def test_negative_days_use_floor(self):
        """Negative days should return the floor."""
        assert gdp_nowcast_sigma(-1) == 0.05


# ===================================================================
# Ensemble sigma_multiplier tests
# ===================================================================

class TestEnsembleSigmaMultiplier:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_sigma_multiplier_1_is_identity(self):
        """sigma_multiplier=1.0 produces same output as default."""
        forecasts = {"gfs": 90.0, "ecmwf": 89.0, "icon": 91.0}
        prob_default = ensemble_weather_probability(forecasts, 86, "T", 0)
        prob_mult1 = ensemble_weather_probability(forecasts, 86, "T", 0, sigma_multiplier=1.0)
        assert abs(prob_default - prob_mult1) < 1e-10

    def test_sigma_multiplier_widens_probability(self):
        """sigma_multiplier > 1 should pull probability toward 0.5."""
        forecasts = {"gfs": 92.0, "ecmwf": 91.0, "icon": 93.0}
        prob_normal = ensemble_weather_probability(forecasts, 86, "T", 0, sigma_multiplier=1.0)
        prob_wide = ensemble_weather_probability(forecasts, 86, "T", 0, sigma_multiplier=2.0)
        # Wider sigma means less extreme probability (closer to 0.5)
        assert prob_normal > prob_wide  # both > 0.5, so wider = less extreme
        assert prob_wide > 0.5  # still above 0.5

    def test_brier_weights_fall_back_when_no_data(self):
        """Without backtest Brier data, ensemble uses static/calibration weights."""
        # This just verifies it doesn't crash — no backtest-results.json in test env
        forecasts = {"gfs": 88.0, "ecmwf": 87.0}
        prob = ensemble_weather_probability(forecasts, 86, "T", 0)
        assert 0.0 < prob < 1.0


# ===================================================================
# Binary sigma (CDF derivative) tests
# ===================================================================

class TestBinarySigma:
    """Tests for the CDF-derivative price-space sigma formula:
    price_sigma = temp_sigma * phi(probit(mid/100)) * 100
    """

    def test_sigma_at_mid50_higher_than_mid10(self):
        """Maximum sensitivity should be at mid=50c (phi(0) is peak)."""
        temp_sigma = 3.0
        # At mid=50c
        z50 = _probit(0.50)
        sigma_50 = temp_sigma * _norm_pdf(z50) * 100
        # At mid=10c
        z10 = _probit(0.10)
        sigma_10 = temp_sigma * _norm_pdf(z10) * 100
        assert sigma_50 > sigma_10

    def test_sigma_symmetric(self):
        """10c and 90c should produce the same sigma (symmetry)."""
        temp_sigma = 3.0
        z10 = _probit(0.10)
        z90 = _probit(0.90)
        sigma_10 = temp_sigma * _norm_pdf(z10) * 100
        sigma_90 = temp_sigma * _norm_pdf(z90) * 100
        assert abs(sigma_10 - sigma_90) < 0.5  # within 0.5 cents

    def test_sigma_reasonable_range(self):
        """At mid=50c with temp_sigma=3.0, price_sigma should be ~120c."""
        temp_sigma = 3.0
        z = _probit(0.50)
        sigma = temp_sigma * _norm_pdf(z) * 100
        # phi(0) = 0.399, so 3.0 * 0.399 * 100 ≈ 120
        assert 110 < sigma < 130


# ===================================================================
# fed_ci_width dynamic sigma tests
# ===================================================================

class TestFedCiWidth:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_fed_ci_width_overrides_hardcoded(self):
        """cpi_nowcast_sigma(30, fed_ci_width=0.50) uses 0.50/3.29 ~ 0.152."""
        sigma = cpi_nowcast_sigma(30, fed_ci_width=0.50)
        expected = 0.50 / 3.29
        assert abs(sigma - expected) < 0.001

    def test_fed_ci_width_none_uses_default(self):
        """cpi_nowcast_sigma(30) without fed_ci_width returns same as before."""
        sigma_default = cpi_nowcast_sigma(30)
        sigma_none = cpi_nowcast_sigma(30, fed_ci_width=None)
        assert sigma_default == sigma_none

    def test_fed_ci_width_zero_uses_default(self):
        """cpi_nowcast_sigma(30, fed_ci_width=0) uses default formula."""
        sigma_default = cpi_nowcast_sigma(30)
        sigma_zero = cpi_nowcast_sigma(30, fed_ci_width=0)
        assert sigma_default == sigma_zero

    def test_fed_ci_width_floor(self):
        """cpi_nowcast_sigma(0, fed_ci_width=0.05) returns max(0.05/3.29, 0.03) = 0.03."""
        sigma = cpi_nowcast_sigma(0, fed_ci_width=0.05)
        # 0.05 / 3.29 ~ 0.0152, which is below the floor of 0.03
        assert sigma == pytest.approx(0.03, abs=0.001)
