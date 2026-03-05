"""Tests for crypto model stack — GBM, JD, Heston, AR1 Vol, BMA Ensemble."""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "kalshi"))

from probability import _reset_calibration


def setup_module():
    _reset_calibration()

def teardown_module():
    _reset_calibration()


class TestEnsembleModel:
    """BMA ensemble blending of sub-models."""

    def test_ensemble_returns_valid_prob(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
        )
        assert 0.001 <= prob <= 0.999

    def test_ensemble_atm_near_50(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=1440, vol=0.50,
        )
        assert 0.40 < prob < 0.60

    def test_ensemble_regime_changes_weights(self):
        """Different regimes should produce different probabilities for OTM."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        # Moderately OTM market where fat-tail models matter
        prob_calm = model.estimate_prob(
            current_price=80000, threshold=84000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="low_vol",
        )
        prob_crisis = model.estimate_prob(
            current_price=80000, threshold=84000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="crisis",
        )
        # Crisis regime weights fat-tail models more -> different prob
        assert abs(prob_crisis - prob_calm) > 0.001

    def test_ensemble_bracket_probability(self):
        """Bracket = P(above low) - P(above high)."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_bracket_prob(
            current_price=80000, low_threshold=79000, high_threshold=81000,
            time_horizon_minutes=60, vol=0.50,
        )
        assert 0.0 < prob < 1.0

    def test_ensemble_default_regime_is_normal(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
        )
        prob_explicit = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50, regime="normal",
        )
        assert abs(prob - prob_explicit) < 0.001

    def test_ensemble_with_heston_params(self):
        """Passing Heston params should use them."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
            heston_params={"v0": 0.25, "kappa": 2.0, "theta": 0.25, "xi": 0.3, "rho": -0.7},
        )
        assert 0.001 <= prob <= 0.999


class TestRegimeAwareJD:
    """Jump-diffusion with regime-dependent lambda."""

    def test_crisis_regime_higher_jump_intensity(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        # Access JD prob directly through model
        prob_calm = model._jd_prob(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="low_vol",
        )
        prob_crisis = model._jd_prob(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="crisis",
        )
        # Higher lambda in crisis -> fatter tails -> higher OTM prob
        assert prob_crisis > prob_calm


class TestAR1Vol:
    """AR(1) volatility forecast."""

    def test_ar1_forecast_returns_positive(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        ar1.update(0.50)
        ar1.update(0.55)
        ar1.update(0.52)
        forecast = ar1.forecast()
        assert forecast > 0

    def test_ar1_mean_reverts(self):
        """After high vol, forecast should revert toward long-run mean."""
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast(alpha=0.05, beta=0.85, long_run=0.50)
        # Feed high vol
        for _ in range(20):
            ar1.update(1.0)
        high_forecast = ar1.forecast()
        # Feed normal vol
        for _ in range(20):
            ar1.update(0.50)
        normal_forecast = ar1.forecast()
        assert normal_forecast < high_forecast

    def test_ar1_insufficient_data_returns_none(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        assert ar1.forecast() is None


class TestSmoothEdgeThreshold:
    """Smooth edge threshold function."""

    def test_extreme_prob_uses_base_threshold(self):
        from crypto_models import smooth_edge_threshold
        # prob=0.05 (very confident) -> near base threshold
        threshold = smooth_edge_threshold(0.05, base=0.06)
        assert 0.06 <= threshold < 0.07

    def test_mid_prob_uses_higher_threshold(self):
        from crypto_models import smooth_edge_threshold
        # prob=0.50 (max uncertainty) -> highest threshold
        threshold = smooth_edge_threshold(0.50, base=0.06)
        assert threshold > 0.07

    def test_smooth_no_discontinuity(self):
        """No jump at the old 0.25/0.75 boundary."""
        from crypto_models import smooth_edge_threshold
        t_24 = smooth_edge_threshold(0.24, base=0.06)
        t_25 = smooth_edge_threshold(0.25, base=0.06)
        t_26 = smooth_edge_threshold(0.26, base=0.06)
        # Should be smooth — no big jump between adjacent values
        assert abs(t_25 - t_24) < 0.005
        assert abs(t_26 - t_25) < 0.005

    def test_symmetric_around_50(self):
        from crypto_models import smooth_edge_threshold
        t_30 = smooth_edge_threshold(0.30, base=0.06)
        t_70 = smooth_edge_threshold(0.70, base=0.06)
        assert abs(t_30 - t_70) < 0.001


class TestHorizonKelly:
    """Horizon-scaled Kelly fractions."""

    def test_short_horizon_smaller_kelly(self):
        from crypto_models import horizon_kelly_fraction
        short = horizon_kelly_fraction(5)    # 5 min
        long = horizon_kelly_fraction(1440)  # 24h
        assert short < long

    def test_5min_returns_eighth_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(5)
        assert abs(frac - 0.125) < 0.01

    def test_24h_returns_half_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(1440)
        assert abs(frac - 0.50) < 0.01

    def test_1h_returns_quarter_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(60)
        assert abs(frac - 0.25) < 0.02


class TestHorizonVolWeight:
    """Horizon-dependent IV/RV weighting."""

    def test_short_horizon_prefers_rv(self):
        from crypto_models import horizon_vol_weights
        w_iv, w_rv = horizon_vol_weights(5)  # 5 min
        assert w_rv > w_iv

    def test_long_horizon_prefers_iv(self):
        from crypto_models import horizon_vol_weights
        w_iv, w_rv = horizon_vol_weights(1440)  # 24h
        assert w_iv > w_rv

    def test_weights_sum_to_one(self):
        from crypto_models import horizon_vol_weights
        for t in [5, 15, 60, 360, 1440, 10080]:
            w_iv, w_rv = horizon_vol_weights(t)
            assert abs(w_iv + w_rv - 1.0) < 0.001


class TestAR1NegativeVol:
    """AR1 should never return negative vol."""

    def test_floor_at_positive_value(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        # Feed decreasing vols toward zero
        for v in [0.5, 0.3, 0.1, 0.01, 0.001]:
            ar1.update(v)
        forecast = ar1.forecast()
        assert forecast is not None
        assert forecast >= 0.01

    def test_constant_zero_vol_returns_long_run(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        for _ in range(10):
            ar1.update(0.0)
        forecast = ar1.forecast()
        # Should return long_run (0.50) since all identical -> den=0
        assert forecast == 0.50
