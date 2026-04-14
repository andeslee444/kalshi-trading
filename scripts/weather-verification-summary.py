#!/usr/bin/env python3
"""Print weather verification actual-source mix from weather-verification.json."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from forecast_verifier import ForecastVerifier
from runtime_paths import resolve_data_dir


def main():
    parser = argparse.ArgumentParser(description="Weather verification actual-source summary")
    parser.add_argument(
        "--state-path",
        default=str(resolve_data_dir(PROJECT_DIR) / "weather-verification.json"),
        help="Path to weather-verification.json",
    )
    parser.add_argument(
        "--lookback-days",
        nargs="*",
        type=int,
        default=[7, 30],
        help="One or more lookback windows in days (default: 7 30)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable lines",
    )
    args = parser.parse_args()

    verifier = ForecastVerifier(Path(args.state_path))
    verifier.load()
    summaries = [verifier.get_actual_source_summary(days) for days in args.lookback_days]

    if args.json:
        print(json.dumps({
            "state_path": str(args.state_path),
            "summaries": summaries,
        }, indent=2, sort_keys=True))
        return

    for summary in summaries:
        total = summary.get("total", 0)
        if total == 0:
            print(f"Weather actuals ({summary['lookback_days']}d): no verified records")
            continue

        parts = []
        for source, count in summary.get("counts", {}).items():
            share = summary.get("shares", {}).get(source, 0.0)
            parts.append(f"{source}={count} ({share:.1%})")
        print(
            f"Weather actuals ({summary['lookback_days']}d): "
            f"total={total} | " + ", ".join(parts)
        )


if __name__ == "__main__":
    main()
