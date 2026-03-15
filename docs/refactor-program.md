# Refactor Program

Generated: 2026-03-14
Status: Active. Phases 0, 1, 2, and 3 complete on 2026-03-14. Phase 4 implementation is complete on 2026-03-14; parity observation is now pending.

This program is optimized for three goals:

1. Reduce refactor risk while the desk keeps trading.
2. Improve scalability, observability, and research velocity.
3. Preserve the current strategy edge and operational controls.

## Principles

- Preserve behavior before improving structure.
- Freeze data contracts before changing module boundaries.
- Prefer wrappers and adapters before rewrites.
- Dual-write and compare before migrating operational state.
- Every phase must leave the system shippable.

## Current Constraints

- Bots have import-time side effects and boot global runtime state.
- `kalshi_auth.py` is a shared god module.
- Many scripts and bots coordinate through ad hoc JSON files in `data/`.
- Analytics are strong, but artifact registries are not fully canonical.
- The repo is not packaged as an installable Python application.

## North Star

Target architecture:

- `apps/`: thin bot entrypoints and CLI wrappers
- `domain/`: strategy logic, models, sizing, parsers
- `execution/`: broker, order lifecycle, reconciliation, fills
- `risk/`: allocator, limits, correlation, kill switch, circuit breakers
- `storage/`: trade store, event ledger, state store, snapshots
- `ops/`: health, supervisor interfaces, notifications, dashboard backends
- `research/`: attribution, backtests, calibration, experiment registry

Target operating model:

- One canonical event ledger
- One canonical trade/decision schema
- Strategies emit intents, not broker calls
- Research reads the same canonical records that production writes
- Every model and config change is versioned and attributable

## Phase 0: Freeze Contracts

Objective: define what must not break.

Status: complete on 2026-03-14.

Deliverables:

- Canonical bot registry:
  - `bot_id`
  - display name
  - trade log path
  - decision log path
  - health key
  - supervisor key
- Canonical schemas:
  - golden trade record
  - decision record
  - verification record
  - allocator decision record
  - health summary record
- State inventory of every persisted JSON file in `data/`
- Compatibility matrix showing which scripts read which files

Required code moves:

- Promote `trade_files.py` into a more complete registry module
- Add a schema/version field to mutable state files that lack one
- Add tests asserting canonical field presence for trade and decision records

Acceptance gate:

- Existing bots run unchanged
- Existing dashboard and analytics pass against the same artifacts
- A single document lists all persisted contracts and owners

Estimated size:

- 3 to 5 PRs
- 3 to 4 days

## Phase 1: Package The Repo Without Changing Behavior

Objective: eliminate path hacks and import-name hazards.

Status: complete on 2026-03-14.

Deliverables:

- `pyproject.toml`
- importable package layout rooted at `src/kalshi`
- stable module names for hyphenated bot files via wrapper scripts
- CLI entrypoints for bots and operational scripts

Required code moves:

- Keep existing files like `src/kalshi/weather-bot.py` as wrappers only
- Move real code into importable modules such as:
  - `src/kalshi/apps/weather_bot.py`
  - `src/kalshi/apps/crypto_bot.py`
  - `src/kalshi/apps/source_monitor.py`
- Replace dynamic import workarounds in scripts where practical
- Reduce `sys.path.insert(...)` usage to wrappers and tests only

Acceptance gate:

- `npm run weather`, `npm run crypto`, `npm run supervisor`, and dashboard still work
- Tests no longer require most hyphen-file import tricks for newly migrated modules
- Runtime behavior is unchanged

Estimated size:

- 4 to 6 PRs
- 1 week

## Phase 2: Remove Import-Time Runtime Construction

Objective: make bots import-safe and testable.

Status: complete on 2026-03-14.

Deliverables:

- Explicit `main()` and `build_app()` pattern for each bot
- Per-bot `AppContext` or dependency bundle
- Config loading performed in bootstrap, not at module import

Required code moves:

- Replace module globals like:
  - `client = KalshiClient()`
  - `allocator = PortfolioAllocator(...)`
  - `trade_manager = TradeManager(...)`
- Move those into factories:
  - `load_config()`
  - `build_runtime(config)`
  - `run_once(runtime)`
  - `run_forever(runtime)`

Priority order:

1. `weather-bot.py`
2. `crypto-bot.py`
3. `economics-bot.py`
4. `source-monitor.py`
5. `position-monitor.py`
6. `strategy-trader.py`
7. `market-maker.py`
8. entertainment and ancillary bots

Acceptance gate:

- Bot modules can be imported without network calls, file writes, or signal registration
- Existing tests become simpler, with fewer stubbed module imports
- Supervisor still launches wrappers unchanged

Estimated size:

- 6 to 10 PRs
- 2 weeks

## Phase 3: Introduce Storage Interfaces

Objective: stop coupling business logic to direct JSON file access.

Status: complete on 2026-03-14.

Deliverables:

- `TradeStore`
- `DecisionStore`
- `StateStore`
- `SnapshotStore`
- `MetricsStore`

Phase 3 rule:

- These interfaces initially still use the current JSON files.
- No storage migration yet.

Required code moves:

- Replace direct `read_text()/write_text()/json.loads()` calls in core runtime paths with store methods
- Start with:
  - `TradeManager`
  - `ForecastVerifier`
  - `PortfolioAllocator`
  - dashboard health loaders
  - reconciliation pipeline

Acceptance gate:

- Current files remain the operational source of truth
- The same test suite passes with store-backed adapters
- File format compatibility is preserved

Estimated size:

- 5 to 8 PRs
- 1 to 2 weeks

## Phase 4: Add A Canonical Event Ledger

Objective: create one source of truth for research, audit, and ops.

Status: implementation complete on 2026-03-14. The 2-week parity observation window starts after rollout.

Recommended first implementation:

- SQLite for production write path
- DuckDB or Parquet export for research and analysis

Canonical event types:

- `source_observation`
- `market_snapshot`
- `forecast_snapshot`
- `trade_decision`
- `budget_decision`
- `order_submitted`
- `order_update`
- `fill`
- `position_snapshot`
- `settlement`
- `verification_result`
- `post_trade_attribution`

Migration strategy:

- Dual-write from stores to:
  - existing JSON artifacts
  - new ledger tables
- Add parity jobs comparing counts and hashes between legacy and ledger views
- Do not cut over analytics until parity is stable

Acceptance gate:

- Daily parity report is clean for 2 weeks
- Dashboard can read either source behind a feature flag
- Reconciliation and attribution run off the ledger with identical outputs

Estimated size:

- 6 to 10 PRs
- 2 to 3 weeks

## Phase 5: Split `kalshi_auth.py`

Objective: reduce blast radius in the shared runtime.

Target split:

- `infra/kalshi_client.py`
- `execution/trade_manager.py`
- `execution/order_monitor.py`
- `risk/circuit_breaker.py`
- `risk/kill_switch.py`
- `ops/logging.py`
- `ops/health_monitor.py`
- `ops/notifications.py`
- `storage/json_atomic.py`

Current bridge state:

- Slice 1 is implemented in `storage.py` first, because the repo already ships a top-level `storage.py` module
- The later package split to `storage/json_atomic.py` should happen only after the existing `storage.py` module is broken into a package without changing public behavior
- Slice 2 extracts runtime logging and signal installation into `ops/logging.py`, with `kalshi_auth.py` preserving the existing public imports
- Slice 3 extracts kill-switch and circuit-breaker helpers into `risk/`, with `kalshi_auth.py` preserving the existing public imports
- Slice 4 extracts `OrderMonitor` into `execution/order_monitor.py`, with `kalshi_auth.py` preserving the existing public imports
- Slice 5 extracts `TradeManager`, `RecentTradeTracker`, and trade-log helpers into `execution/trade_manager.py`, with `kalshi_auth.py` preserving the existing public imports
- Slice 6 extracts `HealthCheckMonitor` and the bot/source health contract into `ops/health_monitor.py`, with `kalshi_auth.py` preserving the existing public imports
- Slice 7 extracts notification helpers into `ops/notifications.py`, with `kalshi_auth.py` preserving the existing public imports
- Slice 8 extracts `KalshiClient`, market normalization, and market-cache helpers into `infra/kalshi_client.py`, with `kalshi_auth.py` preserving the existing public imports

Rules:

- Keep compatibility imports during the transition
- Move one concern at a time
- Do not rename public APIs and change behavior in the same PR

Suggested order:

1. atomic JSON helpers
2. logging and signal helpers
3. circuit breaker and kill switch
4. order monitor
5. trade manager
6. health monitor
7. notifications
8. client last

Acceptance gate:

- `kalshi_auth.py` becomes a compatibility shim
- Tests target the extracted modules directly
- No bot imports need to change all at once

Estimated size:

- 8 to 12 PRs
- 2 weeks

## Phase 6: Split Domain Logic By Strategy Family

Objective: make model changes local and auditable.

Target modules:

- `domain/weather/`
- `domain/crypto/`
- `domain/economics/`
- `domain/entertainment/`
- `domain/longshot/`
- `domain/shared/` for shared math only

Key actions:

- Break up `probability.py` by market family
- Move per-strategy parsers and model inputs next to their domains
- Keep one shared sizing layer, but version model families independently

Current bridge state:

- Slice 1 extracts shared fee, sizing, and liquidity helpers into `domain/shared/sizing.py`
- Slice 2 extracts the weather probability family into `domain/weather/models.py`
- Slice 3 extracts the crypto pricing models into `domain/crypto/models.py`
- Slice 4 extracts the economics nowcast and sigma helpers into `domain/economics/models.py`
- Slice 5 extracts entertainment info-arb and data-sigma helpers into `domain/entertainment/models.py`
- `probability.py` remains the compatibility import surface while strategy-specific models stay in place

Acceptance gate:

- Every production trade record includes:
  - `strategy_id`
  - `model_name`
  - `model_version`
  - `config_version`
  - `feature_snapshot_id` or inline model inputs

Estimated size:

- 6 to 10 PRs
- 2 weeks

## Phase 7: Research And Learning Loop Upgrade

Objective: make the desk smarter from every trade and every missed trade.

Deliverables:

- model registry
- experiment registry
- post-trade attribution job
- missed-trade analysis
- source scorecards
- promotion workflow: research -> shadow -> capped live

Daily outputs:

- P&L explain by strategy and market type
- forecast error by model/city/horizon
- fill quality and slippage by bot and order type
- binding constraint analysis from allocator and daily limits
- top 10 winners, top 10 losers, top 10 missed trades
- source freshness and parser failure report

Required artifacts:

- `model_registry`
- `strategy_config_registry`
- `experiment_runs`
- `source_catalog`
- `trade_attribution`
- `opportunity_log`

Acceptance gate:

- Every config/model change can be linked to later P&L and calibration outcomes
- Operators can answer:
  - why did we trade?
  - why did we not trade?
  - what changed?
  - what got worse?
  - what source added value?

Estimated size:

- 4 to 8 PRs
- 2 weeks

## Phase 8: Desk Operations And Governance

Objective: run the system like a professional small quant desk.

Deliverables:

- service ownership per bot and shared module
- runbooks for each bot and each critical data source
- change-management policy for model/config promotions
- incident review template
- weekly model review and monthly strategy review

Required operating cadences:

- Daily:
  - reconciliation
  - attribution
  - source freshness
  - stale bot/process review
- Weekly:
  - calibration drift review
  - source scorecard review
  - execution quality review
- Monthly:
  - strategy capital allocation review
  - retired-source and retired-model cleanup

Acceptance gate:

- dashboard surfaces operator actions, not only raw state
- runbooks exist for top failure modes
- postmortems are linked to follow-up code or config changes

Estimated size:

- ongoing

## First 12 PRs

Recommended first sequence:

1. Add canonical bot registry and contract inventory doc.
2. Add schema/version fields to mutable state artifacts.
3. Add `pyproject.toml` and installable package config.
4. Create wrapper-based app module for weather bot.
5. Create wrapper-based app module for crypto bot.
6. Introduce `build_runtime()` for weather bot.
7. Introduce `build_runtime()` for crypto bot.
8. Add `TradeStore` and migrate `TradeManager`.
9. Add `StateStore` and migrate `ForecastVerifier`.
10. Add `StateStore` and migrate `PortfolioAllocator`.
11. Add ledger dual-write for trade decisions and orders.
12. Add parity report for legacy files vs ledger views.

## Delivery Rules

- Maximum scope: one boundary change per PR.
- Every PR must include:
  - migration note
  - rollback note
  - explicit invariants preserved
  - focused tests
- Never combine:
  - file format migration
  - module rename
  - behavior change
  in the same PR.

## Success Metrics

Engineering:

- module imports without side effects
- reduced `sys.path.insert(...)` count
- reduced direct JSON file access in runtime code
- reduced size of `kalshi_auth.py`, `probability.py`, and bot entrypoint files

Operational:

- faster root-cause time during incidents
- fewer dashboard/supervisor code-path discrepancies
- stable parity between legacy artifacts and ledger outputs

Research:

- all trades and skipped opportunities attributable
- model/version level P&L and calibration available by query
- faster source onboarding with measurable shadow performance

## What Not To Do

- Do not rewrite all bots into a framework first.
- Do not migrate all storage at once.
- Do not split `kalshi_auth.py` before storage and bootstrap seams exist.
- Do not change trade schemas casually once Phase 0 is frozen.
- Do not force all scripts onto the new ledger until parity is proven.

## Recommended Immediate Start

Start with Phase 0 and Phase 1 only:

1. freeze contracts
2. add package metadata
3. move one bot to wrapper + import-safe bootstrap

The weather bot is the best first candidate because it touches the widest set of current concerns:

- forecasting
- verification
- allocation
- dashboard health
- decision logging
- calibration inputs

If weather can be made import-safe and store-backed without changing behavior, the rest of the refactor becomes much lower risk.
