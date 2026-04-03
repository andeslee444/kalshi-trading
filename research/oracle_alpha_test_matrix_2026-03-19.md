# Oracle Alpha Test Matrix

Date: 2026-03-19

## Purpose

This document turns the prior research into a concrete proof framework.

Goal:

- stop debating vague edge narratives
- test the highest-value hypotheses in ranked order
- define what counts as a real edge
- define what kills a thesis quickly

This is the standard I would use before claiming Oracle can compete with strong funds, market makers, or serious sports quants.

## Status Update

Status as of 2026-03-29:

- The Real live websocket capture path was repaired to mirror the browser Engine.IO v3 websocket semantics, so H1 source-event collection is no longer stuck at zero.
- Real live game-detail feeds do not currently expose usable `playerBoxScores` during active NBA games; the H1 collector now falls back to recent `plays` participant objects to build live player contexts.
- H1 live collection now maps Kalshi player prop markets alongside game markets:
  - open Kalshi prop series are fetched for `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PM`, `KXNBASTL`, `KXNBABLK`, and `KXNBATO`
  - Real player contexts are extracted from per-game detail feeds and used to map live player-driven events to affected prop tickers
  - player-driven source rows now persist `mapped_prop_tickers` in addition to `mapped_game_tickers`
- Kalshi's live NBA prop parser now understands the production daily ticker format like `KXNBAPTS-26MAR29LACMIL-LACBLOPEZ11-10`, which was previously failing the prop mapping path even after player contexts were available.
- H1 runtime health now fails loudly if live mapped games exist but Oracle extracts zero player contexts or zero mapped prop tickers, instead of silently looking healthy on game markets alone.
- H1 now records partial prop-mapping diagnostics for the remaining unmapped live player contexts, with reason buckets for:
  - `no_open_kalshi_market`
  - `player_token_mismatch`
  - `team_mismatch`
  - `date_mismatch`
- `oracle-latency-report.py` now breaks out event-driven H1 capture by market type, so game-market and prop-market source/quote coverage can be audited separately instead of only looking at aggregate quote counts.
- H1 live-event classification now includes `injury_player_out`, so the event-class list in the matrix is fully represented in code.
- H1 execution reconciliation and reporting now use Oracle-linked rows only; unrelated generic ledger trades are excluded from Oracle fill-rate / EV summaries.
- H1 live collection is now running read-only against Real plus Kalshi production under `launchd`.
- Oracle shadow collection now also runs under a separate `launchd` service in `--demo` mode, so Stage 2 shadow rows can accumulate without opening a terminal.
- Shadow Book C scans now persist detailed zero-signal diagnostics into health state, and prolonged zero-signal live-opportunity runs emit a non-fatal health warning instead of failing silently.
- The collector now has:
  - Kalshi orderbook WebSocket state
  - `t+0 / +1s / +3s / +5s` quote captures
  - runtime health failures for zero mapping, stale/disconnected live sources, and mapped-event droughts
- Parallel Book C research now has an offline stored-feed analyzer. Current build priority from the `201` stored Real game feeds is:
  1. `clutch_comeback`
  2. `ot_likely`
  3. `blowout`
  4. `technical_foul`
- `clutch_comeback` is now wired into Oracle's live/shadow Book C scan path using:
  - Real home-feed discovery
  - Real per-game detail fetch for score / period / clock confirmation
  - Kalshi game-market orderbook quality checks before signal emission
- Current Book C live-scan boundary:
  - `clutch_comeback` is active on game markets
  - `ot_likely`, `blowout`, and `foul_trouble` are now active on prop markets via Real game-detail player box scores plus Kalshi prop orderbook checks
  - Book C now has a Real player/game WebSocket cache with REST fallback, so the remaining execution gap is resiliency/replay plus tighter execution telemetry rather than missing live-state plumbing
- Oracle alpha capture now records:
  - H1 source and quote rows from the live latency collector
  - Oracle bot signal rows for skipped, demo/shadow, and executed decisions
  - order-submission rows for demo/shadow and live executions
  - fill rows only when a real fill is observed
- Oracle alpha reconciliation now has an offline import path from the generic event ledger / trade logs into the Oracle alpha ledger for order and fill backfill.
- Oracle alpha reconciliation now also backfills settlement / close rows into the Oracle alpha ledger, including inferred fee and settlement-price fields when the source trade artifacts only retain partial close data.
- `oracle-latency-report.py` now includes a shadow execution section with:
  - signal count
  - order count
  - observed order fill rate
  - observed signal fill rate
  - median / p75 time-to-fill
  - conservative execution summary / pass-fail status that stays `insufficient_close_data` until settlement data exists
- `oracle-latency-report.py` now also includes:
  - daily H1 activity summaries
  - an H1 decision block with latency gates, fill-rate gates, and clustered-bootstrap EV / CLV checks
  - a `--daily-summary-only` mode for compact monitoring output
- Hourly Oracle alpha maintenance now writes:
  - `oracle-alpha-reconcile-latest.json`
  - `oracle-latency-report-latest.json`
  - `oracle-latency-report-latest.txt`
  - `oracle-h1-daily-summary-latest.json`
  - `oracle-h1-daily-summary-latest.txt`
- Offline Book C parameter sweeps now exist for:
  - `clutch_comeback`
  - `ot_likely`
  - `blowout`
  and are exposed through `src/kalshi/oracle-book-c-sweep.py` so threshold work can continue while live H1 data accumulates.
- Current processed feed gaps:
  - `foul_trouble` is available live, but offline stored-feed research is still blocked because per-player foul counts are not retained
  - `scoring_run` is blocked because scoring-run / possession text is not retained

## What counts as a real edge

A strategy is **not** "real" just because it has:

- a good story
- a positive backtest on synthetic prices
- one good night
- high model accuracy without executable PnL

A strategy is only "real" if it clears all of these:

1. **Executable**
   Uses real bid/ask, fees, depth, and fill assumptions.

2. **Out-of-sample**
   Positive results persist outside the tuning period.

3. **Statistically defensible**
   Net edge remains positive under clustered bootstrap or equivalent uncertainty checks.

4. **Stable**
   Results are not driven by one night, one player, one stat, or one extreme regime.

5. **Capacity-aware**
   There is enough fillable size to matter.

6. **Operationally durable**
   The edge survives realistic latency, quote fade, and data-source outages.

## Common proof standard

Use this standard for every test below.

### Entry and PnL conventions

- Enter `YES` at `yes_ask` unless you have evidence of passive fills.
- Enter `NO` at `1 - yes_bid` unless you have evidence of passive fills.
- Net EV must subtract:
  - exchange fees
  - expected slippage
  - cancellation/failure rate
  - adverse-selection cost

### Time discipline

- Every signal must be timestamped in UTC.
- Every feature must be available **as of** the decision time.
- No lookahead from final injury status, final lineups, or final prices.

### Statistical discipline

- Cluster by `game_id` or `market_ticker` when bootstrapping.
- Report:
  - sample size
  - mean EV per contract
  - median EV per contract
  - total PnL
  - CLV
  - 95% bootstrap confidence interval
- For live strategies, also report:
  - latency distribution
  - fill probability
  - time-to-fill

### Minimum evidence bar by stage

#### Stage 1: Research signal

- At least `200` historical opportunities
- Positive gross EV or consistent CLV

#### Stage 2: Shadow edge

- At least `100` shadow trades
- Positive net EV
- Positive CLV in at least `60%` of trading days

#### Stage 3: Small-capital live edge

- At least `100` live micro-sized fills
- Net EV confidence interval above `0`
- No single day contributes more than `25%` of PnL

#### Stage 4: Scale candidate

- At least `300` live fills
- Positive net EV after all fees and slippage
- Stable across at least `3` disjoint time windows
- Capacity above the minimum daily dollar target you care about

## Core metrics

### Net EV per contract

`net_ev = realized_payout - entry_cost - fees - slippage`

### CLV

Track both:

- `close_clv`: position value versus close
- `post_fill_clv_60s`: position value 60 seconds after fill

Why:

- close is useful for pregame
- 60-second CLV is more relevant for live latency trades

### Capture rate

`capture_rate = filled_signals / eligible_signals`

### Latency gap

`latency_gap = t_first_kalshi_move - t_real_event`

### Adverse selection

`adverse_60s = mid_60s_after_fill - entry_fair_value`

Compute directionally so worse immediate price movement after fill is negative.

### Stability score

A strategy fails stability if profits are concentrated in:

- one stat
- one side
- one player archetype
- one time bucket
- one date cluster

## Required event log schema

Every future shadow or live trade row should capture:

- `signal_id`
- `timestamp_utc`
- `book`
- `hypothesis_id`
- `market_ticker`
- `game_id`
- `player_id`
- `team`
- `stat`
- `side`
- `source_event_type`
- `source_event_timestamp_utc`
- `signal_timestamp_utc`
- `yes_bid`
- `yes_ask`
- `yes_bid_depth`
- `yes_ask_depth`
- `midpoint`
- `spread`
- `model_prob`
- `market_prob`
- `entry_price`
- `entry_type` (`aggressive` or `passive`)
- `filled`
- `fill_price`
- `fill_timestamp_utc`
- `close_price`
- `settlement`
- `gross_pnl`
- `net_pnl`

Without this, most strategy debates will stay unresolved.

## Ranked test matrix

## 1. H1: Real-to-Kalshi live event latency edge

### Thesis

Real App play and player updates arrive before Kalshi fully reprices the relevant market.

### Why this is ranked first

- It is the strongest plausible proprietary edge.
- It is hardest for generic stat vendors to replicate.
- It directly tests whether Book C is worth building.

### Data needed

- Real websocket events from `src/kalshi/domain/oracle/real_sports_client.py`
- Kalshi websocket or high-frequency orderbook snapshots
- Stored Real game feeds in `research/nba-props/data/processed/game_feeds/`
- Kalshi historical trades/candlesticks for validation

### Event classes to test

- scoring run
- foul trouble
- technical foul
- injury / player out
- clutch state entry
- blowout state entry
- OT-likely state

### Primary metrics

- median `latency_gap`
- 75th percentile `latency_gap`
- fillable edge at `t+0`, `t+1s`, `t+3s`, `t+5s`
- net EV per signal
- capture rate

### Pass criteria

- median latency gap `> 3s` for at least one event class
- 75th percentile latency gap `> 5s`
- `>= 200` labeled opportunities
- positive net EV with 95% clustered bootstrap CI above `0`
- fill rate `>= 20%` at realistic size

### Kill criteria

- median latency gap `< 1s` across all event classes
- fill rate `< 5%`
- post-fill adverse selection wipes out gross signal edge

### If it passes

- Build Book C first.

## 2. H2: Book A divergence edge, Real market price versus Kalshi

### Thesis

Real crowd/game-market prices and Kalshi game prices reflect different information sets or different crowd biases, and the divergence contains alpha.

### Data needed

- `get_game_markets()` and `get_market_detail()` from `real_sports_client.py`
- Kalshi game-market prices, orderbooks, and historical candles

### Design

- Align same game and same side across Real and Kalshi
- Record divergence at multiple horizons:
  - open
  - midday
  - pre-tip
  - late live
- Test raw divergence and filtered divergence by:
  - Real participation/volume
  - Kalshi spread/depth
  - game salience

### Primary metrics

- net EV
- CLV
- divergence decay speed
- edge by volume bucket

### Pass criteria

- `>= 300` matched market snapshots
- positive CLV and positive net EV
- edge remains after excluding wide-spread or thin Kalshi markets
- not entirely concentrated in one team/popularity bucket

### Kill criteria

- divergence mean-reverts before fills are possible
- no positive CLV after transaction costs
- signal vanishes once volume and spread filters are applied

### If it passes

- Book A becomes the first shadow-trading book.

## 3. H3: Late-game comeback overpricing on Kalshi

### Thesis

In the final minutes, the losing side is overpriced because traders overestimate comeback probability.

### Literature basis

- Yogi Berra bias
- late-settlement miscalibration in prediction markets

### Data needed

- Real live state or play-by-play
- Kalshi late-game quotes and trades

### Buckets

- `2:00` to `1:00`
- `1:00` to `0:30`
- `0:30` to `0:10`
- by margin `1`, `2`, `3-5`, `6-8`

### Primary metrics

- displayed probability minus empirical win rate
- net EV of fading comeback-side enthusiasm
- maker versus taker performance

### Pass criteria

- overpricing of at least `5` probability points in one or more late-game buckets
- positive net EV after fees on at least `200` opportunities
- effect survives excluding ultra-wide markets

### Kill criteria

- miscalibration exists but cannot be executed net of spread
- no stable effect after clustering by game

### If it passes

- Add a late-game Book C module with strict time and spread filters.

## 4. H4: Kalshi favorite-longshot bias in sports contracts

### Thesis

Low-priced YES contracts are systematically worse buys than their displayed probabilities imply.

### Why it matters

- This is directly relevant to binary sports contracts.
- It may support contrarian or maker-heavy strategies.

### Data needed

- Kalshi historical trades
- Kalshi historical candles
- settlement outcomes

### Buckets

- `0.01-0.05`
- `0.05-0.10`
- `0.10-0.20`
- `0.20-0.40`
- `0.40-0.60`
- `0.60-0.80`
- `0.80-0.95`
- `0.95-0.99`

### Primary metrics

- realized frequency minus implied probability
- net EV by bucket
- maker alpha versus taker alpha

### Pass criteria

- consistent underperformance in the low-price YES buckets
- effect remains after fees and spread filters
- maker style outperforms taker style

### Kill criteria

- favorite-longshot pattern exists in raw prices but disappears net of execution

### If it passes

- Build a market-making / passive-quote or contrarian fade module for selected buckets.

## 5. H5: Open-to-close injury / lineup / officials edge

### Thesis

The market underreacts or reacts slowly to official injury reports, projected lineups, confirmed lineups, and referee assignments.

### Why it matters

- Strong literature support from player-absence work
- likely highest-value pregame edge

### Data needed

- NBA official injury reports
- lineup feeds from SportsDataIO or equivalent
- NBA officials assignments
- Kalshi open, intermediate, and close prices
- optional sportsbook consensus

### Feature groups

- starter in/out
- minutes-restriction risk
- back-to-back rest management
- first-start / role change
- crew foul tendency

### Primary metrics

- price move after update
- CLV from trading immediately after update
- net EV by feature type

### Pass criteria

- `>= 200` event-driven opportunities
- positive CLV on the first post-update trade
- positive net EV after fees
- effect persists after excluding extreme news shocks that move all books instantly

### Kill criteria

- signal only exists in hindsight because updates are already reflected before Kalshi is tradable

### If it passes

- This becomes the top pregame strategy.

## 6. H6: Price-aware props in liquid stat classes only

### Thesis

Pregame props can work, but only in liquid stat classes and only with real price-aware modeling.

### Why this is not ranked higher

- The current model has predictive signal but not proven executable alpha.
- It is a second-order opportunity after latency and info-timing edges.

### Eligible stat classes

- assists
- points
- rebounds
- 3PT

Deprioritize for now:

- steals
- blocks

### Data needed

- `research/nba-props/data/processed/gamelogs`
- Kalshi historical prop prices and trades
- lineup and injury context
- optional sportsbook consensus

### Model families to compare

- raw empirical hit rate
- minutes x rate negative-binomial model
- hierarchical shrinkage model
- multi-source probability blend

### Primary metrics

- net EV
- CLV
- Brier versus settle
- calibration by stat and player archetype

### Pass criteria

- one of the liquid stat classes has positive net EV with CI above `0`
- outperforms raw hit-rate baseline
- not dependent on one side only
- stable over at least `3` disjoint date windows

### Kill criteria

- predictive Brier is acceptable but net EV is negative after real prices
- edge only exists in a tiny sample or one date cluster

### If it passes

- Build Book B only for the winning stat classes.

## 7. H7: Ladder-relative value and monotone-curve mispricing

### Thesis

Even if outright arbitrage is rare, ladder quotes can deviate enough from a monotone fitted curve to create relative-value trades.

### Data needed

- Kalshi orderbook snapshots
- Kalshi historical trades
- prop ladders by player/stat/game

### Model

- fit monotone probability curve across lines
- compare live quote to fitted fair value

### Primary metrics

- out-of-sample net EV of curve-deviation trades
- mispricing persistence
- sensitivity to 0.50 stale quotes

### Pass criteria

- mispricing remains after removing zero-ask or 0.50 midpoint artifacts
- positive net EV on `>= 150` trades

### Kill criteria

- all apparent edge disappears after filtering stale quotes and zero-depth artifacts

### If it passes

- Use as a secondary overlay, not the core strategy.

## 8. H8: Maker-versus-taker execution edge

### Thesis

The same forecast edge is much more profitable when entered passively than aggressively.

### Why it matters

- This may be the difference between a viable strategy and a dead one.
- It is especially relevant if competing against faster takers and market makers.

### Data needed

- Kalshi orderbook
- queue position where available
- fill timestamps
- fill outcomes

### Compare

- aggressive entry at ask / no-offer
- passive resting orders inside or at top of book
- hybrid entry: passive first, aggressive only if signal survives

### Primary metrics

- fill rate
- net EV
- fee capture / spread capture
- adverse selection by entry type

### Pass criteria

- passive style adds at least `1` cent per contract of net improvement
- fill rate remains high enough to keep opportunity capture acceptable

### Kill criteria

- passive style materially reduces fill rate with no net EV benefit

### If it passes

- prioritize FIX and queue-aware execution.

## 9. H9: Cross-market blend beats any one source

### Thesis

The best fair probability comes from combining:

- Oracle model
- Real crowd
- sportsbook consensus
- Kalshi current price

### Why it matters

- The literature favors information aggregation and smaller smarter crowds.
- Your likely edge is in combining different information sets.

### Data needed

- all of the above, aligned by timestamp

### Model

- logistic blend on logits
- isotonic or Platt recalibration
- compare against each single input alone

### Primary metrics

- Brier
- calibration
- net EV
- CLV

### Pass criteria

- blended model outperforms every standalone source on both calibration and net EV

### Kill criteria

- blend improves Brier but not net EV

### If it passes

- make this the core fair-value engine.

## 10. H10: Regime segmentation matters more than global tuning

### Thesis

Alpha is regime-specific, and global models dilute it.

### Regimes to test

- pregame versus live
- open versus close
- early season versus midseason versus post-trade-deadline
- high-salience versus low-salience games
- star versus role-player props
- high-liquidity versus low-liquidity markets

### Primary metrics

- interaction effects on net EV and CLV
- strategy stability within regime

### Pass criteria

- at least one regime materially outperforms the pooled baseline

### Kill criteria

- no regime meaningfully improves stability or EV

### If it passes

- stop training one global model and specialize by regime.

## Recommended build and test order

## Week 1

1. Build the canonical signal/quote/fill log schema.
2. Fix ticker parsing and market mapping.
3. Record Real event timestamps and Kalshi quote timestamps.
4. Backfill Kalshi historical candles and trades.

## Week 2

5. Run H1 latency study.
6. Run H2 Book A divergence study.
7. Run H5 injury/lineup/officials open-to-close study.

## Week 3

8. Run H3 comeback-overpricing study.
9. Run H4 longshot-bias study.
10. Run H8 maker-versus-taker study.

## Week 4

11. Run H6 price-aware props only for assists, points, rebounds, and 3PT.
12. Run H7 ladder-relative-value study.
13. Run H9 blend-model test.
14. Run H10 regime segmentation.

## Capital deployment rules

### Do not trade live size if

- no real fill logging exists
- no timestamp alignment exists
- no CLV measurement exists
- the edge depends on stale or impossible prices

### Shadow trade first if

- a thesis has positive historical EV but no live execution evidence

### Trade micro size only if

- historical pass criteria are met
- capture rate is adequate
- adverse selection is bounded

### Scale only if

- live micro-size trades clear the Stage 4 proof standard

## What I would personally bet on first

If the goal is highest probability of discovering a real edge quickly, I would prioritize:

1. H1: Real-to-Kalshi latency edge
2. H5: injury / lineup / officials open-to-close edge
3. H2: Book A divergence
4. H3: late-game comeback overpricing
5. H8: maker-versus-taker execution edge

I would only put current pregame props modeling after those.

## Final decision rule

At the end of these tests, the strategy stack should be categorized as one of:

### Category A: real edge

- live or shadow results clear the full proof standard

### Category B: research edge only

- predictive or structural signal exists, but not enough executable net EV

### Category C: no edge

- fails after realistic prices, fills, and uncertainty checks

Right now, Oracle is still in **Category B**.

This matrix is the shortest path I see to determining whether it can reach **Category A**.
