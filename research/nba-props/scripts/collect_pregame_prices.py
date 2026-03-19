"""Pre-game Kalshi orderbook snapshot — run ~1 hour before NBA tipoff.

Captures a comprehensive snapshot of all open NBA prop orderbooks so the
backtester can use real pre-game prices instead of proxy estimates. Extracts
the game schedule from market tickers and maintains an index file for
date-based lookups.

Usage:
    python3 scripts/collect_pregame_prices.py          # capped at 500 markets
    python3 scripts/collect_pregame_prices.py --all     # no cap, full coverage
    python3 scripts/collect_pregame_prices.py --dry-run # preview without fetching
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collect.kalshi_orderbook import snapshot_orderbooks, NBA_PROP_SERIES

_log = logging.getLogger("collect.pregame_prices")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

_SNAPSHOT_DIR = _PROJECT_ROOT / "data" / "processed" / "kalshi_orderbooks"
_INDEX_PATH = _SNAPSHOT_DIR / "index.json"

_TICKER_DATE_RE = re.compile(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})")
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_TICKER_TEAMS_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}([A-Z]{6})$")


def _parse_game_date(ticker: str) -> str | None:
    """Extract game date as YYYY-MM-DD from a Kalshi ticker."""
    m = _TICKER_DATE_RE.match(ticker)
    if not m:
        return None
    dd, mon_str, yy = m.group(1), m.group(2), m.group(3)
    month = _MONTH_MAP.get(mon_str.upper())
    if not month:
        return None
    year = 2000 + int(yy)
    try:
        return f"{year:04d}-{month:02d}-{int(dd):02d}"
    except ValueError:
        return None


def _parse_matchup(ticker: str) -> str | None:
    """Extract team matchup from ticker (e.g. 'OKC vs BKN')."""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    date_teams = parts[1]
    m = _TICKER_TEAMS_RE.match(date_teams)
    if not m:
        return None
    teams_str = m.group(1)
    if len(teams_str) == 6:
        return f"{teams_str[:3]} vs {teams_str[3:]}"
    return None


def _extract_schedule(snapshots: list[dict]) -> list[dict]:
    """Build today's game schedule from market tickers."""
    games = {}
    for snap in snapshots:
        ticker = snap.get("ticker", "")
        game_date = _parse_game_date(ticker)
        matchup = _parse_matchup(ticker)
        if not game_date or not matchup:
            continue
        key = f"{game_date}_{matchup}"
        if key not in games:
            games[key] = {"date": game_date, "matchup": matchup, "market_count": 0, "series": set()}
        games[key]["market_count"] += 1
        series = ticker.split("-")[0] if "-" in ticker else ""
        if series:
            games[key]["series"].add(series)
    result = []
    for g in sorted(games.values(), key=lambda x: (x["date"], x["matchup"])):
        g["series"] = sorted(g["series"])
        result.append(g)
    return result


def _load_index() -> dict:
    if _INDEX_PATH.exists():
        try:
            return json.loads(_INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            _log.warning("Corrupt index.json, starting fresh")
    return {"snapshots": []}


def _save_index(index: dict) -> None:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    tmp.rename(_INDEX_PATH)


def collect_pregame_prices(
    all_markets: bool = False,
    dry_run: bool = False,
    series_list: list[str] | None = None,
) -> dict:
    """Snapshot all open NBA prop orderbooks and update the index."""
    if series_list is None:
        series_list = list(NBA_PROP_SERIES)

    max_markets = 999_999 if all_markets else 500
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    if dry_run:
        print(f"DRY RUN: would snapshot orderbooks for {len(series_list)} series")
        print(f"  Max markets: {'unlimited' if all_markets else 500}")
        print(f"  Date: {today_str}")
        return {"dry_run": True}

    _log.info("Taking pre-game orderbook snapshot (max_markets=%s)...",
              "unlimited" if all_markets else max_markets)
    result = snapshot_orderbooks(series_list=series_list, max_markets=max_markets)

    snapshot_file = result.get("snapshot_file", "")
    if not snapshot_file:
        _log.error("snapshot_orderbooks returned no snapshot_file")
        return {"error": "no_snapshot_file"}

    snapshot_path = Path(snapshot_file)
    snapshot_data = json.loads(snapshot_path.read_text())
    snapshots = snapshot_data.get("snapshots", [])
    schedule = _extract_schedule(snapshots)

    tight_spread_count = sum(
        1 for s in snapshots
        if s.get("yes_bid", 0) > 0 and s.get("yes_ask", 0) > 0 and s.get("spread", 1.0) < 0.10
    )
    tradeable_count = sum(
        1 for s in snapshots
        if s.get("yes_bid", 0) > 0 and s.get("yes_ask", 0) > 0 and s.get("spread", 1.0) < 0.50
    )

    snapshot_data["schedule"] = schedule
    snapshot_data["pregame_meta"] = {
        "collection_type": "pregame",
        "collected_at_utc": now_utc.isoformat(timespec="seconds"),
        "game_date": today_str,
        "all_markets": all_markets,
        "tight_spreads_lt_010": tight_spread_count,
        "tradeable_lt_050": tradeable_count,
        "games_found": len(schedule),
    }
    with open(snapshot_path, "w") as f:
        json.dump(snapshot_data, f, indent=2)

    index = _load_index()
    existing_files = {e["snapshot_file"] for e in index["snapshots"]}
    rel_file = snapshot_path.name

    if rel_file not in existing_files:
        index["snapshots"].append({
            "date": today_str,
            "snapshot_file": rel_file,
            "collected_at_utc": now_utc.isoformat(timespec="seconds"),
            "markets_total": result.get("markets", 0),
            "markets_with_depth": result.get("with_depth", 0),
            "tight_spreads_lt_010": tight_spread_count,
            "tradeable_lt_050": tradeable_count,
            "games": len(schedule),
            "game_matchups": [g["matchup"] for g in schedule],
        })
        _save_index(index)
        _log.info("Index updated: %d total snapshots", len(index["snapshots"]))

    print(f"\n{'='*60}")
    print("Pre-Game Orderbook Snapshot Summary")
    print(f"{'='*60}")
    print(f"  Date:             {today_str}")
    print(f"  Total markets:    {result.get('markets', 0)}")
    print(f"  Tight spread <10c: {tight_spread_count}")
    print(f"  Tradeable <50c:   {tradeable_count}")
    print(f"  Games found:      {len(schedule)}")
    if schedule:
        print(f"\n  Today's Games:")
        for g in schedule:
            print(f"    {g['matchup']:<15} {g['market_count']:>3} markets  ({', '.join(g['series'])})")
    print(f"{'='*60}")

    return {
        "snapshot_file": str(snapshot_path),
        "markets": result.get("markets", 0),
        "tight_spreads": tight_spread_count,
        "tradeable": tradeable_count,
        "games": len(schedule),
    }


def main():
    parser = argparse.ArgumentParser(description="Pre-game Kalshi orderbook snapshot")
    parser.add_argument("--all", action="store_true", help="Fetch all markets (no cap)")
    parser.add_argument("--series", type=str, default=",".join(NBA_PROP_SERIES))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    series = [s.strip() for s in args.series.split(",")]
    collect_pregame_prices(all_markets=args.all, dry_run=args.dry_run, series_list=series)


if __name__ == "__main__":
    main()
