# Ledger Retention Implementation Audit

Generated: 2026-04-03
Status: Implemented in dev, validated, trade-decision backlog caught up through the rolling one-day retention window.

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

Completed on the old dev ledger in staged per-date batches:

- ledger path: `data/event-ledger.sqlite3`
- archive root: `data/archive/events`
- event type: `trade_decision`
- staged batches:
  1. `2026-03-14`
  2. `2026-03-15`
  3. `2026-03-16`
  4. `2026-03-17`
  5. `2026-03-18`
  6. `2026-03-19`
  7. `2026-03-20`
  8. `2026-03-21`
  9. `2026-03-22`
  10. `2026-03-23`
  11. `2026-03-24`
  12. `2026-03-25`
  13. `2026-03-26`
  14. `2026-03-27`
  15. `2026-03-28`
  16. `2026-03-29`
  17. `2026-03-30`
  18. `2026-03-31`
  19. `2026-04-01`
  20. `2026-04-02`
  21. `2026-04-03`

Real result:

- archived rows:
  - `2026-03-14`: `200,261`
  - `2026-03-15`: `714,004`
  - `2026-03-16`: `355,989`
  - `2026-03-17`: `1,901,132`
  - `2026-03-18`: `2,133,122`
  - `2026-03-19`: `2,275,705`
  - `2026-03-20`: `2,321,512`
  - `2026-03-21`: `2,288,923`
  - `2026-03-22`: `2,097,720`
  - `2026-03-23`: `1,979,053`
  - `2026-03-24`: `2,126,785`
  - `2026-03-25`: `2,198,575`
  - `2026-03-26`: `2,179,490`
  - `2026-03-27`: `2,171,755`
  - `2026-03-28`: `2,188,459`
  - `2026-03-29`: `2,201,282`
  - `2026-03-30`: `467,915`
  - `2026-03-31`: `17,331`
  - `2026-04-01`: `12,128`
  - `2026-04-02`: `5,275`
  - `2026-04-03`: `998,037`
- total archived rows so far: `30,352,127`
- archive partitions written: `21`
- archived dates: `2026-03-14` through `2026-04-03`
- cold-store size after the staged rollout: about `2.4 GB`
- hot `trade_decision` rows remaining older than the rolling one-day cutoff: `0`

Artifacts written:

- `data/archive/events/trade_decision/date=2026-03-14/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-14/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-15/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-15/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-16/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-16/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-17/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-17/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-18/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-18/manifest.json`
- `data/archive/events/trade_decision/date=2026-03-19/events.jsonl.gz`
- `data/archive/events/trade_decision/date=2026-03-19/manifest.json`
- `data/reports/ledger-retention-latest.json`

Logical space reclaimed in hot SQLite:

- `freelist_count = 13,291,730`
- `page_size = 4096`
- reclaimable on next compact: about `50.70 GiB`

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
- The staged `trade_decision` archive/prune rollout is now caught up to the rolling one-day retention window, but the broader legacy-ledger rollout should still proceed by event type / event-date batch.
- The first likely rollout order remains:
  1. `trade_decision`
  2. `forecast_snapshot`
  3. `market_snapshot`
  4. `source_observation`
  5. `position_snapshot`
  6. `budget_decision`
- The next rollout target should move to the next highest-volume event type:
  1. `forecast_snapshot`
  2. `market_snapshot`
  3. `source_observation`
  4. `position_snapshot`
  5. `budget_decision`
- `VACUUM` is now worth considering because the hot ledger has roughly `50.70 GiB` of reclaimable free pages, but it should still be run in a dedicated maintenance window, not chained onto retention batches.

## Compaction Planning

Current compaction preconditions:

- no active process is holding the old dev ledger open
- `trade_decision` retention is caught up to the rolling one-day window
- reclaimable free space inside SQLite is about `50.70 GiB`

Current compaction blocker:

- the host volume only has about `2.8 GiB` free
- the current compaction path is an in-place SQLite `VACUUM`
- that is not safe to run with this little free space

Operational conclusion:

- do **not** run in-place compaction on the current machine state

Recommended compaction preconditions before running:

- confirm the old dev ledger is idle with `lsof`
- confirm no active runtime is writing to `data/event-ledger.sqlite3`
- free at least `60-70 GiB` on the host volume before attempting in-place `VACUUM`

Preferred compaction command once preconditions are met:

- `python3 scripts/ledger-retention.py --event-type trade_decision --compact --json --save`

Why that command is acceptable once space exists:

- retention is already caught up, so the archive/prune portion should be a no-op
- the script already has sibling-runtime PID guards for compaction

If local free space cannot be increased enough:

- add an alternate compaction path using `VACUUM INTO` on a different volume
- write the compacted ledger to external storage
- validate counts and integrity
- swap the compacted ledger into place during a maintenance window
