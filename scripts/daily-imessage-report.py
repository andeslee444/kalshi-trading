#!/usr/bin/env python3
"""Daily P&L iMessage report via BlueBubbles.

Reads the financial snapshot and sends a concise performance summary
to iMessage. Designed to run after pnl-snapshot.py in daily automation.

Usage:
    python3 scripts/daily-imessage-report.py           # Send report
    python3 scripts/daily-imessage-report.py --dry-run  # Print only, don't send
"""

import argparse
import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
from kalshi_auth import setup_logging

log = setup_logging("daily-imessage-report")

PROJECT_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = PROJECT_DIR / "data" / "financial-snapshot.json"
BACKTEST_PATH = PROJECT_DIR / "data" / "backtest-results.json"

BB_URL = "http://localhost:1234/api/v1/message/text"
BB_PASSWORD = "Cheeseslice8!"
BB_CHAT_GUID = "iMessage;-;+14255336828"


def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        log.error("No financial snapshot found at %s", SNAPSHOT_PATH)
        sys.exit(1)
    return json.loads(SNAPSHOT_PATH.read_text())


def format_dollars(cents: int) -> str:
    """Format cents as dollar string with sign."""
    if cents >= 0:
        return f"+${cents / 100:.2f}"
    return f"-${abs(cents) / 100:.2f}"


def build_report(snap: dict) -> str:
    lines = []
    now = datetime.now().strftime("%b %d, %Y")
    lines.append(f"Kalshi Daily Report - {now}")
    lines.append("=" * 32)

    # Account overview
    acct = snap.get("account", {})
    bc = snap.get("balance_check", {})
    nav = acct.get("nav_cents", 0)
    balance = acct.get("balance_cents", 0)
    portfolio = acct.get("portfolio_value_cents", 0)

    lines.append("")
    lines.append(f"NAV: ${nav / 100:.2f}")
    lines.append(f"  Cash: ${balance / 100:.2f}")
    lines.append(f"  Positions: ${portfolio / 100:.2f}")

    # P&L summary
    total_pnl = bc.get("true_total_pnl_cents", 0)
    realized = bc.get("realized_net_cents", 0)
    unrealized = bc.get("implied_unrealized_cents", 0)
    deposited = snap.get("deposits", {}).get("net_funded_cents", 0)
    roi = snap.get("deposits", {}).get("roi_pct", 0)

    lines.append("")
    lines.append(f"Total P&L: {format_dollars(total_pnl)} ({roi:+.1f}% ROI)")
    lines.append(f"  Realized: {format_dollars(realized)}")
    lines.append(f"  Unrealized: {format_dollars(unrealized)}")

    # Today's P&L (from by_day)
    rp = snap.get("realized_pnl", {})
    by_day = rp.get("by_day", {})
    today_key = datetime.now().strftime("%Y-%m-%d")
    yesterday_key = None
    day_keys = sorted(by_day.keys())
    if day_keys:
        yesterday_key = day_keys[-1]
    today_pnl = by_day.get(today_key)
    if today_pnl is not None:
        lines.append(f"  Today: {format_dollars(today_pnl)}")
    elif yesterday_key:
        lines.append(f"  Last settle ({yesterday_key}): {format_dollars(by_day[yesterday_key])}")

    # Per-bot performance
    by_bot = rp.get("by_bot", {})
    active_bots = {k: v for k, v in by_bot.items()
                   if v.get("wins", 0) + v.get("losses", 0) > 0 or v.get("pnl_cents", 0) != 0}

    if active_bots:
        lines.append("")
        lines.append("Bot Performance:")
        # Sort by P&L descending
        for bot, d in sorted(active_bots.items(), key=lambda x: x[1].get("pnl_cents", 0), reverse=True):
            wins = d.get("wins", 0)
            losses = d.get("losses", 0)
            total = wins + losses
            wr = d.get("win_rate", 0)
            pnl = d.get("pnl_cents", 0)
            fees = d.get("fees_cents", 0)
            lines.append(f"  {bot}: {format_dollars(pnl)} | {wins}W/{losses}L ({wr * 100:.0f}%) | fees ${fees / 100:.2f}")

    # Open positions summary
    unrealized_data = snap.get("unrealized_pnl", {})
    positions = unrealized_data.get("positions", [])
    if positions:
        lines.append("")
        lines.append(f"Open Positions: {len(positions)}")
        total_cost = sum(p.get("cost_cents", 0) for p in positions)
        lines.append(f"  Total exposure: ${total_cost / 100:.2f}")

    # Backtest Brier score
    if BACKTEST_PATH.exists():
        try:
            bt = json.loads(BACKTEST_PATH.read_text())
            bs = bt.get("brier_score")
            if bs is not None:
                lines.append("")
                lines.append(f"Model: Brier={bs:.4f}")
                per_bot = bt.get("per_bot", {})
                for bot, stats in sorted(per_bot.items()):
                    bot_bs = stats.get("brier_score")
                    n = stats.get("n_evaluated", 0)
                    if bot_bs is not None and n > 0:
                        lines.append(f"  {bot}: {bot_bs:.4f} (n={n})")
        except Exception:
            pass

    # Verification status
    v = snap.get("verification", {})
    status = v.get("status", "unknown")
    if status != "ok":
        lines.append("")
        lines.append(f"Verification: {status}")

    return "\n".join(lines)


def send_imessage(message: str) -> bool:
    """Send iMessage via BlueBubbles REST API."""
    url = f"{BB_URL}?password={urllib.parse.quote(BB_PASSWORD)}"
    payload = json.dumps({
        "chatGuid": BB_CHAT_GUID,
        "message": message,
        "tempGuid": f"temp-daily-{int(datetime.now().timestamp())}"
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("status") == 200:
                log.info("iMessage sent successfully")
                return True
            log.warning("BlueBubbles returned status %s: %s", result.get("status"), result.get("message"))
            return False
    except Exception as e:
        log.error("Failed to send iMessage: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily P&L iMessage report")
    parser.add_argument("--dry-run", action="store_true", help="Print report without sending")
    args = parser.parse_args()

    snap = load_snapshot()
    report = build_report(snap)

    print(report)

    if not args.dry_run:
        send_imessage(report)


if __name__ == "__main__":
    main()
