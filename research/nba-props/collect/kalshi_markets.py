"""Kalshi market snapshot collector (placeholder).

Pulls NBA-related Kalshi market prices for comparison with model probabilities.

Usage:
    python -m collect.kalshi_markets --prefix KXNBA
"""

from __future__ import annotations

import argparse
from pathlib import Path


DATA_DIR = Path(__file__).parent.parent / "data"
RAW_KALSHI_DIR = DATA_DIR / "raw" / "kalshi"


def main():
    parser = argparse.ArgumentParser(description="Collect Kalshi NBA market snapshots")
    parser.add_argument("--prefix", default="KXNBA", help="Market ticker prefix")
    args = parser.parse_args()

    RAW_KALSHI_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Would collect markets with prefix: {args.prefix}")
    print(f"Output: {RAW_KALSHI_DIR}")
    print("TODO: Wire up Kalshi client")


if __name__ == "__main__":
    main()
