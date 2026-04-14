"""Helpers for resolving runtime artifact directories.

By default bots and scripts read/write under ``<project>/data``.
Operators can override that location with ``KALSHI_DATA_DIR`` so a checked-out
repo can inspect live deploy artifacts without symlinks or local rewrites.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR_ENV_VAR = "KALSHI_DATA_DIR"


def resolve_data_dir(project_dir: str | Path | None = None, *, env_var: str = DATA_DIR_ENV_VAR) -> Path:
    """Return the active data directory for runtime artifacts."""
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    return Path(project_dir or PROJECT_DIR) / "data"


def data_path(*parts: str | Path, project_dir: str | Path | None = None, env_var: str = DATA_DIR_ENV_VAR) -> Path:
    """Return a path rooted in the active runtime data directory."""
    path = resolve_data_dir(project_dir=project_dir, env_var=env_var)
    for part in parts:
        path /= Path(part)
    return path

