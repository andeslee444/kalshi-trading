#!/usr/bin/env python3
"""City-level audit for weather bias priors, live verification, and trade activity."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from forecast_verifier import ForecastVerifier
from weather_data import BiasCorrector


def load_config():
    return json.loads((PROJECT_DIR / "config" / "kalshi-config.json").read_text())


def resolve_path(path_str):
    path = Path(path_str)
    if path.is_absolute():
        return path
    return PROJECT_DIR / path


def historical_bias_map(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    per_city = data.get("per_city", {})
    result = {}
    for city, models in per_city.items():
        biases = [stats.get("bias") for stats in models.values() if isinstance(stats, dict) and stats.get("bias") is not None]
        if biases:
            result[city] = sum(biases) / len(biases)
    return result


def weather_trade_summary(path, lookback_prefix=None):
    if not path.exists():
        return {}
    trades = json.loads(path.read_text())
    out = defaultdict(lambda: {"trades": 0, "executed": 0, "resting": 0, "maker": 0})
    for trade in trades:
        if trade.get("source_bot") != "weather":
            continue
        city = trade.get("city")
        if not city:
            continue
        if lookback_prefix and not str(trade.get("timestamp", "")).startswith(lookback_prefix):
            continue
        out[city]["trades"] += 1
        if trade.get("status") == "executed":
            out[city]["executed"] += 1
        if trade.get("status") == "resting":
            out[city]["resting"] += 1
        if trade.get("execution_style") == "maker":
            out[city]["maker"] += 1
    return out


def build_rows(lookback_days, historical_path, trades_path):
    config = load_config()
    bias_cfg = config.get("biasCorrection", {})
    verifier = ForecastVerifier(PROJECT_DIR / "data" / "weather-verification.json")
    verifier.load()
    bias_corrector = BiasCorrector(calibration_path=str(historical_path), logger=None)

    hist_map = historical_bias_map(historical_path)
    live_bias = verifier.get_city_bias(
        lookback_days=lookback_days,
        min_samples=int(bias_cfg.get("liveMinSamples", 2)),
        full_weight_n=int(bias_cfg.get("skewFullWeightSamples", 10)),
    )
    trades = weather_trade_summary(trades_path)

    cities = sorted(set(hist_map) | set(live_bias) | set(trades))
    rows = []
    for city in cities:
        hist = hist_map.get(city)
        live = live_bias.get(city, {})
        live_f = live.get("bias_f")
        live_n = live.get("n", 0)
        confidence = live.get("confidence", 0.0)
        blended, _, alpha, meta = bias_corrector.blend_live_bias(
            city,
            live_bias=live_f,
            live_n=live_n,
            ramp_n=int(bias_cfg.get("liveRampSamples", 8)),
            min_live_samples=int(bias_cfg.get("liveMinSamples", 2)),
            max_abs_bias_f=float(bias_cfg.get("historicalMaxAbsF", 6.0)),
            conflict_gap_f=float(bias_cfg.get("conflictGapF", 4.0)),
            conflict_alpha_floor=float(bias_cfg.get("conflictAlphaFloor", 0.35)),
        )
        trade_stats = trades.get(city, {})
        rows.append({
            "city": city,
            "hist_bias_f": round(hist, 2) if hist is not None else None,
            "live_bias_f": round(live_f, 2) if live_f is not None else None,
            "live_n": live_n,
            "live_confidence": confidence,
            "guarded_bias_f": round(blended, 2),
            "blend_alpha": round(alpha, 2),
            "sign_flip": bool(hist and live_f and ((hist > 0 > live_f) or (hist < 0 < live_f))),
            "gap_f": round(abs(hist - live_f), 2) if hist is not None and live_f is not None else None,
            "bias_capped": meta.get("capped", False),
            "bias_conflict": meta.get("conflict", False),
            "trade_count": trade_stats.get("trades", 0),
            "executed_count": trade_stats.get("executed", 0),
            "resting_count": trade_stats.get("resting", 0),
            "maker_count": trade_stats.get("maker", 0),
        })
    return rows


def print_table(rows):
    headers = [
        "city", "hist", "live", "n", "conf", "guarded",
        "alpha", "gap", "flip", "cap", "conflict", "trades", "exec", "maker",
    ]
    print(" ".join(f"{h:>9}" for h in headers))
    for row in rows:
        print(
            f"{row['city']:>9} "
            f"{(row['hist_bias_f'] if row['hist_bias_f'] is not None else '-'):>9} "
            f"{(row['live_bias_f'] if row['live_bias_f'] is not None else '-'):>9} "
            f"{row['live_n']:>9} "
            f"{row['live_confidence']:>9.2f} "
            f"{row['guarded_bias_f']:>9} "
            f"{row['blend_alpha']:>9.2f} "
            f"{(row['gap_f'] if row['gap_f'] is not None else '-'):>9} "
            f"{str(row['sign_flip']):>9} "
            f"{str(row['bias_capped']):>9} "
            f"{str(row['bias_conflict']):>9} "
            f"{row['trade_count']:>9} "
            f"{row['executed_count']:>9} "
            f"{row['maker_count']:>9}"
        )


def main():
    parser = argparse.ArgumentParser(description="Audit weather city bias priors against live verification")
    parser.add_argument("--lookback-days", type=int, default=30, help="Live verification lookback")
    parser.add_argument(
        "--historical-path",
        default="config/historical-calibration.json",
        help="Historical calibration artifact to audit against",
    )
    parser.add_argument(
        "--trades-path",
        default="data/kalshi-trades.json",
        help="Trade log path",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    rows = build_rows(
        args.lookback_days,
        resolve_path(args.historical_path),
        resolve_path(args.trades_path),
    )
    if args.json:
        print(json.dumps({"rows": rows}, indent=2, sort_keys=True))
        return
    print_table(rows)


if __name__ == "__main__":
    main()
