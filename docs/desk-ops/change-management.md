# Change Management

This policy governs model, config, and promotion changes that can affect live trading.

## Scope

Use this workflow for:

- calibration suggestions and applications
- model promotions between `research`, `shadow`, `capped_live`, and `live`
- risk-limit or allocator changes that alter live exposure

Do not update `data/experiment-runs.json` by hand. Use the promotion workflow so the lifecycle remains auditable.
Do not track incident follow-up links only in a free-form doc. Use `data/incident-reviews.json` through the incident workflow so PRs, config rollbacks, and experiment ids stay queryable.

## Required Evidence Before Promotion

### Research -> Shadow

- a stable `experiment_id` exists in `data/experiment-runs.json`
- the related suggestion, model, or config artifact is saved
- expected affected services are identified

### Shadow -> Capped Live

- no unresolved blocker in the shadow window
- attribution and missed-trade review show the change is directionally acceptable
- owner signs off on limited live exposure

### Capped Live -> Live

- capped-live results are stable enough to remove the cap
- no open parity, source freshness, or execution quality incident tied to the experiment
- rollback path is documented before promotion

## Canonical Commands

Inspect an experiment:

```bash
python3 scripts/promotion-workflow.py show <experiment_id>
python3 scripts/incident-workflow.py show <incident_id>
```

Advance one stage:

```bash
python3 scripts/promotion-workflow.py promote <experiment_id> --to shadow --actor <name> --note "start shadow rollout"
python3 scripts/promotion-workflow.py promote <experiment_id> --to capped_live --actor <name> --note "limit live exposure"
python3 scripts/promotion-workflow.py promote <experiment_id> --to live --actor <name> --note "remove cap after stable window"
```

Rollback a stage:

```bash
python3 scripts/promotion-workflow.py rollback <experiment_id> --to research --actor <name> --reason "performance regression"
python3 scripts/incident-workflow.py link <incident_id> --pr-number <pr_number> --config-version <config_version> --experiment-id <experiment_id> --note "rollback follow-up"
```

Apply a calibration suggestion:

```bash
python3 scripts/calibration-pipeline.py --apply-suggestion data/calibration-suggestions/<file>.json
```

## Rollback Rules

- Stage rollback and config rollback are separate actions. Record both when both happen.
- If a live calibration was already applied, review `config/calibration-backup.json` and restore the live config through an auditable change, not an undocumented local edit.
- Every rollback must include a reason, actor, and linked incident or review note.
- When a promotion or rollback is driven by an incident, update the canonical incident record with the PR, config version, and experiment id before closing it.

## Change Record Minimum

Every promotion or rollback record should answer:

- what changed
- why now
- what evidence was reviewed
- what could regress
- how to reverse it

Use the incident template when the rollback is triggered by a production issue.
