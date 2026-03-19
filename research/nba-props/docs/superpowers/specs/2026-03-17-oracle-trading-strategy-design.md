# The Oracle: NBA Multi-Book Trading System for Kalshi

> **Date**: 2026-03-17 (v2 — major redesign)
> **Status**: Design approved, pending implementation
> **Scope**: NBA-focused, three independent trading books on Kalshi using Real Sports App as data source
> **Capital**: $1-5k bankroll, max 10 simultaneous positions across all books
> **Supersedes**: Risk parameters in `KALSHI_MAPPING.md` — this spec is the single source of truth.

---

## 1. Overview

The Oracle is a **three-book trading system**, not a single unified strategy. Each book has independent alpha generation, risk limits, edge thresholds, position sizing, and execution rules. The books share infrastructure (API clients, data pipeline, risk ledger) but are otherwise isolated.

**Why three books**: Cross-platform divergence, pregame player props, and live event trading have fundamentally different latency profiles, signal characteristics, fill behavior, and calibration needs. A single threshold and sizing rule across all three will misprice risk and overallocate to the noisiest signals.

| Book | Alpha Source | Latency Profile | Edge Character |
|---|---|---|---|
| **A: Game Divergence** | Real market price vs Kalshi price | Minutes (price discovery lag) | Wide, slow-moving, high conviction |
| **B: Pregame Player Props** | Real splits/stats vs Kalshi prop lines | Hours (pre-game edge) | Moderate, data-driven, medium conviction |
| **C: Live Events** | Real WebSocket signals vs Kalshi adjustment speed | Seconds (speed edge) | Narrow, time-sensitive, binary conviction |

**Critical design principle**: Kalshi prices are NEVER used as alpha inputs in the model. They appear only in two roles: (1) execution target (what price do we get?), and (2) risk overlay (how much exposure do we have?). Using Kalshi prices as predictive features creates endogenous circularity — the model would bake venue consensus into its fair value, then compare back to venue consensus, manufacturing false edge through double-counting.

---

## 2. Market Universe

### Player Props (Books B and C)

| Kalshi Series | Prop | Real Data Advantage |
|---|---|---|
| `KXNBAPTS` | Points O/U | Splits, per-opponent, home/away, pace, stat tracker odds |
| `KXNBAREB` | Rebounds O/U | Per-opponent, position matchup, stat tracker odds |
| `KXNBAAST` | Assists O/U | Pace, teammate availability, per-opponent |
| `KXNBA3PT` | 3-Pointers O/U | Last-5 shooting splits, opponent 3PT defense |
| `KXNBA2D` | Double-Double | Combined splits (pts+reb or pts+ast) |
| `KXNBA3D` | Triple-Double | Only when all three stats trending high |
| `KXNBABLK` | Blocks O/U | Per-opponent, rim protection matchup |
| `KXNBASTL` | Steals O/U | Per-opponent, pace-adjusted |

### Game-Level (Book A)

| Kalshi Series | Market | Real Data Advantage |
|---|---|---|
| `KXNBAGAME` | Game Winner | Real's crowd probability vs Kalshi's crowd probability |
| `KXNBASPREAD` | Spread | Same |
| `KXNBATOTAL` | Total O/U | Same |
| `KXNBATEAMTOTAL` | Team Total | Same |
| `KXNBA1HWINNER` | 1st Half Winner | Same |
| `KXNBA2HWINNER` | 2nd Half Winner | Same |

---

## 3. Book A — Real-vs-Kalshi Game Divergence

### Alpha Theory

Real Sports and Kalshi are two independent prediction markets with different participant pools. Real's prices reflect the Real crowd's view; Kalshi's prices reflect the Kalshi crowd's view. When they disagree significantly, one of them is wrong. We trade the cheaper side on Kalshi.

This is the cleanest external-prior book because Real's price IS the model output — no statistical model needed, just price comparison.

### Signal Generation

```python
def book_a_scan(real_markets, kalshi_markets):
    """Compare Real's implied probability to Kalshi's price for game-level markets."""
    for game in matched_games:
        real_prob = game.real_price / 100  # Real uses 0-100 percentage
        kalshi_price = game.kalshi_yes_price / 100  # Kalshi uses 1-99 cents

        edge = real_prob - kalshi_price

        # Only trust high-volume Real markets
        if game.real_volume < 200_000:
            continue

        if abs(edge) >= BOOK_A_MIN_EDGE:
            if edge > 0:
                signal = Signal(side='yes', edge=edge)  # Real thinks YES is underpriced
            else:
                signal = Signal(side='no', edge=abs(edge))  # Real thinks NO is underpriced
```

### Book A Parameters

| Parameter | Value | Rationale |
|---|---|---|
| Min edge threshold | $0.15 (15 cents) | Wider threshold — cross-platform divergence is noisier |
| Min Real volume | 200,000 | Only trust well-traded Real markets |
| Position sizing | Fixed fractional: 2% of bankroll per trade | Conservative — Real's price is not a calibrated model |
| Max positions (Book A) | 4 | Game-level markets are correlated within a night |
| Max total Book A exposure | 8% of bankroll | Limited because this is the least sophisticated book |
| Polling frequency | Every 30 seconds (Real REST + Kalshi REST) | Slow-moving edge, no need for speed |

### When NOT to trade Book A

- Real volume < 200k (thin crowd = unreliable signal)
- Game is already in progress (live prices are noisy, defer to Book C)
- Edge has been narrowing over the last 3 polls (converging = edge is pricing in)

---

## 4. Book B — Pregame NBA Player Props

### Alpha Theory

Use Real's rich player data — splits, per-opponent history, home/away performance, game-by-game season feed, stat tracker prop lines/odds, and teammate availability — to build an independent fair-value estimate for each player prop. Compare to Kalshi's price. Trade when our estimate disagrees by enough.

**Critical rule**: No Kalshi-derived data enters the alpha model. Kalshi prices are used ONLY at the execution step to calculate edge and fill orders.

### Data Pipeline (Pre-Game, T-2 hours)

```
FOR EACH player on tonight's slate:

1. EMPIRICAL DISTRIBUTION (core prior)
   Source: GET /players/{id}/sport/nba/seasonfeed?limit=20&season=2025
   → Fetch last 20 game-by-game stat lines
   → Build empirical CDF for each stat (pts, reb, ast, 3pt, etc.)
   → This gives the ACTUAL distribution, not a normal approximation

2. RECENCY ADJUSTMENT
   Source: GET /players/{id}/sport/nba?season=2025 → splits
   → Compare last-5 average to season average
   → If last-5 > season by > 1 std dev → upward shift
   → If last-5 < season by > 1 std dev → downward shift

3. MATCHUP ADJUSTMENT
   Source: Player profile per-opponent splits + team stat leaders
   → Player's per-opponent history (if >= 3 games)
   → Opponent defensive ranking for this stat category
   → statId discovery: GET /teamstatleaders/nba/seasons → cache stat category IDs

4. OPPORTUNITY ADJUSTMENT
   a) Minutes: from splits or box score history
   b) Pace: from Real team stat leaders (team PPG as proxy)
   c) Teammate availability: Real player status Active/Out/Questionable
   d) Back-to-back: computed from Real schedule

5. VENUE ADJUSTMENT
   Source: Player profile home/away splits
   → Home vs away delta for this stat

6. EXTERNAL ODDS CONTEXT (from Real stat trackers)
   Source: GET /stattrackers?day={today}&sport=nba
   → Real exposes overOdds/underOdds for each prop line
   → These are sportsbook-sourced odds, NOT Kalshi prices
   → Use as a Bayesian prior: if external books say over is -130 (56.5%),
     and our model says 62%, we have independent confirmation
   → If external odds strongly disagree with our model (>10% gap),
     flag for review — our model may be wrong
```

### Fair Value Computation

Instead of a hand-weighted 6-factor linear blend (which is mathematically questionable), use an **empirical distribution approach**:

```python
def book_b_fair_value(player_id, stat, line, context):
    """
    Build fair value from empirical game-by-game distribution,
    adjusted by matchup/venue/opportunity factors.
    """
    # Step 1: Get last 20 game stat values from season feed
    recent_games = fetch_season_feed(player_id, limit=20)
    stat_values = [g.stats[stat] for g in recent_games]

    # Step 2: Apply adjustments as shifts to the distribution
    shift = 0.0

    # Matchup adjustment
    opp_def_rank = get_opponent_defensive_rank(context.opponent, stat)
    if opp_def_rank >= 25:       # Bottom 5 defense
        shift += 0.08 * mean(stat_values)  # +8%
    elif opp_def_rank <= 5:      # Top 5 defense
        shift -= 0.08 * mean(stat_values)  # -8%

    # Per-opponent history override (if enough data)
    opp_history = get_per_opponent_splits(player_id, context.opponent)
    if opp_history and opp_history.games >= 3:
        opp_avg = opp_history.average[stat]
        season_avg = mean(stat_values)
        shift += (opp_avg - season_avg) * 0.5  # blend toward opponent-specific

    # Venue adjustment
    home_avg = get_home_away_split(player_id, 'home')[stat]
    away_avg = get_home_away_split(player_id, 'away')[stat]
    venue_delta = (home_avg - away_avg) if context.is_home else (away_avg - home_avg)
    shift += venue_delta * 0.3  # partial credit

    # Opportunity: B2B penalty
    if context.is_back_to_back:
        shift -= 0.06 * mean(stat_values)  # -6% on B2B

    # Opportunity: Pace adjustment
    pace_factor = context.matchup_pace / league_avg_pace
    shift += (pace_factor - 1.0) * mean(stat_values)  # proportional to pace differential

    # Opportunity: Teammate out → usage boost for points,
    #              usage reduction for assists
    if context.key_teammate_out:
        if stat == 'pts':
            shift += 2.5  # ~2-3 point boost
        elif stat == 'ast':
            shift -= 1.5  # fewer assist opportunities

    # Step 3: Compute hit rate from adjusted distribution
    adjusted_values = [v + shift for v in stat_values]
    hit_rate = sum(1 for v in adjusted_values if v > line) / len(adjusted_values)

    # Step 4: Recency weighting — weight recent games more
    # Last 5 games get 2x weight, last 6-10 get 1.5x, last 11-20 get 1x
    weights = [2.0]*5 + [1.5]*5 + [1.0]*10
    weights = weights[:len(adjusted_values)]
    weighted_hits = sum(w * (1 if v > line else 0)
                       for w, v in zip(weights, adjusted_values))
    weighted_total = sum(weights[:len(adjusted_values)])
    weighted_hit_rate = weighted_hits / weighted_total

    # Step 5: Cross-reference with external odds (from Real stat trackers)
    tracker = get_stat_tracker(player_id, stat, context.date)
    if tracker and tracker.over_odds:
        external_prob = implied_probability(tracker.over_odds)
        # Bayesian blend: 70% our model, 30% external odds
        fair_value = 0.70 * weighted_hit_rate + 0.30 * external_prob
    else:
        fair_value = weighted_hit_rate

    return fair_value
```

### Lineup Gating Rules

NBA prop repricing is dominated by late availability information. These hard rules prevent trading on stale lineup assumptions:

```
LINEUP GATES (Book B):

1. QUESTIONABLE PLAYERS
   - If the prop player's status is "Questionable": NO TRADE
   - Wait for status to change to "Active" or "Out"
   - If still "Questionable" at T-30 min before tip: skip this player entirely

2. KEY TEAMMATE STATUS
   - If a key teammate (top-3 usage on team) is "Questionable":
     NO TRADE on any prop for players on that team
   - Reason: teammate playing/sitting changes usage distribution for everyone

3. NO-TRADE WINDOW
   - No NEW Book B orders within 15 minutes of tip-off
   - Reason: late lineup changes can flip edge; we can't reprice fast enough
   - Exception: if a "Questionable" player is officially confirmed Active/Out
     in the last 15 min, allow ONE trade on that player's props (edge is fresh)

4. MINUTES CAPS
   - If a player's last-5 minutes average is < 20 min: NO TRADE on any prop
   - Reason: low-minutes players have too much variance; they're one
     coaching decision from a 12-minute night
   - Exception: if player has been consistently starting (>28 min) and one
     recent low-minutes game was a blowout, discard that outlier

5. OFFICIAL STARTERS
   - Ideal: confirm starting lineup before trading
   - Real doesn't expose starting lineups directly
   - Proxy: if player's last-5 average > 28 minutes, treat as presumed starter
   - If player's minutes are 20-28 (rotation player), apply 50% position size reduction
```

### Book B Parameters

| Parameter | Value | Rationale |
|---|---|---|
| Min edge threshold | $0.10 (10 cents) | Tighter than Book A — better calibrated alpha |
| Position sizing | Fixed fractional: 1.5% of bankroll per trade | Conservative for v1 heuristic model |
| Max positions (Book B) | 6 | Diversified across players/games |
| Max per-game exposure (Book B) | 4% of bankroll | Correlated: blowout/OT affects all game props |
| Max per-player exposure (Book B) | 2.5% of bankroll | Injury wipes all player props |
| Execution | Limit orders only, never market orders | Pregame = patient, no speed pressure |
| External odds agreement | Flag if model disagrees with stat tracker odds by >10% | Sanity check, not blocking |

---

## 5. Book C — Live Event Trading

### Alpha Theory

Trade a **narrow set of hard signals** where Real's Socket.io feed is demonstrably faster than Kalshi repricing. This is NOT a general live model — it's a discrete event book that fires only on specific, validated triggers.

**Critical rule**: Book C does NOT use a continuous probability update model. The v1 live model (naive per-minute rate switching after 8 minutes) would chase hot starts and overtrade. Instead, Book C only trades on discrete state transitions with clear mechanical edge.

### Validated Signal Set (v1 — start narrow, expand with data)

Only these three signals are active in v1. Each has a clear mechanical reason why Kalshi lags:

#### Signal 1: Foul Trouble

```
TRIGGER: Star player accumulates 4th personal foul before Q4
DETECTION:
  Primary: LiveFeedSocketPlayersUpdated → check pf/personalFouls field
  Fallback: LiveFeedSocketPlaysAdded → parse "foul" in description, maintain local counter
  Secondary fallback: Poll GET /playerboxscores/{id}?version=2 every 45s

WHY KALSHI LAGS: Most Kalshi traders are not monitoring individual foul counts.
  The player's over/under props should drop 15-25 cents because:
  - Coach will limit minutes to avoid 5th foul → fewer stats
  - Player plays passively to avoid fouling → less aggressive scoring

ACTION:
  - SELL (buy NO) on all active over props for this player
  - BUY (buy YES) on under props if available

EDGE: 15-25 cents expected
DECAY: Edge lasts ~60-120 seconds as Kalshi adjusts
```

#### Signal 2: Overtime Transition

```
TRIGGER: Game score tied with < 2:00 remaining in Q4
DETECTION: LiveFeedSocketPlaysAdded → score + clock parsing

WHY KALSHI LAGS: Prop lines are set for regulation. OT adds 5 minutes of
  starter play → ~15% boost to all counting stats. Kalshi doesn't reprice
  until OT actually starts (2+ minute window).

ACTION:
  - BUY (yes) on over props for BOTH teams' stars
  - Only props where current accumulation + expected OT production clears the line

VALIDATION BEFORE TRADE:
  Player has current_stat + (per_minute_rate * 5_min_OT_estimate) > line * 1.05
  (require 5% margin — don't trade marginal cases)

EDGE: 10-20 cents expected
DECAY: Edge exists from "tied < 2 min" until OT tip-off (~3-5 min window)
```

#### Signal 3: Blowout Minutes Cut

```
TRIGGER: Score differential >= 20 points at any point in Q3 or later
DETECTION: LiveFeedSocketPlaysAdded → score differential parsing

WHY KALSHI LAGS: When a game is a 20+ point blowout by Q3, the losing team's
  starters AND winning team's starters will sit most of Q4 (replaced by bench).
  Props were set for 34-36 minutes; stars may only play 26-28.

ACTION:
  - SELL (buy NO) on over props for BOTH teams' starters
  - Only trade players who haven't already cleared the line

VALIDATION:
  Player's current stat < line AND
  Projected remaining minutes < 10 (blowout scenario)

EDGE: 10-15 cents expected
DECAY: Edge lasts until Kalshi reprices (1-3 minutes)
```

### Execution Rules (Book C — Speed-Optimized)

Book C execution is fundamentally different from Books A and B:

```
BOOK C EXECUTION:

1. HIGH-FREQUENCY TARGETED READS
   - When a Book C trigger fires, immediately poll Kalshi for the
     specific ticker(s) affected — don't wait for the next polling cycle
   - Use up to 5 read/s on specific tickers (within 20 read/s budget)

2. QUOTE QUALITY CHECKS (before every order)
   - Quote age: orderbook must be < 5 seconds old (re-fetch if stale)
   - Spread check: bid-ask spread must be < 8 cents; wider = illiquid, skip
   - Displayed depth: at least 5 contracts on the side we're taking; less = skip
   - If ANY check fails, do NOT trade — the edge may be illusory

3. ORDER TYPE
   - Limit orders at best ask (or best bid for sells), NOT market orders
   - Market orders on thin books will get terrible fills and leak all the edge
   - If our limit isn't filled in 15 seconds, cancel and re-assess
   - Max 2 re-prices; after that, the edge has likely priced in

4. SLIPPAGE ATTRIBUTION
   - Log for every Book C trade:
     - Signal timestamp (when we detected the event)
     - Kalshi quote at signal time
     - Kalshi quote at fill time
     - Fill price
     - Slippage = fill_price - quote_at_signal
   - If average slippage exceeds 3 cents over 20+ trades,
     the signal is not fast enough — disable it

5. CANCEL DISCIPLINE
   - If the signal condition reverses (e.g., blowout narrows to < 15),
     cancel any unfilled Book C orders immediately
   - Never leave stale Book C orders sitting
```

### Book C Parameters

| Parameter | Value | Rationale |
|---|---|---|
| Min edge threshold | $0.12 (12 cents) | Moderate — need to cover slippage + fees |
| Position sizing | Fixed fractional: 1% of bankroll per signal | Smallest book — highest noise |
| Max positions (Book C) | 3 | Narrow signal set, fast in/out |
| Max total Book C exposure | 3% of bankroll | This is the riskiest book |
| Execution | Limit orders with 15s timeout, quote checks | Speed matters but fills must be clean |
| Signal validation | Mechanical checks per signal type | No continuous model — discrete events only |
| Slippage monitoring | Track and disable signals if avg slippage > 3c | Self-correcting |

### Expansion Path (v2+)

After collecting 100+ Book C paper trades with full slippage data:
- Evaluate which signals actually have speed edge (positive P&L net of slippage)
- Consider adding: teammate ejection/injury, clutch mode (Q4 < 3 min, competitive)
- Consider adding a **Bayesian shrinkage live model** for general live prop trading:

```
Bayesian Shrinkage (v2 concept — NOT in v1):

Instead of switching to tonight's raw per-minute rate after 8 minutes:
  shrunk_rate = (n * tonight_rate + k * prior_rate) / (n + k)

  where:
    n = minutes played tonight (grows during game)
    k = shrinkage strength (tuned, ~15-20)
    tonight_rate = current_stat / minutes_played
    prior_rate = season per-minute rate adjusted for matchup/venue

  Early game (n=5): prior dominates → no hot-start chasing
  Mid game (n=20): balanced → reasonable projection
  Late game (n=35): tonight dominates → mostly arithmetic

  Combined with role-conditioned minutes estimation:
    starter → 32-38 min
    rotation → 20-28 min
    situational → 10-18 min

  And separate state transitions for foul/blowout/OT
  (already implemented as Book C discrete signals)
```

---

## 6. Risk Management

### Portfolio-Wide Limits

All limits as percentages of current bankroll. Dollar examples assume $2k.

| Parameter | Limit | Example ($2k) | Rationale |
|---|---|---|---|
| Max total capital at risk | 18% of bankroll | $360 | Conservative for v1 heuristic model |
| Max simultaneous positions | 10 (all books combined) | 10 | Manageable; reduces correlation risk |
| Daily stop-loss | 5% of bankroll | -$100 | Stop all trading, review model |
| Max drawdown (cumulative) | 15% before full halt | -$300 | Halt system, re-evaluate everything |

### Per-Book Limits

| Parameter | Book A | Book B | Book C |
|---|---|---|---|
| Max positions | 4 | 6 | 3 |
| Max total exposure | 8% | 9% | 3% |
| Per-market exposure | 2% | 1.5% | 1% |
| Per-game exposure | 4% | 4% | 2% |
| Per-player exposure | n/a | 2.5% | 1.5% |
| Min edge (gross) | $0.15 | $0.10 | $0.12 |
| Sizing method | Fixed 2% | Fixed 1.5% | Fixed 1% |

### Why NOT Half-Kelly in v1

The spec previously used half-Kelly. This is wrong for a v1 heuristic model on a small bankroll:

1. **Kelly requires accurate probability estimates**. Our v1 model is a hand-weighted heuristic with no live calibration data. Kelly amplifies calibration errors — if the model is overconfident, Kelly sizes too large, accelerating ruin.

2. **Correlated positions**. Kelly assumes independent bets. NBA props for players in the same game are correlated (blowout, OT, pace all affect every prop). Kelly on correlated bets overallocates.

3. **Small bankroll**. With $2k, a few max-Kelly losses can destroy the bankroll before the law of large numbers kicks in.

**v1 approach**: Fixed fractional sizing (1-2% per trade). Simple, robust, and won't blow up on calibration errors. Graduate to fractional Kelly (0.1x Kelly, then 0.25x Kelly) only after collecting 200+ live trades with verified calibration.

### Correlation Guards

- Never hold YES on both sides of a spread
- Max 2 props per player across all books
- Max 4 prop positions in the same game across all books
- If Book A holds a game-level position and Book B/C holds player props in the same game, reduce Book B/C sizing by 40%
- Track directional correlation: if 3+ positions all benefit from "high-scoring game," enforce per-game cap

### Lineup Gates (Portfolio-Wide)

```
NO-TRADE RULES:

1. Player status "Questionable" → no trades on that player (Book B)
2. Key teammate "Questionable" → no trades on that team's players (Book B)
3. T-15 min to tip-off → no NEW Book B orders (late lineup changes)
4. Player last-5 avg < 20 minutes → skip (too much variance)
5. Rotation player (20-28 min avg) → 50% position size reduction
```

---

## 7. Player & Market Matching

### Discovering Tonight's Players

1. Fetch tonight's schedule: `GET /home/nba/next?cohort=0`
2. Fetch stat trackers: `GET /stattrackers?day={today}&sport=nba` — returns player names, prop lines, and **overOdds/underOdds** (external odds context)
3. Player lookup table (SQLite): `{player_name, team}` → `{real_player_id}`. Populated via `GET /players/sport/nba/search?season=2025` on first encounter. Cached permanently.

**Pre-game API budget**: ~104 calls (90 player profiles + 12 team data + 2 meta), ~21 seconds at 200ms each.

### Matching Real Players to Kalshi Tickers

1. Query Kalshi `GET /markets?series_ticker=KXNBAPTS&status=open` — parse full player name from `title` field
2. Fuzzy match to Real's player names (handle Jr./III, hyphenated, nicknames)
3. Store mapping `{real_player_id}` → `{kalshi_ticker_pattern}` in SQLite
4. Refresh daily

### statId Discovery

Query `GET /teamstatleaders/nba/seasons` at startup to get stat category → numeric ID mapping. Cache permanently. Expected categories (verify at runtime):

| Stat Name | Used For |
|---|---|
| Points Per Game | Points prop matchup |
| Rebounds Per Game | Rebounds prop matchup |
| Assists Per Game | Assists prop matchup |
| 3-Pointers Per Game | 3PT prop matchup |
| Opponent Points Per Game | Defensive rating |

---

## 8. Daily Operating Cycle

```
T-2 hours (before first game):
  ├── Fetch tonight's slate from Kalshi + Real
  ├── Discover player IDs (lookup table + search fallback)
  ├── Pull player profiles, splits, season feeds from Real
  ├── Fetch stat trackers (prop lines + external odds)
  ├── Apply lineup gates: exclude Questionable players
  ├── Discover statId mappings (if not cached)
  ├── Match Real players to Kalshi tickers
  ├── Run Book A: compare Real game prices to Kalshi game prices
  ├── Run Book B: compute prop fair values from Real data
  ├── Identify edges, queue limit orders
  └── Log FULL opportunity set (all model outputs, not just triggered trades)

T-15 min (lineup gate):
  ├── Re-check all player statuses
  ├── Cancel Book B orders for players now Questionable
  ├── Place final Book B orders for players confirmed Active
  └── No new Book B orders after this point

T-0 (tip-off):
  ├── Open Socket.io connection to Real (LiveFeed + PlayerBoxScore)
  ├── Start Kalshi price polling (every 10 seconds for Book A/B monitoring)
  ├── Book C event detector activates
  └── Monitor existing positions for exit signals

T+ongoing (during games):
  ├── Book C: watch for foul trouble / OT / blowout triggers
  ├── On Book C trigger: targeted Kalshi read → quote check → order
  ├── All books: monitor positions for exit rules
  ├── Log all signals (triggered and NOT triggered) for validation
  └── Enforce risk limits continuously

T+final (games end):
  ├── Positions settle on Kalshi ($1 or $0)
  ├── Record per-trade: predicted edge, fill price, slippage, outcome
  ├── Bucket results by book (A, B, C separately)
  ├── Update calibration log
  └── Generate daily P&L report by book

Weekly:
  ├── Recalibrate Book B distribution shifts
  ├── Review Book C slippage data — disable unprofitable signals
  ├── Evaluate expanding Book C signal set
  ├── Adjust edge thresholds per book based on realized vs predicted
  └── Review full opportunity set: are we missing profitable signals?
```

---

## 9. Validation & Performance Measurement

### Why Win Rate Alone Is Wrong

For a mixed-price book (trades at $0.30 and trades at $0.70), win rate is misleading. A 60% win rate on $0.70 contracts is much worse than 45% on $0.30 contracts.

### Required Metrics (tracked per book)

| Metric | Definition | Target |
|---|---|---|
| **Brier Score** | Mean squared error of probability forecasts: `mean((predicted - outcome)^2)` | < 0.20 |
| **Log Loss** | `-mean(outcome * log(predicted) + (1-outcome) * log(1-predicted))` | < 0.60 |
| **Realized Edge** | Average (model_prob - fill_price) on winning trades | Positive per book |
| **Quoted vs Realized Edge** | Compare pre-trade edge estimate to post-settlement actual edge | Within 3 cents |
| **CLV (Closing Line Value)** | Compare our entry price to the price at game end. If we consistently buy below closing price, we have real edge. | Positive |
| **Slippage** (Book C) | Average (fill_price - quote_at_signal_time) | < 3 cents |
| **P&L by Book** | Net profit per book after fees | Positive per book |
| **ROI by Book** | P&L / capital_at_risk per book | > 0% per book |
| **Calibration Curve** | Bin predicted probabilities (50-60%, 60-70%, etc.) and compare to actual hit rates | Within 5% per bin |

### Validation Phases

```
PHASE 1: Paper Trading (Kalshi Demo API)
  Duration: Minimum 4 weeks or 150+ trades (whichever is later)
  Capital: $3,447 demo balance
  Books: All three active
  Gate to Phase 2:
    - Positive P&L in each book independently
    - Brier score < 0.22 (slightly relaxed for small sample)
    - No individual book has > 25% drawdown
    - Book C slippage < 4 cents average
    - Calibration curve within 8% per bin

PHASE 2: Small Live Trading (Kalshi Production)
  Duration: 4 weeks
  Capital: $500 (subset of bankroll)
  Books: Book B only first (best data-driven edge)
  Sizing: 50% of normal (0.75% per trade instead of 1.5%)
  Gate to Phase 3:
    - Positive P&L on Book B
    - CLV consistently positive
    - Calibration within 5% per bin
    - Then add Book A (2 more weeks)
    - Then add Book C (2 more weeks)

PHASE 3: Full Deployment
  Capital: Full bankroll ($1-5k)
  Books: All three at normal sizing
  Ongoing: Weekly metric review, monthly strategy audit

LOG EVERYTHING:
  - Every model output (not just triggers)
  - Every Kalshi quote at signal time
  - Every fill price and slippage
  - Every lineup gate decision (why we didn't trade)
  - Every game state transition
  - Full opportunity set for backtesting
```

---

## 10. Key Statistical Correlations Exploited

| Correlation | Direction | Trading Implication |
|---|---|---|
| Pace ↔ All counting stats | Strong positive | High-pace = inflate all props |
| Minutes ↔ All counting stats | Strongest predictor | +1 min = ~0.7 pts, ~0.3 reb, ~0.2 ast |
| Teammate absence ↔ Usage | + for pts, - for ast | Buy points over, sell assists over |
| Blowout ↔ Star minutes | Strong negative | Book C signal: sell overs |
| Back-to-back ↔ Performance | -5-8% | Discount all props on B2B |
| Opponent pace ↔ Player stats | Strong positive | Fast opponent = more possessions |
| Foul trouble ↔ Minutes | Strong negative | Book C signal: sell overs |
| OT ↔ Counting stats | +15% boost | Book C signal: buy overs |

---

## 11. Data Sources Summary

### Real Sports App (Signal Source — NO Kalshi data enters alpha models)

| Data | Endpoint | Used By | Frequency |
|---|---|---|---|
| Player profile + splits | `GET /players/{id}/sport/nba?season=2025` | Book B | Pre-game, cache 5 min |
| Player season feed | `GET /players/{id}/sport/nba/seasonfeed?limit=20&season=2025` | Book B | Pre-game, cache 5 min |
| Player box scores | `GET /playerboxscores/{id}?version=2` | Book B (minutes fallback) | Pre-game |
| Stat trackers + odds | `GET /stattrackers?day={date}&sport=nba` | Book B (external odds prior) | Pre-game |
| Game markets (prices) | `GET /predictions/gamemarkets/nba` | Book A | Pre-game + every 30s |
| Team standings | `GET /teamstandings/sport/nba/...` | Book B (situational) | Pre-game, cache 15 min |
| Team rankings | `GET /rankings/sport/nba/entity/team/ranking/{period}` | Book A | Pre-game, cache 5 min |
| Team stat leaders | `GET /teamstatleaders/nba/seasons/2025/...` | Book B (matchup) | Pre-game, cache 15 min |
| Schedule | `GET /home/nba/days?type=condensed` | All (B2B detection) | Daily |
| Tonight's games | `GET /home/nba/next?cohort=0` | All (slate) | Pre-game |
| Live feed (Socket.io) | `https://web.realsports.io` LiveFeed | Book C | Real-time |
| Player box score (Socket.io) | `https://web.realsports.io` PlayerBoxScore | Book C | Real-time |

### Kalshi (Execution Venue — prices used for execution and risk only)

| Data | Endpoint | Used By | Frequency |
|---|---|---|---|
| Market list + prices | `GET /markets?series_ticker=KXNBA*&status=open` | All (execution) | Pre-game + 10s polling |
| Market orderbook | `GET /markets/{ticker}/orderbook` | Book C (quote checks) | On signal (targeted) |
| Place order | `POST /portfolio/orders` | All | On signal |
| Cancel order | `DELETE /portfolio/orders/{id}` | All | On re-price/exit |
| Positions | `GET /portfolio/positions` | All (risk) | Every 30s |
| Balance | `GET /portfolio/balance` | All (risk) | Every 60s |

---

## 12. Failure Modes & Recovery

### WebSocket Disconnection

| Scenario | Detection | Recovery |
|---|---|---|
| Socket.io disconnect | `disconnect` event | Auto-reconnect. Re-subscribe. |
| No data 30+ seconds | Heartbeat watchdog | Mark STALE. Fall back to REST 5s. No new Book C trades. |
| Reconnect fails 3x | Counter | REST-only mode. Book C disabled. Log alert. |

### Kalshi API Failures

| Scenario | Detection | Recovery |
|---|---|---|
| Order rejected (400) | HTTP 400 | Log. No retry. |
| Rate limited (429) | HTTP 429 | Back off 2s. Reduce polling 50%. |
| Auth failure (401) | HTTP 401 | Re-sign. If still failing, halt all books. |
| Timeout (5s) | No response | Retry once. Then mark DOWN, no new orders. |

### Kill Switch Triggers

1. Daily stop-loss hit (-5% of bankroll)
2. 3+ consecutive order failures
3. Real WebSocket AND REST both down
4. Position tracking disagrees with Kalshi by > 2 contracts
5. Any single book draws down > 10% of its allocation in one session

---

## 13. Tech Stack

- **Language**: Python 3.11+ (scipy, asyncio)
- **Real API client**: httpx (async) + python-socketio
- **Kalshi API client**: httpx + cryptography (RSA-PSS)
- **Probability**: scipy.stats for distributions, empirical CDF
- **Scheduling**: asyncio event loop
- **Storage**: SQLite (trade log, calibration, player lookup, statId cache, opportunity log)
- **Configuration**: YAML (per-book risk params, edge thresholds, signal toggles)
- **Logging**: Structured JSON logs with full opportunity set
