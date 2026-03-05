#!/usr/bin/env python3
"""Crypto model calibration pipeline.

Fetches historical price data from Coinbase, generates synthetic Kalshi markets,
evaluates each model, and optimizes ensemble weights + model parameters.

Usage:
    python3 scripts/calibrate-crypto.py              # Full calibration
    python3 scripts/calibrate-crypto.py --days 30     # Shorter lookback
    python3 scripts/calibrate-crypto.py --asset BTC    # Single asset
    python3 scripts/calibrate-crypto.py --dry-run      # Report only, don't save

Output: config/crypto-calibration.json
"""
import json, math, time, argparse, sys, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

import requests
from probability import (
    crypto_price_probability,
    crypto_price_probability_jd,
    crypto_price_probability_heston,
    _reset_calibration,
)
from crypto_models import EnsembleModel, REGIME_BMA_WEIGHTS, REGIME_JUMP_INTENSITY

CALIBRATION_PATH = PROJECT_DIR / "config" / "crypto-calibration.json"
ASSETS = ["BTC", "ETH"]
HORIZONS_MINUTES = [15, 60, 360, 1440]
THRESHOLD_OFFSETS = [0.90, 0.95, 0.97, 0.99, 1.01, 1.03, 1.05, 1.10]


def fetch_coinbase_candles(asset, days=90, granularity=300):
    """Fetch historical OHLCV candles from Coinbase.

    Args:
        asset: "BTC" or "ETH"
        days: Lookback period
        granularity: Candle size in seconds (300=5min, 3600=1h)

    Returns:
        List of dicts with time, open, high, low, close, volume, sorted by time.
    """
    all_candles = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # Coinbase limits to 300 candles per request
    max_candles = 300
    chunk_seconds = max_candles * granularity
    current_start = start

    while current_start < end:
        current_end = min(current_start + timedelta(seconds=chunk_seconds), end)
        url = (
            f"https://api.exchange.coinbase.com/products/{asset}-USD/candles"
            f"?start={current_start.isoformat()}&end={current_end.isoformat()}"
            f"&granularity={granularity}"
        )
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            candles = r.json()
            # Coinbase returns [time, low, high, open, close, volume]
            for c in candles:
                all_candles.append({
                    "time": c[0], "open": c[3], "high": c[2],
                    "low": c[1], "close": c[4], "volume": c[5],
                })
        except Exception as e:
            print(f"  Warning: fetch failed for {asset} chunk: {e}")
        current_start = current_end
        time.sleep(0.3)  # rate limiting

    all_candles.sort(key=lambda x: x["time"])
    return all_candles


def compute_realized_vol_from_candles(candles, window=None):
    """Compute annualized realized vol from close prices."""
    prices = [c["close"] for c in candles]
    if window:
        prices = prices[-window:]
    if len(prices) < 5:
        return 0.50  # default
    log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices)) if prices[i-1] > 0]
    if len(log_returns) < 3:
        return 0.50
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    # Assume 5-min candles -> 105120 intervals per year
    intervals_per_year = 365.25 * 24 * 60 / 5
    return math.sqrt(var * intervals_per_year)


def generate_synthetic_markets(candles, horizons=HORIZONS_MINUTES, offsets=THRESHOLD_OFFSETS):
    """Generate synthetic binary markets from price history.

    For each candle, creates markets: "Will price be above threshold in T minutes?"
    Looks ahead T minutes to determine actual outcome.

    Returns list of dicts with: current_price, threshold, direction, horizon_min, outcome (0 or 1).
    """
    markets = []
    # Build time->price lookup (5-min granularity)
    time_price = {c["time"]: c["close"] for c in candles}
    times = sorted(time_price.keys())

    for i, t in enumerate(times):
        price = time_price[t]
        for horizon in horizons:
            # Find future price
            future_time = t + horizon * 60
            future_price = time_price.get(future_time)
            if future_price is None:
                continue
            for offset in offsets:
                threshold = price * offset
                outcome = 1 if future_price > threshold else 0
                markets.append({
                    "current_price": price,
                    "threshold": threshold,
                    "direction": "above",
                    "horizon_min": horizon,
                    "outcome": outcome,
                    "rv_window": min(horizon * 3, 288),  # 3x horizon or 24h
                    "candle_idx": i,
                })
    return markets


def evaluate_model(model_fn, markets, candles, split_idx=None):
    """Evaluate a model on synthetic markets and return Brier score.

    Args:
        model_fn: callable(current_price, threshold, direction, horizon_min, vol) -> prob
        markets: list of synthetic market dicts
        candles: for computing rolling vol
        split_idx: if provided, only evaluate markets with candle_idx >= split_idx

    Returns:
        dict with brier_score, per_horizon_brier, n_markets
    """
    brier_sum = 0.0
    horizon_brier = {}
    n = 0

    for m in markets:
        idx = m["candle_idx"]
        if split_idx is not None and idx < split_idx:
            continue
        window = min(idx, m["rv_window"])
        if window < 5:
            continue
        vol = compute_realized_vol_from_candles(candles[:idx+1], window=window)

        prob = model_fn(m["current_price"], m["threshold"], m["direction"], m["horizon_min"], vol)
        prob = max(0.001, min(0.999, prob))

        sq_err = (prob - m["outcome"]) ** 2
        brier_sum += sq_err
        n += 1

        h = m["horizon_min"]
        if h not in horizon_brier:
            horizon_brier[h] = {"sum": 0.0, "n": 0}
        horizon_brier[h]["sum"] += sq_err
        horizon_brier[h]["n"] += 1

    return {
        "brier_score": brier_sum / max(1, n),
        "per_horizon": {h: d["sum"] / max(1, d["n"]) for h, d in horizon_brier.items()},
        "n_markets": n,
    }


def calibrate(days=90, assets=None, dry_run=False):
    """Run full calibration pipeline."""
    assets = assets or ASSETS
    print(f"Crypto Calibration Pipeline — {days}-day lookback")
    print("=" * 60)

    results = {}
    for asset in assets:
        print(f"\n--- {asset} ---")

        # 1. Fetch data
        print(f"  Fetching {days} days of 5-min candles from Coinbase...")
        candles = fetch_coinbase_candles(asset, days=days)
        print(f"  Got {len(candles)} candles")
        if len(candles) < 100:
            print(f"  Insufficient data for {asset}, skipping")
            continue

        # 2. Generate synthetic markets
        print(f"  Generating synthetic markets...")
        markets = generate_synthetic_markets(candles)
        print(f"  Generated {len(markets)} synthetic markets")

        # Train/test split: first 70% for optimization, last 30% for validation
        max_idx = max(m["candle_idx"] for m in markets) if markets else 0
        split_idx = int(max_idx * 0.7)
        train_markets = [m for m in markets if m["candle_idx"] < split_idx]
        print(f"  Train/test split at candle index {split_idx} (train={len(train_markets)}, test={len(markets)-len(train_markets)})")

        # 3. Evaluate each model (train set for optimization)
        print(f"  Evaluating models...")

        # GBM
        def gbm_fn(p, k, d, t, v):
            return crypto_price_probability(p, k, d, t, realized_vol_pct=v)
        gbm_result = evaluate_model(gbm_fn, train_markets, candles)
        print(f"    GBM Brier:     {gbm_result['brier_score']:.4f} ({gbm_result['n_markets']} markets)")

        # JD (with various lambda)
        best_jd_lambda = 1.0
        best_jd_brier = 1.0
        for lam in [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]:
            def jd_fn(p, k, d, t, v, _lam=lam):
                return crypto_price_probability_jd(p, k, d, t, realized_vol_pct=v, jump_intensity=_lam)
            jd_result = evaluate_model(jd_fn, train_markets, candles)
            if jd_result["brier_score"] < best_jd_brier:
                best_jd_brier = jd_result["brier_score"]
                best_jd_lambda = lam
        print(f"    JD Brier:      {best_jd_brier:.4f} (best lambda={best_jd_lambda})")

        # Heston parameter search
        best_heston_params = {"kappa": 2.0, "xi": 0.3, "rho": -0.7}
        best_heston_brier = 1.0
        for h_kappa in [1.0, 2.0, 5.0]:
            for h_xi in [0.1, 0.3, 0.5, 1.0]:
                for h_rho in [-0.9, -0.7, -0.5, -0.3]:
                    def heston_fn(p, k, d, t, v, _kap=h_kappa, _xi=h_xi, _rho=h_rho):
                        return crypto_price_probability_heston(
                            p, k, d, t, v0=v**2, kappa=_kap, theta=v**2, xi=_xi, rho=_rho)
                    result = evaluate_model(heston_fn, train_markets, candles)
                    if result["brier_score"] < best_heston_brier:
                        best_heston_brier = result["brier_score"]
                        best_heston_params = {"kappa": h_kappa, "xi": h_xi, "rho": h_rho}
        print(f"    Heston Brier:  {best_heston_brier:.4f} (kappa={best_heston_params['kappa']}, "
              f"xi={best_heston_params['xi']}, rho={best_heston_params['rho']})")

        # 4. Optimize ensemble weights (using calibrated Heston params)
        print(f"  Optimizing ensemble weights...")
        best_weights = [0.33, 0.34, 0.33]
        best_ensemble_brier = 1.0
        hp = best_heston_params

        for w_gbm in [x / 20 for x in range(0, 21, 2)]:
            for w_jd in [x / 20 for x in range(0, 21 - int(w_gbm * 20), 2)]:
                w_heston = 1.0 - w_gbm - w_jd
                if w_heston < 0:
                    continue

                def ensemble_fn(p, k, d, t, v, _wg=w_gbm, _wj=w_jd, _wh=w_heston,
                                _lam=best_jd_lambda, _hp=hp):
                    pg = crypto_price_probability(p, k, d, t, realized_vol_pct=v)
                    pj = crypto_price_probability_jd(p, k, d, t, realized_vol_pct=v, jump_intensity=_lam)
                    ph = crypto_price_probability_heston(p, k, d, t, v0=v**2,
                                                        kappa=_hp["kappa"], theta=v**2,
                                                        xi=_hp["xi"], rho=_hp["rho"])
                    return max(0.001, min(0.999, _wg * pg + _wj * pj + _wh * ph))

                result = evaluate_model(ensemble_fn, train_markets, candles)
                if result["brier_score"] < best_ensemble_brier:
                    best_ensemble_brier = result["brier_score"]
                    best_weights = [w_gbm, w_jd, w_heston]

        print(f"    Ensemble Brier: {best_ensemble_brier:.4f} (weights: GBM={best_weights[0]:.2f}, JD={best_weights[1]:.2f}, Heston={best_weights[2]:.2f})")

        # 5. Validation Brier on held-out test set
        def final_ensemble_fn(p, k, d, t, v, _wg=best_weights[0], _wj=best_weights[1],
                              _wh=best_weights[2], _lam=best_jd_lambda, _hp=hp):
            pg = crypto_price_probability(p, k, d, t, realized_vol_pct=v)
            pj = crypto_price_probability_jd(p, k, d, t, realized_vol_pct=v, jump_intensity=_lam)
            ph = crypto_price_probability_heston(p, k, d, t, v0=v**2,
                                                kappa=_hp["kappa"], theta=v**2,
                                                xi=_hp["xi"], rho=_hp["rho"])
            return max(0.001, min(0.999, _wg * pg + _wj * pj + _wh * ph))
        validation_result = evaluate_model(final_ensemble_fn, markets, candles, split_idx=split_idx)
        print(f"    Validation Brier: {validation_result['brier_score']:.4f} ({validation_result['n_markets']} test markets)")

        results[asset] = {
            "gbm_brier": round(gbm_result["brier_score"], 4),
            "jd_brier": round(best_jd_brier, 4),
            "jd_lambda": best_jd_lambda,
            "heston_brier": round(best_heston_brier, 4),
            "heston_params": best_heston_params,
            "ensemble_brier": round(best_ensemble_brier, 4),
            "validation_brier": round(validation_result["brier_score"], 4),
            "ensemble_weights": [round(w, 2) for w in best_weights],
            "per_horizon_gbm": {str(k): round(v, 4) for k, v in gbm_result["per_horizon"].items()},
            "n_markets": gbm_result["n_markets"],
        }

    # 5. Write output
    output = {
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "assets": results,
        "regime_jump_intensity": dict(REGIME_JUMP_INTENSITY),
    }

    print(f"\n{'='*60}")
    print("CALIBRATION RESULTS")
    print(f"{'='*60}")
    for asset, r in results.items():
        print(f"\n{asset}:")
        print(f"  GBM:        {r['gbm_brier']:.4f}")
        print(f"  JD:         {r['jd_brier']:.4f} (lambda={r['jd_lambda']})")
        hp = r.get('heston_params', {})
        print(f"  Heston:     {r['heston_brier']:.4f} (kappa={hp.get('kappa')}, xi={hp.get('xi')}, rho={hp.get('rho')})")
        print(f"  Ensemble:   {r['ensemble_brier']:.4f} (w={r['ensemble_weights']})")
        print(f"  Validation: {r.get('validation_brier', 'N/A')}")
        improvement = (r['gbm_brier'] - r['ensemble_brier']) / r['gbm_brier'] * 100
        print(f"  Improvement over GBM: {improvement:.1f}%")

    if not dry_run:
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nSaved to {CALIBRATION_PATH}")
    else:
        print("\n(dry run — not saved)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto model calibration")
    parser.add_argument("--days", type=int, default=90, help="Lookback days")
    parser.add_argument("--asset", type=str, help="Single asset to calibrate")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    args = parser.parse_args()
    calibrate(
        days=args.days,
        assets=[args.asset] if args.asset else None,
        dry_run=args.dry_run,
    )
