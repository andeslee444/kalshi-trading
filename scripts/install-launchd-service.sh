#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 <dashboard|supervisor|daily-report|boot-notify|oracle-latency-probe|oracle-shadow-bot|oracle-alpha-maintenance|ops-loop>" >&2
  exit 1
}

SERVICE="${1:-}"
[ -n "$SERVICE" ] || usage

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
TMP_PLIST="$(mktemp)"
START_MODE="kickstart"

cleanup() {
  rm -f "$TMP_PLIST"
}
trap cleanup EXIT

case "$SERVICE" in
  dashboard)
    LABEL="com.kalshi.dashboard"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.dashboard.plist"
    ;;
  supervisor)
    LABEL="com.kalshi.supervisor"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.supervisor.plist"
    ;;
  daily-report)
    LABEL="com.kalshi.daily-report"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.daily-report.plist"
    START_MODE="bootstrap-only"
    ;;
  boot-notify)
    LABEL="com.kalshi.boot-notify"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.boot-notify.plist"
    ;;
  oracle-latency-probe)
    LABEL="com.kalshi.oracle-latency-probe"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.oracle-latency-probe.plist"
    ;;
  oracle-shadow-bot)
    LABEL="com.kalshi.oracle-shadow-bot"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.oracle-shadow-bot.plist"
    ;;
  oracle-alpha-maintenance)
    LABEL="com.kalshi.oracle-alpha-maintenance"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.oracle-alpha-maintenance.plist"
    START_MODE="bootstrap-only"
    ;;
  ops-loop)
    LABEL="com.kalshi.ops-loop"
    TEMPLATE="$PROJECT_DIR/scripts/com.kalshi.ops-loop.plist"
    START_MODE="bootstrap-only"
    ;;
  *)
    usage
    ;;
esac

DEST_PLIST="$DEST_DIR/$LABEL.plist"
mkdir -p "$DEST_DIR" "$PROJECT_DIR/data/logs"

sed \
  -e "s|REPLACE_WITH_PROJECT_DIR|$PROJECT_DIR|g" \
  -e "s|REPLACE_WITH_HOME|$HOME|g" \
  "$TEMPLATE" > "$TMP_PLIST"

install -m 644 "$TMP_PLIST" "$DEST_PLIST"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN" "$DEST_PLIST" >/dev/null 2>&1 || \
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  sleep 1
fi

launchctl bootstrap "$DOMAIN" "$DEST_PLIST"
if [ "$START_MODE" = "kickstart" ]; then
  launchctl kickstart -k "$DOMAIN/$LABEL"
fi

echo "Installed $LABEL"
echo "Plist: $DEST_PLIST"
if [ "$START_MODE" = "kickstart" ]; then
  echo "Service restarted"
else
  echo "Service loaded (not force-started)"
fi
