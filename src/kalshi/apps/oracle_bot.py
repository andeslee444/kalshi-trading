#!/usr/bin/env python3
"""Oracle NBA Multi-Book Trading Bot.

Three independent trading books powered by Real Sports App data:
  - Book A: Game-level price divergence (Real implied prob vs Kalshi price)
  - Book B: Pregame player props (empirical distribution from game logs)
  - Book C: Live event signals (foul trouble, OT probability, blowout detection)

Architecture: Sync process shell with async island internally.
  supervisor → oracle-bot.py (sync process)
    └─ asyncio.run(async_main())
        ├─ await real_client.get_games()            # async httpx
        ├─ book_a.evaluate(...)                     # pure functions
        ├─ book_b.evaluate(player_data, lookup)     # pure functions
        ├─ book_c.evaluate(live_data, orderbook)    # pure functions
        └─ await asyncio.to_thread(trade_manager.place_order(...))

Usage:
    python3 src/kalshi/oracle-bot.py          # daemon mode
    python3 src/kalshi/oracle-bot.py --once   # single scan
    python3 src/kalshi/oracle-bot.py --demo   # signal-only (no execution)
"""

import asyncio
import json
import time
import datetime
import os
import sys
import argparse
import logging
from pathlib import Path
from typing import Optional

from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log,
    HealthCheckMonitor, is_shutdown_requested,
    ScanSummary, build_market_snapshot,
)
from capital_allocator import PortfolioAllocator
from singleton_lock import acquire_process_singleton
from storage import atomic_write_json, save_decision

# Oracle domain imports
from domain.oracle.models import Book, Signal, OraclePosition
from domain.oracle.books.book_a import scan_divergence, divergence_to_signal, evaluate_games
from domain.oracle.books.book_b import evaluate_prop, prop_to_signal
from domain.oracle.books.book_c import (
    detect_foul_trouble, detect_ot_likely, detect_blowout, detect_clutch_comeback,
    live_signal_to_signal,
)
from domain.oracle.alpha_capture import (
    DEFAULT_HYPOTHESIS_ID,
    DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    OracleAlphaCapture,
)
from domain.oracle.risk.ledger import OracleRiskLedger
from domain.oracle.risk.internal_limits import check_all_limits, check_cross_book_sizing
from domain.oracle.risk.sizing import contracts_for_book
from domain.oracle.risk.fees import net_profit_cents, expected_value_cents
from domain.oracle.execution.quote_check import check_quote_quality, quote_from_orderbook
from domain.oracle.execution.fill_monitor import FillMonitor
from domain.oracle.market_mapper import (
    match_game_markets,
    match_prop_markets,
    normalize_team,
    real_game_to_game_id,
)
from domain.oracle.nba_ticker_utils import parse_nba_ticker
from domain.oracle.player_cache import PlayerCache
from domain.oracle.real_sports_client import (
    LiveEvent,
    RealSportsClient,
    RealSportsConfig,
    RealSportsWebSocket,
    parse_home_game_response,
)

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-oracle-trades.json"
DECISIONS_PATH = PROJECT_DIR / "data" / "kalshi-oracle-trades-decisions.json"
RISK_STATE_PATH = PROJECT_DIR / "data" / "oracle-risk-state.json"
PLAYER_CACHE_PATH = PROJECT_DIR / "data" / "oracle-player-cache.json"

# Module-level state (initialized in init())
log = None
client = None
trade_manager = None
allocator = None
health = None
ledger = None
fill_monitor = None
player_cache = None
real_client = None
real_ws = None
oracle_config = {}
_demo_mode = False
_book_c_live_state_cache = None
_oracle_alpha_capture = None
_oracle_alpha_capture_unavailable = False
LIVE_GAME_STATUSES = frozenset({"inprogress", "live", "active"})
_STAT_VALUE_TYPE_MAP = {
    1: "points",
    2: "assists",
    3: "rebounds",
    4: "steals",
    5: "blocks",
    6: "turnovers",
    8: "minutes",
    25: "fouls",
    27: "three_pointers",
}
_BOOK_C_SUPPORTED_PROP_STATS = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "three_pointers": "three_pointers",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
}
_BOOK_C_PROP_SIGNAL_PRIORITY = ("foul_trouble", "ot_likely", "blowout")
_BOOK_C_LIVE_CACHE_MAX_AGE_SECONDS = 15.0
_BOOK_C_ZERO_SIGNAL_ALERT_SOURCE = "oracle-book-c-zero-signals"
_BOOK_C_ZERO_SIGNAL_ALERT_THRESHOLD = 3
_BOOK_C_SCAN_DIAGNOSTICS = {
    "last_scan_utc": None,
    "live_games": 0,
    "game_opportunities": 0,
    "prop_opportunities": 0,
    "total_opportunities": 0,
    "signals": 0,
    "zero_signal_live_scans": 0,
    "zero_signal_live_streak": 0,
    "zero_signal_alert_active": False,
    "last_nonzero_signal_utc": None,
    "last_zero_signal_warning_utc": None,
}


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _cache_key(value):
    parsed = _coerce_int(value)
    if parsed is not None:
        return parsed
    if value in (None, ""):
        return None
    return str(value)


class _BookCLiveStateCache:
    def __init__(self):
        self._game_context: dict[object, dict] = {}
        self._game_state: dict[object, dict] = {}
        self._player_state: dict[object, dict[object, dict]] = {}
        self._game_state_updated_at: dict[object, float] = {}
        self._player_state_updated_at: dict[object, float] = {}

    def set_game_context(self, game_id, game: dict) -> None:
        key = _cache_key(game_id)
        if key is None or not isinstance(game, dict):
            return
        existing = dict(self._game_context.get(key, {}))
        for field in ("home_team", "away_team", "scheduled_day", "start_time"):
            value = game.get(field)
            if value not in (None, ""):
                existing[field] = value
        if existing:
            self._game_context[key] = existing

    def update_game_state_from_payload(self, game_id, payload: dict) -> None:
        key = _cache_key(game_id)
        if key is None or not isinstance(payload, dict):
            return
        game_state = _extract_live_game_state(payload)
        if game_state:
            self._game_state[key] = game_state
            self._game_state_updated_at[key] = time.time()

    def replace_player_states(self, game_id, players: list[dict]) -> None:
        key = _cache_key(game_id)
        if key is None:
            return
        state_by_player = {}
        for player_state in players:
            player_key = self._player_key(player_state)
            if player_key is None:
                continue
            state_by_player[player_key] = dict(player_state)
        self._player_state[key] = state_by_player
        self._player_state_updated_at[key] = time.time()

    def update_player_states_from_payload(self, game_id, payload: dict) -> None:
        key = _cache_key(game_id)
        if key is None or not isinstance(payload, dict):
            return
        rows = _box_score_rows_from_payload(payload)
        if not rows:
            return
        game_context = self._game_context.get(key, {})
        players = _extract_live_player_states({"playerBoxScores": rows}, game_context)
        if not players:
            return
        player_state = self._player_state.setdefault(key, {})
        for item in players:
            player_key = self._player_key(item)
            if player_key is None:
                continue
            player_state[player_key] = dict(item)
        self._player_state_updated_at[key] = time.time()

    def hydrate_from_game_detail(self, game_id, game: dict, game_detail: dict) -> None:
        self.set_game_context(game_id, game)
        self.update_game_state_from_payload(game_id, game_detail)
        self.replace_player_states(game_id, _extract_live_player_states(game_detail, game))

    def game_state(self, game_id, *, max_age_seconds: float | None = None) -> dict | None:
        key = _cache_key(game_id)
        if key is None:
            return None
        if not self._is_fresh(self._game_state_updated_at.get(key), max_age_seconds):
            return None
        value = self._game_state.get(key)
        return dict(value) if isinstance(value, dict) else None

    def player_states(self, game_id, *, max_age_seconds: float | None = None) -> list[dict]:
        key = _cache_key(game_id)
        if key is None:
            return []
        if not self._is_fresh(self._player_state_updated_at.get(key), max_age_seconds):
            return []
        players = self._player_state.get(key, {})
        return [dict(player) for player in players.values()]

    @staticmethod
    def _is_fresh(updated_at: float | None, max_age_seconds: float | None) -> bool:
        if updated_at is None:
            return False
        if max_age_seconds is None:
            return True
        return (time.time() - updated_at) <= max_age_seconds

    @staticmethod
    def _player_key(player_state: dict):
        player_id = player_state.get("player_id")
        if player_id is not None:
            return player_id
        player_name = str(player_state.get("player_name", "")).strip().lower()
        team = str(player_state.get("team", "")).strip().lower()
        if not player_name:
            return None
        return f"{team}|{player_name}"


def _coerce_int(value) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_period(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        if 1 <= value <= 4:
            return f"Q{value}"
        return str(value)
    text = str(value).strip().upper()
    aliases = {
        "1": "Q1",
        "2": "Q2",
        "3": "Q3",
        "4": "Q4",
        "Q1": "Q1",
        "Q2": "Q2",
        "Q3": "Q3",
        "Q4": "Q4",
        "1ST": "Q1",
        "2ND": "Q2",
        "3RD": "Q3",
        "4TH": "Q4",
        "OT": "OT",
        "OT1": "OT",
        "FINAL": "FINAL",
        "HALF": "HALFTIME",
        "HALFTIME": "HALFTIME",
    }
    return aliases.get(text, text)


def _parse_clock_seconds(value) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        return _coerce_int(text)
    minutes_text, seconds_text = text.split(":", 1)
    seconds_text = seconds_text.split(".", 1)[0]
    minutes = _coerce_int(minutes_text)
    seconds = _coerce_int(seconds_text)
    if minutes is None or seconds is None:
        return None
    return max(0, minutes * 60 + seconds)


def _parse_minutes_float(value) -> float:
    if value in (None, ""):
        return 0.0
    parsed = _parse_clock_seconds(value)
    if parsed is not None and isinstance(value, str) and ":" in str(value):
        return parsed / 60.0
    parsed_float = _coerce_float(value)
    return parsed_float if parsed_float is not None else 0.0


def _extract_home_games(home_feed: dict) -> list[dict]:
    latest_day = home_feed.get("latestDay") if isinstance(home_feed, dict) else None
    latest_day_content = home_feed.get("latestDayContent", {}) if isinstance(home_feed, dict) else {}
    games_raw = latest_day_content.get("games", []) if isinstance(latest_day_content, dict) else []
    games = []
    for raw in games_raw:
        if not isinstance(raw, dict):
            continue
        parsed = parse_home_game_response(raw)
        if latest_day and not parsed.get("scheduled_day"):
            parsed["scheduled_day"] = latest_day
        games.append(parsed)
    return games


def _game_date_from_home_game(game: dict) -> Optional[datetime.date]:
    scheduled_day = game.get("scheduled_day")
    if scheduled_day:
        try:
            return datetime.date.fromisoformat(str(scheduled_day))
        except ValueError:
            return None

    start_time = game.get("start_time")
    if not start_time:
        return None
    try:
        return datetime.datetime.fromisoformat(str(start_time).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extract_live_game_state(game_detail: dict) -> dict | None:
    if not isinstance(game_detail, dict):
        return None
    for source in _payload_dict_candidates(game_detail):
        home_score = _coerce_int(
            source.get("homeScore", source.get("home_score", source.get("homeTeamScore")))
        )
        away_score = _coerce_int(
            source.get("awayScore", source.get("away_score", source.get("awayTeamScore")))
        )
        period = _normalize_period(
            source.get("period", source.get("periodName", source.get("currentPeriod")))
        )
        clock_seconds = _parse_clock_seconds(
            source.get(
                "clock",
                source.get("gameClock", source.get("timeRemaining", source.get("timeRemainingSeconds"))),
            )
        )
        if home_score is None or away_score is None or period is None or clock_seconds is None:
            continue
        return {
            "home_score": home_score,
            "away_score": away_score,
            "period": period,
            "clock_seconds": clock_seconds,
            "margin": abs(home_score - away_score),
        }
    return None


def _payload_dict_candidates(payload: dict):
    if not isinstance(payload, dict):
        return
    yield payload
    for key in ("game", "data", "liveGameInfo"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            yield from _payload_dict_candidates(nested)


def _payload_game_id(payload: dict) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    game_id = _cache_key(payload.get("gameId") or payload.get("game_id"))
    if game_id is not None:
        return game_id
    if "id" in payload and any(
        payload.get(key) not in (None, "")
        for key in ("homeScore", "awayScore", "homeTeamScore", "awayTeamScore", "playerBoxScore", "playerBoxScores")
    ):
        game_id = _cache_key(payload.get("id"))
        if game_id is not None:
            return game_id
    for key in ("game", "data", "liveGameInfo"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            game_id = _payload_game_id(nested)
            if game_id is not None:
                return game_id
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
            found = _find_box_scores(nested)
            if found:
                return found
    return []


def _box_score_rows_from_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = _find_box_scores(payload)
    if rows:
        return rows
    for key in ("playerBoxScore", "boxScore"):
        value = payload.get(key)
        if isinstance(value, dict):
            return [value]
    if payload.get("playerId") or payload.get("player_id"):
        return [payload]
    for key in ("game", "data", "liveGameInfo"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            rows = _box_score_rows_from_payload(nested)
            if rows:
                return rows
    return []


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
        return game.get("home_team", "")
    if team_id and away_team_id and team_id == away_team_id:
        return game.get("away_team", "")
    return ""


def _stat_values_map(box_score: dict) -> dict[int, object]:
    values = box_score.get("statValues", [])
    if not isinstance(values, list):
        return {}
    result = {}
    for row in values:
        if not isinstance(row, dict):
            continue
        stat_type = _coerce_int(row.get("type"))
        if stat_type is None:
            continue
        result[stat_type] = row.get("value")
    return result


def _parse_three_pointers(value) -> int:
    if isinstance(value, str) and "/" in value:
        made, _, _ = value.partition("/")
        return _coerce_int(made) or 0
    return _coerce_int(value) or 0


def _live_stat_value(box_score: dict, stat_values: dict[int, object], stat_name: str):
    direct_aliases = {
        "points": ("points", "pts"),
        "rebounds": ("rebounds", "reb"),
        "assists": ("assists", "ast"),
        "steals": ("steals", "stl"),
        "blocks": ("blocks", "blk"),
        "turnovers": ("turnovers", "to"),
        "fouls": ("fouls", "pf", "personalFouls"),
        "minutes": ("minutes", "min"),
        "three_pointers": ("threePointers", "three_pointers", "threes", "3pm"),
    }
    stats_dict = box_score.get("stats")
    if not isinstance(stats_dict, dict):
        stats_dict = {}

    for key in direct_aliases.get(stat_name, ()):
        if box_score.get(key) not in (None, ""):
            if stat_name == "minutes":
                return _parse_minutes_float(box_score.get(key))
            if stat_name == "three_pointers":
                return _parse_three_pointers(box_score.get(key))
            return _coerce_int(box_score.get(key)) or 0
        if stats_dict.get(key) not in (None, ""):
            if stat_name == "minutes":
                return _parse_minutes_float(stats_dict.get(key))
            if stat_name == "three_pointers":
                return _parse_three_pointers(stats_dict.get(key))
            return _coerce_int(stats_dict.get(key)) or 0

    for stat_type, mapped_name in _STAT_VALUE_TYPE_MAP.items():
        if mapped_name != stat_name:
            continue
        if stat_type not in stat_values:
            continue
        if stat_name == "minutes":
            return _parse_minutes_float(stat_values[stat_type])
        return _coerce_int(stat_values[stat_type]) or 0

    if stat_name == "three_pointers":
        return _parse_three_pointers(box_score.get("threePointersMade"))
    return 0


def _extract_live_player_states(game_detail: dict, game: dict) -> list[dict]:
    players = []
    for box_score in _find_box_scores(game_detail):
        if box_score.get("didNotPlay") is True:
            continue
        player_name = _player_name_from_box_score(box_score)
        team_name = _team_name_from_box_score(box_score, game)
        if not player_name or not team_name:
            continue

        stat_values = _stat_values_map(box_score)
        player_id = _coerce_int(box_score.get("playerId"))
        player = box_score.get("player")
        if player_id is None and isinstance(player, dict):
            player_id = _coerce_int(player.get("id"))

        player_state = {
            "player_id": player_id,
            "player_name": player_name,
            "team": team_name,
            "started": bool(box_score.get("started", False)),
            "minutes": _live_stat_value(box_score, stat_values, "minutes"),
            "fouls": _live_stat_value(box_score, stat_values, "fouls"),
        }
        for field in _BOOK_C_SUPPORTED_PROP_STATS.values():
            player_state[field] = _live_stat_value(box_score, stat_values, field)
        players.append(player_state)
    return players


def _book_c_live_cache_is_healthy() -> bool:
    return bool(
        real_ws is not None
        and real_ws.connected
        and not real_ws.is_stale
        and not real_ws.is_disabled
    )


def _book_c_scan_diagnostics_snapshot() -> dict:
    return dict(_BOOK_C_SCAN_DIAGNOSTICS)


def _book_c_record_scan_outcome(*, live_games: int, game_opportunities: int, prop_opportunities: int, signals: int) -> None:
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    diagnostics = _BOOK_C_SCAN_DIAGNOSTICS
    diagnostics["last_scan_utc"] = now_iso
    diagnostics["live_games"] = live_games
    diagnostics["game_opportunities"] = game_opportunities
    diagnostics["prop_opportunities"] = prop_opportunities
    diagnostics["total_opportunities"] = game_opportunities + prop_opportunities
    diagnostics["signals"] = signals

    live_opportunities = diagnostics["total_opportunities"]
    alert_active = bool(diagnostics.get("zero_signal_alert_active"))

    if signals > 0:
        diagnostics["zero_signal_live_streak"] = 0
        diagnostics["zero_signal_alert_active"] = False
        diagnostics["last_nonzero_signal_utc"] = now_iso
        if alert_active and health is not None:
            try:
                health.record_source_success(_BOOK_C_ZERO_SIGNAL_ALERT_SOURCE)
            except Exception as exc:
                _oracle_logger().debug("Book C zero-signal recovery hook failed: %s", exc)
        return

    if live_opportunities <= 0:
        diagnostics["zero_signal_live_streak"] = 0
        diagnostics["zero_signal_alert_active"] = False
        if alert_active and health is not None:
            try:
                health.record_source_success(_BOOK_C_ZERO_SIGNAL_ALERT_SOURCE)
            except Exception as exc:
                _oracle_logger().debug("Book C zero-signal idle recovery hook failed: %s", exc)
        return

    diagnostics["zero_signal_live_scans"] = diagnostics.get("zero_signal_live_scans", 0) + 1
    diagnostics["zero_signal_live_streak"] = diagnostics.get("zero_signal_live_streak", 0) + 1

    if diagnostics["zero_signal_live_streak"] >= _BOOK_C_ZERO_SIGNAL_ALERT_THRESHOLD and not alert_active:
        diagnostics["zero_signal_alert_active"] = True
        diagnostics["last_zero_signal_warning_utc"] = now_iso
        if health is not None:
            message = (
                "Book C produced 0 signals across "
                f"{diagnostics['zero_signal_live_streak']} consecutive scans "
                f"with {live_opportunities} live opportunities"
            )
            try:
                if hasattr(health, "record_source_warning"):
                    health.record_source_warning(_BOOK_C_ZERO_SIGNAL_ALERT_SOURCE, message)
                else:
                    health.record_source_error(_BOOK_C_ZERO_SIGNAL_ALERT_SOURCE, message)
            except Exception as exc:
                _oracle_logger().debug("Book C zero-signal warning hook failed: %s", exc)


async def _handle_real_game_updated(event: LiveEvent) -> None:
    if _book_c_live_state_cache is None:
        return
    game_id = _cache_key(event.game_id)
    if game_id is None and isinstance(event.data, dict):
        game_id = _payload_game_id(event.data)
    if game_id is None:
        return
    _book_c_live_state_cache.update_game_state_from_payload(game_id, event.data)


async def _handle_real_player_box_score_updated(event: LiveEvent) -> None:
    if _book_c_live_state_cache is None:
        return
    game_id = _cache_key(event.game_id)
    if game_id is None and isinstance(event.data, dict):
        game_id = _payload_game_id(event.data)
    if game_id is None:
        return
    _book_c_live_state_cache.update_player_states_from_payload(game_id, event.data)


async def _ensure_book_c_live_stream() -> None:
    if real_ws is None or real_ws.connected or real_ws.is_disabled:
        return
    try:
        await real_ws.connect()
        log.info("Book C live-state stream connected")
    except Exception as exc:
        log.warning("Book C live-state stream unavailable, REST fallback only: %s", exc)


def _is_book_c_core_player(player_state: dict) -> bool:
    return bool(player_state.get("started")) or player_state.get("minutes", 0.0) >= 20.0


def _conservative_per_min_rate(current_stat: float, minutes_played: float, line: float) -> float:
    if current_stat <= 0 or minutes_played <= 0:
        return 0.0
    observed = current_stat / max(minutes_played, 1.0)
    baseline = line / 36.0 if line > 0 else observed
    lower = baseline * 0.75
    upper = baseline * 1.25 if baseline > 0 else observed
    return max(lower, min(observed, upper))


async def _fetch_book_c_quote(ticker: str):
    orderbook = await asyncio.to_thread(client.get_orderbook, ticker)
    return quote_from_orderbook(ticker, orderbook)


def _book_c_quote_quality(quote, *, max_spread: int, min_depth: int, max_quote_age: float):
    return check_quote_quality(
        quote,
        max_spread=max_spread,
        min_depth=min_depth,
        max_age_seconds=max_quote_age,
    )


def _enrich_book_c_signal(
    signal: Signal,
    *,
    quote,
    game: dict,
    game_state: dict,
    strategy: str,
    line: float | None = None,
    stat_type: str | None = None,
    player_state: dict | None = None,
):
    signal.metadata.update(
        {
            "home_team": game.get("home_team", ""),
            "away_team": game.get("away_team", ""),
            "home_score": game_state["home_score"],
            "away_score": game_state["away_score"],
            "period": game_state["period"],
            "clock_seconds": game_state["clock_seconds"],
            "kalshi_yes_bid": quote.yes_bid,
            "kalshi_yes_ask": quote.yes_ask,
            "kalshi_yes_bid_depth": quote.bid_depth,
            "kalshi_yes_ask_depth": quote.ask_depth,
            "quote_spread_cents": quote.spread,
            "quote_age_seconds": round(quote.age_seconds, 3),
            "book_c_strategy": strategy,
        }
    )
    if line is not None:
        signal.metadata["line"] = line
    if stat_type:
        signal.metadata["stat_type"] = stat_type
    if player_state:
        signal.metadata.update(
            {
                "player_id": player_state.get("player_id"),
                "player_name": player_state.get("player_name", ""),
                "team": player_state.get("team", ""),
                "minutes_played": round(player_state.get("minutes", 0.0), 2),
                "fouls": player_state.get("fouls", 0),
                "current_stat": player_state.get(stat_type or "", 0),
            }
        )


async def _scan_book_c_prop_signals_for_game(
    *,
    game: dict,
    game_date: datetime.date,
    game_state: dict,
    players: list[dict],
    kalshi_markets: list[dict],
    book_c_config: dict,
    scan_metrics: dict | None = None,
) -> list[Signal]:
    signals = []
    if not players:
        return signals

    max_spread = book_c_config.get("maxSpreadCents", 8)
    min_depth = book_c_config.get("minDepthContracts", 5)
    max_quote_age = book_c_config.get("maxQuoteAgeSeconds", 5.0)
    min_edge = book_c_config.get("minEdge", oracle_config.get("edgeThreshold", 0.10))
    canonical_game_id = real_game_to_game_id(
        game.get("home_team", ""),
        game.get("away_team", ""),
        game_date,
    )

    for player_state in players:
        if not _is_book_c_core_player(player_state):
            continue
        player_name = player_state["player_name"]
        team_name = player_state["team"]
        player_id = player_state.get("player_id")
        if player_cache is not None and player_id is not None:
            player_cache.put(player_name, team_name, player_id)

        for stat_type, field_name in _BOOK_C_SUPPORTED_PROP_STATS.items():
            current_stat = player_state.get(field_name)
            if current_stat in (None, ""):
                continue
            prop_markets = match_prop_markets(
                player_name,
                team_name,
                stat_type,
                game_date,
                kalshi_markets,
            )
            if not prop_markets:
                continue

            for market in prop_markets:
                ticker = market.get("ticker", "")
                if not ticker:
                    continue
                if ledger and ledger.has_position(ticker):
                    continue
                parsed = parse_nba_ticker(ticker)
                if not parsed or parsed.get("type") != "prop":
                    continue
                line = parsed.get("line")
                if line is None:
                    continue

                try:
                    quote = await _fetch_book_c_quote(ticker)
                except Exception as exc:
                    log.warning("Book C orderbook fetch failed for %s: %s", ticker, exc)
                    continue

                quote_quality = _book_c_quote_quality(
                    quote,
                    max_spread=max_spread,
                    min_depth=min_depth,
                    max_quote_age=max_quote_age,
                )
                if not quote_quality.passed:
                    continue
                if scan_metrics is not None:
                    scan_metrics["prop_opportunities"] = scan_metrics.get("prop_opportunities", 0) + 1

                live_signal = None
                strategy = None
                for signal_type in _BOOK_C_PROP_SIGNAL_PRIORITY:
                    if signal_type == "foul_trouble":
                        live_signal = detect_foul_trouble(
                            fouls=player_state.get("fouls", 0),
                            period=game_state["period"],
                            clock_seconds=game_state["clock_seconds"],
                            current_stat=float(current_stat),
                            line=float(line),
                            kalshi_price_cents=quote.yes_bid,
                            ticker=ticker,
                            player_name=player_name,
                            game_id=canonical_game_id,
                            player_id=player_id,
                            min_edge=min_edge,
                        )
                    elif signal_type == "ot_likely":
                        per_min_rate = _conservative_per_min_rate(
                            float(current_stat),
                            float(player_state.get("minutes", 0.0)),
                            float(line),
                        )
                        if per_min_rate > 0:
                            live_signal = detect_ot_likely(
                                margin=game_state["margin"],
                                period=game_state["period"],
                                clock_seconds=game_state["clock_seconds"],
                                current_stat=float(current_stat),
                                per_min_rate=per_min_rate,
                                line=float(line),
                                kalshi_price_cents=quote.yes_ask,
                                ticker=ticker,
                                player_name=player_name,
                                game_id=canonical_game_id,
                                player_id=player_id,
                                min_edge=min_edge,
                            )
                        else:
                            live_signal = None
                    elif signal_type == "blowout":
                        live_signal = detect_blowout(
                            margin=game_state["margin"],
                            period=game_state["period"],
                            clock_seconds=game_state["clock_seconds"],
                            current_stat=float(current_stat),
                            line=float(line),
                            kalshi_price_cents=quote.yes_bid,
                            ticker=ticker,
                            player_name=player_name,
                            game_id=canonical_game_id,
                            player_id=player_id,
                            min_edge=min_edge,
                        )
                    if live_signal:
                        strategy = signal_type
                        break

                if not live_signal or not strategy:
                    continue

                signal = live_signal_to_signal(live_signal)
                signal.metadata["kalshi_price_cents"] = live_signal.kalshi_price_cents
                _enrich_book_c_signal(
                    signal,
                    quote=quote,
                    game=game,
                    game_state=game_state,
                    strategy=strategy,
                    line=float(line),
                    stat_type=stat_type,
                    player_state=player_state,
                )
                signals.append(signal)
    return signals


def _market_for_pick(markets: list[dict], team_code: str) -> dict | None:
    for market in markets:
        ticker = market.get("ticker", "")
        parsed = parse_nba_ticker(ticker)
        if parsed and parsed.get("type") == "game" and parsed.get("pick") == team_code:
            return market
    return None


def _oracle_logger():
    return log or logging.getLogger("oracle")


def _build_oracle_alpha_capture():
    ledger_path = os.environ.get("ORACLE_ALPHA_LEDGER_PATH") or DEFAULT_ORACLE_ALPHA_LEDGER_PATH
    return OracleAlphaCapture(path=ledger_path, logger=_oracle_logger())


def _get_oracle_alpha_capture():
    global _oracle_alpha_capture, _oracle_alpha_capture_unavailable
    if _oracle_alpha_capture_unavailable:
        return None
    if _oracle_alpha_capture is None:
        try:
            _oracle_alpha_capture = _build_oracle_alpha_capture()
        except Exception as exc:
            _oracle_logger().warning("Oracle alpha capture unavailable: %s", exc)
            _oracle_alpha_capture_unavailable = True
            return None
    return _oracle_alpha_capture


def _signal_execution_mode(order_info: dict | None) -> str:
    if _demo_mode:
        return "demo"
    if order_info and order_info.get("order_id"):
        return "live"
    return "shadow"


def _record_oracle_alpha_fill(signal: Signal, order_info: dict | None, signal_record: dict | None = None) -> None:
    if not isinstance(order_info, dict):
        return
    fill_price_cents = _coerce_int(order_info.get("fill_price_cents"))
    fill_count = _coerce_int(order_info.get("fill_count")) or _coerce_int(order_info.get("count"))
    if fill_price_cents is None or not fill_count:
        return

    capture = _get_oracle_alpha_capture()
    if capture is None:
        return

    signal_id = signal_record.get("signal_id") if isinstance(signal_record, dict) else None
    try:
        capture.record_fill(
            order_id=str(order_info.get("order_id") or signal.ticker),
            market_ticker=signal.ticker,
            fill_timestamp_utc=order_info.get("fill_timestamp_utc")
            or order_info.get("filled_at")
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            fill_price_cents=fill_price_cents,
            fill_count=fill_count,
            side=signal.side,
            hypothesis_id=signal.metadata.get("hypothesis_id") or DEFAULT_HYPOTHESIS_ID,
            signal_id=signal_id,
            source_event_id=signal.metadata.get("source_event_id"),
            extra={
                "order_status": order_info.get("status"),
                "order_price_cents": order_info.get("price_cents"),
                "mode": _signal_execution_mode(order_info),
            },
            source_artifact="oracle_bot_execution",
        )
    except Exception as exc:
        _oracle_logger().warning("Oracle alpha fill capture failed for %s: %s", signal.ticker, exc)


def _record_oracle_alpha_order_submission(
    signal: Signal,
    *,
    contracts: int,
    reason: str,
    order_info: dict | None = None,
    signal_record: dict | None = None,
):
    capture = _get_oracle_alpha_capture()
    if capture is None:
        return None

    metadata = signal.metadata or {}
    price_cents = _coerce_int(order_info.get("price_cents")) if isinstance(order_info, dict) else None
    if price_cents is None and signal.kalshi_price is not None and signal.kalshi_price > 0:
        price_cents = int(round(signal.kalshi_price * 100))
    if price_cents is None:
        price_cents = _coerce_int(metadata.get("kalshi_price_cents"))
    if price_cents is None:
        price_cents = 0

    order_count = _coerce_int(order_info.get("count")) if isinstance(order_info, dict) else None
    if order_count is None:
        order_count = contracts

    signal_id = signal_record.get("signal_id") if isinstance(signal_record, dict) else None
    expected_fill_probability = (
        _coerce_float(metadata.get("expected_fill_probability"))
        if metadata.get("expected_fill_probability") is not None
        else _coerce_float(metadata.get("predicted_fill_probability"))
    )
    entry_type = metadata.get("entry_type") or "aggressive"
    order_timestamp_utc = (
        order_info.get("order_timestamp_utc")
        or order_info.get("created_time")
        or datetime.datetime.now(datetime.timezone.utc).isoformat()
    ) if isinstance(order_info, dict) else datetime.datetime.now(datetime.timezone.utc).isoformat()
    status = ""
    if isinstance(order_info, dict):
        status = str(order_info.get("status") or "")
    elif _demo_mode:
        status = "shadow"

    try:
        return capture.record_order_submission(
            order_id=str(order_info.get("order_id")) if isinstance(order_info, dict) and order_info.get("order_id") else None,
            signal_id=signal_id,
            signal_timestamp_utc=(signal_record or {}).get("signal_timestamp_utc") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            order_timestamp_utc=order_timestamp_utc,
            market_ticker=signal.ticker,
            side=signal.side,
            price_cents=price_cents,
            count=order_count,
            book=signal.book.value if isinstance(signal.book, Book) else str(signal.book),
            hypothesis_id=metadata.get("hypothesis_id") or DEFAULT_HYPOTHESIS_ID,
            game_id=metadata.get("game_id"),
            player_id=metadata.get("player_id"),
            status=status,
            entry_type=entry_type,
            expected_fill_probability=expected_fill_probability,
            extra={
                "mode": _signal_execution_mode(order_info),
                "reason": reason,
                "signal_type": metadata.get("signal_type"),
                "team": metadata.get("team"),
                "stat": metadata.get("stat_type") or metadata.get("stat"),
                "yes_bid": metadata.get("kalshi_yes_bid"),
                "yes_ask": metadata.get("kalshi_yes_ask"),
                "yes_bid_depth": metadata.get("kalshi_yes_bid_depth"),
                "yes_ask_depth": metadata.get("kalshi_yes_ask_depth"),
                "spread_cents": metadata.get("quote_spread_cents"),
                "source_event_id": metadata.get("source_event_id"),
                "source_event_type": metadata.get("source_event_type"),
                "source_event_timestamp_utc": metadata.get("source_event_timestamp_utc"),
            },
            source_artifact="oracle_bot_execution",
        )
    except Exception as exc:
        _oracle_logger().warning("Oracle alpha order capture failed for %s: %s", signal.ticker, exc)
        return None


def _record_oracle_alpha_signal(
    signal: Signal,
    *,
    triggered: bool,
    reason: str,
    contracts: int = 0,
    order_info: dict | None = None,
):
    capture = _get_oracle_alpha_capture()
    if capture is None:
        return None

    metadata = signal.metadata or {}
    yes_bid = metadata.get("kalshi_yes_bid")
    yes_ask = metadata.get("kalshi_yes_ask")
    yes_bid_depth = metadata.get("kalshi_yes_bid_depth")
    yes_ask_depth = metadata.get("kalshi_yes_ask_depth")
    spread_cents = metadata.get("quote_spread_cents")
    midpoint_cents = None
    if yes_bid is not None and yes_ask is not None:
        try:
            midpoint_cents = round((float(yes_bid) + float(yes_ask)) / 2)
        except (TypeError, ValueError):
            midpoint_cents = None

    entry_price = metadata.get("entry_price")
    if entry_price is None:
        if signal.kalshi_price is not None:
            entry_price = signal.kalshi_price
        else:
            entry_price = metadata.get("kalshi_price_cents")
            if isinstance(entry_price, (int, float)) and entry_price > 1:
                entry_price = float(entry_price) / 100.0

    market_prob = metadata.get("market_prob")
    if market_prob is None:
        market_prob = signal.kalshi_price if signal.kalshi_price is not None else metadata.get("kalshi_price_cents")
    if isinstance(market_prob, (int, float)) and market_prob > 1:
        market_prob = float(market_prob) / 100.0

    entry_type = metadata.get("entry_type") or ("aggressive" if triggered else "passive")
    order_status = order_info.get("status") if isinstance(order_info, dict) else None
    payload = {
        "triggered": triggered,
        "reason": reason,
        "contracts": contracts,
        "mode": _signal_execution_mode(order_info),
        "entry_type": entry_type,
        "signal_mode": _signal_execution_mode(order_info),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_bid_depth": yes_bid_depth,
        "yes_ask_depth": yes_ask_depth,
        "spread_cents": spread_cents,
        "midpoint_cents": midpoint_cents,
        "order_id": order_info.get("order_id") if isinstance(order_info, dict) else None,
        "order_status": order_status,
        "order_count": order_info.get("count") if isinstance(order_info, dict) else None,
        "order_price_cents": order_info.get("price_cents") if isinstance(order_info, dict) else None,
        "source_event_id": metadata.get("source_event_id"),
        "source_event_type": metadata.get("source_event_type"),
        "source_event_timestamp_utc": metadata.get("source_event_timestamp_utc"),
        "team": metadata.get("team"),
        "stat": metadata.get("stat_type") or metadata.get("stat"),
        "signal_type": metadata.get("signal_type"),
        "game_status": metadata.get("game_status"),
        "entry_signal_type": metadata.get("signal_type"),
        "expected_fill_probability": metadata.get("expected_fill_probability"),
        "predicted_fill_probability": metadata.get("predicted_fill_probability"),
        "execution_stage": "submitted" if order_status or triggered else "decision",
    }
    try:
        return capture.record_signal(
            hypothesis_id=metadata.get("hypothesis_id") or DEFAULT_HYPOTHESIS_ID,
            signal_timestamp_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            market_ticker=signal.ticker,
            side=signal.side,
            model_prob=signal.model_prob,
            market_prob=market_prob,
            entry_price=entry_price,
            book=signal.book.value if isinstance(signal.book, Book) else str(signal.book),
            game_id=metadata.get("game_id"),
            player_id=metadata.get("player_id"),
            stat=metadata.get("stat_type") or metadata.get("stat"),
            team=metadata.get("team"),
            source_event_id=metadata.get("source_event_id"),
            source_event_type=metadata.get("source_event_type"),
            source_event_timestamp_utc=metadata.get("source_event_timestamp_utc"),
            extra=payload,
            source_artifact="oracle_bot_decision",
        )
    except Exception as exc:
        _oracle_logger().warning("Oracle alpha signal capture failed for %s: %s", signal.ticker, exc)
        return None


def _build_oracle_kalshi_client():
    """Use Oracle-specific Kalshi credentials so other bots can stay on demo."""
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


def _load_config():
    """Load oracle config from bots-config.json."""
    global oracle_config
    try:
        with open(BOTS_CONFIG_PATH) as f:
            bots_config = json.load(f)
        oracle_config = bots_config.get("oracle", {})
        enabled_override = _env_flag("ORACLE_ENABLED_OVERRIDE")
        if enabled_override is not None:
            oracle_config = dict(oracle_config)
            oracle_config["enabled"] = enabled_override
    except Exception as e:
        if log:
            log.warning("Failed to load oracle config: %s", e)
        oracle_config = {}
    return oracle_config


def init():
    """Initialize all bot components."""
    global log, client, trade_manager, allocator, health
    global ledger, fill_monitor, player_cache, real_client, real_ws, _demo_mode
    global _book_c_live_state_cache

    setup_unbuffered()
    log = setup_logging("oracle")
    setup_signal_handlers()
    if not acquire_process_singleton("oracle", PROJECT_DIR, log):
        log.error("Another Oracle instance is already running. Exiting.")
        sys.exit(1)

    _load_config()

    if not oracle_config.get("enabled", False):
        log.info("Oracle bot is disabled in config. Exiting.")
        sys.exit(0)

    client = _build_oracle_kalshi_client()

    trade_config = {
        "maxTradeAmount": oracle_config.get("maxTradeAmount", 25),
        "maxDailyTrades": oracle_config.get("maxDailyTrades", 30),
        "maxDailyLoss": oracle_config.get("maxDailyLoss", 100),
    }
    trade_manager = TradeManager(client, TRADES_PATH, trade_config)
    allocator = PortfolioAllocator(client, logger=log)
    health = HealthCheckMonitor(logger=log)

    ledger = OracleRiskLedger(state_path=RISK_STATE_PATH)
    player_cache = PlayerCache(cache_path=PLAYER_CACHE_PATH)
    real_config = RealSportsConfig.from_bots_config(oracle_config)
    real_client = RealSportsClient(real_config)
    _book_c_live_state_cache = _BookCLiveStateCache()
    real_ws = None

    book_c_config = oracle_config.get("books", {}).get("C", {})
    if book_c_config.get("enabled", True):
        real_ws = RealSportsWebSocket(real_config)
        real_ws.on("GameUpdated", _handle_real_game_updated)
        real_ws.on("PlayerBoxScoreUpdated", _handle_real_player_box_score_updated)
    fill_monitor = FillMonitor(
        fill_timeout_seconds=book_c_config.get("fillTimeoutSeconds", 15),
        max_reprices=book_c_config.get("maxReprices", 2),
        slippage_disable_threshold_cents=book_c_config.get("slippageDisableThresholdCents", 3),
        slippage_disable_min_trades=book_c_config.get("slippageDisableMinTrades", 20),
    )

    # Verify Kalshi auth works before entering scan loop
    try:
        balance, _ = client.get_balance()
        log.info("Kalshi auth OK — balance: $%.2f", balance / 100)
    except Exception as e:
        log.error("Kalshi auth FAILED at startup: %s", e)
        sys.exit(1)

    # Startup banner — operational visibility into configured limits
    risk_cfg = oracle_config.get("risk", {})
    log.info("=" * 60)
    log.info("Oracle NBA Multi-Book Trading Bot")
    log.info("  Mode: %s | Demo: %s", client.mode, _demo_mode)
    log.info("  Max: $%d/trade | Edge: %.0f%% | Daily loss cap: $%d",
             oracle_config.get("maxTradeAmount", 25),
             oracle_config.get("edgeThreshold", 0.10) * 100,
             oracle_config.get("maxDailyLoss", 100))
    log.info("  Books: A=%s B=%s C=%s",
             "ON" if oracle_config.get("books", {}).get("A", {}).get("enabled", True) else "OFF",
             "ON" if oracle_config.get("books", {}).get("B", {}).get("enabled", True) else "OFF",
             "ON" if oracle_config.get("books", {}).get("C", {}).get("enabled", True) else "OFF")
    log.info("  Risk: max %d positions | stop-loss %.0f%% | drawdown %.0f%%",
             risk_cfg.get("maxSimultaneousPositions", 10),
             risk_cfg.get("dailyStopLossPct", 0.05) * 100,
             risk_cfg.get("maxDrawdownPct", 0.15) * 100)
    log.info("  Scan interval: %d min | Ledger: %d positions",
             oracle_config.get("scanIntervalMinutes", 1),
             ledger.position_count())
    log.info("=" * 60)


def _get_bankroll_cents():
    """Get current bankroll from Kalshi balance."""
    try:
        balance, _ = client.get_balance()
        return balance
    except Exception as e:
        log.warning("Failed to get balance: %s", e)
        return 0


def _reconcile_ledger_positions():
    """Remove ledger positions that no longer exist on Kalshi.

    Position-monitor may exit Oracle positions without notifying the ledger.
    This reconciliation prevents overcounting open positions in limit checks.
    """
    if ledger.position_count() == 0:
        return
    try:
        api_positions = client.get("/portfolio/positions")
        if not api_positions:
            return
        settlements = api_positions.get("market_positions", [])
        api_tickers = set()
        for pos in settlements:
            ticker = pos.get("ticker", "")
            count = pos.get("total_traded", 0) - pos.get("total_settled", 0)
            if count > 0:
                api_tickers.add(ticker)

        stale = []
        for ticker in list(ledger._positions.keys()):
            if ticker not in api_tickers:
                stale.append(ticker)

        for ticker in stale:
            pos = ledger.remove_position(ticker)
            if pos:
                log.info("Reconciled stale ledger position: %s (exited by position-monitor)", ticker)
    except Exception as e:
        log.debug("Ledger reconciliation skipped: %s", e)


def _resolve_execution_price(signal: Signal) -> tuple[int, str]:
    """Resolve order price based on execution mode (taker vs maker).

    Taker (aggressive): cross the spread — buy at ask, sell at bid.
    Maker (passive): post at our side of the spread — buy at bid, sell at ask.
      H8 research shows +2.6c maker advantage over taker on game markets.

    Returns (price_cents, execution_mode).
    """
    book_c_config = oracle_config.get("books", {}).get("C", {})
    use_passive = book_c_config.get("passiveExecution", False)

    if use_passive and signal.book == Book.C:
        # Passive: post at the bid (YES) or 100-ask (NO) to capture spread
        if signal.side == "yes":
            passive_price = signal.metadata.get("kalshi_yes_bid")
            if passive_price and 1 <= passive_price <= 99:
                return passive_price, "passive"
        else:
            # NO side: post at our price = 100 - yes_ask
            yes_ask = signal.metadata.get("kalshi_yes_ask")
            if yes_ask and 1 <= yes_ask <= 99:
                no_passive = 100 - yes_ask
                if 1 <= no_passive <= 99:
                    return no_passive, "passive"

    # Taker (default): use the signal's ask-derived price
    taker_price = (
        int(round(signal.kalshi_price * 100))
        if signal.kalshi_price > 0
        else signal.metadata.get("kalshi_price_cents", 50)
    )
    return taker_price, "taker"


def _execute_signal(signal: Signal, bankroll_cents: int):
    """Execute a trading signal after limit checks pass."""
    book_enum = signal.book if isinstance(signal.book, Book) else Book.A
    book_str = book_enum.name

    # Compute price based on execution mode (taker vs passive/maker)
    price_cents, exec_mode = _resolve_execution_price(signal)

    # Preliminary contract count for proposed_cost_cents estimate
    preliminary_contracts = contracts_for_book(book_str, bankroll_cents, price_cents)

    # Check all risk limits
    limit_result = check_all_limits(
        book=book_enum,
        ledger=ledger,
        bankroll_cents=bankroll_cents,
        config=oracle_config,
        game_id=signal.metadata.get("game_id"),
        player_id=signal.metadata.get("player_id"),
        side=signal.side,
        ticker=signal.ticker,
        proposed_cost_cents=preliminary_contracts * price_cents,
    )
    if not limit_result.allowed:
        log.info("Limit check failed for %s: %s", signal.ticker, limit_result.reason)
        _log_decision(signal, triggered=False, reason=f"limit: {limit_result.reason}")
        return

    # Post-fee EV safety check — reject trades that are negative EV after 7% fee
    ev = expected_value_cents(signal.model_prob, price_cents, contracts=1)
    if ev <= 0:
        log.info("Negative post-fee EV for %s: %.2fc (model=%.1f%%, price=%dc)",
                 signal.ticker, ev, signal.model_prob * 100, price_cents)
        _log_decision(signal, triggered=False, reason=f"negative EV after fees: {ev:.2f}c")
        return

    contracts = preliminary_contracts

    # Rotation player sizing reduction (Book B, spec Section 4 rule 5)
    sizing_mult = signal.metadata.get("sizing_multiplier", 1.0)
    if sizing_mult < 1.0:
        contracts = max(1, int(contracts * sizing_mult))

    # Cross-book sizing reduction (spec Section 6)
    # If Book A holds game-level position + Book B/C props on same game → 40% reduction
    cross_mult = check_cross_book_sizing(book_enum, signal.metadata.get("game_id"), ledger)
    if cross_mult < 1.0:
        contracts = max(1, int(contracts * cross_mult))

    if contracts <= 0:
        log.info("Zero contracts for %s at %dc", signal.ticker, price_cents)
        _log_decision(signal, triggered=False, reason="zero contracts")
        return

    if _demo_mode:
        log.info(
            "DEMO: Would %s %s %s %dx @ %dc (edge=%.1f%%, book=%s)",
            exec_mode, signal.side, signal.ticker, contracts, price_cents,
            signal.edge * 100, book_str,
        )
        _log_decision(signal, triggered=True, reason=f"demo mode ({exec_mode})", contracts=contracts)
        _record_oracle_alpha_signal(signal)
        _record_oracle_alpha_order(signal, None)
        return True  # count as placed for scan summary

    # Request budget from portfolio allocator (skipped in demo mode)
    cost_cents = contracts * price_cents
    budget = allocator.request_budget(
        "oracle", signal.ticker,
        edge=signal.edge,
        bot_max_cost_cents=cost_cents,
    )
    if not budget.approved:
        log.info("Allocator denied %s: %s", signal.ticker, budget.reason)
        _log_decision(signal, triggered=False, reason=f"allocator: {budget.reason}")
        return

    # Capture market snapshot for execution quality audit
    snapshot = build_market_snapshot(
        yes_bid=signal.metadata.get("kalshi_yes_bid"),
        yes_ask=signal.metadata.get("kalshi_yes_ask"),
        volume=signal.metadata.get("real_volume"),
    )

    # Execute trade
    reasoning = (
        f"Book {book_str}: {signal.metadata.get('signal_type', 'unknown')} "
        f"edge={signal.edge:.1%} model_prob={signal.model_prob:.1%}"
    )

    try:
        result = trade_manager.place_order(
            signal.ticker,
            signal.side,
            price_cents,
            contracts,
            reasoning,
            book=book_str,
            model_prob=round(signal.model_prob, 4),
            raw_edge=round(signal.edge, 4),
            market_snapshot=snapshot,
            execution_mode=exec_mode,
        )
        if result:
            order_id = result.get("order_id") or (result.get("order", {}).get("order_id"))
            signal_record = _log_decision(
                signal,
                triggered=True,
                reason=f"executed ({exec_mode})",
                contracts=contracts,
                order_info=result,
            )
            ledger.add_position(OraclePosition(
                book=book_enum,
                ticker=signal.ticker,
                side=signal.side,
                contracts=contracts,
                entry_price_cents=price_cents,
                game_id=signal.metadata.get("game_id"),
                player_id=signal.metadata.get("player_id"),
            ))
            ledger.record_order_success()
            log.info(
                "Executed (%s): %s %s %dx @ %dc (book=%s, edge=%.1f%%)",
                exec_mode, signal.side, signal.ticker, contracts, price_cents,
                book_str, signal.edge * 100,
            )
            _record_oracle_alpha_fill(signal, result, signal_record=signal_record)

            # Passive orders: schedule cancel after timeout if not filled
            if exec_mode == "passive" and order_id:
                book_c_config = oracle_config.get("books", {}).get("C", {})
                cancel_timeout = book_c_config.get("passiveCancelTimeoutSeconds", 5.0)
                log.info(
                    "Passive order %s: will cancel in %.0fs if not filled",
                    order_id, cancel_timeout,
                )
                # The cancel is handled by the existing fill_monitor or
                # can be scheduled via asyncio in the scan loop.
                # For now, store the order_id + deadline for the next scan cycle.
                signal.metadata["passive_order_id"] = order_id
                signal.metadata["passive_cancel_deadline"] = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=cancel_timeout)
                ).isoformat()

            return True
        else:
            ledger.record_order_failure()
            _log_decision(signal, triggered=False, reason="trade_manager returned None")
    except Exception as e:
        ledger.record_order_failure()
        log.error("Trade execution failed for %s: %s", signal.ticker, e)
        _log_decision(signal, triggered=False, reason=f"exception: {e}")


def _log_decision(signal: Signal, triggered: bool, reason: str, contracts: int = 0, order_info: dict | None = None):
    """Log a decision (triggered or skipped) to the decisions file."""
    book_str = signal.book.value if isinstance(signal.book, Book) else str(signal.book)
    decision = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "book": book_str,
        "ticker": signal.ticker,
        "side": signal.side,
        "model_prob": round(signal.model_prob, 4),
        "kalshi_price_cents": signal.metadata.get("kalshi_price_cents", 0),
        "edge": round(signal.edge, 4),
        "triggered": triggered,
        "reason": reason,
        "contracts": contracts,
        "signal_type": signal.metadata.get("signal_type", ""),
        "player_name": signal.metadata.get("player_name", ""),
        "game_id": signal.metadata.get("game_id", ""),
    }
    alpha_record = _record_oracle_alpha_signal(
        signal,
        triggered=triggered,
        reason=reason,
        contracts=contracts,
        order_info=order_info,
    )
    if triggered:
        _record_oracle_alpha_order_submission(
            signal,
            contracts=contracts,
            reason=reason,
            order_info=order_info,
            signal_record=alpha_record if isinstance(alpha_record, dict) else None,
        )
    try:
        save_decision(DECISIONS_PATH, decision, logger=log)
    except Exception as e:
        log.warning("Failed to log decision: %s", e)
    return alpha_record or decision


async def scan_book_a(kalshi_markets: list):
    """Scan Book A: game-level price divergence."""
    book_a_config = oracle_config.get("books", {}).get("A", {})
    if not book_a_config.get("enabled", True):
        return []

    min_edge = book_a_config.get("minEdge", 0.15)

    # In production this would fetch from Real Sports API
    # For now, return empty (no Real Sports data without credentials)
    log.debug("Book A scan: no Real Sports data available yet")
    return []


async def scan_book_b(kalshi_markets: list):
    """Scan Book B: pregame player props."""
    book_b_config = oracle_config.get("books", {}).get("B", {})
    if not book_b_config.get("enabled", True):
        return []

    log.debug("Book B scan: no Real Sports data available yet")
    return []


async def scan_book_c(kalshi_markets: list):
    """Scan Book C: live event signals."""
    book_c_config = oracle_config.get("books", {}).get("C", {})
    if not book_c_config.get("enabled", True):
        return []

    if fill_monitor.is_disabled:
        # Re-enable after daily reset (slippage stats are per-session)
        if ledger.daily_pnl_cents == 0.0 and ledger._daily_date:
            fill_monitor.reset_disable()
            log.info("Book C re-enabled after daily reset")
        else:
            log.warning("Book C auto-disabled due to slippage")
            return []

    if real_client is None:
        log.debug("Book C scan: Real client unavailable")
        return []

    try:
        home_feed = await real_client.get_home_feed("nba")
    except Exception as exc:
        log.warning("Book C scan: Real home feed fetch failed: %s", exc)
        return []

    all_home_games = _extract_home_games(home_feed)
    live_games = [
        game
        for game in all_home_games
        if str(game.get("status", "")).lower() in LIVE_GAME_STATUSES
    ]

    # Diagnostic: log game statuses when games exist but none are live
    if all_home_games and not live_games:
        statuses = set(str(g.get("status", "")).lower() for g in all_home_games)
        log.info(
            "Book C scan: %d games in feed, 0 live (statuses: %s)",
            len(all_home_games), ", ".join(sorted(statuses)),
        )
    elif not all_home_games:
        log.info("Book C scan: Real home feed returned 0 games")
    else:
        log.info(
            "Book C scan: %d live games found (of %d total)",
            len(live_games), len(all_home_games),
        )

    if not live_games:
        return []

    min_edge = book_c_config.get("minEdge", oracle_config.get("edgeThreshold", 0.10))
    max_spread = book_c_config.get("maxSpreadCents", 8)
    min_depth = book_c_config.get("minDepthContracts", 5)
    max_quote_age = book_c_config.get("maxQuoteAgeSeconds", 5.0)
    max_live_cache_age = book_c_config.get(
        "liveCacheMaxAgeSeconds",
        _BOOK_C_LIVE_CACHE_MAX_AGE_SECONDS,
    )

    signals = []
    scan_metrics = {
        "live_games": len(live_games),
        "game_opportunities": 0,
        "prop_opportunities": 0,
    }
    for game in live_games:
        game_id = game.get("game_id")
        game_date = _game_date_from_home_game(game)
        if not game_id or game_date is None:
            continue

        if _book_c_live_state_cache is not None:
            _book_c_live_state_cache.set_game_context(game_id, game)

        game_state = None
        players = []
        if _book_c_live_cache_is_healthy() and _book_c_live_state_cache is not None:
            game_state = _book_c_live_state_cache.game_state(
                game_id,
                max_age_seconds=max_live_cache_age,
            )
            players = _book_c_live_state_cache.player_states(
                game_id,
                max_age_seconds=max_live_cache_age,
            )

        if game_state is None or not players:
            game_detail = await real_client.get_game_detail(game_id, "nba")
            if _book_c_live_state_cache is not None:
                _book_c_live_state_cache.hydrate_from_game_detail(game_id, game, game_detail)
                game_state = _book_c_live_state_cache.game_state(game_id)
                players = _book_c_live_state_cache.player_states(game_id)
            else:
                game_state = _extract_live_game_state(game_detail)
                players = _extract_live_player_states(game_detail, game)

        if not game_state:
            continue

        signals.extend(
            await _scan_book_c_prop_signals_for_game(
                game=game,
                game_date=game_date,
                game_state=game_state,
                players=players,
                kalshi_markets=kalshi_markets,
                book_c_config=book_c_config,
                scan_metrics=scan_metrics,
            )
        )

        matched_markets = match_game_markets(
            game.get("home_team", ""),
            game.get("away_team", ""),
            game_date,
            kalshi_markets,
        )
        if not matched_markets:
            continue

        home_score = game_state["home_score"]
        away_score = game_state["away_score"]
        if home_score == away_score:
            continue

        if home_score > away_score:
            leading_team = game.get("home_team", "")
            trailing_team = game.get("away_team", "")
        else:
            leading_team = game.get("away_team", "")
            trailing_team = game.get("home_team", "")

        trailing_code = normalize_team(trailing_team)
        if not trailing_code:
            continue

        trailing_market = _market_for_pick(matched_markets, trailing_code)
        if not trailing_market:
            continue

        trailing_ticker = trailing_market.get("ticker", "")
        if not trailing_ticker:
            continue
        if ledger and ledger.has_position(trailing_ticker):
            continue

        try:
            quote = await _fetch_book_c_quote(trailing_ticker)
        except Exception as exc:
            log.warning("Book C orderbook fetch failed for %s: %s", trailing_ticker, exc)
            continue

        quote_quality = _book_c_quote_quality(
            quote,
            max_spread=max_spread,
            min_depth=min_depth,
            max_quote_age=max_quote_age,
        )
        if not quote_quality.passed:
            log.debug("Book C skip %s: %s", trailing_ticker, quote_quality.reason)
            continue
        scan_metrics["game_opportunities"] = scan_metrics.get("game_opportunities", 0) + 1

        canonical_game_id = real_game_to_game_id(
            game.get("home_team", ""),
            game.get("away_team", ""),
            game_date,
        )
        log.info(
            "Book C clutch eval: %s  period=%s clock=%ss margin=%d  trailing=%s bid=%dc spread=%dc",
            trailing_ticker,
            game_state.get("period", "?"),
            game_state.get("clock_seconds", "?"),
            game_state.get("margin", 0),
            trailing_team,
            quote.yes_bid,
            quote.spread_cents if hasattr(quote, "spread_cents") else 0,
        )
        live_signal = detect_clutch_comeback(
            margin=game_state["margin"],
            period=game_state["period"],
            clock_seconds=game_state["clock_seconds"],
            trailing_team_price_cents=quote.yes_bid,
            ticker=trailing_ticker,
            trailing_team=trailing_team,
            leading_team=leading_team,
            game_id=canonical_game_id,
            min_edge=min_edge,
        )
        if not live_signal:
            continue

        signal = live_signal_to_signal(live_signal)
        signal.metadata["kalshi_price_cents"] = live_signal.kalshi_price_cents
        _enrich_book_c_signal(
            signal,
            quote=quote,
            game=game,
            game_state=game_state,
            strategy="clutch_comeback",
        )
        signals.append(signal)

    _book_c_record_scan_outcome(
        live_games=scan_metrics.get("live_games", 0),
        game_opportunities=scan_metrics.get("game_opportunities", 0),
        prop_opportunities=scan_metrics.get("prop_opportunities", 0),
        signals=len(signals),
    )

    if signals:
        log.info("Book C scan: generated %d live signal(s)", len(signals))
    else:
        log.debug(
            "Book C scan: no live signals (live_games=%d opportunities=%d streak=%d)",
            scan_metrics.get("live_games", 0),
            scan_metrics.get("game_opportunities", 0) + scan_metrics.get("prop_opportunities", 0),
            _BOOK_C_SCAN_DIAGNOSTICS.get("zero_signal_live_streak", 0),
        )
    return signals


async def async_scan_cycle():
    """Run one full scan cycle across all books."""
    ss = ScanSummary("oracle", log)

    # Reset daily PnL at midnight (blocking I/O — run off event loop)
    if await asyncio.to_thread(ledger.maybe_reset_daily):
        log.info("Daily PnL reset (new day)")

    bankroll_cents = await asyncio.to_thread(_get_bankroll_cents)
    if bankroll_cents <= 0:
        log.warning("Zero bankroll, skipping scan cycle")
        ss.skip("zero_bankroll")
        await asyncio.to_thread(ss.finalize)
        return

    # Reconcile ledger against actual Kalshi positions (fixes drift from
    # position-monitor exits that are invisible to Oracle's risk ledger)
    await asyncio.to_thread(_reconcile_ledger_positions)

    # Fetch Kalshi NBA markets
    kalshi_markets = await asyncio.to_thread(
        client.get_all_markets, "KXNBA", "open"
    )
    ss.markets_fetched = len(kalshi_markets)
    ss.source_ok("kalshi")

    # Scan all books concurrently
    book_a_signals, book_b_signals, book_c_signals = await asyncio.gather(
        scan_book_a(kalshi_markets),
        scan_book_b(kalshi_markets),
        scan_book_c(kalshi_markets),
    )

    all_signals = book_a_signals + book_b_signals + book_c_signals
    ss.markets_evaluated = len(kalshi_markets)

    # Execute signals
    for signal in all_signals:
        result = await asyncio.to_thread(_execute_signal, signal, bankroll_cents)
        if result:
            ss.trades_placed += 1

    # Finalize scan summary (writes to data/scan-summaries.json, logs SCAN SUMMARY)
    await asyncio.to_thread(ss.finalize)

    # Record health state for supervisor/daily-ops/dashboard
    await asyncio.to_thread(
        _record_scan_health,
        len(kalshi_markets),
        len(book_a_signals), len(book_b_signals), len(book_c_signals),
        ss.trades_placed,
    )


def _record_scan_health(
    n_markets: int, n_a: int, n_b: int, n_c: int,
    n_trades: int,
):
    """Record health state for supervisor, daily ops loop, and dashboard.

    ScanSummary handles the scan-level logging and metrics file.
    This function handles the health-state.json integration.
    """
    # Heartbeat — tells supervisor "I'm alive"
    health.record_bot_heartbeat("oracle")

    # Source tracking — tells daily ops loop "real-sports is reachable/down"
    health.record_source_success("real-sports")

    # Enrich health state with Oracle-specific metrics
    scan_metrics = {
        "kalshi_markets": n_markets,
        "signals_a": n_a,
        "signals_b": n_b,
        "signals_c": n_c,
        "total_signals": n_a + n_b + n_c,
        "trades_executed": n_trades,
        "open_positions": ledger.position_count(),
        "daily_pnl_cents": ledger.daily_pnl_cents,
        "book_c": _book_c_scan_diagnostics_snapshot(),
    }
    if hasattr(health, "update_bot_state"):
        try:
            health.update_bot_state("oracle", scan_metrics=scan_metrics)
            return
        except Exception as exc:
            _oracle_logger().debug("Failed to update Oracle health metrics via helper: %s", exc)
    health._state["bots"].setdefault("oracle", {})
    health._state["bots"]["oracle"]["scan_metrics"] = scan_metrics
    if hasattr(health, "_dirty_bots"):
        health._dirty_bots.add("oracle")
    try:
        health._save()
    except Exception as exc:
        _oracle_logger().debug("Failed to persist Oracle health metrics: %s", exc)


async def async_main(once: bool = False):
    """Async main loop."""
    scan_interval = oracle_config.get("scanIntervalMinutes", 1) * 60

    try:
        while True:
            try:
                await _ensure_book_c_live_stream()
                await async_scan_cycle()
            except Exception as e:
                log.error("Scan cycle error: %s", e, exc_info=True)
                # Record source error so health monitor can detect Real Sports failures
                try:
                    health.record_source_error("real-sports", str(e))
                except Exception:
                    pass

            if once:
                break

            # Check for shutdown
            if is_shutdown_requested():
                log.info("Shutdown requested, exiting")
                break

            await asyncio.sleep(scan_interval)
    finally:
        log.info("Oracle bot shutting down, cleaning up async resources")
        if real_ws is not None:
            try:
                await real_ws.disconnect()
            except Exception as e:
                log.debug("Failed to disconnect Real WebSocket: %s", e)
        if real_client is not None:
            try:
                await real_client.close()
            except Exception as e:
                log.debug("Failed to close Real client: %s", e)


def main():
    """Sync entrypoint — wraps async island."""
    global _demo_mode

    parser = argparse.ArgumentParser(description="Oracle NBA Trading Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan cycle")
    parser.add_argument("--demo", action="store_true", help="Signal-only mode (no execution)")
    args = parser.parse_args()

    _demo_mode = args.demo

    init()
    trim_trade_log(TRADES_PATH)
    log.info("Starting Oracle NBA bot (once=%s, demo=%s)", args.once, args.demo)

    asyncio.run(async_main(once=args.once))


if __name__ == "__main__":
    main()
