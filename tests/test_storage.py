import json

from storage import DecisionStore, MetricsStore, StateStore, TradeStore


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
