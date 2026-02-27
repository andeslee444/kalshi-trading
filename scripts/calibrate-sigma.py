#!/usr/bin/env python3
"""Calibrate sigma parameters for probability models using historical trade settlements.

Reads trade logs from all bots (via canonical TRADE_FILES module), matches them
against Kalshi settlement data, and grid-searches for optimal sigma values that
minimize Brier score (primary) with realized P&L as tiebreaker.

Usage:
    python3 scripts/calibrate-sigma.py              # Display calibration report
    python3 scripts/calibrate-sigma.py --save       # Save to config/calibration.json
    python3 scripts/calibrate-sigma.py --json       # JSON output
    python3 scripts/calibrate-sigma.py --no-api     # Local-only (skip settlement fetch)
    python3 scripts/calibrate-sigma.py --dry-run    # Report data availability without modifying anything
"""

import argparse
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── Path setup ───
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
PROJECT_DIR = Path(__file__).resolve().parent.parent

from probability import _norm_cdf, _student_t_cdf, half_kelly
from trade_files import TRADE_FILES, ALL_TRADE_PATHS

CALIBRATION_PATH = PROJECT_DIR / "config" / "calibration.json"
CALIBRATION_BACKUP_PATH = PROJECT_DIR / "config" / "calibration-backup.json"

# Minimum sample sizes for reliable calibration
MIN_TRADES_PER_CITY = 10
MIN_TRADES_GLOBAL = 30

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
        path = "/portfolio/settlements?limit=100"
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


def simulated_pnl(matched_trades, intercept, slope, df):
    """Compute simulated P&L for a set of matched trades given sigma parameters.

    For each trade, recompute what half_kelly sizing would have been with the
    candidate sigma, then compute profit/loss based on settlement outcome.

    Returns total P&L in cents.
    """
    total_pnl = 0
    for m in matched_trades:
        sigma = intercept + slope * math.sqrt(m["days_out"])
        sigma = max(0.5, sigma)
        prob = weather_prob_with_sigma(m["forecast_temp"], m["threshold"],
                                       m["direction"], sigma, df=df)

        # Use the trade's original price to compute edge and sizing
        price_cents = m.get("price_cents")
        if price_cents is None or price_cents <= 0 or price_cents >= 100:
            continue

        implied_prob = price_cents / 100.0

        # Determine side from trade
        side = m.get("side", "yes")
        if side == "yes":
            edge = prob - implied_prob
        else:
            edge = (1.0 - prob) - (1.0 - implied_prob)

        if edge <= 0:
            continue

        # half_kelly returns (contracts, risk_cents)
        contracts, risk_cents = half_kelly(edge, price_cents, max_cost_cents=500,
                                           bankroll_cents=50000)
        if contracts <= 0:
            continue

        # Compute P&L: if outcome matches our side, we win (100-price)*contracts
        # otherwise we lose price*contracts
        if side == "yes":
            if m["actual"] == 1:
                total_pnl += (100 - price_cents) * contracts
            else:
                total_pnl -= price_cents * contracts
        else:
            if m["actual"] == 0:
                total_pnl += price_cents * contracts
            else:
                total_pnl -= (100 - price_cents) * contracts

    return total_pnl


def days_out_bucket(days):
    """Bucket days_out into groups for calibration."""
    if days <= 1:
        return "0-1"
    elif days <= 3:
        return "2-3"
    else:
        return "4+"


def _backup_calibration():
    """Create a backup of calibration.json before overwriting."""
    if CALIBRATION_PATH.exists():
        shutil.copy2(CALIBRATION_PATH, CALIBRATION_BACKUP_PATH)
        print(f"Backup saved to {CALIBRATION_BACKUP_PATH}")


def _print_diff(old_cal, new_cal):
    """Print a diff summary comparing old vs new calibration parameters."""
    print("\n--- Parameter Changes ---")

    # Weather global
    old_w = old_cal.get("weather", {})
    new_w = new_cal.get("weather", {})
    if new_w.get("n", 0) > 0:
        old_intercept = old_w.get("global_sigma_intercept", "N/A")
        new_intercept = new_w.get("global_sigma_intercept", "N/A")
        old_slope = old_w.get("global_sigma_slope", "N/A")
        new_slope = new_w.get("global_sigma_slope", "N/A")
        print(f"  Weather global intercept: {old_intercept} -> {new_intercept}")
        print(f"  Weather global slope:     {old_slope} -> {new_slope}")

        # Per-city changes
        old_cities = old_w.get("per_city", {})
        new_cities = new_w.get("per_city", {})
        all_cities = sorted(set(list(old_cities.keys()) + list(new_cities.keys())))
        for city in all_cities:
            old_c = old_cities.get(city, {})
            new_c = new_cities.get(city, {})
            old_si = old_c.get("sigma_intercept", "N/A")
            new_si = new_c.get("sigma_intercept", "N/A")
            old_ss = old_c.get("sigma_slope", "N/A")
            new_ss = new_c.get("sigma_slope", "N/A")
            if old_si != new_si or old_ss != new_ss:
                print(f"  {city}: intercept {old_si}->{new_si}, slope {old_ss}->{new_ss}")

    # NWS
    old_nws = old_cal.get("nws", {}).get("sigma_by_hour", {})
    new_nws = new_cal.get("nws", {}).get("sigma_by_hour", {})
    if new_nws:
        for bucket in sorted(set(list(old_nws.keys()) + list(new_nws.keys()))):
            old_v = old_nws.get(bucket, "N/A")
            new_v = new_nws.get(bucket, "N/A")
            if old_v != new_v:
                print(f"  NWS {bucket}: {old_v} -> {new_v}")

    if not new_w.get("n", 0) and not new_nws:
        print("  No parameter changes (no matched trades).")


# ─── Calibration logic ───

def calibrate_weather(trades, settlement_map):
    """Calibrate weather sigma from matched trades.

    Uses multi-objective optimization:
    - Primary: minimize Brier score
    - Tiebreaker: maximize realized P&L (within 0.001 Brier tolerance)

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

        # Extract price for P&L tiebreaker computation
        price_cents = t.get("price_cents") or t.get("price")
        if price_cents is not None:
            try:
                price_cents = int(price_cents)
            except (TypeError, ValueError):
                price_cents = None

        matched.append({
            "forecast_temp": forecast_temp,
            "threshold": parsed["threshold"],
            "direction": parsed["direction"],
            "days_out": days,
            "city": parsed["city"],
            "actual": actual,
            "side": side,
            "price_cents": price_cents,
        })

    if not matched:
        return {"n": 0}

    if len(matched) < MIN_TRADES_GLOBAL:
        print(f"Warning: Only {len(matched)} matched trades (minimum {MIN_TRADES_GLOBAL} recommended) "
              f"-- calibration results may be unreliable", file=sys.stderr)

    # Log per-city trade counts
    city_counts = defaultdict(int)
    for m in matched:
        city_counts[m["city"]] += 1
    for city, count in sorted(city_counts.items()):
        if count < MIN_TRADES_PER_CITY:
            print(f"Warning: {city} has only {count} trades (minimum {MIN_TRADES_PER_CITY} for per-city calibration)",
                  file=sys.stderr)

    # Global grid search: find (intercept, slope, df) minimizing Brier score
    # with P&L as tiebreaker when Brier scores are within tolerance
    BRIER_TOLERANCE = 0.001
    best_brier = float("inf")
    best_pnl = float("-inf")
    best_intercept = 2.5
    best_slope = 0.5
    best_df = 6
    best_log_loss_val = None
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
                bs = brier_score(preds)
                if bs is None:
                    continue

                # Multi-objective: Brier primary, P&L tiebreaker
                if bs < best_brier - BRIER_TOLERANCE:
                    # Clearly better Brier -- use this
                    best_brier = bs
                    best_intercept = intercept
                    best_slope = slope
                    best_df = df
                    best_pnl = simulated_pnl(matched, intercept, slope, df)
                    best_log_loss_val = log_loss(preds)
                elif abs(bs - best_brier) <= BRIER_TOLERANCE:
                    # Brier within tolerance -- use P&L as tiebreaker
                    pnl = simulated_pnl(matched, intercept, slope, df)
                    if pnl > best_pnl:
                        best_brier = bs
                        best_pnl = pnl
                        best_intercept = intercept
                        best_slope = slope
                        best_df = df
                        best_log_loss_val = log_loss(preds)

    # Compute final Brier and log_loss for reporting
    final_preds = []
    for m in matched:
        sigma = best_intercept + best_slope * math.sqrt(m["days_out"])
        prob = weather_prob_with_sigma(m["forecast_temp"], m["threshold"], m["direction"], sigma, df=best_df)
        final_preds.append((prob, m["actual"]))
    final_brier = brier_score(final_preds)
    final_log_loss = log_loss(final_preds)

    # Per-city calibration with Bayesian shrinkage toward global
    SHRINKAGE_K = 15  # shrinkage prior strength
    by_city = defaultdict(list)
    for m in matched:
        by_city[m["city"]].append(m)

    per_city = {}
    for city, city_trades in by_city.items():
        if len(city_trades) < MIN_TRADES_PER_CITY:
            continue
        city_best_brier = float("inf")
        city_best_pnl = float("-inf")
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
                    bs = brier_score(preds)
                    if bs is None:
                        continue

                    if bs < city_best_brier - BRIER_TOLERANCE:
                        city_best_brier = bs
                        city_intercept = intercept
                        city_slope = slope
                        city_df = df
                        city_best_pnl = simulated_pnl(city_trades, intercept, slope, df)
                    elif abs(bs - city_best_brier) <= BRIER_TOLERANCE:
                        pnl = simulated_pnl(city_trades, intercept, slope, df)
                        if pnl > city_best_pnl:
                            city_best_brier = bs
                            city_best_pnl = pnl
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
        "global_brier": round(final_brier, 6) if final_brier is not None else None,
        "global_log_loss": round(final_log_loss, 6) if final_log_loss is not None else None,
        "global_pnl_cents": best_pnl if best_pnl > float("-inf") else None,
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
            bs = brier_score(preds)
            if bs is not None and bs < best_bs:
                best_bs = bs
                best_sigma = sigma
        sigma_by_hour[bucket] = round(best_sigma, 1)

    return {"sigma_by_hour": sigma_by_hour, "n": len(matched)}


def calibrate_info_arb(trades, settlement_map, label):
    """Calibrate info-arb sigma for album or box office trades.

    Uses source_type field from source-monitor trades to filter correctly,
    prefers model_prob over confidence, and performs proper grid search
    with Brier score objective and Bayesian shrinkage.
    """
    matched = []
    for t in trades:
        ticker = t.get("ticker", "")
        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        # Require exact source_type match to prevent NWS trades (which lack
        # source_type) from contaminating album/box_office calibration
        source_type = t.get("source_type", "")
        if label == "album_sales" and source_type != "album":
            continue
        if label == "box_office" and source_type != "boxoffice":
            continue

        # Prefer model_prob (source-monitor), fall back to confidence (entertainment)
        model_prob = t.get("model_prob") or t.get("confidence") or t.get("est_edge")
        if model_prob is None:
            continue

        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        ts = t.get("timestamp", "")
        try:
            dow = datetime.fromisoformat(ts.replace("Z", "+00:00")).weekday()
        except (ValueError, TypeError):
            dow = 2

        try:
            conf_val = float(str(model_prob).rstrip("%")) / 100 if "%" in str(model_prob) else float(model_prob)
        except (ValueError, TypeError):
            continue

        matched.append({"predicted": conf_val, "actual": actual, "dow": dow})

    if not matched:
        return {"n": 0}

    # Group by day bucket based on label
    day_buckets = defaultdict(list)
    for m in matched:
        if label == "box_office":
            # Match boxoffice_data_sigma() buckets: fri_sat, sun, mon_thu
            if m["dow"] in (4, 5):       # Fri, Sat
                day_buckets["fri_sat"].append(m)
            elif m["dow"] == 6:           # Sun
                day_buckets["sun"].append(m)
            else:                         # Mon-Thu
                day_buckets["mon_thu"].append(m)
        else:
            # Match album_data_sigma() buckets: mon_tue, wed_thu, fri_sun
            if m["dow"] <= 1:
                day_buckets["mon_tue"].append(m)
            elif m["dow"] <= 3:
                day_buckets["wed_thu"].append(m)
            else:
                day_buckets["fri_sun"].append(m)

    # Default sigma values (match production code defaults)
    if label == "box_office":
        default_sigmas = {"fri_sat": 0.12, "sun": 0.05, "mon_thu": 0.04}
    else:
        default_sigmas = {"mon_tue": 0.15, "wed_thu": 0.10, "fri_sun": 0.05}

    sigma_by_day = {}
    for bucket, items in day_buckets.items():
        if len(items) < 10:  # Minimum 10 for reliability
            continue

        # Grid search for optimal sigma using Brier score
        default_sigma = default_sigmas[bucket]
        best_sigma = default_sigma
        best_loss = float("inf")

        for sigma_x100 in range(1, 51):  # 0.01 to 0.50
            sigma = sigma_x100 / 100.0
            preds = []
            for m in items:
                # Platt-style recalibration: test how sharpening/softening
                # the model's probability outputs affects calibration
                z = (m["predicted"] - 0.5) / sigma if sigma > 0 else 0
                prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
                preds.append((prob, m["actual"]))
            loss = brier_score(preds)
            if loss is not None and loss < best_loss:
                best_loss = loss
                best_sigma = sigma

        # Bayesian shrinkage toward default
        weight = len(items) / (len(items) + 15)
        shrunk_sigma = weight * best_sigma + (1 - weight) * default_sigma
        sigma_by_day[bucket] = round(shrunk_sigma, 3)

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Report data availability without modifying anything")
    args = parser.parse_args()

    # Load all trade logs from canonical TRADE_FILES
    DATA_DIR = PROJECT_DIR / "data"
    all_trades = {}
    for tf in TRADE_FILES:
        filepath = DATA_DIR / tf["filename"]
        trades = load_trades_safe(filepath)
        all_trades[tf["label"]] = trades

    total_trades = sum(len(t) for t in all_trades.values())

    # Count trades with settlement_result annotations
    n_with_settlement = 0
    for trades in all_trades.values():
        for t in trades:
            if t.get("settlement_result") is not None:
                n_with_settlement += 1

    if args.dry_run:
        print("=" * 60)
        print("CALIBRATION DRY RUN")
        print("=" * 60)
        print(f"\nTrade files read (canonical TRADE_FILES, {len(TRADE_FILES)} files):")
        for tf in TRADE_FILES:
            filepath = DATA_DIR / tf["filename"]
            count = len(all_trades.get(tf["label"], []))
            exists = filepath.exists()
            print(f"  {tf['label']:25s} {count:4d} trades  {'(found)' if exists else '(missing)'}")
        print(f"\nTotal trades loaded: {total_trades}")
        print(f"Trades with settlement_result: {n_with_settlement}")
        if n_with_settlement == 0:
            print("\nNo reconciled trades found. Run 'npm run reconcile' first to annotate")
            print("trade logs with settlement outcomes, then re-run calibration.")
        else:
            print(f"\n{n_with_settlement} trades have settlement data -- ready for calibration.")
        return

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

    # Check if we have any data to work with
    if not settlement_map and n_with_settlement == 0:
        print("0 settled trades found. No settlement data available.", file=sys.stderr)
        print("Run 'npm run reconcile' first to annotate trade logs with settlement outcomes.",
              file=sys.stderr)
        if args.save:
            print("Skipping save -- will not overwrite calibration.json with empty data.",
                  file=sys.stderr)
        return

    # Run calibrations
    weather_trades = all_trades.get("Weather Bot", [])
    weather_cal = calibrate_weather(weather_trades, settlement_map)

    # NWS trades come from multiple sources
    all_bot_trades = []
    for trades in all_trades.values():
        all_bot_trades.extend(trades)
    nws_cal = calibrate_nws(all_bot_trades, settlement_map)

    # Info-arb calibration: combine entertainment + source-monitor trades
    info_arb_trades = all_trades.get("Entertainment Bot", []) + all_trades.get("Source Monitor", [])
    album_cal = calibrate_info_arb(info_arb_trades, settlement_map, "album_sales")
    box_cal = calibrate_info_arb(info_arb_trades, settlement_map, "box_office")

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

    # Check if calibration produced any non-zero results
    has_data = any([
        weather_cal.get("n", 0) > 0,
        nws_cal.get("n", 0) > 0,
        album_cal.get("n", 0) > 0,
        box_cal.get("n", 0) > 0,
        ensemble_cal.get("n", 0) > 0,
    ])

    if args.save:
        if not has_data:
            print("Warning: No categories have matched trades. Skipping save to avoid "
                  "overwriting calibration.json with empty data.", file=sys.stderr)
        else:
            # Backup existing calibration before overwriting
            old_cal = {}
            if CALIBRATION_PATH.exists():
                try:
                    old_cal = json.loads(CALIBRATION_PATH.read_text())
                except (json.JSONDecodeError, OSError):
                    old_cal = {}
                _backup_calibration()

            CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
            CALIBRATION_PATH.write_text(json.dumps(calibration, indent=2) + "\n")

            # Print diff summary
            if old_cal:
                _print_diff(old_cal, calibration)

    if args.json:
        print(json.dumps(calibration, indent=2))
    else:
        _print_report(calibration, args.save and has_data)


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
        if w.get("global_log_loss") is not None:
            print(f"  Log loss:    {w['global_log_loss']:.4f}")
        if w.get("global_pnl_cents") is not None:
            pnl_dollars = w["global_pnl_cents"] / 100.0
            print(f"  Simulated P&L: ${pnl_dollars:+.2f}")
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
