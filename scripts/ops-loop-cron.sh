#!/bin/bash
# Daily ops loop cron wrapper.
# Runs the self-healing daily loop and logs output.
#
# Recommended crontab entry (run once daily at 06:00 UTC):
#   0 6 * * * /Users/andeslee/Documents/cursor-projects/kalshi-trading/scripts/ops-loop-cron.sh

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export SHELL="/bin/bash"
cd /Users/andeslee/Documents/cursor-projects/kalshi-trading

# Load env vars for API access
set -a
source .env 2>/dev/null || true
set +a

LOG="data/logs/ops-loop-cron.log"
mkdir -p data/logs

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — ops-loop-cron starting" >> "$LOG"

# Run the daily ops loop (dry-run during Phase 4 observation)
/opt/homebrew/bin/python3 scripts/daily-ops-loop.py --dry-run >> "$LOG" 2>&1
EXIT_CODE=$?

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — ops-loop-cron complete (exit=$EXIT_CODE)" >> "$LOG"
