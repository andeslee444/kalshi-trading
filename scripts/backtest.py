#!/usr/bin/env python3
"""Backtesting harness for Kalshi probability models and position sizing.

Evaluates model quality using settled trades: calibration tables, Brier scores,
sizing comparisons, and edge threshold sweeps.

Usage:
    python3 scripts/backtest.py                     # Full report
    python3 scripts/backtest.py --json              # JSON output
    python3 scripts/backtest.py --bot weather       # Filter by bot
    python3 scripts/backtest.py --no-api            # Local-only mode
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

from probability import (
    weather_probability,
    half_kelly,
    half_kelly_sell,
)


# ─── Trade file definitions ───
TRADE_FILES = [
    {"label": "weather", "path": PROJECT_DIR / "data" / "kalshi-trades.json"},
    {"label": "strategy", "path": PROJECT_DIR / "data" / "kalshi-strategy-trades.json"},
    {"label": "entertainment", "path": PROJECT_DIR / "data" / "kalshi-entertainment-trades.json"},
    {"label": "beatrelease", "path": PROJECT_DIR / "data" / "beatrelease-trades.json"},
]

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


def fetch_fills(client):
    """Paginate /portfolio/fills and return all fill records."""
    all_fills = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("fills", [])
        all_fills.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return all_fills


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


# ─── Pure functions (tested separately) ───

def brier_score(predictions):
    """Compute Brier score from list of (predicted_prob, actual_outcome) tuples.

    Returns None if empty. Lower is better (0=perfect, 0.25=random, 1=worst).
    """
    if not predictions:
        return None
    return sum((p - a) ** 2 for p, a in predictions) / len(predictions)


def calibration_table(predictions, n_bins=5):
    """Bin predictions and compute calibration statistics.

    predictions: list of (predicted_prob, actual_outcome) tuples
    Returns list of dicts: {bin_label, n, predicted_avg, actual_avg, gap}
    """
    if not predictions:
        return []

    bin_width = 1.0 / n_bins
    bins = defaultdict(list)

    for pred, actual in predictions:
        bin_idx = min(int(pred / bin_width), n_bins - 1)
        lo = bin_idx * bin_width
        hi = lo + bin_width
        label = f"{lo:.1f}-{hi:.1f}"
        bins[label].append((pred, actual))

    table = []
    for i in range(n_bins):
        lo = i * bin_width
        hi = lo + bin_width
        label = f"{lo:.1f}-{hi:.1f}"
        items = bins.get(label, [])
        if not items:
            continue
        pred_avg = sum(p for p, _ in items) / len(items)
        actual_avg = sum(a for _, a in items) / len(items)
        table.append({
            "bin": label,
            "n": len(items),
            "predicted_avg": round(pred_avg, 3),
            "actual_avg": round(actual_avg, 3),
            "gap": round(actual_avg - pred_avg, 3),
        })

    return table


# ─── Model re-evaluation ───

def reeval_weather_trade(trade, settlement_revenue):
    """Re-evaluate a weather trade using current model. Returns dict or None."""
    ticker = trade.get("ticker", "")
    parsed = parse_weather_ticker(ticker)
    if not parsed:
        return None

    forecast_temp = trade.get("forecast_temp")
    if forecast_temp is None:
        return None

    # Determine actual outcome
    side = trade.get("side", "").lower()
    if side == "yes":
        actual = 1 if settlement_revenue > 0 else 0
    elif side == "no":
        actual = 0 if settlement_revenue > 0 else 1
    else:
        return None

    # Compute days_out
    ts = trade.get("timestamp", "")
    try:
        trade_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        market_date = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
        days_out = max(0, (market_date - trade_date).days)
    except (ValueError, TypeError):
        days_out = 0

    predicted = weather_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out)

    # Re-compute Kelly sizing
    price = trade.get("price", 0) or trade.get("price_cents", 0)
    if price and price > 0 and price < 100:
        edge = abs(predicted - price / 100.0)
        kelly_contracts, kelly_risk = half_kelly(edge, price, 500)
    else:
        kelly_contracts, kelly_risk = 0, 0

    return {
        "ticker": ticker,
        "predicted": predicted,
        "actual": actual,
        "side": side,
        "revenue": settlement_revenue,
        "price": price,
        "kelly_contracts": kelly_contracts,
    }


def reeval_strategy_trade(trade, settlement_revenue):
    """Re-evaluate a strategy/longshot trade. Returns dict or None."""
    ticker = trade.get("ticker", "")
    yes_price = trade.get("yes_price_at_entry") or trade.get("yes_price", 0)
    if not yes_price or yes_price <= 0 or yes_price > 15:
        return None

    # Becker model: edge = 0.57 * exp(-0.15 * price)
    est_edge = 0.57 * math.exp(-0.15 * yes_price)

    # Determine outcome: selling YES, so we win if event doesn't occur
    # Revenue > 0 means we won
    actual_win = 1 if settlement_revenue > 0 else 0
    # For calibration: predicted probability that our SELL wins = 1 - true_prob
    implied = yes_price / 100.0
    p_true = max(0.001, implied - est_edge)
    predicted_win = 1 - p_true

    # Re-compute Kelly sizing
    sell_price = yes_price
    contracts, risk = half_kelly_sell(est_edge, sell_price, 500)

    return {
        "ticker": ticker,
        "predicted": predicted_win,
        "actual": actual_win,
        "side": "no",
        "revenue": settlement_revenue,
        "price": 100 - yes_price,
        "kelly_contracts": contracts,
    }


def reeval_entertainment_trade(trade, settlement_revenue):
    """Re-evaluate entertainment/beatrelease trade. Limited: use stored confidence."""
    ticker = trade.get("ticker", "")
    confidence = trade.get("confidence")
    if confidence is None:
        return None

    try:
        conf_val = float(str(confidence).rstrip("%")) / 100 if "%" in str(confidence) else float(confidence)
    except (ValueError, TypeError):
        return None

    side = trade.get("side", "").lower()
    if side == "yes":
        actual = 1 if settlement_revenue > 0 else 0
        predicted = conf_val
    elif side == "no":
        actual = 0 if settlement_revenue > 0 else 1
        predicted = 1 - conf_val
    else:
        return None

    return {
        "ticker": ticker,
        "predicted": predicted,
        "actual": actual,
        "side": side,
        "revenue": settlement_revenue,
        "price": trade.get("price", 0) or trade.get("price_cents", 0),
        "kelly_contracts": 0,
    }


# ─── Sizing comparison ───

def sizing_comparison(evaluated_trades):
    """Compare actual P&L vs flat sizing vs Kelly sizing."""
    actual_pnl = 0
    flat_pnl = 0
    kelly_pnl = 0

    for t in evaluated_trades:
        revenue = t.get("revenue", 0)
        actual_pnl += revenue

        # Flat: 1 contract per trade
        price = t.get("price", 0)
        if t["actual"] == 1:
            flat_pnl += (100 - price)  # win
        else:
            flat_pnl -= price  # lose

        # Kelly: recomputed contracts
        kc = t.get("kelly_contracts", 0)
        if kc > 0:
            if t["actual"] == 1:
                kelly_pnl += kc * (100 - price)
            else:
                kelly_pnl -= kc * price

    return {
        "actual_pnl_cents": actual_pnl,
        "flat_pnl_cents": flat_pnl,
        "kelly_pnl_cents": kelly_pnl,
    }


# ─── Edge threshold sweep ───

def threshold_sweep(evaluated_trades, thresholds=None):
    """Test different edge thresholds and report win rate / P&L at each."""
    if thresholds is None:
        thresholds = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25]

    results = []
    for thresh in thresholds:
        filtered = [t for t in evaluated_trades
                    if abs(t["predicted"] - 0.5) >= thresh / 2]  # edge proxy
        if not filtered:
            results.append({
                "threshold": thresh,
                "trades": 0,
                "win_rate": None,
                "pnl_cents": 0,
            })
            continue

        wins = sum(1 for t in filtered if t["actual"] == 1)
        pnl = sum(t["revenue"] for t in filtered)
        results.append({
            "threshold": thresh,
            "trades": len(filtered),
            "win_rate": round(wins / len(filtered), 3),
            "pnl_cents": pnl,
        })

    return results


# ─── Main ───

def main():
    parser = argparse.ArgumentParser(description="Backtest Kalshi probability models.")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--bot", type=str, help="Filter by bot (weather, strategy, entertainment, beatrelease)")
    parser.add_argument("--no-api", action="store_true", help="Skip Kalshi API calls")
    parser.add_argument("--save", action="store_true", help="Save results to data/backtest-results.json")
    args = parser.parse_args()

    # Load trades
    trades_by_bot = {}
    for tf in TRADE_FILES:
        if args.bot and tf["label"] != args.bot:
            continue
        trades = load_trades_safe(tf["path"])
        if trades:
            trades_by_bot[tf["label"]] = trades

    total_loaded = sum(len(t) for t in trades_by_bot.values())

    # Fetch settlements
    settlement_map = {}
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
                settlement_map[ticker] = settlement_map.get(ticker, 0) + revenue
        except Exception as e:
            print(f"Warning: Could not fetch settlements: {e}", file=sys.stderr)

    # Re-evaluate trades
    all_evaluated = []

    for bot_label, trades in trades_by_bot.items():
        for t in trades:
            ticker = t.get("ticker", "")
            revenue = settlement_map.get(ticker)
            if revenue is None:
                continue

            if bot_label == "weather":
                result = reeval_weather_trade(t, revenue)
            elif bot_label == "strategy":
                result = reeval_strategy_trade(t, revenue)
            elif bot_label in ("entertainment", "beatrelease"):
                result = reeval_entertainment_trade(t, revenue)
            else:
                continue

            if result:
                result["bot"] = bot_label
                all_evaluated.append(result)

    # Compute metrics
    predictions = [(t["predicted"], t["actual"]) for t in all_evaluated]
    bs = brier_score(predictions)
    cal_table = calibration_table(predictions)
    sizing = sizing_comparison(all_evaluated)
    sweep = threshold_sweep(all_evaluated)

    # Per-bot breakdown
    per_bot = {}
    for bot_label in trades_by_bot:
        bot_evals = [t for t in all_evaluated if t["bot"] == bot_label]
        bot_preds = [(t["predicted"], t["actual"]) for t in bot_evals]
        per_bot[bot_label] = {
            "n_evaluated": len(bot_evals),
            "brier_score": brier_score(bot_preds),
            "win_rate": round(sum(a for _, a in bot_preds) / len(bot_preds), 3) if bot_preds else None,
        }

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "n_trades_loaded": total_loaded,
        "n_settlements": n_settlements,
        "n_evaluated": len(all_evaluated),
        "brier_score": round(bs, 6) if bs is not None else None,
        "calibration_table": cal_table,
        "sizing_comparison": sizing,
        "threshold_sweep": sweep,
        "per_bot": per_bot,
    }

    if args.save:
        save_path = PROJECT_DIR / "data" / "backtest-results.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(report, indent=2))
        print(f"Results saved to {save_path}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)


def _print_report(r):
    """Print human-readable backtest report."""
    print("=" * 70)
    print("BACKTEST REPORT")
    print(f"Generated: {r['generated_at']}")
    print(f"Trades loaded: {r['n_trades_loaded']} | Settlements: {r['n_settlements']} | Evaluated: {r['n_evaluated']}")
    print("=" * 70)

    # Brier score
    bs = r["brier_score"]
    if bs is not None:
        print(f"\nBrier Score: {bs:.4f}  (0=perfect, 0.25=random, 1=worst)")
    else:
        print("\nBrier Score: N/A (no evaluated trades)")

    # Calibration table
    cal = r.get("calibration_table", [])
    if cal:
        print("\n--- Calibration Table ---")
        print(f"{'Bin':<12} {'N':>4}  {'Predicted':>9}  {'Actual':>7}  {'Gap':>6}")
        print("-" * 45)
        for row in cal:
            print(f"{row['bin']:<12} {row['n']:>4}  {row['predicted_avg']:>9.3f}  {row['actual_avg']:>7.3f}  {row['gap']:>+6.3f}")

    # Sizing comparison
    sz = r.get("sizing_comparison", {})
    if sz:
        print("\n--- Sizing Comparison ---")
        print(f"  Actual P&L:  ${sz.get('actual_pnl_cents', 0) / 100:>8.2f}")
        print(f"  Flat (1x):   ${sz.get('flat_pnl_cents', 0) / 100:>8.2f}")
        print(f"  Kelly:       ${sz.get('kelly_pnl_cents', 0) / 100:>8.2f}")

    # Threshold sweep
    sweep = r.get("threshold_sweep", [])
    if sweep:
        print("\n--- Edge Threshold Sweep ---")
        print(f"{'Threshold':>10}  {'Trades':>6}  {'Win Rate':>8}  {'P&L':>10}")
        print("-" * 40)
        for row in sweep:
            wr = f"{row['win_rate'] * 100:.1f}%" if row["win_rate"] is not None else "N/A"
            print(f"   {row['threshold'] * 100:>5.0f}%    {row['trades']:>6}  {wr:>8}  ${row['pnl_cents'] / 100:>8.2f}")

    # Per-bot
    per_bot = r.get("per_bot", {})
    if per_bot:
        print("\n--- Per-Bot Breakdown ---")
        for label, stats in per_bot.items():
            bs_str = f"{stats['brier_score']:.4f}" if stats["brier_score"] is not None else "N/A"
            wr_str = f"{stats['win_rate'] * 100:.1f}%" if stats["win_rate"] is not None else "N/A"
            print(f"  {label}: n={stats['n_evaluated']}, Brier={bs_str}, WinRate={wr_str}")

    if r["n_evaluated"] == 0:
        print("\nNo trades could be matched to settlements.")
        print("Run with Kalshi API access or ensure trade logs exist in data/.")
    elif r["n_evaluated"] < 30:
        print(f"\nWARNING: Only {r['n_evaluated']} evaluated trades — metrics may be unreliable (need >= 30)")


if __name__ == "__main__":
    main()
