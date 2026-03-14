# Contract Inventory

Generated: 2026-03-14
Status: Phase 0 baseline

This document records the current persisted contracts that multiple bots,
analytics jobs, and operator tools depend on.

## Canonical Registry

- Bot/process registry: [bot_registry.py](/Users/andeslee/Documents/cursor-projects/kalshi-trading/src/kalshi/bot_registry.py#L1)
- Canonical trade log registry: [trade_files.py](/Users/andeslee/Documents/cursor-projects/kalshi-trading/src/kalshi/trade_files.py#L1)
- Trade record schema: [trade-record-schema.md](/Users/andeslee/Documents/cursor-projects/kalshi-trading/docs/trade-record-schema.md#L1)

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
