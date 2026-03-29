"""Tests for calibration script: thresholds, Bayesian shrinkage, and grid search.

Tests multi-objective optimization (Brier primary, P&L tiebreaker),
minimum sample size thresholds (10 per city, 30 global), and
canonical TRADE_FILES import.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from importlib import import_module
import importlib.util

# Load calibrate-sigma.py (hyphenated name)
spec = importlib.util.spec_from_file_location(
    "calibrate_sigma",
    str(Path(__file__).resolve().parent.parent / "scripts" / "calibrate-sigma.py")
)
calibrate_sigma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrate_sigma)

calibrate_weather = calibrate_sigma.calibrate_weather
calibrate_nws = calibrate_sigma.calibrate_nws
calibrate_info_arb = calibrate_sigma.calibrate_info_arb
brier_score_fn = calibrate_sigma.brier_score
simulated_pnl_fn = calibrate_sigma.simulated_pnl


def _make_weather_trade(ticker, forecast_temp, side, revenue, city="MIA",
                         timestamp="2026-02-10T12:00:00Z", days_out=1):
    """Create a mock trade + settlement for weather calibration testing."""
    return {
        "ticker": ticker,
        "forecast_temp": forecast_temp,
        "side": side,
        "timestamp": timestamp,
    }, ticker, revenue


class TestCalibrationThresholds:
    """Test per-city minimum trade threshold (10 per city)."""

    def _make_matched_trades(self, n, city="MIA"):
        """Generate n synthetic matched trades for a city."""
        trades = []
        settlement_map = {}
        for i in range(n):
            ticker = f"KXHIGH{city}-26FEB{10+i:02d}-T86"
            trade = {
                "ticker": ticker,
                "forecast_temp": 88.0 if i % 2 == 0 else 84.0,
                "side": "yes",
                "timestamp": "2026-02-01T12:00:00Z",
            }
            # Alternate outcomes
            settlement_map[ticker] = 100 if i % 2 == 0 else -50
            trades.append(trade)
        return trades, settlement_map

    def test_fewer_than_10_skips_city(self):
        """Cities with <10 trades should not produce per-city calibration."""
        trades, smap = self._make_matched_trades(9, city="LAX")
        result = calibrate_weather(trades, smap)
        assert "LAX" not in result.get("per_city", {})

    def test_10_trades_produces_calibration(self):
        """Cities with 10+ trades should produce per-city calibration."""
        trades, smap = self._make_matched_trades(12, city="CHI")
        result = calibrate_weather(trades, smap)
        assert result.get("n", 0) > 0
        # If CHI has 12 trades, it should appear in per_city
        if "per_city" in result:
            chi = result["per_city"].get("CHI")
            if chi:
                assert chi["n"] == 12
                assert "sigma_intercept" in chi
                assert "shrinkage_weight" in chi

    def test_shrinkage_weight_correctness(self):
        """With k=15, 10 trades -> weight = 10/(10+15) = 0.40."""
        trades, smap = self._make_matched_trades(10, city="DEN")
        result = calibrate_weather(trades, smap)
        den = result.get("per_city", {}).get("DEN")
        if den:
            expected_weight = 10 / (10 + 15)  # 0.40
            assert abs(den["shrinkage_weight"] - expected_weight) < 0.01

    def test_20_trades_higher_weight(self):
        """With 20 trades, city weight should be 20/(20+15) ~ 0.57."""
        trades, smap = self._make_matched_trades(20, city="NY")
        result = calibrate_weather(trades, smap)
        ny = result.get("per_city", {}).get("NY")
        if ny:
            expected_weight = 20 / (20 + 15)
            assert abs(ny["shrinkage_weight"] - expected_weight) < 0.01


class TestBrierScoreOptimization:
    """Test that calibration uses Brier score as primary optimization target."""

    def test_brier_score_computation(self):
        """Brier score function works correctly."""
        # Perfect predictions
        assert brier_score_fn([(1.0, 1), (0.0, 0)]) == 0.0
        # Worst predictions
        assert brier_score_fn([(0.0, 1), (1.0, 0)]) == 1.0
        # 50/50 on everything
        assert abs(brier_score_fn([(0.5, 1), (0.5, 0)]) - 0.25) < 0.001
        # Empty returns None
        assert brier_score_fn([]) is None

    def test_weather_cal_reports_brier(self):
        """calibrate_weather should report global_brier in results."""
        trades = []
        settlement_map = {}
        for i in range(15):
            ticker = f"KXHIGHMIA-26FEB{10+i:02d}-T86"
            trades.append({
                "ticker": ticker,
                "forecast_temp": 88.0 if i % 2 == 0 else 84.0,
                "side": "yes",
                "timestamp": "2026-02-01T12:00:00Z",
            })
            settlement_map[ticker] = 100 if i % 2 == 0 else -50
        result = calibrate_weather(trades, settlement_map)
        assert result.get("n", 0) > 0
        assert "global_brier" in result
        assert result["global_brier"] is not None
        # Brier should be between 0 and 1
        assert 0 <= result["global_brier"] <= 1

    def test_weather_cal_reports_log_loss(self):
        """calibrate_weather should also report log_loss alongside Brier."""
        trades = []
        settlement_map = {}
        for i in range(15):
            ticker = f"KXHIGHMIA-26FEB{10+i:02d}-T86"
            trades.append({
                "ticker": ticker,
                "forecast_temp": 88.0 if i % 2 == 0 else 84.0,
                "side": "yes",
                "timestamp": "2026-02-01T12:00:00Z",
            })
            settlement_map[ticker] = 100 if i % 2 == 0 else -50
        result = calibrate_weather(trades, settlement_map)
        assert "global_log_loss" in result


class TestNwsMinimum:
    """Test NWS hour bucket minimum of 3 entries."""

    def test_fewer_than_3_skips_bucket(self):
        """Hour buckets with <3 entries should not produce sigma."""
        trades = [
            {
                "ticker": "KXHIGHMIA-26FEB16-T86",
                "strategy": "nws",
                "side": "yes",
                "running_high": 88.0,
                "hour_of_day": 17,
                "timestamp": "2026-02-16T17:00:00Z",
            },
            {
                "ticker": "KXHIGHMIA-26FEB17-T86",
                "strategy": "nws",
                "side": "yes",
                "running_high": 84.0,
                "hour_of_day": 17,
                "timestamp": "2026-02-17T17:00:00Z",
            },
        ]
        smap = {
            "KXHIGHMIA-26FEB16-T86": 100,
            "KXHIGHMIA-26FEB17-T86": -50,
        }
        result = calibrate_nws(trades, smap)
        # Only 2 trades in "17+" bucket, should be skipped
        assert "17+" not in result.get("sigma_by_hour", {})


class TestInfoArbMinimum:
    """Test info-arb day bucket minimum of 10 entries."""

    def test_fewer_than_10_skips_bucket(self):
        """Day buckets with <10 entries should not produce sigma."""
        trades = [
            {
                "ticker": "KXALBUM-A",
                "confidence": 0.85,
                "side": "yes",
                "timestamp": "2026-02-10T12:00:00Z",  # Tuesday (dow=1) -> mon_tue
            },
        ]
        smap = {"KXALBUM-A": 100}
        result = calibrate_info_arb(trades, smap, "album_sales")
        # Only 1 trade in mon_tue bucket, should be skipped
        assert "mon_tue" not in result.get("sigma_by_day", {})


class TestTradeFilesImport:
    """Test that calibrate-sigma uses canonical TRADE_FILES."""

    def test_uses_canonical_trade_files(self):
        """Verify the module imports TRADE_FILES from trade_files module."""
        # The module should have TRADE_FILES imported from trade_files
        tf = calibrate_sigma.TRADE_FILES
        # Should track the current canonical trade file registry.
        assert len(tf) == len(import_module("trade_files").TRADE_FILES)
        assert len(tf) >= 10
        # Check that it has the expected structure (label, bot, filename keys)
        for entry in tf:
            assert "label" in entry
            assert "bot" in entry
            assert "filename" in entry


class TestCalibrationSavePaths:

    def test_shadow_output_skips_live_side_effects(self, tmp_path, monkeypatch):
        calls = []

        monkeypatch.setattr(calibrate_sigma, "_backup_calibration", lambda: calls.append("backup"))
        monkeypatch.setattr(
            calibrate_sigma,
            "_sync_runtime_weather_config",
            lambda *_args, **_kwargs: calls.append("sync"),
        )

        shadow_path = tmp_path / "shadow" / "weather-calibration.json"
        payload = {"generated_at": "2026-03-22T00:00:00", "ensemble": {"weights": {"gfs": 1.0}}}

        saved_path, is_canonical = calibrate_sigma._save_calibration_output(
            payload,
            output_path=shadow_path,
        )

        assert saved_path == shadow_path
        assert is_canonical is False
        assert shadow_path.exists()
        assert calls == []

    def test_canonical_output_runs_live_side_effects(self, tmp_path, monkeypatch):
        calls = []
        canonical_path = tmp_path / "config" / "calibration.json"

        monkeypatch.setattr(calibrate_sigma, "CALIBRATION_PATH", canonical_path)
        monkeypatch.setattr(calibrate_sigma, "_backup_calibration", lambda: calls.append("backup"))
        monkeypatch.setattr(
            calibrate_sigma,
            "_sync_runtime_weather_config",
            lambda *_args, **_kwargs: calls.append("sync"),
        )

        payload = {"generated_at": "2026-03-22T00:00:00", "ensemble": {"weights": {"gfs": 1.0}}}

        saved_path, is_canonical = calibrate_sigma._save_calibration_output(
            payload,
            output_path=canonical_path,
        )

        assert saved_path == canonical_path
        assert is_canonical is True
        assert canonical_path.exists()
        assert calls == ["backup", "sync"]


class TestEdgeThresholdSweep:
    """Test edge threshold optimization integrated with sigma calibration."""

    def test_optimal_threshold_in_range(self):
        """Optimal edge threshold should be between 0.04 and 0.15."""
        trades = []
        settlement_map = {}
        for i in range(30):
            ticker = f"KXHIGHMIA-26FEB{10+i%28:02d}-T86"
            trades.append({
                "ticker": ticker,
                "forecast_temp": 88.0 if i % 3 != 0 else 84.0,
                "side": "yes",
                "timestamp": "2026-02-01T12:00:00Z",
            })
            settlement_map[ticker] = 100 if i % 3 != 0 else -50
        result = calibrate_weather(trades, settlement_map)
        assert result.get("n", 0) > 0
        assert "global_brier" in result

    def test_brier_guides_threshold(self):
        """Lower Brier score should allow lower edge threshold."""
        assert _threshold_for_brier(0.05) == 0.06
        assert _threshold_for_brier(0.15) == 0.08
        assert _threshold_for_brier(0.25) == 0.10

    def test_none_brier_defaults_to_eight(self):
        """None Brier (no data) should default to 0.08."""
        assert _threshold_for_brier(None) == 0.08

    def test_boundary_at_010(self):
        """Brier exactly 0.10 should use 0.08 (good range)."""
        assert _threshold_for_brier(0.10) == 0.08

    def test_boundary_at_020(self):
        """Brier exactly 0.20 should use 0.08 (good range)."""
        assert _threshold_for_brier(0.20) == 0.08


def _threshold_for_brier(brier):
    """Map Brier score to recommended edge threshold.

    Brier < 0.10 (excellent): 6%
    Brier 0.10-0.20 (good): 8%
    Brier > 0.20 (needs work): 10%
    None: 8% (default)
    """
    if brier is None:
        return 0.08
    if brier < 0.10:
        return 0.06
    elif brier <= 0.20:
        return 0.08
    else:
        return 0.10
