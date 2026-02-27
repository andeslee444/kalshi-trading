"""Tests for the shared probability module (src/kalshi/probability.py)."""

import math
import pytest

# conftest.py adds src/kalshi/ to sys.path, so direct import works
from probability import (
    _norm_cdf,
    _student_t_cdf,
    weather_probability,
    nws_probability,
    info_arb_probability,
    album_data_sigma,
    boxoffice_data_sigma,
    half_kelly,
    half_kelly_sell,
    _reset_calibration,
    gas_price_probability,
    cpi_nowcast_sigma,
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
# half_kelly tests
# ===================================================================

class TestHalfKelly:

    def test_zero_edge_returns_zero(self):
        contracts, risk = half_kelly(0, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_negative_edge_returns_zero(self):
        contracts, risk = half_kelly(-0.10, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_positive_edge_returns_positive(self):
        contracts, risk = half_kelly(0.20, 30, 500)
        assert contracts > 0
        assert risk > 0

    def test_respects_max_cost(self):
        """Contracts * price should not exceed max_cost_cents."""
        contracts, risk = half_kelly(0.30, 20, 200)
        assert risk <= 200

    def test_returns_tuple(self):
        result = half_kelly(0.15, 40, 500)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_price_at_boundary(self):
        """Price at 100 should return zero."""
        contracts, risk = half_kelly(0.20, 100, 500)
        assert contracts == 0

    def test_with_bankroll(self):
        """Bankroll constraint should limit contracts."""
        c_no_bank, _ = half_kelly(0.20, 10, 10000)
        c_small_bank, _ = half_kelly(0.20, 10, 10000, bankroll_cents=500)
        assert c_small_bank <= c_no_bank


# ===================================================================
# half_kelly_sell tests
# ===================================================================

class TestHalfKellySell:

    def test_zero_edge_returns_zero(self):
        contracts, risk = half_kelly_sell(0, 5, 500)
        assert contracts == 0
        assert risk == 0

    def test_negative_edge_returns_zero(self):
        contracts, risk = half_kelly_sell(-0.10, 5, 500)
        assert contracts == 0
        assert risk == 0

    def test_valid_edge_returns_positive(self):
        contracts, risk = half_kelly_sell(0.20, 5, 500, bankroll_cents=50000)
        assert contracts > 0
        assert risk > 0

    def test_returns_tuple(self):
        result = half_kelly_sell(0.15, 10, 500, bankroll_cents=50000)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_risk_equals_contracts_times_no_price(self):
        """Risk = contracts * (100 - sell_price)."""
        contracts, risk = half_kelly_sell(0.20, 10, 5000, bankroll_cents=50000)
        if contracts > 0:
            assert risk == contracts * (100 - 10)

    def test_respects_max_cost_cap(self):
        contracts, risk = half_kelly_sell(0.50, 5, 500, bankroll_cents=10_000_000)
        assert risk <= 500

    def test_boundary_price_100(self):
        contracts, risk = half_kelly_sell(0.20, 100, 500)
        assert contracts == 0

    def test_bankroll_scaling(self):
        c1, _ = half_kelly_sell(0.20, 5, 50000, bankroll_cents=10000)
        c2, _ = half_kelly_sell(0.20, 5, 50000, bankroll_cents=20000)
        assert c2 >= c1

    def test_symmetry_at_50c(self):
        """At 50c, sell-side and buy-side produce same sizing."""
        buy_c, _ = half_kelly(0.10, 50, 5000, bankroll_cents=50000)
        sell_c, _ = half_kelly_sell(0.10, 50, 5000, bankroll_cents=50000)
        assert buy_c == sell_c


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

    def test_release_day_floor(self):
        """At release day (days=0), sigma should be 0.03 (lowest uncertainty)."""
        assert cpi_nowcast_sigma(0) == 0.03

    def test_day_14_near_010(self):
        """At 14 days out, sigma should be near 0.10."""
        sigma = cpi_nowcast_sigma(14)
        assert 0.09 <= sigma <= 0.11

    def test_monotonically_decreasing_to_release(self):
        """Sigma should decrease (or stay flat) from 14d down to 0d (release)."""
        sigmas = [cpi_nowcast_sigma(d) for d in [14, 10, 7, 3, 1, 0]]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] >= sigmas[i + 1]

    def test_release_day_not_higher_than_day_before(self):
        """Release day (0) has tighter or equal sigma to day-before."""
        assert cpi_nowcast_sigma(0) <= cpi_nowcast_sigma(1)

    def test_negative_days_use_floor(self):
        """Negative days_to_release should return the floor (0.03)."""
        assert cpi_nowcast_sigma(-1) == 0.03
        assert cpi_nowcast_sigma(-5) == 0.03

    def test_integration_with_econ_nowcast(self):
        """CPI sigma feeds into econ_nowcast_probability correctly."""
        sigma = cpi_nowcast_sigma(0)
        # 5bp gap with 3bp sigma -> z=1.67 -> high confidence
        prob = econ_nowcast_probability(2.80, sigma, 2.85, "above")
        # nowcast (2.80) is below threshold (2.85), so P(above 2.85) < 0.5
        assert prob < 0.5


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
