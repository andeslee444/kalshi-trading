"""Sequential Monte Carlo (Particle Filter) for Bayesian belief tracking.

Replaces stateless per-scan probability snapshots with belief state that
carries memory across scans. Produces filtered probabilities with
confidence intervals for CI-aware position sizing.

Pure Python — no scipy/numpy dependency.
"""

import json
import math
import random
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Tuple

_log = logging.getLogger("particle_filter")


@dataclass
class FilterConfig:
    """Configuration for a particle filter instance."""
    n_particles: int = 200           # Number of particles
    process_noise: float = 0.02      # Per-step diffusion noise (sigma)
    observation_noise: float = 0.05  # Observation likelihood sigma
    resample_threshold: float = 0.5  # ESS/N ratio to trigger resampling
    trend_window: int = 3            # Consecutive same-direction updates for trend


@dataclass
class FilteredEstimate:
    """Output of the particle filter: filtered probability with CI."""
    prob: float                  # Weighted mean of particles
    ci_low: float                # 5th percentile
    ci_high: float               # 95th percentile
    trend: str                   # "up", "down", "none"
    n_updates: int               # Number of observations incorporated

    @property
    def ci_width(self) -> float:
        """Width of the 90% confidence interval."""
        return self.ci_high - self.ci_low

    def is_confident(self, threshold: float = 0.15) -> bool:
        """True if CI is narrower than threshold."""
        return self.ci_width < threshold


class ParticleFilter:
    """Sequential Monte Carlo filter for probability estimation.

    Maintains a set of weighted particles representing the belief
    distribution over the true probability of an event.

    Usage:
        pf = ParticleFilter(config=FilterConfig(process_noise=0.02))
        pf.update(observation=0.65)      # new scan result
        est = pf.estimate()              # FilteredEstimate
        print(est.prob, est.ci_low, est.ci_high)
    """

    def __init__(self, config: Optional[FilterConfig] = None):
        self.config = config or FilterConfig()
        self.n_particles = self.config.n_particles
        # Initialize particles uniformly in [0, 1]
        self.particles = [random.random() for _ in range(self.n_particles)]
        self.weights = [1.0 / self.n_particles] * self.n_particles
        self._update_count = 0
        self._recent_directions: List[str] = []  # last N update directions
        self._last_prob = 0.5

    def estimate(self) -> FilteredEstimate:
        """Compute the current filtered estimate from particles."""
        # Weighted mean
        prob = sum(p * w for p, w in zip(self.particles, self.weights))
        prob = max(0.001, min(0.999, prob))

        # Weighted percentiles for CI
        ci_low, ci_high = self._weighted_percentiles([0.05, 0.95])

        # Trend detection
        trend = self._detect_trend()

        return FilteredEstimate(
            prob=round(prob, 6),
            ci_low=round(ci_low, 6),
            ci_high=round(ci_high, 6),
            trend=trend,
            n_updates=self._update_count,
        )

    def _weighted_percentiles(self, quantiles: List[float]) -> List[float]:
        """Compute weighted percentiles of the particle distribution."""
        # Sort particles by value, keeping weights aligned
        indexed = sorted(zip(self.particles, self.weights), key=lambda x: x[0])
        cum_weight = 0.0
        results = []
        qi = 0
        for particle, weight in indexed:
            cum_weight += weight
            while qi < len(quantiles) and cum_weight >= quantiles[qi]:
                results.append(particle)
                qi += 1
        # Fill any remaining quantiles with the last particle
        while len(results) < len(quantiles):
            results.append(indexed[-1][0])
        return results

    def _detect_trend(self) -> str:
        """Detect trend from recent update directions."""
        window = self.config.trend_window
        if len(self._recent_directions) < window:
            return "none"
        recent = self._recent_directions[-window:]
        if all(d == "up" for d in recent):
            return "up"
        if all(d == "down" for d in recent):
            return "down"
        return "none"
