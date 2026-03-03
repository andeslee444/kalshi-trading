"""Tests for regime_detector.py — HMM-based vol regime detection.

The regime detector classifies market conditions into 4 states:
low_vol, normal, high_vol, crisis. It uses realized volatility
observations and online Bayesian updates.
"""

import json
import math
import tempfile
from pathlib import Path
import pytest


class TestRegimeDetectorInit:
    """Test regime detector initialization and default parameters."""

    def test_default_four_states(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.n_states == 4
        assert rd.state_names == ["low_vol", "normal", "high_vol", "crisis"]

    def test_default_transition_matrix_shape(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.transition_matrix) == 4
        assert all(len(row) == 4 for row in rd.transition_matrix)

    def test_transition_rows_sum_to_one(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for row in rd.transition_matrix:
            assert abs(sum(row) - 1.0) < 1e-9

    def test_default_emission_params(self):
        """Each state has a mean and std for vol emission (log-normal)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.emission_params) == 4
        for state in rd.state_names:
            assert "mean" in rd.emission_params[state]
            assert "std" in rd.emission_params[state]
            assert rd.emission_params[state]["std"] > 0

    def test_low_vol_mean_less_than_crisis(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.emission_params["low_vol"]["mean"] < rd.emission_params["crisis"]["mean"]

    def test_initial_belief_uniform(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.belief) == 4
        for p in rd.belief:
            assert abs(p - 0.25) < 1e-9


class TestRegimeUpdate:
    """Test online Bayesian regime updates."""

    def test_low_vol_observation_shifts_belief(self):
        """Observing low vol should increase P(low_vol)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Low vol observation (e.g., 0.15 annualized = 15% vol for BTC is calm)
        rd.update(0.15)
        assert rd.belief[0] > 0.25  # low_vol probability increased

    def test_high_vol_observation_shifts_belief(self):
        """Observing high vol should increase P(high_vol) or P(crisis)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # High vol observation (e.g., 1.20 annualized = 120% vol = extreme)
        rd.update(1.20)
        # high_vol (index 2) + crisis (index 3) should dominate
        assert rd.belief[2] + rd.belief[3] > 0.5

    def test_multiple_updates_converge(self):
        """Repeated low-vol observations should converge belief to low_vol."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)  # Consistently low vol
        assert rd.belief[0] > 0.8  # Strong low_vol belief

    def test_regime_switch_detected(self):
        """After regime switch (low→high vol), belief should shift."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Start in low vol
        for _ in range(5):
            rd.update(0.15)
        assert rd.belief[0] > 0.5  # In low_vol regime
        # Switch to high vol
        for _ in range(5):
            rd.update(0.90)
        assert rd.belief[0] < 0.3  # Left low_vol regime
        assert rd.belief[2] + rd.belief[3] > 0.5  # Moved to high/crisis

    def test_belief_sums_to_one_after_update(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        rd.update(0.50)
        assert abs(sum(rd.belief) - 1.0) < 1e-9

    def test_update_increments_count(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.n_updates == 0
        rd.update(0.50)
        assert rd.n_updates == 1


class TestRegimeClassification:
    """Test regime classification from belief state."""

    def test_current_regime_returns_max_belief(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)
        assert rd.current_regime() == "low_vol"

    def test_current_regime_after_crisis(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(1.50)  # Extremely high vol
        regime = rd.current_regime()
        assert regime in ("high_vol", "crisis")

    def test_regime_confidence(self):
        """Confidence should be max(belief) — higher when regime is clear."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Uniform belief → low confidence
        assert rd.regime_confidence() < 0.5
        # After convergence → high confidence
        for _ in range(10):
            rd.update(0.15)
        assert rd.regime_confidence() > 0.7

    def test_is_elevated_vol(self):
        """Helper: returns True if high_vol or crisis."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)
        assert rd.is_elevated_vol() is False
        for _ in range(10):
            rd.update(1.00)
        assert rd.is_elevated_vol() is True
