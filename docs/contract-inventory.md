# Contract Inventory

Generated: 2026-03-22
Status: Phase 0 frozen; Phase 4 weather shadow-bundle guidance added

This document records the current persisted contracts that multiple bots,
analytics jobs, and operator tools depend on.

## Canonical Registry

- Bot/process registry: [bot_registry.py](/Users/andeslee/Documents/cursor-projects/kalshi-trading/src/kalshi/bot_registry.py#L1)
- Canonical trade log registry: [trade_files.py](/Users/andeslee/Documents/cursor-projects/kalshi-trading/src/kalshi/trade_files.py#L1)
- Trade record schema: [trade-record-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/trade-record-schema.md#L1)
- Decision record schema: [decision-record-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/decision-record-schema.md#L1)
- Verification record schema: [verification-record-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/verification-record-schema.md#L1)
- Allocator decision schema: [allocator-decision-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/allocator-decision-schema.md#L1)
- Health summary schema: [health-summary-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/health-summary-schema.md#L1)
- Shared state schema helpers: [artifact_contracts.py](/Users/andeslee/Documents/cursor-projects/kalshi-trading/src/kalshi/artifact_contracts.py#L1)

## Trade And Decision Artifacts

| Bot ID | Runtime Alias | Trade Log | Decision Log |
|--------|---------------|-----------|--------------|
| `weather` | `weather` | `data/kalshi-trades.json` | `data/kalshi-trades-decisions.json` |
| `entertainment` | `entertainment` | `data/kalshi-entertainment-trades.json` | `data/kalshi-entertainment-trades-decisions.json` |
| `crypto` | `crypto` | `data/kalshi-crypto-trades.json` | `data/kalshi-crypto-trades-decisions.json` |
| `economics` | `economics` | `data/kalshi-economics-trades.json` | `data/kalshi-economics-trades-decisions.json` |
| `positions` | `position-monitor` | `data/kalshi-position-trades.json` | `data/kalshi-position-trades-decisions.json` |
| `monitor` | `source-monitor` | `data/kalshi-monitor-trades.json` | `data/kalshi-monitor-trades-decisions.json` |
| `strategy` | `strategy` | `data/kalshi-strategy-trades.json` | `data/kalshi-strategy-trades-decisions.json` |
| `arb` | `cross-platform-arb` | `data/kalshi-arb-trades.json` | `data/kalshi-arb-trades-decisions.json` |
| `mm` | `market-maker` | `data/kalshi-mm-trades.json` | `data/kalshi-mm-trades-decisions.json` |
| `beatrelease` | `beatrelease` | `data/beatrelease-trades.json` | `data/beatrelease-trades-decisions.json` |

Notes:

- Decision log filenames are derived from the trade log stem by `TradeManager.log_decision()`.
- Control-plane bot ids and runtime aliases are intentionally recorded separately because several systems use different names today.

## Shared Operational State

| Artifact | Current Writers | Current Readers | Purpose |
|----------|-----------------|-----------------|---------|
| `data/health-state.json` | bots via `HealthCheckMonitor` | dashboard, supervisor, operator scripts | heartbeats, source failures, health status |
| `data/allocator-state.json` | `PortfolioAllocator` | bots, dashboard | cross-bot exposure and budget coordination |
| `data/weather-verification.json` | weather bot / `ForecastVerifier` | dashboard, audits, calibration | forecast verification and source mix |
| `data/weather-nws-cross-check.json` | weather bot / `NWSCrossCheckVerifier` | dashboard, weather audit scripts | Open-Meteo vs NWS comparison history |
| `data/financial-snapshot.json` | `scripts/pnl-snapshot.py` | sync validation, reports | portfolio snapshot for audit/sync |
| `data/pids/supervisor-state.json` | supervisor | dashboard | restart counts and process metadata |
| `data/regime-state.json` | `PortfolioAllocator`, crypto bot | dashboard, daily attribution | regime detector beliefs |
| `data/correlation-state.json` | `CorrelationEngine` via allocator | dashboard, allocator | cluster risk state |
| `data/edge-monitor-state.json` | `EdgeMonitor` | allocator, daily attribution, dashboard | strategy edge durability weights |
| `data/trailing-state.json` | position monitor | dashboard, position monitor | exit trailing stops and peak tracking |
| `data/scan-summaries.json` | bots via `ScanSummary` | S3 sync, audits | per-scan summaries and zero-trade diagnostics |
| `data/circuit-breaker-state.json` | `CircuitBreaker` | S3 sync, operator tools | broker/source breaker persistence |
| `data/market-cache.json` | `KalshiClient.fetch_markets_cached()` | all trading bots | shared market data cache |
| `data/deposits.json` | manual/operator | allocator, pnl snapshot | external funding ledger |

State metadata convention:

- Shared mutable state files should include `artifact_type` and `schema_version` at the top level.
- Phase 0 schema version for the current shared-state files is `1`.

## Compatibility Matrix

| Artifact | Owner | Known Readers |
|----------|-------|---------------|
| trade logs (`data/*trades.json`) | individual bots / `TradeManager` | `scripts/pnl-snapshot.py`, `scripts/analyze-performance.py`, `scripts/daily-attribution.py`, `scripts/backtest.py`, `scripts/audit.py`, `scripts/weather-execution-audit.py` |
| decision logs (`data/*-decisions.json`) | individual bots / `TradeManager.log_decision()` | `scripts/skip-audit.py`, `src/kalshi/position-monitor.py`, operator investigations |
| `data/health-state.json` | `HealthCheckMonitor` | `scripts/dashboard.py`, `scripts/supervisor.py`, `src/kalshi/strategy_engine.py`, `scripts/check-sync-health.py` |
| `data/allocator-state.json` | `PortfolioAllocator` | trading bots via allocator, `scripts/dashboard.py` |
| `data/weather-verification.json` | `ForecastVerifier` | `scripts/dashboard.py`, `scripts/weather-city-audit.py`, `scripts/weather-verification-summary.py`, `scripts/backfill-weather-verification-sources.py`, `scripts/supervisor.py` |
| `data/weather-nws-cross-check.json` | `NWSCrossCheckVerifier` | `scripts/dashboard.py`, `scripts/weather-nws-crosscheck-audit.py` |
| `data/financial-snapshot.json` | `scripts/pnl-snapshot.py` | `scripts/dashboard.py`, `scripts/validate-sync-data.py`, `scripts/check-sync-health.py`, `scripts/daily-imessage-report.py` |
| `data/pids/supervisor-state.json` | `scripts/supervisor.py` | `scripts/dashboard.py` |
| `data/regime-state.json` | allocator / crypto bot | `scripts/dashboard.py`, `scripts/daily-attribution.py` |
| `data/correlation-state.json` | allocator / correlation engine | `scripts/dashboard.py`, allocator |
| `data/weather-observation-pack.json` | `scripts/weather-observation-pack.py` | operator/manual review only | derived non-canonical weather observation summary; top-level copy only when explicitly requested |
| `data/shadow/weather-refresh/*` | `scripts/weather-shadow-refresh.py` | operator/manual review only | preferred Phase 4 shadow-only weather observation, audit, backtest, calibration, shadow-prior, and promotion bundle |
| `data/weather-promotion-candidates.json` | `scripts/weather-promotion-candidates.py` | operator/manual review only | derived non-canonical weather promotion ranking; top-level copy only when explicitly requested |

## Additional Persisted Artifacts In Scope

These artifacts are not yet fully schema-frozen, but they are part of the
Phase 0 inventory and must be accounted for before later storage migration:

| Category | Artifacts |
|----------|-----------|
| research and analytics | `data/backtest-results.json`, `data/performance-metrics.json`, `data/attribution-report.json`, `data/calibration-baselines.json`, `data/calibration-history/*`, `data/calibration-suggestions/*` |
| bot-local state | `data/strategy-metrics.json`, `data/strategy-last-run.json`, `data/kalshi-strategy-performance.json`, `data/position-monitor-metrics.json`, `data/source-monitor-metrics.json`, `data/beatrelease-state.json`, `data/beatrelease-llm-log.json`, `data/arb-spread-log.json` |
| source and market caches | `data/econ-nowcast-cache.json`, `data/macro-cache.json`, `data/nowcast-history.json`, `data/crypto-invalid-markets.json`, `data/crypto-price-history.json`, `data/all-open-markets.json`, `data/hdd-articles.json` |
| sync and ops metadata | `data/.sync-manifest.json`, `data/.sync-history.jsonl`, `data/.sync-sizes.json`, `data/pids/*.pid`, `data/pids/*.lock`, `data/logs/*.log` |

## Bot Control Contracts

| Contract | Purpose |
|----------|---------|
| `data/HALT_TRADING` | global kill switch |
| `data/HALT_<bot>` | per-bot halt via `per_bot_halt_path()` |
| `data/pids/*.pid` | process identity for supervisor/dashboard |
| `data/pids/*.lock` | singleton process locks for long-running bots |
| `data/logs/*.log` | operator log surface and duplicate-launch detection |

## Phase 0 Rules

- Any new persisted artifact should have a clear owner and reader list.
- New multi-reader artifacts should prefer canonical registries over local hardcoded path lists.
- Control-plane bot ids must remain stable even if runtime aliases or script paths change.
