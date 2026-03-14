"""Console entrypoints for packaged operational scripts."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]


def _run_script(script_relative_path: str):
    script_path = PROJECT_DIR / script_relative_path
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    runpy.run_path(str(script_path), run_name="__main__")


def dashboard():
    _run_script("scripts/dashboard.py")


def supervisor():
    _run_script("scripts/supervisor.py")


def daily_report():
    _run_script("scripts/daily-report.py")


def snapshot():
    _run_script("scripts/pnl-snapshot.py")

