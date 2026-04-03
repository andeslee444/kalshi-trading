import importlib.util
import json
from pathlib import Path

from event_ledger import EVENT_TYPE_TRADE_DECISION, EventLedger


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    path = PROJECT_DIR / "scripts" / "ledger-retention.py"
    spec = importlib.util.spec_from_file_location("ledger_retention", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ledger_retention_script_dry_run_and_save_summary(tmp_path, monkeypatch):
    module = _load_module()
    ledger_path = tmp_path / "ledger.sqlite3"
    archive_root = tmp_path / "archive"
    summary_path = tmp_path / "summary.json"
    decision_path = tmp_path / "decisions.json"

    ledger = EventLedger(ledger_path, archive_root=archive_root)
    ledger.record_trade_decision(
        {
            "timestamp": "2026-03-01T10:00:00+00:00",
            "ticker": "KXHIGHHOU-26MAR01-T70",
            "side": "yes",
            "action": "skipped",
            "reason": "edge",
            "source_bot": "weather",
        },
        source_path=decision_path,
    )

    monkeypatch.setattr(module, "DEFAULT_SUMMARY_PATH", summary_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ledger-retention.py",
            "--ledger-path",
            str(ledger_path),
            "--archive-root",
            str(archive_root),
            "--event-type",
            EVENT_TYPE_TRADE_DECISION,
            "--dry-run",
            "--save",
            "--json",
        ],
    )

    module.main()

    saved = json.loads(summary_path.read_text())
    assert saved["dry_run"] is True
    assert saved["results"][0]["event_type"] == EVENT_TYPE_TRADE_DECISION
    assert saved["results"][0]["candidate_rows"] == 1


def test_ledger_retention_script_blocks_compact_when_live_pids_exist(tmp_path, monkeypatch):
    module = _load_module()
    ledger_path = tmp_path / "ledger.sqlite3"
    archive_root = tmp_path / "archive"
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    (pid_dir / "weather.pid").write_text("12345")

    monkeypatch.setattr(module, "_pid_is_live", lambda pid: True)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ledger-retention.py",
            "--ledger-path",
            str(ledger_path),
            "--archive-root",
            str(archive_root),
            "--compact",
        ],
    )

    try:
        module.main()
    except SystemExit as exc:
        assert "Refusing to compact" in str(exc)
    else:
        raise AssertionError("expected SystemExit when compact is blocked by live pids")
