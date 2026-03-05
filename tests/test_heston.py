"""Tests for Heston stochastic volatility model."""
import math
import pytest

# probability.py is on sys.path via conftest.py
from probability import crypto_price_probability_heston, _norm_cdf


class TestHestonBasic:
    """Basic Heston model behavior."""

    def test_atm_returns_near_50_pct(self):
        """ATM option with no drift should be ~50%."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=1440,  # 1 day
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.45 < prob < 0.55

    def test_deep_itm_returns_high_prob(self):
        """BTC at 85k, threshold 70k -> high probability above."""
        prob = crypto_price_probability_heston(
            current_price=85000, threshold=70000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob > 0.90

    def test_deep_otm_returns_low_prob(self):
        """BTC at 80k, threshold 95k -> low probability above."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=95000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob < 0.10

    def test_below_direction_complements(self):
        """P(below) = 1 - P(above)."""
        above = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        below = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="below",
            time_horizon_minutes=60,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert abs(above + below - 1.0) < 0.01

    def test_short_horizon_tighter_than_long(self):
        """5-min horizon should give more extreme probs than 24h for same OTM."""
        short = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=5,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        long = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # Short horizon: price less likely to reach 82k in 5 min
        assert short < long

    def test_higher_vol_of_vol_fatter_tails(self):
        """Higher xi (vol-of-vol) should produce fatter tails = higher OTM prob."""
        # Use rho=0 to isolate the vol-of-vol effect (no leverage skew)
        low_xi = crypto_price_probability_heston(
            current_price=80000, threshold=85000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.1, rho=0.0,
        )
        high_xi = crypto_price_probability_heston(
            current_price=80000, threshold=85000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.8, rho=0.0,
        )
        assert high_xi > low_xi

    def test_negative_rho_creates_skew(self):
        """Negative rho produces different skew than positive rho.

        The key property is that rho != 0 breaks symmetry — the distribution
        is meaningfully different for negative vs positive rho.
        """
        neg_rho = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=10080,  # 7 days for rho effect to compound
            v0=0.25, kappa=2.0, theta=0.25, xi=0.5, rho=-0.9,
        )
        pos_rho = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=10080,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.5, rho=0.9,
        )
        # Rho should create a meaningful difference in probabilities
        assert abs(neg_rho - pos_rho) > 0.01

    def test_clamps_to_valid_range(self):
        """Result always in [0.001, 0.999]."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=200000, direction="above",
            time_horizon_minutes=5,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.001 <= prob <= 0.999

    def test_vol_mean_reversion(self):
        """High v0 with low theta should see vol decrease -> narrower distribution."""
        # High starting vol, low long-run vol
        high_v0 = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=1.0, kappa=5.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # Low starting vol matching long-run
        low_v0 = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=5.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # High v0 -> wider distribution -> higher OTM prob
        assert high_v0 > low_v0


class TestHestonEdgeCases:
    """Edge cases and numerical stability."""

    def test_zero_time_horizon(self):
        """T=0 should return 1.0 if price > threshold, else 0."""
        prob = crypto_price_probability_heston(
            current_price=85000, threshold=80000, direction="above",
            time_horizon_minutes=0,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob > 0.99

    def test_very_small_time_horizon(self):
        """T=0.1 min should still produce valid result."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=0.1,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.001 <= prob <= 0.999

    def test_extreme_params_no_crash(self):
        """Extreme but valid params should not crash."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=60,
            v0=2.0, kappa=10.0, theta=2.0, xi=2.0, rho=-0.99,
        )
        assert 0.001 <= prob <= 0.999
