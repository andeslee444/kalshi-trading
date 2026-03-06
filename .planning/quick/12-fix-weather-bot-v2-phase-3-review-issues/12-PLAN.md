# Quick Task 12: Fix Weather Bot v2 Phase 3 Review Issues

## Task 1: Fix critical bugs in weather_data.py and weather-bot.py

**Files:** `src/kalshi/weather_data.py`, `src/kalshi/weather-bot.py`

**Actions:**
1. Rewrite `next_model_run()` to only return future runs (minutes > 0), remove Loop 1 early-return and Loop 3
2. Fix HRRR timezone from `America%2FNew_York` to `auto`
3. Fix NO-side fill price: convert YES-VWAP to NO price (100 - fill_price_yes), flip comparison
4. Add skip tracking (ss.skip + log_decision) for depth gating
5. Move fill price estimation after Kelly sizing to use actual `count`

## Task 2: Update tests

**Files:** `tests/test_weather_data.py`, `tests/test_weather_bot_phase3.py`

**Actions:**
1. Update `test_next_model_run_available_now` and `test_next_model_run_prefers_soonest` to assert minutes > 0
2. Update `test_hrrr_imminent_shortens_interval` to verify minutes == 2 at XX:43
3. Update `test_no_imminent_run_keeps_base` to verify minutes > 2 at XX:50
4. Add `test_fetch_hrrr_url_uses_auto_timezone` assertion
5. Add `test_no_side_fill_price_conversion` test
6. Replace tautological TestAdaptiveInterval/TestHRRRMemberInjection with behavioral tests

## Verification

```bash
python3 -m pytest tests/test_weather_data.py tests/test_weather_bot_phase3.py tests/test_weather.py tests/test_weather_bot_bugs.py tests/test_advanced_weather.py tests/test_empirical_ensemble.py -v
```
