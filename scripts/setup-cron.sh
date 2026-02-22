#!/usr/bin/env bash
set -euo pipefail

# Idempotent cron installer for Kalshi trade log S3 sync.
# Safe to run multiple times — removes old entry before adding new one.

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CRON_TAG="# kalshi-sync"

# Auto-detect paths
NPM_PATH="$(which npm 2>/dev/null || echo "")"
AWS_PATH="$(which aws 2>/dev/null || echo "")"

if [ -z "$NPM_PATH" ]; then
  echo "ERROR: npm not found in PATH. Install Node.js first."
  exit 1
fi

if [ -z "$AWS_PATH" ]; then
  echo "ERROR: aws not found in PATH. Install AWS CLI first."
  exit 1
fi

# Build PATH that includes directories for npm and aws
NPM_DIR="$(dirname "$NPM_PATH")"
AWS_DIR="$(dirname "$AWS_PATH")"
CRON_PATH="$NPM_DIR:$AWS_DIR:/usr/local/bin:/usr/bin:/bin"

CRON_LINE="0 * * * * /bin/bash -c \"cd $PROJECT_DIR && $NPM_PATH run sync:up\" >> $PROJECT_DIR/data/logs/sync.log 2>&1 $CRON_TAG"

# Get existing crontab (empty string if none)
EXISTING="$(crontab -l 2>/dev/null || true)"

# Remove any existing kalshi-sync lines
CLEANED="$(echo "$EXISTING" | grep -v "$CRON_TAG" | grep -v "# Kalshi trade log S3 sync" || true)"

# Remove old PATH line if we set it before (identified by containing npm path)
CLEANED="$(echo "$CLEANED" | grep -v "kalshi-cron-path" || true)"

# Build new crontab
NEW_CRON="$(echo "$CLEANED" | sed '/^$/N;/^\n$/d')"  # collapse blank lines

if [ -n "$NEW_CRON" ]; then
  NEW_CRON="$NEW_CRON

"
fi

NEW_CRON="${NEW_CRON}PATH=$CRON_PATH # kalshi-cron-path
# Kalshi trade log S3 sync (hourly)
$CRON_LINE"

echo "$NEW_CRON" | crontab -

echo "Cron job installed:"
echo "  Schedule: every hour at :00"
echo "  Command:  cd $PROJECT_DIR && npm run sync:up"
echo "  Log:      $PROJECT_DIR/data/logs/sync.log"
echo ""
echo "Verify with: crontab -l"
