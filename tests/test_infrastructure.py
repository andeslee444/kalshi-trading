"""Tests for infrastructure: SIGUSR1 shutdown, webhook --require-secret, crypto vol reader."""

import json
import os
import signal
import sys
import tempfile
import types
import importlib.util
import logging
import pytest
import datetime
from unittest.mock import patch, MagicMock
from pathlib import Path

# conftest.py adds src/kalshi/ to sys.path
from kalshi_auth import is_shutdown_requested


# ===================================================================
# SIGUSR1 graceful shutdown tests
# ===================================================================

class TestSIGUSR1Shutdown:

    def setup_method(self):
        """Reset shutdown flag before each test."""
        import kalshi_auth
        kalshi_auth._shutdown_requested = False

    def teardown_method(self):
        import kalshi_auth
        kalshi_auth._shutdown_requested = False

    def test_is_shutdown_requested_initially_false(self):
        """Flag should be False by default."""
        assert is_shutdown_requested() is False

    def test_sigusr1_sets_flag(self):
        """Sending SIGUSR1 to the current process should set the flag."""
        import kalshi_auth
        kalshi_auth.setup_signal_handlers()
        os.kill(os.getpid(), signal.SIGUSR1)
        assert is_shutdown_requested() is True


# ===================================================================
# Webhook --require-secret tests
# ===================================================================

class TestWebhookRequireSecret:

    def test_require_secret_exits_when_missing(self):
        """--require-secret with no env var should exit with code 1."""
        # Import the refactored main() from the webhook script
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "github_webhook",
                str(Path(__file__).resolve().parent.parent / "scripts" / "github-webhook.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
                with pytest.raises(SystemExit) as exc_info:
                    mod.main(args=["--require-secret"])
                assert exc_info.value.code == 1
        finally:
            sys.path.pop(0)

    def test_no_flag_does_not_exit_without_secret(self):
        """Without --require-secret, missing secret should not exit."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "github_webhook2",
                str(Path(__file__).resolve().parent.parent / "scripts" / "github-webhook.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
                # Should NOT raise SystemExit, but will try to start server
                # We mock HTTPServer to prevent that
                with patch.object(mod, "HTTPServer") as mock_server:
                    mock_instance = mock_server.return_value
                    mock_instance.serve_forever.side_effect = KeyboardInterrupt
                    mock_instance.server_close = lambda: None
                    # Should not raise SystemExit
                    mod.main(args=[])
        finally:
            sys.path.pop(0)


# ===================================================================
# Crypto vol reader tests — calls real _get_latest_crypto_vol
# ===================================================================

_pm_module = None

def _load_pm():
    """Load position-monitor module once with stubbed dependencies."""
    global _pm_module
    if _pm_module is not None:
        return _pm_module

    orig_modules = {}
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "ticker_utils"):
        if mod_name in sys.modules:
            orig_modules[mod_name] = sys.modules[mod_name]

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.is_shutdown_requested = lambda: False
    fake_auth.setup_logging = lambda *a, **kw: logging.getLogger("test-infra")
    fake_auth.PROJECT_DIR = Path(tempfile.mkdtemp())
    fake_auth.TradeManager = type("TradeManager", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    fake_auth.trim_trade_log = lambda *a, **kw: None
    fake_auth.load_trades = lambda *a, **kw: []
    fake_auth._atomic_write_json = lambda *a, **kw: None
    fake_auth.CITY_TIMEZONES = {}
    fake_auth._local_today = lambda *a: "2026-03-04"
    fake_auth.round_half_up = lambda x: round(x)
    fake_auth.retry_request = MagicMock()
    fake_auth.fetch_parallel = MagicMock(return_value={})
    fake_auth.HealthCheckMonitor = type("HealthCheckMonitor", (), {
        "__init__": lambda self, *a, **kw: None,
        "record_bot_heartbeat": lambda self, *a, **kw: None,
        "check_health": lambda self, *a, **kw: [],
    })
    fake_auth.ScanSummary = MagicMock()
    fake_auth.notify_whatsapp = lambda *a, **kw: None
    sys.modules["kalshi_auth"] = fake_auth

    fake_prob = types.ModuleType("probability")
    fake_prob.weather_probability = MagicMock(return_value=0.5)
    fake_prob.nws_probability = MagicMock(return_value=0.5)
    fake_prob.half_kelly = MagicMock(return_value=(1, 50))
    fake_prob.kalshi_fee_cents = MagicMock(return_value=0)
    fake_prob.crypto_price_probability = MagicMock(return_value=0.5)
    sys.modules["probability"] = fake_prob

    fake_ticker = types.ModuleType("ticker_utils")
    fake_ticker.parse_weather_ticker = MagicMock(return_value=None)
    fake_ticker.parse_crypto_ticker = MagicMock(return_value=None)
    sys.modules["ticker_utils"] = fake_ticker

    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = MagicMock()
    sys.modules["capital_allocator"] = fake_alloc

    # Create dirs and config
    data_dir = fake_auth.PROJECT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir = fake_auth.PROJECT_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bots-config.json").write_text(json.dumps({"positionMonitor": {}}))
    (config_dir / "kalshi-config.json").write_text(json.dumps({"cities": {}}))

    spec = importlib.util.spec_from_file_location(
        "position_monitor_infra",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "position-monitor.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original modules
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "ticker_utils"):
        if mod_name in orig_modules:
            sys.modules[mod_name] = orig_modules[mod_name]
        elif mod_name in sys.modules:
            del sys.modules[mod_name]

    _pm_module = mod
    return mod


class TestGetLatestCryptoVol:

    @pytest.fixture(autouse=True)
    def setup_pm(self, tmp_path):
        """Load position-monitor and point PROJECT_DIR at tmp_path."""
        self.mod = _load_pm()
        self._orig_project_dir = self.mod.PROJECT_DIR
        self.mod.PROJECT_DIR = tmp_path
        (tmp_path / "data").mkdir(exist_ok=True)
        yield
        self.mod.PROJECT_DIR = self._orig_project_dir

    def _decisions_path(self):
        return self.mod.PROJECT_DIR / "data" / "kalshi-crypto-trades-decisions.json"

    def test_returns_none_when_no_file(self):
        """Should return None when decisions file doesn't exist."""
        assert not self._decisions_path().exists()
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result is None

    def test_returns_none_when_stale(self):
        """Vol data older than 4h should return None."""
        old_time = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(hours=5)).isoformat()
        decisions = [{"asset": "BTC", "vol_used": 0.55, "timestamp": old_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result is None

    def test_returns_vol_when_fresh(self):
        """Fresh vol data should be returned."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        decisions = [{"asset": "BTC", "vol_used": 0.62, "timestamp": fresh_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result == 0.62

    def test_returns_none_for_invalid_vol_type(self):
        """Non-numeric vol should return None."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        decisions = [{"asset": "BTC", "vol_used": "high", "timestamp": fresh_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result is None

    def test_returns_none_for_vol_out_of_range(self):
        """Vol outside (0, 5) should return None."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Zero vol
        decisions = [{"asset": "BTC", "vol_used": 0.0, "timestamp": fresh_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        assert self.mod._get_latest_crypto_vol("BTC") is None
        # Negative vol
        decisions = [{"asset": "BTC", "vol_used": -0.5, "timestamp": fresh_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        assert self.mod._get_latest_crypto_vol("BTC") is None
        # Extremely high vol
        decisions = [{"asset": "BTC", "vol_used": 6.0, "timestamp": fresh_time}]
        self._decisions_path().write_text(json.dumps(decisions))
        assert self.mod._get_latest_crypto_vol("BTC") is None

    def test_filters_by_asset(self):
        """Should only return vol for the requested asset."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        decisions = [
            {"asset": "ETH", "vol_used": 0.70, "timestamp": fresh_time},
            {"asset": "BTC", "vol_used": 0.45, "timestamp": fresh_time},
        ]
        self._decisions_path().write_text(json.dumps(decisions))
        assert self.mod._get_latest_crypto_vol("BTC") == 0.45
        assert self.mod._get_latest_crypto_vol("ETH") == 0.70

    def test_returns_most_recent_entry(self):
        """Should return vol from the most recent matching entry."""
        now = datetime.datetime.now(datetime.timezone.utc)
        old_time = (now - datetime.timedelta(hours=2)).isoformat()
        new_time = now.isoformat()
        decisions = [
            {"asset": "BTC", "vol_used": 0.40, "timestamp": old_time},
            {"asset": "BTC", "vol_used": 0.55, "timestamp": new_time},
        ]
        self._decisions_path().write_text(json.dumps(decisions))
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result == 0.55

    def test_handles_malformed_json(self):
        """Should return None on malformed JSON."""
        self._decisions_path().write_text("not valid json {{{")
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result is None

    def test_missing_timestamp_still_returns_vol(self):
        """Entry with no timestamp but valid vol should still return value."""
        decisions = [{"asset": "BTC", "vol_used": 0.50}]
        self._decisions_path().write_text(json.dumps(decisions))
        result = self.mod._get_latest_crypto_vol("BTC")
        assert result == 0.50
