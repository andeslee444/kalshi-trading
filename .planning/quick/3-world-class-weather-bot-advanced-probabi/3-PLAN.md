---
phase: quick-3
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/probability.py
  - src/kalshi/weather-bot.py
  - src/kalshi/forecast_verifier.py
  - tests/test_advanced_weather.py
  - config/kalshi-config.json
autonomous: true
requirements: [WEATHER-WORLD-CLASS]

must_haves:
  truths:
    - "Weather probability model uses skew-normal distribution capturing asymmetric forecast errors (warm bias in summer, cold bias in winter)"
    - "Sigma adapts based on forecast hour of day, not just days_out (morning forecasts more uncertain than afternoon)"
    - "Ensemble weights update automatically based on recent model skill (inverse-Brier weighting from verification data)"
    - "Forecast verifier tracks forecast-vs-actual errors per city/horizon and writes verification data for adaptive calibration"
    - "Bot uses ensemble disagreement as a confidence signal, gating trades when models diverge beyond threshold"
  artifacts:
    - path: "src/kalshi/probability.py"
      provides: "Skew-normal CDF, hour-aware sigma, adaptive ensemble weighting"
    - path: "src/kalshi/forecast_verifier.py"
      provides: "Forecast verification loop that records forecast accuracy for online calibration"
    - path: "src/kalshi/weather-bot.py"
      provides: "Integration of verifier, adaptive ensemble, and disagreement gating"
    - path: "tests/test_advanced_weather.py"
      provides: "Tests for skew-normal, hour-aware sigma, adaptive weighting, and verifier"
  key_links:
    - from: "src/kalshi/weather-bot.py"
      to: "src/kalshi/forecast_verifier.py"
      via: "ForecastVerifier called each scan to record forecasts and check prior forecasts against actuals"
    - from: "src/kalshi/forecast_verifier.py"
      to: "src/kalshi/probability.py"
      via: "Verification data feeds into adaptive ensemble weights and sigma estimation"
    - from: "src/kalshi/probability.py"
      to: "config/calibration.json"
      via: "Skew parameter and adaptive weights read from calibration alongside existing sigma params"
---

<objective>
Upgrade the weather bot from functional CDF model to world-class quant-grade probability estimation.

Purpose: The current model uses a symmetric Student-t distribution with static sigma scaling (intercept + slope * sqrt(days_out)). This misses three critical dimensions that professional weather quant models capture: (1) asymmetric forecast error distributions (NWS forecasts have systematic warm/cold biases that vary by season and city), (2) time-of-day sigma decay (a 6am forecast has more uncertainty than a 2pm forecast on the same day), and (3) adaptive ensemble intelligence (model weights should evolve based on recent verification skill, not be static). Additionally, the existing calibration.json has stale sigma values (intercept 5.5) that override the better hardcoded defaults (1.5), and there's no feedback loop comparing forecasts to actual outcomes.

Output: Advanced probability model with skew-normal distribution, hour-of-day-aware sigma, skill-weighted ensemble, and a forecast verification pipeline.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/probability.py
@src/kalshi/weather-bot.py
@src/kalshi/ticker_utils.py
@src/kalshi/particle_filter.py
@config/kalshi-config.json
@config/calibration.json
@scripts/calibrate-sigma.py
@scripts/backtest.py
@tests/test_probability.py

<interfaces>
<!-- Key types and contracts the executor needs. -->

From src/kalshi/probability.py:
```python
def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None, sigma_override=None) -> float
def weather_sigma(days_out=0, city=None) -> float
def ensemble_weather_probability(forecasts, threshold, direction, days_out=0, city=None, sigma_multiplier=1.0) -> float
def ensemble_spread_sigma_multiplier(spread_f) -> float
def _student_t_cdf(x, df=6) -> float
def _norm_cdf(x) -> float
def _load_calibration() -> dict
def _reset_calibration() -> None
```

From src/kalshi/weather-bot.py:
```python
def get_ensemble_forecast(lat, lon) -> dict  # {date: {model: temp}}
def compute_probability(forecast_temp, threshold, direction, days_out=0, city=None) -> float
def scan_and_trade() -> None
```

From config/calibration.json (STALE -- global_sigma_intercept=5.5 overrides good default of 1.5):
```json
{
  "weather": {
    "global_sigma_intercept": 5.5,
    "global_sigma_slope": 0.2,
    "df": 30,
    "global_brier": 0.309726,
    "per_city": { "DEN": {"sigma_intercept": 4.15, ...}, "AUS": {...}, "CHI": {...}, "NY": {...} }
  }
}
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Advanced probability model -- skew-normal distribution, hour-aware sigma, adaptive ensemble</name>
  <files>src/kalshi/probability.py, tests/test_advanced_weather.py</files>
  <behavior>
    - Test: skew_normal_cdf(0, alpha=0) equals norm_cdf(0) = 0.5 (zero skew = symmetric)
    - Test: skew_normal_cdf(x, alpha=3) > norm_cdf(x) for positive x (positive skew shifts mass right)
    - Test: skew_normal_cdf(x, alpha=-3) < norm_cdf(x) for positive x (negative skew shifts mass left)
    - Test: weather_probability with positive skew produces higher prob for above-threshold than symmetric model
    - Test: weather_sigma_hourly(days_out=0, hour=6) > weather_sigma_hourly(days_out=0, hour=14) (morning more uncertain)
    - Test: weather_sigma_hourly(days_out=0, hour=17) < weather_sigma_hourly(days_out=0, hour=10) (late afternoon most certain)
    - Test: weather_sigma_hourly falls back to weather_sigma when hour is None (backward compatibility)
    - Test: adaptive_ensemble_weights returns inverse-Brier-weighted probabilities when verification data exists
    - Test: adaptive_ensemble_weights falls back to static weights when no verification data
    - Test: ensemble_disagreement_score returns 0 when all models agree
    - Test: ensemble_disagreement_score returns high value when models disagree significantly
  </behavior>
  <action>
    **1. Implement skew-normal CDF in probability.py**

    Add a skew-normal CDF function that extends the existing symmetric model. The skew-normal distribution is defined by Owen's T function: Phi_SN(x, alpha) = Phi(x) - 2*T(x, alpha), where T is Owen's T function. For implementation simplicity and no-scipy constraint, use the approximation:

    ```python
    def _skew_normal_cdf(x, alpha=0.0):
        """Skew-normal CDF. alpha=0 reduces to standard normal.
        Positive alpha = right skew (warm bias), negative = left skew (cold bias).
        Uses the closed-form: Phi_SN(x) = Phi(x) - 2*T(x, alpha)
        where T(x, alpha) is Owen's T function, approximated via Gaussian quadrature."""
    ```

    Implement Owen's T function using a 10-point Gauss-Legendre quadrature (the integral is well-behaved). This gives accurate results without scipy. The integral is: T(h, a) = (1/2pi) * integral_0^a exp(-0.5*h^2*(1+t^2)) / (1+t^2) dt.

    **2. Add hour-of-day-aware sigma function**

    ```python
    def weather_sigma_hourly(days_out=0, city=None, hour_of_day=None):
        """Sigma with intra-day decay. Uses exponential decay within the forecast day.
        At hour 6 (morning), sigma is at full daily value.
        At hour 14 (afternoon), sigma is reduced ~40%.
        At hour 17 (evening), sigma is at minimum (forecast nearly settled).
        For days_out > 0, hour_of_day is ignored (full daily sigma applies).
        """
    ```

    The formula for day-0: sigma = base_sigma * decay_factor, where decay_factor = max(0.4, exp(-0.08 * (hour - 6))) for hour >= 6. For hour < 6 (overnight), use decay_factor = 1.2 (extra uncertainty). For days_out > 0, call existing weather_sigma unchanged.

    **3. Update weather_probability to accept hour_of_day and skew parameters**

    Add optional `hour_of_day` parameter. When provided and days_out == 0, use weather_sigma_hourly. Add optional `skew` parameter (default 0.0 = symmetric, backward compatible). When skew != 0, use _skew_normal_cdf instead of _student_t_cdf. Note: skew-normal and Student-t are different distributions. For the combined model, use a "skew-t" approach: apply the skew adjustment as a post-hoc correction to the Student-t probability: P_skew = P_t + skew_correction, where skew_correction is proportional to alpha * pdf(z) * (CDF(alpha*z) - 0.5). This keeps the fat tails from Student-t while adding asymmetry. The skew parameter is read from calibration.json under `weather.skew` (global) or `weather.per_city.{city}.skew` (per-city). Default 0.0 if not present.

    **4. Add adaptive ensemble weight computation**

    ```python
    def compute_adaptive_ensemble_weights(verification_data, default_weights=None):
        """Compute model weights from recent forecast verification data.
        verification_data: dict of {model_name: [list of (predicted_prob, actual_outcome) tuples]}
        Returns: dict of {model_name: weight} using inverse-Brier weighting.
        Falls back to default_weights if verification data is insufficient (< 20 samples per model).
        """
    ```

    **5. Add ensemble disagreement score**

    ```python
    def ensemble_disagreement_score(model_probs):
        """Compute disagreement between ensemble model probabilities.
        model_probs: dict of {model_name: probability}
        Returns: float in [0, 1] where 0 = perfect agreement, 1 = maximum disagreement.
        Uses coefficient of variation of probabilities. When score > 0.3, signals regime
        uncertainty and the bot should require higher edge threshold or skip the trade.
        """
    ```

    **6. Update ensemble_weather_probability to use adaptive weights and return disagreement**

    Add optional parameter `verification_data` to ensemble_weather_probability. When provided, compute adaptive weights instead of using static config weights. Also compute and return disagreement_score (change return to a namedtuple or add optional return_details flag like Kelly functions do).

    Actually, to maintain backward compatibility, add a separate function:
    ```python
    def ensemble_weather_probability_v2(forecasts, threshold, direction, days_out=0, city=None,
                                         sigma_multiplier=1.0, hour_of_day=None,
                                         verification_data=None, return_details=False):
        """Enhanced ensemble with adaptive weights, hour-aware sigma, and disagreement scoring.
        When return_details=True, returns (prob, details_dict) where details_dict contains
        disagreement_score, weights_used, per_model_probs.
        When return_details=False, returns just prob (backward compatible).
        """
    ```

    **7. Write tests in tests/test_advanced_weather.py**

    Follow existing test patterns (see tests/test_probability.py). Use `_reset_calibration()` in setup/teardown. Test all behaviors listed above. Import from probability module directly (conftest.py handles path).

    **IMPORTANT**: Do NOT modify existing function signatures -- only ADD new optional parameters with defaults that preserve existing behavior. All existing tests must continue to pass.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_advanced_weather.py tests/test_probability.py -x -v</automated>
  </verify>
  <done>
    - skew-normal CDF implemented and tested (alpha=0 matches standard normal within 1e-6)
    - Hour-of-day sigma decay reduces uncertainty by ~40-60% from morning to afternoon for day-0 markets
    - Adaptive ensemble weights compute from verification data with inverse-Brier weighting
    - Ensemble disagreement score quantifies model divergence on [0, 1] scale
    - All existing probability tests still pass (backward compatibility)
    - New test file has 10+ tests covering all new functionality
  </done>
</task>

<task type="auto">
  <name>Task 2: Forecast verification pipeline and weather bot integration</name>
  <files>src/kalshi/forecast_verifier.py, src/kalshi/weather-bot.py, config/kalshi-config.json</files>
  <action>
    **1. Create src/kalshi/forecast_verifier.py -- the learning loop**

    This module creates the feedback loop that makes the model adaptive. It:
    - Records every forecast the bot makes (city, date, forecast_temp per model, threshold, model_prob, timestamp)
    - After settlement day passes, fetches actual high temperature from Open-Meteo historical API
    - Compares forecast vs actual to compute per-model MAE, bias (systematic over/under prediction), and per-city skew
    - Persists verification data to `data/weather-verification.json`
    - Provides verification_data in the format that `compute_adaptive_ensemble_weights()` expects

    ```python
    class ForecastVerifier:
        """Tracks forecast accuracy and provides adaptive calibration data.

        Lifecycle:
        1. record_forecast(city, date, model_forecasts, threshold, model_prob) -- called each scan
        2. verify_past_forecasts() -- called each scan, checks if yesterday's forecasts can be verified
        3. get_verification_summary() -- returns data for adaptive ensemble weights
        4. get_city_bias() -- returns systematic bias per city for skew parameter
        """

        def __init__(self, state_path, logger=None):
            """
            state_path: Path to data/weather-verification.json
            """

        def record_forecast(self, city, date_str, model_forecasts, threshold=None,
                           model_prob=None, hour_of_day=None):
            """Record a forecast for later verification.
            model_forecasts: dict of {model_name: temp_f}
            Deduplicates by (city, date, model) -- keeps latest forecast for each.
            """

        def verify_past_forecasts(self):
            """Check forecasts from 2+ days ago against actual temperatures.
            Fetches actual high from Open-Meteo historical API:
            https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}
            &start_date={date}&end_date={date}&daily=temperature_2m_max&temperature_unit=fahrenheit

            For each verified forecast, computes:
            - error = forecast - actual (positive = warm bias)
            - abs_error = |error|
            - squared_error for Brier-like scoring

            Marks forecasts as verified to avoid re-fetching.
            Rate-limits API calls (max 5 per scan to avoid hammering archive API).
            """

        def get_verification_summary(self, lookback_days=30):
            """Return per-model verification data for adaptive ensemble weights.
            Returns: {model_name: {"mae": float, "bias": float, "n": int,
                      "brier_predictions": [(pred_prob, actual_outcome), ...]}}
            Only includes models with >= 10 verified forecasts in the lookback window.
            """

        def get_city_bias(self, lookback_days=30):
            """Return per-city forecast bias for skew parameter estimation.
            Returns: {city: {"bias_f": float, "skew_alpha": float, "n": int}}
            skew_alpha is derived from bias: positive bias (warm) -> negative alpha (model overestimates),
            negative bias (cold) -> positive alpha (model underestimates).
            Scaling: alpha = -bias_f * 0.3 (heuristic, will be tuned by calibration pipeline).
            Only includes cities with >= 15 verified forecasts.
            """

        def save(self):
            """Persist state to data/weather-verification.json using _atomic_write_json."""

        def load(self):
            """Load state from disk. Start fresh if file missing or corrupt."""

        def cleanup(self, max_age_days=90):
            """Remove verification records older than max_age_days."""
    ```

    Use `_atomic_write_json` from kalshi_auth for safe writes. Use `retry_request` for API calls. Store both pending (unverified) and completed (verified) forecasts. The state structure:
    ```json
    {
      "pending": [{"city": "MIA", "date": "2026-03-04", "models": {"gfs": 82.5, "ecmwf": 83.1}, "recorded_at": "..."}],
      "verified": [{"city": "MIA", "date": "2026-03-04", "models": {"gfs": 82.5}, "actual_high": 83.0, "errors": {"gfs": -0.5}, "verified_at": "..."}],
      "stats": {"last_verification": "...", "total_verified": 0}
    }
    ```

    **2. Integrate verifier into weather-bot.py**

    At the top of scan_and_trade(), add:
    ```python
    # Forecast verification loop
    verifier = ForecastVerifier(PROJECT_DIR / "data" / "weather-verification.json", logger=log)
    verifier.load()
    verifier.verify_past_forecasts()  # check yesterday's forecasts against actuals
    ```

    After fetching ensemble forecasts, record them:
    ```python
    for code, info in CITIES.items():
        if code in forecasts:
            for date_str, model_data in forecasts[code].items():
                if isinstance(model_data, dict):
                    verifier.record_forecast(code, date_str, model_data)
    ```

    When computing probability, use adaptive weights and hour-of-day:
    ```python
    # Get adaptive ensemble weights from verification data
    verification_summary = verifier.get_verification_summary()
    city_bias = verifier.get_city_bias()

    # Get current hour for intra-day sigma
    current_hour = datetime.datetime.now().hour
    ```

    Then in the ensemble probability call, pass `verification_data=verification_summary`, `hour_of_day=current_hour` (for day-0 markets only), and city-specific skew from `city_bias`.

    Use `ensemble_weather_probability_v2` with `return_details=True` to get disagreement_score. When disagreement_score > 0.3, require 2x edge threshold (similar to existing spread_mult > 1.5 logic, but more principled).

    At the end of scan_and_trade():
    ```python
    verifier.cleanup()
    verifier.save()
    ```

    **3. Add verifier config to config/kalshi-config.json**

    Add under the top-level:
    ```json
    "verification": {
      "enabled": true,
      "lookback_days": 30,
      "min_samples_for_adaptive": 10,
      "disagreement_edge_multiplier": 2.0,
      "max_api_calls_per_scan": 5
    }
    ```

    **4. Make the verifier a module-level singleton in weather-bot.py**

    Initialize once at module level (like `trade_manager`), not inside scan_and_trade. This avoids re-loading state every 30 minutes:
    ```python
    VERIFICATION_ENABLED = config.get("verification", {}).get("enabled", True)
    verifier = ForecastVerifier(PROJECT_DIR / "data" / "weather-verification.json", logger=log) if VERIFICATION_ENABLED else None
    if verifier:
        verifier.load()
    ```

    **CRITICAL CONSTRAINTS**:
    - The verifier must be non-blocking -- if the archive API is down, log a warning and continue trading
    - The verifier must not slow down the scan loop -- rate-limit API calls to max 5 per scan
    - All new code must work with existing imports (no new pip dependencies)
    - The bot must continue to work identically if verification is disabled or data/weather-verification.json doesn't exist (graceful degradation)
    - Use the same import patterns as the rest of the codebase (from kalshi_auth import ...)
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_advanced_weather.py tests/test_probability.py tests/test_weather.py tests/test_weather_bot_bugs.py -x -v</automated>
  </verify>
  <done>
    - ForecastVerifier class records forecasts and verifies against Open-Meteo historical API
    - Verification data feeds into adaptive ensemble weights (inverse-Brier weighting)
    - City-level bias estimation provides skew parameter for asymmetric probability model
    - Weather bot integrates verifier as non-blocking module-level singleton
    - Disagreement score gates trades when ensemble models diverge (> 0.3 requires 2x edge)
    - Hour-of-day passed to probability model for day-0 markets
    - All existing tests pass, bot degrades gracefully when verification disabled
    - Config flag controls verification feature
  </done>
</task>

</tasks>

<verification>
1. All existing tests pass: `pytest tests/ -x` (no regressions)
2. New tests pass: `pytest tests/test_advanced_weather.py -v`
3. Probability model backward compatible: weather_probability() with no new args returns same values
4. Bot starts without errors: `timeout 10 python3 src/kalshi/weather-bot.py 2>&1 | head -20` (auth will fail in dev, but module loads)
5. Verifier handles missing data gracefully: no crashes if data/weather-verification.json doesn't exist
</verification>

<success_criteria>
- Skew-normal CDF extends Student-t model with asymmetric forecast error modeling
- Hour-of-day sigma decay reduces day-0 uncertainty by 40-60% from morning to afternoon
- Adaptive ensemble weights auto-update from verification data (no manual calibration needed for weights)
- Ensemble disagreement score provides principled trade gating beyond raw spread heuristic
- Forecast verifier creates the learning loop: forecast -> observe -> learn -> improve
- Zero regressions in existing test suite
- All new functionality tested with 15+ new test cases
</success_criteria>

<output>
After completion, create `.planning/quick/3-world-class-weather-bot-advanced-probabi/3-SUMMARY.md`
</output>
