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
    INSTALL_NAME="demo-supervisor"
    RUNTIME_ROOT="$HOME/deploy/kalshi-demo"
    if launchctl print "$DOMAIN/com.kalshi.supervisor" >/dev/null 2>&1; then
      echo "Legacy dev supervisor is still loaded. Uninstall or stop com.kalshi.supervisor before starting demo." >&2
      exit 1
    fi
    ;;
  prod)
    LABEL="com.kalshi.prod.supervisor"
    INSTALL_NAME="prod-supervisor"
    RUNTIME_ROOT="$HOME/deploy/kalshi-prod"
    if [ ! -f "$RUNTIME_ROOT/.env" ]; then
      echo "Missing $RUNTIME_ROOT/.env. Restore the production secret bundle before starting prod." >&2
      exit 1
    fi
    ;;
  *)
    usage
    ;;
esac

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ ! -f "$PLIST" ]; then
  echo "Missing $PLIST. Run: bash scripts/install-launchd-service.sh $INSTALL_NAME" >&2
  exit 1
fi

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
fi

launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Started $LABEL"
echo "Root: $RUNTIME_ROOT"
