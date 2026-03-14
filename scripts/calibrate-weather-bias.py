#!/usr/bin/env python3
"""Build a lead-time-matched weather bias artifact from training data.

The input DB is populated by scripts/backfill-weather-data.py using the
Open-Meteo Previous Runs API plus settlement-aligned actuals. Unlike the
historical forecast archive, this source is safe to use for live weather
bias correction because lead time is explicit.
"""

import argparse
import datetime
import json
import math
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from kalshi_auth import atomic_write_json
from weather_data import TrainingStore


def _parse_csv(value):
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _compute_stats(errors):
    if not errors:
        return None
    n = len(errors)
    bias = sum(errors) / n
    mae = sum(abs(err) for err in errors) / n
    rmse = math.sqrt(sum(err * err for err in errors) / n)
    return {
        "bias": round(bias, 3),
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "n": n,
    }


def build_bias_artifact(
    rows,
    min_samples=5,
    min_lead_days=0,
    max_lead_days=1,
    source_db=None,
):
    """Aggregate training rows into a live-safe bias artifact."""
    per_city_errors = {}
    global_errors = {}
    dates = set()

    for row in rows:
        actual = row.get("actual_temp_cli")
        forecast = row.get("forecast_temp")
        if actual is None or forecast is None:
            continue
        lead_days = row.get("lead_days")
        if min_lead_days is not None and lead_days is not None and lead_days < min_lead_days:
            continue
        if max_lead_days is not None and lead_days is not None and lead_days > max_lead_days:
            continue

        city = row.get("city")
        model = row.get("model")
        if not city or not model:
            continue

        error = float(forecast) - float(actual)
        per_city_errors.setdefault(city, {}).setdefault(model, []).append(error)
        global_errors.setdefault(model, []).append(error)
        if row.get("date"):
            dates.add(row["date"])

    per_city = {}
    n_pairs = 0
    for city, model_map in sorted(per_city_errors.items()):
        city_stats = {}
        for model, errors in sorted(model_map.items()):
            if len(errors) < min_samples:
                continue
            stats = _compute_stats(errors)
            city_stats[model] = stats
            n_pairs += stats["n"]
        if city_stats:
            per_city[city] = city_stats

    global_stats = {}
    for model, errors in sorted(global_errors.items()):
        if len(errors) < min_samples:
            continue
        global_stats[model] = _compute_stats(errors)

    date_list = sorted(dates)
    period = {
        "start": date_list[0] if date_list else None,
        "end": date_list[-1] if date_list else None,
        "days": len(date_list),
    }
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "previous_runs_training",
        "lead_time_matched": True,
        "actuals_source": "settlement",
        "lead_days": {"min": min_lead_days, "max": max_lead_days},
        "period": period,
        "source_db": str(source_db) if source_db else None,
        "n_forecasts": n_pairs,
        "n_cities": len(per_city),
        "per_city": per_city,
        "global": global_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="Build weather bias artifact from training DB")
    parser.add_argument(
        "--db-path",
        default=str(PROJECT_DIR / "data" / "weather-training.db"),
        help="Path to weather-training.db",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_DIR / "config" / "weather-live-bias.json"),
        help="Output path for the bias artifact",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum forecast/actual pairs required per model (default: 5)",
    )
    parser.add_argument(
        "--min-lead-days",
        type=int,
        default=0,
        help="Minimum lead_days to include (default: 0)",
    )
    parser.add_argument(
        "--max-lead-days",
        type=int,
        default=1,
        help="Maximum lead_days to include (default: 1)",
    )
    parser.add_argument(
        "--cities",
        default=None,
        help="Optional comma-separated city filter",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Optional comma-separated model filter",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the artifact JSON to stdout",
    )
    args = parser.parse_args()

    store = TrainingStore(db_path=args.db_path)
    try:
        rows = store.get_pairs(
            min_lead_days=args.min_lead_days,
            max_lead_days=args.max_lead_days,
        )
    finally:
        store.close()

    cities = set(_parse_csv(args.cities) or [])
    models = set(_parse_csv(args.models) or [])
    if cities:
        rows = [row for row in rows if row.get("city") in cities]
    if models:
        rows = [row for row in rows if row.get("model") in models]

    artifact = build_bias_artifact(
        rows,
        min_samples=args.min_samples,
        min_lead_days=args.min_lead_days,
        max_lead_days=args.max_lead_days,
        source_db=args.db_path,
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_DIR / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, artifact)

    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
        return

    print(f"Saved {artifact['n_forecasts']} matched forecast pairs to {output_path}")
    print(f"Cities: {artifact['n_cities']} | Global models: {len(artifact['global'])}")
    print(
        "Lead days: "
        f"{artifact['lead_days']['min']}..{artifact['lead_days']['max']} | "
        f"Period: {artifact['period']['start']} -> {artifact['period']['end']}"
    )


if __name__ == "__main__":
    main()
