import json
from pathlib import Path
from unittest.mock import patch

from event_ledger import EventLedger
from pnl_attribution import PnLAttributor


def _trade(order_id="order-1", source_path=None):
    return {
        "timestamp": "2026-03-14T10:00:00+00:00",
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


def _decision():
    return {
        "timestamp": "2026-03-14T10:00:01+00:00",
        "ticker": "KXHIGHHOU-26MAR14-T75",
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
