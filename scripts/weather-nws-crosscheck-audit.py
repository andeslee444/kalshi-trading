#!/usr/bin/env python3
"""Summarize verified Open-Meteo vs NWS gridpoint comparisons against settlement."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from forecast_verifier import NWSCrossCheckVerifier


def _format_metric(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _print_block(label, stats):
    print(
        f"{label:>8} "
        f"n={stats['n']:>3} "
        f"om_mae={_format_metric(stats['open_meteo_mae']):>5} "
        f"nws_mae={_format_metric(stats['nws_mae']):>5} "
        f"om_bias={_format_metric(stats['open_meteo_bias']):>6} "
        f"nws_bias={_format_metric(stats['nws_bias']):>6} "
        f"om-nws={_format_metric(stats['mean_open_meteo_minus_nws']):>6} "
        f"om_win={stats['open_meteo_better']:>3} "
        f"nws_win={stats['nws_better']:>3} "
        f"ties={stats['ties']:>3}"
    )


def main():
    parser = argparse.ArgumentParser(description="Weather NWS cross-check audit")
    parser.add_argument(
        "--state-path",
        default=str(PROJECT_DIR / "data" / "weather-nws-cross-check.json"),
        help="Path to weather-nws-cross-check.json",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Verified lookback window in days",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable rows",
    )
    args = parser.parse_args()

    verifier = NWSCrossCheckVerifier(Path(args.state_path))
    verifier.load()
    summary = verifier.get_summary(lookback_days=args.lookback_days)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    overall = summary["overall"]
    if overall["n"] == 0:
        print(f"NWS cross-check audit ({args.lookback_days}d): no verified records")
        return

    print(f"NWS cross-check audit ({args.lookback_days}d)")
    _print_block("overall", overall)
    print("\nPer city")
    for city, stats in summary["per_city"].items():
        _print_block(city, stats)

    print("\nPer days_out")
    for days_out, stats in summary["per_days_out"].items():
        _print_block(f"d+{days_out}", stats)


if __name__ == "__main__":
    main()
