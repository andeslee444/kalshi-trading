#!/usr/bin/env python3
"""Skip Audit Tool — analyse decision logs for missed opportunities.

Reads decision log files written by all trading bots and produces:
  1. Skip distribution: count of skip reasons per bot
  2. Money left on table: skipped decisions with positive edge, sorted by edge
  3. Summary report: human-readable text overview

Usage:
  python3 scripts/skip-audit.py                         # all decision files
  python3 scripts/skip-audit.py --bot crypto             # filter to one bot
  python3 scripts/skip-audit.py --json                   # JSON output
  python3 scripts/skip-audit.py --days 7                 # last 7 days only
  python3 scripts/skip-audit.py data/my-decisions.json   # specific files
"""

import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def load_decisions(paths):
    """Load and merge JSON decision files.  Skip missing or corrupt files."""
    all_decisions = []
    for p in paths:
        try:
            with open(p, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                all_decisions.extend(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return all_decisions


def skip_distribution(decisions):
    """Return ``{bot: {reason: count}}`` for skipped/rejected decisions only."""
    dist = defaultdict(lambda: defaultdict(int))
    for d in decisions:
        action = d.get("action", "")
        if action in ("skipped", "rejected"):
            bot = d.get("source_bot", "unknown")
            reason = d.get("reason", "unknown")
            dist[bot][reason] += 1
    # Convert to plain dicts for JSON serialisation
    return {bot: dict(reasons) for bot, reasons in dist.items()}


def money_left_on_table(decisions):
    """Return skipped decisions with positive edge and valid price, sorted by edge descending.

    Each returned dict includes an ``estimated_pnl`` field:
        estimated_pnl = edge * (100 - price_cents)
    representing the expected profit per contract in cents.
    """
    results = []
    for d in decisions:
        action = d.get("action", "")
        if action not in ("skipped", "rejected"):
            continue
        edge = d.get("edge")
        price = d.get("price_cents")
        if edge is None or price is None or edge <= 0:
            continue
        estimated_pnl = edge * (100 - price)
        results.append({
            "ticker": d.get("ticker", ""),
            "source_bot": d.get("source_bot", "unknown"),
            "reason": d.get("reason", ""),
            "edge": edge,
            "price_cents": price,
            "estimated_pnl": round(estimated_pnl, 4),
            "timestamp": d.get("timestamp", ""),
            "side": d.get("side", ""),
        })
    results.sort(key=lambda x: x["edge"], reverse=True)
    return results


def summary_report(decisions):
    """Produce a human-readable text summary of skip patterns."""
    if not decisions:
        return "No decisions to analyse."

    dist = skip_distribution(decisions)
    money = money_left_on_table(decisions)

    lines = []
    lines.append("=" * 60)
    lines.append("SKIP AUDIT REPORT")
    lines.append("=" * 60)

    total_decisions = len(decisions)
    total_skips = sum(1 for d in decisions if d.get("action") in ("skipped", "rejected"))
    total_placed = sum(1 for d in decisions if d.get("action") == "placed")
    lines.append(f"\nTotal decisions: {total_decisions}")
    lines.append(f"  Placed: {total_placed}")
    lines.append(f"  Skipped/rejected: {total_skips}")
    if total_decisions > 0:
        lines.append(f"  Skip rate: {total_skips / total_decisions * 100:.1f}%")

    lines.append("\n" + "-" * 60)
    lines.append("SKIP DISTRIBUTION BY BOT")
    lines.append("-" * 60)

    for bot in sorted(dist.keys()):
        reasons = dist[bot]
        bot_total = sum(reasons.values())
        lines.append(f"\n  {bot} ({bot_total} skips):")
        for reason in sorted(reasons, key=reasons.get, reverse=True):
            lines.append(f"    {reason}: {reasons[reason]}")

    if money:
        lines.append("\n" + "-" * 60)
        lines.append("TOP MONEY LEFT ON TABLE (by edge)")
        lines.append("-" * 60)
        for item in money[:10]:
            lines.append(
                f"  {item['ticker']}  edge={item['edge']:.2%}  "
                f"price={item['price_cents']}c  est_pnl={item['estimated_pnl']:.2f}c  "
                f"reason={item['reason']}  bot={item['source_bot']}"
            )

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit skip/reject decisions across trading bots"
    )
    parser.add_argument(
        "files", nargs="*",
        help="Decision log files to analyse (default: data/*-decisions.json)"
    )
    parser.add_argument(
        "--bot", type=str, default=None,
        help="Filter to a specific source_bot"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output as JSON instead of text"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="Only include decisions from the last N days"
    )
    args = parser.parse_args()

    # Resolve file paths
    if args.files:
        paths = args.files
    else:
        paths = sorted(glob.glob(str(DATA_DIR / "*-decisions.json")))

    decisions = load_decisions(paths)

    # Filter by bot
    if args.bot:
        decisions = [d for d in decisions if d.get("source_bot") == args.bot]

    # Filter by days
    if args.days is not None:
        cutoff = datetime.now() - timedelta(days=args.days)
        filtered = []
        for d in decisions:
            ts = d.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                if dt >= cutoff:
                    filtered.append(d)
            except (ValueError, TypeError):
                continue
        decisions = filtered

    if args.json_output:
        output = {
            "skip_distribution": skip_distribution(decisions),
            "money_left_on_table": money_left_on_table(decisions),
            "total_decisions": len(decisions),
            "total_skips": sum(
                1 for d in decisions
                if d.get("action") in ("skipped", "rejected")
            ),
        }
        print(json.dumps(output, indent=2))
    else:
        print(summary_report(decisions))


if __name__ == "__main__":
    main()
