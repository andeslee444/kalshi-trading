"""Kalshi orderbook snapshotter — captures live bid/ask prices for NBA props.

Fetches production orderbook data (unauthenticated) for all open NBA player
prop markets. Stores snapshots that can be used to:
1. Replace proxy market prices in backtester with real Kalshi prices
2. Track price movements over time
3. Identify mispriced markets in real-time

Usage:
    python3 -m collect.kalshi_orderbook
    python3 -m collect.kalshi_orderbook --series KXNBAPTS,KXNBAREB
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

_log = logging.getLogger("collect.kalshi_orderbook")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

NBA_PROP_SERIES = [
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT",
    "KXNBASTL", "KXNBABLK",
]


def _get_open_markets(series_ticker: str, limit: int = 200) -> list[dict]:
    """Fetch open markets for a series from production API (unauthenticated)."""
    all_markets = []
    cursor = None
    for _ in range(20):  # max pages
        params = {"series_ticker": series_ticker, "status": "open", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=15)
        if resp.status_code != 200:
            _log.warning("Markets API returned %d for %s", resp.status_code, series_ticker)
            break
        data = resp.json()
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor")
        if not cursor or not markets:
            break
    return all_markets


def _get_orderbook(ticker: str) -> dict:
    """Fetch orderbook for a single market (unauthenticated)."""
    resp = requests.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", timeout=10)
    if resp.status_code != 200:
        return {}
    return resp.json()


def _parse_orderbook(ob_data: dict) -> dict:
    """Parse orderbook into yes_bid, yes_ask, spread, depth."""
    ob = ob_data.get("orderbook_fp", ob_data.get("orderbook", {}))
    yes_bids = ob.get("yes_dollars", ob.get("yes", []))
    no_bids = ob.get("no_dollars", ob.get("no", []))

    result = {
        "yes_bid_levels": len(yes_bids),
        "no_bid_levels": len(no_bids),
        "yes_bid": 0.0,
        "yes_ask": 0.0,
        "yes_bid_depth": 0,
        "yes_ask_depth": 0,
        "spread": 1.0,
        "midpoint": 0.5,
    }

    if yes_bids:
        best = yes_bids[-1]
        result["yes_bid"] = float(best[0])
        result["yes_bid_depth"] = int(float(best[1]))

    if no_bids:
        best = no_bids[-1]
        result["yes_ask"] = round(1.0 - float(best[0]), 4)
        result["yes_ask_depth"] = int(float(best[1]))

    if result["yes_bid"] > 0 and result["yes_ask"] > 0:
        result["spread"] = round(result["yes_ask"] - result["yes_bid"], 4)
        result["midpoint"] = round((result["yes_bid"] + result["yes_ask"]) / 2, 4)

    return result


def snapshot_orderbooks(
    series_list: list[str] | None = None,
    max_markets: int = 500,
) -> dict:
    """Capture orderbook snapshots for all open NBA prop markets."""
    if series_list is None:
        series_list = NBA_PROP_SERIES

    snapshot_dir = _PROJECT_ROOT / "data" / "processed" / "kalshi_orderbooks"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_file = snapshot_dir / f"snapshot_{timestamp}.json"

    _log.info("Fetching open NBA prop markets from production Kalshi...")

    all_markets = []
    for series in series_list:
        markets = _get_open_markets(series)
        _log.info("  %s: %d open markets", series, len(markets))
        all_markets.extend(markets)
        time.sleep(0.2)

    _log.info("Total open markets: %d", len(all_markets))

    if len(all_markets) > max_markets:
        all_markets = all_markets[:max_markets]

    snapshots = []
    with_depth = 0
    start = time.time()

    for i, m in enumerate(all_markets):
        ticker = m.get("ticker", "")
        try:
            ob_data = _get_orderbook(ticker)
            parsed = _parse_orderbook(ob_data)
            parsed["ticker"] = ticker
            parsed["title"] = m.get("title", "")
            parsed["series_ticker"] = m.get("series_ticker", "")
            parsed["volume"] = m.get("volume", 0)
            parsed["open_interest"] = m.get("open_interest", 0)
            snapshots.append(parsed)

            if parsed["yes_bid_levels"] > 0 or parsed["no_bid_levels"] > 0:
                with_depth += 1

        except Exception as e:
            _log.debug("Error on %s: %s", ticker, e)

        if (i + 1) % 100 == 0:
            elapsed = time.time() - start
            _log.info("[%d/%d] %d with depth (%.1fs)", i + 1, len(all_markets), with_depth, elapsed)

        time.sleep(0.05)  # light rate limiting

    elapsed = time.time() - start
    _log.info("Complete: %d snapshots, %d with depth in %.1fs", len(snapshots), with_depth, elapsed)

    # Save snapshot
    output = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "markets_total": len(snapshots),
        "markets_with_depth": with_depth,
        "snapshots": snapshots,
    }
    with open(snapshot_file, "w") as f:
        json.dump(output, f, indent=2)

    _log.info("Saved to %s", snapshot_file.name)

    # Print summary of tightest spreads
    tight = sorted(
        [s for s in snapshots if s["spread"] < 0.50 and s["yes_bid"] > 0],
        key=lambda s: s["spread"],
    )

    if tight:
        print(f"\nTightest spreads ({len(tight)} tradeable markets):")
        for s in tight[:15]:
            print(f"  {s['ticker']:<55} bid={s['yes_bid']:.2f} ask={s['yes_ask']:.2f} "
                  f"spread={s['spread']:.2f} mid={s['midpoint']:.2f} "
                  f"depth={s['yes_bid_depth']}/{s['yes_ask_depth']}")

    return {
        "markets": len(snapshots),
        "with_depth": with_depth,
        "tradeable": len(tight),
        "snapshot_file": str(snapshot_file),
    }


def main():
    parser = argparse.ArgumentParser(description="Snapshot Kalshi NBA prop orderbooks")
    parser.add_argument("--series", type=str, default=",".join(NBA_PROP_SERIES))
    parser.add_argument("--max-markets", type=int, default=500)
    args = parser.parse_args()

    series = [s.strip() for s in args.series.split(",")]
    snapshot_orderbooks(series_list=series, max_markets=args.max_markets)


if __name__ == "__main__":
    main()
