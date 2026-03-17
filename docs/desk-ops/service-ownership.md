# Service Ownership

This matrix maps the current runtime surface to functional owners, operator entrypoints, and the artifacts that matter first during an incident.

## Services

| Service | Owner Role | Runtime Entry | First Checks | Canonical Artifacts |
| --- | --- | --- | --- | --- |
| Weather bot | Weather strategy owner | `python3 src/kalshi/weather-bot.py` | `python3 scripts/supervisor.py status`, `python3 scripts/daily-attribution.py --save` | `data/kalshi-trades.json`, `data/weather-verification.json`, `data/weather-nws-cross-check.json` |
| Crypto bot | Crypto strategy owner | `python3 src/kalshi/crypto-bot.py` | `python3 scripts/supervisor.py status`, `python3 scripts/analyze-performance.py --reconcile` | `data/kalshi-crypto-trades.json`, `data/regime-state.json`, `data/edge-monitor-state.json` |
| Economics bot | Macro strategy owner | `python3 src/kalshi/economics-bot.py` | `python3 scripts/supervisor.py status`, `python3 scripts/daily-attribution.py --save` | `data/kalshi-economics-trades.json`, `data/allocator-state.json` |
| Entertainment bot | Entertainment strategy owner | `python3 src/kalshi/entertainment-bot.py` | `python3 scripts/supervisor.py status`, `python3 scripts/analyze-performance.py --reconcile` | `data/kalshi-entertainment-trades.json`, `data/opportunity-log.json` |
| Source Monitor (`monitor` / `source-monitor`) | Info-arb source owner | `python3 src/kalshi/source-monitor.py` | `python3 scripts/supervisor.py status`, `python3 scripts/source-scorecard.py` | `data/kalshi-monitor-trades.json`, `data/health-state.json`, `data/source-catalog.json` |
| Position Monitor (`positions` / `position-monitor`) | Execution and risk owner | `python3 src/kalshi/position-monitor.py` | `python3 scripts/supervisor.py status`, review trailing state | `data/kalshi-position-trades.json`, `data/trailing-state.json` |
| Supervisor | Platform ops owner | `python3 scripts/supervisor.py run` | `python3 scripts/supervisor.py status` | `data/pids/supervisor-state.json`, `data/health-state.json`, `data/logs/supervisor.log` |
| Dashboard | Platform ops owner | `python3 scripts/dashboard.py` | dashboard health endpoint, `python3 scripts/source-scorecard.py`, `python3 scripts/ledger-parity-report.py` | `data/health-state.json`, `data/financial-snapshot.json`, `data/source-catalog.json` |
| Calibration pipeline | Research and model governance owner | `python3 scripts/calibration-pipeline.py` | `python3 scripts/calibration-pipeline.py --dry-run` | `data/calibration-suggestions/*`, `data/experiment-runs.json`, `config/calibration.json` |
| Event ledger and research artifacts | Platform ops owner with research support | `python3 scripts/ledger-parity-report.py` | parity report, attribution save path, source scorecard | `data/event-ledger.sqlite3`, `data/opportunity-log.json`, `data/attribution-report.json`, `data/experiment-runs.json` |

## Critical Sources

| Source | Primary Owner Role | Dependent Services | First Report |
| --- | --- | --- | --- |
| `open-meteo-batch` / `open-meteo-single` / `open-meteo-ensemble` | Weather strategy owner | weather | `python3 scripts/source-scorecard.py` |
| `nws-forecast` / `nws` | Weather strategy owner and info-arb source owner | weather, source-monitor | `python3 scripts/source-scorecard.py` |
| `coinbase` / `deribit` | Crypto strategy owner | crypto | `python3 scripts/source-scorecard.py` |
| `cleveland-fed` / `gdpnow` / `cme-fedwatch` | Macro strategy owner | economics | `python3 scripts/source-scorecard.py` |
| `hdd` / `boxoffice` | Entertainment strategy owner and info-arb source owner | entertainment, source-monitor | `python3 scripts/source-scorecard.py` |
| `beatrelease` | Entertainment strategy owner | beatrelease | `python3 scripts/source-scorecard.py` |

## Ownership Rules

- Every live service must have one functional owner even if the same person holds multiple roles.
- Shared control-plane processes default to the platform ops owner unless a later Phase 8 slice assigns them elsewhere.
- Source ownership follows the trading strategy that consumes the source first, with platform ops supporting transport and persistence failures.
- Promotion decisions belong to the research and model governance owner, but production rollouts still require the owning strategy owner to sign off.
