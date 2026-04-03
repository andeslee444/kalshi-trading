#!/usr/bin/env python3
"""Remove non-Oracle reconcile contamination from the Oracle alpha ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from domain.oracle.alpha_capture import DEFAULT_ORACLE_ALPHA_LEDGER_PATH
from domain.oracle.nba_ticker_utils import parse_nba_ticker


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_ARCHIVE_DIR = PROJECT_DIR / "data" / "reports" / "oracle-alpha-ledger-cleanups"
DEFAULT_SUMMARY_PATH = PROJECT_DIR / "data" / "reports" / "oracle-alpha-ledger-cleanup-latest.json"
TARGET_EVENT_TYPES = ("order_submitted", "fill", "settlement", "order_update")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _archive_path(archive_dir: Path, now: dt.datetime) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return archive_dir / f"oracle-alpha-ledger-cleanup-{stamp}.json"


def _is_oracle_ticker(ticker: str | None) -> bool:
    text = str(ticker or "").strip()
    return bool(text) and parse_nba_ticker(text) is not None


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _candidate_order_rows(conn: sqlite3.Connection, *, include_unlinked_oracle_trades: bool) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_id, event_type, event_time, order_id, payload_json, source_artifact
        FROM events
        WHERE event_type = 'order_submitted'
          AND source_artifact = 'oracle_alpha_reconcile'
        ORDER BY event_time ASC, event_id ASC
        """
    ).fetchall()

    candidates = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        signal_id = str(payload.get("signal_id") or "").strip()
        if signal_id:
            continue
        ticker = str(payload.get("ticker") or payload.get("market_ticker") or "").strip()
        if not include_unlinked_oracle_trades and _is_oracle_ticker(ticker):
            continue
        candidates.append(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "event_time": row["event_time"],
                "order_id": row["order_id"],
                "source_artifact": row["source_artifact"],
                "payload": payload,
            }
        )
    return candidates


def _events_for_order_ids(conn: sqlite3.Connection, order_ids: list[str]) -> list[dict]:
    if not order_ids:
        return []
    placeholders = ",".join("?" for _ in order_ids)
    rows = conn.execute(
        f"""
        SELECT event_id, event_type, event_time, order_id, payload_json, source_artifact, source_path, legacy_key
        FROM events
        WHERE source_artifact = 'oracle_alpha_reconcile'
          AND order_id IN ({placeholders})
          AND event_type IN ({",".join("?" for _ in TARGET_EVENT_TYPES)})
        ORDER BY event_time ASC, event_id ASC
        """,
        [*order_ids, *TARGET_EVENT_TYPES],
    ).fetchall()
    events = []
    for row in rows:
        events.append(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "event_time": row["event_time"],
                "order_id": row["order_id"],
                "source_artifact": row["source_artifact"],
                "source_path": row["source_path"],
                "legacy_key": row["legacy_key"],
                "payload": json.loads(row["payload_json"]),
            }
        )
    return events


def cleanup_alpha_ledger(
    *,
    alpha_ledger_path: Path = DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    include_unlinked_oracle_trades: bool = False,
    dry_run: bool = False,
) -> dict:
    alpha_ledger_path = Path(alpha_ledger_path)
    now = _utc_now()
    with _connect(alpha_ledger_path) as conn:
        order_rows = _candidate_order_rows(
            conn,
            include_unlinked_oracle_trades=include_unlinked_oracle_trades,
        )
        order_rows_with_order_id = [
            row for row in order_rows
            if row.get("order_id") not in (None, "")
        ]
        order_ids = [str(row["order_id"]) for row in order_rows_with_order_id]
        events = _events_for_order_ids(conn, order_ids)

        by_event_type: dict[str, int] = {}
        for event in events:
            by_event_type[event["event_type"]] = by_event_type.get(event["event_type"], 0) + 1

        summary = {
            "generated_at": now.isoformat(),
            "alpha_ledger_path": str(alpha_ledger_path),
            "archive_path": None,
            "dry_run": dry_run,
            "include_unlinked_oracle_trades": include_unlinked_oracle_trades,
            "candidate_order_rows": len(order_rows_with_order_id),
            "candidate_rows_without_order_id": len(order_rows) - len(order_rows_with_order_id),
            "candidate_order_ids": len(order_ids),
            "candidate_event_rows": len(events),
            "candidate_event_rows_by_type": by_event_type,
            "order_ids": order_ids[:50],
            "deleted_event_rows": 0,
        }
        if dry_run or not events:
            return summary

        archive_path = _archive_path(Path(archive_dir), now)
        archive_payload = {
            "summary": summary,
            "events": events,
        }
        archive_path.write_text(json.dumps(archive_payload, indent=2, sort_keys=True))
        summary["archive_path"] = str(archive_path)

        event_ids = [event["event_id"] for event in events]
        placeholders = ",".join("?" for _ in event_ids)
        conn.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids)
        conn.commit()
        summary["deleted_event_rows"] = len(event_ids)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean non-Oracle reconcile rows from the Oracle alpha ledger")
    parser.add_argument(
        "--alpha-ledger-path",
        default=str(DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        help="Path to the Oracle alpha ledger SQLite file",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Directory for JSON archives of deleted rows",
    )
    parser.add_argument(
        "--include-unlinked-oracle-trades",
        action="store_true",
        help="Also delete unlinked KXNBA* reconcile rows; default keeps them",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the cleanup summary without deleting any rows",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write the cleanup summary to data/reports/oracle-alpha-ledger-cleanup-latest.json",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format",
    )
    args = parser.parse_args()

    summary = cleanup_alpha_ledger(
        alpha_ledger_path=Path(args.alpha_ledger_path),
        archive_dir=Path(args.archive_dir),
        include_unlinked_oracle_trades=args.include_unlinked_oracle_trades,
        dry_run=args.dry_run,
    )

    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"alpha_ledger_path={summary['alpha_ledger_path']}")
        print(
            "candidate_order_rows="
            f"{summary['candidate_order_rows']} "
            f"candidate_rows_without_order_id={summary['candidate_rows_without_order_id']} "
            f"candidate_order_ids={summary['candidate_order_ids']} "
            f"candidate_event_rows={summary['candidate_event_rows']} "
            f"deleted_event_rows={summary['deleted_event_rows']} "
            f"archive_path={summary['archive_path'] or '-'} "
            f"dry_run={summary['dry_run']}"
        )
        print(f"candidate_event_rows_by_type={summary['candidate_event_rows_by_type']}")

    if args.save:
        DEFAULT_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(DEFAULT_SUMMARY_PATH) + ".tmp")
        tmp_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
        tmp_path.replace(DEFAULT_SUMMARY_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
