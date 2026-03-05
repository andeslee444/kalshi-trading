---
phase: quick-6
plan: 1
subsystem: trading
tags: [weather-bot, ticker-parsing, calibration, sigma]

requires:
  - phase: quick-2
    provides: "Improved sigma intercept 1.5 default"
  - phase: quick-3
    provides: "Skew-normal model designed for sigma=1.5, df=6"
provides:
  - "KXHIGHINFLATION filter in weather-bot scan loop"
  - "Purged stale calibration.json weather sigma overrides"
  - "Regression tests for all 20 config city codes"
affects: [weather-bot, probability, calibration]

tech-stack:
  added: []
  patterns: ["Pre-filter non-target tickers before parse to avoid noisy warnings"]

key-files:
  created: []
  modified:
    - src/kalshi/weather-bot.py
    - config/calibration.json
    - tests/test_ticker_utils.py

key-decisions:
  - "Removed per_city and global sigma overrides entirely rather than updating them, since the 1.5 default from quick-2 is the correct value"
  - "Removed df=30 from calibration to let probability.py use its default df=6 (Student-t fat tails as designed in quick-3)"
  - "is_calibrated now always False, so weather bot uses quarter-Kelly for all trades (safer than 60%-Kelly on stale data)"

patterns-established:
  - "Pre-filter tickers by prefix before calling parse_ticker to avoid noisy log warnings"

requirements-completed: [TICKER-FIX, CALIBRATION-PURGE]

duration: 4min
completed: 2026-03-05
---

# Quick Task 6: Fix Weather Bot Ticker Parsing & Calibration Summary

**Filter KXHIGHINFLATION tickers from weather scan, purge stale calibration.json sigma overrides (3.6-5.5) to restore correct 1.5 default**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-05T05:26:13Z
- **Completed:** 2026-03-05T05:30:06Z
- **Tasks:** 3 (2 with commits, 1 verification-only)
- **Files modified:** 3

## Accomplishments
- Added KXHIGHINFLATION pre-filter in weather-bot.py scan loop (silent skip, no misleading warnings)
- Purged all stale weather sigma overrides from calibration.json (global_sigma_intercept=5.5, per_city DEN/AUS/CHI/NY overrides, df=30)
- Added 3 new test methods covering KXHIGHINFLATION rejection and all 20 config cities in both KXHIGH and KXHIGHT formats
- Full test suite passes: 1542 tests, 0 failures

## Task Commits

Each task was committed atomically:

1. **Task 1: Filter KXHIGHINFLATION tickers and add regression tests** - `829d930` (feat)
2. **Task 2: Purge stale calibration.json weather sigma overrides** - `a813225` (fix)
3. **Task 3: Run full test suite** - (verification only, no commit needed)

## Files Created/Modified
- `src/kalshi/weather-bot.py` - Added KXHIGHINFLATION pre-filter before parse_ticker call in scan_and_trade
- `config/calibration.json` - Removed weather.global_sigma_intercept, weather.global_sigma_slope, weather.df, weather.per_city (33 lines deleted)
- `tests/test_ticker_utils.py` - Added test_kxhighinflation_variants_return_none, test_all_config_cities_parse, test_all_config_cities_parse_t_prefix

## Decisions Made
- Removed per_city and global sigma overrides entirely rather than updating them, since the 1.5 default from quick-2 is the correct value based on NWS MAE data
- Removed df=30 from calibration so probability.py uses its default df=6 (Student-t fat tails as designed in quick-3's skew-normal model)
- Weather bot's is_calibrated flag now always evaluates to False (per_city removed), so quarter-Kelly is used for all trades. This is intentionally safer than 60%-Kelly on stale data.
- Kept historical reference data (global_brier, global_log_loss, global_pnl_cents, n) since these are read-only metrics not used by probability.py for sigma computation

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Verification Results
- `weather_sigma(0, "DEN")` = 1.5 (was 4.15 with stale calibration)
- `weather_sigma(3, "DEN")` = 2.3660 (was 4.50 with stale calibration)
- calibration.json weather section has no sigma overrides, no per_city, no df
- All 50 ticker_utils tests pass
- Full suite: 1542 passed in 18.82s

## Self-Check: PASSED

All files verified present. Both task commits found in git log. Test suite: 1542 passed, 0 failed.

---
*Quick Task: 6*
*Completed: 2026-03-05*
