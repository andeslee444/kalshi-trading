"""Tests for the Oracle operational scorecard."""

from __future__ import annotations

import datetime as dt

import apps.oracle_scorecard as oracle_scorecard

from domain.oracle.alpha_capture import OracleAlphaCapture


def test_build_scorecard_reports_stale_orders_pregame_capture_and_maker_metrics(monkeypatch, tmp_path):
    ledger_path = tmp_path / "oracle-alpha-ledger.sqlite3"
    capture = OracleAlphaCapture(path=ledger_path)
    hypothesis_id = "H1_real_to_kalshi_latency"
    now = dt.datetime(2026, 4, 2, 22, 0, tzinfo=dt.timezone.utc)

    monkeypatch.setattr(
        oracle_scorecard,
        "_load_oracle_config",
        lambda config_path=oracle_scorecard.BOTS_CONFIG_PATH: {
            "enabled": False,
            "books": {
                "A": {"enabled": False},
                "B": {"enabled": False},
                "C": {
                    "enabled": False,
                    "propSignalsEnabled": False,
                    "clutchComebackEnabled": False,
                    "passiveExecution": False,
                },
            },
        },
    )

    stale_signal = capture.record_signal(
        hypothesis_id=hypothesis_id,
        signal_timestamp_utc=(now - dt.timedelta(minutes=25)).isoformat(),
        market_ticker="KXNBAGAME-26APR02ATLGSW-ATL",
        side="yes",
        model_prob=0.58,
        market_prob=0.40,
        entry_price=0.40,
        book="C",
        game_id="ATL-GSW-20260402",
        signal_id="sig-stale",
        extra={
            "triggered": True,
            "mode": "demo",
            "entry_type": "passive",
            "signal_type": "clutch_comeback",
            "reason": "demo mode (passive)",
            "yes_bid": 39,
            "yes_ask": 40,
            "yes_bid_depth": 15,
            "yes_ask_depth": 18,
            "spread_cents": 1,
        },
    )
    capture.record_order_submission(
        order_id="ord-stale",
        signal_id=stale_signal["signal_id"],
        signal_timestamp_utc=stale_signal["signal_timestamp_utc"],
        order_timestamp_utc=(now - dt.timedelta(minutes=25)).isoformat(),
        market_ticker=stale_signal["market_ticker"],
        side="yes",
        price_cents=39,
        count=2,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        status="resting",
        entry_type="passive",
        extra={"mode": "demo", "signal_type": "clutch_comeback"},
    )

    canceled_signal = capture.record_signal(
        hypothesis_id=hypothesis_id,
        signal_timestamp_utc=(now - dt.timedelta(minutes=20)).isoformat(),
        market_ticker="KXNBAGAME-26APR02ATLGSW-GSW",
        side="no",
        model_prob=0.55,
        market_prob=0.61,
        entry_price=0.39,
        book="C",
        game_id="ATL-GSW-20260402",
        signal_id="sig-canceled",
        extra={
            "triggered": True,
            "mode": "demo",
            "entry_type": "passive",
            "signal_type": "clutch_comeback",
            "reason": "demo mode (passive)",
        },
    )
    capture.record_order_submission(
        order_id="ord-canceled",
        signal_id=canceled_signal["signal_id"],
        signal_timestamp_utc=canceled_signal["signal_timestamp_utc"],
        order_timestamp_utc=(now - dt.timedelta(minutes=20)).isoformat(),
        market_ticker=canceled_signal["market_ticker"],
        side="no",
        price_cents=61,
        count=1,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        status="resting",
        entry_type="passive",
        extra={"mode": "demo", "signal_type": "clutch_comeback"},
    )
    capture.record_order_update(
        order_id="ord-canceled",
        market_ticker=canceled_signal["market_ticker"],
        update_timestamp_utc=(now - dt.timedelta(minutes=19, seconds=55)).isoformat(),
        signal_id=canceled_signal["signal_id"],
        signal_timestamp_utc=canceled_signal["signal_timestamp_utc"],
        side="no",
        price_cents=61,
        count=1,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        status="canceled",
        entry_type="passive",
        reason="passive_timeout_cancel",
        extra={"mode": "demo", "signal_type": "clutch_comeback"},
    )

    filled_signal = capture.record_signal(
        hypothesis_id=hypothesis_id,
        signal_timestamp_utc=(now - dt.timedelta(minutes=10)).isoformat(),
        market_ticker="KXNBAGAME-26APR02ATLGSW-ATL",
        side="yes",
        model_prob=0.63,
        market_prob=0.39,
        entry_price=0.39,
        book="C",
        game_id="ATL-GSW-20260402",
        signal_id="sig-filled",
        extra={
            "triggered": True,
            "mode": "demo",
            "entry_type": "passive",
            "signal_type": "clutch_comeback",
            "reason": "demo mode (passive)",
            "yes_bid": 39,
            "yes_ask": 40,
            "yes_bid_depth": 14,
            "yes_ask_depth": 16,
            "spread_cents": 1,
        },
    )
    capture.record_order_submission(
        order_id="ord-filled",
        signal_id=filled_signal["signal_id"],
        signal_timestamp_utc=filled_signal["signal_timestamp_utc"],
        order_timestamp_utc=(now - dt.timedelta(minutes=10)).isoformat(),
        market_ticker=filled_signal["market_ticker"],
        side="yes",
        price_cents=39,
        count=2,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        status="resting",
        entry_type="passive",
        extra={"mode": "demo", "signal_type": "clutch_comeback"},
    )
    capture.record_fill(
        order_id="ord-filled",
        market_ticker=filled_signal["market_ticker"],
        fill_timestamp_utc=(now - dt.timedelta(minutes=9, seconds=30)).isoformat(),
        fill_price_cents=39,
        fill_count=2,
        side="yes",
        hypothesis_id=hypothesis_id,
        signal_id=filled_signal["signal_id"],
        extra={"mode": "demo"},
    )
    capture.record_order_update(
        order_id="ord-filled",
        market_ticker=filled_signal["market_ticker"],
        update_timestamp_utc=(now - dt.timedelta(minutes=9, seconds=30)).isoformat(),
        signal_id=filled_signal["signal_id"],
        signal_timestamp_utc=filled_signal["signal_timestamp_utc"],
        side="yes",
        price_cents=39,
        count=2,
        fill_price_cents=39,
        fill_count=2,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        status="filled",
        entry_type="passive",
        reason="passive_terminal_status",
        extra={"mode": "demo", "signal_type": "clutch_comeback"},
    )
    capture.record_reconciled_settlement(
        order_id="ord-filled",
        market_ticker=filled_signal["market_ticker"],
        settlement_timestamp_utc=(now - dt.timedelta(minutes=5)).isoformat(),
        signal_id=filled_signal["signal_id"],
        side="yes",
        fill_price_cents=39,
        fill_count=2,
        book="C",
        hypothesis_id=hypothesis_id,
        game_id="ATL-GSW-20260402",
        settlement_result="win",
        settlement_revenue_cents=200,
        fee_cents=4,
        close_price_cents=100,
    )

    observed_at = dt.datetime(2026, 4, 2, 18, 30, tzinfo=dt.timezone.utc)
    cycle_id = "oracle-h2-pregame-cycle-1"
    capture.record_probe_snapshot(
        snapshot_name="crowd_probability",
        observed_at=observed_at,
        hypothesis_id=hypothesis_id,
        payload={
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "game_id": "pregame-1",
            "game_status": "scheduled",
            "team_a": "ATL",
            "team_b": "GSW",
            "team_a_prob": 0.57,
            "team_b_prob": 0.43,
            "start_time": "2026-04-02T23:00:00+00:00",
            "hours_to_tip": 4.5,
        },
    )
    snapshot = capture.record_probe_snapshot(
        snapshot_name="market_index_snapshot",
        observed_at=observed_at,
        hypothesis_id=hypothesis_id,
        payload={
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "mapped_games": 1,
            "mapped_tickers": ["KXNBAGAME-26APR02ATLGSW-ATL"],
            "market_index": {"pregame-1": ["KXNBAGAME-26APR02ATLGSW-ATL"]},
        },
    )
    capture.record_quote_snapshot(
        ticker="KXNBAGAME-26APR02ATLGSW-ATL",
        yes_bid_cents=52,
        yes_ask_cents=54,
        yes_bid_depth=18,
        yes_ask_depth=20,
        quote_timestamp=observed_at.isoformat(),
        source_event_id=snapshot["event_id"],
        source_event_type="market_index_snapshot",
        source_event_timestamp_utc=snapshot["observed_at"],
        game_id="pregame-1",
        hypothesis_id=hypothesis_id,
        extra={
            "capture_mode": "baseline",
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "hours_to_tip": 4.5,
            "game_status": "scheduled",
            "start_time": "2026-04-02T23:00:00+00:00",
        },
    )

    scorecard = oracle_scorecard.build_scorecard(
        ledger_path=ledger_path,
        hypothesis_id=hypothesis_id,
        stale_order_minutes=15,
        now=now,
    )

    assert scorecard["study_metrics"]["signal_rows"] == 3
    assert scorecard["stale_open_orders"]["count"] == 1
    assert scorecard["stale_open_orders"]["rows"][0]["order_id"] == "ord-stale"
    assert scorecard["passive_demo_study"]["order_rows"] == 3
    assert scorecard["passive_demo_study"]["filled_orders"] == 1
    assert scorecard["passive_demo_study"]["timeout_cancel_attempts"] == 1
    assert scorecard["passive_demo_study"]["timeout_canceled_orders"] == 1
    assert scorecard["passive_demo_study"]["timeout_cancel_success_rate"] == 1.0
    assert scorecard["h2_pregame_capture"]["collector_cycles"] == 1
    assert scorecard["h2_pregame_capture"]["crowd_probability_rows"] == 1
    assert scorecard["h2_pregame_capture"]["quote_rows"] == 1

    text = oracle_scorecard.render_text(scorecard)
    assert "avg_edge=+12.00pp" in text
    assert "maker_demo:" in text
