"""Tests for dashboard /api/health endpoint.

The health endpoint reads health-state.json and regime-state.json from disk,
then returns a merged dict with sources, bots, and regime info.
"""

import json
import sys
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
