"""Direct tests for the extracted ops.notifications helpers."""

import json
import threading
from unittest.mock import MagicMock

import requests

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


def test_notify_webhook_sends_slack_payload_and_rate_limits():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    requests_module = MagicMock()
    requests_module.post.return_value = response
    requests_module.exceptions = requests.exceptions
    env = {"ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}

    assert notify_webhook("test message", env=env, requests_module=requests_module) is True
    assert notify_webhook("test message", env=env, requests_module=requests_module) is False
    assert requests_module.post.call_args[1]["json"]["text"].endswith("test message")


def _dispatch_and_join(message, **kwargs):
    before = set(threading.enumerate())
    result = notify_imessage(message, **kwargs)
    after = set(threading.enumerate())
    for thread in after - before:
        thread.join(timeout=5)
    return result


def test_notify_imessage_dispatches_background_request():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    requests_module = MagicMock()
    requests_module.post.return_value = response
    requests_module.exceptions = requests.exceptions
    env = {
        "BLUEBUBBLES_URL": "http://localhost:1234",
        "BLUEBUBBLES_PASSWORD": "secret",
        "BLUEBUBBLES_CHAT_GUID": "iMessage;+;chat123",
    }

    result = _dispatch_and_join("hello world", env=env, requests_module=requests_module)

    assert result is True
    args, kwargs = requests_module.post.call_args
    assert args[0] == "http://localhost:1234/api/v1/message/text"
    assert kwargs["params"] == {"password": "secret"}
    assert kwargs["json"]["chatGuid"] == "iMessage;+;chat123"


def test_notify_imessage_returns_false_without_env():
    assert notify_imessage("hello", env={}) is False
