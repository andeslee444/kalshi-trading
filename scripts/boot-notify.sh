#!/bin/bash
# Sends an iMessage notification when the Mac Mini boots up.
# Installed as a LaunchAgent (com.kalshi.boot-notify) with RunAtLoad: true.

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJECT_DIR="/Users/andeslee/Documents/cursor-projects/kalshi-trading"
LOG="$PROJECT_DIR/data/logs/boot-notify.log"
BB_URL="http://localhost:1234/api/v1/message/text?password=Cheeseslice8%21"
CHAT_GUID="iMessage;-;+14255336828"

log() { echo "[$(date)] $1" >> "$LOG"; }

log "Boot detected. Waiting for BlueBubbles to come online..."

# Wait up to 2 minutes for BlueBubbles to respond
for i in $(seq 1 24); do
    if curl -sf "http://localhost:1234/api/v1/ping?password=Cheeseslice8%21" > /dev/null 2>&1; then
        log "BlueBubbles is online after ${i}x5s"
        break
    fi
    sleep 5
done

# Gather system info
UPTIME=$(uptime | sed 's/.*up /up /' | sed 's/,.*//')
BOOT_TIME=$(who -b 2>/dev/null | awk '{print $3, $4}' || echo "unknown")

# Check if supervisor is running (it should be, via its own LaunchAgent)
SUPERVISOR_STATUS="not running"
if pgrep -f "supervisor.py run" > /dev/null 2>&1; then
    SUPERVISOR_STATUS="running"
fi

MSG="[Kalshi Mac Mini] System rebooted at $BOOT_TIME ($UPTIME). Supervisor: $SUPERVISOR_STATUS."

TEMP_GUID="temp-boot-$(date +%s)-$$-$RANDOM"
RESPONSE=$(curl -sf -X POST "$BB_URL" \
    -H "Content-Type: application/json" \
    -d "{\"chatGuid\": \"$CHAT_GUID\", \"message\": \"$MSG\", \"tempGuid\": \"$TEMP_GUID\"}" 2>&1) || true

log "Notification sent: $MSG"
log "BlueBubbles response: $RESPONSE"
