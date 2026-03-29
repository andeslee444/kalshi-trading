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


def test_record_bot_heartbeat_preserves_existing_bot_state(tmp_path):
    monitor, _ = _make_monitor(tmp_path)
    monitor._state["bots"]["oracle"] = {
        "last_heartbeat": "2026-03-22T00:00:00+00:00",
        "scan_metrics": {
            "kalshi_markets": 12,
            "signals_a": 1,
            "signals_b": 0,
            "signals_c": 0,
            "total_signals": 1,
            "trades_executed": 0,
            "open_positions": 0,
            "daily_pnl_cents": 0,
        },
        "zero_signal_streak": 2,
    }

    monitor.record_bot_heartbeat("oracle")

    data = json.loads((tmp_path / "health-state.json").read_text())
    assert data["bots"]["oracle"]["scan_metrics"]["kalshi_markets"] == 12
    assert data["bots"]["oracle"]["zero_signal_streak"] == 2
    assert data["bots"]["oracle"]["last_heartbeat"] != "2026-03-22T00:00:00+00:00"


def test_record_source_error_opens_breaker_and_notifies(tmp_path):
    monitor, notifications = _make_monitor(tmp_path, source_breaker_threshold=3)

    monitor.record_source_error("nws", "err1")
    monitor.record_source_error("nws", "err2")
    monitor.record_source_error("nws", "err3")

    entry = monitor._state["sources"]["nws"]
    assert entry["error_count"] == 3
    assert entry["last_error_message"] == "err3"
    assert entry["opened_at"] is not None
    assert [item[0] for item in notifications] == ["webhook", "imessage"]


def test_get_summary_marks_stale_and_error(tmp_path):
    monitor, _ = _make_monitor(tmp_path, staleness_minutes=10, source_breaker_threshold=3)
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat()
    monitor._state["bots"]["weather"] = {"last_heartbeat": old_time}
    monitor._state["sources"]["nws"] = {
        "last_success": None,
        "last_error": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_error_message": "timeout",
        "error_count": 3,
        "opened_at": time.time(),
    }

    summary = monitor.get_summary()

    assert summary["bots"]["weather"]["status"] == "stale"
    assert summary["sources"]["nws"]["status"] == "error"
    assert summary["sources"]["nws"]["last_error_message"] == "timeout"
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


def test_record_source_warning_notifies_and_marks_summary_warning(tmp_path):
    monitor, notifications = _make_monitor(tmp_path)

    monitor.record_source_warning("oracle-book-c-zero-signals", "3 scans without a live signal")

    assert monitor._state["sources"]["oracle-book-c-zero-signals"]["error_count"] == 1
    summary = monitor.get_summary()
    assert summary["sources"]["oracle-book-c-zero-signals"]["status"] == "warning"
    assert any(item[0] == "webhook" for item in notifications)
    assert any(item[0] == "imessage" for item in notifications)


def test_record_source_warning_rearms_after_success(tmp_path):
    monitor, notifications = _make_monitor(tmp_path)

    monitor.record_source_warning("oracle-book-c-zero-signals", "first")
    monitor.record_source_success("oracle-book-c-zero-signals")
    monitor.record_source_warning("oracle-book-c-zero-signals", "second")

    webhook_count = sum(1 for kind, *_ in notifications if kind == "webhook")
    imessage_count = sum(1 for kind, *_ in notifications if kind == "imessage")
    assert webhook_count == 2
    assert imessage_count == 2


def test_update_bot_state_persists_scan_metrics(tmp_path):
    monitor, _ = _make_monitor(tmp_path)

    monitor.update_bot_state(
        "oracle",
        scan_metrics={
            "kalshi_markets": 12,
            "signals_a": 0,
            "signals_b": 0,
            "signals_c": 1,
            "total_signals": 1,
            "book_c": {"live_games": 2},
        },
    )

    data = json.loads((tmp_path / "health-state.json").read_text())
    assert data["bots"]["oracle"]["scan_metrics"]["total_signals"] == 1
    assert data["bots"]["oracle"]["scan_metrics"]["book_c"]["live_games"] == 2


def test_summary_includes_ignored_bot_when_scan_metrics_exist(tmp_path):
    monitor, _ = _make_monitor(tmp_path, ignored_bot_names={"oracle"})

    monitor.update_bot_state(
        "oracle",
        last_heartbeat=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        scan_metrics={"total_signals": 0, "book_c": {"live_games": 1}},
    )

    summary = monitor.get_summary()
    assert "oracle" in summary["bots"]
    assert summary["bots"]["oracle"]["scan_metrics"]["book_c"]["live_games"] == 1
