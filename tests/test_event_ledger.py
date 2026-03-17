import json
from pathlib import Path
from unittest.mock import patch

from event_ledger import EventLedger
from pnl_attribution import PnLAttributor


def _trade(order_id="order-1", source_path=None, timestamp="2026-03-14T10:00:00+00:00"):
    return {
        "timestamp": timestamp,
        "ticker": "KXHIGHHOU-26MAR14-T75",
        "action": "buy",
        "side": "yes",
        "price_cents": 50,
        "count": 2,
        "cost_cents": 100,
        "reasoning": "edge",
        "order_id": order_id,
        "status": "resting",
        "source_bot": "weather",
        "best_bid": 48,
        "best_ask": 52,
        "spread": 4,
    }


def _decision(timestamp="2026-03-14T10:00:01+00:00", ticker="KXHIGHHOU-26MAR14-T75"):
    return {
        "timestamp": timestamp,
        "ticker": ticker,
        "side": "yes",
        "action": "skipped",
        "reason": "allocator denied",
        "source_bot": "weather",
    }


def test_event_ledger_reconstructs_settled_trade_view(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    trade = _trade()

    ledger.record_order_submitted(trade, source_path=trade_path)
    ledger.record_fill(
        {
            "timestamp": trade["timestamp"],
            "ticker": trade["ticker"],
            "order_id": trade["order_id"],
            "fill_price_cents": 49,
            "fill_count": 2,
            "source_bot": trade["source_bot"],
        },
        source_path=trade_path,
    )
    settled = dict(trade)
    settled["settlement_result"] = "won"
    settled["settlement_revenue_cents"] = 200
    settled["realized_edge"] = 0.51
    ledger.record_settlement(settled, source_path=trade_path)

    rows = ledger.get_trade_records(trade_path)

    assert len(rows) == 1
    assert rows[0]["fill_price_cents"] == 49
    assert rows[0]["settlement_result"] == "won"
    assert rows[0]["settlement_revenue_cents"] == 200
    assert rows[0]["best_ask"] == 52


def test_event_ledger_parity_report_matches_legacy_files(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    decision_path = tmp_path / "decisions.json"
    verification_path = tmp_path / "weather-verification.json"

    trade = _trade()
    decision = _decision()
    verified = {
        "city": "HOU",
        "date": "2026-03-10",
        "models": {"om": 75.0},
        "actual_high": 76.0,
        "errors": {"om": -1.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-08T12:00:00+00:00",
        "verified_at": "2026-03-11T12:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }

    trade_path.write_text(json.dumps([trade]))
    decision_path.write_text(json.dumps([decision]))
    verification_path.write_text(json.dumps({"pending": [], "verified": [verified], "stats": {"total_verified": 1}}))

    ledger.record_order_submitted(trade, source_path=trade_path)
    ledger.record_trade_decision(decision, source_path=decision_path)
    ledger.record_verification_result(verified, source_path=verification_path, category="weather_verification")

    report = ledger.build_parity_report(
        trade_specs=[{"path": trade_path}],
        decision_specs=[{"path": decision_path}],
        verification_specs=[{"path": verification_path, "category": "weather_verification"}],
    )

    assert report["trade_logs"][0]["count_match"] is True
    assert report["trade_logs"][0]["hash_match"] is True
    assert report["decision_logs"][0]["hash_match"] is True
    assert report["verification"][0]["verified_hash_match"] is True


def test_event_ledger_trade_parity_uses_overlap_window(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    old_trade = _trade(order_id="order-old", timestamp="2026-03-10T10:00:00+00:00")
    new_trade = _trade(order_id="order-new", timestamp="2026-03-14T10:00:00+00:00")

    trade_path.write_text(json.dumps([old_trade, new_trade]))
    ledger.record_order_submitted(new_trade, source_path=trade_path)

    report = ledger.build_parity_report(trade_specs=[{"path": trade_path}])
    row = report["trade_logs"][0]

    assert row["comparison_mode"] == "overlap_window"
    assert row["comparison_status"] == "matched"
    assert row["legacy_total_count"] == 2
    assert row["ledger_total_count"] == 1
    assert row["legacy_count"] == 1
    assert row["ledger_count"] == 1
    assert row["hash_match"] is True


def test_event_ledger_trade_parity_reports_ledger_superset(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    trade_a = _trade(order_id="order-a", timestamp="2026-03-14T10:00:00+00:00")
    trade_b = _trade(order_id="order-b", timestamp="2026-03-14T10:00:01+00:00")
    trade_c = _trade(order_id="order-c", timestamp="2026-03-14T10:00:02+00:00")

    trade_path.write_text(json.dumps([trade_a, trade_c]))
    ledger.record_order_submitted(trade_a, source_path=trade_path)
    ledger.record_order_submitted(trade_b, source_path=trade_path)
    ledger.record_order_submitted(trade_c, source_path=trade_path)

    report = ledger.build_parity_report(trade_specs=[{"path": trade_path}])
    row = report["trade_logs"][0]

    assert row["comparison_mode"] == "ordered_subsequence"
    assert row["comparison_status"] == "ledger_superset"
    assert row["legacy_count"] == 2
    assert row["ledger_count"] == 3
    assert row["ledger_extra_count"] == 1
    assert row["legacy_is_ordered_subsequence"] is True


def test_event_ledger_trade_parity_ignores_ledger_only_fill_count(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    trade = _trade()
    trade["fill_price_cents"] = trade["price_cents"]

    trade_path.write_text(json.dumps([trade]))
    ledger.record_order_submitted(trade, source_path=trade_path)
    ledger.record_fill(
        {
            "timestamp": trade["timestamp"],
            "ticker": trade["ticker"],
            "order_id": trade["order_id"],
            "fill_price_cents": trade["price_cents"],
            "fill_count": trade["count"],
            "source_bot": trade["source_bot"],
        },
        source_path=trade_path,
    )

    report = ledger.build_parity_report(trade_specs=[{"path": trade_path}])
    row = report["trade_logs"][0]

    assert row["comparison_status"] == "matched"
    assert row["hash_match"] is True


def test_event_ledger_trade_parity_treats_zero_fill_price_as_missing(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    trade_path = tmp_path / "trades.json"
    trade = _trade()
    trade["fill_price_cents"] = None

    trade_path.write_text(json.dumps([trade]))
    ledger.record_order_submitted(trade, source_path=trade_path)
    ledger.record_fill(
        {
            "timestamp": trade["timestamp"],
            "ticker": trade["ticker"],
            "order_id": trade["order_id"],
            "fill_price_cents": 0,
            "fill_count": trade["count"],
            "source_bot": trade["source_bot"],
        },
        source_path=trade_path,
    )

    report = ledger.build_parity_report(trade_specs=[{"path": trade_path}])
    row = report["trade_logs"][0]

    assert row["comparison_status"] == "matched"
    assert row["hash_match"] is True


def test_event_ledger_decision_parity_uses_latest_suffix(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    decision_path = tmp_path / "decisions.json"
    old_decision = _decision(timestamp="2026-03-14T10:00:01+00:00", ticker="OLD")
    mid_decision = _decision(timestamp="2026-03-14T10:00:02+00:00", ticker="MID")
    new_decision = _decision(timestamp="2026-03-14T10:00:03+00:00", ticker="NEW")

    decision_path.write_text(json.dumps([mid_decision, new_decision]))
    ledger.record_trade_decision(old_decision, source_path=decision_path)
    ledger.record_trade_decision(mid_decision, source_path=decision_path)
    ledger.record_trade_decision(new_decision, source_path=decision_path)

    report = ledger.build_parity_report(decision_specs=[{"path": decision_path}])
    row = report["decision_logs"][0]

    assert row["comparison_mode"] == "latest_suffix"
    assert row["comparison_status"] == "matched"
    assert row["legacy_total_count"] == 2
    assert row["ledger_total_count"] == 3
    assert row["legacy_count"] == 2
    assert row["ledger_count"] == 2
    assert row["hash_match"] is True


def test_event_ledger_decision_parity_reports_ledger_superset(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    decision_path = tmp_path / "decisions.json"
    decision_a = _decision(timestamp="2026-03-14T10:00:01+00:00", ticker="AAA")
    decision_b = _decision(timestamp="2026-03-14T10:00:02+00:00", ticker="BBB")
    decision_c = _decision(timestamp="2026-03-14T10:00:03+00:00", ticker="CCC")

    decision_path.write_text(json.dumps([decision_a, decision_c]))
    ledger.record_trade_decision(decision_a, source_path=decision_path)
    ledger.record_trade_decision(decision_b, source_path=decision_path)
    ledger.record_trade_decision(decision_c, source_path=decision_path)

    report = ledger.build_parity_report(decision_specs=[{"path": decision_path}])
    row = report["decision_logs"][0]

    assert row["comparison_mode"] == "ordered_subsequence"
    assert row["comparison_status"] == "ledger_superset"
    assert row["legacy_count"] == 2
    assert row["ledger_count"] == 3
    assert row["ledger_extra_count"] == 1
    assert row["legacy_is_ordered_subsequence"] is True


def test_event_ledger_decision_parity_reports_no_overlap_for_pre_coverage_legacy_file(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    stale_decision_path = tmp_path / "stale-decisions.json"
    active_decision_path = tmp_path / "active-decisions.json"
    stale_decision = _decision(timestamp="2026-03-10T10:00:01+00:00", ticker="STALE")
    active_decision = _decision(timestamp="2026-03-14T10:00:01+00:00", ticker="ACTIVE")

    stale_decision_path.write_text(json.dumps([stale_decision]))
    ledger.record_trade_decision(active_decision, source_path=active_decision_path)

    report = ledger.build_parity_report(decision_specs=[{"path": stale_decision_path}])
    row = report["decision_logs"][0]

    assert row["comparison_mode"] == "latest_suffix"
    assert row["comparison_status"] == "no_overlap"
    assert row["legacy_total_count"] == 1
    assert row["ledger_total_count"] == 0
    assert row["legacy_count"] == 0
    assert row["ledger_count"] == 0
    assert row["hash_match"] is True


def test_event_ledger_decision_parity_keeps_post_coverage_gap_as_mismatch(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    missing_decision_path = tmp_path / "missing-decisions.json"
    active_decision_path = tmp_path / "active-decisions.json"
    missing_decision = _decision(timestamp="2026-03-14T10:00:02+00:00", ticker="MISSING")
    active_decision = _decision(timestamp="2026-03-14T10:00:01+00:00", ticker="ACTIVE")

    missing_decision_path.write_text(json.dumps([missing_decision]))
    ledger.record_trade_decision(active_decision, source_path=active_decision_path)

    report = ledger.build_parity_report(decision_specs=[{"path": missing_decision_path}])
    row = report["decision_logs"][0]

    assert row["comparison_mode"] == "latest_suffix"
    assert row["comparison_status"] == "mismatch"
    assert row["legacy_total_count"] == 1
    assert row["ledger_total_count"] == 0
    assert row["legacy_count"] == 1
    assert row["ledger_count"] == 0
    assert row["hash_match"] is False


def test_event_ledger_connection_context_closes_sqlite_handle(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")

    class FakeConn:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = FakeConn()
    with patch.object(ledger, "_connect", return_value=fake):
        with ledger._connection() as conn:
            assert conn is fake
            assert fake.closed is False

    assert fake.closed is True


def test_event_ledger_verification_parity_uses_recorded_overlap_and_ignores_verified_at(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    verification_path = tmp_path / "weather-verification.json"
    old_verified = {
        "city": "HOU",
        "date": "2026-03-09",
        "models": {"om": 75.0},
        "actual_high": 76.0,
        "errors": {"om": -1.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-08T12:00:00+00:00",
        "verified_at": "2026-03-09T12:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    new_verified = {
        "city": "AUS",
        "date": "2026-03-10",
        "models": {"om": 80.0},
        "actual_high": 79.0,
        "errors": {"om": 1.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-10T12:00:00+00:00",
        "verified_at": "2026-03-11T12:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    verification_path.write_text(json.dumps({"pending": [], "verified": [old_verified, new_verified]}))

    ledger_verified = dict(new_verified)
    ledger_verified["verified_at"] = "2026-03-17T03:00:00+00:00"
    ledger.record_verification_result(
        ledger_verified,
        source_path=verification_path,
        category="weather_verification",
    )

    report = ledger.build_parity_report(
        verification_specs=[{"path": verification_path, "category": "weather_verification"}],
    )
    row = report["verification"][0]

    assert row["comparison_mode"] == "deduped_overlap_window"
    assert row["comparison_status"] == "matched"
    assert row["legacy_verified_total_count"] == 2
    assert row["ledger_verified_total_count"] == 1
    assert row["legacy_verified_count"] == 1
    assert row["ledger_verified_count"] == 1
    assert row["verified_hash_match"] is True


def test_event_ledger_verification_parity_ignores_duplicate_ledger_rows(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    verification_path = tmp_path / "weather-verification.json"
    verified = {
        "city": "MIA",
        "date": "2026-03-10",
        "models": {"gfs": 82.0},
        "actual_high": 80.0,
        "errors": {"gfs": 2.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-10T12:00:00+00:00",
        "verified_at": "2026-03-11T12:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    verification_path.write_text(json.dumps({"pending": [], "verified": [verified]}))

    ledger.record_verification_result(
        verified,
        source_path=verification_path,
        category="weather_verification",
    )
    duplicate = dict(verified)
    duplicate["verified_at"] = "2026-03-12T12:00:00+00:00"
    ledger.record_verification_result(
        duplicate,
        source_path=verification_path,
        category="weather_verification",
    )

    report = ledger.build_parity_report(
        verification_specs=[{"path": verification_path, "category": "weather_verification"}],
    )
    row = report["verification"][0]

    assert row["comparison_mode"] == "deduped_overlap_window"
    assert row["comparison_status"] == "matched"
    assert row["legacy_verified_count"] == 1
    assert row["ledger_verified_count"] == 1
    assert row["verified_hash_match"] is True


def test_event_ledger_verification_parity_reports_ledger_superset(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    verification_path = tmp_path / "weather-verification.json"
    verified = {
        "city": "MIA",
        "date": "2026-03-10",
        "models": {"gfs": 82.0},
        "actual_high": 80.0,
        "errors": {"gfs": 2.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-10T12:00:00+00:00",
        "verified_at": "2026-03-11T12:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    verified_late = {
        "city": "DEN",
        "date": "2026-03-10",
        "models": {"gfs": 75.0},
        "actual_high": 74.0,
        "errors": {"gfs": 1.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-10T14:00:00+00:00",
        "verified_at": "2026-03-11T14:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    extra = {
        "city": "ATL",
        "date": "2026-03-10",
        "models": {"gfs": 70.0},
        "actual_high": 71.0,
        "errors": {"gfs": -1.0},
        "record_kind": "snapshot",
        "recorded_at": "2026-03-10T13:00:00+00:00",
        "verified_at": "2026-03-11T13:00:00+00:00",
        "actual_source": "nws_cli",
        "category": "weather_verification",
    }
    verification_path.write_text(json.dumps({"pending": [], "verified": [verified, verified_late]}))

    ledger.record_verification_result(
        verified,
        source_path=verification_path,
        category="weather_verification",
    )
    ledger.record_verification_result(
        verified_late,
        source_path=verification_path,
        category="weather_verification",
    )
    ledger.record_verification_result(
        extra,
        source_path=verification_path,
        category="weather_verification",
    )

    report = ledger.build_parity_report(
        verification_specs=[{"path": verification_path, "category": "weather_verification"}],
    )
    row = report["verification"][0]

    assert row["comparison_mode"] == "ordered_subsequence"
    assert row["comparison_status"] == "ledger_superset"
    assert row["legacy_verified_count"] == 2
    assert row["ledger_verified_count"] == 3
    assert row["ledger_extra_count"] == 1


def test_pnl_attributor_can_read_from_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite3"
    ledger = EventLedger(ledger_path)
    trade_path = tmp_path / "trades.json"
    trade = _trade()
    settled = dict(trade)
    settled["settlement_result"] = "won"
    settled["settlement_revenue_cents"] = 200

    ledger.record_order_submitted(trade, source_path=trade_path)
    ledger.record_settlement(settled, source_path=trade_path)

    attr = PnLAttributor(
        trade_file_paths=[{"path": trade_path, "bot": "weather"}],
        ledger_path=ledger_path,
    )
    attr.load_trades()
    report = attr.full_report()

    assert report["summary"]["total_trades_settled"] == 1
    assert report["summary"]["total_pnl_cents"] == 100
    assert report["by_bot"]["weather"]["trades"] == 1


def test_dashboard_load_trades_safe_uses_ledger_when_enabled(tmp_path):
    import tests.test_dashboard_health as dashboard_tests

    dashboard = dashboard_tests._load_dashboard()
    trade_path = Path(dashboard.TRADE_FILES[0]["path"])
    ledger_rows = [{"ticker": "KXHIGHHOU-26MAR14-T75", "timestamp": "2026-03-14T10:00:00Z"}]

    class FakeLedger:
        def get_trade_records(self, path):
            return ledger_rows if Path(path) == trade_path else []

        def get_decision_records(self, path):
            return []

    with patch.dict(dashboard.os.environ, {"KALSHI_DASHBOARD_DATA_SOURCE": "ledger"}):
        with patch.object(dashboard, "_ledger", return_value=FakeLedger()):
            assert dashboard.load_trades_safe(trade_path) == ledger_rows


def test_event_ledger_returns_source_observation_records(tmp_path):
    ledger = EventLedger(tmp_path / "ledger.sqlite3")

    ledger.record_source_observation(
        "nws",
        "nws_20260316_145900.json",
        "hash-nws",
        observed_at="2026-03-16T14:59:00+00:00",
        extra={"ext": "json"},
    )
    ledger.record_source_observation(
        "hdd",
        "hdd_20260316_150000.json",
        "hash-hdd",
        observed_at="2026-03-16T15:00:00+00:00",
    )

    all_records = ledger.get_source_observation_records()
    nws_records = ledger.get_source_observation_records("nws")

    assert len(all_records) == 2
    assert len(nws_records) == 1
    assert nws_records[0]["snapshot_name"] == "nws_20260316_145900.json"
