# Deferred Items - Phase 01

## Pre-existing Issues (Out of Scope)

### 1. test_optimization.py import error
- **Found during:** 01-01 Task 2 (test validation)
- **Issue:** `beatrelease-scanner.py` has uncommitted local changes that import `ScanSummary` from `kalshi_auth`, but `ScanSummary` does not exist in `kalshi_auth.py`. This causes `test_optimization.py` to fail at collection time.
- **Impact:** 1 test file (test_optimization.py) cannot be collected; 527 other tests pass.
- **Fix:** Either add `ScanSummary` class to `kalshi_auth.py` or update `beatrelease-scanner.py` to remove the import. This is related to uncommitted local development work, not the 01-01 plan changes.
