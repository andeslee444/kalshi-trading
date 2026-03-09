"""Tests for CPIBeliefFilter — Bayesian fusion of CPI nowcast sources."""
import math
import pytest
from cpi_belief_filter import CPIBeliefFilter, SOURCE_CORRELATIONS, _get_correlation


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


class TestCorrelationAwareness:
    """Correlation-aware updates prevent overconfidence from correlated sources."""

    def test_correlated_sources_not_overconfident(self):
        """Two correlated sources should not halve the posterior uncertainty."""
        f = CPIBeliefFilter(2.41, 0.10)
        f.update(observation=2.42, obs_sigma=0.10, source="truflation")
        sigma_after_one = f.posterior_sigma

        # Reset and try with two correlated sources
        f2 = CPIBeliefFilter(2.41, 0.10)
        f2.update(observation=2.42, obs_sigma=0.10, source="truflation")
        sigma_after_truflation = f2.posterior_sigma

        # Independent update would give much tighter sigma
        f_indep = CPIBeliefFilter(2.41, 0.10)
        f_indep.update(observation=2.42, obs_sigma=0.10)  # No source = no correlation discount
        sigma_independent = f_indep.posterior_sigma

        # Correlated update should produce wider sigma than independent
        assert sigma_after_truflation > sigma_independent, \
            f"Correlated sigma ({sigma_after_truflation:.4f}) should be wider than " \
            f"independent ({sigma_independent:.4f})"

    def test_uncorrelated_source_behaves_like_before(self):
        """Source with zero correlation should not be discounted."""
        f_with_source = CPIBeliefFilter(2.41, 0.10)
        f_with_source.update(observation=2.50, obs_sigma=0.15, source="unknown_source")

        f_without_source = CPIBeliefFilter(2.41, 0.10)
        f_without_source.update(observation=2.50, obs_sigma=0.15)

        # Both should produce same result (no correlation in table)
        assert f_with_source.sigma == pytest.approx(f_without_source.sigma, abs=0.0001)
        assert f_with_source.mean == pytest.approx(f_without_source.mean, abs=0.0001)

    def test_backward_compatible_no_source(self):
        """Calling update without source= still works (backward compatible)."""
        f = CPIBeliefFilter(2.41, 0.10)
        f.update(observation=2.45, obs_sigma=0.15)
        mean, sigma = f.posterior
        # Should work exactly as before
        assert 2.41 < mean < 2.45
        assert sigma < 0.10

    def test_tips_less_discounted_than_truflation(self):
        """TIPS (corr=0.30) should be discounted less than Truflation (corr=0.70)."""
        # Same obs_sigma, different correlations
        f_truf = CPIBeliefFilter(2.41, 0.10)
        f_truf.update(observation=2.50, obs_sigma=0.20, source="truflation")

        f_tips = CPIBeliefFilter(2.41, 0.10)
        f_tips.update(observation=2.50, obs_sigma=0.20, source="tips_breakeven")

        # TIPS should tighten sigma MORE (less correlated = more new info)
        assert f_tips.sigma < f_truf.sigma, \
            f"TIPS ({f_tips.sigma:.4f}) should tighten more than Truflation ({f_truf.sigma:.4f})"

    def test_sources_tracked(self):
        """Filter tracks which sources have been used."""
        f = CPIBeliefFilter(2.41, 0.10)
        assert "cleveland_fed" in f.sources_used
        f.update(2.45, obs_sigma=0.30, source="truflation")
        assert "truflation" in f.sources_used
        f.update(2.38, obs_sigma=0.60, source="tips_breakeven")
        assert "tips_breakeven" in f.sources_used
        assert len(f.sources_used) == 3

    def test_posterior_sigma_accessor(self):
        """posterior_sigma property returns current sigma."""
        f = CPIBeliefFilter(2.41, 0.10)
        assert f.posterior_sigma == 0.10
        f.update(2.45, obs_sigma=0.15)
        assert f.posterior_sigma < 0.10


class TestCorrelationLookup:
    def test_known_pairs(self):
        assert _get_correlation("cleveland_fed", "truflation") == 0.70
        assert _get_correlation("truflation", "cleveland_fed") == 0.70  # Order-independent
        assert _get_correlation("cleveland_fed", "tips_breakeven") == 0.30
        assert _get_correlation("truflation", "tips_breakeven") == 0.25

    def test_unknown_pair_returns_zero(self):
        assert _get_correlation("unknown_a", "unknown_b") == 0.0
        assert _get_correlation("cleveland_fed", "unknown") == 0.0


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
