#!/usr/bin/env python3
"""Backfill historical weather data for training the empirical ensemble model.

Fetches historical forecasts from Open-Meteo Previous Runs API and actual
temperatures from the settlement fetcher (NWS climate reports with IEM fallback),
then stores matched pairs in a SQLite training DB.

Usage:
    python3 scripts/backfill-weather-data.py                 # All cities, 90 days
    python3 scripts/backfill-weather-data.py --days 30       # 30 days
    python3 scripts/backfill-weather-data.py --city MIA      # Miami only
    python3 scripts/backfill-weather-data.py --models gfs,ecmwf,icon,gem
    python3 scripts/backfill-weather-data.py --dry-run       # Preview only
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

# Add src/kalshi to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import PROJECT_DIR, retry_request
from weather_data import (
    STATION_MAP,
    SettlementTemperatureFetcher,
    TrainingStore,
    _city_timezone_name,
    open_meteo_model_name,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def get_city_coords():
    """Load city coordinates from kalshi-config.json."""
    config_path = PROJECT_DIR / "config" / "kalshi-config.json"
    config = json.loads(config_path.read_text())
    return config.get("cities", {})


def _parse_previous_runs_daily(payload):
    """Parse current and previous-day forecasts into lead-day buckets."""
    daily = payload.get("daily", {}) if "daily" in payload else payload
    dates = daily.get("time", [])
    current = daily.get("temperature_2m_max", [])
    previous_day1 = daily.get("temperature_2m_max_previous_day1", [])
    if dates and current:
        by_lead = {0: {}, 1: {}}
        for idx, date_str in enumerate(dates):
            cur = current[idx] if idx < len(current) else None
            prev = previous_day1[idx] if idx < len(previous_day1) else None
            if cur is not None:
                by_lead[0][date_str] = cur
            if prev is not None:
                by_lead[1][date_str] = prev
        return {lead_days: values for lead_days, values in by_lead.items() if values}

    hourly = payload.get("hourly", {}) if "hourly" in payload else {}
    times = hourly.get("time", [])
    current = hourly.get("temperature_2m", [])
    previous_day1 = hourly.get("temperature_2m_previous_day1", [])
    if not times:
        return {}

    by_lead = {0: {}, 1: {}}
    for idx, ts in enumerate(times):
        date_str = str(ts).split("T", 1)[0]
        cur = current[idx] if idx < len(current) else None
        prev = previous_day1[idx] if idx < len(previous_day1) else None
        if cur is not None:
            existing = by_lead[0].get(date_str)
            by_lead[0][date_str] = cur if existing is None else max(existing, cur)
        if prev is not None:
            existing = by_lead[1].get(date_str)
            by_lead[1][date_str] = prev if existing is None else max(existing, prev)
    return {lead_days: values for lead_days, values in by_lead.items() if values}


def fetch_previous_runs_forecasts(lat, lon, past_days, model_name, city_code=None, strict=False):
    """Fetch historical deterministic forecasts from Open-Meteo Previous Runs API.

    Returns:
        dict of {lead_days: {date_str: temp_f}} or empty dict on failure.
        When strict=True, raises with model/city/url context instead of
        silently returning an empty dict.
    """
    api_key = os.environ.get("OPEN_METEO_API_KEY", "")
    request_model = open_meteo_model_name(model_name, api_key=api_key)
    timezone_name = quote(_city_timezone_name(city_code=city_code), safe="")
    base = ("https://customer-previous-runs-api.open-meteo.com/v1/forecast"
            if api_key else "https://previous-runs-api.open-meteo.com/v1/forecast")
    url = (
        f"{base}?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,temperature_2m_previous_day1"
        f"&temperature_unit=fahrenheit"
        f"&timezone={timezone_name}&past_days={past_days}"
        f"&models={request_model}"
        + (f"&apikey={api_key}" if api_key else "")
    )

    try:
        resp = retry_request("GET", url, timeout=15, max_retries=2)
        if resp is None or resp.status_code != 200:
            if strict:
                raise RuntimeError(
                    f"previous-runs fetch failed for city={city_code or '?'} "
                    f"model={model_name} status={getattr(resp, 'status_code', 'no_response')} url={url}"
                )
            return {}
        data = resp.json()
        return _parse_previous_runs_daily(data)
    except Exception as e:
        if strict:
            raise RuntimeError(
                f"previous-runs fetch failed for city={city_code or '?'} "
                f"model={model_name} url={url}: {e}"
            ) from e
        print(f"  Warning: Previous runs API error for {model_name}: {e}")
        return {}


def fetch_settled_markets(city_code):
    """Fetch settled KXHIGH markets from Kalshi public API.

    Returns:
        list of settled market dicts.
    """
    url = (
        f"https://api.elections.kalshi.com/trade-api/v2/markets?"
        f"series_ticker=KXHIGH{city_code}&status=settled&limit=200"
    )

    try:
        resp = retry_request("GET", url, timeout=10, max_retries=2)
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("markets", [])
    except Exception:
        return []


MODEL_MAP = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs025",
    "icon": "icon_seamless",
    "gem": "gem_global",
    "graphcast": "gfs_graphcast025",
}


def main():
    parser = argparse.ArgumentParser(description="Backfill weather training data")
    parser.add_argument("--days", type=int, default=90, help="Days of history to fetch (default: 90)")
    parser.add_argument("--city", type=str, default=None, help="Single city code (default: all)")
    parser.add_argument(
        "--models",
        type=str,
        default="gfs,ecmwf",
        help="Comma-separated model list from: gfs, ecmwf, icon, gem, graphcast (default: gfs,ecmwf)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(PROJECT_DIR / "data" / "weather-training.db"),
        help="Output SQLite DB path (default: data/weather-training.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't write to DB")
    args = parser.parse_args()

    cities = get_city_coords()
    target_cities = {args.city: cities[args.city]} if args.city and args.city in cities else cities
    actuals_fetcher = SettlementTemperatureFetcher()
    requested_models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown_models = [m for m in requested_models if m not in MODEL_MAP]
    if unknown_models:
        raise SystemExit(
            f"Unknown models: {', '.join(unknown_models)}. "
            f"Choose from: {', '.join(sorted(MODEL_MAP))}"
        )
    selected_models = {MODEL_MAP[m]: m for m in requested_models}

    if args.dry_run:
        print("[DRY RUN] Would process these cities:")
        for code in target_cities:
            print(f"  {code} -> {STATION_MAP.get(code, '?')}")
        print(f"  Days: {args.days}")
        print(f"  Models: {', '.join(requested_models)}")
        return

    db_path = str((PROJECT_DIR / args.db_path).resolve()) if not Path(args.db_path).is_absolute() else args.db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = TrainingStore(db_path=db_path)
    total_pairs = 0
    today = datetime.date.today()

    print(f"Backfilling {args.days} days of weather data for {len(target_cities)} cities...")

    for code, info in target_cities.items():
        station = STATION_MAP.get(code)
        if not station:
            print(f"  Skipping {code}: no station mapping")
            continue

        lat, lon = info["lat"], info["lon"]
        print(f"\n--- {code} ({info['name']}) -> {station} ---")

        # 1. Fetch historical actuals from the settlement source path
        start_date = (today - datetime.timedelta(days=args.days)).isoformat()
        end_date = (today - datetime.timedelta(days=1)).isoformat()
        print(f"  Fetching settlement actuals: {start_date} to {end_date}")
        actuals = actuals_fetcher.fetch_daily_highs(station, start_date, end_date, city_code=code)
        print(f"  Got {len(actuals)} actual observations")

        # 2. Fetch historical forecasts from Open-Meteo Previous Runs
        all_forecasts = {}  # {(model_name, lead_days): {date: temp}}
        model_errors = []
        for api_model, short_name in selected_models.items():
            print(f"  Fetching {short_name} historical forecasts...")
            try:
                forecasts_by_lead = fetch_previous_runs_forecasts(
                    lat,
                    lon,
                    args.days,
                    api_model,
                    city_code=code,
                    strict=True,
                )
            except Exception as e:
                model_errors.append(str(e))
                print(f"  Error fetching {short_name}: {e}")
                time.sleep(0.3)
                continue
            total_model_dates = sum(len(fc) for fc in forecasts_by_lead.values())
            for lead_days, fc in forecasts_by_lead.items():
                all_forecasts[(short_name, lead_days)] = fc
            print(
                f"  Got {total_model_dates} forecast dates for {short_name} "
                f"across leads {sorted(forecasts_by_lead.keys())}"
            )
            time.sleep(0.3)  # Rate limit

        if not all_forecasts and model_errors:
            raise SystemExit(
                f"All previous-runs forecast fetches failed for {code}: "
                + " | ".join(model_errors)
            )

        # 3. Match forecasts to actuals
        rows = []
        for (model_name, lead_days), fc in all_forecasts.items():
            for date_str, forecast_temp in fc.items():
                actual = actuals.get(date_str)
                if actual is None:
                    continue
                rows.append((
                    code, date_str, model_name, lead_days, 0,
                    forecast_temp, actual, None,
                ))

        if rows:
            store.insert_batch(rows)
            total_pairs += len(rows)
            print(f"  Inserted {len(rows)} training pairs")

        # 4. Fetch settled market outcomes (optional enrichment)
        settled = fetch_settled_markets(code)
        if settled:
            print(f"  Found {len(settled)} settled markets (outcome enrichment)")

        # Rate limit between cities
        time.sleep(0.5)

    print(f"\nBackfill complete: {total_pairs} total pairs in {db_path}")
    print(f"Total rows in DB: {store.count()}")
    store.close()
    if total_pairs <= 0:
        raise SystemExit("Backfill produced zero training pairs")


if __name__ == "__main__":
    main()
