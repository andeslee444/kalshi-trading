"""Canonical trade file definitions shared across all scripts."""

from pathlib import Path

from bot_registry import TRADE_FILE_SPECS
from runtime_paths import resolve_data_dir

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

DATA_DIR = resolve_data_dir(PROJECT_DIR)

TRADE_FILES = [dict(spec) for spec in TRADE_FILE_SPECS]

ALL_TRADE_PATHS = [DATA_DIR / tf["filename"] for tf in TRADE_FILES]
