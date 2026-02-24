#!/usr/bin/env python3
"""Automated trade reconciliation — annotates trade records with settlement outcomes.

Walks all trade log files, matches each trade to API settlement data and fill info,
and writes settlement_result, settlement_revenue_cents, fill_price_cents, and
realized_edge back into the trade record.

Idempotent: skips records that already have settlement_result set.
Atomic writes: uses _atomic_write_json for safe file updates.

Usage:
    python3 scripts/reconcile-trades.py              # annotate all trade files
    python3 scripts/reconcile-trades.py --dry-run    # show what would change, don't write
"""

import argparse
import json
import sys
from pathlib import Path

# Add src/kalshi to path for imports
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from kalshi_auth import KalshiClient, load_trades, _atomic_write_json, setup_logging, PROJECT_DIR

log = setup_logging("reconcile")

# All trade log files produced by bots
TRADE_FILES = [
    PROJECT_DIR / "data" / "kalshi-trades.json",
    PROJECT_DIR / "data" / "kalshi-strategy-trades.json",
    PROJECT_DIR / "data" / "kalshi-entertainment-trades.json",
    PROJECT_DIR / "data" / "kalshi-monitor-trades.json",
    PROJECT_DIR / "data" / "kalshi-economics-trades.json",
    PROJECT_DIR / "data" / "kalshi-crypto-trades.json",
    PROJECT_DIR / "data" / "kalshi-position-trades.json",
    PROJECT_DIR / "data" / "beatrelease-trades.json",
]


def _fetch_all_settlements(client):
    """Fetch all settled positions from the API (paginated).

    Returns dict: {ticker: {"revenue_cents": int, "yes_won": bool, "settled_time": str}}
    """
    settlements = {}
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            log.warning("Failed to fetch settlements: %s", e)
            break
        for s in data.get("settlements", []):
            ticker = s.get("ticker", s.get("market_ticker", ""))
            if ticker:
                settlements[ticker] = {
                    "revenue_cents": s.get("revenue", 0),
                    "yes_won": s.get("market_result", "") == "yes",
                    "settled_time": s.get("settled_time", ""),
                }
        cursor = data.get("cursor")
        if not cursor or not data.get("settlements"):
            break
    return settlements


def _fetch_all_fills(client):
    """Fetch all order fills from the API (paginated).

    Returns dict: {order_id: {"fill_price_cents": int, "fill_count": int}}
    """
    fills = {}
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            log.warning("Failed to fetch fills: %s", e)
            break
        for f in data.get("fills", []):
            order_id = f.get("order_id", "")
            if order_id:
                if order_id in fills:
                    existing = fills[order_id]
                    total_count = existing["fill_count"] + (f.get("count", 0) or 0)
                    if total_count > 0:
                        existing["fill_price_cents"] = int(
                            (existing["fill_price_cents"] * existing["fill_count"] +
                             (f.get("yes_price", 0) or f.get("no_price", 0)) * (f.get("count", 0) or 0))
                            / total_count
                        )
                        existing["fill_count"] = total_count
                else:
                    fills[order_id] = {
                        "fill_price_cents": f.get("yes_price", 0) or f.get("no_price", 0),
                        "fill_count": f.get("count", 0) or 0,
                    }
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break
    return fills


def _annotate_trade(trade, settlements, fills):
    """Annotate a single trade record with settlement/fill data.

    Returns True if the record was modified.
    """
    # Skip already-annotated records
    if trade.get("settlement_result") is not None:
        return False

    ticker = trade.get("ticker", "")
    order_id = trade.get("order_id", "")
    modified = False

    # Match settlement
    if ticker in settlements:
        s = settlements[ticker]
        side = trade.get("side", "yes")
        yes_won = s["yes_won"]

        if side == "yes":
            trade["settlement_result"] = "won" if yes_won else "lost"
        else:
            trade["settlement_result"] = "won" if not yes_won else "lost"

        trade["settlement_revenue_cents"] = s["revenue_cents"]
        modified = True

    # Match fill price
    if order_id and order_id in fills:
        f = fills[order_id]
        trade["fill_price_cents"] = f["fill_price_cents"]
        modified = True

    # Compute realized edge if we have enough data
    if trade.get("settlement_result") and trade.get("model_prob") is not None:
        actual = 1.0 if trade["settlement_result"] == "won" else 0.0
        fill_price = trade.get("fill_price_cents") or trade.get("price_cents", 50) or 50
        implied = fill_price / 100.0
        trade["realized_edge"] = round(actual - implied, 4)
        modified = True

    return modified


def reconcile_all(dry_run=False):
    """Walk all trade files, match to API settlements/fills, annotate records."""
    client = KalshiClient()

    log.info("Fetching settlements and fills from API...")
    settlements = _fetch_all_settlements(client)
    fills = _fetch_all_fills(client)
    log.info("  %d settlements, %d fills fetched", len(settlements), len(fills))

    total_annotated = 0
    total_skipped = 0

    for trade_file in TRADE_FILES:
        if not trade_file.exists():
            continue

        trades = load_trades(trade_file)
        if not trades:
            continue

        file_modified = 0
        for trade in trades:
            if _annotate_trade(trade, settlements, fills):
                file_modified += 1
            else:
                total_skipped += 1

        if file_modified > 0:
            log.info("  %s: %d/%d records annotated", trade_file.name, file_modified, len(trades))
            if not dry_run:
                _atomic_write_json(trade_file, trades)
            total_annotated += file_modified

    log.info("Reconciliation complete: %d annotated, %d skipped (already done or no match)",
             total_annotated, total_skipped)
    if dry_run:
        log.info("DRY RUN — no files were modified")

    return total_annotated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile trade records with API data")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    reconcile_all(dry_run=args.dry_run)
