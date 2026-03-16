"""Direct tests for the research.source_catalog module."""

import datetime
import json

from event_ledger import EventLedger
from research.source_catalog import SourceCatalog, _load_health_state_safe


def test_load_health_state_safe_handles_missing_and_valid_files(tmp_path):
    path = tmp_path / "health-state.json"
    path.write_text(json.dumps({"sources": {"nws": {"error_count": 1}}, "bots": {}}))

    assert _load_health_state_safe(tmp_path / "missing.json")["sources"] == {}
    assert _load_health_state_safe(path)["sources"]["nws"]["error_count"] == 1


def test_source_catalog_builds_entries_from_health_state_and_observations():
    fixed_now = datetime.datetime(2026, 3, 16, 15, 0, tzinfo=datetime.timezone.utc)
    catalog = SourceCatalog(
        bot_source_map={"weather": ["nws", "open-meteo-batch"], "source-monitor": ["nws"]},
        utc_now_func=lambda: fixed_now,
    )
    catalog.load(
        health_state={
            "sources": {
                "nws": {
                    "last_success": "2026-03-16T14:50:00+00:00",
                    "last_error": "2026-03-16T14:30:00+00:00",
                    "last_error_message": "missing observation",
                    "error_count": 2,
                },
                "open-meteo-batch": {
                    "last_success": "2026-03-16T14:40:00+00:00",
                    "last_error": None,
                    "last_error_message": None,
                    "error_count": 0,
                },
            },
            "bots": {},
        },
        source_observations=[
            {"source_name": "nws", "observed_at": "2026-03-16T14:55:00+00:00"},
            {"source_name": "nws", "observed_at": "2026-03-16T12:00:00+00:00"},
            {"source_name": "archive-feed", "observed_at": "2026-03-15T15:00:00+00:00"},
        ],
    )

    report = catalog.build()
    entries = {entry["source_name"]: entry for entry in report["sources"]}

    assert report["summary"]["total_sources"] == 3
    assert entries["nws"]["owner_bots"] == ["source-monitor", "weather"]
    assert entries["nws"]["status"] == "warning"
    assert entries["nws"]["last_error_message"] == "missing observation"
    assert entries["nws"]["last_observation"] == "2026-03-16T14:55:00+00:00"
    assert entries["nws"]["freshness_minutes"] == 5.0
    assert entries["nws"]["observation_count_24h"] == 2
    assert entries["archive-feed"]["status"] == "ok"
    assert entries["archive-feed"]["owner_bots"] == []


def test_source_catalog_summary_report_includes_error_message():
    fixed_now = datetime.datetime(2026, 3, 16, 15, 0, tzinfo=datetime.timezone.utc)
    catalog = SourceCatalog(utc_now_func=lambda: fixed_now)
    catalog.load(
        health_state={
            "sources": {
                "hdd": {
                    "last_success": None,
                    "last_error": "2026-03-16T14:00:00+00:00",
                    "last_error_message": "parser returned no rows",
                    "error_count": 1,
                }
            },
            "bots": {},
        },
        source_observations=[],
    )

    summary = catalog.summary_report()

    assert "SOURCE SCORECARD" in summary
    assert "hdd" in summary
    assert "parser returned no rows" in summary


def test_source_catalog_can_load_observations_from_ledger(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite3"
    EventLedger(ledger_path).record_source_observation(
        "nws",
        "nws_snapshot.json",
        "abc123",
        observed_at="2026-03-16T14:59:00+00:00",
        extra={"ext": "json"},
    )

    fixed_now = datetime.datetime(2026, 3, 16, 15, 0, tzinfo=datetime.timezone.utc)
    catalog = SourceCatalog(
        health_state_path=tmp_path / "health-state.json",
        ledger_path=ledger_path,
        bot_source_map={"weather": ["nws"]},
        utc_now_func=lambda: fixed_now,
    )
    catalog.load()

    report = catalog.build()
    entry = next(item for item in report["sources"] if item["source_name"] == "nws")

    assert entry["observation_count_24h"] == 1
    assert entry["last_observation"] == "2026-03-16T14:59:00+00:00"
