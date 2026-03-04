# Kalshi Quant Trading System

## What This Is

Automated prediction market trading system for Kalshi. Python bots execute weather forecasting, entertainment/album sales info arbitrage, crypto price, economics nowcast, and copy-trading strategies against the Kalshi REST API. The system has been built out across two major development efforts: GSD Phases 1-6 (core infrastructure) and the Consolidated Quant Desk (institutional-grade analytics and risk management).

## Core Value

Every bot must produce consistent daily P&L — bots trading regularly on validated edges, exits protecting capital, and measurable model calibration proving the math works.

## Status: ALL WORK COMPLETE

All development milestones have been completed and merged to main:

- **GSD Phases 1-6**: Feedback loop, position sizing, position management, bot activation, automated calibration, crypto validation
- **Track 1 (Alpha Generation)**: Macro engine, source monitor optimization, new strategies + ops hardening
- **Track 2 (Quant Infrastructure)**: Particle filter, correlation engine, simulation engine (regime detection, jump-diffusion)
- **Phase 7 (Microstructure & Attribution)**: P&L attribution, execution quality, edge monitoring, orderbook simulation

## Requirements

### Validated (Pre-existing Infrastructure)

- Authenticated Kalshi API client with RSA-PSS signing, retry, and demo/production switching
- Centralized probability models (weather CDF, info-arb, NWS, crypto GBM, econ nowcast, Kelly sizing)
- TradeManager with full safety guardrails (kill switch, circuit breaker, daily limits, dedup, balance check)
- Capital allocator with per-bot risk budgets, concentration limits, and circuit breaker
- Weather bot scanning KXHIGH markets with ensemble forecasts (GFS/ECMWF/ICON)
- Crypto bot trading BTC/ETH via log-normal GBM model
- Supervisor with auto-restart, crash detection, heartbeat monitoring
- FastAPI dashboard with real-time bot status, P&L, trade history, decision logs
- Backtest framework with Brier score evaluation
- S3 sync for cross-machine trade log sharing
- Decision logging (ScanSummary) for every market evaluated
- WhatsApp and webhook alerting

### Delivered (GSD Phases 1-6)

- [x] Settlement reconciliation pipeline with Brier scores (weather 0.309, crypto 0.037)
- [x] Per-city sigma calibration in calibration.json
- [x] Quarter-Kelly default sizing, calibration-gated weather tiering
- [x] Position monitor with take-profit, stop-loss, trailing stops, model-shift exits
- [x] All 6 bots activated with complete decision logging filter cascades
- [x] Daily calibration pipeline with drift detection and WhatsApp alerts
- [x] Crypto model validated (Brier 0.037, vol config near-optimal)

### Delivered (Consolidated Quant Desk)

- [x] Macro/geopolitics engine (FRED, Truflation, RSS, DeepSeek LLM)
- [x] Particle filter engine (Sequential Monte Carlo, Bayesian belief tracking)
- [x] Source monitor optimization (all 10 data sources)
- [x] Correlation & dependency layer (cross-market correlation, adjusted sizing)
- [x] Advanced simulation engine (HMM regime detector, importance sampling, jump-diffusion)
- [x] New strategies (box office bot) + ops hardening
- [x] P&L attribution engine (5-dimensional decomposition)
- [x] Execution quality analytics (fill rate, slippage, shortfall)
- [x] Edge monitor (decay detection, competitor alerts)
- [x] Orderbook simulator + MM calibration
- [x] Intelligence dashboard tab

### Out of Scope

- Building a custom frontend beyond the existing dashboard — dashboard is operational, focus on trading
- Mobile app or notifications beyond WhatsApp/webhook — current alerting is sufficient
- Multi-exchange support beyond Kalshi + Polymarket read — focus on Kalshi profitability first
- Market making at scale — Avellaneda-Stoikov bot exists, MM calibration done, enable after core bots profitable
- Real-time websocket streaming — REST polling at current intervals is sufficient for these markets

## Context

- Production runs on Mac Mini, development on MacBook; S3 syncs trade logs between them
- Bankroll: ~$5K live ($3,187 cash + $1,904 in open positions as of 2026-03-02)
- Speed edge is the primary alpha thesis: get external data (NWS actuals, album sales, earnings) before Kalshi markets price it in
- CPI concentration risk identified and addressed via correlation engine

## Constraints

- **Risk budget**: Max $5-10 per trade, $10-50 daily loss per bot — conservative sizing until models are validated
- **No scipy in core**: Probability models use math.erf for normal CDF (scipy only in infrastructure modules)
- **API rate limits**: Kalshi REST API with retry/backoff; no websocket support currently
- **Data sources**: Dependent on free/public APIs (Open-Meteo, NWS, Coinbase, Cleveland Fed, HDD Sanity CMS)
- **Production safety**: KALSHI_CONFIRM_PRODUCTION=yes required; kill switch via data/HALT_TRADING file
- **Git author**: All commits must use `andes.lee444@gmail.com`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Fix feedback loop before adding strategies | Can't optimize what you can't measure — need Brier scores first | Phases 1-6 delivered measurement infrastructure |
| Speed edge over analysis edge | Getting data first is more reliable than better models on same data | Source monitor optimized for all 10 sources |
| Conservative Kelly sizing (half/quarter) | Unvalidated models mean uncertain edge — size small until proven | Quarter-Kelly default, calibration-gated tiering |
| File-based state over database | Simplicity for single-machine deployment; atomic JSON writes sufficient | All state in data/*.json files |
| Python-only stack (no compiled deps) | Deployment simplicity; math.erf sufficient for CDF computations | scipy only in infra modules |
| Two parallel tracks | Alpha generation and quant infrastructure can develop independently | Both tracks complete, merged |
| Macro engine with curated scraping | No X API — use FRED, Truflation, curated RSS feeds | macro_engine.py delivers MacroSignal |
| Correlation-adjusted sizing | CPI concentration risk showed need for cross-market awareness | correlation_engine.py limits correlated exposure |

---
*Last updated: 2026-03-04 — all milestones complete*
