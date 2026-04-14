#!/usr/bin/env python3
"""Settlement backfill — queries individual market endpoints to fill in settlement outcomes.

Complements reconcile-trades.py (which uses /portfolio/settlements). This script
queries GET /markets/{ticker} for each unsettled trade, which works even when the
portfolio settlements endpoint doesn't return a match.

Also produces a summary report: win rate, P&L, average edge on wins vs losses,
and edge calibration.

Idempotent: skips records that already have settlement_result set.

Usage:
    python3 scripts/backfill-settlements.py              # backfill + summary
    python3 scripts/backfill-settlements.py --dry-run    # preview changes, don't write
    python3 scripts/backfill-settlements.py --report     # summary report only (no backfill)
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

# Add src/kalshi to path for imports
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from event_ledger import get_event_ledger
from kalshi_auth import KalshiClient, load_trades, _atomic_write_json, setup_logging
from settlement_utils import (
    compute_trade_pnl_cents,
    realized_edge_for_trade,
    settlement_payout_cents,
    settlement_result_for_trade,
    trade_has_filled_exposure,
)
from trade_files import ALL_TRADE_PATHS

log = setup_logging("backfill")

# Use canonical trade file list from trade_files module
TRADE_FILES = ALL_TRADE_PATHS


def _sync_settled_trades_to_ledger(trade_file, trades, ledger):
    """Mirror settled local trade rows into the ledger idempotently."""
    synced = 0
    for trade in trades:
        if trade.get("action", "buy") != "buy":
            continue
        if trade.get("settlement_result") is None:
            continue
        try:
            ledger.record_settlement(trade, source_path=trade_file)
            synced += 1
        except Exception as e:
            log.warning(
                "Failed to dual-write settlement for %s in %s: %s",
                trade.get("ticker", "?"),
                Path(trade_file).name,
                e,
            )
    return synced


def sync_local_settlements_to_ledger():
    """Sync settled local trade rows into the ledger without querying the API."""
    ledger = get_event_ledger(logger=log)
    total_synced = 0

    for trade_file in TRADE_FILES:
        if not trade_file.exists():
            continue
        trades = load_trades(trade_file)
        if not trades:
            continue
        synced = _sync_settled_trades_to_ledger(trade_file, trades, ledger)
        if synced:
            log.info("  %s: synced %d settled rows to ledger", trade_file.name, synced)
            total_synced += synced

    log.info("Ledger settlement sync complete: %d settled rows mirrored", total_synced)
    return total_synced


def _query_market(client, ticker):
    """Query a single market's settlement status.

    Returns dict with result and yes_price, or None if not settled.
    """
    try:
        data = client.get(f"/markets/{ticker}")
        market = data.get("market", data)
        result = market.get("result", "")
        status = market.get("status", "")

        if status == "settled" and result:
            yes_won = result == "yes"
            return {"yes_won": yes_won, "status": "settled"}
        elif status in ("finalized", "closed"):
            # Try to infer from close_time and result
            if result:
                return {"yes_won": result == "yes", "status": status}
        return None
    except Exception as e:
        log.debug("  Failed to query %s: %s", ticker, e)
        return None


def backfill(dry_run=False):
    """Walk all trade files, query unsettled tickers, annotate records."""
    client = KalshiClient()
    ledger = get_event_ledger(logger=log)

    # Gather all unsettled tickers first to batch queries
    unsettled_tickers = set()
    all_file_trades = []

    for trade_file in TRADE_FILES:
        if not trade_file.exists():
            continue
        trades = load_trades(trade_file)
        if not trades:
            continue
        all_file_trades.append((trade_file, trades))
        for t in trades:
            if t.get("settlement_result") is None:
                ticker = t.get("ticker", "")
                if ticker:
                    unsettled_tickers.add(ticker)

    if not unsettled_tickers:
        log.info("No unsettled trades found — nothing to backfill.")
        return 0

    log.info("Querying %d unsettled tickers...", len(unsettled_tickers))

    # Query each ticker (with rate limiting)
    settlements = {}
    queried = 0
    for ticker in sorted(unsettled_tickers):
        result = _query_market(client, ticker)
        if result:
            settlements[ticker] = result
        queried += 1
        if queried % 20 == 0:
            log.info("  Queried %d/%d tickers (%d settled)", queried, len(unsettled_tickers), len(settlements))
            time.sleep(0.5)  # rate limit

    log.info("  %d/%d tickers have settled", len(settlements), len(unsettled_tickers))

    # Annotate trade records
    total_annotated = 0
    for trade_file, trades in all_file_trades:
        file_modified = 0
        for trade in trades:
            # Skip sell (exit) records — legacy records without action are assumed buys
            if trade.get("action", "buy") != "buy":
                continue
            if trade.get("settlement_result") is not None:
                continue
            has_exposure, contract_count, _fill_price_cents = trade_has_filled_exposure(trade)
            if not has_exposure:
                continue

            ticker = trade.get("ticker", "")
            if ticker not in settlements:
                continue

            s = settlements[ticker]
            yes_won = s["yes_won"]
            trade["settlement_result"] = settlement_result_for_trade(trade.get("side", "yes"), yes_won)
            trade["settlement_revenue_cents"] = settlement_payout_cents(
                trade["settlement_result"],
                contract_count,
            )
            trade["realized_edge"] = realized_edge_for_trade(trade, trade["settlement_result"])

            file_modified += 1

        if file_modified > 0:
            log.info("  %s: %d records annotated", trade_file.name, file_modified)
            if not dry_run:
                _atomic_write_json(trade_file, trades)
            total_annotated += file_modified
        if not dry_run:
            _sync_settled_trades_to_ledger(trade_file, trades, ledger)

    log.info("Backfill complete: %d records annotated%s",
             total_annotated, " (DRY RUN)" if dry_run else "")
    return total_annotated


def summary_report():
    """Print a summary report of all settled trades."""
    all_trades = []
    for trade_file in TRADE_FILES:
        if not trade_file.exists():
            continue
        trades = load_trades(trade_file)
        all_trades.extend(trades)

    settled = [t for t in all_trades if t.get("settlement_result") is not None]
    unsettled = [t for t in all_trades if t.get("settlement_result") is None]

    if not settled:
        log.info("No settled trades to report on.")
        return

    wins = [t for t in settled if t["settlement_result"] == "won"]
    losses = [t for t in settled if t["settlement_result"] == "lost"]

    # P&L
    total_pnl = sum(compute_trade_pnl_cents(t)[0] for t in settled)

    # Average edge on wins vs losses
    win_edges = [t["model_prob"] - (t.get("price_cents", 50) or 50) / 100.0
                 for t in wins if t.get("model_prob") is not None]
    loss_edges = [t["model_prob"] - (t.get("price_cents", 50) or 50) / 100.0
                  for t in losses if t.get("model_prob") is not None]

    # Edge calibration: bucket model probabilities and compare to actual win rates
    calibration = defaultdict(lambda: {"count": 0, "wins": 0})
    for t in settled:
        if t.get("model_prob") is not None:
            bucket = round(t["model_prob"] * 10) / 10  # bucket to nearest 0.1
            calibration[bucket]["count"] += 1
            if t["settlement_result"] == "won":
                calibration[bucket]["wins"] += 1

    # Print report
    print("\n" + "=" * 60)
    print("  SETTLEMENT SUMMARY REPORT")
    print("=" * 60)
    print(f"\n  Total trades:     {len(all_trades)}")
    print(f"  Settled:          {len(settled)}")
    print(f"  Unsettled:        {len(unsettled)}")
    print(f"\n  Wins:             {len(wins)}")
    print(f"  Losses:           {len(losses)}")
    print(f"  Win rate:         {len(wins)/len(settled)*100:.1f}%")
    print(f"\n  Total P&L:        ${total_pnl/100:+.2f}")
    print(f"  Avg P&L/trade:    ${total_pnl/len(settled)/100:+.2f}")

    if win_edges:
        print(f"\n  Avg edge (wins):  {sum(win_edges)/len(win_edges)*100:+.1f}%")
    if loss_edges:
        print(f"  Avg edge (losses):{sum(loss_edges)/len(loss_edges)*100:+.1f}%")

    if calibration:
        print(f"\n  Edge Calibration (model_prob bucket → actual win rate):")
        print(f"  {'Bucket':>8} {'Count':>6} {'Wins':>6} {'Win%':>7} {'Gap':>7}")
        print(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*7} {'-'*7}")
        for bucket in sorted(calibration.keys()):
            c = calibration[bucket]
            actual_wr = c["wins"] / c["count"] if c["count"] > 0 else 0
            gap = actual_wr - bucket
            print(f"  {bucket:>8.1f} {c['count']:>6d} {c['wins']:>6d} "
                  f"{actual_wr*100:>6.1f}% {gap*100:>+6.1f}%")

    # Per-bot breakdown
    by_bot = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    for t in settled:
        bot = t.get("source_bot", t.get("bot", t.get("source", "unknown")))
        if t["settlement_result"] == "won":
            by_bot[bot]["wins"] += 1
        else:
            by_bot[bot]["losses"] += 1
        by_bot[bot]["pnl"] += compute_trade_pnl_cents(t)[0]

    if by_bot:
        print(f"\n  Per-Bot Breakdown:")
        print(f"  {'Bot':<25} {'W':>4} {'L':>4} {'Win%':>7} {'P&L':>10}")
        print(f"  {'-'*25} {'-'*4} {'-'*4} {'-'*7} {'-'*10}")
        for bot in sorted(by_bot.keys()):
            b = by_bot[bot]
            total = b["wins"] + b["losses"]
            wr = b["wins"] / total * 100 if total > 0 else 0
            print(f"  {bot:<25} {b['wins']:>4d} {b['losses']:>4d} "
                  f"{wr:>6.1f}% ${b['pnl']/100:>+9.2f}")

    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill settlement results from Kalshi API")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--report", action="store_true", help="Summary report only (no backfill)")
    parser.add_argument(
        "--sync-ledger-only",
        action="store_true",
        help="Sync settled local trade rows into the ledger without API calls",
    )
    args = parser.parse_args()

    if args.sync_ledger_only:
        sync_local_settlements_to_ledger()
    elif not args.report:
        backfill(dry_run=args.dry_run)

    summary_report()
