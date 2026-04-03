# Oracle NBA Research Verdict

Date: 2026-04-02 (updated 2026-04-03)
Data: 340,526 alpha ledger events (Mar 15 - Apr 3), 200 stored game feeds, 38 settled prop signals

---

> Correction (2026-04-02): the Apr 3 passive H8 PASS story was invalidated after rewriting H8 to evaluate deployable fixed-side policies. Current t+5s results are `passive_yes = -0.115c` EV/attempt and `passive_no = -0.113c`, both with confidence intervals below zero. Oracle should remain fail-closed until new evidence exists.

## Executive Summary

Five hypotheses tested. Current deployable result: no Oracle book is trading-ready.

| Hypothesis | Verdict | Key Finding |
|-----------|---------|-------------|
| **H1** Latency edge (taker) | **PARTIAL PASS** | Only `clutch_entry` events show positive markout (+0.535c, CI [+0.04, +0.88]). All other event classes are negative. |
| **H2** Crowd divergence | **DATA COLLECTING** | Crowd API was broken by JSON key bug (fixed). Live-game divergence 0.3-2.8% (too small). Need pregame data. |
| **H3** Comeback overpricing | **KILLED** | Model *underprices* trailing teams by 19-33pp. Fading comebacks would lose money. |
| **H6** Pregame props | **KILLED** | 0/32 NO-side wins. Model calibration catastrophically overconfident. Edge inversion. |
| **H8** Maker-vs-taker | **INVALIDATED** | The prior PASS depended on ex-post side selection and counting unrealized markout on unfilled passive orders. Deployable fixed-side passive policies are negative. |

**Key fixes:** The Real Sports crowd market API bug (`"markets"` vs `"gameMarkets"`) was fixed, and the H8 methodology was corrected to evaluate actual tradable policies rather than ex-post best-side outcomes.

**Current posture:** Keep Oracle disabled. H1's clutch-state signal remains a research lead, but H8 does not justify passive execution, Book B remains killed, and Book A still needs pregame evidence before any trading decision.

**Operational guardrails:** The current bounded-study gates and kill deadline live in `research/oracle-activation-gates-2026-04-02.md`. The daily operational artifact is `python3 src/kalshi/oracle-scorecard.py`.

---

## H1: Real-to-Kalshi Live Event Latency Edge

### Data
- 28,382 source events, 267,540 quote snapshots across 24 games
- 6,486 fillable t+5s paired opportunities

### Results by Event Class (t+5s, fillable)

| Event Class | N | Mean Markout | CI (95%) | Positive% | Verdict |
|------------|---|-------------|----------|-----------|---------|
| **clutch_entry** | **43** | **+0.535c** | **[+0.04, +0.88]** | **30.2%** | **PASS** |
| scoring_run | 3,552 | -1.156c | [-1.40, -0.95] | 3.7% | KILLED |
| injury_player_out | 2,549 | -1.171c | [-1.50, -0.90] | 6.3% | KILLED |
| blowout_entry | 296 | -1.169c | [-1.44, -0.97] | 0.0% | KILLED |
| technical_foul | 41 | -1.220c | [-1.55, -0.89] | 0.0% | KILLED |
| ot_likely_entry | 5 | -1.000c | [-1.00, -1.00] | 0.0% | KILLED |

### Key Diagnostics

| Dimension | Finding |
|-----------|---------|
| **Market type** | Game markets: -0.76c. Props: -4.56c. Props are 6x worse. |
| **Spread** | 0-4c spread: -0.92c. 5-8c spread: -9.36c. Only tight markets are tradeable. |
| **Period** | Q4: -0.75c (better). Q1-Q3: -1.23c. Late-game has smaller losses. |
| **Game state** | Clutch state: -0.03c (near zero). Competitive: -1.18c. Blowout: -1.07c. |
| **Direction** | YES entry: -2.17c. NO entry: -1.70c. Both negative; NO slightly better. |
| **Adverse selection** | t+1s: -1.33c, t+3s: -1.22c, t+5s: -1.15c. Market overshoots then reverts. |
| **Tight filter** | spread<=6c + depth>=10: -0.99c aggregate, but clutch_entry still +0.54c. |

### Interpretation

The ~1c negative markout across all non-clutch event classes equals approximately one bid-ask spread crossing. The information edge exists (Real events correlate with Kalshi price moves) but the spread consumption destroys it on aggressive entry. The market overshoots then partially reverts (t+1s worse than t+5s), which is consistent with a "momentum then reversion" microstructure.

The clutch_entry exception works because: (1) Q4 late-game generates maximum market uncertainty and repricing speed is slower, (2) the sample is small (43 observations, 5 games) so the signal may not survive scaling, (3) game markets (not props) have tighter spreads and deeper books in clutch moments.

### H1 Recommendation

Narrow all live Book C activity to:
- **Event class:** clutch_entry only (game state transitions into CLUTCH)
- **Market type:** game markets only (props are -4.56c)
- **Spread filter:** <= 6c spread, >= 10 depth
- **Period:** Q4 only

Shadow-trade this narrow filter for 20+ game nights to reach 200+ observations before going live.

---

## H3: Late-Game Comeback Overpricing

### Data
- 200 stored game feeds
- 915 clutch comeback opportunities across 8 time/margin buckets

### Empirical Win Rates vs Model

| Time | Margin | N | Trailing Wins | Model Prob | Overpricing |
|------|--------|---|---------------|------------|-------------|
| 1:00-0:30 | 1 pt | 109 | **38.5%** | 16.2% | **-22.3pp** |
| 1:00-0:30 | 2 pts | 105 | **35.2%** | 8.7% | **-26.5pp** |
| 1:00-0:30 | 3-5 pts | 153 | **32.7%** | 1.0% | **-31.7pp** |
| 1:00-0:30 | 6-8 pts | 97 | **22.7%** | 1.0% | **-21.7pp** |
| 0:30-0:10 | 1 pt | 107 | **40.2%** | 7.5% | **-32.7pp** |
| 0:30-0:10 | 2 pts | 96 | **34.4%** | 1.0% | **-33.4pp** |
| 0:30-0:10 | 3-5 pts | 148 | **31.1%** | 1.0% | **-30.1pp** |
| 0:30-0:10 | 6-8 pts | 100 | **20.0%** | 1.0% | **-19.0pp** |

**Negative overpricing = model underprices the trailing team.** The model says the trailing team has 1-16% chance; empirically they win 20-40% of the time.

### Interpretation

The H3 thesis ("trailing team is overpriced") is **exactly backwards**. The Book C `detect_clutch_comeback()` model severely underestimates comeback probability. A team down 1 point with 30-60 seconds left wins 38.5% of the time, but the model gives them only 16.2%.

This means:
1. Fading the trailing team (the H3 strategy) would **lose** money
2. The model formula `0.08 + 0.42*time_factor - 0.075*margin` is far too pessimistic
3. If anything, **buying** the trailing team in tight games might be profitable

### H3 Recommendation

KILLED. Do not fade comebacks. The model needs to be recalibrated with the empirical table above before any late-game game-market trades. The `clutch_entry` positive markout from H1 may relate to this -- if Kalshi underprices comebacks, then buying YES on the trailing team when clutch state enters could work. But this is a new thesis (H3-inverted), not the original H3.

---

## H6: Pregame Player Props (Book B)

### Data
- 300 generated signals, 38 settled against real Kalshi prices
- 86 experiment runs in the autoresearch pipeline

### Results by Stat Class

| Stat | Trades | Wins | Win% | P&L | ROI | Verdict |
|------|--------|------|------|-----|-----|---------|
| rebounds | 6 | 3 | 50.0% | +$0.67 | +30.1% | KEEP (tiny sample) |
| points | 11 | 3 | 27.3% | -$2.43 | -45.2% | KILL |
| assists | 10 | 0 | 0.0% | -$3.13 | -100% | KILL |
| three_pointers | 6 | 0 | 0.0% | -$1.95 | -100% | KILL |
| steals | 3 | 0 | 0.0% | -$1.16 | -100% | KILL |
| blocks | 2 | 0 | 0.0% | -$0.32 | -100% | KILL |

### Critical Findings

1. **NO-side catastrophe:** 0 wins out of 32 NO-side trades (-$10.54). The model thinks players will miss lines but they almost never do (on the buckets the model identifies).

2. **YES-side works:** 6/6 YES-side wins (+$2.21). Tiny sample, but directionally correct.

3. **Edge inversion:** The 6-10% edge bucket (small edge) has 100% win rate. The 30%+ edge bucket (high confidence) has 14.3% win rate. The model is most wrong when most confident.

4. **Calibration disaster:** At 90-100% model confidence, actual win rate is 20% (predicted: 97.5%). At 50-60% confidence, actual win rate is 5.9% (predicted: 43.7%).

5. **Brier score:** 0.3626 against real outcomes, which is **worse than a coin flip** (0.25). The autoresearch Brier of 0.19 is meaningless because it tests against synthetic lines, not real Kalshi prices.

### Root Cause

The weighted hit-rate model is structurally biased toward under bets. When a player has historically missed a line (e.g., assists < 4 in 8/10 games), the model assigns very high under probability (97%+). But:
1. Kalshi lines are set by the market, not by historical averages -- the line already reflects the player's track record
2. The model double-counts information already in the price
3. Variance events (hot games, OT, blowouts) break historical patterns more often than the model expects

### H6 Recommendation

KILLED as currently designed. The autoresearch pipeline has been optimizing a model that produces good synthetic-line Brier scores but catastrophically wrong real-market predictions. To resurrect:
1. **Drop NO-side entirely** -- only generate YES-side (over) signals until calibration is fixed
2. **Incorporate Kalshi price as a base rate** -- blend model probability with market probability instead of treating them independently
3. **Switch model family** -- minutes x rate negative binomial would capture the variance structure that the hit-rate model misses
4. **Restrict to rebounds only** -- the one stat class with (tiny) positive results
5. **Kill the autoresearch pipeline's Brier optimization** -- it's optimizing the wrong loss function against the wrong benchmark

---

## Consolidated Recommendations

### What to do now

1. **Keep Oracle fail-closed.** Leave the Oracle bot disabled in config. Leave Book C disabled, `passiveExecution` off, and both `propSignalsEnabled` and `clutchComebackEnabled` off until fresh evidence clears explicit go-live gates.

2. **Use the fixed execution path only for shadow/demo validation.** The demo recording bug and passive cancel-after-timeout gap are fixed, but that only makes validation possible. It is not evidence of profitability.

3. **Treat clutch comeback as research-only.** If Book C is reactivated for calibration, start with game markets only, props off, passive execution off, and require 100+ clean shadow signals plus measured post-fee EV before revisiting live capital.

4. **Stop the Book B autoresearch loop.** H6 is killed as currently designed. Any future Book B work needs a different model family, sportsbook/base-rate anchoring, and a YES-side-first design.

5. **Treat H8 as a separate market-making research track if revisited.** Any future maker work must be based on real demo fills, queue behavior, and cancel outcomes rather than quote-follow proxies.
   The bounded runner is `scripts/run-oracle-h8-maker-demo.sh`, and the resulting maker-study metrics roll into `python3 src/kalshi/oracle-scorecard.py`.

### What to stop doing

- Autoresearch parameter sweeps on Book B
- Book A divergence trading before pregame H2 data exists
- Broad Book C passive execution based on the old H8 PASS
- Book C prop signals as a live trading strategy
- NO-side prop trades (0/32 win rate)

### Shadow trading configuration

If you want to run a fresh Oracle calibration cycle:

1. Keep Oracle disabled by default. Re-enable only for a bounded demo/shadow study with:
   - Book C only
   - `propSignalsEnabled = false`
   - `clutchComebackEnabled = true`
   - `passiveExecution = false`
   - game markets only

2. Position sizing: minimum demo size only. Do not deploy real capital from this configuration.

3. Target: 100+ clean shadow signals, 50+ real demo orders if maker research resumes, measured cancel/fill behavior, and post-fee EV confidence above zero before any live trading discussion.
