# Oracle / NBA Alpha Research Audit

Date: 2026-03-19

## Bottom line

I do **not** have super-high confidence yet that the current stack has a proven, durable edge against funds, market makers, or strong sports quants.

I **do** have medium confidence that there are real alpha candidates in this repo, but they are **not** the current NBA props autoresearch loop. The strongest threads are:

1. Real App live data latency and game-state edge against Kalshi repricing.
2. Real App crowd / game-market prices versus Kalshi game prices.
3. Price-aware pregame props, but only after replacing synthetic proxy pricing with real Kalshi history.
4. Pregame lineup / injury / official-assignment timing edge.
5. Execution edge: tighter routing, queue awareness, and potentially Kalshi FIX.

The current Book B parameter sweep work is not where the alpha is.

## What the repo says right now

### Oracle is not live-consuming the research

- `src/kalshi/apps/oracle_bot.py` exits unless `oracle.enabled` is set.
- `config/bots-config.json` currently has no `oracle` section.
- `scan_book_a()`, `scan_book_b()`, and `scan_book_c()` are still placeholders returning `[]`.

Implication: even if the research were strong, Oracle is not currently monetizing it.

### The production Kalshi NBA ticker parser is broken for real props

- `src/kalshi/domain/oracle/nba_ticker_utils.py` expects older synthetic tickers like `KXNBAPTS-18MAR26-LALJAMESL-O27`.
- The collected Kalshi data uses real tickers like `KXNBAPTS-26MAR18PORIND-PORDCLINGAN23-15` and `KXNBAAST-26MAR18GSWBOS-BOSPPRITCHARD11-4`.
- Real tickers currently fail the production parser.

Implication: even before modeling, the real market mapping path is broken.

### The exported calibration is stale and misleading

- `config/oracle-calibration.json` still advertises `brier_score = 0.094`.
- That number came from widened test offsets in `research/nba-props/results.tsv`, not the currently locked realistic setup in `research/nba-props/run_experiment.py`.

Local rerun on 2026-03-19:

- Locked baseline: `brier 0.1941`, `calibration 0.0261`, `ROI +5.8%`, `sample 93,313`, `trades 8,030`
- Current tuned params: `brier 0.1936`, `calibration 0.0215`, `ROI -24.1%`, `sample 93,313`, `trades 233`

Implication: tiny probabilistic improvement, no robust trading improvement.

### The main props backtest is still optimizing the wrong target

- `research/nba-props/analysis/backtester.py` uses a proxy market price derived from uniform hit rate, not real Kalshi executable prices.
- `research/nba-props/analysis/kalshi_backtest.py` uses actual settled outcomes, but still hardcodes `0.50` entry pricing for the PnL logic.

Implication: the current research does not yet answer the only question that matters:

"Did we beat real Kalshi prices net of spread, fees, and fills?"

## Local empirical findings

### 1. The simple pregame model has some predictive signal, but not a proven trading edge

Current `kalshi_backtest_report()` output on stored settled Kalshi props:

- Settled markets: `971`
- Matched to gamelogs: `679`
- Brier: `0.1940`
- Calibration error: `0.0920`
- Directional accuracy: `0.718`

Per-stat on that matched sample:

| Stat | Count | Brier | Accuracy |
| --- | ---: | ---: | ---: |
| assists | 150 | 0.1677 | 0.753 |
| blocks | 60 | 0.1892 | 0.717 |
| points | 168 | 0.1961 | 0.725 |
| steals | 126 | 0.2067 | 0.722 |
| rebounds | 175 | 0.2069 | 0.680 |

Interpretation:

- There is enough predictive structure to justify continued work.
- The best current pregame stat bucket appears to be **assists**.
- But the PnL number from this module is not usable because it still assumes `0.50` fills.

### 2. The saved live-ish signal run was bad

`research/nba-props/data/signal_results/results_20260318_212834.json`:

- Trades: `38`
- Win rate: `15.8%`
- Model accuracy: `44.7%`
- ROI: `-58.8%`

Filtering did not rescue it:

- Depth `>= 10`: `28` trades, `-51.9%` ROI
- Spread `<= 0.05`: `29` trades, `-66.4%` ROI
- Edge `>= 0.20`: `22` trades, `-74.3%` ROI

Important detail:

- `YES` trades: `6` trades, `100%` win rate, `+61.1%` ROI
- `NO` trades: `32` trades, `0%` win rate, `-100%` ROI

Interpretation:

- This sample is too small to prove a stable asymmetry.
- But it strongly suggests the current live signal path is not market-safe, especially on unders / `NO`.

### 3. Kalshi NBA orderbooks look more tradeable than expected

From stored snapshots in `research/nba-props/data/processed/kalshi_orderbooks/`:

Best full snapshot (`snapshot_20260318_193041.json`):

- Markets: `956`
- Markets with depth: `955`
- Median spread: `0.05`
- Median displayed depth: `1000`
- Markets with spread `<= 0.05`: `537`
- Markets with spread `<= 0.10`: `656`

By stat on that snapshot:

| Stat | Markets | Median Spread |
| --- | ---: | ---: |
| rebounds | 240 | 0.03 |
| points | 266 | 0.04 |
| assists | 190 | 0.04 |
| 3PT | 119 | 0.05 |
| steals | 78 | 0.185 |
| blocks | 63 | 0.62 |

Interpretation:

- **Points / rebounds / assists / 3PT** are viable research and trading targets.
- **Steals / blocks** are much wider and look structurally worse for capture.

### 4. Ladder incoherence exists, but it is not yet the main edge

On `snapshot_20260318_193041.json`:

- Adjacent-line midpoint monotonicity violations: `102 / 731` adjacent pairs (`14.0%`)
- Of those, only `15` were non-`0.50`/non-stale style midpoint violations
- True executable bid/ask cross violations: only `9`, and those were mostly zero-ask artifacts

Interpretation:

- Ladder structure is noisy and worth scanning.
- But the clean, immediately executable arbitrage is limited.
- This looks more like a **relative-value and stale-quote ranking signal** than a core standalone strategy.

### 5. Book C is under-researched, not data-starved

Stored Real game-feed summaries:

- Games collected: `201`
- Median key snapshots per game: `29`
- Games with clutch moments: `124`
- Games with blowout moments: `73`
- Games with technical fouls: `129`

Interpretation:

- You already have enough structure to begin a first real Book C backtest.
- Book C is probably a higher-expected-value research direction than more Book B parameter sweeps.

## Highest-alpha threads, ranked

### 1. Real App live event latency edge versus Kalshi repricing

Why it matters:

- This is the most plausible edge that is hardest for slower participants to copy.
- Your repo already has a reverse-engineered Real client with REST and WebSocket hooks for:
  - game markets and prices
  - market detail with price history
  - live plays
  - player box score updates
  - game updates
  - market updates

Evidence in repo:

- `src/kalshi/domain/oracle/real_sports_client.py` supports:
  - `/predictions/gamemarkets/{sport}`
  - `/predictions/marketembed/{market_id}`
  - `/games/{game_id}/sport/{sport}/feed`
  - websocket events: `LiveFeedSocketPlaysAdded`, `LiveFeedSocketPlayersUpdated`, `PlayerBoxScoreUpdated`, `GameUpdated`, `GameMarketUpdated`

Confidence:

- Medium-high that this is the best alpha candidate.
- Low confidence that it is already proven.

What must be measured next:

- Event timestamp from Real
- First Kalshi quote move after the event
- Time to first executable fill
- Quote fade / adverse selection after fill

### 2. Real App crowd/game-market prices versus Kalshi game prices

Why it matters:

- This is the cleanest "different market, different information set" signal.
- It avoids the hardest part of prop modeling.

Why it beats current Book B work:

- Book A can exploit disagreement between two price formation systems.
- Current Book B mostly tries to squeeze more value from recency-weighted hit rates versus synthetic benchmarks.

Confidence:

- Medium.
- Not proven because it has zero real backtest coverage in this repo.

### 3. Pregame props, but only with real Kalshi prices and only in the best submarkets

Where the current evidence points:

- Assists have the best matched predictive metrics in the stored settled sample.
- Points and rebounds are liquid enough to matter.
- Steals and blocks look much less attractive due to wide spreads.

Recommended submarket priority:

1. assists
2. points
3. rebounds
4. 3PT
5. steals
6. blocks

Confidence:

- Medium that there is some alpha here after measurement is fixed.
- Low confidence that the current implementation captures it.

### 4. Pregame lineup / injury / official-assignment timing edge

Why it matters:

- A lot of prop mispricing happens around role clarity, starting status, minutes uncertainty, and late scratches.
- These edges are easier to systematize than deep player micro-models.

What matters most:

- projected lineups
- confirmed lineups
- official injury reports
- depth-chart movement
- officials assignments

Confidence:

- Medium.
- Probably most useful in the `9:00 a.m. ET` to tipoff window.

### 5. Execution edge on Kalshi

This is not alpha generation by itself, but it determines whether signal alpha survives contact with the market.

Why it matters:

- The stored orderbook snapshots suggest many NBA prop markets are actually tradable.
- Competing against better execution stacks without measuring queue position, fill rate, and quote fade is a losing setup.
- Kalshi now has official historical data and FIX support, including RFQ and market-maker features.

Confidence:

- High that this matters operationally.
- Medium that it adds enough incremental value to justify priority immediately after signal measurement.

Sources:

- https://docs.kalshi.com/getting_started/historical_data
- https://docs.kalshi.com/fix/index

## What does **not** currently look like the edge

### Book B parameter sweeps against synthetic prices

This has already produced diminishing and possibly negative returns.

### Paying for duplicate slow stat feeds before exploiting Real

If a vendor is 15-20 seconds behind broadcast and you already have a faster live website-derived feed, that vendor is primarily a redundancy / data-quality / pregame feature product, not a direct live edge product.

### Steals and blocks as main tradable pregame prop focus

Based on stored orderbooks, spreads are too wide versus points / rebounds / assists / 3PT.

### Midpoint-only ladder arbitrage

There is noise, but the clean executable cross count is small and dominated by stale / zero-ask artifacts.

## Other sources that could add significant edge

### Free or low-cost

#### 1. Kalshi official historical and market data

Use for:

- real historical prop prices
- real candlesticks
- real historical trades
- orderbook and websocket-based execution research

Why it matters:

- This is the missing benchmark for the current research stack.
- It removes the excuse for proxy pricing.

Source:

- https://docs.kalshi.com/getting_started/historical_data
- https://docs.kalshi.com/api-reference/market/get-market-candlesticks
- https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks
- https://docs.kalshi.com/api-reference/historical/get-historical-trades

#### 2. NBA official injury report

Use for:

- late scratch detection
- rest management
- pregame minutes uncertainty

Why it matters:

- The official report is updated throughout the day and directly drives line movement.

Source:

- https://official.nba.com/nba-injury-report-2025-26-season/

Verified 2026-03-19 notes from the page:

- teams must report by the afternoon before games
- game-day reports are required again on game day
- reports are updated on a continual basis throughout the day

#### 3. NBA official referee assignments

Use for:

- pace / foul-rate / whistle-style features
- foul trouble and free-throw expectation priors

Why it matters:

- Official assignments are published around `9:00 a.m. ET`.
- That creates a predictable pregame feature update.

Sources:

- https://official.nba.com/page/69/
- https://official.nba.com/page/44/

#### 4. NBA official last two minute reports archive

Use for:

- referee tendency features
- late-game foul / whistle style study
- Book C clutch-state context

Why it matters:

- It is not a direct trade feed, but it can help parameterize late-game live models.

Source:

- https://official.nba.com/2025-26-nba-officiating-last-two-minute-reports/

#### 5. The Odds API

Use for:

- cheap sportsbook consensus snapshots
- bookmaker comparison
- historical player-prop and event-odds snapshots at coarse frequency

Why it matters:

- Good value for line-comparison research and historical backfill.
- Not likely fast enough to be a direct live edge against serious participants.

What the official docs say:

- current odds available on free plans
- player props available through event-odds
- historical additional markets, including player props, available from 2023-05-03
- historical snapshots are available at 5-minute intervals

Sources:

- https://the-odds-api.com/
- https://the-odds-api.com/sports/nba-odds.html
- https://the-odds-api.com/historical-odds-data/
- https://the-odds-api.com/liveapi/guides/v4/

### Paid and potentially high-value

#### 6. SportsDataIO NBA

Use for:

- official-style injury / lineup / depth-chart layer
- live game state and play-by-play
- props coverage with line-movement history
- clean integration and entity mapping

Why it matters:

- Strong for the pregame window.
- Also useful as a redundancy / validation layer if the Real feed breaks.

What the official workflow guide says:

- live game state and live play-by-play are typically `15-20 seconds` behind TV
- projected lineups are available by `9:00 a.m. ET`
- confirmed lineups are typically confirmed within minutes of official release
- betting endpoints include props and line movement

Interpretation:

- Excellent **pregame** and **data-quality** source.
- Not likely faster than Real for live-event alpha.

Sources:

- https://sportsdata.io/developers/workflow-guide/nba
- https://sportsdata.io/cart/free-trial/nba

#### 7. Sportradar NBA + Odds Comparison + Synergy

Use for:

- low-latency structured game data
- bookmaker odds comparison
- possession and play-type data
- advanced projections and possession-based modeling

High-value products to care about:

- NBA Push Statistics: real-time team and player stats for all live games
- Odds Comparison Live Odds API: odds comparison across books, updated every `15-30 seconds`
- Synergy Basketball API: possession events, play-type stats, lineup possessions
- Synergy advanced player projections: forward-looking per-game projections

Why it matters:

- This is the premium option if you want to compete more like an actual data team.
- Especially valuable if you decide to build minutes x role x possession models or book-consensus fair values.

Interpretation:

- Strongest paid data option.
- Most likely overkill until Oracle is measuring real edge and execution correctly.

Sources:

- https://developer.sportradar.com/basketball/v7/reference/push-statistics
- https://developer.sportradar.com/sportradar-updates/changelog/odds-comparison-live-odds-api
- https://developer.sportradar.com/basketball/reference/get_advanced-basketball-league-playerprojections
- https://developer.sportradar.com/basketball/reference/global-basketball-push-events

### Borrowed / reverse-engineered / scraped

#### 8. Real Sports website API

Use for:

- real-time crowd and game-market prices
- market detail and price history
- live plays and player updates
- live game and market events

Why it matters:

- This is likely your single best current edge source.
- It is differentiated from commodity stat feeds.

Costs / risks:

- breakage risk
- auth churn
- possible terms / sustainability risk
- no contractual SLA

Interpretation:

- Highest potential alpha source in the current stack.
- Should be isolated behind one adapter and paired with redundancy.

Repo evidence:

- `src/kalshi/domain/oracle/real_sports_client.py`

#### 9. NBA official webpages as parsed feeds

Use for:

- injuries
- officials
- officiating archives

Why it matters:

- These are cheap, official, and relevant.
- They are not enough on their own, but they are worthwhile structured features.

Costs / risks:

- page layout changes
- scrape maintenance

## What I would pay for first

If budget is constrained:

1. Nothing new until the real-price Kalshi backtest exists.
2. Keep Real Sports as the primary live edge source.
3. Add either SportsDataIO or The Odds API for lineup / injury / book-consensus support.
4. Ask Kalshi about FIX only if execution quality becomes the bottleneck.

If budget is moderate:

1. Keep Real.
2. Add SportsDataIO for projected and confirmed lineups, injury normalization, and prop line movement.
3. Add The Odds API or equivalent low-cost odds comparator for sportsbook consensus.

If budget is serious:

1. Keep Real.
2. Add Sportradar Odds Comparison and either Push Statistics or Synergy, depending on whether you want:
   - better live structured data
   - better possession / projection modeling
3. Explore Kalshi FIX / institutional execution.

## Recommended next research / build order

### P0

1. Replace the synthetic-price backtest with official Kalshi historical prices and forward quote logging.
2. Fix the real NBA ticker parser and market mapping.
3. Build a latency study:
   - Real event time
   - first Kalshi move
   - first executable fill
   - fill quality after signal

### P1

4. Backtest Book A using Real game-market prices versus Kalshi game prices.
5. Backtest Book C using the `201` stored game feeds.
6. Add lineup / injury / officials features to the pregame pipeline.

### P2

7. Rebuild Book B around real prices and focus only on:
   - assists
   - points
   - rebounds
   - 3PT
8. Drop steals and blocks until spreads justify them.
9. Add a book-consensus comparison layer.

## Final assessment

The current NBA props researcher is useful as a **research sandbox** and for generating negative evidence.

It is **not** currently the main edge.

The edge, if it exists, is much more likely to come from:

- Real live data
- Real market-price backtesting
- execution quality
- pregame lineup / injury state
- selective submarket focus

Until the stack can prove alpha against real Kalshi prices and real fills, it is not reasonable to claim a durable edge against strong funds or market makers.
