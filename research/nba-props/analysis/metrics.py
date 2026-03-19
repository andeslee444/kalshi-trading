"""Evaluation metrics for sports research experiments.

Scoring functions used by the backtest harness to evaluate parameter
configurations against historical game data. All metrics work on
parallel lists of (predicted_probability, actual_outcome) pairs.
"""

from __future__ import annotations

import math


def brier_score(predictions: list[float], outcomes: list[float]) -> float:
    """Brier score: mean((predicted - actual)^2).

    Lower is better. Range [0, 1].
    Perfect: 0.0, coin flip at 0.5: 0.25, worst: 1.0.
    """
    if not predictions:
        return 1.0
    n = len(predictions)
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / n


def calibration_error(
    predictions: list[float],
    outcomes: list[float],
    n_bins: int = 10,
) -> float:
    """Expected Calibration Error (ECE).

    Bins predictions into equal-width buckets [0-0.1, 0.1-0.2, ...] and
    measures |actual_rate - mean_predicted| weighted by bin count.
    Lower is better. Range [0, 1].
    """
    if not predictions:
        return 1.0

    bins = [[] for _ in range(n_bins)]
    bin_outcomes = [[] for _ in range(n_bins)]

    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append(p)
        bin_outcomes[idx].append(o)

    total = len(predictions)
    ece = 0.0
    for preds, outs in zip(bins, bin_outcomes):
        if not preds:
            continue
        avg_pred = sum(preds) / len(preds)
        avg_out = sum(outs) / len(outs)
        ece += len(preds) / total * abs(avg_out - avg_pred)

    return ece


def expected_profit(
    predictions: list[float],
    market_prices: list[float],
    outcomes: list[float],
    min_edge: float = 0.10,
    fee_rate: float = 0.07,
) -> float:
    """Simulated profit percentage from edge-trading.

    For each prediction:
      - If model_prob > market_price + min_edge: buy YES at market_price
      - If model_prob < market_price - min_edge: buy NO at (1 - market_price)
      - Track profit/loss with Kalshi 7% fee-on-profit structure

    Returns profit as percentage of total capital risked.
    """
    if not predictions:
        return 0.0

    total_risked = 0.0
    total_profit = 0.0

    for model_prob, market_price, outcome in zip(predictions, market_prices, outcomes):
        edge_yes = model_prob - market_price
        edge_no = (1 - model_prob) - (1 - market_price)

        if edge_yes >= min_edge:
            cost = market_price
            if outcome > 0.5:
                gross = 1.0 - cost
                total_profit += gross - gross * fee_rate
            else:
                total_profit -= cost
            total_risked += cost

        elif edge_no >= min_edge:
            cost = 1.0 - market_price
            if outcome < 0.5:
                gross = 1.0 - cost
                total_profit += gross - gross * fee_rate
            else:
                total_profit -= cost
            total_risked += cost

    if total_risked <= 0:
        return 0.0
    return (total_profit / total_risked) * 100


def log_loss(predictions: list[float], outcomes: list[float]) -> float:
    """Log loss: -mean(o*log(p) + (1-o)*log(1-p)).

    Lower is better. Heavily penalizes confident wrong predictions.
    """
    if not predictions:
        return float("inf")

    eps = 1e-15
    n = len(predictions)
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = max(eps, min(1 - eps, p))
        total += o * math.log(p) + (1 - o) * math.log(1 - p)
    return -total / n


def directional_accuracy(predictions: list[float], outcomes: list[float]) -> float:
    """How often the model's directional call was correct.

    Correct when: p > 0.5 and outcome=1, or p < 0.5 and outcome=0.
    """
    if not predictions:
        return 0.0
    correct = sum(
        1
        for p, o in zip(predictions, outcomes)
        if (p > 0.5 and o > 0.5) or (p < 0.5 and o < 0.5)
    )
    # Exclude ties (p == 0.5) from denominator — they carry no directional info
    non_ties = sum(1 for p in predictions if p != 0.5)
    return correct / non_ties if non_ties > 0 else 0.5


def calibration_curve(
    predictions: list[float],
    outcomes: list[float],
    n_bins: int = 10,
) -> tuple[list[float], list[float], list[float], list[int]]:
    """Calibration curve data for plotting.

    Returns (bin_centers, actual_rates, predicted_rates, bin_sizes).
    A well-calibrated model has actual_rates tracking predicted_rates.
    """
    bins_preds = [[] for _ in range(n_bins)]
    bins_outs = [[] for _ in range(n_bins)]

    for p, o in zip(predictions, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins_preds[idx].append(p)
        bins_outs[idx].append(o)

    centers = []
    actual_rates = []
    predicted_rates = []
    bin_sizes = []

    for i in range(n_bins):
        center = (i + 0.5) / n_bins
        if bins_preds[i]:
            centers.append(center)
            predicted_rates.append(sum(bins_preds[i]) / len(bins_preds[i]))
            actual_rates.append(sum(bins_outs[i]) / len(bins_outs[i]))
            bin_sizes.append(len(bins_preds[i]))

    return centers, actual_rates, predicted_rates, bin_sizes


def bootstrap_brier_ci(
    predictions: list[float],
    outcomes: list[float],
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> tuple[float | None, float | None]:
    """Bootstrap 95% confidence interval for Brier score.

    Resamples (prediction, outcome) pairs with replacement and computes
    Brier score on each resample. Returns (lower, upper) bounds.
    Returns (None, None) if insufficient data.
    """
    import random

    n = len(predictions)
    if n < 10:
        return None, None

    scores = []
    for _ in range(n_bootstrap):
        indices = [random.randint(0, n - 1) for _ in range(n)]
        resampled_p = [predictions[i] for i in indices]
        resampled_o = [outcomes[i] for i in indices]
        scores.append(brier_score(resampled_p, resampled_o))

    scores.sort()
    alpha = (1 - confidence) / 2
    lo_idx = int(alpha * n_bootstrap)
    hi_idx = int((1 - alpha) * n_bootstrap) - 1
    return scores[lo_idx], scores[hi_idx]
