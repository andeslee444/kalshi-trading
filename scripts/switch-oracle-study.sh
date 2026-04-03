#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: $0 <shadow|maker|stop>" >&2
  exit 1
}

MODE="${1:-}"
[ -n "$MODE" ] || usage

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

case "$MODE" in
  shadow)
    bash "$PROJECT_DIR/scripts/uninstall-launchd-service.sh" oracle-h8-maker-demo >/dev/null 2>&1 || true
    bash "$PROJECT_DIR/scripts/install-launchd-service.sh" oracle-shadow-bot
    echo "Oracle bounded study mode: shadow"
    ;;
  maker)
    bash "$PROJECT_DIR/scripts/uninstall-launchd-service.sh" oracle-shadow-bot >/dev/null 2>&1 || true
    bash "$PROJECT_DIR/scripts/install-launchd-service.sh" oracle-h8-maker-demo
    echo "Oracle bounded study mode: maker"
    ;;
  stop)
    bash "$PROJECT_DIR/scripts/uninstall-launchd-service.sh" oracle-shadow-bot >/dev/null 2>&1 || true
    bash "$PROJECT_DIR/scripts/uninstall-launchd-service.sh" oracle-h8-maker-demo >/dev/null 2>&1 || true
    echo "Oracle bounded study mode: stopped"
    ;;
  *)
    usage
    ;;
esac
