"""Tests for the Oracle latency report CLI."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import apps.oracle_latency_report as oracle_latency_report
from domain.oracle.alpha_capture import OracleAlphaCapture


def _seed_alpha_ledger(tmp_path, *, with_source_quote=False, with_settlement=False):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    if with_source_quote:
        capture.record_source_event(
            SimpleNamespace(
                event_type="GameUpdated",
                game_id="23454",
                player_id=None,
                timestamp="2026-03-19T11:59:58+00:00",
                data={"period": "Q4", "clock": "1:20", "homeScore": 101, "awayScore": 99},
            ),
            observed_at="2026-03-19T12:00:00+00:00",
            extra={"derived_event_class": "clutch_entry"},
        )
        capture.record_quote_snapshot(
            ticker="KXNBA-18MAR26-LALHOU-LAL",
            yes_bid_cents=60,
            yes_ask_cents=62,
            yes_bid_depth=8,
            yes_ask_depth=7,
            quote_timestamp="2026-03-19T12:00:01+00:00",
            source_event_id="oracle-source:GameUpdated:23454:na:placeholder",
            source_event_type="GameUpdated",
            source_event_timestamp_utc="2026-03-19T12:00:00+00:00",
            game_id="23454",
            extra={"derived_event_class": "clutch_entry", "horizon_seconds": 0.0},
        )
    signal = capture.record_signal(
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
        extra={"signal_type": "clutch_comeback", "expected_fill_probability": 0.42},
    )
    capture.record_order_submission(
        order_id="order-1",
        signal_id=signal["signal_id"],
        signal_timestamp_utc="2026-03-19T12:00:00+00:00",
        order_timestamp_utc="2026-03-19T12:00:02+00:00",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        price_cents=61,
        count=2,
        book="C",
        status="filled",
        entry_type="shadow",
        hypothesis_id="H1_real_to_kalshi_latency",
    )
    capture.record_fill(
        order_id="order-1",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        fill_timestamp_utc="2026-03-19T12:00:08+00:00",
        fill_price_cents=62,
        fill_count=2,
        side="yes",
        signal_id=signal["signal_id"],
        hypothesis_id="H1_real_to_kalshi_latency",
    )
    if with_settlement:
        capture.record_reconciled_settlement(
            order_id="order-1",
            market_ticker="KXNBA-18MAR26-LALHOU-LAL",
            settlement_timestamp_utc="2026-03-19T15:00:00+00:00",
            signal_id=signal["signal_id"],
            side="yes",
            fill_price_cents=62,
            fill_count=2,
            settlement_result="win",
            settlement_revenue_cents=200,
            fee_cents=5,
            close_price_cents=100,
        )
    return capture.path


def test_oracle_latency_report_json_includes_shadow_execution_summary(tmp_path, monkeypatch, capsys):
    ledger_path = _seed_alpha_ledger(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-latency-report.py",
            "--ledger-path",
            str(ledger_path),
            "--format",
            "json",
        ],
    )

    exit_code = oracle_latency_report.main()
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["shadow_execution_summary"]["signal_rows"] == 1
    assert payload["shadow_execution_summary"]["order_rows"] == 1
    assert payload["shadow_execution_summary"]["fill_rows"] == 1
    assert payload["shadow_execution_summary"]["order_fill_rate"] == 1.0
    assert payload["shadow_execution_summary"]["median_time_to_fill_seconds"] == 6.0
    assert payload["execution_summary"]["pass_fail_status"] == "insufficient_close_data"
    assert payload["execution_summary"]["no_settlement_yet"] is True


def test_oracle_latency_report_table_includes_shadow_summary_line(tmp_path, monkeypatch, capsys):
    ledger_path = _seed_alpha_ledger(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-latency-report.py",
            "--ledger-path",
            str(ledger_path),
            "--limit",
            "3",
        ],
    )

    exit_code = oracle_latency_report.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "shadow_signal_rows=1" in output
    assert "order_fill_rate=1.000" in output
    assert "median_time_to_fill=6s" in output
    assert "pass_fail=insufficient_close_data" in output


def test_oracle_latency_report_json_includes_daily_summary_and_h1_decision(tmp_path, monkeypatch, capsys):
    ledger_path = _seed_alpha_ledger(tmp_path, with_source_quote=True, with_settlement=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-latency-report.py",
            "--ledger-path",
            str(ledger_path),
            "--format",
            "json",
        ],
    )

    exit_code = oracle_latency_report.main()
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["daily_h1_summary"]["days"] == 1
    assert payload["daily_h1_summary"]["rows"][0]["date"] == "2026-03-19"
    assert payload["daily_h1_summary"]["rows"][0]["source_events"] == 1
    assert payload["daily_h1_summary"]["rows"][0]["quote_snapshots"] == 1
    assert payload["daily_h1_summary"]["rows"][0]["settlements"] == 1
    assert payload["h1_decision"]["status"] == "fail"
    assert payload["h1_decision"]["criteria"]["fill_rate_gte_20pct"] is True


def test_oracle_latency_report_daily_summary_only_json(tmp_path, monkeypatch, capsys):
    ledger_path = _seed_alpha_ledger(tmp_path, with_source_quote=True, with_settlement=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-latency-report.py",
            "--ledger-path",
            str(ledger_path),
            "--format",
            "json",
            "--daily-summary-only",
        ],
    )

    exit_code = oracle_latency_report.main()
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["days"] == 1
    assert payload["rows"][0]["date"] == "2026-03-19"
    assert "positive_clv_day_share" in payload
