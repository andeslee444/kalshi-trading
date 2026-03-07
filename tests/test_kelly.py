"""Tests for the half-Kelly position-sizing functions in probability.py.

Tests both buy-side half_kelly() and sell-side half_kelly_sell().
conftest.py adds src/kalshi/ to sys.path so direct import works.
"""

import math
import pytest

from probability import half_kelly, half_kelly_sell, quarter_kelly, quarter_kelly_sell, apply_kelly_multipliers


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

    def test_fee_cents_reduces_contracts(self):
        """Fix A: fee_cents > 0 should reduce contracts vs fee_cents=0.

        When fee_cents is passed, the payout is reduced from 100 to (100-fee),
        which lowers the Kelly fraction and thus the number of contracts.
        Use a large enough max_cost so Kelly (not cost cap) is the binding constraint.
        """
        c_no_fee, _ = half_kelly(0.15, 50, 50000, bankroll_cents=50000, fee_cents=0)
        c_with_fee, _ = half_kelly(0.15, 50, 50000, bankroll_cents=50000, fee_cents=1.75)
        assert c_with_fee < c_no_fee

    def test_fee_cents_reduces_payout_not_edge(self):
        """Verify fee reduces win amount (100-fee-price), not edge probability."""
        # With fee=0: win = 100 - 50 = 50c
        # With fee=5: win = 100 - 5 - 50 = 45c (10% less payout)
        c0, _ = half_kelly(0.10, 50, 10000, bankroll_cents=100000, fee_cents=0)
        c5, _ = half_kelly(0.10, 50, 10000, bankroll_cents=100000, fee_cents=5)
        # Fee should meaningfully reduce sizing
        assert c5 < c0
        # But shouldn't zero it out for 10% edge
        assert c5 > 0


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

    def test_fee_cents_reduces_sell_contracts(self):
        """Fix A: fee_cents > 0 should reduce sell-side contracts too."""
        c_no_fee, _ = half_kelly_sell(0.20, 5, 5000, bankroll_cents=50000, fee_cents=0)
        c_with_fee, _ = half_kelly_sell(0.20, 5, 5000, bankroll_cents=50000, fee_cents=0.33)
        assert c_with_fee <= c_no_fee

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


# ---------------------------------------------------------------------------
# Sell-side quarter_kelly_sell tests (Phase 2, Plan 01)
# ---------------------------------------------------------------------------

class TestQuarterKellySell:
    """Unit tests for ``quarter_kelly_sell`` — quarter-Kelly for sell-side trades."""

    def test_zero_edge_returns_zero(self):
        contracts, risk = quarter_kelly_sell(0, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_negative_edge_returns_zero(self):
        contracts, risk = quarter_kelly_sell(-0.10, 50, 500)
        assert contracts == 0
        assert risk == 0

    def test_positive_edge_returns_positive(self):
        contracts, risk = quarter_kelly_sell(0.20, 90, 500, bankroll_cents=50000)
        assert contracts > 0

    def test_halves_half_kelly_sell(self):
        """quarter_kelly_sell should return roughly half of half_kelly_sell contracts."""
        hk_contracts, _ = half_kelly_sell(0.20, 90, 5000, bankroll_cents=50000)
        qk_contracts, _ = quarter_kelly_sell(0.20, 90, 5000, bankroll_cents=50000)
        # round() may give +1 vs floor division for odd half_kelly results,
        # but exposure cap brings it back down; allow ±1 tolerance
        assert qk_contracts <= (hk_contracts + 1) // 2

    def test_respects_max_exposure(self):
        """With max_exposure_cents, total risk should not exceed it."""
        contracts, risk = quarter_kelly_sell(
            0.30, 90, 50000, bankroll_cents=500000, max_exposure_cents=100
        )
        # risk per contract = 100 - 90 = 10
        assert contracts * (100 - 90) <= 100

    def test_return_details(self):
        """With return_details=True, returns 3-tuple with dict."""
        result = quarter_kelly_sell(0.20, 90, 5000, bankroll_cents=50000, return_details=True)
        assert len(result) == 3
        contracts, risk, details = result
        assert "kelly_fraction" in details
        assert "bankroll_used" in details

    def test_fee_reduces_contracts(self):
        """fee_cents > 0 should reduce contracts or keep them the same."""
        c_no_fee, _ = quarter_kelly_sell(0.15, 90, 5000, bankroll_cents=10000, fee_cents=0)
        c_with_fee, _ = quarter_kelly_sell(0.15, 90, 5000, bankroll_cents=10000, fee_cents=2)
        assert c_with_fee <= c_no_fee

    def test_sell_price_boundary(self):
        """At sell_price=100, should return (0, 0) — boundary case."""
        contracts, risk = quarter_kelly_sell(0.10, 100, 500)
        assert contracts == 0
        assert risk == 0


# ---------------------------------------------------------------------------
# Exact-value Kelly formula pinning tests
# ---------------------------------------------------------------------------

class TestKellyExactValues:
    """Pin concrete Kelly outputs against manually verified formula calculations."""

    def test_half_kelly_exact(self):
        """half_kelly(0.20, 30, 500, bankroll=10000).

        our_prob = 0.50, b = 70/30, f* = (b*p-q)/b = 0.2857, f*/2 = 0.1429
        max_kelly = int(0.1429 * 10000 / 30) = 47
        max_cap = 500 // 30 = 16 (binding)
        => (16, 480)
        """
        count, risk = half_kelly(0.20, 30, 500, bankroll_cents=10000)
        assert count == 16
        assert risk == 480

    def test_half_kelly_sell_exact(self):
        """half_kelly_sell(0.20, 10, 5000, bankroll=50000).

        p_true = 10/100 - 0.20 = -0.10, clamped to 0.001
        win_prob = 1 - 0.001 = 0.999, win_amount = 10 - 0 = 10, loss = 90
        b = 10/90, f* = (b*0.999 - 0.001)/b, half_f
        risk_per = 90, contracts from Kelly and cap
        => (55, 4950)
        """
        count, risk = half_kelly_sell(0.20, 10, 5000, bankroll_cents=50000)
        assert count == 55
        assert risk == 4950

    def test_quarter_kelly_exact(self):
        """quarter_kelly(0.20, 30, 500, bankroll=10000).

        Halves half_kelly result: half_kelly gives 16, quarter = 16 // 2 = 8
        => (8, 240)
        """
        count, risk = quarter_kelly(0.20, 30, 500, bankroll_cents=10000)
        assert count == 8
        assert risk == 240

    def test_quarter_kelly_sell_exact(self):
        """quarter_kelly_sell(0.20, 10, 5000, bankroll=50000).

        Halves half_kelly_sell result: half gives 55, quarter = 55 // 2 = 27
        risk_per = 90, risk = 27 * 90 = 2430
        => (27, 2430)
        """
        count, risk = quarter_kelly_sell(0.20, 10, 5000, bankroll_cents=50000)
        assert count == 27
        assert risk == 2430

    def test_half_kelly_sell_at_99_returns_zero(self):
        """sell_price=99 means risk_per=1 which is <= 1, returns zero."""
        count, risk = half_kelly_sell(0.10, 99, 500, bankroll_cents=10000)
        assert count == 0
        assert risk == 0

    def test_half_kelly_sell_at_50_reasonable(self):
        """sell_price=50 with 10% edge should produce reasonable sizing."""
        count, risk = half_kelly_sell(0.10, 50, 500, bankroll_cents=10000)
        assert count == 10
        assert risk == 500


# ---------------------------------------------------------------------------
# Quarter-Kelly integer truncation regression tests
# ---------------------------------------------------------------------------

class TestQuarterKellyPreservesSingleContract:
    """quarter_kelly should not silently zero out 1-contract positions via floor division."""

    def test_quarter_kelly_preserves_single_contract(self):
        """quarter_kelly should return 1 contract when half_kelly returns 1, not zero."""
        # Params calibrated to produce hk=1: edge=0.08, price=80, bankroll=500
        hk_contracts, _, _ = half_kelly(0.08, 80, 500, bankroll_cents=500, return_details=True)
        assert hk_contracts >= 1, f"half_kelly should return >= 1 contract, got {hk_contracts}"
        assert hk_contracts <= 2, f"half_kelly should return <= 2 contracts, got {hk_contracts}"
        qk_contracts, _, _ = quarter_kelly(0.08, 80, 500, bankroll_cents=500, return_details=True)
        assert qk_contracts >= 1, (
            f"quarter_kelly zeroed out: half_kelly={hk_contracts}, quarter_kelly={qk_contracts}"
        )

    def test_quarter_kelly_sell_preserves_single_contract(self):
        """quarter_kelly_sell should return 1 contract when half_kelly_sell returns 1, not zero."""
        # Params calibrated to produce hk=1: edge=0.05, price=10, bankroll=500
        hk_contracts, _, _ = half_kelly_sell(0.05, 10, 500, bankroll_cents=500, return_details=True)
        assert hk_contracts >= 1, f"half_kelly_sell should return >= 1 contract, got {hk_contracts}"
        assert hk_contracts <= 2, f"half_kelly_sell should return <= 2 contracts, got {hk_contracts}"
        qk_contracts, _, _ = quarter_kelly_sell(0.05, 10, 500, bankroll_cents=500, return_details=True)
        assert qk_contracts >= 1, (
            f"quarter_kelly_sell zeroed out: half_kelly_sell={hk_contracts}, quarter_kelly_sell={qk_contracts}"
        )

    def test_quarter_kelly_one_becomes_one_not_zero(self):
        """Directly verify: if half_kelly gives 1, round(1/2)=0 but max(1,...) saves it."""
        # Params calibrated to produce hk=1: edge=0.05, price=80, bankroll=800
        hk, _, details = half_kelly(0.05, 80, 500, bankroll_cents=800, return_details=True)
        assert hk == 1, f"Expected half_kelly to return 1 contract, got {hk}"
        qk, _, _ = quarter_kelly(0.05, 80, 500, bankroll_cents=800, return_details=True)
        assert qk == 1, f"Expected 1 contract, got {qk}"


# ---------------------------------------------------------------------------
# apply_kelly_multipliers tests
# ---------------------------------------------------------------------------

class TestApplyKellyMultipliers:
    """Tests for apply_kelly_multipliers with floor protection."""

    def test_single_multiplier(self):
        result = apply_kelly_multipliers(100, [0.5])
        assert result == 50

    def test_multiple_multipliers(self):
        result = apply_kelly_multipliers(100, [0.5, 0.5])
        assert result == 25

    def test_floor_prevents_crushing(self):
        """4 multipliers that would crush to ~43% should be respected (above 25% floor)."""
        result = apply_kelly_multipliers(100, [0.7, 0.8, 0.9, 0.85])
        expected_product = 100 * 0.7 * 0.8 * 0.9 * 0.85  # ~42.84
        assert result == pytest.approx(expected_product, abs=0.01)

    def test_floor_kicks_in_when_crushed_below(self):
        """Extreme multipliers that would crush below floor should be clamped."""
        result = apply_kelly_multipliers(100, [0.1, 0.1, 0.1])
        # 100 * 0.001 = 0.1, but floor = 25
        assert result == 25.0

    def test_custom_floor(self):
        result = apply_kelly_multipliers(100, [0.1, 0.1], floor_pct=0.50)
        # 100 * 0.01 = 1, but floor = 50
        assert result == 50.0

    def test_zero_base_returns_zero(self):
        result = apply_kelly_multipliers(0, [0.5, 0.5])
        assert result == 0

    def test_empty_multipliers(self):
        result = apply_kelly_multipliers(100, [])
        assert result == 100

    def test_all_ones_no_change(self):
        result = apply_kelly_multipliers(100, [1.0, 1.0, 1.0])
        assert result == 100

    def test_with_real_kelly_output(self):
        """Integration: apply multipliers to actual half_kelly output."""
        contracts, _ = half_kelly(0.20, 30, 500, 50000)
        assert contracts > 0
        reduced = apply_kelly_multipliers(contracts, [0.7, 0.8, 0.9, 0.85])
        assert reduced >= contracts * 0.25  # floor
        assert reduced <= contracts  # can't increase
