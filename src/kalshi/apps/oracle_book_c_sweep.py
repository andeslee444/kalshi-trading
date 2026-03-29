#!/usr/bin/env python3
"""Offline Book C parameter sweep report from stored Real game feeds."""

from __future__ import annotations

import argparse
import json

from domain.oracle.book_c_research import (
    DEFAULT_BOOK_C_FEEDS_PATH,
    load_processed_game_feeds,
    summarize_book_c_parameter_sweep,
)


def _render_int(value) -> str:
    return "-" if value is None else str(value)


def _render_score(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _signal_header(signal_class: str, payload: dict) -> list[str]:
    best = payload.get("best") or {}
    params = best.get("parameters", {})
    if signal_class in ("clutch_comeback", "ot_likely"):
        param_text = f"max_margin={params.get('max_margin', '-')}, max_time_remaining={params.get('max_time_remaining', '-')}"
    else:
        period = params.get("min_period")
        period_text = f"Q{period}" if isinstance(period, int) else str(period or "-")
        param_text = f"min_margin={params.get('min_margin', '-')}, min_period={period_text}"
    return [
        f"{signal_class}: best={param_text} score={_render_score(best.get('priority_score'))} recommendation={best.get('recommendation', '-')}",
        "rank  window                               games  share%  opps  opp/g  score  next",
        "----  ----------------------------------  -----  ------  ----  -----  -----  ----------------",
    ]


def _signal_rows(payload: dict, limit: int) -> list[str]:
    rows = []
    for row in payload.get("rows", [])[:limit]:
        rows.append(
            f"{row['rank']:>4}  "
            f"{row['window']:<34}  "
            f"{row['games_with_signal']:>5}  "
            f"{int(round(row['games_share'] * 100)):>6}%  "
            f"{row['opportunities']:>4}  "
            f"{row['opportunities_per_game']:>5}  "
            f"{_render_score(row['priority_score']):>5}  "
            f"{row['recommendation']}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle Book C parameter sweep report")
    parser.add_argument(
        "--feeds-path",
        default=str(DEFAULT_BOOK_C_FEEDS_PATH),
        help="Path to processed Real game-feed JSON files",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum candidate rows to print per signal in table mode",
    )
    args = parser.parse_args()

    feeds = load_processed_game_feeds(args.feeds_path)
    summary = summarize_book_c_parameter_sweep(feeds)

    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"feeds_path={args.feeds_path}")
    print(f"games={summary['total_games']} signals={len(summary['sweeps'])}")
    for signal_class, payload in summary["sweeps"].items():
        print("")
        for line in _signal_header(signal_class, payload):
            print(line)
        for line in _signal_rows(payload, args.limit):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
