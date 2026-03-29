# Weather Observation-Window Worklist

Generated: 2026-03-22
Status: Phase 4 observation-window only. No live weather calibration or runtime config changes until after 2026-03-31.

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

## Scope Clarification

In this document, Phase 4 means the repo-wide ledger parity observation window, not a separate weather-only calibration rollout.

Weather work during this window is split into three distinct paths:

1. Live collection that should keep running:
   - `data/weather-verification.json`
   - `data/weather-nws-cross-check.json`
2. Offline prior-building that is currently stale and should be refreshed only after the window when targeting live paths:
   - `data/weather-training.db`
   - `config/weather-live-bias.json`
3. Shadow-only measurement and promotion prep that is safe now:
   - observation packs
   - shadow backtests
   - shadow calibration candidates
   - shadow prior refresh under `data/shadow/**`

Keeping these paths separate avoids the main source of confusion:
the live weather verifier should continue collecting now, while live calibration artifacts and sizing should remain frozen until the parity window closes cleanly.

## Current State

- Weather bot is already profitable: current realized P&L is positive and win rate is above 69%.
- Live verification is healthy: `data/weather-verification.json` is updating and the source mix is dominated by `nws_cli`.
- Backtest and sigma calibration are stale: `data/backtest-results.json` and `config/calibration.json` are still from March 7, 2026.
- City-level bias conflicts are present and need analysis before any live recalibration.
- The full multi-city shadow prior refresh is still blocked today:
  - `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior` now fails loudly when the shadow DB is empty
  - a single-city 14-day, 5-model AUS probe succeeded with 130 training pairs, so the remaining issue is the full multi-city backfill path at scale, not the prior pipeline itself
- Ledger retention / hot-cold storage work is explicitly post-Phase-4.

## Safe Now

These are safe during the observation window because they are read-only, shadow-only, or local analysis only.
Do not overwrite live tuning/config artifacts during this window.

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
  - it is not permission to refresh `data/weather-training.db` or `config/weather-live-bias.json`
  - default prior refresh now mirrors the live previous-runs window more closely: 14 days across `gfs,ecmwf,icon,gem,graphcast`

Use this as the preferred way to refresh the shadow bundle during Phase 4 because it keeps the outputs disjoint from live config/state.

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
- One compact weather observation pack with current P&L, source mix, stale-artifact flags, and city risk notes.
- Note:
  - the observation pack source mix is a forecast-date-window summary over verified rows, not a separate processing-latency metric

### 2. Rank cities by profitability and conflict

- Use settled trades in `data/kalshi-trades.json`
- Use city conflict output from `weather-city-audit.py`
- Compare city-level realized P&L, executed count, and sign-flip / conflict status

Deliverable:
- A city ranking for “keep trading”, “trade cautiously”, and “shadow only”.

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
- A non-canonical ranking of city promotion candidates for the post-window review.

## After March 31, 2026

These should wait until the Phase 4 observation window closes cleanly.

- Refresh `config/weather-live-bias.json` from the weather training pipeline.
- Apply any live per-city threshold or sizing changes.
- Add richer verification provenance fields if the schema needs it.
- Implement ledger hot/cold retention and archive pruning.
- Restart or retune the weather bot for new calibration behavior.

## Execution Gate

Do not promote any weather calibration or sizing changes until all of the following are true:

- The Phase 4 parity window is closed.
- The latest parity report is stable and understood.
- The shadow weather pack has been refreshed on post-fix data.
- The shadow prior refresh and diff have been reviewed.
- City-level profitability and conflict analysis supports the change.
- The change has a clear rollback path.

## Suggested Outputs

- `data/shadow/weather-refresh/weather-observation-pack.json` as the preferred Phase 4 operator summary artifact.
- `data/shadow/weather-refresh/weather-promotion-candidates.json` as the preferred Phase 4 promotion ranking artifact.
- `data/weather-observation-pack.json` and `data/weather-promotion-candidates.json` only when a top-level derived copy is explicitly needed.
- `docs/plans/2026-03-22-weather-observation-window-worklist.md` as the operator checklist.
- `docs/plans/2026-03-22-weather-april1-promotion-list.md` as the ranked post-window promotion review.
- A post-Phase-4 archive plan for `data/event-ledger.sqlite3` storage reduction.
