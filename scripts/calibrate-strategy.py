#!/usr/bin/env python3
"""Calibrate strategy-trader longshot bias parameters from settled trades.

Reads settled strategy trades, computes empirical win rates by category
and price bucket, and proposes updated LONGSHOT_BIAS_PARAMS and bayes-params.json.

Usage:
    python3 scripts/calibrate-strategy.py              # Display report
    python3 scripts/calibrate-strategy.py --save        # Save to bayes-params.json
    python3 scripts/calibrate-strategy.py --json        # JSON output for pipeline
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from probability import classify_ticker_category, LONGSHOT_BIAS_PARAMS
from trade_files import ALL_TRADE_PATHS

BAYES_PARAMS_PATH = PROJECT_DIR / "config" / "bayes-params.json"

BUCKET_RANGES = [
    ("1-5", 1, 5),
    ("6-10", 6, 10),
    ("11-15", 11, 15),
    ("16-20", 16, 20),
    ("21-30", 21, 30),
]

MIN_TRADES_FOR_FIT = 5


def load_settled_strategy_trades():
    """Load all settled trades in the longshot price range from trade logs."""
    trades = []
    for path in ALL_TRADE_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for t in data:
            sr = t.get("settlement_result")
            if sr is None:
                continue
            # Extract price from various field names used across bots
            price = t.get("price_cents") or t.get("yes_price_cents") or t.get("price", 0)
            if not isinstance(price, (int, float)):
                continue
            if price <= 0 or price > 30:
                continue  # only longshot range
            ticker = t.get("ticker", "")
            category = classify_ticker_category(ticker)
            won = sr in ("won", "win", True, 1)
            bot = t.get("bot", "")
            # Include strategy trades and any longshot-range trades
            if "strategy" in bot or price <= 15:
                trades.append({
                    "ticker": ticker,
                    "price_cents": int(price),
                    "won": won,
                    "category": category,
                    "bot": bot,
                    "side": t.get("side", ""),
                })
    return trades


def count_buckets(trades):
    """Count wins/losses by price bucket."""
    buckets = {}
    for label, lo, hi in BUCKET_RANGES:
        buckets[label] = {"wins": 0, "losses": 0}

    for t in trades:
        p = t["price_cents"]
        for label, lo, hi in BUCKET_RANGES:
            if lo <= p <= hi:
                if t["won"]:
                    buckets[label]["wins"] += 1
                else:
                    buckets[label]["losses"] += 1
                break
    return buckets


def fit_becker_params(trades):
    """Fit amplitude and decay_rate for Becker longshot model.

    Model: overpricing_ratio = amplitude * exp(-decay_rate * price)
    Empirical: overpricing_ratio = 1 - (actual_win_rate / implied_prob)

    Returns (amplitude, decay_rate) or None if insufficient data.
    """
    if len(trades) < MIN_TRADES_FOR_FIT:
        return None

    # Group by price and compute empirical win rates
    by_price = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        p = t["price_cents"]
        by_price[p]["total"] += 1
        if t["won"]:
            by_price[p]["wins"] += 1

    # Build (price, overpricing_ratio) pairs
    points = []
    for price, counts in by_price.items():
        if counts["total"] < 3:
            continue
        implied = price / 100.0
        actual_wr = counts["wins"] / counts["total"]
        if actual_wr >= implied:
            # No overpricing at this price -- longshot bias doesn't apply
            overpricing = 0.0
        else:
            overpricing = 1.0 - (actual_wr / implied) if implied > 0 else 0.0
        points.append((price, max(0, min(1, overpricing))))

    if len(points) < 2:
        # Not enough distinct prices -- use simple average
        if not points:
            return (0.0, 0.15)
        return (points[0][1], 0.15)

    # Simple least-squares fit of log(overpricing) = log(amplitude) - decay * price
    # Filter zero overpricing (can't take log)
    log_points = [(p, math.log(max(o, 0.001))) for p, o in points if o > 0.001]

    if len(log_points) < 2:
        avg_op = sum(o for _, o in points) / len(points)
        return (max(0.01, avg_op), 0.15)

    # Linear regression: y = a + b*x where y=log(overpricing), x=price
    n = len(log_points)
    sx = sum(x for x, _ in log_points)
    sy = sum(y for _, y in log_points)
    sxy = sum(x * y for x, y in log_points)
    sxx = sum(x * x for x, _ in log_points)

    denom = n * sxx - sx * sx
    if abs(denom) < 1e-10:
        avg_op = sum(o for _, o in points) / len(points)
        return (max(0.01, avg_op), 0.15)

    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n

    amplitude = min(0.90, max(0.01, math.exp(a)))
    decay_rate = min(0.50, max(0.05, -b))  # b should be negative

    return (round(amplitude, 2), round(decay_rate, 2))


def calibrate():
    """Main calibration routine."""
    trades = load_settled_strategy_trades()
    if not trades:
        return {"error": "No settled strategy trades found"}

    # Group by category
    by_category = defaultdict(list)
    for t in trades:
        by_category[t["category"]].append(t)

    # Fit params per category
    proposed_params = {}
    for category, cat_trades in by_category.items():
        result = fit_becker_params(cat_trades)
        current = LONGSHOT_BIAS_PARAMS.get(category, LONGSHOT_BIAS_PARAMS["default"])
        buckets = count_buckets(cat_trades)
        total = sum(b["wins"] + b["losses"] for b in buckets.values())
        wins = sum(b["wins"] for b in buckets.values())

        proposed_params[category] = {
            "current": {"amplitude": current[0], "decay_rate": current[1]},
            "proposed": {"amplitude": result[0], "decay_rate": result[1]} if result else None,
            "n_trades": total,
            "win_rate": wins / total if total > 0 else 0,
            "buckets": buckets,
        }

    return {
        "total_trades": len(trades),
        "categories": proposed_params,
    }


def main():
    parser = argparse.ArgumentParser(description="Strategy-trader longshot calibration")
    parser.add_argument("--save", action="store_true", help="Save to bayes-params.json")
    parser.add_argument("--json", action="store_true", help="JSON output for pipeline")
    args = parser.parse_args()

    result = calibrate()

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print("=" * 60)
    print("STRATEGY LONGSHOT CALIBRATION")
    print(f"Total settled trades in longshot range: {result.get('total_trades', 0)}")
    print("=" * 60)

    for cat, data in result.get("categories", {}).items():
        print(f"\n{cat.upper()}:")
        print(f"  Trades: {data['n_trades']}, Win Rate: {data['win_rate']:.1%}")
        cur = data["current"]
        print(f"  Current:  amplitude={cur['amplitude']}, decay={cur['decay_rate']}")
        if data["proposed"]:
            prop = data["proposed"]
            print(f"  Proposed: amplitude={prop['amplitude']}, decay={prop['decay_rate']}")
        else:
            print("  Proposed: insufficient data (keeping current)")
        print("  Buckets:")
        for bk, counts in data["buckets"].items():
            total = counts["wins"] + counts["losses"]
            wr = counts["wins"] / total if total > 0 else 0
            print(f"    {bk}c: {counts['wins']}W/{counts['losses']}L ({wr:.0%}) n={total}")

    if args.save:
        # Update bayes-params.json bucket counts
        bp = json.loads(BAYES_PARAMS_PATH.read_text()) if BAYES_PARAMS_PATH.exists() else {"kappa": 30, "categories": {}}
        for cat, data in result.get("categories", {}).items():
            bp.setdefault("categories", {}).setdefault(cat, {})["buckets"] = data["buckets"]
            if data["proposed"]:
                bp["categories"][cat]["empirical_alpha"] = data["proposed"]["amplitude"]
                bp["categories"][cat]["empirical_delta"] = data["proposed"]["decay_rate"]
        BAYES_PARAMS_PATH.write_text(json.dumps(bp, indent=2) + "\n")
        print(f"\nSaved to {BAYES_PARAMS_PATH}")
    elif not args.json:
        print("\nRun with --save to write config/bayes-params.json")


if __name__ == "__main__":
    main()
