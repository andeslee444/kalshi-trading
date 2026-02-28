---
phase: 06-crypto-validation
plan: 01
subsystem: data-pipeline
tags: [coinbase, deribit, kalshi-historical, crypto, caching, settlement-audit]

# Dependency graph
requires:
  - phase: 01-feedback-loop
    provides: "retry_request, _atomic_write_json, backtest infrastructure"
provides:
  - "Cached Coinbase 15-min candles for BTC/ETH/SOL (90 days, 8640 each)"
  - "Cached Deribit DVOL hourly data for BTC/ETH (90 days, 2161 each)"
  - "Cached Kalshi settled crypto markets (40k+ markets)"
  - "Settlement duration distributions by market type and asset"
  - "estimate_time_to_settlement() validation verdict"
affects: [06-crypto-validation]

# Tech tracking
tech-stack:
  added: []
  patterns: ["fetch-and-cache with --force override", "time-window pagination for API rate limits"]

key-files:
  created:
    - "scripts/crypto-backtest.py"
    - "data/crypto-validation/coinbase-candles-BTC.json"
    - "data/crypto-validation/coinbase-candles-ETH.json"
    - "data/crypto-validation/coinbase-candles-SOL.json"
    - "data/crypto-validation/deribit-dvol-BTC.json"
    - "data/crypto-validation/deribit-dvol-ETH.json"
    - "data/crypto-validation/kalshi-crypto-markets.json"
    - "data/crypto-validation/settlement-audit.json"
  modified: []

key-decisions:
  - "estimate_time_to_settlement() validated as correct -- uses close_time - now dynamically, no hardcoded T=2456min"
  - "15-minute markets (KXBTC15M/KXETH15M) have different ticker format not handled by parse_crypto_ticker -- documented as anomalies but not blocking"
  - "SOL DVOL unavailable on Deribit -- backtest will use realized vol only for SOL"

patterns-established:
  - "Data fetch-and-cache: check cache first, skip unless --force, use _atomic_write_json for crash-safe writes"
  - "Time-window pagination: chunk large date ranges into API-friendly windows with sleep between requests"

requirements-completed: [CRYP-01, CRYP-02]

# Metrics
duration: 6min
completed: 2026-02-28
---

# Phase 6 Plan 01: Data Fetching and Settlement Audit Summary

**Coinbase/Deribit/Kalshi data pipeline with 40k+ market settlement audit validating estimate_time_to_settlement() correctness**

## Performance

- **Duration:** 6 min
- **Started:** 2026-02-28T09:37:15Z
- **Completed:** 2026-02-28T09:43:30Z
- **Tasks:** 2
- **Files modified:** 1 (scripts/crypto-backtest.py created)

## Accomplishments
- Built complete data fetching pipeline: Coinbase candles (BTC/ETH/SOL), Deribit DVOL (BTC/ETH), Kalshi historical markets (40,222 settled)
- Exhaustive settlement audit covering all 40k markets with duration distributions by type (hourly/daily/weekly) and by asset
- Validated estimate_time_to_settlement() as correct -- uses dynamic close_time computation, not hardcoded values

## Task Commits

Each task was committed atomically:

1. **Task 1: Build data fetching and caching pipeline** - `776d529` (feat)
2. **Task 2: Implement time-to-settlement exhaustive audit** - `776d529` (included in Task 1 commit -- both tasks were in same file)

## Files Created/Modified
- `scripts/crypto-backtest.py` - Main backtest harness with --fetch, --settlement-audit, --backtest, --vol-sweep subcommands (350+ lines)
- `data/crypto-validation/coinbase-candles-BTC.json` - 8,640 15-min BTC candles (90 days)
- `data/crypto-validation/coinbase-candles-ETH.json` - 8,640 15-min ETH candles (90 days)
- `data/crypto-validation/coinbase-candles-SOL.json` - 8,640 15-min SOL candles (90 days)
- `data/crypto-validation/deribit-dvol-BTC.json` - 2,161 hourly BTC DVOL points (90 days)
- `data/crypto-validation/deribit-dvol-ETH.json` - 2,161 hourly ETH DVOL points (90 days)
- `data/crypto-validation/kalshi-crypto-markets.json` - 40,222 settled crypto markets
- `data/crypto-validation/settlement-audit.json` - Structured audit results

## Decisions Made

1. **estimate_time_to_settlement() VALIDATED**: The function correctly computes `close_time - now` in minutes for each market. It does not use a hardcoded T=2456min value. The T=2456min from research was a historical average for weekly markets, not a code assumption. No fix needed.

2. **15-minute market tickers (KXBTC15M)**: These use a different format (e.g., `KXBTC15M-26FEB280415-15`) without T/B direction prefix. They are classified as "UNKNOWN" asset in the audit but their durations are still computed correctly. Not blocking -- will be addressed if needed in Plan 02.

3. **SOL DVOL limitation**: Deribit only publishes DVOL for BTC and ETH. SOL backtesting in Plan 02 will use realized vol only. Documented clearly in the script output.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed datetime.utcfromtimestamp deprecation**
- **Found during:** Task 1 (Coinbase candle fetching)
- **Issue:** `datetime.utcfromtimestamp()` is deprecated in Python 3.12+
- **Fix:** Replaced with `datetime.fromtimestamp(ts, tz=timezone.utc)`
- **Files modified:** scripts/crypto-backtest.py
- **Verification:** No deprecation warnings on re-run
- **Committed in:** 776d529

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor fix for Python version compatibility. No scope creep.

## Settlement Audit Key Findings

| Market Type | Count | Mean Duration | Median Duration |
|-------------|-------|---------------|-----------------|
| Hourly | 21,517 | 56.0 min (0.9h) | 60.0 min (1.0h) |
| Daily | 11,090 | 1,611.6 min (26.9h) | 1,662.2 min (27.7h) |
| Weekly | 7,615 | 2,286.7 min (38.1h) | 2,309.8 min (38.5h) |

- **Anomalies found:** 1,872 (mostly 15-minute markets flagged for <30min duration)
- **Overall mean:** 907.2 min (15.1 hours)
- **Overall median:** 60.0 min (1.0 hours) -- majority are hourly markets
- **T=2456min claim:** Not hardcoded anywhere. Weekly markets average 2,287 min, close to the research figure.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All cached data ready for Plan 02 (model replay backtest, vol parameter sweep)
- Coinbase candles provide price history for market replay
- Deribit DVOL provides IV benchmark for vol comparison
- Kalshi markets provide ground truth outcomes for Brier scoring
- Settlement audit provides typical durations for backtest validation

## Self-Check: PASSED

All files verified present. Commit 776d529 verified in git log.

---
*Phase: 06-crypto-validation*
*Completed: 2026-02-28*
