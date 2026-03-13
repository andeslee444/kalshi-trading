#!/usr/bin/env python3
"""Backfill historical weather data for training the empirical ensemble model.

Fetches historical forecasts from Open-Meteo Previous Runs API and actual
temperatures from the settlement fetcher (NWS climate reports with IEM fallback),
then stores matched pairs in a SQLite training DB.

Usage:
    python3 scripts/backfill-weather-data.py                 # All cities, 90 days
    python3 scripts/backfill-weather-data.py --days 30       # 30 days
    python3 scripts/backfill-weather-data.py --city MIA      # Miami only
    python3 scripts/backfill-weather-data.py --dry-run       # Preview only
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

# Add src/kalshi to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import PROJECT_DIR, retry_request
from weather_data import STATION_MAP, SettlementTemperatureFetcher, TrainingStore


def get_city_coords():
    """Load city coordinates from kalshi-config.json."""
    config_path = PROJECT_DIR / "config" / "kalshi-config.json"
    config = json.loads(config_path.read_text())
    return config.get("cities", {})


def fetch_historical_forecasts(lat, lon, past_days, model_name):
    """Fetch historical deterministic forecasts from Open-Meteo Previous Runs API.

    Returns:
        dict of {date_str: temp_f} or empty dict on failure.
    """
    url = (
        f"https://previous-runs-api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&timezone=America%2FNew_York&past_days={past_days}"
        f"&models={model_name}"
    )

    try:
        resp = retry_request("GET", url, timeout=15, max_retries=2)
        if resp is None or resp.status_code != 200:
            return {}
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        return {d: t for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
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


def main():
    parser = argparse.ArgumentParser(description="Backfill weather training data")
    parser.add_argument("--days", type=int, default=90, help="Days of history to fetch (default: 90)")
    parser.add_argument("--city", type=str, default=None, help="Single city code (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't write to DB")
    args = parser.parse_args()

    cities = get_city_coords()
    target_cities = {args.city: cities[args.city]} if args.city and args.city in cities else cities
    actuals_fetcher = SettlementTemperatureFetcher()

    if args.dry_run:
        print("[DRY RUN] Would process these cities:")
        for code in target_cities:
            print(f"  {code} -> {STATION_MAP.get(code, '?')}")
        print(f"  Days: {args.days}")
        return

    db_path = str(PROJECT_DIR / "data" / "weather-training.db")
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
        print(f"  Fetching IEM actuals: {start_date} to {end_date}")
        actuals = actuals_fetcher.fetch_daily_highs(station, start_date, end_date, city_code=code)
        print(f"  Got {len(actuals)} actual observations")

        # 2. Fetch historical forecasts from Open-Meteo Previous Runs
        models = {"gfs_seamless": "gfs", "ecmwf_ifs025": "ecmwf"}
        all_forecasts = {}  # {model_name: {date: temp}}
        for api_model, short_name in models.items():
            print(f"  Fetching {short_name} historical forecasts...")
            fc = fetch_historical_forecasts(lat, lon, args.days, api_model)
            all_forecasts[short_name] = fc
            print(f"  Got {len(fc)} forecast dates for {short_name}")
            time.sleep(0.3)  # Rate limit

        # 3. Match forecasts to actuals
        rows = []
        for model_name, fc in all_forecasts.items():
            for date_str, forecast_temp in fc.items():
                actual = actuals.get(date_str)
                rows.append((
                    code, date_str, model_name, 0, 0,
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


if __name__ == "__main__":
    main()
