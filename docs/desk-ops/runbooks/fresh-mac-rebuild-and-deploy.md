# Fresh Mac Rebuild And Deploy

Use this runbook after a factory reset or when rebuilding the runtime machine from scratch.

## Purpose

This repo uses one GitHub repository with multiple local worktrees:

- dev: `/Users/andeslee/Documents/cursor-projects/kalshi-trading`
- demo runtime: `~/deploy/kalshi-demo`
- prod runtime: `~/deploy/kalshi-prod`
- oracle demo runtime: `~/deploy/kalshi-oracle-demo`

Each deploy root is a full Git worktree of the same repo history. Do not copy source files between roots manually. Promote code by commit/branch, then update the worktree to the approved commit.

## Source Of Truth

Code and non-secret config live in GitHub:

- dev branch: `autoresearch/20260319`
- demo deploy branch: `deploy-demo`
- prod deploy branch: `deploy-prod`
- oracle demo deploy branch: `deploy-oracle-demo`

Machine-local state does **not** live in GitHub:

- `.env`
- `config/keys/*.pem`
- deploy-root `data/`
- installed launchd plists in `~/Library/LaunchAgents`

Those must be restored from a separate encrypted backup.

## Current Deploy Labels

- demo supervisor: `com.kalshi.demo.supervisor`
- prod supervisor: `com.kalshi.prod.supervisor`
- oracle shadow bot: `com.kalshi.oracle-shadow-bot`
- oracle alpha maintenance: `com.kalshi.oracle-alpha-maintenance`
- oracle latency probe: `com.kalshi.oracle-latency-probe`
- oracle h2 pregame collector: `com.kalshi.oracle-h2-pregame-collector`

Legacy label `com.kalshi.supervisor` belongs to the old dev-root runtime and should stay disabled unless explicitly reviving that pattern.

## Pre-Reset Checklist

1. Push the code branches listed above.
2. Create an encrypted backup containing:
   - repo-root `.env`
   - repo-root `config/keys/`
   - `~/deploy/kalshi-demo/.env`
   - `~/deploy/kalshi-demo/config/keys/`
   - `~/deploy/kalshi-demo/data/`
   - `~/deploy/kalshi-prod/.env`
   - `~/deploy/kalshi-prod/config/keys/`
   - `~/deploy/kalshi-prod/data/`
   - `~/deploy/kalshi-oracle-demo/.env`
   - `~/deploy/kalshi-oracle-demo/config/keys/`
   - `~/deploy/kalshi-oracle-demo/data/`
3. Optional backup:
   - `~/Library/LaunchAgents/com.kalshi*.plist`

Installed launchd files are optional because the repo now contains canonical install helpers and plist templates.

## Clean-Mac Restore

1. Clone the repo:

```bash
git clone git@github.com:andeslee444/kalshi-trading.git /Users/andeslee/Documents/cursor-projects/kalshi-trading
```

2. Bootstrap the standard worktrees:

```bash
cd /Users/andeslee/Documents/cursor-projects/kalshi-trading
bash scripts/bootstrap-deploy-worktrees.sh
```

This creates:

- `~/deploy/kalshi-demo`
- `~/deploy/kalshi-prod`
- `~/deploy/kalshi-oracle-demo`

3. Restore the encrypted backup so the deploy roots recover:

- `.env`
- `config/keys`
- `data`

4. Reinstall launchd services from the repo:

```bash
cd /Users/andeslee/Documents/cursor-projects/kalshi-trading
bash scripts/install-launchd-service.sh demo-supervisor
bash scripts/install-launchd-service.sh prod-supervisor
bash scripts/install-launchd-service.sh oracle-demo-shadow-bot
bash scripts/install-launchd-service.sh oracle-demo-alpha-maintenance
bash scripts/install-launchd-service.sh oracle-demo-latency-probe
bash scripts/install-launchd-service.sh oracle-demo-h2-pregame-collector
```

5. Start the runtime supervisors explicitly if needed:

```bash
cd /Users/andeslee/Documents/cursor-projects/kalshi-trading
bash scripts/start-runtime-supervisor.sh demo
bash scripts/start-runtime-supervisor.sh prod
```

Oracle services are started by their install step because those launchd jobs are loaded directly.

## Promotion Workflow

1. Edit code in the dev root.
2. Commit in the dev root.
3. Push to GitHub.
4. Move the target worktree to the approved commit or branch.
5. Restart only the target runtime.

Do not manually copy source files from dev into demo or prod.

## Runtime Scope

The deploy worktrees contain the full repo, including all bot code. Runtime scope is controlled by:

- branch/commit
- bot enablement config
- which launchd services are installed and running

This means it is valid for a worktree to contain all bots while only one bot or one lane is live.

## Repo-Owned Helper Scripts

Use these repo scripts instead of ad hoc machine-local deploy helpers:

- `scripts/bootstrap-deploy-worktrees.sh`
- `scripts/install-launchd-service.sh`
- `scripts/status-launchd-service.sh`
- `scripts/uninstall-launchd-service.sh`
- `scripts/start-runtime-supervisor.sh`
- `scripts/stop-runtime-supervisor.sh`

The old one-off helper copies in `~/deploy/*.sh` are no longer the canonical source of truth.

## Operator Notes For The Next LLM

- Treat GitHub as the source of truth for code and non-secret config only.
- Treat the encrypted backup as the source of truth for secrets and runtime state.
- If a deploy root exists but is not on the correct code, update the worktree through Git rather than rsyncing files.
- If a clean Mac restore is missing launchd plists, regenerate them with the repo install scripts instead of hand-authoring new plist files.
- If prod is being restored, confirm the prod `.env` and `config/keys/kalshi-production.pem` were restored before starting `com.kalshi.prod.supervisor`.
