# Supervisor And Stale Bots

## When To Use

Use this runbook when bots are missing heartbeats, repeatedly restarting, or clearly duplicated.

## Signals

- `python3 scripts/supervisor.py status` shows a bot as stopped, stale, or crash-looping
- dashboard bot status disagrees with the supervisor view
- log files in `data/logs/` show duplicate-launch or repeated restart messages

## Immediate Actions

1. Check current supervisor state:

```bash
python3 scripts/supervisor.py status
```

2. Restart only the affected bot first:

```bash
python3 scripts/supervisor.py restart weather
python3 scripts/supervisor.py restart crypto
python3 scripts/supervisor.py restart economics
python3 scripts/supervisor.py restart entertainment
python3 scripts/supervisor.py restart monitor
python3 scripts/supervisor.py restart positions
```

3. If multiple bots are stale, review `data/pids/supervisor-state.json` and `data/logs/supervisor.log` before restarting the whole supervisor.

## Escalation

- If a bot restarts repeatedly, treat it as an incident and capture the recent bot log plus `supervisor.log`.
- If supervisor state and dashboard state disagree, preserve both snapshots before restarting anything else.

## Exit Criteria

- affected bot heartbeat is fresh
- restart count is no longer climbing
- dashboard and supervisor views agree on status
