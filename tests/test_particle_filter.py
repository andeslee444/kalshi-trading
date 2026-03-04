"""Tests for particle_filter.py — Sequential Monte Carlo belief state."""

import json
import math
import random
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from particle_filter import (
    ParticleFilter,
    FilteredEstimate,
    FilterConfig,
    FilterManager,
    ci_kelly_multiplier,
)


class TestParticleFilterInit:
    """Test particle filter initialization."""

    def test_default_config(self):
        """Default config creates 200 particles."""
        pf = ParticleFilter()
        assert pf.n_particles == 200
        assert len(pf.particles) == 200
        assert len(pf.weights) == 200

    def test_custom_particle_count(self):
        """Custom particle count works."""
        pf = ParticleFilter(config=FilterConfig(n_particles=500))
        assert pf.n_particles == 500
        assert len(pf.particles) == 500

    def test_initial_particles_uniform(self):
        """Initial particles should be uniformly distributed in [0, 1]."""
        pf = ParticleFilter(config=FilterConfig(n_particles=1000))
        mean = sum(pf.particles) / len(pf.particles)
        assert mean == pytest.approx(0.5, abs=0.05)

    def test_initial_weights_uniform(self):
        """Initial weights should be equal (1/N)."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        expected = 1.0 / 100
        for w in pf.weights:
            assert w == pytest.approx(expected, abs=1e-6)

    def test_initial_estimate(self):
        """Initial estimate should be ~0.5 with wide CI."""
        pf = ParticleFilter(config=FilterConfig(n_particles=1000))
        est = pf.estimate()
        assert est.prob == pytest.approx(0.5, abs=0.05)
        assert est.ci_low < 0.3
        assert est.ci_high > 0.7
        assert est.n_updates == 0
        assert est.trend == "none"


class TestFilteredEstimate:
    """Test FilteredEstimate data structure."""

    def test_ci_width(self):
        """CI width computation."""
        est = FilteredEstimate(prob=0.6, ci_low=0.5, ci_high=0.7,
                               trend="none", n_updates=5)
        assert est.ci_width == pytest.approx(0.2, abs=0.01)

    def test_is_confident_narrow_ci(self):
        """Narrow CI -> confident."""
        est = FilteredEstimate(prob=0.6, ci_low=0.55, ci_high=0.65,
                               trend="none", n_updates=10)
        assert est.is_confident(threshold=0.15)

    def test_not_confident_wide_ci(self):
        """Wide CI -> not confident."""
        est = FilteredEstimate(prob=0.6, ci_low=0.3, ci_high=0.9,
                               trend="none", n_updates=2)
        assert not est.is_confident(threshold=0.15)


class TestPredictionStep:
    """Test the prediction (diffusion) step."""

    def test_predict_adds_noise(self):
        """After prediction, particles should spread out."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.05))
        # Set all particles to 0.5
        pf.particles = [0.5] * pf.n_particles
        pf.weights = [1.0 / pf.n_particles] * pf.n_particles

        pf.predict()

        # Particles should no longer all be 0.5
        unique = len(set(pf.particles))
        assert unique > 1
        # Mean should still be near 0.5
        mean = sum(pf.particles) / len(pf.particles)
        assert mean == pytest.approx(0.5, abs=0.05)

    def test_predict_clamps_to_valid_range(self):
        """Particles should stay in (0, 1) after prediction."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=500, process_noise=0.1))
        # Set particles near boundaries
        pf.particles = [0.01] * 250 + [0.99] * 250

        pf.predict()

        for p in pf.particles:
            assert 0.0 < p < 1.0

    def test_predict_with_zero_noise(self):
        """Zero process noise -> particles unchanged."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=100, process_noise=0.0))
        pf.particles = [0.5] * pf.n_particles
        original = list(pf.particles)

        pf.predict()

        assert pf.particles == original

    def test_predict_spread_scales_with_noise(self):
        """Higher process noise -> wider particle spread."""
        pf_low = ParticleFilter(config=FilterConfig(n_particles=2000, process_noise=0.01))
        pf_high = ParticleFilter(config=FilterConfig(n_particles=2000, process_noise=0.10))
        pf_low.particles = [0.5] * pf_low.n_particles
        pf_high.particles = [0.5] * pf_high.n_particles

        pf_low.predict()
        pf_high.predict()

        var_low = sum((p - 0.5)**2 for p in pf_low.particles) / len(pf_low.particles)
        var_high = sum((p - 0.5)**2 for p in pf_high.particles) / len(pf_high.particles)
        assert var_high > var_low


class TestUpdateStep:
    """Test the update (observation) step with resampling."""

    def test_update_shifts_estimate_toward_observation(self):
        """After observing 0.8, estimate should move toward 0.8."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=2000, process_noise=0.01, observation_noise=0.05))
        # Start uniform
        est_before = pf.estimate()
        assert est_before.prob == pytest.approx(0.5, abs=0.1)

        pf.update(0.8)
        est_after = pf.estimate()
        assert est_after.prob > est_before.prob
        assert est_after.prob > 0.6

    def test_multiple_updates_converge(self):
        """Repeated observations at 0.7 should converge the filter."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.01, observation_noise=0.05))

        for _ in range(10):
            pf.update(0.7)

        est = pf.estimate()
        assert est.prob == pytest.approx(0.7, abs=0.05)
        assert est.ci_width < 0.15  # should be relatively narrow

    def test_update_increments_count(self):
        """Each update increments n_updates."""
        pf = ParticleFilter()
        assert pf.estimate().n_updates == 0
        pf.update(0.5)
        assert pf.estimate().n_updates == 1
        pf.update(0.6)
        assert pf.estimate().n_updates == 2

    def test_conflicting_observations_center_estimate(self):
        """Alternating high/low observations should center the estimate."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.02, observation_noise=0.05))

        # Alternate between 0.3 and 0.7
        for _ in range(5):
            pf.update(0.3)
            pf.update(0.7)

        est = pf.estimate()
        # Estimate should be near the mean of the conflicting observations
        assert est.prob == pytest.approx(0.5, abs=0.1)
        # Trend should be "none" (no consistent direction)
        assert est.trend == "none"

    def test_update_with_high_observation_noise(self):
        """High observation noise -> slower convergence."""
        pf_tight = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.01, observation_noise=0.02))
        pf_loose = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.01, observation_noise=0.20))

        for _ in range(5):
            pf_tight.update(0.8)
            pf_loose.update(0.8)

        est_tight = pf_tight.estimate()
        est_loose = pf_loose.estimate()
        # Tight obs noise -> closer to 0.8 and narrower CI
        assert est_tight.ci_width < est_loose.ci_width


class TestSystematicResampling:
    """Test systematic resampling algorithm."""

    def test_resampling_preserves_particle_count(self):
        """After resampling, still have N particles."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        # Make weights very uneven (should trigger resampling)
        pf.weights = [0.0] * 99 + [1.0]
        pf._systematic_resample()
        assert len(pf.particles) == 100
        assert len(pf.weights) == 100

    def test_resampling_equalizes_weights(self):
        """After resampling, weights should be uniform."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        pf.weights = [0.0] * 99 + [1.0]
        pf._systematic_resample()
        expected = 1.0 / 100
        for w in pf.weights:
            assert w == pytest.approx(expected, abs=1e-6)

    def test_resampling_concentrates_on_high_weight(self):
        """High-weight particle should be duplicated many times."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        pf.particles = [float(i) / 100 for i in range(100)]
        # Give all weight to particle at 0.75
        pf.weights = [0.0] * 100
        pf.weights[75] = 1.0
        pf._systematic_resample()
        # Most particles should now be at 0.75
        count_75 = sum(1 for p in pf.particles if abs(p - 0.75) < 0.01)
        assert count_75 > 80

    def test_ess_computation(self):
        """Effective sample size with uniform weights = N."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        ess = pf._effective_sample_size()
        assert ess == pytest.approx(100, abs=1.0)

    def test_ess_with_degenerate_weights(self):
        """ESS with one dominant weight -> near 1."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        pf.weights = [0.0001] * 99 + [1.0 - 0.0099]
        ess = pf._effective_sample_size()
        assert ess < 5


class TestStatePersistence:
    """Test JSON state serialization/deserialization."""

    def test_serialize_roundtrip(self):
        """Serialize then deserialize preserves state."""
        pf = ParticleFilter(config=FilterConfig(n_particles=50))
        pf.update(0.7)
        pf.update(0.7)

        state = pf.serialize()
        pf2 = ParticleFilter.deserialize(state)

        est1 = pf.estimate()
        est2 = pf2.estimate()
        assert est1.prob == pytest.approx(est2.prob, abs=0.001)
        assert est1.n_updates == est2.n_updates
        assert len(pf2.particles) == 50

    def test_save_and_load_file(self, tmp_path):
        """Save to file and load back."""
        pf = ParticleFilter(config=FilterConfig(n_particles=50))
        pf.update(0.8)

        filepath = tmp_path / "pf-test.json"
        pf.save(filepath)
        assert filepath.exists()

        pf2 = ParticleFilter.load(filepath)
        est = pf2.estimate()
        assert est.n_updates == 1

    def test_load_nonexistent_returns_new(self, tmp_path):
        """Loading from nonexistent file returns fresh filter."""
        filepath = tmp_path / "nonexistent.json"
        pf = ParticleFilter.load(filepath)
        assert pf.estimate().n_updates == 0

    def test_load_corrupt_file_returns_new(self, tmp_path):
        """Loading corrupt JSON returns fresh filter."""
        filepath = tmp_path / "corrupt.json"
        filepath.write_text("{invalid json")
        pf = ParticleFilter.load(filepath)
        assert pf.estimate().n_updates == 0

    def test_staleness_check(self, tmp_path):
        """State older than max_age is rejected."""
        pf = ParticleFilter(config=FilterConfig(n_particles=50))
        pf.update(0.8)

        filepath = tmp_path / "pf-stale.json"
        pf.save(filepath)

        # Manually backdate the saved_at timestamp
        data = json.loads(filepath.read_text())
        data["saved_at"] = time.time() - 100000
        filepath.write_text(json.dumps(data))

        pf2 = ParticleFilter.load(filepath, max_age_seconds=3600)
        assert pf2.estimate().n_updates == 0  # rejected as stale

    def test_serialize_includes_config(self):
        """Serialized state includes config for reconstruction."""
        config = FilterConfig(n_particles=100, process_noise=0.03)
        pf = ParticleFilter(config=config)
        state = pf.serialize()
        assert state["config"]["process_noise"] == 0.03
        assert state["config"]["n_particles"] == 100

    def test_deserialize_mismatched_lengths_resets(self):
        """Mismatched particles/weights should create a fresh filter."""
        state = {
            "config": {"n_particles": 200},
            "particles": [0.5] * 200,
            "weights": [0.005] * 150,  # 150 != 200
            "update_count": 10,
        }
        pf = ParticleFilter.deserialize(state)
        assert len(pf.particles) == len(pf.weights)
        assert pf._update_count == 0  # reset to fresh


class TestCIAwareKelly:
    """Test CI-aware position sizing adjustments."""

    def test_narrow_ci_no_reduction(self):
        """Narrow CI -> full Kelly (multiplier = 1.0)."""
        est = FilteredEstimate(prob=0.7, ci_low=0.65, ci_high=0.75,
                               trend="none", n_updates=20)
        mult = ci_kelly_multiplier(est)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_wide_ci_reduces_kelly(self):
        """Wide CI -> reduced Kelly."""
        est = FilteredEstimate(prob=0.7, ci_low=0.4, ci_high=1.0,
                               trend="none", n_updates=5)
        mult = ci_kelly_multiplier(est)
        assert mult < 0.8

    def test_very_wide_ci_minimum_multiplier(self):
        """Very wide CI -> minimum multiplier (0.25)."""
        est = FilteredEstimate(prob=0.5, ci_low=0.05, ci_high=0.95,
                               trend="none", n_updates=1)
        mult = ci_kelly_multiplier(est)
        assert mult == pytest.approx(0.25, abs=0.05)

    def test_trend_bonus(self):
        """Confirmed trend gives a small multiplier bonus."""
        est_trend = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                     trend="up", n_updates=10)
        est_no_trend = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                        trend="none", n_updates=10)
        mult_trend = ci_kelly_multiplier(est_trend)
        mult_no = ci_kelly_multiplier(est_no_trend)
        assert mult_trend >= mult_no

    def test_few_updates_reduces_multiplier(self):
        """Fewer updates -> less confident -> lower multiplier."""
        est_many = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                     trend="none", n_updates=20)
        est_few = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                    trend="none", n_updates=2)
        mult_many = ci_kelly_multiplier(est_many)
        mult_few = ci_kelly_multiplier(est_few)
        assert mult_many >= mult_few


class TestFilterManager:
    """Test per-market filter management."""

    def test_get_or_create_new(self):
        """Getting a new ticker creates a fresh filter."""
        mgr = FilterManager(bot_name="crypto", state_dir=Path("/tmp"))
        pf = mgr.get_filter("KXBTC-MAR-T90000")
        assert isinstance(pf, ParticleFilter)
        assert pf.estimate().n_updates == 0

    def test_get_same_ticker_returns_same_filter(self):
        """Getting the same ticker twice returns the same filter."""
        mgr = FilterManager(bot_name="crypto", state_dir=Path("/tmp"))
        pf1 = mgr.get_filter("KXBTC-MAR-T90000")
        pf1.update(0.7)
        pf2 = mgr.get_filter("KXBTC-MAR-T90000")
        assert pf2.estimate().n_updates == 1

    def test_different_tickers_independent(self):
        """Different tickers get independent filters."""
        mgr = FilterManager(bot_name="crypto", state_dir=Path("/tmp"))
        pf1 = mgr.get_filter("KXBTC-MAR-T90000")
        pf2 = mgr.get_filter("KXETH-MAR-T3000")
        pf1.update(0.8)
        assert pf1.estimate().n_updates == 1
        assert pf2.estimate().n_updates == 0

    def test_save_and_load_all(self, tmp_path):
        """Save all filters, then load them back."""
        mgr = FilterManager(bot_name="test", state_dir=tmp_path)
        pf1 = mgr.get_filter("TICKER1")
        pf1.update(0.7)
        pf2 = mgr.get_filter("TICKER2")
        pf2.update(0.3)

        mgr.save_all()

        # Create new manager, load from same dir
        mgr2 = FilterManager(bot_name="test", state_dir=tmp_path)
        mgr2.load_all()

        pf1_loaded = mgr2.get_filter("TICKER1")
        assert pf1_loaded.estimate().n_updates == 1

    def test_cleanup_expired_filters(self, tmp_path):
        """Filters for settled markets are cleaned up."""
        mgr = FilterManager(bot_name="test", state_dir=tmp_path)
        mgr.get_filter("ACTIVE_TICKER")
        mgr.get_filter("EXPIRED_TICKER")

        active_tickers = {"ACTIVE_TICKER"}
        mgr.cleanup(active_tickers)

        assert "ACTIVE_TICKER" in mgr._filters
        assert "EXPIRED_TICKER" not in mgr._filters


class TestCryptoBotIntegration:
    """Test the crypto-bot integration pattern."""

    def test_filtered_prob_replaces_raw(self):
        """Filtered prob should be used instead of raw model prob."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.02, observation_noise=0.05))

        # Simulate 5 scans with raw model prob ~0.7
        for _ in range(5):
            pf.update(0.70 + random.gauss(0, 0.02))

        est = pf.estimate()
        # Filtered prob should be near 0.7
        assert est.prob == pytest.approx(0.7, abs=0.05)
        # CI should be tighter than initial
        assert est.ci_width < 0.30

    def test_ci_aware_sizing_reduces_on_uncertainty(self):
        """When CI is wide, Kelly multiplier should reduce position size."""
        # Few updates -> wide CI -> lower multiplier
        est_uncertain = FilteredEstimate(prob=0.7, ci_low=0.4, ci_high=1.0,
                                          trend="none", n_updates=2)
        # Many updates -> narrow CI -> higher multiplier
        est_confident = FilteredEstimate(prob=0.7, ci_low=0.65, ci_high=0.75,
                                          trend="up", n_updates=20)

        mult_uncertain = ci_kelly_multiplier(est_uncertain)
        mult_confident = ci_kelly_multiplier(est_confident)

        assert mult_confident > mult_uncertain
        assert mult_confident >= 0.9
        assert mult_uncertain < 0.7

    def test_bot_config_mapping(self):
        """Verify bot-specific filter configs are reasonable."""
        configs = {
            "crypto": FilterConfig(n_particles=200, process_noise=0.02,
                                   observation_noise=0.05),
            "weather": FilterConfig(n_particles=200, process_noise=0.01,
                                    observation_noise=0.03),
            "economics": FilterConfig(n_particles=200, process_noise=0.005,
                                      observation_noise=0.02),
        }
        # Crypto should have highest process noise (fast-moving)
        assert configs["crypto"].process_noise > configs["weather"].process_noise
        assert configs["weather"].process_noise > configs["economics"].process_noise
