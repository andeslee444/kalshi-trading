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

# Keep webhook/iMessage compatibility entry points on a shared limiter so
# callers that still invoke both paths do not generate duplicate WhatsApp
# alerts.
_alert_rate_limiter = {}
_webhook_rate_limiter = _alert_rate_limiter
_WEBHOOK_COOLDOWN_SECONDS = 1800

_imessage_rate_limiter = _alert_rate_limiter
_IMESSAGE_COOLDOWN_SECONDS = 1800


def _resolve_notification_phone(project_dir, env):
    """Resolve the WhatsApp destination from env or repo config."""
    resolved_phone = env.get("NOTIFICATION_PHONE", "").strip()
    if resolved_phone:
        return resolved_phone
    try:
        cfg_path = Path(project_dir) / "config" / "bots-config.json"
        with open(cfg_path) as handle:
            cfg = json.load(handle)
        return str(cfg.get("notificationPhone", "")).strip()
    except Exception:
        return ""


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
    resolved_phone = phone or _resolve_notification_phone(project_dir, environ)
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
    project_dir=PROJECT_DIR,
    env=None,
    requests_module=requests,
    subprocess_module=subprocess,
    time_func=time.time,
):
    """Legacy webhook entry point routed through WhatsApp."""
    log = logger or _log
    environ = os.environ if env is None else env

    prefix = message[:80]
    now = time_func()
    last_sent = _webhook_rate_limiter.get(prefix, 0)
    if now - last_sent < _WEBHOOK_COOLDOWN_SECONDS:
        return False

    _ = requests_module  # Kept for backward-compatible call signatures.
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
    full_message = f"{emoji} [{level.upper()}] {message}"
    if notify_whatsapp(
        full_message,
        logger=log,
        project_dir=project_dir,
        env=environ,
        subprocess_module=subprocess_module,
    ):
        _webhook_rate_limiter[prefix] = now
        log.info("Legacy webhook alert routed to WhatsApp: %s", message[:100])
        return True
    return False


def _reset_imessage_rate_limiter():
    """Reset the iMessage rate limiter (for testing)."""
    _imessage_rate_limiter.clear()


def _send_imessage_blocking(
    message,
    prefix,
    logger,
    *,
    project_dir=PROJECT_DIR,
    env=None,
    requests_module=requests,
    subprocess_module=subprocess,
    time_func=time.time,
):
    """Blocking legacy iMessage send routed to WhatsApp (runs in a background thread)."""
    log = logger or _log
    try:
        _ = requests_module  # Kept for backward-compatible call signatures.
        if notify_whatsapp(
            message,
            logger=log,
            project_dir=project_dir,
            env=os.environ if env is None else env,
            subprocess_module=subprocess_module,
        ):
            _imessage_rate_limiter[prefix] = time_func()
            log.info("Legacy iMessage notification routed to WhatsApp: %s", prefix)
    except Exception as exc:
        log.warning("Legacy iMessage notification failed: %s", exc)


def notify_imessage(
    message,
    logger=None,
    *,
    project_dir=PROJECT_DIR,
    env=None,
    requests_module=requests,
    threading_module=threading,
    subprocess_module=subprocess,
    time_func=time.time,
):
    """Legacy iMessage entry point routed through WhatsApp (fire-and-forget)."""
    environ = os.environ if env is None else env
    if not _resolve_notification_phone(project_dir, environ):
        return False

    prefix = message[:80]
    now = time_func()
    if now - _imessage_rate_limiter.get(prefix, 0) < _IMESSAGE_COOLDOWN_SECONDS:
        return False

    _imessage_rate_limiter[prefix] = now
    thread = threading_module.Thread(
        target=_send_imessage_blocking,
        args=(message, prefix, logger),
        kwargs={
            "project_dir": project_dir,
            "env": environ,
            "requests_module": requests_module,
            "subprocess_module": subprocess_module,
            "time_func": time_func,
        },
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
