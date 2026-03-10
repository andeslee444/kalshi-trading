#!/usr/bin/env bash
# Daily automation: run P&L report + backtest with drift detection.
# Intended for cron/LaunchAgent — sends WhatsApp alert on completion or failure.
#
# Usage: ./scripts/daily-automation.sh
# Recommended schedule: daily at 9:00 AM ET (after overnight settlements)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOCK_FILE="$PROJECT_DIR/data/daily-run.lock"
LOG_FILE="$PROJECT_DIR/data/logs/daily-automation.log"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Lock file check (prevent double-run)
if [ -f "$LOCK_FILE" ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        lock_mtime=$(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0)
    else
        lock_mtime=$(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0)
    fi
    lock_age=$(( $(date +%s) - lock_mtime ))
    if [ "$lock_age" -lt 3600 ]; then
        echo "$(date): Lock file exists and is fresh ($lock_age seconds old). Skipping." >> "$LOG_FILE"
        exit 0
    fi
    echo "$(date): Stale lock file ($lock_age seconds). Removing." >> "$LOG_FILE"
fi

# Create lock
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$PROJECT_DIR"

# Source environment variables (needed for LaunchAgent which lacks shell env)
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

echo "$(date): Starting daily automation" >> "$LOG_FILE"

# Step 1: Run backfill (settle any unsettled trades)
echo "$(date): Running settlement backfill..." >> "$LOG_FILE"
python3 scripts/backfill-settlements.py >> "$LOG_FILE" 2>&1 || true

# Step 2: Run reconciliation
echo "$(date): Running trade reconciliation..." >> "$LOG_FILE"
python3 scripts/reconcile-trades.py >> "$LOG_FILE" 2>&1 || true

# Step 3: Generate verified P&L snapshot
echo "$(date): Generating P&L snapshot..." >> "$LOG_FILE"
python3 scripts/pnl-snapshot.py >> "$LOG_FILE" 2>&1 || true

# Step 4: Run daily report with WhatsApp notification
echo "$(date): Running daily report..." >> "$LOG_FILE"
python3 scripts/daily-report.py --notify --with-backtest >> "$LOG_FILE" 2>&1

# Step 5: Send daily iMessage report via BlueBubbles
echo "$(date): Sending iMessage report..." >> "$LOG_FILE"
python3 scripts/daily-imessage-report.py >> "$LOG_FILE" 2>&1 || true

# Step 6: Run daily backtest with drift detection
echo "$(date): Running daily backtest..." >> "$LOG_FILE"
python3 scripts/daily-backtest.py >> "$LOG_FILE" 2>&1 || true

echo "$(date): Daily automation complete" >> "$LOG_FILE"
