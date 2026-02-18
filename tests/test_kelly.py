"""Tests for the half-Kelly position-sizing functions in probability.py.

Tests both buy-side half_kelly() and sell-side half_kelly_sell().
conftest.py adds src/kalshi/ to sys.path so direct import works.
"""

import math
import pytest

from probability import half_kelly, half_kelly_sell


# ---------------------------------------------------------------------------
# Buy-side half_kelly tests (preserved from Phase 1)
# ---------------------------------------------------------------------------

class TestHalfKelly:
    """Unit tests for ``half_kelly(edge, price_cents, max_cost_cents, bankroll_cents)``."""

    def test_zero_edge_returns_zero(self):
        contracts, risk = half_kelly(0, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_negative_edge_returns_zero(self):
        contracts, risk = half_kelly(-0.10, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_positive_edge_returns_positive(self):
        contracts, risk = half_kelly(0.20, 30, 500)
        assert contracts > 0
        assert risk > 0

    def test_respects_max_cost(self):
        """Contracts * price should not exceed max_cost_cents."""
        contracts, risk = half_kelly(0.30, 20, 200)
        assert risk <= 200

    def test_returns_tuple(self):
        result = half_kelly(0.15, 40, 500)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_price_at_boundary(self):
        """Price at 100 should return zero."""
        contracts, risk = half_kelly(0.20, 100, 500)
        assert contracts == 0

    def test_with_bankroll(self):
        """Bankroll constraint should limit contracts."""
        c_no_bank, _ = half_kelly(0.20, 10, 10000)
        c_small_bank, _ = half_kelly(0.20, 10, 10000, bankroll_cents=500)
        assert c_small_bank <= c_no_bank

    def test_result_is_integer(self):
        contracts, _ = half_kelly(0.20, 5, 5000)
        assert isinstance(contracts, int)

    def test_larger_bankroll_more_contracts(self):
        c1, _ = half_kelly(0.20, 5, 50000)
        c2, _ = half_kelly(0.20, 5, 50000, bankroll_cents=10000)
        c3, _ = half_kelly(0.20, 5, 50000, bankroll_cents=20000)
        assert c3 >= c2

    def test_higher_edge_more_contracts(self):
        c_low, _ = half_kelly(0.10, 5, 50000)
        c_high, _ = half_kelly(0.30, 5, 50000)
        assert c_high >= c_low

    def test_risk_equals_contracts_times_price(self):
        contracts, risk = half_kelly(0.20, 30, 1000)
        assert risk == contracts * 30


# ---------------------------------------------------------------------------
# Sell-side half_kelly_sell tests
# ---------------------------------------------------------------------------

class TestHalfKellySell:
    """Unit tests for ``half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents)``."""

    def test_returns_zero_when_edge_is_zero(self):
        contracts, risk = half_kelly_sell(0, 5, 500)
        assert contracts == 0
        assert risk == 0

    def test_returns_zero_when_edge_is_negative(self):
        contracts, risk = half_kelly_sell(-0.10, 5, 500)
        assert contracts == 0
        assert risk == 0

    def test_returns_zero_when_price_cents_is_zero(self):
        contracts, risk = half_kelly_sell(0.20, 0, 500)
        assert contracts == 0

    def test_returns_zero_when_price_cents_at_100(self):
        contracts, risk = half_kelly_sell(0.20, 100, 500)
        assert contracts == 0

    def test_returns_zero_when_price_cents_above_100(self):
        contracts, risk = half_kelly_sell(0.20, 150, 500)
        assert contracts == 0

    def test_positive_contracts_for_valid_edge(self):
        """A healthy edge on a 5-cent contract should produce positive contracts."""
        contracts, risk = half_kelly_sell(0.20, 5, 500, bankroll_cents=50000)
        assert contracts > 0

    def test_returns_tuple(self):
        result = half_kelly_sell(0.15, 10, 500, bankroll_cents=50000)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_risk_equals_contracts_times_no_price(self):
        """Risk = contracts * (100 - sell_price)."""
        contracts, risk = half_kelly_sell(0.20, 10, 5000, bankroll_cents=50000)
        if contracts > 0:
            assert risk == contracts * (100 - 10)

    def test_result_is_integer(self):
        contracts, _ = half_kelly_sell(0.20, 5, 5000, bankroll_cents=50000)
        assert isinstance(contracts, int)

    def test_respects_max_cost_cap(self):
        """Risk (contracts * (100 - sell_price)) should not exceed max_cost_cents."""
        contracts, risk = half_kelly_sell(0.50, 5, 500, bankroll_cents=10_000_000)
        assert risk <= 500

    def test_bankroll_scaling(self):
        """Larger bankroll should produce at least as many contracts."""
        c1, _ = half_kelly_sell(0.20, 5, 50000, bankroll_cents=10000)
        c2, _ = half_kelly_sell(0.20, 5, 50000, bankroll_cents=20000)
        assert c2 >= c1

    def test_edge_scaling(self):
        """Higher edge should produce at least as many contracts."""
        c1, _ = half_kelly_sell(0.10, 5, 50000, bankroll_cents=50000)
        c2, _ = half_kelly_sell(0.30, 5, 50000, bankroll_cents=50000)
        assert c2 >= c1

    def test_returns_zero_for_tiny_edge(self):
        """An edge so small that half-Kelly rounds to 0."""
        contracts, risk = half_kelly_sell(0.001, 50, 1000, bankroll_cents=1000)
        assert contracts == 0

    def test_symmetry_at_50c(self):
        """At 50c, sell-side and buy-side should produce same sizing.

        At 50c: implied=0.50, win/loss amounts are symmetric (50/50).
        Buy side: edge=0.10 means our_prob=0.60, win=50, lose=50
        Sell side: edge=0.10 means p_true=0.40, win_prob=0.60, win=50, lose=50
        The Kelly fractions should be identical.
        """
        buy_contracts, buy_risk = half_kelly(0.10, 50, 5000, bankroll_cents=50000)
        sell_contracts, sell_risk = half_kelly_sell(0.10, 50, 5000, bankroll_cents=50000)
        assert buy_contracts == sell_contracts
