"""Tests for Book B: pregame player props."""

import pytest

import domain.oracle.books.book_b as book_b_module
from domain.oracle.books.book_b import (
    weighted_hit_rate,
    matchup_shift,
    venue_shift,
    b2b_penalty,
    pace_factor,
    teammate_out_shift,
    external_odds_blend,
    is_within_no_trade_window,
    compute_adjusted_prob,
    evaluate_prop,
    prop_to_signal,
)
from domain.oracle.models import Book, PlayerSplits


@pytest.fixture(autouse=True)
def _use_default_book_b_calibration(tmp_path, monkeypatch):
    monkeypatch.setattr(book_b_module, "_ORACLE_CAL_PATH", tmp_path / "oracle-calibration.json")
    book_b_module._reset_oracle_calibration()
    yield
    book_b_module._reset_oracle_calibration()


def _make_splits(**kwargs):
    defaults = {
        "last_5": [28, 32, 22, 35, 30],
        "last_10": [28, 32, 22, 35, 30, 24, 19, 31, 27, 26],
        "season_avg": 26.5,
        "home_avg": 28.0,
        "away_avg": 25.0,
    }
    defaults.update(kwargs)
    return PlayerSplits(**defaults)


def test_weighted_hit_rate_above_line():
    splits = _make_splits()
    hr = weighted_hit_rate(splits, line=25.0)
    # 4/5 recent games above 25: 0.8 * 0.6 = 0.48
    # season_avg=26.5 > 25: season component > 0.5
    assert hr > 0.5


def test_weighted_hit_rate_below_line():
    splits = _make_splits()
    hr = weighted_hit_rate(splits, line=35.0)
    # Only 1/5 above 35: 0.2 * 0.6 = 0.12
    assert hr < 0.4


def test_matchup_shift_favorable():
    splits = _make_splits(per_opponent={"HOU": 32.0})
    shift = matchup_shift(splits, "HOU", line=27.0)
    assert shift > 0  # performs better vs HOU


def test_matchup_shift_unfavorable():
    splits = _make_splits(per_opponent={"MIA": 20.0})
    shift = matchup_shift(splits, "MIA", line=27.0)
    assert shift < 0  # performs worse vs MIA


def test_matchup_shift_no_data():
    splits = _make_splits()
    shift = matchup_shift(splits, "UNK", line=27.0)
    assert shift == 0.0


def test_venue_shift_home():
    splits = _make_splits(home_avg=30.0, season_avg=26.5)
    shift = venue_shift(splits, is_home=True)
    assert shift > 0


def test_venue_shift_away():
    splits = _make_splits(away_avg=22.0, season_avg=26.5)
    shift = venue_shift(splits, is_home=False)
    assert shift < 0


def test_b2b_penalty_yes():
    assert b2b_penalty(True) == -0.05


def test_b2b_penalty_no():
    assert b2b_penalty(False) == 0.0


def test_pace_factor_fast():
    shift = pace_factor(team_pace=108, league_avg_pace=100)
    assert shift > 0


def test_pace_factor_slow():
    shift = pace_factor(team_pace=92, league_avg_pace=100)
    assert shift < 0


def test_teammate_out_shift():
    # Points: +3% (more usage when teammate out)
    assert teammate_out_shift(True, stat_type="points") == 0.03
    assert teammate_out_shift(True, stat_type="pts") == 0.03
    # Assists: -2% (fewer assist opportunities)
    assert teammate_out_shift(True, stat_type="assists") == -0.02
    assert teammate_out_shift(True, stat_type="ast") == -0.02
    # Other stats: +1.5%
    assert teammate_out_shift(True, stat_type="rebounds") == 0.015
    assert teammate_out_shift(True, stat_type="") == 0.015
    # Not out: always 0
    assert teammate_out_shift(False) == 0.0
    assert teammate_out_shift(False, stat_type="points") == 0.0


def test_compute_adjusted_prob():
    splits = _make_splits()
    prob = compute_adjusted_prob(splits, line=25.0, is_home=True)
    assert 0.01 <= prob <= 0.99


def test_compute_adjusted_prob_bounded():
    splits = _make_splits(last_5=[50, 50, 50, 50, 50], season_avg=50)
    prob = compute_adjusted_prob(splits, line=10.0)
    assert prob <= 0.99

    prob2 = compute_adjusted_prob(splits, line=100.0)
    assert prob2 >= 0.01


def test_evaluate_prop_above_threshold():
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O25",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=25.0,
        direction="over",
        kalshi_price_cents=40,  # underpriced
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=35.0,
    )
    assert opp is not None
    assert opp.edge >= 0.10
    assert opp.direction == "over"


def test_evaluate_prop_below_threshold():
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O25",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=25.0,
        direction="over",
        kalshi_price_cents=70,  # fairly priced
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=35.0,
    )
    assert opp is None


def test_evaluate_prop_low_minutes():
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O10",
        player_name="Bench Player",
        player_id=999,
        team="LAL",
        stat_type="points",
        line=10.0,
        direction="over",
        kalshi_price_cents=30,
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=8.0,  # below 20 min threshold
        min_minutes_avg=20.0,
    )
    assert opp is None  # filtered by minutes gate


def test_prop_to_signal():
    from domain.oracle.books.book_b import PropOpportunity
    opp = PropOpportunity(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O27",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=27.0,
        direction="over",
        model_prob=0.72,
        kalshi_price_cents=60,
        edge=0.12,
    )
    signal = prop_to_signal(opp)
    assert signal.book == Book.B
    assert signal.side == "yes"
    assert signal.edge == 0.12
    assert signal.metadata["player_name"] == "LeBron James"


# ── External odds blend ──

def test_external_odds_blend_shifts_prob():
    # Model says 0.70, external says 0.60, blend should pull down
    blended = external_odds_blend(0.70, 0.60, blend_weight=0.2)
    assert 0.60 < blended < 0.70
    assert abs(blended - 0.68) < 0.01  # 0.8*0.7 + 0.2*0.6 = 0.68


def test_external_odds_blend_invalid_external():
    # Invalid external prob should return model unchanged
    assert external_odds_blend(0.70, 0.0) == 0.70
    assert external_odds_blend(0.70, 1.0) == 0.70
    assert external_odds_blend(0.70, -0.5) == 0.70


def test_external_odds_blend_bounded():
    blended = external_odds_blend(0.99, 0.99, blend_weight=0.5)
    assert blended <= 0.99
    blended = external_odds_blend(0.01, 0.01, blend_weight=0.5)
    assert blended >= 0.01


# ── No-trade window ──

def test_no_trade_window_inside():
    assert is_within_no_trade_window(10.0, no_trade_window_min=15.0)
    assert is_within_no_trade_window(0.0, no_trade_window_min=15.0)
    assert is_within_no_trade_window(14.9, no_trade_window_min=15.0)


def test_no_trade_window_outside():
    assert not is_within_no_trade_window(15.0, no_trade_window_min=15.0)
    assert not is_within_no_trade_window(60.0, no_trade_window_min=15.0)
    assert not is_within_no_trade_window(-1.0, no_trade_window_min=15.0)


def test_evaluate_prop_no_trade_window_blocks():
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O25",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=25.0,
        direction="over",
        kalshi_price_cents=40,
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=35.0,
        minutes_to_start=10.0,  # 10 min to start, within 15 min window
        no_trade_window_min=15.0,
    )
    assert opp is None


def test_evaluate_prop_with_external_odds():
    splits = _make_splits()
    # Without external: edge should be large enough
    opp_no_ext = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O25",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=25.0,
        direction="over",
        kalshi_price_cents=40,
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=35.0,
    )
    # With external odds pulling prob down
    opp_ext = evaluate_prop(
        ticker="KXNBAPTS-18MAR26-LALJAMESL-O25",
        player_name="LeBron James",
        player_id=2544,
        team="LAL",
        stat_type="points",
        line=25.0,
        direction="over",
        kalshi_price_cents=40,
        splits=splits,
        min_edge=0.10,
        minutes_avg_last5=35.0,
        ext_prob=0.45,  # external says lower
        ext_blend_weight=0.2,
    )
    assert opp_no_ext is not None, "Expected opportunity without external odds"
    assert opp_ext is not None, "Expected opportunity with external odds"
    assert opp_ext.model_prob <= opp_no_ext.model_prob


# ── Lineup gating tests (spec Section 4) ──

def test_questionable_player_blocked():
    """Questionable player status → NO TRADE (spec rule 1)."""
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="T1", player_name="X", player_id=1, team="LAL",
        stat_type="points", line=25.0, direction="over",
        kalshi_price_cents=40, splits=splits, min_edge=0.10,
        minutes_avg_last5=35.0,
        player_status="Questionable",
    )
    assert opp is None


def test_key_teammate_questionable_blocked():
    """Key teammate Questionable → NO TRADE (spec rule 2)."""
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="T1", player_name="X", player_id=1, team="LAL",
        stat_type="points", line=25.0, direction="over",
        kalshi_price_cents=40, splits=splits, min_edge=0.10,
        minutes_avg_last5=35.0,
        key_teammate_questionable=True,
    )
    assert opp is None


def test_active_player_allowed():
    """Active player passes lineup gates."""
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="T1", player_name="X", player_id=1, team="LAL",
        stat_type="points", line=25.0, direction="over",
        kalshi_price_cents=40, splits=splits, min_edge=0.10,
        minutes_avg_last5=35.0,
        player_status="Active",
    )
    assert opp is not None


def test_rotation_player_sizing_reduction():
    """Rotation player (20-28 min avg) gets 50% sizing multiplier (spec rule 5)."""
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="T1", player_name="X", player_id=1, team="LAL",
        stat_type="points", line=25.0, direction="over",
        kalshi_price_cents=40, splits=splits, min_edge=0.10,
        minutes_avg_last5=24.0,  # rotation player: 20-28 range
        player_status="Active",
    )
    assert opp is not None
    assert opp.metadata["is_rotation_player"] is True
    assert opp.metadata["sizing_multiplier"] == 0.5


def test_starter_full_sizing():
    """Starter (>28 min avg) gets full sizing."""
    splits = _make_splits()
    opp = evaluate_prop(
        ticker="T1", player_name="X", player_id=1, team="LAL",
        stat_type="points", line=25.0, direction="over",
        kalshi_price_cents=40, splits=splits, min_edge=0.10,
        minutes_avg_last5=35.0,  # starter
        player_status="Active",
    )
    assert opp is not None
    assert opp.metadata["is_rotation_player"] is False
    assert opp.metadata["sizing_multiplier"] == 1.0
