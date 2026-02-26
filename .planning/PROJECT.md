# Kalshi Quant Trading System

## What This Is

Automated prediction market trading system for Kalshi. Python bots execute weather forecasting, entertainment/album sales info arbitrage, crypto price, economics nowcast, and copy-trading strategies against the Kalshi REST API. The system has infrastructure (auth, risk controls, probability models, supervisor, dashboard) but most bots are producing zero or near-zero executed trades despite being technically operational.

## Core Value

Every bot must produce consistent daily P&L — bots trading regularly on validated edges, exits protecting capital, and measurable model calibration proving the math works.

## Requirements

### Validated

- Authenticated Kalshi API client with RSA-PSS signing, retry, and demo/production switching — existing
- Centralized probability models (weather CDF, info-arb, NWS, crypto GBM, econ nowcast, Kelly sizing) — existing
- TradeManager with full safety guardrails (kill switch, circuit breaker, daily limits, dedup, balance check) — existing
- Capital allocator with per-bot risk budgets, concentration limits, and circuit breaker — existing
- Weather bot scanning KXHIGH markets with ensemble forecasts (GFS/ECMWF/ICON) — existing
- Crypto bot trading BTC/ETH via log-normal GBM model — existing
- Supervisor with auto-restart, crash detection, heartbeat monitoring — existing
- FastAPI dashboard with real-time bot status, P&L, trade history, decision logs — existing
- Backtest framework with Brier score evaluation — existing
- S3 sync for cross-machine trade log sharing — existing
- Decision logging (ScanSummary) for every market evaluated — existing
- WhatsApp and webhook alerting — existing

### Active

- [ ] Diagnose and fix why bots aren't executing trades (entertainment, beatrelease, economics, strategy all near-zero)
- [ ] Build settlement reconciliation pipeline (reconcile + backfill + validate Brier scores)
- [ ] Run and validate per-city sigma calibration (calibration.json is empty)
- [ ] Fix position monitor to actually execute exits (take-profit, stop-loss, model-shift)
- [ ] Debug and fix entertainment bot (99.7% skip rate, 2 trades ever)
- [ ] Debug and fix beatrelease scanner (zero executed trades, LLM pipeline untested)
- [ ] Debug and fix economics bot (Cleveland Fed scraper, zero executed trades)
- [ ] Build box office trading bot (framework exists in probability.py, bot doesn't)
- [ ] Scale longshot bias selling (strategy-trader: 5 trades ever, should be 100x more)
- [ ] Enable cross-platform arb execution (Polymarket monitoring exists, execution disabled)
- [ ] Validate crypto model calibration (claimed 8-80% edges vs 3-5% signal quality)
- [ ] Build automated calibration pipeline (reconcile -> backtest -> recalibrate, daily)
- [ ] Verify and expand weather city coverage against actual Kalshi tickers
- [ ] Harden speed edge: get data (NWS actuals, earnings, press releases) before market prices it in
- [ ] Set up daily automation (P&L reports, backtest drift alerts, settlement reconciliation)

### Out of Scope

- Building a custom frontend beyond the existing dashboard — dashboard is operational, focus on trading
- Mobile app or notifications beyond WhatsApp/webhook — current alerting is sufficient
- Multi-exchange support beyond Kalshi + Polymarket read — focus on Kalshi profitability first
- Market making at scale — Avellaneda-Stoikov bot exists but disabled, revisit after core bots profitable
- Real-time websocket streaming — REST polling at current intervals is sufficient for these markets

## Context

- Production runs on Mac Mini, development on MacBook; S3 syncs trade logs between them
- The system has been live but underperforming: weather bot has the most trades but unvalidated edges; all other bots have near-zero execution
- calibration.json is empty — all probability models running on hardcoded defaults
- Zero settlement reconciliation has been done — Brier scores are null, no model validation exists
- Position monitor has zero exits ever — all positions held to settlement
- The quant roadmap (docs/plans/2026-02-26-quant-roadmap.md) prioritizes: P0 feedback loop, P1 enable alpha sources, P2 new strategies, P3 model improvements, P4 operational hardening
- Speed edge is the primary alpha thesis: get external data (NWS actuals, album sales, earnings) before Kalshi markets price it in

## Constraints

- **Risk budget**: Max $5-10 per trade, $10-50 daily loss per bot — conservative sizing until models are validated
- **No scipy**: Probability models use math.erf for normal CDF to avoid heavy dependencies
- **API rate limits**: Kalshi REST API with retry/backoff; no websocket support currently
- **Data sources**: Dependent on free/public APIs (Open-Meteo, NWS, Coinbase, Cleveland Fed, HDD Sanity CMS) — any can break without notice
- **Production safety**: KALSHI_CONFIRM_PRODUCTION=yes required; kill switch via data/HALT_TRADING file
- **Git author**: All commits must use `andes.lee444@gmail.com`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Fix feedback loop before adding strategies | Can't optimize what you can't measure — need Brier scores first | -- Pending |
| Speed edge over analysis edge | Getting data first is more reliable than better models on same data | -- Pending |
| Conservative Kelly sizing (half/quarter) | Unvalidated models mean uncertain edge — size small until proven | -- Pending |
| File-based state over database | Simplicity for single-machine deployment; atomic JSON writes sufficient | -- Pending |
| Python-only stack (no compiled deps) | Deployment simplicity; math.erf sufficient for CDF computations | -- Pending |

---
*Last updated: 2026-02-26 after initialization*
