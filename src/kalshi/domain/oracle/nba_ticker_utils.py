"""NBA ticker parsing utilities for Kalshi KXNBA* markets.

Supported game winner patterns:
  Legacy synthetic: KXNBA-18MAR26-LALHOU-LAL
  Live daily game:  KXNBAGAME-26MAR21GSWATL-GSW

The live daily-game series uses YYMMMDD and encodes the away team first, then
the home team.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# NBA team 3-letter codes
NBA_TEAMS = frozenset({
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN",
    "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM", "MIA",
    "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX",
    "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
})

# Stat type prefixes
STAT_PREFIXES = {
    "KXNBAPTS": "points",
    "KXNBAREB": "rebounds",
    "KXNBAAST": "assists",
    "KXNBA3PM": "three_pointers",
    "KXNBASTL": "steals",
    "KXNBABLK": "blocks",
    "KXNBATO":  "turnovers",
    "KXNBA2D":  "double_double",
    "KXNBA3D":  "triple_double",
}

# Game-level series ticker prefixes (spec Section 2)
GAME_LEVEL_PREFIXES = frozenset({
    "KXNBA",          # game winner
    "KXNBAGAME",      # game winner (alternate)
    "KXNBASPREAD",    # spread
    "KXNBATOTAL",     # total O/U
    "KXNBATEAMTOTAL", # team total
    "KXNBA1HWINNER",  # 1st half winner
    "KXNBA2HWINNER",  # 2nd half winner
})

# Pattern: PREFIX-DDMMMYY-rest
_LEGACY_TICKER_RE = re.compile(
    r"^(KXNBA[A-Z0-9]*)-(\d{2})([A-Z]{3})(\d{2})-(.+)$"
)

# Current Kalshi daily NBA game winner pattern:
#   KXNBAGAME-YYMMMDDAWAYHOME-PICK
_LIVE_GAME_RE = re.compile(
    r"^(KXNBAGAME)-(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})-([A-Z]{3})$"
)

# Player prop tail: TEAMLASTNAMEINITIAL-O/U<line>
_PROP_RE = re.compile(
    r"^([A-Z]{3})([A-Z]+[A-Z])-([OU])(\d+(?:\.\d+)?)$"
)

# Game winner tail: TEAM1TEAM2-WINNER
_GAME_RE = re.compile(
    r"^([A-Z]{3})([A-Z]{3})-([A-Z]{3})$"
)


def parse_nba_ticker(ticker: str) -> Optional[dict]:
    """Parse a KXNBA* ticker into structured components.

    Returns dict with keys depending on market type:
      Game: {"type": "game", "prefix": ..., "date": ..., "home": ..., "away": ..., "pick": ...}
      Prop: {"type": "prop", "prefix": ..., "date": ..., "stat": ..., "team": ...,
             "player_code": ..., "direction": "over"|"under", "line": float}
      None if parsing fails.
    """
    live_game = _LIVE_GAME_RE.match(ticker)
    if live_game:
        prefix, year_str, mon_str, day_str, away, home, pick = live_game.groups()
        month = MONTHS.get(mon_str)
        if month is None:
            return None
        try:
            date = _dt.date(2000 + int(year_str), month, int(day_str))
        except ValueError:
            return None
        if away not in NBA_TEAMS or home not in NBA_TEAMS or pick not in (away, home):
            return None
        return {
            "type": "game",
            "prefix": prefix,
            "date": date.isoformat(),
            "home": home,
            "away": away,
            "pick": pick,
        }

    m = _LEGACY_TICKER_RE.match(ticker)
    if not m:
        return None

    prefix, day_str, mon_str, year_str, tail = m.groups()
    month = MONTHS.get(mon_str)
    if month is None:
        return None

    try:
        date = _dt.date(2000 + int(year_str), month, int(day_str))
    except ValueError:
        return None

    stat_type = STAT_PREFIXES.get(prefix)

    if stat_type:
        pm = _PROP_RE.match(tail)
        if not pm:
            return None
        team, player_code, direction_char, line_str = pm.groups()
        if team not in NBA_TEAMS:
            return None
        return {
            "type": "prop",
            "prefix": prefix,
            "date": date.isoformat(),
            "stat": stat_type,
            "team": team,
            "player_code": player_code,
            "direction": "over" if direction_char == "O" else "under",
            "line": float(line_str),
        }

    if prefix in {"KXNBA", "KXNBAGAME"}:
        gm = _GAME_RE.match(tail)
        if not gm:
            return None
        team1, team2, pick = gm.groups()
        if team1 not in NBA_TEAMS or team2 not in NBA_TEAMS or pick not in NBA_TEAMS:
            return None
        if pick not in (team1, team2):
            return None
        return {
            "type": "game",
            "prefix": prefix,
            "date": date.isoformat(),
            "home": team1,
            "away": team2,
            "pick": pick,
        }

    return None


def make_game_ticker(home: str, away: str, pick: str, date: _dt.date) -> str:
    """Build a game-winner ticker from components."""
    mon = list(MONTHS.keys())[date.month - 1]
    return f"KXNBA-{date.day:02d}{mon}{date.year % 100:02d}-{home}{away}-{pick}"


def make_prop_ticker(
    stat: str, team: str, player_code: str,
    direction: str, line: float, date: _dt.date,
) -> str:
    """Build a player prop ticker from components."""
    prefix_map = {v: k for k, v in STAT_PREFIXES.items()}
    prefix = prefix_map.get(stat, "KXNBAPTS")
    mon = list(MONTHS.keys())[date.month - 1]
    d = "O" if direction == "over" else "U"
    line_str = str(int(line)) if line == int(line) else str(line)
    return f"{prefix}-{date.day:02d}{mon}{date.year % 100:02d}-{team}{player_code}-{d}{line_str}"


def extract_game_id(ticker: str) -> Optional[str]:
    """Extract a canonical game ID from a ticker (e.g., 'LAL-HOU-20260318')."""
    parsed = parse_nba_ticker(ticker)
    if not parsed:
        return None
    if parsed["type"] == "game":
        return f"{parsed['home']}-{parsed['away']}-{parsed['date'].replace('-', '')}"
    if parsed["type"] == "prop":
        return None  # props don't encode the opponent
    return None


def extract_team(ticker: str) -> Optional[str]:
    """Extract the team code from a ticker."""
    parsed = parse_nba_ticker(ticker)
    if not parsed:
        return None
    if parsed["type"] == "prop":
        return parsed["team"]
    if parsed["type"] == "game":
        return parsed["pick"]
    return None
