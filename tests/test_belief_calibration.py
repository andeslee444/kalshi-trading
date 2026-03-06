"""Tests for calibrated Bayesian belief filter observation sigmas.

Observation sigmas are calibrated against empirical tracking error:
- Truflation: tracks different basket than BLS CPI, ~0.30pp RMSE
- TIPS 10Y breakeven: long-term measure, ~0.60pp mapping error to 1-month CPI
- Cleveland Fed nowcast: prior, sigma from cpi_nowcast_sigma()
"""
import math
import pytest
from cpi_belief_filter import CPIBeliefFilter
from probability import cpi_nowcast_sigma, _reset_calibration


class TestCalibratedObsSigmas:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_truflation_does_not_dominate_prior(self):
        """Truflation (obs_sigma=0.30) should not dominate Cleveland Fed prior.

        With prior_sigma=0.15 (14-day horizon) and obs_sigma=0.30,
        Truflation's precision is 1/0.30^2 = 11.1 vs prior 1/0.15^2 = 44.4.
        Prior should have ~4x the influence.
        """
        prior_sigma = cpi_nowcast_sigma(14)
        belief = CPIBeliefFilter(2.50, prior_sigma)
        belief.update(3.00, obs_sigma=0.30)  # Truflation says 3.0%
        fused, _ = belief.posterior
        # Fused should be closer to prior (2.50) than observation (3.00)
        assert fused < 2.65, f"Truflation pulled fused too far: {fused}"
        assert fused > 2.50, f"Truflation had zero effect: {fused}"

    def test_tips_breakeven_is_weak_signal(self):
        """TIPS 10Y (obs_sigma=0.60) should barely move the posterior.

        At obs_sigma=0.60, precision is 1/0.36 = 2.78 — very weak vs
        prior precision of ~44.4 at 14d. Should move posterior < 0.05pp.
        """
        prior_sigma = cpi_nowcast_sigma(14)
        belief = CPIBeliefFilter(2.50, prior_sigma)
        belief.update(3.00, obs_sigma=0.60)  # TIPS says 3.0%
        fused, _ = belief.posterior
        # Should barely move
        assert fused < 2.55, f"TIPS moved posterior too much: {fused}"

    def test_three_source_fusion_reasonable(self):
        """Full 3-source fusion should produce reasonable posterior."""
        prior_sigma = cpi_nowcast_sigma(30)  # ~0.25
        belief = CPIBeliefFilter(2.50, prior_sigma)
        belief.update(2.60, obs_sigma=0.30)   # Truflation
        belief.update(2.40, obs_sigma=0.60)   # TIPS
        fused, post_sigma = belief.posterior
        # Fused should be close to Cleveland Fed (strongest signal)
        assert 2.45 <= fused <= 2.55
        # Posterior should be tighter than prior
        assert post_sigma < prior_sigma
        # But not unreasonably tight (correlated sources)
        assert post_sigma > prior_sigma * 0.70

    def test_old_obs_sigmas_produce_overconfident_posterior(self):
        """Demonstrate that old obs_sigmas (0.15, 0.25) are too tight.

        With old values, Truflation would have precision 44.4 — equal to
        a 14-day Cleveland Fed nowcast. This is wrong because Truflation
        tracks a different basket.
        """
        prior_sigma = cpi_nowcast_sigma(14)
        belief = CPIBeliefFilter(2.50, prior_sigma)
        belief.update(3.00, obs_sigma=0.15)  # OLD Truflation sigma
        fused_old, sigma_old = belief.posterior
        # With equal precision, fused would be ~2.75 (midpoint)
        # This is too far from prior — proves obs_sigma=0.15 is too tight
        assert fused_old > 2.65, f"Old sigma doesn't pull enough to prove the point: {fused_old}"
