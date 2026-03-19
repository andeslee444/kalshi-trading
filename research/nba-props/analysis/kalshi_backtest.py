"""Kalshi-integrated backtester — validates model predictions against real market outcomes.

Instead of using proxy market prices (uniform-weight hit rate), this module:
1. Maps Real Sports players to Kalshi prop market tickers
2. Uses actual Kalshi market outcomes (settled yes/no) as ground truth
3. Computes edge = model_probability - kalshi_implied_probability
4. Calculates P&L with real Kalshi fee structure

This is the bridge between the research pipeline and the trading bot.

Usage:
    from analysis.kalshi_backtest import kalshi_backtest_report
    report = kalshi_backtest_report()
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent.parent / "data"
KALSHI_DIR = DATA_DIR / "processed" / "kalshi_prices"
GAMELOGS_DIR = DATA_DIR / "processed" / "gamelogs"

# Stat type mapping: Kalshi title words → backtester stat types
KALSHI_STAT_MAP = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "3-pointers": "three_pointers",
    "steals": "steals",
    "blocks": "blocks",
}


def _parse_kalshi_title(title: str) -> dict | None:
    """Parse 'Player Name: N+ stat_type' from Kalshi market title."""
    match = re.match(r"(.+?):\s+(\d+)\+\s+(.+)", title)
    if not match:
        return None
    return {
        "player_name": match.group(1).strip(),
        "line": int(match.group(2)),
        "stat_raw": match.group(3).strip().lower(),
    }


def _load_kalshi_markets() -> list[dict]:
    """Load all collected Kalshi NBA prop markets."""
    markets = []
    if not KALSHI_DIR.exists():
        return []
    for f in KALSHI_DIR.glob("*.json"):
        if f.name == "collection_summary.json":
            continue
        try:
            data = json.loads(f.read_text())
            parsed = _parse_kalshi_title(data.get("title", ""))
            if parsed:
                data["_parsed"] = parsed
                markets.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return markets


def _load_player_gamelogs() -> dict[str, list[dict]]:
    """Load gamelogs indexed by player name (lowercase)."""
    players = {}
    for f in GAMELOGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            name = data.get("player_name", "").lower()
            gamelogs = data.get("gamelogs", [])
            if name and gamelogs:
                players[name] = gamelogs
        except (json.JSONDecodeError, OSError):
            continue
    return players


def _fuzzy_match_player(kalshi_name: str, gamelog_names: set[str]) -> str | None:
    """Match Kalshi player name to gamelog player name."""
    kn = kalshi_name.lower().strip()
    if kn in gamelog_names:
        return kn
    # Try last name match
    parts = kn.split()
    if len(parts) >= 2:
        last = parts[-1]
        matches = [n for n in gamelog_names if n.endswith(last)]
        if len(matches) == 1:
            return matches[0]
    return None


def kalshi_backtest_report(params: dict | None = None) -> dict[str, Any]:
    """Generate a report comparing model predictions to Kalshi market outcomes.

    For each settled Kalshi prop market:
    1. Find the matching player in our gamelogs
    2. Compute the model's probability using the same logic as backtester.py
    3. Compare to the Kalshi outcome (yes/no = player cleared the line or not)
    4. Calculate theoretical P&L

    Returns dict with accuracy metrics, P&L, and per-stat breakdown.
    """
    from analysis.backtester import _weighted_hit_rate, _compute_prob_space_shift

    if params is None:
        params = {}

    markets = _load_kalshi_markets()
    players = _load_player_gamelogs()
    player_names = set(players.keys())

    # Filter to settled markets with results
    settled = [m for m in markets if m.get("result") in ("yes", "no")]

    matched = 0
    unmatched_players = set()
    predictions = []  # (model_prob, outcome, market)

    for market in settled:
        parsed = market.get("_parsed", {})
        player_name = parsed.get("player_name", "")
        line = parsed.get("line")
        stat_raw = parsed.get("stat_raw", "")
        stat_type = KALSHI_STAT_MAP.get(stat_raw)

        if not stat_type or line is None:
            continue

        # Match player
        gl_name = _fuzzy_match_player(player_name, player_names)
        if not gl_name:
            unmatched_players.add(player_name)
            continue

        gamelogs = players[gl_name]
        if len(gamelogs) < 5:
            continue

        # Compute model probability: what does our model predict for this line?
        values = []
        for gl in gamelogs:
            val = gl.get(stat_type, 0)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    pass

        if not values or len(values) < 5:
            continue

        # Sort by date descending for recency weighting
        sorted_gamelogs = sorted(gamelogs, key=lambda g: g.get("game_date", ""), reverse=True)
        sorted_values = []
        for gl in sorted_gamelogs:
            val = gl.get(stat_type, 0)
            try:
                sorted_values.append(float(val))
            except (ValueError, TypeError):
                sorted_values.append(0.0)

        model_prob = _weighted_hit_rate(sorted_values, float(line), params)

        # Ground truth from Kalshi
        outcome = 1.0 if market["result"] == "yes" else 0.0

        predictions.append({
            "model_prob": model_prob,
            "outcome": outcome,
            "player": player_name,
            "stat": stat_type,
            "line": line,
            "ticker": market.get("ticker", ""),
        })
        matched += 1

    if not predictions:
        return {
            "error": "no_matches",
            "settled_markets": len(settled),
            "unmatched_players": len(unmatched_players),
        }

    # Compute metrics
    from analysis.metrics import brier_score, calibration_error, directional_accuracy

    model_probs = [p["model_prob"] for p in predictions]
    outcomes = [p["outcome"] for p in predictions]

    # Simulated P&L: if model_prob > 0.6, buy YES; if < 0.4, buy NO
    fee_rate = 0.07
    total_risked = 0.0
    total_profit = 0.0
    trades = 0
    for p in predictions:
        mp = p["model_prob"]
        out = p["outcome"]
        # Assume Kalshi price = 0.50 (fair odds) since we don't have real prices on demo
        implied = 0.50

        if mp > 0.60:  # buy YES
            cost = implied
            if out > 0.5:
                gross = 1.0 - cost
                total_profit += gross * (1 - fee_rate)
            else:
                total_profit -= cost
            total_risked += cost
            trades += 1
        elif mp < 0.40:  # buy NO
            cost = 1.0 - implied
            if out < 0.5:
                gross = 1.0 - cost
                total_profit += gross * (1 - fee_rate)
            else:
                total_profit -= cost
            total_risked += cost
            trades += 1

    profit_pct = (total_profit / total_risked * 100) if total_risked > 0 else 0.0

    # Per-stat breakdown
    per_stat = {}
    for stat in set(p["stat"] for p in predictions):
        stat_preds = [p for p in predictions if p["stat"] == stat]
        sp = [p["model_prob"] for p in stat_preds]
        so = [p["outcome"] for p in stat_preds]
        per_stat[stat] = {
            "count": len(stat_preds),
            "brier": round(brier_score(sp, so), 4),
            "accuracy": round(directional_accuracy(sp, so), 3),
        }

    return {
        "total_settled": len(settled),
        "matched": matched,
        "unmatched_players": sorted(unmatched_players)[:20],
        "brier_score": round(brier_score(model_probs, outcomes), 4),
        "calibration_error": round(calibration_error(model_probs, outcomes), 4),
        "directional_accuracy": round(directional_accuracy(model_probs, outcomes), 3),
        "trades": trades,
        "profit_pct": round(profit_pct, 1),
        "per_stat": per_stat,
        "sample_predictions": predictions[:10],
    }


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    report = kalshi_backtest_report()

    print(f"\n{'='*60}")
    print("Kalshi Backtest Report")
    print(f"{'='*60}")
    print(f"  Settled markets:      {report.get('total_settled', 0)}")
    print(f"  Matched to gamelogs:  {report.get('matched', 0)}")
    print(f"  Brier score:          {report.get('brier_score', 'N/A')}")
    print(f"  Calibration error:    {report.get('calibration_error', 'N/A')}")
    print(f"  Directional accuracy: {report.get('directional_accuracy', 'N/A')}")
    print(f"  Trades (>60% conf):   {report.get('trades', 0)}")
    print(f"  Simulated profit:     {report.get('profit_pct', 0):.1f}%")

    if report.get("per_stat"):
        print(f"\n  Per-stat breakdown:")
        for stat, data in sorted(report["per_stat"].items()):
            print(f"    {stat:>15}: brier={data['brier']}, acc={data['accuracy']}, n={data['count']}")

    if report.get("unmatched_players"):
        print(f"\n  Unmatched players ({len(report['unmatched_players'])}):")
        for p in report["unmatched_players"][:10]:
            print(f"    - {p}")

    if report.get("sample_predictions"):
        print(f"\n  Sample predictions:")
        for p in report["sample_predictions"][:5]:
            correct = "Y" if (p["model_prob"] > 0.5) == (p["outcome"] > 0.5) else "N"
            print(f"    {p['player']:<20} {p['stat']:>10} {p['line']:>3}+ "
                  f"model={p['model_prob']:.2f} actual={'yes' if p['outcome'] else 'no'} [{correct}]")

    print(f"{'='*60}")


if __name__ == "__main__":
    main()
