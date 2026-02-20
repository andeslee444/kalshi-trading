#!/usr/bin/env python3
"""Daily P&L summary report with optional WhatsApp notification.

Reads trade logs, queries Kalshi API for settlements/fills, and formats
a concise daily summary. Optionally sends via WhatsApp using openclaw CLI.

Usage:
    python3 scripts/daily-report.py              # Print report to stdout
    python3 scripts/daily-report.py --notify     # Print + send via WhatsApp
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import KalshiClient, notify_whatsapp, setup_logging

# Import helpers from analyze-performance (same directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module
import importlib.util

# Load analyze-performance module (hyphenated filename)
_perf_spec = importlib.util.spec_from_file_location(
    "analyze_performance",
    str(Path(__file__).resolve().parent / "analyze-performance.py"),
)
_perf_mod = importlib.util.module_from_spec(_perf_spec)
_perf_spec.loader.exec_module(_perf_mod)

log = setup_logging("daily-report")


def build_report() -> dict:
    """Build the daily report data by reconciling local trades with Kalshi API."""
    client = KalshiClient()

    # Get balance
    try:
        total_balance, available_balance = client.get_balance()
    except Exception as e:
        log.error("Balance fetch failed: %s", e)
        total_balance = available_balance = 0

    # Fetch settlements and fills
    settlements = _perf_mod.fetch_settlements(client)
    fills = _perf_mod.fetch_fills(client)

    # Load local trade logs
    local_trades_by_bot = []
    for tf in _perf_mod.TRADE_FILES:
        trades = _perf_mod.load_trades_safe(tf["path"])
        local_trades_by_bot.append({
            "label": tf["label"],
            "trades": trades or [],
        })

    # Reconcile
    reconciliation = _perf_mod.reconcile_trades(local_trades_by_bot, settlements, fills)

    # Local trade stats
    results = []
    for tf in _perf_mod.TRADE_FILES:
        trades = _perf_mod.load_trades_safe(tf["path"])
        if trades:
            stats = _perf_mod.analyze_trades(trades)
            results.append({"label": tf["label"], "stats": stats})

    return {
        "total_balance": total_balance,
        "available_balance": available_balance,
        "reconciliation": reconciliation,
        "bot_stats": results,
    }


def format_report(data: dict) -> str:
    """Format the report data into a concise text summary."""
    lines = []
    lines.append("=== Kalshi Daily Report ===")
    lines.append("")

    # Balance
    total = data["total_balance"]
    avail = data["available_balance"]
    lines.append(f"Balance: ${total/100:.2f} (available: ${avail/100:.2f})")

    # Reconciliation summary
    rec = data.get("reconciliation")
    if rec:
        agg = rec["aggregate"]
        total_settled = agg["wins"] + agg["losses"]
        lines.append("")
        lines.append(f"Settled: {total_settled} trades")
        if total_settled > 0:
            wr_str = f"{agg['win_rate']*100:.1f}%"
            parts = []
            if agg.get("yes_win_rate") is not None:
                parts.append(f"YES: {agg['yes_win_rate']*100:.1f}%")
            if agg.get("no_win_rate") is not None:
                parts.append(f"NO: {agg['no_win_rate']*100:.1f}%")
            if parts:
                wr_str += f" ({', '.join(parts)})"
            lines.append(f"Win rate: {wr_str}")
        lines.append(f"P&L: ${agg['pnl_cents']/100:.2f}")
        if agg.get("sharpe") is not None:
            lines.append(f"Sharpe: {agg['sharpe']:.2f}")

        # Per-bot breakdown
        lines.append("")
        lines.append("Per-bot:")
        for bot in rec.get("per_bot", []):
            settled = bot["wins"] + bot["losses"]
            if settled > 0 or bot["pnl_cents"] != 0:
                lines.append(
                    f"  {bot['label']}: {bot['wins']}W/{bot['losses']}L "
                    f"({bot['win_rate']*100:.0f}%) P&L=${bot['pnl_cents']/100:.2f}"
                )

    # Recent trade count
    total_trades = sum(
        r["stats"]["total"] for r in data.get("bot_stats", []) if r.get("stats")
    )
    if total_trades:
        lines.append("")
        lines.append(f"Total local trades: {total_trades}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Daily P&L report")
    parser.add_argument("--notify", action="store_true",
                        help="Send report via WhatsApp")
    args = parser.parse_args()

    log.info("Building daily report...")
    data = build_report()
    report = format_report(data)

    print(report)

    if args.notify:
        log.info("Sending WhatsApp notification...")
        notify_whatsapp(report, logger=log)


if __name__ == "__main__":
    main()
