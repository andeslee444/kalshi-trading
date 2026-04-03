#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_DIR"

set -a
source "$PROJECT_DIR/.env"
set +a

export PYTHONUNBUFFERED=1

exec python3.13 src/kalshi/oracle-h2-pregame-collector.py \
  --duration-seconds "${ORACLE_H2_PREGAME_DURATION_SECONDS:-18000}" \
  --refresh-seconds "${ORACLE_H2_PREGAME_REFRESH_SECONDS:-120}" \
  --lookahead-hours "${ORACLE_H2_PREGAME_LOOKAHEAD_HOURS:-8}"
