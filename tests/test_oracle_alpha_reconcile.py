"""Tests for Oracle alpha-ledger reconciliation."""

from __future__ import annotations

import json
import sys

import apps.oracle_alpha_reconcile as oracle_alpha_reconcile
from domain.oracle.alpha_capture import OracleAlphaCapture
from event_ledger import EventLedger


def _seed_source_ledger(path):
    ledger = EventLedger(path)
    trade = {
        "ticker": "KXNBA-18MAR26-LALHOU-LAL",
        "action": "buy",
        "side": "yes",
        "count": 2,
        "price_cents": 61,
        "order_id": "order-1",
        "timestamp": "2026-03-19T12:00:02+00:00",
        "source_bot": "oracle",
        "status": "filled",
    }
    ledger.record_order_submitted(trade, source_path=path)
    ledger.record_fill(
        {
            "order_id": "order-1",
            "ticker": trade["ticker"],
            "timestamp": "2026-03-19T12:00:08+00:00",
            "fill_price_cents": 62,
            "fill_count": 2,
            "source_bot": "oracle",
        },
        source_path=path,
    )
    ledger.record_settlement(
        {
            "order_id": "order-1",
            "ticker": trade["ticker"],
            "timestamp": "2026-03-19T19:00:00+00:00",
            "fill_price_cents": 62,
            "fill_count": 2,
            "settlement_result": "win",
            "settlement_revenue_cents": 200,
            "fee_cents": 5,
            "close_price_cents": 100,
            "source_bot": "oracle",
        },
        source_path=path,
    )
    return path


def _seed_alpha_signal(path):
    capture = OracleAlphaCapture(path=path)
    capture.record_signal(
        hypothesis_id="H1_real_to_kalshi_latency",
        signal_timestamp_utc="2026-03-19T12:00:00+00:00",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        model_prob=0.63,
        market_prob=0.54,
        entry_price=0.61,
        book="C",
        game_id="23454",
        player_id=77,
        stat="points",
        team="LAL",
        signal_id="signal-1",
        extra={"signal_type": "clutch_comeback", "order_id": "order-1"},
    )
    return capture


def test_reconcile_alpha_ledger_imports_from_event_ledger_and_is_idempotent(tmp_path):
    source_path = _seed_source_ledger(tmp_path / "event-ledger.sqlite3")
    capture = _seed_alpha_signal(tmp_path / "oracle-alpha.sqlite3")

    summary_1 = oracle_alpha_reconcile.reconcile_alpha_ledger(
        source_ledger_path=source_path,
        alpha_ledger_path=capture.path,
        dry_run=False,
    )
    summary_2 = oracle_alpha_reconcile.reconcile_alpha_ledger(
        source_ledger_path=source_path,
        alpha_ledger_path=capture.path,
        dry_run=False,
    )

    orders = capture.load_order_rows()
    fills = capture.load_fill_rows()
    settlements = capture.load_settlement_rows()

    assert summary_1["source_trade_rows"] == 1
    assert summary_1["order_rows"] == 1
    assert summary_1["fill_rows"] == 1
    assert summary_1["settlement_rows"] == 1
    assert summary_1["linked_signal_rows"] == 1
    assert summary_2["order_rows"] == 1
    assert summary_2["fill_rows"] == 1
    assert summary_2["settlement_rows"] == 1
    assert len(orders) == 1
    assert len(fills) == 1
    assert len(settlements) == 1
    assert orders[0]["signal_id"] == "signal-1"
    assert orders[0]["status"] == "filled"
    assert fills[0]["fill_price_cents"] == 62
    assert settlements[0]["settlement_result"] == "win"
    assert settlements[0]["close_price_cents"] == 100
    assert settlements[0]["fee_cents"] == 5


def test_reconcile_alpha_ledger_preserves_unlinked_orders_without_inventing_fills(tmp_path):
    source_path = tmp_path / "event-ledger.sqlite3"
    ledger = EventLedger(source_path)
    ledger.record_order_submitted(
        {
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "action": "buy",
            "side": "no",
            "count": 1,
            "price_cents": 43,
            "order_id": "order-2",
            "timestamp": "2026-03-19T12:05:00+00:00",
            "source_bot": "oracle",
            "status": "resting",
        },
        source_path=source_path,
    )
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    summary = oracle_alpha_reconcile.reconcile_alpha_ledger(
        source_ledger_path=source_path,
        alpha_ledger_path=capture.path,
        dry_run=False,
    )

    orders = capture.load_order_rows()
    fills = capture.load_fill_rows()

    assert summary["order_rows"] == 1
    assert summary["fill_rows"] == 0
    assert summary["unlinked_order_rows"] == 1
    assert len(orders) == 1
    assert orders[0]["signal_id"] is None
    assert orders[0]["ticker"] == "KXNBA-18MAR26-LALHOU-LAL"
    assert len(fills) == 0


def test_oracle_alpha_reconcile_main_json_output(tmp_path, monkeypatch, capsys):
    source_path = _seed_source_ledger(tmp_path / "event-ledger.sqlite3")
    capture = _seed_alpha_signal(tmp_path / "oracle-alpha.sqlite3")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-alpha-reconcile.py",
            "--source-ledger-path",
            str(source_path),
            "--alpha-ledger-path",
            str(capture.path),
            "--format",
            "json",
        ],
    )

    exit_code = oracle_alpha_reconcile.main()
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["source_trade_rows"] == 1
    assert payload["order_rows"] == 1
    assert payload["fill_rows"] == 1
    assert payload["settlement_rows"] == 1
