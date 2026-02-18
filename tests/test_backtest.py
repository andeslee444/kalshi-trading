"""Tests for pure functions in scripts/backtest.py."""

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import backtest module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtest import brier_score, calibration_table


# ===================================================================
# Brier score tests
# ===================================================================

class TestBrierScore:

    def test_perfect_predictions(self):
        """All correct with full confidence -> Brier = 0."""
        predictions = [(1.0, 1), (0.0, 0), (1.0, 1)]
        assert brier_score(predictions) == pytest.approx(0.0)

    def test_worst_predictions(self):
        """All wrong with full confidence -> Brier = 1."""
        predictions = [(1.0, 0), (0.0, 1)]
        assert brier_score(predictions) == pytest.approx(1.0)

    def test_random_predictions(self):
        """50/50 predictions on balanced outcomes -> Brier ≈ 0.25."""
        predictions = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
        assert brier_score(predictions) == pytest.approx(0.25)

    def test_empty_returns_none(self):
        assert brier_score([]) is None

    def test_single_prediction(self):
        """Single correct prediction."""
        assert brier_score([(0.9, 1)]) == pytest.approx(0.01)

    def test_mixed_predictions(self):
        """Known computation: [(0.8, 1), (0.3, 0)] -> mean of 0.04 and 0.09."""
        predictions = [(0.8, 1), (0.3, 0)]
        expected = ((0.8 - 1) ** 2 + (0.3 - 0) ** 2) / 2
        assert brier_score(predictions) == pytest.approx(expected)


# ===================================================================
# Calibration table tests
# ===================================================================

class TestCalibrationTable:

    def test_empty_predictions(self):
        assert calibration_table([]) == []

    def test_well_calibrated(self):
        """Predictions near 0.7 with 70% actual win rate -> small gap."""
        predictions = [(0.7, 1)] * 7 + [(0.7, 0)] * 3
        table = calibration_table(predictions, n_bins=5)
        # All should fall in the 0.6-0.8 bin
        row = [r for r in table if "0.6" in r["bin"]]
        assert len(row) == 1
        assert row[0]["n"] == 10
        assert row[0]["actual_avg"] == pytest.approx(0.7)
        assert abs(row[0]["gap"]) < 0.05

    def test_multiple_bins(self):
        """Predictions across different ranges create multiple bins."""
        predictions = [(0.2, 0), (0.2, 0), (0.8, 1), (0.8, 1)]
        table = calibration_table(predictions, n_bins=5)
        assert len(table) == 2

    def test_bin_count(self):
        """Each bin has correct count."""
        predictions = [(0.15, 0), (0.15, 1), (0.85, 1)]
        table = calibration_table(predictions, n_bins=5)
        total = sum(r["n"] for r in table)
        assert total == 3

    def test_gap_direction(self):
        """If actual > predicted, gap should be positive."""
        # Predict 0.3 but actual win rate is 100%
        predictions = [(0.3, 1), (0.3, 1)]
        table = calibration_table(predictions, n_bins=5)
        row = table[0]
        assert row["gap"] > 0
