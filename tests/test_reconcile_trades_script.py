import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script():
    path = PROJECT_DIR / "scripts" / "reconcile-trades.py"
    spec = importlib.util.spec_from_file_location("reconcile_trades", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconcile_all_dual_writes_api_fill_count_to_ledger(tmp_path, monkeypatch):
    module = _load_script()
    trade_file = tmp_path / "trades.json"
    trade_file.write_text("[]")

    trades = [
        {
            "ticker": "T",
            "action": "buy",
            "side": "yes",
            "count": 3,
            "price_cents": 40,
            "order_id": "order-1",
            "timestamp": "2026-03-17T00:00:00+00:00",
            "source_bot": "weather",
            "settlement_result": None,
        }
    ]
    saved = {}

    class FakeStore:
        def __init__(self, path, logger=None):
            self.path = path

        def load(self):
            return [dict(t) for t in trades]

        def save(self, rows):
            saved["rows"] = rows

    fake_ledger = MagicMock()

    monkeypatch.setattr(module, "TRADE_FILES", [trade_file])
    monkeypatch.setattr(module, "TradeStore", FakeStore)
    monkeypatch.setattr(module, "KalshiClient", lambda: object())
    monkeypatch.setattr(
        module,
        "_fetch_all_settlements",
        lambda client: {"T": {"yes_won": True, "revenue_cents": 300, "settled_time": "2026-03-17T01:00:00+00:00"}},
    )
    monkeypatch.setattr(
        module,
        "_fetch_all_fills",
        lambda client: {"order-1": {"fill_price_cents": 35, "fill_count": 1}},
    )
    monkeypatch.setattr(module, "ledger", fake_ledger)

    annotated = module.reconcile_all(dry_run=False)

    assert annotated == 1
    assert saved["rows"][0]["settlement_revenue_cents"] == 100
    fill_payload = fake_ledger.record_fill.call_args.args[0]
    assert fill_payload["fill_count"] == 1
