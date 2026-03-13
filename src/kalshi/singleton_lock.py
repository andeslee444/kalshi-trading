"""Shared non-blocking process singleton locks for long-running bots."""

from __future__ import annotations

import atexit
import datetime
import json
import os
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - fallback for non-POSIX environments
    fcntl = None


_LOCK_HANDLES: dict[str, object] = {}


def release_process_singleton(name: str) -> None:
    """Release a previously acquired singleton lock."""
    handle = _LOCK_HANDLES.pop(name, None)
    if handle is None:
        return

    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass

    try:
        handle.close()
    except OSError:
        pass


def acquire_process_singleton(
    name: str,
    project_dir,
    logger,
    *,
    lock_path=None,
    display_name: str | None = None,
    argv=None,
) -> bool:
    """Acquire a non-blocking singleton lock for a bot process.

    Returns True if the caller owns the lock, False if another live process does.
    """
    if name in _LOCK_HANDLES:
        return True

    display_name = display_name or name
    project_dir = Path(project_dir)
    lock_path = Path(lock_path) if lock_path else project_dir / "data" / "pids" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    handle = lock_path.open("a+")

    if fcntl is None:
        _LOCK_HANDLES[name] = handle
        atexit.register(release_process_singleton, name)
        return True

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        raw = handle.read().strip()
        owner_info = raw
        if raw:
            try:
                meta = json.loads(raw)
                owner_info = f"pid={meta.get('pid', '?')} started={meta.get('started_at', '?')}"
            except (json.JSONDecodeError, TypeError):
                owner_info = raw[:200]  # truncate corrupt data
        if owner_info:
            logger.warning("Another %s instance already holds %s: %s", display_name, lock_path, owner_info)
        else:
            logger.warning("Another %s instance already holds %s", display_name, lock_path)
        handle.close()
        return False

    metadata = {
        "pid": os.getpid(),
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "argv": list(argv or sys.argv),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(metadata))
    handle.flush()

    _LOCK_HANDLES[name] = handle
    atexit.register(release_process_singleton, name)
    return True
