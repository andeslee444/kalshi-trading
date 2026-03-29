"""Tests for Oracle market mapper (Real Sports <-> Kalshi matching)."""

import datetime

from domain.oracle.market_mapper import (
    normalize_team,
    make_player_code,
    match_game_markets,
    match_prop_markets,
    real_game_to_game_id,
)


def test_normalize_team_full_name():
    assert normalize_team("Los Angeles Lakers") == "LAL"
    assert normalize_team("Boston Celtics") == "BOS"
    assert normalize_team("Golden State Warriors") == "GSW"


def test_normalize_team_code():
    assert normalize_team("LAL") == "LAL"
    assert normalize_team("BOS") == "BOS"


def test_normalize_team_aliases():
    assert normalize_team("Lakers") == "LAL"
    assert normalize_team("Golden State") == "GSW"
    assert normalize_team("Los Angeles L") == "LAL"


def test_normalize_team_case_insensitive():
    assert normalize_team("lal") == "LAL"
    assert normalize_team("los angeles lakers") == "LAL"


def test_normalize_team_unknown():
    assert normalize_team("Unknown Team") is None


def test_make_player_code():
    assert make_player_code("LeBron James") == "JAMESL"
    assert make_player_code("Anthony Davis") == "DAVISA"
    assert make_player_code("Stephen Curry") == "CURRYS"
    assert make_player_code("Jayson Tatum") == "TATUMJ"


def test_make_player_code_single_name():
    assert make_player_code("Nene") == "NENE"


def test_match_game_markets():
    markets = [
        {"ticker": "KXNBA-18MAR26-LALHOU-LAL", "yes_price": 55},
        {"ticker": "KXNBA-18MAR26-LALHOU-HOU", "yes_price": 45},
        {"ticker": "KXNBA-18MAR26-BOSNYK-BOS", "yes_price": 60},
    ]
    date = datetime.date(2026, 3, 18)

    matches = match_game_markets("Los Angeles Lakers", "Houston Rockets", date, markets)
    assert len(matches) == 2
    tickers = {m["ticker"] for m in matches}
    assert "KXNBA-18MAR26-LALHOU-LAL" in tickers
    assert "KXNBA-18MAR26-LALHOU-HOU" in tickers


def test_match_game_markets_live_kxnbagame():
    markets = [
        {"ticker": "KXNBAGAME-26MAR21GSWATL-GSW", "yes_price": 24},
        {"ticker": "KXNBAGAME-26MAR21GSWATL-ATL", "yes_price": 76},
        {"ticker": "KXNBAGAME-26MAR21INDSAS-SAS", "yes_price": 55},
    ]
    date = datetime.date(2026, 3, 21)

    matches = match_game_markets("Atlanta Hawks", "Golden State Warriors", date, markets)
    assert len(matches) == 2
    tickers = {m["ticker"] for m in matches}
    assert "KXNBAGAME-26MAR21GSWATL-GSW" in tickers
    assert "KXNBAGAME-26MAR21GSWATL-ATL" in tickers


def test_match_game_markets_no_match():
    markets = [{"ticker": "KXNBA-18MAR26-BOSNYK-BOS"}]
    date = datetime.date(2026, 3, 18)
    assert match_game_markets("LAL", "HOU", date, markets) == []


def test_match_game_markets_wrong_date():
    markets = [{"ticker": "KXNBA-19MAR26-LALHOU-LAL"}]
    date = datetime.date(2026, 3, 18)
    assert match_game_markets("LAL", "HOU", date, markets) == []


def test_match_prop_markets():
    markets = [
        {"ticker": "KXNBAPTS-18MAR26-LALJAMESL-O27"},
        {"ticker": "KXNBAPTS-18MAR26-LALJAMESL-O30"},
        {"ticker": "KXNBAREB-18MAR26-LALDAVISA-O10"},
    ]
    date = datetime.date(2026, 3, 18)

    matches = match_prop_markets("LeBron James", "LAL", "points", date, markets)
    assert len(matches) == 2  # both lines for LeBron pts


def test_match_prop_markets_wrong_stat():
    markets = [{"ticker": "KXNBAREB-18MAR26-LALJAMESL-O10"}]
    date = datetime.date(2026, 3, 18)
    matches = match_prop_markets("LeBron James", "LAL", "points", date, markets)
    assert len(matches) == 0


def test_real_game_to_game_id():
    gid = real_game_to_game_id("Los Angeles Lakers", "Houston Rockets",
                                datetime.date(2026, 3, 18))
    assert gid == "LAL-HOU-20260318"


def test_real_game_to_game_id_unknown_team():
    assert real_game_to_game_id("Unknown", "HOU", datetime.date(2026, 3, 18)) is None
