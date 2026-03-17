"""Direct tests for the research.incident_registry module."""

import json

import pytest

from research.incident_registry import IncidentRegistry, IncidentRegistryError


def test_incident_registry_open_link_and_close_tracks_follow_up_refs(tmp_path):
    registry = IncidentRegistry(
        tmp_path / "incident-reviews.json",
        utc_now_iso_func=lambda: "2026-03-17T00:00:00+00:00",
    )

    opened = registry.open(
        "INC-101",
        summary="NWS parser failure blocked live weather updates",
        severity="sev2",
        owner="andes",
        affected_services=["weather", "source-monitor"],
        affected_sources=["nws"],
        experiment_ids=["exp-weather-shadow"],
        note="initial mitigation started",
    )
    linked = registry.link(
        "INC-101",
        actor="andes",
        pr_numbers=[38, 38],
        config_versions=["cfg-2026-03-17"],
        model_versions=["mdl-weather-v3"],
        change_refs=["config/calibration-backup.json"],
        note="linking parser hotfix and config rollback",
    )
    closed = registry.close(
        "INC-101",
        actor="andes",
        resolution="Parser hotfix deployed and weather promotion frozen",
        pr_numbers=[38],
        change_refs=["scripts/incident-workflow.py"],
    )

    state = json.loads((tmp_path / "incident-reviews.json").read_text())
    entry = state["entries"]["INC-101"]

    assert opened["status"] == "open"
    assert opened["affected_sources"] == ["nws"]
    assert linked["pr_numbers"] == [38]
    assert linked["config_versions"] == ["cfg-2026-03-17"]
    assert linked["model_versions"] == ["mdl-weather-v3"]
    assert entry["change_refs"] == ["config/calibration-backup.json", "scripts/incident-workflow.py"]
    assert entry["history"][1]["event_type"] == "linked_follow_up"
    assert entry["history"][1]["actor"] == "andes"
    assert closed["status"] == "closed"
    assert closed["closed_at"] == "2026-03-17T00:00:00+00:00"
    assert entry["resolution"] == "Parser hotfix deployed and weather promotion frozen"


def test_incident_registry_link_requires_existing_incident(tmp_path):
    registry = IncidentRegistry(tmp_path / "incident-reviews.json")

    with pytest.raises(IncidentRegistryError, match="Unknown incident_id"):
        registry.link("INC-404", pr_numbers=[99])
