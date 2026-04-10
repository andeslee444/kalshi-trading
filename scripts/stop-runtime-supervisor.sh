#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 <demo|prod>" >&2
  exit 1
}

RUNTIME="${1:-}"
[ -n "$RUNTIME" ] || usage

UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

case "$RUNTIME" in
  demo)
    LABEL="com.kalshi.demo.supervisor"
    ;;
  prod)
    LABEL="com.kalshi.prod.supervisor"
    ;;
  *)
    usage
    ;;
esac

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
echo "Stopped $LABEL"
