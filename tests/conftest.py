"""Shared fixtures and path setup for the Kalshi trading bot test suite."""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure ``src/kalshi/`` is importable by all test modules.
# This insert lets us do ``from strategy_trader import half_kelly`` etc.
# (The source files use underscored module names once imported.)
# ---------------------------------------------------------------------------
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
