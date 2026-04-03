#!/usr/bin/env python3
"""Oracle latency probe for Real Sports live events versus Kalshi quotes.

This is a research-only collector. It does not place trades.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time

from kalshi_auth import (
    KalshiClient,
    PROJECT_DIR,
    is_shutdown_requested,
    setup_logging,
    setup_signal_handlers,
    setup_unbuffered,
)

from domain.oracle.alpha_capture import DEFAULT_ORACLE_ALPHA_LEDGER_PATH, OracleAlphaCapture
from domain.oracle.kalshi_orderbook_stream import KalshiOrderbookStream
from domain.oracle.latency_probe import OracleLatencyProbe
from domain.oracle.real_sports_client import (
    RealSportsClient,
    RealSportsConfig,
    RealSportsWebSocket,
    parse_game_market_response,
    parse_home_game_response,
)


BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"

log = logging.getLogger("oracle.latency_probe.app")
KALSHI_GAME_MARKET_PREFIX = "KXNBAGAME"
KALSHI_PROP_MARKET_PREFIXES = (
    "KXNBAPTS",
    "KXNBAREB",
    "KXNBAAST",
    "KXNBA3PM",
    "KXNBASTL",
    "KXNBABLK",
    "KXNBATO",
)
RUNTIME_EVENT_DROUGHT_SECONDS = 120.0
LIVE_GAME_STATUSES = frozenset({"inprogress", "live", "active"})


def _build_oracle_kalshi_client():
    """Use Oracle-specific Kalshi credentials so the shared env can stay demo."""
    api_key = os.environ.get("ORACLE_KALSHI_API_KEY") or None
    key_path = os.environ.get("ORACLE_KALSHI_KEY_FILE") or None
    mode = os.environ.get("ORACLE_KALSHI_MODE") or None
    confirm_production = None
    if "ORACLE_KALSHI_CONFIRM_PRODUCTION" in os.environ:
        confirm_production = os.environ.get("ORACLE_KALSHI_CONFIRM_PRODUCTION") == "yes"
    if api_key is None and key_path is None and mode is None and confirm_production is None:
        return KalshiClient()
    try:
        return KalshiClient(
            api_key=api_key,
            key_path=key_path,
            mode=mode,
            confirm_production=confirm_production,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return KalshiClient()


def _load_oracle_config() -> dict:
    try:
        return json.loads(BOTS_CONFIG_PATH.read_text()).get("oracle", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_home_games(home_feed: dict) -> tuple[str | None, list[dict]]:
    latest_day = home_feed.get("latestDay") if isinstance(home_feed, dict) else None
    latest_day_content = home_feed.get("latestDayContent", {}) if isinstance(home_feed, dict) else {}
    games_raw = latest_day_content.get("games", []) if isinstance(latest_day_content, dict) else []
    games = []
    for game in games_raw:
        if not isinstance(game, dict):
            continue
        parsed = parse_home_game_response(game)
        if latest_day and not parsed.get("scheduled_day"):
            parsed["scheduled_day"] = latest_day
        games.append(parsed)
    return latest_day, games


def _schedule_game_count(schedule: dict, day: str | None) -> int | None:
    if not day or not isinstance(schedule, dict):
        return None
    days = schedule.get("days", [])
    if not isinstance(days, list):
        return None
    for entry in days:
        if not isinstance(entry, dict):
            continue
        if entry.get("day") != day:
            continue
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0
    return None


def _coerce_int(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_box_scores(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("playerBoxScores", "boxScores", "boxscores"):
        value = payload.get(key)
        if isinstance(value, list):
            rows = [row for row in value if isinstance(row, dict)]
            if rows:
                return rows
    for key in ("game", "data", "liveGameInfo"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            rows = _find_box_scores(nested)
            if rows:
                return rows
    return []


def _full_name_from_person(person: dict | None) -> str:
    if not isinstance(person, dict):
        return ""
    for key in ("name", "fullName"):
        value = person.get(key)
        if value:
            return str(value).strip()
    first = str(person.get("firstName", "")).strip()
    last = str(person.get("lastName", "")).strip()
    if first or last:
        return f"{first} {last}".strip()
    display = person.get("displayName")
    if display and "." not in str(display):
        return str(display).strip()
    return ""


def _team_name_from_play_participant(play: dict, participant: dict | None) -> str:
    if not isinstance(play, dict):
        return ""
    if isinstance(participant, dict):
        participant_team_id = _coerce_int(participant.get("teamId"))
        if participant_team_id is not None:
            for key in ("team", "homeTeam", "awayTeam"):
                team = play.get(key)
                if not isinstance(team, dict):
                    continue
                if _coerce_int(team.get("id")) == participant_team_id:
                    return str(team.get("name") or team.get("displayName") or team.get("key") or "").strip()
    team = play.get("team")
    if isinstance(team, dict):
        return str(team.get("name") or team.get("displayName") or team.get("key") or "").strip()
    return ""


def _player_name_from_box_score(box_score: dict) -> str:
    for key in ("playerName", "name"):
        value = box_score.get(key)
        if value:
            return str(value).strip()
    player = box_score.get("player")
    if isinstance(player, dict):
        for key in ("name", "displayName", "fullName"):
            value = player.get(key)
            if value:
                return str(value).strip()
        first = str(player.get("firstName", "")).strip()
        last = str(player.get("lastName", "")).strip()
        if first or last:
            return f"{first} {last}".strip()
    return ""


def _team_name_from_box_score(box_score: dict, game: dict) -> str:
    for key in ("teamName", "teamAbbreviation", "team"):
        value = box_score.get(key)
        if isinstance(value, dict):
            for nested_key in ("name", "abbreviation", "displayName"):
                nested_value = value.get(nested_key)
                if nested_value:
                    return str(nested_value).strip()
        elif value:
            return str(value).strip()

    team_id = box_score.get("teamId")
    home_team_id = box_score.get("homeTeamId")
    away_team_id = box_score.get("awayTeamId")
    if team_id and home_team_id and team_id == home_team_id:
        return str(game.get("home_team", "")).strip()
    if team_id and away_team_id and team_id == away_team_id:
        return str(game.get("away_team", "")).strip()
    return ""


def _extract_player_market_contexts(game: dict, game_detail: dict) -> list[dict]:
    contexts = []
    seen: set[tuple[int, str, str]] = set()

    def add_context(player_id, player_name: str, team_name: str) -> None:
        normalized_name = str(player_name or "").strip()
        normalized_team = str(team_name or "").strip()
        parsed_player_id = _coerce_int(player_id)
        if parsed_player_id is None or not normalized_name or not normalized_team:
            return
        dedupe_key = (parsed_player_id, normalized_team.lower(), normalized_name.lower())
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        contexts.append(
            {
                "game_id": game.get("game_id"),
                "player_id": parsed_player_id,
                "player_name": normalized_name,
                "team": normalized_team,
                "scheduled_day": game.get("scheduled_day"),
                "start_time": game.get("start_time"),
            }
        )

    for box_score in _find_box_scores(game_detail):
        if box_score.get("didNotPlay") is True:
            continue
        player_name = _player_name_from_box_score(box_score)
        team_name = _team_name_from_box_score(box_score, game)
        if not player_name or not team_name:
            continue
        player_id = _coerce_int(box_score.get("playerId"))
        player = box_score.get("player")
        if player_id is None and isinstance(player, dict):
            player_id = _coerce_int(player.get("id"))
        add_context(player_id, player_name, team_name)

    plays = game_detail.get("plays")
    if isinstance(plays, list):
        for play in plays:
            if not isinstance(play, dict):
                continue
            primary_player = play.get("primaryPlayer")
            secondary_player = play.get("secondaryPlayer")
            add_context(
                play.get("primaryPlayerId"),
                _full_name_from_person(primary_player),
                _team_name_from_play_participant(play, primary_player),
            )
            add_context(
                play.get("secondaryPlayerId"),
                _full_name_from_person(secondary_player),
                _team_name_from_play_participant(play, secondary_player),
            )
    return contexts


async def _fetch_player_market_contexts(real_client: RealSportsClient, discovery_games: list[dict]) -> list[dict]:
    games = [game for game in discovery_games if game.get("game_id") not in (None, "")]
    if not games:
        return []
    details = await asyncio.gather(
        *[
            real_client.get_game_detail(int(game["game_id"]), "nba")
            for game in games
        ],
        return_exceptions=True,
    )
    contexts = []
    for game, detail in zip(games, details):
        if isinstance(detail, Exception) or not isinstance(detail, dict):
            continue
        contexts.extend(_extract_player_market_contexts(game, detail))
    return contexts


async def _fetch_market_context(real_client: RealSportsClient, kalshi_client: KalshiClient):
    schedule = await real_client.get_game_schedule("nba")
    home_feed = await real_client.get_home_feed("nba")
    crowd_markets_raw = await real_client.get_game_markets("nba")
    crowd_markets = [parse_game_market_response(m) for m in crowd_markets_raw]
    latest_day, discovery_games = _extract_home_games(home_feed)
    kalshi_markets, kalshi_prop_markets = await asyncio.gather(
        _fetch_kalshi_game_markets(kalshi_client),
        _fetch_kalshi_prop_markets(kalshi_client),
    )
    player_market_contexts = await _fetch_player_market_contexts(real_client, discovery_games)
    return {
        "schedule": schedule,
        "home_feed": home_feed,
        "latest_day": latest_day,
        "expected_game_count": _schedule_game_count(schedule, latest_day),
        "discovery_games": discovery_games,
        "player_market_contexts": player_market_contexts,
        "crowd_markets": crowd_markets,
        "kalshi_markets": kalshi_markets,
        "kalshi_prop_markets": kalshi_prop_markets,
    }


async def _fetch_kalshi_series_markets(
    kalshi_client: KalshiClient,
    *,
    series_ticker: str,
    status: str = "open",
    max_pages: int = 10,
) -> list[dict]:
    def _fetch() -> list[dict]:
        markets = []
        cursor = None
        for _ in range(max_pages):
            path = f"/markets?series_ticker={series_ticker}&status={status}&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            data = kalshi_client.get(path)
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return markets

    return await asyncio.to_thread(_fetch)


async def _fetch_kalshi_game_markets(
    kalshi_client: KalshiClient,
    *,
    series_ticker: str = KALSHI_GAME_MARKET_PREFIX,
    status: str = "open",
    max_pages: int = 10,
) -> list[dict]:
    return await _fetch_kalshi_series_markets(
        kalshi_client,
        series_ticker=series_ticker,
        status=status,
        max_pages=max_pages,
    )


async def _fetch_kalshi_prop_markets(
    kalshi_client: KalshiClient,
    *,
    series_tickers: tuple[str, ...] = KALSHI_PROP_MARKET_PREFIXES,
    status: str = "open",
    max_pages: int = 10,
) -> list[dict]:
    batches = await asyncio.gather(
        *[
            _fetch_kalshi_series_markets(
                kalshi_client,
                series_ticker=series_ticker,
                status=status,
                max_pages=max_pages,
            )
            for series_ticker in series_tickers
        ]
    )
    deduped: dict[str, dict] = {}
    for batch in batches:
        for market in batch:
            ticker = market.get("ticker", "")
            if ticker:
                deduped[ticker] = market
    return [deduped[ticker] for ticker in sorted(deduped)]


def _fatal_source_failures(context: dict) -> list[dict]:
    failures = []
    schedule = context.get("schedule")
    latest_day = context.get("latest_day")
    expected_game_count = context.get("expected_game_count")
    discovery_games = context.get("discovery_games", [])
    kalshi_markets = context.get("kalshi_markets", [])

    if not isinstance(schedule, dict) or not isinstance(schedule.get("days"), list) or not schedule.get("days"):
        failures.append(
            {
                "source_name": "real_schedule",
                "failure_code": "missing_schedule_days",
                "message": "Primary Real schedule returned no NBA day list",
                "extra": {"latest_day": latest_day, "kalshi_markets_count": len(kalshi_markets)},
            }
        )
        return failures

    if not latest_day:
        failures.append(
            {
                "source_name": "real_home_next",
                "failure_code": "missing_latest_day",
                "message": "Primary Real home feed did not provide latestDay",
                "extra": {"expected_game_count": expected_game_count, "kalshi_markets_count": len(kalshi_markets)},
            }
        )
    elif expected_game_count is None:
        failures.append(
            {
                "source_name": "real_schedule",
                "failure_code": "missing_latest_day_entry",
                "message": f"Primary Real schedule did not contain latestDay {latest_day}",
                "extra": {"latest_day": latest_day, "discovery_games_count": len(discovery_games)},
            }
        )
    elif expected_game_count > 0 and not discovery_games:
        failures.append(
            {
                "source_name": "real_home_next",
                "failure_code": "empty_same_day_games",
                "message": f"Primary Real home feed returned 0 games for latestDay {latest_day}",
                "extra": {"latest_day": latest_day, "expected_game_count": expected_game_count},
            }
        )
    elif expected_game_count == 0 and discovery_games:
        failures.append(
            {
                "source_name": "real_schedule",
                "failure_code": "schedule_home_mismatch",
                "message": f"Real schedule shows 0 games for {latest_day} but home feed returned games",
                "extra": {"latest_day": latest_day, "discovery_games_count": len(discovery_games)},
            }
        )

    if not kalshi_markets:
        failures.append(
            {
                "source_name": "kalshi_open_game_markets",
                "failure_code": "empty_tradeable_universe",
                "message": f"Primary Kalshi {KALSHI_GAME_MARKET_PREFIX} query returned 0 open markets",
                "extra": {"latest_day": latest_day, "discovery_games_count": len(discovery_games), "kalshi_markets_count": 0},
            }
        )
    return failures


def _crowd_source_failures(context: dict) -> list[dict]:
    crowd_markets = context.get("crowd_markets", [])
    if crowd_markets:
        return []
    return [
        {
            "source_name": "real_sports_game_markets",
            "failure_code": "empty_crowd_price_response",
            "message": "Book A crowd source returned 0 NBA game markets",
            "extra": {
                "latest_day": context.get("latest_day"),
                "expected_game_count": context.get("expected_game_count"),
                "discovery_games_count": len(context.get("discovery_games", [])),
            },
        }
    ]


def _record_source_failures(capture: OracleAlphaCapture, failures: list[dict]) -> list[dict]:
    rows = []
    for failure in failures:
        rows.append(
            capture.record_source_failure(
                source_name=failure["source_name"],
                failure_code=failure["failure_code"],
                message=failure["message"],
                extra=failure.get("extra"),
            )
        )
    return rows


def _mapping_games(context: dict) -> list[dict]:
    return list(context.get("discovery_games", []))


def _mapping_kalshi_markets(context: dict) -> list[dict]:
    return list(context.get("kalshi_markets", []))


def _mapping_kalshi_prop_markets(context: dict) -> list[dict]:
    return list(context.get("kalshi_prop_markets", []))


def _mapping_player_contexts(context: dict) -> list[dict]:
    return list(context.get("player_market_contexts", []))


def _mapped_tickers(index: dict[str, list[str]]) -> list[str]:
    return sorted({ticker for tickers in index.values() for ticker in tickers})


def _is_live_game(game: dict) -> bool:
    status = str(game.get("status", "")).strip().lower()
    return status in LIVE_GAME_STATUSES


def _count_live_mapped_games(context: dict, index: dict[str, list[str]]) -> int:
    mapped_game_ids = set(index.keys())
    if not mapped_game_ids:
        return 0
    count = 0
    for game in context.get("discovery_games", []):
        game_id = game.get("game_id")
        if game_id in (None, ""):
            continue
        if str(game_id) not in mapped_game_ids:
            continue
        if _is_live_game(game):
            count += 1
    return count


def _runtime_health_failures(
    *,
    context: dict,
    index: dict[str, list[str]],
    probe: OracleLatencyProbe,
    real_ws: RealSportsWebSocket | None = None,
    kalshi_stream: KalshiOrderbookStream | None = None,
    event_drought_seconds: float = RUNTIME_EVENT_DROUGHT_SECONDS,
    now: float | None = None,
) -> list[dict]:
    now_ts = time.time() if now is None else float(now)
    discovery_games = list(context.get("discovery_games", []))
    mapped_tickers_count = sum(len(v) for v in index.values())
    live_mapped_games = _count_live_mapped_games(context, index)
    prop_markets_count = len(context.get("kalshi_prop_markets", []))
    player_contexts_count = len(context.get("player_market_contexts", []))
    player_prop_index = getattr(probe, "player_prop_market_index", {})
    player_prop_mapping_diagnostics = getattr(probe, "player_prop_mapping_diagnostics", {})
    player_prop_tickers_count = (
        sum(len(v) for v in player_prop_index.values())
        if isinstance(player_prop_index, dict)
        else 0
    )
    mapped_players_count = int(player_prop_mapping_diagnostics.get("mapped_players") or 0)
    unmapped_players_count = int(player_prop_mapping_diagnostics.get("unmapped_players") or 0)
    failures = []

    if discovery_games and mapped_tickers_count == 0:
        failures.append(
            {
                "source_name": "oracle_mapping",
                "failure_code": "zero_mapped_tickers",
                "message": "Oracle mapped 0 Kalshi tickers despite discovery games being present",
                "extra": {
                    "latest_day": context.get("latest_day"),
                    "discovery_games_count": len(discovery_games),
                    "kalshi_markets_count": len(context.get("kalshi_markets", [])),
                    "live_mapped_games_count": live_mapped_games,
                },
            }
        )

    if live_mapped_games <= 0:
        return failures

    if prop_markets_count > 0 and player_contexts_count <= 0:
        failures.append(
            {
                "source_name": "oracle_player_contexts",
                "failure_code": "zero_player_contexts",
                "message": "Oracle extracted 0 player contexts while live mapped games and Kalshi prop markets are active",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "kalshi_prop_markets_count": prop_markets_count,
                    "player_contexts_count": player_contexts_count,
                },
            }
        )
    elif prop_markets_count > 0 and player_prop_tickers_count <= 0:
        failures.append(
            {
                "source_name": "oracle_mapping",
                "failure_code": "zero_mapped_prop_tickers",
                "message": "Oracle mapped 0 Kalshi prop tickers despite active live games and available player contexts",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "kalshi_prop_markets_count": prop_markets_count,
                    "player_contexts_count": player_contexts_count,
                    "mapped_prop_tickers_count": player_prop_tickers_count,
                },
            }
        )
    elif unmapped_players_count > 0 and mapped_players_count > 0:
        failures.append(
            {
                "source_name": "oracle_mapping",
                "failure_code": "unmapped_prop_players",
                "message": "Oracle left some live player contexts unmapped to Kalshi prop tickers",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "kalshi_prop_markets_count": prop_markets_count,
                    "player_contexts_count": player_contexts_count,
                    "mapped_players_count": mapped_players_count,
                    "unmapped_players_count": unmapped_players_count,
                    "reason_counts": player_prop_mapping_diagnostics.get("reason_counts", {}),
                    "examples": player_prop_mapping_diagnostics.get("examples", {}),
                },
            }
        )

    if real_ws is None or not real_ws.connected:
        failures.append(
            {
                "source_name": "real_live_websocket",
                "failure_code": "disconnected",
                "message": "Real Sports WebSocket is disconnected while mapped live games are active",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "mapped_tickers_count": mapped_tickers_count,
                },
            }
        )
    elif real_ws.is_disabled:
        failures.append(
            {
                "source_name": "real_live_websocket",
                "failure_code": "disabled",
                "message": "Real Sports WebSocket is disabled while mapped live games are active",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "mapped_tickers_count": mapped_tickers_count,
                },
            }
        )
    elif real_ws.is_stale:
        failures.append(
            {
                "source_name": "real_live_websocket",
                "failure_code": "stale",
                "message": "Real Sports WebSocket is stale while mapped live games are active",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "mapped_tickers_count": mapped_tickers_count,
                    "data_age_seconds": round(real_ws.data_age_seconds, 1),
                },
            }
        )
    else:
        mapped_event_age = probe.last_mapped_source_event_age_seconds(now=now_ts)
        if mapped_event_age is None or mapped_event_age > event_drought_seconds:
            failures.append(
                {
                    "source_name": "oracle_mapping",
                    "failure_code": "mapped_event_drought",
                    "message": "Oracle has mapped live games but has not captured a mapped Real event recently",
                    "extra": {
                        "live_mapped_games_count": live_mapped_games,
                        "mapped_tickers_count": mapped_tickers_count,
                        "mapped_source_event_count": probe.mapped_source_event_count,
                        "last_mapped_event_age_seconds": None if mapped_event_age is None else round(mapped_event_age, 1),
                        "event_drought_seconds": float(event_drought_seconds),
                    },
                }
            )

    if kalshi_stream is not None and not kalshi_stream.connected:
        failures.append(
            {
                "source_name": "kalshi_orderbook_ws",
                "failure_code": "disconnected",
                "message": "Kalshi orderbook WebSocket is disconnected while mapped live games are active",
                "extra": {
                    "live_mapped_games_count": live_mapped_games,
                    "mapped_tickers_count": mapped_tickers_count,
                },
            }
        )

    return failures


class _RuntimeFailureTracker:
    def __init__(self):
        self.active_keys: set[tuple[str, str]] = set()

    def apply(self, capture: OracleAlphaCapture, failures: list[dict]) -> None:
        next_keys = set()
        for failure in failures:
            key = (failure["source_name"], failure["failure_code"])
            next_keys.add(key)
            if key in self.active_keys:
                continue
            capture.record_source_failure(
                source_name=failure["source_name"],
                failure_code=failure["failure_code"],
                message=failure["message"],
                extra=failure.get("extra"),
            )
            log.warning("Latency probe runtime health failure: %s", failure["message"])
        for source_name, failure_code in sorted(self.active_keys - next_keys):
            log.info("Latency probe runtime health recovered: %s/%s", source_name, failure_code)
        self.active_keys = next_keys


def _parse_followup_delays(value: str | None) -> tuple[float, ...]:
    if value is None:
        return OracleLatencyProbe.DEFAULT_FOLLOWUP_DELAYS_SECONDS
    text = str(value).strip()
    if not text:
        return ()
    delays = []
    for piece in text.split(","):
        part = piece.strip()
        if not part:
            continue
        delay = float(part)
        if delay < 0:
            raise ValueError(f"Follow-up delay must be non-negative, got {part}")
        delays.append(delay)
    return tuple(delays)


async def _refresh_loop(
    probe: OracleLatencyProbe,
    capture: OracleAlphaCapture,
    real_client: RealSportsClient,
    kalshi_client: KalshiClient,
    refresh_seconds: int,
    kalshi_stream: KalshiOrderbookStream | None = None,
    real_ws: RealSportsWebSocket | None = None,
    runtime_failures: _RuntimeFailureTracker | None = None,
):
    while not is_shutdown_requested():
        try:
            context = await _fetch_market_context(real_client, kalshi_client)
            fatal_failures = _fatal_source_failures(context)
            if fatal_failures:
                _record_source_failures(capture, fatal_failures)
                probe.refresh_game_market_index([], [])
                log.error(
                    "Latency probe cleared game index after fatal source failure: %s",
                    "; ".join(f["message"] for f in fatal_failures),
                )
                await asyncio.sleep(refresh_seconds)
                continue
            crowd_failures = _crowd_source_failures(context)
            if crowd_failures:
                _record_source_failures(capture, crowd_failures)
                log.warning(
                    "Latency probe crowd source unavailable: %s",
                    "; ".join(f["message"] for f in crowd_failures),
                )
            index = probe.refresh_market_indexes(
                _mapping_games(context),
                _mapping_kalshi_markets(context),
                _mapping_player_contexts(context),
                _mapping_kalshi_prop_markets(context),
            )
            if kalshi_stream is not None:
                await kalshi_stream.set_market_tickers(_mapped_tickers(index))
            if runtime_failures is not None:
                runtime_failures.apply(
                    capture,
                    _runtime_health_failures(
                        context=context,
                        index=index,
                        probe=probe,
                        real_ws=real_ws,
                        kalshi_stream=kalshi_stream,
                    ),
                )
            mapping_diagnostics = probe.player_prop_mapping_diagnostics
            log.info(
                "Latency probe refreshed market index: %d discovery games, %d player contexts, %d crowd markets, %d game markets, %d prop markets, %d mapped tickers, %d mapped prop players, %d unmapped prop players",
                len(_mapping_games(context)),
                len(_mapping_player_contexts(context)),
                len(context.get("crowd_markets", [])),
                len(_mapping_kalshi_markets(context)),
                len(_mapping_kalshi_prop_markets(context)),
                sum(len(v) for v in index.values()),
                int(mapping_diagnostics.get("mapped_players") or 0),
                int(mapping_diagnostics.get("unmapped_players") or 0),
            )
            if mapping_diagnostics.get("reason_counts"):
                log.info(
                    "Latency probe prop mapping diagnostics: %s",
                    mapping_diagnostics.get("reason_counts", {}),
                )
        except Exception as exc:
            log.warning("Latency probe market-index refresh failed: %s", exc)
        await asyncio.sleep(refresh_seconds)


async def async_main(
    once: bool = False,
    duration_seconds: int = 0,
    refresh_seconds: int = 300,
    followup_delays: tuple[float, ...] | None = None,
):
    oracle_cfg = _load_oracle_config()
    real_cfg = RealSportsConfig.from_bots_config(oracle_cfg)
    missing = real_cfg.validate()
    if missing:
        raise SystemExit(
            "Missing Real Sports credentials for latency probe: " + ", ".join(missing)
        )

    real_client = RealSportsClient(real_cfg)
    if real_cfg.can_auto_login:
        ok = await real_client.login()
        if not ok:
            raise SystemExit("Real Sports auto-login failed for latency probe")

    kalshi_client = _build_oracle_kalshi_client()
    capture = OracleAlphaCapture(
        path=oracle_cfg.get("research", {}).get("alphaLedgerPath", DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        logger=log,
    )
    kalshi_stream = None if once else KalshiOrderbookStream(kalshi_client, logger=log)
    if kalshi_stream is not None:
        try:
            await kalshi_stream.connect()
        except Exception as exc:
            log.warning("Latency probe Kalshi WebSocket unavailable, falling back to REST orderbooks: %s", exc)
            kalshi_stream = None
    probe = OracleLatencyProbe(
        kalshi_client,
        capture,
        followup_delays_seconds=followup_delays,
        fetch_quote_func=(kalshi_stream.get_quote if kalshi_stream is not None else None),
        logger=log,
    )

    context = await _fetch_market_context(real_client, kalshi_client)
    fatal_failures = _fatal_source_failures(context)
    if fatal_failures:
        _record_source_failures(capture, fatal_failures)
        await real_client.close()
        raise SystemExit(
            "Latency probe failing closed: " + "; ".join(f["message"] for f in fatal_failures)
        )
    crowd_failures = _crowd_source_failures(context)
    if crowd_failures:
        _record_source_failures(capture, crowd_failures)
        log.warning(
            "Latency probe crowd source unavailable at startup: %s",
            "; ".join(f["message"] for f in crowd_failures),
        )

    index = probe.refresh_market_indexes(
        _mapping_games(context),
        _mapping_kalshi_markets(context),
        _mapping_player_contexts(context),
        _mapping_kalshi_prop_markets(context),
    )
    if kalshi_stream is not None:
        await kalshi_stream.set_market_tickers(_mapped_tickers(index))
    mapping_diagnostics = probe.player_prop_mapping_diagnostics
    log.info(
        "Latency probe initialized: %d discovery games, %d player contexts, %d crowd markets, %d Kalshi game markets, %d Kalshi prop markets, %d mapped games, %d mapped prop players, %d unmapped prop players",
        len(_mapping_games(context)),
        len(_mapping_player_contexts(context)),
        len(context.get("crowd_markets", [])),
        len(_mapping_kalshi_markets(context)),
        len(_mapping_kalshi_prop_markets(context)),
        len(index),
        int(mapping_diagnostics.get("mapped_players") or 0),
        int(mapping_diagnostics.get("unmapped_players") or 0),
    )
    if mapping_diagnostics.get("reason_counts"):
        log.info(
            "Latency probe prop mapping diagnostics at startup: %s",
            mapping_diagnostics.get("reason_counts", {}),
        )

    baseline_snapshots = await probe.capture_market_index_snapshot(
        extra={
            "latest_day": context.get("latest_day"),
            "expected_game_count": context.get("expected_game_count"),
            "discovery_games_count": len(_mapping_games(context)),
            "player_contexts_count": len(_mapping_player_contexts(context)),
            "crowd_markets_count": len(context.get("crowd_markets", [])),
            "kalshi_game_markets_count": len(_mapping_kalshi_markets(context)),
            "kalshi_prop_markets_count": len(_mapping_kalshi_prop_markets(context)),
        }
    )
    log.info(
        "Latency probe captured baseline snapshot: %d mapped tickers",
        len(baseline_snapshots),
    )

    if once:
        await real_client.close()
        return

    ws = RealSportsWebSocket(real_cfg)
    for event_name in OracleLatencyProbe.DEFAULT_EVENT_TYPES:
        ws.on(event_name, probe.handle_event)
    runtime_failures = _RuntimeFailureTracker()

    started = time.monotonic()
    refresh_task = None
    try:
        await ws.connect()
        refresh_task = asyncio.create_task(
            _refresh_loop(
                probe,
                capture,
                real_client,
                kalshi_client,
                refresh_seconds,
                kalshi_stream,
                ws,
                runtime_failures,
            ),
            name="oracle-latency-refresh",
        )
        while not is_shutdown_requested():
            if duration_seconds and (time.monotonic() - started) >= duration_seconds:
                log.info("Latency probe duration reached (%ds), exiting", duration_seconds)
                break
            await asyncio.sleep(1.0)
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        await ws.disconnect()
        await probe.wait_for_pending_followups(timeout_seconds=6.0)
        if kalshi_stream is not None:
            await kalshi_stream.disconnect()
        await real_client.close()


def main():
    setup_unbuffered()
    global log
    log = setup_logging("oracle-latency-probe")
    setup_signal_handlers()

    parser = argparse.ArgumentParser(description="Oracle latency probe")
    parser.add_argument("--once", action="store_true", help="Build the current mapping and exit")
    parser.add_argument("--duration-seconds", type=int, default=0, help="Optional max runtime")
    parser.add_argument("--refresh-seconds", type=int, default=300, help="Market-index refresh cadence")
    parser.add_argument(
        "--followup-delays",
        default="1,3,5",
        help="Comma-separated follow-up quote snapshot delays in seconds; empty string disables follow-ups",
    )
    args = parser.parse_args()
    followup_delays = _parse_followup_delays(args.followup_delays)

    asyncio.run(
        async_main(
            once=args.once,
            duration_seconds=args.duration_seconds,
            refresh_seconds=args.refresh_seconds,
            followup_delays=followup_delays,
        )
    )


if __name__ == "__main__":
    main()
