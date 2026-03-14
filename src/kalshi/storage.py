"""Storage helpers that preserve the current JSON artifacts and append flows."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

_MISSING = object()
_log = logging.getLogger("storage")


def atomic_write_json(path: Path, data):
    """Write JSON atomically via temp file + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class JsonFileStore:
    """Base JSON-file adapter with advisory locking."""

    def __init__(self, path, logger=None, default_factory=None):
        self.path = Path(path)
        self.log = logger or _log
        self._default_factory = default_factory or (lambda: None)

    def _default_value(self):
        if callable(self._default_factory):
            return self._default_factory()
        return self._default_factory

    def _resolve_default(self, default):
        if default is _MISSING:
            return self._default_value()
        return default

    @contextmanager
    def lock(self, shared=False):
        """Hold the store lock for callers coordinating multiple operations."""
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _coerce_loaded(self, data, fallback):
        return data

    def _prepare_for_save(self, data):
        return data

    def load_unlocked(self, default=_MISSING):
        fallback = self._resolve_default(default)
        if not self.path.exists():
            return self._coerce_loaded(fallback, fallback)
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError, ValueError) as e:
            self.log.warning("Failed to load JSON from %s: %s", self.path, e)
            return self._coerce_loaded(fallback, fallback)
        return self._coerce_loaded(data, fallback)

    def load(self, default=_MISSING):
        with self.lock(shared=True):
            return self.load_unlocked(default=default)

    def save_unlocked(self, data):
        prepared = self._prepare_for_save(data)
        atomic_write_json(self.path, prepared)
        return prepared

    def save(self, data):
        with self.lock():
            return self.save_unlocked(data)

    def update_unlocked(self, updater, default=_MISSING):
        current = self.load_unlocked(default=default)
        updated = updater(current)
        if updated is None:
            updated = current
        return self.save_unlocked(updated)

    def update(self, updater, default=_MISSING):
        with self.lock():
            return self.update_unlocked(updater, default=default)


class SnapshotStore(JsonFileStore):
    """Generic adapter for read-mostly JSON snapshots."""

    def load(self, default=_MISSING):
        # Snapshot readers are intentionally lock-free so config/dashboard reads
        # do not leave behind incidental .lock files.
        return self.load_unlocked(default=default)


class StateStore(JsonFileStore):
    """Adapter for dict-backed mutable state files."""

    def __init__(self, path, logger=None, normalizer=None, default_factory=dict):
        super().__init__(path, logger=logger, default_factory=default_factory)
        self._normalizer = normalizer

    def _normalize(self, data):
        if self._normalizer is not None:
            return self._normalizer(data)
        if isinstance(data, dict):
            return dict(data)
        return data

    def _coerce_loaded(self, data, fallback):
        if not isinstance(data, dict):
            self.log.warning(
                "Expected JSON object in %s, found %s",
                self.path,
                type(data).__name__,
            )
            data = fallback
        return self._normalize(data)

    def _prepare_for_save(self, data):
        if data is None:
            data = self._default_value()
        if not isinstance(data, dict):
            raise TypeError(f"{self.path} expects a dict state payload")
        return self._normalize(data)


class _RecordListStore(JsonFileStore):
    """Adapter for append-heavy list artifacts."""

    def __init__(self, path, logger=None, default_factory=list):
        super().__init__(path, logger=logger, default_factory=default_factory)

    def _coerce_loaded(self, data, fallback):
        if isinstance(data, list):
            return list(data)
        self.log.warning(
            "Expected JSON list in %s, found %s",
            self.path,
            type(data).__name__,
        )
        return fallback

    def _prepare_for_save(self, data):
        if data is None:
            return []
        if not isinstance(data, list):
            raise TypeError(f"{self.path} expects a list payload")
        return list(data)

    @staticmethod
    def _trim_records(records, max_records=None, trim_to=None):
        if max_records is None or len(records) <= max_records:
            return records
        keep = trim_to if trim_to is not None else max_records
        keep = max(0, min(int(keep), int(max_records)))
        return records[-keep:] if keep else []

    def append_unlocked(self, record, *, max_records=None, trim_to=None):
        records = self.load_unlocked()
        records.append(record)
        return self.save_unlocked(
            self._trim_records(records, max_records=max_records, trim_to=trim_to)
        )

    def append(self, record, *, max_records=None, trim_to=None):
        with self.lock():
            return self.append_unlocked(record, max_records=max_records, trim_to=trim_to)


class TradeStore(_RecordListStore):
    """Trade log adapter that preserves existing JSON list files."""

    def load(self, default=_MISSING):
        # Trade logs have existing call sites that already hold the lock file.
        # Keep reads unlocked to preserve that behavior and avoid self-deadlock.
        return self.load_unlocked(default=default)


class DecisionStore(_RecordListStore):
    """Decision log adapter with the current rotating-file semantics."""

    def __init__(self, path, logger=None, max_records=5000, trim_to=4000):
        super().__init__(path, logger=logger)
        self.max_records = max_records
        self.trim_to = trim_to

    def append(self, record, *, max_records=None, trim_to=None):
        max_records = self.max_records if max_records is None else max_records
        trim_to = self.trim_to if trim_to is None else trim_to
        with self.lock():
            records = self.load_unlocked()
            if max_records is not None and len(records) >= max_records:
                keep = max(0, int(trim_to))
                records = records[-keep:] if keep else []
            records.append(record)
            return self.save_unlocked(records)


class MetricsStore(_RecordListStore):
    """Metrics/event list adapter with bounded retention."""

    def __init__(self, path, logger=None, max_records=2000, trim_to=1500):
        super().__init__(path, logger=logger)
        self.max_records = max_records
        self.trim_to = trim_to

    def append(self, record, *, max_records=None, trim_to=None):
        return super().append(
            record,
            max_records=self.max_records if max_records is None else max_records,
            trim_to=self.trim_to if trim_to is None else trim_to,
        )


def load_trades(trades_path: Path, logger=None) -> list:
    """Load trades from a JSON file. Returns [] on missing/corrupt file."""
    return TradeStore(trades_path, logger=logger or _log).load()


def save_trade(trades_path: Path, trade: dict, logger=None):
    """Append a trade to a JSON trades file and dual-write to the ledger."""
    log = logger or _log
    TradeStore(trades_path, logger=log).append(trade)
    try:
        from event_ledger import get_event_ledger

        get_event_ledger(logger=log).record_order_submitted(
            trade,
            source_path=trades_path,
        )
    except Exception as e:
        log.warning("Failed to dual-write trade to event ledger: %s", e)


def save_decision(decisions_path: Path, decision: dict, logger=None):
    """Append a decision to the decisions log and dual-write to the ledger."""
    log = logger or _log
    try:
        DecisionStore(decisions_path, logger=log).append(decision)
        try:
            from event_ledger import get_event_ledger

            get_event_ledger(logger=log).record_trade_decision(
                decision,
                source_path=decisions_path,
            )
        except Exception as e:
            log.warning("Failed to dual-write decision to event ledger: %s", e)
    except Exception as e:
        log.warning("Failed to save decision: %s", e)


__all__ = [
    "DecisionStore",
    "MetricsStore",
    "SnapshotStore",
    "StateStore",
    "TradeStore",
    "atomic_write_json",
    "load_trades",
    "save_decision",
    "save_trade",
]
