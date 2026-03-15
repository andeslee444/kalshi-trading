"""Notification helpers extracted from kalshi_auth."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parents[3]

_log = logging.getLogger("notifications")

_webhook_rate_limiter = {}
_WEBHOOK_COOLDOWN_SECONDS = 1800

_imessage_rate_limiter = {}
_IMESSAGE_COOLDOWN_SECONDS = 1800


def notify_whatsapp(
    message,
    phone=None,
    logger=None,
    *,
    project_dir=PROJECT_DIR,
    env=None,
    subprocess_module=subprocess,
):
    """Send a WhatsApp notification via openclaw CLI."""
    log = logger or logging.getLogger("notify")
    environ = os.environ if env is None else env
    resolved_phone = phone or environ.get("NOTIFICATION_PHONE", "")
    if not resolved_phone:
        try:
            cfg_path = Path(project_dir) / "config" / "bots-config.json"
            with open(cfg_path) as handle:
                cfg = json.load(handle)
            resolved_phone = cfg.get("notificationPhone", "")
        except Exception:
            pass
    if not resolved_phone:
        log.warning("No notificationPhone configured — notification logged only")
        return False
    try:
        result = subprocess_module.run(
            ["openclaw", "message", "send", "--to", resolved_phone,
             "--message", message, "--channel", "whatsapp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("WhatsApp notification sent")
            return True
        log.warning("WhatsApp send failed: %s", result.stderr[:200])
        return False
    except FileNotFoundError:
        log.warning("openclaw CLI not found — notification logged only")
        return False
    except Exception as exc:
        log.warning("WhatsApp error: %s", exc)
        return False


def _reset_webhook_rate_limiter():
    """Reset the webhook rate limiter (for testing)."""
    _webhook_rate_limiter.clear()


def notify_webhook(
    message,
    level="info",
    logger=None,
    *,
    env=None,
    requests_module=requests,
    time_func=time.time,
):
    """Send an alert to a Slack or Discord webhook."""
    log = logger or _log
    environ = os.environ if env is None else env
    url = environ.get("ALERT_WEBHOOK_URL", "")
    if not url:
        return False

    prefix = message[:80]
    now = time_func()
    last_sent = _webhook_rate_limiter.get(prefix, 0)
    if now - last_sent < _WEBHOOK_COOLDOWN_SECONDS:
        return False

    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
    full_message = f"{emoji} [{level.upper()}] {message}"
    payload = {"content": full_message} if "discord" in url.lower() else {"text": full_message}

    try:
        response = requests_module.post(url, json=payload, timeout=10)
        response.raise_for_status()
        _webhook_rate_limiter[prefix] = now
        log.info("Webhook alert sent: %s", message[:100])
        return True
    except requests_module.exceptions.ConnectionError:
        log.warning("Webhook connection error — alert not delivered")
        return False
    except requests_module.exceptions.HTTPError as exc:
        log.warning(
            "Webhook HTTP error %s — alert not delivered",
            exc.response.status_code if exc.response else "?",
        )
        return False
    except Exception as exc:
        log.warning("Webhook error: %s", exc)
        return False


def _reset_imessage_rate_limiter():
    """Reset the iMessage rate limiter (for testing)."""
    _imessage_rate_limiter.clear()


def _send_imessage_blocking(message, prefix, logger, *, env=None, requests_module=requests, time_func=time.time):
    """Blocking iMessage send (runs in background thread)."""
    log = logger or _log
    environ = os.environ if env is None else env
    bb_url = environ.get("BLUEBUBBLES_URL", "")
    bb_password = environ.get("BLUEBUBBLES_PASSWORD", "")
    bb_chat = environ.get("BLUEBUBBLES_CHAT_GUID", "")
    try:
        response = requests_module.post(
            f"{bb_url}/api/v1/message/text",
            params={"password": bb_password},
            json={"chatGuid": bb_chat, "message": message},
            timeout=10,
        )
        response.raise_for_status()
        _imessage_rate_limiter[prefix] = time_func()
        log.info("iMessage sent: %s", prefix)
    except Exception as exc:
        log.warning("iMessage send failed: %s", exc)


def notify_imessage(
    message,
    logger=None,
    *,
    env=None,
    requests_module=requests,
    threading_module=threading,
    time_func=time.time,
):
    """Send an iMessage via BlueBubbles API (fire-and-forget)."""
    environ = os.environ if env is None else env
    bb_url = environ.get("BLUEBUBBLES_URL", "")
    bb_password = environ.get("BLUEBUBBLES_PASSWORD", "")
    bb_chat = environ.get("BLUEBUBBLES_CHAT_GUID", "")
    if not bb_url or not bb_password or not bb_chat:
        return False

    prefix = message[:80]
    now = time_func()
    if now - _imessage_rate_limiter.get(prefix, 0) < _IMESSAGE_COOLDOWN_SECONDS:
        return False

    _imessage_rate_limiter[prefix] = now
    thread = threading_module.Thread(
        target=_send_imessage_blocking,
        args=(message, prefix, logger),
        kwargs={"env": environ, "requests_module": requests_module, "time_func": time_func},
        daemon=True,
    )
    thread.start()
    return True


__all__ = [
    "_reset_imessage_rate_limiter",
    "_reset_webhook_rate_limiter",
    "notify_imessage",
    "notify_webhook",
    "notify_whatsapp",
]
