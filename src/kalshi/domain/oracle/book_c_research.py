"""Offline Book C opportunity analysis from stored Real game feeds."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[4]
DEFAULT_BOOK_C_FEEDS_PATH = PROJECT_DIR / "research" / "nba-props" / "data" / "processed" / "game_feeds"

_SUPPORTED_SIGNAL_SPECS = (
    {
        "event_class": "clutch_comeback",
        "snapshot_event": "clutch_moment",
        "priority_prior": 0.95,
        "direction": "fade late-game comeback enthusiasm",
        "rationale": "Matches the ranked H3 thesis and the strongest behavioral late-game research.",
        "max_margin": 6,
        "max_time_remaining": 120,
    },
    {
        "event_class": "ot_likely",
        "snapshot_event": "clutch_moment",
        "priority_prior": 0.90,
        "direction": "buy extra-time over candidates",
        "rationale": "Extra minutes create a direct mechanical path to counting-stat overs.",
        "max_margin": 2,
        "max_time_remaining": 120,
    },
    {
        "event_class": "blowout",
        "snapshot_event": "blowout_moment",
        "priority_prior": 0.80,
        "direction": "sell starter overs in garbage-time risk",
        "rationale": "Existing Book C signal family already supports this and the trigger is clean.",
    },
    {
        "event_class": "technical_foul",
        "snapshot_event": "technical_foul",
        "priority_prior": 0.35,
        "direction": "treat as context/volatility trigger first",
        "rationale": "Frequent in the stored feeds, but the direct trade mapping is weaker than OT or blowout.",
    },
)

_UNSUPPORTED_SIGNAL_SPECS = (
    {
        "event_class": "foul_trouble",
        "reason": "processed game feeds do not retain per-player personal foul counts",
    },
    {
        "event_class": "scoring_run",
        "reason": "processed game feeds do not retain scoring-run text or possession sequences",
    },
)

_BOOK_C_SWEEP_SPECS = {
    "clutch_comeback": {
        "snapshot_event": "clutch_moment",
        "priority_prior": 0.95,
        "parameter_name": "max_margin",
        "candidate_grid": (
            {"max_margin": 1, "max_time_remaining": 30},
            {"max_margin": 1, "max_time_remaining": 60},
            {"max_margin": 2, "max_time_remaining": 30},
            {"max_margin": 2, "max_time_remaining": 60},
            {"max_margin": 3, "max_time_remaining": 30},
            {"max_margin": 3, "max_time_remaining": 60},
            {"max_margin": 4, "max_time_remaining": 30},
            {"max_margin": 4, "max_time_remaining": 60},
            {"max_margin": 5, "max_time_remaining": 30},
            {"max_margin": 5, "max_time_remaining": 60},
            {"max_margin": 6, "max_time_remaining": 30},
            {"max_margin": 6, "max_time_remaining": 60},
        ),
    },
    "ot_likely": {
        "snapshot_event": "clutch_moment",
        "priority_prior": 0.90,
        "parameter_name": "max_margin",
        "candidate_grid": (
            {"max_margin": 0, "max_time_remaining": 30},
            {"max_margin": 0, "max_time_remaining": 60},
            {"max_margin": 1, "max_time_remaining": 30},
            {"max_margin": 1, "max_time_remaining": 60},
            {"max_margin": 2, "max_time_remaining": 30},
            {"max_margin": 2, "max_time_remaining": 60},
        ),
    },
    "blowout": {
        "snapshot_event": "blowout_moment",
        "priority_prior": 0.80,
        "parameter_name": "min_margin",
        "candidate_grid": (
            {"min_margin": 20, "min_period": 3},
            {"min_margin": 20, "min_period": 4},
            {"min_margin": 22, "min_period": 3},
            {"min_margin": 22, "min_period": 4},
            {"min_margin": 24, "min_period": 3},
            {"min_margin": 24, "min_period": 4},
            {"min_margin": 26, "min_period": 3},
            {"min_margin": 26, "min_period": 4},
        ),
    },
}


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _period_rank(value: Any) -> int | None:
    period = _coerce_int(value)
    if period is not None:
        return period
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text == "OT":
        return 5
    if text.startswith("OT") and text[2:].isdigit():
        return 4 + int(text[2:])
    return None


def _safe_games_share(count: int, total_games: int) -> float:
    if total_games <= 0:
        return 0.0
    return count / float(total_games)


def _priority_score(*, games_share: float, opportunities_per_game: float, priority_prior: float) -> float:
    # The heuristic intentionally favors ranked research priors over raw frequency alone.
    density_score = min(max(opportunities_per_game, 0.0) / 5.0, 1.0)
    return round((0.40 * games_share) + (0.20 * density_score) + (0.40 * priority_prior), 3)


def _recommendation_label(score: float) -> str:
    if score >= 0.70:
        return "build now"
    if score >= 0.45:
        return "parallel research"
    return "defer"


def _matches_snapshot(snapshot: dict, spec: dict) -> bool:
    if str(snapshot.get("event", "")).strip() != spec["snapshot_event"]:
        return False
    margin = _coerce_int(snapshot.get("margin"))
    time_remaining = _coerce_int(snapshot.get("time_remaining"))
    max_margin = spec.get("max_margin")
    max_time_remaining = spec.get("max_time_remaining")
    if max_margin is not None and (margin is None or margin > max_margin):
        return False
    if max_time_remaining is not None and (time_remaining is None or time_remaining > max_time_remaining):
        return False
    return True


def _period_label(value: Any) -> str | None:
    period_rank = _period_rank(value)
    if period_rank is None:
        return None
    if period_rank == 5:
        return "OT"
    return f"Q{period_rank}"


def _matches_sweep_candidate(snapshot: dict, signal_class: str, candidate: dict) -> bool:
    if signal_class in ("clutch_comeback", "ot_likely"):
        margin = _coerce_int(snapshot.get("margin"))
        time_remaining = _coerce_int(snapshot.get("time_remaining"))
        if margin is None or time_remaining is None:
            return False
        return margin <= candidate["max_margin"] and time_remaining <= candidate["max_time_remaining"]

    if signal_class == "blowout":
        margin = _coerce_int(snapshot.get("margin"))
        period_rank = _period_rank(snapshot.get("period"))
        if margin is None or period_rank is None:
            return False
        return margin >= candidate["min_margin"] and period_rank >= candidate["min_period"]

    return False


def _candidate_window_label(signal_class: str, candidate: dict) -> str:
    if signal_class in ("clutch_comeback", "ot_likely"):
        return f"<= {candidate['max_margin']} pts, <= {candidate['max_time_remaining']}s"
    return f">= {candidate['min_margin']} pts, >= {_period_label(candidate['min_period']) or 'Q3'}"


def _sweep_priority_row(
    *,
    signal_class: str,
    candidate: dict,
    total_games: int,
    games_with_signal: int,
    opportunities: int,
    priority_prior: float,
) -> dict:
    games_share = _safe_games_share(games_with_signal, total_games)
    opportunities_per_game = (opportunities / float(total_games)) if total_games else 0.0
    score = _priority_score(
        games_share=games_share,
        opportunities_per_game=opportunities_per_game,
        priority_prior=priority_prior,
    )
    row = {
        "event_class": signal_class,
        "parameters": candidate,
        "window": _candidate_window_label(signal_class, candidate),
        "games_with_signal": games_with_signal,
        "games_share": round(games_share, 3),
        "opportunities": opportunities,
        "opportunities_per_game": round(opportunities_per_game, 2),
        "priority_prior": priority_prior,
        "priority_score": score,
        "recommendation": _recommendation_label(score),
        "data_ready": True,
    }
    return row


def summarize_book_c_parameter_sweep(feeds: list[dict]) -> dict:
    total_games = len(feeds)
    sweeps = {}

    for signal_class, spec in _BOOK_C_SWEEP_SPECS.items():
        rows = []
        for candidate in spec["candidate_grid"]:
            opportunities = 0
            games_with_signal = 0
            for feed in feeds:
                matched = False
                for snapshot in feed.get("game_state_snapshots", []):
                    if not isinstance(snapshot, dict):
                        continue
                    if not _matches_sweep_candidate(snapshot, signal_class, candidate):
                        continue
                    matched = True
                    opportunities += 1
                if matched:
                    games_with_signal += 1
            rows.append(
                _sweep_priority_row(
                    signal_class=signal_class,
                    candidate=candidate,
                    total_games=total_games,
                    games_with_signal=games_with_signal,
                    opportunities=opportunities,
                    priority_prior=spec["priority_prior"],
                )
            )

        rows.sort(
            key=lambda row: (
                -row["priority_score"],
                -row["games_with_signal"],
                -row["opportunities"],
                tuple(sorted(row["parameters"].items())),
            )
        )
        for index, row in enumerate(rows, start=1):
            row["rank"] = index

        sweeps[signal_class] = {
            "best": rows[0] if rows else None,
            "rows": rows,
        }

    return {
        "total_games": total_games,
        "sweeps": sweeps,
        "best_parameters": {signal_class: payload["best"] for signal_class, payload in sweeps.items()},
    }


def _final_score(feed: dict) -> tuple[int | None, int | None]:
    best_key = None
    best_score = None
    for snapshot in feed.get("game_state_snapshots", []):
        if not isinstance(snapshot, dict) or snapshot.get("event") != "period_end":
            continue
        home_score = _coerce_int(snapshot.get("home_score"))
        away_score = _coerce_int(snapshot.get("away_score"))
        period_rank = _period_rank(snapshot.get("period"))
        if home_score is None or away_score is None or period_rank is None:
            continue
        score_key = (period_rank, home_score + away_score)
        if best_key is None or score_key > best_key:
            best_key = score_key
            best_score = (home_score, away_score)
    return best_score if best_score is not None else (None, None)


def _clutch_time_bucket(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if 60 < seconds <= 120:
        return "2:00-1:00"
    if 30 < seconds <= 60:
        return "1:00-0:30"
    if 10 <= seconds <= 30:
        return "0:30-0:10"
    return None


def _clutch_margin_bucket(margin: int | None) -> str | None:
    if margin is None:
        return None
    if margin == 1:
        return "1"
    if margin == 2:
        return "2"
    if 3 <= margin <= 5:
        return "3-5"
    if 6 <= margin <= 8:
        return "6-8"
    return None


def evaluate_clutch_comeback_buckets(feeds: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for feed in feeds:
        final_home, final_away = _final_score(feed)
        if final_home is None or final_away is None or final_home == final_away:
            continue
        final_home_won = final_home > final_away
        game_id = feed.get("game_id")

        per_game_candidates: dict[tuple[str, str, str], dict] = {}
        for snapshot in feed.get("game_state_snapshots", []):
            if not isinstance(snapshot, dict):
                continue
            if snapshot.get("event") != "clutch_moment":
                continue
            margin = _coerce_int(snapshot.get("margin"))
            time_remaining = _coerce_int(snapshot.get("time_remaining"))
            home_score = _coerce_int(snapshot.get("home_score"))
            away_score = _coerce_int(snapshot.get("away_score"))
            time_bucket = _clutch_time_bucket(time_remaining)
            margin_bucket = _clutch_margin_bucket(margin)
            if (
                time_bucket is None
                or margin_bucket is None
                or home_score is None
                or away_score is None
                or home_score == away_score
            ):
                continue

            trailing_side = "home" if home_score < away_score else "away"
            dedupe_key = (time_bucket, margin_bucket, trailing_side)
            existing = per_game_candidates.get(dedupe_key)
            if existing is None or time_remaining > existing["time_remaining"]:
                per_game_candidates[dedupe_key] = {
                    "time_bucket": time_bucket,
                    "margin_bucket": margin_bucket,
                    "trailing_side": trailing_side,
                    "time_remaining": time_remaining,
                    "margin": margin,
                }

        for candidate in per_game_candidates.values():
            bucket_key = (candidate["time_bucket"], candidate["margin_bucket"])
            row = buckets.setdefault(
                bucket_key,
                {
                    "time_bucket": candidate["time_bucket"],
                    "margin_bucket": candidate["margin_bucket"],
                    "opportunities": 0,
                    "games": set(),
                    "trailing_wins": 0,
                    "leader_holds": 0,
                },
            )
            row["opportunities"] += 1
            if game_id not in (None, ""):
                row["games"].add(str(game_id))

            trailing_won = final_home_won if candidate["trailing_side"] == "home" else (not final_home_won)
            if trailing_won:
                row["trailing_wins"] += 1
            else:
                row["leader_holds"] += 1

    def time_order(bucket: str) -> int:
        order = {"2:00-1:00": 0, "1:00-0:30": 1, "0:30-0:10": 2}
        return order.get(bucket, 99)

    def margin_order(bucket: str) -> int:
        order = {"1": 0, "2": 1, "3-5": 2, "6-8": 3}
        return order.get(bucket, 99)

    rows = []
    for row in buckets.values():
        opportunities = row["opportunities"]
        trailing_win_rate = row["trailing_wins"] / float(opportunities) if opportunities else None
        leader_hold_rate = row["leader_holds"] / float(opportunities) if opportunities else None
        rows.append(
            {
                "time_bucket": row["time_bucket"],
                "margin_bucket": row["margin_bucket"],
                "opportunities": opportunities,
                "games": len(row["games"]),
                "trailing_wins": row["trailing_wins"],
                "leader_holds": row["leader_holds"],
                "trailing_win_rate": None if trailing_win_rate is None else round(trailing_win_rate, 3),
                "leader_hold_rate": None if leader_hold_rate is None else round(leader_hold_rate, 3),
            }
        )

    rows.sort(
        key=lambda row: (
            time_order(row["time_bucket"]),
            margin_order(row["margin_bucket"]),
        )
    )
    return rows


def load_processed_game_feeds(path: str | Path = DEFAULT_BOOK_C_FEEDS_PATH) -> list[dict]:
    base = Path(path)
    feeds = []
    if not base.exists():
        return feeds
    for feed_path in sorted(base.glob("*.json")):
        if feed_path.name == "collection_summary.json":
            continue
        try:
            payload = json.loads(feed_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            feeds.append(payload)
    return feeds


def summarize_book_c_dataset(feeds: list[dict]) -> dict:
    total_games = len(feeds)
    supported = []
    clutch_comeback_buckets = evaluate_clutch_comeback_buckets(feeds)

    for spec in _SUPPORTED_SIGNAL_SPECS:
        opportunities = 0
        games_with_signal = 0
        margins = []
        times = []

        for feed in feeds:
            matched = []
            for snapshot in feed.get("game_state_snapshots", []):
                if not isinstance(snapshot, dict):
                    continue
                if not _matches_snapshot(snapshot, spec):
                    continue
                matched.append(snapshot)
                margin = _coerce_int(snapshot.get("margin"))
                time_remaining = _coerce_int(snapshot.get("time_remaining"))
                if margin is not None:
                    margins.append(margin)
                if time_remaining is not None:
                    times.append(time_remaining)
            if matched:
                games_with_signal += 1
                opportunities += len(matched)

        games_share = _safe_games_share(games_with_signal, total_games)
        opportunities_per_game = (opportunities / float(total_games)) if total_games else 0.0
        score = _priority_score(
            games_share=games_share,
            opportunities_per_game=opportunities_per_game,
            priority_prior=spec["priority_prior"],
        )
        supported.append(
            {
                "event_class": spec["event_class"],
                "direction": spec["direction"],
                "rationale": spec["rationale"],
                "games_with_signal": games_with_signal,
                "games_share": round(games_share, 3),
                "opportunities": opportunities,
                "opportunities_per_game": round(opportunities_per_game, 2),
                "median_margin": None if not margins else int(round(statistics.median(margins))),
                "median_time_remaining_seconds": None if not times else int(round(statistics.median(times))),
                "priority_prior": spec["priority_prior"],
                "priority_score": score,
                "recommendation": _recommendation_label(score),
                "data_ready": True,
            }
        )

    ranked = sorted(
        supported,
        key=lambda row: (
            -row["priority_score"],
            -row["games_with_signal"],
            -row["opportunities"],
            row["event_class"],
        ),
    )
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index

    unsupported = []
    for spec in _UNSUPPORTED_SIGNAL_SPECS:
        unsupported.append(
            {
                "event_class": spec["event_class"],
                "reason": spec["reason"],
                "data_ready": False,
                "recommendation": "data gap",
            }
        )

    return {
        "total_games": total_games,
        "supported_event_classes": supported,
        "ranked_event_classes": ranked,
        "unsupported_event_classes": unsupported,
        "top_recommendation": ranked[0]["event_class"] if ranked else None,
        "clutch_comeback_buckets": clutch_comeback_buckets,
    }


__all__ = [
    "DEFAULT_BOOK_C_FEEDS_PATH",
    "evaluate_clutch_comeback_buckets",
    "load_processed_game_feeds",
    "summarize_book_c_parameter_sweep",
    "summarize_book_c_dataset",
]
