"""Tests for dynamic scenario weight computation."""
import pytest
from scenario_engine import compute_scenario_weights, DEFAULT_WEIGHTS


class TestScenarioWeightDynamics:

    def test_default_weights_sum_to_one(self):
        """Default weights should sum to 1.0."""
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-10

    def test_no_data_returns_defaults(self):
        """With no market or FRED data, return default weights."""
        weights = compute_scenario_weights({}, {})
        for k in DEFAULT_WEIGHTS:
            assert abs(weights[k] - DEFAULT_WEIGHTS[k]) < 0.01

    def test_high_oil_increases_supply_shock(self):
        """Oil 20%+ above 90d MA should increase supply_shock weight."""
        weights = compute_scenario_weights({}, {
            "crude_oil": 100.0,
            "crude_oil_90d_ma": 75.0,  # 33% above MA
        })
        assert weights["supply_shock"] > DEFAULT_WEIGHTS["supply_shock"]

    def test_oil_same_as_ma_no_effect(self):
        """Oil at MA should not change supply_shock (deviation = 0)."""
        weights = compute_scenario_weights({}, {
            "crude_oil": 75.0,
            "crude_oil_90d_ma": 75.0,
        })
        assert abs(weights["supply_shock"] - DEFAULT_WEIGHTS["supply_shock"]) < 0.02

    def test_inverted_yield_curve_increases_recession(self):
        """Deeply inverted yield curve should increase recession weight."""
        weights = compute_scenario_weights({}, {"T10Y2Y": -1.0})
        assert weights["recession"] > DEFAULT_WEIGHTS["recession"]

    def test_flat_yield_curve_no_recession_boost(self):
        """Positive yield curve should not boost recession."""
        weights = compute_scenario_weights({}, {"T10Y2Y": 1.5})
        # Should be at or near default
        assert abs(weights["recession"] - DEFAULT_WEIGHTS["recession"]) < 0.02

    def test_low_gdp_with_rising_tips_boosts_stagflation(self):
        """Low GDPNow + rising long-term TIPS = stagflation signal."""
        weights = compute_scenario_weights({}, {
            "gdpnow": 0.3,
            "tips_5y_minus_10y": 0.5,
        })
        assert weights["stagflation"] > DEFAULT_WEIGHTS["stagflation"]

    def test_weights_always_sum_to_one(self):
        """Weights must always sum to 1.0 regardless of inputs."""
        test_cases = [
            ({}, {}),
            ({}, {"crude_oil": 120, "crude_oil_90d_ma": 80}),
            ({}, {"T10Y2Y": -2.0}),
            ({"tariff_escalation_prob": 0.9}, {}),
            ({"tariff_escalation_prob": 0.9}, {"T10Y2Y": -1.5, "crude_oil": 150, "crude_oil_90d_ma": 80}),
        ]
        for poly, fred in test_cases:
            weights = compute_scenario_weights(poly, fred)
            total = sum(weights.values())
            assert abs(total - 1.0) < 1e-10, f"Weights sum to {total} for inputs {poly}, {fred}"
