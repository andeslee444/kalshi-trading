---
phase: quick-7
plan: 1
subsystem: strategy-bot
tags: [bayesian-edge, dual-tail-longshot, copula-sizing, kelly-confidence]
dependency_graph:
  requires: [probability.py, strategy-trader.py, bots-config.json]
  provides: [strategy_engine.py, bayes-params.json]
  affects: [strategy-trader.py, bots-config.json]
tech_stack:
  added: []
  patterns: [bayesian-shrinkage, moment-matching-grid-search, copula-equicorrelation, confidence-scaled-kelly]
key_files:
  created:
    - src/kalshi/strategy_engine.py
    - tests/test_strategy_engine.py
  modified:
    - src/kalshi/strategy-trader.py
    - config/bots-config.json
decisions:
  - "Bayesian Kelly multiplier uses <= 1.0 boundary for quadratic branch (1.0^2 = 1.0 is better than 1.0/2.0 = 0.5)"
  - "Moment matching grid search tolerances widened to match 0.05 alpha / 0.01 delta step resolution"
  - "Sell range expanded to 30c, buy range 70-99c per design doc Section 2"
metrics:
  duration: 11m33s
  completed: "2026-03-05"
  tasks_completed: 2
  tests_added: 44
  tests_total_passing: 1597
  files_created: 2
  files_modified: 2
---

# Quick Task 7: World-Class Strategy Bot - Bayesian Edge Model Summary

Bayesian edge model with online learning, dual-tail longshot exploitation (sell 1-30c + buy 70-99c), copula-based correlation-aware sizing with category caps, and confidence-scaled Kelly using only the math module.

## Tasks Completed

### Task 1: Create strategy_engine.py (TDD)

| Phase | Status | Commit |
|-------|--------|--------|
| RED (tests) | 44 tests written, all failing | 86c09e0 |
| GREEN (impl) | All 44 tests passing | 0251263 |

**BayesianEdgeEstimator (463 lines):**
- Hierarchical Bayesian model: Becker priors per category, online posterior updates via moment-matched shrinkage
- Per-category, per-price-bucket win/loss tracking (5 buckets: 1-5, 6-10, 11-15, 16-20, 21-30)
- Shrinkage weight = n/(n+kappa) with kappa=30: thin data stays near prior, 60+ observations converge to empirical
- Moment matching via grid search: alpha [0.1-0.9] step 0.05, delta [0.05-0.30] step 0.01
- EdgeEstimate namedtuple returns (mu_edge, sigma_edge, confidence_ratio, category, price_bucket, n_observations)
- JSON persistence (save_params/load_params) for cross-session learning

**Dual-tail longshot functions:**
- `longshot_edge_sell(price, ticker, hours, estimator)`: 1-30c range (expanded from 15c), Bayesian or fallback to point estimate
- `longshot_edge_buy(price, ticker, hours, estimator)`: 70-99c range (NO-side is overpriced longshot), symmetric Becker model

**CorrelationAwareSizer:**
- Equicorrelation copula: effective_n = n/(1+(n-1)*rho), kelly_scale = sqrt(effective_n/n)
- Category correlation priors from design doc (sports 0.15, weather 0.30, cross-category 0.02)
- Hard category caps (30% of daily budget), daily reset

**bayesian_kelly_multiplier:**
- Confidence ratio < 1.0: quadratic penalty (CR^2)
- 1.0 <= CR < 2.0: linear (CR/2.0)
- CR >= 2.0: capped at 1.0

### Task 2: Integrate into strategy-trader.py

| Commit | Description |
|--------|-------------|
| c6b0eed | Full integration with dual-tail scanning |

**Changes to strategy-trader.py:**
- New imports: BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy, quarter_kelly
- Module-level initialization of edge_estimator and correlation_sizer
- find_longshot_sells: expanded to sellMaxPrice (30c), Bayesian edge with copula scaling and confidence multiplier
- New find_longshot_buys: scans YES 70-99c markets, applies same Bayesian + copula pipeline
- run_scan: executes sell-side (top 10) then buy-side (top 5), resets daily sizer
- check_settled_trades: feeds settlement outcomes to Bayesian model, persists params
- Sell-side and buy-side both record to correlation_sizer for cap tracking

**Changes to bots-config.json:**
- Added: enableBuyLongshots, sellMaxPrice (30), buyMinPrice (70), categoryCap (0.30), singleTradeCap (0.05), bayesianEdge (true), bayesKappa (30)

## Deviations from Plan

None - plan executed exactly as written.

## Verification

1. `pytest tests/test_strategy_engine.py -v` -- 44/44 pass
2. `pytest tests/test_strategy_bugs.py -v` -- 14/14 pass
3. `pytest tests/ -x` -- 1597/1597 pass (no regressions)
4. Import verification: `from strategy_engine import BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy` -- OK
5. No scipy/numpy: `grep -c "import numpy\|import scipy" src/kalshi/strategy_engine.py` returns 0
6. Module load with stubbed deps: find_longshot_sells, find_longshot_buys, edge_estimator, correlation_sizer all present

## Commits

| Hash | Type | Description |
|------|------|-------------|
| 86c09e0 | test | Add failing tests for strategy engine (TDD RED) |
| 0251263 | feat | Implement strategy_engine.py (TDD GREEN) |
| c6b0eed | feat | Integrate Bayesian edge + dual-tail into strategy-trader |

## Self-Check: PASSED

All files exist, all commits verified, all tests pass.
