"""Direct tests for the extracted ops.logging helpers."""

import logging
import os
import signal
from pathlib import Path

from ops.logging import (
    is_shutdown_requested,
    setup_logging,
    setup_signal_handlers,
    setup_unbuffered,
)


def _clear_logger(name):
    logger = logging.getLogger(name)
    logger.handlers.clear()
    return logger


def test_setup_unbuffered_sets_pythonunbuffered(monkeypatch):
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)

    setup_unbuffered()

    assert os.environ["PYTHONUNBUFFERED"] == "1"


def test_setup_logging_uses_project_dir_default(tmp_path):
    name = "test-ops-logging-default"
    _clear_logger(name)

    logger = setup_logging(name, project_dir=tmp_path)

    assert logger.handlers
    assert (tmp_path / "data" / "logs" / f"{name}.log").exists()
    _clear_logger(name)


def test_is_shutdown_requested_coerces_bool():
    assert is_shutdown_requested(False) is False
    assert is_shutdown_requested(True) is True
    assert is_shutdown_requested(1) is True


def test_setup_signal_handlers_invokes_shutdown_callback():
    state = {"requested": False}
    old_handlers = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1)
    }

    try:
        setup_signal_handlers(
            lambda requested=True: state.__setitem__("requested", requested)
        )
        os.kill(os.getpid(), signal.SIGUSR1)
        assert state["requested"] is True
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
