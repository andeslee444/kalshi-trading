# Incident Template

Use this template for production incidents, failed promotions, severe source outages, or parity regressions.

## Summary

- Incident id:
- Start time:
- End time:
- Severity:
- Owner:

## Impact

- User or trading impact:
- Affected services:
- Affected sources:
- Estimated P&L or opportunity impact:

## Detection

- How it was first detected:
- Which alert, dashboard panel, or report showed it:
- Time to detection:

## Timeline

- `HH:MM` detection
- `HH:MM` first mitigation
- `HH:MM` stable recovery

## Root Cause

- Technical cause:
- Why the current controls did not stop it earlier:
- Whether this was a promotion, source, runtime, or ops failure:

## Artifacts Reviewed

- `data/incident-reviews.json`
- `data/experiment-runs.json`
- `data/attribution-report.json`
- `data/source-catalog.json`
- `data/ledger-parity-report.json`
- relevant bot logs in `data/logs/`

## Canonical Commands

```bash
python3 scripts/incident-workflow.py open <incident_id> --summary "<summary>" --severity <severity> --owner <name>
python3 scripts/incident-workflow.py link <incident_id> --pr-number <pr_number> --config-version <config_version> --experiment-id <experiment_id> --note "follow-up change"
python3 scripts/incident-workflow.py close <incident_id> --actor <name> --resolution "<resolution>"
```

## Mitigation

- Immediate mitigation taken:
- Rollback or disable action taken:
- Residual risk after mitigation:

## Follow-Up Actions

- Code change:
- Config or promotion workflow change:
- Runbook update:
- Owner and due date:

## Links

- PR:
- Incident ticket:
- Promotion record:
- Experiment id:
- Config version:
- Post-incident review:
