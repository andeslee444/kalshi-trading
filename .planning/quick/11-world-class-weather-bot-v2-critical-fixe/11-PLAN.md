---
phase: quick-11
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - config/kalshi-config.json
  - config/kalshi-monitor-config.json
  - src/kalshi/forecast_verifier.py
  - src/kalshi/weather-bot.py
  - src/kalshi/weather_data.py
  - src/kalshi/probability.py
  - scripts/backfill-weather-data.py
  - tests/test_weather_data.py
  - tests/test_empirical_ensemble.py
autonomous: true
requirements: []

must_haves:
  truths:
    - "NY coordinates in kalshi-config.json point to Central Park (40.7829, -73.9654), not JFK"
    - "NY station in kalshi-monitor-config.json is KNYC, not KJFK"
    - "ForecastVerifier fetches actual temps from IEM ASOS, not Open-Meteo ERA5"
    - "weather_data.py provides STATION_MAP, EnsembleCollector, IEMFetcher, and TrainingStore classes"
    - "empirical_ensemble_probability() in probability.py produces calibrated CDF from raw ensemble members"
    - "weather-bot.py uses empirical_ensemble_probability() as primary model with parametric fallback"
  artifacts:
    - path: "config/kalshi-config.json"
      provides: "Corrected NY coordinates and comment"
      contains: "40.7829"
    - path: "config/kalshi-monitor-config.json"
      provides: "Corrected NY station"
      contains: "KNYC"
    - path: "src/kalshi/forecast_verifier.py"
      provides: "IEM ASOS actual temp fetching"
      contains: "mesonet.agron.iastate.edu"
    - path: "src/kalshi/weather_data.py"
      provides: "Weather data infrastructure module"
      exports: ["STATION_MAP", "EnsembleCollector", "IEMFetcher", "TrainingStore"]
    - path: "src/kalshi/probability.py"
      provides: "Empirical ensemble CDF function"
      contains: "empirical_ensemble_probability"
    - path: "scripts/backfill-weather-data.py"
      provides: "Historical data backfill script"
    - path: "tests/test_weather_data.py"
      provides: "Tests for weather data infrastructure"
    - path: "tests/test_empirical_ensemble.py"
      provides: "Tests for empirical ensemble CDF"
  key_links:
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/weather_data.py"
      via: "EnsembleCollector import for fetching raw member data"
      pattern: "from weather_data import"
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/probability.py"
      via: "empirical_ensemble_probability() call as primary model"
      pattern: "empirical_ensemble_probability"
    - from: "src/kalshi/forecast_verifier.py"
      to: "IEM ASOS API"
      via: "HTTP fetch for actual station temperatures"
      pattern: "mesonet.agron.iastate.edu"
---

<objective>
Weather bot v2: Fix critical config errors (NY coordinates, station, verification source), build data infrastructure module for ensemble collection and IEM station actuals, and implement empirical ensemble CDF as the primary probability model.

Purpose: Transform the weather bot from parametric sigma guessing (Brier 0.31) to data-driven empirical ensemble CDF targeting Brier < 0.22. Fix the NY station error that causes 2-5 deg F bias in summer.
Output: Corrected configs, new weather_data.py module, empirical_ensemble_probability() function, integrated weather bot, backfill script, tests.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@config/kalshi-config.json
@config/kalshi-monitor-config.json
@src/kalshi/forecast_verifier.py
@src/kalshi/weather-bot.py
@src/kalshi/probability.py
@src/kalshi/ticker_utils.py
@tests/test_weather.py
@docs/plans/2026-03-05-weather-bot-world-class-design.md
</context>

<tasks>

<task type="auto">
  <name>Task 1: Critical fixes — NY coordinates, station, verifier data source, near-threshold filter</name>
  <files>config/kalshi-config.json, config/kalshi-monitor-config.json, src/kalshi/forecast_verifier.py, src/kalshi/weather-bot.py</files>
  <action>
  1. **config/kalshi-config.json**:
     - Line 2: Change the `_comment` from "NY uses KJFK (JFK Int'l), not KNYC (Central Park)" to "City coordinates are NWS airport weather station locations (not city centers). Kalshi settles against these stations. NY uses KNYC (Central Park)."
     - Line 15: Change NY lat from 40.6413 to 40.7829 and lon from -73.7781 to -73.9654 (Central Park, not JFK)

  2. **config/kalshi-monitor-config.json**:
     - Line 30: Change `"NY": "KJFK"` to `"NY": "KNYC"`

  3. **src/kalshi/forecast_verifier.py** — Replace `_fetch_actual_high()` method:
     - Instead of calling `https://archive-api.open-meteo.com/v1/archive?...&daily=temperature_2m_max`, call IEM ASOS endpoint
     - New URL pattern: `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={station_id}&data=max_tmpf&tz=America/New_York&format=comma&year1={Y}&month1={M}&day1={D}&year2={Y}&month2={M}&day2={D}`
     - The method signature changes: accept `station_id` (str) instead of `lat, lon`. E.g. `_fetch_actual_high(self, station_id, date_str)`
     - Parse the CSV response: skip header lines (starting with #), find the data row, extract `max_tmpf` column (returns temp in Fahrenheit as a float)
     - Handle the case where max_tmpf is "M" (missing) — return None
     - Update `verify_past_forecasts()` to pass station_id instead of lat/lon. It currently receives `city_coords` dict with lat/lon; change the parameter to accept a `station_map` dict of `{city_code: station_id}` (e.g. `{"NY": "KNYC", "MIA": "KMIA"}`). Keep backward compatibility: if the caller still passes the old format with lat/lon, log a deprecation warning and skip verification for that call.
     - Add a module-level `DEFAULT_STATION_MAP` dict with all 20 city codes mapped to IEM station IDs (use the station mapping from the design doc):
       ```
       MIA->KMIA, LAX->KLAX, PHIL->KPHL, NY->KNYC, CHI->KMDW, AUS->KAUS, DEN->KDEN, HOU->KHOU, ATL->KATL, BOS->KBOS, SFO->KSFO, SEA->KSEA, LV->KLAS, DAL->KDFW, MIN->KMSP, PHX->KPHX, DC->KDCA, NOLA->KMSY, OKC->KOKC, SATX->KSAT
       ```

  4. **src/kalshi/weather-bot.py**:
     - Line 295: Change `MIN_FORECAST_DISTANCE_F = 2.0` to `MIN_FORECAST_DISTANCE_F = 4.0`
     - Update the `verify_past_forecasts()` call (around line 179): instead of passing `city_coords` with lat/lon, import `DEFAULT_STATION_MAP` from `forecast_verifier` and pass it as `station_map`:
       ```python
       from forecast_verifier import ForecastVerifier, DEFAULT_STATION_MAP
       ...
       verifier.verify_past_forecasts(station_map=DEFAULT_STATION_MAP)
       ```
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -c "
import json
# Check NY coordinates
cfg = json.loads(open('config/kalshi-config.json').read())
assert cfg['cities']['NY']['lat'] == 40.7829, f'NY lat wrong: {cfg[\"cities\"][\"NY\"][\"lat\"]}'
assert cfg['cities']['NY']['lon'] == -73.9654, f'NY lon wrong: {cfg[\"cities\"][\"NY\"][\"lon\"]}'
assert 'KNYC' in cfg['_comment'], f'Comment not updated: {cfg[\"_comment\"]}'
# Check monitor config
mon = json.loads(open('config/kalshi-monitor-config.json').read())
assert mon['sources']['nws']['stations']['NY'] == 'KNYC', f'NY station wrong: {mon[\"sources\"][\"nws\"][\"stations\"][\"NY\"]}'
print('Config fixes verified OK')
" && python3 -c "
import sys; sys.path.insert(0, 'src/kalshi')
from forecast_verifier import DEFAULT_STATION_MAP
assert DEFAULT_STATION_MAP['NY'] == 'KNYC'
assert DEFAULT_STATION_MAP['MIA'] == 'KMIA'
assert len(DEFAULT_STATION_MAP) == 20
print('Station map verified OK')
" && pytest tests/test_weather.py -x -q 2>&1 | tail -5</automated>
  </verify>
  <done>NY coordinates point to Central Park (40.7829, -73.9654). NY station is KNYC. ForecastVerifier uses IEM ASOS instead of ERA5. Near-threshold filter widened to 4.0 deg F. All existing weather tests pass.</done>
</task>

<task type="auto">
  <name>Task 2: Data infrastructure — weather_data.py module, backfill script, and tests</name>
  <files>src/kalshi/weather_data.py, scripts/backfill-weather-data.py, tests/test_weather_data.py</files>
  <action>
  Create **src/kalshi/weather_data.py** with the following components:

  1. **STATION_MAP** — Module-level dict mapping all 20 Kalshi city codes to IEM ASOS station IDs (same mapping as DEFAULT_STATION_MAP in forecast_verifier.py, but this is the canonical source — forecast_verifier can import from here later):
     ```
     MIA->KMIA, LAX->KLAX, PHIL->KPHL, NY->KNYC, CHI->KMDW, AUS->KAUS, DEN->KDEN, HOU->KHOU, ATL->KATL, BOS->KBOS, SFO->KSFO, SEA->KSEA, LV->KLAS, DAL->KDFW, MIN->KMSP, PHX->KPHX, DC->KDCA, NOLA->KMSY, OKC->KOKC, SATX->KSAT
     ```

  2. **EnsembleCollector** class:
     - `__init__(self, logger=None)` — stores logger
     - `fetch_ensemble(self, lat, lon, forecast_days=14)` method:
       - Calls Open-Meteo Ensemble API: `https://ensemble-api.open-meteo.com/v1/ensemble?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days={forecast_days}&models=gfs_seamless,ecmwf_ifs025`
       - Uses `retry_request("GET", url, timeout=15, max_retries=2)` from kalshi_auth
       - Parses response: fields are `temperature_2m_max_member01`, `temperature_2m_max_member02`, etc.
       - Returns dict: `{date_str: {"gfs_members": [list of 31 floats], "ecmwf_members": [list of 51 floats]}}` — members separated by model prefix in the response field names (GFS members come first as `gfs_seamless_temperature_2m_max_member{NN}`, ECMWF as `ecmwf_ifs025_temperature_2m_max_member{NN}`)
       - Actually, Open-Meteo Ensemble API returns flat member columns without model prefix when you request multiple models. The response `daily` dict will have keys like `temperature_2m_max_member00`, `temperature_2m_max_member01`, ... up to the total member count. Parse ALL member columns and return them as a flat list per date: `{date_str: [temp1, temp2, ..., temp_N]}` where N is total members found.
       - Returns None on API failure (log warning, don't crash)

  3. **IEMFetcher** class:
     - `__init__(self, logger=None)` — stores logger
     - `fetch_daily_high(self, station_id, date_str)` method:
       - Calls: `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={station_id}&data=max_tmpf&tz=America/New_York&format=comma&year1={Y}&month1={M}&day1={D}&year2={Y}&month2={M}&day2={D}`
       - Uses `retry_request("GET", url, timeout=10, max_retries=2)` from kalshi_auth
       - Parses CSV response: skip comment lines (starting with #), find data row, extract max_tmpf
       - Returns float (deg F) or None if missing ("M") or API fails
     - `fetch_daily_highs(self, station_id, start_date, end_date)` method:
       - Same endpoint but with date range. Returns dict: `{date_str: float}`
       - Parses all rows in the CSV response

  4. **TrainingStore** class:
     - `__init__(self, db_path="data/weather-training.db", logger=None)` — creates SQLite DB if not exists
     - Schema: `CREATE TABLE IF NOT EXISTS training_pairs (city TEXT, date TEXT, model TEXT, lead_days INTEGER, member_id INTEGER, forecast_temp REAL, actual_temp_cli REAL, market_outcome TEXT, PRIMARY KEY (city, date, model, lead_days, member_id))`
     - `insert_pair(self, city, date, model, lead_days, member_id, forecast_temp, actual_temp_cli=None, market_outcome=None)` — INSERT OR REPLACE
     - `insert_batch(self, rows)` — bulk insert using executemany
     - `get_pairs(self, city=None, model=None, min_lead_days=None, max_lead_days=None)` — SELECT with optional filters, returns list of dicts
     - `count(self)` — returns total row count
     - `close(self)` — close DB connection

  Create **scripts/backfill-weather-data.py**:
  - CLI script with argparse: `--days N` (default 90), `--city CITY_CODE` (optional, default all), `--dry-run` flag
  - For each city in STATION_MAP (or specified city):
    1. Fetch historical actuals from IEM ASOS for the past N days using IEMFetcher.fetch_daily_highs()
    2. Fetch historical deterministic forecasts from Open-Meteo Previous Runs API: `https://previous-runs-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America/New_York&past_days={days}&models=gfs_seamless,ecmwf_ifs025`
    3. Match forecasts to actuals by date
    4. Store pairs in TrainingStore
    5. Query Kalshi public API for settled KXHIGH markets: `https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGH{city}&status=settled&limit=200`
    6. Match settled market outcomes to training pairs by date/city
  - Print summary: total pairs inserted, cities processed, date range
  - Uses `PROJECT_DIR` from kalshi_auth to resolve paths relative to project root
  - Rate-limit API calls: 0.5s sleep between cities to be polite to IEM/Open-Meteo

  Create **tests/test_weather_data.py**:
  - Test STATION_MAP has all 20 cities, all values start with "K", NY maps to KNYC
  - Test TrainingStore: create in-memory DB (`:memory:`), insert pairs, query by city, query by lead_days range, count
  - Test EnsembleCollector.fetch_ensemble() with mocked HTTP response (mock retry_request): verify it parses member columns correctly, returns dict of {date: [temps]}, handles API failure gracefully (returns None)
  - Test IEMFetcher.fetch_daily_high() with mocked HTTP response: verify it parses CSV, handles "M" missing values, handles API failure
  - Use `unittest.mock.patch` to mock `kalshi_auth.retry_request`
  - Import pattern: `sys.path.insert(0, 'src/kalshi')` then `from weather_data import ...`
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_weather_data.py -x -v 2>&1 | tail -20</automated>
  </verify>
  <done>weather_data.py exists with STATION_MAP (20 cities), EnsembleCollector (fetches 82-member ensemble data), IEMFetcher (fetches IEM ASOS actuals), TrainingStore (SQLite storage). backfill-weather-data.py exists as CLI script. All tests pass with mocked HTTP.</done>
</task>

<task type="auto">
  <name>Task 3: Empirical ensemble CDF in probability.py and weather-bot.py integration</name>
  <files>src/kalshi/probability.py, src/kalshi/weather-bot.py, tests/test_empirical_ensemble.py</files>
  <action>
  1. **Add `empirical_ensemble_probability()` to src/kalshi/probability.py** (place after `ensemble_weather_probability_v2`):

     ```python
     def empirical_ensemble_probability(member_temps, threshold, direction, bias_offset=0.0):
         """Empirical CDF from raw ensemble member temperatures.

         Ranks ensemble members, applies optional station bias correction,
         and computes KDE-smoothed probability. No parametric sigma assumption.

         Args:
             member_temps: list of forecast temperatures (F) from ensemble members.
                 Typically 82 members (31 GEFS + 51 ECMWF ENS).
             threshold: market threshold temperature (F).
             direction: "T" (P(T > threshold)) or "B" (P(threshold <= T < threshold+1)).
             bias_offset: station bias correction (F) added to all members before CDF.
                 Positive = warm bias in forecasts (subtract from members).
                 Default 0.0 (no correction).

         Returns:
             float probability in [0, 1], or None if member_temps is empty/invalid.
         """
     ```

     Implementation details:
     - Validate: return None if `member_temps` is empty or has fewer than 5 members
     - Apply bias correction: `corrected = [t - bias_offset for t in member_temps]`
     - Sort corrected temps
     - Compute KDE bandwidth: `h = 1.06 * std(corrected) * len(corrected)**(-1/5)` (Silverman's rule). Minimum bandwidth of 0.5 deg F to avoid overfitting with tight ensembles.
     - For direction "T": compute `P(T > threshold)` using KDE-smoothed CDF:
       ```
       P = (1/N) * sum(1 - _norm_cdf((threshold - t_i) / h) for t_i in corrected)
       ```
       This is the kernel CDF estimator — each member contributes a Gaussian kernel centered at t_i with bandwidth h.
     - For direction "B": compute `P(threshold <= T < threshold+1)`:
       ```
       P_lower = (1/N) * sum(_norm_cdf((threshold - t_i) / h) for t_i in corrected)
       P_upper = (1/N) * sum(_norm_cdf((threshold + 1 - t_i) / h) for t_i in corrected)
       P = P_upper - P_lower
       ```
     - Clamp result to [0.01, 0.99] (avoid extremes with limited ensemble size)
     - Uses `_norm_cdf` already defined in probability.py (no new dependencies)

  2. **Integrate into src/kalshi/weather-bot.py**:
     - Add imports at top: `from weather_data import EnsembleCollector, STATION_MAP`
     - Add import: `from probability import empirical_ensemble_probability` (add to existing probability import line)
     - Create module-level `ensemble_collector = EnsembleCollector(logger=log)`
     - In `scan_and_trade()`, after the existing forecast fetching block (around line 163-173), add a new block that fetches ensemble member data:
       ```python
       # Fetch raw ensemble member data for empirical CDF
       ensemble_members = {}  # {city_code: {date_str: [member_temps]}}
       for code, info in CITIES.items():
           try:
               members = ensemble_collector.fetch_ensemble(info["lat"], info["lon"])
               if members:
                   ensemble_members[code] = members
           except Exception as e:
               log.warning("Ensemble member fetch failed for %s: %s", code, e)
       ```
     - In the market analysis loop, when computing probability (around line 243-292), add a new branch BEFORE the existing ensemble logic:
       ```python
       # Primary model: empirical ensemble CDF (if member data available)
       if city in ensemble_members and date_str in ensemble_members.get(city, {}):
           members = ensemble_members[city][date_str]
           if members and len(members) >= 10:
               # Get station bias from verifier if available
               bias = city_bias.get(city, {}).get("bias_f", 0.0) if city_bias else 0.0
               our_prob = empirical_ensemble_probability(
                   members, parsed["threshold"], parsed["direction"],
                   bias_offset=bias
               )
               if our_prob is not None:
                   log.info("  %s: empirical CDF from %d members (bias=%.1fF) -> P=%.3f",
                            ticker, len(members), bias, our_prob)
                   # Still compute disagreement from parametric models for edge threshold adjustment
                   if ENSEMBLE_ENABLED and isinstance(forecast_data, dict) and len(forecast_data) >= 2:
                       disagreement_score = 0.0  # empirical CDF already captures disagreement
                   # Skip to edge computation (don't run parametric model)
                   # ... (the rest of the opportunity logic continues below)
               else:
                   log.warning("  %s: empirical CDF returned None, falling back to parametric", ticker)
                   # Fall through to existing parametric logic
       ```
       This is the key integration point. Structure it so the empirical CDF is tried first, and if it returns a valid probability, skip the parametric ensemble/single-model logic entirely. If it fails (None), fall through to existing parametric path unchanged.
     - Keep `forecast_temp` computation from the parametric path (needed for near-threshold filter that runs before probability computation). The empirical CDF block only replaces the probability calculation, not the forecast_temp used for logging and threshold distance.

  3. **Create tests/test_empirical_ensemble.py**:
     - Test with uniform members all above threshold -> probability near 1.0
     - Test with uniform members all below threshold -> probability near 0.0
     - Test with members split 50/50 around threshold -> probability near 0.5
     - Test with realistic spread (e.g., 31 GFS members around 85F, threshold 82F, direction "T") -> probability should be well above 0.5
     - Test direction "B" (bracket): members clustered at 85F, threshold 84 -> should give reasonable bracket probability
     - Test bias_offset: members at 85F with bias_offset=2.0 should give same result as members at 83F with no bias
     - Test edge cases: empty list returns None, fewer than 5 members returns None, single member returns None
     - Test clamping: all members far above threshold -> result clamped to 0.99, all far below -> clamped to 0.01
     - Import: `from probability import empirical_ensemble_probability, _reset_calibration`
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_empirical_ensemble.py tests/test_weather_data.py tests/test_weather.py -x -v 2>&1 | tail -30</automated>
  </verify>
  <done>empirical_ensemble_probability() exists in probability.py with KDE-smoothed CDF, bias correction, and [0.01, 0.99] clamping. weather-bot.py uses it as primary model (with parametric fallback). All tests pass: empirical ensemble tests, weather data tests, existing weather tests.</done>
</task>

</tasks>

<verification>
1. Config correctness: NY coordinates are 40.7829, -73.9654 (Central Park). NY station is KNYC. Comment is updated.
2. Verifier uses IEM ASOS (mesonet.agron.iastate.edu), not Open-Meteo ERA5 (archive-api.open-meteo.com).
3. weather_data.py module importable with all 4 components (STATION_MAP, EnsembleCollector, IEMFetcher, TrainingStore).
4. empirical_ensemble_probability() gives correct results for known inputs (all above/below/split).
5. weather-bot.py tries empirical CDF first, falls back to parametric on failure.
6. All existing tests pass: `pytest tests/test_weather.py tests/test_weather_bot_bugs.py tests/test_weather_city_expansion.py -x -q`
</verification>

<success_criteria>
- NY config points to Central Park in both config files
- ForecastVerifier uses IEM ASOS data source (not ERA5)
- Near-threshold filter is 4.0 deg F (not 2.0)
- weather_data.py exists with STATION_MAP (20 entries), EnsembleCollector, IEMFetcher, TrainingStore
- backfill-weather-data.py exists as runnable CLI script
- empirical_ensemble_probability() in probability.py handles "T" and "B" directions with KDE smoothing
- weather-bot.py uses empirical ensemble as primary model, parametric as fallback
- All new + existing weather tests pass
</success_criteria>

<output>
After completion, create `.planning/quick/11-world-class-weather-bot-v2-critical-fixe/11-SUMMARY.md`
</output>
