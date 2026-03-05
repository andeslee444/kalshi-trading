---
phase: quick-9
plan: 1
subsystem: strategy-bot
tags: [wave-scheduling, settlement-sources, fill-model, info-arb]
dependency_graph:
  requires: [strategy_engine.py, strategy-trader.py, health-state.json]
  provides: [ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator, InfoEdge]
  affects: [strategy-trader.py, bots-config.json]
tech_stack:
  added: [zoneinfo]
  patterns: [sigmoid-fill-model, wave-budget-allocation, settlement-source-info-arb]
key_files:
  created: []
  modified:
    - src/kalshi/strategy_engine.py
    - src/kalshi/strategy-trader.py
    - tests/test_strategy_engine.py
    - config/bots-config.json
decisions:
  - "Wave budget split 30/50/20 across morning/midday/afternoon ET windows"
  - "Info-arb uses half_kelly sizing and full ask pricing for urgency"
  - "Fill model uses sigmoid with 4 features and online SGD learning"
  - "All three features config-gated for backward compatibility"
metrics:
  duration: "5 min"
  completed: "2026-03-05"
  tasks_completed: 2
  tasks_total: 2
  tests_added: 30
  tests_total: 72
---

# Quick Task 9: Strategy Bot Intraday Wave Scheduling Summary

Intraday wave scheduling, settlement source info-arb, and sigmoid fill probability model added to strategy bot engine and trader.

## One-liner

Three ET wave windows (8-10am/11am-2pm/3-5pm) with 30/50/20% budget split, settlement source info-arb via health-state.json for high-conviction half-kelly trades, and sigmoid fill probability model with online SGD learning.

## What was done

### Task 1: ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator (TDD)

**ScheduledScanner** -- Three ET wave windows with per-wave budget allocation. `current_wave()` returns 1/2/3/None based on hour. `remaining_budget()` tracks spend per wave. `should_scan()` gates scanning on wave + budget. `should_emergency_scan()` overrides on volume spikes. `next_scan_time()` computes sleep until next wave.

**SettlementSourceChecker** -- Reads `health-state.json` for external data freshness. Maps ticker prefixes to sources: KXHIGH->NWS (edge=0.50), ALBUM/BILLBOARD/HDD->HDD (edge=0.80), BOX/MOVIE->BoxOfficeMojo (edge=0.60). Returns `InfoEdge` namedtuple with source, edge, confidence, sizing_method, pricing_method.

**FillProbabilityEstimator** -- Sigmoid model: `P(fill) = 1/(1+exp(-(b0+b1*price_pos+b2*depth+b3*time)))`. Default betas are conservative priors. `adjust_limit_price()` moves toward ask when fill prob is low and edge is high. `update_from_outcome()` shifts betas via online SGD (lr=0.01). JSON persistence for learned betas.

30 new tests covering all three classes. 72 total tests passing.

### Task 2: Integration into strategy-trader.py

- Wave-aware daemon loop replaces fixed-interval sleep
- Strategy 0 (info-arb) runs before longshot scanning with half_kelly sizing
- Fill probability adjusts limit prices in both sell and buy functions
- Budget tracked via `scheduler.record_spend()` after every successful trade
- All features gated by config flags: `waveScheduling`, `settlementSources`, `fillModel`
- `--once` mode unchanged

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 6025d2d | feat(quick-9): add ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator |
| 2 | f63d815 | feat(quick-9): integrate wave scheduling, settlement sources, fill model |

## Deviations from Plan

None -- plan executed exactly as written.

## Self-Check: PASSED

- [x] src/kalshi/strategy_engine.py -- modified with 3 new classes
- [x] src/kalshi/strategy-trader.py -- modified with integration
- [x] tests/test_strategy_engine.py -- 72 tests passing
- [x] config/bots-config.json -- new config keys added
- [x] Commit 6025d2d exists
- [x] Commit f63d815 exists
