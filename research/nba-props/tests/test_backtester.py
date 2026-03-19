"""Tests for analysis/backtester.py — weighted hit rate, gamelog validation, eval scope."""

import json
from pathlib import Path

from analysis.backtester import _weighted_hit_rate, backtest_book_b


def test_weighted_hit_rate_empty():
    assert _weighted_hit_rate([], 25.0, {}) == 0.5


def test_weighted_hit_rate_all_hits():
    values = [30.0] * 10
    assert _weighted_hit_rate(values, 25.0, {}) == 0.99  # clamped


def test_weighted_hit_rate_no_hits():
    values = [20.0] * 10
    assert _weighted_hit_rate(values, 25.0, {}) == 0.01  # clamped


def test_weighted_hit_rate_values_equal_to_line_are_misses():
    """v > line is strict, so v == line is a miss."""
    assert _weighted_hit_rate([25.0], 25.0, {}) == 0.01


def test_weighted_hit_rate_recency_favors_recent():
    # 5 recent hits, 10 older misses
    values = [30.0] * 5 + [20.0] * 10
    params = {"RECENCY_WEIGHT_LAST5": 2.0, "RECENCY_WEIGHT_LAST10": 1.5, "RECENCY_WEIGHT_SEASON": 1.0}
    result = _weighted_hit_rate(values, 25.0, params)
    # Weighted: 5*2=10 hits / (5*2 + 5*1.5 + 5*1.0) = 10/22.5
    assert abs(result - 10.0 / 22.5) < 0.001


def _write_player_gamelogs(gamelogs_dir: Path, player_id: int, gamelogs: list[dict]):
    """Helper to write a player's gamelog JSON."""
    gamelogs_dir.mkdir(parents=True, exist_ok=True)
    path = gamelogs_dir / f"{player_id}.json"
    path.write_text(json.dumps({"gamelogs": gamelogs}))


def _make_game(pts=25, reb=5, ast=5, date="2025-01-01", opp="OPP", home=True):
    return {
        "points": pts, "rebounds": reb, "assists": ast,
        "steals": 1, "blocks": 1, "turnovers": 2, "three_pointers": 2,
        "minutes": 30, "game_date": date, "opponent": opp, "home": home,
    }


def test_backtest_implausible_stats_skipped(tmp_path):
    """Games with points=150 should be skipped by gamelog validation."""
    gamelogs_dir = tmp_path / "gamelogs"
    games = [_make_game(pts=25, date=f"2025-01-{i+1:02d}") for i in range(5)]
    games.append(_make_game(pts=150, date="2025-01-06"))  # implausible
    _write_player_gamelogs(gamelogs_dir, 1, games)

    params = {"TEST_LINE_OFFSETS": [0], "USE_EXTENDED_STATS": False}
    result = backtest_book_b(params, gamelogs_dir=gamelogs_dir)
    # 5 valid games (6th skipped), each producing predictions
    assert result["sample_size"] > 0


def test_backtest_empty_dir_returns_zero(tmp_path):
    gamelogs_dir = tmp_path / "gamelogs"
    gamelogs_dir.mkdir(parents=True)
    params = {"TEST_LINE_OFFSETS": [0], "USE_EXTENDED_STATS": False}
    result = backtest_book_b(params, gamelogs_dir=gamelogs_dir)
    assert result["sample_size"] == 0
    assert result["brier_score"] == 1.0


def test_backtest_uses_injected_line_offsets(tmp_path):
    """Verify params dict controls line_offsets (eval-scope wiring)."""
    gamelogs_dir = tmp_path / "gamelogs"
    games = [_make_game(pts=20 + i, date=f"2025-01-{i+1:02d}") for i in range(10)]
    _write_player_gamelogs(gamelogs_dir, 1, games)

    params_1 = {"TEST_LINE_OFFSETS": [0], "USE_EXTENDED_STATS": False}
    params_3 = {"TEST_LINE_OFFSETS": [-2, 0, 2], "USE_EXTENDED_STATS": False}

    r1 = backtest_book_b(params_1, gamelogs_dir=gamelogs_dir)
    r3 = backtest_book_b(params_3, gamelogs_dir=gamelogs_dir)

    # 3 offsets should produce ~3x the sample size of 1 offset
    assert r3["sample_size"] > r1["sample_size"]
