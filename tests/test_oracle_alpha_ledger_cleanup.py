"""Tests for Oracle alpha-ledger cleanup."""

from __future__ import annotations

import json

import apps.oracle_alpha_ledger_cleanup as cleanup

from domain.oracle.alpha_capture import OracleAlphaCapture


def test_cleanup_alpha_ledger_removes_non_oracle_reconcile_rows_and_archives(tmp_path):
    ledger_path = tmp_path / "oracle-alpha.sqlite3"
    capture = OracleAlphaCapture(path=ledger_path)

    capture.record_reconciled_order_submission(
        order_id="weather-order-1",
        market_ticker="KXHIGHAUS-26MAR14-T89",
        side="no",
        price_cents=45,
        count=2,
        status="resting",
        source_input_path="data/event-ledger.sqlite3",
        source_record_kind="trade_record",
    )
    capture.record_reconciled_fill(
        order_id="weather-order-1",
        market_ticker="KXHIGHAUS-26MAR14-T89",
        fill_price_cents=45,
        fill_count=2,
        side="no",
        source_input_path="data/event-ledger.sqlite3",
        source_record_kind="trade_record",
    )
    capture.record_reconciled_settlement(
        order_id="weather-order-1",
        market_ticker="KXHIGHAUS-26MAR14-T89",
        side="no",
        fill_price_cents=45,
        fill_count=2,
        settlement_result="win",
        settlement_revenue_cents=200,
        fee_cents=3,
        close_price_cents=100,
        source_input_path="data/event-ledger.sqlite3",
        source_record_kind="trade_record",
    )
    capture.record_reconciled_order_submission(
        order_id="oracle-order-1",
        market_ticker="KXNBAGAME-26MAR21GSWATL-ATL",
        side="yes",
        price_cents=58,
        count=1,
        status="resting",
        source_input_path="data/event-ledger.sqlite3",
        source_record_kind="trade_record",
    )

    summary = cleanup.cleanup_alpha_ledger(
        alpha_ledger_path=ledger_path,
        archive_dir=tmp_path / "archives",
        dry_run=False,
    )

    assert summary["candidate_order_rows"] == 1
    assert summary["candidate_event_rows"] == 3
    assert summary["deleted_event_rows"] == 3
    assert summary["archive_path"] is not None

    remaining_orders = capture.load_order_rows(hypothesis_id=None)
    remaining_fills = capture.load_fill_rows(hypothesis_id=None)
    remaining_settlements = capture.load_settlement_rows(hypothesis_id=None)
    assert [row["order_id"] for row in remaining_orders] == ["oracle-order-1"]
    assert remaining_fills == []
    assert remaining_settlements == []

    archive_payload = json.loads((tmp_path / "archives").glob("*.json").__iter__().__next__().read_text())
    assert archive_payload["summary"]["candidate_event_rows"] == 3
    assert archive_payload["events"][0]["order_id"] == "weather-order-1"


def test_cleanup_alpha_ledger_dry_run_does_not_delete(tmp_path):
    ledger_path = tmp_path / "oracle-alpha.sqlite3"
    capture = OracleAlphaCapture(path=ledger_path)
    capture.record_reconciled_order_submission(
        order_id="weather-order-2",
        market_ticker="KXHIGHCHI-26MAR14-T39",
        side="no",
        price_cents=69,
        count=1,
        status="resting",
        source_input_path="data/event-ledger.sqlite3",
        source_record_kind="trade_record",
    )

    summary = cleanup.cleanup_alpha_ledger(
        alpha_ledger_path=ledger_path,
        archive_dir=tmp_path / "archives",
        dry_run=True,
    )

    assert summary["candidate_order_rows"] == 1
    assert summary["deleted_event_rows"] == 0
    assert summary["archive_path"] is None
    assert len(capture.load_order_rows(hypothesis_id=None)) == 1
