"""Tests for edge monitoring and competitive intelligence.

Tests pure analytics — no API calls or kalshi_auth dependency.
Observations are provided as fixture data.
"""

import math
import pytest
from edge_monitor import EdgeMonitor, _linear_regression


# ── Fixtures ──

def _obs(timestamp, market_type, model_prob, market_price_cents, won):
    """Build an observation dict."""
    return {
        "timestamp": timestamp,
        "market_type": market_type,
        "model_prob": model_prob,
        "market_price_cents": market_price_cents,
        "raw_edge": model_prob - market_price_cents / 100.0,
        "settled_won": won,
    }


# Stable edge: consistent ~15% gap, high win rate
STABLE_OBSERVATIONS = [
    _obs(f"2026-02-{d:02d}T10:00:00", "weather", 0.70, 55, d % 3 != 0)
    for d in range(1, 29)
]

# Decaying edge: gap shrinks from 20% to 5% over 4 weeks
DECAYING_OBSERVATIONS = [
    _obs(f"2026-02-{d:02d}T10:00:00", "crypto",
         0.60 - d * 0.005,           # model_prob decreases slightly
         int((0.40 + d * 0.005) * 100),  # market catches up
         d < 15)                     # stops winning in second half
    for d in range(1, 29)
]

# Sudden competitor: stable for 2 weeks, then gap collapses
COMPETITOR_OBSERVATIONS = (
    [_obs(f"2026-02-{d:02d}T10:00:00", "economics", 0.75, 60, True)
     for d in range(1, 15)]
    + [_obs(f"2026-02-{d:02d}T10:00:00", "economics", 0.72, 70, d % 2 == 0)
       for d in range(15, 29)]
)


# ── Tests: _linear_regression ──

class TestLinearRegression:
    def test_positive_slope(self):
        xs = [1, 2, 3, 4, 5]
        ys = [2, 4, 6, 8, 10]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert abs(slope - 2.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_negative_slope(self):
        xs = [1, 2, 3, 4, 5]
        ys = [10, 8, 6, 4, 2]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert slope < 0
        assert abs(slope + 2.0) < 0.01

    def test_flat(self):
        xs = [1, 2, 3, 4, 5]
        ys = [5, 5, 5, 5, 5]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert abs(slope) < 0.01

    def test_single_point(self):
        slope, intercept, r_sq = _linear_regression([1], [5])
        assert slope == 0.0


# ── Tests: EdgeMonitor ──

class TestEdgeRealizationRate:
    def test_high_win_rate(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        rate = em.edge_realization_rate("weather")
        assert rate > 0.5  # Weather observations win ~67% of the time

    def test_declining_win_rate_for_decaying_edge(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        rate = em.edge_realization_rate("crypto")
        assert rate == pytest.approx(14 / 28, abs=0.01)  # wins first 14 days

    def test_unknown_market_type_returns_zero(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        rate = em.edge_realization_rate("nonexistent")
        assert rate == 0.0

    def test_windowed_rate(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        # Last 14 days should have lower win rate
        rate_recent = em.edge_realization_rate("crypto", window_days=14)
        rate_all = em.edge_realization_rate("crypto")
        assert rate_recent < rate_all


class TestEfficiencyTrend:
    def test_stable_edge_flat_trend(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        slope = em.efficiency_trend("weather")
        # Stable edge → slope near zero
        assert abs(slope) < 0.005

    def test_decaying_edge_negative_trend(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        slope = em.efficiency_trend("crypto")
        # Gap is shrinking → slope should be negative (gap decreasing over time)
        assert slope < -0.001

    def test_no_observations_returns_zero(self):
        em = EdgeMonitor()
        slope = em.efficiency_trend("weather")
        assert slope == 0.0


class TestEdgeHalfLife:
    def test_decaying_edge_has_finite_half_life(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        half_life = em.edge_half_life("crypto")
        # Should be a positive finite number (edge is decaying)
        assert 0 < half_life < 365

    def test_stable_edge_has_long_half_life(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        half_life = em.edge_half_life("weather")
        # Stable edge → very long (or infinite) half-life
        assert half_life > 90 or half_life == float("inf")


class TestDetectCompetitor:
    def test_detects_sudden_efficiency_jump(self):
        em = EdgeMonitor()
        em.add_observations(COMPETITOR_OBSERVATIONS)
        detected = em.detect_competitor("economics")
        assert detected is True

    def test_no_competitor_when_stable(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        detected = em.detect_competitor("weather")
        assert detected is False


class TestOptimalStrategyWeights:
    def test_weights_sum_to_one(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS + DECAYING_OBSERVATIONS)
        weights = em.optimal_strategy_weights()
        if weights:
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01

    def test_durable_edge_gets_higher_weight(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS + DECAYING_OBSERVATIONS)
        weights = em.optimal_strategy_weights()
        if "weather" in weights and "crypto" in weights:
            assert weights["weather"] > weights["crypto"]


class TestStatePersistence:
    def test_save_and_load(self, tmp_path):
        state_path = str(tmp_path / "edge-state.json")
        em = EdgeMonitor(state_path=state_path)
        em.add_observations(STABLE_OBSERVATIONS[:5])
        em.save_state()

        em2 = EdgeMonitor(state_path=state_path)
        em2.load_state()
        assert len(em2._observations) == 5

    def test_json_report_structure(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        report = em.json_report()
        assert "market_types" in report
        assert "weather" in report["market_types"]
        assert "realization_rate" in report["market_types"]["weather"]
