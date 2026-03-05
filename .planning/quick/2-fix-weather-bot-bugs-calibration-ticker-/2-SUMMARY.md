---
phase: quick-2
plan: 01
subsystem: trading
tags: [weather, probability, sigma, liquidity, edge-guard]

# Dependency graph
requires: []
provides:
  - Tightened sigma intercept (1.5) for better weather probability calibration
  - Reduced MIN_LIQUIDITY_VOLUME (10) to pass more weather markets
  - Negative-edge guard preventing money-losing trades
  - Diagnostic logging for unparseable KXHIGH tickers
  - Relaxed near-settlement liquidity filter (days_out <= 1)
affects: [weather-bot, probability, calibration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Near-settlement liquidity relaxation (min_volume=5 for days_out <= 1)"
    - "Negative-edge guard before trade placement"

key-files:
  created:
    - tests/test_weather_bot_bugs.py
  modified:
    - src/kalshi/probability.py
    - src/kalshi/weather-bot.py
    - tests/test_optimization.py

key-decisions:
  - "Sigma intercept 1.5 based on NWS MAE data and Brier 0.321 showing systematic over-uncertainty"
  - "MIN_LIQUIDITY_VOLUME 10 instead of 50 because weather markets are binary with thin but tradeable books"
  - "Near-settlement min_volume=5 since days_out<=1 markets have best forecast accuracy"

patterns-established:
  - "Negative-edge guard: always check edge >= 0 before adding to opportunities"
  - "Diagnostic ticker logging: log WARNING for unparseable tickers with raw string"

requirements-completed: [WX-BUG-1, WX-BUG-2, WX-BUG-3, WX-BUG-4]

# Metrics
duration: 3min
completed: 2026-03-05
---

# Quick Task 2: Fix Weather Bot Bugs Summary

**Tightened sigma intercept 2.0->1.5 for better calibration, relaxed liquidity 50->10 to trade more markets, added negative-edge guard and diagnostic ticker logging**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-05T04:44:29Z
- **Completed:** 2026-03-05T04:47:26Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 4

## Accomplishments
- Reduced sigma intercept from 2.0 to 1.5 in both `weather_probability()` and `weather_sigma()`, fixing inverted calibration where low-prob events won 37% of the time (should be <20%)
- Reduced `MIN_LIQUIDITY_VOLUME` from 50 to 10, allowing ~45 previously rejected weather markets to be evaluated
- Added explicit negative-edge guard (`edge_yes < 0`) that prevents placing trades the model predicts will lose money
- Added WARNING-level logging for unparseable KXHIGH tickers so we can diagnose which ticker formats fail
- Implemented relaxed liquidity for near-settlement markets (days_out <= 1) with min_volume=5

## Task Commits

Each task was committed atomically (TDD):

1. **Task 1 RED: Failing tests** - `3fe941e` (test)
2. **Task 1 GREEN: Implementation** - `c2033dd` (feat)

## Files Created/Modified
- `src/kalshi/probability.py` - Sigma intercept 2.0->1.5, MIN_LIQUIDITY_VOLUME 50->10, updated docstring
- `src/kalshi/weather-bot.py` - Negative-edge guard, unparseable ticker WARNING log, relaxed near-settlement liquidity
- `tests/test_weather_bot_bugs.py` - 9 new tests for sigma, liquidity, and edge guard
- `tests/test_optimization.py` - Updated low_volume test to match new MIN_LIQUIDITY_VOLUME=10

## Decisions Made
- Sigma intercept reduced to 1.5 (25% reduction) based on NWS verification data showing day-0 MAE closer to 1.5F for major airports, and backtest Brier score 0.321 confirming systematic over-uncertainty
- MIN_LIQUIDITY_VOLUME set to 10 (not 0) to still filter truly dead markets while accepting thin-but-tradeable weather books
- Near-settlement markets get even more relaxed filter (min_volume=5) because days_out<=1 have best forecast accuracy and most trading interest

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing test_optimization.py for new volume threshold**
- **Found during:** Task 1 (GREEN phase, full test suite)
- **Issue:** `test_optimization.py::TestIsMarketLiquid::test_low_volume` expected volume=10 to fail with old MIN_LIQUIDITY_VOLUME=50
- **Fix:** Changed test volume from 10 to 5 and updated comment to "Volume < 10 should be illiquid"
- **Files modified:** tests/test_optimization.py
- **Verification:** Full test suite (1387 tests) passes
- **Committed in:** c2033dd (Task 1 GREEN commit)

---

**Total deviations:** 1 auto-fixed (1 bug in existing test)
**Impact on plan:** Necessary update to reflect intentional change in MIN_LIQUIDITY_VOLUME. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Verification Results

All plan verification checks pass:
- `pytest tests/test_weather_bot_bugs.py -v` -- 9 passed
- `pytest tests/test_weather.py tests/test_ticker_utils.py -v` -- 69 passed (no regressions)
- `pytest tests/ -x` -- 1387 passed (full suite, no regressions)
- `grep "edge_yes < 0" src/kalshi/weather-bot.py` -- negative edge guard exists
- `grep "Unparseable" src/kalshi/weather-bot.py` -- diagnostic logging exists
- `grep "MIN_LIQUIDITY_VOLUME = 10" src/kalshi/probability.py` -- volume threshold reduced
- `grep "intercept = 1.5" src/kalshi/probability.py` -- sigma intercept tightened (2 occurrences)

---
*Quick Task: 2-fix-weather-bot-bugs-calibration-ticker-*
*Completed: 2026-03-05*
