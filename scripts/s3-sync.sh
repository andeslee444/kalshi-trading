#!/usr/bin/env bash
set -euo pipefail

BUCKET="${S3_BUCKET:-kalshi-trading-logs}"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

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
  echo "--include=kalshi-*-trades.json"
  echo "--include=kalshi-trades.json"
  echo "--include=beatrelease-trades.json"
  echo "--include=beatrelease-state.json"
  echo "--include=backtest-results.json"
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
  echo "Uploading trade data to s3://${BUCKET}..."

  # Sync data/ trade logs
  aws s3 sync "$PROJECT_DIR/data/" "s3://${BUCKET}/data/" \
    $(sync_filters)

  # Sync config/calibration.json
  if [ -f "$PROJECT_DIR/config/calibration.json" ]; then
    aws s3 cp "$PROJECT_DIR/config/calibration.json" "s3://${BUCKET}/config/calibration.json"
  fi

  echo "Upload complete."
}

cmd_download() {
  echo "Downloading trade data from s3://${BUCKET}..."

  # Sync data/ trade logs
  aws s3 sync "s3://${BUCKET}/data/" "$PROJECT_DIR/data/" \
    $(sync_filters)

  # Sync config/calibration.json
  aws s3 cp "s3://${BUCKET}/config/calibration.json" "$PROJECT_DIR/config/calibration.json" 2>/dev/null || true

  echo "Download complete."
}

case "${1:-}" in
  setup)   cmd_setup ;;
  upload)  cmd_upload ;;
  download) cmd_download ;;
  *)       usage ;;
esac
