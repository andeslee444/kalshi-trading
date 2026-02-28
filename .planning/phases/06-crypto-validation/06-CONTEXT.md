# Phase 6: Crypto Validation - Context

**Gathered:** 2026-02-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Validate the crypto trading model (GBM/log-normal) against historical data. Produce documented accuracy metrics (Brier score, calibration curve), verify time-to-settlement calculations, and benchmark volatility computations against Deribit DVOL. If validation reveals bugs (e.g., wrong settlement times), fix them as part of this phase.

</domain>

<decisions>
## Implementation Decisions

### Historical Data & Backtest Scope
- Source BTC/ETH/SOL 15-minute candle data from Coinbase API (same source the live bot uses for spot prices — keeps backtest consistent with production)
- Backtest period: 90 days of recent data (~8,600 candles per asset)
- All three assets (BTC, ETH, SOL) must be backtested
- Replay actual Kalshi markets using the Kalshi Historical Data API (`GET /historical/markets/{ticker}/candlesticks`, `GET /historical/markets`) — NOT synthetic markets
  - Reference: https://docs.kalshi.com/getting_started/historical_data
  - Note: Kalshi is migrating historical data off live API by March 6, 2026 — use historical endpoints
- Include all crypto market durations (hourly, daily, weekly brackets)
- Store both raw per-market predictions (ticker, model prob, actual outcome, vol used, time-to-settlement) AND aggregate Brier scores

### Volatility Benchmark Methodology
- Side-by-side time series comparison: compute our realized vol at each point in time, fetch Deribit DVOL for same timestamps, plot together with rolling correlation and mean absolute error
- Pre-download and cache Deribit DVOL history to a local file (faster backtest runs, reproducible, no rate limits)
- Validate the 60% IV / 40% RV blend ratio — test alternatives (50/50, 70/30, pure IV) and measure which gives the best Brier score
- Test alternative realized vol lookback windows (6h, 12h, 24h, 48h) and document the optimal window

### Time-to-Settlement Verification
- Exhaustive audit: pull ALL settled crypto markets from Kalshi historical API, compute actual duration (open_time to close_time) for every market
- Break down settlement time distributions by market type (hourly, daily, weekly)
- Flag anomalies: markets that closed early, were voided, or had unusual settlement patterns
- If the T=2456min assumption or `estimate_time_to_settlement()` logic is wrong, fix it in this phase (not just document)

### Validation Output & Thresholds
- Brier score target: 0.20 or better (crypto is noisier than weather's ~0.15 target)
- Extend existing `data/backtest-results.json` with crypto validation results (alongside weather/other bots), plus human-readable stdout summary
- Generate calibration curve (reliability diagram) for the crypto model — reuse Phase 1 charting infrastructure
- On validation failure (Brier > 0.20): document gaps and suggest specific parameter fixes, do NOT auto-disable the bot — user decides

### Claude's Discretion
- Exact data fetching pagination strategy for Kalshi historical API
- Coinbase API candle request batching (300 per request limit)
- Chart styling and formatting details for calibration curves
- How to structure the optimization sweep (grid search vs sequential) for vol parameters

</decisions>

<specifics>
## Specific Ideas

- Use the Kalshi Historical Data API (https://docs.kalshi.com/getting_started/historical_data) — this is the canonical source, not the live API
- Backtest should be reproducible: cached data, deterministic sweep, saved raw predictions
- Vol parameter sweep should report Brier score for each configuration so optimal settings are clear

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 06-crypto-validation*
*Context gathered: 2026-02-28*
