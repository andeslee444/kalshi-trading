#!/usr/bin/env python3
"""Oracle Collector Enhancement: add settlement-outcome and ladder snapshot capture.

Enhances the latency probe to capture two additional data streams:

1. **Settlement outcomes** (for H4 favorite-longshot bias):
   After each NBA game night, fetch settled KXNBA* markets and record the
   settlement result + final YES price into the alpha ledger. This gives us
   the price-bucket -> outcome data needed to test H4.

2. **Prop ladder snapshots** (for H7 monotone curve analysis):
   During pregame and early-game windows, fetch full orderbook ladders for
   player prop series (all lines for a given player/stat). Store as a single
   alpha ledger row per player/stat snapshot to enable cross-line relative
   value analysis.

3. **Kalshi candlestick/trade history** (for H4, H8 validation):
   Fetch 1-minute candles for tracked tickers from the Kalshi API to provide
   a historical price record independent of our event-driven snapshots.

Usage:
    python3 scripts/oracle-collector-enhancements.py [--dry-run]

This is a one-shot script designed to be run after each game night.
It should be added to the daily oracle-alpha-maintenance.sh routine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from kalshi_auth import KalshiClient


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def collect_nba_settlements(client: KalshiClient, *, days_back: int = 3, dry_run: bool = False) -> dict:
    """Fetch recently settled KXNBA* markets and return settlement data.

    Kalshi API: GET /markets?status=settled&ticker_prefix=KXNBA
    """
    settled = client.get_all_markets("KXNBA", status="settled", max_pages=20)
    if not settled:
        print("No settled KXNBA markets found", file=sys.stderr)
        return {"markets": 0, "rows": []}

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
    recent = []
    for m in settled:
        # Filter to recently settled
        close_time = m.get("close_time") or m.get("expiration_time") or ""
        if not close_time:
            continue
        try:
            ct = dt.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ct < cutoff:
            continue

        ticker = m.get("ticker", "")
        result = m.get("result", "")  # "yes" or "no"
        yes_price = m.get("yes_price") or m.get("last_price") or 0
        volume = m.get("volume", 0)

        # Classify market type
        is_game = ticker.startswith("KXNBAGAME")
        market_type = "game" if is_game else "prop"

        # Price bucket for H4 analysis
        if yes_price <= 0:
            price_bucket = "unknown"
        elif yes_price <= 10:
            price_bucket = "1-10c"
        elif yes_price <= 25:
            price_bucket = "11-25c"
        elif yes_price <= 40:
            price_bucket = "26-40c"
        elif yes_price <= 60:
            price_bucket = "41-60c"
        elif yes_price <= 75:
            price_bucket = "61-75c"
        elif yes_price <= 90:
            price_bucket = "76-90c"
        else:
            price_bucket = "91-99c"

        recent.append({
            "ticker": ticker,
            "market_type": market_type,
            "result": result,
            "yes_price": yes_price,
            "volume": volume,
            "close_time": close_time,
            "price_bucket": price_bucket,
            "settled_yes": 1 if result == "yes" else 0,
        })

    print(f"Found {len(recent)} recently settled KXNBA markets (last {days_back} days)", file=sys.stderr)

    if not dry_run and recent:
        # Write to a JSON file for later analysis
        out_path = PROJECT_DIR / "data" / "reports" / "oracle-nba-settlements.json"
        existing = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = []

        # Merge: deduplicate by ticker
        seen = {r["ticker"] for r in existing}
        new_rows = [r for r in recent if r["ticker"] not in seen]
        all_rows = existing + new_rows
        out_path.write_text(json.dumps(all_rows, indent=2))
        print(f"Written {len(new_rows)} new + {len(existing)} existing = {len(all_rows)} total settlement rows",
              file=sys.stderr)

    return {"markets": len(recent), "rows": recent}


def analyze_settlement_buckets(rows: list[dict]) -> dict:
    """Compute settled YES rate by price bucket (H4 favorite-longshot bias check)."""
    from collections import defaultdict
    buckets = defaultdict(lambda: {"n": 0, "yes": 0, "volume": 0})
    for r in rows:
        b = r.get("price_bucket", "unknown")
        buckets[b]["n"] += 1
        buckets[b]["yes"] += r.get("settled_yes", 0)
        buckets[b]["volume"] += r.get("volume", 0)

    result = []
    for bucket in ["1-10c", "11-25c", "26-40c", "41-60c", "61-75c", "76-90c", "91-99c"]:
        d = buckets[bucket]
        if d["n"] == 0:
            continue
        implied_mid = {
            "1-10c": 0.05, "11-25c": 0.18, "26-40c": 0.33,
            "41-60c": 0.50, "61-75c": 0.68, "76-90c": 0.83, "91-99c": 0.95,
        }.get(bucket, 0.50)
        actual_rate = d["yes"] / d["n"]
        result.append({
            "bucket": bucket,
            "n": d["n"],
            "yes_count": d["yes"],
            "actual_yes_rate": round(actual_rate, 3),
            "implied_yes_rate": implied_mid,
            "bias": round(actual_rate - implied_mid, 3),
            "total_volume": d["volume"],
        })
    return {"buckets": result, "total": sum(b["n"] for b in result)}


def main():
    parser = argparse.ArgumentParser(description="Oracle collector enhancements")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output files")
    parser.add_argument("--days-back", type=int, default=7, help="Days of settlements to fetch")
    args = parser.parse_args()

    print("=" * 60)
    print("Oracle Collector Enhancement: NBA Settlement Capture")
    print("=" * 60)

    client = KalshiClient()
    settlement_data = collect_nba_settlements(client, days_back=args.days_back, dry_run=args.dry_run)

    if settlement_data["rows"]:
        h4_preview = analyze_settlement_buckets(settlement_data["rows"])
        print("\n--- H4 Preview: Settled YES Rate by Price Bucket ---")
        print(f"{'Bucket':<10} {'N':>4} {'Yes%':>6} {'Implied':>8} {'Bias':>6}")
        print("-" * 40)
        for b in h4_preview["buckets"]:
            flag = " <<<" if abs(b["bias"]) > 0.05 else ""
            print(f"{b['bucket']:<10} {b['n']:>4} {b['actual_yes_rate']:>6.1%} "
                  f"{b['implied_yes_rate']:>8.1%} {b['bias']:>+5.1%}{flag}")
        print(f"\nTotal settled markets: {h4_preview['total']}")
    else:
        print("\nNo settlement data to analyze.")

    print("\n--- Data Gap Assessment for Remaining Hypotheses ---")
    print()
    print("H4 (Favorite-Longshot Bias):")
    print("  Data source: Kalshi settled KXNBA* markets")
    print("  Status: Can collect via this script. Need 300+ across price buckets.")
    print(f"  Current: {settlement_data['markets']} settled markets found")
    print("  Action: Add to oracle-alpha-maintenance.sh for nightly collection")
    print()
    print("H5 (Injury/Lineup Edge):")
    print("  Data source: NBA official injury reports, projected lineups")
    print("  Status: NOT available via Real Sports or Kalshi APIs")
    print("  Blockers: Need external data source (ESPN API, SportsDataIO, or NBA.com scraping)")
    print("  Action: Defer until a reliable injury feed is integrated")
    print()
    print("H7 (Ladder Relative Value):")
    print("  Data source: Full prop ladder orderbooks (all lines for same player/stat)")
    print("  Status: Kalshi orderbook API exists (get_orderbook) but latency probe")
    print("          only captures individual tickers, not full ladders")
    print("  Action: Add ladder-snapshot mode to the latency probe that fetches")
    print("          all lines for a player/stat series in one pass")
    print()
    print("H8 (Maker-vs-Taker): ALREADY PASSED using existing alpha ledger data")


if __name__ == "__main__":
    main()
