"""Direct tests for the extracted ops.notifications helpers."""

import json
import threading
from unittest.mock import MagicMock

from ops.notifications import (
    _reset_imessage_rate_limiter,
    _reset_webhook_rate_limiter,
    notify_imessage,
    notify_webhook,
    notify_whatsapp,
)


def setup_function():
    _reset_webhook_rate_limiter()
    _reset_imessage_rate_limiter()


def test_notify_whatsapp_reads_phone_from_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "bots-config.json").write_text(json.dumps({"notificationPhone": "+15555550123"}))
    subprocess_module = MagicMock()
    subprocess_module.run.return_value = MagicMock(returncode=0, stderr="")

    result = notify_whatsapp("hello", project_dir=tmp_path, subprocess_module=subprocess_module, env={})

    assert result is True
    assert subprocess_module.run.call_args[0][0][4] == "+15555550123"


def test_notify_webhook_routes_to_whatsapp_and_rate_limits():
    subprocess_module = MagicMock()
    subprocess_module.run.return_value = MagicMock(returncode=0, stderr="")
    env = {"NOTIFICATION_PHONE": "+15555550123"}

    assert notify_webhook("test message", env=env, subprocess_module=subprocess_module) is True
    assert notify_webhook("test message", env=env, subprocess_module=subprocess_module) is False
    command = subprocess_module.run.call_args[0][0]
    assert command[4] == "+15555550123"
    assert command[6] == "ℹ️ [INFO] test message"


def test_notify_webhook_rate_limits_stale_heartbeat_messages_with_varying_age():
    subprocess_module = MagicMock()
    subprocess_module.run.return_value = MagicMock(returncode=0, stderr="")
    env = {"NOTIFICATION_PHONE": "+15555550123"}

    first = "Health check: bot/weather stale: last heartbeat 60min ago"
    second = "Health check: bot/weather stale: last heartbeat 61min ago"

    assert notify_webhook(first, level="warning", env=env, subprocess_module=subprocess_module) is True
    assert notify_webhook(second, level="warning", env=env, subprocess_module=subprocess_module) is False
    assert subprocess_module.run.call_count == 1


def _dispatch_and_join(message, **kwargs):
    before = set(threading.enumerate())
    result = notify_imessage(message, **kwargs)
    after = set(threading.enumerate())
    for thread in after - before:
        thread.join(timeout=5)
    return result


def test_notify_imessage_dispatches_background_request():
    subprocess_module = MagicMock()
    subprocess_module.run.return_value = MagicMock(returncode=0, stderr="")
    env = {"NOTIFICATION_PHONE": "+15555550123"}

    result = _dispatch_and_join("hello world", env=env, subprocess_module=subprocess_module)

    assert result is True
    command = subprocess_module.run.call_args[0][0]
    assert command[4] == "+15555550123"
    assert command[6] == "hello world"


def test_notify_imessage_returns_false_without_notification_target(tmp_path):
    assert notify_imessage("hello", env={}, project_dir=tmp_path) is False


def test_notify_imessage_rate_limits_stale_heartbeat_messages_with_varying_age():
    subprocess_module = MagicMock()
    subprocess_module.run.return_value = MagicMock(returncode=0, stderr="")
    env = {"NOTIFICATION_PHONE": "+15555550123"}

    first = "Health check: bot/weather stale: last heartbeat 60min ago"
    second = "Health check: bot/weather stale: last heartbeat 61min ago"

    assert _dispatch_and_join(first, env=env, subprocess_module=subprocess_module) is True
    assert notify_imessage(second, env=env, subprocess_module=subprocess_module) is False
    assert subprocess_module.run.call_count == 1
