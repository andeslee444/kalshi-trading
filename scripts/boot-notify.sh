#!/bin/bash
# Sends a WhatsApp notification when the Mac Mini boots up.
# Installed as a LaunchAgent (com.kalshi.boot-notify) with RunAtLoad: true.

set -euo pipefail

export PATH="/opt/homebrew/opt/python@3.11/libexec/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi
LOG="$PROJECT_DIR/data/logs/boot-notify.log"

log() { echo "[$(date)] $1" >> "$LOG"; }

mkdir -p "$(dirname "$LOG")"

# Gather system info
UPTIME=$(uptime | sed 's/.*up /up /' | sed 's/,.*//')
BOOT_TIME=$(who -b 2>/dev/null | awk '{print $3, $4}' || echo "unknown")

# Check if supervisor is running (it should be, via its own LaunchAgent)
SUPERVISOR_STATUS="not running"
if pgrep -f "supervisor.py run" > /dev/null 2>&1; then
    SUPERVISOR_STATUS="running"
fi

MSG="[Kalshi Mac Mini] System rebooted at $BOOT_TIME ($UPTIME). Supervisor: $SUPERVISOR_STATUS."

if /opt/homebrew/bin/python3.11 -c 'import sys; from pathlib import Path; project_dir = Path(sys.argv[1]); message = sys.argv[2]; sys.path.insert(0, str(project_dir / "src" / "kalshi")); from ops.notifications import notify_whatsapp; raise SystemExit(0 if notify_whatsapp(message, project_dir=project_dir) else 1)' "$PROJECT_DIR" "$MSG" >> "$LOG" 2>&1; then
    log "WhatsApp boot notification sent: $MSG"
else
    log "WhatsApp boot notification failed or no notification target is configured."
fi
