"""Integration tests for scripts/promotion-workflow.py."""

import importlib.util
import json
import sys
from pathlib import Path

from research.registry import ExperimentRunRegistry


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "promotion-workflow.py"


def _load_promotion_workflow():
    spec = importlib.util.spec_from_file_location("promotion_workflow_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_promotion_workflow_promote_cli_updates_registry(tmp_path, monkeypatch, capsys):
    registry_path = tmp_path / "experiment-runs.json"
    registry = ExperimentRunRegistry(registry_path)
    registry.register(
        "exp-1",
        "calibration_suggestion",
        strategy_id="weather",
        source_bot="calibration-pipeline",
        status="suggested",
        promotion_stage="research",
        metadata={"trigger": "weather improved"},
        event_type="seeded",
        event_at="2026-03-17T00:00:00+00:00",
    )

    promotion_workflow = _load_promotion_workflow()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "promotion-workflow.py",
            "--path",
            str(registry_path),
            "promote",
            "exp-1",
            "--to",
            "shadow",
            "--actor",
            "andes",
            "--note",
            "start shadow rollout",
            "--incident-id",
            "INC-17",
            "--config-version",
            "cfg-17",
            "--model-version",
            "mdl-17",
            "--pr-number",
            "38",
            "--change-ref",
            "config/calibration-backup.json",
        ],
    )

    promotion_workflow.main()
    output = json.loads(capsys.readouterr().out)

    assert output["promotion_stage"] == "shadow"
    state = json.loads(registry_path.read_text())
    assert state["entries"]["exp-1"]["history"][-1]["event_type"] == "promoted_to_shadow"
    assert state["entries"]["exp-1"]["history"][-1]["metadata"]["incident_id"] == "INC-17"
    assert state["entries"]["exp-1"]["history"][-1]["metadata"]["config_version"] == "cfg-17"
