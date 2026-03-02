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
