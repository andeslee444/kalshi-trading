"""Integration tests for the world-class economics bot pipeline.

Tests the full flow: belief filter -> scenario engine -> uncertainty_kelly
without network calls.
"""
import math
import pytest
from cpi_belief_filter import CPIBeliefFilter
from scenario_engine import (
    compute_scenario_weights, scenario_probability, DEFAULT_WEIGHTS,
)
from probability import (
    cpi_nowcast_sigma, gdp_nowcast_sigma, econ_nowcast_probability,
    uncertainty_kelly, _reset_calibration,
)


class TestFullPipeline:
    """End-to-end: data sources -> filter -> scenarios -> sizing."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_typical_cpi_trade(self):
        """Simulate a typical CPI trade with 3 data sources."""
        # Data sources
        cleveland_nowcast = 2.41
        truflation_cpi = 2.45
        tips_breakeven = 2.38
        days_to_release = 107
        threshold = 2.0

        # Step 1: Bayesian filter
        base_sigma = cpi_nowcast_sigma(days_to_release)
        assert base_sigma > 0.30, f"Sigma should be wide at {days_to_release}d"

        belief = CPIBeliefFilter(cleveland_nowcast, base_sigma)
        belief.update(truflation_cpi, obs_sigma=0.15)
        belief.update(tips_breakeven, obs_sigma=0.25)
        fused_nowcast, posterior_sigma = belief.posterior

        # Fused should be between sources
        assert 2.38 <= fused_nowcast <= 2.45
        # Posterior should be tighter than any single source
        assert posterior_sigma < base_sigma
        assert posterior_sigma < 0.15

        # Step 2: Scenario probability
        weights = compute_scenario_weights({}, {})  # No market data
        result = scenario_probability(
            fused_nowcast, posterior_sigma, threshold, "above", weights,
        )

        # Should NOT be fake certainty
        assert result.probability < 0.99
        assert result.probability > 0.60  # Still likely above 2.0%

        # Step 3: Sizing
        edge = result.probability - 0.02  # 2-cent contract
        count, risk, details = uncertainty_kelly(
            edge=edge, price_cents=2, max_cost_cents=5000,
            bankroll_cents=500000,
            scenario_agreement=result.agreement,
            posterior_sigma=posterior_sigma,
        )

        assert count > 0
        assert count < 5000  # Sanity: not placing insane number of contracts
        assert details["confidence"] < 1.0  # Reduced by uncertainty
        assert details["confidence"] > 0.1  # But not to nothing

    def test_no_extra_sources_degrades_gracefully(self):
        """With only Cleveland Fed, system should still work but be more conservative."""
        cleveland_nowcast = 2.41
        days_to_release = 107

        belief = CPIBeliefFilter(cleveland_nowcast, cpi_nowcast_sigma(days_to_release))
        # No updates -- no Truflation, no TIPS
        fused_nowcast, posterior_sigma = belief.posterior

        assert fused_nowcast == 2.41  # Unchanged
        assert posterior_sigma == cpi_nowcast_sigma(days_to_release)  # Unchanged

        result = scenario_probability(
            fused_nowcast, posterior_sigma, 2.0, "above", DEFAULT_WEIGHTS,
        )

        # Should still produce reasonable probability (wider sigma -> lower confidence)
        assert 0.50 < result.probability < 0.95

        # Sizing should be smaller due to wide sigma
        edge = result.probability - 0.02
        count_no_sources, _, det = uncertainty_kelly(
            edge=edge, price_cents=2, max_cost_cents=5000,
            bankroll_cents=500000,
            scenario_agreement=result.agreement,
            posterior_sigma=posterior_sigma,
        )
        assert det["sigma_mult"] < 0.5  # Wide sigma penalizes

    def test_tariff_shock_shifts_probability_up(self):
        """With high tariff probability, CPI probability should shift up."""
        cleveland_nowcast = 2.41
        days_to_release = 30
        sigma = cpi_nowcast_sigma(days_to_release)
        belief = CPIBeliefFilter(cleveland_nowcast, sigma)
        fused, post_sigma = belief.posterior

        # Status quo scenario
        weights_calm = compute_scenario_weights({}, {})
        result_calm = scenario_probability(fused, post_sigma, 2.0, "above", weights_calm)

        # Tariff escalation scenario
        weights_tariff = compute_scenario_weights({"tariff_escalation_prob": 0.80}, {})
        result_tariff = scenario_probability(fused, post_sigma, 2.0, "above", weights_tariff)

        # With tariff risk, probability should be higher (CPI shifts up)
        # because tariff_escalation has a positive CPI shift
        assert result_tariff.probability > result_calm.probability

        # Both should have weighted_std tracking scenario disagreement
        assert result_tariff.weighted_std > 0
        assert result_calm.weighted_std > 0

    def test_gdp_sigma_uses_correct_floor_and_range(self):
        """GDP sigma should use floor=0.15, range=0.45 (fixed in Plan 1).

        At d=0: sigma = 0.15 (floor)
        At d=30: sigma = 0.15 + 0.45 * (1 - exp(-0.12*30)) ~ 0.59
        At d=107: sigma ~ 0.60 (near ceiling)
        """
        import math

        sigma_0 = gdp_nowcast_sigma(0)
        assert sigma_0 == pytest.approx(0.15, abs=0.001), \
            f"GDP sigma at d=0 should be 0.15 (floor), got {sigma_0}"

        sigma_7 = gdp_nowcast_sigma(7)
        expected_7 = 0.15 + 0.45 * (1 - math.exp(-0.12 * 7))
        assert sigma_7 == pytest.approx(expected_7, abs=0.01), \
            f"GDP sigma at d=7 should be ~{expected_7:.3f}, got {sigma_7}"

        sigma_30 = gdp_nowcast_sigma(30)
        expected_30 = 0.15 + 0.45 * (1 - math.exp(-0.12 * 30))
        assert sigma_30 == pytest.approx(expected_30, abs=0.01), \
            f"GDP sigma at d=30 should be ~{expected_30:.3f}, got {sigma_30}"

        # GDP sigma should always be wider than CPI sigma at the same horizon
        for d in [0, 7, 14, 30]:
            assert gdp_nowcast_sigma(d) > cpi_nowcast_sigma(d), \
                f"GDP sigma should be wider than CPI at d={d}"

    def test_near_threshold_produces_small_edge(self):
        """When nowcast is very close to threshold, edge should be small."""
        cleveland_nowcast = 2.02  # Just above threshold
        sigma = cpi_nowcast_sigma(14)
        belief = CPIBeliefFilter(cleveland_nowcast, sigma)
        fused, post_sigma = belief.posterior

        result = scenario_probability(fused, post_sigma, 2.0, "above", DEFAULT_WEIGHTS)
        # Near 50% -- some scenarios have it above, some below due to shifts
        assert 0.30 < result.probability < 0.75
