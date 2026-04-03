#!/usr/bin/env python3
"""Research-only audit of same-day Open-Meteo HRRR 15-minute weather features."""

import argparse
import datetime
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from kalshi_auth import atomic_write_json
from weather_data import IntradayFeatureFetcher


def _load_cities():
    config_path = PROJECT_DIR / "config" / "kalshi-config.json"
    config = json.loads(config_path.read_text())
    return config.get("cities", {})


def _risk_flags(summary):
    flags = []
    cape = summary.get("cape_max_jkg")
    precip = summary.get("precipitation_total")
    cloud_mean = summary.get("cloud_cover_mean_pct")
    temp_range = None
    if isinstance(summary.get("temperature_max_f"), (int, float)) and isinstance(summary.get("temperature_min_f"), (int, float)):
        temp_range = float(summary["temperature_max_f"]) - float(summary["temperature_min_f"])

    if isinstance(cape, (int, float)) and cape >= 1000 and isinstance(precip, (int, float)) and precip >= 0.05:
        flags.append("convective_risk")
    if isinstance(cloud_mean, (int, float)) and cloud_mean >= 70:
        flags.append("cloud_cap_risk")
    if temp_range is not None and temp_range <= 3.0 and isinstance(cloud_mean, (int, float)) and cloud_mean >= 60:
        flags.append("flat_diurnal_range")
    return flags


def build_intraday_feature_audit(*, city=None, target_date=None, fetcher=None):
    cities = _load_cities()
    if city:
        cities = {city: cities[city]}

    fetcher = fetcher or IntradayFeatureFetcher()
    per_city = {}
    risk_buckets = {
        "convective_risk": [],
        "cloud_cap_risk": [],
        "flat_diurnal_range": [],
    }

    for code, info in cities.items():
        summary = fetcher.fetch_same_day_summary(
            info["lat"],
            info["lon"],
            city_code=code,
            target_date=target_date,
        )
        if summary is None:
            continue
        flags = _risk_flags(summary)
        for flag in flags:
            risk_buckets.setdefault(flag, []).append(code)
        per_city[code] = {
            "city_name": info.get("name"),
            **summary,
            "risk_flags": flags,
        }

    return {
        "artifact_type": "weather_intraday_feature_audit",
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target_date": target_date,
        "source": "open_meteo_hrrr_minutely_15",
        "n_cities": len(per_city),
        "per_city": per_city,
        "summary": {
            "convective_risk_cities": risk_buckets.get("convective_risk", []),
            "cloud_cap_risk_cities": risk_buckets.get("cloud_cap_risk", []),
            "flat_diurnal_range_cities": risk_buckets.get("flat_diurnal_range", []),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit same-day Open-Meteo HRRR 15-minute weather features")
    parser.add_argument("--city", default=None, help="Optional single city code")
    parser.add_argument("--target-date", default=None, help="Optional local YYYY-MM-DD target date")
    parser.add_argument("--save", action="store_true", help="Save the audit artifact to disk")
    parser.add_argument(
        "--output",
        default=str(PROJECT_DIR / "data" / "weather-intraday-feature-audit.json"),
        help="Output path when --save is used",
    )
    parser.add_argument("--json", action="store_true", help="Print the audit JSON to stdout")
    args = parser.parse_args(argv)

    audit = build_intraday_feature_audit(city=args.city, target_date=args.target_date)

    if args.save:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = PROJECT_DIR / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_path, audit)

    if args.json or not args.save:
        print(json.dumps(audit, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
