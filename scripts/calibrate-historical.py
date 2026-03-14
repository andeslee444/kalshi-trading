#!/usr/bin/env python3
"""Historical bias calibration using Open-Meteo Historical Forecast API.

Fetches what models PREDICTED in the past, compares against settlement actuals,
and computes per-city, per-model systematic temperature bias priors.

Unlike backfill-weather-data.py (which uses the Previous Runs API with ~90-day
lookback), this uses the Historical Forecast API which provides forecast data
going back to 2022 for all models including NBM, AIFS, and GraphCast.

Important: Open-Meteo's Historical Forecast API is useful for station/grid bias
estimation, but it is not lead-time matched to the horizons the live bot trades.
Do not use this script to overwrite live sigma or ensemble weights.

Usage:
    python3 scripts/calibrate-historical.py                          # All cities, 180 days
    python3 scripts/calibrate-historical.py --days 90                # 90 days
    python3 scripts/calibrate-historical.py --city MIA               # Single city
    python3 scripts/calibrate-historical.py --cities AUS,DEN,HOU     # Targeted city set
    python3 scripts/calibrate-historical.py --models gfs,ecmwf,nbm   # Specific models
    python3 scripts/calibrate-historical.py --actuals-source iem     # Faster proxy actuals
    python3 scripts/calibrate-historical.py --save                   # Write audit artifact
    python3 scripts/calibrate-historical.py --output data/foo.json   # Write to explicit path
    python3 scripts/calibrate-historical.py --merge-bias             # Store bias priors in calibration.json
    python3 scripts/calibrate-historical.py --dry-run                # Preview only
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

# Add src/kalshi to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import PROJECT_DIR, retry_request, atomic_write_json
from weather_data import (
    IEMFetcher,
    STATION_MAP,
    SettlementTemperatureFetcher,
    _city_timezone_name,
    open_meteo_model_name,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


# Models to calibrate (Open-Meteo API identifiers)
MODELS = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_seamless",
    "nbm": "nbm_conus",
    "aifs": "ecmwf_aifs025",
    "graphcast": "gfs_graphcast025",
}


def get_city_coords():
    """Load city coordinates from kalshi-config.json."""
    config_path = PROJECT_DIR / "config" / "kalshi-config.json"
    return json.loads(config_path.read_text()).get("cities", {})


def fetch_historical_forecasts(lat, lon, start_date, end_date, model_name, city_code=None):
    """Fetch historical model predictions from Open-Meteo Historical Forecast API.

    This returns what the model PREDICTED for each date (not the same as actuals).

    Args:
        lat, lon: coordinates
        start_date, end_date: ISO date strings (YYYY-MM-DD)
        model_name: Open-Meteo model identifier (e.g., "gfs_seamless")

    Returns:
        dict of {date_str: temp_f} or empty dict on failure.
    """
    api_key = os.environ.get("OPEN_METEO_API_KEY", "")
    request_model = open_meteo_model_name(model_name, api_key=api_key)
    timezone_name = quote(_city_timezone_name(city_code=city_code), safe="")
    base = ("https://customer-historical-forecast-api.open-meteo.com/v1/forecast"
            if api_key else "https://historical-forecast-api.open-meteo.com/v1/forecast")

    url = (
        f"{base}?latitude={lat}&longitude={lon}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&timezone={timezone_name}"
        f"&models={request_model}"
        + (f"&apikey={api_key}" if api_key else "")
    )

    try:
        resp = retry_request("GET", url, timeout=30, max_retries=2)
        if resp is None or resp.status_code != 200:
            return {}
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        return {d: t for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
        print(f"  Warning: Historical forecast API error for {model_name}: {e}")
        return {}


def compute_model_stats(forecasts, actuals):
    """Compute error statistics for a single model.

    Args:
        forecasts: dict of {date_str: temp_f} (model predictions)
        actuals: dict of {date_str: temp_f} (observed temps from IEM)

    Returns:
        dict with keys: bias, rmse, mae, n, or None if no overlapping dates.
    """
    errors = []
    for date_str, fc_temp in forecasts.items():
        if date_str in actuals:
            errors.append(fc_temp - actuals[date_str])

    if not errors:
        return None

    n = len(errors)
    bias = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)

    return {
        "bias": round(bias, 3),
        "rmse": round(rmse, 3),
        "mae": round(mae, 3),
        "n": n,
    }


def compute_optimal_weights(model_stats):
    """Compute inverse-MAE ensemble weights across all models.

    Args:
        model_stats: dict of {model_name: {"mae": float, ...}}

    Returns:
        dict of {model_name: weight} summing to 1.0
    """
    inv_mae = {}
    for model, stats in model_stats.items():
        if stats and stats["mae"] > 0 and stats["n"] >= 30:
            inv_mae[model] = 1.0 / stats["mae"]

    if not inv_mae:
        return {}

    total = sum(inv_mae.values())
    return {k: round(v / total, 4) for k, v in inv_mae.items()}


def compute_residual_stds(all_city_stats):
    """Compute per-city residual std (forecast uncertainty after bias removal).

    Returns dict of {city: residual_std_f}
    """
    result = {}
    for city, models in all_city_stats.items():
        residuals = []
        for model, stats in models.items():
            if stats and stats.get("rmse") is not None and stats.get("bias") is not None:
                sq = stats["rmse"] ** 2 - stats["bias"] ** 2
                if sq > 0:
                    residuals.append(math.sqrt(sq))
        if residuals:
            result[city] = round(sum(residuals) / len(residuals), 3)
    return result


def write_bias_to_calibration(output):
    """Write bias priors into config/calibration.json for live loading."""
    cal_path = PROJECT_DIR / "config" / "calibration.json"
    try:
        cal = json.loads(cal_path.read_text()) if cal_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        cal = {}

    weather = cal.setdefault("weather", {})
    weather["bias_correction"] = {
        "generated_at": output["generated_at"],
        "source": output.get("source"),
        "lead_time_matched": output.get("lead_time_matched", False),
        "period": output["period"],
        "actuals_source": output.get("actuals_source"),
        "n_forecasts": output["n_forecasts"],
        "n_cities": output["n_cities"],
        "per_city": output["per_city"],
        "global": output["global"],
    }
    atomic_write_json(cal_path, cal)
    print(f"\nBias priors written to {cal_path} (weather.bias_correction)")


def main():
    parser = argparse.ArgumentParser(description="Historical forecast calibration")
    parser.add_argument("--days", type=int, default=180,
                        help="Days of history to analyze (default: 180)")
    parser.add_argument("--city", type=str, default=None,
                        help="Single city code (default: all)")
    parser.add_argument("--cities", type=str, default=None,
                        help="Comma-separated city codes (e.g. AUS,DEN,HOU)")
    parser.add_argument("--models", type=str, default=None,
                        help="Comma-separated model short names (e.g., gfs,ecmwf,nbm)")
    parser.add_argument(
        "--actuals-source",
        choices=("iem", "settlement"),
        default="settlement",
        help="Actual temperature source for calibration (default: settlement)",
    )
    parser.add_argument("--save", action="store_true",
                        help="Write results to config/historical-calibration.json")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Explicit output path for audit artifact (relative paths resolve under project root)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only, don't fetch data")
    parser.add_argument("--merge-bias", action="store_true",
                        help="Write bias priors to config/calibration.json")
    parser.add_argument("--write-sigma", action="store_true",
                        help="Deprecated: historical API is not suitable for live sigma calibration")
    args = parser.parse_args()

    if args.write_sigma:
        print("Error: --write-sigma was removed because the historical forecast API is not lead-time matched.")
        print("Use scripts/calibrate-sigma.py or live verification data for sigma calibration.")
        sys.exit(2)

    cities = get_city_coords()
    if args.city and args.cities:
        print("Error: use either --city or --cities, not both")
        sys.exit(1)

    if args.city:
        if args.city not in cities:
            print(f"Error: unknown city '{args.city}'. Available: {', '.join(cities.keys())}")
            sys.exit(1)
        target_cities = {args.city: cities[args.city]}
    elif args.cities:
        requested = [c.strip() for c in args.cities.split(",") if c.strip()]
        unknown = [c for c in requested if c not in cities]
        if unknown:
            print(f"Error: unknown cities {', '.join(unknown)}. Available: {', '.join(cities.keys())}")
            sys.exit(1)
        target_cities = {c: cities[c] for c in requested}
    else:
        target_cities = cities

    if args.models:
        target_models = {}
        for m in args.models.split(","):
            m = m.strip()
            if m in MODELS:
                target_models[m] = MODELS[m]
            else:
                print(f"Warning: unknown model '{m}', skipping. Available: {', '.join(MODELS.keys())}")
        if not target_models:
            print("Error: no valid models specified")
            sys.exit(1)
    else:
        target_models = MODELS

    today = datetime.date.today()
    start_date = (today - datetime.timedelta(days=args.days)).isoformat()
    end_date = (today - datetime.timedelta(days=2)).isoformat()  # 2-day lag for actuals

    print(f"Historical Forecast Calibration")
    print(f"  Period: {start_date} to {end_date} ({args.days} days)")
    print(f"  Cities: {len(target_cities)} ({', '.join(target_cities.keys())})")
    print(f"  Models: {len(target_models)} ({', '.join(target_models.keys())})")

    if args.dry_run:
        print("\n[DRY RUN] Would fetch:")
        for code in target_cities:
            print(f"  {code} -> {STATION_MAP.get(code, '?')}")
        for model_short, model_api in target_models.items():
            print(f"  Model: {model_short} ({model_api})")
        print(f"  Total API calls: {len(target_cities)} IEM + {len(target_cities) * len(target_models)} historical forecast")
        return

    if args.actuals_source == "settlement":
        actuals_fetcher = SettlementTemperatureFetcher()
    else:
        actuals_fetcher = IEMFetcher()
    all_city_stats = {}
    global_errors = {m: [] for m in target_models}
    total_forecasts = 0

    for code, info in target_cities.items():
        station = STATION_MAP.get(code)
        if not station:
            print(f"  Skipping {code}: no station mapping")
            continue

        lat, lon = info["lat"], info["lon"]
        print(f"\n--- {code} ({info['name']}) -> {station} ---")

        # Fetch IEM actuals
        print(f"  Fetching {args.actuals_source.upper()} actuals: {start_date} to {end_date}")
        actuals = actuals_fetcher.fetch_daily_highs(station, start_date, end_date, city_code=code)
        print(f"  Got {len(actuals)} actual observations")

        if not actuals:
            print(f"  No actuals available, skipping {code}")
            time.sleep(0.3)
            continue

        city_stats = {}
        for model_short, model_api in target_models.items():
            print(f"  Fetching {model_short} historical forecasts...")
            forecasts = fetch_historical_forecasts(
                lat,
                lon,
                start_date,
                end_date,
                model_api,
                city_code=code,
            )
            print(f"  Got {len(forecasts)} forecast dates for {model_short}")
            total_forecasts += len(forecasts)

            stats = compute_model_stats(forecasts, actuals)
            if stats:
                city_stats[model_short] = stats
                # Accumulate global errors for aggregation
                for date_str, fc_temp in forecasts.items():
                    if date_str in actuals:
                        global_errors[model_short].append(fc_temp - actuals[date_str])
                print(f"    Bias: {stats['bias']:+.2f}F  MAE: {stats['mae']:.2f}F  RMSE: {stats['rmse']:.2f}F  (n={stats['n']})")
            else:
                print(f"    No overlapping dates for {model_short}")

            time.sleep(0.3)  # Rate limit

        if city_stats:
            all_city_stats[code] = city_stats

        time.sleep(0.5)  # Rate limit between cities

    # Compute global stats
    global_stats = {}
    for model_short, errors in global_errors.items():
        if errors:
            n = len(errors)
            bias = sum(errors) / n
            mae = sum(abs(e) for e in errors) / n
            rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
            global_stats[model_short] = {
                "bias": round(bias, 3),
                "rmse": round(rmse, 3),
                "mae": round(mae, 3),
                "n": n,
            }

    diagnostic_weights = compute_optimal_weights(global_stats)

    # Summary
    print(f"\n{'='*60}")
    print(f"GLOBAL RESULTS ({total_forecasts} total forecast-actual pairs)")
    print(f"{'='*60}")
    for model_short in sorted(global_stats.keys(), key=lambda m: global_stats[m]["mae"]):
        s = global_stats[model_short]
        w = diagnostic_weights.get(model_short, 0)
        print(f"  {model_short:12s}  Bias: {s['bias']:+.2f}F  MAE: {s['mae']:.2f}F  RMSE: {s['rmse']:.2f}F  Weight: {w:.1%}  (n={s['n']})")

    # Build output
    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "source": "historical_forecast_api",
        "lead_time_matched": False,
        "period": {"start": start_date, "end": end_date, "days": args.days},
        "actuals_source": args.actuals_source,
        "n_forecasts": total_forecasts,
        "n_cities": len(all_city_stats),
        "per_city": all_city_stats,
        "global": global_stats,
        "diagnostic_inverse_mae_weights": diagnostic_weights,
    }

    if args.merge_bias:
        write_bias_to_calibration(output)

    out_path = None
    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = PROJECT_DIR / out_path
    elif args.save:
        out_path = PROJECT_DIR / "config" / "historical-calibration.json"

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out_path, output)
        print(f"\nSaved to {out_path}")
    else:
        print(f"\n(Use --save or --output PATH to write an audit artifact)")


if __name__ == "__main__":
    main()
