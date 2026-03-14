"""Kill-switch helpers extracted from kalshi_auth."""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[3]
KILL_SWITCH_PATH = PROJECT_DIR / "data" / "HALT_TRADING"
PER_BOT_HALT_PREFIX = "HALT_bot_"


def check_kill_switch(path=None):
    """Return True if the kill switch file exists (trading should halt)."""
    p = Path(path) if path else KILL_SWITCH_PATH
    return p.exists()


def per_bot_halt_path(bot_name, project_dir=PROJECT_DIR):
    """Return Path for a per-bot halt file: data/HALT_bot_{name}."""
    return Path(project_dir) / "data" / f"{PER_BOT_HALT_PREFIX}{bot_name}"


__all__ = [
    "KILL_SWITCH_PATH",
    "PER_BOT_HALT_PREFIX",
    "check_kill_switch",
    "per_bot_halt_path",
]

