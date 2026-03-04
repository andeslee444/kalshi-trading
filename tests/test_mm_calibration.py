"""Tests for market maker calibration integration.

Tests the calibration config format, loading logic, and activation criteria.
Uses importlib to load market-maker.py (hyphenated filename).
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
import importlib
import importlib.util
import sys


def _load_mm_module():
    """Load market-maker.py with stubbed dependencies."""
    mock_auth = MagicMock()
    mock_client = MagicMock()
    mock_auth.KalshiClient.return_value = mock_client
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    mock_auth.TradeManager = MagicMock()
    mock_auth.trim_trade_log = MagicMock()
    mock_auth.build_market_snapshot = MagicMock(return_value={})

    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["probability"] = MagicMock()
    sys.modules["capital_allocator"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "market_maker",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "market-maker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Tests: Calibration config format ──

class TestCalibrationConfig:
    def test_valid_config_format(self):
        """Verify the expected calibration config structure."""
        config = {
            "KXHIGH": {
                "gamma": 0.25,
                "k": 1.2,
                "sharpe": 1.5,
                "activated": True,
                "calibrated_at": "2026-03-03T10:00:00",
            },
            "KXBTC": {
                "gamma": 0.5,
                "k": 2.0,
                "sharpe": 0.8,
                "activated": False,
                "calibrated_at": "2026-03-03T10:00:00",
            },
        }
        # Validate structure
        for prefix, params in config.items():
            assert "gamma" in params
            assert "k" in params
            assert "sharpe" in params
            assert "activated" in params
            assert params["gamma"] > 0
            assert params["k"] > 0

    def test_activation_criteria(self):
        """Only activate when simulated Sharpe > 1.0."""
        config = {
            "KXHIGH": {"gamma": 0.25, "k": 1.2, "sharpe": 1.5, "activated": True},
            "KXBTC": {"gamma": 0.5, "k": 2.0, "sharpe": 0.8, "activated": False},
        }
        for prefix, params in config.items():
            assert params["activated"] == (params["sharpe"] > 1.0)


class TestLoadCalibratedParams:
    def test_load_calibrated_gamma_k(self, tmp_path):
        """Verify load_calibrated_params reads per-market params."""
        cal_path = tmp_path / "mm-calibration.json"
        cal_data = {
            "KXHIGH": {"gamma": 0.25, "k": 1.2, "sharpe": 1.5, "activated": True},
        }
        cal_path.write_text(json.dumps(cal_data))

        # The function we'll add: load_calibrated_params(path, ticker_prefix)
        # Returns (gamma, k) if activated, else None
        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXHIGH")
        assert result is not None
        assert result["gamma"] == 0.25
        assert result["k"] == 1.2

    def test_returns_none_for_inactive(self, tmp_path):
        cal_path = tmp_path / "mm-calibration.json"
        cal_data = {
            "KXBTC": {"gamma": 0.5, "k": 2.0, "sharpe": 0.8, "activated": False},
        }
        cal_path.write_text(json.dumps(cal_data))

        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXBTC")
        assert result is None

    def test_returns_none_for_missing_prefix(self, tmp_path):
        cal_path = tmp_path / "mm-calibration.json"
        cal_path.write_text("{}")

        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXHIGH")
        assert result is None

    def test_returns_none_for_missing_file(self, tmp_path):
        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(tmp_path / "nonexistent.json"), "KXHIGH")
        assert result is None
