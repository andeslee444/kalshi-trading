"""Tests for analysis/empirical_cdf.py — median, hit rate, empty input."""

from analysis.empirical_cdf import build_empirical_cdf


def test_median_even_count():
    result = build_empirical_cdf([10.0, 20.0, 30.0, 40.0], line=25.0)
    assert result["median"] == 25.0  # (20+30)/2


def test_median_odd_count():
    result = build_empirical_cdf([10.0, 20.0, 30.0], line=25.0)
    assert result["median"] == 20.0


def test_empty_returns_zero():
    result = build_empirical_cdf([], line=25.0)
    assert result["n"] == 0
    assert result["hit_rate"] == 0.0


def test_hit_rate_strict_greater_than():
    """Values equal to line are misses (strict >)."""
    result = build_empirical_cdf([25.0, 25.0, 30.0], line=25.0)
    assert abs(result["hit_rate"] - 1 / 3) < 0.001
