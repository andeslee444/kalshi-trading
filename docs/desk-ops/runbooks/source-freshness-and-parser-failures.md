# Source Freshness And Parser Failures

## When To Use

Use this runbook when a critical source is stale, erroring, or producing parser failures.

## Signals

- `python3 scripts/source-scorecard.py` shows `warning` or `error`
- `data/health-state.json` has rising `error_count` or a populated `last_error_message`
- a bot stops trading because upstream source data is stale

## Immediate Actions

1. Generate the canonical source view:

```bash
python3 scripts/source-scorecard.py
```

2. Confirm whether the failure is transport, parsing, or source freshness by checking:

- `data/health-state.json`
- the affected bot log in `data/logs/`
- recent source observations in `data/event-ledger.sqlite3` if needed

3. Restart only the service that owns the failing source after the root cause is understood:

```bash
python3 scripts/supervisor.py restart weather
python3 scripts/supervisor.py restart monitor
python3 scripts/supervisor.py restart economics
python3 scripts/supervisor.py restart entertainment
```

## Escalation

- If both the strategy bot and `source-monitor` fail on the same source, open an incident instead of repeatedly restarting both.
- If the source is healthy upstream but parsing is broken locally, freeze the affected promotion or release until the parser issue is fixed.

## Exit Criteria

- source status is back to `ok` in the scorecard
- `last_error_message` is cleared or stale history only
- dependent services resume normal scans without restart loops
