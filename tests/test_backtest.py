"""Tests for pure functions in scripts/backtest.py."""

import json
import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import backtest module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtest import _determine_outcome, brier_score, calibration_table


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


class TestDetermineOutcome:

    def test_local_settlement_result_respects_yes_side(self):
        assert _determine_outcome({"settlement_result": "won"}, settlement_revenue=None, side="yes") == 1
        assert _determine_outcome({"settlement_result": "lost"}, settlement_revenue=None, side="yes") == 0

    def test_local_settlement_result_respects_no_side(self):
        assert _determine_outcome({"settlement_result": "won"}, settlement_revenue=None, side="no") == 0
        assert _determine_outcome({"settlement_result": "lost"}, settlement_revenue=None, side="no") == 1


class TestBacktestSavePaths:

    @staticmethod
    def _configure_single_weather_trade(monkeypatch, tmp_path, backtest_module):
        trade = {
            "ticker": "KXHIGHMIA-26FEB28-T86",
            "timestamp": "2026-02-27T12:00:00Z",
            "side": "yes",
            "forecast_temp": 90.0,
            "settlement_result": "won",
            "price": 45,
        }

        monkeypatch.setattr(backtest_module, "PROJECT_DIR", tmp_path)
        monkeypatch.setattr(backtest_module, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(
            backtest_module,
            "TRADE_FILES",
            [{"label": "weather", "path": tmp_path / "data" / "weather-trades.json"}],
        )
        monkeypatch.setattr(backtest_module, "load_trades_safe", lambda _: [trade])

        class DummyClient:
            def get(self, path):
                return {"settlements": [{"ticker": trade["ticker"], "revenue": 0}], "cursor": None}

        import kalshi_auth

        monkeypatch.setattr(kalshi_auth, "KalshiClient", DummyClient)

    def test_save_uses_canonical_default_path(self, monkeypatch, tmp_path, capsys):
        import backtest as backtest_module

        self._configure_single_weather_trade(monkeypatch, tmp_path, backtest_module)

        monkeypatch.setattr(
            sys,
            "argv",
            ["backtest.py", "--save", "--allow-canonical-save"],
        )

        backtest_module.main()
        capsys.readouterr()

        default_path = tmp_path / "data" / "backtest-results.json"
        assert default_path.exists()
        payload = json.loads(default_path.read_text())
        assert payload["n_evaluated"] == 1
        assert payload["per_bot"]["weather"]["n_evaluated"] == 1

    def test_canonical_save_requires_explicit_override(self, monkeypatch, tmp_path):
        import backtest as backtest_module

        self._configure_single_weather_trade(monkeypatch, tmp_path, backtest_module)

        monkeypatch.setattr(
            sys,
            "argv",
            ["backtest.py", "--save"],
        )

        with pytest.raises(SystemExit):
            backtest_module.main()

    def test_save_honors_custom_shadow_output_path(self, monkeypatch, tmp_path, capsys):
        import backtest as backtest_module

        self._configure_single_weather_trade(monkeypatch, tmp_path, backtest_module)

        shadow_path = tmp_path / "shadow" / "weather-backtest.json"
        monkeypatch.setattr(
            sys,
            "argv",
            ["backtest.py", "--save", "--output", str(shadow_path)],
        )

        backtest_module.main()
        capsys.readouterr()

        canonical_path = tmp_path / "data" / "backtest-results.json"
        assert shadow_path.exists()
        assert canonical_path.exists() is False

        payload = json.loads(shadow_path.read_text())
        assert payload["n_evaluated"] == 1
        assert payload["per_bot"]["weather"]["n_evaluated"] == 1

    def test_no_api_still_uses_local_settlement_result(self, monkeypatch, tmp_path, capsys):
        import backtest as backtest_module

        self._configure_single_weather_trade(monkeypatch, tmp_path, backtest_module)

        shadow_path = tmp_path / "shadow" / "weather-backtest.json"
        monkeypatch.setattr(
            sys,
            "argv",
            ["backtest.py", "--no-api", "--save", "--output", str(shadow_path)],
        )

        backtest_module.main()
        capsys.readouterr()

        payload = json.loads(shadow_path.read_text())
        assert payload["n_evaluated"] == 1
        assert payload["per_bot"]["weather"]["n_evaluated"] == 1

    def test_no_api_canonical_save_requires_explicit_override(self, monkeypatch, tmp_path):
        import backtest as backtest_module

        self._configure_single_weather_trade(monkeypatch, tmp_path, backtest_module)
        monkeypatch.setattr(
            sys,
            "argv",
            ["backtest.py", "--no-api", "--save"],
        )

        with pytest.raises(SystemExit):
            backtest_module.main()
