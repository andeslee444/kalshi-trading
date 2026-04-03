"""Helpers for classifying and summarizing Oracle latency-capture events."""

from __future__ import annotations

import datetime as dt
import math
import random
import re
import statistics
from typing import Any

from domain.oracle.models import GameState
from domain.oracle.nba_ticker_utils import parse_nba_ticker


_TECHNICAL_FOUL_RE = re.compile(r"\b(?:technical|double technical|tech)\b", re.IGNORECASE)
_SCORING_RUN_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s+run", re.IGNORECASE)
_PLAYER_OUT_RE = re.compile(
    r"\b(?:injur(?:y|ed)|hurt|left(?: the)? game|will not return|won'?t return|"
    r"ruled out|out for the game|out\b|inactive|eject(?:ed|ion)?|foul(?:ed)? out|"
    r"disqualif(?:ied|ication))\b",
    re.IGNORECASE,
)
_PLAYER_OUT_STATUSES = {
    "OUT",
    "INACTIVE",
    "DOUBTFUL",
    "WILL NOT RETURN",
    "RULED OUT",
}


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _deep_find(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in keys and value not in (None, ""):
                return value
        for value in data.values():
            found = _deep_find(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _deep_find(item, keys)
            if found not in (None, ""):
                return found
    return None


def _normalize_period(value: Any) -> str | None:
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
        "HALF": "HALFTIME",
        "HALFTIME": "HALFTIME",
        "FINAL": "FINAL",
    }
    return aliases.get(text, text)


def _parse_clock_seconds(value: Any) -> int | None:
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
    seconds_text = seconds_text.split(".")[0]
    minutes = _coerce_int(minutes_text)
    seconds = _coerce_int(seconds_text)
    if minutes is None or seconds is None:
        return None
    return max(0, minutes * 60 + seconds)


def _parse_clock_from_parts(minutes_value: Any, seconds_value: Any) -> int | None:
    minutes = _coerce_int(minutes_value)
    seconds = _coerce_int(seconds_value)
    if minutes is None or seconds is None:
        return None
    return max(0, minutes * 60 + seconds)


def _normalize_status_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().upper().replace("_", " ")
    return " ".join(text.split()) or None


def _payload_indicates_player_out(data: dict[str, Any], play_text: str) -> bool:
    if play_text and _PLAYER_OUT_RE.search(play_text):
        return True

    injury_status = _normalize_status_text(
        _deep_find(
            data,
            {
                "injuryStatus",
                "injury_status",
                "availabilityStatus",
                "playerStatus",
                "participationStatus",
            },
        )
    )
    if injury_status in _PLAYER_OUT_STATUSES:
        return True

    bool_checks = {
        "didNotPlay": True,
        "did_not_play": True,
        "isAvailable": False,
        "is_available": False,
        "available": False,
        "active": False,
        "isActive": False,
        "is_active": False,
        "ejected": True,
        "isEjected": True,
        "is_ejected": True,
        "fouledOut": True,
        "fouled_out": True,
        "disqualified": True,
    }
    for key, target in bool_checks.items():
        value = _deep_find(data, {key})
        if value is None:
            continue
        if isinstance(value, bool):
            if value is target:
                return True
            continue
        normalized = _normalize_status_text(value)
        if normalized in {"TRUE", "FALSE"}:
            if (normalized == "TRUE") is target:
                return True
    return False


def _previous_state_parts(previous_game_state: Any) -> tuple[str | None, dict]:
    if isinstance(previous_game_state, dict):
        return previous_game_state.get("game_state"), previous_game_state
    if previous_game_state in (None, ""):
        return None, {}
    return str(previous_game_state), {}


def _state_from_context(*, margin: int | None, period: str | None, clock_seconds: int | None) -> str | None:
    if period in ("FINAL",):
        return GameState.FINAL.value
    if period in ("HALFTIME",):
        return GameState.HALFTIME.value
    if period in ("OT", "OT1", "OT2"):
        return GameState.OVERTIME.value
    if margin is None or period is None or clock_seconds is None:
        return None
    return GameState.classify(margin, period, clock_seconds).value


def _state_transition(previous_state: str | None, current_state: str | None) -> str | None:
    if current_state is None or previous_state == current_state:
        return None
    return f"{previous_state or 'unknown'}->{current_state}"


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    fraction = rank - low
    return int(round(ordered[low] + (ordered[high] - ordered[low]) * fraction))


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    return int(round(statistics.median(values)))


def _mean(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _mean_float(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _percentile_float(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(float(ordered[low]), 3)
    fraction = rank - low
    value = ordered[low] + (ordered[high] - ordered[low]) * fraction
    return round(float(value), 3)


def _opportunity_cluster_key(*, source_row: dict, ticker: str | None) -> str | None:
    game_id = source_row.get("game_id")
    if game_id not in (None, ""):
        return f"game:{game_id}"
    if ticker not in (None, ""):
        return f"ticker:{ticker}"
    return None


def _clustered_bootstrap_mean_ci(
    opportunities: list[dict[str, Any]],
    *,
    samples: int = 500,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, float | int | None]:
    return _clustered_bootstrap_value_ci(
        opportunities,
        value_key="best_markout_cents",
        samples=samples,
        confidence=confidence,
        seed=seed,
    )


def _clustered_bootstrap_value_ci(
    records: list[dict[str, Any]],
    *,
    value_key: str,
    samples: int = 500,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, float | int | None]:
    if not records:
        return {
            "mean": None,
            "ci_low": None,
            "ci_high": None,
            "sample_size": 0,
            "cluster_count": 0,
            "bootstrap_samples": 0,
        }
    clusters: dict[str, list[float]] = {}
    for record in records:
        value = _coerce_float(record.get(value_key))
        if value is None:
            continue
        cluster_key = str(
            record.get("cluster_key")
            or record.get("outcome_id")
            or record.get("opportunity_id")
            or "unclustered"
        )
        clusters.setdefault(cluster_key, []).append(float(value))
    if not clusters:
        return {
            "mean": None,
            "ci_low": None,
            "ci_high": None,
            "sample_size": 0,
            "cluster_count": 0,
            "bootstrap_samples": 0,
        }
    numeric_values = [value for cluster_values in clusters.values() for value in cluster_values]
    rng = random.Random(seed)
    draw_means = []
    cluster_keys = list(clusters)
    cluster_count = len(cluster_keys)
    for _ in range(samples):
        sampled_keys = [cluster_keys[rng.randrange(cluster_count)] for _ in range(cluster_count)]
        draw = [value for key in sampled_keys for value in clusters[key]]
        draw_means.append(sum(draw) / len(draw))
    alpha = max(0.0, min(1.0, 1.0 - confidence))
    return {
        "mean": _mean_float(numeric_values),
        "ci_low": _percentile_float(draw_means, 100.0 * (alpha / 2.0)),
        "ci_high": _percentile_float(draw_means, 100.0 * (1.0 - (alpha / 2.0))),
        "sample_size": len(numeric_values),
        "cluster_count": cluster_count,
        "bootstrap_samples": samples,
    }


def _clustered_bootstrap_positive_rate_ci(
    opportunities: list[dict[str, Any]],
    *,
    samples: int = 500,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, float | int | None]:
    return _clustered_bootstrap_positive_value_rate_ci(
        opportunities,
        value_key="best_markout_cents",
        samples=samples,
        confidence=confidence,
        seed=seed,
    )


def _clustered_bootstrap_positive_value_rate_ci(
    records: list[dict[str, Any]],
    *,
    value_key: str,
    samples: int = 500,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, float | int | None]:
    if not records:
        return {
            "rate": None,
            "ci_low": None,
            "ci_high": None,
            "sample_size": 0,
            "cluster_count": 0,
            "bootstrap_samples": 0,
        }
    clusters: dict[str, list[float]] = {}
    for record in records:
        value = _coerce_float(record.get(value_key))
        if value is None:
            continue
        cluster_key = str(
            record.get("cluster_key")
            or record.get("outcome_id")
            or record.get("opportunity_id")
            or "unclustered"
        )
        clusters.setdefault(cluster_key, []).append(1.0 if value > 0 else 0.0)
    if not clusters:
        return {
            "rate": None,
            "ci_low": None,
            "ci_high": None,
            "sample_size": 0,
            "cluster_count": 0,
            "bootstrap_samples": 0,
        }
    indicators = [value for cluster_values in clusters.values() for value in cluster_values]
    rng = random.Random(seed)
    draw_rates = []
    cluster_keys = list(clusters)
    cluster_count = len(cluster_keys)
    for _ in range(samples):
        sampled_keys = [cluster_keys[rng.randrange(cluster_count)] for _ in range(cluster_count)]
        draw = [value for key in sampled_keys for value in clusters[key]]
        draw_rates.append(sum(draw) / len(draw))
    alpha = max(0.0, min(1.0, 1.0 - confidence))
    return {
        "rate": _mean_float(indicators),
        "ci_low": _percentile_float(draw_rates, 100.0 * (alpha / 2.0)),
        "ci_high": _percentile_float(draw_rates, 100.0 * (1.0 - (alpha / 2.0))),
        "sample_size": len(indicators),
        "cluster_count": cluster_count,
        "bootstrap_samples": samples,
    }


def _proof_checks(
    *,
    source_events: int,
    paired_fillable_opportunities: int,
    bootstrap_best_markout_ci_low: float | None,
) -> dict[str, bool]:
    return {
        "stage1_research_signal_target_met": source_events >= 200,
        "stage2_shadow_trade_target_met_proxy": paired_fillable_opportunities >= 100,
        "bootstrap_mean_best_markout_ci_above_zero": (
            bootstrap_best_markout_ci_low is not None and bootstrap_best_markout_ci_low > 0
        ),
    }


def _parse_scoring_run(play_text: str) -> tuple[int | None, int | None]:
    match = _SCORING_RUN_RE.search(play_text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def classify_live_event(
    event,
    *,
    previous_game_state: Any = None,
    previous_player_fouls: int | None = None,
) -> dict:
    """Classify a live Real event into H1 event classes and context fields."""
    data = getattr(event, "data", {}) if isinstance(getattr(event, "data", {}), dict) else {}
    previous_state, previous_context = _previous_state_parts(previous_game_state)

    home_score = _coerce_int(_deep_find(data, {"homeScore", "home_score", "homeTeamScore", "home_team_score"}))
    away_score = _coerce_int(_deep_find(data, {"awayScore", "away_score", "awayTeamScore", "away_team_score"}))
    if home_score is None:
        home_score = _coerce_int(previous_context.get("home_score"))
    if away_score is None:
        away_score = _coerce_int(previous_context.get("away_score"))

    period = _normalize_period(_deep_find(data, {"period", "quarter", "currentPeriod", "gamePeriod"}))
    if period is None:
        period = _normalize_period(previous_context.get("period"))

    clock_seconds = _parse_clock_seconds(_deep_find(data, {"clock", "gameClock", "timeRemaining"}))
    if clock_seconds is None:
        clock_seconds = _parse_clock_from_parts(
            _deep_find(data, {"timeRemainingMinutes", "minutesRemaining"}),
            _deep_find(data, {"timeRemainingSeconds", "secondsRemaining"}),
        )
    if clock_seconds is None:
        clock_seconds = _coerce_int(previous_context.get("clock_seconds"))

    margin = None
    if home_score is not None and away_score is not None:
        margin = abs(home_score - away_score)

    current_state = _state_from_context(margin=margin, period=period, clock_seconds=clock_seconds)
    state_transition = _state_transition(previous_state, current_state)

    text_parts = [
        _deep_find(data, {"description", "playDescription", "text", "title", "message", "display", "detail"}),
        _deep_find(data, {"type"}),
        _deep_find(data, {"subType", "sub_type"}),
    ]
    play_text = " ".join(str(part).strip() for part in text_parts if part not in (None, "")).strip()

    player_fouls = _coerce_int(_deep_find(data, {"pf", "personalFouls", "fouls"}))
    if player_fouls is None:
        player_fouls = _coerce_int(previous_context.get("player_fouls"))

    scoring_run_for = None
    scoring_run_points = None
    derived_event_class = None

    run_for, run_against = _parse_scoring_run(play_text)
    if run_for is not None and run_against is not None:
        scoring_run_points = run_for - run_against
        if scoring_run_points >= 6:
            scoring_run_for = "unknown"

    previous_home = _coerce_int(previous_context.get("home_score"))
    previous_away = _coerce_int(previous_context.get("away_score"))
    if home_score is not None and away_score is not None and previous_home is not None and previous_away is not None:
        home_delta = home_score - previous_home
        away_delta = away_score - previous_away
        margin_delta = abs(home_score - away_score) - abs(previous_home - previous_away)
        if home_delta >= 6 and away_delta <= 1 and margin_delta >= 5:
            scoring_run_for = "home"
            scoring_run_points = home_delta - away_delta
        elif away_delta >= 6 and home_delta <= 1 and margin_delta >= 5:
            scoring_run_for = "away"
            scoring_run_points = away_delta - home_delta

    if _payload_indicates_player_out(data, play_text):
        derived_event_class = "injury_player_out"
    elif play_text and _TECHNICAL_FOUL_RE.search(play_text):
        derived_event_class = "technical_foul"
    elif (
        player_fouls is not None
        and player_fouls >= 4
        and period in ("Q1", "Q2", "Q3")
        and (previous_player_fouls is None or previous_player_fouls < 4)
    ):
        derived_event_class = "foul_trouble_entry"
    elif current_state == GameState.OT_LIKELY.value and previous_state != GameState.OT_LIKELY.value:
        derived_event_class = "ot_likely_entry"
    elif current_state == GameState.CLUTCH.value and previous_state != GameState.CLUTCH.value:
        derived_event_class = "clutch_entry"
    elif current_state == GameState.BLOWOUT.value and previous_state != GameState.BLOWOUT.value:
        derived_event_class = "blowout_entry"
    elif scoring_run_points is not None and scoring_run_points >= 6:
        derived_event_class = "scoring_run"

    return {
        "derived_event_class": derived_event_class,
        "game_state": current_state,
        "state_transition": state_transition,
        "period": period,
        "clock_seconds": clock_seconds,
        "home_score": home_score,
        "away_score": away_score,
        "score_margin": margin,
        "player_fouls": player_fouls,
        "scoring_run_for": scoring_run_for,
        "scoring_run_points": scoring_run_points,
    }


def summarize_latency_capture(
    source_rows: list[dict],
    quote_rows: list[dict],
    *,
    hypothesis_id: str | None = None,
    max_spread_cents: int = 8,
    min_depth_contracts: int = 5,
) -> dict:
    """Summarize H1 latency-capture records by derived event class."""

    def include(row: dict) -> bool:
        if hypothesis_id and row.get("hypothesis_id") != hypothesis_id:
            return False
        return True

    source_events = [
        row for row in source_rows
        if include(row) and row.get("record_kind", "source_event") == "source_event"
    ]
    source_by_id = {row.get("event_id"): row for row in source_events if row.get("event_id")}
    event_quotes = [
        row for row in quote_rows
        if include(row)
        and row.get("record_kind", "quote_snapshot") == "quote_snapshot"
        and row.get("source_event_id") in source_by_id
    ]

    buckets: dict[str, dict[str, Any]] = {}
    baseline_quotes: dict[tuple[str, str], dict] = {}
    overall_quoted_source_events: set[str] = set()
    overall_fillable_opportunities: dict[str, dict[str, Any]] = {}

    def market_type_bucket() -> dict[str, Any]:
        return {
            "source_events": 0,
            "quote_snapshots": 0,
            "quoted_source_event_ids": set(),
            "tickers": set(),
            "event_immediate_quote_snapshots": 0,
            "event_followup_quote_snapshots": 0,
            "paired_fillable_opportunities": 0,
        }

    overall_market_types: dict[str, dict[str, Any]] = {}

    def horizon_bucket() -> dict[str, Any]:
        return {
            "quote_snapshots": 0,
            "tickers": set(),
            "latencies": [],
            "midpoint_changes": [],
            "moved_quote_snapshots": 0,
            "displayed_yes_fillable_snapshots": 0,
            "displayed_no_fillable_snapshots": 0,
            "displayed_any_fillable_snapshots": 0,
            "paired_snapshots": 0,
            "midpoint_changes_from_initial": [],
            "yes_markouts": [],
            "no_markouts": [],
            "best_markouts": [],
            "positive_best_markouts": 0,
            "paired_fillable_opportunities": 0,
            "fillable_opportunity_records": [],
        }

    def bucket_for(event_class: str) -> dict[str, Any]:
        return buckets.setdefault(
            event_class,
            {
                "source_events": 0,
                "quote_snapshots": 0,
                "games": set(),
                "players": set(),
                "tickers": set(),
                "latencies": [],
                "midpoint_changes": [],
                "moved_quote_snapshots": 0,
                "quoted_source_event_ids": set(),
                "horizons": {},
                "by_market_type": {},
            },
        )

    for row in source_events:
        event_class = row.get("derived_event_class") or "unclassified"
        bucket = bucket_for(event_class)
        bucket["source_events"] += 1
        if row.get("game_id") not in (None, ""):
            bucket["games"].add(str(row["game_id"]))
        if row.get("player_id") not in (None, ""):
            bucket["players"].add(str(row["player_id"]))
        if _source_mapped_ticker_count(row, "mapped_game_tickers") > 0:
            bucket["by_market_type"].setdefault("game", market_type_bucket())["source_events"] += 1
            overall_market_types.setdefault("game", market_type_bucket())["source_events"] += 1
        if _source_mapped_ticker_count(row, "mapped_prop_tickers") > 0:
            bucket["by_market_type"].setdefault("prop", market_type_bucket())["source_events"] += 1
            overall_market_types.setdefault("prop", market_type_bucket())["source_events"] += 1

    for row in event_quotes:
        source_row = source_by_id.get(row.get("source_event_id"), {})
        event_class = row.get("derived_event_class") or source_row.get("derived_event_class") or "unclassified"
        bucket = bucket_for(event_class)
        market_type = _market_type_for_ticker(row.get("ticker"))
        market_summary = bucket["by_market_type"].setdefault(market_type, market_type_bucket())
        overall_market_summary = overall_market_types.setdefault(market_type, market_type_bucket())
        bucket["quote_snapshots"] += 1
        source_event_id = row.get("source_event_id")
        horizon_seconds = _coerce_float(row.get("horizon_seconds"))
        if horizon_seconds is None:
            horizon_seconds = 0.0
        if row.get("ticker"):
            bucket["tickers"].add(row["ticker"])
        latency = _coerce_int(row.get("source_to_quote_ms"))
        if latency is not None:
            bucket["latencies"].append(latency)
        midpoint_change = _coerce_int(row.get("midpoint_change_cents"))
        if midpoint_change is not None:
            bucket["midpoint_changes"].append(midpoint_change)
            if midpoint_change != 0:
                bucket["moved_quote_snapshots"] += 1
        if source_event_id and horizon_seconds == 0.0:
            bucket["quoted_source_event_ids"].add(source_event_id)
            overall_quoted_source_events.add(source_event_id)
            market_summary["quoted_source_event_ids"].add(source_event_id)
            overall_market_summary["quoted_source_event_ids"].add(source_event_id)
        market_summary["quote_snapshots"] += 1
        overall_market_summary["quote_snapshots"] += 1
        if row.get("ticker"):
            market_summary["tickers"].add(row["ticker"])
            overall_market_summary["tickers"].add(row["ticker"])
        capture_mode = str(row.get("capture_mode") or "")
        if capture_mode == "event_immediate":
            market_summary["event_immediate_quote_snapshots"] += 1
            overall_market_summary["event_immediate_quote_snapshots"] += 1
        elif capture_mode == "event_followup":
            market_summary["event_followup_quote_snapshots"] += 1
            overall_market_summary["event_followup_quote_snapshots"] += 1

        horizon = bucket["horizons"].setdefault(horizon_seconds, horizon_bucket())
        horizon["quote_snapshots"] += 1
        if row.get("ticker"):
            horizon["tickers"].add(row["ticker"])
        if latency is not None:
            horizon["latencies"].append(latency)
        if midpoint_change is not None:
            horizon["midpoint_changes"].append(midpoint_change)
            if midpoint_change != 0:
                horizon["moved_quote_snapshots"] += 1

        spread_cents = _coerce_int(row.get("spread_cents"))
        yes_bid_depth = _coerce_int(row.get("yes_bid_depth")) or 0
        yes_ask_depth = _coerce_int(row.get("yes_ask_depth")) or 0
        yes_fillable = bool(
            spread_cents is not None and spread_cents <= max_spread_cents and yes_ask_depth >= min_depth_contracts
        )
        no_fillable = bool(
            spread_cents is not None and spread_cents <= max_spread_cents and yes_bid_depth >= min_depth_contracts
        )
        if yes_fillable:
            horizon["displayed_yes_fillable_snapshots"] += 1
        if no_fillable:
            horizon["displayed_no_fillable_snapshots"] += 1
        if yes_fillable or no_fillable:
            horizon["displayed_any_fillable_snapshots"] += 1

        key = (str(source_event_id), str(row.get("ticker")))
        if source_event_id and row.get("ticker") and horizon_seconds == 0.0:
            previous = baseline_quotes.get(key)
            previous_latency = _coerce_int(previous.get("source_to_quote_ms")) if previous else None
            if previous is None or (
                latency is not None and (previous_latency is None or latency < previous_latency)
            ):
                baseline_quotes[key] = row

    for row in event_quotes:
        source_row = source_by_id.get(row.get("source_event_id"), {})
        event_class = row.get("derived_event_class") or source_row.get("derived_event_class") or "unclassified"
        bucket = bucket_for(event_class)
        horizon_seconds = _coerce_float(row.get("horizon_seconds"))
        if horizon_seconds in (None, 0.0):
            continue
        key = (str(row.get("source_event_id")), str(row.get("ticker")))
        baseline = baseline_quotes.get(key)
        if baseline is None:
            continue
        horizon = bucket["horizons"].setdefault(horizon_seconds, horizon_bucket())
        horizon["paired_snapshots"] += 1

        baseline_mid = _coerce_int(baseline.get("midpoint_cents"))
        current_mid = _coerce_int(row.get("midpoint_cents"))
        if baseline_mid is not None and current_mid is not None:
            horizon["midpoint_changes_from_initial"].append(current_mid - baseline_mid)

        baseline_spread = _coerce_int(baseline.get("spread_cents"))
        baseline_yes_bid_depth = _coerce_int(baseline.get("yes_bid_depth")) or 0
        baseline_yes_ask_depth = _coerce_int(baseline.get("yes_ask_depth")) or 0
        baseline_yes_bid = _coerce_int(baseline.get("yes_bid_cents"))
        baseline_yes_ask = _coerce_int(baseline.get("yes_ask_cents"))
        current_yes_bid = _coerce_int(row.get("yes_bid_cents"))
        current_yes_ask = _coerce_int(row.get("yes_ask_cents"))

        yes_fillable = bool(
            baseline_spread is not None
            and baseline_spread <= max_spread_cents
            and baseline_yes_ask_depth >= min_depth_contracts
        )
        no_fillable = bool(
            baseline_spread is not None
            and baseline_spread <= max_spread_cents
            and baseline_yes_bid_depth >= min_depth_contracts
        )

        candidates = []
        if yes_fillable and baseline_yes_ask is not None and current_yes_bid is not None:
            yes_markout = current_yes_bid - baseline_yes_ask
            horizon["yes_markouts"].append(yes_markout)
            candidates.append(yes_markout)
        if no_fillable and baseline_yes_bid is not None and current_yes_ask is not None:
            no_markout = baseline_yes_bid - current_yes_ask
            horizon["no_markouts"].append(no_markout)
            candidates.append(no_markout)
        if candidates:
            best_markout = max(candidates)
            opportunity_id = f"{row.get('source_event_id')}::{row.get('ticker')}"
            opportunity_record = {
                "opportunity_id": opportunity_id,
                "cluster_key": _opportunity_cluster_key(source_row=source_row, ticker=row.get("ticker")),
                "best_markout_cents": best_markout,
                "game_id": source_row.get("game_id"),
                "ticker": row.get("ticker"),
                "source_event_id": row.get("source_event_id"),
                "horizon_seconds": horizon_seconds,
            }
            horizon["best_markouts"].append(best_markout)
            horizon["paired_fillable_opportunities"] += 1
            horizon["fillable_opportunity_records"].append(opportunity_record)
            previous_overall = overall_fillable_opportunities.get(opportunity_id)
            previous_horizon = _coerce_float(previous_overall.get("horizon_seconds")) if previous_overall else None
            if previous_overall is None or (
                previous_horizon is not None and horizon_seconds < previous_horizon
            ) or previous_horizon is None:
                overall_fillable_opportunities[opportunity_id] = opportunity_record
            if best_markout > 0:
                horizon["positive_best_markouts"] += 1
            market_type = _market_type_for_ticker(row.get("ticker"))
            bucket["by_market_type"].setdefault(market_type, market_type_bucket())["paired_fillable_opportunities"] += 1
            overall_market_types.setdefault(market_type, market_type_bucket())["paired_fillable_opportunities"] += 1

    ranked = []
    ranked_horizon_rows = []
    by_event_class = {}

    def summarize_market_type_rows(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        summary_rows: dict[str, dict[str, Any]] = {}
        for market_type, row in rows.items():
            quoted_source_events = len(row["quoted_source_event_ids"])
            source_events_count = max(row["source_events"], quoted_source_events)
            summary_rows[market_type] = {
                "source_events": source_events_count,
                "quoted_source_events": quoted_source_events,
                "capture_rate": (
                    round(quoted_source_events / source_events_count, 3)
                    if source_events_count
                    else None
                ),
                "quote_snapshots": row["quote_snapshots"],
                "tickers": len(row["tickers"]),
                "event_immediate_quote_snapshots": row["event_immediate_quote_snapshots"],
                "event_followup_quote_snapshots": row["event_followup_quote_snapshots"],
                "paired_fillable_opportunities": row["paired_fillable_opportunities"],
            }
        return summary_rows

    for event_class, bucket in buckets.items():
        midpoint_abs = [abs(value) for value in bucket["midpoint_changes"]]
        quote_snapshots = bucket["quote_snapshots"]
        source_events_count = bucket["source_events"]
        quoted_source_events = len(bucket["quoted_source_event_ids"])
        by_horizon = {}
        for horizon_seconds in sorted(bucket["horizons"]):
            horizon = bucket["horizons"][horizon_seconds]
            horizon_midpoint_abs = [abs(value) for value in horizon["midpoint_changes"]]
            horizon_midpoint_from_initial_abs = [abs(value) for value in horizon["midpoint_changes_from_initial"]]
            horizon_quotes = horizon["quote_snapshots"]
            paired_snapshots = horizon["paired_snapshots"]
            horizon_summary = {
                "quote_snapshots": horizon_quotes,
                "tickers": len(horizon["tickers"]),
                "median_latency_ms": _median(horizon["latencies"]),
                "p75_latency_ms": _percentile(horizon["latencies"], 75),
                "moved_quote_snapshots": horizon["moved_quote_snapshots"],
                "moved_quote_rate": (
                    round(horizon["moved_quote_snapshots"] / horizon_quotes, 3)
                    if horizon_quotes
                    else None
                ),
                "median_abs_midpoint_change_cents": _median(horizon_midpoint_abs),
                "displayed_yes_fillable_rate": (
                    round(horizon["displayed_yes_fillable_snapshots"] / horizon_quotes, 3)
                    if horizon_quotes
                    else None
                ),
                "displayed_no_fillable_rate": (
                    round(horizon["displayed_no_fillable_snapshots"] / horizon_quotes, 3)
                    if horizon_quotes
                    else None
                ),
                "displayed_any_fillable_rate": (
                    round(horizon["displayed_any_fillable_snapshots"] / horizon_quotes, 3)
                    if horizon_quotes
                    else None
                ),
                "paired_snapshots": paired_snapshots,
                "paired_fillable_opportunities": horizon["paired_fillable_opportunities"],
                "median_abs_midpoint_change_from_initial_cents": _median(horizon_midpoint_from_initial_abs),
                "median_yes_markout_cents": _median(horizon["yes_markouts"]),
                "median_no_markout_cents": _median(horizon["no_markouts"]),
                "median_best_markout_cents": _median(horizon["best_markouts"]),
                "mean_best_markout_cents": _mean(horizon["best_markouts"]),
                "positive_best_markout_rate": (
                    round(horizon["positive_best_markouts"] / horizon["paired_fillable_opportunities"], 3)
                    if horizon["paired_fillable_opportunities"]
                    else None
                ),
            }
            bootstrap_best_markout = _clustered_bootstrap_mean_ci(horizon["fillable_opportunity_records"])
            bootstrap_positive_rate = _clustered_bootstrap_positive_rate_ci(horizon["fillable_opportunity_records"])
            horizon_summary["bootstrap_mean_best_markout_cents"] = bootstrap_best_markout
            horizon_summary["bootstrap_positive_best_markout_rate"] = bootstrap_positive_rate
            horizon_summary["proof_checks"] = _proof_checks(
                source_events=source_events_count,
                paired_fillable_opportunities=horizon["paired_fillable_opportunities"],
                bootstrap_best_markout_ci_low=bootstrap_best_markout["ci_low"],
            )
            by_horizon[horizon_seconds] = horizon_summary
            ranked_horizon_rows.append({
                "event_class": event_class,
                "horizon_seconds": horizon_seconds,
                "source_events": source_events_count,
                "quoted_source_events": quoted_source_events,
                "capture_rate": (
                    round(quoted_source_events / source_events_count, 3)
                    if source_events_count
                    else None
                ),
                **horizon_summary,
            })
        summary = {
            "source_events": source_events_count,
            "quote_snapshots": quote_snapshots,
            "games": len(bucket["games"]),
            "players": len(bucket["players"]),
            "tickers": len(bucket["tickers"]),
            "quoted_source_events": quoted_source_events,
            "capture_rate": (
                round(quoted_source_events / source_events_count, 3)
                if source_events_count
                else None
            ),
            "median_latency_ms": _median(bucket["latencies"]),
            "p75_latency_ms": _percentile(bucket["latencies"], 75),
            "moved_quote_snapshots": bucket["moved_quote_snapshots"],
            "moved_quote_rate": (
                round(bucket["moved_quote_snapshots"] / quote_snapshots, 3)
                if quote_snapshots
                else None
            ),
            "median_abs_midpoint_change_cents": _median(midpoint_abs),
            "max_abs_midpoint_change_cents": max(midpoint_abs) if midpoint_abs else None,
            "by_horizon": by_horizon,
            "by_market_type": summarize_market_type_rows(bucket["by_market_type"]),
        }
        by_event_class[event_class] = summary
        ranked.append({"event_class": event_class, **summary})

    ranked.sort(
        key=lambda row: (
            row["source_events"],
            row["quoted_source_events"],
            row["moved_quote_snapshots"],
            row["quote_snapshots"],
            row["max_abs_midpoint_change_cents"] or 0,
        ),
        reverse=True,
    )
    ranked_horizon_rows.sort(
        key=lambda row: (
            row["proof_checks"]["bootstrap_mean_best_markout_ci_above_zero"],
            row["source_events"],
            row["paired_fillable_opportunities"],
            row["paired_snapshots"],
            row["quote_snapshots"],
            row["mean_best_markout_cents"] or 0,
        ),
        reverse=True,
    )

    overall_paired_fillable = len(overall_fillable_opportunities)
    overall_bootstrap_best_markout = _clustered_bootstrap_mean_ci(
        list(overall_fillable_opportunities.values())
    )

    return {
        "hypothesis_id": hypothesis_id,
        "source_events": len(source_events),
        "quote_snapshots": len(event_quotes),
        "quoted_source_events": len(overall_quoted_source_events),
        "capture_rate": (
            round(len(overall_quoted_source_events) / len(source_events), 3)
            if source_events
            else None
        ),
        "event_class_count": len(by_event_class),
        "paired_fillable_opportunities": overall_paired_fillable,
        "bootstrap_mean_best_markout_cents": overall_bootstrap_best_markout,
        "proof_checks": _proof_checks(
            source_events=len(source_events),
            paired_fillable_opportunities=overall_paired_fillable,
            bootstrap_best_markout_ci_low=overall_bootstrap_best_markout["ci_low"],
        ),
        "by_market_type": summarize_market_type_rows(overall_market_types),
        "by_event_class": by_event_class,
        "ranked_event_classes": ranked,
        "ranked_horizon_rows": ranked_horizon_rows,
    }


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _row_timestamp(row: dict) -> dt.datetime | None:
    for key in (
        "signal_timestamp_utc",
        "fill_timestamp_utc",
        "order_timestamp_utc",
        "settlement_timestamp_utc",
        "quote_timestamp_utc",
        "observed_at",
        "event_timestamp_utc",
        "timestamp",
        "created_time",
        "created_at",
        "event_time",
    ):
        parsed = _parse_timestamp(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _order_timestamp(row: dict) -> dt.datetime | None:
    for key in (
        "order_timestamp_utc",
        "timestamp",
        "created_time",
        "created_at",
        "event_time",
        "signal_timestamp_utc",
    ):
        parsed = _parse_timestamp(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _signal_group_label(row: dict) -> str:
    for key in ("signal_type", "book_c_strategy", "strategy", "book"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return "unclassified"


def _row_book_label(row: dict) -> str:
    value = row.get("book")
    return str(value) if value not in (None, "") else "unknown"


def _source_mapped_ticker_count(row: dict, key: str) -> int:
    value = row.get(key)
    if isinstance(value, list):
        return len([item for item in value if item not in (None, "")])
    if value in (None, ""):
        return 0
    return 1


def _market_type_for_ticker(ticker: Any) -> str:
    if ticker in (None, ""):
        return "unknown"
    parsed = parse_nba_ticker(str(ticker))
    if parsed and parsed.get("type") in {"game", "prop"}:
        return str(parsed["type"])
    return "unknown"


def _row_signal_id(row: dict) -> str | None:
    value = row.get("signal_id")
    if value in (None, ""):
        return None
    return str(value)


def _row_order_id(row: dict) -> str | None:
    value = row.get("order_id")
    if value in (None, ""):
        return None
    return str(value)


def _row_settlement_revenue_cents(row: dict) -> int | None:
    for key in ("settlement_revenue_cents", "revenue_cents"):
        value = _coerce_int(row.get(key))
        if value is not None:
            return value
    return None


def _row_fee_cents(row: dict) -> int | None:
    for key in ("fee_cents", "fees_cents", "commission_cents"):
        value = _coerce_int(row.get(key))
        if value is not None:
            return value
    return None


def _row_close_price_cents(row: dict) -> int | None:
    for key in ("close_price_cents", "settlement_price_cents", "close_price", "settlement_price"):
        value = _coerce_int(row.get(key))
        if value is not None:
            return value
    return None


def _row_close_value_cents(row: dict) -> int | None:
    close_price = _row_close_price_cents(row)
    if close_price is None:
        return None
    side = str(row.get("side") or row.get("direction") or "").strip().lower()
    if side == "no":
        return 100 - close_price
    return close_price


def _row_date(row: dict) -> str | None:
    timestamp = _row_timestamp(row)
    if timestamp is None:
        return None
    return timestamp.date().isoformat()


def _execution_cluster_key(row: dict) -> str:
    game_id = row.get("game_id")
    if game_id not in (None, ""):
        return f"game:{game_id}"
    ticker = row.get("ticker") or row.get("market_ticker")
    if ticker not in (None, ""):
        return f"ticker:{ticker}"
    order_id = _row_order_id(row)
    if order_id is not None:
        return f"order:{order_id}"
    signal_id = _row_signal_id(row)
    if signal_id is not None:
        return f"signal:{signal_id}"
    return "unclustered"


def _build_settlement_outcome_record(
    *,
    settlement: dict,
    source_row: dict,
    entry_cost_cents: int,
    settlement_revenue_cents: int | None,
    settlement_fees_cents: int | None,
    clv_cents: int | None,
    fill_count: int,
) -> dict[str, Any]:
    gross_pnl_cents = (
        settlement_revenue_cents - entry_cost_cents
        if settlement_revenue_cents is not None
        else None
    )
    net_pnl_cents = (
        settlement_revenue_cents - entry_cost_cents - settlement_fees_cents
        if settlement_revenue_cents is not None and settlement_fees_cents is not None
        else None
    )
    return {
        "outcome_id": (
            _row_signal_id(settlement)
            or _row_order_id(settlement)
            or settlement.get("event_id")
            or settlement.get("legacy_key")
            or _execution_cluster_key(source_row)
        ),
        "cluster_key": _execution_cluster_key(source_row),
        "trade_date": _row_date(settlement),
        "book": _row_book_label(source_row),
        "signal_type": _signal_group_label(source_row),
        "market_ticker": source_row.get("ticker") or source_row.get("market_ticker"),
        "game_id": source_row.get("game_id"),
        "signal_id": _row_signal_id(settlement) or _row_signal_id(source_row),
        "order_id": _row_order_id(settlement) or _row_order_id(source_row),
        "settlement_result": settlement.get("settlement_result"),
        "fill_count": fill_count,
        "entry_cost_cents": entry_cost_cents,
        "revenue_cents": settlement_revenue_cents,
        "fee_cents": settlement_fees_cents,
        "gross_pnl_cents": gross_pnl_cents,
        "net_pnl_cents": net_pnl_cents,
        "clv_cents": clv_cents,
    }


def _order_fill_summary(order: dict, fills_by_order_id: dict[str, list[dict]]) -> dict[str, Any]:
    order_id = _row_order_id(order)
    fills = fills_by_order_id.get(order_id or "", [])
    order_ts = _order_timestamp(order)
    fill_times = []
    total_fill_count = 0
    for fill in fills:
        fill_ts = _row_timestamp(fill)
        if fill_ts is not None:
            fill_times.append(fill_ts)
        fill_count = _coerce_int(fill.get("fill_count"))
        if fill_count is not None:
            total_fill_count += fill_count
    first_fill_ts = min(fill_times) if fill_times else None
    time_to_fill_seconds = None
    if order_ts is not None and first_fill_ts is not None:
        time_to_fill_seconds = round((first_fill_ts - order_ts).total_seconds(), 3)
    return {
        "order_id": order_id,
        "filled": bool(fills),
        "fill_rows": len(fills),
        "total_fill_count": total_fill_count if fills else 0,
        "time_to_fill_seconds": time_to_fill_seconds,
        "first_fill_timestamp_utc": first_fill_ts.isoformat() if first_fill_ts else None,
    }


def _filter_execution_rows_for_linked_signals(
    signal_rows: list[dict],
    order_rows: list[dict],
    fill_rows: list[dict],
    settlement_rows: list[dict],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    signal_ids = {
        signal_id
        for row in signal_rows
        if row.get("record_kind", "signal") == "signal"
        if (signal_id := _row_signal_id(row)) is not None
    }
    if not signal_ids:
        return signal_rows, [], [], []

    linked_orders = [
        row
        for row in order_rows
        if row.get("record_kind", "order_submission") == "order_submission"
        and _row_signal_id(row) in signal_ids
    ]
    linked_order_ids = {
        order_id
        for row in linked_orders
        if (order_id := _row_order_id(row)) is not None
    }
    linked_fills = [
        row
        for row in fill_rows
        if row.get("record_kind", "fill") == "fill"
        and (
            _row_signal_id(row) in signal_ids
            or _row_order_id(row) in linked_order_ids
        )
    ]
    linked_fill_order_ids = {
        order_id
        for row in linked_fills
        if (order_id := _row_order_id(row)) is not None
    }
    linked_settlements = [
        row
        for row in settlement_rows
        if row.get("record_kind", "settlement") == "settlement"
        and (
            _row_signal_id(row) in signal_ids
            or _row_order_id(row) in linked_order_ids
            or _row_order_id(row) in linked_fill_order_ids
        )
    ]
    return signal_rows, linked_orders, linked_fills, linked_settlements


def summarize_alpha_execution_capture(
    signal_rows: list[dict],
    order_rows: list[dict],
    fill_rows: list[dict],
    *,
    hypothesis_id: str | None = None,
    settlement_rows: list[dict] | None = None,
    require_signal_link: bool = False,
) -> dict:
    """Summarize alpha-ledger signal/order/fill rows without inventing fills."""

    def include(row: dict) -> bool:
        if hypothesis_id and row.get("hypothesis_id") != hypothesis_id:
            return False
        return True

    signals = [
        row for row in signal_rows
        if include(row) and row.get("record_kind", "signal") == "signal"
    ]
    orders = [
        row for row in order_rows
        if include(row) and row.get("record_kind", "order_submission") == "order_submission"
    ]
    fills = [
        row for row in fill_rows
        if include(row) and row.get("record_kind", "fill") == "fill"
    ]
    settlements = [
        row for row in (settlement_rows or [])
        if include(row) and row.get("record_kind", "settlement") == "settlement"
    ]
    if require_signal_link:
        signals, orders, fills, settlements = _filter_execution_rows_for_linked_signals(
            signals,
            orders,
            fills,
            settlements,
        )

    signals_by_id = {
        signal_id: signal
        for signal in signals
        if (signal_id := _row_signal_id(signal)) is not None
    }
    fills_by_order_id: dict[str, list[dict]] = {}
    for fill in fills:
        order_id = _row_order_id(fill)
        if order_id is None:
            continue
        fills_by_order_id.setdefault(order_id, []).append(fill)
    settlements_by_order_id: dict[str, list[dict]] = {}
    for settlement in settlements:
        order_id = _row_order_id(settlement)
        if order_id is None:
            continue
        settlements_by_order_id.setdefault(order_id, []).append(settlement)

    linked_orders: list[dict[str, Any]] = []
    orders_with_signal_id = 0
    signals_with_order = 0
    signals_with_filled_order = 0
    by_signal_type: dict[str, dict[str, Any]] = {}
    by_book: dict[str, dict[str, Any]] = {}

    orders_by_signal_id: dict[str, list[dict]] = {}
    for order in orders:
        signal_id = _row_signal_id(order)
        if signal_id is None:
            continue
        orders_by_signal_id.setdefault(signal_id, []).append(order)
    settlements_by_signal_id: dict[str, list[dict]] = {}
    for settlement in settlements:
        signal_id = _row_signal_id(settlement)
        if signal_id is None:
            continue
        settlements_by_signal_id.setdefault(signal_id, []).append(settlement)

    def _expected_fill_probability(row: dict) -> float | None:
        return _coerce_float(
            row.get("expected_fill_probability")
            if row.get("expected_fill_probability") is not None
            else row.get("predicted_fill_probability")
        )

    def ensure_bucket(bucket_map: dict[str, dict[str, Any]], key: str, *, book: str, signal_type: str) -> dict[str, Any]:
        bucket = bucket_map.setdefault(
            key,
            {
                "book": book,
                "signal_type": signal_type,
                "signal_rows": 0,
                "order_rows": 0,
                "fill_rows": 0,
                "filled_order_rows": 0,
                "time_to_fill_seconds": [],
                "expected_fill_probabilities": [],
                "signal_ids_with_order": set(),
                "signal_ids_with_fill": set(),
                "settled_signal_ids": set(),
                "settled_order_ids": set(),
                "settlement_rows": 0,
                "settlement_revenue_cents": 0,
                "settlement_entry_cost_cents": 0,
                "settlement_fees_cents": 0,
                "settlement_clv_cents": 0,
                "settled_contracts": 0,
                "has_close_data": False,
                "settlement_outcomes": [],
            },
        )
        bucket["book"] = bucket.get("book") or book
        bucket["signal_type"] = bucket.get("signal_type") or signal_type
        return bucket

    def accumulate_bucket(
        bucket: dict[str, Any],
        *,
        signal_rows: int = 0,
        order_rows: int = 0,
        fill_rows: int = 0,
        filled_order_rows: int = 0,
        time_to_fill_seconds: float | None = None,
        expected_fill_probability: float | None = None,
    ) -> None:
        bucket["signal_rows"] += signal_rows
        bucket["order_rows"] += order_rows
        bucket["fill_rows"] += fill_rows
        bucket["filled_order_rows"] += filled_order_rows
        if time_to_fill_seconds is not None:
            bucket["time_to_fill_seconds"].append(time_to_fill_seconds)
        if expected_fill_probability is not None:
            bucket["expected_fill_probabilities"].append(expected_fill_probability)

    for signal in signals:
        book = _row_book_label(signal)
        signal_type = _signal_group_label(signal)
        signal_bucket = ensure_bucket(by_signal_type, signal_type, book=book, signal_type=signal_type)
        book_bucket = ensure_bucket(by_book, book, book=book, signal_type=signal_type)
        accumulate_bucket(signal_bucket, signal_rows=1)
        accumulate_bucket(book_bucket, signal_rows=1)

        expected_fill_probability = _expected_fill_probability(signal)
        signal_id = _row_signal_id(signal)
        signal_orders = orders_by_signal_id.get(signal_id or "", []) if signal_id else []
        linked_orders_have_expected_fill = any(
            _expected_fill_probability(order) is not None for order in signal_orders
        )
        if expected_fill_probability is not None and not linked_orders_have_expected_fill:
            signal_bucket["expected_fill_probabilities"].append(expected_fill_probability)
            book_bucket["expected_fill_probabilities"].append(expected_fill_probability)

        if signal_orders:
            signals_with_order += 1
            if any(
                _row_order_id(order) and fills_by_order_id.get(_row_order_id(order) or "")
                for order in signal_orders
            ):
                signals_with_filled_order += 1

    for settlement in settlements:
        settlement_order_id = _row_order_id(settlement)
        linked_fills = fills_by_order_id.get(settlement_order_id or "", [])
        linked_order = None
        if settlement_order_id is not None:
            for order in orders:
                if _row_order_id(order) == settlement_order_id:
                    linked_order = order
                    break
        settlement_signal_id = _row_signal_id(settlement)
        order_signal = None
        if settlement_signal_id is not None:
            order_signal = signals_by_id.get(settlement_signal_id)
        elif linked_order is not None:
            order_signal = signals_by_id.get(_row_signal_id(linked_order) or "")
        source_row = order_signal or linked_order or settlement
        signal_type = _signal_group_label(source_row)
        book = _row_book_label(source_row)
        signal_bucket = ensure_bucket(by_signal_type, signal_type, book=book, signal_type=signal_type)
        book_bucket = ensure_bucket(by_book, book, book=book, signal_type=signal_type)

        settlement_fill_count = _coerce_int(settlement.get("fill_count"))
        if settlement_fill_count is None:
            settlement_fill_count = sum(_coerce_int(fill.get("fill_count")) or 0 for fill in linked_fills)
        entry_cost_cents = sum(
            (_coerce_int(fill.get("fill_price_cents")) or 0) * (_coerce_int(fill.get("fill_count")) or 0)
            for fill in linked_fills
        )
        if entry_cost_cents == 0:
            fallback_fill_price = _coerce_int(settlement.get("fill_price_cents"))
            if fallback_fill_price is not None and settlement_fill_count:
                entry_cost_cents = fallback_fill_price * settlement_fill_count

        revenue_cents = _row_settlement_revenue_cents(settlement)
        fee_cents = _row_fee_cents(settlement)
        close_value_cents = _row_close_value_cents(settlement)
        clv_cents = None
        if close_value_cents is not None and settlement_fill_count:
            avg_fill_price = entry_cost_cents / settlement_fill_count if settlement_fill_count else None
            if avg_fill_price is not None:
                clv_cents = int(round((close_value_cents - avg_fill_price) * settlement_fill_count))

        if settlement_signal_id is not None:
            signal_bucket["settled_signal_ids"].add(settlement_signal_id)
            book_bucket["settled_signal_ids"].add(settlement_signal_id)
        if settlement_order_id is not None:
            signal_bucket["settled_order_ids"].add(settlement_order_id)
            book_bucket["settled_order_ids"].add(settlement_order_id)
        signal_bucket["settlement_rows"] += 1
        book_bucket["settlement_rows"] += 1
        signal_bucket["settlement_entry_cost_cents"] += entry_cost_cents
        book_bucket["settlement_entry_cost_cents"] += entry_cost_cents
        if revenue_cents is not None:
            signal_bucket["settlement_revenue_cents"] += revenue_cents
            book_bucket["settlement_revenue_cents"] += revenue_cents
        if fee_cents is not None:
            signal_bucket["settlement_fees_cents"] += fee_cents
            book_bucket["settlement_fees_cents"] += fee_cents
        if clv_cents is not None:
            signal_bucket["settlement_clv_cents"] += clv_cents
            book_bucket["settlement_clv_cents"] += clv_cents
        if settlement_fill_count is not None:
            signal_bucket["settled_contracts"] += settlement_fill_count
            book_bucket["settled_contracts"] += settlement_fill_count
        if close_value_cents is not None:
            signal_bucket["has_close_data"] = True
            book_bucket["has_close_data"] = True

        outcome_record = _build_settlement_outcome_record(
            settlement=settlement,
            source_row=source_row,
            entry_cost_cents=entry_cost_cents,
            settlement_revenue_cents=revenue_cents,
            settlement_fees_cents=fee_cents,
            clv_cents=clv_cents,
            fill_count=settlement_fill_count or 0,
        )
        signal_bucket["settlement_outcomes"].append(dict(outcome_record))
        book_bucket["settlement_outcomes"].append(dict(outcome_record))

    for order in orders:
        signal_id = _row_signal_id(order)
        if signal_id is not None:
            orders_with_signal_id += 1
        order_summary = _order_fill_summary(order, fills_by_order_id)
        linked_orders.append(
            {
                **dict(order),
                **order_summary,
            }
        )
        order_signal = signals_by_id.get(signal_id) if signal_id else None
        signal_type = _signal_group_label(order_signal or order)
        book = _row_book_label(order_signal or order)
        signal_bucket = ensure_bucket(by_signal_type, signal_type, book=book, signal_type=signal_type)
        book_bucket = ensure_bucket(by_book, book, book=book, signal_type=signal_type)
        if signal_id is not None:
            signal_bucket["signal_ids_with_order"].add(signal_id)
            book_bucket["signal_ids_with_order"].add(signal_id)
            if order_summary["filled"]:
                signal_bucket["signal_ids_with_fill"].add(signal_id)
                book_bucket["signal_ids_with_fill"].add(signal_id)
        expected_fill_probability = _expected_fill_probability(order)
        accumulate_bucket(
            signal_bucket,
            order_rows=1,
            fill_rows=order_summary["fill_rows"],
            filled_order_rows=1 if order_summary["filled"] else 0,
            time_to_fill_seconds=order_summary["time_to_fill_seconds"],
            expected_fill_probability=expected_fill_probability,
        )
        accumulate_bucket(
            book_bucket,
            order_rows=1,
            fill_rows=order_summary["fill_rows"],
            filled_order_rows=1 if order_summary["filled"] else 0,
            time_to_fill_seconds=order_summary["time_to_fill_seconds"],
            expected_fill_probability=expected_fill_probability,
        )

    def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        time_to_fill_values = bucket.pop("time_to_fill_seconds", [])
        expected_fill_probabilities = bucket.pop("expected_fill_probabilities", [])
        signal_ids_with_order = bucket.pop("signal_ids_with_order", set())
        signal_ids_with_fill = bucket.pop("signal_ids_with_fill", set())
        settled_signal_ids = bucket.pop("settled_signal_ids", set())
        settled_order_ids = bucket.pop("settled_order_ids", set())
        settlement_rows_count = bucket.pop("settlement_rows", 0)
        settlement_revenue_cents = bucket.pop("settlement_revenue_cents", 0)
        settlement_entry_cost_cents = bucket.pop("settlement_entry_cost_cents", 0)
        settlement_fees_cents = bucket.pop("settlement_fees_cents", 0)
        settlement_clv_cents = bucket.pop("settlement_clv_cents", 0)
        settled_contracts = bucket.pop("settled_contracts", 0)
        has_close_data = bucket.pop("has_close_data", False)
        settlement_outcomes = bucket.pop("settlement_outcomes", [])
        bucket["order_fill_rate"] = (
            round(bucket["filled_order_rows"] / bucket["order_rows"], 3)
            if bucket["order_rows"]
            else None
        )
        bucket["signal_to_order_rate"] = (
            round(len(signal_ids_with_order) / bucket["signal_rows"], 3)
            if bucket["signal_rows"]
            else None
        )
        bucket["signal_fill_rate"] = (
            round(len(signal_ids_with_fill) / bucket["signal_rows"], 3)
            if bucket["signal_rows"]
            else None
        )
        bucket["expected_fill_probability_mean"] = (
            _mean_float(expected_fill_probabilities)
            if expected_fill_probabilities
            else None
        )
        bucket["settlement_rows"] = settlement_rows_count
        bucket["settled_signal_rows"] = len(settled_signal_ids)
        bucket["settled_order_rows"] = len(settled_order_ids)
        bucket["settled_contracts"] = settled_contracts
        bucket["has_settlement_data"] = settlement_rows_count > 0
        bucket["has_close_data"] = has_close_data
        bucket["realized_gross_pnl_cents"] = (
            settlement_revenue_cents - settlement_entry_cost_cents
            if settlement_rows_count and settlement_revenue_cents is not None
            else None
        )
        bucket["realized_net_pnl_cents"] = (
            settlement_revenue_cents - settlement_entry_cost_cents - settlement_fees_cents
            if settlement_rows_count and settlement_revenue_cents is not None and settlement_fees_cents
            else None
        )
        bucket["realized_gross_ev_per_contract_cents"] = (
            round((settlement_revenue_cents - settlement_entry_cost_cents) / settled_contracts, 3)
            if settlement_rows_count and settlement_revenue_cents is not None and settled_contracts
            else None
        )
        bucket["realized_net_ev_per_contract_cents"] = (
            round((settlement_revenue_cents - settlement_entry_cost_cents - settlement_fees_cents) / settled_contracts, 3)
            if settlement_rows_count and settlement_revenue_cents is not None and settlement_fees_cents and settled_contracts
            else None
        )
        bucket["realized_clv_cents"] = settlement_clv_cents if has_close_data else None
        bucket["realized_clv_per_contract_cents"] = (
            round(settlement_clv_cents / settled_contracts, 3)
            if has_close_data and settled_contracts
            else None
        )
        bucket["bootstrap_net_ev_per_signal_cents"] = _clustered_bootstrap_value_ci(
            settlement_outcomes,
            value_key="net_pnl_cents",
        )
        bucket["bootstrap_clv_per_signal_cents"] = _clustered_bootstrap_value_ci(
            settlement_outcomes,
            value_key="clv_cents",
        )
        bucket["bootstrap_positive_net_ev_rate"] = _clustered_bootstrap_positive_value_rate_ci(
            settlement_outcomes,
            value_key="net_pnl_cents",
        )
        bucket["bootstrap_positive_clv_rate"] = _clustered_bootstrap_positive_value_rate_ci(
            settlement_outcomes,
            value_key="clv_cents",
        )
        if not settlement_rows_count:
            bucket["pass_fail_status"] = "insufficient_close_data"
            bucket["pass_fail_reason"] = "no_settlement_rows"
        elif not has_close_data:
            bucket["pass_fail_status"] = "insufficient_close_data"
            bucket["pass_fail_reason"] = "no_close_price_data"
        elif not settlement_fees_cents:
            bucket["pass_fail_status"] = "insufficient_fee_data"
            bucket["pass_fail_reason"] = "no_fee_data"
        else:
            bucket["pass_fail_status"] = (
                "provisional_pass"
                if bucket["realized_net_pnl_cents"] is not None and bucket["realized_net_pnl_cents"] > 0 and bucket["realized_clv_cents"] is not None and bucket["realized_clv_cents"] > 0
                else "provisional_fail"
            )
            bucket["pass_fail_reason"] = (
                "positive_net_pnl_and_clv"
                if bucket["pass_fail_status"] == "provisional_pass"
                else "non_positive_pnl_or_clv"
            )
        bucket["mean_time_to_fill_seconds"] = _mean_float(time_to_fill_values)
        bucket["median_time_to_fill_seconds"] = _median(
            [int(round(value * 1000)) for value in time_to_fill_values]
        ) / 1000 if time_to_fill_values else None
        bucket["p75_time_to_fill_seconds"] = _percentile_float(time_to_fill_values, 75)
        return bucket

    by_signal_type = {
        key: finalize_bucket(value)
        for key, value in sorted(by_signal_type.items())
    }
    by_book = {
        key: finalize_bucket(value)
        for key, value in sorted(by_book.items())
    }

    fillable_order_rows = [order for order in linked_orders if order["filled"]]
    fill_time_samples = [
        order["time_to_fill_seconds"]
        for order in fillable_order_rows
        if order["time_to_fill_seconds"] is not None
    ]
    signal_ids_with_order = set(orders_by_signal_id)
    expected_fill_probs = []
    for order in orders:
        value = _expected_fill_probability(order)
        if value is not None:
            expected_fill_probs.append(value)
    for signal in signals:
        signal_id = _row_signal_id(signal)
        linked_orders = orders_by_signal_id.get(signal_id or "", []) if signal_id else []
        if linked_orders and any(_expected_fill_probability(order) is not None for order in linked_orders):
            continue
        value = _expected_fill_probability(signal)
        if value is not None:
            expected_fill_probs.append(value)
    expected_fill_probs = [value for value in expected_fill_probs if value is not None]

    settled_signal_ids: set[str] = set()
    settled_order_ids: set[str] = set()
    settlement_contracts = 0
    settlement_entry_cost_cents = 0
    settlement_revenue_cents = 0
    settlement_fees_cents = 0
    settlement_clv_cents = 0
    has_close_data = False
    settlement_outcomes: list[dict[str, Any]] = []
    for settlement in settlements:
        signal_id = _row_signal_id(settlement)
        if signal_id is not None:
            settled_signal_ids.add(signal_id)
        order_id = _row_order_id(settlement)
        if order_id is not None:
            settled_order_ids.add(order_id)
        linked_fills = fills_by_order_id.get(order_id or "", [])
        fill_count = _coerce_int(settlement.get("fill_count"))
        if fill_count is None:
            fill_count = sum(_coerce_int(fill.get("fill_count")) or 0 for fill in linked_fills)
        if fill_count is None:
            fill_count = 0
        settlement_contracts += fill_count
        entry_cost_cents = sum(
            (_coerce_int(fill.get("fill_price_cents")) or 0) * (_coerce_int(fill.get("fill_count")) or 0)
            for fill in linked_fills
        )
        if entry_cost_cents == 0:
            fallback_fill_price = _coerce_int(settlement.get("fill_price_cents"))
            if fallback_fill_price is not None and fill_count:
                entry_cost_cents = fallback_fill_price * fill_count
        settlement_entry_cost_cents += entry_cost_cents
        revenue_cents = _row_settlement_revenue_cents(settlement)
        if revenue_cents is not None:
            settlement_revenue_cents += revenue_cents
        fee_cents = _row_fee_cents(settlement)
        if fee_cents is not None:
            settlement_fees_cents += fee_cents
        close_value_cents = _row_close_value_cents(settlement)
        if close_value_cents is not None and fill_count:
            has_close_data = True
            avg_fill_price = entry_cost_cents / fill_count if fill_count else None
            if avg_fill_price is not None:
                settlement_clv_cents += int(round((close_value_cents - avg_fill_price) * fill_count))
        signal = signals_by_id.get(signal_id) if signal_id else None
        linked_order = None
        if order_id is not None:
            for order in orders:
                if _row_order_id(order) == order_id:
                    linked_order = order
                    break
        source_row = signal or linked_order or settlement
        clv_cents = None
        if close_value_cents is not None and fill_count:
            avg_fill_price = entry_cost_cents / fill_count if fill_count else None
            if avg_fill_price is not None:
                clv_cents = int(round((close_value_cents - avg_fill_price) * fill_count))
        settlement_outcomes.append(
            _build_settlement_outcome_record(
                settlement=settlement,
                source_row=source_row,
                entry_cost_cents=entry_cost_cents,
                settlement_revenue_cents=revenue_cents,
                settlement_fees_cents=fee_cents,
                clv_cents=clv_cents,
                fill_count=fill_count,
            )
        )

    gross_pnl_cents = (
        settlement_revenue_cents - settlement_entry_cost_cents
        if settlements and settlement_revenue_cents is not None
        else None
    )
    net_pnl_cents = (
        settlement_revenue_cents - settlement_entry_cost_cents - settlement_fees_cents
        if gross_pnl_cents is not None and settlement_fees_cents
        else None
    )
    gross_ev_per_contract_cents = (
        round(gross_pnl_cents / settlement_contracts, 3)
        if gross_pnl_cents is not None and settlement_contracts
        else None
    )
    net_ev_per_contract_cents = (
        round(net_pnl_cents / settlement_contracts, 3)
        if net_pnl_cents is not None and settlement_contracts
        else None
    )
    clv_per_contract_cents = (
        round(settlement_clv_cents / settlement_contracts, 3)
        if has_close_data and settlement_contracts
        else None
    )
    bootstrap_net_ev_per_signal = _clustered_bootstrap_value_ci(
        settlement_outcomes,
        value_key="net_pnl_cents",
    )
    bootstrap_clv_per_signal = _clustered_bootstrap_value_ci(
        settlement_outcomes,
        value_key="clv_cents",
    )
    bootstrap_positive_net_ev_rate = _clustered_bootstrap_positive_value_rate_ci(
        settlement_outcomes,
        value_key="net_pnl_cents",
    )
    bootstrap_positive_clv_rate = _clustered_bootstrap_positive_value_rate_ci(
        settlement_outcomes,
        value_key="clv_cents",
    )
    if not settlements:
        pass_fail_status = "insufficient_close_data"
        pass_fail_reason = "no_settlement_rows"
    elif not has_close_data:
        pass_fail_status = "insufficient_close_data"
        pass_fail_reason = "no_close_price_data"
    elif not settlement_fees_cents:
        pass_fail_status = "insufficient_fee_data"
        pass_fail_reason = "no_fee_data"
    else:
        pass_fail_status = (
            "provisional_pass"
            if net_pnl_cents is not None and net_pnl_cents > 0 and settlement_clv_cents > 0
            else "provisional_fail"
        )
        pass_fail_reason = (
            "positive_net_pnl_and_clv"
            if pass_fail_status == "provisional_pass"
            else "non_positive_pnl_or_clv"
        )

    summary = {
        "hypothesis_id": hypothesis_id,
        "signal_rows": len(signals),
        "order_rows": len(orders),
        "fill_rows": len(fills),
        "signals_with_order": signals_with_order,
        "signals_with_filled_order": signals_with_filled_order,
        "orders_with_signal_id": orders_with_signal_id,
        "orders_with_fills": len(fillable_order_rows),
        "no_fills_yet": len(fills) == 0,
        "signal_to_order_rate": round(signals_with_order / len(signals), 3) if signals else None,
        "signal_fill_rate": round(signals_with_filled_order / len(signals), 3) if signals else None,
        "signal_to_fill_rate": round(signals_with_filled_order / len(signals), 3) if signals else None,
        "order_fill_rate": (
            round(len(fillable_order_rows) / len(orders), 3)
            if orders
            else None
        ),
        "fill_rows_per_order": round(len(fills) / len(orders), 3) if orders else None,
        "mean_time_to_fill_seconds": _mean_float(fill_time_samples),
        "median_time_to_fill_seconds": _median([int(round(value * 1000)) for value in fill_time_samples]) / 1000 if fill_time_samples else None,
        "p75_time_to_fill_seconds": _percentile_float(fill_time_samples, 75),
        "expected_fill_probability_mean": _mean_float(expected_fill_probs),
        "settlement_rows": len(settlements),
        "settled_signal_rows": len(settled_signal_ids),
        "settled_order_rows": len(settled_order_ids),
        "settled_contracts": settlement_contracts,
        "has_settlement_data": bool(settlements),
        "has_close_data": has_close_data,
        "no_settlement_yet": not settlements,
        "no_close_yet": not has_close_data,
        "realized_gross_pnl_cents": gross_pnl_cents,
        "realized_net_pnl_cents": net_pnl_cents,
        "realized_gross_ev_per_contract_cents": gross_ev_per_contract_cents,
        "realized_net_ev_per_contract_cents": net_ev_per_contract_cents,
        "realized_clv_cents": settlement_clv_cents if has_close_data else None,
        "realized_clv_per_contract_cents": clv_per_contract_cents,
        "bootstrap_net_ev_per_signal_cents": bootstrap_net_ev_per_signal,
        "bootstrap_clv_per_signal_cents": bootstrap_clv_per_signal,
        "bootstrap_positive_net_ev_rate": bootstrap_positive_net_ev_rate,
        "bootstrap_positive_clv_rate": bootstrap_positive_clv_rate,
        "pass_fail_status": pass_fail_status,
        "pass_fail_reason": pass_fail_reason,
        "by_signal_type": by_signal_type,
        "by_book": by_book,
        "linked_orders": linked_orders,
        "execution_summary": {
            "settlement_rows": len(settlements),
            "settled_signal_rows": len(settled_signal_ids),
            "settled_order_rows": len(settled_order_ids),
            "settled_contracts": settlement_contracts,
            "has_settlement_data": bool(settlements),
            "has_close_data": has_close_data,
            "no_settlement_yet": not settlements,
            "no_close_yet": not has_close_data,
            "realized_gross_pnl_cents": gross_pnl_cents,
            "realized_net_pnl_cents": net_pnl_cents,
            "realized_gross_ev_per_contract_cents": gross_ev_per_contract_cents,
            "realized_net_ev_per_contract_cents": net_ev_per_contract_cents,
            "realized_clv_cents": settlement_clv_cents if has_close_data else None,
            "realized_clv_per_contract_cents": clv_per_contract_cents,
            "bootstrap_net_ev_per_signal_cents": bootstrap_net_ev_per_signal,
            "bootstrap_clv_per_signal_cents": bootstrap_clv_per_signal,
            "bootstrap_positive_net_ev_rate": bootstrap_positive_net_ev_rate,
            "bootstrap_positive_clv_rate": bootstrap_positive_clv_rate,
            "pass_fail_status": pass_fail_status,
            "pass_fail_reason": pass_fail_reason,
        },
    }
    return summary


def summarize_h1_daily_activity(
    source_rows: list[dict],
    quote_rows: list[dict],
    signal_rows: list[dict],
    order_rows: list[dict],
    fill_rows: list[dict],
    settlement_rows: list[dict],
    *,
    hypothesis_id: str | None = None,
    require_signal_link: bool = False,
) -> dict[str, Any]:
    def include(row: dict) -> bool:
        return not hypothesis_id or row.get("hypothesis_id") == hypothesis_id

    daily: dict[str, dict[str, Any]] = {}

    filtered_signals = [
        row for row in signal_rows
        if include(row) and row.get("record_kind", "signal") == "signal"
    ]
    filtered_orders = [
        row for row in order_rows
        if include(row) and row.get("record_kind", "order_submission") == "order_submission"
    ]
    filtered_fills = [
        row for row in fill_rows
        if include(row) and row.get("record_kind", "fill") == "fill"
    ]
    filtered_settlements = [
        row for row in settlement_rows
        if include(row) and row.get("record_kind", "settlement") == "settlement"
    ]
    if require_signal_link:
        filtered_signals, filtered_orders, filtered_fills, filtered_settlements = _filter_execution_rows_for_linked_signals(
            filtered_signals,
            filtered_orders,
            filtered_fills,
            filtered_settlements,
        )

    def ensure_day(day: str) -> dict[str, Any]:
        return daily.setdefault(
            day,
            {
                "date": day,
                "source_events": 0,
                "quote_snapshots": 0,
                "signals": 0,
                "orders": 0,
                "fills": 0,
                "settlements": 0,
                "settled_contracts": 0,
                "realized_net_pnl_cents": 0,
                "realized_clv_cents": 0,
            },
        )

    for row in source_rows:
        if not include(row) or row.get("record_kind", "source_event") != "source_event":
            continue
        day = _row_date(row)
        if day is None:
            continue
        ensure_day(day)["source_events"] += 1

    for row in quote_rows:
        if not include(row):
            continue
        day = _row_date(row)
        if day is None:
            continue
        ensure_day(day)["quote_snapshots"] += 1

    for row in filtered_signals:
        day = _row_date(row)
        if day is None:
            continue
        ensure_day(day)["signals"] += 1

    for row in filtered_orders:
        day = _row_date(row)
        if day is None:
            continue
        ensure_day(day)["orders"] += 1

    for row in filtered_fills:
        day = _row_date(row)
        if day is None:
            continue
        ensure_day(day)["fills"] += 1

    for row in filtered_settlements:
        day = _row_date(row)
        if day is None:
            continue
        bucket = ensure_day(day)
        bucket["settlements"] += 1
        fill_count = _coerce_int(row.get("fill_count")) or 0
        bucket["settled_contracts"] += fill_count
        revenue_cents = _row_settlement_revenue_cents(row)
        fee_cents = _row_fee_cents(row)
        fill_price_cents = _coerce_int(row.get("fill_price_cents"))
        entry_cost_cents = None
        if fill_price_cents is not None and fill_count:
            entry_cost_cents = fill_price_cents * fill_count
        if revenue_cents is not None and entry_cost_cents is not None and fee_cents is not None:
            bucket["realized_net_pnl_cents"] += revenue_cents - entry_cost_cents - fee_cents
        close_value_cents = _row_close_value_cents(row)
        if close_value_cents is not None and entry_cost_cents is not None and fill_count:
            avg_fill_price = entry_cost_cents / fill_count
            bucket["realized_clv_cents"] += int(round((close_value_cents - avg_fill_price) * fill_count))

    rows = [daily[key] for key in sorted(daily)]
    settled_rows = [row for row in rows if row["settlements"] > 0]
    positive_net_days = sum(1 for row in settled_rows if row["realized_net_pnl_cents"] > 0)
    positive_clv_days = sum(1 for row in settled_rows if row["realized_clv_cents"] > 0)
    for row in rows:
        row["capture_rate"] = (
            round(row["quote_snapshots"] / row["source_events"], 3)
            if row["source_events"]
            else None
        )
        row["order_fill_rate_proxy"] = (
            round(row["fills"] / row["orders"], 3)
            if row["orders"]
            else None
        )
    return {
        "days": len(rows),
        "settled_days": len(settled_rows),
        "positive_net_pnl_days": positive_net_days,
        "positive_clv_days": positive_clv_days,
        "positive_net_pnl_day_share": (
            round(positive_net_days / len(settled_rows), 3)
            if settled_rows
            else None
        ),
        "positive_clv_day_share": (
            round(positive_clv_days / len(settled_rows), 3)
            if settled_rows
            else None
        ),
        "rows": rows,
    }


def summarize_h1_decision(
    latency_summary: dict[str, Any],
    execution_summary: dict[str, Any],
    daily_summary: dict[str, Any],
) -> dict[str, Any]:
    ranked_event_classes = latency_summary.get("ranked_event_classes", [])
    best_event = None
    passing_latency_events = []
    for row in ranked_event_classes:
        median_ms = _coerce_int(row.get("median_latency_ms"))
        p75_ms = _coerce_int(row.get("p75_latency_ms"))
        if best_event is None:
            best_event = row
        else:
            current_key = (
                median_ms or -1,
                p75_ms or -1,
                _coerce_int(row.get("source_events")) or 0,
            )
            best_key = (
                _coerce_int(best_event.get("median_latency_ms")) or -1,
                _coerce_int(best_event.get("p75_latency_ms")) or -1,
                _coerce_int(best_event.get("source_events")) or 0,
            )
            if current_key > best_key:
                best_event = row
        if (median_ms or 0) > 3000 and (p75_ms or 0) > 5000:
            passing_latency_events.append(str(row.get("event_class") or "unknown"))

    bootstrap_net = execution_summary.get("bootstrap_net_ev_per_signal_cents", {})
    bootstrap_clv = execution_summary.get("bootstrap_clv_per_signal_cents", {})
    criteria = {
        "latency_gate_any_event_class": bool(passing_latency_events),
        "labeled_opportunities_gte_200": (latency_summary.get("source_events") or 0) >= 200,
        "fillable_pairs_gte_100": (latency_summary.get("paired_fillable_opportunities") or 0) >= 100,
        "fill_rate_gte_20pct": (execution_summary.get("order_fill_rate") or 0.0) >= 0.20,
        "net_ev_bootstrap_ci_above_zero": (
            bootstrap_net.get("ci_low") is not None and float(bootstrap_net["ci_low"]) > 0
        ),
        "clv_bootstrap_ci_above_zero": (
            bootstrap_clv.get("ci_low") is not None and float(bootstrap_clv["ci_low"]) > 0
        ),
        "positive_clv_day_share_gte_60pct": (
            daily_summary.get("positive_clv_day_share") is not None
            and float(daily_summary["positive_clv_day_share"]) >= 0.60
        ),
    }

    blockers = []
    if not execution_summary.get("has_settlement_data"):
        blockers.append("no_settlement_rows")
    if execution_summary.get("has_settlement_data") and not execution_summary.get("has_close_data"):
        blockers.append("no_close_price_data")
    if execution_summary.get("has_settlement_data") and execution_summary.get("realized_net_pnl_cents") is not None:
        if execution_summary.get("bootstrap_net_ev_per_signal_cents", {}).get("sample_size", 0) == 0:
            blockers.append("no_bootstrap_net_ev_data")
    if execution_summary.get("has_settlement_data") and execution_summary.get("realized_clv_cents") is not None:
        if execution_summary.get("bootstrap_clv_per_signal_cents", {}).get("sample_size", 0) == 0:
            blockers.append("no_bootstrap_clv_data")

    if blockers:
        status = "insufficient_data"
        reasons = blockers
    else:
        pass_checks = (
            criteria["latency_gate_any_event_class"]
            and criteria["labeled_opportunities_gte_200"]
            and criteria["fill_rate_gte_20pct"]
            and criteria["net_ev_bootstrap_ci_above_zero"]
        )
        status = "pass" if pass_checks else "fail"
        reasons = [
            key for key, value in criteria.items()
            if key in {
                "latency_gate_any_event_class",
                "labeled_opportunities_gte_200",
                "fill_rate_gte_20pct",
                "net_ev_bootstrap_ci_above_zero",
            }
            and not value
        ] or ["all_required_criteria_met"]

    return {
        "status": status,
        "reasons": reasons,
        "passing_latency_event_classes": passing_latency_events,
        "best_latency_event_class": best_event.get("event_class") if isinstance(best_event, dict) else None,
        "best_latency_median_ms": best_event.get("median_latency_ms") if isinstance(best_event, dict) else None,
        "best_latency_p75_ms": best_event.get("p75_latency_ms") if isinstance(best_event, dict) else None,
        "source_events": latency_summary.get("source_events"),
        "paired_fillable_opportunities": latency_summary.get("paired_fillable_opportunities"),
        "order_fill_rate": execution_summary.get("order_fill_rate"),
        "realized_net_pnl_cents": execution_summary.get("realized_net_pnl_cents"),
        "realized_clv_cents": execution_summary.get("realized_clv_cents"),
        "bootstrap_net_ev_per_signal_cents": bootstrap_net,
        "bootstrap_clv_per_signal_cents": bootstrap_clv,
        "positive_clv_day_share": daily_summary.get("positive_clv_day_share"),
        "criteria": criteria,
    }


__all__ = [
    "classify_live_event",
    "summarize_alpha_execution_capture",
    "summarize_h1_daily_activity",
    "summarize_h1_decision",
    "summarize_latency_capture",
]
