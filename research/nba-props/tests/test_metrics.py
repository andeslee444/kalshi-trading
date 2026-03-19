"""Tests for analysis/metrics.py — Brier score, ECE, expected profit, bootstrap CI."""

from analysis.metrics import (
    brier_score,
    calibration_error,
    directional_accuracy,
    expected_profit,
    bootstrap_brier_ci,
)


def test_brier_score_perfect():
    assert brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == 0.0


def test_brier_score_worst():
    assert brier_score([0.0, 1.0], [1.0, 0.0]) == 1.0


def test_brier_score_coin_flip():
    assert brier_score([0.5, 0.5], [1.0, 0.0]) == 0.25


def test_brier_score_empty():
    assert brier_score([], []) == 1.0


def test_expected_profit_no_edge_no_trades():
    assert expected_profit([0.55], [0.50], [1.0], min_edge=0.10) == 0.0


def test_expected_profit_winning_yes_trade():
    profit = expected_profit([0.70], [0.50], [1.0], min_edge=0.10, fee_rate=0.07)
    # cost=0.50, gross=0.50, net=0.50*0.93=0.465, pct=(0.465/0.50)*100=93.0
    assert abs(profit - 93.0) < 0.1


def test_expected_profit_losing_yes_trade():
    profit = expected_profit([0.70], [0.50], [0.0], min_edge=0.10, fee_rate=0.07)
    # cost=0.50, loss=-0.50, pct=(-0.50/0.50)*100=-100.0
    assert abs(profit - (-100.0)) < 0.1


def test_calibration_error_empty():
    assert calibration_error([], []) == 1.0


def test_directional_accuracy_handles_ties():
    # p=0.5 is excluded from denominator, only p=0.8 counts (correct)
    assert directional_accuracy([0.5, 0.8], [1.0, 1.0]) == 1.0


def test_directional_accuracy_empty():
    assert directional_accuracy([], []) == 0.0


def test_bootstrap_ci_returns_none_for_small_samples():
    assert bootstrap_brier_ci([0.5] * 5, [1.0] * 5) == (None, None)


def test_bootstrap_ci_bounded_zero_one():
    lo, hi = bootstrap_brier_ci([0.5] * 100, [1.0] * 50 + [0.0] * 50)
    assert lo is not None and hi is not None
    assert 0.0 <= lo <= hi <= 1.0


def test_bootstrap_ci_widens_with_offsets():
    import random
    rng = random.Random(99)
    preds = [rng.uniform(0.3, 0.7) for _ in range(200)]
    outs = [1.0 if rng.random() > 0.5 else 0.0 for _ in range(200)]
    lo1, hi1 = bootstrap_brier_ci(preds, outs, n_offsets=1)
    lo3, hi3 = bootstrap_brier_ci(preds, outs, n_offsets=3)
    assert (hi3 - lo3) > (hi1 - lo1)


def test_bootstrap_ci_deterministic():
    preds = [0.5] * 100
    outs = [1.0] * 50 + [0.0] * 50
    r1 = bootstrap_brier_ci(preds, outs)
    r2 = bootstrap_brier_ci(preds, outs)
    assert r1 == r2
