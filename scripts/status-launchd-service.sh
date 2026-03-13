#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 <dashboard|supervisor|daily-report|boot-notify>" >&2
  exit 1
}

SERVICE="${1:-}"
[ -n "$SERVICE" ] || usage

UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
DEST_DIR="$HOME/Library/LaunchAgents"

case "$SERVICE" in
  dashboard) LABEL="com.kalshi.dashboard" ;;
  supervisor) LABEL="com.kalshi.supervisor" ;;
  daily-report) LABEL="com.kalshi.daily-report" ;;
  boot-notify) LABEL="com.kalshi.boot-notify" ;;
  *) usage ;;
esac

DEST_PLIST="$DEST_DIR/$LABEL.plist"

echo "Label: $LABEL"
echo "Plist: $DEST_PLIST"
if [ -f "$DEST_PLIST" ]; then
  echo "Installed: yes"
else
  echo "Installed: no"
fi

if launchctl print "$DOMAIN/$LABEL" >/tmp/kalshi-launchd-status.$$ 2>/dev/null; then
  echo "Loaded: yes"
  grep -E 'state =|pid =|last exit code =' /tmp/kalshi-launchd-status.$$ || true
  rm -f /tmp/kalshi-launchd-status.$$
else
  echo "Loaded: no"
fi
