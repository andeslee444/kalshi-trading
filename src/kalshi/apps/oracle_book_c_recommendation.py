#!/usr/bin/env python3
"""Report Book C build priorities from stored Real game feeds."""

from __future__ import annotations

import argparse
import json

from domain.oracle.book_c_research import DEFAULT_BOOK_C_FEEDS_PATH, load_processed_game_feeds, summarize_book_c_dataset


def _render_int(value) -> str:
    return "-" if value is None else str(value)


def _table_rows(summary: dict, limit: int) -> list[str]:
    rows = [
        "rank  signal             games  share%  opps  opp/g  med_t   med_m  score  next",
        "----  -----------------  -----  ------  ----  -----  ------  -----  -----  ----------------",
    ]
    for row in summary.get("ranked_event_classes", [])[:limit]:
        rows.append(
            f"{row['rank']:>4}  "
            f"{row['event_class']:<17}  "
            f"{row['games_with_signal']:>5}  "
            f"{int(round(row['games_share'] * 100)):>6}%  "
            f"{row['opportunities']:>4}  "
            f"{row['opportunities_per_game']:>5}  "
            f"{_render_int(row['median_time_remaining_seconds']):>6}  "
            f"{_render_int(row['median_margin']):>5}  "
            f"{row['priority_score']:>5}  "
            f"{row['recommendation']}"
        )
    return rows


def _clutch_bucket_rows(summary: dict) -> list[str]:
    rows = [
        "bucket     margin  games  opps  trail_win%  leader_hold%",
        "---------  ------  -----  ----  ----------  ------------",
    ]
    for row in summary.get("clutch_comeback_buckets", []):
        trailing = "-" if row.get("trailing_win_rate") is None else f"{int(round(row['trailing_win_rate'] * 100))}%"
        leader = "-" if row.get("leader_hold_rate") is None else f"{int(round(row['leader_hold_rate'] * 100))}%"
        rows.append(
            f"{row['time_bucket']:<9}  "
            f"{row['margin_bucket']:>6}  "
            f"{row['games']:>5}  "
            f"{row['opportunities']:>4}  "
            f"{trailing:>10}  "
            f"{leader:>12}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle Book C recommendation report")
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
        default=10,
        help="Maximum ranked signals to print in table mode",
    )
    args = parser.parse_args()

    feeds = load_processed_game_feeds(args.feeds_path)
    summary = summarize_book_c_dataset(feeds)

    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"feeds_path={args.feeds_path}")
    print(
        f"games={summary['total_games']} "
        f"supported={len(summary['supported_event_classes'])} "
        f"unsupported={len(summary['unsupported_event_classes'])} "
        f"top_recommendation={summary['top_recommendation']}"
    )
    for line in _table_rows(summary, args.limit):
        print(line)
    if summary.get("unsupported_event_classes"):
        print("")
        print("data_gaps:")
        for row in summary["unsupported_event_classes"]:
            print(f"- {row['event_class']}: {row['reason']}")
    if summary.get("clutch_comeback_buckets"):
        print("")
        print("clutch_comeback_buckets:")
        for line in _clutch_bucket_rows(summary):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
