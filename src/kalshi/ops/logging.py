"""Runtime logging and signal helpers extracted from kalshi_auth."""

from __future__ import annotations

import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_unbuffered():
    """Enable unbuffered stdout for real-time logging."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    os.environ["PYTHONUNBUFFERED"] = "1"


def setup_logging(name, log_file=None, project_dir=None):
    """Configure a logger with consistent format for a bot."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if log_file is None and project_dir is not None:
        log_file = str(Path(project_dir) / "data" / "logs" / f"{name}.log")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def is_shutdown_requested(requested=False):
    """Return the current shutdown flag as a bool."""
    return bool(requested)


def setup_signal_handlers(
    on_shutdown_requested=None,
    *,
    logger=None,
    signal_module=signal,
    exit_func=None,
):
    """Install graceful shutdown handlers for SIGTERM, SIGINT, and SIGUSR1."""
    log = logger or logging.getLogger("kalshi_auth")
    exit_fn = exit_func or sys.exit

    def _handler(signum, frame):
        log.info("Received signal %s, shutting down gracefully...", signum)
        exit_fn(0)

    signal_module.signal(signal_module.SIGTERM, _handler)
    signal_module.signal(signal_module.SIGINT, _handler)

    def _sigusr1_handler(signum, frame):
        log.info("Received SIGUSR1, requesting graceful shutdown...")
        if on_shutdown_requested is not None:
            on_shutdown_requested(True)

    signal_module.signal(signal_module.SIGUSR1, _sigusr1_handler)


__all__ = [
    "is_shutdown_requested",
    "setup_logging",
    "setup_signal_handlers",
    "setup_unbuffered",
]
