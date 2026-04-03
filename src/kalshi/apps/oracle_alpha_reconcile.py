#!/usr/bin/env python3
"""Backfill Oracle alpha ledger order/fill rows from local trade artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from domain.oracle.alpha_capture import DEFAULT_HYPOTHESIS_ID, DEFAULT_ORACLE_ALPHA_LEDGER_PATH, OracleAlphaCapture
from event_ledger import DEFAULT_LEDGER_PATH, EventLedger
from storage import TradeStore


def _trade_key(row: dict, *, source_path: str | None = None) -> str:
    order_id = row.get("order_id")
    if order_id not in (None, ""):
        return f"order:{order_id}"
    parts = [
        str(source_path or ""),
        str(row.get("ticker") or row.get("market_ticker") or ""),
        str(row.get("timestamp") or row.get("created_time") or ""),
        str(row.get("side") or ""),
        str(row.get("price_cents") or row.get("fill_price_cents") or ""),
        str(row.get("count") or row.get("fill_count") or ""),
    ]
    return "trade:" + "|".join(parts)


def _load_trade_rows(source_ledger_path: Path | None, trade_paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    if source_ledger_path and source_ledger_path.exists():
        for row in EventLedger(source_ledger_path).get_trade_records():
            item = dict(row)
            item["source_input_path"] = str(source_ledger_path)
            rows.append(item)
    for trade_path in trade_paths:
        if not trade_path.exists():
            continue
        for row in TradeStore(trade_path).load():
            item = dict(row)
            item["source_input_path"] = str(trade_path)
            rows.append(item)

    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _trade_key(row, source_path=row.get("source_input_path"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda row: str(row.get("timestamp") or row.get("created_time") or ""))
    return deduped


def reconcile_alpha_ledger(
    *,
    source_ledger_path: Path | None = None,
    alpha_ledger_path: Path = DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    trade_paths: list[Path] | None = None,
    hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
    linked_only: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    alpha = OracleAlphaCapture(path=alpha_ledger_path)
    signal_index = alpha.load_signal_index_by_order_id(hypothesis_id=hypothesis_id)
    trade_rows = _load_trade_rows(source_ledger_path, trade_paths or [])
    eligible_trade_rows = list(trade_rows)
    skipped_unlinked_rows = 0
    if linked_only:
        eligible_trade_rows = []
        for row in trade_rows:
            if not isinstance(row, dict):
                continue
            signal_id = row.get("signal_id")
            order_id = row.get("order_id")
            linked = signal_id not in (None, "")
            if not linked and order_id not in (None, ""):
                linked = str(order_id) in signal_index
            if linked:
                eligible_trade_rows.append(row)
            else:
                skipped_unlinked_rows += 1

    summary = {
        "source_trade_rows": len(trade_rows),
        "eligible_trade_rows": len(eligible_trade_rows),
        "order_rows": 0,
        "fill_rows": 0,
        "settlement_rows": 0,
        "linked_signal_rows": 0,
        "unlinked_order_rows": 0,
        "skipped_unlinked_rows": skipped_unlinked_rows,
        "linked_only": linked_only,
    }
    if dry_run:
        return summary

    reconcile_summary = alpha.reconcile_trade_records(
        eligible_trade_rows,
        hypothesis_id=hypothesis_id,
        source_input_path=str(source_ledger_path) if source_ledger_path else None,
        signal_index_by_order_id=signal_index,
        source_record_kind="trade_record",
    )
    summary.update(reconcile_summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle alpha-ledger reconciliation")
    parser.add_argument(
        "--source-ledger-path",
        default=str(DEFAULT_LEDGER_PATH),
        help="Path to the generic event ledger SQLite file",
    )
    parser.add_argument(
        "--alpha-ledger-path",
        default=str(DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        help="Path to the Oracle alpha ledger SQLite file",
    )
    parser.add_argument(
        "--trade-path",
        action="append",
        default=[],
        help="Additional JSON trade log paths to reconcile",
    )
    parser.add_argument(
        "--hypothesis-id",
        default=DEFAULT_HYPOTHESIS_ID,
        help="Hypothesis id to reconcile",
    )
    parser.add_argument(
        "--linked-only",
        action="store_true",
        help="Import only trade rows linked to Oracle signal ids or known Oracle order ids",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to the alpha ledger",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format",
    )
    args = parser.parse_args()

    source_ledger_path = Path(args.source_ledger_path) if args.source_ledger_path else None
    alpha_ledger_path = Path(args.alpha_ledger_path)
    trade_paths = [Path(path) for path in args.trade_path]

    summary = reconcile_alpha_ledger(
        source_ledger_path=source_ledger_path,
        alpha_ledger_path=alpha_ledger_path,
        trade_paths=trade_paths,
        hypothesis_id=args.hypothesis_id,
        linked_only=args.linked_only,
        dry_run=args.dry_run,
    )

    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"source_ledger_path={source_ledger_path}")
    print(f"alpha_ledger_path={alpha_ledger_path}")
    print(f"hypothesis_id={args.hypothesis_id}")
    print(
        "source_trade_rows="
        f"{summary['source_trade_rows']} "
        f"eligible_trade_rows={summary['eligible_trade_rows']} "
        f"order_rows={summary['order_rows']} "
        f"fill_rows={summary['fill_rows']} "
        f"settlement_rows={summary['settlement_rows']} "
        f"linked_signal_rows={summary['linked_signal_rows']} "
        f"unlinked_order_rows={summary['unlinked_order_rows']} "
        f"skipped_unlinked_rows={summary['skipped_unlinked_rows']} "
        f"linked_only={summary['linked_only']} "
        f"dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
