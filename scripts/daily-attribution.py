#!/usr/bin/env python3
"""Daily P&L Attribution Report — Generates and saves attribution analysis.

Usage:
    python3 scripts/daily-attribution.py              # Print report
    python3 scripts/daily-attribution.py --save       # Save to data/attribution-report.json
    python3 scripts/daily-attribution.py --notify     # Send edge decay alerts via WhatsApp
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from pnl_attribution import PnLAttributor
from edge_monitor import EdgeMonitor

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

# Trade file definitions (mirrors dashboard.py / trade_files.py)
TRADE_FILES = [
    {"path": str(DATA_DIR / "kalshi-trades.json"), "bot": "weather"},
    {"path": str(DATA_DIR / "kalshi-strategy-trades.json"), "bot": "strategy"},
    {"path": str(DATA_DIR / "kalshi-entertainment-trades.json"), "bot": "entertainment"},
    {"path": str(DATA_DIR / "beatrelease-trades.json"), "bot": "beatrelease"},
    {"path": str(DATA_DIR / "kalshi-monitor-trades.json"), "bot": "monitor"},
    {"path": str(DATA_DIR / "kalshi-position-trades.json"), "bot": "positions"},
    {"path": str(DATA_DIR / "kalshi-economics-trades.json"), "bot": "economics"},
    {"path": str(DATA_DIR / "kalshi-crypto-trades.json"), "bot": "crypto"},
    {"path": str(DATA_DIR / "kalshi-arb-trades.json"), "bot": "arb"},
    {"path": str(DATA_DIR / "kalshi-mm-trades.json"), "bot": "mm"},
]


def print_report(report):
    """Print human-readable attribution report."""
    print("=" * 60)
    print("P&L ATTRIBUTION REPORT")
    print("=" * 60)

    s = report["summary"]
    print(f"\nSettled trades: {s['total_trades_settled']} of {s['total_trades_all']} total")
    print(f"Total P&L: ${s['total_pnl_cents'] / 100:.2f}")

    print("\n--- By Bot ---")
    for bot, stats in sorted(report["by_bot"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {bot:20s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

    print("\n--- By Edge Bucket ---")
    for bucket, stats in report["by_edge_bucket"].items():
        print(f"  {bucket:10s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR  "
              f"avg edge {stats.get('avg_edge', 0)*100:.1f}%")

    print("\n--- By Market Type ---")
    for mt, stats in sorted(report["by_market_type"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {mt:15s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")


def check_edge_decay(notify=False):
    """Check edge monitor for decay warnings."""
    em = EdgeMonitor(state_path=str(DATA_DIR / "edge-monitor-state.json"))
    em.load_state()
    report = em.json_report()

    alerts = []
    for mt, info in report.get("market_types", {}).items():
        if info.get("competitor_detected"):
            alerts.append(f"Competitor detected in {mt} markets")
        hl = info.get("edge_half_life_days", float("inf"))
        if hl != float("inf") and hl < 30:
            alerts.append(f"{mt} edge half-life is {hl:.0f} days (< 30 day threshold)")

    if alerts:
        msg = "EDGE DECAY ALERT\n" + "\n".join(alerts)
        print(f"\n{msg}")
        if notify:
            try:
                from kalshi_auth import notify_whatsapp
                notify_whatsapp(msg)
            except Exception as e:
                print(f"WhatsApp notification failed: {e}")

    return alerts


def main():
    parser = argparse.ArgumentParser(description="Daily P&L Attribution")
    parser.add_argument("--save", action="store_true", help="Save report to JSON")
    parser.add_argument("--notify", action="store_true", help="Send WhatsApp alerts")
    args = parser.parse_args()

    attr = PnLAttributor(
        trade_file_paths=TRADE_FILES,
        regime_state_path=str(DATA_DIR / "regime-state.json"),
    )
    attr.load_trades()
    report = attr.full_report()

    print_report(report)

    if args.save:
        out_path = DATA_DIR / "attribution-report.json"
        with open(str(out_path) + ".tmp", "w") as f:
            json.dump(report, f, indent=2)
        os.replace(str(out_path) + ".tmp", str(out_path))
        print(f"\nSaved to {out_path}")

    check_edge_decay(notify=args.notify)


if __name__ == "__main__":
    main()
