"""Integration tests for scripts/incident-workflow.py."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "incident-workflow.py"


def _load_incident_workflow():
    spec = importlib.util.spec_from_file_location("incident_workflow_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_incident_workflow_open_link_and_close_cli(tmp_path, monkeypatch, capsys):
    registry_path = tmp_path / "incident-reviews.json"
    incident_workflow = _load_incident_workflow()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "incident-workflow.py",
            "--path",
            str(registry_path),
            "open",
            "INC-17",
            "--summary",
            "Source parser failure",
            "--severity",
            "sev2",
            "--owner",
            "andes",
            "--service",
            "source-monitor",
            "--source",
            "nws",
            "--experiment-id",
            "exp-1",
        ],
    )
    incident_workflow.main()
    output = json.loads(capsys.readouterr().out)
    assert output["incident_id"] == "INC-17"
    assert output["affected_services"] == ["source-monitor"]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "incident-workflow.py",
            "--path",
            str(registry_path),
            "link",
            "INC-17",
            "--actor",
            "andes",
            "--pr-number",
            "38",
            "--config-version",
            "cfg-17",
            "--change-ref",
            "config/calibration-backup.json",
        ],
    )
    incident_workflow.main()
    output = json.loads(capsys.readouterr().out)
    assert output["pr_numbers"] == [38]
    assert output["config_versions"] == ["cfg-17"]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "incident-workflow.py",
            "--path",
            str(registry_path),
            "close",
            "INC-17",
            "--actor",
            "andes",
            "--resolution",
            "Rollback completed and parser patched",
        ],
    )
    incident_workflow.main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "closed"

    state = json.loads(registry_path.read_text())
    assert state["entries"]["INC-17"]["history"][-1]["event_type"] == "closed"
