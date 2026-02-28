# Roadmap: Kalshi Quant Trading System

## Overview

This system has extensive infrastructure but near-zero executed trades and zero model validation. The roadmap progresses from establishing measurement (feedback loop) through fixing the plumbing (sizing, exits) to activating bots, validating models, expanding strategies, and hardening operations. Each phase unlocks the next -- you cannot optimize what you cannot measure, cannot trade safely without proper sizing and exits, and cannot expand what is not yet working.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Feedback Loop** - Settlement reconciliation, Brier scores, calibration curves, and P&L tracking
- [ ] **Phase 2: Position Sizing** - Fix Kelly bankroll basis, standardize fees, default to quarter-Kelly
- [ ] **Phase 3: Position Management** - Enable take-profit, stop-loss, model-shift exits, trailing stops, and order TTL
- [ ] **Phase 4: Bot Activation** - Debug and activate entertainment, beatrelease, economics, strategy, and weather bots
- [ ] **Phase 5: Automated Calibration** - Daily reconcile-backtest-recalibrate pipeline with drift alerting
- [ ] **Phase 6: Crypto Validation** - Backtest crypto model, validate time-to-settlement and vol computations
- [ ] **Phase 7: New Strategies** - Box office bot, cross-platform arb execution, weather city expansion
- [ ] **Phase 8: Operational Hardening** - Daily automation, data source health monitoring, HDD endpoint recovery

## Phase Details

### Phase 1: Feedback Loop
**Goal**: The system can measure whether its probability models are accurate and whether bots are profitable
**Depends on**: Nothing (first phase)
**Requirements**: FEED-01, FEED-02, FEED-03, FEED-04, FEED-05, FEED-06
**Success Criteria** (what must be TRUE):
  1. Running `npm run reconcile` annotates trade logs with settlement outcomes and the output shows non-zero matched trades
  2. Running `npm run backtest` produces non-null Brier scores for every bot that has settled trades
  3. A calibration curve (reliability diagram) is viewable showing predicted-vs-actual probabilities binned by decile
  4. Per-bot P&L summary shows realized P&L, win rate, and Sharpe ratio computed from settled trades
  5. `config/calibration.json` contains per-city sigma parameters generated from actual settlement data
**Plans:** 5 plans

Plans:
- [x] 01-01-PLAN.md -- Settlement reconciliation pipeline: canonical trade files module, fix reconcile + backfill scripts
- [x] 01-02-PLAN.md -- Brier scores, calibration curves, P&L metrics: per-market-type breakdowns, Chart.js dashboard, daily/weekly P&L
- [x] 01-03-PLAN.md -- Per-city sigma calibration: multi-objective optimization (Brier + P&L), minimum sample thresholds
- [x] 01-04-PLAN.md -- Gap closure: execute pipeline end-to-end (reconcile + backfill + backtest + calibrate + performance) in write mode
- [ ] 01-05-PLAN.md -- UAT gap closure: fix API pagination limit (limit=1000->100) in 4 scripts, fix 3 test failures

### Phase 2: Position Sizing
**Goal**: All bots use correct position sizing math before being activated for live trading
**Depends on**: Phase 1
**Requirements**: SIZE-01, SIZE-02, SIZE-03
**Success Criteria** (what must be TRUE):
  1. Kelly sizing functions compute bankroll from available balance (after subtracting open position exposure), not total balance
  2. Fee calculation follows a single standardized pattern across all bots via the `fee_cents` parameter
  3. All bots default to quarter-Kelly sizing until their Brier score validates model accuracy
**Plans**: 2 plans

Plans:
- [ ] 02-01-PLAN.md -- Core sizing: Add quarter_kelly_sell, fix get_status bankroll, deprecate edge_after_fees, TDD tests
- [ ] 02-02-PLAN.md -- Bot migration: Switch 6 bots to quarter_kelly default, calibration-gated weather sizing

### Phase 3: Position Management
**Goal**: The position monitor actively manages open positions with exits instead of holding everything to settlement
**Depends on**: Phase 1
**Requirements**: EXIT-01, EXIT-02, EXIT-03, EXIT-04, EXIT-05
**Success Criteria** (what must be TRUE):
  1. Position monitor sells positions when bid price reaches the configured take-profit threshold (80 cents default)
  2. Position monitor sells positions when bid price drops below the configured stop-loss threshold (30 cents default)
  3. Position monitor exits positions when the current model probability disagrees with entry probability by more than 20 percentage points
  4. Trailing stop tracks peak bid value and triggers exit on a 10-cent drop from peak
  5. Resting orders older than the configured TTL (120 minutes default) are automatically cancelled
**Plans**: 2 plans

Plans:
- [x] 03-01-PLAN.md -- Take-profit (partial exits, limit orders), stop-loss (market orders), model-shift (multi-model routing), per-bot exit config, WhatsApp alerts
- [ ] 03-02-PLAN.md -- Trailing stops (illiquidity protection, peak persistence, market orders), stale order TTL, active exits dashboard panel

### Phase 4: Bot Activation
**Goal**: All existing bots execute trades regularly on validated edges with instrumented decision logging
**Depends on**: Phase 1, Phase 2, Phase 3
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04, EXEC-05, EXEC-06, EXEC-07
**Success Criteria** (what must be TRUE):
  1. Entertainment bot skip rate is below 80% and it executes multiple trades per week when album markets are active
  2. Beatrelease scanner executes at least 1 trade per week when blog content with market-relevant data exists
  3. Economics bot successfully scrapes Cleveland Fed nowcast data and evaluates CPI/GDP/Jobs markets each cycle
  4. Strategy trader produces 20+ longshot bias trades per week across available sports and entertainment markets
  5. Weather bot trades using calibrated per-city sigma parameters from calibration.json (not hardcoded defaults)
  6. Source monitor NWS arbitrage polls at 5-minute intervals during peak hours (10am-4pm local time)
  7. Decision logs for every bot show which specific filter in the cascade caused each market skip
**Plans**: TBD

Plans:
- [ ] 04-01: Debug entertainment bot filter cascade and liquidity thresholds
- [ ] 04-02: Debug beatrelease scanner LLM pipeline and trade execution
- [ ] 04-03: Debug economics bot Cleveland Fed scraper and market evaluation
- [ ] 04-04: Scale strategy trader longshot bias and activate weather calibrated trading

### Phase 5: Automated Calibration
**Goal**: A daily pipeline automatically validates model quality and suggests recalibration when drift is detected
**Depends on**: Phase 1, Phase 4
**Requirements**: CAL-01, CAL-02, CAL-03
**Success Criteria** (what must be TRUE):
  1. A daily cron job runs reconcile, then backtest, then compares Brier scores to baseline, completing without manual intervention
  2. When calibration analysis finds sigma parameters that improve Brier score, a suggestion is surfaced for human review (not auto-applied)
  3. WhatsApp alert fires within one pipeline cycle when any model's Brier score degrades more than 10% from established baseline
**Plans**: TBD

Plans:
- [ ] 05-01: Daily reconcile-backtest-alert pipeline
- [ ] 05-02: Auto-suggest calibration parameters with drift detection

### Phase 6: Crypto Validation
**Goal**: The crypto trading model is validated against historical data with documented accuracy metrics
**Depends on**: Phase 1
**Requirements**: CRYP-01, CRYP-02, CRYP-03
**Success Criteria** (what must be TRUE):
  1. Crypto model has a documented Brier score computed from backtesting against historical BTC 15-minute candle data
  2. Time-to-settlement calculation is verified against actual Kalshi crypto market durations (the T=2456min claim is confirmed or corrected)
  3. Realized volatility computation output is compared against Deribit DVOL benchmark and the discrepancy is documented
**Plans**: TBD

Plans:
- [ ] 06-01: Crypto model backtesting and time-to-settlement validation
- [ ] 06-02: Realized vol validation against Deribit DVOL

### Phase 7: New Strategies
**Goal**: The system expands into new alpha sources after existing bots are validated and trading
**Depends on**: Phase 4, Phase 6
**Requirements**: STRAT-01, STRAT-02, STRAT-03
**Success Criteria** (what must be TRUE):
  1. A box office trading bot scrapes weekend estimates from The Numbers or Box Office Mojo and evaluates KXBOX/KXMOVIE markets
  2. Cross-platform arb execution places $1-2 initial trades when Kalshi vs Polymarket price discrepancy exceeds threshold after monitoring period
  3. Weather city list matches actual Kalshi KXHIGH tickers and any missing cities are added to the config
**Plans**: TBD

Plans:
- [ ] 07-01: Box office trading bot
- [ ] 07-02: Cross-platform arb execution and weather city expansion

### Phase 8: Operational Hardening
**Goal**: The system runs reliably with automated monitoring, alerting, and self-healing for all data sources
**Depends on**: Phase 5
**Requirements**: OPS-01, OPS-02, OPS-03, OPS-04, OPS-05
**Success Criteria** (what must be TRUE):
  1. Daily P&L report runs automatically (cron) and delivers a WhatsApp summary without manual triggering
  2. Daily backtest runs automatically and sends a WhatsApp alert when Brier score drifts more than 10%
  3. Settlement reconciliation runs daily as part of the automated pipeline (not manually invoked)
  4. Data source health monitor detects stale or failed sources (NWS, Open-Meteo, Coinbase, Cleveland Fed, HDD) within 2 scan cycles and alerts
  5. HDD Sanity CMS endpoints are periodically health-checked and re-enabled automatically when they become functional
**Plans**: TBD

Plans:
- [ ] 08-01: Daily P&L, backtest, and reconciliation automation
- [ ] 08-02: Data source health monitoring and HDD endpoint recovery

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8

Note: Phase 2 and Phase 3 can execute in parallel (both depend only on Phase 1). Phase 6 can begin after Phase 1 completes, overlapping with Phases 2-5.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Feedback Loop | 4/5 | UAT gap closure | - |
| 2. Position Sizing | 0/2 | Not started | - |
| 3. Position Management | 1/2 | In progress | - |
| 4. Bot Activation | 0/4 | Not started | - |
| 5. Automated Calibration | 0/2 | Not started | - |
| 6. Crypto Validation | 0/2 | Not started | - |
| 7. New Strategies | 0/2 | Not started | - |
| 8. Operational Hardening | 0/2 | Not started | - |
