#!/usr/bin/env bash
set -euo pipefail

BUCKET="${S3_BUCKET:-kalshi-trading-logs}"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_KEY=".sync-lock"

usage() {
  echo "Usage: $0 {setup|upload|download}"
  echo "  setup    — Create S3 bucket (idempotent)"
  echo "  upload   — Push trade data to S3"
  echo "  download — Pull trade data from S3"
  exit 1
}

# Common exclude/include flags for data/ sync
# Strategy: exclude everything, then include only what we want
sync_filters() {
  echo "--exclude=*"

  # ── Trade logs (golden records) ──
  echo "--include=kalshi-*-trades.json"
  echo "--include=kalshi-trades.json"
  echo "--include=beatrelease-trades.json"
  echo "--include=beatrelease-state.json"

  # ── Analytics & snapshots ──
  echo "--include=backtest-results.json"
  echo "--include=performance-metrics.json"
  echo "--include=financial-snapshot.json"
  echo "--include=deposits.json"

  # ── Per-bot metrics (Plans 2-8) ──
  echo "--include=*-metrics.json"

  # ── Observability state ──
  echo "--include=health-state.json"
  echo "--include=allocator-state.json"
  echo "--include=scan-summaries.json"
  echo "--include=circuit-breaker-state.json"
  echo "--include=correlation-state.json"
  echo "--include=regime-state.json"
  echo "--include=pf-state-crypto.json"

  # ── Decision logs ──
  echo "--include=*-decisions.json"

  # ── Model tracking ──
  echo "--include=weather-verification.json"
  echo "--include=nowcast-history.json"
  echo "--include=crypto-price-history.json"

  # ── Caches (useful for debugging, not critical) ──
  echo "--include=econ-nowcast-cache.json"
  echo "--include=macro-cache.json"
}

acquire_lock() {
  # Check for existing lock
  lock_info=$(aws s3 ls "s3://${BUCKET}/${LOCK_KEY}" 2>/dev/null || true)
  if [ -n "$lock_info" ]; then
    # Parse lock timestamp and check for staleness (>10 min)
    lock_date=$(echo "$lock_info" | awk '{print $1, $2}')
    if [ -n "$lock_date" ]; then
      lock_epoch=$(date -j -f "%Y-%m-%d %H:%M:%S" "$lock_date" +%s 2>/dev/null || date -d "$lock_date" +%s 2>/dev/null || echo 0)
      now_epoch=$(date +%s)
      lock_age=$(( now_epoch - lock_epoch ))
      if [ "$lock_age" -gt 600 ]; then
        echo "Stale lock detected (${lock_age}s old), removing..."
        aws s3 rm "s3://${BUCKET}/${LOCK_KEY}" 2>/dev/null || true
      else
        echo "Another sync in progress (${lock_age}s ago) — aborting"
        exit 1
      fi
    else
      echo "Another sync in progress — aborting"
      exit 1
    fi
  fi
  echo "$(hostname):$(date -u +%Y-%m-%dT%H:%M:%SZ)" | aws s3 cp - "s3://${BUCKET}/${LOCK_KEY}"
  trap 'aws s3 rm "s3://${BUCKET}/${LOCK_KEY}" 2>/dev/null' EXIT
}

cmd_setup() {
  echo "Creating bucket s3://${BUCKET} in ${REGION}..."
  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "Bucket already exists."
  else
    aws s3api create-bucket \
      --bucket "$BUCKET" \
      --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
    echo "Bucket created."
  fi
}

cmd_upload() {
  acquire_lock
  echo "Uploading trade data to s3://${BUCKET}..."

  # Pre-upload validation
  echo "Running pre-upload validation..."
  if ! python3 "$PROJECT_DIR/scripts/validate-sync-data.py"; then
    echo "Upload blocked by validation. Use --force to override."
    if [ "${2:-}" != "--force" ]; then
      exit 1
    fi
    echo "WARNING: --force flag set, uploading despite validation failures"
  fi

  # Sync data/ trade logs
  aws s3 sync "$PROJECT_DIR/data/" "s3://${BUCKET}/data/" \
    $(sync_filters)

  # Sync bot log files
  aws s3 sync "$PROJECT_DIR/data/logs/" "s3://${BUCKET}/data/logs/" \
    --exclude "*" --include "*.log"

  # Sync config files
  if [ -f "$PROJECT_DIR/config/calibration.json" ]; then
    aws s3 cp "$PROJECT_DIR/config/calibration.json" "s3://${BUCKET}/config/calibration.json"
  fi
  if [ -f "$PROJECT_DIR/config/bayes-params.json" ]; then
    aws s3 cp "$PROJECT_DIR/config/bayes-params.json" "s3://${BUCKET}/config/bayes-params.json"
  fi

  # Verify key files by comparing local vs remote sizes
  echo "Verifying upload..."
  for f in kalshi-trades.json kalshi-monitor-trades.json kalshi-entertainment-trades.json kalshi-economics-trades.json kalshi-crypto-trades.json kalshi-strategy-trades.json kalshi-arb-trades.json kalshi-position-trades.json kalshi-mm-trades.json beatrelease-trades.json health-state.json scan-summaries.json; do
    if [ -f "$PROJECT_DIR/data/$f" ]; then
      local_size=$(stat -f%z "$PROJECT_DIR/data/$f" 2>/dev/null || stat -c%s "$PROJECT_DIR/data/$f" 2>/dev/null || echo 0)
      remote_info=$(aws s3 ls "s3://${BUCKET}/data/$f" 2>/dev/null || true)
      remote_size=$(echo "$remote_info" | awk '{print $3}')
      if [ -n "$remote_size" ] && [ "$local_size" != "$remote_size" ]; then
        echo "WARNING: Size mismatch for $f (local=$local_size, remote=$remote_size)"
      fi
    fi
  done

  echo "Upload complete."
}

cmd_download() {
  acquire_lock
  echo "Downloading trade data from s3://${BUCKET}..."

  # Sync data/ trade logs (--size-only prevents overwriting newer local files
  # when sizes match; for trade logs, more trades = larger file = newer)
  aws s3 sync "s3://${BUCKET}/data/" "$PROJECT_DIR/data/" \
    $(sync_filters) --size-only

  # Sync bot log files
  aws s3 sync "s3://${BUCKET}/data/logs/" "$PROJECT_DIR/data/logs/" \
    --exclude "*" --include "*.log"

  # Sync config files
  aws s3 cp "s3://${BUCKET}/config/calibration.json" "$PROJECT_DIR/config/calibration.json" 2>/dev/null || true
  aws s3 cp "s3://${BUCKET}/config/bayes-params.json" "$PROJECT_DIR/config/bayes-params.json" 2>/dev/null || true

  echo "Download complete."
}

case "${1:-}" in
  setup)   cmd_setup ;;
  upload)  cmd_upload ;;
  download) cmd_download ;;
  *)       usage ;;
esac
