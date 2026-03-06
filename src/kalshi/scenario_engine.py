"""Scenario engine for macro-regime-aware CPI probability estimation.

Defines 6 macro scenarios (status quo, tariff escalation, tariff reversal,
supply shock, recession, stagflation) with dynamic weights derived from
market-implied signals and FRED proxy data.

Final probability is a mixture: P(CPI > T) = sum(w_i * P_i(CPI > T))
where each scenario has its own CPI shift and sigma multiplier.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from probability import _student_t_cdf, _norm_cdf

# Default degrees of freedom — matches probability.py's econ_nowcast_probability default
_ECON_DF = 5


@dataclass
class ScenarioConfig:
    """Configuration for a single macro scenario."""
    cpi_shift: float       # Additive shift to fused nowcast (percentage points)
    sigma_mult: float      # Multiplier on posterior sigma (1.0 = no change)


@dataclass
class ScenarioResult:
    """Output of scenario-weighted probability computation."""
    probability: float                        # Mixture probability
    agreement: float                          # 0-1, how much scenarios agree
    per_scenario: Dict[str, float]            # Per-scenario probabilities
    weights_used: Dict[str, float] = field(default_factory=dict)


# === Scenario Definitions ===

SCENARIOS: Dict[str, ScenarioConfig] = {
    "status_quo":         ScenarioConfig(cpi_shift=0.0,   sigma_mult=1.0),
    "tariff_escalation":  ScenarioConfig(cpi_shift=0.40,  sigma_mult=1.5),
    "tariff_reversal":    ScenarioConfig(cpi_shift=-0.20, sigma_mult=1.2),
    "supply_shock":       ScenarioConfig(cpi_shift=0.70,  sigma_mult=2.5),
    "recession":          ScenarioConfig(cpi_shift=-0.40, sigma_mult=2.0),
    "stagflation":        ScenarioConfig(cpi_shift=0.20,  sigma_mult=3.0),
}

DEFAULT_WEIGHTS: Dict[str, float] = {
    "status_quo":         0.45,
    "tariff_escalation":  0.15,
    "tariff_reversal":    0.10,
    "supply_shock":       0.08,
    "recession":          0.12,
    "stagflation":        0.10,
}


def compute_scenario_weights(
    polymarket_data: Dict,
    fred_data: Dict,
) -> Dict[str, float]:
    """Compute scenario weights from market-implied + proxy signals.

    Falls back to DEFAULT_WEIGHTS when no market data is available.
    Always normalizes output to sum to 1.0.

    Args:
        polymarket_data: Dict with optional keys:
            - tariff_escalation_prob: float (0-1) from Polymarket tariff markets
        fred_data: Dict with optional keys:
            - crude_oil: current crude oil price
            - crude_oil_90d_ma: 90-day moving average
            - T10Y2Y: yield curve spread (negative = inverted)
            - gdpnow: Atlanta Fed GDPNow estimate
            - tips_5y_minus_10y: TIPS term spread (positive = rising long-term)

    Returns:
        Dict[str, float] with scenario names -> weights summing to 1.0
    """
    weights = DEFAULT_WEIGHTS.copy()

    # Tier 1: Market-implied (Polymarket)
    tariff_prob = polymarket_data.get("tariff_escalation_prob")
    if tariff_prob is not None and 0 <= tariff_prob <= 1:
        weights["tariff_escalation"] = tariff_prob * 0.5
        weights["tariff_reversal"] = (1 - tariff_prob) * 0.3

    # Tier 2: FRED proxy signals
    crude = fred_data.get("crude_oil")
    crude_ma = fred_data.get("crude_oil_90d_ma")
    if crude and crude_ma and crude_ma > 0:
        oil_dev = crude / crude_ma - 1
        if oil_dev > 0.20:
            weights["supply_shock"] += min(0.15, oil_dev * 0.3)

    yield_spread = fred_data.get("T10Y2Y")
    if yield_spread is not None and yield_spread < -0.50:
        weights["recession"] += min(0.15, abs(yield_spread) * 0.1)

    gdpnow = fred_data.get("gdpnow")
    tips_trend = fred_data.get("tips_5y_minus_10y")
    if gdpnow is not None and gdpnow < 0.5 and tips_trend is not None and tips_trend > 0:
        weights["stagflation"] += 0.08

    # Normalize
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}

    return weights


def scenario_probability(
    fused_nowcast: float,
    posterior_sigma: float,
    threshold: float,
    direction: str,
    scenario_weights: Optional[Dict[str, float]] = None,
) -> ScenarioResult:
    """Compute mixture probability across all scenarios.

    P(CPI > threshold) = sum(w_i * P_i(CPI > threshold))

    Each scenario shifts the mean and scales sigma to model
    different macro outcomes.

    Args:
        fused_nowcast: Bayesian-fused CPI nowcast (from CPIBeliefFilter)
        posterior_sigma: Posterior uncertainty from the filter
        threshold: Market threshold (e.g., 2.0%)
        direction: "above" or "below"
        scenario_weights: Pre-computed weights (or None for defaults)

    Returns:
        ScenarioResult with mixture probability and agreement metric
    """
    if scenario_weights is None:
        scenario_weights = DEFAULT_WEIGHTS

    total_prob = 0.0
    per_scenario = {}

    for name, weight in scenario_weights.items():
        config = SCENARIOS[name]
        shifted_mean = fused_nowcast + config.cpi_shift
        scaled_sigma = posterior_sigma * config.sigma_mult

        # Guard against zero sigma
        if scaled_sigma <= 0:
            scaled_sigma = 0.001

        z = (threshold - shifted_mean) / scaled_sigma
        # Use Student-t(df=5) for fatter tails — matches probability.py econ model
        if _student_t_cdf is not None:
            if direction == "above":
                p = 1.0 - _student_t_cdf(z, _ECON_DF)
            else:
                p = _student_t_cdf(z, _ECON_DF)
        else:
            if direction == "above":
                p = 1.0 - _norm_cdf(z)
            else:
                p = _norm_cdf(z)

        per_scenario[name] = p
        total_prob += weight * p

    # Clamp probability
    total_prob = max(0.001, min(0.999, total_prob))

    # Scenario agreement: 1.0 = all scenarios agree, 0.0 = total disagreement
    probs = list(per_scenario.values())
    if probs:
        agreement = 1.0 - (max(probs) - min(probs))
    else:
        agreement = 0.0

    return ScenarioResult(
        probability=total_prob,
        agreement=max(0.0, agreement),
        per_scenario=per_scenario,
        weights_used=scenario_weights,
    )
