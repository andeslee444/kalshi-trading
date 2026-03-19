"""Batch NBA player data collector.

Searches all NBA players, filters by minutes played, and collects
game logs + profile data for qualifying players. Supports incremental
mode (skip recent files) and dry-run (roster preview only).

Usage:
    python3 -m collect.batch_collector --min-minutes 20 --max-players 60 --sport nba --dry-run
    python3 -m collect.batch_collector --min-minutes 20 --max-players 60
    python3 -m collect.batch_collector --force  # ignore cached files
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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
from collect.real_sports import extract_player_gamelog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("collect.batch_collector")

# Incremental mode: skip files newer than this (seconds)
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Rate limiting delay between players (seconds)
_INTER_PLAYER_DELAY = 0.2


def _resolve_player_name(player: dict) -> str:
    """Extract display name from a search result player dict."""
    name = player.get("name") or player.get("fullName")
    if not name:
        first = player.get("firstName", "")
        last = player.get("lastName", "")
        name = f"{first} {last}".strip() if (first or last) else f"Player {player.get('id', '?')}"
    return name


def _resolve_team(player: dict) -> str:
    """Extract team name from a search result player dict."""
    team_raw = player.get("team", player.get("teamName", player.get("teamAbbreviation", "")))
    if isinstance(team_raw, dict):
        team_raw = team_raw.get("name", team_raw.get("displayName", team_raw.get("key", "")))
    return str(team_raw) if team_raw else ""


def _is_cache_fresh(filepath: Path) -> bool:
    """Return True if file exists and was modified less than 24h ago."""
    if not filepath.exists():
        return False
    age = time.time() - filepath.stat().st_mtime
    return age < _CACHE_TTL_SECONDS


async def _compute_avg_minutes(
    client: RealSportsClient,
    player_id: int,
    sport: str = "nba",
) -> float:
    """Fetch season feed and compute average minutes from game entries."""
    feed = await client.get_player_season_feed(player_id, sport=sport, limit=20)
    if not feed or not isinstance(feed, dict):
        return 0.0

    # Find box score entries
    box_scores = None
    for key in ("playerBoxScores", "boxScores", "boxscores"):
        val = feed.get(key)
        if val and isinstance(val, list):
            box_scores = val
            break
    if not box_scores and "data" in feed:
        data_inner = feed["data"]
        if isinstance(data_inner, list):
            box_scores = data_inner
        elif isinstance(data_inner, dict):
            for key in ("playerBoxScores", "boxScores", "boxscores"):
                val = data_inner.get(key)
                if val and isinstance(val, list):
                    box_scores = val
                    break
    if not box_scores:
        return 0.0

    total_min = 0.0
    games_counted = 0
    for bs in box_scores:
        if not isinstance(bs, dict):
            continue
        if bs.get("didNotPlay", False) is True:
            continue
        try:
            minutes = float(bs.get("minutes", 0) or 0)
        except (ValueError, TypeError):
            continue
        if minutes > 0:
            total_min += minutes
            games_counted += 1

    return (total_min / games_counted) if games_counted > 0 else 0.0


async def _filter_players_by_minutes(
    client: RealSportsClient,
    players: list[dict],
    min_minutes: float,
    sport: str = "nba",
) -> list[dict]:
    """Filter player list to those averaging >= min_minutes per game.

    Checks search result stats first; falls back to fetching season feed
    if minutes data is not present in the search response.
    """
    qualified = []

    for i, player in enumerate(players):
        if not isinstance(player, dict):
            continue

        pid = player.get("id") or player.get("playerId")
        if not pid:
            continue
        pid = int(pid)
        name = _resolve_player_name(player)

        # Check if search results include minutes data
        stats = player.get("stats") or player.get("seasonStats") or {}
        avg_min = None
        if isinstance(stats, dict):
            for key in ("minutes", "min", "avgMinutes", "mpg"):
                val = stats.get(key)
                if val is not None:
                    try:
                        avg_min = float(val)
                    except (ValueError, TypeError):
                        pass
                    break

        # Fallback: fetch season feed to compute avg minutes
        if avg_min is None:
            avg_min = await _compute_avg_minutes(client, pid, sport=sport)
            # Brief delay after API call
            await asyncio.sleep(0.1)

        if avg_min >= min_minutes:
            player["_avg_minutes"] = avg_min
            qualified.append(player)
            _log.debug("  PASS %s (%.1f min)", name, avg_min)
        else:
            _log.debug("  SKIP %s (%.1f min < %.0f)", name, avg_min, min_minutes)

    return qualified


def _avg_minutes_from_gamelogs(gamelogs: list[GameLog]) -> float:
    """Compute average minutes from collected GameLog objects."""
    if not gamelogs:
        return 0.0
    played = [gl.minutes for gl in gamelogs if gl.minutes > 0]
    return (sum(played) / len(played)) if played else 0.0


def _search_result_avg_minutes(player: dict) -> Optional[float]:
    """Extract average minutes from search result stats (no API call).

    Returns the avg minutes if present in the search response, else None.
    """
    stats = player.get("stats") or player.get("seasonStats") or {}
    if not isinstance(stats, dict):
        return None
    for key in ("minutes", "min", "avgMinutes", "mpg"):
        val = stats.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


async def batch_collect(
    client: RealSportsClient,
    sport: str = "nba",
    min_minutes: float = 20.0,
    max_players: int = 60,
    n_games: int = 20,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Batch-collect game logs for qualifying NBA players.

    Players are filtered by average minutes. When the search response includes
    minutes data the check is free; otherwise the season feed is fetched once
    and used for both the minutes check AND game-log extraction (no double
    fetch).

    Returns summary dict with collection stats.
    """
    data_dir = _PROJECT_ROOT / "data"
    processed_dir = data_dir / "processed" / "gamelogs"
    processed_dir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()

    # Step 1: Search all players
    _log.info("Searching for %s players...", sport.upper())
    players = await client.search_players(sport=sport)

    if not players:
        _log.error("No players returned from search endpoint")
        return {"error": "no_players", "players_collected": 0}

    _log.info("Found %d players from search", len(players))

    # Step 2: Pre-filter using search-result minutes (free, no API calls).
    # Players without search-result minutes are kept as "needs_collection"
    # and will be filtered after their season feed is fetched once.
    pre_qualified: list[dict] = []
    needs_collection: list[dict] = []

    for player in players:
        if not isinstance(player, dict):
            continue
        pid = player.get("id") or player.get("playerId")
        if not pid:
            continue

        search_min = _search_result_avg_minutes(player)
        if search_min is not None:
            if search_min >= min_minutes:
                player["_avg_minutes"] = search_min
                pre_qualified.append(player)
            else:
                _log.debug(
                    "  SKIP %s (%.1f min < %.0f from search)",
                    _resolve_player_name(player), search_min, min_minutes,
                )
        else:
            needs_collection.append(player)

    _log.info(
        "Pre-filter: %d qualified from search stats, %d need collection to check minutes",
        len(pre_qualified), len(needs_collection),
    )

    # For dry-run we need minutes for all players, so fetch season feeds
    # for the needs_collection group via _compute_avg_minutes (filter only).
    if dry_run:
        for player in needs_collection:
            pid = int(player.get("id") or player.get("playerId"))
            avg_min = await _compute_avg_minutes(client, pid, sport=sport)
            await asyncio.sleep(0.1)
            if avg_min >= min_minutes:
                player["_avg_minutes"] = avg_min
                pre_qualified.append(player)
            else:
                _log.debug(
                    "  SKIP %s (%.1f min < %.0f)",
                    _resolve_player_name(player), avg_min, min_minutes,
                )
        qualified = pre_qualified
    else:
        # Combine both lists; needs_collection players will be filtered
        # after their gamelogs are fetched (single API call serves both).
        qualified = pre_qualified + needs_collection

    _log.info("%d candidate players after pre-filter", len(qualified))

    # Cap to max_players
    if len(qualified) > max_players:
        qualified = qualified[:max_players]
        _log.info("Capped to %d players (--max-players)", max_players)

    # Dry-run: print roster and exit
    if dry_run:
        print(f"\n{'='*70}")
        print(f"DRY RUN -- Qualified Roster ({len(qualified)} players)")
        print(f"{'='*70}")
        for i, p in enumerate(qualified):
            pid = int(p.get("id") or p.get("playerId"))
            name = _resolve_player_name(p)
            team = _resolve_team(p)
            avg_min = p.get("_avg_minutes", 0.0)
            cache_path = processed_dir / f"{pid}.json"
            cached = "[CACHED]" if _is_cache_fresh(cache_path) else ""
            print(f"  {i+1:>3}. {name:<28} {team:<5} {avg_min:>5.1f} min  (ID: {pid}) {cached}")
        print(f"{'='*70}")
        elapsed = time.time() - start_time
        print(f"  Elapsed: {elapsed:.1f}s")
        return {
            "dry_run": True,
            "qualified_count": len(qualified),
            "elapsed_seconds": elapsed,
        }

    # Step 3: Collect each candidate player. Players without pre-verified
    # minutes are filtered AFTER collection (collect once, check minutes
    # from the returned gamelogs -- no double season-feed fetch).
    all_gamelogs: dict[int, list[GameLog]] = {}
    total_games = 0
    skipped_cached = 0
    skipped_minutes = 0
    collected_count = 0
    errors = 0

    for i, player in enumerate(qualified):
        pid = int(player.get("id") or player.get("playerId"))
        name = _resolve_player_name(player)
        team = _resolve_team(player)
        pre_verified_min = player.get("_avg_minutes")  # None if not pre-checked

        gamelog_path = processed_dir / f"{pid}.json"

        # Incremental mode: skip if cached and not forced
        if not force and _is_cache_fresh(gamelog_path):
            _log.info(
                "[%d/%d] SKIP %s (ID: %d) -- cached (< 24h old)",
                i + 1, len(qualified), name, pid,
            )
            skipped_cached += 1
            # Load existing data for summary
            try:
                with open(gamelog_path) as f:
                    existing = json.load(f)
                n_existing = existing.get("games_collected", 0)
                total_games += n_existing
            except (json.JSONDecodeError, OSError):
                pass
            continue

        # Stop collecting once we have enough players
        if collected_count + skipped_cached >= max_players:
            break

        elapsed_so_far = time.time() - start_time
        min_label = f"{pre_verified_min:.1f}" if pre_verified_min is not None else "?"
        _log.info(
            "[%d/%d] Collecting %s (ID: %d, team: %s, %s min) -- elapsed: %.1fs",
            i + 1, len(qualified), name, pid, team, min_label, elapsed_so_far,
        )

        try:
            # Fetch game logs via shared extractor (single season-feed call)
            gamelogs = await extract_player_gamelog(
                client, pid, player_name=name, team=team,
                sport=sport, n_games=n_games,
            )

            # Post-collection minutes filter for players that lacked
            # search-result stats. The season feed was already fetched by
            # extract_player_gamelog, so this is free (no extra API call).
            if pre_verified_min is None and gamelogs:
                avg_min = _avg_minutes_from_gamelogs(gamelogs)
                if avg_min < min_minutes:
                    _log.info(
                        "  SKIP %s -- post-collection avg %.1f min < %.0f threshold",
                        name, avg_min, min_minutes,
                    )
                    skipped_minutes += 1
                    # Rate limiting delay between players
                    if i < len(qualified) - 1:
                        await asyncio.sleep(_INTER_PLAYER_DELAY)
                    continue
                player["_avg_minutes"] = avg_min

            avg_min = player.get("_avg_minutes", 0.0)

            # Fetch player profile for splits enrichment
            profile = None
            try:
                profile = await client.get_player_profile(pid, sport=sport)
            except Exception as exc:
                _log.warning("  Failed to fetch profile for %s: %s", name, exc)

            # Enrich gamelogs with profile splits (home/away) where available
            if profile and isinstance(profile, dict) and gamelogs:
                _enrich_with_profile(gamelogs, profile)

            if gamelogs:
                all_gamelogs[pid] = gamelogs
                total_games += len(gamelogs)
                collected_count += 1

                # Write individual player file
                with open(gamelog_path, "w") as f:
                    json.dump(
                        {
                            "player_id": pid,
                            "player_name": name,
                            "team": team,
                            "sport": sport,
                            "avg_minutes": round(avg_min, 1),
                            "games_collected": len(gamelogs),
                            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "gamelogs": [asdict(gl) for gl in gamelogs],
                            "profile_splits": _extract_splits(profile) if profile else None,
                        },
                        f,
                        indent=2,
                    )
                _log.info(
                    "  Saved %d games -> %s (running total: %d games, %d players)",
                    len(gamelogs), gamelog_path.name, total_games,
                    len(all_gamelogs) + skipped_cached,
                )
            else:
                _log.info("  No game logs extracted for %s", name)

        except Exception as exc:
            _log.error("  ERROR collecting %s (ID: %d): %s", name, pid, exc)
            errors += 1

        # Rate limiting delay between players
        if i < len(qualified) - 1:
            await asyncio.sleep(_INTER_PLAYER_DELAY)

    elapsed = time.time() - start_time

    # Step 4: Write collection summary
    summary = {
        "sport": sport,
        "min_minutes": min_minutes,
        "max_players_requested": max_players,
        "search_total": len(players),
        "qualified_count": len(all_gamelogs) + skipped_cached,
        "players_collected": len(all_gamelogs),
        "players_cached": skipped_cached,
        "players_skipped_minutes": skipped_minutes,
        "players_errored": errors,
        "total_games": total_games,
        "elapsed_seconds": round(elapsed, 1),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "players": {
            str(pid): {
                "name": logs[0].player_name if logs else "",
                "games": len(logs),
            }
            for pid, logs in all_gamelogs.items()
        },
    }

    summary_path = data_dir / "processed" / "collection_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    _log.info(
        "Batch collection complete: %d collected, %d cached, %d skipped (minutes), %d errors, %d total games in %.1fs",
        len(all_gamelogs), skipped_cached, skipped_minutes, errors, total_games, elapsed,
    )

    return summary


def _extract_splits(profile: dict) -> Optional[dict]:
    """Extract home/away splits from player profile if available."""
    if not profile or not isinstance(profile, dict):
        return None

    splits = profile.get("splits") or profile.get("statSplits")
    if not splits:
        # Try nested in "data" or "player"
        for key in ("data", "player", "playerProfile"):
            inner = profile.get(key)
            if isinstance(inner, dict):
                splits = inner.get("splits") or inner.get("statSplits")
                if splits:
                    break

    if not splits:
        return None

    # Return raw splits for downstream analysis
    if isinstance(splits, (dict, list)):
        return splits
    return None


def _enrich_with_profile(gamelogs: list[GameLog], profile: dict) -> None:
    """Enrich game logs with profile data where available.

    Currently a no-op enrichment placeholder -- the profile splits are
    stored alongside gamelogs in the output JSON for downstream analysis
    rather than modifying GameLog fields directly (which has a fixed schema).
    """
    # Profile splits are saved in the output JSON alongside gamelogs.
    # Future enrichment (e.g., adding pace factor, usage rate) can be
    # applied here when the GameLog schema is extended.
    pass


async def _async_main(args: argparse.Namespace) -> None:
    """Async entry point."""
    try:
        from dotenv import load_dotenv
        env_loaded = False
        for env_path in [
            _PROJECT_ROOT / ".env",
            _PROJECT_ROOT.parent.parent / ".env",
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
        summary = await batch_collect(
            client,
            sport=args.sport,
            min_minutes=args.min_minutes,
            max_players=args.max_players,
            n_games=args.games,
            dry_run=args.dry_run,
            force=args.force,
        )

        if not args.dry_run and not summary.get("error"):
            print(f"\n{'='*60}")
            print("Batch Collection Summary")
            print(f"{'='*60}")
            print(f"  Sport:           {args.sport.upper()}")
            print(f"  Min minutes:     {args.min_minutes}")
            print(f"  Search results:  {summary.get('search_total', 0)}")
            print(f"  Qualified:       {summary.get('qualified_count', 0)}")
            print(f"  Collected:       {summary.get('players_collected', 0)}")
            print(f"  Cached (skip):   {summary.get('players_cached', 0)}")
            print(f"  Errors:          {summary.get('players_errored', 0)}")
            print(f"  Total games:     {summary.get('total_games', 0)}")
            print(f"  Elapsed:         {summary.get('elapsed_seconds', 0):.1f}s")
            print(f"  Output dir:      data/processed/gamelogs/")
            print(f"{'='*60}")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Batch collect NBA player game logs with minutes filter",
    )
    parser.add_argument(
        "--min-minutes", type=float, default=20.0,
        help="Minimum average minutes per game to qualify (default: 20)",
    )
    parser.add_argument(
        "--max-players", type=int, default=60,
        help="Maximum number of players to collect (default: 60)",
    )
    parser.add_argument(
        "--sport", type=str, default="nba",
        help="Sport to collect (default: nba)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Search and filter only, print qualified roster without fetching data",
    )
    parser.add_argument(
        "--games", type=int, default=20,
        help="Number of recent games per player (default: 20)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-collection even if cached files exist (< 24h old)",
    )
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
