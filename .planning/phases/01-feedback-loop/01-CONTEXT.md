# Phase 1: Feedback Loop - Context

**Gathered:** 2026-02-26
**Status:** Ready for planning

<domain>
## Phase Boundary

Establish the measurement foundation: settlement reconciliation, Brier score computation, calibration curves, per-bot P&L tracking, and per-city sigma calibration. This phase produces the data and metrics that every downstream phase depends on. No model changes, no bot activation, no new strategies — just measurement.

</domain>

<decisions>
## Implementation Decisions

### Reconciliation scope
- Reconcile ALL historical trades — maximum data for calibration, not just recent
- Idempotent design — safe to re-run daily without duplicating results (Claude's discretion on implementation)
- Missing settlement data handling: Claude decides best approach (mark unresolved, infer from final price, or hybrid)
- Backfill rate limiting: Claude decides based on Kalshi API rate limit documentation

### Calibration strategy
- Minimum sample size per city: Claude decides based on statistical best practice for CDF models
- Sparse data fallback: Claude decides (global pooled sigma vs nearest-climate city borrowing)
- Optimization method: Claude decides (keep grid search if fast enough, or switch to Optuna/TPE)
- Multi-objective optimization: Brier score as primary target, realized P&L as tiebreaker

### Metrics & reporting
- Storage: Both JSON files in data/ (persistent, S3-syncable) AND dashboard API endpoints (live view)
- Calibration curves: Claude decides bin count based on available sample size (10 or 20 bins)
- Brier score breakdowns: ALL levels — per bot, per market type, per city (weather), AND aggregate
- Report format: Both CLI table (quick terminal checks) AND dashboard page (visual deep-dives with charts)

### P&L computation
- P&L method: Both realized (settlement-only) AND unrealized (mark-to-market) shown separately
- Fee handling: Show both gross P&L and net-of-fees P&L side by side
- Sharpe ratio: Both rolling 30-day AND all-time, shown side by side
- P&L granularity: Daily, weekly, AND all-time cumulative per bot

### Claude's Discretion
- Reconciliation idempotency implementation (overwrite vs upsert pattern)
- Missing settlement data handling strategy
- Backfill rate limiting (1 req/sec vs 1 req/3sec based on API docs)
- Calibration minimum sample size threshold
- Sparse data fallback method (pooled global vs nearest-climate)
- Grid search vs Optuna decision (based on actual runtime)
- Calibration curve bin count (based on sample size)
- Dashboard chart library and visualization approach

</decisions>

<specifics>
## Specific Ideas

- Calibration optimization should be multi-objective: minimize Brier score as primary, maximize P&L as tiebreaker — this ensures models are both well-calibrated AND profitable
- User wants maximum granularity everywhere: all breakdown levels for Brier scores, all time windows for P&L (daily + weekly + cumulative), both rolling and all-time Sharpe
- Both CLI and dashboard output for everything — terminal for quick checks, web for deep analysis
- All metrics should be JSON-persisted in data/ for S3 sync between production Mac Mini and development MacBook

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 01-feedback-loop*
*Context gathered: 2026-02-26*
