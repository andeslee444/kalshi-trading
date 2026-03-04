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

    def predict(self):
        """Prediction step: diffuse particles with process noise.

        Each particle p_i += N(0, process_noise).
        Clamped to (epsilon, 1-epsilon) to avoid degeneracy.
        """
        noise = self.config.process_noise
        if noise <= 0:
            return
        eps = 0.001
        for i in range(self.n_particles):
            self.particles[i] += random.gauss(0, noise)
            self.particles[i] = max(eps, min(1.0 - eps, self.particles[i]))

    def _gaussian_likelihood(self, observation: float, particle: float) -> float:
        """Gaussian likelihood: P(obs | particle) = N(obs; particle, sigma)."""
        sigma = self.config.observation_noise
        if sigma <= 0:
            return 1.0 if abs(observation - particle) < 0.001 else 0.0
        diff = observation - particle
        return math.exp(-0.5 * (diff / sigma) ** 2)

    def _effective_sample_size(self) -> float:
        """Compute ESS = 1 / sum(w_i^2). Measures weight degeneracy."""
        sum_sq = sum(w * w for w in self.weights)
        if sum_sq <= 0:
            return 0.0
        return 1.0 / sum_sq

    def _systematic_resample(self):
        """Systematic resampling: deterministic O(N) algorithm.

        More variance-efficient than multinomial resampling.
        """
        n = self.n_particles
        cumulative = []
        cum = 0.0
        for w in self.weights:
            cum += w
            cumulative.append(cum)

        # Systematic: one random offset, then evenly spaced
        u0 = random.random() / n
        new_particles = []
        idx = 0
        for i in range(n):
            u = u0 + i / n
            while idx < n - 1 and cumulative[idx] < u:
                idx += 1
            new_particles.append(self.particles[idx])

        self.particles = new_particles
        self.weights = [1.0 / n] * n

    def update(self, observation: float):
        """Update step: incorporate a new observation.

        1. Predict (diffuse particles)
        2. Compute likelihood weights
        3. Resample if ESS is low

        Args:
            observation: New probability estimate from the model (0-1).
        """
        observation = max(0.001, min(0.999, observation))

        # 1. Predict
        self.predict()

        # 2. Update weights with likelihood
        for i in range(self.n_particles):
            likelihood = self._gaussian_likelihood(observation, self.particles[i])
            self.weights[i] *= likelihood

        # Normalize weights
        total = sum(self.weights)
        if total > 0:
            self.weights = [w / total for w in self.weights]
        else:
            # Weight collapse: reset to uniform
            self.weights = [1.0 / self.n_particles] * self.n_particles
            _log.warning("Particle weight collapse — resetting to uniform")

        # 3. Resample if ESS drops below threshold
        ess = self._effective_sample_size()
        ess_ratio = ess / self.n_particles
        if ess_ratio < self.config.resample_threshold:
            self._systematic_resample()

        # Track update direction for trend detection
        new_prob = sum(p * w for p, w in zip(self.particles, self.weights))
        direction = "up" if new_prob > self._last_prob + 0.005 else \
                    "down" if new_prob < self._last_prob - 0.005 else "flat"
        self._recent_directions.append(direction)
        if len(self._recent_directions) > 10:
            self._recent_directions = self._recent_directions[-10:]
        self._last_prob = new_prob
        self._update_count += 1

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

    def serialize(self) -> dict:
        """Serialize filter state to a JSON-compatible dict."""
        return {
            "particles": self.particles,
            "weights": self.weights,
            "update_count": self._update_count,
            "recent_directions": self._recent_directions,
            "last_prob": self._last_prob,
            "config": asdict(self.config),
            "saved_at": time.time(),
        }

    @classmethod
    def deserialize(cls, state: dict) -> "ParticleFilter":
        """Reconstruct a filter from serialized state."""
        config_data = state.get("config", {})
        config = FilterConfig(**{k: v for k, v in config_data.items()
                                  if k in FilterConfig.__dataclass_fields__})
        pf = cls(config=config)
        particles = state.get("particles", pf.particles)
        weights = state.get("weights", pf.weights)
        if len(particles) != len(weights):
            _log.warning("Particle/weight length mismatch (%d vs %d), starting fresh",
                         len(particles), len(weights))
            return cls(config=config)
        pf.particles = particles
        pf.weights = weights
        pf._update_count = state.get("update_count", 0)
        pf._recent_directions = state.get("recent_directions", [])
        pf._last_prob = state.get("last_prob", 0.5)
        pf.n_particles = len(pf.particles)
        return pf

    def save(self, filepath: Path):
        """Save filter state to a JSON file."""
        try:
            from kalshi_auth import _atomic_write_json
            _atomic_write_json(filepath, self.serialize())
        except ImportError:
            # Fallback if kalshi_auth not available (e.g. in tests)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(json.dumps(self.serialize(), indent=2))

    @classmethod
    def load(cls, filepath: Path, max_age_seconds: int = 86400,
             config: Optional[FilterConfig] = None) -> "ParticleFilter":
        """Load filter state from a JSON file.

        Returns:
            Restored filter, or fresh filter if file missing/stale/corrupt.
        """
        try:
            if not filepath.exists():
                return cls(config=config)
            data = json.loads(filepath.read_text())
            saved_at = data.get("saved_at", 0)
            if time.time() - saved_at > max_age_seconds:
                _log.info("Particle filter state stale (%.0fh old), starting fresh",
                          (time.time() - saved_at) / 3600)
                return cls(config=config)
            pf = cls.deserialize(data)
            if config:
                pf.config = config
            return pf
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            _log.warning("Failed to load particle filter state: %s", e)
            return cls(config=config)


def ci_kelly_multiplier(estimate: FilteredEstimate,
                        narrow_threshold: float = 0.10,
                        wide_threshold: float = 0.60,
                        min_multiplier: float = 0.25,
                        min_updates: int = 5) -> float:
    """Compute a Kelly fraction multiplier based on filter CI width.

    Narrow CI (< narrow_threshold) -> full Kelly (1.0)
    Wide CI (> wide_threshold) -> minimum Kelly (min_multiplier)
    In between -> linear interpolation

    Also reduces for:
      - Few updates (< min_updates): scales from 0.5 to 1.0
      - No trend confirmation: no bonus

    Returns:
        Multiplier in [min_multiplier, 1.0] to apply to Kelly fraction.
    """
    ci_width = estimate.ci_width

    # CI-based multiplier
    if ci_width <= narrow_threshold:
        ci_mult = 1.0
    elif ci_width >= wide_threshold:
        ci_mult = min_multiplier
    else:
        # Linear interpolation
        fraction = (ci_width - narrow_threshold) / (wide_threshold - narrow_threshold)
        ci_mult = 1.0 - fraction * (1.0 - min_multiplier)

    # Update count adjustment: ramp from 0.5 to 1.0 over min_updates
    if estimate.n_updates < min_updates:
        update_mult = 0.5 + 0.5 * (estimate.n_updates / min_updates)
    else:
        update_mult = 1.0

    # Trend bonus: confirmed trend -> 10% bonus (capped at 1.0)
    trend_bonus = 1.1 if estimate.trend in ("up", "down") else 1.0

    return min(1.0, max(min_multiplier, ci_mult * update_mult * trend_bonus))


class FilterManager:
    """Manages per-market particle filters for a bot.

    Each market ticker gets its own filter instance. State is persisted
    to a single JSON file per bot in the state directory.

    Usage:
        mgr = FilterManager(bot_name="crypto", state_dir=PROJECT_DIR / "data")
        mgr.load_all()

        pf = mgr.get_filter("KXBTC-MAR-T90000")
        pf.update(model_prob)
        est = pf.estimate()

        mgr.save_all()
    """

    def __init__(self, bot_name: str, state_dir: Path,
                 default_config: Optional[FilterConfig] = None):
        self.bot_name = bot_name
        self.state_dir = state_dir
        self.default_config = default_config or FilterConfig()
        self._filters: dict = {}  # ticker -> ParticleFilter
        self._state_path = state_dir / f"pf-state-{bot_name}.json"

    def get_filter(self, ticker: str) -> ParticleFilter:
        """Get or create a filter for a market ticker."""
        if ticker not in self._filters:
            self._filters[ticker] = ParticleFilter(config=FilterConfig(
                n_particles=self.default_config.n_particles,
                process_noise=self.default_config.process_noise,
                observation_noise=self.default_config.observation_noise,
                resample_threshold=self.default_config.resample_threshold,
                trend_window=self.default_config.trend_window,
            ))
        return self._filters[ticker]

    def save_all(self):
        """Save all filter states to a single JSON file."""
        data = {
            "bot_name": self.bot_name,
            "saved_at": time.time(),
            "filters": {
                ticker: pf.serialize()
                for ticker, pf in self._filters.items()
            },
        }
        try:
            from kalshi_auth import _atomic_write_json
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._state_path, data)
        except ImportError:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(data, indent=2))

    def load_all(self, max_age_seconds: int = 86400):
        """Load all filter states from disk."""
        try:
            if not self._state_path.exists():
                return
            data = json.loads(self._state_path.read_text())
            saved_at = data.get("saved_at", 0)
            if time.time() - saved_at > max_age_seconds:
                _log.info("Filter state stale (%.0fh), starting fresh",
                          (time.time() - saved_at) / 3600)
                return
            for ticker, state in data.get("filters", {}).items():
                self._filters[ticker] = ParticleFilter.deserialize(state)
            _log.info("Loaded %d particle filters for %s",
                      len(self._filters), self.bot_name)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            _log.warning("Failed to load filter states: %s", e)

    def cleanup(self, active_tickers: set):
        """Remove filters for tickers no longer in active markets."""
        expired = [t for t in self._filters if t not in active_tickers]
        for t in expired:
            del self._filters[t]
        if expired:
            _log.info("Cleaned up %d expired filters", len(expired))
