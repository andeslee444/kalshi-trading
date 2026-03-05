---
phase: quick-4
plan: 01
subsystem: economics-bot
tags: [bayesian, scenarios, uncertainty-kelly, cpi, position-sizing]
dependency_graph:
  requires: [macro_engine.py, probability.py, kalshi_auth.py]
  provides: [cpi_belief_filter.py, scenario_engine.py, uncertainty_kelly, EdgeScaler]
  affects: [economics-bot.py, probability.py, kalshi_auth.py, macro_engine.py]
tech_stack:
  added: [conjugate-normal-bayesian, scenario-mixture-model, uncertainty-kelly-sizing]
  patterns: [precision-weighted-fusion, scenario-agreement-metric, edge-ratchet]
key_files:
  created:
    - src/kalshi/cpi_belief_filter.py
    - src/kalshi/scenario_engine.py
    - tests/test_cpi_belief_filter.py
    - tests/test_scenario_engine.py
    - tests/test_uncertainty_kelly.py
    - tests/test_edge_scaler.py
    - tests/test_econ_integration.py
    - tests/test_sizing_ceiling.py
    - tests/test_econ_concentration.py
  modified:
    - src/kalshi/probability.py
    - src/kalshi/kalshi_auth.py
    - src/kalshi/economics-bot.py
    - src/kalshi/macro_engine.py
    - tests/test_probability.py
    - tests/test_audit.py
decisions:
  - "CPI sigma uses exponential decay 0.05 + 0.35*(1-exp(-0.05*d)) matching Cleveland Fed 90% CI width"
  - "Static config limits (maxTradeAmount, maxDailyLoss) act as ceilings via min(), not floors via max()"
  - "Concentration limits: 15% per ticker family, 25% per release date, 40% total econ exposure"
  - "uncertainty_kelly uses geometric mean of agreement_mult and sigma_mult for confidence scaling"
  - "EdgeScaler 4-tier ratchet: 0/5/10/20 profitable settlements unlock 20/40/60/100% exposure"
metrics:
  completed: "2026-03-05"
  tasks_completed: 3
  sub_tasks_completed: 16
  commits: 15
  tests_added: 55
  files_created: 9
  files_modified: 6
---

# Quick Task 4: Economics Bot World-Class Redesign Summary

Scenario-weighted Bayesian system with conjugate normal CPI fusion, 6-scenario macro mixture model, uncertainty-aware Kelly sizing, and adaptive edge scaling -- transforming the economics bot from single-distribution nowcast trading to honest uncertainty modeling.

## What Changed

### Phase 1: Bug Fixes (5 commits)

1. **BUG-1 -- CPI sigma asymptote** (`066b769`): Changed `cpi_nowcast_sigma()` from `0.03 + 0.07*(1-exp(-0.20*d))` to `0.05 + 0.35*(1-exp(-0.05*d))`. At 107 days out, sigma is now ~0.40% (matching Cleveland Fed 90% CI width) instead of fake-certain 0.10%.

2. **BUG-2 -- Sizing ceiling** (`0eaded5`): Changed `max()` to `min()` in `_effective_max_trade_cents()` and `_effective_max_daily_loss_cents()` in kalshi_auth.py. Static config limits are now ceilings (the lower value wins), not floors.

3. **BUG-3 -- Concentration limits** (`58c45bf`): Added `_ticker_family()`, `_compute_exposure()`, `_check_concentration()` to economics-bot.py with constants FAMILY_EXPOSURE_PCT=0.15, RELEASE_EXPOSURE_PCT=0.25, TOTAL_ECON_PCT=0.40.

4. **BUG-4 -- Optional MacroEngine** (`5ab3e56`): Wrapped `from macro_engine import MacroEngine` in try/except with fallback `MacroEngine = None`. Guards all macro usage with `if macro is not None`.

5. **BUG-5 -- Edge field** (`cd39c8b`): Added `edge=round(edge, 4)` to `place_order()` call so all economics trade records include the computed edge.

### Phase 2: New Modules (5 commits)

6. **CPIBeliefFilter** (`a5cdb8f`): New `src/kalshi/cpi_belief_filter.py` -- conjugate normal Bayesian filter. Precision-weighted fusion: posterior_precision = prior_precision + obs_precision. Fuses Cleveland Fed nowcast, Truflation CPI, and TIPS breakevens.

7. **Belief filter wiring** (`3278ff4`): Economics bot constructs CPIBeliefFilter with nowcast as prior, updates with truflation_cpi and tips_breakeven from macro engine. Fused nowcast and posterior sigma flow into probability computation.

8. **Scenario engine** (`1d8eba6`): New `src/kalshi/scenario_engine.py` -- 6 scenarios (status_quo, tariff_escalation, tariff_reversal, supply_shock, recession, stagflation) with ScenarioConfig(cpi_shift, sigma_mult). `scenario_probability()` returns mixture probability clamped to [0.001, 0.999] with agreement metric (1 - max-min spread).

9. **FRED proxy series** (`46125fb`): Added crude_oil (DCOILWTICO) and yield_curve (T10Y2Y) to macro_engine.py FREDClient.SERIES for scenario weight adjustment.

10. **Scenario engine wiring** (`ec22d31`): Economics bot replaces `econ_nowcast_probability()` with `scenario_probability()` mixture for CPI/GDP markets. Scenario agreement and per-scenario details added to trade records.

### Phase 3: Uncertainty Sizing (5 commits)

11. **uncertainty_kelly** (`314c776`): New function in probability.py wrapping quarter_kelly with confidence scaling. agreement_mult = 0.1 + 0.9 * agreement^2, sigma_mult = min(1.0, 0.10/sigma), confidence = sqrt(agreement_mult * sigma_mult).

12. **uncertainty_kelly wiring** (`e99fd30`): Economics bot uses uncertainty_kelly for CPI/GDP markets (keeps quarter_kelly for gas/fed). Trade records include confidence, agreement_mult, sigma_mult.

13. **EdgeScaler** (`25bfa68`): 4-tier ratchet in economics-bot.py: 0/5/10/20 profitable settlements unlock 20%/40%/60%/100% max exposure. 3-consecutive-loss streak halves the limit.

14. **Integration tests** (`1037b01`): `tests/test_econ_integration.py` with TestFullPipeline covering typical CPI trade, no-extra-sources degradation, tariff shock widening, near-threshold small edge.

15. **Regression fix** (`0407aeb`): Updated `test_audit.py::TestCPINowcastSigma::test_at_release` from 0.03 to 0.05 to match new CPI sigma floor.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] test_audit.py regression from BUG-1 CPI sigma fix**
- **Found during:** Task 15 (full test suite run)
- **Issue:** `test_at_release` asserted `cpi_nowcast_sigma(0) == 0.03` but new formula returns 0.05
- **Fix:** Updated assertion to 0.05
- **Files modified:** tests/test_audit.py
- **Commit:** 0407aeb

### Pre-existing Collection Errors (Not Fixed -- Out of Scope)

20 test files fail to collect under `pytest tests/` due to import resolution order conflicts (conftest.py path setup vs pytest collection). These are pre-existing and unrelated to this task. All 20 files work when specified individually. Logged to deferred items.

## Test Results

- **New tests added:** 55 tests across 9 new test files
- **All tests passing:** 1006 passed (full suite minus pre-existing collection issues)
- **Test files created:**
  - tests/test_cpi_belief_filter.py (10 tests)
  - tests/test_scenario_engine.py (13 tests)
  - tests/test_uncertainty_kelly.py (6 tests)
  - tests/test_edge_scaler.py (7 tests)
  - tests/test_econ_integration.py (4 tests)
  - tests/test_sizing_ceiling.py (6 tests)
  - tests/test_econ_concentration.py (8 tests)
  - tests/test_probability.py updates (1 test modified)

## Commits

| # | Hash | Message |
|---|------|---------|
| 1 | 066b769 | fix: widen CPI sigma asymptote from 0.10% to 0.40% |
| 2 | 0eaded5 | fix: static config is ceiling not floor for trade/loss limits |
| 3 | 58c45bf | fix: add per-ticker-family and total econ concentration limits |
| 4 | 5ab3e56 | fix: make MacroEngine import optional for feedparser resilience |
| 5 | cd39c8b | fix: populate edge field in economics trade records |
| 6 | a5cdb8f | feat: add CPIBeliefFilter -- Bayesian fusion for CPI nowcast sources |
| 7 | 3278ff4 | feat: wire Bayesian belief filter into economics bot |
| 8 | 1d8eba6 | feat: add scenario engine -- 6 macro scenarios with dynamic weights |
| 9 | 46125fb | feat: add crude oil and yield curve FRED series for scenario weights |
| 10 | ec22d31 | feat: wire scenario engine into economics bot |
| 11 | 314c776 | feat: add uncertainty_kelly -- confidence-scaled position sizing |
| 12 | e99fd30 | feat: use uncertainty_kelly for CPI/GDP markets |
| 13 | 25bfa68 | feat: add EdgeScaler -- auto-scaling exposure based on settlements |
| 14 | 1037b01 | test: add integration tests for full economics bot pipeline |
| 15 | 0407aeb | fix: update audit test for new CPI sigma floor (0.05 not 0.03) |

## Self-Check: PASSED

All 9 created files exist. All 15 commit hashes verified in git log.
