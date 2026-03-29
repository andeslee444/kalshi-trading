#!/usr/bin/env python3
"""Daily backtest runner with drift detection and alerting.

Runs backtest.py --save --allow-canonical-save, compares Brier scores to previous results,
and sends a WhatsApp alert if model quality degrades >10%.

Usage:
    python3 scripts/daily-backtest.py            # Run + alert on drift
    python3 scripts/daily-backtest.py --dry-run   # Compare + print, no alerts
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import notify_whatsapp, setup_logging

log = setup_logging("daily-backtest")
PROJECT_DIR = Path(__file__).resolve().parent.parent
RESULTS_PATH = PROJECT_DIR / "data" / "backtest-results.json"
BACKTEST_SCRIPT = PROJECT_DIR / "scripts" / "backtest.py"

DRIFT_THRESHOLD = 0.10  # 10% degradation


def load_results():
    """Load backtest-results.json or return None."""
    if not RESULTS_PATH.exists():
        return None
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return None


def check_drift(previous, current):
    """Compare Brier scores and detect degradation.

    Returns (drifted: bool, message: str) with per-bot breakdown.
    """
    if not previous or not current:
        return False, "No previous results to compare."

    old_bs = previous.get("brier_score")
    new_bs = current.get("brier_score")

    if old_bs is None or new_bs is None or old_bs == 0:
        return False, "Brier scores unavailable for comparison."

    change = (new_bs - old_bs) / old_bs
    lines = []
    lines.append(f"Brier: {old_bs:.4f} -> {new_bs:.4f} ({change:+.1%})")

    # Per-bot breakdown
    old_bots = previous.get("per_bot", {})
    new_bots = current.get("per_bot", {})
    bot_drifts = []
    for bot in sorted(set(old_bots) | set(new_bots)):
        ob = old_bots.get(bot, {}).get("brier_score")
        nb = new_bots.get(bot, {}).get("brier_score")
        if ob is not None and nb is not None and ob > 0:
            bot_change = (nb - ob) / ob
            lines.append(f"  {bot}: {ob:.4f} -> {nb:.4f} ({bot_change:+.1%})")
            if bot_change > DRIFT_THRESHOLD:
                bot_drifts.append(bot)
        elif nb is not None:
            lines.append(f"  {bot}: new ({nb:.4f})")

    drifted = change > DRIFT_THRESHOLD
    message = "\n".join(lines)

    if drifted:
        message = f"DRIFT ALERT: Brier score degraded {change:+.1%}\n{message}"
        if bot_drifts:
            message += f"\nDegraded bots: {', '.join(bot_drifts)}"

    return drifted, message


def main():
    parser = argparse.ArgumentParser(description="Daily backtest with drift detection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compare and print but don't send alerts")
    args = parser.parse_args()

    # Load previous results before overwriting
    previous = load_results()
    if previous:
        log.info(f"Previous results: Brier={previous.get('brier_score')}, from {previous.get('generated_at')}")
    else:
        log.info("No previous backtest results found.")

    # Run backtest
    log.info("Running backtest...")
    result = subprocess.run(
        [sys.executable, str(BACKTEST_SCRIPT), "--save", "--allow-canonical-save"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        log.error(f"Backtest failed (exit {result.returncode}): {result.stderr[:500]}")
        sys.exit(1)

    # Print backtest output
    if result.stdout.strip():
        print(result.stdout)

    # Load new results
    current = load_results()
    if not current:
        log.error("Backtest ran but no results file found.")
        sys.exit(1)

    log.info(f"New results: Brier={current.get('brier_score')}, from {current.get('generated_at')}")

    # Drift detection
    drifted, message = check_drift(previous, current)
    print(f"\n--- Drift Analysis ---\n{message}")

    if drifted and not args.dry_run:
        alert = f"Kalshi Backtest Drift\n{message}"
        log.info("Sending drift alert...")
        notify_whatsapp(alert, logger=log)
    elif drifted:
        log.info("Drift detected (dry-run, no alert sent)")
    else:
        log.info("No significant drift detected.")


if __name__ == "__main__":
    main()
