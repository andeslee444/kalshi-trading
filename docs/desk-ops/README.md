# Desk Operations

This folder is the Phase 8 operating baseline for the trading system.

It is intentionally practical:

- service ownership is recorded against the actual bot and process ids in the codebase
- operator commands point at the current scripts and artifacts
- promotion and rollback steps use the canonical `experiment_runs` workflow
- incidents and postmortems use the canonical `incident_reviews` workflow

Until named humans are assigned, ownership is by functional role instead of person.

## Index

- [service-ownership.md](./service-ownership.md)
- [operating-cadence.md](./operating-cadence.md)
- [change-management.md](./change-management.md)
- [incident-template.md](./incident-template.md)
- [runbooks/README.md](./runbooks/README.md)

## Cadence

### Daily

- Reconciliation: `python3 scripts/reconcile-trades.py`
- Attribution: `python3 scripts/daily-attribution.py --save`
- Source freshness: `python3 scripts/source-scorecard.py`
- Stale bot/process review: `python3 scripts/supervisor.py status`

### Weekly

- Calibration drift review: `python3 scripts/calibration-pipeline.py --dry-run`
- Source scorecard review: `python3 scripts/source-scorecard.py --json`
- Execution quality review: `python3 scripts/analyze-performance.py --reconcile`

### Monthly

- Strategy capital allocation review: review allocator state, attribution, and execution quality together
- Retired source and retired model cleanup: prune stale entries in `experiment-runs.json`, `model-registry.json`, and disabled source configs after the postmortem window is closed

## Scope Of This Slice

This slice establishes:

- the operating cadence checklists
- the ownership matrix
- the initial change-management policy
- the incident review template
- runbooks for the top current failure modes

Later Phase 8 slices should build on this with dashboard operator actions and deeper review templates.
