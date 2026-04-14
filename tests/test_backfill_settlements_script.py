import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script():
    path = PROJECT_DIR / "scripts" / "backfill-settlements.py"
    spec = importlib.util.spec_from_file_location("backfill_settlements", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_local_settlements_to_ledger_only_writes_settled_buys(tmp_path, monkeypatch):
    module = _load_script()
    trade_path = tmp_path / "trades.json"
    trade_path.write_text("[]")

    trades = [
        {
            "ticker": "SETTLED-BUY",
            "action": "buy",
            "side": "yes",
            "settlement_result": "won",
            "timestamp": "2026-03-15T00:00:00+00:00",
        },
        {
            "ticker": "UNSETTLED-BUY",
            "action": "buy",
            "side": "yes",
            "settlement_result": None,
            "timestamp": "2026-03-15T00:01:00+00:00",
        },
        {
            "ticker": "SETTLED-SELL",
            "action": "sell",
            "side": "yes",
            "settlement_result": "lost",
            "timestamp": "2026-03-15T00:02:00+00:00",
        },
    ]
    fake_ledger = MagicMock()

    monkeypatch.setattr(module, "TRADE_FILES", [trade_path])
    monkeypatch.setattr(module, "load_trades", lambda path: list(trades))
    monkeypatch.setattr(module, "get_event_ledger", lambda logger=None: fake_ledger)

    synced = module.sync_local_settlements_to_ledger()

    assert synced == 1
    fake_ledger.record_settlement.assert_called_once_with(trades[0], source_path=trade_path)
