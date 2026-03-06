---
phase: quick-12
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/weather_data.py
  - src/kalshi/weather-bot.py
  - config/kalshi-config.json
  - tests/test_weather_data.py
  - tests/test_weather_bot_phase3.py
autonomous: true
requirements: [HRRR-INTEGRATION, ADAPTIVE-CADENCE, ORDERBOOK-DEPTH, MODEL-RUN-TIMING]

must_haves:
  truths:
    - "HRRR forecast data is fetched for all 20 cities and blended into the ensemble with day-dependent weighting"
    - "Bot scans every 5 min for day-0 markets, 15 min for day-1, 30 min for day-2+"
    - "Bot triggers scans within 2 minutes of new GFS/ECMWF/HRRR model runs becoming available"
    - "Order book depth is fetched for candidate markets and used to improve limit pricing"
  artifacts:
    - path: "src/kalshi/weather_data.py"
      provides: "HRRRFetcher class for HRRR deterministic forecast data"
      contains: "class HRRRFetcher"
    - path: "src/kalshi/weather-bot.py"
      provides: "Adaptive scan cadence, HRRR blending, orderbook depth, model-run timing"
      contains: "def compute_adaptive_interval"
    - path: "tests/test_weather_data.py"
      provides: "Tests for HRRRFetcher"
      contains: "TestHRRRFetcher"
    - path: "tests/test_weather_bot_phase3.py"
      provides: "Tests for adaptive cadence, HRRR blending, orderbook depth, model-run timing"
      contains: "test_adaptive_interval"
  key_links:
    - from: "src/kalshi/weather_data.py"
      to: "api.open-meteo.com"
      via: "HRRRFetcher.fetch_hrrr() calls Open-Meteo with model=hrrr_conus"
      pattern: "hrrr_conus"
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/weather_data.py"
      via: "imports HRRRFetcher and uses in scan_and_trade"
      pattern: "from weather_data import.*HRRRFetcher"
    - from: "src/kalshi/weather-bot.py"
      to: "Kalshi API"
      via: "client.get(/markets/{ticker}/orderbook) for depth data"
      pattern: "orderbook"
---

<objective>
Implement weather bot v2 Phase 3: HRRR integration, adaptive scan cadence, order book depth fetching, and model-run timing awareness.

Purpose: HRRR (3km resolution, hourly updates) dramatically improves day-0 and day-1 forecasts where most trading edge exists. Adaptive cadence ensures the bot captures short-lived opportunities near settlement. Order book depth improves fill rates and limit pricing. Model-run timing prevents stale-data trades.

Output: Updated weather_data.py with HRRRFetcher, updated weather-bot.py with all four Phase 3 features, updated config, comprehensive tests.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/weather_data.py
@src/kalshi/weather-bot.py
@config/kalshi-config.json
@tests/test_weather_data.py
@src/kalshi/forecast_verifier.py
@src/kalshi/probability.py

IMPORTANT: Bot development scope rules — only modify weather-bot.py, weather_data.py, forecast_verifier.py, test_weather*.py, test_advanced_weather.py, test_empirical_ensemble.py, kalshi-config.json. Do NOT modify probability.py, kalshi_auth.py, or other bots.
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: HRRR data fetcher + order book depth utility in weather_data.py</name>
  <files>src/kalshi/weather_data.py, tests/test_weather_data.py</files>
  <behavior>
    - HRRRFetcher.fetch_hrrr(lat, lon) returns {date_str: temp_f} dict for next 48h from Open-Meteo hrrr_conus model
    - HRRRFetcher.fetch_hrrr() returns None on API failure (connection error, non-200 status)
    - HRRRFetcher.fetch_hrrr() skips None values in response
    - HRRRFetcher.fetch_hrrr() extracts daily max from hourly data (Open-Meteo hrrr_conus only provides hourly, not daily max)
    - OrderBookDepth.fetch_depth(client, ticker) returns {"yes_bids": [(price, qty)], "yes_asks": [(price, qty)], "total_bid_depth": int, "total_ask_depth": int} or None on failure
    - OrderBookDepth.estimate_fill_price(depth, side, quantity) returns estimated average fill price considering depth, or None if insufficient liquidity
    - MODEL_RUN_SCHEDULE dict maps model names to their UTC publication hours and typical delay minutes
    - next_model_run(now_utc) returns (model_name, minutes_until_available) for the soonest upcoming model run
  </behavior>
  <action>
Add to weather_data.py:

1. **HRRRFetcher class**: Fetches HRRR deterministic forecast from Open-Meteo.
   - URL: `https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&temperature_unit=fahrenheit&timezone=America%2FNew_York&models=hrrr_conus&forecast_days=2`
   - HRRR only provides hourly data, not daily max. Fetch `hourly.temperature_2m` and compute daily max by grouping hours into calendar days (using the date portion of `hourly.time` strings which are in format "2026-03-05T14:00").
   - Returns `{date_str: max_temp_f}` for the next ~48 hours (typically 2-3 calendar days).
   - Uses `_retry_request` like EnsembleCollector. Returns None on any failure.
   - Log number of hours fetched and dates computed.

2. **OrderBookDepth class**: Fetches and analyzes Kalshi order book.
   - `fetch_depth(client, ticker)`: Calls `client.get(f"/markets/{ticker}/orderbook")`. The API returns `{"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}`. Parse into structured dict with `yes_bids` (sorted descending by price), `yes_asks` (sorted ascending by price), and total depth on each side. The "yes" array contains bids for YES contracts, "no" array contains bids for NO contracts (which are equivalent to asks for YES at 100-price).
   - `estimate_fill_price(depth, side, quantity)`: Walk through the book (asks for buy, bids for sell) to estimate volume-weighted average fill price for `quantity` contracts. Return None if total available depth < quantity.
   - Returns None on API error. Log warning but don't crash — this is an optional enhancement.

3. **MODEL_RUN_SCHEDULE constant and next_model_run() function**:
   ```python
   MODEL_RUN_SCHEDULE = {
       "gfs": {"hours_utc": [0, 6, 12, 18], "delay_minutes": 210},     # ~3.5h processing
       "ecmwf": {"hours_utc": [0, 12], "delay_minutes": 360},           # ~6h processing
       "hrrr": {"hours_utc": list(range(24)), "delay_minutes": 45},     # hourly, ~45min delay
   }
   ```
   - `next_model_run(now_utc=None)`: Returns tuple `(model_name, minutes_until_available)` for the model run that will become available soonest. If `now_utc` is None, uses `datetime.datetime.utcnow()`. For each model, compute when its next output will be available (run hour + delay). Return the one with smallest positive minutes_until_available. If one is available NOW (minutes <= 0), return it with 0 minutes.

Add tests to test_weather_data.py in new test classes: TestHRRRFetcher (mock HTTP, test parsing of hourly->daily max, None handling, skip nulls), TestOrderBookDepth (mock client.get, test parsing, fill estimation, error handling), TestModelRunSchedule (test next_model_run at various UTC times).
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_weather_data.py -v -x</automated>
  </verify>
  <done>HRRRFetcher fetches and parses HRRR hourly data into daily maxes. OrderBookDepth fetches and analyzes Kalshi order book. MODEL_RUN_SCHEDULE and next_model_run() provide model timing awareness. All new functionality has passing tests with mocked HTTP.</done>
</task>

<task type="auto">
  <name>Task 2: Integrate HRRR blending, adaptive cadence, orderbook depth, and model-run timing into weather-bot.py</name>
  <files>src/kalshi/weather-bot.py, config/kalshi-config.json, tests/test_weather_bot_phase3.py</files>
  <action>
**Changes to config/kalshi-config.json:**
Add new config keys (do not remove any existing keys):
```json
{
  "hrrr": {
    "enabled": true,
    "weight_day0": 0.60,
    "weight_day1": 0.30
  },
  "adaptiveScan": {
    "enabled": true,
    "day0Minutes": 5,
    "day1Minutes": 15,
    "day2PlusMinutes": 30,
    "modelRunTriggerMinutes": 2
  },
  "orderbookDepth": {
    "enabled": true,
    "minDepthContracts": 5
  }
}
```

**Changes to weather-bot.py:**

1. **HRRR integration** (in scan_and_trade):
   - Import `HRRRFetcher` from `weather_data`.
   - Create `hrrr_fetcher = HRRRFetcher(logger=log)` alongside `ensemble_collector`.
   - In the forecast fetching section, fetch HRRR data for each city: `hrrr_data[code] = hrrr_fetcher.fetch_hrrr(info["lat"], info["lon"])`.
   - When computing probability for a market, if HRRR data exists for that city+date and `days_out <= 1`:
     - For empirical CDF path: Inject HRRR forecast as additional "members" with appropriate weight. If `days_out == 0`, replicate the HRRR temp 60% of the member count (e.g., if 82 members, add ~49 HRRR values). For `days_out == 1`, replicate 30% of member count (~25 values). This naturally weights the empirical CDF toward HRRR.
     - For parametric fallback path: Blend HRRR into ensemble_data dict as `"hrrr": hrrr_temp` before passing to `ensemble_weather_probability_v2()`. The existing BMA weighting in probability.py will handle it if the model is in the dict, otherwise HRRR temp will be included as another forecast source in the mean calculation.
   - Log HRRR integration: `"HRRR temp for {ticker}: {temp}F (weight={weight})"`.
   - If HRRR fetch fails for a city, silently continue without it (non-blocking).

2. **Adaptive scan cadence** (in main loop):
   - Add function `compute_adaptive_interval(markets, config)` that:
     - Scans through all open KXHIGH markets
     - Parses ticker dates to compute `days_out` for each
     - Returns the appropriate scan interval: `day0Minutes` if any market is day-0, `day1Minutes` if nearest is day-1, `day2PlusMinutes` otherwise
   - Replace the fixed `interval = config["scanIntervalMinutes"]` in `main()` with:
     ```python
     base_interval = compute_adaptive_interval(markets, config)
     interval = base_interval
     ```
   - Pass the `markets` list from `scan_and_trade` back to `main` (or store as a module-level variable after each scan) so adaptive interval can access it. Simplest approach: have `scan_and_trade()` return the markets list, and main uses it.

3. **Model-run timing awareness** (in main loop):
   - Import `next_model_run` from `weather_data`.
   - After computing `base_interval`, check `next_model_run()`:
     ```python
     model, minutes_until = next_model_run()
     if minutes_until <= config.get("adaptiveScan", {}).get("modelRunTriggerMinutes", 2):
         interval = min(interval, max(1, minutes_until))
         log.info(f"New {model} run imminent in {minutes_until:.0f}min, scanning in {interval:.0f}min")
     ```
   - This overrides the adaptive interval when fresh model data is about to drop, so the bot catches it immediately.

4. **Order book depth** (in trade evaluation):
   - Import `OrderBookDepth` from `weather_data`.
   - Create `orderbook = OrderBookDepth(logger=log)` at module level.
   - In the opportunities evaluation loop (before trade placement), for each opportunity:
     ```python
     if config.get("orderbookDepth", {}).get("enabled", False):
         depth = orderbook.fetch_depth(client, ticker)
         if depth:
             min_depth = config.get("orderbookDepth", {}).get("minDepthContracts", 5)
             side_depth = depth["total_ask_depth"] if side == "yes" else depth["total_bid_depth"]
             if side_depth < min_depth:
                 log.info(f"  Skipping {ticker}: insufficient depth ({side_depth} < {min_depth})")
                 ss.skip("low_depth")
                 continue
             # Improve limit price using depth
             fill_price = orderbook.estimate_fill_price(depth, side, count)
             if fill_price and fill_price < price:
                 log.info(f"  {ticker}: depth suggests better fill at {fill_price}c (vs {price}c)")
                 price = fill_price
     ```
   - Add `orderbook_depth` field to trade_manager.place_order() kwargs for logging (pass the raw depth total, or None).

**Changes to scan_and_trade return value:**
- Have `scan_and_trade()` return the `markets` list (currently returns None implicitly). Change the end to `return markets` after `ss.finalize()`. In `main()`, capture this: `last_markets = scan_and_trade() or []`.

**Create tests/test_weather_bot_phase3.py:**
- Test `compute_adaptive_interval()` with various market mixes (day-0, day-1, day-2 only, no markets).
- Test HRRR member injection logic: given 82 ensemble members and HRRR temp, verify correct number of injected members for day-0 (60%) and day-1 (30%).
- Test model-run timing override: mock datetime.utcnow() and verify interval is shortened when model run is imminent.
- Test orderbook depth skip: verify market is skipped when depth < minDepthContracts.
- Use the same bot module stubbing pattern from test_kelly.py (stub kalshi_auth in sys.modules before importing).
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_weather_bot_phase3.py tests/test_weather_data.py -v -x</automated>
  </verify>
  <done>
    - HRRR forecasts are fetched and blended into ensemble with 60% weight for day-0, 30% for day-1, 0% for day-2+
    - Bot dynamically adjusts scan interval: 5min (day-0), 15min (day-1), 30min (day-2+)
    - Bot shortens interval when a new GFS/ECMWF/HRRR model run is about to become available
    - Order book depth is fetched for candidate trades; trades skipped if insufficient depth; limit prices improved based on book
    - All features gated behind config flags (hrrr.enabled, adaptiveScan.enabled, orderbookDepth.enabled) for safe rollout
    - All existing weather tests still pass
    - New phase 3 tests pass
  </done>
</task>

</tasks>

<verification>
```bash
# All weather-related tests pass
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_weather_data.py tests/test_weather_bot_phase3.py tests/test_weather.py tests/test_weather_bot_bugs.py tests/test_advanced_weather.py tests/test_empirical_ensemble.py -v

# Smoke: bot starts without errors in demo mode (Ctrl+C after first scan)
timeout 120 python3 src/kalshi/weather-bot.py 2>&1 | head -50
```
</verification>

<success_criteria>
- HRRRFetcher class in weather_data.py fetches and parses HRRR hourly data into daily maxes
- HRRR data blended into empirical CDF ensemble with day-dependent weighting (60%/30%/0%)
- Adaptive scan cadence returns 5/15/30 min based on nearest market settlement date
- Model-run timing shortens scan interval when fresh data is imminent
- Order book depth gating prevents trades into thin books; improves limit pricing when depth exists
- All features behind config flags for safe production rollout
- All weather bot tests pass (existing + new)
</success_criteria>

<output>
After completion, create `.planning/quick/12-implement-weather-bot-v2-phase-3-hrrr-in/12-SUMMARY.md`
</output>
