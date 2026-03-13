# Weather Bot Bias Correction & Recalibration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct massive systematic warm bias (+4-18F) in NWP model forecasts, recalibrate sigma using post-bias residual std, and update ensemble weights from historical calibration evidence.

**Architecture:** BiasCorrector class in weather_data.py loads per-city/per-model bias from config/historical-calibration.json. weather-bot.py applies corrections at three points: empirical ensemble (bias_offset), parametric ensemble (per-model temp correction), and single-model fallback. calibrate-historical.py enhanced to output sigma values from residual std.

**Tech Stack:** Python 3, pytest, Open-Meteo Historical Forecast API, IEM ASOS

**Spec:** `docs/superpowers/specs/2026-03-11-weather-bias-correction-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/kalshi/weather_data.py` | Modify | Add BiasCorrector class (~80 lines) |
| `src/kalshi/weather-bot.py` | Modify | Apply bias correction at 4 points in scan_and_trade |
| `scripts/calibrate-historical.py` | Modify | Add --write-sigma flag for sigma output |
| `config/kalshi-config.json` | Modify | Update ensemble weights, enable previousRuns |
| `tests/test_weather_data.py` | Modify | Add TestBiasCorrector class (~18 tests) |
| `tests/test_weather.py` | Modify | Add bias correction + blending tests |
| `src/kalshi/forecast_verifier.py` | NOT modified | Integration handled via alpha-blending in weather-bot.py |

---

## Chunk 1: BiasCorrector Class + Tests

### Task 1: BiasCorrector — Write failing tests

**Files:**
- Modify: `tests/test_weather_data.py` (add after line 863)

- [ ] **Step 1: Add TestBiasCorrector class with core tests**

Add at the end of `tests/test_weather_data.py`:

```python
class TestBiasCorrector:
    """Tests for BiasCorrector — per-city, per-model forecast bias correction."""

    SAMPLE_CALIBRATION = {
        "n_forecasts": 100,
        "per_city": {
            "MIA": {
                "gfs": {"bias": 8.9, "rmse": 9.9, "mae": 8.9, "n": 178},
                "ecmwf": {"bias": 7.0, "rmse": 8.3, "mae": 7.0, "n": 178},
            },
            "DEN": {
                "gfs": {"bias": 18.1, "rmse": 19.3, "mae": 18.1, "n": 178},
                "graphcast": {"bias": 14.8, "rmse": 16.3, "mae": 14.9, "n": 178},
            },
        },
        "global": {
            "gfs": {"bias": 10.4, "rmse": 12.1, "mae": 10.5, "n": 3560},
            "ecmwf": {"bias": 9.3, "rmse": 11.2, "mae": 9.4, "n": 3417},
        },
    }

    def _make_corrector(self, data=None):
        from weather_data import BiasCorrector
        bc = BiasCorrector.__new__(BiasCorrector)
        bc.log = MagicMock()
        bc._data = data if data is not None else self.SAMPLE_CALIBRATION
        return bc

    def test_correct_subtracts_city_model_bias(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "gfs", 85.0) == pytest.approx(85.0 - 8.9)

    def test_correct_none_returns_none(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "gfs", None) is None

    def test_correct_unknown_model_uses_city_average(self):
        bc = self._make_corrector()
        # MIA average: (8.9 + 7.0) / 2 = 7.95
        assert bc.correct("MIA", "aifs", 85.0) == pytest.approx(85.0 - 7.95)

    def test_correct_unknown_city_uses_global_model_bias(self):
        bc = self._make_corrector()
        assert bc.correct("SEA", "gfs", 55.0) == pytest.approx(55.0 - 10.4)

    def test_correct_unknown_city_and_model_uses_global_average(self):
        bc = self._make_corrector()
        # global average: (10.4 + 9.3) / 2 = 9.85
        assert bc.correct("SEA", "aifs", 55.0) == pytest.approx(55.0 - 9.85)

    def test_correct_forecast_dict(self):
        bc = self._make_corrector()
        forecasts = {"gfs": 85.0, "ecmwf": 83.0}
        result = bc.correct_forecast_dict("MIA", forecasts)
        assert result["gfs"] == pytest.approx(85.0 - 8.9)
        assert result["ecmwf"] == pytest.approx(83.0 - 7.0)

    def test_correct_forecast_dict_preserves_none(self):
        bc = self._make_corrector()
        result = bc.correct_forecast_dict("MIA", {"gfs": None, "ecmwf": 83.0})
        assert result["gfs"] is None
        assert result["ecmwf"] == pytest.approx(83.0 - 7.0)

    def test_city_average_bias(self):
        bc = self._make_corrector()
        assert bc.city_average_bias("MIA") == pytest.approx((8.9 + 7.0) / 2)

    def test_city_average_bias_unknown_city_uses_global(self):
        bc = self._make_corrector()
        assert bc.city_average_bias("SEA") == pytest.approx((10.4 + 9.3) / 2)

    def test_residual_std_specific_model(self):
        bc = self._make_corrector()
        import math
        expected = math.sqrt(9.9**2 - 8.9**2)
        assert bc.residual_std("MIA", "gfs") == pytest.approx(expected, abs=0.01)

    def test_residual_std_city_average(self):
        bc = self._make_corrector()
        import math
        r_gfs = math.sqrt(9.9**2 - 8.9**2)
        r_ecmwf = math.sqrt(8.3**2 - 7.0**2)
        expected = (r_gfs + r_ecmwf) / 2
        assert bc.residual_std("MIA") == pytest.approx(expected, abs=0.01)

    def test_residual_std_unknown_city_uses_global(self):
        bc = self._make_corrector()
        import math
        r_gfs = math.sqrt(12.1**2 - 10.4**2)
        r_ecmwf = math.sqrt(11.2**2 - 9.3**2)
        expected = (r_gfs + r_ecmwf) / 2
        assert bc.residual_std("SEA") == pytest.approx(expected, abs=0.01)

    def test_empty_data_returns_raw_temp(self):
        bc = self._make_corrector(data={})
        assert bc.correct("MIA", "gfs", 85.0) == 85.0

    def test_empty_data_residual_std_returns_default(self):
        bc = self._make_corrector(data={})
        assert bc.residual_std("MIA") == 4.7

    def test_residual_std_floor(self):
        """residual_std should never go below 0.5F (even when RMSE == bias)."""
        data = {"per_city": {"TEST": {"model": {"bias": 5.0, "rmse": 5.0, "n": 100}}}}
        bc = self._make_corrector(data=data)
        assert bc.residual_std("TEST", "model") == 0.5

    def test_constructor_missing_file(self):
        """BiasCorrector with nonexistent path should work with no corrections."""
        from weather_data import BiasCorrector
        bc = BiasCorrector(calibration_path="/nonexistent/path.json")
        assert bc.correct("MIA", "gfs", 85.0) == 85.0
        assert bc.city_average_bias("MIA") == 0.0

    def test_constructor_loads_real_calibration(self):
        """BiasCorrector loads config/historical-calibration.json if it exists."""
        from weather_data import BiasCorrector
        import os
        cal_path = os.path.join(os.path.dirname(__file__), "..", "config", "historical-calibration.json")
        if os.path.exists(cal_path):
            bc = BiasCorrector(calibration_path=cal_path)
            # Should have loaded data — MIA bias should be > 0
            assert bc.city_average_bias("MIA") > 0
```

Also update the import line at the top of the file (line 10):
```python
from weather_data import STATION_MAP, EnsembleCollector, IEMFetcher, TrainingStore, NWSForecastFetcher, NWS_GRID_MAP, NAMFetcher, PreviousRunsFetcher, BiasCorrector
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_weather_data.py::TestBiasCorrector -v`
Expected: FAIL — `ImportError: cannot import name 'BiasCorrector' from 'weather_data'`

---

### Task 2: BiasCorrector — Write implementation

**Files:**
- Modify: `src/kalshi/weather_data.py` (add after PreviousRunsFetcher class, before OrderBookDepth at line 703)

- [ ] **Step 3: Add imports needed by BiasCorrector**

At the top of `weather_data.py`, add `json` and `math` to imports (line 15-18), and add `Path` from pathlib:

```python
import datetime
import json
import logging
import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
```

- [ ] **Step 4: Add BiasCorrector class**

Insert before the `class OrderBookDepth:` line (before line 703):

```python
class BiasCorrector:
    """Corrects systematic forecast bias using historical calibration data.

    Loads per-city, per-model bias from config/historical-calibration.json
    and applies correction: corrected_temp = raw_temp - bias.

    Fallback chain: per-city per-model > city average > global model > global average > 0.
    """

    def __init__(self, calibration_path=None, logger=None):
        self.log = logger or _log
        self._data = {}
        self._load(calibration_path)

    def _load(self, path=None):
        if path is None:
            path = Path(__file__).resolve().parent.parent.parent / "config" / "historical-calibration.json"
        try:
            p = Path(path)
            if p.exists():
                self._data = json.loads(p.read_text())
                n = self._data.get("n_forecasts", 0)
                self.log.info("BiasCorrector loaded: %d forecast pairs", n)
        except Exception as e:
            self.log.warning("BiasCorrector: failed to load calibration: %s", e)

    def correct(self, city, model, temp):
        """Apply per-city, per-model bias correction.

        Returns corrected_temp = raw_temp - bias.
        Falls back to city average bias if model not calibrated.
        Returns raw temp if no calibration data available.
        """
        if temp is None:
            return None
        bias = self._get_bias(city, model)
        return temp - bias

    def correct_forecast_dict(self, city, forecasts):
        """Correct all model temps in a {model: temp} dict.

        Returns new dict with corrected temperatures.
        """
        return {
            model: self.correct(city, model, temp)
            for model, temp in forecasts.items()
        }

    def city_average_bias(self, city):
        """Average bias across all calibrated models for a city.

        Used for ensemble members where individual model attribution
        is not possible (GEFS + ECMWF EPS mixed members).
        """
        per_city = self._data.get("per_city", {}).get(city, {})
        if not per_city:
            return self._global_average_bias()
        biases = [m["bias"] for m in per_city.values() if "bias" in m]
        return sum(biases) / len(biases) if biases else 0.0

    def residual_std(self, city, model=None):
        """Compute residual std: sqrt(RMSE^2 - bias^2).

        This is the forecast uncertainty AFTER bias removal.
        If model is None, returns weighted average across models.
        """
        per_city = self._data.get("per_city", {}).get(city, {})
        if model and model in per_city:
            return self._compute_residual(per_city[model])

        residuals = []
        for m_stats in per_city.values():
            r = self._compute_residual(m_stats)
            if r is not None:
                residuals.append(r)
        if residuals:
            return sum(residuals) / len(residuals)

        return self._global_residual_std()

    def _get_bias(self, city, model):
        per_city = self._data.get("per_city", {}).get(city, {})
        if model in per_city:
            return per_city[model].get("bias", 0.0)
        if per_city:
            return self.city_average_bias(city)
        global_stats = self._data.get("global", {}).get(model, {})
        if global_stats:
            return global_stats.get("bias", 0.0)
        return self._global_average_bias()

    def _global_average_bias(self):
        global_stats = self._data.get("global", {})
        biases = [m["bias"] for m in global_stats.values() if "bias" in m]
        return sum(biases) / len(biases) if biases else 0.0

    def _compute_residual(self, stats):
        if not stats or "rmse" not in stats or "bias" not in stats:
            return None
        sq = stats["rmse"] ** 2 - stats["bias"] ** 2
        return max(0.5, math.sqrt(max(0, sq)))

    def _global_residual_std(self):
        global_stats = self._data.get("global", {})
        residuals = [self._compute_residual(s) for s in global_stats.values()]
        residuals = [r for r in residuals if r is not None]
        return sum(residuals) / len(residuals) if residuals else 4.7
```

- [ ] **Step 5: Run BiasCorrector tests to verify they pass**

Run: `pytest tests/test_weather_data.py::TestBiasCorrector -v`
Expected: All 15 tests PASS

- [ ] **Step 6: Run full weather_data test suite to verify no regressions**

Run: `pytest tests/test_weather_data.py -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 7: Commit BiasCorrector**

```bash
git add src/kalshi/weather_data.py tests/test_weather_data.py
git commit -m "feat(weather): add BiasCorrector for per-city per-model forecast bias correction

Loads historical calibration data and corrects systematic warm bias
(+4-18F) discovered across all NWP models. Fallback chain:
city+model > city average > global model > global average > 0."
```

---

## Chunk 2: Apply Bias Correction in weather-bot.py

### Task 3: Bias correction integration — Write failing test

**Files:**
- Modify: `tests/test_weather.py`

- [ ] **Step 8: Add bias correction integration and blending tests**

Add to `tests/test_weather.py` (after existing TestEnsembleModels class):

```python
def test_bias_corrector_imported():
    """BiasCorrector must be importable from weather_data and used in weather-bot."""
    assert hasattr(_mod, 'bias_corrector'), "weather-bot.py must instantiate bias_corrector"


class TestBiasBlending:
    """Test the alpha-ramp blending between historical and live bias."""

    def _compute_blend(self, hist_bias, live_bias, live_n):
        """Replicate the blending logic from weather-bot.py scan_and_trade."""
        alpha = min(1.0, live_n / 20.0) if live_n > 0 else 0.0
        return alpha * live_bias + (1 - alpha) * hist_bias

    def test_no_live_data_uses_historical(self):
        """When live_n=0, alpha=0, 100% historical bias."""
        assert self._compute_blend(hist_bias=8.5, live_bias=3.0, live_n=0) == 8.5

    def test_half_ramp_blends_50_50(self):
        """When live_n=10, alpha=0.5, 50/50 blend."""
        result = self._compute_blend(hist_bias=8.0, live_bias=4.0, live_n=10)
        assert abs(result - 6.0) < 0.01

    def test_full_ramp_uses_live(self):
        """When live_n=20, alpha=1.0, 100% live bias."""
        assert self._compute_blend(hist_bias=8.0, live_bias=3.0, live_n=20) == 3.0

    def test_beyond_ramp_stays_live(self):
        """When live_n>20, alpha still capped at 1.0."""
        assert self._compute_blend(hist_bias=8.0, live_bias=3.0, live_n=100) == 3.0

    def test_none_city_bias_defaults_to_historical(self):
        """Simulates city_bias=None scenario (no ForecastVerifier data)."""
        # In weather-bot.py: live_bias defaults to 0.0, live_n defaults to 0
        result = self._compute_blend(hist_bias=8.5, live_bias=0.0, live_n=0)
        assert result == 8.5
```

- [ ] **Step 9: Run tests to verify they fail**

Run: `pytest tests/test_weather.py::test_bias_corrector_imported tests/test_weather.py::TestBiasBlending -v`
Expected: FAIL — `bias_corrector` not found on module (blending tests should pass since they don't import the bot module)

---

### Task 4: Apply bias correction in weather-bot.py

**Files:**
- Modify: `src/kalshi/weather-bot.py`

- [ ] **Step 10: Add BiasCorrector import and instantiation**

Update the import line (line 14) to include BiasCorrector:
```python
from weather_data import EnsembleCollector, HRRRFetcher, NAMFetcher, PreviousRunsFetcher, OrderBookDepth, next_model_run, STATION_MAP, NWSForecastFetcher, BiasCorrector
```

Add after the `prev_runs_fetcher` instantiation (after the PreviousRunsFetcher line, around line 194):
```python
# Bias corrector — loads historical calibration for per-city per-model correction
bias_corrector = BiasCorrector(logger=log)
```

- [ ] **Step 11: Apply Point D — bias-correct forecast_temp for logging/filter**

In `scan_and_trade()`, replace the mean forecast_temp computation (line 802-803):

**Before (line 802-803):**
```python
            valid_temps = [t for t in forecast_data.values() if t is not None]
            forecast_temp = sum(valid_temps) / len(valid_temps) if valid_temps else None  # mean for logging
```

**After:**
```python
            # Bias-correct each model's forecast before computing mean
            corrected_data = bias_corrector.correct_forecast_dict(city, forecast_data)
            valid_temps = [t for t in corrected_data.values() if t is not None]
            forecast_temp = sum(valid_temps) / len(valid_temps) if valid_temps else None
```

- [ ] **Step 12: Apply Point B — empirical ensemble bias_offset**

Replace the bias line for empirical ensemble (line 849-850):

**Before (line 849-850):**
```python
                    # Get station bias from verifier if available
                    bias = city_bias.get(city, {}).get("bias_f", 0.0) if city_bias else 0.0
```

**After:**
```python
                    # Blend historical calibration bias with live ForecastVerifier bias
                    hist_bias = bias_corrector.city_average_bias(city)
                    live_bias = city_bias.get(city, {}).get("bias_f", 0.0) if city_bias else 0.0
                    live_n = city_bias.get(city, {}).get("n", 0) if city_bias else 0
                    alpha = min(1.0, live_n / 20.0) if live_n > 0 else 0.0
                    bias = alpha * live_bias + (1 - alpha) * hist_bias
```

- [ ] **Step 13: Apply Point A — parametric ensemble bias correction**

Replace the parametric_data construction (line 867):

**Before (line 867):**
```python
                parametric_data = dict(forecast_data)
```

**After:**
```python
                parametric_data = bias_corrector.correct_forecast_dict(city, dict(forecast_data))
```

Note: HRRR and NAM temps added AFTER this line are NOT bias-corrected (per spec — mesoscale models have better terrain representation).

- [ ] **Step 14: Apply Point C — single-model fallback bias correction**

Replace the single-model probability computation (lines 907-917):

**Before (lines 907-917):**
```python
        else:
            if isinstance(forecast_data, dict):
                if not forecast_data:
                    ss.skip("empty_forecast")
                    continue
                forecast_temp = list(forecast_data.values())[0]
            else:
                forecast_temp = forecast_data
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue
            our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)
```

**After:**
```python
        else:
            if isinstance(forecast_data, dict):
                if not forecast_data:
                    ss.skip("empty_forecast")
                    continue
                # Get first model key and correct its temp
                first_model = list(forecast_data.keys())[0]
                forecast_temp = bias_corrector.correct(city, first_model, forecast_data[first_model])
            else:
                forecast_temp = forecast_data
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue
            our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)
```

- [ ] **Step 15: Add bias correction logging**

After the bias_offset computation (after the new alpha/bias blend), add logging:

```python
                    if abs(bias) > 0.1:
                        log.info("  %s: bias correction %.1fF (hist=%.1fF, live=%.1fF, alpha=%.2f)",
                                 ticker, bias, hist_bias, live_bias, alpha)
```

- [ ] **Step 16: Run integration test**

Run: `pytest tests/test_weather.py::test_bias_corrector_imported -v`
Expected: PASS

- [ ] **Step 17: Run full weather bot test suite**

Run: `pytest tests/test_weather.py tests/test_weather_data.py tests/test_advanced_weather.py tests/test_weather_bot_phase3.py tests/test_weather_bot_bugs.py tests/test_weather_city_expansion.py tests/test_empirical_ensemble.py -v`
Expected: All tests PASS

- [ ] **Step 18: Commit bias correction integration**

```bash
git add src/kalshi/weather-bot.py tests/test_weather.py
git commit -m "feat(weather): apply bias correction at all 4 forecast points

Corrects parametric ensemble (per-model), empirical ensemble (city avg),
single-model fallback, and mean forecast_temp for logging/filter.
Blends historical bias with ForecastVerifier live bias (alpha ramp)."
```

---

## Chunk 3: Config Updates + Sigma Enhancement

### Task 5: Update ensemble weights and enable previousRuns

**Files:**
- Modify: `config/kalshi-config.json`

- [ ] **Step 19: Update ensemble weights from historical calibration evidence**

In `config/kalshi-config.json`, replace the weights section (lines 36-43):

**Before:**
```json
    "weights": {
      "gfs": 0.18,
      "ecmwf": 0.20,
      "icon": 0.08,
      "nbm": 0.30,
      "aifs": 0.12,
      "graphcast": 0.12
    }
```

**After:**
```json
    "weights": {
      "graphcast": 0.25,
      "ecmwf": 0.23,
      "gfs": 0.20,
      "icon": 0.20,
      "nbm": 0.08,
      "aifs": 0.04
    }
```

- [ ] **Step 20: Enable previousRuns convergence signal**

In `config/kalshi-config.json`, change previousRuns.enabled from false to true (line 74):

**Before:**
```json
  "previousRuns": {
    "enabled": false,
    "model": "gfs_seamless"
  }
```

**After:**
```json
  "previousRuns": {
    "enabled": true,
    "model": "gfs_seamless"
  }
```

- [ ] **Step 21: Run config-dependent tests**

Run: `pytest tests/test_weather_city_expansion.py tests/test_weather_bot_phase3.py -v`
Expected: All PASS (these tests check config structure, not specific weight values)

- [ ] **Step 22: Commit config updates**

```bash
git add config/kalshi-config.json
git commit -m "feat(weather): update ensemble weights from historical calibration

GraphCast 25% (was 12%), ECMWF 23% (was 20%), GFS/ICON 20%.
NBM reduced to 8% (no historical data), AIFS 4%.
Enable previousRuns convergence signal."
```

---

### Task 6: Enhance calibrate-historical.py with --write-sigma

**Files:**
- Modify: `scripts/calibrate-historical.py`

- [ ] **Step 23: Add --write-sigma flag and residual_std computation**

Add `--write-sigma` argument to the argument parser (after line 157):
```python
    parser.add_argument("--write-sigma", action="store_true",
                        help="Write per-city sigma values to config/calibration.json")
```

Add a `compute_residual_stds` function after `compute_optimal_weights` (after line 143):
```python
def compute_residual_stds(all_city_stats):
    """Compute per-city residual std (forecast uncertainty after bias removal).

    Returns dict of {city: residual_std_f}
    """
    result = {}
    for city, models in all_city_stats.items():
        residuals = []
        for model, stats in models.items():
            if stats and stats.get("rmse") and stats.get("bias"):
                sq = stats["rmse"] ** 2 - stats["bias"] ** 2
                if sq > 0:
                    residuals.append(math.sqrt(sq))
        if residuals:
            result[city] = round(sum(residuals) / len(residuals), 3)
    return result
```

Add a `write_sigma_to_calibration` function:
```python
def write_sigma_to_calibration(residual_stds, shrinkage_k=15):
    """Write per-city sigma_intercept to config/calibration.json.

    Uses Bayesian shrinkage toward global average.
    Preserves existing calibration.json fields.
    """
    cal_path = PROJECT_DIR / "config" / "calibration.json"
    try:
        cal = json.loads(cal_path.read_text()) if cal_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        cal = {}

    # Compute global average residual std
    all_stds = list(residual_stds.values())
    if not all_stds:
        print("No residual stds computed, skipping sigma write")
        return
    global_std = sum(all_stds) / len(all_stds)

    # Update weather section
    weather = cal.setdefault("weather", {})
    weather["global_sigma_intercept"] = round(global_std, 2)
    weather["global_sigma_slope"] = weather.get("global_sigma_slope", 0.2)

    per_city = weather.setdefault("per_city", {})
    for city, city_std in residual_stds.items():
        n = 178  # typical n from historical calibration
        shrinkage_weight = round(n / (n + shrinkage_k), 3)
        shrunk_std = round((n * city_std + shrinkage_k * global_std) / (n + shrinkage_k), 2)

        city_cal = per_city.setdefault(city, {})
        city_cal["sigma_intercept"] = shrunk_std
        city_cal["sigma_slope"] = city_cal.get("sigma_slope", 0.2)
        city_cal["n"] = n
        city_cal["shrinkage_weight"] = shrinkage_weight
        city_cal["raw_residual_std"] = round(city_std, 3)

    cal["generated_at"] = datetime.datetime.now().isoformat()
    cal_path.write_text(json.dumps(cal, indent=2))
    print(f"\nSigma values written to {cal_path}")
    print(f"  Global sigma_intercept: {global_std:.2f}F")
    print(f"  Per-city sigmas: {len(residual_stds)} cities")
    for city in sorted(residual_stds.keys()):
        city_data = per_city[city]
        print(f"    {city}: {city_data['sigma_intercept']:.2f}F (raw={city_data['raw_residual_std']:.2f}F)")
```

In the `main()` function, after the output is built (after line 285), add:
```python
    # Compute and optionally write sigma values
    residual_stds = compute_residual_stds(all_city_stats)
    if residual_stds:
        print(f"\nResidual Std (post-bias forecast uncertainty):")
        for city in sorted(residual_stds.keys()):
            print(f"  {city}: {residual_stds[city]:.2f}F")

    if args.write_sigma:
        write_sigma_to_calibration(residual_stds)
```

- [ ] **Step 24: Run calibrate-historical.py --dry-run to verify flag is accepted**

Run: `python3 scripts/calibrate-historical.py --dry-run`
Expected: Shows "Historical Forecast Calibration" preview with no errors

- [ ] **Step 25: Commit calibrate-historical.py enhancement**

```bash
git add scripts/calibrate-historical.py
git commit -m "feat(weather): add --write-sigma flag to historical calibration

Computes per-city residual_std = sqrt(RMSE^2 - bias^2) from historical
forecast data. Writes to calibration.json with Bayesian shrinkage (k=15)
toward global average. Preserves existing calibration.json fields."
```

---

## Chunk 4: Update module docstring + Final verification

### Task 7: Update weather_data.py module docstring

**Files:**
- Modify: `src/kalshi/weather_data.py`

- [ ] **Step 26: Update module docstring to include BiasCorrector**

Replace line 1 of `src/kalshi/weather_data.py`:

**Before:**
```python
"""Weather data infrastructure for ensemble collection, IEM station actuals, and training storage.

Provides:
- STATION_MAP: Kalshi city code -> IEM ASOS station ID mapping
- NWS_GRID_MAP: Kalshi city code -> NWS API grid point mapping
- NWSForecastFetcher: Fetches NWS 7-day forecast as fallback data source
- EnsembleCollector: Fetches raw ensemble member temperatures from Open-Meteo
- HRRRFetcher: Fetches HRRR deterministic forecast data from Open-Meteo
- IEMFetcher: Fetches actual daily high temperatures from Iowa Environmental Mesonet
- OrderBookDepth: Fetches and analyzes Kalshi order book depth
- MODEL_RUN_SCHEDULE / next_model_run(): Model run timing awareness
- TrainingStore: SQLite storage for forecast-vs-actual training pairs
"""
```

**After:**
```python
"""Weather data infrastructure for ensemble collection, IEM station actuals, and training storage.

Provides:
- STATION_MAP: Kalshi city code -> IEM ASOS station ID mapping
- NWS_GRID_MAP: Kalshi city code -> NWS API grid point mapping
- NWSForecastFetcher: Fetches NWS 7-day forecast as fallback data source
- EnsembleCollector: Fetches raw ensemble member temperatures from Open-Meteo
- HRRRFetcher: Fetches HRRR deterministic forecast data from Open-Meteo
- NAMFetcher: Fetches NAM 3km deterministic forecast from Open-Meteo
- PreviousRunsFetcher: Forecast convergence analysis from previous model runs
- BiasCorrector: Per-city per-model systematic forecast bias correction
- IEMFetcher: Fetches actual daily high temperatures from Iowa Environmental Mesonet
- OrderBookDepth: Fetches and analyzes Kalshi order book depth
- MODEL_RUN_SCHEDULE / next_model_run(): Model run timing awareness
- TrainingStore: SQLite storage for forecast-vs-actual training pairs
"""
```

---

### Task 8: Full test suite verification

- [ ] **Step 27: Run the full weather test suite**

Run: `pytest tests/test_weather.py tests/test_weather_data.py tests/test_advanced_weather.py tests/test_weather_bot_phase3.py tests/test_weather_bot_bugs.py tests/test_weather_city_expansion.py tests/test_empirical_ensemble.py -v`
Expected: All tests PASS (202+ tests)

- [ ] **Step 28: Final commit**

```bash
git add src/kalshi/weather_data.py
git commit -m "docs(weather): update weather_data module docstring with new classes"
```

---

## Post-Implementation Checklist

After all tasks are complete:

1. **Run sigma calibration:**
   ```bash
   python3 scripts/calibrate-historical.py --save --write-sigma
   ```
   This updates both `config/historical-calibration.json` (for BiasCorrector) and `config/calibration.json` (for probability.py sigma).

2. **Demo mode smoke test:**
   ```bash
   KALSHI_MODE=demo python3 src/kalshi/weather-bot.py
   ```
   Watch for: "BiasCorrector loaded: 14177 forecast pairs" log line, bias correction logs showing corrected temps, no errors.

3. **Monitor first scan cycle:** Verify that:
   - Corrected forecast temps are ~4-18F lower than raw (depending on city)
   - Probabilities look reasonable (not all 99% or all 1%)
   - Near-threshold filter uses corrected temps for distance calculation
   - Empirical ensemble bias_offset uses blended historical+live bias
