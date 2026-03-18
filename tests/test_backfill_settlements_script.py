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


def test_backfill_writes_gross_settlement_payout(tmp_path, monkeypatch):
    module = _load_script()
    trade_path = tmp_path / "trades.json"
    trade_path.write_text("[]")

    trades = [
        {
            "ticker": "SETTLED-BUY",
            "action": "buy",
            "side": "yes",
            "count": 2,
            "cost_cents": 180,
            "price_cents": 90,
            "settlement_result": None,
            "timestamp": "2026-03-15T00:00:00+00:00",
        }
    ]
    written = {}
    fake_ledger = MagicMock()

    monkeypatch.setattr(module, "TRADE_FILES", [trade_path])
    monkeypatch.setattr(module, "load_trades", lambda path: [dict(t) for t in trades])
    monkeypatch.setattr(module, "KalshiClient", lambda: object())
    monkeypatch.setattr(module, "_query_market", lambda client, ticker: {"yes_won": True, "status": "settled"})
    monkeypatch.setattr(module, "_atomic_write_json", lambda path, rows: written.setdefault("rows", rows))
    monkeypatch.setattr(module, "get_event_ledger", lambda logger=None: fake_ledger)

    annotated = module.backfill(dry_run=False)

    assert annotated == 1
    assert written["rows"][0]["settlement_revenue_cents"] == 200


def test_summary_report_derives_pnl_from_outcome_not_revenue_field(tmp_path, monkeypatch, capsys):
    module = _load_script()
    trade_path = tmp_path / "trades.json"
    trade_path.write_text("[]")

    trades = [
        {
            "ticker": "WIN",
            "action": "buy",
            "side": "yes",
            "count": 1,
            "cost_cents": 90,
            "settlement_result": "won",
            "settlement_revenue_cents": 10,
            "source_bot": "weather",
        },
        {
            "ticker": "LOSS",
            "action": "buy",
            "side": "yes",
            "count": 1,
            "cost_cents": 30,
            "settlement_result": "lost",
            "settlement_revenue_cents": 0,
            "source_bot": "weather",
        },
    ]

    monkeypatch.setattr(module, "TRADE_FILES", [trade_path])
    monkeypatch.setattr(module, "load_trades", lambda path: [dict(t) for t in trades])

    module.summary_report()
    out = capsys.readouterr().out

    assert "Total P&L:        $-0.20" in out
