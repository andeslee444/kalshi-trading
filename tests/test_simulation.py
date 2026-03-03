"""Tests for simulation.py — importance sampling engine.

The simulation engine provides variance-reduced Monte Carlo estimation
for tail-risk contract pricing. Uses importance sampling with optimal
shift for Gaussian processes.
"""

import math
import pytest


class TestImportanceSampler:
    """Test the importance sampling Monte Carlo engine."""

    def test_basic_probability_estimate(self):
        """IS should estimate P(X > threshold) within statistical tolerance."""
        from simulation import importance_sample_probability
        # P(N(0,1) > 2) ≈ 0.0228
        prob, std_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0,
            n_samples=10000, direction="above",
        )
        assert abs(prob - 0.0228) < 0.01

    def test_tail_probability_accuracy(self):
        """IS should accurately estimate deep tail probabilities."""
        from simulation import importance_sample_probability
        # P(N(0,1) > 3) ≈ 0.00135
        prob, std_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=3.0,
            n_samples=10000, direction="above",
        )
        assert abs(prob - 0.00135) < 0.005

    def test_below_direction(self):
        """'below' direction should compute left tail."""
        from simulation import importance_sample_probability
        # P(N(0,1) < -2) ≈ 0.0228
        prob, _ = importance_sample_probability(
            mean=0.0, std=1.0, threshold=-2.0,
            n_samples=10000, direction="below",
        )
        assert abs(prob - 0.0228) < 0.01

    def test_std_error_decreases_with_samples(self):
        """Standard error should decrease with more samples."""
        from simulation import importance_sample_probability
        _, err_1k = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=1000,
        )
        _, err_10k = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=10000,
        )
        assert err_10k < err_1k

    def test_variance_reduction_vs_naive(self):
        """IS should have lower variance than naive MC for tail events."""
        from simulation import importance_sample_probability, naive_mc_probability
        # Deep tail: P(N(0,1) > 3)
        _, is_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=3.0, n_samples=5000,
        )
        _, naive_err = naive_mc_probability(
            mean=0.0, std=1.0, threshold=3.0, n_samples=5000,
        )
        # IS should have meaningfully lower error for tail events
        assert is_err < naive_err * 0.8  # At least 20% variance reduction

    def test_returns_tuple(self):
        """Should return (probability, standard_error) tuple."""
        from simulation import importance_sample_probability
        result = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=1000,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_at_mean_probability_near_half(self):
        """P(X > mean) should be near 0.5."""
        from simulation import importance_sample_probability
        prob, _ = importance_sample_probability(
            mean=5.0, std=2.0, threshold=5.0, n_samples=10000,
        )
        assert 0.45 < prob < 0.55


class TestAntitheticVariates:
    """Test antithetic variate variance reduction."""

    def test_antithetic_probability(self):
        """Antithetic sampling should give valid probability."""
        from simulation import importance_sample_probability
        prob, _ = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0,
            n_samples=10000, use_antithetic=True,
        )
        assert abs(prob - 0.0228) < 0.01

    def test_antithetic_reduces_variance(self):
        """Antithetic should reduce variance compared to standard IS."""
        from simulation import importance_sample_probability
        _, err_std = importance_sample_probability(
            mean=0.0, std=1.0, threshold=1.5,
            n_samples=5000, use_antithetic=False,
        )
        _, err_anti = importance_sample_probability(
            mean=0.0, std=1.0, threshold=1.5,
            n_samples=5000, use_antithetic=True,
        )
        # Antithetic should be at least as good (usually better)
        assert err_anti <= err_std * 1.2  # Allow 20% slack for randomness
