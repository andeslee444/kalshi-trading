# Ledger Retention Implementation Audit

Generated: 2026-04-03
Status: Implemented in dev, validated, ready for staged promotion.

## Goal

Ship the event-ledger hot/cold retention path that was previously only documented in the March 22 storage plan, without breaking live bot runtime artifacts or archive-blinding existing readers.

## Implemented Changes

Changed:

- `src/kalshi/event_ledger.py`
- `scripts/ledger-retention.py`
- `tests/test_event_ledger.py`
- `tests/test_ledger_retention_script.py`
- `tests/test_forecast_verifier.py`

What changed:

- Added archive/prune support for the high-volume event surfaces:
  - `trade_decision`
  - `forecast_snapshot`
  - `market_snapshot`
  - `source_observation`
  - `position_snapshot`
  - `budget_decision`
- Added a cold-store layout under:
  - `data/archive/events/<event_type>/date=YYYY-MM-DD/events.jsonl.gz`
  - `data/archive/events/<event_type>/date=YYYY-MM-DD/manifest.json`
- Added archive-aware reader behavior inside `EventLedger`, so archive-eligible reads now union hot SQLite plus archived partitions and dedupe on `event_id`.
- Added a new retention maintenance entry point:
  - `python3 scripts/ledger-retention.py`
- Added a new date-aware ledger index:
  - `idx_events_type_date_time(event_type, event_date, event_time)`

## Runtime Safety

The retention implementation does not remove or weaken the canonical runtime JSON/state artifacts. These remain unchanged:

- `data/*trades.json`
- `data/*-decisions.json`
- `data/weather-verification.json`
- `data/weather-nws-cross-check.json`
- `data/health-state.json`
- `data/allocator-state.json`

Orders, fills, settlements, verification results, and post-trade attribution remain hot in SQLite.

## Validation

Commands run:

- `pytest tests/test_event_ledger.py tests/test_ledger_retention_script.py -q`
- `pytest tests/test_event_ledger.py tests/test_ledger_retention_script.py tests/test_ledger_parity_report_script.py tests/test_research_source_catalog.py tests/test_storage.py tests/test_forecast_verifier.py -q`
- `python3 -m py_compile src/kalshi/event_ledger.py scripts/ledger-retention.py tests/test_forecast_verifier.py`

Result:

- `57 passed`

Additional validation:

- The existing demo runtime was cut over to the isolated demo deploy root before this dev implementation, so the dev ledger changes are not attached to the live demo bots until an explicit demo worktree promotion.

## Important Design Notes

- The original plan proposed Parquet + DuckDB.
- The implemented version uses `jsonl.gz + manifest.json`.
- That deviation was deliberate:
  - no new data/query dependency was required
  - the archive path can run on the existing runtime immediately
  - archive-aware readers live inside `EventLedger`, so downstream scripts did not need broad rewrites

## Known Operational Follow-Up

- The old dev-root ledger at `data/event-ledger.sqlite3` is already about 51 GB.
- The code path is implemented and tested, but a real historical archive/prune run on that legacy ledger is still an operational maintenance job and should be staged by event type / event-date batch.
- The first likely rollout order remains:
  1. `trade_decision`
  2. `forecast_snapshot`
  3. `market_snapshot`
  4. `source_observation`
  5. `position_snapshot`
  6. `budget_decision`
