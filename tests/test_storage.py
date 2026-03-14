import json

import event_ledger

from storage import (
    DecisionStore,
    MetricsStore,
    StateStore,
    TradeStore,
    load_trades,
    save_decision,
    save_trade,
)


def test_trade_store_appends_without_changing_file_shape(tmp_path):
    path = tmp_path / "trades.json"
    store = TradeStore(path)

    store.append({"ticker": "A"})
    store.append({"ticker": "B"})

    assert store.load() == [{"ticker": "A"}, {"ticker": "B"}]
    assert json.loads(path.read_text()) == [{"ticker": "A"}, {"ticker": "B"}]


def test_decision_store_trims_before_append_to_match_legacy_behavior(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps([1, 2, 3]))
    store = DecisionStore(path, max_records=3, trim_to=2)

    store.append(4)

    assert store.load() == [2, 3, 4]


def test_metrics_store_trims_after_append_to_match_scan_summary_behavior(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps([1, 2, 3]))
    store = MetricsStore(path, max_records=3, trim_to=2)

    store.append(4)

    assert store.load() == [3, 4]


def test_state_store_normalizes_missing_and_saved_state(tmp_path):
    path = tmp_path / "state.json"

    def normalize(data):
        payload = dict(data) if isinstance(data, dict) else {}
        payload.setdefault("counter", 0)
        return payload

    store = StateStore(path, normalizer=normalize)

    assert store.load() == {"counter": 0}

    store.save({"name": "allocator"})

    assert store.load() == {"name": "allocator", "counter": 0}


def test_load_trades_returns_empty_list_for_corrupt_json(tmp_path):
    path = tmp_path / "trades.json"
    path.write_text("{not-json")

    assert load_trades(path) == []


def test_save_trade_appends_and_dual_writes_to_ledger(tmp_path, monkeypatch):
    path = tmp_path / "trades.json"
    ledger_calls = []

    class _FakeLedger:
        def record_order_submitted(self, trade, source_path=None):
            ledger_calls.append((trade, source_path))

    monkeypatch.setattr(event_ledger, "get_event_ledger", lambda logger=None: _FakeLedger())

    save_trade(path, {"ticker": "ABC", "price": 42})

    assert json.loads(path.read_text()) == [{"ticker": "ABC", "price": 42}]
    assert ledger_calls == [({"ticker": "ABC", "price": 42}, path)]


def test_save_decision_appends_and_dual_writes_to_ledger(tmp_path, monkeypatch):
    path = tmp_path / "decisions.json"
    ledger_calls = []

    class _FakeLedger:
        def record_trade_decision(self, decision, source_path=None):
            ledger_calls.append((decision, source_path))

    monkeypatch.setattr(event_ledger, "get_event_ledger", lambda logger=None: _FakeLedger())

    save_decision(path, {"ticker": "ABC", "action": "buy"})

    assert json.loads(path.read_text()) == [{"ticker": "ABC", "action": "buy"}]
    assert ledger_calls == [({"ticker": "ABC", "action": "buy"}, path)]
