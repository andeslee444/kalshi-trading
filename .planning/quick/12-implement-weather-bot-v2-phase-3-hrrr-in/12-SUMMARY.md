---
phase: quick-12
plan: 01
subsystem: weather-bot
tags: [hrrr, adaptive-cadence, orderbook-depth, model-run-timing, weather-v2]
dependency_graph:
  requires: [weather_data.py, weather-bot.py, kalshi-config.json]
  provides: [HRRRFetcher, OrderBookDepth, MODEL_RUN_SCHEDULE, next_model_run, compute_adaptive_interval]
  affects: [weather-bot scan loop, ensemble probability computation, trade execution]
tech_stack:
  added: [Open-Meteo hrrr_conus API]
  patterns: [member injection for empirical CDF weighting, volume-weighted book walk for fill estimation, UTC model-run scheduling]
key_files:
  created:
    - tests/test_weather_bot_phase3.py
  modified:
    - src/kalshi/weather_data.py
    - src/kalshi/weather-bot.py
    - config/kalshi-config.json
    - tests/test_weather_data.py
decisions:
  - "HRRR member injection via replication (60%/30% of member count) rather than separate weighted average, preserving empirical CDF nonparametric structure"
  - "OrderBookDepth converts NO bids to YES asks at (100-price) for unified book analysis"
  - "next_model_run returns 0 for recently-available runs within 120-minute lookback window"
metrics:
  duration: 5m
  completed: "2026-03-05"
  tasks_completed: 2
  tasks_total: 2
  tests_added: 40
  tests_total: 143
---

# Quick Task 12: Weather Bot v2 Phase 3 Summary

HRRR 3km deterministic forecasts integrated into ensemble with day-dependent member injection; adaptive scan cadence (5/15/30 min by days_out); orderbook depth gating and fill price improvement; model-run timing awareness triggers scans when fresh GFS/ECMWF/HRRR data drops.

## Task Results

| # | Task | Commit | Key Changes |
|---|------|--------|-------------|
| 1 | HRRRFetcher, OrderBookDepth, model-run timing in weather_data.py | 18fbc9d | HRRRFetcher class (hourly->daily max), OrderBookDepth (book walk), MODEL_RUN_SCHEDULE + next_model_run() |
| 2 | Integrate all Phase 3 features into weather-bot.py | 6344cf9 | HRRR blending, compute_adaptive_interval(), orderbook depth gating, model-run timing override, config flags |

## What Was Built

### 1. HRRRFetcher (weather_data.py)
- Fetches HRRR deterministic forecast from Open-Meteo `hrrr_conus` model
- HRRR provides hourly temperatures; fetcher groups by calendar day and computes daily max
- Returns `{date_str: max_temp_f}` for next ~48 hours
- Handles API failures gracefully (returns None, non-blocking)

### 2. OrderBookDepth (weather_data.py)
- Fetches Kalshi orderbook via `/markets/{ticker}/orderbook` API
- Parses YES bids (sorted descending) and converts NO bids to YES asks at `100-price` (sorted ascending)
- `estimate_fill_price()`: walks the book to compute volume-weighted average fill price
- Returns None on insufficient liquidity or API error

### 3. Model Run Timing (weather_data.py)
- `MODEL_RUN_SCHEDULE`: GFS (4x/day, 3.5h delay), ECMWF (2x/day, 6h delay), HRRR (hourly, 45min delay)
- `next_model_run(now_utc)`: returns `(model_name, minutes_until_available)` for soonest upcoming model run
- Bot shortens scan interval when fresh data is about to drop

### 4. HRRR Blending (weather-bot.py)
- Fetches HRRR data for all 20 cities each scan cycle
- **Empirical CDF path**: injects HRRR temp as replicated members (60% of count for day-0, 30% for day-1)
- **Parametric fallback path**: adds HRRR as additional model in forecast dict
- Silently continues without HRRR if fetch fails (non-blocking)

### 5. Adaptive Scan Cadence (weather-bot.py)
- `compute_adaptive_interval()`: parses market tickers to find nearest settlement date
- Returns 5 min (day-0), 15 min (day-1), or 30 min (day-2+)
- Disabled gracefully when `adaptiveScan.enabled` is false (falls back to `scanIntervalMinutes`)

### 6. Orderbook Depth Gating (weather-bot.py)
- Before trade placement, fetches orderbook depth for each opportunity
- Skips markets with insufficient depth (`< minDepthContracts`)
- Improves limit pricing: uses book walk to find better fill price than listed ask

### 7. Config Flags (kalshi-config.json)
- `hrrr.enabled`, `hrrr.weight_day0`, `hrrr.weight_day1`
- `adaptiveScan.enabled`, `adaptiveScan.day0Minutes`, etc.
- `orderbookDepth.enabled`, `orderbookDepth.minDepthContracts`
- All features can be toggled independently for safe production rollout

## Test Coverage

- **20 new tests in test_weather_data.py**: HRRRFetcher (6), OrderBookDepth (6), ModelRunSchedule (8)
- **20 new tests in test_weather_bot_phase3.py**: AdaptiveInterval (7), HRRRMemberInjection (4), ModelRunTiming (2), OrderbookDepth (3), ConfigPhase3 (4)
- **143 total weather tests pass** (0 regressions)

## Deviations from Plan

None - plan executed exactly as written.

## Self-Check: PASSED

All created files exist, all commits verified, all 143 tests pass.
