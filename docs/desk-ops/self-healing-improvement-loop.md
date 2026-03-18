# Self-Healing And Self-Improving Daily Loop

This document is the implementation blueprint for a technical PM to turn the current desk-ops checklist into a closed-loop system that:

- detects operational and trading-quality problems early
- heals safe failure modes automatically
- escalates unsafe failure modes consistently
- converts daily observations into explicit improvement actions

It is meant to sit on top of the existing Phase 7 and Phase 8 artifacts, not replace them.

## Why This Exists

The current repo already has most of the raw ingredients for a professional daily loop:

- daily reconciliation, attribution, and source checks in [operating-cadence.md](./operating-cadence.md)
- a canonical event ledger and parity reporting
- operator actions in the dashboard
- incident and promotion workflows
- allocator budget decisions with `binding_constraint`
- missed-trade analysis over the canonical opportunity log
- execution-quality analytics

What is still missing is the control layer that connects them into one opinionated daily system.

Today the desk can answer many questions manually.
The goal of this document is to define how to answer them automatically and consistently.

## Current Context

- Phase 4 ledger parity is still in observation mode.
- If the current fixed Phase 4 build rolled out on 2026-03-17, the parity observation window should be treated as running through 2026-03-31.
- During that window, the loop should automate observation, incident opening, and recommendation generation.
- During that window, the loop should not auto-apply allocator, config, promotion, or schema changes.

This means the first rollout should be recommendation-heavy and mutation-light.

## Primary Objectives

1. Catch data integrity, source, and process failures before they silently affect live trading.
2. Explain realized P&L and missed P&L every day.
3. Show which capital or risk constraint blocked the next best trade.
4. Convert repeated patterns into explicit follow-up actions, not tribal knowledge.
5. Keep every change auditable through incidents, experiments, and config history.

## Non-Goals

- Full autonomous trading policy changes without review.
- Automatic model promotion during Phase 4 parity observation.
- Replacing human review for incidents, promotions, or large capital changes.
- Rewriting the existing Phase 8 docs.

## Existing Building Blocks

The implementation should treat these as system inputs, not new requirements.

| Area | Existing command or module | Main artifact or output | Notes |
|---|---|---|---|
| Ledger parity | `python3 scripts/ledger-parity-report.py --json --save` | `data/ledger-parity-report.json` | Must gate ledger-backed readers |
| Reconciliation | `python3 scripts/reconcile-trades.py` | trade logs, ledger settlement/fill events | Truth update for settled trades |
| Attribution | `python3 scripts/daily-attribution.py --save` | `data/attribution-report.json` | Canonical post-trade explain |
| Financial snapshot | `python3 scripts/pnl-snapshot.py` | `data/financial-snapshot.json` | NAV and balance-equation cross-check |
| Performance and execution | `python3 scripts/analyze-performance.py --reconcile --save` | `data/performance-metrics.json` | Includes realized P&L and fees |
| Source health | `python3 scripts/source-scorecard.py --json` | `data/source-catalog.json` | Freshness and parser failure review |
| Process health | `python3 scripts/supervisor.py status`, `python3 scripts/check-sync-health.py` | supervisor state, health state | Bot/process sanity |
| Missed trades | `python3 scripts/missed-trade-report.py --limit 20 --save` | `data/missed-trade-report.json` | Best current view of foregone EV |
| Promotions | `python3 scripts/promotion-workflow.py show <experiment_id>` | `data/experiment-runs.json` | Audited stage changes |
| Incidents | `python3 scripts/incident-workflow.py ...` | `data/incident-reviews.json` | Canonical follow-up path |
| Allocator state | `src/kalshi/capital_allocator.py` | `data/allocator-state.json`, budget decisions in ledger | Already records `binding_constraint` |

## Core Gap To Close

The desk lacks one canonical daily orchestration layer that does all of the following in one place:

1. run the key checks in a stable order
2. normalize their outputs into one daily operations artifact
3. classify failures by severity and allowed remediation type
4. trigger safe automations
5. open incidents or recommendations for unsafe automations
6. track whether yesterday's recommendations were acted on

That orchestration layer is the main implementation target.

## Proposed Daily Loop

The loop should run in five stages.

| Stage | Goal | Inputs | Output |
|---|---|---|---|
| 0. Refresh | Produce fresh daily artifacts | parity, source, process, reconcile, attribution, snapshot, performance, missed-trade commands | fresh saved artifacts |
| 1. Health gate | Decide whether the desk is healthy enough to trust its own telemetry | saved artifacts plus dashboard operator actions | health verdict and incident candidates |
| 2. Explain | Turn results into business understanding | attribution, performance, snapshot, execution quality, missed trades | daily explain report |
| 3. Diagnose | Identify why returns were lower or risk was higher than expected | missed trades, allocator constraints, source failures, execution metrics | ranked improvement opportunities |
| 4. Act | Heal safe failures and queue reviewed changes | rules engine, incident workflow, promotion workflow | auto-actions plus proposed actions |

## Recommended Artifact Contract

Add a canonical daily artifact:

- `data/daily-ops-loop.json`

The implementation should normalize all daily decisions into this file.

Recommended top-level fields:

```json
{
  "generated_at": "ISO-8601",
  "phase4_window": {
    "mode": "observation",
    "parity_required_until": "2026-03-31"
  },
  "health": {
    "overall_status": "ok|warning|critical",
    "parity": {},
    "sources": {},
    "processes": {},
    "snapshot": {}
  },
  "trading_outcome": {
    "realized_pnl_cents": 0,
    "today_pnl_cents": 0,
    "settled_trade_count": 0,
    "by_bot": {},
    "by_market_type": {}
  },
  "execution_quality": {
    "fill_rate": null,
    "avg_slippage_cents": null,
    "impl_shortfall_cents": null,
    "by_bot": {}
  },
  "missed_opportunity": {
    "top_missed_trades": [],
    "reason_distribution": {},
    "estimated_foregone_pnl_cents": 0
  },
  "allocator": {
    "binding_constraints": {},
    "capital_utilization": {},
    "recommended_reallocations": []
  },
  "actions": {
    "auto_executed": [],
    "operator_required": [],
    "incident_candidates": [],
    "promotion_candidates": []
  },
  "follow_through": {
    "open_incidents": [],
    "open_experiments": [],
    "stale_recommendations": []
  }
}
```

The exact contract can vary, but the orchestration layer should write one normalized record every day.

## Automation Boundaries

The system should separate actions into three classes.

### Class A: Safe To Auto-Execute

These can run automatically during Phase 4.

- regenerate saved parity report
- regenerate saved attribution report
- regenerate saved source scorecard
- regenerate financial snapshot
- rerun a non-destructive check after a transient failure
- restart one stale bot once when the runbook already treats restart as standard remediation
- open or update an incident record when a known threshold is crossed

### Class B: Auto-Propose, Human-Approve

These should become recommendations, not automatic changes.

- reduce a bot's daily limit
- increase a bot's daily limit
- disable or re-enable a source
- change execution aggressiveness
- advance an experiment from `shadow` to `capped_live`
- roll back an experiment
- apply a calibration suggestion

### Class C: Explicitly Manual

These should never auto-run from the daily loop.

- schema changes
- ledger cutover changes
- new strategy enablement
- large capital allocation policy changes
- deleting history or artifacts

## Decision Rules

These should be implemented as explicit policy functions, not scattered string matching.

### 1. Health Gate Rules

- If parity `overall_ok` is false, mark the day `critical`, keep affected readers in legacy mode, and open or update an incident.
- If a critical source is in `error`, mark the related strategies as degraded and block size increases for those strategies.
- If a bot is stale or crash-looping, restart once. If the issue repeats the same day, open an incident and stop auto-restarting.
- If the financial snapshot cannot be produced, mark the day `critical` and block promotion decisions.

### 2. Improvement Rules

- If missed-trade estimated EV is high and the dominant reason is allocator denial, generate a capital reallocation recommendation.
- If missed-trade estimated EV is high and the dominant reason is source failure, generate a source remediation or disable recommendation.
- If realized P&L is weak and realized edge is weak, generate a model or threshold review recommendation.
- If realized P&L is weak but realized edge looks acceptable while execution quality deteriorates, generate an execution-policy recommendation instead of a model recommendation.

### 3. Promotion Rules

- A strategy should not be promoted while parity, source freshness, or execution quality incidents are open.
- Promotion recommendations should include experiment id, last 7-day results, last 30-day results, current stage, and rollback note.

## Missing Daily Signal: Binding Constraint Analysis

This is the highest-value missing report.

The allocator already stores enough information to build it:

- budget decisions are recorded to the ledger
- each decision includes `approved`, `reason`, `max_cost_cents`, `bankroll_cents`, and `binding_constraint`
- the missed-trade analyzer already exposes missed opportunities by reason

The new loop should add a daily allocator report that answers:

1. Which constraint blocked the most opportunities today?
2. Which constraint blocked the highest estimated EV today?
3. Which bot had the highest foregone EV due to caps?
4. Which markets were skipped due to concentration, not lack of edge?
5. If capital were reallocated across bots, what was the top candidate move?

Recommended output fields:

- denial count by `binding_constraint`
- estimated foregone EV by `binding_constraint`
- denial count by bot
- estimated foregone EV by bot
- top 10 denied opportunities
- candidate reallocation moves with supporting evidence

## Missing Daily Signal: Capital Efficiency

The allocator is already risk-aware, but the loop should explicitly measure capital efficiency.

Recommended daily metrics:

- capital deployed as percent of available cash
- realized P&L per dollar deployed
- estimated foregone EV per idle dollar
- expected return per dollar-day locked
- concentration by ticker, city, region, and cluster
- per-bot spend versus realized P&L and missed EV

This should inform a weekly or monthly reallocation process, but the metrics should be computed daily.

## Proposed Implementation Slices

The technical PM should plan this as additive slices.

### Slice 1: Daily Loop Orchestrator

Add:

- `scripts/daily-ops-loop.py`

Responsibilities:

- run the canonical daily commands in order
- load their saved artifacts
- write `data/daily-ops-loop.json`
- emit a concise text summary for operators

Acceptance criteria:

- one command can regenerate the full daily ops record
- failures are captured structurally, not only in stdout

### Slice 2: Policy Engine

Add:

- `src/kalshi/ops/daily_loop_policy.py`

Responsibilities:

- convert raw signals into `ok`, `warning`, `critical`
- classify candidate actions into auto, proposal, or manual
- generate incident candidates and promotion blockers

Acceptance criteria:

- policy rules are unit-tested
- no hidden remediation logic lives only in the CLI script

### Slice 3: Constraint And Missed-EV Report

Add:

- `src/kalshi/research/allocator_constraint_analysis.py`
- optional `scripts/allocator-constraint-report.py`

Responsibilities:

- combine budget-decision events with opportunity-log records
- quantify missed EV by `binding_constraint`
- surface capital reallocation candidates

Acceptance criteria:

- daily loop can explain whether returns are being limited by model quality, source quality, or capital policy

### Slice 4: Auto-Heal Connectors

Add safe adapters for:

- one-time stale bot restart
- incident open or update
- recommendation persistence

Do not add auto-promotion or auto-config writes in this slice.

Acceptance criteria:

- safe actions are idempotent
- every action leaves an auditable record

### Slice 5: Dashboard Integration

Extend the dashboard with:

- daily loop summary
- open recommendations
- repeated unresolved anomalies
- top missed EV due to allocator constraints

Acceptance criteria:

- the system tab shows not only failures, but also the highest-value next operator action

## Rollout Plan

### Phase A: Shadow Only

- run the daily loop
- save artifacts
- generate recommendations
- do not auto-execute anything except non-destructive report refreshes

### Phase B: Safe Self-Heal

- allow one stale-bot restart
- allow auto-incident open or update
- allow auto-refresh of saved artifacts

### Phase C: Recommendation Discipline

- require operators to disposition every recommendation as:
  - accepted
  - rejected
  - deferred
- store the disposition in the daily loop artifact or linked registry

### Phase D: Post-Phase-4 Expansion

After the parity observation window closes cleanly, consider:

- allocator-limit recommendation automation
- experiment promotion suggestion automation
- execution-policy suggestion automation

Do not skip directly from shadow to autonomous config changes.

## Success Metrics

The PM should measure whether the loop is actually improving the desk.

### Reliability

- percent of days with a fresh `daily-ops-loop.json`
- mean time to detect parity, source, and stale-bot failures
- mean time to resolution for repeated incident classes

### Improvement Velocity

- percent of anomaly days with a linked follow-up action
- percent of follow-ups closed within target SLA
- repeated-issue recurrence rate after fix

### Trading Quality

- missed EV due to capital constraints
- missed EV due to source failures
- execution shortfall trend by bot
- realized P&L versus capital deployed
- per-bot and per-market-type P&L stability

## Recommended PM Questions

The technical PM should be able to answer these before implementation starts:

1. Which daily actions are safe enough to auto-run now?
2. Which daily actions require explicit owner approval?
3. What artifact is the single source of truth for the daily loop?
4. What thresholds open incidents automatically?
5. What thresholds create allocator or promotion recommendations?
6. How are unresolved recommendations carried forward day to day?
7. What is the disposition workflow for rejected recommendations?

## Immediate Next Step

The first implementation should be:

1. `scripts/daily-ops-loop.py`
2. `data/daily-ops-loop.json`
3. a policy module that classifies parity, source, process, snapshot, attribution, execution, and missed-trade signals
4. a daily allocator constraint report based on `binding_constraint`

That is the minimum path to a real self-healing and self-improving loop.
