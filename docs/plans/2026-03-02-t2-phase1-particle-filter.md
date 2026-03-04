# T2-Phase 1: Particle Filter Engine — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace stateless per-scan probability snapshots with a Bayesian belief state that carries memory across scans, producing filtered probabilities with confidence intervals. Integrate first with crypto-bot (highest value — 5-minute scans, volatile data).

**Architecture:** New module `src/kalshi/particle_filter.py` implements Sequential Monte Carlo (SMC) with systematic resampling. Each bot gets its own filter instance keyed by market ticker. Particles represent possible true probabilities; the filter predicts forward (diffusion), then updates on new observations (likelihood). Output: `FilteredEstimate(prob, ci_low, ci_high, trend, n_updates)`. State persists in `data/pf-state-{bot}.json` with staleness checks. CI-aware Kelly sizing reduces position size when the filter is uncertain.

**Tech Stack:** Pure Python 3 (no scipy/numpy). Uses `math` module for statistics. JSON state persistence via existing `_atomic_write_json`.

**Branch:** `track2/quant-infrastructure`

---

### Task 1: Create Branch

**Step 1: Create the feature branch**

```bash
git checkout -b track2/quant-infrastructure main
```

**Step 2: Commit placeholder**

No files to commit — branch is ready.

---

### Task 2: Particle Filter Core — Data Structures and Tests

**Files:**
- Create: `src/kalshi/particle_filter.py`
- Create: `tests/test_particle_filter.py`

**Step 1: Write tests for core data structures and initialization**

```python
"""Tests for particle_filter.py — Sequential Monte Carlo belief state."""

import json
import math
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from particle_filter import (
    ParticleFilter,
    FilteredEstimate,
    FilterConfig,
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
        """Narrow CI → confident."""
        est = FilteredEstimate(prob=0.6, ci_low=0.55, ci_high=0.65,
                               trend="none", n_updates=10)
        assert est.is_confident(threshold=0.15)

    def test_not_confident_wide_ci(self):
        """Wide CI → not confident."""
        est = FilteredEstimate(prob=0.6, ci_low=0.3, ci_high=0.9,
                               trend="none", n_updates=2)
        assert not est.is_confident(threshold=0.15)
```

**Step 2: Write the minimal stub**

Create `src/kalshi/particle_filter.py`:

```python
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
```

**Step 3: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All 8 tests PASS

**Step 4: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add particle filter core data structures and initialization"
```

---

### Task 3: Prediction Step (Diffusion)

**Files:**
- Modify: `src/kalshi/particle_filter.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write prediction step tests**

Add to `tests/test_particle_filter.py`:

```python
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
        """Zero process noise → particles unchanged."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=100, process_noise=0.0))
        pf.particles = [0.5] * pf.n_particles
        original = list(pf.particles)

        pf.predict()

        assert pf.particles == original

    def test_predict_spread_scales_with_noise(self):
        """Higher process noise → wider particle spread."""
        pf_low = ParticleFilter(config=FilterConfig(n_particles=2000, process_noise=0.01))
        pf_high = ParticleFilter(config=FilterConfig(n_particles=2000, process_noise=0.10))
        pf_low.particles = [0.5] * pf_low.n_particles
        pf_high.particles = [0.5] * pf_high.n_particles

        pf_low.predict()
        pf_high.predict()

        var_low = sum((p - 0.5)**2 for p in pf_low.particles) / len(pf_low.particles)
        var_high = sum((p - 0.5)**2 for p in pf_high.particles) / len(pf_high.particles)
        assert var_high > var_low
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_particle_filter.py::TestPredictionStep -v`
Expected: FAIL (predict method not defined)

**Step 3: Implement prediction step**

Add to `ParticleFilter` class in `particle_filter.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add particle filter prediction (diffusion) step"
```

---

### Task 4: Update Step (Observation Likelihood + Resampling)

**Files:**
- Modify: `src/kalshi/particle_filter.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write update step tests**

Add to `tests/test_particle_filter.py`:

```python
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

    def test_conflicting_observations_widen_ci(self):
        """Alternating high/low observations should keep CI wide."""
        pf = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.02, observation_noise=0.05))

        # Alternate between 0.3 and 0.7
        for _ in range(5):
            pf.update(0.3)
            pf.update(0.7)

        est = pf.estimate()
        # CI should be wider than with consistent observations
        assert est.ci_width > 0.10

    def test_update_with_high_observation_noise(self):
        """High observation noise → slower convergence."""
        pf_tight = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.01, observation_noise=0.02))
        pf_loose = ParticleFilter(config=FilterConfig(
            n_particles=1000, process_noise=0.01, observation_noise=0.20))

        for _ in range(5):
            pf_tight.update(0.8)
            pf_loose.update(0.8)

        est_tight = pf_tight.estimate()
        est_loose = pf_loose.estimate()
        # Tight obs noise → closer to 0.8 and narrower CI
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
        """ESS with one dominant weight → near 1."""
        pf = ParticleFilter(config=FilterConfig(n_particles=100))
        pf.weights = [0.0001] * 99 + [1.0 - 0.0099]
        ess = pf._effective_sample_size()
        assert ess < 5
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_particle_filter.py::TestUpdateStep -v`
Expected: FAIL (update method not defined)

**Step 3: Implement update step with systematic resampling**

Add to `ParticleFilter` class:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add particle filter update step with systematic resampling"
```

---

### Task 5: State Persistence

**Files:**
- Modify: `src/kalshi/particle_filter.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write persistence tests**

Add to `tests/test_particle_filter.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_particle_filter.py::TestStatePersistence -v`
Expected: FAIL (serialize/deserialize not defined)

**Step 3: Implement persistence**

Add to `ParticleFilter` class:

```python
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
        pf.particles = state.get("particles", pf.particles)
        pf.weights = state.get("weights", pf.weights)
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

        Args:
            filepath: Path to the state file.
            max_age_seconds: Maximum age before state is considered stale.
                Default 24 hours.
            config: Optional config override (if state has different config).

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
                # Override config but keep particles/weights
                pf.config = config
            return pf
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            _log.warning("Failed to load particle filter state: %s", e)
            return cls(config=config)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add particle filter state persistence with staleness check"
```

---

### Task 6: CI-Aware Kelly Sizing

**Files:**
- Modify: `src/kalshi/particle_filter.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write CI-aware sizing tests**

Add to `tests/test_particle_filter.py`:

```python
class TestCIAwareKelly:
    """Test CI-aware position sizing adjustments."""

    def test_narrow_ci_no_reduction(self):
        """Narrow CI → full Kelly (multiplier = 1.0)."""
        est = FilteredEstimate(prob=0.7, ci_low=0.65, ci_high=0.75,
                               trend="none", n_updates=20)
        mult = ci_kelly_multiplier(est)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_wide_ci_reduces_kelly(self):
        """Wide CI → reduced Kelly."""
        est = FilteredEstimate(prob=0.7, ci_low=0.4, ci_high=1.0,
                               trend="none", n_updates=5)
        mult = ci_kelly_multiplier(est)
        assert mult < 0.8

    def test_very_wide_ci_minimum_multiplier(self):
        """Very wide CI → minimum multiplier (0.25)."""
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
        """Fewer updates → less confident → lower multiplier."""
        est_many = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                     trend="none", n_updates=20)
        est_few = FilteredEstimate(prob=0.7, ci_low=0.55, ci_high=0.85,
                                    trend="none", n_updates=2)
        mult_many = ci_kelly_multiplier(est_many)
        mult_few = ci_kelly_multiplier(est_few)
        assert mult_many >= mult_few
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_particle_filter.py::TestCIAwareKelly -v`
Expected: FAIL (ci_kelly_multiplier not defined)

**Step 3: Implement CI-aware Kelly**

Add to `particle_filter.py` (module-level function):

```python
def ci_kelly_multiplier(estimate: FilteredEstimate,
                        narrow_threshold: float = 0.10,
                        wide_threshold: float = 0.60,
                        min_multiplier: float = 0.25,
                        min_updates: int = 5) -> float:
    """Compute a Kelly fraction multiplier based on filter CI width.

    Narrow CI (< narrow_threshold) → full Kelly (1.0)
    Wide CI (> wide_threshold) → minimum Kelly (min_multiplier)
    In between → linear interpolation

    Also reduces for:
      - Few updates (< min_updates): scales from 0.5 to 1.0
      - No trend confirmation: no bonus

    Args:
        estimate: FilteredEstimate from the particle filter.
        narrow_threshold: CI width below this → full sizing.
        wide_threshold: CI width above this → minimum sizing.
        min_multiplier: Floor for the Kelly multiplier.
        min_updates: Number of updates before full confidence.

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

    # Trend bonus: confirmed trend → 10% bonus (capped at 1.0)
    trend_bonus = 1.1 if estimate.trend in ("up", "down") else 1.0

    return min(1.0, max(min_multiplier, ci_mult * update_mult * trend_bonus))
```

Add to the imports in `tests/test_particle_filter.py`:

```python
from particle_filter import ci_kelly_multiplier
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add CI-aware Kelly sizing multiplier"
```

---

### Task 7: Per-Market Filter Manager

**Files:**
- Modify: `src/kalshi/particle_filter.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write filter manager tests**

Add to `tests/test_particle_filter.py`:

```python
from particle_filter import FilterManager


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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_particle_filter.py::TestFilterManager -v`
Expected: FAIL (FilterManager not defined)

**Step 3: Implement FilterManager**

Add to `particle_filter.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add per-market FilterManager with persistence"
```

---

### Task 8: Crypto Bot Integration

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Modify: `tests/test_particle_filter.py`

**Step 1: Write integration test**

Add to `tests/test_particle_filter.py`:

```python
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
        # Few updates → wide CI → lower multiplier
        est_uncertain = FilteredEstimate(prob=0.7, ci_low=0.4, ci_high=1.0,
                                          trend="none", n_updates=2)
        # Many updates → narrow CI → higher multiplier
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
```

Add at top of test file:

```python
import random
```

**Step 2: Run test to verify it passes**

Run: `pytest tests/test_particle_filter.py::TestCryptoBotIntegration -v`
Expected: PASS (uses existing classes)

**Step 3: Integrate particle filter into crypto-bot.py**

In `crypto-bot.py`, add the filter integration. Key changes:

1. Add imports at top:
```python
from particle_filter import FilterManager, FilterConfig, ci_kelly_multiplier
```

2. Initialize FilterManager after client/allocator setup:
```python
# Particle filter for Bayesian belief tracking
pf_config = FilterConfig(
    n_particles=200,
    process_noise=crypto_config.get("pfProcessNoise", 0.02),
    observation_noise=crypto_config.get("pfObservationNoise", 0.05),
)
filter_mgr = FilterManager(bot_name="crypto", state_dir=PROJECT_DIR / "data",
                            default_config=pf_config)
filter_mgr.load_all()
```

3. In `scan_and_trade()`, after computing `prob` for each market, update the filter and use the filtered probability:
```python
        # Particle filter: update belief state and use filtered prob
        pf = filter_mgr.get_filter(ticker)
        pf.update(prob)
        filtered_est = pf.estimate()
        raw_prob = prob
        prob = filtered_est.prob  # use filtered probability for edge computation
```

4. Apply CI-aware Kelly multiplier to the position sizing:
```python
        # CI-aware sizing: reduce position when filter is uncertain
        kelly_mult = ci_kelly_multiplier(filtered_est)
        count = max(0, int(count * kelly_mult))
```

5. At the end of `scan_and_trade()`, save filter state:
```python
    # Save particle filter state
    filter_mgr.save_all()
```

6. Add filtered estimate data to trade records:
```python
                                            pf_prob=round(filtered_est.prob, 4),
                                            pf_ci_low=round(filtered_est.ci_low, 4),
                                            pf_ci_high=round(filtered_est.ci_high, 4),
                                            pf_trend=filtered_est.trend,
                                            pf_updates=filtered_est.n_updates,
                                            pf_kelly_mult=round(kelly_mult, 4),
```

**Step 4: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/kalshi/crypto-bot.py src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate particle filter into crypto bot for Bayesian belief tracking"
```

---

### Task 9: Config and Documentation

**Files:**
- Modify: `config/bots-config.json`
- Modify: `CLAUDE.md`

**Step 1: Add particle filter config to bots-config.json**

Add to the `"crypto"` section:

```json
"pfProcessNoise": 0.02,
"pfObservationNoise": 0.05,
"pfParticles": 200,
"pfStalenessHours": 24
```

**Step 2: Update CLAUDE.md**

Add to Shared Modules section:

```
**`src/kalshi/particle_filter.py`** — Sequential Monte Carlo (particle filter) for Bayesian belief tracking (~350 lines). Maintains weighted particle distributions per market, carries memory across scans. `FilterManager` manages per-ticker filters with JSON state persistence. `ci_kelly_multiplier()` reduces Kelly fraction when filter CI is wide. Integrates with crypto-bot (5-min scans), extensible to weather/economics.
```

**Step 3: Commit**

```bash
git add config/bots-config.json CLAUDE.md
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "docs: add particle filter config and documentation"
```

---

### Task 10: Final Verification

**Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 2: Verify particle_filter imports cleanly**

Run: `cd src/kalshi && python3 -c "from particle_filter import ParticleFilter, FilterManager, ci_kelly_multiplier; print('OK')"`

**Step 3: Verify crypto bot can start with `--once` in demo mode**

Run: `KALSHI_MODE=demo python3 src/kalshi/crypto-bot.py --once 2>&1 | head -30`
Expected: Bot starts, particle filter loads/creates state, scan completes.

**Step 4: Verify state file is created**

Run: `ls -la data/pf-state-crypto.json`
Expected: File exists with filter state.

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Create branch | — | — |
| 2 | Core data structures + tests | particle_filter.py, test_particle_filter.py | 8 |
| 3 | Prediction step (diffusion) | particle_filter.py, test_particle_filter.py | +4 |
| 4 | Update step + resampling | particle_filter.py, test_particle_filter.py | +10 |
| 5 | State persistence | particle_filter.py, test_particle_filter.py | +6 |
| 6 | CI-aware Kelly sizing | particle_filter.py, test_particle_filter.py | +5 |
| 7 | Per-market FilterManager | particle_filter.py, test_particle_filter.py | +5 |
| 8 | Crypto bot integration | crypto-bot.py, test_particle_filter.py | +3 |
| 9 | Config + docs | bots-config.json, CLAUDE.md | — |
| 10 | Final verification | — | Full suite |

**Total new tests:** ~41
**New files:** `src/kalshi/particle_filter.py`, `tests/test_particle_filter.py`
**Modified files:** `crypto-bot.py`, `bots-config.json`, `CLAUDE.md`
