# Weather Bot v2 — Continuation Prompt

## Start here

Create branch `weather-bot-v2` from main and continue implementing the world-class weather bot design. Phases 0-2 are complete on main. Implement Phases 3-4.

## What's done (on main)

**Phase 0: Critical Fixes** — COMPLETE
- NY coordinates fixed to Central Park (40.7829, -73.9654), station KNYC
- ForecastVerifier switched from ERA5 reanalysis to IEM ASOS station data
- Near-threshold filter widened from 2F to 4F
- Commits: c87b077

**Phase 1: Data Infrastructure** — COMPLETE
- New module `src/kalshi/weather_data.py`: STATION_MAP (20 cities), EnsembleCollector (fetches 82-member data from Open-Meteo Ensemble API), IEMFetcher (fetches IEM ASOS actuals), TrainingStore (SQLite)
- New script `scripts/backfill-weather-data.py`: CLI for historical data collection
- 24 tests in `tests/test_weather_data.py`
- Commits: 5498760

**Phase 2: Empirical Ensemble CDF** — COMPLETE
- New function `empirical_ensemble_probability()` in `src/kalshi/probability.py`: KDE-smoothed CDF from raw ensemble members, Silverman bandwidth, station bias correction, [0.01, 0.99] clamping
- Integrated into `src/kalshi/weather-bot.py` as primary model with parametric fallback
- 22 tests in `tests/test_empirical_ensemble.py`
- Commits: f1feba5

## What to build next

### Phase 3: HRRR Integration + Adaptive Execution

1. **HRRR data fetching**: Add HRRR to Open-Meteo fetch using `model=hrrr_conus` (48h forecast horizon, hourly updates). Available at `https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&temperature_unit=fahrenheit&timezone=America%2FNew_York&models=hrrr_conus`

2. **HRRR weighting in ensemble**: For day-0 markets, HRRR gets 60% weight (3km resolution dominates). Day-1: 30% weight. Day-2+: 0% (HRRR only goes 48h). Blend HRRR deterministic forecast with ensemble CDF uncertainty.

3. **Adaptive scan cadence**: Change the fixed 30-min scan interval to:
   - 5 min when day-0 markets exist
   - 15 min when day-1 markets are the nearest
   - 30 min otherwise (day-2+)
   The bot should check which markets are closest to settlement and set the next scan interval accordingly.

4. **Order book depth**: Fetch `/markets/{ticker}/orderbook` from Kalshi API for markets we're considering trading. Use depth to estimate fill probability and improve limit price selection. The API endpoint exists but is never called in the current codebase.

5. **Model-run timing awareness**: Know when GFS (4x/day: 00Z, 06Z, 12Z, 18Z), ECMWF (2x/day: 00Z, 12Z), and HRRR (hourly) publish new runs. Trigger a scan within 2 minutes of new data being available.

### Phase 4: ML Post-Processor (if enough data has accumulated)

Run `python3 scripts/backfill-weather-data.py --days 90` FIRST to populate the training store.

Then build XGBoost EMOS model:
- **Features**: ensemble mean, spread, skewness, kurtosis, P10/P25/P50/P75/P90 quantiles, HRRR-vs-ensemble disagreement, station rolling bias, day-of-year (seasonality), lead time, recent forecast error trend
- **Target**: P(T_max > threshold) calibrated against NWS CLI actuals (from IEM ASOS)
- **Calibration**: Isotonic regression on held-out set
- **Initially trained on**: deterministic forecast errors from Previous Runs API (2021+) + IEM actuals
- **Retrained with**: ensemble features once 4-6 weeks of data accumulates

## Key files to read

- `docs/plans/2026-03-05-weather-bot-world-class-design.md` — Full design document
- `src/kalshi/weather-bot.py` — Main bot (already has empirical CDF integration)
- `src/kalshi/probability.py` — Probability models (has empirical_ensemble_probability)
- `src/kalshi/weather_data.py` — Data infrastructure (EnsembleCollector, IEMFetcher, TrainingStore)
- `src/kalshi/forecast_verifier.py` — Verification pipeline (IEM ASOS, DEFAULT_STATION_MAP)
- `config/kalshi-config.json` — Bot config (20 cities, ensemble settings)

## Key context

- **Brier score is 0.31** (target: <0.15). Calibration is broken: 0-20% predicted bin has 35% actual hit rate.
- **Bot is profitable only due to Kelly sizing** — flat sizing would lose money.
- **Miami is worst city** (Brier 0.577), LA bad (0.391), Austin best (0.184).
- **Open-Meteo Ensemble API**: `https://ensemble-api.open-meteo.com/v1/ensemble` — returns per-member data (temperature_2m_max_member01, etc.). GEFS 31 members + ECMWF ENS 51 members = 82 total.
- **IEM ASOS**: `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py` — actual station temps.
- **Kalshi settles on NWS CLI** (not ERA5). Time zone is always LST (not DST). Whole degrees F.
- **Other Claudes working on other bots** — stay on `weather-bot-v2` branch, merge to main when done.

## Branch setup

```bash
git checkout main
git pull
git checkout -b weather-bot-v2
```

## Use GSD quick for implementation

```
/gsd:quick Implement weather bot v2 Phase 3: HRRR integration, adaptive scan cadence, order book depth fetching, model-run timing awareness. Design doc: docs/plans/2026-03-05-weather-bot-world-class-design.md
```
