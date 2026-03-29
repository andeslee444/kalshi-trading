#!/usr/bin/env python3
"""Build a read-only weather observation pack from local artifacts.

The pack is Phase-4 safe: it reads existing local files only, with an
optional --save for a derived summary artifact.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CONFIG_DIR = PROJECT_DIR / "config"
DEFAULT_OUTPUT_PATH = DATA_DIR / "weather-observation-pack.json"
DEFAULT_FINANCIAL_SNAPSHOT_PATH = DATA_DIR / "financial-snapshot.json"
DEFAULT_BACKTEST_RESULTS_PATH = DATA_DIR / "backtest-results.json"
DEFAULT_CALIBRATION_PATH = CONFIG_DIR / "calibration.json"
DEFAULT_WEATHER_VERIFICATION_PATH = DATA_DIR / "weather-verification.json"
DEFAULT_KALSHI_TRADES_PATH = DATA_DIR / "kalshi-trades.json"
DEFAULT_WEATHER_BIAS_PATH = CONFIG_DIR / "weather-live-bias.json"

KNOWN_CITY_ALIASES = {
    "AUS": "AUS",
    "Austin": "AUS",
    "ATL": "ATL",
    "BOS": "BOS",
    "CHI": "CHI",
    "DAL": "DAL",
    "Denver": "DEN",
    "DEN": "DEN",
    "DC": "DC",
    "Houston": "HOU",
    "HOU": "HOU",
    "LAX": "LAX",
    "LV": "LV",
    "MIA": "MIA",
    "MIN": "MIN",
    "NOLA": "NOLA",
    "NY": "NY",
    "OKC": "OKC",
    "PHIL": "PHIL",
    "PHX": "PHX",
    "SATX": "SATX",
    "SEA": "SEA",
    "SFO": "SFO",
    "Washington DC": "DC",
    "Los Angeles": "LAX",
    "Miami": "MIA",
    "New York": "NY",
    "Philadelphia": "PHIL",
    "Chicago": "CHI",
    "Boston": "BOS",
    "Seattle": "SEA",
    "San Francisco": "SFO",
    "Phoenix": "PHX",
    "Minneapolis": "MIN",
    "New Orleans": "NOLA",
    "Oklahoma City": "OKC",
    "San Antonio": "SATX",
    "Las Vegas": "LV",
}


def _load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _resolve_path(path, default_path):
    if path is None:
        return Path(default_path)
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_DIR / resolved
    return resolved


def _safe_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _parse_iso_datetime(value):
    if not value or not isinstance(value, str):
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_summary(timestamp_value, now=None):
    ts = _parse_iso_datetime(timestamp_value)
    if ts is None:
        return {
            "timestamp": timestamp_value,
            "age_seconds": None,
            "age_minutes": None,
            "age_hours": None,
            "age_days": None,
        }
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    age_seconds = max(0.0, delta.total_seconds())
    return {
        "timestamp": ts.isoformat(),
        "age_seconds": round(age_seconds, 1),
        "age_minutes": round(age_seconds / 60.0, 2),
        "age_hours": round(age_seconds / 3600.0, 2),
        "age_days": round(age_seconds / 86400.0, 2),
    }


def _local_today(now=None):
    if now is None:
        return date.today()
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            return now.astimezone().date()
        return now.date()
    return date.today()


def _normalize_city(value):
    if not value:
        return None
    if value in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[value]
    upper = str(value).upper()
    if upper in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[upper]
    return str(value)


def _city_from_ticker(ticker):
    if not ticker or not isinstance(ticker, str):
        return None
    if not ticker.startswith("KXHIGH"):
        return None
    suffix = ticker[len("KXHIGH"):].split("-", 1)[0]
    if suffix in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[suffix]
    if suffix.startswith("T"):
        candidate = suffix[1:]
        if candidate in KNOWN_CITY_ALIASES:
            return KNOWN_CITY_ALIASES[candidate]
    match = re.match(r"^T?([A-Z]+)$", suffix)
    if match:
        return _normalize_city(match.group(1))
    return _normalize_city(suffix)


def _trade_city(trade):
    return _normalize_city(trade.get("city")) or _city_from_ticker(trade.get("ticker"))


def _trade_cost_cents(trade):
    cost = trade.get("cost_cents")
    if cost is not None:
        return _safe_int(cost)
    price = trade.get("price_cents")
    if price is None:
        price = trade.get("price")
    return _safe_int(price) * _safe_int(trade.get("count", 0))


def compute_weather_pnl_from_trades(trades):
    """Compute city-level realized P&L from local weather trade logs."""
    by_city = defaultdict(lambda: {
        "pnl_cents": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "settled": 0,
        "fees_cents": 0,
    })
    overall = {
        "pnl_cents": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "settled": 0,
        "fees_cents": 0,
    }

    for trade in trades:
        if trade.get("source_bot") not in (None, "weather") and not str(trade.get("ticker", "")).startswith("KXHIGH"):
            continue
        if not str(trade.get("ticker", "")).startswith("KXHIGH"):
            continue
        result = trade.get("settlement_result")
        if result not in ("won", "lost"):
            continue
        city = _trade_city(trade)
        if not city:
            continue

        count = _safe_int(trade.get("count", 0), default=0)
        cost_cents = _trade_cost_cents(trade)
        fee_cents = _safe_int(trade.get("fee_cents", 0), default=0)
        if result == "won":
            pnl = 100 * count - cost_cents
        else:
            pnl = -cost_cents

        row = by_city[city]
        row["pnl_cents"] += pnl
        row["trades"] += 1
        row["settled"] += 1
        row["fees_cents"] += fee_cents
        if result == "won":
            row["wins"] += 1
        else:
            row["losses"] += 1

        overall["pnl_cents"] += pnl
        overall["trades"] += 1
        overall["settled"] += 1
        overall["fees_cents"] += fee_cents
        if result == "won":
            overall["wins"] += 1
        else:
            overall["losses"] += 1

    rows = []
    for city, stats in by_city.items():
        rows.append({
            "city": city,
            **stats,
            "win_rate": round(stats["wins"] / stats["settled"], 3) if stats["settled"] else None,
            "net_after_fees_cents": stats["pnl_cents"] - stats["fees_cents"],
        })
    rows.sort(key=lambda row: (-row["pnl_cents"], row["city"]))
    overall["win_rate"] = round(overall["wins"] / overall["settled"], 3) if overall["settled"] else None
    overall["net_after_fees_cents"] = overall["pnl_cents"] - overall["fees_cents"]
    return {"source": "local_trade_log_settlements", "overall": overall, "by_city": rows}


def compute_verification_source_mix(verified_rows, lookback_days, now=None):
    cutoff = _local_today(now=now) - date.resolution * lookback_days
    counts = Counter()
    total = 0
    for row in verified_rows:
        row_date = row.get("date")
        if not row_date:
            continue
        try:
            parsed = date.fromisoformat(row_date)
        except ValueError:
            continue
        if parsed < cutoff:
            continue
        counts[row.get("actual_source") or "missing"] += 1
        total += 1
    shares = {source: round(count / total, 3) for source, count in sorted(counts.items())} if total else {}
    return {
        "lookback_days": lookback_days,
        "total": total,
        "counts": dict(sorted(counts.items())),
        "shares": shares,
    }


def compute_weather_execution_quality(trades):
    city_rows = defaultdict(lambda: {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    })
    overall = {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    }

    for trade in trades:
        if trade.get("source_bot") != "weather":
            continue
        city = trade.get("city") or "UNKNOWN"
        row = city_rows[city]
        status = trade.get("status")
        style = trade.get("execution_style") or "legacy"
        edge = float(trade.get("edge") or 0.0)
        bias_applied = trade.get("bias_applied_f")
        bias_conflict = bool(trade.get("bias_conflict"))
        bias_capped = bool(trade.get("bias_capped"))

        for target in (row, overall):
            target["trades"] += 1
            target["total_edge"] += edge
            if status == "executed":
                target["executed"] += 1
            if status == "resting":
                target["resting"] += 1
            if style == "maker":
                target["maker"] += 1
                if status == "executed":
                    target["maker_executed"] += 1
            elif status == "executed":
                target["taker_executed"] += 1
            if isinstance(bias_applied, (int, float)):
                target["bias_records"] += 1
                target["total_abs_bias"] += abs(float(bias_applied))
                if bias_conflict:
                    target["bias_conflict"] += 1
                if bias_capped:
                    target["bias_capped"] += 1

    def finalize(stats):
        trades_n = max(1, stats["trades"])
        maker_n = max(1, stats["maker"])
        executed_n = max(1, stats["executed"])
        bias_records = max(1, stats["bias_records"])
        return {
            **stats,
            "avg_edge": round(stats["total_edge"] / trades_n, 3),
            "maker_share": round(stats["maker"] / trades_n, 3),
            "fill_rate": round(stats["executed"] / trades_n, 3),
            "maker_fill_rate": round(stats["maker_executed"] / maker_n, 3),
            "maker_exec_share": round(stats["maker_executed"] / executed_n, 3) if stats["executed"] else 0.0,
            "avg_abs_bias_applied": round(stats["total_abs_bias"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_conflict_share": round(stats["bias_conflict"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_cap_share": round(stats["bias_capped"] / bias_records, 3) if stats["bias_records"] else None,
        }

    return {
        "overall": finalize(overall),
        "per_city": {city: finalize(stats) for city, stats in sorted(city_rows.items())},
    }


def compute_historical_city_bias(historical_data):
    per_city = historical_data.get("per_city", {}) if isinstance(historical_data, dict) else {}
    out = {}
    for city, models in per_city.items():
        biases = []
        if isinstance(models, dict):
            for stats in models.values():
                if isinstance(stats, dict) and stats.get("bias") is not None:
                    biases.append(float(stats["bias"]))
        if biases:
            out[city] = {
                "bias_f": round(sum(biases) / len(biases), 2),
                "n_models": len(biases),
            }
    return out


def compute_live_city_bias(verified_rows, lookback_days, now=None, min_samples=2):
    cutoff = _local_today(now=now) - date.resolution * lookback_days
    city_errors = defaultdict(list)
    for row in verified_rows:
        if row.get("record_kind", "snapshot") != "snapshot":
            continue
        city = _normalize_city(row.get("city"))
        if not city:
            continue
        row_date = row.get("date")
        if not row_date:
            continue
        try:
            parsed = date.fromisoformat(row_date)
        except ValueError:
            continue
        if parsed < cutoff:
            continue
        errors = row.get("errors", {})
        if not isinstance(errors, dict) or not errors:
            continue
        vals = [v for v in errors.values() if isinstance(v, (int, float))]
        if not vals:
            continue
        city_errors[city].append(sum(vals) / len(vals))

    summary = {}
    for city, values in city_errors.items():
        if len(values) < min_samples:
            continue
        bias_f = sum(values) / len(values)
        summary[city] = {
            "bias_f": round(bias_f, 2),
            "n": len(values),
        }
    return summary


def build_city_bias_conflicts(historical_biases, live_biases, city_pnl, min_gap_f=4.0):
    rows = []
    for city in sorted(set(historical_biases) | set(live_biases)):
        hist = historical_biases.get(city, {})
        live = live_biases.get(city, {})
        hist_bias = hist.get("bias_f")
        live_bias = live.get("bias_f")
        if hist_bias is None or live_bias is None:
            continue
        gap = abs(hist_bias - live_bias)
        sign_flip = (hist_bias > 0 > live_bias) or (hist_bias < 0 < live_bias)
        conflict = sign_flip or gap >= min_gap_f
        pnl_row = city_pnl.get(city, {})
        rows.append({
            "city": city,
            "historical_bias_f": hist_bias,
            "live_bias_f": live_bias,
            "gap_f": round(gap, 2),
            "sign_flip": sign_flip,
            "bias_conflict": conflict,
            "historical_models": hist.get("n_models"),
            "live_samples": live.get("n"),
            "weather_pnl_cents": pnl_row.get("pnl_cents", 0),
            "weather_trades": pnl_row.get("trades", 0),
        })

    rows.sort(key=lambda row: (
        not row["bias_conflict"],
        not row["sign_flip"],
        -row["gap_f"],
        row["city"],
    ))
    return rows


def _freshness_from(path, payload, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return {
            "path": str(path),
            "present": False,
        }
    timestamp = payload.get("generated_at") or payload.get("timestamp") or payload.get("sigma_updated_at")
    age = _age_summary(timestamp, now=now)
    return {
        "path": str(path),
        "present": True,
        **age,
    }


def build_observation_pack(
    lookback_days=30,
    source_lookback_days=(7, 30),
    top_cities=8,
    min_conflict_gap_f=4.0,
    now=None,
    financial_snapshot_path=None,
    backtest_results_path=None,
    calibration_path=None,
    weather_verification_path=None,
    weather_trades_path=None,
    weather_bias_path=None,
):
    now = now or datetime.now(timezone.utc)
    financial_snapshot_path = _resolve_path(financial_snapshot_path, DEFAULT_FINANCIAL_SNAPSHOT_PATH)
    backtest_results_path = _resolve_path(backtest_results_path, DEFAULT_BACKTEST_RESULTS_PATH)
    calibration_path = _resolve_path(calibration_path, DEFAULT_CALIBRATION_PATH)
    weather_verification_path = _resolve_path(weather_verification_path, DEFAULT_WEATHER_VERIFICATION_PATH)
    weather_trades_path = _resolve_path(weather_trades_path, DEFAULT_KALSHI_TRADES_PATH)
    weather_bias_path = _resolve_path(weather_bias_path, DEFAULT_WEATHER_BIAS_PATH)

    financial_snapshot = _load_json(financial_snapshot_path, default={})
    backtest_results = _load_json(backtest_results_path, default={})
    calibration = _load_json(calibration_path, default={})
    verification = _load_json(weather_verification_path, default={})
    trades = _load_json(weather_trades_path, default=[])
    prior_bias = _load_json(weather_bias_path, default={})

    realized_weather = None
    if isinstance(financial_snapshot, dict):
        realized_pnl = financial_snapshot.get("realized_pnl", {})
        by_bot = realized_pnl.get("by_bot", {}) if isinstance(realized_pnl, dict) else {}
        realized_weather = by_bot.get("weather")

    verification_rows = verification.get("verified", []) if isinstance(verification, dict) else []
    verification_source_mix = [
        compute_verification_source_mix(verification_rows, days, now=now)
        for days in source_lookback_days
    ]
    execution_quality = compute_weather_execution_quality(trades)

    pack = {
        "artifact_type": "weather_observation_pack",
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "sources": {
            "financial_snapshot": str(financial_snapshot_path),
            "backtest_results": str(backtest_results_path),
            "calibration": str(calibration_path),
            "weather_verification": str(weather_verification_path),
            "weather_bias": str(weather_bias_path),
            "kalshi_trades": str(weather_trades_path),
            "weather_execution_trades": str(weather_trades_path),
        },
        "weather_pnl": {
            "realized": realized_weather,
            "financial_snapshot": _freshness_from(financial_snapshot_path, financial_snapshot, now=now),
        },
        "freshness": {
            "backtest_results": _freshness_from(backtest_results_path, backtest_results, now=now),
            "calibration": _freshness_from(calibration_path, calibration, now=now),
            "calibration_sigma_updated_at": _age_summary(calibration.get("sigma_updated_at"), now=now) if isinstance(calibration, dict) else None,
            "weather_bias": _freshness_from(weather_bias_path, prior_bias, now=now),
        },
        "verification_source_mix": {
            "state_path": str(weather_verification_path),
            "summaries": verification_source_mix,
        },
        "city_pnl": compute_weather_pnl_from_trades(trades),
        "execution_quality": execution_quality,
        "city_bias": {
            "historical": compute_historical_city_bias(prior_bias),
            "live": compute_live_city_bias(verification_rows, lookback_days, now=now),
        },
    }
    all_conflicts = build_city_bias_conflicts(
        pack["city_bias"]["historical"],
        pack["city_bias"]["live"],
        {row["city"]: row for row in pack["city_pnl"]["by_city"]},
        min_gap_f=min_conflict_gap_f,
    )
    pack["city_bias"]["conflicts"] = all_conflicts
    pack["city_bias"]["top_conflicts"] = all_conflicts[:top_cities]
    return pack


def _print_human(pack):
    realized = pack.get("weather_pnl", {}).get("realized") or {}
    if realized:
        print(
            f"Weather realized P&L: {realized.get('pnl_cents', 0)} cents "
            f"({realized.get('wins', 0)}W/{realized.get('losses', 0)}L, "
            f"win_rate={realized.get('win_rate')})"
        )
    freshness = pack.get("freshness", {})
    backtest = freshness.get("backtest_results", {})
    calibration = freshness.get("calibration", {})
    sigma_age = freshness.get("calibration_sigma_updated_at", {})
    print(
        f"Backtest freshness: {backtest.get('timestamp')} age_days={backtest.get('age_days')}"
    )
    print(
        f"Calibration freshness: {calibration.get('timestamp')} age_days={calibration.get('age_days')} "
        f"sigma_updated_at={sigma_age.get('timestamp')} sigma_age_days={sigma_age.get('age_days')}"
    )

    print("Verification source mix:")
    for summary in pack.get("verification_source_mix", {}).get("summaries", []):
        parts = ", ".join(
            f"{source}={count} ({summary['shares'].get(source, 0):.1%})"
            for source, count in summary.get("counts", {}).items()
        )
        print(f"  {summary.get('lookback_days')}d: total={summary.get('total', 0)} | {parts}")

    exec_quality = pack.get("execution_quality", {}).get("overall", {})
    if exec_quality:
        print(
            "Execution quality: "
            f"trades={exec_quality.get('trades', 0)} "
            f"executed={exec_quality.get('executed', 0)} "
            f"fill_rate={exec_quality.get('fill_rate')} "
            f"maker_share={exec_quality.get('maker_share')}"
        )

    print("City bias conflicts:")
    for row in pack.get("city_bias", {}).get("top_conflicts", pack.get("city_bias", {}).get("conflicts", [])[:8]):
        print(
            f"  {row['city']}: hist={row['historical_bias_f']:.2f} live={row['live_bias_f']:.2f} "
            f"gap={row['gap_f']:.2f} sign_flip={row['sign_flip']} "
            f"conflict={row['bias_conflict']} pnl={row['weather_pnl_cents']}"
        )

    city_pnl_rows = pack.get("city_pnl", {}).get("by_city", [])
    print("City P&L:")
    for row in city_pnl_rows[:8]:
        print(
            f"  {row['city']}: pnl={row['pnl_cents']} trades={row['trades']} "
            f"win_rate={row.get('win_rate')}"
        )


def main():
    parser = argparse.ArgumentParser(description="Build a read-only weather observation pack")
    parser.add_argument("--lookback-days", type=int, default=30, help="Live-bias lookback window")
    parser.add_argument(
        "--source-lookback-days",
        nargs="*",
        type=int,
        default=[7, 30],
        help="Verification source-mix windows",
    )
    parser.add_argument("--top-cities", type=int, default=8, help="Number of conflict rows to include")
    parser.add_argument(
        "--min-conflict-gap-f",
        type=float,
        default=4.0,
        help="Historical-vs-live bias gap needed to flag a conflict",
    )
    parser.add_argument(
        "--financial-snapshot-path",
        default=str(DEFAULT_FINANCIAL_SNAPSHOT_PATH),
        help="Path to financial-snapshot.json",
    )
    parser.add_argument(
        "--backtest-results-path",
        default=str(DEFAULT_BACKTEST_RESULTS_PATH),
        help="Path to backtest-results.json",
    )
    parser.add_argument(
        "--calibration-path",
        default=str(DEFAULT_CALIBRATION_PATH),
        help="Path to calibration.json",
    )
    parser.add_argument(
        "--weather-verification-path",
        default=str(DEFAULT_WEATHER_VERIFICATION_PATH),
        help="Path to weather-verification.json",
    )
    parser.add_argument(
        "--weather-trades-path",
        default=str(DEFAULT_KALSHI_TRADES_PATH),
        help="Path to the canonical weather trade log",
    )
    parser.add_argument(
        "--weather-bias-path",
        default=str(DEFAULT_WEATHER_BIAS_PATH),
        help="Path to weather-live-bias.json",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--save", action="store_true", help="Save a derived artifact to data/weather-observation-pack.json")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output path for a saved derived artifact",
    )
    args = parser.parse_args()

    pack = build_observation_pack(
        lookback_days=args.lookback_days,
        source_lookback_days=args.source_lookback_days,
        top_cities=args.top_cities,
        min_conflict_gap_f=args.min_conflict_gap_f,
        financial_snapshot_path=args.financial_snapshot_path,
        backtest_results_path=args.backtest_results_path,
        calibration_path=args.calibration_path,
        weather_verification_path=args.weather_verification_path,
        weather_trades_path=args.weather_trades_path,
        weather_bias_path=args.weather_bias_path,
    )

    if args.save:
        output_path = _resolve_path(args.output, DEFAULT_OUTPUT_PATH)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(pack, indent=2, sort_keys=True) + "\n")
        print(f"Saved derived observation pack to {output_path}", file=sys.stderr)

    if args.json:
        print(json.dumps(pack, indent=2, sort_keys=True))
        return

    _print_human(pack)


if __name__ == "__main__":
    main()
