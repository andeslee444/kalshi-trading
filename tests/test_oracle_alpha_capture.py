"""Tests for Oracle research capture helpers."""

from event_ledger import EVENT_TYPE_MARKET_SNAPSHOT, EVENT_TYPE_SETTLEMENT, EVENT_TYPE_SOURCE_OBSERVATION
from domain.oracle.alpha_capture import OracleAlphaCapture
from domain.oracle.real_sports_client import LiveEvent


def test_alpha_capture_records_source_event(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        player_id=77,
        data={"homeScore": 101, "awayScore": 98},
        timestamp=1_763_339_200.0,
    )

    record = capture.record_source_event(event)
    rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)

    assert record["event_id"].startswith("oracle-source:GameUpdated:23454")
    assert rows[-1]["record_kind"] == "source_event"
    assert rows[-1]["game_id"] == 23454
    assert rows[-1]["player_id"] == 77


def test_alpha_capture_records_quote_snapshot(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    record = capture.record_quote_snapshot(
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        yes_bid_cents=61,
        yes_ask_cents=63,
        yes_bid_depth=9,
        yes_ask_depth=14,
        quote_timestamp="2026-03-19T12:00:03+00:00",
        source_event_id="oracle-source:test",
        source_event_type="GameUpdated",
        source_event_timestamp_utc="2026-03-19T12:00:00+00:00",
        game_id=23454,
    )
    rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)

    assert record["event_id"].startswith("oracle-quote:KXNBA-18MAR26-LALHOU-LAL")
    assert rows[-1]["record_kind"] == "quote_snapshot"
    assert rows[-1]["spread_cents"] == 2
    assert rows[-1]["midpoint_cents"] == 62
    assert rows[-1]["source_to_quote_ms"] == 3000


def test_alpha_capture_records_probe_snapshot(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    record = capture.record_probe_snapshot(
        snapshot_name="market_index_snapshot",
        payload={"mapped_games": 2, "mapped_tickers": ["KXNBA-18MAR26-LALHOU-LAL"]},
        observed_at="2026-03-19T12:00:00+00:00",
    )
    rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)

    assert record["event_id"].startswith("oracle-probe:market_index_snapshot:")
    assert rows[-1]["record_kind"] == "probe_snapshot"
    assert rows[-1]["mapped_games"] == 2


def test_alpha_capture_records_source_failure(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    record = capture.record_source_failure(
        source_name="real_sports_game_markets",
        failure_code="empty_primary_response",
        message="Primary Real Sports game_markets returned 0 NBA markets",
        extra={"real_games_count": 0},
        observed_at="2026-03-19T12:00:00+00:00",
    )
    rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)

    assert record["event_id"].startswith("oracle-source-failure:real_sports_game_markets:")
    assert rows[-1]["record_kind"] == "source_failure"
    assert rows[-1]["status"] == "failed"
    assert rows[-1]["failure_code"] == "empty_primary_response"


def test_alpha_capture_records_order_submission(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    record = capture.record_order_submission(
        order_id="order-1",
        signal_id="signal-1",
        signal_timestamp_utc="2026-03-19T12:00:00+00:00",
        order_timestamp_utc="2026-03-19T12:00:02+00:00",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        price_cents=61,
        count=2,
        book="C",
        status="resting",
        entry_type="shadow",
        expected_fill_probability=0.42,
    )
    rows = capture.ledger._fetch_event_payloads("order_submitted")

    assert record["event_id"].startswith("oracle-order:order-1:")
    assert rows[-1]["record_kind"] == "order_submission"
    assert rows[-1]["order_id"] == "order-1"
    assert rows[-1]["signal_id"] == "signal-1"
    assert rows[-1]["expected_fill_probability"] == 0.42


def test_alpha_capture_load_shadow_trade_rows_filters_by_hypothesis(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

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
        extra={"signal_type": "clutch_comeback"},
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
        status="resting",
    )
    capture.record_fill(
        order_id="order-1",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        fill_timestamp_utc="2026-03-19T12:00:06+00:00",
        fill_price_cents=62,
        fill_count=2,
        side="yes",
        signal_id=signal["signal_id"],
        hypothesis_id="H1_real_to_kalshi_latency",
    )
    capture.record_signal(
        hypothesis_id="other-hypothesis",
        signal_timestamp_utc="2026-03-19T12:01:00+00:00",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        model_prob=0.51,
        market_prob=0.5,
        entry_price=0.5,
        book="C",
        signal_id="signal-2",
    )

    rows = capture.load_shadow_trade_rows()
    filtered = capture.load_shadow_trade_rows(hypothesis_id="H1_real_to_kalshi_latency")

    assert len(rows["signals"]) == 1
    assert len(filtered["signals"]) == 1
    assert filtered["signals"][0]["signal_id"] == "signal-1"
    assert len(filtered["orders"]) == 1
    assert filtered["orders"][0]["order_id"] == "order-1"
    assert len(filtered["fills"]) == 1
    assert filtered["fills"][0]["fill_price_cents"] == 62
    assert filtered["settlements"] == []


def test_alpha_capture_records_reconciled_settlement_with_inferred_close_and_fee(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")

    record = capture.record_reconciled_settlement(
        order_id="order-1",
        market_ticker="KXNBA-18MAR26-LALHOU-LAL",
        settlement_timestamp_utc="2026-03-19T15:00:00+00:00",
        signal_id="signal-1",
        side="yes",
        fill_price_cents=62,
        fill_count=2,
        settlement_result="win",
        settlement_revenue_cents=200,
        fee_cents=5,
        close_price_cents=100,
    )
    rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SETTLEMENT)

    assert record["event_id"].startswith("oracle-reconcile-settlement:order-1:")
    assert rows[-1]["record_kind"] == "settlement"
    assert rows[-1]["settlement_result"] == "win"
    assert rows[-1]["close_price_cents"] == 100
    assert rows[-1]["fee_cents"] == 5
