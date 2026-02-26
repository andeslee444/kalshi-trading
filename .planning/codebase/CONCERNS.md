# Codebase Concerns

**Analysis Date:** 2026-02-26

## Tech Debt

**Fee Treatment Bug (Known):**
- Issue: Three-way split in how fees are handled across bots. Some use `edge_after_fees()` (deprecated), others pass `fee_cents` to Kelly functions (correct), others ignore fees entirely.
- Files: `src/kalshi/weather-bot.py`, `src/kalshi/crypto-bot.py`, `src/kalshi/economics-bot.py`, `src/kalshi/source-monitor.py`, `src/kalshi/entertainment-bot.py`, `src/kalshi/strategy-trader.py`
- Impact: Inconsistent Kelly sizing across bots. `edge_after_fees()` undersizes conservatively; ignoring fees also undersizes but differently. Not dangerous, but introduces ~5-15% variance in position sizing.
- Fix approach: Audit all 6 bots, standardize on single pattern: pass `fee_cents` parameter to all `half_kelly()` and `half_kelly_sell()` calls; remove `edge_after_fees()` calls entirely.
- Status: Audit script detects this (section 4.1) but fix is deferred.

**Bankroll Basis Risk (WARN):**
- Issue: Kelly sizing uses total portfolio balance rather than available balance (locked capital from open positions). During high-activity periods, new trades may be sized against "phantom" capital.
- Files: `src/kalshi/capital_allocator.py`, `src/kalshi/probability.py` (Kelly functions), bot implementations
- Impact: Position sizes ~2x too large when >50% of capital is locked. Violates Kelly criterion assumption of independent bankroll availability.
- Fix approach: Change `bankroll = available_balance` in capital allocator and verify all Kelly calls use available_balance_cents, not total_balance.
- Status: Audit (4.5) detects this; fix should be high priority.

**Uncalibrated Sigma Parameters (Medium):**
- Issue: Some probability models lack calibration support or use stale calibration. NWS sigma has both continuous exponential model and legacy step-function, creating maintenance burden.
- Files: `src/kalshi/probability.py` (lines 273-299 for dual models), `config/calibration.json`
- Impact: Model probabilities can drift 10-20% from reality if calibration isn't updated monthly. Economics nowcast sigma is hardcoded step function.
- Fix approach: Extend `calibrate-sigma.py` to support all models. Retire legacy step-function in favor of continuous models.

**Market Cache Staleness:**
- Issue: Shared market cache (`data/market-cache.json`) has 60-second TTL but can be invalidated independently by each bot. No TTL on individual prefixes within the cache dict.
- Files: `src/kalshi/kalshi_auth.py` (lines 287-340 `get_all_markets()`, 405-427 `read_market_cache()`)
- Impact: One bot may see stale market list (KXHIGH markets from 61s ago) while another sees fresh data. Worst case: skipping valid markets due to outdated list.
- Fix approach: Add per-prefix timestamp in cache dict; check age per prefix, not globally. Or increase TTL to 120s.

## Known Bugs

**Settlement Field Inconsistency:**
- Symptoms: Some API responses use `ticker`, others use `market_ticker`. Reconciliation may miss settlements.
- Files: `scripts/reconcile-trades.py` (line 60 handles both), `scripts/backfill-settlements.py`
- Trigger: API response variation across settlement endpoints
- Workaround: Code already handles both (`s.get("ticker", s.get("market_ticker", ""))`)
- Status: Fixed in reconcile script but indicates upstream API inconsistency.

**HDD Parser Fail-Open Behavior:**
- Symptoms: Unparseable dates in HDD data silently pass through (not filtered as stale). If API changes date format, old entries never age out.
- Files: `src/kalshi/hdd_parser.py` (lines 82-83 in `scrape_album_data()`)
- Trigger: HDD API changes date format or returns missing dates
- Impact: Stale data accumulation; extremely old albums may still trade if timestamp parsing fails
- Fix approach: Enforce strict date parsing; log and skip entries with unparseable/missing dates instead of fail-open.

**Order Monitoring Race Condition:**
- Symptoms: Order status may be checked before fill data propagates to API. Position monitor may skip exits on new orders with "pending" status.
- Files: `src/kalshi/position-monitor.py` (order_monitor usage), `src/kalshi/kalshi_auth.py` (OrderMonitor class)
- Trigger: Very fast fills (< 2 seconds) or API delays in settlement propagation
- Impact: Rare, but take-profit may not fire on fast-filled orders if monitor checks before fill appears in API
- Fix approach: Retry order status check if "pending" detected; implement 5-second polling before giving up.

## Security Considerations

**RSA Private Key File Permissions:**
- Risk: Private keys stored in `config/keys/` (gitignored) but no chmod 600 enforcement. Readable by other processes on shared Mac Mini.
- Files: `src/kalshi/kalshi_auth.py` (line 147 loads key), `config/keys/` (not committed but created at runtime)
- Current mitigation: `.gitignore` prevents commit, file is on local disk only
- Recommendations:
  1. Add startup check: `if key_file.stat().st_mode & 0o077: raise PermissionError()`
  2. Document: "Run `chmod 600 config/keys/*.pem` after setup"
  3. Consider: Move keys to pass-protected store (Keychain) for production

**API Key in Environment/Logs:**
- Risk: `KALSHI_API_KEY` and `KALSHI_KEY_FILE` in `.env` could be logged or leaked in error messages
- Files: `src/kalshi/kalshi_auth.py` (lines 135-139 load from env), logs written to `data/logs/`
- Current mitigation: Keys not printed in log messages; error messages show path, not key value
- Recommendations:
  1. Verify no bot prints `api_key` or env vars in logs
  2. Add log sanitizer: strip anything matching API key pattern before writing
  3. Document: "Keep `.env` out of git, use pass or 1Password for CI/CD"

**Production Mode Safety Gate (Good):**
- Current: `KALSHI_CONFIRM_PRODUCTION=yes` env var required, checked at `KalshiClient` init
- Files: `src/kalshi/kalshi_auth.py` (lines 160-165)
- Recommendation: Add second gate in `TradeManager.place_order()` to catch env var unset after client creation

**Webhook Notifications (Info):**
- Risk: Webhook URL stored in `.env` or hardcoded. Could leak trading activity to external systems.
- Files: `src/kalshi/kalshi_auth.py` (notify_webhook function), bot implementations
- Current mitigation: `.env` is gitignored, webhook is optional
- Recommendations: Document that webhook URL should be trusted endpoint only; add TLS verification

## Performance Bottlenecks

**Market Cache Bypass in Busy Periods:**
- Problem: If 3+ bots fetch KXHIGH simultaneously, shared cache may not exist yet. All 3 hit API instead of 1.
- Files: `src/kalshi/kalshi_auth.py` (lines 288-294 shared cache read)
- Cause: No file-lock protection on cache write; concurrent reads race with write
- Improvement path: Wrap shared cache write in fcntl lock; readers wait for lock release before retrying. Saves 200-400ms per bot per scan when cache is fresh.

**Calibration Lazy-Load in Hot Path:**
- Problem: `_load_calibration()` is called every time `weather_probability()` is invoked. Reads from disk on every trade decision.
- Files: `src/kalshi/probability.py` (lines ~80 `_load_calibration()` with file read)
- Cause: No in-memory cache; assumes calibration rarely changes
- Improvement path: Cache calibration in module-level variable; invalidate on file mtime change. Saves 10-20ms per probability call.

**Order Monitor Polling Loop (5 min timeout):**
- Problem: If API is slow, order monitor waits 5 minutes for fills before giving up. During this window, position monitor can't exit the trade.
- Files: `src/kalshi/kalshi_auth.py` (OrderMonitor class), `src/kalshi/position-monitor.py`
- Cause: 5-minute polling interval is arbitrary; no exponential backoff
- Improvement path: Reduce polling interval to 10s with exponential backoff; timeout after 60s instead of 5 min.

**Capital Allocator State Lock Contention:**
- Problem: All 8 bots compete for exclusive lock on `data/allocator-state.json`. Under load, file lock holds can exceed 100ms.
- Files: `src/kalshi/capital_allocator.py` (lines 356-362, 433-437 fcntl locks)
- Cause: Serialized write access; no read-only shared lock option
- Improvement path: Implement tiered locking: read-only shared lock (fcntl.LOCK_SH) for budget requests, exclusive lock (LOCK_EX) only for state write.

## Fragile Areas

**Cross-Process File Synchronization (High Risk):**
- Files: `data/allocator-state.json`, `data/health-state.json`, `data/*-decisions.json`, `data/market-cache.json`
- Why fragile: File-based state with fcntl locks is vulnerable to:
  1. Crashed process that held lock (lock orphaned until reboot/timeout)
  2. Network mounts (NFS) where fcntl is unreliable
  3. Stale data if one bot crashes mid-write
  4. No transaction log; partial writes unrecoverable
- Safe modification: Always use `_atomic_write_json()` for writes; never direct file manipulation. Test with `fuser` to detect orphaned locks in production. Consider migrating to SQLite (with WAL mode) for higher-level locking.
- Test coverage: File lock tests in `test_trade_manager.py` are basic; no chaos tests for lock timeouts or orphaned locks.

**Probability Model Edge Cases (Medium Risk):**
- Files: `src/kalshi/probability.py` (Kelly formulas, CDF functions)
- Why fragile:
  1. `_regularized_beta_cf()` has max_iter=200; convergence not guaranteed for all (a,b) pairs
  2. `_ln_gamma()` Lanczos approximation may overflow for x > 170
  3. Kelly sizing with price_cents = 1 or 99 produces extreme position sizes (1-9900 contracts)
  4. Edge = 1.0 (100% probability) causes division by zero in Kelly formula
- Safe modification: Add bounds checks: `edge = max(0.001, min(0.999, edge))`, `price_cents = max(2, min(98, price_cents))`
- Test coverage: `test_probability.py` covers normal cases; no edge case tests for extreme prices or 100% edge.

**Outdoor Data Source Coupling (High Risk):**
- Files: `src/kalshi/hdd_parser.py` (Sanity CMS API), `src/kalshi/beatrelease-scanner.py` (BeatRelease blog + DeepSeek API), `src/kalshi/entertainment-bot.py` (Box Office Mojo scraping)
- Why fragile:
  1. HDD Sanity CMS API can change GROQ query schema or rate-limit without warning
  2. BeatRelease blog HTML structure changes require regex updates
  3. DeepSeek API key stored in unencrypted text file; no rotation mechanism
  4. Box Office Mojo scraping via BeautifulSoup is fragile to CSS changes
  5. No fallback sources if primary source fails (HDD only, no backup album charts)
- Safe modification: Version external APIs (GROQ queries, DeepSeek prompts). Add schema validation. Implement fallback sources. Rotate DeepSeek key monthly.
- Test coverage: `test_hdd_parser.py`, `test_entertainment.py` mock API responses; no tests for API schema changes or failures.

**Supervisor Crash Recovery (Medium Risk):**
- Files: `scripts/supervisor.py`
- Why fragile:
  1. If supervisor crashes, crashed bots don't auto-restart until supervisor restarts
  2. Zombie/hung processes not reaped if supervisor doesn't check heartbeats frequently enough (CHECK_INTERVAL = 30s)
  3. PID files can become stale if process is killed ungracefully
  4. Restart loop can oscillate if bot has a repeating crash-on-startup bug
- Safe modification: Run supervisor under systemd/launchd with auto-restart. Implement watchdog timer (systemd socket activation). Add process resource limits (memory, CPU) to prevent runaway bots.
- Test coverage: `test_supervisor.py` covers basic start/stop; no chaos tests for crash loops or hung processes.

**DeepSeek LLM Parsing (Medium Risk):**
- Files: `src/kalshi/beatrelease-scanner.py` (lines 200-270 LLM parsing)
- Why fragile: DeepSeek response format is unstructured JSON. Regex parsing of "side" and "price" fields is fragile:
  ```python
  match = re.search(r"\"side\":\s*\"(buy|sell)\"", response_text)
  ```
  If model changes phrasing (e.g., `"action": "buy"` instead of `"side"`), parsing fails silently and trade is skipped.
- Safe modification: Require strict JSON schema in prompt. Parse JSON, not regex. Validate all required fields present before trade.
- Test coverage: `test_beatrelease.py` mocks LLM responses; no tests for malformed JSON or unexpected field names.

## Scaling Limits

**Daily Trade Logs Without Cleanup:**
- Current capacity: ~5000 entries per log file (trimmed), ~10 log files = 50k total trade records
- Limit: Trade file reads/writes become slow when files exceed 10MB (100k records). JSON parsing 100k-entry file = 500ms.
- Scaling path:
  1. Switch to append-only JSON lines format (one trade per line) for O(1) append
  2. Implement trade archival: move trades older than 90 days to separate dated files
  3. Consider SQLite with index on ticker for fast lookups (trades.db)

**Market Cache Growth:**
- Current capacity: ~10 ticker prefixes cached, ~500 markets per prefix = 5k markets, ~5MB JSON
- Limit: Shared market cache becomes slow if fetching 20+ prefixes (cache file I/O contention)
- Scaling path: Migrate to in-memory cache per bot (no shared file), use API caching headers (ETag) to avoid redundant fetches

**Capital Allocator State Lock:**
- Current capacity: 8 concurrent bots, each hitting allocator ~2x per scan
- Limit: File lock contention spikes under high load. If allocator takes 300ms and 8 bots queue, total delay = 2.4 seconds
- Scaling path: Split allocator into per-market-type state files (weather.json, entertainment.json, crypto.json) to reduce lock contention. Or use Redis for sub-millisecond distributed locking.

**DeepSeek API Rate Limits:**
- Current usage: Beatrelease scanner calls DeepSeek API ~1x per 4 hours = 6 calls/day
- Limit: Free tier likely has 10-100 calls/day limit. If more frequent scanning added, will hit rate limit.
- Scaling path: Batch LLM calls (analyze 5 posts in one call). Cache LLM results by content hash. Implement exponential backoff for 429 responses.

## Dependencies at Risk

**BeautifulSoup4 Web Scraping:**
- Risk: Entertainment bot scrapes Box Office Mojo and The Numbers using BeautifulSoup. Changes to CSS structure break parsing.
- Impact: Bot can't fetch box office data, stops trading entertainment markets.
- Current mitigation: Sanity CMS API is preferred source; box office is fallback
- Migration plan: Implement Box Office Mojo API (if available) or replace with alternative data source (Rotten Tomatoes API). Or use Playwright for more robust HTML parsing.

**Requests Library (HTTP):**
- Risk: External dependency; if future version breaks API compatibility, bots can't fetch data
- Current usage: All external API calls use requests (weather, HDD, box office, Kalshi, Polymarket)
- Migration plan: Already pinned in requirements.txt; regularly update and test. Or switch to httpx for modern async support.

**DeepSeek API (External):**
- Risk: Proprietary API could change pricing, rate limits, or shut down with notice. LLM output is nondeterministic.
- Impact: If DeepSeek shuts down, beatrelease scanner becomes non-functional
- Current mitigation: Optional feature; manual trading possible
- Migration plan: Add OpenAI/Claude API as fallback. Or implement regex-based parsing as fallback (no LLM).

**cryptography Library:**
- Risk: RSA-PSS signing is CPU-intensive; if library becomes unmaintained, security fixes may lag
- Current usage: Kalshi API authentication (RSA-PSS with SHA256)
- Status: Actively maintained by Python Cryptographic Authority
- Recommendation: Monitor for security advisories; auto-update via dependabot

## Missing Critical Features

**Distributed Tracing:**
- Problem: No request ID or trace ID across bot → API → settlement flows. Can't correlate trades end-to-end.
- Blocks: Root cause analysis of trade settlement discrepancies; hard to debug order-to-fill lag
- Improvement: Add trace_id to all log messages and API request headers. Write to structured log (JSON).

**Bid-Ask Spread Monitoring:**
- Problem: No tracking of market liquidity at decision time. Could place limit orders in markets with 50%+ spread and get bad fills.
- Blocks: Optimization of limit prices; risk of order rejection due to illiquidity
- Improvement: Log `best_bid`, `best_ask` in trade record (already done). Add bot that monitors spread trends and alerts on low liquidity.

**Model Performance Tracking:**
- Problem: No automated comparison of predicted vs actual market results at scale. Edge calculations are not validated.
- Blocks: Detecting when probability models degrade (e.g., after market structure change)
- Improvement: Audit script (scripts/audit.py) has section 1.3 (golden record coverage) but doesn't compute Brier score distribution or calibration curve

**Circuit Breaker Granularity:**
- Problem: Single circuit breaker for entire system. If Kalshi API is down, all bots halt.
- Blocks: Partial trading (e.g., weather trades OK but entertainment API fails)
- Improvement: Per-market-type or per-bot circuit breakers. Or per-endpoint breakers (GET vs POST).

## Test Coverage Gaps

**TradeManager Safety Checks:**
- Untested: Interaction between kill switch and circuit breaker. What if kill switch trips while circuit breaker is recovering? Is circuit breaker state cleared?
- Files: `src/kalshi/kalshi_auth.py` (TradeManager class), `tests/test_trade_manager.py`
- Risk: Stale circuit breaker state could persist after market recovery
- Priority: High — directly impacts capital safety

**File Lock Race Conditions:**
- Untested: What happens if process crashes while holding fcntl lock? Are locks properly released on SIGKILL?
- Files: `src/kalshi/kalshi_auth.py` (save_trade, trim_trade_log functions with fcntl)
- Risk: Orphaned locks could deadlock all bots
- Priority: High — production reliability blocker

**Capital Allocator Under Contention:**
- Untested: Allocator state consistency when 8 bots request budget simultaneously
- Files: `src/kalshi/capital_allocator.py`, `tests/test_allocator.py`
- Risk: Race conditions in per-bot or per-ticker fractional accounting
- Priority: Medium — affects position sizing correctness

**Cross-Platform Arbitrage:**
- Untested: Integration with Polymarket CLOB API. No live tests against Polymarket.
- Files: `src/kalshi/polymarket_client.py`, `src/kalshi/cross-platform-arb.py`, `tests/test_arb.py`
- Risk: Polymarket API errors propagate unhandled; arb bot crashes
- Priority: Low — arb is monitoring-only, disabled by default

**Economics Nowcast Margin of Safety:**
- Untested: When CPI/GDP/Jobs actual release is within nowcast sigma, does model correctly decline to trade? Edge computation at release time (0 days out).
- Files: `src/kalshi/economics-bot.py`, `src/kalshi/probability.py`, `tests/test_economics.py`
- Risk: Trading with 0.01% sigma (release time) could cause extreme position sizes or skipped trades
- Priority: Medium — nowcast edge can be 0% near release

**Supervisor Crash Resilience:**
- Untested: What if supervisor crashes and restarts? Does it correctly detect crashed bots and restart them? Are processes double-started?
- Files: `scripts/supervisor.py`, `tests/test_supervisor.py`
- Risk: Bot may be started twice (duplicate PID files, race condition)
- Priority: Medium — affects operational reliability

---

*Concerns audit: 2026-02-26*
