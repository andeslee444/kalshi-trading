---
phase: 02-position-sizing
plan: 01
subsystem: trading
tags: [kelly-criterion, position-sizing, quarter-kelly, capital-allocator]

# Dependency graph
requires:
  - phase: 01-feedback-loop
    provides: "Test infrastructure, calibration framework, probability models"
provides:
  - "quarter_kelly_sell() function for sell-side quarter-Kelly sizing"
  - "Fixed get_status() reporting available_balance as bankroll_cents"
  - "_edge_after_fees() with underscore deprecation prefix and backward-compatible alias"
  - "10 new tests (8 for quarter_kelly_sell, 2 for get_status bankroll fix)"
affects: [02-position-sizing plan 02, bot-migration, crypto-bot, strategy-trader]

# Tech tracking
tech-stack:
  added: []
  patterns: ["quarter-Kelly sell-side sizing via half_kelly_sell delegation", "underscore-prefix deprecation with public alias"]

key-files:
  created: []
  modified:
    - src/kalshi/probability.py
    - src/kalshi/capital_allocator.py
    - tests/test_kelly.py
    - tests/test_allocator.py

key-decisions:
  - "Used simple alias (edge_after_fees = _edge_after_fees) instead of warnings.warn for deprecation -- avoids log clutter in production"
  - "quarter_kelly_sell delegates to half_kelly_sell with return_details=True then halves -- mirrors quarter_kelly pattern exactly"

patterns-established:
  - "Sell-side quarter-Kelly pattern: delegate to half-Kelly, halve result, apply exposure cap"
  - "Deprecation via underscore prefix + alias for backward compatibility"

requirements-completed: [SIZE-01, SIZE-02, SIZE-03]

# Metrics
duration: 2min
completed: 2026-02-27
---

# Phase 02 Plan 01: Position Sizing Foundation Summary

**quarter_kelly_sell for sell-side quarter-Kelly sizing, get_status bankroll fix to use available_balance, and _edge_after_fees deprecation rename with backward-compatible alias**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-27T23:18:00Z
- **Completed:** 2026-02-27T23:19:59Z
- **Tasks:** 1 (TDD: RED + GREEN phases)
- **Files modified:** 4

## Accomplishments
- Added `quarter_kelly_sell()` function that delegates to `half_kelly_sell`, halves the result, and applies exposure cap -- ready for bot migration in Plan 02
- Fixed `get_status()` to report `available_balance` as `bankroll_cents` instead of `total_balance` -- prevents oversizing when capital is locked in positions
- Renamed `edge_after_fees` to `_edge_after_fees` with backward-compatible alias, preserving all existing callers (audit.py, tests)
- All 724 tests pass with zero regressions

## Task Commits

Each task was committed atomically (TDD flow):

1. **Task 1 RED: Write failing tests** - `3e30aaf` (test)
   - TestQuarterKellySell: 8 tests for sell-side quarter-Kelly
   - TestBankrollStatusReporting: 2 tests for get_status fix
2. **Task 1 GREEN: Implement** - `4f7efc6` (feat)
   - quarter_kelly_sell() in probability.py
   - get_status() fix in capital_allocator.py
   - _edge_after_fees deprecation rename

## Files Created/Modified
- `src/kalshi/probability.py` - Added quarter_kelly_sell(), renamed edge_after_fees to _edge_after_fees with alias
- `src/kalshi/capital_allocator.py` - Fixed get_status() to use available_balance for bankroll_cents, added total_balance_cents field
- `tests/test_kelly.py` - Added TestQuarterKellySell class with 8 test methods
- `tests/test_allocator.py` - Added TestBankrollStatusReporting class with 2 test methods

## Decisions Made
- Used simple alias (`edge_after_fees = _edge_after_fees`) instead of `warnings.warn()` for deprecation -- avoids log clutter in production bots that import the function
- quarter_kelly_sell mirrors the quarter_kelly pattern exactly (delegate to half-Kelly variant, halve, apply exposure cap) for consistency

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `quarter_kelly_sell` is ready for bot migration in Plan 02
- All sizing functions have test coverage
- get_status() now correctly reports available balance for monitoring dashboards

## Self-Check: PASSED

All files verified present, all commits verified in git log.

---
*Phase: 02-position-sizing*
*Completed: 2026-02-27*
