"""Tests for Oracle domain models (ported from sportsmarket + additions)."""

import pytest
import time

from domain.oracle.models import (
    Book, Signal, Position, Game, Market, Player, PlayerSplits,
    StatTracker, GameState, QuoteSnapshot, OraclePosition,
)


def test_signal_creation():
    s = Signal(
        book=Book.A,
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        edge=0.15,
        model_prob=0.72,
        kalshi_price=0.57,
    )
    assert s.book == Book.A
    assert s.edge == 0.15
    assert s.side == "yes"


def test_signal_rejects_negative_edge():
    with pytest.raises(ValueError):
        Signal(book=Book.B, ticker="X", side="yes", edge=-0.05,
               model_prob=0.5, kalshi_price=0.55)


def test_signal_rejects_invalid_side():
    with pytest.raises(ValueError):
        Signal(book=Book.A, ticker="X", side="buy", edge=0.10,
               model_prob=0.5, kalshi_price=0.40)


def test_position_cost():
    p = Position(
        book=Book.B,
        ticker="KXNBAPTS-18MAR26-LALLEBRONJ-O27",
        side="yes",
        contracts=10,
        fill_price=0.63,
    )
    assert p.cost == 6.30


def test_game_state_classification():
    assert GameState.classify(margin=5, period="Q3", clock_seconds=300) == GameState.COMPETITIVE
    assert GameState.classify(margin=20, period="Q3", clock_seconds=300) == GameState.BLOWOUT
    assert GameState.classify(margin=3, period="Q4", clock_seconds=120) == GameState.CLUTCH
    assert GameState.classify(margin=0, period="Q4", clock_seconds=90) == GameState.OT_LIKELY


def test_game_state_blowout_q4():
    assert GameState.classify(margin=25, period="Q4", clock_seconds=400) == GameState.BLOWOUT


def test_game_state_competitive_q2():
    assert GameState.classify(margin=30, period="Q2", clock_seconds=500) == GameState.COMPETITIVE


def test_player_splits_hit_rate():
    splits = PlayerSplits(
        last_5=[28, 32, 22, 35, 30],
        last_10=[28, 32, 22, 35, 30, 24, 19, 31, 27, 26],
        season_avg=26.5,
        home_avg=28.0,
        away_avg=25.0,
    )
    assert splits.hit_rate(line=27.5, n=5) == 0.8  # 4 of 5 cleared
    assert splits.hit_rate(line=27.5, n=10) == 0.5  # 5 of 10 cleared


def test_player_splits_hit_rate_empty():
    splits = PlayerSplits(last_5=[], last_10=[], season_avg=0, home_avg=0, away_avg=0)
    assert splits.hit_rate(line=10, n=5) == 0.0


def test_quote_snapshot_spread():
    q = QuoteSnapshot(ticker="X", yes_bid=45, yes_ask=50, bid_depth=10, ask_depth=8)
    assert q.spread == 5


def test_quote_snapshot_age():
    q = QuoteSnapshot(
        ticker="X", yes_bid=45, yes_ask=50, bid_depth=10, ask_depth=8,
        timestamp=time.time() - 10,
    )
    assert q.age_seconds >= 9.0


def test_oracle_position_exposure():
    op = OraclePosition(
        book=Book.A, ticker="T1", side="yes",
        contracts=10, entry_price_cents=50,
    )
    assert op.exposure_cents == 500


def test_oracle_position_roundtrip():
    op = OraclePosition(
        book=Book.B, ticker="T2", side="no",
        contracts=5, entry_price_cents=30,
        game_id="LAL-HOU-20260318", player_id=42,
        timestamp=1234567890.0,
    )
    d = op.to_dict()
    restored = OraclePosition.from_dict(d)
    assert restored.book == Book.B
    assert restored.ticker == "T2"
    assert restored.contracts == 5
    assert restored.game_id == "LAL-HOU-20260318"
    assert restored.player_id == 42


def test_book_enum_values():
    assert Book.A.value == "game_divergence"
    assert Book.B.value == "pregame_props"
    assert Book.C.value == "live_events"


def test_market_creation():
    m = Market(
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        series_ticker="KXNBA-18MAR26-LALHOU",
        title="Lakers vs Rockets - Lakers win",
        yes_price=55,
        no_price=45,
    )
    assert m.yes_price == 55
    assert m.status == "open"
