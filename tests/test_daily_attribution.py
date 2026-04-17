"""Integration tests for scripts/daily-attribution.py."""

import importlib.util
import datetime
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
    assert "nws_source_monitor" in saved

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


def test_build_nws_source_monitor_report_groups_city_hour_price():
    daily_attribution = _load_daily_attribution()
    now = datetime.datetime(2026, 4, 17, 16, 0, tzinfo=datetime.timezone.utc)
    trades = [
        {
            "source_bot": "source-monitor",
            "source_type": "nws",
            "timestamp": "2026-04-17T14:00:00+00:00",
            "settlement_result": "won",
            "fill_count": 4,
            "cost_cents": 188,
            "city": "CHI",
            "hour_of_day": 16,
            "fill_price_cents": 47,
            "direction": "T",
        },
        {
            "source_bot": "source-monitor",
            "source_type": "nws",
            "timestamp": "2026-04-16T14:00:00+00:00",
            "settlement_result": "lost",
            "fill_count": 100,
            "cost_cents": 100,
            "city": "DAL",
            "hour_of_day": 13,
            "fill_price_cents": 1,
            "direction": "B",
        },
        {
            "source_bot": "weather",
            "source_type": "nws",
            "timestamp": "2026-04-17T14:00:00+00:00",
            "settlement_result": "won",
            "fill_count": 5,
            "cost_cents": 100,
            "city": "NY",
            "hour_of_day": 12,
            "fill_price_cents": 20,
            "direction": "T",
        },
        {
            "source_bot": "source-monitor",
            "source_type": "nws",
            "timestamp": "2026-03-01T14:00:00+00:00",
            "settlement_result": "won",
            "fill_count": 10,
            "cost_cents": 100,
            "city": "PHX",
            "hour_of_day": 10,
            "fill_price_cents": 60,
            "direction": "T",
        },
    ]

    report = daily_attribution.build_nws_source_monitor_report_from_trades(
        trades,
        now=now,
        lookback_days=30,
    )

    assert report["summary"]["total_trades"] == 2
    assert report["summary"]["total_pnl_cents"] == 112
    assert report["by_direction"]["threshold"]["pnl_cents"] == 212
    assert report["by_direction"]["bracket"]["pnl_cents"] == -100
    assert report["by_city"]["CHI"]["trades"] == 1
    assert report["by_city"]["DAL"]["trades"] == 1
    assert report["by_hour_bucket"]["15-17"]["pnl_cents"] == 212
    assert report["by_hour_bucket"]["12-14"]["pnl_cents"] == -100
    assert report["by_price_bucket"]["26-50c"]["pnl_cents"] == 212
    assert report["by_price_bucket"]["<=5c"]["pnl_cents"] == -100
