#!/usr/bin/env python3
"""Calibrate CPI nowcast sigma from BLS CPI-U historical data.

Fetches CPI-U (All Items, Seasonally Adjusted) from the BLS public API,
computes year-over-year CPI changes, and fits an exponential decay model
for sigma(days_to_release).

Usage:
    python3 scripts/calibrate-cpi-sigma.py              # Display report
    python3 scripts/calibrate-cpi-sigma.py --save        # Save to config/calibration.json
    python3 scripts/calibrate-cpi-sigma.py --json        # JSON output
    python3 scripts/calibrate-cpi-sigma.py --start-year 2020 --end-year 2026
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
CALIBRATION_PATH = PROJECT_DIR / "config" / "calibration.json"

# BLS public API (no registration required, rate-limited)
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
CPI_SERIES_ID = "CUUR0000SA0"  # CPI-U All Items, Seasonally Adjusted


def fetch_cpi_data(start_year=2020, end_year=2026):
    """Fetch CPI-U data from BLS public API.

    Returns list of (year, month, cpi_value) tuples sorted chronologically.
    BLS API limits 20 years per request and 2 years for unauthenticated.
    """
    all_data = []

    # BLS public API (no key) allows 2-year spans; chunk accordingly
    year = start_year
    while year <= end_year:
        chunk_end = min(year + 1, end_year)  # 2-year window
        payload = {
            "seriesid": [CPI_SERIES_ID],
            "startyear": str(year),
            "endyear": str(chunk_end),
        }
        try:
            r = requests.post(BLS_API_URL, json=payload, timeout=30)
            r.raise_for_status()
            result = r.json()

            if result.get("status") != "REQUEST_SUCCEEDED":
                msg = result.get("message", ["Unknown error"])
                print(f"Warning: BLS API returned status {result.get('status')}: {msg}", file=sys.stderr)
                year = chunk_end + 1
                continue

            for series in result.get("Results", {}).get("series", []):
                for dp in series.get("data", []):
                    try:
                        yr = int(dp["year"])
                        # BLS uses M01-M12 for monthly, M13 for annual avg
                        period = dp["periodName"]
                        month_str = dp["period"]
                        if not month_str.startswith("M") or month_str == "M13":
                            continue
                        month = int(month_str[1:])
                        value = float(dp["value"])
                        all_data.append((yr, month, value))
                    except (ValueError, KeyError):
                        continue
        except Exception as e:
            print(f"Warning: BLS fetch failed for {year}-{chunk_end}: {e}", file=sys.stderr)

        year = chunk_end + 1

    # Sort chronologically
    all_data.sort()
    return all_data


def compute_yoy_changes(cpi_data):
    """Compute year-over-year CPI percentage changes.

    Returns list of (year, month, yoy_pct_change) tuples.
    """
    # Build lookup: (year, month) -> value
    lookup = {(yr, mo): val for yr, mo, val in cpi_data}

    changes = []
    for yr, mo, val in cpi_data:
        prev = lookup.get((yr - 1, mo))
        if prev and prev > 0:
            yoy = ((val - prev) / prev) * 100.0  # percentage
            changes.append((yr, mo, yoy))

    return changes


def compute_baseline_sigma(yoy_changes):
    """Compute the baseline month-to-month volatility of YoY CPI changes.

    Returns sigma in percentage points.
    """
    if len(yoy_changes) < 3:
        return 0.10  # default

    values = [c[2] for c in yoy_changes]

    # Compute changes in YoY rate (month-to-month surprise)
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    if not deltas:
        return 0.10

    mean_delta = sum(deltas) / len(deltas)
    variance = sum((d - mean_delta) ** 2 for d in deltas) / (len(deltas) - 1)
    sigma = math.sqrt(variance)

    return sigma


def fit_sigma_by_days(base_sigma, max_days=14):
    """Fit exponential decay: sigma(d) = max(0.01, base * (1 - exp(-0.15 * d))).

    At d=0 (release day): sigma ≈ 0.01 (essentially known)
    At d=14: sigma ≈ base_sigma (full uncertainty)

    Returns dict mapping str(days) -> sigma value.
    """
    sigma_by_days = {}
    for d in range(max_days + 1):
        if d == 0:
            sigma = 0.01
        else:
            sigma = max(0.01, base_sigma * (1 - math.exp(-0.15 * d)))
        sigma_by_days[str(d)] = round(sigma, 4)

    return sigma_by_days


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate CPI nowcast sigma from BLS historical data."
    )
    parser.add_argument("--save", action="store_true", help="Save to config/calibration.json")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--start-year", type=int, default=2020, help="Start year (default: 2020)")
    parser.add_argument("--end-year", type=int, default=2026, help="End year (default: 2026)")
    args = parser.parse_args()

    print("Fetching CPI-U data from BLS...", file=sys.stderr)
    cpi_data = fetch_cpi_data(args.start_year, args.end_year)
    print(f"  {len(cpi_data)} monthly observations", file=sys.stderr)

    if len(cpi_data) < 13:
        print("Error: Need at least 13 months of data for YoY computation.", file=sys.stderr)
        sys.exit(1)

    yoy = compute_yoy_changes(cpi_data)
    print(f"  {len(yoy)} YoY changes computed", file=sys.stderr)

    base_sigma = compute_baseline_sigma(yoy)
    print(f"  Baseline sigma: {base_sigma:.4f} pct pts", file=sys.stderr)

    sigma_by_days = fit_sigma_by_days(base_sigma)

    result = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "n_observations": len(cpi_data),
        "n_yoy_changes": len(yoy),
        "base_sigma_pct": round(base_sigma, 4),
        "sigma_by_days": sigma_by_days,
    }

    if args.save:
        # Merge into existing calibration.json
        existing = {}
        if CALIBRATION_PATH.exists():
            try:
                existing = json.loads(CALIBRATION_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        existing["cpi"] = result
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_PATH.write_text(json.dumps(existing, indent=2) + "\n")
        print(f"\nSaved to {CALIBRATION_PATH}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n" + "=" * 50)
        print("CPI SIGMA CALIBRATION")
        print(f"Data: {args.start_year}-{args.end_year} ({len(cpi_data)} months)")
        print(f"Base sigma: {base_sigma:.4f} pct pts")
        print("=" * 50)
        print("\nSigma by days to release:")
        for d in range(15):
            s = sigma_by_days[str(d)]
            bar = "#" * int(s * 200)
            print(f"  {d:2d}d: {s:.4f}  {bar}")
        if not args.save:
            print("\nRun with --save to write config/calibration.json")


if __name__ == "__main__":
    main()
