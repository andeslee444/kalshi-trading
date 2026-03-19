"""Backtest engine for sports research experiments.

Runs hold-one-out cross-validation on player game logs to evaluate
parameter configurations from experiment.py.

For each player, for each stat type, for each held-out game:
  1. Build empirical distribution from remaining N-1 games
  2. Compute adjusted probability using experiment params
  3. Compare to actual outcome (did player clear the line?)

The "line" is the training-set mean rounded to nearest 0.5 — approximating
what sportsbooks would set. The "market price" is the unadjusted hit rate —
what the market roughly prices without our adjustments.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from analysis.metrics import (
    brier_score,
    calibration_error,
    directional_accuracy,
    expected_profit,
)

DATA_DIR = Path(__file__).parent.parent / "data"
GAMELOGS_DIR = DATA_DIR / "processed" / "gamelogs"

# Core stats (high-volume Kalshi markets)
STAT_TYPES_CORE = [
    "points",
    "rebounds",
    "assists",
    "three_pointers",
    "points+rebounds+assists",
]

# Extended stats (lower-volume but reveals model limits on sparse data)
STAT_TYPES_EXTENDED = [
    "steals",
    "blocks",
    "turnovers",
    "rebounds+assists",
]

# Default: core only. Agent can toggle via experiment.py USE_EXTENDED_STATS
STAT_TYPES = STAT_TYPES_CORE


def _extract_stat(game: dict, stat_type: str) -> float:
    """Extract a stat value from a game log dict. Supports "+" combos."""
    total = 0.0
    for part in stat_type.split("+"):
        total += float(game.get(part, 0))
    return total


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _detect_b2b(game_date: str, other_dates: list[str]) -> bool:
    """Check if a game is the second of a back-to-back (game played yesterday)."""
    try:
        gd = datetime.strptime(game_date, "%Y-%m-%d")
    except ValueError:
        return False
    yesterday = gd - timedelta(days=1)
    for d in other_dates:
        try:
            if datetime.strptime(d, "%Y-%m-%d") == yesterday:
                return True
        except ValueError:
            continue
    return False


def _weighted_hit_rate(values: list[float], line: float, params: dict) -> float:
    """Recency-weighted hit rate using experiment params.

    Values must be sorted most-recent-first (by game date descending).
    Mirrors Book B's weighted_hit_rate logic with parameterized weights.
    """
    if not values:
        return 0.5

    w5 = params.get("RECENCY_WEIGHT_LAST5", 2.0)
    w10 = params.get("RECENCY_WEIGHT_LAST10", 1.5)
    ws = params.get("RECENCY_WEIGHT_SEASON", 1.0)

    weights = []
    for i in range(len(values)):
        if i < 5:
            weights.append(w5)
        elif i < 10:
            weights.append(w10)
        else:
            weights.append(ws)

    hits = sum(w * (1.0 if v > line else 0.0) for w, v in zip(weights, values))
    total = sum(weights)
    if total <= 0:
        return 0.5
    return max(0.01, min(0.99, hits / total))


def _compute_prob_space_shift(
    held_out: dict,
    training: list[dict],
    stat_type: str,
    params: dict,
) -> float:
    """Compute total probability-space adjustment.

    Mirrors Book B's additive probability shifts for matchup, venue, and B2B.
    """
    season_values = [_extract_stat(g, stat_type) for g in training]
    season_avg = _mean(season_values)
    if season_avg <= 0:
        return 0.0

    total_shift = 0.0

    # Matchup: (opp_avg / season_avg - 1.0) * multiplier, clamped
    opp = held_out.get("opponent", "")
    opp_games = [g for g in training if g.get("opponent") == opp]
    if opp_games:
        opp_avg = _mean([_extract_stat(g, stat_type) for g in opp_games])
        mult = params.get("MATCHUP_MULTIPLIER", 0.15)
        cap = params.get("MATCHUP_CAP", 0.10)
        shift = (opp_avg / season_avg - 1.0) * mult
        total_shift += max(-cap, min(cap, shift))

    # Venue: (venue_avg / season_avg - 1.0) * multiplier, clamped
    is_home = held_out.get("home", True)
    venue_games = [g for g in training if g.get("home") == is_home]
    if venue_games:
        venue_avg = _mean([_extract_stat(g, stat_type) for g in venue_games])
        mult = params.get("VENUE_MULTIPLIER", 0.10)
        cap = params.get("VENUE_CAP", 0.05)
        shift = (venue_avg / season_avg - 1.0) * mult
        total_shift += max(-cap, min(cap, shift))

    # B2B: fixed penalty if game played the day after another
    other_dates = [g.get("game_date", "") for g in training]
    if _detect_b2b(held_out.get("game_date", ""), other_dates):
        total_shift += params.get("B2B_PENALTY", -0.05)

    return total_shift


def _compute_stat_space_shift(
    held_out: dict,
    training: list[dict],
    stat_type: str,
    params: dict,
) -> float:
    """Compute total stat-space shift (in raw stat units).

    Instead of shifting probability, shift the raw stat values before
    computing hit rate. Preserves distribution shape near the line.
    """
    season_values = [_extract_stat(g, stat_type) for g in training]
    season_avg = _mean(season_values)
    if season_avg <= 0:
        return 0.0

    total_shift = 0.0

    # Matchup: raw difference scaled by multiplier
    opp = held_out.get("opponent", "")
    opp_games = [g for g in training if g.get("opponent") == opp]
    if opp_games:
        opp_avg = _mean([_extract_stat(g, stat_type) for g in opp_games])
        mult = params.get("MATCHUP_MULTIPLIER", 0.15)
        total_shift += (opp_avg - season_avg) * mult

    # Venue: raw difference scaled by multiplier
    is_home = held_out.get("home", True)
    venue_games = [g for g in training if g.get("home") == is_home]
    if venue_games:
        venue_avg = _mean([_extract_stat(g, stat_type) for g in venue_games])
        mult = params.get("VENUE_MULTIPLIER", 0.10)
        total_shift += (venue_avg - season_avg) * mult

    # B2B: convert probability penalty to stat units (scale by season avg)
    other_dates = [g.get("game_date", "") for g in training]
    if _detect_b2b(held_out.get("game_date", ""), other_dates):
        b2b = params.get("B2B_PENALTY", -0.05)
        total_shift += b2b * season_avg

    return total_shift


def backtest_book_b(
    params: dict,
    gamelogs_dir: Path | None = None,
) -> dict[str, Any]:
    """Run Book B backtest with hold-one-out cross-validation.

    Args:
        params: Parameter dict (from experiment.py module attributes).
        gamelogs_dir: Path to player gamelog JSON files.

    Returns:
        Dict with brier_score, calibration_error, expected_profit_pct,
        hit_rate, sample_size, and per_stat breakdown.
    """
    if gamelogs_dir is None:
        gamelogs_dir = GAMELOGS_DIR

    # LOCKED eval-scope params — fixed to prevent metric gaming.
    stat_types = STAT_TYPES_CORE + STAT_TYPES_EXTENDED
    line_offsets = [-2, 0, 2]
    min_games = 5

    use_stat_space = params.get("USE_STAT_SPACE", False)

    all_predictions: list[float] = []
    all_outcomes: list[float] = []
    all_market_prices: list[float] = []
    per_stat: dict[str, dict[str, list]] = {
        st: {"preds": [], "outs": [], "prices": []} for st in stat_types
    }

    player_files = sorted(gamelogs_dir.glob("*.json"))

    for pf in player_files:
        if pf.name.startswith("."):
            continue
        try:
            data = json.loads(pf.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if isinstance(data, dict):
            gamelogs = data.get("gamelogs", [])
        elif isinstance(data, list):
            gamelogs = data
        else:
            continue

        if len(gamelogs) < min_games:
            continue

        for stat_type in stat_types:
            # Skip stats where all values are zero
            values = [_extract_stat(g, stat_type) for g in gamelogs]
            if all(v == 0 for v in values):
                continue

            for i, held_out in enumerate(gamelogs):
                training = gamelogs[:i] + gamelogs[i + 1 :]
                if len(training) < 3:
                    continue

                # Base line: training set mean rounded to nearest 0.5
                train_values = [_extract_stat(g, stat_type) for g in training]
                season_avg = _mean(train_values)
                base_line = round(season_avg * 2) / 2
                if base_line <= 0:
                    continue

                # Sort training by date descending for recency weighting
                sorted_training = sorted(
                    training,
                    key=lambda g: g.get("game_date", ""),
                    reverse=True,
                )
                sorted_values = [
                    _extract_stat(g, stat_type) for g in sorted_training
                ]

                # Pre-compute adjustments (same for all line offsets)
                if use_stat_space:
                    stat_shift = _compute_stat_space_shift(
                        held_out, training, stat_type, params
                    )
                else:
                    prob_shift = _compute_prob_space_shift(
                        held_out, training, stat_type, params
                    )

                actual_value = _extract_stat(held_out, stat_type)

                # Test at each line offset (default [0], can be [-2, 0, 2])
                for offset in line_offsets:
                    line = base_line + offset
                    if line <= 0:
                        continue

                    # Market price proxy: uniform-weight hit rate
                    _UNIFORM = {
                        "RECENCY_WEIGHT_LAST5": 1.0,
                        "RECENCY_WEIGHT_LAST10": 1.0,
                        "RECENCY_WEIGHT_SEASON": 1.0,
                    }
                    market_prob = _weighted_hit_rate(sorted_values, line, _UNIFORM)

                    # Model probability uses experiment's recency weights
                    base_prob = _weighted_hit_rate(sorted_values, line, params)

                    if use_stat_space:
                        shifted_values = [v + stat_shift for v in sorted_values]
                        model_prob = _weighted_hit_rate(shifted_values, line, params)
                    else:
                        model_prob = max(0.01, min(0.99, base_prob + prob_shift))

                    outcome = 1.0 if actual_value > line else 0.0

                    all_predictions.append(model_prob)
                    all_outcomes.append(outcome)
                    all_market_prices.append(market_prob)

                    per_stat[stat_type]["preds"].append(model_prob)
                    per_stat[stat_type]["outs"].append(outcome)
                    per_stat[stat_type]["prices"].append(market_prob)

    if not all_predictions:
        return {
            "brier_score": 1.0,
            "calibration_error": 1.0,
            "expected_profit_pct": 0.0,
            "hit_rate": 0.0,
            "sample_size": 0,
            "trades_taken": 0,
            "per_stat": {},
            "_predictions": [],
            "_outcomes": [],
        }

    min_edge = params.get("BOOK_B_MIN_EDGE", 0.10)

    # Count how many trades the strategy would take
    trades = sum(
        1
        for mp, mkt in zip(all_predictions, all_market_prices)
        if abs(mp - mkt) >= min_edge
    )

    result: dict[str, Any] = {
        "brier_score": brier_score(all_predictions, all_outcomes),
        "calibration_error": calibration_error(all_predictions, all_outcomes),
        "expected_profit_pct": expected_profit(
            all_predictions, all_market_prices, all_outcomes, min_edge=min_edge
        ),
        "hit_rate": directional_accuracy(all_predictions, all_outcomes),
        "sample_size": len(all_predictions),
        "trades_taken": trades,
        "per_stat": {},
        "_predictions": all_predictions,
        "_outcomes": all_outcomes,
    }

    for st in stat_types:
        preds = per_stat[st]["preds"]
        outs = per_stat[st]["outs"]
        prices = per_stat[st]["prices"]
        if preds:
            result["per_stat"][st] = {
                "brier_score": brier_score(preds, outs),
                "sample_size": len(preds),
                "hit_rate": directional_accuracy(preds, outs),
                "expected_profit_pct": expected_profit(
                    preds, prices, outs, min_edge=min_edge
                ),
            }

    return result
