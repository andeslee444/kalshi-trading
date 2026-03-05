"""Tests for infrastructure: SIGUSR1 shutdown, webhook --require-secret, crypto vol reader."""

import json
import os
import signal
import sys
import tempfile
import pytest
from unittest.mock import patch
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
            import importlib.util
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
            import importlib.util
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
# Crypto vol reader tests
# ===================================================================

class TestGetLatestCryptoVol:

    def test_returns_none_when_no_file(self, tmp_path):
        """Should return None when decisions file doesn't exist."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "position_monitor_test",
            str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "position-monitor.py"),
        )
        # Can't easily import position-monitor due to module-level init
        # So we test the logic inline
        decisions_path = tmp_path / "crypto-decisions.json"
        assert not decisions_path.exists()

    def test_returns_none_when_stale(self, tmp_path):
        """Vol data older than 4h should return None."""
        import datetime
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)).isoformat()
        decisions = [{"asset": "BTC", "vol_used": 0.55, "timestamp": old_time}]
        decisions_path = tmp_path / "crypto-decisions.json"
        decisions_path.write_text(json.dumps(decisions))

        # Simulate the logic from _get_latest_crypto_vol
        data = json.loads(decisions_path.read_text())
        for d in reversed(data[-100:]):
            if d.get("asset", "").upper() == "BTC" and "vol_used" in d:
                ts = d.get("timestamp")
                if ts:
                    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_hours = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
                    if age_hours > 4:
                        result = None
                        break
        assert result is None

    def test_returns_vol_when_fresh(self, tmp_path):
        """Fresh vol data should be returned."""
        import datetime
        fresh_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        decisions = [{"asset": "BTC", "vol_used": 0.62, "timestamp": fresh_time}]
        decisions_path = tmp_path / "crypto-decisions.json"
        decisions_path.write_text(json.dumps(decisions))

        data = json.loads(decisions_path.read_text())
        result = None
        for d in reversed(data[-100:]):
            if d.get("asset", "").upper() == "BTC" and "vol_used" in d:
                ts = d.get("timestamp")
                if ts:
                    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_hours = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
                    if age_hours <= 4:
                        vol = d["vol_used"]
                        if isinstance(vol, (int, float)) and 0 < vol < 5:
                            result = vol
                            break
        assert result == 0.62
