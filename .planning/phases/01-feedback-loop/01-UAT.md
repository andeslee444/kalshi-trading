---
status: diagnosed
phase: 01-feedback-loop
source: 01-01-SUMMARY.md, 01-02-SUMMARY.md, 01-03-SUMMARY.md, 01-04-SUMMARY.md
started: 2026-02-27T22:30:00Z
updated: 2026-02-27T22:35:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Canonical Trade Files Module
expected: Import TRADE_FILES from shared module and see 10 trade files listed.
result: pass

### 2. Reconciled Settlement Data
expected: Trade logs contain settlement_result annotations (100+ trades).
result: pass

### 3. Backtest Brier Scores
expected: Run backtest.py and see per-market-type Brier scores. Aggregate ~0.31.
result: issue
reported: "backtest.py fails live with 400 error — still uses limit=1000 for settlements API (fix was only applied to analyze-performance.py). Saved data/backtest-results.json has correct Brier 0.309 from previous production run."
severity: major

### 4. Calibration Parameters
expected: config/calibration.json has per-city sigma parameters from real settlement data with n>0.
result: pass

### 5. Dashboard Loads and Shows Metrics
expected: Dashboard at localhost:3456 shows Model Quality (Brier tables, calibration charts) and P&L Analytics (cumulative P&L chart). Charts render without infinite scroll growth.
result: pass

### 6. Dashboard API Endpoints
expected: /api/backtest, /api/performance, /api/calibration-curve return valid JSON with Brier scores, P&L ~$191, and calibration data.
result: pass

### 7. Calibrate Dry Run
expected: calibrate-sigma.py --dry-run reports data availability per city, shows settled weather trades, exits without modifying calibration.json.
result: pass

### 8. Tests Pass
expected: pytest tests/ passes all 500+ tests. No failures related to phase 1 modules.
result: issue
reported: "3 test failures: test_probability.py::test_b_forecast_at_center, test_weather.py::test_bracket_forecast_at_center, test_weather.py::test_bracket_moderate_distance. All are hard-coded assertion ranges that no longer match after calibration.json was updated with real settlement data. 528 passed, 3 failed. Also test_optimization.py fails at collection (pre-existing ScanSummary import error, documented in plan 01-01)."
severity: minor

## Summary

total: 8
passed: 6
issues: 2
pending: 0
skipped: 0

## Gaps

- truth: "backtest.py runs successfully and shows per-market-type Brier scores"
  status: failed
  reason: "User reported: backtest.py fails live with 400 error — still uses limit=1000 for settlements API (fix was only applied to analyze-performance.py). Saved data/backtest-results.json has correct Brier 0.309 from previous production run."
  severity: major
  test: 3
  root_cause: "Kalshi API rejects limit=1000 on portfolio endpoints (settlements, fills). Fix was applied to analyze-performance.py but not to backtest.py, reconcile-trades.py, audit.py, or calibrate-sigma.py."
  artifacts:
    - path: "scripts/backtest.py"
      issue: "line 110: limit=1000 on /portfolio/settlements, line 127: limit=1000 on /portfolio/fills"
    - path: "scripts/reconcile-trades.py"
      issue: "line 43: limit=1000 on /portfolio/settlements, line 73: limit=1000 on /portfolio/fills"
    - path: "scripts/audit.py"
      issue: "line 162: limit=1000 on /portfolio/settlements, line 179: limit=1000 on /portfolio/fills"
    - path: "scripts/calibrate-sigma.py"
      issue: "line 65: limit=1000 on /portfolio/settlements"
  missing:
    - "Change limit=1000 to limit=100 in all portfolio endpoint calls across backtest.py, reconcile-trades.py, audit.py, calibrate-sigma.py"

- truth: "All tests pass after phase 1 changes"
  status: failed
  reason: "User reported: 3 test failures in weather probability bracket tests, plus pre-existing test_optimization.py collection error."
  severity: minor
  test: 8
  root_cause: "calibration.json updated sigma from default 2.0 to 5.5 — wider sigma produces lower bracket probabilities (0.072 vs expected >0.10). Tests lack _reset_calibration() in setup/teardown, so they inherit calibrated values instead of testing default model behavior. Separately, test_optimization.py mock is missing ScanSummary and load_trades stubs."
  artifacts:
    - path: "tests/test_probability.py"
      issue: "TestWeatherProbability class missing setup_method/_reset_calibration() — line 138-142 assertion fails"
    - path: "tests/test_weather.py"
      issue: "TestComputeProbability class missing setup_method/_reset_calibration() — lines 193-198, 205-208 fail"
    - path: "tests/test_optimization.py"
      issue: "_load_beatrelease_scanner() mock missing ScanSummary and load_trades stubs — lines 86-91"
  missing:
    - "Add setup_method/teardown_method with _reset_calibration() to TestWeatherProbability in test_probability.py"
    - "Add setup_method/teardown_method with _reset_calibration() to TestComputeProbability in test_weather.py"
    - "Add ScanSummary stub and load_trades stub to _load_beatrelease_scanner() in test_optimization.py"
