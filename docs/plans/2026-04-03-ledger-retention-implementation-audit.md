# Ledger Retention Implementation Audit

Generated: 2026-04-03
Status: Implemented in dev, validated, first staged dev-ledger rollout completed, ready for staged promotion.

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

- `58 passed`

Additional validation:

- The existing demo runtime was cut over to the isolated demo deploy root before this dev implementation, so the dev ledger changes are not attached to the live demo bots until an explicit demo worktree promotion.

## Operational Hardening

The first real dev-ledger maintenance attempt exposed one rollout issue that did not show up in the tmp-path tests:

- the archive write succeeded, but prune was still issuing a giant `DELETE ... WHERE event_id IN (...)` statement for each archived partition
- on the 54 GB legacy ledger, that prune path was too slow and scaled poorly on large `trade_decision` dates

That was fixed in `src/kalshi/event_ledger.py` before completing the real rollout:

- prune now deletes by archived partition predicate:
  - `event_type = ?`
  - `event_time < cutoff_time`
  - `event_date = ?`
- the archive/prune step now verifies exact rowcount parity and raises if the archived row count and deleted row count diverge

The focused ledger suite was rerun after this fix and remained green.

## First Real Rollout

Completed on the old dev ledger:

- ledger path: `data/event-ledger.sqlite3`
- archive root: `data/archive/events`
- event type: `trade_decision`
- staged batches:
  1. `2026-03-14`
  2. `2026-03-15`
  3. `2026-03-16`

Real result:

- archived rows: `1,270,254`
- archived dates: `2026-03-14`, `2026-03-15`, `2026-03-16`
- cold-store size after first rollout: about `92 MB`
- hot rows remaining for those dates: `0`

Artifacts written:

- `data/archive/events/trade_decision/date=2026-03-14/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-14/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-15/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-15/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-16/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-16/manifest.json`
- `data/reports/ledger-retention-latest.json`

Logical space reclaimed in hot SQLite:

- `freelist_count = 537,280`
- `page_size = 4096`
- reclaimable on next compact: about `2.05 GiB`

The SQLite file itself remains about `54 GB` because no `VACUUM` has been run yet.

## Important Design Notes

- The original plan proposed Parquet + DuckDB.
- The implemented version uses `jsonl.gz + manifest.json`.
- That deviation was deliberate:
  - no new data/query dependency was required
  - the archive path can run on the existing runtime immediately
  - archive-aware readers live inside `EventLedger`, so downstream scripts did not need broad rewrites

## Known Operational Follow-Up

- The old dev-root ledger at `data/event-ledger.sqlite3` is now about 54 GB before compaction.
- The first real staged archive/prune run is complete, but the broader legacy-ledger rollout should still proceed by event type / event-date batch.
- The first likely rollout order remains:
  1. `trade_decision`
  2. `forecast_snapshot`
  3. `market_snapshot`
  4. `source_observation`
  5. `position_snapshot`
  6. `budget_decision`
- The next three `trade_decision` dates are materially larger:
  - `2026-03-17`: `1,901,132`
  - `2026-03-18`: `2,133,122`
  - `2026-03-19`: `2,275,705`
- Because of that step-up, the next rollout should remain bounded and should not compact until more hot space has been reclaimed.
