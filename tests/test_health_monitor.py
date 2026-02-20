"""Tests for HealthCheckMonitor: staleness detection, auto-halt, source error counting.

Fix 5C: Validates the health monitoring infrastructure.
"""

import datetime
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from kalshi_auth import HealthCheckMonitor, KILL_SWITCH_PATH


class TestBotHeartbeat:
    """Test bot heartbeat recording and staleness detection."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_record_heartbeat(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        hm.record_bot_heartbeat("weather")
        assert "weather" in hm._state["bots"]
        assert "last_heartbeat" in hm._state["bots"]["weather"]

    def test_heartbeat_persisted(self, tmp_path):
        state_path = tmp_path / "health-state.json"
        hm = HealthCheckMonitor(state_path=str(state_path))
        hm.record_bot_heartbeat("crypto")

        # Load a new instance from the same state file
        hm2 = HealthCheckMonitor(state_path=str(state_path))
        assert "crypto" in hm2._state["bots"]

    def test_fresh_heartbeat_not_stale(self, tmp_path):
        hm = self._make_monitor(tmp_path, staleness_minutes=60)
        hm.record_bot_heartbeat("weather")
        issues = hm.check_health()
        stale_issues = [i for i in issues if "stale" in i]
        assert len(stale_issues) == 0

    def test_old_heartbeat_is_stale(self, tmp_path):
        hm = self._make_monitor(tmp_path, staleness_minutes=30)
        # Manually set an old heartbeat
        old_time = (datetime.datetime.now() - datetime.timedelta(minutes=60)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}
        hm._save()

        issues = hm.check_health()
        stale_issues = [i for i in issues if "stale" in i and "weather" in i]
        assert len(stale_issues) == 1

    def test_multiple_bots_tracked(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        hm.record_bot_heartbeat("weather")
        hm.record_bot_heartbeat("crypto")
        hm.record_bot_heartbeat("economics")
        assert len(hm._state["bots"]) == 3


class TestSourceTracking:
    """Test source success/error recording."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_record_source_success(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        hm.record_source_success("nws")
        entry = hm._state["sources"]["nws"]
        assert entry["last_success"] is not None
        assert entry["error_count"] == 0

    def test_record_source_error(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        hm.record_source_error("nws", "timeout")
        entry = hm._state["sources"]["nws"]
        assert entry["last_error"] is not None
        assert entry["error_count"] == 1

    def test_consecutive_errors_accumulate(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        for i in range(5):
            hm.record_source_error("nws", f"error {i}")
        assert hm._state["sources"]["nws"]["error_count"] == 5

    def test_success_resets_error_count(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        hm.record_source_error("nws", "err1")
        hm.record_source_error("nws", "err2")
        assert hm._state["sources"]["nws"]["error_count"] == 2
        hm.record_source_success("nws")
        assert hm._state["sources"]["nws"]["error_count"] == 0

    def test_five_errors_flagged_in_health(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        for i in range(5):
            hm.record_source_error("nws", f"error {i}")
        issues = hm.check_health()
        failing_issues = [i for i in issues if "failing" in i and "nws" in i]
        assert len(failing_issues) == 1

    def test_four_errors_not_flagged(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        for i in range(4):
            hm.record_source_error("nws", f"error {i}")
        issues = hm.check_health()
        failing_issues = [i for i in issues if "failing" in i]
        assert len(failing_issues) == 0


class TestAutoHalt:
    """Test auto-halt on critical failure."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_auto_halt_disabled_by_default(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        assert hm.auto_halt is False

    def test_auto_halt_creates_file_on_two_critical(self, tmp_path, monkeypatch):
        """Auto-halt should trigger when 2+ critical issues exist."""
        import kalshi_auth
        halt_path = tmp_path / "data" / "HALT_TRADING"
        monkeypatch.setattr(kalshi_auth, "KILL_SWITCH_PATH", halt_path)

        hm = self._make_monitor(tmp_path, auto_halt=True, staleness_minutes=10)

        # Create 2 critical issues: stale bot + failing source
        old_time = (datetime.datetime.now() - datetime.timedelta(minutes=30)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}
        for i in range(5):
            hm.record_source_error("nws", f"err{i}")

        issues = hm.check_health()
        assert halt_path.exists()
        halt_content = halt_path.read_text()
        assert "stale" in halt_content or "failing" in halt_content

    def test_no_auto_halt_on_single_issue(self, tmp_path, monkeypatch):
        """Single issue should not trigger auto-halt."""
        import kalshi_auth
        halt_path = tmp_path / "data" / "HALT_TRADING"
        monkeypatch.setattr(kalshi_auth, "KILL_SWITCH_PATH", halt_path)

        hm = self._make_monitor(tmp_path, auto_halt=True, staleness_minutes=10)

        # Only one issue: stale bot
        old_time = (datetime.datetime.now() - datetime.timedelta(minutes=30)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}

        issues = hm.check_health()
        assert not halt_path.exists()

    def test_auto_halt_idempotent(self, tmp_path, monkeypatch):
        """If HALT_TRADING already exists, don't overwrite."""
        import kalshi_auth
        halt_path = tmp_path / "data" / "HALT_TRADING"
        halt_path.parent.mkdir(parents=True, exist_ok=True)
        halt_path.write_text("Manual halt")
        monkeypatch.setattr(kalshi_auth, "KILL_SWITCH_PATH", halt_path)

        hm = self._make_monitor(tmp_path, auto_halt=True, staleness_minutes=10)
        old_time = (datetime.datetime.now() - datetime.timedelta(minutes=30)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}
        for i in range(5):
            hm.record_source_error("nws", f"err{i}")

        hm.check_health()
        # Original content preserved
        assert halt_path.read_text() == "Manual halt"


class TestHealthStateLoad:
    """Test loading from corrupt or missing state files."""

    def test_missing_state_file(self, tmp_path):
        state_path = tmp_path / "nonexistent.json"
        hm = HealthCheckMonitor(state_path=str(state_path))
        assert hm._state["sources"] == {}
        assert hm._state["bots"] == {}

    def test_corrupt_state_file(self, tmp_path):
        state_path = tmp_path / "corrupt.json"
        state_path.write_text("not valid json{{{")
        hm = HealthCheckMonitor(state_path=str(state_path))
        assert hm._state["sources"] == {}
        assert hm._state["bots"] == {}

    def test_custom_staleness_minutes(self, tmp_path):
        hm = HealthCheckMonitor(
            state_path=str(tmp_path / "h.json"),
            staleness_minutes=120,
        )
        assert hm.staleness_minutes == 120
