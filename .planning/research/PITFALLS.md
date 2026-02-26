# Domain Pitfalls

**Domain:** Automated prediction market (Kalshi) quant trading system
**Researched:** 2026-02-26
**Focus:** Going from "system exists but zero trades" to "consistent daily P&L"

---

## Critical Pitfalls

Mistakes that cause system-wide failure, capital loss, or months of wasted effort.

### Pitfall 1: The Filter Cascade — Death by a Thousand Guards

**What goes wrong:** Each safety guardrail independently seems reasonable (edge threshold, liquidity filter, dedup cooldown, daily trade limit, daily loss limit, cost cap, balance check, stale data check, circuit breaker, kill switch, allocator budget). But stacked together, the probability of ANY trade passing ALL filters simultaneously approaches zero. This is exactly what has happened: the entertainment bot has a 99.7% skip rate and 2 trades ever; economics, beatrelease, and strategy bots have zero trades.

**Why it happens:** Each filter was added in isolation to prevent a specific failure mode. Nobody measured the compound pass-through rate. When 8 filters each have a 70% pass rate, the compound pass rate is 0.7^8 = 5.7%. When some filters have a 30% pass rate (e.g., edge threshold + liquidity in illiquid entertainment markets), compound drops below 1%.

**Consequences:** System runs for months burning compute and API quota while executing zero trades. Creates the illusion of safety while producing no revenue. Worse, you cannot validate models because you have no settlement outcomes, creating a vicious cycle: no trades -> no settlements -> no Brier scores -> can't calibrate -> models stay wrong -> no trades.

**Prevention:**
1. Instrument the filter cascade: log which specific filter rejected each trade candidate with `ScanSummary.skip(reason)` (already partially implemented). Aggregate skip reasons across 24h to find the binding constraint.
2. Run a "filter pass-through audit" weekly: for each bot, compute `(candidates evaluated) / (trades placed)`. If ratio exceeds 500:1, at least one filter needs relaxing.
3. Set edge thresholds and liquidity filters PER MARKET TYPE, not globally. Entertainment markets (volume=5, spread=40c) need different thresholds than weather (volume=50, spread=20c).
4. Before adding any new filter, simulate its impact on historical trade candidates. "Would this have blocked any profitable trades?"

**Detection:**
- `*-decisions.json` files showing 100% skip rate for a bot across 7+ days
- `health-state.json` heartbeats active but zero trades in trade logs
- ScanSummary totals showing 0 placed / N skipped consistently

**Phase:** P0 (Feedback Loop) and P1 (Enable Alpha Sources). Must be diagnosed before any other work.

---

### Pitfall 2: Flying Blind Without Calibration (Garbage In, Garbage Out)

**What goes wrong:** Probability models produce numbers, but those numbers bear no validated relationship to reality. `calibration.json` is empty. All weather models use hardcoded defaults (sigma intercept=2.0, slope=0.5). Crypto model claims 8-80% edges when signal quality is 3-5%. No Brier scores exist. No settlement reconciliation has been done. The system is generating trades (when filters allow) based on completely unvalidated probabilities.

**Why it happens:** Calibration requires a feedback loop: trade -> settle -> compare predicted vs actual -> adjust parameters. But with zero settlements reconciled and empty calibration, there is no feedback loop. The system is open-loop, and open-loop control systems diverge from reality.

**Consequences:**
- Edge estimates are wrong (potentially in either direction). Overestimated edges lead to oversizing (Kelly is extremely sensitive to probability estimation errors: a 3 percentage point overestimate in win probability can cause 5-10x oversizing). Underestimated edges lead to zero trades.
- Cannot distinguish "model is good but filters are too tight" from "model is bad and filters are correctly blocking garbage."
- Running recalibration on bad data produces confidently wrong parameters (Brier score of 0 does not mean a perfect model; even well-specified models have non-zero Brier scores).
- The crypto model disconnect (8-80% claimed edges vs 3-5% signal quality) is a specific instance: the GBM model may be using wrong volatility inputs or incorrect time-to-settlement calculations.

**Prevention:**
1. Run reconciliation BEFORE any model changes: `npm run reconcile` and `npm run backfill` to annotate trade logs with settlement outcomes. This is step zero.
2. Generate Brier scores and calibration curves (not just aggregate Brier score -- decompose into calibration + resolution + uncertainty components).
3. Compare predicted probabilities against settlement rates in buckets (e.g., for trades where model said 70-80%, what fraction settled YES?). This is the calibration curve.
4. Calibrate one model at a time, validating each before moving to the next (weather first, since it has the most historical trades).
5. For crypto specifically: validate time-to-settlement calculations (T=2456min seems wrong for hourly markets) and cross-check realized vol against Deribit DVOL.

**Detection:**
- `config/calibration.json` is empty or has not been updated in >30 days
- `data/backtest-results.json` shows null Brier scores
- Edge claims >20% for any market type (should trigger immediate skepticism)
- Predicted probabilities cluster at extremes (>95% or <5%) instead of distributing across the range

**Phase:** P0 (Feedback Loop). This is the number one priority and blocks all other work.

---

### Pitfall 3: Kelly Criterion on Bad Probability Estimates

**What goes wrong:** Kelly sizing is mathematically optimal ONLY when the probability estimate is correct. When probability estimates are wrong (and they are -- see Pitfall 2), Kelly becomes a capital destruction engine. The system currently has a bankroll basis risk bug: Kelly sizing uses total portfolio balance rather than available balance, meaning positions can be sized 2x too large when >50% of capital is locked. Combined with overestimated edges, this creates compounding ruin risk.

**Why it happens:** Kelly's formula `f = (bp - q) / b` amplifies estimation errors. If you think you have a 15% edge but actually have 5%, half-Kelly suggests ~3x the correct bet size. With quarter-Kelly the damage is reduced but not eliminated. The system also has inconsistent fee handling (three-way split across bots), further distorting the effective edge input to Kelly.

**Consequences:**
- Position sizes too large for actual edge, leading to excess volatility and drawdowns
- Ruin risk increases nonlinearly with probability estimation error
- Bankroll basis risk means new trades sized against "phantom" capital locked in open positions
- The `high_conviction_kelly` function at 60% Kelly is especially dangerous if the "high conviction" signals are themselves uncalibrated

**Prevention:**
1. Fix bankroll basis risk immediately: `bankroll = available_balance`, not `total_balance`.
2. Standardize fee handling across all bots: pass `fee_cents` to all Kelly functions, remove all `edge_after_fees()` calls.
3. Use quarter-Kelly (not half-Kelly) until models are calibrated and validated. The system already does this for crypto/brackets but should do it for ALL unvalidated bots.
4. Add a hard position-size cap: no single trade should risk >2% of available balance regardless of Kelly output. The current $5-10 max per trade provides this, but verify it is enforced.
5. Log Kelly inputs (edge, price, bankroll, fee) with every trade for post-hoc analysis of sizing quality.

**Detection:**
- Realized edge (from settlements) consistently lower than predicted edge by >5 percentage points
- Drawdowns exceeding 3x the expected loss from Kelly simulations
- `bankroll_used` in trade records showing total balance instead of available balance
- Inconsistent `fee_cents` values across different bots for same price levels

**Phase:** P0/P1. Fix bankroll basis risk in P0. Standardize fee handling in P1. Downgrade to quarter-Kelly until calibration validates models.

---

### Pitfall 4: Speed Edge Window Is Narrower Than You Think

**What goes wrong:** The primary alpha thesis is "get external data (NWS actuals, album sales, earnings) before Kalshi markets price it in." But this window is shrinking as more participants automate. NWS publishes temperature observations; within minutes (not hours), automated traders update Kalshi prices. If your bot polls every 30 minutes (weather bot) or 15 minutes (entertainment bot), you arrive after the window has closed. A speed edge that was profitable 6 months ago may no longer exist.

**Why it happens:** Information arbitrage has a half-life. Early movers profit, then their trading activity moves prices to fair value, and latecomers get adverse selection (they buy after the best entry is gone). With REST API polling at fixed intervals, you cannot react faster than your poll interval. Meanwhile, competitors may use WebSocket feeds or lower-latency polling.

**Consequences:**
- Trades execute at prices that already reflect the information, eliminating the edge
- Worse: adverse selection means you systematically buy after informed traders have already moved the price, giving you negative expected value
- The NWS pre-dawn gate (hour < 8) was a correct fix for a specific timing bug, but the broader issue is that ANY scan interval >5 minutes is potentially too slow for info-arb
- Album sales data from HDD publishes weekly; if competitors scrape it faster, the window for entertainment trades evaporates

**Prevention:**
1. Measure the information window empirically: log the timestamp when new data appears (NWS observation time, HDD chart publication) and the timestamp when Kalshi market prices move. The delta is your window.
2. For time-critical data sources (NWS actuals), poll every 1-2 minutes during peak hours (10am-5pm), not every 30 minutes.
3. Implement event-driven scanning: trigger a scan when a data source updates, not on a fixed timer. Compare file hashes or API ETags to detect changes.
4. Track fill rates and execution quality: if limit orders sit unfilled for >1 minute in liquid markets, you are likely trading after the window has closed.
5. For each data source, document the expected information window (NWS: ~5 min after observation; HDD: ~1-6 hours after chart publish; Cleveland Fed: ~24 hours after update).

**Detection:**
- High order non-fill rate (the crypto bot already shows 87% non-fill on brackets)
- Edge at order placement time vs edge at fill time shows significant decay
- Decision logs showing trade candidates found but prices moved before execution
- Competitors' orders visible in the order book before yours

**Phase:** P1 (Enable Alpha Sources). Must be addressed alongside bot enablement; there is no point enabling bots if the speed window has already closed.

---

### Pitfall 5: Holding All Positions to Settlement (No Exit Discipline)

**What goes wrong:** The position monitor has zero exits ever. All positions are held to settlement. This means: (a) capital is locked for the full duration of every trade instead of being recycled, (b) profitable positions that could be exited at 85c are held and sometimes settle at 0, (c) losing positions that should be cut at 20c bleed to full loss, and (d) model-shift exits never fire, so even when the model reverses its opinion, the position stays.

**Why it happens:** The position monitor exists but has not been validated in production. Its thresholds (TP=80%, SL=30%, model-shift=20%) were set theoretically, never tested against actual position P&L. The monitor must cross-reference entry prices from 6 different trade log files, introducing fragility. And the order monitoring race condition means fast fills may not be detected.

**Consequences:**
- Capital efficiency drops dramatically. With $500 bankroll and 20 open positions at $5 each = $100 locked = 20% of capital unavailable.
- Expected P&L is worse than optimal: a position bought at 30c that reaches 85c has locked in ~55c of profit but risks losing it all if the event outcome flips. Selling at 85c captures the profit and frees capital.
- The interaction between "no exits" and "bankroll basis risk" in Kelly sizing compounds: Kelly sizes new trades assuming the locked capital is available, leading to oversized new positions.

**Prevention:**
1. Validate position monitor in demo mode first: run for 48h, verify it detects TP/SL/model-shift conditions and attempts exits.
2. Start with take-profit only (lowest risk). SL and model-shift exits have more complex edge cases.
3. Track "capital efficiency" = (trades placed * avg holding period) / (available balance * time). This should decrease over time as positions are cycled.
4. Address the order monitoring race condition: retry order status check with 5-second polling before giving up on a fast fill.
5. Set realistic thresholds: 85c take-profit is aggressive; 90c may be more appropriate for prediction markets where terminal values are 0 or 100.

**Detection:**
- `data/kalshi-position-trades.json` is empty or has zero entries
- Open positions older than 7 days with bid > 80c (should have been exited)
- Capital utilization (locked capital / total balance) consistently >30%
- Position monitor heartbeat active in `health-state.json` but no exit attempts logged

**Phase:** P1 (Enable Alpha Sources). Enable position monitor alongside other bot fixes.

---

## Moderate Pitfalls

### Pitfall 6: Overfitting Calibration to Small Samples

**What goes wrong:** With only ~50-100 weather trades and near-zero trades from other bots, running grid-search calibration (`npm run calibrate`) will overfit sigma parameters to noise. Per-city calibration with <10 trades per city is statistically meaningless. The calibrated parameters may perform worse out-of-sample than the hardcoded defaults.

**Prevention:**
1. Require minimum sample sizes before calibrating: at least 30 settled trades per parameter being optimized. For per-city calibration, this means 30 settled trades per city.
2. Use regularization: penalize deviations from the hardcoded defaults (Bayesian prior centered on intercept=2.0, slope=0.5). Only move away from defaults when data strongly supports it.
3. Use walk-forward validation (not in-sample Brier score) to evaluate calibration quality: train on first 70% of trades, evaluate on last 30%.
4. Start with global calibration (one sigma for all cities), then move to per-city only when sample sizes support it.

**Detection:**
- Calibrated sigmas that deviate >50% from defaults with <30 trades supporting them
- Out-of-sample Brier score worse than in-sample (classic overfitting signal)
- Per-city parameters that are wildly different from each other (e.g., MIA sigma=1.0 but NYC sigma=4.0) without meteorological justification

**Phase:** P0 (Feedback Loop). Calibration is part of P0 but must be done carefully.

---

### Pitfall 7: External Data Source Fragility

**What goes wrong:** Every alpha source depends on an external API or web scrape that can break without warning. HDD Sanity CMS API can change GROQ query schema. BeatRelease blog HTML structure changes require regex updates. Cleveland Fed nowcast page can restructure. Box Office Mojo CSS changes break BeautifulSoup parsing. The HDD endpoints are already disabled because they stopped working.

**Prevention:**
1. For every data source, implement a health check that runs independently of trading: does the API return valid data? Does the response match expected schema? Log freshness timestamp.
2. Source monitor (`source-monitor.py`) already exists but needs to be extended: add schema validation (not just "did the HTTP request succeed?" but "does the response contain the expected fields?").
3. Build fallback data sources for critical signals. Weather has Open-Meteo as primary; add WeatherAPI or Visual Crossing as backup. Entertainment has HDD; add Billboard API as backup.
4. Implement the "stale data" detection properly: the HDD parser has a fail-open bug where unparseable dates pass through. Fix this to fail-closed (reject data with unparseable dates).
5. Version your scraping code separately from trading logic so scraper fixes can be deployed without touching trade execution.

**Detection:**
- Source freshness timestamps in `health-state.json` showing stale data (>2x expected refresh interval)
- HTTP error rates >5% for any data source over 24h
- Trade decisions showing `data_age_hours > MAX_DATA_AGE_HOURS` for any source
- HDD, BeatRelease, or Cleveland Fed returning 4xx/5xx errors

**Phase:** P1 and P4. Fix critical sources (NWS, HDD) in P1; build redundancy in P4.

---

### Pitfall 8: Confusing Demo Mode with Production Reality

**What goes wrong:** The system has been running in demo mode (`KALSHI_MODE=demo`) which uses a different API endpoint (`demo-api.kalshi.co`). Demo mode has different liquidity, different available markets, possibly different pricing behavior. Strategies that work in demo may fail in production because: (a) real markets have tighter competition, (b) real fills face actual counterparty risk, (c) demo may show markets that don't exist in production or vice versa.

**Prevention:**
1. When switching to production, start with a single bot (weather, since it has the most validated history) at minimum position sizes.
2. Compare demo trade logs against production for the same time periods. If demo showed edges that production doesn't, investigate why.
3. Track fill rates separately for demo vs production. If production fill rates are significantly lower, your limit pricing strategy needs adjustment.
4. The `KALSHI_CONFIRM_PRODUCTION=yes` safety gate is good, but add monitoring: alert if any bot switches from demo to production without explicit operator action.
5. Never calibrate models on demo data and deploy to production. Calibrate on production data only.

**Detection:**
- `KALSHI_MODE` in `.env` set to `demo` while expecting real P&L
- Trade volume in demo significantly different from production possibilities
- Fill rate drops >20% when moving from demo to production

**Phase:** P1/P2. Transition to production as bots are validated.

---

### Pitfall 9: The Reconciliation-Calibration Chicken-and-Egg

**What goes wrong:** You need calibrated models to trade profitably. You need trade data to calibrate. You need settled trades to validate calibration. But with zero settlements reconciled, you cannot start the loop. Teams get stuck debating whether to "fix calibration first" or "just trade and see what happens." Both are wrong: trading without calibration wastes capital; waiting for calibration without trading produces no data.

**Prevention:**
1. Break the loop by running reconciliation on EXISTING trades (the weather bot has some historical trades, even if few). Any data is better than no data.
2. Bootstrap with external calibration data: NWS publishes forecast error statistics. Use published MAE/RMSE data to set initial sigma parameters rather than relying solely on your own trades.
3. Start trading at quarter-Kelly (very conservative sizing) to generate settlement data. Accept that early trades will have poor edge estimates -- the goal is data collection, not profit.
4. Set a timeline: "We will have 50 settled trades by day 14 or we escalate." Track this daily.

**Detection:**
- Zero entries in `data/backtest-results.json` after 7+ days of trading
- `config/calibration.json` still empty after reconciliation has been run
- Team debating "fix model" vs "just trade" without a clear decision

**Phase:** P0 (Feedback Loop). This is the primary P0 deliverable.

---

### Pitfall 10: File-Based State Corruption Under Concurrent Access

**What goes wrong:** Eight bots compete for file locks on shared state files (`allocator-state.json`, `health-state.json`, `market-cache.json`). If a bot crashes while holding an `fcntl` lock, the lock may be orphaned (on macOS, `fcntl` locks are released on file close, but partial writes can corrupt the file). `_atomic_write_json()` mitigates this for writes, but concurrent readers may see partially-written temp files. Under high contention (8 bots, 2 requests each per scan), total lock wait time can reach 2.4 seconds, causing scan intervals to drift.

**Prevention:**
1. Verify `_atomic_write_json()` is used for ALL state file writes (audit codebase). Direct `json.dump()` to shared files is a corruption vector.
2. Implement read-only shared locks (fcntl.LOCK_SH) for budget reads, exclusive locks only for writes. This eliminates most contention.
3. Add lock timeout: if a lock cannot be acquired within 5 seconds, skip and retry next cycle. Never block indefinitely.
4. Consider migrating critical state (allocator) to SQLite with WAL mode. SQLite handles concurrent access natively and eliminates file-lock bugs.
5. Monitor for orphaned locks with `fuser` in supervisor health checks.

**Detection:**
- JSON parse errors in bot logs when reading shared state files
- Scan intervals drifting >50% beyond configured interval (e.g., 30-min interval taking 45+ min)
- `fcntl` errors or timeout warnings in logs
- Inconsistent allocator state (per-bot allocations don't sum to total)

**Phase:** P4 (Operational Hardening). Not urgent until bot activity increases, but becomes critical at scale.

---

## Minor Pitfalls

### Pitfall 11: LLM Nondeterminism in BeatRelease Scanner

**What goes wrong:** The beatrelease scanner uses DeepSeek LLM to extract trading signals from blog posts. LLM output is nondeterministic: the same blog post may produce different signals on different runs. Regex parsing of LLM output (`re.search(r'"side":\s*"(buy|sell)"')`) is fragile: if the LLM changes phrasing (e.g., `"action": "buy"` instead of `"side": "buy"`), parsing fails silently and the trade is skipped. The 15% calibration discount applied to LLM confidence may be too aggressive (killing all signals) or too lenient (passing bad signals).

**Prevention:**
1. Require strict JSON schema in the LLM prompt. Parse JSON (not regex). Validate all required fields before trade.
2. Log the raw LLM response for every scan for post-hoc analysis.
3. Backtest the 15% discount against historical blog posts and market outcomes. Is 15% the right number?
4. Consider replacing LLM with rule-based parsing for structured blog posts (e.g., "Artist X sold Y units") -- LLM is overkill for well-formatted data extraction.

**Detection:**
- BeatRelease decisions showing 100% skip rate with reason "LLM parse failure" or "below confidence threshold"
- Raw LLM responses in logs showing valid trading signals that were unparseable by regex
- Different trade decisions on the same blog post when re-scanned

**Phase:** P1 (Enable Alpha Sources). Part of beatrelease scanner debug.

---

### Pitfall 12: Ignoring Market Microstructure (Spread and Liquidity)

**What goes wrong:** Probability models compute edges against the midpoint or YES price, but execution happens at the ask (for buys). In prediction markets with wide spreads (10-40c on entertainment markets), the edge is consumed by the spread. A 10% model edge at midpoint = 50c translates to ~5% effective edge at ask = 55c. With Kalshi fees (7% * P * (1-P) = ~1.75c at 50c), the net edge may be negative.

**Prevention:**
1. Compute edge against the execution price (ask for buys, bid for sells), not the midpoint. The `compute_limit_price()` function already handles this with edge-tiered pricing, but verify all bots use the output correctly.
2. For each market type, compute the average effective spread cost and subtract it from edge estimates before Kelly sizing.
3. Track execution quality: compare limit price at order submission to actual fill price. If fills are consistently worse than limit, your pricing is too aggressive.
4. For entertainment and other illiquid markets, consider maker-only (post-only) orders that earn the spread instead of paying it.

**Detection:**
- High unfilled order rate in liquid markets (indicates pricing too aggressive)
- Realized edge (post-fill, post-fee) consistently lower than predicted edge by >spread/2
- Decision logs showing edges that are smaller than the spread

**Phase:** P1/P2. Part of individual bot tuning.

---

### Pitfall 13: Time Zone and Settlement Time Bugs

**What goes wrong:** Weather markets settle based on the daily high temperature in a specific city's local time zone. The bot must correctly map between UTC (API times), city local time (settlement basis), and server time (Mac Mini in a specific timezone). Off-by-one-day errors in `days_out` calculation silently shift the probability by ~1 sigma. The NWS pre-dawn bug (fixed) was one instance; similar bugs lurk in crypto (settlement buffer minutes), economics (days to release), and entertainment (chart publication day of week).

**Prevention:**
1. All time calculations should use `zoneinfo.ZoneInfo` with explicit city timezones (already partially implemented in `CITY_TIMEZONES`).
2. Add unit tests for edge cases: markets settling at midnight local time, DST transitions, leap years, and city-specific timezone offsets.
3. Log `days_out`, `hours_to_close`, and `hour_of_day` in every trade decision for post-hoc verification.
4. For crypto: validate that `time_horizon_minutes` matches actual time to settlement (the claimed T=2456min for hourly markets is suspect).

**Detection:**
- `days_out` values that don't match reality (e.g., a market settling tomorrow showing days_out=2)
- Unusual trade patterns around midnight or DST transitions
- Crypto trades placed with time_horizon_minutes that seem too large or small for the market type

**Phase:** P1 (per-bot debugging). Check each bot's time handling during enablement.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| P0: Settlement reconciliation | Reconciliation misses settlements due to API field inconsistency (ticker vs market_ticker) | Code already handles both; verify with 100% coverage test |
| P0: Brier score generation | Interpreting Brier score in isolation without calibration curve decomposition | Generate calibration curve (predicted prob buckets vs actual settlement rate) alongside Brier |
| P0: Sigma calibration | Overfitting per-city params to <10 trades per city | Require min 30 trades per parameter; use global calibration first |
| P1: Entertainment bot | Filter cascade producing 99.7% skip rate | Audit each filter independently; relax liquidity (already done: vol=5, spread=40) |
| P1: BeatRelease scanner | LLM regex parsing silently dropping valid signals | Switch to JSON parsing; log raw LLM responses |
| P1: Economics bot | Cleveland Fed scraper broken (HTML structure changed) | Check for JSON API first; add structured error handling |
| P1: Position monitor | Zero exits due to race condition or threshold misconfiguration | Validate in demo with synthetic positions first |
| P1: Speed edge windows | Polling too slowly for NWS info arb (30 min interval) | Reduce to 2-5 min during peak hours; measure actual window |
| P2: Longshot bias selling | Only 5 trades from strategy-trader; allocator or filter blocking everything | Trace each candidate through full filter pipeline |
| P2: Cross-platform arb | Polymarket price feeds untested in production | Monitor-only phase (already implemented); validate price accuracy first |
| P3: Crypto model validation | 8-80% edge claims with 3-5% signal quality | Backtest against historical data; validate vol inputs and settlement times |
| P3: Automated calibration pipeline | Auto-updating calibration with bad data degrades all models | Gate auto-updates on out-of-sample validation; keep rollback capability |
| P4: Daily automation | Cron jobs silently fail without alerting | Use supervisor for periodic tasks; add failure alerting |
| P4: Production migration | Demo strategies fail in production due to different liquidity | Start with 1 bot at minimum size; compare demo vs production metrics |

---

## Sources

- [Prediction Market Thoughts part 3: The Holy Trinity of Bots | Manifold](https://manifold.markets/post/prediction-market-thoughts-part-3-t)
- [Why AI Trading Bots Fail & How to Build a Profitable One | Amplework](https://www.amplework.com/blog/ai-trading-bots-failures-how-to-build-profitable-bot/)
- [How AI is helping retail traders exploit prediction market 'glitches' | CoinDesk](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money)
- [Kelly Criterion Bankroll Management | ManageBankroll](https://managebankroll.com/blog/kelly-criterion-betting-strategy-bankroll-management)
- [Why fractional Kelly? Simulations with uncertainty | Matthew Downey](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html)
- [Kelly Criterion Formula Explained | Quant Matter](https://quantmatter.com/kelly-criterion-formula/)
- [On misconceptions about the Brier score in binary prediction models | PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC12818272/)
- [Brier Score: Understanding Model Calibration | Neptune.ai](https://neptune.ai/blog/brier-score-and-model-calibration)
- [Prediction Market Arbitrage Guide 2026 | NYC Servers](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide)
- [How a VPS Can Give You an Edge in Prediction Markets | QuantVPS](https://www.quantvps.com/blog/vps-for-polymarket-and-kalshi)
- [Systemic failures and organizational risk management in algorithmic trading | PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC8978471/)
- [Beware of the traps -- Quantitative Trading Mistakes | Harel Jacobson](https://volquant.medium.com/beware-of-the-traps-quantitative-trading-mistakes-f3e434f0a1cb)
- [Best 15min Crypto Up/Down Position Sizing: Kelly Criterion | Crypticorn](https://www.crypticorn.com/position-sizing-on-polymarket-and-kalshi-crypto-up-down-predictions/)
- [Building an Automated Event Trading Bot with Kalshi | JIN](https://jinlow.medium.com/building-an-automated-event-trading-bot-with-kalshi-prediction-markets-a-practical-engineering-a1af3ee619e6)
- Codebase analysis: `src/kalshi/kalshi_auth.py`, `src/kalshi/probability.py`, `config/bots-config.json`, `.planning/codebase/CONCERNS.md`

---

*Concerns audit: 2026-02-26*
