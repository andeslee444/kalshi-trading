# Promotion Rollback

## When To Use

Use this runbook when an experiment in `shadow`, `capped_live`, or `live` must be moved back because of performance, source, or runtime regressions.

## Signals

- attribution or missed-trade review deteriorates after a promotion
- source failures make the promoted experiment unsafe to keep live
- a production incident is traced to a recent model or config promotion

## Immediate Actions

1. Inspect the current experiment record:

```bash
python3 scripts/promotion-workflow.py show <experiment_id>
```

2. Roll the promotion stage back explicitly:

```bash
python3 scripts/promotion-workflow.py rollback <experiment_id> --to research --actor <name> --reason "performance regression"
```

3. If the promotion also changed live calibration or config, record a separate config rollback and link it to the same incident.

## Escalation

- Do not edit `data/experiment-runs.json` by hand.
- If `config/calibration.json` was changed, review `config/calibration-backup.json` before restoring anything.
- If the rollback happened after live trading, open an incident and attach the relevant attribution and source-scorecard outputs.

## Exit Criteria

- `scripts/promotion-workflow.py show <experiment_id>` reflects the rollback stage
- rollback reason and actor are present in the experiment history
- any linked config rollback is recorded in the follow-up notes
