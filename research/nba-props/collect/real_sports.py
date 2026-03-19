"""Real Sports data collection pipeline.

Collects player game logs from the Real Sports API by:
1. Fetching player list via search endpoint
2. For each player, fetching their season feed (playerBoxScores entries)
3. Extracting stats from the season feed's statValues array (fast path)
4. Falling back to box score detail + play metadata if statValues missing

Season feed statValues type mapping:
  1=pts, 2=ast, 3=reb, 4=stl, 5=blk, 6=to, 8=min,
  9=fg%, 14=+/-, 23=oreb, 24=dreb, 25=pf, 26=fg (e.g. "15/23"),
  27=3fg (e.g. "2/5"), 28=fts (e.g. "3/4"), 38=ts%

Box score play metadata[str(player_id)] keys:
  "1"=pts, "2"=ast, "3"=reb, "4"=stl, "5"=blk, "6"=to, "fps"=fantasy pts

Usage:
    python -m collect.real_sports --players 3 --games 5
    python -m collect.real_sports --players 50 --games 20 --sport nba
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Add project root to path so shared/ is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.real_sports_client import RealSportsClient, RealSportsConfig
from shared.models import GameLog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("collect.real_sports")

# statValues type IDs from season feed
_SV_TYPE_PTS = 1
_SV_TYPE_AST = 2
_SV_TYPE_REB = 3
_SV_TYPE_STL = 4
_SV_TYPE_BLK = 5
_SV_TYPE_TO = 6
_SV_TYPE_MIN = 8
_SV_TYPE_FG = 26    # "15/23" format
_SV_TYPE_3FG = 27   # "2/5" format
_SV_TYPE_FTS = 28   # "3/4" format

# Box score play metadata[str(player_id)] key mapping (fallback)
_PLAY_STAT_KEYS = {
    "1": "points",
    "2": "assists",
    "3": "rebounds",
    "4": "steals",
    "5": "blocks",
    "6": "turnovers",
}


def _parse_slash_stat(val) -> tuple[int, int]:
    """Parse '15/23' format into (made, attempted). Returns (0, 0) on failure."""
    if not val or not isinstance(val, str):
        return 0, 0
    parts = val.split("/")
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, TypeError):
        return 0, 0


def _int_safe(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _float_safe(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _extract_from_stat_values(stat_values: list[dict]) -> Optional[dict]:
    """Extract stats from season feed entry's statValues array.

    Each entry: {"type": int, "value": number_or_string, "label": str}
    Returns dict of parsed stats, or None if statValues is empty.
    """
    if not stat_values:
        return None

    sv_map = {sv.get("type"): sv.get("value") for sv in stat_values if isinstance(sv, dict)}

    if _SV_TYPE_PTS not in sv_map:
        return None  # no points = probably not a real game entry

    fg_made, fg_att = _parse_slash_stat(sv_map.get(_SV_TYPE_FG))
    fg3_made, _ = _parse_slash_stat(sv_map.get(_SV_TYPE_3FG))
    ft_made, ft_att = _parse_slash_stat(sv_map.get(_SV_TYPE_FTS))

    return {
        "points": _int_safe(sv_map.get(_SV_TYPE_PTS)),
        "assists": _int_safe(sv_map.get(_SV_TYPE_AST)),
        "rebounds": _int_safe(sv_map.get(_SV_TYPE_REB)),
        "steals": _int_safe(sv_map.get(_SV_TYPE_STL)),
        "blocks": _int_safe(sv_map.get(_SV_TYPE_BLK)),
        "turnovers": _int_safe(sv_map.get(_SV_TYPE_TO)),
        "three_pointers": fg3_made,
        "field_goals_made": fg_made,
        "field_goals_attempted": fg_att,
        "free_throws_made": ft_made,
        "free_throws_attempted": ft_att,
    }


def _extract_stats_from_play(play: dict, player_id: int) -> Optional[dict]:
    """Extract stat line from a box score play's metadata (fallback path)."""
    metadata = play.get("metadata")
    if not metadata:
        return None

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            return None

    pid_str = str(player_id)
    player_stats = metadata.get(pid_str)
    if not player_stats:
        return None

    if isinstance(player_stats, str):
        try:
            player_stats = json.loads(player_stats)
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(player_stats, dict):
        return None

    stats = {}
    for raw_key, stat_name in _PLAY_STAT_KEYS.items():
        stats[stat_name] = _int_safe(player_stats.get(raw_key, 0))

    # Play metadata doesn't include FG/3FG/FT splits
    stats["three_pointers"] = 0
    stats["field_goals_made"] = 0
    stats["field_goals_attempted"] = 0
    stats["free_throws_made"] = 0
    stats["free_throws_attempted"] = 0

    return stats


def _resolve_opponent(bs: dict) -> str:
    """Extract opponent team name from a season feed entry."""
    # The season feed includes homeTeam/awayTeam objects and opponentTeamId
    player_team_id = bs.get("teamId")
    home_team = bs.get("homeTeam", {})
    away_team = bs.get("awayTeam", {})

    if isinstance(home_team, dict) and isinstance(away_team, dict):
        home_id = bs.get("homeTeamId") or home_team.get("id")
        if player_team_id == home_id:
            name = away_team.get("displayName") or away_team.get("name") or away_team.get("key")
            if name:
                return str(name)
        else:
            name = home_team.get("displayName") or home_team.get("name") or home_team.get("key")
            if name:
                return str(name)

    # Fallback to opponentTeamId
    opp_id = bs.get("opponentTeamId") or bs.get("opponentId") or ""
    return str(opp_id) if opp_id else "unknown"


def _resolve_player_name(bs: dict, fallback: str) -> str:
    """Extract player name from season feed entry."""
    player = bs.get("player", {})
    if isinstance(player, dict):
        display = player.get("displayName")
        if display:
            return str(display)
        first = player.get("firstName", "")
        last = player.get("lastName", "")
        if first or last:
            return f"{first} {last}".strip()
    return fallback


def _find_box_scores(feed: dict) -> list:
    """Locate box score entries from a season feed response."""
    for key in ("playerBoxScores", "boxScores", "boxscores"):
        val = feed.get(key)
        if val and isinstance(val, list):
            return val

    if "data" in feed:
        data_inner = feed["data"]
        if isinstance(data_inner, list):
            return data_inner
        if isinstance(data_inner, dict):
            for key in ("playerBoxScores", "boxScores", "boxscores"):
                val = data_inner.get(key)
                if val and isinstance(val, list):
                    return val

    return []


async def extract_player_gamelog(
    client: RealSportsClient,
    player_id: int,
    player_name: str = "",
    team: str = "",
    sport: str = "nba",
    n_games: int = 20,
    save_raw: bool = True,
) -> list[GameLog]:
    """Extract game log for a single player.

    Primary path: extracts stats from season feed statValues (no extra API calls).
    Fallback path: fetches individual box scores and reads play metadata.

    Returns list of GameLog dataclasses.
    """
    data_dir = _PROJECT_ROOT / "data"
    raw_dir = data_dir / "raw" / "players" / str(player_id)
    raw_dir.mkdir(parents=True, exist_ok=True)

    feed = await client.get_player_season_feed(player_id, sport=sport, limit=n_games)

    if save_raw:
        with open(raw_dir / "season_feed.json", "w") as f:
            json.dump(feed, f, indent=2)

    if not feed or not isinstance(feed, dict):
        _log.warning("  Empty season feed for player %d (%s)", player_id, player_name)
        return []

    box_scores = _find_box_scores(feed)

    if not box_scores:
        _log.warning("  No box scores found in season feed for player %d (%s)", player_id, player_name)
        return []

    gamelogs: list[GameLog] = []
    skipped = 0
    box_score_fetches = 0

    for bs in box_scores:
        if not isinstance(bs, dict):
            skipped += 1
            continue

        # Filter: skip DNP entries (didNotPlay=True overrides played=True)
        did_not_play = bs.get("didNotPlay", False)
        if did_not_play is True:
            skipped += 1
            continue

        minutes_raw = bs.get("minutes", 0)
        try:
            minutes = float(minutes_raw) if minutes_raw else 0.0
        except (ValueError, TypeError):
            minutes = 0.0

        # Skip zero-minute entries (another DNP signal)
        if minutes == 0:
            skipped += 1
            continue

        # Game metadata from the season feed entry (no extra API call)
        game_date = bs.get("day") or bs.get("dateTime") or bs.get("date") or ""
        if "T" in str(game_date):
            game_date = str(game_date).split("T")[0]

        opponent = _resolve_opponent(bs)

        player_team_id = bs.get("teamId")
        home_team_id = bs.get("homeTeamId")
        is_home = (player_team_id == home_team_id) if player_team_id and home_team_id else False

        started = bool(bs.get("started", 0))

        effective_name = player_name or _resolve_player_name(bs, f"Player {player_id}")

        # Primary path: extract from statValues (already in season feed)
        stat_values = bs.get("statValues", [])
        stats = _extract_from_stat_values(stat_values)

        # Fallback: fetch individual box score and read play metadata
        if not stats:
            bs_id = bs.get("id") or bs.get("boxScoreId") or bs.get("playerBoxScoreId")
            if bs_id:
                try:
                    detail = await client.get_player_box_score(int(bs_id))
                    box_score_fetches += 1

                    if save_raw:
                        with open(raw_dir / f"boxscore_{bs_id}.json", "w") as f:
                            json.dump(detail, f, indent=2)

                    if detail and isinstance(detail, dict):
                        plays = detail.get("plays", [])
                        if plays and isinstance(plays, list):
                            sorted_plays = sorted(
                                [p for p in plays if isinstance(p, dict)],
                                key=lambda p: p.get("sequence", p.get("seq", 0)),
                                reverse=True,
                            )
                            for play in sorted_plays:
                                stats = _extract_stats_from_play(play, player_id)
                                if stats:
                                    break
                except Exception as exc:
                    _log.warning("  Failed to fetch box score %s: %s", bs_id, exc)

        if not stats:
            _log.debug("  No stat line for player %d game %s", player_id, game_date)
            skipped += 1
            continue

        gamelog = GameLog(
            player_id=player_id,
            player_name=effective_name,
            game_date=str(game_date),
            opponent=opponent,
            home=is_home,
            minutes=minutes,
            points=stats.get("points", 0),
            rebounds=stats.get("rebounds", 0),
            assists=stats.get("assists", 0),
            steals=stats.get("steals", 0),
            blocks=stats.get("blocks", 0),
            turnovers=stats.get("turnovers", 0),
            three_pointers=stats.get("three_pointers", 0),
            field_goals_made=stats.get("field_goals_made", 0),
            field_goals_attempted=stats.get("field_goals_attempted", 0),
            free_throws_made=stats.get("free_throws_made", 0),
            free_throws_attempted=stats.get("free_throws_attempted", 0),
        )
        gamelogs.append(gamelog)

    if skipped:
        _log.debug("  Skipped %d entries (DNP or missing data)", skipped)
    if box_score_fetches:
        _log.debug("  Fetched %d individual box scores (fallback path)", box_score_fetches)

    return gamelogs


async def collect_all_players(
    client: RealSportsClient,
    sport: str = "nba",
    n_games: int = 20,
    max_players: int = 50,
) -> dict[int, list[GameLog]]:
    """Collect game logs for multiple players.

    Returns dict mapping player_id -> list of GameLog.
    Saves raw responses and processed game logs to data/ directory.
    """
    data_dir = _PROJECT_ROOT / "data"
    processed_dir = data_dir / "processed" / "gamelogs"
    processed_dir.mkdir(parents=True, exist_ok=True)

    _log.info("Searching for %s players (max %d)...", sport.upper(), max_players)
    players = await client.search_players(sport=sport)

    if not players:
        _log.error("No players returned from search endpoint")
        return {}

    _log.info("Found %d players, collecting top %d", len(players), min(max_players, len(players)))

    players = players[:max_players]

    all_gamelogs: dict[int, list[GameLog]] = {}
    total_games = 0

    for i, player in enumerate(players):
        if not isinstance(player, dict):
            continue

        pid = player.get("id") or player.get("playerId")
        if not pid:
            continue

        pid = int(pid)
        # Player search returns firstName/lastName, not "name"
        name = player.get("name") or player.get("fullName")
        if not name:
            first = player.get("firstName", "")
            last = player.get("lastName", "")
            name = f"{first} {last}".strip() if (first or last) else f"Player {pid}"
        team_raw = player.get("team", player.get("teamName", player.get("teamAbbreviation", "")))
        if isinstance(team_raw, dict):
            team_raw = team_raw.get("name", team_raw.get("displayName", team_raw.get("key", "")))

        _log.info(
            "[%d/%d] Collecting %s (ID: %d, team: %s) -- up to %d games",
            i + 1, len(players), name, pid, team_raw, n_games,
        )

        try:
            gamelogs = await extract_player_gamelog(
                client, pid, player_name=name, team=str(team_raw),
                sport=sport, n_games=n_games,
            )
        except Exception as exc:
            _log.error("  Failed to collect player %d (%s): %s", pid, name, exc)
            continue

        if gamelogs:
            all_gamelogs[pid] = gamelogs
            total_games += len(gamelogs)

            gamelog_path = processed_dir / f"{pid}.json"
            with open(gamelog_path, "w") as f:
                json.dump(
                    {
                        "player_id": pid,
                        "player_name": name,
                        "team": str(team_raw),
                        "sport": sport,
                        "games_collected": len(gamelogs),
                        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "gamelogs": [asdict(gl) for gl in gamelogs],
                    },
                    f,
                    indent=2,
                )
            _log.info("  Saved %d games -> %s", len(gamelogs), gamelog_path.name)
        else:
            _log.info("  No game logs extracted")

        # Brief pause between players to be respectful to the API
        if i < len(players) - 1:
            await asyncio.sleep(0.3)

    _log.info(
        "Collection complete: %d players, %d total games",
        len(all_gamelogs), total_games,
    )

    # Save summary index
    summary_path = data_dir / "processed" / "collection_summary.json"
    with open(summary_path, "w") as f:
        json.dump(
            {
                "sport": sport,
                "n_games_requested": n_games,
                "max_players_requested": max_players,
                "players_collected": len(all_gamelogs),
                "total_games": total_games,
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "players": {
                    str(pid): {
                        "name": logs[0].player_name if logs else "",
                        "games": len(logs),
                    }
                    for pid, logs in all_gamelogs.items()
                },
            },
            f,
            indent=2,
        )

    return all_gamelogs


async def _async_main(args: argparse.Namespace) -> None:
    """Async entry point."""
    try:
        from dotenv import load_dotenv
        env_loaded = False
        for env_path in [
            _PROJECT_ROOT / ".env",
            Path.home() / "Documents" / "cursor-projects" / "kalshi-trading" / ".env",
        ]:
            if env_path.exists():
                load_dotenv(env_path)
                _log.info("Loaded env from %s", env_path)
                env_loaded = True
                break
        if not env_loaded:
            _log.warning("No .env file found, using environment variables")
    except ImportError:
        _log.warning("python-dotenv not installed, using environment variables directly")

    config = RealSportsConfig.from_env()
    missing = config.validate()
    if missing:
        _log.error(
            "Missing credentials: %s. Set in .env or environment.", ", ".join(missing)
        )
        sys.exit(1)

    client = RealSportsClient(config)
    try:
        start_time = time.time()

        all_gamelogs = await collect_all_players(
            client,
            sport=args.sport,
            n_games=args.games,
            max_players=args.players,
        )

        elapsed = time.time() - start_time

        total_games = sum(len(gl) for gl in all_gamelogs.values())
        print(f"\n{'='*60}")
        print("Collection Summary")
        print(f"{'='*60}")
        print(f"  Sport:           {args.sport.upper()}")
        print(f"  Players:         {len(all_gamelogs)} collected")
        print(f"  Total games:     {total_games}")
        print(f"  Time elapsed:    {elapsed:.1f}s")
        print(f"  Raw data:        data/raw/players/")
        print(f"  Processed data:  data/processed/gamelogs/")
        print(f"{'='*60}")

        if all_gamelogs:
            print("\nPer-player breakdown:")
            for pid, logs in all_gamelogs.items():
                if logs:
                    avg_pts = sum(g.points for g in logs) / len(logs)
                    avg_reb = sum(g.rebounds for g in logs) / len(logs)
                    avg_ast = sum(g.assists for g in logs) / len(logs)
                    print(
                        f"  {logs[0].player_name:<25} {len(logs):>2} games  "
                        f"avg: {avg_pts:.1f}pts  {avg_reb:.1f}reb  {avg_ast:.1f}ast"
                    )
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Collect player game logs from Real Sports API",
    )
    parser.add_argument(
        "--players", type=int, default=10,
        help="Maximum number of players to collect (default: 10)",
    )
    parser.add_argument(
        "--games", type=int, default=20,
        help="Number of recent games per player (default: 20)",
    )
    parser.add_argument(
        "--sport", type=str, default="nba",
        help="Sport to collect (default: nba)",
    )
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
