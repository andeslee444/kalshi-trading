# Weather April 1 Promotion List

Generated: 2026-03-22
Status: Draft for post-Phase-4 promotion review. Do not apply live before the observation window closes cleanly.

## Goal

Rank the highest-value weather-bot changes to consider after 2026-03-31, using current observation-window evidence.

April 1 here is shorthand for the first post-window promotion review checkpoint.
It is not an instruction to auto-promote on 2026-04-01 regardless of parity or shadow results.

## Inputs

This promotion list assumes the operator has already used:

- [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md)
  for the safe-now observation tasks
- [../superpowers/specs/2026-03-11-weather-bias-correction-design.md](../superpowers/specs/2026-03-11-weather-bias-correction-design.md)
  for the bias-correction rationale behind the ranking
- [2026-03-06-plan2-weather-bot.md](./2026-03-06-plan2-weather-bot.md)
  for the underlying weather-bot strategy scope
- `python3 scripts/weather-shadow-refresh.py`
  for the shadow bundle that keeps observation, audit, backtest, and calibration outputs disjoint from live configs
  and supports a shadow-only stale-prior refresh during Phase 4 via `--refresh-shadow-prior`

## Evidence Base

- Live weather verification is still updating, with `data/weather-verification.json` modified on 2026-03-23 and dominated by `nws_cli` actuals.
- The offline weather prior is stale:
  - `config/weather-live-bias.json` modified 2026-03-14
  - `data/weather-training.db` modified 2026-03-14
  - the full multi-city shadow refresh path is still failing with an empty shadow training DB, while a single-city 14-day, 5-model AUS probe succeeds with 130 pairs; this points to a scale/reliability issue in the multi-city backfill path that should be fixed before live promotion
- The main offline measurement artifacts are stale:
  - `data/backtest-results.json` still carries a 2026-03-07 generated timestamp
  - `config/calibration.json` has a newer file mtime but the current weather calibration payload still reflects the March 7 generation cycle
- City-level live-vs-historical bias conflict is widespread:
  - `python3 scripts/weather-city-audit.py --lookback-days 30 --json` reports `bias_conflict=true` for all 20 cities
  - the largest sign-flip gaps are in AUS, OKC, SATX, DAL, ATL, and HOU
- Realized weather P&L remains positive overall, but city outcomes are uneven:
  - strongest realized cities are PHIL, LAX, and DEN
  - weaker active cities are HOU and MIA

## Rank 1: Promote The Stale-Prior Refresh Path

Expected value: high  
Risk: moderate if changed live without a shadow check, low if staged first

### Recommendation

After Phase 4 closes cleanly:

1. Run `python3 scripts/backfill-weather-data.py` to refresh `data/weather-training.db`
2. Run `python3 scripts/calibrate-weather-bias.py` to build a fresh bias artifact
3. Diff the result against the current `config/weather-live-bias.json`
4. Promote only after the shadow comparison looks stable

Before the live promotion review:

1. Run `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior`
2. Review the shadow observation pack and promotion artifact from the same shadow tree
3. Treat that shadow prior diff as required evidence, not optional context

### Why This Is First

- The bot explicitly prefers a lead-time-matched prior when available.
- The current prior is about nine days behind the live verification stream.
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

### A. First Cities To Consider For Expansion

- `PHIL`
  - best realized city P&L in the current observation pack
  - 18 settled trades, 77.8% win rate
  - live bias still conflicts with the historical prior, so expand only after the stale-prior refresh and shadow pack agree
- `LAX`
  - strong realized P&L with meaningful trade count
  - 36 settled trades, 69.4% win rate
  - live confidence is decent but not maxed, so this is a good candidate for a modest loosen/sizing increase, not a big jump
- `DEN`
  - strong realized P&L with 34 settled trades
  - live bias has almost neutralized the very large historical bias
  - likely the cleanest city for a measured expansion if the shadow pack confirms

### B. Keep Current Until The Shadow Pack Is Refreshed

- `AUS`
  - profitable, but the largest active-city bias conflict in the current pack
  - do not loosen just because realized P&L is positive
- `NY`
  - positive realized P&L and good win rate, but the edge looks smaller than the top tier
  - candidate for “keep as-is” unless the refreshed pack shows a clear upside
- `CHI`
  - positive but modest realized contribution with a weaker win rate
  - needs fresh shadow metrics before any live aggression change

### C. Tighten Or Shadow-Only First

- `HOU`
  - weak realized contribution and sub-50% win rate
  - large sign-flip bias conflict
  - strongest candidate for a tighter threshold or smaller size after Phase 4
- `MIA`
  - positive but weak realized contribution relative to activity
  - low win rate and sign-flip bias conflict
  - candidate for tighter entry conditions

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

## Promotion Sequence

The recommended post-window order is:

1. Refresh the weather training DB and live-bias artifact in shadow
2. Refresh the weather backtest/calibration pack in shadow
3. Re-run `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior` and review the shadow bundle as a whole
4. Promote city threshold or sizing changes only for cities supported by both refreshed packs
5. Start the ledger archive rollout with `trade_decision`

## No-Go Conditions

Do not promote on April 1 if any of the following remain true:

- Phase 4 parity is still materially unstable
- the refreshed shadow pack does not clearly outperform the March 7 baseline
- the new live-bias artifact still conflicts sharply with the latest live verifier in the target city
- the proposed city change is based on tiny sample rather than sustained evidence
