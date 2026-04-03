# Weather Family April 1 Promotion List

Generated: 2026-03-22
Status: Draft for the remaining post-Phase-4 weather-family promotion review. Live sigma calibration was refreshed on March 29, 2026; this document now governs the remaining prior, threshold, sizing, and storage promotions.

## Goal

Rank the highest-value weather-family changes to consider after 2026-03-31, using current observation-window evidence.

April 1 here is shorthand for the first post-window promotion review checkpoint.
It is not an instruction to auto-promote on 2026-04-01 regardless of parity or shadow results.
The review should cover forecast-weather and source-monitor NWS weather together, while keeping their calibration actions separate.

## Inputs

This promotion list assumes the operator has already used:

- [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md)
  for the safe-now observation tasks
- [../superpowers/specs/2026-03-11-weather-bias-correction-design.md](../superpowers/specs/2026-03-11-weather-bias-correction-design.md)
  for the bias-correction rationale behind the ranking
- [2026-03-06-plan2-weather-bot.md](./2026-03-06-plan2-weather-bot.md)
  for the forecast-weather strategy scope
- `python3 scripts/weather-shadow-refresh.py`
  for the shadow bundle that keeps observation, audit, backtest, and calibration outputs disjoint from live configs
  and supports a shadow-only stale-prior refresh during Phase 4 via `--refresh-shadow-prior`
- [2026-03-06-plan6-source-monitor.md](./2026-03-06-plan6-source-monitor.md)
  for the observed-weather / source-monitor NWS track and its separate calibration details

## Evidence Base

- Live weather verification is still updating, with `data/weather-verification.json` modified on 2026-03-23 and dominated by `nws_cli` actuals.
- Live weather-family calibration was refreshed on 2026-03-29:
  - `config/calibration.json` now has `weather.n = 268`
  - `config/calibration.json` now has `nws.n = 80`
  - `config/calibration.json` now carries `basis_by_hour = {"17+": "fit", "15-16": "carried_forward", "before_15": "carried_forward"}`
- The forecast-weather offline prior was refreshed later on 2026-03-29:
  - `config/weather-live-bias.json` generated 2026-03-29T21:18:25.986309+00:00
  - `config/weather-live-bias.json` now spans `2026-02-27 -> 2026-03-28`
  - `data/weather-training.db` now carries `5580` matched forecast rows
  - the current all-city shadow backfill and full shadow-prior refresh both succeed on the March 29 tree, and the scripts now fail loudly instead of silently writing empty artifacts
- The main offline backtest artifact is still stale:
  - `data/backtest-results.json` still carries a 2026-03-07 generated timestamp
- City-level live-vs-historical bias conflict is widespread:
  - `python3 scripts/weather-city-audit.py --lookback-days 30 --json` reports `bias_conflict=true` for all 20 cities
  - the largest sign-flip gaps are in AUS, OKC, SATX, DAL, ATL, and HOU
- Current top-level weather-family artifacts are refreshed:
  - `data/weather-observation-pack.json` generated 2026-04-02T17:47:42.155925+00:00
  - `data/weather-promotion-candidates.json` generated 2026-04-02T17:47:42.335397+00:00
- Realized weather-family P&L remains positive overall, but city outcomes are uneven:
  - source-monitor NWS realized snapshot P&L is +$1,675.00, but that figure is still under a reconciliation warning versus the executed local source-monitor trade proxy
  - forecast-weather realized snapshot P&L is +$592.00
  - after removing resting-order settlement noise, the current forecast-weather artifact has no expansion candidates
  - current forecast-weather tighten candidates are CHI, HOU, and NY
- Source-monitor NWS weather is currently the strongest snapshot-measured weather alpha and should be promoted or tightened with the same weather-family gate, not as a separate planning lane.
- Source-monitor NWS execution has already been tightened once on March 29:
  - refreshed NWS calibration is live
  - conservative quarter-Kelly sizing is still in place
  - a narrow threshold-NO liquidity override is live and audit-tagged

## Rank 1: Promote The Stale-Prior Refresh Path

Expected value: high  
Risk: moderate if changed live without a shadow check, low if staged first

### Recommendation

Status on March 29:

- completed on the live path through the canonical training pipeline

Next review step:

1. Diff the refreshed live artifact against the last pre-promotion backup and the March 29 shadow artifact
2. Re-run the shadow weather-family promotion pack with the refreshed live prior now in place
3. Promote city thresholds or sizing only after that shadow comparison looks stable

Before the live promotion review:

1. Run `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior`
2. Review the shadow observation pack and promotion artifact from the same shadow tree
3. Treat that shadow prior diff as required evidence, not optional context

### Why This Is First

- The bot explicitly prefers a lead-time-matched prior when available.
- The refreshed prior materially increased the training window and sample count.
- The live verifier has already overridden the prior in many cities, but not all of them all the time.
- The biggest current global weather quality issue is not that live verification is broken; it is that the offline prior and the live evidence disagree materially.

## Rank 2: Promote A Fresh Shadow Weather Measurement Pack

Expected value: high  
Risk: low if kept shadow-only until review

### Recommendation

Build a fresh shadow pack before any live threshold or sizing change:

1. Refresh backtest results to a shadow path
2. Refresh weather sigma and threshold candidates to a shadow path
3. Compare post-bias-correction performance against the March 7 baseline
4. Promote only the deltas that improve recent weather quality without widening drawdown

### Why This Is Second

- Current live profitability is not well represented by the stale offline pack.
- A city-level threshold change without a fresh shadow pack would be guesswork.
- This is the cleanest gate between “interesting live observations” and “defensible April 1 promotions.”

## Rank 3: City Threshold And Sizing Candidates

These are ranked as candidates, not live instructions. Cities are grouped by the recommended first post-window action.
Separate the review into forecast-weather candidates and source-monitor NWS candidates before any live change.

### A. Current Expansion Status

- No forecast-weather city currently clears the expansion thresholds in the refreshed top-level promotion artifact.
- Treat that as a sign to stay evidence-first after the March 29 calibration refresh rather than forcing an April promotion just because live profitability is positive.
- If a future shadow bundle restores expansion candidates, review them there first rather than loosening off the current top-level artifact.

### B. Keep Current Until The Shadow Pack Is Refreshed

- `AUS`
  - highest current forecast-weather realized city P&L, but still below the expansion threshold
  - very large sign-flip bias conflict remains
- `DEN`
  - still one of the strongest forecast-weather cities
  - currently `hold`, not `expand`, in the refreshed artifact
- `LAX`
  - strong realized P&L, but the refreshed promotion artifact still rates it as `hold`
- `NY`
  - the refreshed artifact currently rates NY as `tighten`, but the magnitude is still small enough that it should be confirmed in shadow before any live change

### C. Tighten Or Shadow-Only First

- `HOU`
  - weak realized contribution and sub-50% win rate
  - large sign-flip bias conflict
  - strongest candidate for a tighter threshold or smaller size after Phase 4
- `CHI`
  - the refreshed artifact now rates CHI as `tighten`
  - realized P&L is negative on the executed-trade city proxy
- `NY`
  - the refreshed artifact now rates NY as `tighten`
  - keep it under review because the realized edge is smaller than the top hold cities

### D. Shadow-Only Until More Sample Exists

- `BOS`
- `DC`
- `DAL`
- `OKC`
- `SATX`
- `ATL`
- `PHX`
- `SFO`
- `LV`
- `MIN`
- `NOLA`
- `SEA`

These cities currently have either no settled sample, almost no settled sample, or no meaningful execution sample in the current pack. They should not drive live threshold loosening yet.

## Rank 4: Storage Rollout Order

Expected value: medium for weather profitability, high for ops durability  
Risk: moderate unless every reader is archive-ready

### Rollout Order

1. `trade_decision`
   - first archive candidate
   - largest known growth driver in the hot ledger
   - high storage payoff with low direct bot-runtime value for old rows
2. `forecast_snapshot`
   - next highest-value archive candidate for weather/history volume
   - useful for research, but old rows do not belong in hot SQLite forever
3. `market_snapshot`
   - similar pattern: useful history, limited need in hot storage
4. `source_observation`
   - preserve recent history hot, archive older rows after reader validation
5. `position_snapshot` and `budget_decision`
   - archive later; lower urgency

Do not prune these from hot storage:

- `order_submitted`
- `order_update`
- `fill`
- `settlement`
- `verification_result`
- `post_trade_attribution`

### Why This Is Fourth

- It improves ops speed and keeps the ledger sustainable.
- It is not the most immediate weather P&L lever.
- It should only start after Phase 4 parity is clean and readers are archive-aware.

## Source-Monitor NWS Promotion Notes

- Source-monitor is the strongest snapshot-measured weather alpha, so its NWS calibration review should be treated as part of the same weather-family gate.
- The March 29 calibration refresh and threshold-NO execution override are already live, so the next source-monitor review should focus on realized execution quality and capital allocation, not ingestion fixes.
- Review the source-monitor NWS track against the forecast-weather track on realized P&L, calibration quality, and execution quality before any shared capital change.
- Do not promote source-monitor NWS changes just because the forecast-weather track is ready, and do not promote forecast-weather changes just because source-monitor is strong.
- Keep the source-monitor promotion decision separate inside the shared weather-family review so the operator can see which track is driving the recommendation.
- Current top-level source-monitor NWS ranking is:
  - `hold`: DEN, NY
  - `tighten`: LAX, CHI
  - `shadow_only`: AUS, MIA, PHIL, HOU, BOS, DAL
- Keep those source-monitor city actions under the current `mismatch_under_review` reporting flag until the executed local trade proxy and snapshot basis are reconciled more deeply.
- While that flag is active, treat source-monitor bot-level P&L as context only; do not count it as attributable source-monitor NWS or combined weather-family realized P&L.

## Promotion Sequence

The recommended post-window order is:

1. Refresh the weather training DB and live-bias artifact in shadow
2. Refresh the weather backtest/calibration pack in shadow
3. Re-run `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior` and review the shadow bundle as a whole
4. Promote city threshold or sizing changes only for cities supported by both refreshed packs
5. Start the ledger archive rollout with `trade_decision`

Use the operator docs in this order after the gate clears:

1. Confirm the shared gate is actually closed in [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md).
2. Review the forecast-weather evidence and candidate changes in [2026-03-06-plan2-weather-bot.md](./2026-03-06-plan2-weather-bot.md).
3. Review the source-monitor NWS evidence and candidate changes in [2026-03-06-plan6-source-monitor.md](./2026-03-06-plan6-source-monitor.md).
4. Make one weather-family promotion decision, with separate sub-decisions for forecast-weather and source-monitor NWS if only one track is ready.

## No-Go Conditions

Do not promote on April 1 if any of the following remain true:

- Phase 4 parity is still materially unstable
- the refreshed shadow pack does not clearly outperform the March 7 baseline
- the new live-bias artifact still conflicts sharply with the latest live verifier in the target city
- the proposed city change is based on tiny sample rather than sustained evidence
