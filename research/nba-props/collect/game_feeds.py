"""Game feed collector — play-by-play data for Book C analysis.

Iterates game IDs from collected player gamelogs and fetches the full
play-by-play feed for each unique game. Stores raw feeds and extracts
game state snapshots useful for Book C parameters (foul trouble, blowout,
overtime probability).

Usage:
    python3 -m collect.game_feeds
    python3 -m collect.game_feeds --max-games 50 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.real_sports_client import RealSportsClient, RealSportsConfig
try:
    from shared.real_sports_client import RealSportsAuthError
except ImportError:
    RealSportsAuthError = Exception

_log = logging.getLogger("collect.game_feeds")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")

_INTER_GAME_DELAY = 0.5  # seconds between API calls


def _discover_game_ids(data_dir: Path) -> list[int]:
    """Extract unique game IDs from raw season feed files.

    The processed gamelogs don't store game_id, but the raw season feeds
    (in data/raw/players/*/season_feed_*.json) have gameId on each box score.
    """
    game_ids = set()
    raw_dir = data_dir / "raw" / "players"
    if not raw_dir.exists():
        return []

    for player_dir in raw_dir.iterdir():
        if not player_dir.is_dir():
            continue
        for sf in player_dir.glob("season_feed*.json"):
            try:
                data = json.loads(sf.read_text())
                for key in ("playerBoxScores", "boxScores", "boxscores"):
                    for bs in data.get(key, []):
                        gid = bs.get("gameId")
                        if gid:
                            game_ids.add(int(gid))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
    return sorted(game_ids)


def _extract_game_state_snapshots(plays: list[dict], game: dict) -> list[dict]:
    """Extract game state at key moments from play-by-play data.

    Returns a list of snapshots useful for Book C parameter calibration:
    - End of each quarter (score, margin)
    - Foul trouble events (TechnicalFoul plays)
    - Blowout detection (margin > 20 at any point in Q3+)
    - OT-relevant moments (close games in Q4 final minutes)
    """
    snapshots = []
    for play in plays:
        period = play.get("period")
        time_remaining = play.get("timeRemainingSeconds", 0)
        home_score = play.get("homeTeamScore", 0)
        away_score = play.get("awayTeamScore", 0)
        margin = abs((home_score or 0) - (away_score or 0))
        play_type = play.get("type", "")

        # Period transitions
        if play_type == "Period":
            snapshots.append({
                "event": "period_end",
                "period": period,
                "home_score": home_score,
                "away_score": away_score,
                "margin": margin,
            })

        # Technical fouls (foul trouble signal)
        if play_type == "TechnicalFoul":
            snapshots.append({
                "event": "technical_foul",
                "period": period,
                "time_remaining": time_remaining,
                "player_id": play.get("primaryPlayerId"),
                "margin": margin,
            })

        # Blowout detection: margin > 20 in Q3+
        if period and period >= 3 and margin > 20:
            snapshots.append({
                "event": "blowout_moment",
                "period": period,
                "time_remaining": time_remaining,
                "margin": margin,
                "home_score": home_score,
                "away_score": away_score,
            })

        # OT-relevant: Q4 final 5 min, margin <= 6
        if period == 4 and time_remaining and time_remaining <= 300 and margin <= 6:
            snapshots.append({
                "event": "clutch_moment",
                "period": period,
                "time_remaining": time_remaining,
                "margin": margin,
                "home_score": home_score,
                "away_score": away_score,
            })

    # Deduplicate blowout_moment (keep first occurrence)
    seen_blowout = False
    deduped = []
    for s in snapshots:
        if s["event"] == "blowout_moment":
            if not seen_blowout:
                seen_blowout = True
                deduped.append(s)
        else:
            deduped.append(s)

    return deduped


async def collect_game_feeds(
    client: RealSportsClient,
    sport: str = "nba",
    max_games: int = 500,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Collect play-by-play feeds for games found in player gamelogs."""
    data_dir = _PROJECT_ROOT / "data"
    feeds_dir = data_dir / "processed" / "game_feeds"
    feeds_dir.mkdir(parents=True, exist_ok=True)

    game_ids = _discover_game_ids(data_dir)
    _log.info("Discovered %d unique game IDs from player gamelogs", len(game_ids))

    if not game_ids:
        _log.error("No game IDs found. Run batch_collector first.")
        return {"error": "no_games", "games_collected": 0}

    # Filter to uncollected games (unless --force)
    if not force:
        cached = [gid for gid in game_ids if (feeds_dir / f"{gid}.json").exists()]
        game_ids = [gid for gid in game_ids if not (feeds_dir / f"{gid}.json").exists()]
        _log.info("Cached: %d, Need collection: %d", len(cached), len(game_ids))

    if len(game_ids) > max_games:
        game_ids = game_ids[:max_games]
        _log.info("Capped to %d games (--max-games)", max_games)

    if dry_run:
        print(f"DRY RUN: would collect {len(game_ids)} game feeds")
        return {"dry_run": True, "games_to_collect": len(game_ids)}

    collected = 0
    errors = 0
    total_plays = 0
    start_time = time.time()

    for i, game_id in enumerate(game_ids):
        try:
            feed = await client.get_game_feed(game_id, sport=sport)
            if not isinstance(feed, dict):
                _log.warning("[%d/%d] Game %d: empty response", i + 1, len(game_ids), game_id)
                errors += 1
                continue

            game = feed.get("game", {})
            plays = feed.get("plays", [])
            snapshots = _extract_game_state_snapshots(plays, game)

            # Store processed game feed
            output = {
                "game_id": game_id,
                "sport": sport,
                "status": game.get("status"),
                "home_team": game.get("homeTeam", {}).get("displayName", ""),
                "away_team": game.get("awayTeam", {}).get("displayName", ""),
                "home_team_id": game.get("homeTeamId") or game.get("homeTeam", {}).get("id"),
                "away_team_id": game.get("awayTeamId") or game.get("awayTeam", {}).get("id"),
                "period": game.get("period"),
                "total_plays": len(plays),
                "play_types": {},
                "game_state_snapshots": snapshots,
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

            # Summarize play types
            for p in plays:
                pt = p.get("type", "unknown")
                output["play_types"][pt] = output["play_types"].get(pt, 0) + 1

            with open(feeds_dir / f"{game_id}.json", "w") as f:
                json.dump(output, f, indent=2)

            collected += 1
            total_plays += len(plays)

            if (i + 1) % 20 == 0:
                elapsed = time.time() - start_time
                _log.info("[%d/%d] Collected %d games, %d plays (%.1fs)",
                          i + 1, len(game_ids), collected, total_plays, elapsed)

        except RealSportsAuthError as e:
            _log.error("FATAL: Auth failure on game %d: %s", game_id, e)
            sys.exit(1)
        except Exception as e:
            _log.error("[%d/%d] Error on game %d: %s", i + 1, len(game_ids), game_id, e)
            errors += 1

        if i < len(game_ids) - 1:
            await asyncio.sleep(_INTER_GAME_DELAY)

    elapsed = time.time() - start_time
    _log.info("Game feed collection complete: %d collected, %d errors, %d total plays in %.1fs",
              collected, errors, total_plays, elapsed)

    summary = {
        "games_collected": collected,
        "games_errored": errors,
        "total_plays": total_plays,
        "elapsed_seconds": round(elapsed, 1),
    }

    summary_path = feeds_dir / "collection_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


async def _async_main(args):
    try:
        from dotenv import load_dotenv
        for env_path in [_PROJECT_ROOT / ".env"]:
            if env_path.exists():
                load_dotenv(env_path)
                break
    except ImportError:
        pass

    config = RealSportsConfig.from_env()
    missing = config.validate()
    if missing:
        _log.error("Missing credentials: %s", ", ".join(missing))
        sys.exit(1)

    client = RealSportsClient(config)
    try:
        summary = await collect_game_feeds(
            client,
            sport=args.sport,
            max_games=args.max_games,
            dry_run=args.dry_run,
            force=args.force,
        )
        if not args.dry_run and not summary.get("error"):
            print(f"\n{'='*60}")
            print("Game Feed Collection Summary")
            print(f"{'='*60}")
            print(f"  Games collected: {summary.get('games_collected', 0)}")
            print(f"  Errors:          {summary.get('games_errored', 0)}")
            print(f"  Total plays:     {summary.get('total_plays', 0)}")
            print(f"  Elapsed:         {summary.get('elapsed_seconds', 0)}s")
            print(f"{'='*60}")
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Collect game feed play-by-play data for Book C")
    parser.add_argument("--sport", default="nba")
    parser.add_argument("--max-games", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
