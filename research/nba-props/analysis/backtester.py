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
from math import erf, sqrt
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


def _compute_rest_days(game_date: str, other_dates: list[str]) -> int:
    """Compute days of rest before this game. 0 = back-to-back, 1 = one day off, etc."""
    try:
        gd = datetime.strptime(game_date, "%Y-%m-%d")
    except ValueError:
        return 2
    prior = []
    for d in other_dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt < gd:
                prior.append(dt)
        except ValueError:
            continue
    if not prior:
        return 2
    last_game = max(prior)
    return (gd - last_game).days - 1


def _stat_param(params: dict, base_key: str, stat_type: str, default: float) -> float:
    """Look up per-stat param (e.g. MATCHUP_MULTIPLIER_POINTS), falling back to base key."""
    suffix = stat_type.upper().replace("+", "_")
    return params.get(f"{base_key}_{suffix}", params.get(base_key, default))


def _minutes_weight_factor(minutes: float, avg_minutes: float, params: dict) -> float:
    """Compute multiplicative weight factor based on minutes played."""
    factor = params.get("MINUTES_WEIGHT_FACTOR", 0.0)
    if factor <= 0 or avg_minutes <= 0:
        return 1.0
    ratio = minutes / avg_minutes
    adjusted = 1.0 + factor * (ratio - 1.0)
    return max(0.3, min(2.0, adjusted))


def _recency_weights(n: int, params: dict) -> list[float]:
    """Build recency weight vector, optionally adjusted by minutes played."""
    w5 = params.get("RECENCY_WEIGHT_LAST5", 2.0)
    w10 = params.get("RECENCY_WEIGHT_LAST10", 1.5)
    ws = params.get("RECENCY_WEIGHT_SEASON", 1.0)
    weights = [w5 if i < 5 else w10 if i < 10 else ws for i in range(n)]
    minutes_list = params.get("_minutes_list")
    avg_minutes = params.get("_avg_minutes", 0.0)
    if minutes_list and avg_minutes > 0:
        weights = [w * _minutes_weight_factor(m, avg_minutes, params)
                   for w, m in zip(weights, minutes_list)]
    return weights


def _weighted_hit_rate(values: list[float], line: float, params: dict) -> float:
    """Recency-weighted hit rate using experiment params.

    Values must be sorted most-recent-first (by game date descending).
    Mirrors Book B's weighted_hit_rate logic with parameterized weights.
    """
    if not values:
        return 0.5

    weights = _recency_weights(len(values), params)

    hits = sum(w * (1.0 if v > line else 0.0) for w, v in zip(weights, values))
    total = sum(weights)
    if total <= 0:
        return 0.5

    prior = params.get("BAYESIAN_PRIOR_STRENGTH", 0.0)
    if prior > 0:
        alpha = prior / 2.0
        n = len(values)
        raw_rate = hits / total
        posterior = (alpha + raw_rate * n) / (2 * alpha + n)
        return max(0.01, min(0.99, posterior))
    return max(0.01, min(0.99, hits / total))


def _kernel_prob(values: list[float], line: float, params: dict) -> float:
    """Gaussian-kernel probability estimate that value exceeds line.

    Uses Silverman's rule for bandwidth with sample standard deviation.
    """
    if not values:
        return 0.5

    sigma = params.get("FALLBACK_SIGMA", 0.15)
    weights = _recency_weights(len(values), params)
    total_w = sum(weights)
    if total_w <= 0:
        return 0.5

    mean_v = sum(values) / len(values)
    data_std = (sum((v - mean_v) ** 2 for v in values) / max(1, len(values) - 1)) ** 0.5
    if data_std <= 0:
        data_std = max(line * 0.1, 0.5)
    bandwidth = sigma * data_std * max(1, len(values)) ** (-0.2)

    prob = 0.0
    for v, w in zip(values, weights):
        z = (line - v) / bandwidth
        cdf_val = 0.5 * (1.0 + erf(z / sqrt(2.0)))
        prob += w * (1.0 - cdf_val)
    return max(0.01, min(0.99, prob / total_w))


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
        mult = _stat_param(params, "MATCHUP_MULTIPLIER", stat_type, 0.15)
        cap = _stat_param(params, "MATCHUP_CAP", stat_type, 0.10)
        shift = (opp_avg / season_avg - 1.0) * mult
        total_shift += max(-cap, min(cap, shift))

    # Venue: (venue_avg / season_avg - 1.0) * multiplier, clamped
    is_home = held_out.get("home", True)
    venue_games = [g for g in training if g.get("home") == is_home]
    if venue_games:
        venue_avg = _mean([_extract_stat(g, stat_type) for g in venue_games])
        mult = _stat_param(params, "VENUE_MULTIPLIER", stat_type, 0.10)
        cap = _stat_param(params, "VENUE_CAP", stat_type, 0.05)
        shift = (venue_avg / season_avg - 1.0) * mult
        total_shift += max(-cap, min(cap, shift))

    # Graduated rest: B2B penalty or extended-rest boost
    other_dates = [g.get("game_date", "") for g in training]
    rest = _compute_rest_days(held_out.get("game_date", ""), other_dates)
    if rest == 0:
        total_shift += _stat_param(params, "B2B_PENALTY", stat_type, -0.05)
    elif rest >= 3:
        total_shift += _stat_param(params, "REST_BOOST", stat_type, 0.0)

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
        mult = _stat_param(params, "MATCHUP_MULTIPLIER", stat_type, 0.15)
        total_shift += (opp_avg - season_avg) * mult

    # Venue: raw difference scaled by multiplier
    is_home = held_out.get("home", True)
    venue_games = [g for g in training if g.get("home") == is_home]
    if venue_games:
        venue_avg = _mean([_extract_stat(g, stat_type) for g in venue_games])
        mult = _stat_param(params, "VENUE_MULTIPLIER", stat_type, 0.10)
        total_shift += (venue_avg - season_avg) * mult

    # Graduated rest: B2B penalty or extended-rest boost (in stat units)
    other_dates = [g.get("game_date", "") for g in training]
    rest = _compute_rest_days(held_out.get("game_date", ""), other_dates)
    if rest == 0:
        b2b = _stat_param(params, "B2B_PENALTY", stat_type, -0.05)
        total_shift += b2b * season_avg
    elif rest >= 3:
        boost = _stat_param(params, "REST_BOOST", stat_type, 0.0)
        total_shift += boost * season_avg

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

    # Eval-scope params — read from params dict (injected by harness).
    line_offsets = params.get("TEST_LINE_OFFSETS", [-2, 0, 2])
    stat_types = (STAT_TYPES_CORE + STAT_TYPES_EXTENDED) if params.get("USE_EXTENDED_STATS", True) else STAT_TYPES_CORE
    min_games = 5

    use_stat_space = params.get("USE_STAT_SPACE", False)
    use_kde = params.get("KDE_MODE", False)

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

                sorted_minutes = [g.get("minutes", 0.0) for g in sorted_training]
                avg_minutes = _mean(sorted_minutes) if sorted_minutes else 0.0

                params_with_minutes = dict(params)
                params_with_minutes["_minutes_list"] = sorted_minutes
                params_with_minutes["_avg_minutes"] = avg_minutes

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
                    _prob_fn = _kernel_prob if use_kde else _weighted_hit_rate
                    market_prob = _weighted_hit_rate(sorted_values, line, _UNIFORM)

                    if use_stat_space:
                        shifted_values = [v + stat_shift for v in sorted_values]
                        model_prob = _prob_fn(shifted_values, line, params_with_minutes)
                    else:
                        base_prob = _prob_fn(sorted_values, line, params_with_minutes)
                        model_prob = max(0.01, min(0.99, base_prob + prob_shift))

                    cv_shrink = params.get("MINUTES_CV_SHRINK", 0.0)
                    if cv_shrink > 0 and len(sorted_minutes) > 1:
                        min_mean = _mean(sorted_minutes)
                        if min_mean > 0:
                            min_var = sum((m - min_mean) ** 2 for m in sorted_minutes) / len(sorted_minutes)
                            cv = min_var ** 0.5 / min_mean
                            shrink_factor = max(0.0, 1.0 - cv_shrink * cv)
                            model_prob = 0.5 + (model_prob - 0.5) * shrink_factor
                            model_prob = max(0.01, min(0.99, model_prob))

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
