"""Tests for setup_logging duplicate handler prevention."""
import logging
import sys
import io
import os
import tempfile
from pathlib import Path

# Add source path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))


def _clear_logger(name):
    """Remove all handlers from a logger."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    return logger


def test_setup_logging_no_duplicate_handlers():
    """setup_logging should not add StreamHandler when stdout is not a TTY."""
    from kalshi_auth import setup_logging

    name = "test-no-dup-handlers"
    _clear_logger(name)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        logger = setup_logging(name, log_file=log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        # Should have at most 2 handlers (stream + file), never more
        assert len(logger.handlers) <= 2, f"Too many handlers: {handler_types}"
        # Calling again should NOT add more handlers
        logger2 = setup_logging(name, log_file=log_file)
        assert logger is logger2
        assert len(logger.handlers) <= 2, "Duplicate handlers added on second call"
    _clear_logger(name)


def test_setup_logging_skips_stream_when_not_tty():
    """When stdout is not a TTY (piped/redirected), skip StreamHandler to avoid duplicates."""
    from kalshi_auth import setup_logging

    name = "test-skip-stream"
    _clear_logger(name)

    # Simulate non-TTY stdout (like supervisor redirect)
    original_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "test.log")
            logger = setup_logging(name, log_file=log_file)
            handler_types = [type(h).__name__ for h in logger.handlers]
            # Should only have RotatingFileHandler, no StreamHandler
            assert "StreamHandler" not in handler_types, (
                f"StreamHandler present when stdout is not a TTY: {handler_types}"
            )
            assert "RotatingFileHandler" in handler_types
    finally:
        sys.stdout = original_stdout
    _clear_logger(name)


def test_setup_logging_adds_stream_when_tty(monkeypatch):
    """When stdout IS a TTY, StreamHandler should be included."""
    from kalshi_auth import setup_logging

    name = "test-with-stream"
    _clear_logger(name)

    # Mock isatty to return True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        logger = setup_logging(name, log_file=log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types, (
            f"StreamHandler missing when stdout IS a TTY: {handler_types}"
        )
        assert "RotatingFileHandler" in handler_types
    _clear_logger(name)
