# Phase 4: Bot Activation - Research

**Researched:** 2026-02-27
**Domain:** Debugging and tuning automated trading bots to produce regular trades on validated edges
**Confidence:** HIGH

## Summary

Phase 4 is about making six bots actually execute trades. The codebase is mature -- all bots are fully implemented with sophisticated probability models, position sizing, decision logging, and risk controls. The problem is not missing code but misconfigured parameters and broken data pipelines that cause the filter cascade to reject every candidate. The entertainment bot has a 99.7% skip rate (2 trades ever), economics bot has zero trades (Cleveland Fed scraper may be broken), beatrelease scanner has near-zero trades (LLM pipeline undertested), and strategy trader only places ~5 trades per scan (capped at `longshots[:5]`).

The work decomposes into four categories: (1) filter cascade diagnosis and tuning (entertainment bot, beatrelease), (2) data source repair (Cleveland Fed scraper, HDD Sanity CMS health check), (3) throughput scaling (strategy trader cap increase, NWS polling frequency), and (4) instrumentation completeness (decision logging for every skip reason across all bots). The weather bot and source monitor are closest to working correctly -- they primarily need config verification that calibrated sigma parameters are being used (calibration.json already has 4 per-city entries from Phase 1).

**Primary recommendation:** Debug each bot's filter cascade independently by tracing one scan cycle end-to-end, identifying the binding constraint, relaxing it with justification, and verifying trades flow through before moving to the next bot.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| EXEC-01 | Entertainment bot skip rate drops below 80% (from 99.7%) | Filter cascade analysis shows confidence threshold (85%), liquidity filter (vol=5, spread=40c), edge thresholds (4-10%), and HDD data freshness (168h) as candidate binding constraints. Config `"enabled": false` must be flipped. Confidence threshold of 85% is very high for info-arb markets. |
| EXEC-02 | Beatrelease scanner executes at least 1 trade per week | LLM pipeline works end-to-end but has multiple skip points: `not_kalshi_related`, `no_trades_extracted`, `low_calibrated_confidence` (60% threshold), `edge_too_small` (2% minimum), `stale_blog_price` (10c tolerance), `allocator_denied`. Blog URL format and content structure need verification. |
| EXEC-03 | Economics bot scrapes Cleveland Fed nowcast and evaluates CPI/GDP/Jobs markets | Three-layer parsing (BS4 structured + regex + cache fallback) already implemented. Needs live verification that Cleveland Fed page structure still matches parsers. Market prefix search (`KXCPI`, `KXGDP`, etc.) needs validation against actual Kalshi tickers. |
| EXEC-04 | Strategy trader scales to 20+ longshot bias trades per week | Currently hard-capped at `longshots[:5]` per scan. With 15-min scan interval and daemon mode, needs cap increase to ~10 per scan. Sports prefix list and category-adjusted Becker model parameters need validation. |
| EXEC-05 | Weather bot trades with calibrated per-city sigma parameters | calibration.json already has 4 cities (DEN, AUS, CHI, NY) with calibrated sigma. `weather_probability()` and `weather_sigma()` already load from calibration. Need to verify the code path actually uses per-city params (not falling through to global defaults) and log which sigma was used. |
| EXEC-06 | Source monitor NWS arbitrage polls at 5-minute intervals during peak hours | Currently fixed at 10-min intervals (config). Main loop sleeps 30s but NWS interval is configurable. Need to implement adaptive polling: 5 min during 10am-4pm local time, 10 min otherwise. |
| EXEC-07 | Per-bot filter cascade instrumented -- decision logs show which specific filter caused each skip | `ScanSummary.skip(reason)` and `trade_manager.log_decision()` infrastructure exists. Most bots already log decisions at major skip points. Gaps exist in strategy-trader (only logs `allocator_denied` and `kelly_zero`, not `yes_ask > 15` or `hours < 0.5` or `no_price > 96`) and some early-exit paths in other bots. |
</phase_requirements>

## Standard Stack

### Core

This phase does not introduce new libraries. All work is configuration changes, parameter tuning, and code fixes within the existing Python codebase.

| Library | Version | Purpose | Already In Use |
|---------|---------|---------|----------------|
| requests | existing | HTTP for Cleveland Fed, NWS, HDD Sanity CMS | Yes |
| beautifulsoup4 | existing | Cleveland Fed HTML parsing | Yes |
| zoneinfo | stdlib | Peak-hour timezone calculations for NWS | Yes |
| math | stdlib | Probability models (erf, sqrt, exp) | Yes |

### Supporting

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `pytest tests/` | Validate probability model changes | After any sigma/threshold changes |
| `npm run calibrate` | Regenerate calibration.json | If weather sigma defaults change |
| Decision log files (`*-decisions.json`) | Diagnose filter cascades | Every debugging session |

## Architecture Patterns

### Existing Bot Structure (DO NOT change)

All bots follow this pattern -- Phase 4 works within it:
```
1. Module-level init: KalshiClient, TradeManager, config load
2. Main loop: sleep(interval) -> health heartbeat -> scan()
3. Scan: fetch data sources -> fetch markets -> evaluate each -> filter cascade -> place_order
4. Each filter logs via trade_manager.log_decision() or ss.skip()
```

### Pattern 1: Filter Cascade Diagnosis

**What:** Trace a single market through the entire filter pipeline to find the binding constraint.
**When to use:** Whenever a bot has >90% skip rate.
**Example:**
```python
# In entertainment-bot match_and_trade():
# Filter cascade order:
# 1. is_market_liquid(m, min_volume=5, max_spread=40) -> skip "illiquid"
# 2. Artist word-match against album_data -> skip "no_match"
# 3. parse_album_threshold(title, ticker) -> skip "threshold_parse_fail"
# 4. confidence < CONFIDENCE_THRESHOLD (0.85) -> skip "low_confidence"
# 5. edge < MIN_EDGE (4-10% depending on sigma) -> skip "low_edge"
# 6. allocator.request_budget() denied -> skip "allocator_denied"
# 7. quarter_kelly() returns 0 contracts -> skip "kelly_zero"
#
# Each step must log_decision() with the specific reason.
```

### Pattern 2: Adaptive Polling Interval

**What:** Different polling intervals based on time of day.
**When to use:** NWS source monitor during peak temperature hours.
**Example:**
```python
# In source-monitor main loop:
from zoneinfo import ZoneInfo
import datetime

def nws_poll_interval_seconds():
    """5 min during 10am-4pm ET, 10 min otherwise."""
    et_now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if 10 <= et_now.hour < 16:
        return 5 * 60  # 5 minutes
    return 10 * 60  # 10 minutes

# Replace fixed nws_interval with dynamic call
```

### Pattern 3: Decision Log Completeness

**What:** Every filter skip must produce a decision log entry with the specific filter name.
**When to use:** EXEC-07 requirement.
**Example:**
```python
# Strategy trader -- currently missing decision logs for early filters:
if yes_ask <= 0 or yes_ask > 15:
    trade_manager.log_decision(ticker, "no", "skipped", "price_out_of_range",
                               yes_ask=yes_ask, filter="longshot_price_range")
    continue

if hours < 0.5:
    trade_manager.log_decision(ticker, "no", "skipped", "too_close_to_settlement",
                               hours_to_close=round(hours, 2), filter="settlement_proximity")
    continue
```

### Anti-Patterns to Avoid

- **Relaxing safety guards without understanding:** Do NOT remove circuit breaker, kill switch, or daily loss limits to increase trade volume. Only tune market-specific filters (edge threshold, liquidity minimums, confidence thresholds).
- **Changing probability models during activation:** This phase tunes FILTERS, not MODELS. If the model is wrong, that is Phase 5 (calibration). Do not change sigma values, probability functions, or Kelly formulas.
- **Testing in production:** All debugging must happen in demo mode first. The `KALSHI_MODE=demo` default is correct. Do not switch to production.
- **Adding new filters:** This phase removes filter-cascade friction. Do NOT add new safety checks here.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Decision logging | Custom file writing | `trade_manager.log_decision()` | Already has atomic writes, rotation, file locking |
| Scan metrics | Manual counters | `ScanSummary` class | Already tracks fetched/evaluated/placed/skipped with reason categorization |
| Market fetching | Direct API calls | `client.get_all_markets(prefix=, cache_ttl=)` | Has pagination, caching, retry logic |
| Position sizing | Inline math | `quarter_kelly()`, `half_kelly()` from probability.py | Already handles fees, bankroll, Kelly fraction correctly |
| Source health tracking | Log parsing | `HealthCheckMonitor` | Already writes health-state.json with source freshness |

**Key insight:** The infrastructure is complete. Phase 4 is about tuning parameters and fixing data pipelines, not building new systems.

## Common Pitfalls

### Pitfall 1: Entertainment Bot Filter Cascade

**What goes wrong:** The entertainment bot has a 99.7% skip rate. Multiple filters stack to block everything.
**Why it happens:** The confidence threshold is 85% (`bots-config.json`), but with album_data_sigma at 5-15% uncertainty, `info_arb_probability()` rarely produces >85% confidence unless observed units are far from the threshold. Additionally, `"enabled": false` in bots-config.json means the supervisor will not start it.
**How to avoid:**
1. Set `"enabled": true` in config.
2. Lower `confidenceThreshold` from 0.85 to 0.70 (the info_arb model with sigma=5% needs observed/threshold ratio of ~1.52 to reach 85% confidence; at 70% it needs ~1.26, which is realistic).
3. Verify HDD Sanity CMS endpoints still return data (they were previously disabled in monitor config).
4. Add logging for every filter step's pass/fail count per scan cycle.
**Warning signs:** Decision logs showing 100% `low_confidence` or `no_match` skip reasons.

### Pitfall 2: Cleveland Fed Scraper Fragility

**What goes wrong:** The economics bot has three parsing layers (BS4 structured, regex, cache) but all may fail if Cleveland Fed changes their page structure. The bot would silently fall back to stale cache and then skip trading due to `_stale` flag.
**Why it happens:** Web scraping is inherently fragile. Cleveland Fed updates their website periodically.
**How to avoid:**
1. First verify the URL (`https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting`) still serves the expected content.
2. Test each parsing strategy independently against the live page.
3. If the page has changed, update the parsers.
4. Check if Cleveland Fed has a REST API or JSON endpoint (some Federal Reserve banks provide these).
**Warning signs:** Cache age in logs showing >24h consistently; `no values parsed` warnings.

### Pitfall 3: Strategy Trader Artificial Cap

**What goes wrong:** Strategy trader finds many longshot candidates but only places 5 trades per scan (`longshots[:5]`). Even running every 15 minutes, this caps weekly output at ~2,240 attempts (5 * 4 * 24 * 7), but with market overlap and dedup, actual unique trades are much lower.
**Why it happens:** Conservative initial cap from development phase. The `maxDailyTrades: 20` config also limits total daily output.
**How to avoid:**
1. Increase `longshots[:5]` to `longshots[:10]` or remove the cap entirely (let daily trade limit be the real constraint).
2. Verify `maxDailyTrades: 20` in bots-config allows 20+ trades per week (it does -- 20/day * 7 = 140/week).
3. The real bottleneck may be the allocator or per-ticker dedup, not the slice cap.
**Warning signs:** Scan summaries showing many candidates found but only 5 placed per scan.

### Pitfall 4: NWS Polling Interval Mismatch

**What goes wrong:** Source monitor polls NWS every 10 minutes (config), but the info-arb window for temperature is narrow. By the time Kalshi adjusts prices after NWS publishes an observation, the edge is gone.
**Why it happens:** The main loop sleeps 30 seconds between iterations, but only checks NWS when `(now - last_nws) >= nws_interval` (10 min). Reducing the config to 5 min works but should only apply during peak hours to avoid rate-limiting NWS.
**How to avoid:**
1. Implement adaptive NWS polling: 5 min during 10am-4pm ET (peak temperature hours), 10 min otherwise.
2. Do NOT poll NWS every 30 seconds -- NWS API has rate limits and observations only update hourly.
3. The main loop's 30-second sleep is fine; the NWS interval gate controls actual poll frequency.
**Warning signs:** NWS arbitrage trades all occurring at hour 16-17 (late afternoon when running_high is already final) rather than distributed through the day.

### Pitfall 5: Beatrelease LLM Pipeline Undertested

**What goes wrong:** The beatrelease scanner depends on DeepSeek LLM to extract trade signals from blog posts. Multiple skip paths exist: posts not Kalshi-related, no trades extracted from LLM, calibrated confidence <60%, edge <2%, stale blog price.
**Why it happens:** Blog content is sporadic and unpredictable. The LLM may extract zero valid trades from most posts.
**How to avoid:**
1. First verify BeatRelease blog URLs (`beatrelease.com/blog/categories/kalshi-predictions`) still serve content.
2. Run `--once` mode and trace a full scan cycle to see which posts are found and what the LLM extracts.
3. The 15% calibration discount (`LLM_CALIBRATION_DISCOUNT = 0.15`) combined with 60% minimum confidence means the LLM must report >75% raw confidence for any trade to pass. This may be too aggressive.
4. Check DeepSeek API key is configured and valid.
**Warning signs:** LLM log showing 0 valid entries across multiple scans; `not_kalshi_related` skip reason dominating.

## Code Examples

### Entertainment Bot Filter Tuning

```python
# In config/bots-config.json, entertainment section:
# Current:
#   "confidenceThreshold": 0.85,
#   "enabled": false,
# Change to:
#   "confidenceThreshold": 0.70,
#   "enabled": true,

# In entertainment-bot.py, the filter cascade is:
# 1. is_market_liquid(m, min_volume=5, max_spread=40) -- already relaxed
# 2. Artist word-match -- depends on HDD returning data
# 3. parse_album_threshold() -- depends on market title format
# 4. confidence < 0.70 (lowered from 0.85) -- primary binding constraint
# 5. edge < MIN_EDGE_CONFIRMED (0.04) or MIN_EDGE_UNCERTAIN (0.10)
# 6. allocator budget check
# 7. quarter_kelly sizing

# KEY INSIGHT: info_arb_probability(observed, threshold, sigma=0.05) returns:
#   - At observed/threshold = 1.0: probability = 0.50 (coinflip, no edge)
#   - At observed/threshold = 1.1: probability = 0.977 (well above 85%)
#   - At observed/threshold = 1.05: probability = 0.841 (just below 85%, above 70%)
# So lowering to 0.70 captures cases where data is 5-10% above/below threshold.
```

### Strategy Trader Cap Increase

```python
# In strategy-trader.py line 281:
# Current:
#   for c in longshots[:5]:
# Change to:
#   for c in longshots[:10]:
# This allows up to 10 trades per scan, 10 * 4 * 24 = 960 attempts/day.
# The real cap is maxDailyTrades: 20 in bots-config.json.
```

### NWS Adaptive Polling

```python
# In source-monitor.py main() loop, replace fixed nws_interval:
def _nws_interval_seconds():
    """Adaptive NWS polling: 5 min during peak hours, 10 min otherwise."""
    from zoneinfo import ZoneInfo
    et_now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if 10 <= et_now.hour < 16:  # 10am-4pm ET
        return 5 * 60
    return config["sources"]["nws"]["intervalMinutes"] * 60

# In the main loop, change:
#   nws_interval = config["sources"]["nws"]["intervalMinutes"] * 60
# To dynamically compute:
#   nws_interval = _nws_interval_seconds()
# And recompute each iteration (move into while loop)
```

### Decision Log Completeness for Strategy Trader

```python
# In strategy-trader.py find_longshot_sells(), add decision logs for early filters:

if yes_ask <= 0 or yes_ask > 15:
    if ss:
        ss.skip("price_out_of_range")
    trade_manager.log_decision(ticker, "no", "skipped", "price_out_of_range",
                               yes_ask=yes_ask)
    continue

if hours < 0.5:
    if ss:
        ss.skip("too_close_to_settlement")
    trade_manager.log_decision(ticker, "no", "skipped", "too_close_to_settlement",
                               hours_to_close=round(hours, 2))
    continue

if est_edge_prelim < min_edge:
    if ss:
        ss.skip("low_edge")
    trade_manager.log_decision(ticker, "no", "skipped", "low_edge",
                               edge=round(est_edge_prelim, 4), min_edge=min_edge,
                               yes_ask=yes_ask)
    continue

no_price = 100 - sell_price
if no_price > 96:
    if ss:
        ss.skip("profit_risk_ratio")
    trade_manager.log_decision(ticker, "no", "skipped", "profit_risk_ratio",
                               no_price=no_price, yes_price=sell_price)
    continue
```

### Weather Bot Calibration Verification

```python
# In weather-bot.py, the calibration check is already in place:
# Line 326-328:
#   cal = _load_calibration()
#   city_code = opp["parsed"]["city"]
#   is_calibrated = bool(cal.get("weather", {}).get("per_city", {}).get(city_code))
#
# calibration.json has 4 cities: DEN, AUS, CHI, NY
# These 4 cities will use per-city sigma; all others use global defaults
# (global_sigma_intercept=5.5, global_sigma_slope=0.2).
#
# VERIFY: The weather_probability() function reads calibration correctly:
#   if city and city in weather_cal.get("per_city", {}):
#       city_cal = weather_cal["per_city"][city]
#       intercept = city_cal.get("sigma_intercept", intercept)
# This code path IS already correct. Just need to confirm calibration.json is loaded.
#
# Already logged in trade records: sigma_used=round(weather_sigma(days_out, city), 2)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Hardcoded sigma=2.5 | Per-city calibrated sigma from calibration.json | Phase 1 (2026-02-26) | 4 cities have calibrated params; 14 use global defaults |
| Fixed NWS polling | Configurable interval (currently 10 min) | Source monitor optimization | Needs further reduction to 5 min during peak hours |
| Global liquidity filter | Per-bot liquidity thresholds | Entertainment bot update | vol=5, spread=40 for entertainment; vol=50, spread=20 for weather |
| No decision logging | `log_decision()` on TradeManager | Phase infrastructure | Most skip points logged; some gaps remain |

**Current gaps that need fixing:**
- Entertainment bot `"enabled": false` in bots-config.json
- HDD source `"enabled": false` in kalshi-monitor-config.json (also affects source-monitor)
- Strategy trader capped at 5 trades per scan
- NWS polling fixed at 10 min (no peak-hour acceleration)
- Decision log coverage incomplete in strategy-trader early filters

## Open Questions

1. **HDD Sanity CMS API availability**
   - What we know: HDD sources are `"enabled": false` in kalshi-monitor-config.json. The Sanity CMS endpoints (project `8aky18h3`) were previously working but status is unclear.
   - What's unclear: Whether Sanity CMS still serves chart data or if HDD changed their CMS provider.
   - Recommendation: Test the Sanity CMS query endpoint during implementation. If it fails, entertainment bot can still use Box Office Mojo data (which is enabled).

2. **Cleveland Fed page structure**
   - What we know: Three-layer parser exists (BS4, regex, cache fallback). The economics bot is untested in production.
   - What's unclear: Whether the current HTML structure matches the parser's expectations.
   - Recommendation: Fetch the page during implementation and test each parsing strategy. Update parsers if structure changed.

3. **BeatRelease blog content frequency**
   - What we know: Two URLs configured. Content is blog-based (sporadic).
   - What's unclear: How often BeatRelease publishes Kalshi-relevant posts. If it is weekly or less, EXEC-02 (1 trade/week) requires nearly every relevant post to produce a trade.
   - Recommendation: Check BeatRelease blog archive to assess posting frequency before tuning filters.

4. **Kalshi market ticker availability**
   - What we know: Bots use prefix searches (`KXALBUMSALES`, `KXCPI`, etc.) to find markets.
   - What's unclear: Whether these market types are currently active on Kalshi. Kalshi launches markets seasonally -- album sales markets may not exist year-round.
   - Recommendation: During implementation, check which prefixes return active markets. If a market type has zero listings, the bot cannot trade regardless of fixes.

## Sources

### Primary (HIGH confidence)
- Direct codebase analysis of all 6 bot files, probability.py, kalshi_auth.py, bots-config.json, kalshi-config.json, kalshi-monitor-config.json, calibration.json
- Existing test suite (24 test files) verified via `Glob tests/test_*.py`
- `.planning/research/PITFALLS.md` -- detailed filter cascade analysis
- `.planning/research/FEATURES.md` -- feature maturity assessment
- `.planning/REQUIREMENTS.md` -- EXEC-01 through EXEC-07 definitions

### Secondary (MEDIUM confidence)
- `.planning/STATE.md` -- project decisions from Phases 1-3
- `.planning/ROADMAP.md` -- planned sub-tasks for Phase 4

### Tertiary (LOW confidence)
- Cleveland Fed scraper status -- needs live verification during implementation
- HDD Sanity CMS availability -- needs live verification
- BeatRelease blog posting frequency -- needs manual check

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new libraries needed; all code exists
- Architecture: HIGH -- all bot patterns well-understood from codebase analysis
- Pitfalls: HIGH -- filter cascade problem is well-documented and confirmed by 99.7% skip rate evidence
- Data source health: LOW -- Cleveland Fed, HDD, BeatRelease all need live verification

**Research date:** 2026-02-27
**Valid until:** 2026-03-27 (stable -- code changes are internal tuning, not external dependency updates)
