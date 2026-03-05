"""Bayesian belief filter for CPI nowcast fusion.

Fuses multiple CPI nowcast sources (Cleveland Fed, Truflation, TIPS breakeven)
into a single posterior estimate using conjugate normal Bayesian inference.

Replaces the heuristic macro_signal.cpi_bias adjustment with proper
precision-weighted fusion. Each source contributes proportionally to its
precision (1/sigma^2), so tighter sources have more influence.
"""

import math


class CPIBeliefFilter:
    """Conjugate normal Bayesian filter for CPI nowcast fusion.

    Usage:
        belief = CPIBeliefFilter(prior_mean=2.41, prior_sigma=0.10)
        belief.update(truflation_cpi, obs_sigma=0.15)
        belief.update(tips_breakeven, obs_sigma=0.25)
        fused_mean, fused_sigma = belief.posterior
    """

    def __init__(self, prior_mean, prior_sigma):
        """Initialize with Cleveland Fed nowcast as prior.

        Args:
            prior_mean: Cleveland Fed CPI nowcast (e.g., 2.41%)
            prior_sigma: Uncertainty from cpi_nowcast_sigma(days_to_release)
        """
        self.mean = prior_mean
        self.sigma = prior_sigma

    def update(self, observation, obs_sigma):
        """Conjugate normal update: fuse a new observation.

        Posterior precision = prior precision + observation precision.
        Posterior mean = precision-weighted average.

        After N updates, posterior sigma is always tighter than
        any individual source -- this is the information gain.

        Args:
            observation: Observed CPI estimate from another source.
            obs_sigma: Uncertainty of the observation (likelihood sigma).
        """
        prior_precision = 1.0 / (self.sigma ** 2)
        obs_precision = 1.0 / (obs_sigma ** 2)
        posterior_precision = prior_precision + obs_precision

        self.mean = (self.mean * prior_precision + observation * obs_precision) / posterior_precision
        self.sigma = 1.0 / math.sqrt(posterior_precision)

    @property
    def posterior(self):
        """Return (mean, sigma) tuple."""
        return (self.mean, self.sigma)
