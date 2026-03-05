"""Tests for uncertainty_kelly -- confidence-scaled position sizing."""
import math
import pytest
from probability import uncertainty_kelly, _reset_calibration


class TestUncertaintyKelly:
    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_full_confidence_matches_quarter_kelly(self):
        """With agreement=1.0 and tight sigma, should match quarter_kelly output."""
        count, risk, details = uncertainty_kelly(
            edge=0.30, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=1.0,
            posterior_sigma=0.05,  # Very tight -> sigma_mult=1.0
        )
        assert count > 0
        assert details["confidence"] == pytest.approx(1.0, abs=0.1)

    def test_low_agreement_reduces_size(self):
        """With low scenario agreement, position should be smaller."""
        count_high, _, det_high = uncertainty_kelly(
            edge=0.30, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.95,
            posterior_sigma=0.08,
        )
        count_low, _, det_low = uncertainty_kelly(
            edge=0.30, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.30,
            posterior_sigma=0.08,
        )
        assert count_low < count_high, "Low agreement should reduce position"
        assert det_low["agreement_mult"] < det_high["agreement_mult"]

    def test_wide_sigma_reduces_size(self):
        """Wide posterior sigma = less confident = smaller position."""
        count_tight, _, _ = uncertainty_kelly(
            edge=0.30, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.90,
            posterior_sigma=0.08,
        )
        count_wide, _, _ = uncertainty_kelly(
            edge=0.30, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.90,
            posterior_sigma=0.40,
        )
        assert count_wide < count_tight, "Wide sigma should reduce position"

    def test_minimum_one_contract(self):
        """Should always return at least 1 contract if base Kelly > 0."""
        count, _, _ = uncertainty_kelly(
            edge=0.10, price_cents=50, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.20,
            posterior_sigma=0.50,
        )
        assert count >= 1

    def test_zero_edge_returns_zero(self):
        """Zero edge -> zero contracts regardless of confidence."""
        count, _, _ = uncertainty_kelly(
            edge=0.0, price_cents=5, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=1.0,
            posterior_sigma=0.05,
        )
        assert count == 0

    def test_details_include_confidence_fields(self):
        """Output details should include confidence breakdown."""
        _, _, details = uncertainty_kelly(
            edge=0.20, price_cents=10, max_cost_cents=5000,
            bankroll_cents=500000, scenario_agreement=0.80,
            posterior_sigma=0.15,
        )
        assert "confidence" in details
        assert "agreement_mult" in details
        assert "sigma_mult" in details
        assert 0 <= details["confidence"] <= 1
        assert 0 <= details["agreement_mult"] <= 1
        assert 0 <= details["sigma_mult"] <= 1
