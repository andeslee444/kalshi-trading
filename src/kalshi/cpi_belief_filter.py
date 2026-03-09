"""Bayesian belief filter for CPI nowcast fusion.

Fuses multiple CPI nowcast sources (Cleveland Fed, Truflation, TIPS breakeven)
into a single posterior estimate using conjugate normal Bayesian inference.

Replaces the heuristic macro_signal.cpi_bias adjustment with proper
precision-weighted fusion. Each source contributes proportionally to its
precision (1/sigma^2), so tighter sources have more influence.

Correlation-aware: when sources share common information (e.g. Cleveland Fed
and Truflation both measure CPI), the filter widens the effective observation
sigma to avoid overcounting evidence. Without this, two correlated sources
would halve posterior sigma as if they were independent, producing
overconfident estimates.
"""

import math


# Pairwise correlation estimates between CPI nowcast sources.
# Higher correlation -> source provides less NEW information.
# cleveland_fed is implicit (it's the prior, not an observation).
SOURCE_CORRELATIONS = {
    ("cleveland_fed", "truflation"): 0.70,       # Both track CPI, high overlap
    ("cleveland_fed", "tips_breakeven"): 0.30,    # TIPS = market expectations, not CPI
    ("truflation", "tips_breakeven"): 0.25,       # Different methodologies
}


def _get_correlation(source_a, source_b):
    """Look up pairwise correlation (order-independent)."""
    key = (source_a, source_b)
    if key in SOURCE_CORRELATIONS:
        return SOURCE_CORRELATIONS[key]
    key_rev = (source_b, source_a)
    if key_rev in SOURCE_CORRELATIONS:
        return SOURCE_CORRELATIONS[key_rev]
    return 0.0


class CPIBeliefFilter:
    """Conjugate normal Bayesian filter for CPI nowcast fusion.

    Supports correlation-aware updates: when sources are correlated, the
    effective observation sigma is widened to prevent overcounting.

    Usage:
        belief = CPIBeliefFilter(prior_mean=2.41, prior_sigma=0.10)
        belief.update(truflation_cpi, obs_sigma=0.30, source="truflation")
        belief.update(tips_breakeven, obs_sigma=0.60, source="tips_breakeven")
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
        self.sources_used = ["cleveland_fed"]  # Prior is always Cleveland Fed

    @property
    def posterior_sigma(self):
        """Convenience accessor for current sigma."""
        return self.sigma

    def update(self, observation, obs_sigma, source=None):
        """Conjugate normal update with optional correlation discount.

        When `source` is provided, the effective obs_sigma is widened based
        on correlation with previously incorporated sources. This prevents
        two highly correlated sources from halving posterior sigma as if
        they were independent.

        Effective sigma formula:
            effective_sigma = obs_sigma / sqrt(1 - max_correlation^2)

        At correlation 0.70: effective_sigma = obs_sigma / sqrt(0.51) = obs_sigma * 1.40
        At correlation 0.30: effective_sigma = obs_sigma / sqrt(0.91) = obs_sigma * 1.05
        At correlation 0.00: effective_sigma = obs_sigma (no discount)

        Args:
            observation: Observed CPI estimate from another source.
            obs_sigma: Uncertainty of the observation (likelihood sigma).
            source: Optional source name for correlation discount.
                Known sources: "truflation", "tips_breakeven".
        """
        effective_sigma = obs_sigma

        if source is not None:
            # Find max correlation with any previously used source
            max_corr = 0.0
            for existing in self.sources_used:
                corr = _get_correlation(existing, source)
                if corr > max_corr:
                    max_corr = corr

            # Widen sigma to account for shared information
            # Factor: 1/sqrt(1 - rho^2), derived from conditional variance
            if max_corr > 0:
                discount_factor = 1.0 / math.sqrt(1.0 - max_corr ** 2)
                effective_sigma = obs_sigma * discount_factor

            self.sources_used.append(source)

        prior_precision = 1.0 / (self.sigma ** 2)
        obs_precision = 1.0 / (effective_sigma ** 2)
        posterior_precision = prior_precision + obs_precision

        self.mean = (self.mean * prior_precision + observation * obs_precision) / posterior_precision
        self.sigma = 1.0 / math.sqrt(posterior_precision)

    @property
    def posterior(self):
        """Return (mean, sigma) tuple."""
        return (self.mean, self.sigma)
