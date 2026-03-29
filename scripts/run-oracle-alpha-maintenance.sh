#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPORT_DIR="$PROJECT_DIR/data/reports"

cd "$PROJECT_DIR"
mkdir -p "$REPORT_DIR"

set -a
source "$PROJECT_DIR/.env"
set +a

export PYTHONUNBUFFERED=1

LIMIT="${ORACLE_ALPHA_REPORT_LIMIT:-10}"

tmp_reconcile_json="$(mktemp)"
tmp_report_json="$(mktemp)"
tmp_report_txt="$(mktemp)"
tmp_daily_json="$(mktemp)"
tmp_daily_txt="$(mktemp)"
cleanup() {
  rm -f \
    "$tmp_reconcile_json" \
    "$tmp_report_json" \
    "$tmp_report_txt" \
    "$tmp_daily_json" \
    "$tmp_daily_txt"
}
trap cleanup EXIT

python3.13 src/kalshi/oracle-alpha-reconcile.py --format json > "$tmp_reconcile_json"
mv "$tmp_reconcile_json" "$REPORT_DIR/oracle-alpha-reconcile-latest.json"

python3.13 src/kalshi/oracle-latency-report.py --format json --limit "$LIMIT" > "$tmp_report_json"
mv "$tmp_report_json" "$REPORT_DIR/oracle-latency-report-latest.json"

python3.13 src/kalshi/oracle-latency-report.py --limit "$LIMIT" > "$tmp_report_txt"
mv "$tmp_report_txt" "$REPORT_DIR/oracle-latency-report-latest.txt"

python3.13 src/kalshi/oracle-latency-report.py --format json --daily-summary-only > "$tmp_daily_json"
mv "$tmp_daily_json" "$REPORT_DIR/oracle-h1-daily-summary-latest.json"

python3.13 src/kalshi/oracle-latency-report.py --daily-summary-only > "$tmp_daily_txt"
mv "$tmp_daily_txt" "$REPORT_DIR/oracle-h1-daily-summary-latest.txt"

echo "Updated Oracle alpha maintenance artifacts:"
echo "  $REPORT_DIR/oracle-alpha-reconcile-latest.json"
echo "  $REPORT_DIR/oracle-latency-report-latest.json"
echo "  $REPORT_DIR/oracle-latency-report-latest.txt"
echo "  $REPORT_DIR/oracle-h1-daily-summary-latest.json"
echo "  $REPORT_DIR/oracle-h1-daily-summary-latest.txt"
