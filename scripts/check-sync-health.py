#!/usr/bin/env python3
"""Post-download data integrity report.

Prints a summary of data freshness, completeness, and reconciliation status.
Non-blocking — always exits 0. Purely informational.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from trade_files import TRADE_FILES


def check(data_dir=None, project_dir=None):
    """Run integrity checks and print report.

    Returns a dict with summary stats for testability.
    """
    d = data_dir or DATA_DIR
    proj = project_dir or PROJECT_DIR

    print("=" * 60)
    print("POST-DOWNLOAD DATA INTEGRITY REPORT")
    print("=" * 60)

    # Trade log summary
    total_trades = 0
    total_reconciled = 0
    total_unreconciled = 0
    missing_source_bot = 0

    for tf in TRADE_FILES:
        path = d / tf["filename"]
        if not path.exists():
            print(f"  {tf['label']:25s}  MISSING")
            continue
        try:
            trades = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  {tf['label']:25s}  CORRUPT JSON")
            continue

        n = len(trades)
        settled = sum(1 for t in trades if t.get("settlement_result"))
        unsettled = sum(1 for t in trades
                        if not t.get("settlement_result")
                        and t.get("status") == "executed")
        no_bot = sum(1 for t in trades if not t.get("source_bot"))

        total_trades += n
        total_reconciled += settled
        total_unreconciled += unsettled
        missing_source_bot += no_bot

        # Freshness: most recent trade timestamp
        if trades:
            latest = max(t.get("timestamp", "") for t in trades)
            print(f"  {tf['label']:25s}  {n:4d} trades  "
                  f"{settled:3d} settled  {unsettled:3d} pending  "
                  f"latest={latest[:16]}")
        else:
            print(f"  {tf['label']:25s}  empty")

    print(f"\n  TOTAL: {total_trades} trades, "
          f"{total_reconciled} reconciled, "
          f"{total_unreconciled} pending reconciliation")

    if missing_source_bot > 0:
        print(f"  WARNING: {missing_source_bot} trades missing source_bot attribution")

    # Snapshot freshness
    print()
    snap_path = d / "financial-snapshot.json"
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text())
            gen = snap.get("generated_at", "unknown")
            nav = snap.get("account", {}).get("nav_cents", 0)
            pnl = snap.get("realized_pnl", {}).get("net_after_fees_cents", 0)
            orphans = len(snap.get("verification", {}).get("orphan_settlements", []))
            print(f"  Snapshot: generated={gen}")
            print(f"  NAV: ${nav/100:.2f}  |  Net P&L: ${pnl/100:.2f}  |  Orphans: {orphans}")
        except (json.JSONDecodeError, ValueError):
            print("  Snapshot: CORRUPT")
    else:
        print("  Snapshot: MISSING — run npm run snapshot")

    # Calibration freshness
    cal_path = proj / "config" / "calibration.json"
    if cal_path.exists():
        try:
            cal = json.loads(cal_path.read_text())
            gen = cal.get("generated", "unknown")
            setts = cal.get("total_settlements", "?")
            print(f"  Calibration: generated={gen}, settlements={setts}")
        except json.JSONDecodeError:
            print("  Calibration: CORRUPT")
    else:
        print("  Calibration: MISSING — run npm run calibrate")

    # Per-bot metrics files
    print()
    metrics_files = list(d.glob("*-metrics.json"))
    if metrics_files:
        print(f"  Bot metrics files: {len(metrics_files)}")
        for mf in sorted(metrics_files):
            try:
                data = json.loads(mf.read_text())
                if isinstance(data, list) and data:
                    latest = data[-1].get("timestamp", "?")
                    print(f"    {mf.name}: {len(data)} entries, latest={latest[:16]}")
                elif isinstance(data, dict):
                    print(f"    {mf.name}: dict with {len(data)} keys")
            except json.JSONDecodeError:
                print(f"    {mf.name}: CORRUPT")
    else:
        print("  Bot metrics files: none (Plans 2-8 not yet implemented)")

    # Health state
    health_path = d / "health-state.json"
    if health_path.exists():
        try:
            h = json.loads(health_path.read_text())
            bots = h.get("bots", {})
            if bots:
                print()
                print(f"  Bot heartbeats ({len(bots)}):")
                for name, info in sorted(bots.items()):
                    hb = info.get("last_heartbeat", "?")
                    print(f"    {name:20s}  {hb[:19]}")
        except json.JSONDecodeError:
            pass

    # Recommendations
    print()
    recs = []
    if total_unreconciled > 10:
        recs.append("Run `npm run reconcile` to annotate settled trades")
    if not snap_path.exists():
        recs.append("Run `npm run snapshot` for verified P&L")
    if missing_source_bot > 0:
        recs.append(f"Fix {missing_source_bot} trades missing source_bot")

    if recs:
        print("  RECOMMENDATIONS:")
        for r in recs:
            print(f"    - {r}")
    else:
        print("  All checks OK.")

    print("=" * 60)

    return {
        "total_trades": total_trades,
        "total_reconciled": total_reconciled,
        "total_unreconciled": total_unreconciled,
        "missing_source_bot": missing_source_bot,
        "recommendations": recs,
    }


if __name__ == "__main__":
    check()
