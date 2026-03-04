# Roadmap: Kalshi Quant Trading System

## Overview

Automated prediction market trading system for Kalshi. Development progressed through two major phases:

1. **GSD Milestone v1.0 (Phases 1-6):** Established measurement, fixed sizing/exits, activated bots, automated calibration, validated crypto model.
2. **Consolidated Quant Desk (Tracks T1/T2 + Phase 7):** Built institutional-grade infrastructure — macro engine, particle filter, correlation layer, simulation engine, microstructure analytics, and P&L attribution.

All work is complete and merged to main.

## GSD Milestone v1.0 — COMPLETE

- [x] **Phase 1: Feedback Loop** - Settlement reconciliation, Brier scores, calibration curves, P&L tracking
- [x] **Phase 2: Position Sizing** - Fix Kelly bankroll basis, standardize fees, default to quarter-Kelly
- [x] **Phase 3: Position Management** - Take-profit, stop-loss, model-shift exits, trailing stops, order TTL
- [x] **Phase 4: Bot Activation** - Debug and activate entertainment, beatrelease, economics, strategy, weather bots
- [x] **Phase 5: Automated Calibration** - Daily reconcile-backtest-recalibrate pipeline with drift alerting
- [x] **Phase 6: Crypto Validation** - Backtest crypto model, validate time-to-settlement and vol computations

### Phase Details

#### Phase 1: Feedback Loop — COMPLETE
**Goal**: The system can measure whether its probability models are accurate and whether bots are profitable
**Verification**: PASSED (5/5 criteria, re-verified 2026-02-27)
**Plans:**
- [x] 01-01-PLAN.md — Settlement reconciliation pipeline
- [x] 01-02-PLAN.md — Brier scores, calibration curves, P&L metrics
- [x] 01-03-PLAN.md — Per-city sigma calibration
- [x] 01-04-PLAN.md — Gap closure: execute pipeline end-to-end
- [x] 01-05-PLAN.md — UAT gap closure: API pagination + test isolation fixes

#### Phase 2: Position Sizing — COMPLETE
**Goal**: All bots use correct position sizing math before being activated for live trading
**Verification**: PASSED (11/11 criteria, 2026-02-27)
**Plans:**
- [x] 02-01-PLAN.md — Core sizing: quarter_kelly_sell, bankroll fix, deprecate edge_after_fees
- [x] 02-02-PLAN.md — Bot migration: 6 bots to quarter_kelly default, calibration-gated weather

#### Phase 3: Position Management — COMPLETE
**Goal**: Position monitor actively manages open positions with exits
**Verification**: PASSED (10/10 criteria, 2026-02-28)
**Plans:**
- [x] 03-01-PLAN.md — Take-profit, stop-loss, model-shift, per-bot exit config, WhatsApp alerts
- [x] 03-02-PLAN.md — Trailing stops, stale order TTL, active exits dashboard panel

#### Phase 4: Bot Activation — COMPLETE
**Goal**: All existing bots execute trades regularly on validated edges with instrumented decision logging
**Verification**: PASSED (7/7 criteria, 2026-02-27)
**Plans:**
- [x] 04-01-PLAN.md — Entertainment bot filter cascade
- [x] 04-02-PLAN.md — Beatrelease + Economics debugging
- [x] 04-03-PLAN.md — Strategy trader scaling
- [x] 04-04-PLAN.md — Weather calibration + NWS adaptive polling

#### Phase 5: Automated Calibration — COMPLETE
**Goal**: Daily pipeline automatically validates model quality and suggests recalibration
**Verification**: PASSED (10/10 criteria, 2026-02-28)
**Plans:**
- [x] 05-01-PLAN.md — Daily reconcile-backtest-alert pipeline
- [x] 05-02-PLAN.md — Calibration suggestion generation

#### Phase 6: Crypto Validation — COMPLETE
**Goal**: Crypto trading model validated against historical data with documented accuracy metrics
**Verification**: PASSED (9/9 criteria, 2026-02-28)
**Plans:**
- [x] 06-01-PLAN.md — Data pipeline + settlement audit
- [x] 06-02-PLAN.md — Market replay backtest + vol sweep

## Consolidated Quant Desk — COMPLETE

Design doc: `docs/plans/2026-03-02-consolidated-quant-desk-design.md`

### Track 1: Alpha Generation — COMPLETE

- [x] **T1-Phase 1: Macro/Geopolitics Engine** — FRED API, Truflation, RSS sentiment, DeepSeek LLM extraction. New module: `macro_engine.py`
- [x] **T1-Phase 2: Source Monitor Optimization** — All 10 data sources optimized, 50 new tests. Merged at `06fa019`.
- [x] **T1-Phase 3: New Strategies + Ops Hardening** — Box office bot, weather city expansion, daily P&L automation, health monitoring, HDD health checks.

### Track 2: Quant Infrastructure — COMPLETE

- [x] **T2-Phase 1: Particle Filter Engine** — Sequential Monte Carlo, Bayesian belief tracking. New module: `particle_filter.py`
- [x] **T2-Phase 2: Correlation & Dependency Layer** — Cross-market correlation, dependency-adjusted sizing. New module: `correlation_engine.py`. 83 tests. Merged at `a2ea2eb`.
- [x] **T2-Phase 3: Advanced Simulation Engine** — HMM regime detector, importance sampling, jump-diffusion model. New modules: `regime_detector.py`, `simulation.py`. 55 tests. Merged at `c60c113`.

### Phase 7: Microstructure & Attribution — COMPLETE

- [x] P&L Attribution Engine (5-dimensional decomposition)
- [x] Execution Quality Analytics (fill rate, slippage, shortfall)
- [x] Edge Monitor (decay detection, competitor alerts)
- [x] Agent-Based Orderbook Simulator (MM calibration)
- [x] Intelligence Dashboard Tab (attribution, edge decay, execution quality views)

## Progress

| Phase | Status | Completed |
|-------|--------|-----------|
| 1. Feedback Loop | COMPLETE | 2026-02-27 |
| 2. Position Sizing | COMPLETE | 2026-02-27 |
| 3. Position Management | COMPLETE | 2026-02-28 |
| 4. Bot Activation | COMPLETE | 2026-02-27 |
| 5. Automated Calibration | COMPLETE | 2026-02-28 |
| 6. Crypto Validation | COMPLETE | 2026-02-28 |
| T1: Alpha Generation (3 phases) | COMPLETE | 2026-03-03 |
| T2: Quant Infrastructure (3 phases) | COMPLETE | 2026-03-03 |
| Phase 7: Microstructure | COMPLETE | 2026-03-04 |
