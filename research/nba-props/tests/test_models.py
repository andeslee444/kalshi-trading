from oracle.models import (
    Book, Signal, Position, Game, Market, Player, PlayerSplits,
    StatTracker, GameState, QuoteSnapshot,
)

def test_signal_creation():
    s = Signal(
        book=Book.A,
        ticker="KXNBAGAME-26MAR18LALHOU-LAL",
        side="yes",
        edge=0.15,
        model_prob=0.72,
        kalshi_price=0.57,
    )
    assert s.book == Book.A
    assert s.edge == 0.15
    assert s.side == "yes"

def test_signal_rejects_negative_edge():
    import pytest
    with pytest.raises(ValueError):
        Signal(book=Book.B, ticker="X", side="yes", edge=-0.05,
               model_prob=0.5, kalshi_price=0.55)

def test_position_cost():
    p = Position(
        book=Book.B,
        ticker="KXNBAPTS-26MAR18-LALLEBRONJ-27",
        side="yes",
        contracts=10,
        fill_price=0.63,
    )
    assert p.cost == 6.30  # 10 * 0.63

def test_game_state_classification():
    assert GameState.classify(margin=5, period="Q3", clock_seconds=300) == GameState.COMPETITIVE
    assert GameState.classify(margin=20, period="Q3", clock_seconds=300) == GameState.BLOWOUT
    assert GameState.classify(margin=3, period="Q4", clock_seconds=120) == GameState.CLUTCH
    assert GameState.classify(margin=0, period="Q4", clock_seconds=90) == GameState.OT_LIKELY

def test_player_splits_hit_rate():
    splits = PlayerSplits(
        last_5=[28, 32, 22, 35, 30],
        last_10=[28, 32, 22, 35, 30, 24, 19, 31, 27, 26],
        season_avg=26.5,
        home_avg=28.0,
        away_avg=25.0,
    )
    assert splits.hit_rate(line=27.5, n=5) == 0.8  # 4 of 5 cleared (28,32,35,30)
    assert splits.hit_rate(line=27.5, n=10) == 0.5  # 5 of 10 cleared
