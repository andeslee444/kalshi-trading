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


class TestEnsembleCalibration:
    """EnsembleModel should accept and use calibration overrides."""

    def test_custom_bma_weights_change_output(self):
        from crypto_models import EnsembleModel
        model_default = EnsembleModel()
        # Custom weights: 100% GBM, 0% JD, 0% Heston
        custom_weights = {
            "normal": [1.0, 0.0, 0.0],
            "low_vol": [1.0, 0.0, 0.0],
            "high_vol": [1.0, 0.0, 0.0],
            "crisis": [1.0, 0.0, 0.0],
        }
        model_custom = EnsembleModel(bma_weights=custom_weights)
        # Use crisis regime where weight differences are largest
        prob_default = model_default.estimate_prob(
            current_price=80000, threshold=83000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="crisis",
        )
        prob_custom = model_custom.estimate_prob(
            current_price=80000, threshold=83000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="crisis",
        )
        # Custom uses 100% GBM, default crisis uses 5% GBM / 35% JD / 60% Heston
        assert abs(prob_default - prob_custom) > 0.0005

    def test_custom_heston_params_used(self):
        from crypto_models import EnsembleModel
        # Very different v0 and kappa — use longer horizon for more divergence
        model_a = EnsembleModel(heston_params={"v0": 0.10, "kappa": 0.5, "theta": 0.10, "xi": 0.3, "rho": -0.7})
        model_b = EnsembleModel(heston_params={"v0": 0.50, "kappa": 10.0, "theta": 0.50, "xi": 0.3, "rho": -0.7})
        prob_a = model_a.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=10080, vol=0.50,
        )
        prob_b = model_b.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=10080, vol=0.50,
        )
        assert abs(prob_a - prob_b) > 0.001

    def test_from_calibration_dict(self):
        """EnsembleModel.from_calibration() should extract weights and params."""
        from crypto_models import EnsembleModel
        cal = {
            "assets": {
                "BTC": {
                    "ensemble_weights": [0.20, 0.50, 0.30],
                    "heston_params": {"kappa": 3.0, "xi": 0.4, "rho": -0.6},
                    "jd_lambda": 2.0,
                }
            }
        }
        model = EnsembleModel.from_calibration(cal, asset="BTC")
        assert model is not None
        # Verify it uses calibrated weights (not defaults)
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
        )
        assert 0.001 <= prob <= 0.999

    def test_from_calibration_missing_asset_uses_defaults(self):
        """Missing asset in calibration should not crash."""
        from crypto_models import EnsembleModel
        cal = {"assets": {"ETH": {"ensemble_weights": [0.3, 0.4, 0.3]}}}
        model = EnsembleModel.from_calibration(cal, asset="BTC")
        assert model is not None


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

    def test_extreme_prob_near_base(self):
        from crypto_models import smooth_edge_threshold
        threshold = smooth_edge_threshold(0.05, base=0.06)
        assert 0.06 <= threshold < 0.065

    def test_mid_prob_highest_threshold(self):
        from crypto_models import smooth_edge_threshold
        threshold = smooth_edge_threshold(0.50, base=0.06)
        # base + 0.09 * 0.25 = 0.0825
        assert abs(threshold - 0.0825) < 0.001

    def test_smooth_no_discontinuity(self):
        from crypto_models import smooth_edge_threshold
        t_24 = smooth_edge_threshold(0.24, base=0.06)
        t_25 = smooth_edge_threshold(0.25, base=0.06)
        t_26 = smooth_edge_threshold(0.26, base=0.06)
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
        assert abs(frac - 0.15) < 0.02  # Updated: was 0.125

    def test_24h_returns_half_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(1440)
        assert abs(frac - 0.50) < 0.01

    def test_1h_returns_third_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(60)
        assert abs(frac - 0.333) < 0.02  # Updated: was 0.25

    def test_6h_returns_three_eighths_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(360)
        assert abs(frac - 0.375) < 0.02  # New tier


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


class TestHestonFellerFallback:
    """Heston should fall back to GBM when Feller condition is violated."""

    def test_feller_violated_returns_valid_prob(self):
        """When xi is too high for Feller, should still return a sane probability."""
        from probability import crypto_price_probability_heston, crypto_price_probability
        # Feller violated: 2*2*0.25=1.0 < 2.0^2=4.0
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=1440, v0=0.25, kappa=2.0, theta=0.25, xi=2.0, rho=-0.7,
        )
        # Should match GBM fallback (vol=sqrt(v0)=0.5)
        gbm_prob = crypto_price_probability(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.5,
        )
        assert 0.001 <= prob <= 0.999
        assert abs(prob - gbm_prob) < 0.01  # Should be close to GBM fallback

    def test_feller_satisfied_does_not_fallback(self):
        """When Feller is satisfied, Heston and GBM should differ."""
        from probability import crypto_price_probability_heston, crypto_price_probability
        # Feller OK: 2*2*0.25=1.0 >= 0.3^2=0.09
        prob_heston = crypto_price_probability_heston(
            current_price=80000, threshold=84000, direction="above",
            time_horizon_minutes=1440, v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        prob_gbm = crypto_price_probability(
            current_price=80000, threshold=84000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.5,
        )
        # Heston and GBM should give different results (stoch vol effect)
        assert abs(prob_heston - prob_gbm) > 0.0005

    def test_extreme_xi_from_garch(self):
        """Simulate GARCH-driven xi=2.0 with low kappa — must not crash or return NaN."""
        from probability import crypto_price_probability_heston
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=85000, direction="above",
            time_horizon_minutes=1440, v0=0.5, kappa=1.0, theta=0.25, xi=2.0, rho=-0.9,
        )
        assert 0.001 <= prob <= 0.999
        assert not math.isnan(prob)


class TestGARCHActualInterval:
    """GARCH should track actual observation intervals for annualization."""

    def test_update_with_interval(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed 15 returns with known interval (5 min = 300 seconds)
        for i in range(15):
            garch.update(0.001 * (i % 3 - 1), interval_seconds=300)
        vol = garch.forecast_vol()
        assert vol is not None
        assert vol > 0

    def test_annualize_from_actual_interval(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        for i in range(15):
            garch.update(0.001 * (i % 3 - 1), interval_seconds=300)
        # Annualize using actual interval
        vol_5min = garch.forecast_vol(use_actual_interval=True)
        # Compare with hardcoded 5-min annualization
        vol_hardcoded = garch.forecast_vol(annualize_factor=365.25 * 24 * 12)
        # Should be similar when actual interval matches assumed
        assert vol_5min is not None
        assert abs(vol_5min - vol_hardcoded) / max(vol_hardcoded, 0.01) < 0.1

    def test_longer_interval_lower_annualized_vol(self):
        """Same per-step variance with longer interval should give lower annualized vol."""
        from vol_forecaster import GARCHForecaster
        garch_5m = GARCHForecaster()
        garch_30m = GARCHForecaster()
        returns = [0.001, -0.002, 0.0015, -0.001, 0.0005] * 3
        for r in returns:
            garch_5m.update(r, interval_seconds=300)
            garch_30m.update(r, interval_seconds=1800)
        vol_5m = garch_5m.forecast_vol(use_actual_interval=True)
        vol_30m = garch_30m.forecast_vol(use_actual_interval=True)
        # Same per-step variance, but 30-min has fewer intervals/year -> lower annualized vol
        assert vol_30m is not None and vol_5m is not None
        assert vol_30m < vol_5m


class TestParticleFilterSmoothing:
    """Verify filter provides actual smoothing with reduced process noise."""

    def test_low_noise_smooths_outliers(self):
        from particle_filter import ParticleFilter, FilterConfig
        config = FilterConfig(n_particles=200, process_noise=0.005, observation_noise=0.05)
        pf = ParticleFilter(config=config)
        # Feed stable 0.70 observations
        for _ in range(10):
            pf.update(0.70)
        stable_est = pf.estimate()
        # Now feed a single outlier
        pf.update(0.40)
        outlier_est = pf.estimate()
        # With low process noise, the filter should resist the outlier
        # Estimate should still be closer to 0.70 than to 0.40
        assert outlier_est.prob > 0.55, f"Filter should smooth outlier, got {outlier_est.prob}"

    def test_high_noise_does_not_smooth(self):
        from particle_filter import ParticleFilter, FilterConfig
        config = FilterConfig(n_particles=200, process_noise=0.02, observation_noise=0.05)
        pf = ParticleFilter(config=config)
        for _ in range(10):
            pf.update(0.70)
        pf.update(0.40)
        outlier_est = pf.estimate()
        # With high process noise, the filter barely resists
        # This test documents the current behavior (less smoothing)
        assert outlier_est.prob < 0.65  # Much more influenced by outlier


class TestDCCWarmup:
    """DCC should not report ready before actual warmup."""

    def test_correlation_matrix_none_during_warmup(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"], min_observations=10)
        # Feed 14 returns — GARCH needs 10 to warm up
        # Buggy code: _count=14, 14>=10 → matrix returned (wrong!)
        # Fixed code: real DCC updates=4 (obs 11-14), 4<10 → None (correct)
        for i in range(14):
            dcc.update({"BTC": 0.01 * (i % 3 - 1), "ETH": 0.008 * (i % 3 - 1)})
        result = dcc.correlation_matrix()
        assert result is None

    def test_correlation_matrix_ready_after_real_warmup(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"], min_observations=5)
        # Feed 30 returns (enough for GARCH warmup + 20 DCC updates)
        for i in range(30):
            dcc.update({"BTC": 0.01 * (i % 3 - 1), "ETH": 0.008 * (i % 3 - 1)})
        result = dcc.correlation_matrix()
        # After 30 updates with 10 GARCH warmup, DCC has ~20 actual updates > 5
        assert result is not None


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
