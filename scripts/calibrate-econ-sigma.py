#!/usr/bin/env python3
"""Calibrate CPI sigma from empirical nowcast history.

Usage:
    python3 scripts/calibrate-econ-sigma.py           # Print empirical sigmas
    python3 scripts/calibrate-econ-sigma.py --save     # Save to calibration.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
from nowcast_tracker import NowcastTracker

CAL_PATH = Path(__file__).resolve().parent.parent / "config" / "calibration.json"


def main():
    tracker = NowcastTracker()
    sigmas = tracker.compute_empirical_sigmas()

    if not sigmas:
        print("No empirical data yet. Record nowcast snapshots and actual values first.")
        return

    print("Empirical CPI sigma by horizon:")
    for bucket, sigma in sorted(sigmas.items(), key=lambda x: int(x[0])):
        print(f"  d={bucket:>3s}: sigma={sigma:.4f}%")

    if "--save" in sys.argv:
        cal = json.loads(CAL_PATH.read_text()) if CAL_PATH.exists() else {}
        cal.setdefault("cpi", {})["sigma_by_days"] = {k: v for k, v in sigmas.items()}
        CAL_PATH.write_text(json.dumps(cal, indent=2))
        print(f"\nSaved to {CAL_PATH}")


if __name__ == "__main__":
    main()
