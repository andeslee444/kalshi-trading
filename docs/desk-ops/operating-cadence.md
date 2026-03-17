# Operating Cadence

This document turns the Phase 8 operating cadence into an executable checklist.

## Daily Checklist

### 1. Reconciliation

Command:

```bash
python3 scripts/reconcile-trades.py
```

Review:

- settlement annotations landed on the expected trade logs
- no obvious orphan or duplicate trade records were introduced

Artifacts:

- `data/*trades.json`
- `data/event-ledger.sqlite3`

Exit criteria:

- reconciliation completes without opening a new incident
- any anomalies are linked to an incident, postmortem draft, or follow-up ticket

### 2. Attribution

Command:

```bash
python3 scripts/daily-attribution.py --save
```

Review:

- total settled trade count looks plausible
- by-bot and by-market-type P&L explain the day
- the saved attribution artifact is updated

Artifacts:

- `data/attribution-report.json`
- `data/event-ledger.sqlite3`

Exit criteria:

- attribution snapshot exists for the day
- unexplained P&L or obviously missing settled trades are escalated

### 3. Source Freshness

Commands:

```bash
python3 scripts/source-scorecard.py
python3 scripts/weather-verification-summary.py --lookback-days 7 30
```

Review:

- no critical source is stuck in `error`
- recent weather actual-source mix still looks plausible
- parser failures have an owner and next action

Artifacts:

- `data/source-catalog.json`
- `data/health-state.json`
- `data/weather-verification.json`

Exit criteria:

- any `warning` or `error` source has a documented owner action
- no silent source failure is left for the next trading session

### 4. Stale Bot And Process Review

Commands:

```bash
python3 scripts/supervisor.py status
python3 scripts/check-sync-health.py
```

Review:

- no bot is stale, crash-looping, or silently disabled
- sync and snapshot health still look sane after the latest run

Artifacts:

- `data/pids/supervisor-state.json`
- `data/health-state.json`
- `data/logs/supervisor.log`

Exit criteria:

- supervisor and dashboard health are directionally consistent
- any restart loop or stale heartbeat has an owner action or incident

## Weekly Checklist

### 1. Calibration Drift Review

Commands:

```bash
python3 scripts/calibration-pipeline.py --dry-run
python3 scripts/promotion-workflow.py show <experiment_id>
```

Review:

- new drift signals are understood
- open experiments have a clear current stage and next decision
- no auto-apply or manual apply happened without an auditable trail

Artifacts:

- `data/experiment-runs.json`
- `data/calibration-suggestions/*`
- `config/calibration.json`

Exit criteria:

- each active experiment is either advanced, held, or rolled back with a recorded reason

### 2. Source Scorecard Review

Commands:

```bash
python3 scripts/source-scorecard.py --json
python3 scripts/missed-trade-report.py --limit 20
```

Review:

- stale or noisy sources are still worth keeping live
- missed trades caused by source issues are visible, not anecdotal

Artifacts:

- `data/source-catalog.json`
- `data/opportunity-log.json`

Exit criteria:

- weak sources have a remediation plan, disable decision, or cleanup note

### 3. Execution Quality Review

Commands:

```bash
python3 scripts/analyze-performance.py --reconcile
python3 scripts/daily-report.py --with-backtest
```

Review:

- realized P&L, win rate, and fill quality are consistent with expectations
- no bot is degrading silently while aggregate P&L still looks acceptable

Artifacts:

- `data/financial-snapshot.json`
- `data/backtest-results.json`
- `data/*trades.json`

Exit criteria:

- outlier execution quality is linked to a concrete follow-up

## Monthly Checklist

### 1. Strategy Capital Allocation Review

Commands:

```bash
python3 scripts/daily-attribution.py --save
python3 scripts/analyze-performance.py --reconcile
python3 scripts/check-sync-health.py
```

Review:

- allocator behavior is consistent with realized returns and observed constraints
- strategy capital still matches the edge and source environment

Artifacts:

- `data/allocator-state.json`
- `data/attribution-report.json`
- `data/financial-snapshot.json`

Exit criteria:

- any capital reallocation has an explicit decision record or follow-up issue

### 2. Retired Source And Model Cleanup

Commands:

```bash
python3 scripts/source-scorecard.py --json
python3 scripts/promotion-workflow.py show <experiment_id>
```

Review:

- retired or permanently degraded sources are identified
- inactive experiment records and stale promotions are understood before cleanup

Artifacts:

- `data/source-catalog.json`
- `data/experiment-runs.json`
- `data/model-registry.json`

Exit criteria:

- cleanup candidates are documented before removal
- no active live experiment is treated as stale by mistake

## Recording Outcomes

- Daily anomalies go into the incident template if they affect live trading or data integrity.
- Weekly promotion decisions should reference the corresponding `experiment_id`.
- Monthly allocation or retirement decisions should link back to the reports reviewed that month.
