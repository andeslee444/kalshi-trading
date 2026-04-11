#!/usr/bin/env bash
set -euo pipefail

# Idempotent cron installer for Kalshi trade log S3 sync and calibration pipeline.
# Safe to run multiple times — removes old entries before adding new ones.

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CRON_TAG="# kalshi-sync"
PIPELINE_CRON_TAG="# kalshi-pipeline"
WEEKLY_CRON_TAG="# kalshi-weekly-calibration"

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
PIPELINE_LOG="$PROJECT_DIR/data/logs/calibration-pipeline-cron.log"
PIPELINE_CRON_LINE="0 6 * * * /bin/bash -c \"cd $PROJECT_DIR && /opt/homebrew/bin/python3.11 scripts/calibration-pipeline.py\" >> $PIPELINE_LOG 2>&1 $PIPELINE_CRON_TAG"
WEEKLY_LOG="$PROJECT_DIR/data/logs/weekly-calibration-cron.log"
WEEKLY_CRON_LINE="0 5 * * 0 /bin/bash -c \"cd $PROJECT_DIR && /opt/homebrew/bin/python3.11 scripts/calibration-pipeline.py --auto-apply\" >> $WEEKLY_LOG 2>&1 $WEEKLY_CRON_TAG"

# Get existing crontab (empty string if none)
EXISTING="$(crontab -l 2>/dev/null || true)"

# Remove any existing kalshi-sync, kalshi-pipeline, and kalshi-weekly lines
CLEANED="$(echo "$EXISTING" | grep -v "$CRON_TAG" | grep -v "# Kalshi trade log S3 sync" || true)"
CLEANED="$(echo "$CLEANED" | grep -v "$PIPELINE_CRON_TAG" | grep -v "# Kalshi calibration pipeline" || true)"
CLEANED="$(echo "$CLEANED" | grep -v "$WEEKLY_CRON_TAG" | grep -v "# Kalshi weekly" || true)"

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
$CRON_LINE
# Kalshi calibration pipeline (daily 6 AM)
$PIPELINE_CRON_LINE
# Kalshi weekly full calibration (Sunday 5 AM)
$WEEKLY_CRON_LINE"

echo "$NEW_CRON" | crontab -

echo "Cron jobs installed:"
echo ""
echo "  1) S3 Sync"
echo "     Schedule: every hour at :00"
echo "     Command:  cd $PROJECT_DIR && npm run sync:up"
echo "     Log:      $PROJECT_DIR/data/logs/sync.log"
echo ""
echo "  2) Calibration Pipeline"
echo "     Schedule: daily at 6:00 AM"
echo "     Command:  cd $PROJECT_DIR && /opt/homebrew/bin/python3.11 scripts/calibration-pipeline.py"
echo "     Log:      $PIPELINE_LOG"
echo ""
echo "  3) Weekly Calibration"
echo "     Schedule: Sunday at 5:00 AM"
echo "     Command:  cd $PROJECT_DIR && /opt/homebrew/bin/python3.11 scripts/calibration-pipeline.py --auto-apply"
echo "     Log:      $WEEKLY_LOG"
echo ""
echo "Verify with: crontab -l"
