"""Tests for CPIBeliefFilter — Bayesian fusion of CPI nowcast sources."""
import math
import pytest
from cpi_belief_filter import CPIBeliefFilter


class TestFilterConstruction:
    def test_initial_posterior_equals_prior(self):
        f = CPIBeliefFilter(prior_mean=2.41, prior_sigma=0.10)
        mean, sigma = f.posterior
        assert mean == pytest.approx(2.41)
        assert sigma == pytest.approx(0.10)


class TestSingleUpdate:
    def test_agreeing_observation_tightens_sigma(self):
        """When observation agrees with prior, posterior sigma shrinks."""
        f = CPIBeliefFilter(2.41, 0.10)
        f.update(observation=2.40, obs_sigma=0.15)
        mean, sigma = f.posterior
        # Posterior sigma < min(prior_sigma, obs_sigma)
        assert sigma < 0.10
        assert sigma < 0.15
        # Mean should be between 2.40 and 2.41, weighted toward prior (tighter)
        assert 2.40 <= mean <= 2.41

    def test_disagreeing_observation_shifts_mean(self):
        """When observation disagrees, mean shifts toward it proportionally."""
        f = CPIBeliefFilter(2.41, 0.10)
        f.update(observation=2.80, obs_sigma=0.10)
        mean, sigma = f.posterior
        # Equal precision -> mean should be midpoint
        assert mean == pytest.approx(2.605, abs=0.01)

    def test_high_uncertainty_observation_barely_moves_mean(self):
        """Wide obs_sigma = low precision = small influence."""
        f = CPIBeliefFilter(2.41, 0.10)
        f.update(observation=3.00, obs_sigma=1.00)
        mean, _ = f.posterior
        # obs_sigma=1.00 has 1% of prior's precision -- barely moves
        assert abs(mean - 2.41) < 0.07


class TestMultipleUpdates:
    def test_three_sources_tighter_than_any_single(self):
        """Fusing 3 sources -> posterior sigma < all individual sigmas."""
        f = CPIBeliefFilter(2.41, 0.10)    # Cleveland Fed
        f.update(2.45, obs_sigma=0.15)      # Truflation
        f.update(2.38, obs_sigma=0.25)      # TIPS
        _, sigma = f.posterior
        assert sigma < 0.10
        assert sigma < 0.15
        assert sigma < 0.25

    def test_posterior_precision_is_sum_of_precisions(self):
        """Verify Bayesian conjugate math: posterior_prec = sum(precisions)."""
        prior_sigma = 0.10
        obs1_sigma = 0.15
        obs2_sigma = 0.25

        expected_precision = (1/prior_sigma**2) + (1/obs1_sigma**2) + (1/obs2_sigma**2)
        expected_sigma = 1 / math.sqrt(expected_precision)

        f = CPIBeliefFilter(2.41, prior_sigma)
        f.update(2.41, obs1_sigma)  # Same value to isolate sigma math
        f.update(2.41, obs2_sigma)
        _, sigma = f.posterior
        assert sigma == pytest.approx(expected_sigma, abs=0.001)


class TestGracefulDegradation:
    def test_no_updates_returns_prior(self):
        """With no observations, posterior equals prior."""
        f = CPIBeliefFilter(2.41, 0.30)
        mean, sigma = f.posterior
        assert mean == 2.41
        assert sigma == 0.30

    def test_single_source_still_works(self):
        """With only Truflation (no TIPS), filter still produces valid posterior."""
        f = CPIBeliefFilter(2.41, 0.30)
        f.update(2.50, obs_sigma=0.15)
        mean, sigma = f.posterior
        assert 2.41 < mean < 2.50
        assert sigma < 0.15  # Tighter than Truflation alone


class TestEdgeCases:
    def test_very_small_sigma_prior(self):
        """Near-release prior (small sigma) dominates observations."""
        f = CPIBeliefFilter(2.41, 0.01)  # Very confident prior
        f.update(3.00, obs_sigma=0.15)   # Wildly different obs
        mean, _ = f.posterior
        # Prior dominates -- mean barely moves
        assert abs(mean - 2.41) < 0.02

    def test_zero_sigma_not_allowed(self):
        """Filter should not crash with very small but non-zero sigma."""
        f = CPIBeliefFilter(2.41, 0.001)
        f.update(2.50, obs_sigma=0.001)
        mean, sigma = f.posterior
        assert sigma > 0
        assert not math.isnan(mean)
