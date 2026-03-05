"""Tests for scenario engine -- macro scenario-weighted CPI probability."""
import math
import pytest
from scenario_engine import (
    SCENARIOS, DEFAULT_WEIGHTS, compute_scenario_weights,
    scenario_probability, ScenarioResult,
)


class TestDefaultWeights:
    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_WEIGHTS.values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_all_scenarios_have_weights(self):
        for name in SCENARIOS:
            assert name in DEFAULT_WEIGHTS, f"Missing weight for scenario: {name}"

    def test_six_scenarios_defined(self):
        assert len(SCENARIOS) == 6


class TestComputeWeights:
    def test_no_market_data_returns_defaults(self):
        weights = compute_scenario_weights({}, {})
        assert sum(weights.values()) == pytest.approx(1.0)
        assert weights["status_quo"] == pytest.approx(DEFAULT_WEIGHTS["status_quo"], abs=0.05)

    def test_polymarket_tariff_shifts_weights(self):
        poly = {"tariff_escalation_prob": 0.60}
        weights = compute_scenario_weights(poly, {})
        assert weights["tariff_escalation"] > DEFAULT_WEIGHTS["tariff_escalation"]
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_oil_spike_increases_supply_shock(self):
        fred = {"crude_oil": 120, "crude_oil_90d_ma": 80}  # 50% above MA
        weights = compute_scenario_weights({}, fred)
        assert weights["supply_shock"] > DEFAULT_WEIGHTS["supply_shock"]
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_yield_inversion_increases_recession(self):
        fred = {"T10Y2Y": -0.80}  # Deep inversion
        weights = compute_scenario_weights({}, fred)
        assert weights["recession"] > DEFAULT_WEIGHTS["recession"]
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_weights_always_normalized(self):
        """Even with extreme signals, weights sum to 1.0."""
        poly = {"tariff_escalation_prob": 0.99}
        fred = {"crude_oil": 200, "crude_oil_90d_ma": 80, "T10Y2Y": -2.0}
        weights = compute_scenario_weights(poly, fred)
        assert sum(weights.values()) == pytest.approx(1.0)
        for w in weights.values():
            assert 0 <= w <= 1


class TestScenarioProbability:
    def test_all_agree_high_prob(self):
        """When nowcast is far above threshold, all scenarios agree -> high prob."""
        result = scenario_probability(
            fused_nowcast=3.0, posterior_sigma=0.10,
            threshold=2.0, direction="above",
            scenario_weights=DEFAULT_WEIGHTS,
        )
        assert result.probability > 0.90
        assert result.agreement > 0.80

    def test_near_threshold_lower_agreement(self):
        """Near threshold, scenarios with shifts disagree -> lower agreement."""
        result = scenario_probability(
            fused_nowcast=2.05, posterior_sigma=0.20,
            threshold=2.0, direction="above",
            scenario_weights=DEFAULT_WEIGHTS,
        )
        assert result.probability < 0.90
        assert result.agreement < 0.90

    def test_direction_below(self):
        result = scenario_probability(
            fused_nowcast=1.5, posterior_sigma=0.10,
            threshold=2.0, direction="below",
            scenario_weights=DEFAULT_WEIGHTS,
        )
        # Nowcast 1.5 vs threshold 2.0: P(CPI < 2.0) should be high
        assert result.probability > 0.50  # P(below 2.0) is high when nowcast is 1.5

    def test_scenario_detail_populated(self):
        result = scenario_probability(
            fused_nowcast=2.41, posterior_sigma=0.15,
            threshold=2.0, direction="above",
            scenario_weights=DEFAULT_WEIGHTS,
        )
        assert len(result.per_scenario) == 6
        for name, p in result.per_scenario.items():
            assert 0 <= p <= 1, f"Scenario {name} has invalid prob {p}"

    def test_not_fake_certainty(self):
        """With widened scenario sigmas, prob should NOT be 1.0 at long horizons."""
        result = scenario_probability(
            fused_nowcast=2.41, posterior_sigma=0.40,  # Wide sigma (107d horizon)
            threshold=2.0, direction="above",
            scenario_weights=DEFAULT_WEIGHTS,
        )
        assert result.probability < 0.99, "Should not be fake certainty"
        assert result.probability > 0.50, "Should still be likely"
