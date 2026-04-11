#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Full Sync Workflow ==="
echo ""

# Step 1: Fresh P&L snapshot
echo "[1/4] Running P&L snapshot..."
/opt/homebrew/bin/python3.11 scripts/pnl-snapshot.py
echo ""

# Step 2: Reconcile trades with API settlements/fills
echo "[2/4] Running trade reconciliation..."
/opt/homebrew/bin/python3.11 scripts/reconcile-trades.py
echo ""

# Step 3: Pre-upload validation
echo "[3/4] Validating data quality..."
/opt/homebrew/bin/python3.11 scripts/validate-sync-data.py
echo ""

# Step 4: Upload to S3
echo "[4/4] Uploading to S3..."
bash scripts/s3-sync.sh upload
echo ""

echo "=== Full sync complete ==="
