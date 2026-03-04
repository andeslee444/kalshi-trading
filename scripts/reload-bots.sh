#!/bin/bash
# Send SIGHUP to supervisor to trigger a full bot restart cycle.
# Used by github-webhook.py after auto-pull to reload bots with new code.

LOCK_FILE="$(dirname "$0")/../data/pids/supervisor.lock"

if [ ! -f "$LOCK_FILE" ]; then
    echo "Supervisor not running (no lock file)" >&2
    exit 1
fi

PID=$(cat "$LOCK_FILE")

if kill -0 "$PID" 2>/dev/null; then
    kill -HUP "$PID"
    echo "Sent SIGHUP to supervisor (PID $PID)"
else
    echo "Supervisor PID $PID not running" >&2
    exit 1
fi
