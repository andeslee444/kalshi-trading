"""Circuit breaker helpers extracted from kalshi_auth."""

from __future__ import annotations

import fcntl
import json
import logging
import time
from pathlib import Path

from storage import atomic_write_json

PROJECT_DIR = Path(__file__).resolve().parents[3]
SHARED_BREAKER_PATH = PROJECT_DIR / "data" / "circuit-breaker-state.json"


class CircuitBreaker:
    """Tracks consecutive failures and opens after a threshold."""

    def __init__(
        self,
        max_failures=5,
        reset_seconds=300,
        state_path=None,
        *,
        state_writer=None,
        notifier=None,
        logger=None,
    ):
        self.max_failures = max_failures
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at = None
        self.state_path = Path(state_path) if state_path else None
        self._state_writer = state_writer or atomic_write_json
        self._notifier = notifier
        self.log = logger or logging.getLogger("circuit-breaker")

    def _with_shared_lock(self, fn):
        """Execute fn under file lock on shared state."""
        if not self.state_path:
            return fn()
        lock_path = self.state_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _load_shared(self):
        """Load shared breaker state from disk."""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            cb = data.get("circuit_breaker", {})
            self._failures = cb.get("failures", 0)
            self._opened_at = cb.get("opened_at")
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    def _save_shared(self):
        """Save breaker state to shared file (merge into existing data)."""
        if not self.state_path:
            return
        try:
            existing = {}
            if self.state_path.exists():
                try:
                    existing = json.loads(self.state_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            existing["circuit_breaker"] = {
                "failures": self._failures,
                "opened_at": self._opened_at,
                "max_failures": self.max_failures,
            }
            self._state_writer(self.state_path, existing)
        except Exception as e:
            self.log.warning("Failed to save circuit breaker state: %s", e)

    def record_success(self):
        """Record a successful operation and reset the failure counter."""

        def _do():
            if self.state_path:
                self._load_shared()
            self._failures = 0
            self._opened_at = None
            if self.state_path:
                self._save_shared()

        self._with_shared_lock(_do)

    def record_failure(self):
        """Record a failed operation and open the breaker if needed."""

        def _do():
            if self.state_path:
                self._load_shared()
            self._failures += 1
            if self._failures >= self.max_failures and self._opened_at is None:
                self._opened_at = time.time()
                if self._notifier is not None:
                    self._notifier(
                        f"Circuit breaker OPEN after {self._failures} consecutive failures",
                        level="critical",
                    )
            if self.state_path:
                self._save_shared()

        self._with_shared_lock(_do)

    def is_open(self):
        """Return True if the breaker is open (callers should back off)."""

        def _do():
            if self.state_path:
                self._load_shared()
            if self._failures < self.max_failures:
                return False
            if self._opened_at is None or not isinstance(self._opened_at, (int, float)):
                self._opened_at = time.time()
                if self.state_path:
                    self._save_shared()
                return True
            if (time.time() - self._opened_at) >= self.reset_seconds:
                self._failures = 0
                self._opened_at = None
                if self.state_path:
                    self._save_shared()
                return False
            return True

        return self._with_shared_lock(_do)


__all__ = [
    "CircuitBreaker",
    "SHARED_BREAKER_PATH",
]
