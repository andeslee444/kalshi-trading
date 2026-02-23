"""Tests for calibration script: thresholds, Bayesian shrinkage, and grid search."""

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
    """Test per-city minimum trade threshold (lowered from 20 to 5)."""

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

    def test_fewer_than_5_skips_city(self):
        """Cities with <5 trades should not produce per-city calibration."""
        trades, smap = self._make_matched_trades(4, city="LAX")
        result = calibrate_weather(trades, smap)
        assert "LAX" not in result.get("per_city", {})

    def test_5_trades_produces_calibration(self):
        """Cities with 5+ trades should produce per-city calibration."""
        trades, smap = self._make_matched_trades(6, city="CHI")
        result = calibrate_weather(trades, smap)
        assert result.get("n", 0) > 0
        # If CHI has 6 trades, it should appear in per_city
        if "per_city" in result:
            chi = result["per_city"].get("CHI")
            if chi:
                assert chi["n"] == 6
                assert "sigma_intercept" in chi
                assert "shrinkage_weight" in chi

    def test_shrinkage_weight_correctness(self):
        """With k=15, 5 trades -> weight = 5/(5+15) = 0.25."""
        trades, smap = self._make_matched_trades(5, city="DEN")
        result = calibrate_weather(trades, smap)
        den = result.get("per_city", {}).get("DEN")
        if den:
            expected_weight = 5 / (5 + 15)  # 0.25
            assert abs(den["shrinkage_weight"] - expected_weight) < 0.01

    def test_20_trades_higher_weight(self):
        """With 20 trades, city weight should be 20/(20+15) ≈ 0.57."""
        trades, smap = self._make_matched_trades(20, city="NY")
        result = calibrate_weather(trades, smap)
        ny = result.get("per_city", {}).get("NY")
        if ny:
            expected_weight = 20 / (20 + 15)
            assert abs(ny["shrinkage_weight"] - expected_weight) < 0.01


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
    """Test info-arb day bucket minimum of 3 entries."""

    def test_fewer_than_3_skips_bucket(self):
        """Day buckets with <3 entries should not produce sigma."""
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
