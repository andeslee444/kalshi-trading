"""Direct tests for the extracted ops.health_monitor helpers."""

import datetime
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from ops.health_monitor import HealthCheckMonitor


def _make_monitor(tmp_path, **kwargs):
    notifications = []
    monitor = HealthCheckMonitor(
        state_path=tmp_path / "health-state.json",
        logger=MagicMock(),
        notify_webhook_func=lambda *args, **kwargs: notifications.append(("webhook", args, kwargs)),
        notify_imessage_func=lambda *args, **kwargs: notifications.append(("imessage", args, kwargs)),
        per_bot_halt_path_func=lambda name: tmp_path / f"HALT_bot_{name}",
        bot_source_map={"weather": ["nws", "open-meteo"]},
        **kwargs,
    )
    return monitor, notifications


def test_record_bot_heartbeat_persists_state(tmp_path):
    monitor, _ = _make_monitor(tmp_path)

    monitor.record_bot_heartbeat("weather")

    data = json.loads((tmp_path / "health-state.json").read_text())
    assert data["bots"]["weather"]["last_heartbeat"]


def test_record_source_error_opens_breaker_and_notifies(tmp_path):
    monitor, notifications = _make_monitor(tmp_path, source_breaker_threshold=3)

    monitor.record_source_error("nws", "err1")
    monitor.record_source_error("nws", "err2")
    monitor.record_source_error("nws", "err3")

    entry = monitor._state["sources"]["nws"]
    assert entry["error_count"] == 3
    assert entry["opened_at"] is not None
    assert [item[0] for item in notifications] == ["webhook", "imessage"]


def test_get_summary_marks_stale_and_error(tmp_path):
    monitor, _ = _make_monitor(tmp_path, staleness_minutes=10, source_breaker_threshold=3)
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat()
    monitor._state["bots"]["weather"] = {"last_heartbeat": old_time}
    monitor._state["sources"]["nws"] = {
        "last_success": None,
        "last_error": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "error_count": 3,
        "opened_at": time.time(),
    }

    summary = monitor.get_summary()

    assert summary["bots"]["weather"]["status"] == "stale"
    assert summary["sources"]["nws"]["status"] == "error"
    assert summary["overall"] == "critical"


def test_check_per_bot_halts_creates_and_removes_halt_file(tmp_path):
    monitor, _ = _make_monitor(tmp_path, auto_halt=True, per_bot_halt_cooldown_seconds=0)
    for source in ("nws", "open-meteo"):
        monitor._state["sources"][source] = {
            "last_success": None,
            "last_error": None,
            "error_count": 5,
        }

    status = monitor.check_per_bot_halts()
    halt_path = tmp_path / "HALT_bot_weather"

    assert status["weather"] == "halted"
    assert halt_path.exists()

    for source in ("nws", "open-meteo"):
        monitor._state["sources"][source]["error_count"] = 0

    status = monitor.check_per_bot_halts()

    assert status["weather"] == "recovered"
    assert not halt_path.exists()
