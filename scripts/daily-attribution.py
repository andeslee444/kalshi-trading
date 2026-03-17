#!/usr/bin/env python3
"""Daily P&L Attribution Report — Generates and saves attribution analysis.

Usage:
    python3 scripts/daily-attribution.py              # Print report
    python3 scripts/daily-attribution.py --save       # Save to data/attribution-report.json
    python3 scripts/daily-attribution.py --notify     # Send edge decay alerts via WhatsApp
"""

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from pnl_attribution import PnLAttributor
from edge_monitor import EdgeMonitor
from event_ledger import DEFAULT_LEDGER_PATH, get_event_ledger
from research.trade_attribution import TradeAttributionArtifact
from trade_files import TRADE_FILES as _CANONICAL_TRADE_FILES

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

TRADE_FILES = [
    {"path": str(DATA_DIR / tf["filename"]), "bot": tf["bot"]}
    for tf in _CANONICAL_TRADE_FILES
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
    parser.add_argument("--use-ledger", action="store_true", help="Read trades from the SQLite event ledger")
    args = parser.parse_args()

    attr = PnLAttributor(
        trade_file_paths=TRADE_FILES,
        regime_state_path=str(DATA_DIR / "regime-state.json"),
        ledger_path=DEFAULT_LEDGER_PATH if args.use_ledger else None,
    )
    attr.load_trades()
    report = attr.full_report()
    report["generated_at"] = report.get("generated_at") or datetime.datetime.now(datetime.timezone.utc).isoformat()

    print_report(report)

    if args.save:
        artifact = TradeAttributionArtifact(path=DATA_DIR / "attribution-report.json")
        saved_report = artifact.save(report)
        try:
            get_event_ledger(path=DEFAULT_LEDGER_PATH).record_post_trade_attribution(
                saved_report,
                report_name=saved_report.get("report_name", "daily_attribution"),
                source_path=artifact.path,
            )
        except Exception:
            pass
        print(f"\nSaved to {artifact.path}")

    check_edge_decay(notify=args.notify)


if __name__ == "__main__":
    main()
