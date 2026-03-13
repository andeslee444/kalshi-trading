"""Tests for dashboard /api/health endpoint.

The health endpoint reads health-state.json and regime-state.json from disk,
then returns a merged dict with sources, bots, and regime info.
"""

import json
import sys
import time
import datetime
import importlib
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

httpx = pytest.importorskip("httpx", reason="Dashboard tests require httpx: pip install httpx")


# ---------------------------------------------------------------------------
# Module loading helper
# ---------------------------------------------------------------------------

_dashboard_mod = None


def _load_dashboard():
    """Load scripts/dashboard.py, reusing cached module if already loaded."""
    global _dashboard_mod
    if _dashboard_mod is not None:
        return _dashboard_mod

    dashboard_path = Path(__file__).resolve().parent.parent / "scripts" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("dashboard", str(dashboard_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _dashboard_mod = mod
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dashboard():
    """Return the loaded dashboard module."""
    return _load_dashboard()


@pytest.fixture
def client(dashboard):
    """Return a FastAPI TestClient for the dashboard app."""
    from fastapi.testclient import TestClient
    return TestClient(dashboard.app)


# ---------------------------------------------------------------------------
# Tests for /api/health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Test /api/health endpoint returns structured health data."""

    def test_health_returns_dict_with_sources_and_bots(self, client, dashboard, tmp_path):
        """Health endpoint returns health-state.json contents with regime info."""
        health_state = {
            "sources": {
                "hdd": {"status": "ok", "last_check": "2026-03-03T10:00:00Z"},
                "nws": {"status": "ok", "last_check": "2026-03-03T10:05:00Z"},
            },
            "bots": {
                "weather": {"last_heartbeat": "2026-03-03T10:00:00Z", "error_count": 0},
                "crypto": {"last_heartbeat": "2026-03-03T10:01:00Z", "error_count": 1},
            },
        }

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=FileNotFoundError):
                resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert "bots" in data
        assert "regime" in data
        assert data["sources"]["hdd"]["status"] == "ok"
        assert data["bots"]["weather"]["error_count"] == 0

    def test_health_returns_empty_when_no_state_file(self, client, dashboard):
        """When health-state.json is missing, returns empty sources/bots."""
        with patch.object(dashboard, "load_json_safe", return_value=None):
            with patch("builtins.open", side_effect=FileNotFoundError):
                resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["sources"] == {}
        assert data["bots"] == {}
        assert data["regime"]["regime"] == "unknown"

    def test_health_includes_regime_info(self, client, dashboard):
        """When regime-state.json exists, regime data is included."""
        health_state = {"sources": {}, "bots": {}}
        regime_state = {
            "belief": [0.1, 0.6, 0.2, 0.1],
            "n_updates": 42,
        }

        def mock_open_fn(path, *args, **kwargs):
            """Simulate reading regime-state.json."""
            from io import StringIO
            return StringIO(json.dumps(regime_state))

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=mock_open_fn):
                resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.json()
        regime = data["regime"]
        assert regime["regime"] == "normal"
        assert regime["confidence"] == 0.6
        assert regime["n_updates"] == 42
        assert "belief" in regime
        assert regime["belief"]["normal"] == 0.6

    def test_health_regime_crisis_state(self, client, dashboard):
        """Regime detector in crisis state is reported correctly."""
        health_state = {"sources": {}, "bots": {}}
        regime_state = {
            "belief": [0.05, 0.05, 0.1, 0.8],
            "n_updates": 100,
        }

        def mock_open_fn(path, *args, **kwargs):
            from io import StringIO
            return StringIO(json.dumps(regime_state))

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=mock_open_fn):
                resp = client.get("/api/health")

        data = resp.json()
        assert data["regime"]["regime"] == "crisis"
        assert data["regime"]["confidence"] == 0.8

    def test_health_regime_low_vol_state(self, client, dashboard):
        """Regime detector in low_vol state is reported correctly."""
        health_state = {"sources": {}, "bots": {}}
        regime_state = {
            "belief": [0.7, 0.1, 0.1, 0.1],
            "n_updates": 5,
        }

        def mock_open_fn(path, *args, **kwargs):
            from io import StringIO
            return StringIO(json.dumps(regime_state))

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=mock_open_fn):
                resp = client.get("/api/health")

        data = resp.json()
        assert data["regime"]["regime"] == "low_vol"
        assert data["regime"]["confidence"] == 0.7
        assert data["regime"]["n_updates"] == 5

    def test_health_regime_malformed_json(self, client, dashboard):
        """When regime-state.json has invalid JSON, falls back to unknown."""
        health_state = {"sources": {"hdd": {"status": "stale"}}, "bots": {}}

        def mock_open_fn(path, *args, **kwargs):
            from io import StringIO
            return StringIO("not valid json{{{")

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=mock_open_fn):
                resp = client.get("/api/health")

        data = resp.json()
        assert data["regime"]["regime"] == "unknown"
        assert data["sources"]["hdd"]["status"] == "stale"

    def test_health_preserves_all_health_state_keys(self, client, dashboard):
        """All keys from health-state.json are passed through, plus regime is added."""
        health_state = {
            "sources": {"nws": {"status": "ok"}},
            "bots": {"weather": {"status": "running"}},
            "custom_key": "preserved",
        }

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch("builtins.open", side_effect=FileNotFoundError):
                resp = client.get("/api/health")

        data = resp.json()
        assert data["custom_key"] == "preserved"
        assert "regime" in data

    def test_health_includes_weather_actual_source_mix(self, client, dashboard):
        """Recent weather actual-source mix is exposed for operator visibility."""
        health_state = {"sources": {}, "bots": {}}
        weather_actuals = {
            "summary_7d": {
                "lookback_days": 7,
                "total": 4,
                "counts": {"nws_cli": 3, "iem_fallback": 1},
                "shares": {"nws_cli": 0.75, "iem_fallback": 0.25},
            },
            "summary_30d": {
                "lookback_days": 30,
                "total": 10,
                "counts": {"nws_cli": 9, "iem_fallback": 1},
                "shares": {"nws_cli": 0.9, "iem_fallback": 0.1},
            },
        }

        with patch.object(dashboard, "load_json_safe", return_value=health_state):
            with patch.object(dashboard, "get_weather_actual_source_summary", return_value=weather_actuals):
                with patch("builtins.open", side_effect=FileNotFoundError):
                    resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["weather_actuals"]["summary_7d"]["counts"]["nws_cli"] == 3
        assert data["weather_actuals"]["summary_30d"]["shares"]["iem_fallback"] == 0.1


class TestBotsEndpoint:
    """Test /api/bots endpoint reflects supervisor-style bot state."""

    def test_bots_endpoint_uses_mapped_heartbeat_names_and_optional_bots(self, client, dashboard):
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = now.isoformat()
        stale = (now - datetime.timedelta(minutes=40)).isoformat()
        health_state = {
            "bots": {
                "position-monitor": {"last_heartbeat": fresh, "error_count": 1},
                "source-monitor": {"last_heartbeat": stale, "error_count": 2},
                "cross-platform-arb": {"last_heartbeat": fresh, "error_count": 3},
                "beatrelease": {"last_heartbeat": fresh, "error_count": 0},
                "hdd-monitor": {"last_heartbeat": fresh, "error_count": 0},
            }
        }
        supervisor_state = {
            "positions": {"restart_count": 4, "started_at": time.time() - 3600},
            "monitor": {"restart_count": 2, "started_at": time.time() - 7200},
            "beatrelease": {"restart_count": 3, "started_at": time.time() - 10800},
        }
        bots_config = {
            "position_monitor": {"scanIntervalMinutes": 15},
            "cross_platform_arb": {"enabled": False, "scanIntervalMinutes": 10},
            "beatrelease": {"enabled": True, "checkIntervalHours": 1},
            "hdd_monitor": {"enabled": False, "scanIntervalMinutes": 15},
        }
        weather_config = {}

        def fake_load_json(path_obj):
            if path_obj == dashboard.HEALTH_STATE_PATH:
                return health_state
            if path_obj == dashboard.SUPERVISOR_STATE_PATH:
                return supervisor_state
            if path_obj == dashboard.BOTS_CONFIG_PATH:
                return bots_config
            if path_obj == dashboard.WEATHER_CONFIG_PATH:
                return weather_config
            return None

        running = {
            "positions": (True, 111),
            "monitor": (True, 222),
            "beatrelease": (True, 333),
        }

        with patch.object(dashboard, "load_json_safe", side_effect=fake_load_json), \
             patch.object(dashboard, "is_bot_running", side_effect=lambda name: running.get(name, (False, None))):
            resp = client.get("/api/bots")

        assert resp.status_code == 200
        data = {row["name"]: row for row in resp.json()}

        assert "beatrelease" in data
        assert "hdd-monitor" in data
        assert data["positions"]["pid"] == 111
        assert data["positions"]["restart_count"] == 4
        assert data["positions"]["heartbeat_stale"] is False
        assert data["monitor"]["scan_interval_min"] == 10
        assert data["monitor"]["status"] == "stale"
        assert data["monitor"]["restart_count"] == 2
        assert data["monitor"]["heartbeat_stale"] is True
        assert data["monitor"]["heartbeat_age_min"] > 25
        assert data["arb"]["status"] == "disabled"
        assert data["beatrelease"]["display_name"] == "Beat Release"
        assert data["beatrelease"]["scan_interval_min"] == 60
        assert data["beatrelease"]["restart_count"] == 3
        assert data["hdd-monitor"]["display_name"] == "HDD Monitor"
        assert data["hdd-monitor"]["status"] == "disabled"
        assert data["hdd-monitor"]["scan_interval_min"] == 15
        assert data["hdd-monitor"]["restart_count"] == 0

    def test_is_bot_running_treats_permission_denied_as_running(self, dashboard, tmp_path):
        pid_dir = tmp_path / "pids"
        pid_dir.mkdir()
        (pid_dir / "weather.pid").write_text("12345")

        with patch.object(dashboard, "PID_DIR", pid_dir),              patch.object(dashboard.os, "kill", side_effect=PermissionError):
            running, pid = dashboard.is_bot_running("weather")

        assert running is True
        assert pid == 12345
