#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 <dashboard|supervisor|demo-supervisor|prod-supervisor|daily-report|boot-notify|oracle-latency-probe|oracle-h2-pregame-collector|oracle-shadow-bot|oracle-h8-maker-demo|oracle-alpha-maintenance|oracle-demo-latency-probe|oracle-demo-h2-pregame-collector|oracle-demo-shadow-bot|oracle-demo-h8-maker-demo|oracle-demo-alpha-maintenance|ops-loop>" >&2
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
  demo-supervisor) LABEL="com.kalshi.demo.supervisor" ;;
  prod-supervisor) LABEL="com.kalshi.prod.supervisor" ;;
  daily-report) LABEL="com.kalshi.daily-report" ;;
  boot-notify) LABEL="com.kalshi.boot-notify" ;;
  oracle-latency-probe) LABEL="com.kalshi.oracle-latency-probe" ;;
  oracle-h2-pregame-collector) LABEL="com.kalshi.oracle-h2-pregame-collector" ;;
  oracle-shadow-bot) LABEL="com.kalshi.oracle-shadow-bot" ;;
  oracle-h8-maker-demo) LABEL="com.kalshi.oracle-h8-maker-demo" ;;
  oracle-alpha-maintenance) LABEL="com.kalshi.oracle-alpha-maintenance" ;;
  oracle-demo-latency-probe) LABEL="com.kalshi.oracle-latency-probe" ;;
  oracle-demo-h2-pregame-collector) LABEL="com.kalshi.oracle-h2-pregame-collector" ;;
  oracle-demo-shadow-bot) LABEL="com.kalshi.oracle-shadow-bot" ;;
  oracle-demo-h8-maker-demo) LABEL="com.kalshi.oracle-h8-maker-demo" ;;
  oracle-demo-alpha-maintenance) LABEL="com.kalshi.oracle-alpha-maintenance" ;;
  ops-loop) LABEL="com.kalshi.ops-loop" ;;
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
