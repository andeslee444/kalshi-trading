---
phase: quick-2
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/weather-bot.py
  - src/kalshi/probability.py
  - src/kalshi/ticker_utils.py
  - tests/test_weather_bot_bugs.py
autonomous: true
requirements: [WX-BUG-1, WX-BUG-2, WX-BUG-3, WX-BUG-4]

must_haves:
  truths:
    - "Weather bot logs unparseable tickers with the raw ticker string so we can diagnose which formats fail"
    - "Weather bot never places a trade with negative edge"
    - "Liquidity filter is relaxed for near-settlement weather markets so more markets are evaluated"
    - "Sigma defaults are tightened to reduce over-uncertainty (Brier improvement)"
  artifacts:
    - path: "src/kalshi/weather-bot.py"
      provides: "Negative-edge guard, unparseable ticker logging, relaxed liquidity for near-settlement"
    - path: "src/kalshi/probability.py"
      provides: "Tightened default sigma intercept and relaxed MIN_LIQUIDITY_VOLUME"
    - path: "tests/test_weather_bot_bugs.py"
      provides: "Tests for negative edge guard, sigma tightening, liquidity relaxation"
  key_links:
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/probability.py"
      via: "weather_probability() and is_market_liquid()"
      pattern: "weather_probability|is_market_liquid"
---

<objective>
Fix four weather bot bugs that prevent it from finding and trading opportunities:
(1) sigma too large causing inverted calibration,
(2) 32% of tickers unparseable with no diagnostic logging,
(3) liquidity filter too strict (46% markets rejected),
(4) negative-edge trades possible.

Purpose: The weather bot has placed zero trades recently despite 97 available markets. These four bugs collectively explain the zero-opportunity outcome.
Output: Fixed weather bot with tighter sigma, diagnostic ticker logging, relaxed liquidity for near-settlement markets, and a negative-edge guard.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docs/plans/bot-improvements/03-weather-bot.md
@src/kalshi/weather-bot.py
@src/kalshi/probability.py
@src/kalshi/ticker_utils.py
@config/kalshi-config.json
@tests/test_weather.py
@tests/test_ticker_utils.py
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add negative-edge guard, diagnostic ticker logging, and relaxed near-settlement liquidity</name>
  <files>src/kalshi/weather-bot.py, src/kalshi/probability.py, tests/test_weather_bot_bugs.py</files>
  <behavior>
    - Test: negative edge values (edge < 0) are rejected and never passed to place_order
    - Test: is_market_liquid with reduced volume threshold (10 instead of 50) passes markets that would otherwise be rejected
    - Test: weather_probability with tightened sigma (intercept 1.5 instead of 2.0) produces tighter probabilities (higher confidence farther from threshold)
    - Test: weather_sigma returns smaller sigma with the new default intercept
  </behavior>
  <action>
    1. In `src/kalshi/probability.py`:
       - Change default sigma intercept from 2.0 to 1.5 on line 249 (and the matching line 290 in weather_sigma). The backtest Brier 0.321 and calibration table show sigma is systematically too large: low-prob events win 37% of the time (should be <20%) and high-prob events win only 53% (should be >70%). Reducing intercept by 25% from 2.0 to 1.5 tightens the distribution to match observed forecast error. NWS verification data shows day-0 MAE closer to 1.5F for major airports.
       - Change MIN_LIQUIDITY_VOLUME from 50 to 10 on line 1213. Weather markets are binary contracts on temperature thresholds — most have thin books but are still tradeable. 50 is too strict for weather markets where a single large bet can drive volume. Keep MAX_SPREAD_FOR_ENTRY at 20 cents (spread matters more than volume for execution quality).

    2. In `src/kalshi/weather-bot.py`:
       - After computing `edge_yes` (around line 255-256), add a guard: if `edge_yes < 0`, skip the market with `ss.skip("negative_edge")` and `trade_manager.log_decision(...)`. This prevents the bot from ever placing a negative-EV trade, which the trade log shows has happened historically.
       - In the ticker parsing section (around line 171-173), when `parse_ticker(ticker)` returns None, log the unparseable ticker at WARNING level: `log.warning("Unparseable KXHIGH ticker: %s", ticker)`. This will surface which specific ticker formats are failing so we can fix the regex in ticker_utils.py. Currently the bot silently skips them with `ss.skip("no_parse")` and we have no visibility.
       - For the liquidity filter (line 242), pass a relaxed volume threshold for near-settlement markets: `if days_out is not None and days_out <= 1: is_liquid = is_market_liquid(m, min_volume=5)` else use the default `is_market_liquid(m)`. Near-settlement markets (0-1 days out) have the best forecast accuracy AND most trading interest, so we should use a more lenient volume filter. NOTE: days_out is computed after parsing (line 186-189), but the liquidity check is at line 242 before we use days_out. Need to reorganize: move the `is_market_liquid` check AFTER the days_out computation (which is at line 186-189) — it is already after it in the current flow, so we can just use the existing `days_out` variable. Actually looking more carefully: days_out is computed at lines 186-189 BEFORE the liquidity check at line 242, so we can simply use it.

    3. Create `tests/test_weather_bot_bugs.py` with tests:
       - Test `is_market_liquid` with volume=10 passes (verifying the reduced threshold)
       - Test `weather_probability` produces higher probabilities far above threshold with intercept 1.5 vs 2.0 (import and call directly, reset calibration)
       - Test `weather_sigma` returns 1.5 for days_out=0 with no calibration (was 2.0)
       - Test `weather_sigma` returns ~2.21 for days_out=2 (1.5 + 0.5*sqrt(2)) with no calibration (was ~2.71)
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_weather_bot_bugs.py tests/test_weather.py tests/test_ticker_utils.py -x -v</automated>
  </verify>
  <done>
    - weather_sigma(0) returns 1.5 (not 2.0)
    - MIN_LIQUIDITY_VOLUME is 10 (not 50)
    - weather-bot.py has explicit `if edge_yes < 0: skip` guard
    - weather-bot.py logs unparseable tickers at WARNING level
    - weather-bot.py uses relaxed liquidity for days_out <= 1
    - All existing weather and ticker tests still pass
    - New tests pass
  </done>
</task>

</tasks>

<verification>
1. `pytest tests/test_weather_bot_bugs.py -v` — all new tests pass
2. `pytest tests/test_weather.py tests/test_ticker_utils.py -v` — no regressions in existing tests
3. `pytest tests/ -x` — full test suite passes (no regressions across the codebase)
4. `grep -n "edge_yes < 0" src/kalshi/weather-bot.py` — negative edge guard exists
5. `grep -n "Unparseable" src/kalshi/weather-bot.py` — diagnostic logging exists
6. `grep -n "MIN_LIQUIDITY_VOLUME = 10" src/kalshi/probability.py` — volume threshold reduced
7. `grep -n "intercept = 1.5" src/kalshi/probability.py` — sigma intercept tightened
</verification>

<success_criteria>
- Sigma intercept reduced from 2.0 to 1.5 (tighter distribution, better calibration)
- MIN_LIQUIDITY_VOLUME reduced from 50 to 10 (more markets pass filter)
- Negative-edge trades impossible (explicit guard before trade placement)
- Unparseable tickers logged with raw ticker string for debugging
- Near-settlement markets (<=1 day) use relaxed liquidity threshold
- All existing tests pass (no regressions)
</success_criteria>

<output>
After completion, create `.planning/quick/2-fix-weather-bot-bugs-calibration-ticker-/2-SUMMARY.md`
</output>
