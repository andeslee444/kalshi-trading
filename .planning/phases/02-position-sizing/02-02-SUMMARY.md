---
phase: 02-position-sizing
plan: 02
subsystem: trading
tags: [kelly-sizing, quarter-kelly, position-sizing, calibration-gate]

# Dependency graph
requires:
  - phase: 02-01
    provides: "quarter_kelly_sell function in probability.py"
provides:
  - "All 6 bots use quarter-Kelly default sizing (SIZE-03 compliant)"
  - "Strategy trader uses quarter_kelly_sell for longshot sells"
  - "Weather bot calibration-gated tiered sizing (quarter/half/high-conviction)"
affects: [03-exit-management, 04-model-calibration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Calibration-gated sizing: check _load_calibration() before choosing Kelly fraction"
    - "Uncalibrated models default to quarter-Kelly; calibrated earn half-Kelly/high-conviction"

key-files:
  created: []
  modified:
    - src/kalshi/entertainment-bot.py
    - src/kalshi/source-monitor.py
    - src/kalshi/economics-bot.py
    - src/kalshi/cross-platform-arb.py
    - src/kalshi/strategy-trader.py
    - src/kalshi/weather-bot.py
    - tests/test_economics.py
    - tests/test_arb.py

key-decisions:
  - "Weather bot uses calibration gate: _load_calibration() checks per-city data to decide sizing tier"
  - "Bracket markets always use quarter-Kelly regardless of calibration (highest uncertainty)"
  - "Descriptive sizing labels in trade records reflect calibration status (e.g. 'half-Kelly (calibrated)' vs 'quarter-Kelly (uncalibrated)')"

patterns-established:
  - "Calibration-gate pattern: cal = _load_calibration(); is_calibrated = bool(cal.get(section, {}).get(key))"

requirements-completed: [SIZE-03]

# Metrics
duration: 4min
completed: 2026-02-27
---

# Phase 02 Plan 02: Bot Migration to Quarter-Kelly Default Sizing Summary

**All 6 bots migrated from half_kelly to quarter_kelly default, with weather-bot calibration gate preserving tiered sizing for validated models**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-27T23:22:12Z
- **Completed:** 2026-02-27T23:26:38Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- Migrated 12 half_kelly call sites across entertainment-bot, source-monitor, economics-bot, cross-platform-arb to quarter_kelly
- Migrated strategy-trader from half_kelly_sell to quarter_kelly_sell (import, call site, sizing_method label, comment)
- Added calibration-gated tiered sizing to weather-bot: uncalibrated cities default to quarter-Kelly, calibrated cities preserve half-Kelly/high-conviction
- Updated test stubs in test_economics.py and test_arb.py for new quarter_kelly imports
- All 724 tests pass with zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Migrate entertainment, source-monitor, economics, and cross-platform-arb to quarter_kelly** - `de4b7b8` (feat)
2. **Task 2: Migrate strategy-trader to quarter_kelly_sell and add calibration gate to weather-bot** - `582063d` (feat)

## Files Created/Modified
- `src/kalshi/entertainment-bot.py` - 4 call sites + 4 labels migrated to quarter_kelly
- `src/kalshi/source-monitor.py` - 6 call sites + 6 labels migrated to quarter_kelly
- `src/kalshi/economics-bot.py` - 1 call site + 1 label migrated to quarter_kelly
- `src/kalshi/cross-platform-arb.py` - 1 call site + 1 label migrated to quarter_kelly
- `src/kalshi/strategy-trader.py` - Import, call site, comment, and label migrated to quarter_kelly_sell
- `src/kalshi/weather-bot.py` - Added _load_calibration import and calibration-gated 4-tier sizing
- `tests/test_economics.py` - Updated probability stub: half_kelly -> quarter_kelly
- `tests/test_arb.py` - Updated probability stub: half_kelly -> quarter_kelly

## Decisions Made
- Weather bot uses calibration gate: `_load_calibration()` checks `weather.per_city.{city_code}` to decide sizing tier
- Bracket markets always use quarter-Kelly regardless of calibration status (comes first in if-chain)
- Sizing labels are descriptive and include calibration status: "half-Kelly (calibrated)", "quarter-Kelly (uncalibrated)", "60%-Kelly (high-conviction, calibrated)"

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Updated test stubs for quarter_kelly imports**
- **Found during:** Task 1 (bot migration)
- **Issue:** test_economics.py and test_arb.py had probability module stubs defining `half_kelly` but the bots now import `quarter_kelly`, causing ImportError during test collection
- **Fix:** Changed `fake_prob.half_kelly` to `fake_prob.quarter_kelly` in both test files
- **Files modified:** tests/test_economics.py, tests/test_arb.py
- **Verification:** All 724 tests pass
- **Committed in:** de4b7b8 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary test stub update for correctness. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All bots now use conservative quarter-Kelly default sizing
- Weather bot preserves earned sizing for calibrated cities
- Phase 02 (Position Sizing) complete -- ready for Phase 03 (Exit Management)

---
*Phase: 02-position-sizing*
*Completed: 2026-02-27*
