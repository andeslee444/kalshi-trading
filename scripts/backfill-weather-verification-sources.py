#!/usr/bin/env python3
"""Backfill missing actual_source fields in weather-verification.json.

Uses the same settlement-source stack as the live verifier:
- Prefer final NWS Daily Climate Report (`nws_cli`) if it matches stored actual_high
- Otherwise use IEM fallback (`iem_fallback`) if that matches stored actual_high

This is intended as a one-off repair for legacy verified rows created before
`actual_source` provenance was stored.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from forecast_verifier import ForecastVerifier, DEFAULT_STATION_MAP
from kalshi_auth import PROJECT_DIR


def weather_bot_running():
    """Return True if a live weather bot worker is running."""
    try:
        output = subprocess.check_output(
            ["pgrep", "-fl", "src/kalshi/weather-bot.py"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return False

    for line in output.strip().splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        exe_name = Path(parts[1]).name.lower() if len(parts) > 1 else ""
        if "python" in exe_name and "src/kalshi/weather-bot.py" in parts[2:]:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Backfill weather verification actual sources")
    parser.add_argument(
        "--state-path",
        default=str(PROJECT_DIR / "data" / "weather-verification.json"),
        help="Path to weather-verification.json",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Summary lookback window after backfill (default: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute updates without writing the file",
    )
    parser.add_argument(
        "--force-running",
        action="store_true",
        help="Allow backfill even if a live weather bot is running",
    )
    args = parser.parse_args()

    if weather_bot_running() and not args.dry_run and not args.force_running:
        print(
            "Refusing to backfill while weather-bot.py is running. "
            "Stop or halt the weather bot first, or pass --force-running.",
            file=sys.stderr,
        )
        sys.exit(2)

    verifier = ForecastVerifier(Path(args.state_path))
    verifier.load()

    before = verifier.get_actual_source_summary(lookback_days=args.lookback_days)
    result = verifier.backfill_actual_sources(station_map=DEFAULT_STATION_MAP)
    after = verifier.get_actual_source_summary(lookback_days=args.lookback_days)

    if not args.dry_run:
        verifier.save()

    print(json.dumps({
        "state_path": str(args.state_path),
        "dry_run": args.dry_run,
        "backfill": result,
        "before": before,
        "after": after,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
