#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_DIR"

set -a
source "$PROJECT_DIR/.env"
set +a

export PYTHONUNBUFFERED=1
export ORACLE_ENABLED_OVERRIDE=yes
export ORACLE_BOOK_C_ENABLED_OVERRIDE=yes
export ORACLE_BOOK_C_PROP_SIGNALS_ENABLED_OVERRIDE=no
export ORACLE_BOOK_C_CLUTCH_COMEBACK_ENABLED_OVERRIDE=yes
export ORACLE_BOOK_C_PASSIVE_EXECUTION_OVERRIDE=yes

# Maker validation must stay on demo credentials and must never inherit
# Oracle's production trading overrides.
export ORACLE_KALSHI_API_KEY=""
export ORACLE_KALSHI_KEY_FILE=""
export ORACLE_KALSHI_MODE=""
export ORACLE_KALSHI_CONFIRM_PRODUCTION=""
export KALSHI_CONFIRM_PRODUCTION=""
export KALSHI_MODE=demo

exec python3.13 src/kalshi/oracle-bot.py --demo
