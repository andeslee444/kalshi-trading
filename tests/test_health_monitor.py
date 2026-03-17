"""Tests for HealthCheckMonitor: staleness detection, auto-halt, source error counting.

Fix 5C: Validates the health monitoring infrastructure.
"""

import datetime
import json
import tempfile
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from artifact_contracts import (
    HEALTH_STATE_ARTIFACT,
    HEALTH_SUMMARY_BOT_FIELDS,
    HEALTH_SUMMARY_REQUIRED_FIELDS,
    HEALTH_SUMMARY_SOURCE_FIELDS,
)
from kalshi_auth import (
    HealthCheckMonitor, KILL_SWITCH_PATH, BOT_SOURCE_MAP,
    notify_imessage, _reset_imessage_rate_limiter,
)


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

    def test_health_state_includes_schema_metadata(self, tmp_path):
        state_path = tmp_path / "health-state.json"
        hm = HealthCheckMonitor(state_path=str(state_path))
        hm.record_bot_heartbeat("weather")

        data = json.loads(state_path.read_text())
        assert data["artifact_type"] == HEALTH_STATE_ARTIFACT
        assert data["schema_version"] == 1

    def test_fresh_heartbeat_not_stale(self, tmp_path):
        hm = self._make_monitor(tmp_path, staleness_minutes=60)
        hm.record_bot_heartbeat("weather")
        issues = hm.check_health()
        stale_issues = [i for i in issues if "stale" in i]
        assert len(stale_issues) == 0

    def test_old_heartbeat_is_stale(self, tmp_path):
        hm = self._make_monitor(tmp_path, staleness_minutes=30)
        # Manually set an old heartbeat
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
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

    def test_disabled_bots_are_ignored_in_health_checks(self, tmp_path):
        hm = self._make_monitor(
            tmp_path,
            staleness_minutes=30,
            ignored_bot_names={"hdd-monitor", "cross-platform-arb"},
        )
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}
        hm._state["bots"]["hdd-monitor"] = {"last_heartbeat": old_time}
        hm._state["bots"]["cross-platform-arb"] = {"last_heartbeat": old_time}

        issues = hm.check_health()

        assert any("bot/weather stale" in issue for issue in issues)
        assert not any("bot/hdd-monitor stale" in issue for issue in issues)
        assert not any("bot/cross-platform-arb stale" in issue for issue in issues)


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
        assert entry["last_error_message"] == "timeout"
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
        assert hm._state["sources"]["nws"]["last_error_message"] is None

    @patch("kalshi_auth.notify_imessage")
    @patch("kalshi_auth.notify_webhook")
    def test_trip_source_breaker_opens_immediately(self, mock_webhook, mock_imessage, tmp_path):
        hm = self._make_monitor(tmp_path, source_breaker_threshold=5)
        hm.trip_source_breaker("open-meteo-nam", "status=400")
        entry = hm._state["sources"]["open-meteo-nam"]
        assert entry["error_count"] == 5
        assert entry["opened_at"] is not None
        mock_webhook.assert_called_once()
        mock_imessage.assert_called_once()

    @patch("kalshi_auth.notify_imessage")
    @patch("kalshi_auth.notify_webhook")
    def test_trip_source_breaker_includes_msg_in_alert(self, mock_webhook, mock_imessage, tmp_path):
        """trip_source_breaker() should include the msg argument in the alert."""
        hm = self._make_monitor(tmp_path, source_breaker_threshold=5)
        hm.trip_source_breaker("open-meteo-batch", msg="HTTP 400 Bad Request")
        alert_text = mock_webhook.call_args[0][0]
        assert "HTTP 400 Bad Request" in alert_text

    @patch("kalshi_auth.notify_imessage")
    @patch("kalshi_auth.notify_webhook")
    def test_trip_source_breaker_alerts_before_save(self, mock_webhook, mock_imessage, tmp_path):
        """Alerts must fire before state is saved (if save crashes, alert still sent)."""
        hm = self._make_monitor(tmp_path, source_breaker_threshold=5)
        call_order = []
        original_save = hm._save
        def tracking_save():
            call_order.append("save")
            original_save()
        hm._save = tracking_save
        mock_webhook.side_effect = lambda *a, **kw: call_order.append("webhook")
        mock_imessage.side_effect = lambda *a, **kw: call_order.append("imessage")

        hm.trip_source_breaker("test-source")
        # Alert should happen before save
        assert call_order.index("webhook") < call_order.index("save")

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

    def test_stale_source_errors_are_not_reported_forever(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
        hm._state["sources"]["open-meteo"] = {
            "last_success": None,
            "last_error": old_time,
            "error_count": 1870,
            "opened_at": None,
        }
        issues = hm.check_health()
        assert not any("source/open-meteo failing" in issue for issue in issues)


class TestAutoHalt:
    """Test auto-halt on critical failure."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_auto_halt_disabled_by_default(self, tmp_path):
        hm = self._make_monitor(tmp_path)
        assert hm.auto_halt is False

    def test_auto_halt_creates_per_bot_halts_on_critical(self, tmp_path, monkeypatch):
        """Auto-halt should create per-bot halt files (not global HALT_TRADING)."""
        import kalshi_auth
        halt_path = tmp_path / "data" / "HALT_TRADING"
        monkeypatch.setattr(kalshi_auth, "KILL_SWITCH_PATH", halt_path)

        hm = self._make_monitor(tmp_path, auto_halt=True, staleness_minutes=10)

        # Create critical source failures for all weather sources
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat()
        hm._state["bots"]["weather"] = {"last_heartbeat": old_time}
        for src in BOT_SOURCE_MAP["weather"]:
            for i in range(5):
                hm.record_source_error(src, f"err{i}")

        # Redirect per-bot halt paths to tmp_path
        monkeypatch.setattr(kalshi_auth, "per_bot_halt_path",
                            lambda name: tmp_path / "data" / f"HALT_bot_{name}")

        issues = hm.check_health()
        # Global halt should NOT be created
        assert not halt_path.exists()
        # Per-bot halt for weather should be created
        assert (tmp_path / "data" / "HALT_bot_weather").exists()
        # Issue list should mention per-bot halt
        assert any("PER-BOT-HALT" in i for i in issues)

    def test_no_auto_halt_on_single_issue(self, tmp_path, monkeypatch):
        """Single issue should not trigger auto-halt."""
        import kalshi_auth
        halt_path = tmp_path / "data" / "HALT_TRADING"
        monkeypatch.setattr(kalshi_auth, "KILL_SWITCH_PATH", halt_path)

        hm = self._make_monitor(tmp_path, auto_halt=True, staleness_minutes=10)

        # Only one issue: stale bot
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat()
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
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat()
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


class TestAlertDeduplication:
    """Test that duplicate alerts are suppressed within cooldown window."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_first_alert_not_suppressed(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        assert mon.should_send_alert("source_stale:hdd") is True

    def test_duplicate_alert_suppressed(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        mon.record_alert_sent("source_stale:hdd")
        assert mon.should_send_alert("source_stale:hdd") is False

    def test_alert_allowed_after_cooldown(self, tmp_path):
        mon = self._make_monitor(tmp_path, alert_cooldown_minutes=0)
        mon.record_alert_sent("source_stale:hdd")
        # With 0-minute cooldown, should be allowed immediately
        assert mon.should_send_alert("source_stale:hdd") is True


class TestHealthSummary:
    """Test health summary for dashboard endpoint."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_summary_includes_all_sources(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        mon.record_source_success("hdd")
        mon.record_source_success("nws")
        for _ in range(5):
            mon.record_source_error("boxoffice", "timeout")
        summary = mon.get_summary()
        assert set(HEALTH_SUMMARY_REQUIRED_FIELDS).issubset(summary)
        assert "hdd" in summary["sources"]
        assert "nws" in summary["sources"]
        assert "boxoffice" in summary["sources"]
        assert set(HEALTH_SUMMARY_SOURCE_FIELDS).issubset(summary["sources"]["hdd"])
        assert summary["sources"]["hdd"]["status"] == "ok"
        assert summary["sources"]["boxoffice"]["status"] == "error"

    def test_summary_includes_bots(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        mon.record_bot_heartbeat("weather")
        summary = mon.get_summary()
        assert "weather" in summary["bots"]
        assert set(HEALTH_SUMMARY_BOT_FIELDS).issubset(summary["bots"]["weather"])

    def test_summary_omits_ignored_bots(self, tmp_path):
        mon = self._make_monitor(tmp_path, ignored_bot_names={"hdd-monitor"})
        mon.record_bot_heartbeat("weather")
        mon.record_bot_heartbeat("hdd-monitor")

        summary = mon.get_summary()

        assert "weather" in summary["bots"]
        assert "hdd-monitor" not in summary["bots"]

    def test_summary_overall_healthy(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        mon.record_source_success("hdd")
        mon.record_bot_heartbeat("weather")
        summary = mon.get_summary()
        assert summary["overall"] == "healthy"

    def test_summary_overall_degraded_on_source_errors(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        for _ in range(5):
            mon.record_source_error("hdd", "timeout")
        summary = mon.get_summary()
        assert summary["overall"] in ("degraded", "critical")


class TestIsSourceOpen:
    """Test source circuit breaker via is_source_open."""

    def _make_monitor(self, tmp_path, **kwargs):
        state_path = tmp_path / "health-state.json"
        return HealthCheckMonitor(state_path=str(state_path), **kwargs)

    def test_below_threshold_returns_false(self, tmp_path):
        mon = self._make_monitor(tmp_path, source_breaker_threshold=5)
        for _ in range(4):
            mon.record_source_error("open-meteo", "err")
        assert mon.is_source_open("open-meteo") is False

    def test_at_threshold_returns_true(self, tmp_path):
        mon = self._make_monitor(tmp_path, source_breaker_threshold=5)
        for _ in range(5):
            mon.record_source_error("open-meteo", "err")
        assert mon.is_source_open("open-meteo") is True

    def test_resets_after_cooldown(self, tmp_path):
        mon = self._make_monitor(tmp_path, source_breaker_threshold=3, source_breaker_cooldown_seconds=1)
        for _ in range(3):
            mon.record_source_error("coinbase", "err")
        assert mon.is_source_open("coinbase") is True
        # Simulate cooldown elapsed by backdating opened_at
        mon._state["sources"]["coinbase"]["opened_at"] = time.time() - 2
        assert mon.is_source_open("coinbase") is False
        # After reset, error_count should be 0 (half-open)
        assert mon._state["sources"]["coinbase"]["error_count"] == 0

    def test_record_source_success_clears_breaker(self, tmp_path):
        mon = self._make_monitor(tmp_path, source_breaker_threshold=3)
        for _ in range(3):
            mon.record_source_error("deribit", "err")
        assert mon.is_source_open("deribit") is True
        mon.record_source_success("deribit")
        assert mon.is_source_open("deribit") is False
        assert mon._state["sources"]["deribit"]["error_count"] == 0
        assert mon._state["sources"]["deribit"]["opened_at"] is None

    def test_opened_at_persists_in_state(self, tmp_path):
        state_path = tmp_path / "health-state.json"
        mon = HealthCheckMonitor(state_path=str(state_path), source_breaker_threshold=3)
        for _ in range(3):
            mon.record_source_error("hdd", "err")
        assert mon._state["sources"]["hdd"].get("opened_at") is not None

        # Reload from disk
        mon2 = HealthCheckMonitor(state_path=str(state_path), source_breaker_threshold=3)
        assert mon2._state["sources"]["hdd"].get("opened_at") is not None
        assert mon2.is_source_open("hdd") is True

    def test_unknown_source_returns_false(self, tmp_path):
        mon = self._make_monitor(tmp_path)
        assert mon.is_source_open("nonexistent") is False


class TestNotifyImessage:
    """Test iMessage notification via BlueBubbles API."""

    def setup_method(self):
        _reset_imessage_rate_limiter()

    def test_returns_false_when_env_vars_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            assert notify_imessage("test message") is False

    def _dispatch_and_join(self, message):
        """Call notify_imessage and join the background thread so assertions are safe."""
        import threading
        before = set(threading.enumerate())
        result = notify_imessage(message)
        after = set(threading.enumerate())
        for t in after - before:
            t.join(timeout=5)
        return result

    def test_sends_post_to_correct_url(self):
        env = {
            "BLUEBUBBLES_URL": "http://localhost:1234",
            "BLUEBUBBLES_PASSWORD": "secret",
            "BLUEBUBBLES_CHAT_GUID": "iMessage;+;chat123",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("kalshi_auth.requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                mock_post.return_value.raise_for_status = MagicMock()
                result = self._dispatch_and_join("hello world")
                assert result is True
                mock_post.assert_called_once()
                args, kwargs = mock_post.call_args
                assert args[0] == "http://localhost:1234/api/v1/message/text"
                assert kwargs["params"] == {"password": "secret"}
                assert kwargs["json"]["chatGuid"] == "iMessage;+;chat123"
                assert kwargs["json"]["message"] == "hello world"

    def test_rate_limits_duplicate_messages(self):
        env = {
            "BLUEBUBBLES_URL": "http://localhost:1234",
            "BLUEBUBBLES_PASSWORD": "secret",
            "BLUEBUBBLES_CHAT_GUID": "iMessage;+;chat123",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("kalshi_auth.requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                mock_post.return_value.raise_for_status = MagicMock()
                assert self._dispatch_and_join("duplicate msg") is True
                assert notify_imessage("duplicate msg") is False
                assert mock_post.call_count == 1

    def test_http_error_does_not_crash(self):
        """HTTP errors are swallowed in the background thread (fire-and-forget)."""
        env = {
            "BLUEBUBBLES_URL": "http://localhost:1234",
            "BLUEBUBBLES_PASSWORD": "secret",
            "BLUEBUBBLES_CHAT_GUID": "iMessage;+;chat123",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch("kalshi_auth.requests.post") as mock_post:
                mock_post.return_value = MagicMock()
                mock_post.return_value.raise_for_status.side_effect = Exception("500 error")
                # Dispatches True (fire-and-forget), error handled in thread
                assert self._dispatch_and_join("fail msg") is True
                mock_post.assert_called_once()
