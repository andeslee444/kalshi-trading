#!/bin/bash
export PATH="/opt/homebrew/opt/python@3.11/libexec/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export SHELL="/bin/bash"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Load env vars for API access
set -a
source .env 2>/dev/null || true
set +a

LOG="data/logs/sync-cron.log"
mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — sync-up-cron starting" >> "$LOG"

# Step 1: Refresh P&L snapshot (captures current API state)
echo "$(date -u +%T) Running snapshot..." >> "$LOG"
/opt/homebrew/bin/npm run snapshot >> "$LOG" 2>&1 || true

# Step 2: Reconcile trades (annotate with settlement/fill data)
echo "$(date -u +%T) Running reconcile..." >> "$LOG"
/opt/homebrew/bin/npm run reconcile >> "$LOG" 2>&1 || true

# Step 3: Upload to S3 (includes pre-upload validation)
echo "$(date -u +%T) Running sync:up..." >> "$LOG"
/opt/homebrew/bin/npm run sync:up >> "$LOG" 2>&1

echo "$(date -u +%T) sync-up-cron complete" >> "$LOG"
