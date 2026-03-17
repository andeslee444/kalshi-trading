"""Integration tests for scripts/daily-attribution.py."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "daily-attribution.py"


def _load_daily_attribution():
    spec = importlib.util.spec_from_file_location("daily_attribution_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_daily_attribution_save_writes_canonical_artifact_and_ledger_event(tmp_path, monkeypatch):
    daily_attribution = _load_daily_attribution()

    class FakeAttributor:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def load_trades(self):
            return None

        def full_report(self):
            return {
                "by_bot": {"weather": {"pnl_cents": 80, "trades": 2, "wins": 1, "losses": 1, "win_rate": 0.5}},
                "by_edge_bucket": {},
                "by_regime": {},
                "by_sizing": {},
                "by_market_type": {},
                "summary": {
                    "total_trades_settled": 2,
                    "total_trades_all": 3,
                    "total_pnl_cents": 80,
                },
            }

    ledger_path = tmp_path / "event-ledger.sqlite3"

    monkeypatch.setattr(daily_attribution, "PnLAttributor", FakeAttributor)
    monkeypatch.setattr(daily_attribution, "DATA_DIR", tmp_path)
    monkeypatch.setattr(daily_attribution, "TRADE_FILES", [])
    monkeypatch.setattr(daily_attribution, "DEFAULT_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(daily_attribution, "print_report", lambda report: None)
    monkeypatch.setattr(daily_attribution, "check_edge_decay", lambda notify=False: [])
    monkeypatch.setattr(sys, "argv", ["daily-attribution.py", "--save"])

    daily_attribution.main()

    out_path = tmp_path / "attribution-report.json"
    saved = json.loads(out_path.read_text())

    assert saved["artifact_type"] == "trade_attribution"
    assert saved["report_name"] == "daily_attribution"
    assert saved["summary"]["total_trades_settled"] == 2

    with sqlite3.connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT source_artifact, source_path, payload_json FROM events WHERE event_type = ?",
            ("post_trade_attribution",),
        ).fetchone()

    assert row is not None
    assert row[0] == "trade_attribution"
    assert row[1] == str(out_path)
    payload = json.loads(row[2])
    assert payload["artifact_type"] == "trade_attribution"
