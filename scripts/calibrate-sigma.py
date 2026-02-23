#!/usr/bin/env python3
"""Calibrate sigma parameters for probability models using historical trade settlements.

Reads trade logs from all bots, matches them against Kalshi settlement data,
and grid-searches for optimal sigma values that minimize Brier score.

Usage:
    python3 scripts/calibrate-sigma.py              # Display calibration report
    python3 scripts/calibrate-sigma.py --save       # Save to config/calibration.json
    python3 scripts/calibrate-sigma.py --json       # JSON output
    python3 scripts/calibrate-sigma.py --no-api     # Local-only (skip settlement fetch)
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── Path setup ───
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
PROJECT_DIR = Path(__file__).resolve().parent.parent

from probability import _norm_cdf, _student_t_cdf


# ─── Trade file definitions (mirrors analyze-performance.py) ───
TRADE_FILES = [
    {"label": "Weather Bot", "path": PROJECT_DIR / "data" / "kalshi-trades.json"},
    {"label": "Strategy Trader", "path": PROJECT_DIR / "data" / "kalshi-strategy-trades.json"},
    {"label": "Entertainment Bot", "path": PROJECT_DIR / "data" / "kalshi-entertainment-trades.json"},
    {"label": "BeatRelease Scanner", "path": PROJECT_DIR / "data" / "beatrelease-trades.json"},
]

CALIBRATION_PATH = PROJECT_DIR / "config" / "calibration.json"

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


# ─── Helpers ───

def load_trades_safe(filepath):
    """Load a JSON trade file. Returns list on success, empty list on failure."""
    if not filepath.exists():
        return []
    try:
        text = filepath.read_text().strip()
        if not text:
            return []
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError, OSError):
        return []


def fetch_settlements(client):
    """Paginate /portfolio/settlements and return all settlement records."""
    all_settlements = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("settlements", [])
        all_settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return all_settlements


def parse_weather_ticker(ticker):
    """Parse KXHIGHMIA-26FEB16-T86 -> {city, date, direction, threshold}."""
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        return None
    city = m.group(1)
    yr, mon, day = int(m.group(2)), m.group(3), int(m.group(4))
    month = MONTHS.get(mon)
    if not month:
        return None
    return {
        "city": city,
        "date": f"{2000 + yr}-{month:02d}-{day:02d}",
        "direction": m.group(5),
        "threshold": float(m.group(6)),
    }


def weather_prob_with_sigma(forecast_temp, threshold, direction, sigma, df=6):
    """Compute weather probability with a specific sigma and df (for grid search)."""
    if direction == "T":
        z = (threshold - forecast_temp) / sigma
        return 1.0 - _student_t_cdf(z, df)
    else:
        z_low = (threshold - forecast_temp) / sigma
        z_high = (threshold + 1 - forecast_temp) / sigma
        return _student_t_cdf(z_high, df) - _student_t_cdf(z_low, df)


def brier_score(predictions):
    """Compute Brier score from list of (predicted_prob, actual_outcome) tuples.

    Returns None if empty. Lower is better (0=perfect, 0.25=random, 1=worst).
    """
    if not predictions:
        return None
    return sum((p - a) ** 2 for p, a in predictions) / len(predictions)


def log_loss(predictions, eps=1e-7):
    """Cross-entropy loss. Better objective for Kelly-based trading.

    Returns None if empty. Lower is better.
    """
    if not predictions:
        return None
    return -sum(a * math.log(max(p, eps)) + (1 - a) * math.log(max(1 - p, eps))
                for p, a in predictions) / len(predictions)


def days_out_bucket(days):
    """Bucket days_out into groups for calibration."""
    if days <= 1:
        return "0-1"
    elif days <= 3:
        return "2-3"
    else:
        return "4+"


# ─── Calibration logic ───

def calibrate_weather(trades, settlement_map):
    """Calibrate weather sigma from matched trades.

    Returns dict with global and per-city sigma parameters.
    """
    # Collect (forecast_temp, threshold, direction, days_out, actual_outcome) tuples
    matched = []
    for t in trades:
        ticker = t.get("ticker", "")
        parsed = parse_weather_ticker(ticker)
        if not parsed:
            continue

        forecast_temp = t.get("forecast_temp")
        if forecast_temp is None:
            continue

        # Determine outcome from settlement
        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        # Determine side: if we bought YES and won (revenue > 0), event occurred
        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        # Compute days_out from trade timestamp and market date
        ts = t.get("timestamp", "")
        try:
            trade_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            market_date = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
            days = max(0, (market_date - trade_date).days)
        except (ValueError, TypeError):
            days = 0

        matched.append({
            "forecast_temp": forecast_temp,
            "threshold": parsed["threshold"],
            "direction": parsed["direction"],
            "days_out": days,
            "city": parsed["city"],
            "actual": actual,
        })

    if not matched:
        return {"n": 0}

    if len(matched) < 30:
        print(f"Warning: Only {len(matched)} matched trades — calibration results may be unreliable", file=sys.stderr)

    # Global grid search: find (intercept, slope, df) minimizing log loss
    best_brier = float("inf")
    best_intercept = 2.5
    best_slope = 0.5
    best_df = 6
    df_candidates = [4, 5, 6, 7, 8, 10, 15, 30]

    for intercept_x10 in range(5, 60):  # 0.5 to 5.9
        intercept = intercept_x10 / 10.0
        for slope_x10 in range(2, 40):  # 0.2 to 3.9 (wider range for sqrt scaling)
            slope = slope_x10 / 10.0
            for df in df_candidates:
                preds = []
                for m in matched:
                    sigma = intercept + slope * math.sqrt(m["days_out"])
                    prob = weather_prob_with_sigma(m["forecast_temp"], m["threshold"], m["direction"], sigma, df=df)
                    preds.append((prob, m["actual"]))
                bs = log_loss(preds)
                if bs is not None and bs < best_brier:
                    best_brier = bs
                    best_intercept = intercept
                    best_slope = slope
                    best_df = df

    # Per-city calibration with Bayesian shrinkage toward global
    SHRINKAGE_K = 15  # shrinkage prior strength
    by_city = defaultdict(list)
    for m in matched:
        by_city[m["city"]].append(m)

    per_city = {}
    for city, city_trades in by_city.items():
        if len(city_trades) < 5:
            continue
        city_best_brier = float("inf")
        city_intercept = best_intercept
        city_slope = best_slope
        city_df = best_df

        # Small-sample regularization: narrow search range to prevent overfitting
        if len(city_trades) < 15:
            intercept_range = range(10, 40)   # 1.0-3.9
            slope_range = range(2, 20)        # 0.2-1.9
            city_df_candidates = [5, 6, 7, 8]
        else:
            intercept_range = range(5, 60)    # 0.5-5.9
            slope_range = range(2, 40)        # 0.2-3.9
            city_df_candidates = df_candidates

        for intercept_x10 in intercept_range:
            intercept = intercept_x10 / 10.0
            for slope_x10 in slope_range:
                slope = slope_x10 / 10.0
                for df in city_df_candidates:
                    preds = []
                    for m in city_trades:
                        sigma = intercept + slope * math.sqrt(m["days_out"])
                        prob = weather_prob_with_sigma(m["forecast_temp"], m["threshold"], m["direction"], sigma, df=df)
                        preds.append((prob, m["actual"]))
                    bs = log_loss(preds)
                    if bs is not None and bs < city_best_brier:
                        city_best_brier = bs
                        city_intercept = intercept
                        city_slope = slope
                        city_df = df

        # Bayesian shrinkage: blend city-specific toward global
        city_weight = len(city_trades) / (len(city_trades) + SHRINKAGE_K)
        shrunk_intercept = city_weight * city_intercept + (1 - city_weight) * best_intercept
        shrunk_slope = city_weight * city_slope + (1 - city_weight) * best_slope

        per_city[city] = {
            "sigma_intercept": round(shrunk_intercept, 2),
            "sigma_slope": round(shrunk_slope, 2),
            "df": city_df,
            "n": len(city_trades),
            "shrinkage_weight": round(city_weight, 3),
        }

    return {
        "global_sigma_intercept": best_intercept,
        "global_sigma_slope": best_slope,
        "df": best_df,
        "global_brier": round(best_brier, 6) if best_brier < float("inf") else None,
        "per_city": per_city,
        "n": len(matched),
    }


def calibrate_nws(trades, settlement_map):
    """Calibrate NWS sigma by hour bucket."""
    matched = []
    for t in trades:
        ticker = t.get("ticker", "")
        if not ticker.startswith("KXHIGH"):
            continue

        # NWS trades from source-monitor have a "source" or "strategy" field
        strategy = t.get("strategy", "")
        if "nws" not in strategy.lower() and "actual" not in t.get("reasoning", "").lower():
            continue

        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        # Extract running_high and hour from trade data
        running_high = t.get("running_high") or t.get("current_temp")
        hour = t.get("hour_of_day")
        if running_high is None or hour is None:
            continue

        parsed = parse_weather_ticker(ticker)
        if not parsed:
            continue

        matched.append({
            "running_high": running_high,
            "threshold": parsed["threshold"],
            "direction": parsed["direction"],
            "hour": hour,
            "actual": actual,
        })

    if not matched:
        return {"n": 0}

    # Grid search sigma per hour bucket
    buckets = {"17+": [], "15-16": [], "before_15": []}
    for m in matched:
        if m["hour"] >= 17:
            buckets["17+"].append(m)
        elif m["hour"] >= 15:
            buckets["15-16"].append(m)
        else:
            buckets["before_15"].append(m)

    sigma_by_hour = {}
    for bucket, items in buckets.items():
        if len(items) < 3:
            continue
        best_sigma = {"17+": 0.5, "15-16": 1.5, "before_15": 3.0}[bucket]
        best_bs = float("inf")
        for sigma_x10 in range(1, 80):  # 0.1 to 7.9
            sigma = sigma_x10 / 10.0
            preds = []
            for m in items:
                if m["direction"] == "T":
                    z = (m["threshold"] - m["running_high"]) / sigma
                    prob = 1.0 - _student_t_cdf(z, 6)
                else:
                    z_lo = (m["threshold"] - m["running_high"]) / sigma
                    z_hi = (m["threshold"] + 1 - m["running_high"]) / sigma
                    prob = _student_t_cdf(z_hi, 6) - _student_t_cdf(z_lo, 6)
                preds.append((prob, m["actual"]))
            bs = log_loss(preds)
            if bs is not None and bs < best_bs:
                best_bs = bs
                best_sigma = sigma
        sigma_by_hour[bucket] = round(best_sigma, 1)

    return {"sigma_by_hour": sigma_by_hour, "n": len(matched)}


def calibrate_info_arb(trades, settlement_map, label):
    """Calibrate info-arb sigma for album or box office trades."""
    matched = []
    for t in trades:
        ticker = t.get("ticker", "")
        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        confidence = t.get("confidence") or t.get("est_edge")
        if confidence is None:
            continue

        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        # Get day of week from timestamp
        ts = t.get("timestamp", "")
        try:
            dow = datetime.fromisoformat(ts.replace("Z", "+00:00")).weekday()
        except (ValueError, TypeError):
            dow = 2  # default Wednesday

        try:
            conf_val = float(str(confidence).rstrip("%")) / 100 if "%" in str(confidence) else float(confidence)
        except (ValueError, TypeError):
            continue

        matched.append({"predicted": conf_val, "actual": actual, "dow": dow})

    if not matched:
        return {"n": 0}

    # Group by day bucket and compute calibration
    day_buckets = defaultdict(list)
    for m in matched:
        if m["dow"] <= 1:
            day_buckets["mon_tue"].append(m)
        elif m["dow"] <= 3:
            day_buckets["wed_thu"].append(m)
        else:
            day_buckets["fri_sun"].append(m)

    sigma_by_day = {}
    for bucket, items in day_buckets.items():
        if len(items) < 3:
            continue
        bs = brier_score([(m["predicted"], m["actual"]) for m in items])
        # Heuristic: sigma ~ sqrt(brier_score) as rough calibration indicator
        sigma_by_day[bucket] = round(math.sqrt(bs) if bs else 0.05, 2)

    return {"sigma_by_day": sigma_by_day, "n": len(matched)}


def calibrate_ensemble_weights(trades, settlement_map):
    """Calibrate ensemble model weights from weather trades with per-model forecasts.

    Scans trades for ensemble_forecasts field, matches against settlements,
    computes per-model MAE, and updates weights via inverse-MAE weighting.

    Returns dict with weights and model_maes, or {"n": 0} if no data.
    """
    matched = []
    for t in trades:
        ticker = t.get("ticker", "")
        ensemble = t.get("ensemble_forecasts")
        if not ensemble or not isinstance(ensemble, dict):
            continue

        parsed = parse_weather_ticker(ticker)
        if not parsed:
            continue

        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        # Compute days_out for sigma
        ts = t.get("timestamp", "")
        try:
            trade_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            market_date = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
            days = max(0, (market_date - trade_date).days)
        except (ValueError, TypeError):
            days = 0

        matched.append({
            "ensemble": ensemble,
            "threshold": parsed["threshold"],
            "direction": parsed["direction"],
            "days_out": days,
            "actual": actual,
        })

    if not matched:
        return {"n": 0}

    # Compute per-model absolute error of probability prediction
    model_errors = defaultdict(list)  # model_name -> [abs_error, ...]
    for m in matched:
        for model_name, temp in m["ensemble"].items():
            prob = weather_prob_with_sigma(temp, m["threshold"], m["direction"],
                                           sigma=2.0 + 0.5 * math.sqrt(m["days_out"]))
            error = abs(prob - m["actual"])
            model_errors[model_name].append(error)

    if not model_errors:
        return {"n": len(matched)}

    # Compute MAE per model
    model_maes = {}
    for model_name, errors in model_errors.items():
        model_maes[model_name] = round(sum(errors) / len(errors), 6)

    # Weights = inverse MAE, normalized (EMA with existing weights)
    existing_weights = {"gfs": 0.40, "ecmwf": 0.40, "icon": 0.20}
    inv_maes = {}
    for model_name, mae in model_maes.items():
        inv_maes[model_name] = 1.0 / max(0.001, mae)

    total_inv = sum(inv_maes.values())
    if total_inv <= 0:
        return {"n": len(matched), "model_maes": model_maes}

    new_weights = {m: inv / total_inv for m, inv in inv_maes.items()}

    # EMA blend: 90% old + 10% new (smooth update)
    ema_alpha = 0.1
    blended = {}
    for model_name in set(list(existing_weights.keys()) + list(new_weights.keys())):
        old_w = existing_weights.get(model_name, 0.0)
        new_w = new_weights.get(model_name, 0.0)
        blended[model_name] = (1 - ema_alpha) * old_w + ema_alpha * new_w

    # Normalize
    total_w = sum(blended.values())
    if total_w > 0:
        blended = {m: round(w / total_w, 4) for m, w in blended.items()}

    return {
        "weights": blended,
        "model_maes": model_maes,
        "n": len(matched),
    }


# ─── Main ───

def main():
    parser = argparse.ArgumentParser(
        description="Calibrate sigma parameters from historical trade settlements."
    )
    parser.add_argument("--save", action="store_true", help="Save calibration to config/calibration.json")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-api", action="store_true", help="Skip Kalshi API calls (local data only)")
    args = parser.parse_args()

    # Load all trade logs
    all_trades = {}
    for tf in TRADE_FILES:
        trades = load_trades_safe(tf["path"])
        all_trades[tf["label"]] = trades

    total_trades = sum(len(t) for t in all_trades.values())

    # Fetch settlements
    settlement_map = {}  # ticker -> revenue
    n_settlements = 0
    if not args.no_api:
        try:
            from kalshi_auth import KalshiClient
            client = KalshiClient()
            settlements = fetch_settlements(client)
            n_settlements = len(settlements)
            for s in settlements:
                ticker = s.get("market_ticker", s.get("ticker", ""))
                revenue = s.get("revenue", 0)
                try:
                    revenue = int(revenue)
                except (TypeError, ValueError):
                    revenue = 0
                # Sum revenue per ticker (multiple contracts possible)
                settlement_map[ticker] = settlement_map.get(ticker, 0) + revenue
        except Exception as e:
            print(f"Warning: Could not fetch settlements: {e}", file=sys.stderr)
            print("Run with --no-api to skip API calls.", file=sys.stderr)

    # Run calibrations
    weather_trades = all_trades.get("Weather Bot", [])
    weather_cal = calibrate_weather(weather_trades, settlement_map)

    # NWS trades come from multiple sources
    all_bot_trades = []
    for trades in all_trades.values():
        all_bot_trades.extend(trades)
    nws_cal = calibrate_nws(all_bot_trades, settlement_map)

    ent_trades = all_trades.get("Entertainment Bot", [])
    album_cal = calibrate_info_arb(ent_trades, settlement_map, "album_sales")

    beat_trades = all_trades.get("BeatRelease Scanner", [])
    box_cal = calibrate_info_arb(beat_trades, settlement_map, "box_office")

    # Ensemble weight calibration
    ensemble_cal = calibrate_ensemble_weights(all_bot_trades, settlement_map)

    # Build calibration result
    calibration = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "n_settlements": n_settlements,
        "n_trades": total_trades,
        "weather": weather_cal,
        "nws": nws_cal,
        "album_sales": album_cal,
        "box_office": box_cal,
        "ensemble": ensemble_cal,
    }

    if args.save:
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_PATH.write_text(json.dumps(calibration, indent=2) + "\n")

    if args.json:
        print(json.dumps(calibration, indent=2))
    else:
        _print_report(calibration, args.save)


def _print_report(cal, saved):
    """Print human-readable calibration report."""
    print("=" * 60)
    print("SIGMA CALIBRATION REPORT")
    print(f"Generated: {cal['generated_at']}")
    print(f"Settlements matched: {cal['n_settlements']}")
    print(f"Total trades loaded: {cal['n_trades']}")
    print("=" * 60)

    # Weather
    w = cal["weather"]
    print(f"\n--- Weather (n={w.get('n', 0)}) ---")
    if w.get("n", 0) > 0:
        print(f"  Global: sigma = {w['global_sigma_intercept']:.1f} + {w['global_sigma_slope']:.1f} * sqrt(days_out)")
        if w.get("global_brier") is not None:
            print(f"  Brier score: {w['global_brier']:.4f}")
        for city, cc in w.get("per_city", {}).items():
            print(f"  {city}: sigma = {cc['sigma_intercept']:.1f} + {cc['sigma_slope']:.1f} * sqrt(days_out) (n={cc['n']})")
    else:
        print("  No matched weather trades.")

    # NWS
    n = cal["nws"]
    print(f"\n--- NWS (n={n.get('n', 0)}) ---")
    if n.get("n", 0) > 0:
        for bucket, sigma in n.get("sigma_by_hour", {}).items():
            print(f"  {bucket}: sigma = {sigma}")
    else:
        print("  No matched NWS trades.")

    # Album sales
    a = cal["album_sales"]
    print(f"\n--- Album Sales (n={a.get('n', 0)}) ---")
    if a.get("n", 0) > 0:
        for bucket, sigma in a.get("sigma_by_day", {}).items():
            print(f"  {bucket}: sigma = {sigma}")
    else:
        print("  No matched album trades.")

    # Box office
    b = cal["box_office"]
    print(f"\n--- Box Office (n={b.get('n', 0)}) ---")
    if b.get("n", 0) > 0:
        for bucket, sigma in b.get("sigma_by_day", {}).items():
            print(f"  {bucket}: sigma = {sigma}")
    else:
        print("  No matched box office trades.")

    # Ensemble weights
    e = cal.get("ensemble", {})
    print(f"\n--- Ensemble Weights (n={e.get('n', 0)}) ---")
    if e.get("n", 0) > 0:
        for model, w in e.get("weights", {}).items():
            mae = e.get("model_maes", {}).get(model, "?")
            print(f"  {model}: weight={w:.3f}  MAE={mae}")
    else:
        print("  No trades with ensemble_forecasts data.")

    if saved:
        print(f"\nCalibration saved to {CALIBRATION_PATH}")
    else:
        print("\nRun with --save to write config/calibration.json")


if __name__ == "__main__":
    main()
