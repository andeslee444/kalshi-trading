---
phase: quick-3
plan: 01
subsystem: probability
tags: [skew-normal, owen-t, gauss-legendre, ensemble, forecast-verification, adaptive-calibration]

# Dependency graph
requires:
  - phase: quick-2
    provides: "Tightened sigma defaults (1.5F intercept), relaxed liquidity, edge guard"
provides:
  - "Skew-normal CDF via Owen's T function (no scipy)"
  - "Hour-of-day-aware sigma decay for day-0 markets"
  - "Adaptive ensemble weights from forecast verification data"
  - "Ensemble disagreement score for principled trade gating"
  - "ForecastVerifier pipeline for online calibration"
  - "ensemble_weather_probability_v2 combining all improvements"
affects: [weather-bot, calibration, probability]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Owen's T function via Gauss-Legendre quadrature (scipy-free)"
    - "Skew-t post-hoc correction preserving fat tails from Student-t"
    - "ForecastVerifier singleton pattern for non-blocking verification"

key-files:
  created:
    - src/kalshi/forecast_verifier.py
    - tests/test_advanced_weather.py
  modified:
    - src/kalshi/probability.py
    - src/kalshi/weather-bot.py
    - config/kalshi-config.json

key-decisions:
  - "Used Owen's T + Gauss-Legendre quadrature for skew-normal CDF instead of scipy"
  - "Applied skew as additive correction to Student-t rather than replacing it, preserving fat tails"
  - "Clamped skew-normal CDF to [0,1] for quadrature numerical precision at extremes"
  - "Hour-of-day sigma uses exponential decay from 6am: max(0.4, exp(-0.08*(h-6)))"
  - "Disagreement score uses coefficient of variation, gating trades at >0.3"
  - "Verifier is non-blocking: all API failures log warnings and continue trading"

patterns-established:
  - "ForecastVerifier pattern: record -> verify -> summarize -> feed back into model"
  - "v2 function pattern: backward-compatible with return_details flag for new metadata"

requirements-completed: [WEATHER-WORLD-CLASS]

# Metrics
duration: 10min
completed: 2026-03-05
---

# Quick Task 3: World-Class Weather Bot Summary

**Skew-normal distribution, hour-aware sigma decay, adaptive ensemble weights, and forecast verification pipeline for quant-grade weather probability estimation**

## Performance

- **Duration:** 10 min
- **Started:** 2026-03-05T04:56:47Z
- **Completed:** 2026-03-05T05:06:47Z
- **Tasks:** 2 (Task 1 TDD: 3 commits, Task 2: 1 commit)
- **Files modified:** 5 (2 created, 3 modified)

## Accomplishments

- Skew-normal CDF implemented via Owen's T function with 10-point Gauss-Legendre quadrature (no scipy dependency)
- Hour-of-day sigma decay reduces day-0 forecast uncertainty by 40-60% from morning to late afternoon
- Adaptive ensemble weights auto-compute from verification data using inverse-Brier weighting
- Ensemble disagreement score quantifies model divergence on [0,1] scale for principled trade gating
- ForecastVerifier creates the learning loop: forecast -> observe actual -> compute bias -> improve model
- 28 new tests covering all new functionality, 153 total weather-related tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1 (TDD RED): Failing tests** - `1d1093f` (test)
2. **Task 1 (TDD GREEN): Implementation** - `a2b01a7` (feat)
3. **Task 2: Verifier + bot integration** - `b11330a` (feat)

## Files Created/Modified

- `src/kalshi/probability.py` - Added _owens_t, _skew_normal_cdf, weather_sigma_hourly, compute_adaptive_ensemble_weights, ensemble_disagreement_score, ensemble_weather_probability_v2; updated weather_probability with hour_of_day and skew params
- `src/kalshi/forecast_verifier.py` - New module: ForecastVerifier class with record/verify/summarize/bias lifecycle
- `src/kalshi/weather-bot.py` - Integrated verifier singleton, switched to ensemble_weather_probability_v2, added disagreement gating
- `config/kalshi-config.json` - Added verification config section (enabled, lookback, disagreement multiplier)
- `tests/test_advanced_weather.py` - 28 tests: skew-normal CDF (6), weather prob skew (3), hourly sigma (6), adaptive weights (4), disagreement (5), ensemble v2 (4)

## Decisions Made

1. **Owen's T quadrature over series expansion:** 10-point Gauss-Legendre gives better numerical stability than truncated power series at extreme values. Required clamping output to [0,1] for ~1e-8 precision issues at tails.

2. **Skew-t hybrid approach:** Rather than replacing Student-t with skew-normal (losing fat tails), applied skew as an additive correction: delta = (1-SN_CDF(z,alpha)) - (1-N_CDF(z)). This preserves the 3x heavier tails at 3-sigma from Student-t while adding asymmetry.

3. **Positive alpha = right skew:** Following the standard skew-normal convention where positive alpha shifts mass to the right (more warm outcomes). For weather: positive bias (warm) maps to negative skew_alpha via -bias*0.3 heuristic.

4. **Disagreement > spread_mult for trade gating:** The new disagreement_score (coefficient of variation of model probabilities) supersedes the raw spread_mult > 1.5 heuristic for edge threshold doubling. Both are checked (disagreement first), providing defense in depth.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed skew-normal CDF test assertions for correct CDF direction**
- **Found during:** Task 1 (TDD GREEN phase)
- **Issue:** Plan stated "skew_normal_cdf(x, alpha=3) > norm_cdf(x) for positive x" but the correct mathematical relationship is the opposite -- positive alpha (right skew) means CDF is LOWER at positive x (more mass in right tail, less accumulated to the left)
- **Fix:** Inverted test assertions to match correct skew-normal mathematics
- **Files modified:** tests/test_advanced_weather.py
- **Verification:** All 28 tests pass; manual numerical verification confirmed
- **Committed in:** a2b01a7 (Task 1 GREEN commit)

**2. [Rule 1 - Bug] Fixed skew correction sign in weather_probability**
- **Found during:** Task 1 (TDD GREEN phase)
- **Issue:** Initial skew correction formula (alpha * pdf(z) * (CDF(alpha*z)-0.5)) produced symmetric results regardless of sign. Switched to direct skew-normal CDF delta approach.
- **Fix:** Replaced formula with (1-SN_CDF) - (1-N_CDF) additive correction
- **Files modified:** src/kalshi/probability.py
- **Verification:** weather_probability with skew=+2 > symmetric > skew=-2 confirmed numerically
- **Committed in:** a2b01a7 (Task 1 GREEN commit)

**3. [Rule 1 - Bug] Clamped skew-normal CDF output to [0,1]**
- **Found during:** Task 1 (TDD GREEN phase, monotonicity test)
- **Issue:** At extreme values (x=2, alpha=-3), Gauss-Legendre quadrature produced CDF values of 1.000000012, causing non-monotonicity when compared to 0.999999987 at x=3
- **Fix:** Added max(0.0, min(1.0, result)) clamping in _skew_normal_cdf
- **Files modified:** src/kalshi/probability.py
- **Verification:** Monotonicity test passes with 1e-7 tolerance
- **Committed in:** a2b01a7 (Task 1 GREEN commit)

---

**Total deviations:** 3 auto-fixed (3 bugs in plan's mathematical specifications)
**Impact on plan:** All auto-fixes necessary for mathematical correctness. No scope creep.

## Issues Encountered

- Pre-existing test failure in tests/test_audit.py::TestCPINowcastSigma (cpi_nowcast_sigma formula was changed by other concurrent work). Not related to this plan's changes -- logged as out-of-scope.

## User Setup Required

None - no external service configuration required. The forecast verification pipeline auto-starts on next weather bot scan and creates data/weather-verification.json automatically.

## Next Steps / Readiness

- Verification data will accumulate over 2-4 weeks of trading (needs 10+ verified forecasts per model and 15+ per city for adaptive weights/bias)
- Consider running calibrate-sigma.py to seed skew parameters in calibration.json once verification data is sufficient
- The ensemble v2 function is backward-compatible and ready for immediate production use

---
*Quick Task: 3-world-class-weather-bot-advanced-probabi*
*Completed: 2026-03-05*
