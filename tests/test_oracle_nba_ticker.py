"""Tests for Oracle NBA ticker parsing utilities."""

import datetime

from domain.oracle.nba_ticker_utils import (
    parse_nba_ticker,
    make_game_ticker,
    make_prop_ticker,
    extract_game_id,
    extract_team,
)


def test_parse_game_ticker():
    result = parse_nba_ticker("KXNBA-18MAR26-LALHOU-LAL")
    assert result is not None
    assert result["type"] == "game"
    assert result["home"] == "LAL"
    assert result["away"] == "HOU"
    assert result["pick"] == "LAL"
    assert result["date"] == "2026-03-18"


def test_parse_game_ticker_away_pick():
    result = parse_nba_ticker("KXNBA-18MAR26-LALHOU-HOU")
    assert result is not None
    assert result["pick"] == "HOU"


def test_parse_live_game_ticker():
    result = parse_nba_ticker("KXNBAGAME-26MAR21GSWATL-GSW")
    assert result is not None
    assert result["type"] == "game"
    assert result["away"] == "GSW"
    assert result["home"] == "ATL"
    assert result["pick"] == "GSW"
    assert result["date"] == "2026-03-21"


def test_parse_prop_ticker_points():
    result = parse_nba_ticker("KXNBAPTS-18MAR26-LALJAMESL-O27")
    assert result is not None
    assert result["type"] == "prop"
    assert result["stat"] == "points"
    assert result["team"] == "LAL"
    assert result["player_code"] == "JAMESL"
    assert result["direction"] == "over"
    assert result["line"] == 27.0


def test_parse_prop_ticker_rebounds():
    result = parse_nba_ticker("KXNBAREB-18MAR26-LALDAVISA-O10")
    assert result is not None
    assert result["stat"] == "rebounds"
    assert result["direction"] == "over"
    assert result["line"] == 10.0


def test_parse_prop_ticker_under():
    result = parse_nba_ticker("KXNBAPTS-18MAR26-BOSTATTUMJ-U25")
    assert result is not None
    assert result["direction"] == "under"
    assert result["team"] == "BOS"
    assert result["line"] == 25.0


def test_parse_prop_ticker_three_pointers():
    result = parse_nba_ticker("KXNBA3PM-18MAR26-GSWCURRYS-O4")
    assert result is not None
    assert result["stat"] == "three_pointers"
    assert result["team"] == "GSW"


def test_parse_live_prop_ticker_points():
    result = parse_nba_ticker("KXNBAPTS-26MAR29LACMIL-LACBLOPEZ11-10")
    assert result is not None
    assert result["type"] == "prop"
    assert result["stat"] == "points"
    assert result["date"] == "2026-03-29"
    assert result["away"] == "LAC"
    assert result["home"] == "MIL"
    assert result["team"] == "LAC"
    assert result["player_code"] == "BLOPEZ11"
    assert result["player_token"] == "BLOPEZ"
    assert result["jersey_number"] == 11
    assert result["line"] == 10.0


def test_parse_invalid_ticker():
    assert parse_nba_ticker("NOTAVALIDTICKER") is None
    assert parse_nba_ticker("") is None
    assert parse_nba_ticker("KXHIGHMIA-26FEB16-T86") is None  # weather ticker
    assert parse_nba_ticker("KXNBA-26-WAS") is None  # season futures, not daily game markets


def test_parse_invalid_date():
    assert parse_nba_ticker("KXNBA-99ZZZ26-LALHOU-LAL") is None


def test_parse_invalid_team():
    assert parse_nba_ticker("KXNBA-18MAR26-XXXYYY-XXX") is None


def test_parse_pick_not_in_game():
    assert parse_nba_ticker("KXNBA-18MAR26-LALHOU-BOS") is None


def test_make_game_ticker():
    ticker = make_game_ticker("LAL", "HOU", "LAL", datetime.date(2026, 3, 18))
    assert ticker == "KXNBA-18MAR26-LALHOU-LAL"


def test_make_prop_ticker():
    ticker = make_prop_ticker(
        "points", "LAL", "JAMESL", "over", 27, datetime.date(2026, 3, 18),
    )
    assert ticker == "KXNBAPTS-18MAR26-LALJAMESL-O27"


def test_extract_game_id():
    gid = extract_game_id("KXNBA-18MAR26-LALHOU-LAL")
    assert gid == "LAL-HOU-20260318"


def test_extract_game_id_live_game_ticker():
    gid = extract_game_id("KXNBAGAME-26MAR21GSWATL-GSW")
    assert gid == "ATL-GSW-20260321"


def test_extract_game_id_prop_returns_none():
    assert extract_game_id("KXNBAPTS-18MAR26-LALJAMESL-O27") is None


def test_extract_team():
    assert extract_team("KXNBA-18MAR26-LALHOU-LAL") == "LAL"
    assert extract_team("KXNBAPTS-18MAR26-BOSBROWND-O15") == "BOS"
    assert extract_team("INVALID") is None
