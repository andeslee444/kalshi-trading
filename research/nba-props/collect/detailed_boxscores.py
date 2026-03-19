"""Detailed box score collector — enriches gamelogs with advanced stats.

Fetches individual box scores for each game in collected gamelogs, adding:
- Personal fouls (pf) — for Book C foul trouble detection
- Offensive/defensive rebounds (oreb, dreb)
- Plus/minus (+/-)
- True shooting percentage
- Shot chart data (x, y, period, make/miss)
- Injury status at game time

Usage:
    python3 -m collect.detailed_boxscores
    python3 -m collect.detailed_boxscores --max-games 100 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.real_sports_client import RealSportsClient, RealSportsConfig
try:
    from shared.real_sports_client import RealSportsAuthError
except ImportError:
    RealSportsAuthError = Exception  # fallback if class was reverted

_log = logging.getLogger("collect.detailed_boxscores")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")

_INTER_REQUEST_DELAY = 0.3


def _discover_box_score_ids(data_dir: Path) -> list[dict]:
    """Extract box score IDs from raw season feed files.

    Returns list of {box_score_id, player_id, game_id, day} dicts.
    """
    entries = []
    seen_ids = set()
    raw_dir = data_dir / "raw" / "players"
    if not raw_dir.exists():
        return []

    for player_dir in raw_dir.iterdir():
        if not player_dir.is_dir():
            continue
        player_id = player_dir.name
        for sf in player_dir.glob("season_feed*.json"):
            try:
                data = json.loads(sf.read_text())
                for key in ("playerBoxScores", "boxScores", "boxscores"):
                    for bs in data.get(key, []):
                        bs_id = bs.get("id")
                        if bs_id and bs_id not in seen_ids:
                            seen_ids.add(bs_id)
                            entries.append({
                                "box_score_id": bs_id,
                                "player_id": player_id,
                                "game_id": bs.get("gameId"),
                                "day": bs.get("day", ""),
                            })
            except (json.JSONDecodeError, OSError):
                continue

    return entries


def _extract_advanced_stats(detail: dict) -> dict:
    """Extract enrichment data from a detailed box score response."""
    pbs = detail.get("playerBoxScore", {})
    if not isinstance(pbs, dict):
        return {}

    # Parse stat values into a dict
    sv_map = {}
    for sv in pbs.get("statValues", []):
        if isinstance(sv, dict):
            sv_map[sv.get("type")] = sv.get("value")

    # Shot chart
    shots = detail.get("shots", [])
    shot_data = []
    for s in shots:
        if isinstance(s, dict):
            shot_data.append({
                "x": s.get("x"),
                "y": s.get("y"),
                "period": s.get("period"),
                "made": s.get("isMadeShot", False),
            })

    return {
        "personal_fouls": sv_map.get(25, 0),  # type 25 = pf
        "offensive_rebounds": sv_map.get(23, 0),  # type 23 = oreb
        "defensive_rebounds": sv_map.get(24, 0),  # type 24 = dreb
        "plus_minus": sv_map.get(14, 0),  # type 14 = +/-
        "true_shooting_pct": sv_map.get(38, 0),  # type 38 = ts%
        "position": pbs.get("position", ""),
        "started": bool(pbs.get("started", 0)),
        "injury_status": pbs.get("injuryStatus"),
        "injury_body_part": pbs.get("injuryBodyPart"),
        "fantasy_points_fanduel": pbs.get("fantasyPointsFanduel", 0),
        "home_team_score": pbs.get("homeTeamScore"),
        "away_team_score": pbs.get("awayTeamScore"),
        "shots": shot_data,
        "shot_count": len(shot_data),
    }


async def collect_detailed_boxscores(
    client: RealSportsClient,
    max_games: int = 500,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Fetch detailed box scores and save enrichment data."""
    data_dir = _PROJECT_ROOT / "data"
    enrichment_dir = data_dir / "processed" / "boxscore_details"
    enrichment_dir.mkdir(parents=True, exist_ok=True)

    entries = _discover_box_score_ids(data_dir)
    _log.info("Discovered %d box score IDs from raw feeds", len(entries))

    if not entries:
        _log.error("No box score IDs found. Run batch_collector first.")
        return {"error": "no_boxscores", "collected": 0}

    # Filter to uncollected
    if not force:
        cached = len([e for e in entries if (enrichment_dir / f"{e['box_score_id']}.json").exists()])
        entries = [e for e in entries if not (enrichment_dir / f"{e['box_score_id']}.json").exists()]
        _log.info("Cached: %d, Need collection: %d", cached, len(entries))

    if len(entries) > max_games:
        entries = entries[:max_games]
        _log.info("Capped to %d (--max-games)", max_games)

    if dry_run:
        print(f"DRY RUN: would fetch {len(entries)} detailed box scores")
        return {"dry_run": True, "to_collect": len(entries)}

    collected = 0
    errors = 0
    total_shots = 0
    start_time = time.time()

    for i, entry in enumerate(entries):
        bs_id = entry["box_score_id"]
        try:
            detail = await client.get_player_box_score(bs_id)
            if not isinstance(detail, dict):
                errors += 1
                continue

            enrichment = _extract_advanced_stats(detail)
            enrichment["box_score_id"] = bs_id
            enrichment["player_id"] = entry["player_id"]
            enrichment["game_id"] = entry["game_id"]
            enrichment["day"] = entry["day"]
            enrichment["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            with open(enrichment_dir / f"{bs_id}.json", "w") as f:
                json.dump(enrichment, f, indent=2)

            collected += 1
            total_shots += enrichment.get("shot_count", 0)

            if (i + 1) % 50 == 0:
                elapsed = time.time() - start_time
                _log.info("[%d/%d] Collected %d box scores, %d shots (%.1fs)",
                          i + 1, len(entries), collected, total_shots, elapsed)

        except RealSportsAuthError as e:
            _log.error("FATAL: Auth failure: %s", e)
            sys.exit(1)
        except Exception as e:
            _log.error("[%d/%d] Error on box score %s: %s", i + 1, len(entries), bs_id, e)
            errors += 1

        if i < len(entries) - 1:
            await asyncio.sleep(_INTER_REQUEST_DELAY)

    elapsed = time.time() - start_time
    _log.info("Complete: %d collected, %d errors, %d shots in %.1fs",
              collected, errors, total_shots, elapsed)

    summary = {
        "collected": collected,
        "errors": errors,
        "total_shots": total_shots,
        "elapsed_seconds": round(elapsed, 1),
    }
    with open(enrichment_dir / "collection_summary.json", "w") as f:
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
    client = RealSportsClient(config)
    try:
        summary = await collect_detailed_boxscores(
            client, max_games=args.max_games, dry_run=args.dry_run, force=args.force,
        )
        if not args.dry_run and not summary.get("error"):
            print(f"\n{'='*60}")
            print("Detailed Box Score Collection Summary")
            print(f"{'='*60}")
            print(f"  Collected:    {summary.get('collected', 0)}")
            print(f"  Errors:       {summary.get('errors', 0)}")
            print(f"  Total shots:  {summary.get('total_shots', 0)}")
            print(f"  Elapsed:      {summary.get('elapsed_seconds', 0)}s")
            print(f"{'='*60}")
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Collect detailed box scores with advanced stats")
    parser.add_argument("--max-games", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
