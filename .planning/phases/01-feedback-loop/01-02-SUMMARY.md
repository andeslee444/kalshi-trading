---
phase: 01-feedback-loop
plan: 02
subsystem: analytics
tags: [brier-score, calibration, pnl, sharpe, chart-js, dashboard, backtest]

# Dependency graph
requires:
  - "Canonical TRADE_FILES module (src/kalshi/trade_files.py) from 01-01"
provides:
  - "Per-bot, per-market-type, per-city Brier scores with sample sizes"
  - "Per-market-type calibration curves (reliability diagrams) with adaptive binning"
  - "Daily/weekly/cumulative P&L with gross and net-of-fees columns"
  - "Rolling 30-day and all-time Sharpe ratio per bot and aggregate"
  - "JSON persistence of backtest results and performance metrics for S3 sync"
  - "Dashboard API endpoints: /api/backtest, /api/calibration-curve, /api/performance"
  - "Chart.js calibration curve scatter plots and cumulative P&L line chart in dashboard"
affects: [01-feedback-loop, calibration, dashboard, daily-automation]

# Tech tracking
tech-stack:
  added: [chart.js]
  patterns:
    - "classify_market_type() for ticker-to-category mapping across backtest pipeline"
    - "Adaptive binning: 10-bin default with fallback to 5 if sparse data"
    - "_atomic_write_json for all JSON persistence (replaces raw file writes)"
    - "Performance metrics JSON structure: per_bot with daily_pnl, aggregate with cumulative/weekly"

key-files:
  created: []
  modified:
    - "scripts/backtest.py"
    - "scripts/analyze-performance.py"
    - "scripts/dashboard.py"
    - "scripts/dashboard.html"
    - "scripts/s3-sync.sh"

key-decisions:
  - "Used 10-bin calibration default with automatic fallback to 5 bins when any bin has <5 observations"
  - "Backtest JSON output maintains backward compat with 'brier_score' key alongside new 'aggregate_brier'"
  - "Performance metrics derived from reconciliation data (settlements API) rather than local trade logs"
  - "Chart.js via CDN (not bundled) to keep dashboard single-file and avoid build tooling"
  - "Cumulative P&L chart color is green if positive, red if negative at endpoint"

patterns-established:
  - "classify_market_type(ticker) for grouping: weather/crypto/economics/entertainment/other"
  - "Dashboard API endpoints read from data/*.json files (no live computation)"
  - "Performance metrics saved with --reconcile --save flags (requires API access)"

requirements-completed: [FEED-03, FEED-04, FEED-05]

# Metrics
duration: 6min
completed: 2026-02-26
---

# Phase 1 Plan 02: Brier Scores, Calibration Curves, P&L Metrics Summary

**Per-market-type Brier breakdowns with adaptive calibration curves, daily/weekly/cumulative P&L with rolling Sharpe, Chart.js dashboard visualization, and three new API endpoints**

## Performance

- **Duration:** 6 min
- **Started:** 2026-02-26T08:11:08Z
- **Completed:** 2026-02-26T08:17:35Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Enhanced backtest.py with per-market-type, per-city, and per-bot Brier score breakdowns with sample sizes and low-sample flagging
- Added adaptive calibration curves (10 bins default, falls back to 5 if sparse) per market type
- Enhanced analyze-performance.py with daily/weekly/cumulative P&L, gross vs net-of-fees, rolling 30-day and all-time Sharpe per bot
- Added three dashboard API endpoints (/api/backtest, /api/calibration-curve, /api/performance) reading from JSON files
- Added Chart.js calibration curve scatter plots (one per market type with perfect-calibration diagonal) and cumulative P&L line chart
- Both scripts now import from canonical trade_files module (10 files) and use _atomic_write_json for persistence
- Added performance-metrics.json to S3 sync whitelist
- All 531 tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Add per-market-type Brier breakdowns and calibration curve output to backtest.py** - `4b94b1a` (feat)
2. **Task 2: Add comprehensive P&L metrics and dashboard visualization** - `443ca76` (feat)

## Files Created/Modified
- `scripts/backtest.py` - Per-market-type/per-city Brier, calibration curves, classify_market_type(), adaptive binning, atomic JSON save
- `scripts/analyze-performance.py` - Daily/weekly/cumulative P&L, rolling Sharpe, gross/net separation, --save flag, canonical trade_files import
- `scripts/dashboard.py` - Three new API endpoints: /api/backtest, /api/calibration-curve, /api/performance
- `scripts/dashboard.html` - Chart.js CDN, Model Quality section (Brier tables + calibration charts), P&L Analytics section (table + cumulative chart)
- `scripts/s3-sync.sh` - Added performance-metrics.json to sync whitelist

## Decisions Made
- Used 10-bin calibration default with automatic fallback to 5 bins when any bin has <5 observations -- balances granularity with statistical validity
- Maintained backward compatibility in JSON output: `brier_score` key kept alongside new `aggregate_brier`
- Performance metrics are derived from reconciliation data (settlements API) rather than local trade logs -- this means --save requires --reconcile
- Chart.js loaded via CDN to keep dashboard as a single HTML file with no build tooling
- Market type classification uses ticker prefix (KXHIGH=weather, KXBTC/KXETH=crypto, etc.) with bot label fallback for ambiguous tickers

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Extended bot label matching for expanded trade files**
- **Found during:** Task 1
- **Issue:** With 10 trade files (up from 4), backtest needed to handle additional bot labels like "monitor" and "economics" that the old 4-file list didn't include
- **Fix:** Added "monitor" to entertainment-style re-evaluation, "economics" to crypto-style (stored model_prob), and a generic fallback for any other bot
- **Files modified:** scripts/backtest.py
- **Verification:** All tests pass, script runs without errors
- **Committed in:** 4b94b1a (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** Essential for correctness -- the new trade file sources need re-evaluation handlers. No scope creep.

## Issues Encountered
None

## User Setup Required

None - no external service configuration required. To populate data files:
- Run `python3 scripts/backtest.py --save` to generate `data/backtest-results.json`
- Run `python3 scripts/analyze-performance.py --reconcile --save` to generate `data/performance-metrics.json`
- Dashboard at localhost:3456 will auto-load these files when they exist

## Next Phase Readiness
- All Brier score and P&L analytics infrastructure is in place for Plan 03 (calibration optimization)
- Calibration curves per market type provide the feedback loop data needed to tune sigma parameters
- Daily P&L with Sharpe provides the profit metric for multi-objective calibration optimization
- Dashboard visualizes all metrics for human review and monitoring

## Self-Check: PASSED

All files verified present, all commits verified in git log.

---
*Phase: 01-feedback-loop*
*Completed: 2026-02-26*
