"""Canonical source-path helpers for migrated bot modules."""

from __future__ import annotations

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_DIR / "src" / "kalshi"

LEGACY_WRAPPER_SOURCE_MAP = {
    "weather-bot.py": "apps/weather_bot.py",
    "crypto-bot.py": "apps/crypto_bot.py",
    "economics-bot.py": "apps/economics_bot.py",
    "entertainment-bot.py": "apps/entertainment_bot.py",
    "source-monitor.py": "apps/source_monitor.py",
    "position-monitor.py": "apps/position_monitor.py",
    "strategy-trader.py": "apps/strategy_trader.py",
    "market-maker.py": "apps/market_maker.py",
    "cross-platform-arb.py": "apps/cross_platform_arb.py",
    "beatrelease-scanner.py": "apps/beatrelease_scanner.py",
    "demo-trader.py": "apps/demo_trader.py",
    "hdd-scraper.py": "apps/hdd_scraper.py",
}


def resolve_bot_source_path(path_or_name: str | Path) -> Path:
    """Return the canonical source file for a legacy wrapper or app module."""
    filename = Path(path_or_name).name
    relative_path = LEGACY_WRAPPER_SOURCE_MAP.get(filename, filename)
    return SRC_ROOT / relative_path

