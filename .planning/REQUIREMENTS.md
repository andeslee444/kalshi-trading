# Requirements: Kalshi Quant Trading System

**Defined:** 2026-02-26
**Core Value:** Every bot must produce consistent daily P&L — bots trading regularly on validated edges, exits protecting capital, and measurable model calibration proving the math works.

## v1 Requirements

Requirements for this milestone. Each maps to roadmap phases.

### Feedback Loop

- [x] **FEED-01**: Settlement reconciliation pipeline annotates all trade logs with `settlement_result` field from Kalshi API
- [x] **FEED-02**: Backfill script queries individual market endpoints for trades missing settlement data
- [x] **FEED-03**: Brier score computation produces non-null scores per bot and per market type
- [x] **FEED-04**: Calibration curve (reliability diagram) shows binned predicted-vs-actual for each probability model
- [x] **FEED-05**: Per-bot P&L tracking computes realized P&L, win rate, and Sharpe ratio from settled trades
- [x] **FEED-06**: Per-city sigma calibration populates `config/calibration.json` with optimized parameters

### Position Sizing

- [x] **SIZE-01**: All Kelly sizing functions use available balance (not total balance) as bankroll basis
- [x] **SIZE-02**: Fee treatment is standardized across all bots (single pattern via `fee_cents` parameter)
- [x] **SIZE-03**: Quarter-Kelly is the default sizing until models are calibration-validated

### Position Management

- [x] **EXIT-01**: Position monitor executes take-profit exits when bid reaches 80% threshold
- [x] **EXIT-02**: Position monitor executes stop-loss exits at 30% threshold
- [x] **EXIT-03**: Position monitor executes model-shift exits when updated probability disagrees with entry by >20%
- [x] **EXIT-04**: Trailing stop logic tracks peak value and exits on 10-cent drop
- [x] **EXIT-05**: Stale resting orders are cancelled after configured TTL (120 min default)

### Bot Execution

- [x] **EXEC-01**: Entertainment bot skip rate drops below 80% (from current 99.7%) with correct liquidity filter tuning
- [x] **EXEC-02**: Beatrelease scanner executes at least 1 trade per week when blog content exists
- [x] **EXEC-03**: Economics bot successfully scrapes Cleveland Fed nowcast and evaluates CPI/GDP/Jobs markets
- [x] **EXEC-04**: Strategy trader scales longshot bias selling to 20+ trades per week across sports/entertainment
- [x] **EXEC-05**: Weather bot trades with calibrated per-city sigma parameters (not hardcoded defaults)
- [x] **EXEC-06**: Source monitor NWS arbitrage polls at 5-minute intervals during peak hours (10am-4pm local)
- [x] **EXEC-07**: Per-bot filter cascade is instrumented — decision logs show which specific filter caused each skip

### Automated Calibration

- [x] **CAL-01**: Daily pipeline runs reconcile → backtest → compare Brier scores → alert on >10% degradation
- [ ] **CAL-02**: Calibration auto-suggests new sigma parameters when improvement detected (human approves)
- [x] **CAL-03**: Drift detection alerts via WhatsApp when any model's Brier score degrades >10% from baseline

### Crypto Validation

- [ ] **CRYP-01**: Crypto model backtested against historical BTC 15-min candles with documented Brier score
- [ ] **CRYP-02**: Time-to-settlement calculation validated (T=2456min claim checked against actual market durations)
- [ ] **CRYP-03**: Realized vol computation validated against Deribit DVOL benchmark

### New Strategies

- [ ] **STRAT-01**: Box office trading bot scrapes The Numbers/Box Office Mojo weekend estimates and trades KXBOX/KXMOVIE markets
- [ ] **STRAT-02**: Cross-platform arb execution enabled with $1-2 initial position sizes after monitoring validation
- [ ] **STRAT-03**: Weather city coverage verified against actual Kalshi tickers and expanded where missing

### Operational Hardening

- [ ] **OPS-01**: Daily P&L report runs automatically and sends WhatsApp summary
- [ ] **OPS-02**: Daily backtest runs automatically and alerts on >10% Brier drift
- [ ] **OPS-03**: Settlement reconciliation runs daily as part of automated pipeline
- [ ] **OPS-04**: Data source health monitoring detects and alerts on stale/failed sources within 2 scan cycles
- [ ] **OPS-05**: HDD Sanity CMS endpoints re-enabled when functional (periodic health check)

## v2 Requirements

Deferred to future milestone. Tracked but not in current roadmap.

### Infrastructure Upgrades

- **INFRA-01**: Kalshi WebSocket integration for sub-second market updates
- **INFRA-02**: httpx async data fetching for concurrent source polling
- **INFRA-03**: Optuna-based calibration optimization replacing grid search
- **INFRA-04**: SQLite state management replacing file-locked JSON (at >100 trades/day)

### Advanced Models

- **MODEL-01**: GARCH(1,1) volatility model for crypto replacing realized vol
- **MODEL-02**: ML/DL models trained on 6+ months of labeled settlement data
- **MODEL-03**: Correlation-aware position limits across related markets

### Scaling

- **SCALE-01**: Market making (Avellaneda-Stoikov) enabled after 3+ months validated profitability
- **SCALE-02**: Multi-exchange execution beyond Kalshi + Polymarket

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| High-frequency trading infrastructure | Kalshi API caches every 5 seconds; REST limits make HFT impractical |
| Custom frontend / mobile app | Dashboard works; building UIs doesn't generate alpha |
| ML/DL models | Zero labeled training data exists; ML before data = overfitting to noise |
| WebSocket streaming (this milestone) | REST polling sufficient for 5-30 min event-driven strategies; measure latency cost first |
| Tax optimization / reporting | Zero priority until system is profitable for 3+ months |
| Social / copy trading features | Product play, not trading edge play |
| Auto-apply calibration without human review | One bad auto-tune can blow up the account |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| FEED-01 | Phase 1 | Complete |
| FEED-02 | Phase 1 | Complete |
| FEED-03 | Phase 1 | Complete |
| FEED-04 | Phase 1 | Complete |
| FEED-05 | Phase 1 | Complete |
| FEED-06 | Phase 1 | Complete |
| SIZE-01 | Phase 2 | Complete |
| SIZE-02 | Phase 2 | Complete |
| SIZE-03 | Phase 2 | Complete |
| EXIT-01 | Phase 3 | Complete |
| EXIT-02 | Phase 3 | Complete |
| EXIT-03 | Phase 3 | Complete |
| EXIT-04 | Phase 3 | Complete |
| EXIT-05 | Phase 3 | Complete |
| EXEC-01 | Phase 4 | Complete |
| EXEC-02 | Phase 4 | Complete |
| EXEC-03 | Phase 4 | Complete |
| EXEC-04 | Phase 4 | Complete |
| EXEC-05 | Phase 4 | Complete |
| EXEC-06 | Phase 4 | Complete |
| EXEC-07 | Phase 4 | Complete |
| CAL-01 | Phase 5 | Complete |
| CAL-02 | Phase 5 | Pending |
| CAL-03 | Phase 5 | Complete |
| CRYP-01 | Phase 6 | Pending |
| CRYP-02 | Phase 6 | Pending |
| CRYP-03 | Phase 6 | Pending |
| STRAT-01 | Phase 7 | Pending |
| STRAT-02 | Phase 7 | Pending |
| STRAT-03 | Phase 7 | Pending |
| OPS-01 | Phase 8 | Pending |
| OPS-02 | Phase 8 | Pending |
| OPS-03 | Phase 8 | Pending |
| OPS-04 | Phase 8 | Pending |
| OPS-05 | Phase 8 | Pending |

**Coverage:**
- v1 requirements: 35 total
- Mapped to phases: 35
- Unmapped: 0

---
*Requirements defined: 2026-02-26*
*Last updated: 2026-02-26 after roadmap creation*
