# Weather Observation-Window Worklist

Generated: 2026-03-22
Status: Phase 4 observation-window with an explicit March 29 weather-family calibration override. Live calibration is refreshed; live prior, threshold, sizing, and storage changes remain gated.

## Goal

Improve weather bot profitability by using the observation window to sharpen measurement, isolate city-level edge, and stage post-Phase-4 changes without changing live trading behavior.

## Primary Documents

Use these in order:

1. [2026-03-06-plan2-weather-bot.md](./2026-03-06-plan2-weather-bot.md)
   - the base weather strategy and longer-run optimization plan
2. [../superpowers/specs/2026-03-11-weather-bias-correction-design.md](../superpowers/specs/2026-03-11-weather-bias-correction-design.md)
   - the model-quality and bias-correction rationale
3. [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md)
   - what is safe to do right now during the Phase 4 observation window
4. [2026-03-22-weather-april1-promotion-list.md](./2026-03-22-weather-april1-promotion-list.md)
   - the ranked post-window promotion order once the gate is clear
5. [2026-03-29-weather-family-calibration-audit.md](./2026-03-29-weather-family-calibration-audit.md)
   - the audit log for the March 29 live calibration refresh and supporting fixes
6. [2026-04-02-weather-outage-recovery-audit.md](./2026-04-02-weather-outage-recovery-audit.md)
   - the audit log for the April 2 outage recovery, automatic drawdown-halt recovery hardening, and live supervisor cutover

## Scope Clarification

In this document, Phase 4 means the repo-wide ledger parity observation window, not a separate weather-only calibration rollout.

Weather-family work during this window is split into three distinct paths:

1. Forecast-weather and NWS live collection that should keep running:
   - `data/weather-verification.json`
   - `data/weather-nws-cross-check.json`
2. Forecast-weather offline prior-building that was stale at the start of the window and now has one explicit live refresh exception recorded on March 29:
   - `data/weather-training.db`
   - `config/weather-live-bias.json`
3. Shared weather-family shadow-only measurement and promotion prep that is safe now:
   - observation packs
   - shadow backtests
   - shadow calibration candidates
   - shadow prior refresh under `data/shadow/**`

Keeping these paths separate avoids the main source of confusion:
the live weather verifier should continue collecting now, while the remaining forecast-weather prior, threshold, schema, and storage promotions stay gated behind the shared weather-family review.

## March 29 Exceptions

This worklist still treats the observation window as mutation-light, but it now has two explicit March 29 live exceptions already executed and audit-logged:

1. Live weather-family sigma calibration refresh:
   - `config/calibration.json`
2. Live forecast-weather prior refresh:
   - `data/weather-training.db`
   - `config/weather-live-bias.json`

Also note:

- the narrow source-monitor NWS threshold-NO liquidity override is already live as part of the March 29 observed-weather execution follow-up
- unless a future audit note says otherwise, do not treat these exceptions as permission for more live threshold, sizing, schema, or storage changes during Phase 4

## Operator Sequence

Use the weather-family docs in this order:

1. This worklist is the execution document during the observation window for both forecast-weather and source-monitor NWS.
2. [2026-03-06-plan2-weather-bot.md](./2026-03-06-plan2-weather-bot.md) is the forecast-weather strategy and implementation background.
3. [2026-03-06-plan6-source-monitor.md](./2026-03-06-plan6-source-monitor.md) is the source-monitor NWS strategy and implementation background.
4. [2026-03-22-weather-april1-promotion-list.md](./2026-03-22-weather-april1-promotion-list.md) is the first post-window promotion review document after the shared weather-family gate clears.

## Current State

- Weather family is already profitable: current realized P&L is positive and win rate is above 69%.
- Live verification is healthy: `data/weather-verification.json` is updating and the source mix is dominated by `nws_cli`.
- Live sigma calibration is refreshed:
  - `config/calibration.json` now has `generated_at = 2026-03-29T22:02:45`
  - `weather.n = 268`
  - `nws.n = 80`
  - `nws.basis_by_hour = {"17+": "fit", "15-16": "carried_forward", "before_15": "carried_forward"}`
- Forecast-weather city-level bias conflicts are present and need analysis before any live recalibration.
- Source-monitor NWS weather is the strongest snapshot-measured weather alpha and should be reviewed alongside forecast-weather, not separately.
- Source-monitor NWS execution is now partially improved on the live path:
  - conservative quarter-Kelly sizing remains in place after review
  - a narrow threshold-NO liquidity override is enabled and audit-tagged via `execution_style`
- Forecast-weather prior has now been refreshed on the live path:
  - `config/weather-live-bias.json` has `generated_at = 2026-03-29T21:18:25.986309+00:00`
  - the live prior now spans `2026-02-27 -> 2026-03-28` with `5580` matched forecast rows
- The full multi-city shadow prior refresh now reproduces on the current tree:
  - March 29 all-city backfill and full shadow-refresh reruns succeeded end-to-end
  - the prior scripts are now hardened to fail loudly on total previous-runs fetch failure or zero-row bias artifacts
- The top-level weather-family artifacts are refreshed, but they now make the reporting split explicit:
  - `data/weather-observation-pack.json` uses `financial_snapshot` as the canonical per-bot basis
  - city-level weather P&L is now labeled `local_executed_trade_log_settlements`
  - source-monitor NWS still carries a `mismatch_under_review` reporting warning versus the executed local trade proxy
  - source-monitor bot-level realized P&L stays visible as context, but is not folded into attributable source-monitor NWS or combined weather-family realized totals while that warning is active
- Current top-level promotion output is materially more conservative after removing resting-order settlement noise:
  - forecast-weather: `hold = 4`, `tighten = 3`, `shadow_only = 13`
  - source-monitor NWS: `hold = 2`, `tighten = 2`, `shadow_only = 6`
- Ledger retention / hot-cold storage work is explicitly post-Phase-4.

## Safe Now

These are safe during the observation window because they are read-only, shadow-only, or local analysis only.
Do not overwrite additional live tuning/config artifacts during this window beyond the explicit March 29 exceptions recorded above.

### Shadow refresh orchestrator

- Script: `python3 scripts/weather-shadow-refresh.py`
- Default output tree: `data/shadow/weather-refresh/`
- Safety rule:
  - the script only writes under `data/shadow/`
- What it refreshes:
  - observation pack
  - city audit
  - verification summary
  - NWS cross-check audit
  - execution audit
  - shadow backtest
  - shadow calibration
  - promotion candidates
- Optional `--refresh-shadow-prior`:
  - refreshes a shadow training DB and shadow live-bias artifact under the same shadow tree
  - this is allowed during Phase 4 because it stays in shadow paths only
  - it is not permission to refresh `data/weather-training.db` or `config/weather-live-bias.json` again on the live path during Phase 4
  - default prior refresh now mirrors the live previous-runs window more closely: 14 days across `gfs,ecmwf,icon,gem,graphcast`

Use this as the preferred way to refresh the shadow bundle during Phase 4 because it keeps the outputs disjoint from live config/state.
Review the resulting bundle as a shared weather-family artifact, then split forecast-weather and NWS recommendations during promotion review.

### 1. Refresh weather measurement pack

- Script: `python3 scripts/weather-verification-summary.py --lookback-days 7 30 --json`
- Script: `python3 scripts/weather-city-audit.py --lookback-days 30 --json`
- Script: `python3 scripts/ledger-parity-report.py --json`
  - note: this refreshes/saves parity state in the ledger; treat it as a Phase-4 artifact refresh, not a pure inspection step
- Artifacts to review:
  - `data/weather-verification.json`
  - `data/weather-nws-cross-check.json`
  - `data/financial-snapshot.json`
  - `data/backtest-results.json`
  - `config/calibration.json`

Deliverable:
- One compact weather-family observation pack with current P&L, source mix, stale-artifact flags, and city risk notes.
- Note:
  - the observation pack source mix is a forecast-date-window summary over verified rows, not a separate processing-latency metric

### 2. Rank cities by profitability and conflict

- Use settled trades in `data/kalshi-trades.json`
- Use city conflict output from `weather-city-audit.py`
- Compare city-level realized P&L, executed count, and sign-flip / conflict status

Deliverable:
- A city ranking for “keep trading”, “trade cautiously”, and “shadow only”, with separate notes for forecast-weather and source-monitor NWS weather.

### 3. Track shadow-only recalibration candidates

- Compare current live bias in `data/weather-verification.json` against historical bias in the weather calibration artifacts.
- Identify cities where historical bias and live bias disagree materially.
- Record the candidate change, but do not apply it live.
- Shadow prior refresh is allowed here if it stays under `data/shadow/**`.
- Any refreshed backtest or calibration outputs must go to shadow paths only, not overwrite:
  - `data/backtest-results.json`
  - `config/calibration.json`

Deliverable:
- A list of candidate per-city threshold or bias updates with estimated impact and confidence.
- Split the output into forecast-weather candidates and source-monitor NWS candidates before any live review.

### 4. Stage ledger storage cleanup

- Keep this as a plan only until Phase 4 closes cleanly.
- Target hot SQLite + cold Parquet split after the observation window.

Deliverable:
- A post-Phase-4 retention implementation plan, not an active change.

### 5. Generate a shadow promotion artifact

- Preferred script: `python3 scripts/weather-shadow-refresh.py`
- If you only need the promotion ranking, `python3 scripts/weather-promotion-candidates.py --save --output data/shadow/weather-refresh/weather-promotion-candidates.json`
- Inputs:
  - `data/shadow/weather-refresh/weather-observation-pack.json`
  - `data/shadow/weather-refresh/weather-city-audit.json` if present
- Output:
  - `data/shadow/weather-refresh/weather-promotion-candidates.json`

Deliverable:
- A non-canonical ranking of weather-family promotion candidates for the post-window review.
- The promotion artifact should explicitly identify forecast-weather versus source-monitor NWS recommendations.

## After March 31, 2026

These should wait until the Phase 4 observation window closes cleanly.

- Apply any live forecast-weather per-city threshold or sizing changes.
- Apply any live source-monitor NWS threshold or sizing changes only with the shared review.
- Add richer verification provenance fields if the schema needs it.
- Implement ledger hot/cold retention and archive pruning.
- Restart or retune the weather bot for new calibration behavior.

## Execution Gate

Do not promote any weather-family calibration or sizing changes until all of the following are true:

- The Phase 4 parity window is closed.
- The latest parity report is stable and understood.
- The shadow weather pack has been refreshed on post-fix data.
- The shadow prior refresh and diff have been reviewed.
- City-level profitability and conflict analysis supports the change for both forecast-weather and source-monitor NWS tracks.
- The change has a clear rollback path.

## Suggested Outputs

- `data/shadow/weather-refresh/weather-observation-pack.json` as the preferred Phase 4 operator summary artifact.
- `data/shadow/weather-refresh/weather-promotion-candidates.json` as the preferred Phase 4 promotion ranking artifact.
- `data/weather-observation-pack.json` and `data/weather-promotion-candidates.json` only when a top-level derived copy is explicitly needed.
- `docs/plans/2026-03-22-weather-observation-window-worklist.md` as the operator checklist.
- `docs/plans/2026-03-22-weather-april1-promotion-list.md` as the ranked post-window promotion review.
- A post-Phase-4 archive plan for `data/event-ledger.sqlite3` storage reduction.
