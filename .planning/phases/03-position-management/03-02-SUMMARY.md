---
phase: 03-position-management
plan: 02
subsystem: trading
tags: [position-management, trailing-stop, illiquidity, market-orders, dashboard, exit-state]

# Dependency graph
requires:
  - phase: 03-position-management
    provides: "Per-bot exit config, sell_position with market orders, evaluate_trailing_stop base"
provides:
  - "Trailing stop with illiquidity protection (no bids / wide spread skip)"
  - "Per-bot trailing config via exit_config parameter"
  - "Market orders for trailing stop exits (urgent execution)"
  - "Trailing state in data/trailing-state.json with source_bot, first_seen, last_updated"
  - "Grace period on restart to prevent stale peak exits"
  - "GET /api/exit-state endpoint for dashboard"
  - "Active exits dashboard panel with color-coded rows"
affects: [04-bot-activation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Illiquidity gate: skip trailing stop when bid=0 or spread>20c"
    - "Grace period pattern: function attribute _has_run to detect first scan"
    - "File migration: check old path, write to new, delete old"

key-files:
  created: []
  modified:
    - src/kalshi/position-monitor.py
    - scripts/dashboard.py
    - scripts/dashboard.html
    - tests/test_position_exits.py

key-decisions:
  - "Market orders for trailing stop exits (urgent, price-deteriorating scenario)"
  - "Illiquidity skip threshold: bid=0 or spread>20c (conservative, avoids bad fills)"
  - "Grace period prevents stale peak data from triggering exits on restart"
  - "Trailing state renamed from position-peaks.json to trailing-state.json with auto-migration"

patterns-established:
  - "evaluate_trailing_stop accepts exit_config dict for per-bot thresholds"
  - "Trailing state enriched with source_bot, first_seen, last_updated for dashboard"
  - "_EXIT_BOT_CONFIG_MAP in dashboard.py mirrors BOT_CONFIG_MAP from position-monitor.py"

requirements-completed: [EXIT-04, EXIT-05]

# Metrics
duration: 4min
completed: 2026-02-28
---

# Phase 3 Plan 2: Trailing Stop & Dashboard Exits Summary

**Trailing stop with illiquidity protection, market orders, per-bot config, and active exits dashboard panel with /api/exit-state endpoint**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-28T03:19:38Z
- **Completed:** 2026-02-28T03:23:47Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Trailing stop skips illiquid markets (no bids or spread > 20c) with logged warning
- Trailing stop uses per-bot exit_config for thresholds and market orders for urgent exit
- Trailing state persisted to trailing-state.json with source_bot, first_seen, last_updated
- Grace period prevents stale peak data from triggering exits on first scan after restart
- Dashboard has /api/exit-state endpoint and Active Exits panel with color-coded rows
- 13 new tests (11 trailing stop, 2 stale order) added; 43 exit tests total, 767 suite total

## Task Commits

Each task was committed atomically:

1. **Task 1: Refine trailing stop logic with illiquidity protection, per-bot config, market orders** - `6c577e7` (feat)
2. **Task 2: Add active exits dashboard panel with API endpoint** - `3e52ff3` (feat)

## Files Created/Modified
- `src/kalshi/position-monitor.py` - Trailing state rename + migration, evaluate_trailing_stop rewrite with illiquidity/market orders/per-bot config, grace period, enriched state
- `scripts/dashboard.py` - GET /api/exit-state endpoint serving trailing state + exit thresholds
- `scripts/dashboard.html` - Active Exits panel with table, color-coded rows, auto-refresh
- `tests/test_position_exits.py` - 13 new tests for trailing stop and stale order logic

## Decisions Made
- Market orders for trailing stop exits (urgent, price is deteriorating)
- Illiquidity skip at bid=0 or spread>20c (conservative to avoid bad fills in thin markets)
- Grace period on restart uses function attribute pattern (scan_positions._has_run)
- Trailing state file renamed from position-peaks.json with auto-migration on first load

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Xcode license agreement expired, blocking git operations. Resolved by using /Library/Developer/CommandLineTools/usr/bin/git directly (same as 03-01).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Full exit management system complete (take-profit, stop-loss, model-shift, trailing stop)
- Dashboard shows both positions and active exit state
- Phase 03 (Position Management) is fully complete
- Phase 04 (Bot Activation) can proceed

## Self-Check: PASSED

- All 4 files FOUND
- Both commits (6c577e7, 3e52ff3) FOUND
- 43 exit tests collected and passing
- 767 full test suite passes (no regressions)

---
*Phase: 03-position-management*
*Completed: 2026-02-28*
