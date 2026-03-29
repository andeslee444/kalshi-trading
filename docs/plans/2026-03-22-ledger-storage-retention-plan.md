# Event Ledger Storage Retention Plan

Generated: 2026-03-22
Status: Proposed. Do not implement until the Phase 4 parity observation window closes cleanly.

## Goal

Reduce hot-ledger size and parity/report latency without losing any data required by live bots, ops, research, or audit.

## Current State

- `data/event-ledger.sqlite3` has grown materially beyond the size needed for hot operational use.
- The main growth driver is append-only `trade_decision` history, not weather verification.
- Runtime bots still depend first on the canonical JSON/state artifacts documented in `docs/contract-inventory.md`.
- Phase 4 parity observation is still active through 2026-03-31, so live writer semantics should not change yet.

## Design

Use a two-tier local storage model:

1. Hot store:
   - SQLite at `data/event-ledger.sqlite3`
   - holds recent and audit-critical operational events
   - optimized for reconciliation, dashboard checks, attribution, and short-horizon investigations

2. Cold store:
   - local Parquet archive under `data/archive/events/<event_type>/date=YYYY-MM-DD/*.parquet`
   - holds older, high-volume event history indefinitely
   - queried via DuckDB or equivalent for research and long-range audits

This remains local-first storage on the machine. No cloud dependency is required.

## Runtime Safety Rule

Do not remove or weaken any current live bot contracts:

- trade logs in `data/*trades.json`
- decision logs in `data/*-decisions.json`
- `data/weather-verification.json`
- `data/weather-nws-cross-check.json`
- `data/health-state.json`
- `data/allocator-state.json`
- other shared state listed in `docs/contract-inventory.md`

These remain the primary runtime artifacts for bots and operator workflows.

## Retention Policy

Keep hot indefinitely:

- `order_submitted`
- `order_update`
- `fill`
- `settlement`
- `verification_result`
- `post_trade_attribution`

Keep hot, then archive:

- `trade_decision`: keep 24 hours hot, archive older indefinitely
- `forecast_snapshot`: keep 14 days hot, archive older indefinitely
- `market_snapshot`: keep 14 days hot, archive older indefinitely
- `source_observation`: keep 30 days hot, archive older indefinitely
- `position_snapshot`: keep 30 days hot, archive older indefinitely
- `budget_decision`: keep 90 days hot, archive older indefinitely

## Why This Split

- `trade_decision` is the dominant storage consumer and grows far faster than the value of keeping its full history in hot SQLite.
- `verification_result` is low-volume and directly useful for weather calibration, so it should stay hot.
- Orders, fills, settlements, and attribution are core audit/reconciliation data and should remain immediately available.
- Forecast, source, and market snapshots are useful historically, but they do not need to live forever in the hot operational store.

## Implementation Order

1. Add archive writer:
   - export eligible rows from SQLite to Parquet by `event_type` and date partition
   - include a manifest with row counts and content hash per batch

2. Add archive-aware readers for research and ops:
   - research queries should read hot SQLite plus archived Parquet
   - parity and dashboard paths should stay on hot SQLite unless explicitly requesting archive history

3. Add prune job:
   - only delete rows from hot SQLite after successful archive write and manifest verification
   - prune by event type and retention horizon

4. Compact SQLite:
   - after pruning, compact the hot ledger via a safe maintenance path

## Validation

Each archive/prune cycle must verify:

- archive row count matches rows selected for export
- archive hash matches the exported payload set
- hot ledger still serves all current dashboard and ops readers
- bot runtime artifacts remain unchanged
- weather verification summaries and settlement/reconciliation outputs remain identical

## Cutover Gate

Archive/prune is not gated only by the date or by the end of the observation window.
It must also be gated per event surface and per reader.

Before pruning any event type from hot SQLite:

- parity must be clean enough for that event surface
- every reader that depends on that event history must have an archive-aware path or an explicit hot-only retention exception
- the archive-aware reader must be validated before hot pruning begins

Known blocking readers to evaluate before pruning:

- `scripts/dashboard.py`
- `scripts/analyze-performance.py`
- `src/kalshi/pnl_attribution.py`
- `src/kalshi/research/source_catalog.py`
- `src/kalshi/apps/oracle_latency_report.py`
- `src/kalshi/event_ledger.py` reader methods used by these consumers

## Rollback

If any validation fails:

- stop pruning
- keep the hot SQLite rows
- leave the archive files in place for inspection
- do not change bot readers until the mismatch is understood

## Post-Phase-4 Execution Gate

Only start implementation after:

- the Phase 4 parity observation window closes cleanly
- `data/ledger-parity-report.json` is stable enough to trust the hot ledger as the canonical operational source
- any remaining parity regressions are understood or explicitly waived
- reader-by-reader archive readiness has been validated for the event types being pruned
