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
