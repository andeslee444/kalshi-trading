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

protected_download_files() {
  python3 -c "
from pathlib import Path
import sys
proj = Path('$PROJECT_DIR')
sys.path.insert(0, str(proj / 'src' / 'kalshi'))
from bot_registry import DECISION_FILE_SPECS
from trade_files import TRADE_FILES

files = {tf['filename'] for tf in TRADE_FILES}
files.update(spec['filename'] for spec in DECISION_FILE_SPECS)
files.update({
    'allocator-state.json',
    'backtest-results.json',
    'beatrelease-state.json',
    'circuit-breaker-state.json',
    'correlation-state.json',
    'crypto-price-history.json',
    'deposits.json',
    'econ-nowcast-cache.json',
    'financial-snapshot.json',
    'health-state.json',
    'macro-cache.json',
    'nowcast-history.json',
    'performance-metrics.json',
    'pf-state-crypto.json',
    'regime-state.json',
    'scan-summaries.json',
    'weather-nws-cross-check.json',
    'weather-verification.json',
})
for path in (proj / 'data').glob('*-metrics.json'):
    files.add(path.name)
for name in sorted(files):
    print(name)
"
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
  if [ -f "$PROJECT_DIR/config/historical-calibration.json" ]; then
    aws s3 cp "$PROJECT_DIR/config/historical-calibration.json" "s3://${BUCKET}/config/historical-calibration.json"
  fi
  if [ -f "$PROJECT_DIR/config/bayes-params.json" ]; then
    aws s3 cp "$PROJECT_DIR/config/bayes-params.json" "s3://${BUCKET}/config/bayes-params.json"
  fi

  # Verify key files by comparing local vs remote sizes
  # Generate verify list from trade_files.py (canonical source of truth)
  echo "Verifying upload..."
  verify_files=$(python3 -c "
import sys; sys.path.insert(0, '$PROJECT_DIR/src/kalshi')
from trade_files import TRADE_FILES
for tf in TRADE_FILES:
    print(tf['filename'])
# Also verify non-trade critical files
for f in ['health-state.json', 'scan-summaries.json', 'financial-snapshot.json']:
    print(f)
")

  for f in $verify_files; do
    if [ -f "$PROJECT_DIR/data/$f" ]; then
      local_size=$(stat -f%z "$PROJECT_DIR/data/$f" 2>/dev/null || stat -c%s "$PROJECT_DIR/data/$f" 2>/dev/null || echo 0)
      remote_info=$(aws s3 ls "s3://${BUCKET}/data/$f" 2>/dev/null || true)
      remote_size=$(echo "$remote_info" | awk '{print $3}')
      if [ -n "$remote_size" ] && [ "$local_size" != "$remote_size" ]; then
        echo "WARNING: Size mismatch for $f (local=$local_size, remote=$remote_size)"
      fi
    fi
  done

  # Generate sync manifest for audit trail
  echo "Generating sync manifest..."
  python3 -c "
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

proj = Path('$PROJECT_DIR')
data = proj / 'data'
sys.path.insert(0, str(proj / 'src' / 'kalshi'))
from trade_files import TRADE_FILES

manifest = {
    'sync_time': datetime.now(timezone.utc).isoformat(),
    'hostname': '$(hostname)',
    'direction': 'upload',
    'trade_logs': {},
    'state_files': {},
}

for tf in TRADE_FILES:
    p = data / tf['filename']
    if p.exists():
        try:
            trades = json.loads(p.read_text())
            manifest['trade_logs'][tf['filename']] = {
                'size_bytes': p.stat().st_size,
                'trade_count': len(trades),
                'latest_timestamp': max((t.get('timestamp','') for t in trades), default=''),
                'reconciled_count': sum(1 for t in trades if t.get('settlement_result')),
            }
        except json.JSONDecodeError:
            manifest['trade_logs'][tf['filename']] = {'error': 'corrupt JSON'}

for sf in ['health-state.json', 'financial-snapshot.json', 'allocator-state.json',
           'scan-summaries.json', 'circuit-breaker-state.json']:
    p = data / sf
    if p.exists():
        manifest['state_files'][sf] = {
            'size_bytes': p.stat().st_size,
            'modified': datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        }

print(json.dumps(manifest, indent=2))
" > "$PROJECT_DIR/data/.sync-manifest.json"

  aws s3 cp "$PROJECT_DIR/data/.sync-manifest.json" "s3://${BUCKET}/data/.sync-manifest.json"

  # Append to sync history (last 100 entries)
  aws s3 cp "s3://${BUCKET}/data/.sync-history.jsonl" "$PROJECT_DIR/data/.sync-history.jsonl" 2>/dev/null || true
  cat "$PROJECT_DIR/data/.sync-manifest.json" >> "$PROJECT_DIR/data/.sync-history.jsonl"
  tail -100 "$PROJECT_DIR/data/.sync-history.jsonl" > "$PROJECT_DIR/data/.sync-history-trimmed.jsonl"
  mv "$PROJECT_DIR/data/.sync-history-trimmed.jsonl" "$PROJECT_DIR/data/.sync-history.jsonl"
  aws s3 cp "$PROJECT_DIR/data/.sync-history.jsonl" "s3://${BUCKET}/data/.sync-history.jsonl"

  echo "Upload complete."
}

cmd_download() {
  acquire_lock
  echo "Downloading trade data from s3://${BUCKET}..."

  # Check for local modifications that would be overwritten
  echo "Checking for local changes..."
  preserve_filters=()
  preserved_files=()
  while IFS= read -r f; do
    local_file="$PROJECT_DIR/data/$f"
    if [ -f "$local_file" ]; then
      local_mod=$(stat -f%m "$local_file" 2>/dev/null || stat -c%Y "$local_file" 2>/dev/null || echo 0)
      remote_info=$(aws s3api head-object --bucket "$BUCKET" --key "data/$f" 2>/dev/null || true)
      if [ -n "$remote_info" ]; then
        remote_mod=$(echo "$remote_info" | python3 -c "
import sys,json
from datetime import datetime
info = json.load(sys.stdin)
dt = datetime.fromisoformat(info['LastModified'].replace('+00:00',''))
print(int(dt.timestamp()))
" 2>/dev/null || echo 0)
        if [ "$local_mod" -gt "$remote_mod" ] 2>/dev/null; then
          echo "WARNING: Local $f is newer than S3 — preserving local copy"
          preserve_filters+=("--exclude=$f")
          preserved_files+=("$f")
        fi
      fi
    fi
  done < <(protected_download_files)

  # Sync data/ trade logs using --exact-timestamps for safer comparison
  # (--size-only is fragile: truncation or reconciliation changes size without
  # adding new trades, causing incorrect overwrite decisions)
  aws s3 sync "s3://${BUCKET}/data/" "$PROJECT_DIR/data/" \
    $(sync_filters) "${preserve_filters[@]}" --exact-timestamps

  if [ "${#preserved_files[@]}" -gt 0 ]; then
    echo "Preserved newer local artifacts:"
    printf '  %s\n' "${preserved_files[@]}"
  fi

  # Sync bot log files
  aws s3 sync "s3://${BUCKET}/data/logs/" "$PROJECT_DIR/data/logs/" \
    --exclude "*" --include "*.log"

  # Sync config files
  aws s3 cp "s3://${BUCKET}/config/calibration.json" "$PROJECT_DIR/config/calibration.json" 2>/dev/null || true
  aws s3 cp "s3://${BUCKET}/config/historical-calibration.json" "$PROJECT_DIR/config/historical-calibration.json" 2>/dev/null || true
  aws s3 cp "s3://${BUCKET}/config/bayes-params.json" "$PROJECT_DIR/config/bayes-params.json" 2>/dev/null || true

  echo "Download complete."

  # Post-download integrity report
  echo ""
  python3 "$PROJECT_DIR/scripts/check-sync-health.py" || true
}

case "${1:-}" in
  setup)   cmd_setup ;;
  upload)  cmd_upload ;;
  download) cmd_download ;;
  *)       usage ;;
esac
