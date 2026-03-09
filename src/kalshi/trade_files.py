"""Canonical trade file definitions shared across all scripts.

Every script that reads trade logs should import from here instead of
maintaining its own hardcoded list.  This ensures reconciliation, backfill,
backtest, calibration, performance analysis, and the dashboard all operate
on the same set of files.

Usage:
    from trade_files import TRADE_FILES, ALL_TRADE_PATHS

    # TRADE_FILES: list of dicts with label, bot, and filename keys
    # ALL_TRADE_PATHS: list of resolved Path objects for all trade logs
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

DATA_DIR = PROJECT_DIR / "data"

# Canonical list of all trade log files produced by bots.
# Order matches analyze-performance.py and dashboard.py.
TRADE_FILES = [
    {"label": "Weather Bot",        "bot": "weather",       "filename": "kalshi-trades.json"},
    {"label": "Strategy Trader",    "bot": "strategy",      "filename": "kalshi-strategy-trades.json"},
    {"label": "Entertainment Bot",  "bot": "entertainment", "filename": "kalshi-entertainment-trades.json"},
    {"label": "BeatRelease Scanner","bot": "beatrelease",   "filename": "beatrelease-trades.json"},
    {"label": "Source Monitor",     "bot": "monitor",       "filename": "kalshi-monitor-trades.json"},
    {"label": "Position Monitor",   "bot": "positions",     "filename": "kalshi-position-trades.json"},
    {"label": "Economics Bot",      "bot": "economics",     "filename": "kalshi-economics-trades.json"},
    {"label": "Crypto Bot",         "bot": "crypto",        "filename": "kalshi-crypto-trades.json"},
    {"label": "Cross-Platform Arb", "bot": "arb",           "filename": "kalshi-arb-trades.json"},
    {"label": "Market Maker",       "bot": "mm",            "filename": "kalshi-mm-trades.json"},
]

# Convenience: resolved Path objects for every trade log file
ALL_TRADE_PATHS = [DATA_DIR / tf["filename"] for tf in TRADE_FILES]
