#!/usr/bin/env python3
"""Archive and prune hot-ledger event history per the storage retention policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from event_ledger import (  # noqa: E402
    ARCHIVABLE_EVENT_TYPES,
    DEFAULT_ARCHIVE_ROOT,
    DEFAULT_LEDGER_PATH,
    DEFAULT_RETENTION_DAYS,
    EventLedger,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SUMMARY_PATH = PROJECT_DIR / "data" / "reports" / "ledger-retention-latest.json"


def _save_summary(path: Path, summary: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(path) + ".tmp")
    tmp_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def _text_report(summary: dict) -> str:
    lines = [
        f"ledger_path={summary['ledger_path']}",
        f"archive_root={summary['archive_root']}",
        f"dry_run={summary['dry_run']}",
    ]
    for result in summary.get("results", []):
        lines.append(
            "event_type="
            f"{result['event_type']} "
            f"cutoff_time={result['cutoff_time']} "
            f"candidate_rows={result['candidate_rows']} "
            f"archived_rows={result['archived_rows']} "
            f"candidate_event_dates={len(result['candidate_event_dates'])} "
            f"archived_event_dates={len(result['archived_event_dates'])}"
        )
    if summary.get("compacted"):
        lines.append("compacted=true")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Apply the event-ledger hot/cold retention policy")
    parser.add_argument(
        "--ledger-path",
        default=str(DEFAULT_LEDGER_PATH),
        help="Path to the hot event-ledger SQLite file",
    )
    parser.add_argument(
        "--archive-root",
        default=str(DEFAULT_ARCHIVE_ROOT),
        help="Root directory for archived event partitions",
    )
    parser.add_argument(
        "--event-type",
        action="append",
        dest="event_types",
        choices=sorted(ARCHIVABLE_EVENT_TYPES),
        help="Restrict the run to one or more archive-eligible event types",
    )
    parser.add_argument(
        "--max-event-dates",
        type=int,
        default=None,
        help="Limit the number of distinct event_date partitions archived per event type",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="VACUUM the hot ledger after archive/prune completes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect archive candidates without writing archive files or pruning rows",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=f"Save the summary to {DEFAULT_SUMMARY_PATH}",
    )
    args = parser.parse_args()

    ledger = EventLedger(
        path=args.ledger_path,
        archive_root=args.archive_root,
    )
    summary = ledger.apply_retention_policy(
        event_types=args.event_types,
        dry_run=args.dry_run,
        max_event_dates=args.max_event_dates,
    )
    summary["compacted"] = False
    if args.compact and not args.dry_run:
        ledger.compact_hot_ledger()
        summary["compacted"] = True

    if args.save:
        _save_summary(DEFAULT_SUMMARY_PATH, summary)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_text_report(summary))


if __name__ == "__main__":
    main()
