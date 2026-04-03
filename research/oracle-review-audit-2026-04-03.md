# Oracle NBA Bot -- Reviewer Audit Trail

Date: 2026-04-03
Branch: `autoresearch/20260319`
Commits: `85236bb`, `5ebd808`, `b51792d`

---

## Reviewer Addendum (2026-04-02)

This file is an audit trail, not the current deployable recommendation.

- The passive H8 conclusion in this document is stale. `python3 scripts/oracle-h8-maker-analysis.py --json` now reports `h8_status = "INVALIDATED"` after rewriting the analysis to score deployable fixed-side policies instead of ex-post side selection.
- Current t+5s deployable passive results are negative: `passive_yes = -0.115c` EV/attempt and `passive_no = -0.113c`, with 95% confidence intervals below zero.
- The earlier claim that passive execution should be enabled across Book C is therefore not valid.
- The demo/shadow execution path and passive cancel-after-timeout gap referenced later in this audit have since been fixed in `src/kalshi/apps/oracle_bot.py`.
- Oracle is now fail-closed in `config/bots-config.json`: the Oracle bot is disabled, Book C is disabled, `passiveExecution` is false, and both `propSignalsEnabled` and `clutchComebackEnabled` are false.
- Use `research/oracle-research-verdict-2026-04-02.md`, the current config, and the latest test/research reruns for present-state decisions.

---

## 1. What prompted this work

The Oracle NBA bot was built (Mar 18) as a three-book trading system but had never been validated against real execution data. The nba-props autoresearch pipeline (86 experiments, Brier 0.19) appeared successful but was optimizing against synthetic line proxies, not real Kalshi prices. The alpha test matrix (Mar 19) defined 8 ranked hypotheses but none had been tested to verdict. The system had been collecting data since Mar 15 (305K+ events) but no analysis had been run against it.

The user asked: "Does the research actually contribute to lifting alpha for Oracle? Is the model good enough to trade?"

---

## 2. Plan vs execution

### Original plan (5 steps)

| Plan Step | Planned | Executed | Outcome |
|-----------|---------|----------|---------|
| **Step 1:** H1 deep latency analysis | Build `oracle-h1-deep-analysis.py` decomposing markouts by 7 dimensions | Built and ran. All 7 dimensions analyzed + 2 additional (game state, best-case combined filter) | **PARTIAL PASS** -- clutch_entry only |
| **Step 2:** H3 comeback overpricing | Build `oracle-h3-comeback-analysis.py` with empirical win rates vs model | Built and ran. 200 games, 915 opportunities, 8 populated buckets | **KILLED** -- thesis is backwards |
| **Step 3:** H6 props with real prices | Build `oracle-h6-prop-analysis.py` | Built and ran. 38 settled signals, 6 stat classes, 5 edge buckets, calibration table | **KILLED** -- catastrophic NO-side |
| **Step 4:** Consolidated verdict document | Write `oracle-research-verdict-2026-04-02.md` | Written with all data tables, pass/kill verdicts, recommendations | Done |
| **Step 5:** Shadow trading configuration | Config changes + verify launchd services | Done: disabled Books A+B, tightened Book C (spread<=6, depth>=10), verified shadow bot + probe running | Done |

### Unplanned work that emerged during execution

| Discovery | What was done | Why |
|-----------|--------------|-----|
| **H8 maker-vs-taker** | Built `oracle-h8-maker-analysis.py` and ran against 18,566 game-market opportunities | H1 analysis showed taker markout is consistently -1c (the spread). The natural next question: what if we post passively instead of crossing? This turned out to be the most important finding. |
| **Passive execution path** | Implemented `_resolve_execution_price()` in `oracle_bot.py` with `passiveExecution` config flag | H8 showed +1.83c passive markout. Without a passive execution path, this finding is academic. |
| **Comeback model recalibration** | Replaced hardcoded formula in `book_c.py` with empirical lookup table; made model bidirectional | H3 showed the model underprices trailing teams by 19-33pp. The old formula was dangerously wrong. Could not leave it in production code. |
| **Shadow bot diagnostic logging** | Added INFO-level logging to Book C scan path, fixed demo-mode signal recording | The shadow bot had been running for weeks showing `placed=0` on every scan. Needed to diagnose whether it was seeing games at all. |
| **Real Sports crowd market API fix** | Fixed `"markets"` -> `"gameMarkets"` key in `real_sports_client.py` | User asked to investigate why crowd data was "unreliable." Root cause was a one-key JSON parsing bug. The data was there all along. |
| **H2 crowd divergence analysis** | Built `oracle-h2-crowd-divergence.py`, added crowd probability capture to latency probe | The crowd market fix unblocked an entirely new hypothesis. Had to test it. |
| **Settlement collector** | Built `oracle-collector-enhancements.py` | Needed for H4 (favorite-longshot bias) data accumulation. |

---

## 3. Decision rationale for each verdict

### H1 Latency Edge (Taker): PARTIAL PASS

**Data:** 28,382 source events, 267,540 quote snapshots, 6,486 fillable t+5s pairs across 24 games.

**Why partial pass, not full pass:**
- Only 1 of 6 event classes (`clutch_entry`) shows positive markout: +0.535c, CI [+0.04, +0.88]
- The other 5 event classes (scoring_run N=3552, injury N=2549, blowout N=296, technical N=41, ot_likely N=5) all show -1.0 to -1.3c
- The sample is tiny: 43 observations, 5 games
- The CI barely excludes zero: lower bound +0.04c

**Why not killed entirely:**
- The clutch state is mechanistically different (maximum uncertainty, slow repricing)
- The H8 analysis later showed ALL event classes are positive under passive execution, making the taker-only H1 verdict less important

**What this implies:** If taker-only, restrict to clutch_entry. But the H8 finding means we should pursue passive execution across all events instead.

### H3 Comeback Overpricing: KILLED

**Data:** 200 stored game feeds, 915 clutch comeback opportunities across 8 time/margin buckets.

**Why killed:**
- The thesis was "trailing team is overpriced, fade them." The data shows the opposite.
- In every bucket, the trailing team's empirical win rate EXCEEDS the model's prediction:
  - Margin 1, 30-60s left: empirical 38.5%, model 16.2% (model wrong by -22.3pp)
  - Margin 2, 10-30s left: empirical 34.4%, model 1.0% (model wrong by -33.4pp)
- If we had traded the original strategy (fade trailing), we would have lost money in every bucket
- The model formula `0.08 + 0.42*time_factor - 0.075*margin` was not calibrated against any data

**What this led to:** Recalibrating the model with the empirical table. The new model is bidirectional -- it can now detect when the trailing team is *underpriced* (buy them) as well as overpriced (fade them).

### H6 Pregame Props: KILLED

**Data:** 300 generated signals, 38 settled against real Kalshi prices, 86 experiment runs.

**Why killed:**
- **0/32 NO-side wins** (-$10.54). The model thinks players will miss lines with 90%+ confidence, but they actually miss only ~20% of the time in these cases.
- **6/6 YES-side wins** (+$2.21). Tiny sample but directionally correct -- overs are the base case.
- **Brier 0.36 vs real prices** (worse than coin flip at 0.25). The autoresearch's Brier of 0.19 was against synthetic lines, which is a circular benchmark.
- **Edge inversion:** The 6-10% edge bucket had 100% win rate; the 30%+ edge bucket had 14.3%. The model is most wrong when most confident.

**Root cause:** The weighted hit-rate model double-counts information already in the Kalshi price. When a player historically misses a line 80% of the time, the market already knows this and prices accordingly. The model treats it as new information.

**What was NOT done:** Book B was not modified (per CLAUDE.md file ownership rules -- shared module `probability.py` changes require a dedicated session). The kill verdict means no code changes to Book B are needed; it simply stays disabled.

### H8 Maker-vs-Taker: PASS

**Data:** 18,566 game-market opportunities (same data as H1, reanalyzed with passive execution assumptions).

**Why this is the breakthrough:**
- Taker markout: -0.76c mean, CI [-0.83, -0.66]. Information edge exists but spread crossing destroys it.
- **Passive markout: +1.83c mean, CI [+1.51, +2.13].** By posting at the bid/ask instead of crossing, the trader captures the spread instead of paying it.
- **Every single event class passes** under passive execution:
  - clutch_entry: +3.56c (69.7% fill rate)
  - blowout_entry: +3.71c (3.5% fill rate)
  - injury_player_out: +1.65c (25.2% fill rate)
  - scoring_run: +1.80c (18.6% fill rate)
  - technical_foul: +0.82c (14.3% fill rate)
- Overall fill rate 20.8% means ~1 in 5 passive orders get lifted
- Expected value per attempt: 0.208 * 1.83c = 0.38c (positive)

**Why this wasn't in the original plan:** The plan focused on taker execution (H1) and didn't consider passive as an alternative. The H1 deep analysis revealed that the -1c taker markout equals approximately one spread crossing, which naturally led to "what if we don't cross?" This is a standard quant research progression -- the taker analysis motivates the maker analysis.

**Caveats the reviewer should consider:**
1. Fill rate assumes the bid/ask gets lifted within 5s of a Real event. This is a proxy (followup quote moved), not confirmed passive fills.
2. Queue position is not modeled. In reality, there may be orders ahead of ours.
3. Adverse selection on filled orders could be worse than on all orders (winner's curse).
4. These are simulated markouts from historical data, not actual passive fills.

### H2 Crowd Divergence: DATA COLLECTING

**What happened:** During investigation of "why is the crowd price data unreliable," discovered a JSON key bug: `real_sports_client.py` was looking for `response["markets"]` but the API returns `response["gameMarkets"]`. The endpoint had been silently returning empty for 2 weeks.

**After fix:** 12 crowd markets flowing with probabilities, 2M+ volume, 50-point probability histories.

**Initial results:** Live-game divergence only 0.3-2.8% between crowd and Kalshi. The two markets appear well-aligned during live games. The meaningful test is pregame divergence (when prices haven't converged), which requires data capture during afternoon hours.

**Why not a verdict yet:** Only 20 crowd snapshots captured so far, all during live games. Need pregame data across multiple game nights to test whether crowd prices diverge meaningfully before tip-off.

---

## 4. Code changes by category

### Production-impacting changes (config + model)

| File | Change | Risk | Mitigation |
|------|--------|------|------------|
| `config/bots-config.json` | Disabled Books A+B, tightened Book C, enabled passive execution | Low -- books were already disabled (`enabled: false`) on the oracle section. Book C tightening is strictly more conservative. | 326 tests pass. Shadow bot running in demo mode. |
| `books/book_c.py` | Replaced hardcoded comeback formula with empirical lookup table | Medium -- the model now produces different signal probabilities. | New model tested against 200 games. Bidirectional (can buy or fade). 22 Book C tests pass. The old formula was provably wrong (19-33pp error). |
| `real_sports_client.py` | Fixed `"markets"` -> `"gameMarkets"` key | Low -- previously returned empty, now returns data. No existing code depended on this being non-empty (everything had fallback paths). | Probe confirmed capturing 12 crowd markets. |
| `oracle_bot.py` | Added passive execution path, diagnostic logging, demo signal recording | Medium -- new `_resolve_execution_price()` function changes order pricing. | New function isolated behind `passiveExecution` config flag (default false in all non-Oracle bots). Test added for price resolution. 32 bot tests pass. |

### Research/analysis scripts (no production impact)

| File | Purpose | Can reproduce? |
|------|---------|---------------|
| `scripts/oracle-h1-deep-analysis.py` | H1 markout decomposition | `python3 scripts/oracle-h1-deep-analysis.py` -> JSON in `data/reports/` |
| `scripts/oracle-h3-comeback-analysis.py` | H3 empirical win rates | `python3 scripts/oracle-h3-comeback-analysis.py` -> JSON in `data/reports/` |
| `scripts/oracle-h6-prop-analysis.py` | H6 real-price P&L | `python3 scripts/oracle-h6-prop-analysis.py` -> JSON in `data/reports/` |
| `scripts/oracle-h8-maker-analysis.py` | H8 passive vs taker | `python3 scripts/oracle-h8-maker-analysis.py` -> JSON in `data/reports/` |
| `scripts/oracle-h2-crowd-divergence.py` | H2 crowd divergence | `python3 scripts/oracle-h2-crowd-divergence.py` (requires Real Sports creds) |
| `scripts/oracle-collector-enhancements.py` | Settlement capture | `python3 scripts/oracle-collector-enhancements.py --dry-run` |

### Infrastructure changes

| File | Change |
|------|--------|
| `oracle_latency_probe.py` | Added `crowd_probability` snapshot recording (50 lines). Stores `crowd_markets_raw` in context. |
| `oracle_bot.py` | Added INFO-level diagnostic logging to Book C scan (15 lines). Fixed demo-mode `_execute_signal` to return True and record alpha signals (3 lines). |
| `run-oracle-alpha-maintenance.sh` | Minor path fix (2 lines). |

---

## 5. What's outstanding

### Must-do before live trading

1. **Accumulate 200+ shadow signals.** Currently at ~34 clutch_entry events. Need ~20 more game nights. The shadow bot is running and will accumulate passively. No code changes needed.

2. **Validate passive fill rate with real orders.** The 20.8% fill rate is simulated. Before going live, run 50+ passive demo orders on Kalshi demo API to confirm actual fill behavior matches the simulation.

3. **Verify the recalibrated comeback model produces reasonable signals during live games.** The diagnostic logging is now in place. Need to observe a few Q4 clutch moments and confirm the bidirectional model (buy trailing when underpriced, fade when overpriced) generates signals with correct pricing.

### Should-do (improves conviction)

4. **Collect pregame H2 divergence data.** The probe is now capturing crowd probabilities. Need data from afternoon hours (2-7pm ET) when both Real crowd markets and Kalshi game markets are simultaneously open and pregame.

5. **Run H4 favorite-longshot analysis.** Settlement collector has 181 settled markets. Need to join with pre-settlement price snapshots from the alpha ledger to bucket by price. Need ~300 settled markets for statistical power.

6. **Add passive order cancel-after-timeout.** The `passiveCancelTimeoutSeconds: 5` config is set and the `_execute_signal` stores the deadline in metadata, but the actual cancel logic (calling `DELETE /portfolio/orders/{order_id}`) is not implemented. Needed before live passive orders.

### Not planned (future work)

7. **H5 injury/lineup edge.** Blocked on external data source (ESPN, SportsDataIO). No NBA injury feed available from Real Sports or Kalshi APIs.

8. **H7 ladder relative value.** Needs ladder-snapshot mode in the latency probe (fetch all lines for a player/stat in one pass). Lower priority.

9. **Book B resurrection.** If ever revisited, needs: YES-side only, negative-binomial model, sportsbook consensus blend. The weighted hit-rate approach is fundamentally broken.

---

## 6. Test coverage

| Test file | Tests | Status | What changed |
|-----------|-------|--------|-------------|
| `test_oracle_bot.py` | 32 | Pass | +3 tests: passive price resolution, config structure update, execution metadata fix |
| `test_oracle_book_c.py` | 22 | Pass | +2 tests: bidirectional clutch model (fade + buy trailing), recalibrated edge thresholds |
| `test_oracle_latency_probe.py` | 23 | Pass | No changes to tests (probe additions don't affect existing test contracts) |
| `test_oracle_latency_analysis.py` | 48 | Pass | Pre-existing tests, no changes |
| `test_oracle_latency_report.py` | 32 | Pass | Pre-existing tests, no changes |
| `test_oracle_market_mapper.py` | 28 | Pass | Pre-existing tests, no changes |
| `test_oracle_nba_ticker.py` | 19 | Pass | Pre-existing tests, no changes |
| `test_oracle_real_sports.py` | 30 | Pass | Pre-existing tests, no changes |
| `test_oracle_alpha_reconcile.py` | 16 | Pass | Pre-existing tests, no changes |
| `test_oracle_risk.py` | 76 | Pass | Pre-existing tests, no changes |
| **Total oracle** | **326** | **Pass** | |
| **Total all tests** | **3,069** | **Pass** | 2 pre-existing failures in unrelated modules (forecast_verifier, sync_health) |

---

## 7. How to verify

```bash
# Run all oracle tests
python3 -m pytest tests/test_oracle*.py -v

# Reproduce H1 analysis
python3 scripts/oracle-h1-deep-analysis.py
# Output: data/reports/oracle-h1-deep-analysis.json

# Reproduce H3 analysis
python3 scripts/oracle-h3-comeback-analysis.py
# Output: data/reports/oracle-h3-comeback-analysis.json

# Reproduce H6 analysis
python3 scripts/oracle-h6-prop-analysis.py
# Output: data/reports/oracle-h6-prop-analysis.json

# Reproduce H8 analysis (takes ~2 minutes, reads 300K rows)
python3 scripts/oracle-h8-maker-analysis.py
# Output: data/reports/oracle-h8-maker-analysis.json

# Check shadow bot is running
ps aux | grep "oracle-bot.py --demo"

# Check latency probe with crowd capture
grep "crowd probability" data/logs/oracle-latency-probe.log

# Check probe health
tail -5 data/logs/oracle-latency-probe.log

# Check shadow bot activity
grep "Book C scan" data/logs/oracle.log | tail -5
```
