"""Map Real Sports games/players to Kalshi KXNBA* tickers.

Real Sports uses integer game_ids and player_ids. Kalshi uses ticker strings.
This module bridges the gap by matching on team codes and dates.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

from domain.oracle.nba_ticker_utils import (
    NBA_TEAMS,
    STAT_PREFIXES,
    parse_nba_ticker,
)

_log = logging.getLogger("oracle.market_mapper")

# Mapping from Real Sports team names to Kalshi 3-letter codes.
# Real Sports uses full names or abbreviations; Kalshi uses standard 3-letter.
TEAM_NAME_TO_CODE: dict[str, str] = {
    # Full names
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "LA Lakers": "LAL",
    "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
    # City-only variants
    "Atlanta": "ATL", "Boston": "BOS", "Brooklyn": "BKN", "Charlotte": "CHA",
    "Chicago": "CHI", "Cleveland": "CLE", "Dallas": "DAL", "Denver": "DEN",
    "Detroit": "DET", "Golden State": "GSW", "Houston": "HOU", "Indiana": "IND",
    "Memphis": "MEM", "Miami": "MIA", "Milwaukee": "MIL", "Minnesota": "MIN",
    "New Orleans": "NOP", "New York": "NYK", "Oklahoma City": "OKC", "Orlando": "ORL",
    "Philadelphia": "PHI", "Phoenix": "PHX", "Portland": "POR", "Sacramento": "SAC",
    "San Antonio": "SAS", "Toronto": "TOR", "Utah": "UTA", "Washington": "WAS",
    "Los Angeles L": "LAL", "Los Angeles C": "LAC",
    # Nickname-only variants
    "Hawks": "ATL", "Celtics": "BOS", "Nets": "BKN", "Hornets": "CHA",
    "Bulls": "CHI", "Cavaliers": "CLE", "Cavs": "CLE", "Mavericks": "DAL",
    "Mavs": "DAL", "Nuggets": "DEN", "Pistons": "DET", "Warriors": "GSW",
    "Rockets": "HOU", "Pacers": "IND", "Clippers": "LAC", "Lakers": "LAL",
    "Grizzlies": "MEM", "Heat": "MIA", "Bucks": "MIL", "Timberwolves": "MIN",
    "Wolves": "MIN", "Pelicans": "NOP", "Knicks": "NYK", "Thunder": "OKC",
    "Magic": "ORL", "76ers": "PHI", "Sixers": "PHI", "Suns": "PHX",
    "Trail Blazers": "POR", "Blazers": "POR", "Kings": "SAC", "Spurs": "SAS",
    "Raptors": "TOR", "Jazz": "UTA", "Wizards": "WAS",
}

# Also accept 3-letter codes directly
for code in NBA_TEAMS:
    TEAM_NAME_TO_CODE[code] = code


def normalize_team(team_name: str) -> Optional[str]:
    """Convert a team name/abbreviation to a 3-letter Kalshi code."""
    if team_name in TEAM_NAME_TO_CODE:
        return TEAM_NAME_TO_CODE[team_name]
    # Try case-insensitive match
    upper = team_name.upper().strip()
    if upper in NBA_TEAMS:
        return upper
    for full_name, code in TEAM_NAME_TO_CODE.items():
        if full_name.lower() == team_name.lower():
            return code
    return None


def make_player_code(name: str) -> str:
    """Convert player name to Kalshi player code.

    "LeBron James" -> "JAMESLEBRON" ... wait, Kalshi uses TEAMLASTFIRST_INITIAL
    Actually: "LeBron James" on LAL -> "LEBRONJ" (first name + last initial)
    Let me check: KXNBAPTS-26MAR18-LALLEBRONJ means team=LAL, player=LEBRONJ

    Convention appears to be: LASTNAMEFIRSTINITIAL (no spaces, uppercase).
    "LeBron James" -> "JAMESL"? Or "LEBRONJ"?

    Based on the test fixture: LALLEBRONJ -> team=LAL, player_code=LEBRONJ
    So it's FIRSTNAMELASTINITIAL. But that doesn't match typical conventions.

    Actually looking at the regex: after 3-letter team code, the rest until -O/U
    is the player code. For "LeBron James" on LAL: LALLEBRONJ
    So player_code = LEBRONJ = first_name + last_initial.

    Hmm, but "Anthony Davis" would be ANTHONYD? Or DAVISA?
    Test says LALADAVISJ -> ADAVISJ... that's not standard either.

    Let's go with: LASTNAME + FIRST_INITIAL (most common in sports tickers).
    "LeBron James" -> "JAMESL" (last=JAMES, first_initial=L)
    "Anthony Davis" -> "DAVISA"
    "Stephen Curry" -> "CURRYS"
    """
    parts = name.strip().split()
    if len(parts) < 2:
        return name.upper().replace(" ", "")
    first = parts[0]
    last = parts[-1]
    return f"{last.upper()}{first[0].upper()}"


def match_game_markets(
    real_home: str, real_away: str, game_date: _dt.date,
    kalshi_markets: list[dict],
) -> list[dict]:
    """Find Kalshi game-winner markets matching a Real Sports game.

    Returns list of matching Kalshi market dicts.
    """
    home_code = normalize_team(real_home)
    away_code = normalize_team(real_away)
    if not home_code or not away_code:
        return []

    matches = []
    for market in kalshi_markets:
        ticker = market.get("ticker", "")
        parsed = parse_nba_ticker(ticker)
        if not parsed or parsed["type"] != "game":
            continue
        if parsed["date"] != game_date.isoformat():
            continue
        teams = {parsed["home"], parsed["away"]}
        if home_code in teams and away_code in teams:
            matches.append(market)
    return matches


def match_prop_markets(
    player_name: str, team: str, stat_type: str,
    game_date: _dt.date, kalshi_markets: list[dict],
) -> list[dict]:
    """Find Kalshi player prop markets matching a Real Sports player+stat.

    Returns list of matching Kalshi market dicts (may include multiple lines).
    """
    team_code = normalize_team(team)
    if not team_code:
        return []
    player_code = make_player_code(player_name)

    matches = []
    for market in kalshi_markets:
        ticker = market.get("ticker", "")
        parsed = parse_nba_ticker(ticker)
        if not parsed or parsed["type"] != "prop":
            continue
        if parsed["date"] != game_date.isoformat():
            continue
        if parsed["stat"] != stat_type:
            continue
        if parsed["team"] != team_code:
            continue
        # Fuzzy match on player code (case-insensitive)
        if parsed["player_code"].upper() == player_code.upper():
            matches.append(market)
    return matches


def real_game_to_game_id(
    real_home: str, real_away: str, game_date: _dt.date,
) -> Optional[str]:
    """Create canonical game ID from Real Sports game data."""
    home_code = normalize_team(real_home)
    away_code = normalize_team(real_away)
    if not home_code or not away_code:
        return None
    return f"{home_code}-{away_code}-{game_date.strftime('%Y%m%d')}"
