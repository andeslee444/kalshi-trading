#!/usr/bin/env python3
"""Calibrate market maker parameters using orderbook simulation.

Runs agent-based simulations for each target market prefix and writes
optimal (gamma, k) to config/mm-calibration.json.

Usage:
    python3 scripts/calibrate-mm.py              # Calibrate all target markets
    python3 scripts/calibrate-mm.py --prefix KXHIGH  # Single prefix
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from orderbook_sim import OrderBookSimulator

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "mm-calibration.json"

# Default target prefixes and their typical true probability ranges
TARGET_MARKETS = {
    "KXHIGH": {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
    "KXBTC":  {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
    "KXETH":  {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
}


def calibrate_prefix(prefix, params):
    """Run calibration for a single market prefix."""
    print(f"Calibrating {prefix}...")
    sim = OrderBookSimulator(true_prob=params["true_prob"], seed=42)
    result = sim.find_optimal_mm_params(
        gamma_range=[0.1, 0.2, 0.3, 0.5, 0.8],
        k_range=[0.5, 1.0, 1.5, 2.0, 3.0],
        n_simulations=params["n_sims"],
        n_steps=params["n_steps"],
    )
    result["activated"] = result["should_activate"]
    result["calibrated_at"] = datetime.now().isoformat()
    del result["should_activate"]
    print(f"  {prefix}: gamma={result['gamma']}, k={result['k']}, "
          f"sharpe={result['sharpe']}, activated={result['activated']}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Calibrate MM params")
    parser.add_argument("--prefix", help="Single prefix to calibrate")
    args = parser.parse_args()

    # Load existing config
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    targets = TARGET_MARKETS
    if args.prefix:
        if args.prefix not in targets:
            targets[args.prefix] = {"true_prob": 0.50, "n_sims": 30, "n_steps": 300}
        targets = {args.prefix: targets[args.prefix]}

    for prefix, params in targets.items():
        result = calibrate_prefix(prefix, params)
        existing[prefix] = result

    # Write atomically
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    import os
    os.replace(tmp, str(CONFIG_PATH))
    print(f"\nCalibration saved to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
