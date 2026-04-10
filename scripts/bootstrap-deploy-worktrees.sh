#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ROOT="${KALSHI_DEPLOY_ROOT:-$HOME/deploy}"

ensure_worktree() {
  local branch="$1"
  local target="$2"

  if [ -e "$target" ]; then
    echo "Skipping existing worktree: $target"
    return 0
  fi

  if git -C "$PROJECT_DIR" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$PROJECT_DIR" worktree add "$target" "$branch"
  else
    git -C "$PROJECT_DIR" worktree add "$target" -b "$branch" "origin/$branch"
  fi
}

mkdir -p "$DEPLOY_ROOT"
git -C "$PROJECT_DIR" fetch origin

ensure_worktree "deploy-demo" "$DEPLOY_ROOT/kalshi-demo"
ensure_worktree "deploy-prod" "$DEPLOY_ROOT/kalshi-prod"
ensure_worktree "deploy-oracle-demo" "$DEPLOY_ROOT/kalshi-oracle-demo"

echo "Deploy worktrees ready under $DEPLOY_ROOT"
