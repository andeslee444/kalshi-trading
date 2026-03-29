#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_DIR"

set -a
source "$PROJECT_DIR/.env"
set +a

export PYTHONUNBUFFERED=1

exec python3.13 src/kalshi/oracle-latency-probe.py \
  --duration-seconds "${ORACLE_LATENCY_PROBE_DURATION_SECONDS:-1209600}" \
  --refresh-seconds "${ORACLE_LATENCY_PROBE_REFRESH_SECONDS:-120}" \
  --followup-delays "${ORACLE_LATENCY_PROBE_FOLLOWUP_DELAYS:-1,3,5}"
