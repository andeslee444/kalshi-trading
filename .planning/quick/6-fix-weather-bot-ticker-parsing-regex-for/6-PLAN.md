---
phase: quick-6
plan: 1
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/weather-bot.py
  - src/kalshi/ticker_utils.py
  - config/calibration.json
  - tests/test_ticker_utils.py
autonomous: true
requirements: [TICKER-FIX, CALIBRATION-PURGE]

must_haves:
  truths:
    - "All KXHIGH weather tickers from Kalshi API parse successfully"
    - "Non-weather KXHIGHINFLATION tickers are filtered out before parsing"
    - "Stale calibration.json per-city and global sigma overrides are purged"
    - "Weather bot uses improved default sigma (1.5 intercept) not stale 4-5.5 values"
  artifacts:
    - path: "src/kalshi/weather-bot.py"
      provides: "KXHIGHINFLATION filter before parse_ticker"
    - path: "config/calibration.json"
      provides: "Purged weather sigma section (no per_city, no global_sigma_intercept)"
    - path: "tests/test_ticker_utils.py"
      provides: "Tests for KXHIGHINFLATION rejection, all config cities parse"
  key_links:
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/ticker_utils.py"
      via: "parse_ticker() call in scan_and_trade"
      pattern: "parse_ticker\\(ticker\\)"
    - from: "src/kalshi/probability.py"
      to: "config/calibration.json"
      via: "_load_calibration() lazy loading"
      pattern: "_load_calibration"
---

<objective>
Fix two high-impact weather bot issues: (1) filter out non-weather KXHIGHINFLATION tickers that share the KXHIGH prefix and pollute skip logs, and (2) purge stale calibration.json sigma overrides (4.15-5.50) that prevent the improved 1.5 default sigma from taking effect.

Purpose: The stale calibration values override the new sigma=1.5 default with values 3-4x larger, making the model too uncertain to find any edge. The KXHIGHINFLATION tickers add noise to scan logs.

Output: Weather bot finds edge with correct sigma; all valid weather tickers parsed; no stale overrides.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/weather-bot.py
@src/kalshi/ticker_utils.py
@src/kalshi/probability.py
@config/calibration.json
@config/kalshi-config.json
@tests/test_ticker_utils.py

<interfaces>
<!-- Key functions and patterns the executor needs -->

From src/kalshi/ticker_utils.py:
```python
def parse_weather_ticker(ticker):
    """Parse KXHIGH weather temperature tickers.
    Returns dict with city, date, direction, threshold or None."""
    m = re.match(r"KXHIGHT?([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
```

From src/kalshi/probability.py (lines 306-317):
```python
cal = _load_calibration()
intercept = 1.5  # Default (improved in quick task 2)
slope = 0.5
weather_cal = cal.get("weather", {})
if city and city in weather_cal.get("per_city", {}):
    city_cal = weather_cal["per_city"][city]
    intercept = city_cal.get("sigma_intercept", intercept)  # OVERRIDES 1.5 with stale values!
elif weather_cal.get("global_sigma_intercept") is not None:
    intercept = weather_cal["global_sigma_intercept"]  # OVERRIDES 1.5 with 5.5!
```

From src/kalshi/weather-bot.py (lines 211-218):
```python
for m in markets:
    ticker = m.get("ticker", "")
    parsed = parse_ticker(ticker)
    if not parsed:
        log.warning("Unparseable KXHIGH ticker: %s", ticker)
        ss.skip("no_parse")
        continue
```

Current calibration.json weather section:
```json
{
  "weather": {
    "global_sigma_intercept": 5.5,  // Overrides 1.5 default for ALL cities!
    "global_sigma_slope": 0.2,
    "per_city": {
      "DEN": {"sigma_intercept": 4.15, ...},
      "AUS": {"sigma_intercept": 4.86, ...},
      "CHI": {"sigma_intercept": 4.82, ...},
      "NY": {"sigma_intercept": 3.62, ...}
    }
  }
}
```

Impact: With calibration loaded, sigma at day-0 is 3.62-5.50 instead of 1.50.
Config cities: MIA, LAX, PHIL, NY, CHI, AUS, DEN, HOU, ATL, BOS, SFO, SEA, LV, DAL, MIN, PHX, DC, NOLA, OKC, SATX
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Filter KXHIGHINFLATION tickers and add regression tests</name>
  <files>src/kalshi/weather-bot.py, tests/test_ticker_utils.py</files>
  <behavior>
    - Test: parse_weather_ticker("KXHIGHINFLATION-26DEC-T3.5") returns None (already true)
    - Test: All 20 config city codes parse successfully in both KXHIGH{CITY} and KXHIGHT{CITY} formats
    - Test: parse_weather_ticker returns None for KXHIGHINFLATION variants (KXHIGHINFLATION-26DEC-T3.0 through T3.5)
    - Verify: weather-bot.py filters KXHIGHINFLATION tickers BEFORE calling parse_ticker to avoid noisy "Unparseable" warnings
  </behavior>
  <action>
    1. In `tests/test_ticker_utils.py`, add a new test class or methods:
       - `test_kxhighinflation_not_weather`: Verify parse_weather_ticker returns None for "KXHIGHINFLATION-26DEC-T3.5", "KXHIGHINFLATION-26DEC-T3.0", etc. (These ALREADY return None from the regex, but we need explicit regression tests.)
       - `test_all_config_cities_parse`: For each of the 20 config city codes (MIA, LAX, PHIL, NY, CHI, AUS, DEN, HOU, ATL, BOS, SFO, SEA, LV, DAL, MIN, PHX, DC, NOLA, OKC, SATX), generate a ticker like `KXHIGH{CITY}-26MAR05-T70` and verify parse_weather_ticker returns a valid result with correct city code.
       - `test_all_t_prefix_cities_parse`: Same cities but with `KXHIGHT{CITY}-26MAR05-T70` format. Note: The T-prefix format is how Kalshi formats newer cities. The T is optional in the regex.

    2. In `src/kalshi/weather-bot.py`, in the `scan_and_trade()` function, add a filter BEFORE the parse_ticker call (around line 213-214) to skip non-weather KXHIGH tickers:
       ```python
       # Filter non-weather KXHIGH tickers (e.g., KXHIGHINFLATION)
       if not ticker.startswith("KXHIGH") or ticker.startswith("KXHIGHINFLATION"):
           continue
       ```
       This silently skips them without logging a warning (they aren't weather markets, so "Unparseable" is misleading). Do NOT count them in ss.skip() since they aren't weather markets at all.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_ticker_utils.py -v -x 2>&1 | tail -30</automated>
  </verify>
  <done>All existing ticker tests pass. New regression tests confirm KXHIGHINFLATION returns None. All 20 config cities parse in both formats. Weather bot filters KXHIGHINFLATION before parse_ticker.</done>
</task>

<task type="auto">
  <name>Task 2: Purge stale calibration.json weather sigma overrides</name>
  <files>config/calibration.json</files>
  <action>
    Read `config/calibration.json` and remove all stale weather sigma overrides that block the improved 1.5 default:

    1. Remove `weather.global_sigma_intercept` (currently 5.5 -- overrides 1.5 for ALL cities)
    2. Remove `weather.global_sigma_slope` (currently 0.2 -- overrides 0.5 default)
    3. Remove `weather.per_city` entirely (DEN=4.15, AUS=4.86, CHI=4.82, NY=3.62 all override 1.5)
    4. Keep `weather.global_brier`, `weather.global_log_loss`, `weather.global_pnl_cents`, `weather.n` as historical reference data (read-only, not used by probability.py for sigma)
    5. Keep `weather.df` at 30 -- this controls degrees of freedom for Student-t and is separate from sigma calibration. Actually, check: the default df in probability.py is 6. The calibration has df=30 which makes the Student-t nearly Gaussian. Since the new skew-normal model from quick task 3 was designed with df=6 in mind, reset weather.df to 6 as well (or remove it so the default takes effect).
    6. Keep all non-weather sections (nws, album_sales, box_office, ensemble) untouched.

    The resulting weather section should look like:
    ```json
    "weather": {
      "global_brier": 0.309726,
      "global_log_loss": 0.937282,
      "global_pnl_cents": 36643,
      "n": 69
    }
    ```

    This ensures `_load_calibration()` finds no sigma overrides, so `weather_probability()` uses its hardcoded intercept=1.5, slope=0.5, df=6 defaults.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -c "
import sys, json
sys.path.insert(0, 'src/kalshi')
from probability import weather_sigma, _reset_calibration, _load_calibration

# Force reload
_reset_calibration()
import probability
probability._calibration = None

cal = _load_calibration()
wc = cal.get('weather', {})
assert 'global_sigma_intercept' not in wc, f'global_sigma_intercept still present: {wc}'
assert 'per_city' not in wc, f'per_city still present: {wc}'

# Verify sigma is now 1.5 for day-0
_reset_calibration()
probability._calibration = None
s = weather_sigma(0, 'DEN')
assert abs(s - 1.5) < 0.01, f'Expected 1.5, got {s}'
s3 = weather_sigma(3, 'DEN')
expected = 1.5 + 0.5 * (3**0.5)
assert abs(s3 - expected) < 0.01, f'Expected {expected:.2f}, got {s3}'
print('PASS: calibration purged, sigma defaults restored')
" 2>&1</automated>
  </verify>
  <done>calibration.json weather section has no sigma overrides. weather_sigma(0, any_city) returns 1.5 (the improved default). weather_sigma(3, any_city) returns ~2.37. The skew-normal model from quick task 3 operates at its intended scale.</done>
</task>

<task type="auto">
  <name>Task 3: Run full test suite to confirm no regressions</name>
  <files></files>
  <action>
    Run the full test suite to confirm nothing is broken by the changes. Pay special attention to:
    - `tests/test_ticker_utils.py` (ticker parsing, including new tests)
    - `tests/test_probability.py` or any probability-related tests (sigma values changed)
    - Any test that might load calibration.json

    Note: Tests that use `_reset_calibration()` in setup/teardown (as per CLAUDE.md test patterns) should be unaffected since they clear cached calibration. But tests that DON'T reset calibration and depend on specific sigma values may need updating.

    If any probability tests fail due to expected sigma values changing (from stale overrides to 1.5), update those test expectations to match the new correct defaults. The new defaults ARE correct -- the stale values were the bug.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/ -x --timeout=60 2>&1 | tail -20</automated>
  </verify>
  <done>Full test suite passes with 0 failures. No regressions from ticker filter or calibration purge.</done>
</task>

</tasks>

<verification>
1. `python3 -m pytest tests/test_ticker_utils.py -v` -- all ticker tests pass including new regression tests
2. `python3 -c "from probability import weather_sigma, _load_calibration; cal = _load_calibration(); assert 'per_city' not in cal.get('weather', {}); assert weather_sigma(0, 'DEN') < 2.0; print('OK')"` -- sigma defaults restored
3. `python3 -m pytest tests/ -x` -- full test suite passes
</verification>

<success_criteria>
- KXHIGHINFLATION tickers are filtered before parse_ticker (no noisy "Unparseable" warnings)
- All 20 config city codes have regression tests for both KXHIGH and KXHIGHT prefix formats
- calibration.json has no weather sigma overrides (no global_sigma_intercept, no per_city)
- weather_sigma(0, any_city) returns 1.5 (not 3.62-5.50)
- Full test suite passes
</success_criteria>

<output>
After completion, create `.planning/quick/6-fix-weather-bot-ticker-parsing-regex-for/6-SUMMARY.md`
</output>
